"""文档管理路由：CRUD + 异步生成 + AI/人工审核 + 选择 + 冻结/变更"""

import asyncio
import json as json_mod
import logging
import re
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from starlette.responses import StreamingResponse
from sqlalchemy import select, func, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from pathlib import Path

from app.core.database import get_session, async_session
from app.core.doc_time import doc_time_prompt_block
from app.models import Project, Document, GenerationTask, Message, StageProgress, Iteration, Task, TaskLog, ProjectAsset, TaskDocument
from app.services import ai_leader, task_docs, git_repo, token_tracker, doc_llm_sanitize
from app.services.doc_title_prefix import next_prefixed_titles_for_stage, strip_doc_title_order_prefix

logger = logging.getLogger(__name__)
REVIEW_LOCK_TIMEOUT_SECONDS = 10 * 60

router = APIRouter(prefix="/api/projects/{project_id}/documents", tags=["documents"])

# 与 git_repo.STAGE_DOC_NAMES 阶段顺序一致（此处为中文短名，用于默认文档标题）
STAGE_DOC_NAMES = ['业务方案', '需求规范', '产品原型', '技术方案', '任务分解', '编码开发', '测试验证', '部署交付']

# task_documents.ref_id 为 VARCHAR(32)，须与入库截断一致，否则删条件与磁盘 ref 对不齐
STAGE_DOC_TASK_REF_LEN = 32


def _stage_doc_task_ref_id(document_id: str) -> str:
    return (f"stage_doc:{document_id}")[:STAGE_DOC_TASK_REF_LEN]


def _default_stage_doc_core_title(stage: int) -> str:
    if 0 <= stage < len(STAGE_DOC_NAMES):
        return f"{STAGE_DOC_NAMES[stage]} 文档"
    return f"Stage {stage} 文档"


def _sanitize_llm_markdown(text: str) -> str:
    return doc_llm_sanitize.sanitize_llm_markdown(text)


def _has_tool_artifacts(text: str) -> bool:
    return doc_llm_sanitize.has_tool_artifacts(text)


def _extract_prototype_spec_json(markdown_text: str) -> dict | None:
    if not markdown_text:
        return None
    blocks = re.findall(r"```json\s*(\{[\s\S]*?\})\s*```", markdown_text, flags=re.IGNORECASE)
    for raw in blocks:
        try:
            obj = json_mod.loads(raw)
            if isinstance(obj, dict) and isinstance(obj.get("pages"), list):
                return obj
        except Exception:
            continue
    return None


async def _normalize_doc_markdown(text: str, model: str | None = None) -> str:
    """自动纠偏：先本地清洗，仍异常则再调用一次 AI 修复。"""
    cleaned = _sanitize_llm_markdown(text)
    if cleaned and not _has_tool_artifacts(cleaned):
        return cleaned

    repaired = await ai_leader._call(
        "你是一个文档清洗器。输入文本中混入了工具调用痕迹。"
        "请提取并输出最终 Markdown 正文；禁止输出任何工具调用、XML 标签、JSON（如 fsWrite）、解释和过程描述。",
        f"请清洗以下内容并仅输出最终文档正文：\n\n{cleaned or text}",
        max_tokens=16384,
        model=model,
        auto_continue=True,
        temperature=0.1,
    )
    repaired = _sanitize_llm_markdown(repaired)
    if not repaired or _has_tool_artifacts(repaired):
        raise ValueError("文档输出仍包含工具调用痕迹")
    return repaired


# ── Request Models ──

class DocGenerateReq(BaseModel):
    title: str = ""
    content: str = ""
    model: str | None = None
    category: str = ""
    tags: list[str] = []
    generation_hints: str = Field(
        default="",
        max_length=4000,
        description="根据会话生成时附加给模型的用户说明（如要求按主题拆分结构）",
    )

class DocUpdateReq(BaseModel):
    title: str | None = None
    content: str | None = None

class DocRegenerateReq(BaseModel):
    feedback: str
    model: str | None = None

class AIReviewReq(BaseModel):
    model: str | None = None
    reviewer_context: str = Field(
        default="",
        max_length=8000,
        description="用户对本次审核的补充说明：项目现状、阶段目标、勿扩大范围等（可选）",
    )

class ManualReviewReq(BaseModel):
    action: str  # approve / reject
    comments: str = ""

class ChangeRequestReq(BaseModel):
    description: str
    priority: int = 0


# ── 文档列表 ──

