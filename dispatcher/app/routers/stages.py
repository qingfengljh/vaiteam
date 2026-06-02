"""阶段管理路由：Stage 0-3 对话 + Stage 4 任务分解 + 阶段推进"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete, or_, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session, async_session
from app.models import (
    Project, Iteration, StageProgress, Message, Document, Task, GenerationTask, TaskDocument,
    TaskLog, TaskComment, AgentMessage, TokenUsageLog, AgentTeam, Agent,
)
from app.services import ai_leader, task_docs, token_tracker, project_git_auth, scheduler
from app.services.model_pool import recommend_model_for_stage, get_all_models_with_tier, STAGE_TIER_MAP, TIER_LABELS, check_coding_readiness, task_min_tier, resolve_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/stages", tags=["stages"])


def _module_needs_subtask_breakdown(mod: Task, child_count: int) -> bool:
    """与任务看板 moduleNeedsBreakdown 一致：未完成子任务分解的模块不可进入编码阶段。"""
    ctx = mod.context or {}
    if ctx.get("subtask_breakdown_completed") is True:
        return False
    manifest = ctx.get("subtask_manifest")
    if isinstance(manifest, dict):
        items = manifest.get("items")
        if isinstance(items, list) and len(items) > 0:
            if any((isinstance(it, dict) and it.get("status") != "done") for it in items):
                return True
    if child_count > 0:
        return False
    return True


def _mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    prefix = "sk-" if key.startswith("sk-") else ""
    body = key[3:] if prefix else key
    if not body:
        return f"{prefix}***"
    start = body[:4]
    end = body[-2:] if len(body) >= 2 else ""
    return f"{prefix}{start}***{end}"


STAGE_NAMES = ["业务方案", "需求分析", "产品原型", "技术方案", "任务分解", "编码实现", "测试验证", "部署交付"]


class RetreatBody(BaseModel):
    cleanup_mode: str = "none"  # none | safe | deep
    confirm_cleanup: bool = False

STAGE_WELCOME = {
    0: """**欢迎使用 AI 协作编排系统！**

我是您的项目规划助手，将通过 **8 阶段渐进式工作流** 协助您完成项目：

> 业务方案 → 需求分析 → 产品原型 → 技术方案 → 任务分解 → 编码实现 → 测试验证 → 部署交付

---

**第一阶段：业务方案**

这是项目的第一步，我会协助您评估：
1. 这个项目值不值得做？
2. 技术上能实现吗？
3. 投入产出比如何？

请告诉我：
1. **项目名称**：这个项目叫什么？
2. **核心目标**：要解决什么业务问题？
3. **目标用户**：服务对象是谁？

我会基于您的回答，逐步引导您完成业务方案的分析。""",

    1: """**第二阶段：需求分析**

基于上一阶段的业务方案，我们来细化具体的需求。

我会帮您梳理：
- 功能需求清单（优先级排序）
- 用户故事
- 非功能需求（性能、安全、可用性）
- 数据需求和接口需求
- 验收标准

请描述您最核心的功能需求，或者我来基于业务方案帮您分析。""",

    2: """**第三阶段：产品原型**

基于需求规范，我们来设计产品的交互和界面。

我会帮您规划：
- 页面清单和布局
- 页面流程和交互逻辑
- API 接口草案
- 数据展示方案

请告诉我您对产品界面的想法，或者我来基于需求帮您设计。""",

    3: """**第四阶段：技术方案**

基于需求和产品设计，我们来制定技术实现方案。

我会帮您确定：
- 技术栈选择
- 系统架构设计
- 数据库设计
- API 详细设计
- 目录结构和安全设计

请告诉我您的技术偏好，或者我来基于前面的文档帮您规划。""",
}

MAINTAIN_STAGE_WELCOME = {
    0: """**项目维护分析**

我是您的技术分析助手。本阶段将对现有项目进行全面的维护性分析，产出《项目维护分析报告》。

**工作流程：**

1. **先聊项目背景** — 告诉我项目的业务场景、技术栈、部署方式、团队情况、您特别关注的方面等，我会记住这些信息
2. **上传代码** — 在上方通过 Git 地址或压缩包上传现有代码
3. **AI 分析** — 点击"AI 分析代码"，我会**结合您刚才提供的背景信息**进行针对性分析
4. **讨论与补充** — 分析完成后，我们一起讨论报告中的发现、补充遗漏、确认待定问题

> 💡 您在对话中提供的信息越充分，AI 分析的质量越高。比如：项目的核心业务是什么？有哪些已知的痛点？哪些模块最需要关注？

新的功能需求将在下一阶段（需求分析）讨论。本阶段专注于**理解现有项目**。

请先介绍一下您的项目。""",
}

REWRITE_STAGE_WELCOME = {
    0: """**旧系统重写模式**

我是您的代码审计专家。此项目将对旧系统进行完全重写。

**建议步骤：**

1. **先聊项目背景** — 告诉我旧系统的业务场景、当前问题、重写原因等
2. **上传代码** — 在上方的代码上传区域，通过 Git 地址或压缩包上传旧系统代码
3. **AI 审计** — 上传后点击"AI 分析代码"，我会进行深度审计（缺陷分析、业务逻辑逆向、数据结构分析）
4. **确认重写方案** — 基于审计结果和您的需求，一起确定新系统的技术方案

请先介绍一下旧系统的情况和重写目标。""",
}


def _get_stage_welcome(stage: int, project_type: str = "new") -> str:
    if project_type == "maintain" and stage in MAINTAIN_STAGE_WELCOME:
        return MAINTAIN_STAGE_WELCOME[stage]
    if project_type == "legacy_rewrite" and stage in REWRITE_STAGE_WELCOME:
        return REWRITE_STAGE_WELCOME[stage]
    return STAGE_WELCOME.get(stage, f"**第 {stage + 1} 阶段：{STAGE_NAMES[stage]}**")


class StageInput(BaseModel):
    content: str
    model: str | None = None


class CompactChatInput(BaseModel):
    model: str | None = None


async def _get_iteration_id(session: AsyncSession, project_id: str) -> str | None:
    project = await session.get(Project, project_id)
    return project.current_iteration_id if project else None


@router.get("")
async def list_stages(project_id: str, session: AsyncSession = Depends(get_session)):
    iteration_id = await _get_iteration_id(session, project_id)
    q = await session.execute(
        select(StageProgress).where(
            StageProgress.project_id == project_id,
            StageProgress.iteration_id == iteration_id,
        ).order_by(StageProgress.stage)
    )
    return [{"stage": s.stage, "status": s.status, "documents": s.documents, "review_result": s.review_result} for s in q.scalars()]


# ── 文件上传与解析（图片 + 文档） ──

UPLOAD_DIR = Path("/tmp/openclaw-uploads")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


@router.post("/{stage}/upload-file")
async def upload_file(
    project_id: str,
    stage: int,
    file: UploadFile = File(...),
    hint: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """上传文件（图片/文档），自动解析为文字描述"""
    if stage > 3:
        raise HTTPException(400, "Stage 0-3 only")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(400, f"文件大小不能超过 {MAX_FILE_SIZE // 1024 // 1024}MB")

    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()

    # 保存原始文件
    upload_dir = UPLOAD_DIR / project_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    (upload_dir / safe_name).write_bytes(data)

    from app.services.doc_parser import parse_document, IMAGE_FORMATS
    is_image = ext in IMAGE_FORMATS
    result = await parse_document(data, filename, vision_analyze=is_image)

    return {
        "filename": safe_name,
        "original_name": filename,
        "size": len(data),
        "format": ext,
        "description": result.text,
        "metadata": result.metadata,
        "warnings": result.warnings,
    }


@router.post("/{stage}/upload-image")
async def upload_image(
    project_id: str,
    stage: int,
    file: UploadFile = File(...),
    hint: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """上传图片（向后兼容），内部转发到 upload-file"""
    return await upload_file(project_id, stage, file, hint, session)


@router.get("/doc-parser/status")
async def doc_parser_status():
    """检查文档解析器依赖状态"""
    from app.services.doc_parser import check_dependencies, supported_extensions
    return {
        "dependencies": check_dependencies(),
        "supported_formats": supported_extensions(),
    }


# ── 多轮对话（SSE 流式输出） ──

@router.post("/{stage}/chat")
async def chat(project_id: str, stage: int, body: StageInput, session: AsyncSession = Depends(get_session)):
    if stage > 3:
        raise HTTPException(400, "Stage 0-3 only")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    iteration_id = project.current_iteration_id

    chosen_model = body.model or resolve_model("leader")
    session.add(Message(
        project_id=project_id,
        iteration_id=iteration_id,
        stage=stage,
        role="user",
        content=body.content,
        metadata_={
            "owner_role": "leader",
            "model_used": chosen_model,
            "model_override": bool(body.model),
        },
    ))
    await session.flush()

    history = await _get_message_history(session, project_id, stage, iteration_id)
    previous_docs = await _collect_approved_docs(
        session,
        project_id,
        stage,
        iteration_id,
        include_current_stage_selected=True,
    )

    from app.services.project_context import build_knowledge_index
    knowledge_idx = await build_knowledge_index(
        session,
        project_id,
        include_experiences=True,
        active_stage=stage,
    )

    await session.commit()

    token_tracker.set_context(project_id=project_id)

    async def sse_generate():
        full_reply = []
        try:
            async for token in ai_leader.chat_in_stage_stream(
                stage, history, previous_docs, model=body.model,
                project_type=project.project_type or "new", rewrite_reason=project.rewrite_reason or "",
                knowledge_index=knowledge_idx, project_id=project_id, session=session,
                tech_stack=project.target_tech_stack or "",
            ):
                full_reply.append(token)
                yield f"data: {json.dumps(token, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"Stage {stage} chat stream failed: {type(e).__name__}: {e}")
            yield f"data: {json.dumps({'error': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
            return
        finally:
            content = "".join(full_reply)
            if content:
                async with async_session() as s:
                    s.add(Message(
                        project_id=project_id,
                        iteration_id=iteration_id,
                        stage=stage,
                        role="assistant",
                        content=content,
                        metadata_={
                            "owner_role": "leader",
                            "model_used": chosen_model,
                            "model_override": bool(body.model),
                        },
                    ))
                    await s.commit()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 阶段推进 ──

@router.post("/{stage}/advance")
async def advance_stage(project_id: str, stage: int, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)

    iteration_id = project.current_iteration_id
    iteration = await session.get(Iteration, iteration_id) if iteration_id else None

    if stage != project.current_stage:
        raise HTTPException(400, f"只能推进当前阶段（当前: Stage {project.current_stage}）")

    if stage == 3 and iteration:
        if iteration.status == "planning":
            other_active = await session.execute(
                select(Iteration).where(
                    Iteration.project_id == project_id,
                    Iteration.status == "active",
                    Iteration.id != iteration.id,
                )
            )
            if other_active.scalars().first():
                raise HTTPException(
                    400,
                    "当前有其他迭代正在执行中（Stage 4+），本迭代规划已完成，"
                    "将在前一个迭代完成或终止后自动进入任务分解阶段"
                )
            iteration.status = "active"

    if stage <= 3:
        approved = await session.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.iteration_id == iteration_id,
                Document.stage == stage,
                Document.status == "approved",
            )
        )
        approved_docs = approved.scalars().all()
        if not approved_docs:
            raise HTTPException(400, "当前阶段没有审核通过的文档，无法进入下一阶段")

        has_selected = any(d.is_selected for d in approved_docs)
        if not has_selected:
            approved_docs[0].is_selected = True

    if stage == 4:
        readiness = check_coding_readiness()
        if not readiness["ready"]:
            raise HTTPException(400, "编码阶段模型未就绪：" + "；".join(readiness["errors"]))

        if not project.git_repo:
            raise HTTPException(400, "项目未配置 Git 仓库地址，请在项目概览页设置后再进入编码阶段")

        iter_id = project.current_iteration_id
        modules_q = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.iteration_id == iter_id,
                Task.parent_task_id == None,  # noqa: E711
                Task.context["is_module"].astext == "true",
            )
        )
        modules = list(modules_q.scalars().all())
        if not modules:
            raise HTTPException(
                400,
                "当前迭代下还没有模块任务。请先在任务分解阶段点击「开始分解」生成模块，再到任务看板完成各模块的子任务分解。",
            )
        for mod in modules:
            cnt_q = await session.execute(
                select(sa_func.count()).where(Task.parent_task_id == mod.id)
            )
            child_count = int(cnt_q.scalar() or 0)
            if _module_needs_subtask_breakdown(mod, child_count):
                label = (mod.title or mod.ref_id or mod.id)[:120]
                raise HTTPException(
                    400,
                    f"仍有模块未完成子任务分解：「{label}」。请打开任务看板，使用「分解子任务」或批量分解处理所有模块后再进入编码开发阶段。",
                )

    if stage == 5:
        from app.services.scheduler import _check_all_tasks_done
        all_done = await _check_all_tasks_done(session, project_id)
        if not all_done:
            total_q = await session.execute(
                select(sa_func.count()).where(Task.project_id == project_id, Task.parent_task_id != None)  # noqa: E711
            )
            done_q = await session.execute(
                select(sa_func.count()).where(Task.project_id == project_id, Task.parent_task_id != None, Task.status == "done")  # noqa: E711
            )
            total = total_q.scalar() or 0
            done = done_q.scalar() or 0
            raise HTTPException(400, f"编码任务未全部完成（{done}/{total}），需要所有任务审核通过后才能进入测试阶段")

    if stage == 5:
        try:
            report = await _generate_stage6_report(session, project_id, iteration_id)
            next_sp = await _get_or_create_stage(session, project_id, 6, iteration_id)
            next_sp.review_result = {"quality": report, "generated_at": __import__('datetime').datetime.utcnow().isoformat()}
        except Exception as e:
            logger.warning(f"Failed to generate quality report: {e}")

    sp = await _get_or_create_stage(session, project_id, stage, iteration_id)
    sp.status = "completed"

    if stage == 7:
        if iteration:
            iteration.status = "completed"
            iteration.current_stage = 7
        project.status = "completed" if not iteration else project.status
        await session.commit()

        if iteration:
            from app.routers.iterations import _try_activate_next
            await _try_activate_next(session, project)
            await session.commit()

        return {"current_stage": project.current_stage, "iteration_completed": True}

    project.current_stage = stage + 1
    if iteration:
        iteration.current_stage = stage + 1

    next_sp = await _get_or_create_stage(session, project_id, stage + 1, iteration_id)
    next_sp.status = "in_progress"

    next_stage = stage + 1
    pt = project.project_type or "new"
    if next_stage <= 3:
        selected_docs = await _collect_approved_docs(session, project_id, next_stage, iteration_id)
        welcome = _get_stage_welcome(next_stage, pt)

        if selected_docs:
            welcome += "\n\n---\n\n我已阅读了上一阶段的文档，以下是我的分析：\n\n"

        session.add(Message(
            project_id=project_id,
            iteration_id=iteration_id,
            stage=next_stage,
            role="assistant",
            content=welcome,
            metadata_={"owner_role": "leader", "model_used": resolve_model("leader"), "model_override": False},
        ))

        if selected_docs:
            try:
                token_tracker.set_context(project_id=project_id)
                from app.services.project_context import get_project_context
                project_ctx = await get_project_context(session, project_id, include_approved_docs=True)
                analysis = await ai_leader.chat_in_stage(
                    next_stage,
                    [{"role": "user", "content": "我已经完成了上一阶段，请分析上一阶段的文档并给出本阶段的工作建议。"}],
                    previous_docs=selected_docs,
                    asset_context=project_ctx,
                    project_type=pt, rewrite_reason=project.rewrite_reason or "",
                )
                session.add(Message(
                    project_id=project_id,
                    iteration_id=iteration_id,
                    stage=next_stage,
                    role="assistant",
                    content=analysis,
                    metadata_={"owner_role": "leader", "model_used": resolve_model("leader"), "model_override": False},
                ))
            except Exception as e:
                logger.warning(f"Failed to generate stage {next_stage} opening analysis: {e}")

    await session.commit()
    return {"current_stage": project.current_stage}


# ── 阶段回退 ──

@router.post("/{stage}/retreat")
async def retreat_stage(
    project_id: str,
    stage: int,
    body: RetreatBody | None = None,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)

    iteration_id = project.current_iteration_id
    iteration = await session.get(Iteration, iteration_id) if iteration_id else None

    if stage != project.current_stage:
        raise HTTPException(400, f"只能从当前阶段回退（当前: Stage {project.current_stage}）")
    if stage == 0:
        raise HTTPException(400, "已经是第一个阶段，无法回退")

    cleanup_mode = (body.cleanup_mode if body else "none").strip().lower()
    if cleanup_mode not in ("none", "safe", "deep"):
        raise HTTPException(400, "cleanup_mode 仅支持 none/safe/deep")
    if cleanup_mode != "none" and not (body and body.confirm_cleanup):
        raise HTTPException(400, "清理任务产物为不可逆操作，请设置 confirm_cleanup=true")

    q = await session.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.iteration_id == iteration_id,
            Document.stage == stage,
        )
    )
    for doc in q.scalars():
        doc.status = "voided"
        doc.is_selected = False

    msgs = await session.execute(
        select(Message).where(
            Message.project_id == project_id,
            Message.iteration_id == iteration_id,
            Message.stage == stage,
        )
    )
    for m in msgs.scalars():
        await session.delete(m)

    sp = await _get_or_create_stage(session, project_id, stage, iteration_id)
    sp.status = "pending"

    prev = stage - 1
    prev_sp = await _get_or_create_stage(session, project_id, prev, iteration_id)
    prev_sp.status = "in_progress"

    prev_docs = await session.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.iteration_id == iteration_id,
            Document.stage == prev,
            Document.status == "approved",
        )
    )
    for doc in prev_docs.scalars():
        doc.status = "draft"
        doc.review_result = {}
        doc.reviewed_by = ""
        doc.is_selected = False

    await session.execute(
        delete(TaskDocument).where(
            TaskDocument.project_id == project_id,
            TaskDocument.iteration_id == iteration_id,
            TaskDocument.doc_type == "stage_document",
            TaskDocument.metadata_.contains({"stage": stage}),
        )
    )
    await session.execute(
        delete(TaskDocument).where(
            TaskDocument.project_id == project_id,
            TaskDocument.iteration_id == iteration_id,
            TaskDocument.doc_type == "stage_document",
            TaskDocument.metadata_.contains({"stage": prev}),
        )
    )

    cleanup_result = {
        "mode": cleanup_mode,
        "module_tasks_deleted": 0,
        "coding_tasks_deleted": 0,
        "teams_updated": 0,
    }
    deep_cleanup_result = {
        "task_documents_deleted": 0,
        "task_logs_deleted": 0,
        "task_comments_deleted": 0,
        "agent_messages_deleted": 0,
        "token_usage_logs_deleted": 0,
        "generation_tasks_deleted": 0,
    }

    if cleanup_mode != "none":
        module_conditions = [
            Task.project_id == project_id,
            Task.parent_task_id == None,  # noqa: E711
            Task.context["is_module"].astext == "true",
        ]
        if iteration_id:
            # 兼容历史数据：早期分解任务可能 iteration_id 为空
            module_conditions.append(or_(Task.iteration_id == iteration_id, Task.iteration_id == None))  # noqa: E711
        module_q = await session.execute(select(Task.id).where(*module_conditions))
        module_ids = [row[0] for row in module_q.all()]

        all_task_ids: list[str] = []
        if module_ids:
            child_q = await session.execute(
                select(Task.id).where(
                    Task.project_id == project_id,
                    Task.parent_task_id.in_(module_ids),
                )
            )
            child_ids = [row[0] for row in child_q.all()]
            all_task_ids = module_ids + child_ids

            cleanup_result["module_tasks_deleted"] = len(module_ids)
            cleanup_result["coding_tasks_deleted"] = len(child_ids)

            # 释放仍绑定到这些任务的 Agent，避免回退后出现悬挂任务引用
            agents_q = await session.execute(
                select(Agent).where(
                    Agent.project_id == project_id,
                    Agent.current_task_id.in_(all_task_ids),
                )
            )
            for a in agents_q.scalars():
                a.current_task_id = None
                if a.status in ("busy", "executing", "assigned"):
                    a.status = "idle"

            teams_q = await session.execute(
                select(AgentTeam).where(AgentTeam.project_id == project_id)
            )
            team_updated = 0
            module_set = set(module_ids)
            for t in teams_q.scalars():
                original = list(t.module_task_ids or [])
                filtered = [x for x in original if x not in module_set]
                if filtered != original:
                    t.module_task_ids = filtered
                    team_updated += 1
            cleanup_result["teams_updated"] = team_updated

            if cleanup_mode == "deep":
                td = await session.execute(delete(TaskDocument).where(TaskDocument.task_id.in_(all_task_ids)))
                deep_cleanup_result["task_documents_deleted"] = td.rowcount or 0
                tl = await session.execute(delete(TaskLog).where(TaskLog.task_id.in_(all_task_ids)))
                deep_cleanup_result["task_logs_deleted"] = tl.rowcount or 0
                tc = await session.execute(delete(TaskComment).where(TaskComment.task_id.in_(all_task_ids)))
                deep_cleanup_result["task_comments_deleted"] = tc.rowcount or 0
                am = await session.execute(delete(AgentMessage).where(AgentMessage.task_id.in_(all_task_ids)))
                deep_cleanup_result["agent_messages_deleted"] = am.rowcount or 0
                tu = await session.execute(delete(TokenUsageLog).where(TokenUsageLog.task_id.in_(all_task_ids)))
                deep_cleanup_result["token_usage_logs_deleted"] = tu.rowcount or 0
                gt = await session.execute(
                    delete(GenerationTask).where(
                        GenerationTask.project_id == project_id,
                        GenerationTask.stage == 4,
                        or_(GenerationTask.iteration_id == iteration_id, GenerationTask.iteration_id == None),  # noqa: E711
                    )
                )
                deep_cleanup_result["generation_tasks_deleted"] = gt.rowcount or 0

            await session.execute(delete(Task).where(Task.id.in_(all_task_ids)))

    project.current_stage = prev
    if iteration:
        iteration.current_stage = prev
    await session.commit()

    return {
        "current_stage": project.current_stage,
        "cleanup": cleanup_result,
        "deep_cleanup": deep_cleanup_result if cleanup_mode == "deep" else None,
    }


# ── Stage 4: 任务分解（两级：Leader 分模块 → Architect 分编码任务） ──

@router.post("/4/breakdown")
async def module_breakdown(project_id: str, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """Leader 分模块级大任务（异步）"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    # 执行分解前强校验：与项目页“测试连接”使用同一条校验链路
    repo_url = (project.git_repo or "").strip()
    if not repo_url:
        raise HTTPException(400, "Git 远程仓库未配置，请先填写")

    auth_info = project_git_auth.get_public_auth_info(project)
    if auth_info.get("mode") == "ssh" and not auth_info["has_ssh_key"]:
        project_git_auth.generate_ssh_keypair(project, updated_by="system")
        await session.commit()
        await session.refresh(project, ["config"])
    check = await project_git_auth.verify_project_repo_access(project, repo_url)
    if not check.get("ok"):
        raise HTTPException(400, (check.get("hint") or f"Git 远程仓库不可访问: {check.get('error', '')}").strip())

    iteration_id = project.current_iteration_id
    technical = await _get_selected_doc_content(session, project_id, 3, iteration_id)
    if not technical:
        raise HTTPException(400, "Stage 3（技术方案）需要有审核通过的文档才能分解任务")

    # 防重复：已有模块任务则不能再分解
    existing_modules = await session.execute(
        select(Task.id).where(
            Task.project_id == project_id,
            Task.iteration_id == iteration_id,
            Task.parent_task_id == None,  # noqa: E711
            Task.context["is_module"].astext == "true",
        ).limit(1)
    )
    if existing_modules.scalar_one_or_none():
        raise HTTPException(400, "已有模块任务，不能重复分解。如需重做请先删除现有模块。")

    # 防重复：正在运行的分解任务（超过10分钟自动视为卡死并释放）
    running_gen = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.doc_title == "模块分解",
            GenerationTask.status == "running",
        ).limit(1)
    )
    stuck = running_gen.scalar_one_or_none()
    if stuck:
        age = (datetime.now(timezone.utc) - stuck.created_at).total_seconds()
        if age > 600:
            stuck.status = "failed"
            stuck.error_message = "超时自动释放（>10min）"
            await session.commit()
            logger.warning(f"Auto-released stuck GenerationTask {stuck.id}")
        else:
            raise HTTPException(400, "模块分解正在进行中，请等待完成")

    task = GenerationTask(
        project_id=project_id, iteration_id=iteration_id, stage=4,
        doc_title="模块分解", status="running", progress=0,
        steps=[
            {"name": "收集需求与技术方案", "status": "pending"},
            {"name": "AI 分析项目结构", "status": "pending"},
            {"name": "AI 拆分功能模块", "status": "pending"},
            {"name": "创建模块任务记录", "status": "pending"},
            {"name": "归档架构决策", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    background_tasks.add_task(_run_module_breakdown, task.id, project_id, iteration_id)
    return {"task_id": task.id}


async def _run_module_breakdown(task_id: str, project_id: str, iteration_id: str | None):
    """后台执行模块分解"""
    token_tracker.set_context(project_id=project_id)
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        try:
            # Step 0: 收集需求与技术方案
            await _update_breakdown_step(session, task, 0, "running")
            project = await session.get(Project, project_id)
            requirements = await _get_selected_doc_content(session, project_id, 1, iteration_id)
            prototype = await _get_selected_doc_content(session, project_id, 2, iteration_id)
            technical = await _get_selected_doc_content(session, project_id, 3, iteration_id)
            ref_docs = await _collect_ref_docs(session, project_id, iteration_id)
            await _update_breakdown_step(session, task, 0, "completed")

            # Step 1: AI 分析项目结构
            await _update_breakdown_step(session, task, 1, "running")
            await asyncio.sleep(0.5)
            await _update_breakdown_step(session, task, 1, "completed")

            # Step 2: AI 拆分功能模块（最耗时）
            await _update_breakdown_step(session, task, 2, "running")
            result = await ai_leader.break_down_modules(
                requirements=requirements or "",
                prototype=prototype or "",
                technical_design=technical or "",
            )
            await _update_breakdown_step(session, task, 2, "completed")

            # Step 3: 创建模块任务记录
            await _update_breakdown_step(session, task, 3, "running")
            modules_data = result.get("modules") or result.get("tasks") or []
            created_modules = []
            idx_to_id: dict[int, str] = {}
            raw_deps: list[list] = []

            for idx, m in enumerate(modules_data):
                project.task_seq = (project.task_seq or 0) + 1
                ref_id = f"MOD-{project.task_seq:03d}"
                module_task = Task(
                    project_id=project_id, iteration_id=iteration_id,
                    ref_id=ref_id, ref_docs=ref_docs,
                    title=m.get("title", ""),
                    description=m.get("description", ""),
                    type=m.get("type", "feature"),
                    priority=m.get("priority", 0),
                    suggested_role="architect", suggested_model="opus",
                    estimated_hours=m.get("estimated_days", 2) * 8,
                    dependencies=[],
                    input_files=m.get("scope", []),
                    context={"is_module": True, "interfaces": m.get("interfaces", [])},
                )
                session.add(module_task)
                await session.flush()
                idx_to_id[idx] = module_task.id
                raw_deps.append(m.get("dependencies", []))
                created_modules.append({
                    "id": module_task.id, "ref_id": ref_id,
                    "title": module_task.title,
                    "estimated_days": m.get("estimated_days", 2),
                })

            await _resolve_dependencies(session, raw_deps, idx_to_id)

            sp = await _get_or_create_stage(session, project_id, 4, iteration_id)
            sp.status = "draft"
            sp.documents = result
            await _update_breakdown_step(session, task, 3, "completed")

            # Step 4: 归档架构决策
            await _update_breakdown_step(session, task, 4, "running")
            iter_seq = "default"
            if iteration_id:
                iteration = await session.get(Iteration, iteration_id)
                if iteration:
                    iter_seq = iteration.seq

            arch_decisions = result.get("architecture_decisions", [])
            if arch_decisions:
                await task_docs.archive_architecture_decisions(
                    session, project_id=project_id, iteration_id=iteration_id,
                    iteration_seq=iter_seq, decisions=arch_decisions,
                )

            await _update_breakdown_step(session, task, 4, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = json.dumps({"modules": created_modules, "count": len(created_modules)}, ensure_ascii=False)
            await session.commit()

        except Exception as e:
            logger.error(f"Module breakdown task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


async def _update_breakdown_step(session: AsyncSession, task: GenerationTask, step_idx: int, status: str):
    task.steps[step_idx]["status"] = status
    task.progress = int((sum(1 for s in task.steps if s["status"] == "completed") / len(task.steps)) * 100)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "steps")
    await session.commit()


@router.post("/4/breakdown/{module_task_id}")
async def subtask_breakdown(project_id: str, module_task_id: str, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """Architect 将模块拆解为编码级小任务（异步）"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    module_task = await session.get(Task, module_task_id)
    if not module_task or module_task.project_id != project_id:
        raise HTTPException(404, "Module task not found")

    if module_task.status == "draft":
        raise HTTPException(400, "模块尚未审核通过，请先通过审核再分解子任务")

    # 模块完成标记：幂等返回 200+skipped，避免前端批量分解时对「已完毕」模块连打 400 红字
    module_ctx = module_task.context or {}
    if module_ctx.get("subtask_breakdown_completed") is True:
        return {
            "task_id": None,
            "skipped": True,
            "reason": "already_completed",
            "module": {"id": module_task.id, "ref_id": module_task.ref_id, "title": module_task.title},
        }

    existing_children_q = await session.execute(
        select(Task.id).where(Task.parent_task_id == module_task_id).limit(1)
    )
    has_existing_children = existing_children_q.scalar_one_or_none() is not None
    if has_existing_children:
        # 兼容历史数据：若已有完成态分解任务，则补写完成标记并幂等跳过
        completed_q = await session.execute(
            select(GenerationTask).where(
                GenerationTask.project_id == project_id,
                GenerationTask.stage == 4,
                GenerationTask.status == "completed",
                GenerationTask.error_message.contains(module_task_id),
            ).limit(1)
        )
        if completed_q.scalar_one_or_none():
            module_ctx["subtask_breakdown_completed"] = True
            module_task.context = module_ctx
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(module_task, "context")
            await session.commit()
            return {
                "task_id": None,
                "skipped": True,
                "reason": "already_completed_reconciled",
                "module": {"id": module_task.id, "ref_id": module_task.ref_id, "title": module_task.title},
            }

    # 防重复：检查是否有正在运行的分解任务（超过10分钟自动释放）
    running_gen = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.doc_title.contains(module_task.title),
            GenerationTask.status == "running",
        ).limit(1)
    )
    stuck = running_gen.scalar_one_or_none()
    if stuck:
        age = (datetime.now(timezone.utc) - stuck.created_at).total_seconds()
        if age > 600:
            stuck.status = "failed"
            stuck.error_message = "超时自动释放（>10min）"
            await session.commit()
            logger.warning(f"Auto-released stuck GenerationTask {stuck.id}")
        else:
            raise HTTPException(400, "该模块正在分解中，请等待完成")

    gen_task = GenerationTask(
        project_id=project_id, iteration_id=project.current_iteration_id, stage=4,
        doc_title=f"任务分解: {module_task.title}",
        status="running", progress=0,
        steps=[
            {"name": "读取模块信息与技术方案", "status": "pending"},
            {"name": "AI 分析模块依赖", "status": "pending"},
            {"name": "生成子任务条目清单", "status": "pending"},
            {"name": "逐条展开并创建任务", "status": "pending"},
            {"name": "归档任务文档", "status": "pending"},
        ],
    )
    session.add(gen_task)
    await session.commit()
    await session.refresh(gen_task)

    background_tasks.add_task(_run_subtask_breakdown, gen_task.id, project_id, module_task_id, project.current_iteration_id)
    return {
        "task_id": gen_task.id,
        "skipped": False,
        "module": {"id": module_task.id, "ref_id": module_task.ref_id, "title": module_task.title},
    }


async def _run_subtask_breakdown(task_id: str, project_id: str, module_task_id: str, iteration_id: str | None):
    """后台执行子任务分解"""
    token_tracker.set_context(project_id=project_id)
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        try:
            # Step 0: 读取模块信息与全量上下文
            await _update_breakdown_step(session, task, 0, "running")
            project = await session.get(Project, project_id)
            module_task = await session.get(Task, module_task_id)
            project_cfg = dict(project.config or {}) if project and isinstance(project.config, dict) else {}
            prototype_fast_track = bool(project_cfg.get("prototype_fast_track"))
            module_override_model = (module_task.suggested_model or "").strip() if module_task else ""
            module_map = (module_task.context or {}).get("role_model_map") if module_task and isinstance(module_task.context, dict) else None
            project_map = project.role_model_map if project and isinstance(project.role_model_map, dict) else None
            architect_model = resolve_model(
                "architect",
                override=module_override_model or None,
                module_map=module_map,
                project_map=project_map,
            )
            task.model_used = architect_model
            requirements = await _get_selected_doc_content(session, project_id, 1, iteration_id)
            prototype = await _get_selected_doc_content(session, project_id, 2, iteration_id)
            technical = await _get_selected_doc_content(session, project_id, 3, iteration_id)
            discussion_summary = await _collect_stage_discussion_context(session, project_id, iteration_id)
            await _update_breakdown_step(session, task, 0, "completed")

            # Step 1: 收集所有模块概览（架构师需要全局视角）
            await _update_breakdown_step(session, task, 1, "running")
            all_modules_q = await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.iteration_id == iteration_id,
                    Task.parent_task_id == None,  # noqa: E711
                    Task.context["is_module"].astext == "true",
                ).order_by(Task.priority.desc())
            )
            all_modules = list(all_modules_q.scalars())
            modules_lines = []
            for i, m in enumerate(all_modules):
                marker = " ← 【当前模块】" if m.id == module_task_id else ""
                deps = m.dependencies or []
                dep_refs = []
                for d in deps:
                    dep_mod = next((x for x in all_modules if x.id == d), None)
                    dep_refs.append(dep_mod.ref_id if dep_mod else d[:8])
                dep_str = f" (依赖: {', '.join(dep_refs)})" if dep_refs else ""
                modules_lines.append(f"{i+1}. [{m.ref_id}] {m.title}{dep_str}{marker}")
            all_modules_summary = "\n".join(modules_lines)
            await _update_breakdown_step(session, task, 1, "completed")

            # Step 2: 先生成条目清单（Phase A）
            await _update_breakdown_step(session, task, 2, "running")
            module_ctx = module_task.context or {}
            manifest = module_ctx.get("subtask_manifest")
            manifest_items = manifest.get("items", []) if isinstance(manifest, dict) else []
            if not manifest_items:
                outline_items = await ai_leader.break_down_task_titles(
                    module_title=module_task.title,
                    module_description=module_task.description,
                    technical_context=technical or "",
                    requirements=requirements or "",
                    prototype=prototype or "",
                    all_modules_summary=all_modules_summary,
                    discussion_context=discussion_summary,
                    model=architect_model,
                )
                if not outline_items:
                    raise ValueError("子任务条目生成为空")
                dedup: dict[str, dict] = {}
                for raw in outline_items:
                    title = (raw.get("title", "") or "").strip()
                    key = _subtask_semantic_key(title)
                    if not title or key in dedup:
                        continue
                    dedup[key] = {
                        "id": _subtask_item_id(module_task.id, title),
                        "title": title,
                        "semantic_key": key,
                        "status": "pending",
                    }
                manifest_items = list(dedup.values())
                if not manifest_items:
                    raise ValueError("子任务条目去重后为空")
                module_ctx["subtask_manifest"] = {
                    "module_id": module_task.id,
                    "version": 1,
                    "items": manifest_items,
                }
                module_task.context = module_ctx
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(module_task, "context")
                await session.commit()
            await _update_breakdown_step(session, task, 2, "completed")

            # Step 3: 按条目展开并创建任务（Phase B，batch + 单条容错，可续跑）
            await _update_breakdown_step(session, task, 3, "running")
            manifest_titles = [i.get("title", "") for i in manifest_items if i.get("title")]
            created_tasks, task_details_by_id, title_to_task_id = await _expand_subtasks_in_batches(
                session=session,
                gen_task=task,
                module_task=module_task,
                manifest_items=manifest_items,
                manifest_titles=manifest_titles,
                technical=technical,
                requirements=requirements,
                prototype=prototype,
                all_modules_summary=all_modules_summary,
                discussion_summary=discussion_summary,
                architect_model=architect_model,
                prototype_fast_track=prototype_fast_track,
                project_id=project_id,
                iteration_id=iteration_id,
            )

            # 依赖解析：使用 Phase A 的 depends_on 索引
            index_to_task_id: dict[int, str] = {}
            for idx, item in enumerate(manifest_items):
                if item.get("status") != "failed":
                    key = item.get("semantic_key") or _subtask_semantic_key(item.get("title", ""))
                    if key in title_to_task_id:
                        index_to_task_id[idx] = title_to_task_id[key]

            for ct in created_tasks:
                ct_id = ct["id"]
                dep_ids: list[str] = []
                for idx, item in enumerate(manifest_items):
                    if item.get("status") == "failed":
                        continue
                    key = item.get("semantic_key") or _subtask_semantic_key(item.get("title", ""))
                    if title_to_task_id.get(key) == ct_id:
                        for dep_idx in item.get("depends_on", []):
                            if isinstance(dep_idx, int) and dep_idx in index_to_task_id and index_to_task_id[dep_idx] != ct_id:
                                dep_ids.append(index_to_task_id[dep_idx])
                        break

                t_obj = await session.get(Task, ct_id)
                if t_obj:
                    t_obj.dependencies = dep_ids
            await _update_breakdown_step(session, task, 3, "completed")

            # Step 4: 归档任务文档
            await _update_breakdown_step(session, task, 4, "running")
            iter_seq = "default"
            if iteration_id:
                iteration = await session.get(Iteration, iteration_id)
                if iteration:
                    iter_seq = iteration.seq

            arch_decisions = []
            if arch_decisions:
                await task_docs.archive_architecture_decisions(
                    session, project_id=project_id, iteration_id=iteration_id,
                    iteration_seq=iter_seq, decisions=arch_decisions,
                )

            for ct in created_tasks:
                if not ct.get("is_new"):
                    continue
                t_data = task_details_by_id.get(ct["id"], {})
                await task_docs.archive(
                    session, project_id=project_id, iteration_id=iteration_id,
                    iteration_seq=iter_seq, task_id=ct["id"], ref_id=ct["ref_id"],
                    doc_type="task_instruction",
                    title=f"[{ct['ref_id']}] {ct['title']}",
                    content=f"# {ct['ref_id']} {ct['title']}\n\n## 所属模块\n{module_task.title} ({module_task.ref_id})\n\n"
                            f"## 描述\n{t_data.get('description', '')}\n\n"
                            f"## 验收标准\n" + "\n".join(f"- {c}" for c in t_data.get("acceptance_criteria", [])) +
                            f"\n\n## Git 分支\n`{ct['git_branch']}`\n\n## 角色\n{ct['suggested_role']}\n",
                    tags=["task", ct["ref_id"], module_task.ref_id, ct.get("suggested_role", "")],
                    metadata={"ref_id": ct["ref_id"], "git_branch": ct["git_branch"], "module_ref_id": module_task.ref_id},
                )

            await _update_breakdown_step(session, task, 4, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = json.dumps({
                "module": {"id": module_task.id, "ref_id": module_task.ref_id, "title": module_task.title},
                "tasks": created_tasks, "count": len(created_tasks),
            }, ensure_ascii=False)
            module_ctx = module_task.context or {}
            module_ctx["subtask_breakdown_completed"] = True
            module_task.context = module_ctx
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(module_task, "context")
            await session.commit()

            # 子任务分解完成后立即触发调度，避免等待额外心跳/人工操作
            try:
                await scheduler.auto_assign(session, project_id, actor_role="architect")
            except Exception as assign_err:
                logger.warning(f"Subtask breakdown auto-assign failed: {assign_err}")

        except Exception as e:
            logger.error(f"Subtask breakdown task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


# ── 消息历史 ──

@router.get("/{stage}/messages")
async def get_stage_messages(project_id: str, stage: int, session: AsyncSession = Depends(get_session)):
    iteration_id = await _get_iteration_id(session, project_id)
    q = await session.execute(
        select(Message).where(
            Message.project_id == project_id,
            Message.iteration_id == iteration_id,
            Message.stage == stage,
        ).order_by(Message.created_at)
    )
    msgs = list(q.scalars())
    visible = [
        m for m in msgs
        if not (m.role == "system" and isinstance(m.metadata_, dict) and m.metadata_.get("kind") == "chat_compaction")
    ]
    return [{"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in visible]


@router.post("/{stage}/messages/compact")
async def compact_stage_messages(
    project_id: str,
    stage: int,
    body: CompactChatInput,
    session: AsyncSession = Depends(get_session),
):
    """整理当前阶段对话为可复用上下文（保留原始消息，不覆盖）"""
    if stage > 3:
        raise HTTPException(400, "Stage 0-3 only")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    iteration_id = project.current_iteration_id
    q = await session.execute(
        select(Message).where(
            Message.project_id == project_id,
            Message.iteration_id == iteration_id,
            Message.stage == stage,
        ).order_by(Message.created_at)
    )
    all_msgs = list(q.scalars())
    chat_msgs = [m for m in all_msgs if m.role in ("user", "assistant")]
    if not chat_msgs:
        raise HTTPException(400, "暂无可整理的会话内容")

    lines: list[str] = []
    for m in chat_msgs[-80:]:
        role = "用户" if m.role == "user" else "AI"
        lines.append(f"{role}: {m.content}")
    transcript = "\n\n".join(lines)

    model = body.model or resolve_model("leader")
    prompt = f"""请将以下项目阶段会话整理为“后续可直接复用”的简明上下文。

要求：
1) 只基于会话内容，不补充外部信息
2) 保留约束、结论、待确认问题、用户明确偏好
3) 删除寒暄、重复讨论、无关发散
4) 输出结构固定为：
   - 背景与目标
   - 已确认约束
   - 已达成结论
   - 待确认问题
   - 下一步分析输入（给 AI 执行时直接使用）
5) 使用简洁中文，避免空话

会话内容：
{transcript}
"""
    summary = await ai_leader._call(
        "你是项目会话整理助手。只做压缩整理，不扩写新事实。",
        prompt,
        model=model,
        max_tokens=4096,
        auto_continue=True,
        temperature=0.2,
    )
    summary = (summary or "").strip()
    if not summary:
        raise HTTPException(500, "会话整理失败：未返回内容")

    source_first_id = chat_msgs[0].id
    source_last_id = chat_msgs[-1].id
    session.add(Message(
        project_id=project_id,
        iteration_id=iteration_id,
        stage=stage,
        role="system",
        content=summary,
        metadata_={
            "kind": "chat_compaction",
            "owner_role": "leader",
            "model_used": model,
            "source_count": len(chat_msgs),
            "source_first_id": source_first_id,
            "source_last_id": source_last_id,
        },
    ))
    await session.commit()
    return {
        "summary": summary,
        "source_count": len(chat_msgs),
        "source_first_id": source_first_id,
        "source_last_id": source_last_id,
        "model_used": model,
    }


@router.get("/{stage}/messages/compact/latest")
async def latest_compact_stage_messages(project_id: str, stage: int, session: AsyncSession = Depends(get_session)):
    if stage > 3:
        raise HTTPException(400, "Stage 0-3 only")

    iteration_id = await _get_iteration_id(session, project_id)
    q = await session.execute(
        select(Message).where(
            Message.project_id == project_id,
            Message.iteration_id == iteration_id,
            Message.stage == stage,
        ).order_by(Message.created_at)
    )
    msgs = list(q.scalars())
    compact_msgs = [
        m for m in msgs
        if m.role == "system" and isinstance(m.metadata_, dict) and m.metadata_.get("kind") == "chat_compaction"
    ]
    if not compact_msgs:
        return {"summary": None}
    m = compact_msgs[-1]
    return {
        "summary": m.content,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "source_count": (m.metadata_ or {}).get("source_count", 0),
        "source_last_id": (m.metadata_ or {}).get("source_last_id"),
        "model_used": (m.metadata_ or {}).get("model_used", ""),
    }


# ── Stage 6: 代码质量报告 ──

@router.get("/{stage}/report")
async def get_stage_report(project_id: str, stage: int, session: AsyncSession = Depends(get_session)):
    iteration_id = await _get_iteration_id(session, project_id)
    sp = await _get_or_create_stage(session, project_id, stage, iteration_id)
    result = dict(sp.review_result or {})
    # Stage 7 部署阶段：返回部署状态与地址
    if stage == 7:
        project = await session.get(Project, project_id)
        if project:
            cfg = dict(project.config or {})
            project_type = cfg.get("project_type", "bs")
            # 检查部署任务是否成功
            deploy_ok = False
            deploy_urls = cfg.get("deployment_urls") or []
            if deploy_urls:
                deploy_ok = True
            else:
                # 自动推导地址：仅在 BS 项目 + 具备环境时生成
                from app.models import Task as _Task
                from sqlalchemy import select as _select
                _q = await session.execute(
                    _select(_Task).where(
                        _Task.project_id == project_id,
                        _Task.type == "deploy",
                    ).order_by(_Task.created_at.desc()).limit(1)
                )
                _deploy_task = _q.scalar_one_or_none()
                if _deploy_task and _deploy_task.status == "done":
                    deploy_ok = True
                    # 优先使用部署任务上报的入口地址，否则用默认推导
                    _task_result = dict(_deploy_task.context or {})
                    deploy_urls = _task_result.get("entry_urls") or []
                    if not deploy_urls:
                        code = project.code or project.id
                        base = (settings.DISPATCHER_PUBLIC_BASE_URL or "").rstrip("/")
                        import re as _re
                        _m = _re.match(r"https?://([^:]+)(?::(\d+))?", base)
                        if _m and project_type == "bs":
                            host = _m.group(1)
                            deploy_urls = [
                                {"label": "主站", "url": f"http://{host}:13000/{code}"},
                                {"label": "管理后台", "url": f"http://{host}:13000/{code}/admin"},
                            ]

            result["deployment_urls"] = deploy_urls
            result["deployment_ok"] = deploy_ok
            result["project_type"] = project_type
    if not result:
        raise HTTPException(404, "暂无报告")
    return result



class DeployUrlsBody(BaseModel):
    urls: list[dict]  # [{"label": "外网访问", "url": "https://example.com"}, {"label": "内网地址", "url": "http://10.0.0.1"}]

class StageReportBody(BaseModel):
    content: dict | None = None  # 自定义报告内容

@router.put("/{stage}/report")
async def update_stage_report(project_id: str, stage: int, body: StageReportBody, session: AsyncSession = Depends(get_session)):
    iteration_id = await _get_iteration_id(session, project_id)
    sp = await _get_or_create_stage(session, project_id, stage, iteration_id)
    if body.content:
        sp.review_result = {**dict(sp.review_result or {}), **body.content}
    await session.commit()
    return sp.review_result

@router.put("/{stage}/deployment-urls")
async def set_deployment_urls(project_id: str, stage: int, body: DeployUrlsBody, session: AsyncSession = Depends(get_session)):
    """设置部署阶段的访问地址（BS: 内外网URL，CS: 下载地址）"""
    if stage != 7:
        raise HTTPException(400, "仅 Stage 7 支持部署地址")
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    cfg = dict(project.config or {})
    cfg["deployment_urls"] = body.urls
    project.config = cfg
    await session.commit()
    return {"deployment_urls": body.urls}

@router.post("/{stage}/report/regenerate")
async def regenerate_report(project_id: str, stage: int, session: AsyncSession = Depends(get_session)):
    if stage != 6:
        raise HTTPException(400, "仅支持 Stage 6 报告重新生成")
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    iteration_id = project.current_iteration_id
    report = await _generate_stage6_report(session, project_id, iteration_id)
    sp = await _get_or_create_stage(session, project_id, 6, iteration_id)
    sp.review_result = {"quality": report, "generated_at": __import__('datetime').datetime.utcnow().isoformat()}
    await session.commit()
    return sp.review_result


# ── Stage 6: 自动测试 ──

class AutoTestRequest(BaseModel):
    mode: str = "integration"  # integration | e2e | full（兼容 basic）


async def _resolve_test_governance(session: AsyncSession, project_id: str, mode: str) -> dict:
    q = await session.execute(
        select(Agent).where(
            Agent.project_id == project_id,
            Agent.role.in_(["leader", "architect", "tester"]),
        )
    )
    agents = list(q.scalars())
    leader = next((a for a in agents if a.role == "leader"), None)
    architect = next((a for a in agents if a.role == "architect"), None)
    testers = [a for a in agents if a.role == "tester"]

    return {
        "manager": {"role": "leader", "agent_id": leader.id if leader else ""},
        "integration": {
            "organizer_role": "architect",
            "organizer_agent_id": architect.id if architect else "",
        },
        "e2e": {
            "organizer_role": "tester",
            "organizer_agent_ids": [a.id for a in testers],
            "fallback_role": "architect",
            "fallback_agent_id": architect.id if architect else "",
        },
        "mode": mode,
    }


@router.post("/6/auto-test")
async def run_auto_test(
    project_id: str, body: AutoTestRequest,
    background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session),
):
    """运行自动测试：integration=集成测试, e2e=端到端测试, full=二者都跑"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    mode = (body.mode or "integration").strip().lower()
    if mode == "basic":
        mode = "integration"
    if mode not in ("integration", "e2e", "full"):
        raise HTTPException(400, "mode 仅支持 integration/e2e/full（兼容 basic）")
    run_api = mode in ("integration", "full")
    run_e2e = mode in ("e2e", "full")
    governance = await _resolve_test_governance(session, project_id, mode)

    task = GenerationTask(
        project_id=project_id, iteration_id=project.current_iteration_id,
        stage=6, doc_title=f"自动测试（{mode}）", status="running", progress=0,
        steps=[{"name": "收集需求和任务信息", "status": "pending"}]
        + ([{"name": "AI 生成集成测试计划", "status": "pending"}, {"name": "保存集成测试结果", "status": "pending"}] if run_api else [])
        + ([{"name": "生成 E2E 测试用例", "status": "pending"}] if run_e2e else []),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    background_tasks.add_task(_run_auto_test, task.id, project_id, mode)
    return {"task_id": task.id, "mode": mode, "test_governance": governance}


async def _run_auto_test(task_id: str, project_id: str, mode: str):
    token_tracker.set_context(project_id=project_id)
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        try:
            run_api = mode in ("integration", "full", "basic")
            run_e2e = mode in ("e2e", "full")
            api_test_plan: dict = {}
            e2e_plan: dict = {}

            # Step 0: 收集信息
            await _update_breakdown_step(session, task, 0, "running")
            project = await session.get(Project, project_id)
            iteration_id = project.current_iteration_id if project else None
            requirements = await _get_selected_doc_content(session, project_id, 1, iteration_id)
            technical = await _get_selected_doc_content(session, project_id, 3, iteration_id)

            tasks_q = await session.execute(
                select(Task).where(Task.project_id == project_id, Task.parent_task_id != None)  # noqa: E711
            )
            coding_tasks = tasks_q.scalars().all()
            api_summary = "\n".join(
                f"- [{t.ref_id}] {t.title}: {t.description[:200]}"
                for t in coding_tasks
            )
            await _update_breakdown_step(session, task, 0, "completed")

            sp = await _get_or_create_stage(session, project_id, 6, iteration_id)
            existing = sp.review_result or {}
            existing["test_mode"] = mode
            existing["test_generated_at"] = datetime.now(timezone.utc).isoformat()
            existing["test_governance"] = await _resolve_test_governance(session, project_id, mode)
            idx = 1

            if run_api:
                # Step 1: 生成集成测试计划
                await _update_breakdown_step(session, task, idx, "running")
                api_test_plan = await ai_leader.generate_test_plan(
                    api_routes=api_summary,
                    requirements=requirements or "",
                    technical_doc=technical or "",
                )
                await _update_breakdown_step(session, task, idx, "completed")
                idx += 1

                # Step 2: 保存集成测试结果
                await _update_breakdown_step(session, task, idx, "running")
                existing["api_test_plan"] = api_test_plan
                test_code = api_test_plan.get("test_file", "")
                if test_code:
                    session.add(Document(
                        project_id=project_id, iteration_id=iteration_id, stage=6,
                        title="API 集成测试代码", content=f"```python\n{test_code}\n```",
                        category="test", tags=["API", "集成测试"], status="draft",
                    ))
                await _update_breakdown_step(session, task, idx, "completed")
                idx += 1

            if run_e2e:
                await _update_breakdown_step(session, task, idx, "running")
                pages_info = "\n".join(
                    f"- {t.title}: {(t.context or {}).get('output_files', [])}"
                    for t in coding_tasks if t.suggested_role in ("mid", "junior", "senior")
                )
                e2e_plan = await ai_leader.generate_e2e_test_plan(
                    pages_info=pages_info or api_summary,
                    requirements=requirements or "",
                )
                existing["e2e_test_plan"] = e2e_plan

                e2e_cases = e2e_plan.get("test_cases", [])
                e2e_md = "# E2E 测试用例\n\n"
                for tc in e2e_cases:
                    e2e_md += f"## {tc.get('name', '')} ({tc.get('priority', 'P1')})\n\n"
                    for i, step in enumerate(tc.get("steps", []), 1):
                        screenshot = " 📸" if step.get("screenshot") else ""
                        e2e_md += f"{i}. **操作**: {step.get('action', '')}\n"
                        e2e_md += f"   **期望**: {step.get('expected', '')}{screenshot}\n\n"

                doc_e2e = Document(
                    project_id=project_id, iteration_id=iteration_id, stage=6,
                    title="E2E 测试用例", content=e2e_md,
                    category="test", tags=["E2E", "测试用例"], status="draft",
                )
                session.add(doc_e2e)
                await _update_breakdown_step(session, task, idx, "completed")

            sp.review_result = existing
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(sp, "review_result")

            e2e_count = 0
            if run_e2e:
                e2e_count = len(e2e_plan.get("test_cases", []))  # noqa: F821

            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = json.dumps({
                "api_tests": len(api_test_plan.get("test_cases", [])),
                "e2e_tests": e2e_count,
            }, ensure_ascii=False)
            await session.commit()

        except Exception as e:
            logger.error(f"Auto-test task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


# ── Stage 5→6: 文档骨架自动生成 ──

@router.post("/5/generate-docs")
async def generate_project_docs(
    project_id: str,
    background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session),
):
    """编码阶段完成后，自动生成用户手册和管理员手册骨架"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)

    task = GenerationTask(
        project_id=project_id, iteration_id=project.current_iteration_id,
        stage=5, doc_title="文档骨架生成", status="running", progress=0,
        steps=[
            {"name": "收集项目结构信息", "status": "pending"},
            {"name": "AI 生成文档骨架", "status": "pending"},
            {"name": "保存文档", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    background_tasks.add_task(_run_doc_generation, task.id, project_id)
    return {"task_id": task.id}


async def _run_doc_generation(task_id: str, project_id: str):
    token_tracker.set_context(project_id=project_id)
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        try:
            # Step 0: 收集信息
            await _update_breakdown_step(session, task, 0, "running")
            project = await session.get(Project, project_id)
            iteration_id = project.current_iteration_id if project else None
            requirements = await _get_selected_doc_content(session, project_id, 1, iteration_id)
            technical = await _get_selected_doc_content(session, project_id, 3, iteration_id)

            tasks_q = await session.execute(
                select(Task).where(Task.project_id == project_id, Task.parent_task_id != None)  # noqa: E711
            )
            coding_tasks = tasks_q.scalars().all()
            project_structure = "\n".join(
                f"- [{t.ref_id}] {t.title} (角色:{t.suggested_role}): {t.description[:150]}"
                for t in coding_tasks
            )
            await _update_breakdown_step(session, task, 0, "completed")

            # Step 1: AI 生成
            await _update_breakdown_step(session, task, 1, "running")
            result = await ai_leader.generate_doc_skeleton(
                project_structure=project_structure,
                api_summary=technical or "",
                deploy_config=f"tech_stack: {project.target_tech_stack or '默认'}\ngit_repo: {project.git_repo or '无'}",
                requirements=requirements or "",
            )
            await _update_breakdown_step(session, task, 1, "completed")

            # Step 2: 保存为文档
            await _update_breakdown_step(session, task, 2, "running")
            user_manual = result.get("user_manual", "")
            admin_manual = result.get("admin_manual", "")

            if user_manual:
                session.add(Document(
                    project_id=project_id, iteration_id=iteration_id, stage=7,
                    title="用户手册（骨架）", content=user_manual,
                    category="deploy", tags=["用户手册"], status="draft",
                ))
            if admin_manual:
                session.add(Document(
                    project_id=project_id, iteration_id=iteration_id, stage=7,
                    title="管理员手册（骨架）", content=admin_manual,
                    category="deploy", tags=["管理员手册", "运维"], status="draft",
                ))

            await _update_breakdown_step(session, task, 2, "completed")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = json.dumps({
                "user_manual": bool(user_manual),
                "admin_manual": bool(admin_manual),
            }, ensure_ascii=False)
            await session.commit()

        except Exception as e:
            logger.error(f"Doc generation task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


# ── 获取可用模型列表（Leader 默认 + 供应商中的模型） ──

@router.get("/models")
async def get_available_models(project_id: str, session: AsyncSession = Depends(get_session)):
    from app.models import ModelProvider
    models = []

    from app.services.model_pool import _get_tier

    q = await session.execute(select(ModelProvider).where(ModelProvider.enabled == True))
    for p in q.scalars():
        for m in (p.models or []):
            suffix = " (default)" if p.is_default else ""
            models.append({
                "model": m, "name": f"{p.name} ({m}){suffix}",
                "provider": p.name, "is_default": p.is_default,
                "api_key_masked": _mask_api_key(p.api_key),
                "tier": _get_tier(m),
            })

    return models


# ── 任务分解进度查询（复用 GenerationTask） ──

@router.get("/4/breakdown/status/{task_id}")
async def breakdown_task_status(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    """查询模块分解/子任务分解的进度"""
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "Task not found")
    result = {
        "id": task.id, "status": task.status, "progress": task.progress,
        "steps": task.steps, "error_message": task.error_message,
        "doc_title": task.doc_title,
    }
    if task.status == "completed" and task.error_message:
        try:
            result["result"] = json.loads(task.error_message)
        except (json.JSONDecodeError, TypeError):
            pass
    return result


@router.post("/4/breakdown/reset")
async def reset_stuck_breakdowns(project_id: str, session: AsyncSession = Depends(get_session)):
    """强制重置所有卡在 running 状态的分解任务，以便重新触发"""
    q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == 4,
            GenerationTask.status == "running",
        )
    )
    stuck_tasks = list(q.scalars())
    reset_ids = []
    for t in stuck_tasks:
        t.status = "failed"
        t.error_message = "手动重置"
        for i, step in enumerate(t.steps):
            if step["status"] == "running":
                t.steps[i]["status"] = "failed"
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(t, "steps")
        reset_ids.append(t.id)
    await session.commit()
    return {"reset": reset_ids, "count": len(reset_ids)}


# ── 内部方法 ──

async def _get_message_history(session: AsyncSession, project_id: str, stage: int, iteration_id: str | None) -> list[dict]:
    q = await session.execute(
        select(Message).where(
            Message.project_id == project_id,
            Message.iteration_id == iteration_id,
            Message.stage == stage,
        ).order_by(Message.created_at)
    )
    return [{"role": m.role, "content": m.content} for m in q.scalars()]


async def _collect_stage_discussion_context(session: AsyncSession, project_id: str, iteration_id: str | None, max_chars: int = 6000) -> str:
    """收集 Stage0-3 的讨论摘要文本，注入架构师分解上下文。"""
    chunks: list[str] = []
    total = 0
    for stage in range(4):
        history = await _get_message_history(session, project_id, stage, iteration_id)
        if not history:
            continue
        lines: list[str] = []
        for msg in history:
            role = msg.get("role", "")
            content = (msg.get("content", "") or "").strip()
            if role == "system":
                continue
            if not content:
                continue
            lines.append(f"[{role}] {content}")
        if not lines:
            continue
        block = f"## Stage {stage} 讨论\n" + "\n".join(lines)
        if total + len(block) > max_chars:
            remain = max_chars - total
            if remain > 400:
                chunks.append(block[:remain] + "\n...(讨论内容已截断)")
            break
        chunks.append(block)
        total += len(block)
    return "\n\n".join(chunks)


_DOCS_MAX_CHARS = 30_000  # 前置文档注入上限（约 12K token）


async def _collect_approved_docs(
    session: AsyncSession,
    project_id: str,
    up_to_stage: int,
    iteration_id: str | None,
    include_current_stage_selected: bool = False,
) -> str:
    """收集阶段文档上下文。

    - 默认：仅收集前置阶段已审核文档（跨阶段只读 approved）
    - 可选：在会话场景下额外注入当前阶段全部文档（含 draft/rejected）
    """
    iteration = await session.get(Iteration, iteration_id) if iteration_id else None
    parts = []
    total = 0
    end_stage = up_to_stage + 1 if include_current_stage_selected else up_to_stage
    for s in range(end_stage):
        approved_only = not (include_current_stage_selected and s == up_to_stage)
        docs = await _find_stage_docs(session, project_id, s, iteration, approved_only=approved_only)
        if not docs:
            continue
        for doc in docs:
            content = doc.content or ""
            if total + len(content) > _DOCS_MAX_CHARS:
                remaining = max(0, _DOCS_MAX_CHARS - total)
                if remaining > 500:
                    parts.append(f"## Stage {s}: {doc.title}\n\n{content[:remaining]}\n\n...(文档过长，已截断)")
                else:
                    parts.append(f"## Stage {s}: {doc.title}\n\n（文档过长已省略，可通过 [NEED_CONTEXT:doc_s{s}] 加载）")
                return "\n\n---\n\n".join(parts)
            parts.append(f"## Stage {s}: {doc.title}\n\n{content}")
            total += len(content)
    return "\n\n---\n\n".join(parts)


async def _find_stage_docs(
    session: AsyncSession,
    project_id: str,
    stage: int,
    iteration: Iteration | None,
    approved_only: bool = True,
) -> list[Document]:
    docs: list[Document] = []
    seen: set[str] = set()
    cur = iteration
    while cur:
        if stage >= cur.start_stage:
            filters = [
                Document.project_id == project_id,
                Document.iteration_id == cur.id,
                Document.stage == stage,
            ]
            if approved_only:
                filters.append(Document.status == "approved")
            q = await session.execute(
                select(Document).where(*filters).order_by(Document.updated_at.desc(), Document.created_at.desc())
            )
            for doc in q.scalars():
                if doc.id in seen:
                    continue
                seen.add(doc.id)
                docs.append(doc)
        if cur.parent_iteration_id:
            cur = await session.get(Iteration, cur.parent_iteration_id)
        else:
            break
    if docs:
        return docs

    fallback_filters = [
        Document.project_id == project_id,
        Document.iteration_id == (iteration.id if iteration else None),
        Document.stage == stage,
    ]
    if approved_only:
        fallback_filters.append(Document.status == "approved")
    fallback_q = await session.execute(
        select(Document).where(*fallback_filters).order_by(Document.updated_at.desc(), Document.created_at.desc())
    )
    return list(fallback_q.scalars())


async def _find_selected_doc(
    session: AsyncSession,
    project_id: str,
    stage: int,
    iteration: Iteration | None,
    approved_only: bool = True,
) -> Document | None:
    """查找指定阶段选中文档，当前迭代没有则沿 parent 链向上查找。

    approved_only=True 时要求 status=approved（默认）
    """
    cur = iteration
    while cur:
        if stage >= cur.start_stage:
            filters = [
                Document.project_id == project_id,
                Document.iteration_id == cur.id,
                Document.stage == stage,
                Document.is_selected == True,
            ]
            if approved_only:
                filters.append(Document.status == "approved")
            q = await session.execute(select(Document).where(*filters))
            doc = q.scalar_one_or_none()
            if doc:
                return doc
            # 会话场景兜底：当前阶段允许取“最新文档”，即使未 selected
            if not approved_only:
                fallback_q = await session.execute(
                    select(Document).where(
                        Document.project_id == project_id,
                        Document.iteration_id == cur.id,
                        Document.stage == stage,
                    ).order_by(Document.updated_at.desc(), Document.created_at.desc()).limit(1)
                )
                fallback_doc = fallback_q.scalar_one_or_none()
                if fallback_doc:
                    return fallback_doc
        if cur.parent_iteration_id:
            cur = await session.get(Iteration, cur.parent_iteration_id)
        else:
            break
    fallback_filters = [
        Document.project_id == project_id,
        Document.iteration_id == (iteration.id if iteration else None),
        Document.stage == stage,
        Document.is_selected == True,
    ]
    if approved_only:
        fallback_filters.append(Document.status == "approved")
    q = await session.execute(select(Document).where(*fallback_filters))
    doc = q.scalar_one_or_none()
    if doc:
        return doc
    if not approved_only:
        loose_q = await session.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.iteration_id == (iteration.id if iteration else None),
                Document.stage == stage,
            ).order_by(Document.updated_at.desc(), Document.created_at.desc()).limit(1)
        )
        return loose_q.scalar_one_or_none()
    return None


async def _get_selected_doc_content(session: AsyncSession, project_id: str, stage: int, iteration_id: str | None) -> str | None:
    iteration = await session.get(Iteration, iteration_id) if iteration_id else None
    doc = await _find_selected_doc(session, project_id, stage, iteration)
    return doc.content if doc else None


import re


def _extract_relevant_section(doc: str, module_title: str, max_chars: int = 6000) -> str:
    """从技术方案文档中提取与模块相关的章节，避免全文传入"""
    if len(doc) <= max_chars:
        return doc

    # 按 markdown 标题分段
    sections = re.split(r'(?=^#{1,3}\s)', doc, flags=re.MULTILINE)
    keywords = [w for w in module_title.replace("-", " ").split() if len(w) > 1]

    scored: list[tuple[int, str]] = []
    for sec in sections:
        score = sum(1 for kw in keywords if kw.lower() in sec.lower())
        scored.append((score, sec))

    scored.sort(key=lambda x: -x[0])

    result = []
    total = 0
    for score, sec in scored:
        if total + len(sec) > max_chars:
            remain = max_chars - total
            if remain > 200:
                result.append(sec[:remain] + "…")
            break
        result.append(sec)
        total += len(sec)

    return "\n".join(result) or doc[:max_chars]


async def _resolve_dependencies(
    session: AsyncSession,
    raw_deps: list[list],
    idx_to_id: dict[int, str],
):
    """将 AI 返回的数组索引形式的 dependencies 转换为真实 task UUID。
    支持整数索引 (0, 1, 2) 和字符串索引 ("0", "1")。"""
    for idx, deps in enumerate(raw_deps):
        if not deps:
            continue
        resolved = []
        for d in deps:
            try:
                dep_idx = int(d)
            except (ValueError, TypeError):
                continue
            if dep_idx in idx_to_id:
                resolved.append(idx_to_id[dep_idx])
        if resolved:
            task = await session.get(Task, idx_to_id[idx])
            if task:
                task.dependencies = resolved


def _slugify(text: str, max_len: int = 30) -> str:
    s = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', text).strip('-').lower()
    return s[:max_len].rstrip('-') or "task"


def _subtask_semantic_key(title: str) -> str:
    normalized = " ".join((title or "").strip().lower().split())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized).strip()
    return normalized


def _subtask_item_id(module_task_id: str, title: str) -> str:
    raw = f"{module_task_id}:{_subtask_semantic_key(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


async def _collect_ref_docs(session: AsyncSession, project_id: str, iteration_id: str | None) -> list[dict]:
    """收集前置阶段中 is_selected 的文档引用信息"""
    refs = []
    for stage in range(4):
        q = await session.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.iteration_id == iteration_id,
                Document.stage == stage,
                Document.is_selected == True,
            )
        )
        doc = q.scalar_one_or_none()
        if doc:
            refs.append({"doc_id": doc.id, "title": doc.title, "stage": stage})
    return refs


async def _get_or_create_stage(session: AsyncSession, project_id: str, stage: int, iteration_id: str | None) -> StageProgress:
    q = await session.execute(
        select(StageProgress).where(
            StageProgress.project_id == project_id,
            StageProgress.iteration_id == iteration_id,
            StageProgress.stage == stage,
        )
    )
    sp = q.scalar_one_or_none()
    if not sp:
        sp = StageProgress(project_id=project_id, iteration_id=iteration_id, stage=stage, status="pending")
        session.add(sp)
        await session.flush()
    return sp


async def _generate_stage6_report(session: AsyncSession, project_id: str, iteration_id: str | None) -> dict:
    """收集任务信息和审核记录，调用 AI 生成质量报告"""
    token_tracker.set_context(project_id=project_id)
    tasks_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.parent_task_id != None,
        )
    )
    tasks = tasks_q.scalars().all()

    tasks_summary = "\n".join(
        f"- [{t.ref_id}] {t.title} | 状态:{t.status} | 角色:{t.suggested_role} | "
        f"测试:{('有' if getattr(t, 'test_results', None) else '无')}"
        for t in tasks
    )

    review_records = "\n".join(
        f"- [{t.ref_id}] 审核结果: {json.dumps(getattr(t, 'review_result', None) or {}, ensure_ascii=False)[:200]}"
        for t in tasks if getattr(t, 'review_result', None)
    )

    technical_doc = await _get_selected_doc_content(session, project_id, 3, iteration_id)

    try:
        return await ai_leader.generate_quality_report(
            tasks_summary=tasks_summary or "无任务记录",
            review_records=review_records or "无审核记录",
            technical_doc=technical_doc or "",
        )
    except Exception as e:
        logger.error(f"Quality report generation failed: {e}")
        return {"error": f"AI 生成质量报告失败: {type(e).__name__}: {e}"}


# ── Batch 展开子任务（Phase B 改进：batch + 单条容错 + 索引依赖） ──

BATCH_SIZE = 4


async def _expand_subtasks_in_batches(
    session: AsyncSession,
    gen_task: GenerationTask,
    module_task: Task,
    manifest_items: list[dict],
    manifest_titles: list[str],
    technical: str,
    requirements: str,
    prototype: str,
    all_modules_summary: str,
    discussion_summary: str,
    architect_model: str,
    prototype_fast_track: bool,
    project_id: str,
    iteration_id: str | None,
) -> tuple[list[dict], dict[str, dict], dict[str, str]]:
    """Batch 展开子任务：3-5 条/次，单条失败降级到单条重试，不中断整体流程。

    返回: (created_tasks, task_details_by_id, title_to_task_id)
    """
    ref_docs = module_task.ref_docs or []
    created_tasks: list[dict] = []
    task_details_by_id: dict[str, dict] = {}
    title_to_task_id: dict[str, str] = {}

    # 收集已存在的子任务
    existing_children_q = await session.execute(
        select(Task).where(Task.parent_task_id == module_task.id)
    )
    existing_children = list(existing_children_q.scalars())
    existing_by_manifest: dict[str, Task] = {}
    existing_by_key: dict[str, Task] = {}
    for child in existing_children:
        ctx = child.context or {}
        item_id = ctx.get("subtask_item_id")
        if item_id:
            existing_by_manifest[item_id] = child
        key = ctx.get("subtask_semantic_key") or _subtask_semantic_key(child.title)
        if key and key not in existing_by_key:
            existing_by_key[key] = child
        title_to_task_id[_subtask_semantic_key(child.title)] = child.id

    project = await session.get(Project, project_id)

    # 按 batch 处理
    pending = [i for i in manifest_items if i.get("status") != "done"]
    for batch_start in range(0, len(pending), BATCH_SIZE):
        batch = pending[batch_start : batch_start + BATCH_SIZE]
        batch_titles = [i.get("title", "") for i in batch]

        # ── Batch 展开 ──
        batch_results: list[dict | None] = [None] * len(batch)
        try:
            batch_results = await ai_leader.expand_task_batch(
                module_title=module_task.title,
                module_description=module_task.description,
                item_titles=batch_titles,
                manifest_titles=manifest_titles,
                technical_context=technical or "",
                requirements=requirements or "",
                prototype=prototype or "",
                all_modules_summary=all_modules_summary,
                discussion_context=discussion_summary,
                model=architect_model,
            )
        except Exception as e:
            logger.warning(f"Batch expand failed for {batch_titles}: {e}")
            # 全部降级到单条重试

        # ── 处理每条结果（batch 失败或 None 时降级单条） ──
        for item, detail in zip(batch, batch_results):
            item_title = (item.get("title", "") or "").strip()
            item_key = item.get("semantic_key") or _subtask_semantic_key(item_title)
            item_id = item.get("id") or _subtask_item_id(module_task.id, item_title)

            # 降级：batch 中某条为 None 或整个 batch 失败
            if detail is None:
                try:
                    detail = await ai_leader.expand_task_item(
                        module_title=module_task.title,
                        module_description=module_task.description,
                        item_title=item_title,
                        manifest_titles=manifest_titles,
                        technical_context=technical or "",
                        requirements=requirements or "",
                        prototype=prototype or "",
                        all_modules_summary=all_modules_summary,
                        discussion_context=discussion_summary,
                        model=architect_model,
                    )
                except Exception as e2:
                    logger.error(f"Single item fallback failed for '{item_title}': {e2}")
                    item["status"] = "failed"
                    item["error"] = str(e2)
                    continue

            # 统一后处理
            title = (detail.get("title", "") or item_title).strip() or item_title
            key = _subtask_semantic_key(title) or item_key
            ai_requires_design = bool(detail.get("requires_design_review"))
            ai_design_reason = str(detail.get("design_review_reason") or "").strip()

            existing = existing_by_manifest.get(item_id) or existing_by_key.get(key)
            if existing:
                existing_ctx = dict(existing.context or {})
                existing_ctx["ai_design_review_suggested"] = ai_requires_design
                existing_ctx["ai_design_review_reason"] = ai_design_reason
                existing.context = existing_ctx
                task_details_by_id[existing.id] = detail
                created_tasks.append({
                    "id": existing.id, "ref_id": existing.ref_id,
                    "title": existing.title, "git_branch": existing.git_branch,
                    "suggested_role": existing.suggested_role,
                    "estimated_hours": existing.estimated_hours,
                    "is_new": False,
                })
                title_to_task_id[key] = existing.id
            else:
                project.task_seq = (project.task_seq or 0) + 1
                ref_id = f"TASK-{project.task_seq:03d}"
                slug = _slugify(title or "task")
                t_type = detail.get("type", "feature")
                task_type = "feat" if t_type in ("feature", "docs") else "fix"
                git_branch = f"{task_type}/{ref_id}-{slug}"
                t_complexity = detail.get("complexity", "")
                t_min_tier = task_min_tier(t_type, t_complexity)

                task_ctx = {
                    "subtask_item_id": item_id,
                    "subtask_semantic_key": key,
                    "subtask_manifest_version": 1,
                    "ai_design_review_suggested": ai_requires_design,
                    "ai_design_review_reason": ai_design_reason,
                }
                if prototype_fast_track:
                    task_ctx["design_phase"] = "none"
                elif ai_requires_design:
                    task_ctx["design_phase"] = "needs_discussion"
                else:
                    task_ctx["design_phase"] = "none"

                new_task = Task(
                    project_id=project_id, iteration_id=iteration_id,
                    ref_id=ref_id, parent_task_id=module_task.id,
                    ref_docs=ref_docs, git_branch=git_branch,
                    title=title,
                    description=detail.get("description", ""),
                    type=t_type, priority=detail.get("priority", 0),
                    status="pending",
                    suggested_role=detail.get("suggested_role", "mid"),
                    complexity=t_complexity or "medium",
                    suggested_model="opus" if t_complexity == "high" else None,
                    min_tier=t_min_tier,
                    estimated_hours=detail.get("estimated_hours", 0.5),
                    dependencies=[],
                    input_files=detail.get("input_files", []),
                    output_files=detail.get("output_files", []),
                    acceptance_criteria=detail.get("acceptance_criteria", []),
                    context=task_ctx,
                    requires_design_review=(False if prototype_fast_track else ai_requires_design),
                )
                session.add(new_task)
                await session.flush()
                task_details_by_id[new_task.id] = detail
                existing_by_manifest[item_id] = new_task
                existing_by_key[key] = new_task
                title_to_task_id[key] = new_task.id
                created_tasks.append({
                    "id": new_task.id, "ref_id": ref_id,
                    "title": new_task.title, "git_branch": git_branch,
                    "suggested_role": new_task.suggested_role,
                    "estimated_hours": new_task.estimated_hours,
                    "is_new": True,
                })

            item["status"] = "done"
            done_count = sum(1 for i in manifest_items if i.get("status") == "done")
            total_count = len(manifest_items) or 1
            gen_task.progress = min(95, 40 + int(done_count * 50 / total_count))
            module_ctx = module_task.context or {}
            module_ctx["subtask_manifest"] = {
                "module_id": module_task.id,
                "version": 1,
                "items": manifest_items,
            }
            module_task.context = module_ctx
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(module_task, "context")
            flag_modified(gen_task, "progress")
            await session.commit()

    return created_tasks, task_details_by_id, title_to_task_id


# ── 阶段推荐模型 ──

@router.get("/{stage}/recommend-model")
async def get_recommended_model(stage: int):
    """获取阶段推荐模型"""
    rec = recommend_model_for_stage(stage)
    target_tier = STAGE_TIER_MAP.get(stage, 3)
    return {
        "stage": stage,
        "target_tier": target_tier,
        "target_tier_label": TIER_LABELS.get(target_tier, "标准"),
        "recommendation": rec,
        "all_models": get_all_models_with_tier(),
    }