@router.get("")
async def list_documents(project_id: str, stage: int | None = None, iteration_id: str | None = None, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    iter_id = iteration_id or project.current_iteration_id

    q = select(Document).where(Document.project_id == project_id, Document.iteration_id == iter_id)
    if stage is not None:
        q = q.where(Document.stage == stage)
    q = q.order_by(Document.stage, Document.created_at.desc())
    result = await session.execute(q)
    return [_doc_to_dict(d) for d in result.scalars()]


@router.get("/{doc_id}")
async def get_document(project_id: str, doc_id: str, session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")
    return _doc_to_dict(doc)


# ── 生成文档（异步后台任务） ──

@router.post("/stages/{stage}/generate")
async def generate_document(
    project_id: str, stage: int, body: DocGenerateReq,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    if stage > 3:
        raise HTTPException(400, "Stage 0-3 only")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    iteration_id = project.current_iteration_id

    raw_title = (body.title or "").strip()
    core = strip_doc_title_order_prefix(raw_title) if raw_title else _default_stage_doc_core_title(stage)
    if not core.strip():
        core = _default_stage_doc_core_title(stage)
    prefixed_title = (
        await next_prefixed_titles_for_stage(session, project_id, iteration_id, stage, [core])
    )[0]

    if body.content:
        from app.core.constants import STAGE_DEFAULT_CATEGORY
        doc = Document(
            project_id=project_id, iteration_id=iteration_id, stage=stage,
            title=prefixed_title,
            content=body.content, status="draft",
            category=body.category or STAGE_DEFAULT_CATEGORY.get(stage, "general"),
            tags=body.tags or [],
        )
        session.add(doc)
        await session.flush()
        await _archive_stage_doc(session, doc)
        await _archive_prototype_spec(session, doc, body.content)
        await session.commit()
        await session.refresh(doc)
        return {"document": _doc_to_dict(doc), "task_id": None}

    task = GenerationTask(
        project_id=project_id, iteration_id=iteration_id, stage=stage,
        doc_title=prefixed_title,
        status="running", model_used=body.model or "",
        steps=[
            {"name": "分析对话记录", "status": "pending"},
            {"name": "生成文档结构", "status": "pending"},
            {"name": "撰写文档内容", "status": "pending"},
            {"name": "质量自检", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    hints = (body.generation_hints or "").strip()
    background_tasks.add_task(
        _run_generation, task.id, project_id, stage, prefixed_title, body.model, iteration_id, hints,
    )

    return {"document": None, "task_id": task.id}


# ── 生成任务状态查询 ──

@router.get("/tasks/{task_id}/status")
async def get_task_status(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "Task not found")
    result = {
        "id": task.id, "status": task.status, "progress": task.progress,
        "steps": task.steps, "error_message": task.error_message,
        "document_id": task.document_id,
        "doc_title": task.doc_title,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
    if task.document_id:
        doc = await session.get(Document, task.document_id)
        if doc:
            result["document"] = _doc_to_dict(doc)
    return result


# ── 取消生成任务 ──

@router.post("/tasks/{task_id}/cancel")
async def cancel_task(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "Task not found")
    if task.status in ("completed", "failed", "cancelled"):
        return {"cancelled": False, "reason": f"Task already {task.status}"}
    task.status = "cancelled"
    task.error_message = "用户手动取消"
    task.completed_at = datetime.now(timezone.utc)
    for i, step in enumerate(task.steps):
        if step["status"] == "running":
            task.steps[i]["status"] = "failed"
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "steps")
    await session.commit()
    return {"cancelled": True}


# ── SSE 任务进度推送 ──

@router.get("/tasks/{task_id}/stream")
async def stream_task_status(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "Task not found")

    async def event_generator():
        while True:
            async with async_session() as s:
                t = await s.get(GenerationTask, task_id)
                if not t:
                    return
                data: dict = {
                    "status": t.status, "progress": t.progress,
                    "steps": t.steps, "doc_title": t.doc_title,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                if t.status == "completed":
                    data["completed_at"] = t.completed_at.isoformat() if t.completed_at else None
                    data["error_message"] = t.error_message
                    data["document_id"] = t.document_id
                    if t.document_id:
                        doc = await s.get(Document, t.document_id)
                        if doc:
                            data["document"] = _doc_to_dict(doc)
                    yield f"event: completed\ndata: {json_mod.dumps(data, ensure_ascii=False)}\n\n"
                    return
                elif t.status in ("failed", "cancelled"):
                    data["error_message"] = t.error_message
                    yield f"event: failed\ndata: {json_mod.dumps(data, ensure_ascii=False)}\n\n"
                    return
                else:
                    yield f"event: progress\ndata: {json_mod.dumps(data, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 查询当前阶段进行中的任务 ──

@router.get("/stages/{stage}/active-task")
async def get_active_task(project_id: str, stage: int, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(GenerationTask)
        .where(GenerationTask.project_id == project_id, GenerationTask.stage == stage,
               GenerationTask.status.in_(["pending", "running"]))
        .order_by(GenerationTask.created_at.desc())
        .limit(1)
    )
    task = q.scalar_one_or_none()
    if not task:
        return {"task": None}
    return {"task": {
        "id": task.id, "status": task.status, "progress": task.progress,
        "steps": task.steps, "document_id": task.document_id,
        "doc_title": task.doc_title or "",
    }}


# ── 更新文档（手动编辑） ──

def _is_prototype_realignment_allowed(doc: Document, project: Project) -> bool:
    """技术方案已定稿后，允许重新打开 Stage2 产品原型以对齐实现，无需整阶段回退。"""
    return doc.stage == 2 and project.current_stage in (3, 4)


def _is_doc_frozen(doc: Document, project: Project) -> bool:
    """文档是否被冻结：已通过审核且阶段已推进（Stage2 对齐窗口除外）"""
    if doc.status != "approved":
        return False
    if doc.stage >= project.current_stage:
        return False
    if _is_prototype_realignment_allowed(doc, project):
        return False
    return True


async def _abort_executing_tasks(session: AsyncSession, doc: Document) -> list[str]:
    """终止正在执行中的、引用了该文档所在阶段的任务"""
    q = await session.execute(
        select(Task).where(
            Task.project_id == doc.project_id,
            Task.iteration_id == doc.iteration_id,
            Task.status.in_(["assigned", "executing"]),
        )
    )
    aborted = []
    for task in q.scalars():
        task.status = "pending"
        task.assigned_agent = None
        task.retry_count = 0
        session.add(TaskLog(
            task_id=task.id, agent_id="system", action="aborted",
            message=f"文档 [{doc.title}] 被修改，任务终止并等待重新分派",
        ))
        aborted.append(task.ref_id or task.id)
    return aborted


@router.put("/{doc_id}")
async def update_document(project_id: str, doc_id: str, body: DocUpdateReq, session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    if _is_doc_frozen(doc, project):
        raise HTTPException(
            403,
            f"文档已冻结（Stage {doc.stage} 已审核通过并推进到下一阶段）。"
            f"产品原型（Stage2）可在技术方案/任务分解阶段直接修订对齐；其他文档如需修改请使用变更流程 POST /{doc_id}/change-request",
        )

    aborted_tasks: list[str] = []
    if body.content is not None and doc.stage >= 4:
        aborted_tasks = await _abort_executing_tasks(session, doc)

    if body.title is not None:
        doc.title = body.title
    if body.content is not None:
        doc.content = body.content
        doc.status = "draft"
        doc.review_result = {}
        doc.reviewed_by = ""
    await session.commit()
    if body.content is not None:
        await _archive_stage_doc(session, doc)

    result = _doc_to_dict(doc)
    if aborted_tasks:
        result["aborted_tasks"] = aborted_tasks
        result["message"] = f"文档已更新，{len(aborted_tasks)} 个执行中的任务已终止并等待重新分派"
    return result


# ── 变更流程：对冻结文档提交变更请求 ──

@router.post("/{doc_id}/change-request")
async def create_change_request(
    project_id: str, doc_id: str, body: ChangeRequestReq,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    project.task_seq = (project.task_seq or 0) + 1
    ref_id = f"TASK-{project.task_seq:03d}"

    task = Task(
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        ref_id=ref_id,
        title=f"[变更] {doc.title}",
        description=(
            f"## 变更请求\n\n"
            f"**原文档**: {doc.title} (Stage {doc.stage}, v{doc.version})\n\n"
            f"**变更说明**:\n{body.description}\n\n"
            f"---\n\n请根据变更说明修改相关代码和文档。"
        ),
        type="change",
        priority=body.priority or 5,
        suggested_role="architect",
        ref_docs=[{"doc_id": doc.id, "title": doc.title, "stage": doc.stage}],
        git_branch=f"change/{ref_id}-{doc.title[:20].replace(' ', '-').lower()}",
        context={"change_request": True, "source_doc_id": doc.id, "source_stage": doc.stage},
    )
    session.add(task)
    session.add(TaskLog(
        task_id=task.id, agent_id="system", action="created",
        message=f"变更请求：{body.description[:200]}",
    ))
    await session.commit()
    await session.refresh(task)

    return {
        "task_id": task.id,
        "ref_id": ref_id,
        "title": task.title,
        "message": f"已创建变更任务 {ref_id}，将由架构师评估后执行",
    }


# ── 重新生成（带修改意见） ──

@router.post("/{doc_id}/regenerate")
async def regenerate_document(
    project_id: str, doc_id: str, body: DocRegenerateReq,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    iteration_id = project.current_iteration_id

    # 重生成：作废与本稿绑定的评审结论，避免新版本与旧 AI/人工审核、旧「审核说明」错配（version 在任务完成时 +1）
    doc.status = "draft"
    doc.review_result = {}
    doc.reviewed_by = ""
    doc.is_selected = False
    await session.commit()
    await _archive_stage_doc(session, doc)

    task = GenerationTask(
        project_id=project_id, iteration_id=iteration_id, stage=doc.stage,
        doc_title=doc.title, document_id=doc.id,
        status="running", model_used=body.model or "",
        steps=[
            {"name": "分析修改意见", "status": "pending"},
            {"name": "重新生成文档", "status": "pending"},
            {"name": "质量自检", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    background_tasks.add_task(_run_regeneration, task.id, doc.id, doc.stage, doc.content, body.feedback, body.model, iteration_id)

    return {"task_id": task.id}


# ── 删除文档 ──

@router.delete("/{doc_id}")
async def delete_document(project_id: str, doc_id: str, session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    if doc.status == "approved":
        raise HTTPException(403, "审核通过的文档不能删除")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    if _is_doc_frozen(doc, project):
        raise HTTPException(403, "文档已冻结，无法删除")

    if doc.stage != project.current_stage:
        raise HTTPException(400, "只能删除当前阶段的文档")

    await session.delete(doc)
    await session.commit()
    return {"deleted": True}


async def _running_generation_task_prefix(
    session: AsyncSession, project_id: str, doc_id: str, title_prefix: str,
) -> bool:
    r = await session.execute(
        select(GenerationTask.id).where(
            GenerationTask.project_id == project_id,
            GenerationTask.document_id == doc_id,
            GenerationTask.status.in_(["pending", "running"]),
            GenerationTask.doc_title.like(f"{title_prefix}%"),
        ).limit(1)
    )
    return r.scalar_one_or_none() is not None


# ── AI 审核 ──

@router.post("/{doc_id}/review/ai")
async def ai_review(
    project_id: str,
    doc_id: str,
    body: AIReviewReq,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")
    if await _running_generation_task_prefix(session, project_id, doc.id, "[APPLY]"):
        raise HTTPException(409, "该文档正在应用讨论修改，请完成后再进行 AI 审核")
    if not doc.content:
        raise HTTPException(400, "文档内容为空，无法审核")
    if doc.status == "under_review":
        # under_review + review_result为空：视为进行中（或残留锁）
        # under_review + review_result非空：视为上次审核已完成但状态未复位，自动释放并允许重审
        rr = doc.review_result or {}
        lock_ts = doc.updated_at or doc.created_at or datetime.now(timezone.utc)
        lock_timed_out = datetime.now(timezone.utc) - lock_ts >= timedelta(seconds=REVIEW_LOCK_TIMEOUT_SECONDS)
        if (isinstance(rr, dict) and rr) or lock_timed_out:
            doc.status = "draft"
            doc.review_result = {}
            doc.reviewed_by = ""
            doc.is_selected = False
            await session.commit()
        else:
            raise HTTPException(409, "该文档正在 AI 审核中，请稍后查看结果")

    active_review = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.document_id == doc.id,
            GenerationTask.status.in_(["pending", "running"]),
            GenerationTask.doc_title.like("[REVIEW]%"),
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    existing_task = active_review.scalar_one_or_none()
    if existing_task:
        raise HTTPException(409, "该文档正在 AI 审核中，请稍后查看结果")

    doc.status = "under_review"
    from app.services.model_pool import resolve_model
    review_model = body.model or resolve_model("architect")
    task = GenerationTask(
        project_id=project_id,
        iteration_id=doc.iteration_id,
        stage=doc.stage,
        doc_title=f"[REVIEW] {doc.title}",
        document_id=doc.id,
        status="running",
        model_used=review_model,
        steps=[
            {"name": "准备审核任务", "status": "pending"},
            {"name": "调用 AI 评审", "status": "pending"},
            {"name": "写入审核结果", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    ctx = (body.reviewer_context or "").strip()
    background_tasks.add_task(_run_ai_review_task, task.id, project_id, doc.id, review_model, ctx)
    return {"task_id": task.id, "status": "running"}


# ── 强制解锁 AI 审核 ──

@router.post("/{doc_id}/review/force-unlock")
async def force_unlock_ai_review(project_id: str, doc_id: str, session: AsyncSession = Depends(get_session)):
    """
    强制释放文档审核锁（小团队运维兜底接口，不做复杂权限分级）。
    仅当文档处于 under_review 时执行解锁。
    """
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    if doc.status != "under_review":
        return {
            "unlocked": False,
            "status": doc.status,
            "message": "文档当前不在审核中，无需解锁",
        }

    doc.status = "draft"
    doc.review_result = {}
    doc.reviewed_by = ""
    doc.is_selected = False
    await session.commit()
    await _archive_stage_doc(session, doc)

    return {
        "unlocked": True,
        "status": doc.status,
        "message": "已强制解锁，可重新发起 AI 审核",
    }


async def _post_manual_review_side_effects(project_id: str, doc_id: str) -> None:
    """审核落库后的 Git 同步与检索归档（长文档会先调 LLM 写摘要），耗时不应阻塞 HTTP。"""
    async with async_session() as session:
        doc = await session.get(Document, doc_id)
        if not doc or doc.project_id != project_id:
            return
        try:
            if doc.status == "approved":
                await _sync_doc_to_git(session, doc)
            await _archive_stage_doc(session, doc)
        except Exception as e:
            logger.warning("manual review follow-up failed doc=%s: %s", doc_id, e)


# ── 人工审核 ──

@router.post("/{doc_id}/review/manual")
async def manual_review(
    project_id: str,
    doc_id: str,
    body: ManualReviewReq,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    if await _running_generation_task_prefix(session, project_id, doc.id, "[APPLY]"):
        raise HTTPException(409, "该文档正在应用讨论修改，请完成后再进行人工审核")

    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'")

    doc.status = "approved" if body.action == "approve" else "rejected"
    doc.reviewed_by = "manual"
    doc.review_result = {"approved": body.action == "approve", "summary": body.comments, "issues": [], "score": 10 if body.action == "approve" else 0}
    await session.commit()

    background_tasks.add_task(_post_manual_review_side_effects, project_id, doc_id)

    return {"status": doc.status, "review": doc.review_result}


# ── 撤回审核（重新审核） ──

@router.post("/{doc_id}/unreview")
async def unreview_document(project_id: str, doc_id: str, session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    if _is_doc_frozen(doc, project):
        raise HTTPException(403, "文档已冻结（阶段已推进），无法撤回审核")

    if doc.stage != project.current_stage and not _is_prototype_realignment_allowed(doc, project):
        raise HTTPException(400, "只能对当前阶段的文档撤回审核")

    doc.status = "draft"
    doc.review_result = {}
    doc.reviewed_by = ""
    doc.is_selected = False
    await session.commit()
    await _archive_stage_doc(session, doc)

    return _doc_to_dict(doc)


# ── 选为下一阶段输入 ──

@router.post("/{doc_id}/select")
async def select_document(project_id: str, doc_id: str, session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")
    if doc.status != "approved":
        raise HTTPException(400, "只有审核通过的文档才能选为下一阶段输入")

    q = await session.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.iteration_id == doc.iteration_id,
            Document.stage == doc.stage,
            Document.is_selected == True,
        )
    )
    for d in q.scalars():
        d.is_selected = False

    doc.is_selected = True
    await session.commit()

    return {"selected": True}


# ── 文档移动到其他阶段 ──

class MoveDocRequest(BaseModel):
    target_stage: int


@router.post("/{doc_id}/move")
async def move_document(project_id: str, doc_id: str, body: MoveDocRequest, session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")
    if doc.status == "approved":
        raise HTTPException(400, "审核通过的文档已锁定，无法移动")
    if body.target_stage == doc.stage:
        return _doc_to_dict(doc)

    doc.stage = body.target_stage
    doc.is_selected = False
    await session.commit()
    await _archive_stage_doc(session, doc)
    return _doc_to_dict(doc)


# ── 针对文档的临时讨论（不存消息，流式返回） ──

class DocChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    model: str | None = None


DOC_CHAT_SYSTEM = """你是一个专业的文档审阅助手。用户正在审阅一份文档，会针对文档内容提出修改意见。

{knowledge_index}

## 当前文档
标题：{title}

{content}

## AI 评审结果（若已生成）
{review_context}

你的职责：
- 基于项目知识回答用户关于文档的问题，如需更多知识可使用 [NEED_CONTEXT:key] 或 [SEARCH:关键词]
- **必须结合上文「AI 评审结果」**（若有）回应用户；用户消息中可能出现 `[评审条目 Rn]`，表示指向该节编号为 n 的具体意见
- 针对用户的修改意见给出具体的分析和建议；**支持多轮深入**：可追问澄清、展开利弊、细化到可落笔的段落或表格改法
- 文档中的 **「待论证 / 待补充」** 小节：用户逐条补充信息时，应给出**如何写入正文**、如何更新报价/边界/指标的明确建议；补全后可提示用户用「重新生成」或「应用讨论到文档」刷新成稿
- 如果用户要求修改，说明应该怎么改及原因
- 保持讨论聚焦在这份文档上
- 语言简洁专业"""


def _format_review_context_for_chat(doc: Document) -> str:
    """将 review_result 格式化为讨论系统提示中的结构化文本（与前端 [评审条目 Rn] 编号一致）。"""
    rr = doc.review_result
    if not rr or not isinstance(rr, dict):
        return "（当前文档尚无 AI 评审记录。）"

    lines: list[str] = []
    rb = doc.reviewed_by or ""
    if rb:
        lines.append(f"评审来源：{rb}")

    score = rr.get("score")
    if score is not None:
        lines.append(f"评分：{score}/10")

    rt = rr.get("recommendation_text") or ""
    if not rt and rr.get("recommendation"):
        rt = "建议通过" if rr["recommendation"] == "approve" else "建议拒绝"
    if isinstance(rr.get("approved"), bool) and not rt:
        rt = "建议通过" if rr["approved"] else "建议拒绝"
    if rt:
        lines.append(f"结论：{rt}")

    summary = (rr.get("summary") or "").strip()
    if summary:
        lines.append(f"摘要：{summary}")

    issues = rr.get("issues")
    if isinstance(issues, list) and issues:
        lines.append("具体问题（编号供用户引用）：")
        for i, raw in enumerate(issues, start=1):
            if not isinstance(raw, dict):
                continue
            sev = raw.get("severity") or ""
            sec = (raw.get("section") or "").strip()
            desc = (raw.get("description") or "").strip()
            if not desc:
                continue
            sec_part = f" 章节「{sec}」" if sec else ""
            lines.append(f"- [R{i}] [{sev}]{sec_part} {desc}")

    body = "\n".join(lines).strip()
    if not body or (rb and body == f"评审来源：{rb}"):
        return "（当前文档尚无 AI 评审记录。）"
    return body


@router.post("/{doc_id}/chat")
async def chat_on_document(project_id: str, doc_id: str, body: DocChatRequest, session: AsyncSession = Depends(get_session)):
    """针对文档的临时流式对话，不存储消息"""
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    from app.services import token_tracker
    from app.services.project_context import build_knowledge_index
    token_tracker.set_context(project_id=project_id)

    knowledge_idx = await build_knowledge_index(
        session,
        project_id,
        include_experiences=True,
        active_stage=doc.stage,
    )
    review_ctx = _format_review_context_for_chat(doc)
    system = DOC_CHAT_SYSTEM.format(
        title=doc.title,
        content=doc.content or "",
        knowledge_index=knowledge_idx,
        review_context=review_ctx,
    )
    messages = [m for m in body.history if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": body.message})

    async def sse():
        try:
            async for token in ai_leader.stream_with_knowledge(
                system, messages, project_id=project_id, session=session,
                active_stage=doc.stage,
                max_tokens=8192, model=body.model, temperature=0.4,
                stream_auto_continue=True,
            ):
                yield f"data: {json_mod.dumps(token, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json_mod.dumps({'error': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class DocApplyRequest(BaseModel):
    history: list[dict]
    model: str | None = None


DOC_APPLY_SYSTEM = """你是一个文档修改专家。根据用户与审阅助手的讨论，重写文档。

{doc_time_block}
## 项目信息
{project_context}

## 原始文档
标题：{title}

{content}

以下是用户与审阅助手的讨论记录，请根据讨论中达成的共识修改文档。

要求：
- 输出完整的修改后文档（Markdown 格式）
- 只输出文档内容，不要输出解释
- 保留没有问题的部分
- 确保修改符合讨论中的所有要求
- 可以引用项目信息中的具体细节来充实文档"""


@router.post("/{doc_id}/apply-discussion")
async def apply_discussion(
    project_id: str,
    doc_id: str,
    body: DocApplyRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """根据讨论结果重写文档（后台长任务，与生成/审核共用 SSE 进度）"""
    doc = await session.get(Document, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404, "Document not found")

    active_apply = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.document_id == doc.id,
            GenerationTask.status.in_(["pending", "running"]),
            GenerationTask.doc_title.like("[APPLY]%"),
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    if active_apply.scalar_one_or_none():
        raise HTTPException(409, "该文档正在应用讨论修改，请稍候")

    if await _running_generation_task_prefix(session, project_id, doc.id, "[REVIEW]"):
        raise HTTPException(409, "该文档正在 AI 审核中，请完成后再应用讨论修改")

    from app.services.model_pool import resolve_model as _resolve
    model_used = body.model or _resolve("leader")
    history = [m for m in body.history if m.get("role") in ("user", "assistant")]
    task = GenerationTask(
        project_id=project_id,
        iteration_id=doc.iteration_id,
        stage=doc.stage,
        doc_title=f"[APPLY] {doc.title}",
        document_id=doc.id,
        status="running",
        model_used=model_used,
        steps=[
            {"name": "准备上下文", "status": "pending"},
            {"name": "AI 重写文档", "status": "pending"},
            {"name": "格式化与保存", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    background_tasks.add_task(_run_apply_discussion_task, task.id, project_id, doc.id, history, body.model)
    return {"task_id": task.id, "status": "running"}


# ── 阶段文档归档到 TaskDocument（纳入统一检索） ──

async def _sync_doc_to_git(session: AsyncSession, doc: Document):
    """文档审核通过后同步到 Git 仓库（失败只打日志；未配置远程地址则整段跳过，不做 clone）。"""
    try:
        project = await session.get(Project, doc.project_id)
        if not project or not (project.git_repo or "").strip():
            return

        iter_seq = "v1"
        if doc.iteration_id:
            it = await session.get(Iteration, doc.iteration_id)
            if it:
                iter_seq = f"v{it.seq}" if isinstance(it.seq, int) else str(it.seq)

        await git_repo.ensure_repo(project.id, project.git_repo)
        file_path = git_repo.doc_file_path(doc.stage, doc.title, iter_seq)
        stage_name = git_repo.STAGE_DOC_NAMES[doc.stage] if doc.stage < len(git_repo.STAGE_DOC_NAMES) else f"stage-{doc.stage}"
        commit_msg = git_repo.build_commit_message(
            "docs", f"{doc.title} (v{doc.version})",
            scope=stage_name,
            iteration=iter_seq,
            stage=doc.stage,
            author="ai/leader",
        )
        result = await git_repo.commit_and_push(
            project.id, file_path, doc.content,
            message=commit_msg,
        )

        if result["ok"]:
            doc.git_path = file_path
            await session.commit()
            logger.info(f"Doc {doc.id} synced to git: {file_path} ({result.get('action')})")
        else:
            logger.warning(f"Doc {doc.id} git sync failed: {result.get('error', result)}")
    except Exception as e:
        logger.warning(f"Doc git sync error (non-blocking): {e}")


async def _generate_doc_summary(content: str, title: str) -> str:
    """为较长文档生成 AI 检索摘要，短文档留空走 task_docs 本地截取"""
    if len(content) < 500:
        return ""
    try:
        result = await ai_leader._call(
            "你是文档摘要生成器。只输出摘要正文，不加标题或前缀。",
            f"请为以下文档生成一段200字以内的检索友好摘要，涵盖核心要点和关键结论。\n\n"
            f"文档标题：{title}\n\n{content[:6000]}",
            max_tokens=400, temperature=0.2,
        )
        return result.strip()[:400]
    except Exception as e:
        logger.warning(f"AI summary generation failed (fallback to local): {e}")
        return ""


async def _archive_stage_doc(session: AsyncSession, doc: Document):
    """将阶段文档同步归档到 TaskDocument，纳入统一检索体系。
    仅 approved 文档入库；其他状态从索引中移除。
    """
    try:
        ref_key = _stage_doc_task_ref_id(doc.id)
        await session.execute(
            delete(TaskDocument).where(
                TaskDocument.project_id == doc.project_id,
                TaskDocument.doc_type == "stage_document",
                or_(
                    TaskDocument.metadata_.contains({"document_id": doc.id}),
                    TaskDocument.ref_id == ref_key,
                ),
            )
        )
        await session.commit()
        if doc.status != "approved":
            return

        summary = await _generate_doc_summary(doc.content or "", doc.title or "")

        iter_seq = "default"
        if doc.iteration_id:
            it = await session.get(Iteration, doc.iteration_id)
            if it:
                iter_seq = it.seq

        stage_label = STAGE_DOC_NAMES[doc.stage] if doc.stage < len(STAGE_DOC_NAMES) else f"Stage {doc.stage}"
        await task_docs.archive(
            session,
            project_id=doc.project_id,
            iteration_id=doc.iteration_id,
            iteration_seq=iter_seq,
            ref_id=ref_key,
            doc_type="stage_document",
            title=f"[{stage_label}] {doc.title}",
            content=doc.content,
            summary=summary,
            tags=["stage_document", f"stage-{doc.stage}", stage_label],
            metadata={
                "stage": doc.stage,
                "document_id": doc.id,
                "version": doc.version,
                "git_synced": bool(doc.git_path),
                "git_path": doc.git_path or "",
            },
        )
    except Exception as e:
        logger.warning(f"Stage doc archive failed (non-blocking): {e}")


async def _archive_prototype_spec(session: AsyncSession, doc: Document, markdown_content: str):
    if doc.stage != 2:
        return
    spec = _extract_prototype_spec_json(markdown_content)
    await session.execute(
        delete(TaskDocument).where(
            TaskDocument.project_id == doc.project_id,
            TaskDocument.iteration_id == doc.iteration_id,
            TaskDocument.doc_type == "prototype_spec",
            TaskDocument.metadata_.contains({"document_id": doc.id}),
        )
    )
    await session.commit()
    if not spec:
        return

    iter_seq = "default"
    if doc.iteration_id:
        it = await session.get(Iteration, doc.iteration_id)
        if it:
            iter_seq = it.seq

    content = json_mod.dumps(spec, ensure_ascii=False, indent=2)
    await task_docs.archive(
        session,
        project_id=doc.project_id,
        iteration_id=doc.iteration_id,
        iteration_seq=iter_seq,
        ref_id=f"prototype_spec:{doc.id}",
        doc_type="prototype_spec",
        title=f"[原型规格] {doc.title}",
        content=content,
        summary="Stage2 原型规格（UI Spec JSON）",
        tags=["prototype_spec", "stage-2", "ui-spec"],
        metadata={
            "stage": doc.stage,
            "document_id": doc.id,
            "version": doc.version,
        },
    )


def _read_file_safe(path: Path, max_chars: int = 50000) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = path.read_text(encoding=enc)
            return text[:max_chars]
        except (UnicodeDecodeError, OSError):
            continue
    return ""


_ASSET_MAX_CHARS = 40_000

async def _build_asset_context(session: AsyncSession, project_id: str, stage: int) -> str:
    q = await session.execute(
        select(ProjectAsset).where(
            ProjectAsset.project_id == project_id,
            ProjectAsset.status == "analyzed",
        )
    )
    parts = []
    api_spec_raw = ""
    for asset in q.scalars():
        label = "项目代码分析" if asset.asset_type == "code" else "API 规范审核"
        purpose = ""
        if asset.asset_type == "code":
            purpose = "（目的：维护此项目）" if asset.purpose == "maintain" else "（目的：学习代码风格）"
        summary = asset.summary or ""
        if len(summary) > _ASSET_MAX_CHARS:
            summary = summary[:_ASSET_MAX_CHARS] + "\n\n...(摘要过长已截断)"
        parts.append(f"### {label}{purpose}\n\n{summary}")

        if asset.asset_type == "api_spec" and stage == 3 and asset.file_path:
            store_dir = Path(asset.file_path)
            if store_dir.is_dir():
                for f in store_dir.iterdir():
                    if f.is_file():
                        text = _read_file_safe(f)
                        if text:
                            api_spec_raw += f"\n\n=== {f.name} ===\n{text}"

    ctx = "\n\n---\n\n".join(parts)
    if api_spec_raw:
        if len(api_spec_raw) > _ASSET_MAX_CHARS:
            api_spec_raw = api_spec_raw[:_ASSET_MAX_CHARS] + "\n\n...(API 规范过长已截断，完整内容请参考原始文件)"
        ctx += (
            "\n\n---\n\n### ⚠️ 用户提供的 API 规范（参考资料）\n\n"
            "以下是用户已上传的 API 相关原文，作为技术方案的**参考输入**。\n\n"
            "**在技术方案文档中应如何吸收（与平台约定一致）：**\n"
            "- 从中提炼并写清**全局约定**：统一响应包装、错误码分段与规则、版本与鉴权、分页/幂等等横切规则\n"
            "- **不要**据此扩展成完整业务接口目录（上传、注册、各域 CRUD 等）；具体路由在详细设计/编码阶段按需增加\n"
            "- 若规范原文里已有大量具体端点，技术方案里可只概括「遵循现有风格」，不必复述全表\n"
            "- 逐字段 Schema、全量端点清单属于编码阶段维护的接口文档，不是本技术方案的职责\n\n"
            f"{api_spec_raw}"
        )
    return ctx


# ── 后台任务执行 ──

async def _run_generation(
    task_id: str,
    project_id: str,
    stage: int,
    title: str,
    model: str | None,
    iteration_id: str | None,
    generation_hints: str = "",
):
    token_tracker.set_context(project_id=project_id)
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        project = await session.get(Project, project_id)
        pt = project.project_type if project else "new"
        rr = project.rewrite_reason if project else ""
        ts = project.target_tech_stack if project else ""
        try:
            await _update_step(session, task, 0, "running")
            await asyncio.sleep(1)

            history = await _get_message_history(session, project_id, stage)
            previous_docs = await _collect_previous_docs(session, project_id, stage, iteration_id)
            asset_ctx = await _build_asset_context(session, project_id, stage)

            await _update_step(session, task, 0, "completed")
            await _update_step(session, task, 1, "running")
            await asyncio.sleep(0.5)

            await _update_step(session, task, 1, "completed")
            await _update_step(session, task, 2, "running")

            content = await ai_leader.generate_stage_document(
                stage, history, previous_docs, title=title, model=model,
                asset_context=asset_ctx,
                project_type=pt, rewrite_reason=rr, tech_stack=ts,
                generation_hints=generation_hints,
            )
            content = await _normalize_doc_markdown(content, model=model)

            await _update_step(session, task, 2, "completed")
            await _update_step(session, task, 3, "running")
            await asyncio.sleep(0.5)

            from app.core.constants import STAGE_DEFAULT_CATEGORY
            from app.services.model_pool import resolve_model

            document_title = (title or "").strip() or (task.doc_title or "").strip()
            if not document_title:
                core = _default_stage_doc_core_title(stage)
                document_title = (
                    await next_prefixed_titles_for_stage(session, project_id, iteration_id, stage, [core])
                )[0]
            elif not re.match(r"^\d{4}\s+", document_title):
                core = strip_doc_title_order_prefix(document_title) or document_title
                document_title = (
                    await next_prefixed_titles_for_stage(session, project_id, iteration_id, stage, [core])
                )[0]

            doc = Document(
                project_id=project_id, iteration_id=iteration_id, stage=stage,
                title=document_title,
                content=content, status="draft",
                category=STAGE_DEFAULT_CATEGORY.get(stage, "general"),
                generated_model=model or resolve_model("leader"),
            )
            session.add(doc)
            await session.flush()

            await _archive_stage_doc(session, doc)
            await _archive_prototype_spec(session, doc, content)

            task.document_id = doc.id
            await _update_step(session, task, 3, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()

        except TaskCancelled:
            logger.info(f"Generation task {task_id} cancelled by user")
            return
        except Exception as e:
            logger.error(f"Generation task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


async def _run_regeneration(task_id: str, doc_id: str, stage: int, original_content: str, feedback: str, model: str | None, iteration_id: str | None):
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        token_tracker.set_context(project_id=task.project_id)
        project = await session.get(Project, task.project_id)
        pt = project.project_type if project else "new"
        ts = project.target_tech_stack if project else ""
        try:
            await _update_step(session, task, 0, "running")
            await asyncio.sleep(0.5)
            await _update_step(session, task, 0, "completed")
            await _update_step(session, task, 1, "running")

            content = await ai_leader.regenerate_document(stage, original_content, feedback, model=model, project_type=pt, tech_stack=ts)
            content = await _normalize_doc_markdown(content, model=model)

            await _update_step(session, task, 1, "completed")
            await _update_step(session, task, 2, "running")
            await asyncio.sleep(0.5)

            doc = await session.get(Document, doc_id)
            if doc:
                from app.services.model_pool import resolve_model as _resolve
                doc.content = content
                doc.version += 1
                doc.status = "draft"
                doc.review_result = {}
                doc.reviewed_by = ""
                doc.generated_model = model or _resolve("leader")
                await _archive_stage_doc(session, doc)
                await _archive_prototype_spec(session, doc, content)

            await _update_step(session, task, 2, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()

        except TaskCancelled:
            logger.info(f"Regeneration task {task_id} cancelled by user")
            return
        except Exception as e:
            logger.error(f"Regeneration task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


async def _run_apply_discussion_task(
    task_id: str, project_id: str, doc_id: str, history: list[dict], model: str | None,
):
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        doc = await session.get(Document, doc_id)
        if not task or not doc:
            return
        token_tracker.set_context(project_id=project_id)
        from app.services.project_context import get_project_context
        from app.services.model_pool import resolve_model as _resolve
        try:
            await _update_step(session, task, 0, "running")
            project_ctx = await get_project_context(session, project_id)
            system = DOC_APPLY_SYSTEM.format(
                title=doc.title,
                content=doc.content,
                project_context=project_ctx,
                doc_time_block=doc_time_prompt_block(),
            )
            discussion = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
                for m in history
            )
            await _update_step(session, task, 0, "completed")
            await _update_step(session, task, 1, "running")

            new_content = await ai_leader._call(
                system, f"讨论记录：\n{discussion}\n\n请根据以上讨论重写文档。",
                max_tokens=16384, model=model, auto_continue=True, temperature=0.3,
            )
            new_content = await _normalize_doc_markdown(new_content, model=model)

            await _update_step(session, task, 1, "completed")
            await _update_step(session, task, 2, "running")

            doc.content = new_content
            doc.version += 1
            doc.status = "draft"
            doc.review_result = {}
            doc.reviewed_by = ""
            doc.generated_model = model or _resolve("leader")
            await _archive_stage_doc(session, doc)

            await _update_step(session, task, 2, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()

        except TaskCancelled:
            logger.info(f"Apply discussion task {task_id} cancelled by user")
            return
        except Exception as e:
            logger.error(f"Apply discussion task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


async def _run_ai_review_task(
    task_id: str, project_id: str, doc_id: str, model: str | None, reviewer_context: str = "",
):
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        doc = await session.get(Document, doc_id)
        if not task or not doc:
            return
        token_tracker.set_context(project_id=project_id)
        from app.services.model_pool import resolve_model
        from app.services.project_context import build_ai_review_background

        review_model = model or resolve_model("architect")
        try:
            await _update_step(session, task, 0, "running")
            await asyncio.sleep(0.2)
            await _update_step(session, task, 0, "completed")
            await _update_step(session, task, 1, "running")

            bg = await build_ai_review_background(
                session,
                project_id,
                current_stage=doc.stage,
                exclude_document_id=doc.id,
                iteration_id=doc.iteration_id,
            )

            result = await ai_leader.review_document(
                doc.stage,
                doc.content or "",
                model=review_model,
                title=doc.title or "",
                category=doc.category or "",
                project_context=bg,
                reviewer_context=reviewer_context,
            )
            if reviewer_context:
                result = {**result, "reviewer_context": reviewer_context}

            await _update_step(session, task, 1, "completed")
            await _update_step(session, task, 2, "running")

            doc.review_result = result
            doc.reviewed_by = f"AI:{review_model}"
            doc.status = "under_review"

            await _update_step(session, task, 2, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()

        except TaskCancelled:
            logger.info(f"AI review task {task_id} cancelled by user")
            return
        except Exception as e:
            logger.error(f"AI review task {task_id} failed: {e}")
            doc.status = "draft"
            doc.review_result = {}
            doc.reviewed_by = ""
            task.status = "failed"
            task.error_message = f"AI 审核失败: {type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


class TaskCancelled(Exception):
    pass


async def _update_step(session: AsyncSession, task: GenerationTask, index: int, status: str):
    await session.refresh(task)
    if task.status == "cancelled":
        raise TaskCancelled("Task cancelled by user")
    task.steps[index]["status"] = status
    task.progress = int((sum(1 for s in task.steps if s["status"] == "completed") / len(task.steps)) * 100)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "steps")
    await session.commit()


async def _get_message_history(session: AsyncSession, project_id: str, stage: int) -> list[dict]:
    q = await session.execute(
        select(Message).where(Message.project_id == project_id, Message.stage == stage).order_by(Message.created_at)
    )
    return [{"role": m.role, "content": m.content} for m in q.scalars()]


_PREV_DOCS_MAX_CHARS = 30_000

async def _collect_previous_docs(session: AsyncSession, project_id: str, up_to_stage: int, iteration_id: str | None) -> str:
    """收集前置阶段中已审核且选中的文档内容，超长自动截断"""
    parts = []
    total = 0
    for s in range(up_to_stage):
        q = await session.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.iteration_id == iteration_id,
                Document.stage == s,
                Document.status == "approved",
                Document.is_selected == True,
            )
        )
        doc = q.scalar_one_or_none()
        if not doc:
            continue
        content = doc.content or ""
        if total + len(content) > _PREV_DOCS_MAX_CHARS:
            remaining = max(0, _PREV_DOCS_MAX_CHARS - total)
            if remaining > 500:
                parts.append(f"## Stage {s}: {doc.title}\n\n{content[:remaining]}\n\n...(文档过长，已截断)")
            break
        parts.append(f"## Stage {s}: {doc.title}\n\n{content}")
        total += len(content)
    return "\n\n---\n\n".join(parts)


def _doc_to_dict(doc: Document) -> dict:
    return {
        "id": doc.id,
        "project_id": doc.project_id,
        "iteration_id": doc.iteration_id,
        "stage": doc.stage,
        "title": doc.title,
        "content": doc.content,
        "status": doc.status,
        "review_result": doc.review_result,
        "reviewed_by": doc.reviewed_by,
        "generated_model": doc.generated_model or "",
        "category": doc.category or "",
        "tags": doc.tags or [],
        "is_selected": doc.is_selected,
        "version": doc.version,
        "git_path": doc.git_path or "",
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }
