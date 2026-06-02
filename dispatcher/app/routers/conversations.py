"""深度对话（私聊）API — 人类与单个 Agent 的 1-on-1 长对话"""

import logging
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import get_session
from app.models import (
    Project, Agent, Task, Conversation, ConversationMessage, UploadedFile,
    GenerationTask, Iteration,
)
from app.services import ai_leader, task_docs, git_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects/{project_id}/conversations", tags=["conversations"])


class ConversationCreate(BaseModel):
    agent_id: str
    task_id: str | None = None
    topic: str = ""


class MessageSend(BaseModel):
    content: str
    file_ids: list[str] = []


class DesignApproval(BaseModel):
    approved_by: str = "human"


@router.post("")
async def create_conversation(
    project_id: str,
    body: ConversationCreate,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    conv = Conversation(
        project_id=project_id,
        agent_id=body.agent_id,
        task_id=body.task_id,
        topic=body.topic,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return _conv_dict(conv)


@router.get("")
async def list_conversations(
    project_id: str,
    status: str = "",
    agent_id: str = "",
    task_id: str = "",
    session: AsyncSession = Depends(get_session),
):
    q = select(Conversation).where(Conversation.project_id == project_id)
    if status:
        q = q.where(Conversation.status == status)
    if agent_id:
        q = q.where(Conversation.agent_id == agent_id)
    if task_id:
        q = q.where(Conversation.task_id == task_id)
    q = q.order_by(Conversation.updated_at.desc())
    result = await session.execute(q)
    return [_conv_dict(c) for c in result.scalars()]


@router.get("/{conv_id}")
async def get_conversation(
    project_id: str, conv_id: str,
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)
    return _conv_dict(conv)


@router.get("/{conv_id}/messages")
async def list_messages(
    project_id: str, conv_id: str,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)
    q = (
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv_id)
        .order_by(ConversationMessage.created_at)
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(q)
    return [_msg_dict(m) for m in result.scalars()]


@router.post("/{conv_id}/messages")
async def send_message(
    project_id: str, conv_id: str,
    body: MessageSend,
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)

    file_descs = []
    has_image_file = False
    if body.file_ids:
        for fid in body.file_ids:
            uf = await session.get(UploadedFile, fid)
            if uf:
                if uf.is_image:
                    has_image_file = True
                file_descs.append(f"[附件: {uf.original_name}]\n{uf.description}")

    msg = ConversationMessage(
        conversation_id=conv_id,
        sender_type="human",
        sender_id="human",
        content=body.content,
        file_ids=body.file_ids,
    )
    session.add(msg)
    await session.flush()

    full_content = body.content
    if file_descs:
        full_content += "\n\n" + "\n\n".join(file_descs)

    history = await _build_history(session, conv_id)
    history.append({"role": "user", "content": full_content})

    agent = await _find_agent(session, project_id, conv.agent_id)
    agent_name = agent.id if agent else conv.agent_id

    from app.services.model_pool import (
        resolve_model,
        get_client,
        supports_vision,
        resolve_vision_model,
    )
    from app.services import ai_leader

    role = agent.role if agent else "mid"
    model = resolve_model(role)
    if not model:
        raise HTTPException(503, f"角色 {role} 没有可用模型")
    if has_image_file and not supports_vision(model):
        vision_model = resolve_vision_model(model)
        if vision_model:
            logger.info(
                "Conversation %s detected image attachments, switch model %s -> %s",
                conv_id,
                model,
                vision_model,
            )
            model = vision_model

    system_prompt = f"你是 {agent_name}（角色: {role}），正在与人类进行深度技术讨论。"
    if conv.topic:
        system_prompt += f"\n讨论主题: {conv.topic}"
    task = await session.get(Task, conv.task_id) if conv.task_id else None
    if task:
        system_prompt += f"\n关联任务: [{task.ref_id}] {task.title}\n{task.description or ''}"

    messages = [{"role": "system", "content": system_prompt}] + history

    client, actual_model = get_client(model)

    async def _stream():
        full_reply = []
        try:
            resp = await client.chat.completions.create(
                model=actual_model,
                messages=messages,
                stream=True,
                max_tokens=4096,
            )
            async for chunk in resp:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    full_reply.append(delta.content)
                    yield f"data: {delta.content}\n\n"
        except Exception as e:
            logger.error(f"Conversation stream error: {e}")
            yield f"data: [AI 响应错误: {e}]\n\n"

        reply_text = "".join(full_reply) or "[无响应]"
        async with (await _get_session()) as s:
            reply_msg = ConversationMessage(
                conversation_id=conv_id,
                sender_type="agent",
                sender_id=conv.agent_id,
                content=reply_text,
            )
            s.add(reply_msg)
            c = await s.get(Conversation, conv_id)
            if c:
                from datetime import datetime, timezone
                c.updated_at = datetime.now(timezone.utc)
            await s.commit()
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{conv_id}/archive")
async def archive_conversation(
    project_id: str, conv_id: str,
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)
    conv.status = "archived"
    await session.commit()
    return {"status": "archived"}


@router.post("/{conv_id}/archive-async")
async def archive_conversation_async(
    project_id: str, conv_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """复用 GenerationTask 机制异步归档深度对话并沉淀过程文档。"""
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    task = GenerationTask(
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        stage=9,  # 9: conversation archive workflow
        doc_title=f"[CONVERSATION] {conv.topic or conv.id}",
        status="running",
        model_used="",
        steps=[
            {"name": "收集会话记录", "status": "pending"},
            {"name": "生成会话总结", "status": "pending"},
            {"name": "提取需求草案", "status": "pending"},
            {"name": "提取详细设计草案", "status": "pending"},
            {"name": "归档过程文档", "status": "pending"},
            {"name": "更新任务设计阶段", "status": "pending"},
            {"name": "更新会话状态", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    background_tasks.add_task(_run_archive_generation_task, task.id, project_id, conv_id)
    return {"task_id": task.id}


@router.post("/{conv_id}/conclude")
async def conclude_with_doc(
    project_id: str, conv_id: str,
    body: dict,
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)
    conv.conclusion_doc = body.get("conclusion", "")
    conv.status = "archived"
    await session.commit()
    return {"status": "archived", "conclusion_doc": conv.conclusion_doc}


@router.post("/{conv_id}/approve-design")
async def approve_design(
    project_id: str, conv_id: str,
    body: DesignApproval,
    session: AsyncSession = Depends(get_session),
):
    conv = await session.get(Conversation, conv_id)
    if not conv or conv.project_id != project_id:
        raise HTTPException(404)

    if not conv.task_id:
        raise HTTPException(400, "该对话未关联任务")

    task = await session.get(Task, conv.task_id)
    if not task:
        raise HTTPException(404, "关联任务不存在")

    task.design_approved = True
    task.design_approved_by = body.approved_by
    task.design_approved_at = datetime.now(timezone.utc)
    task.requires_design_review = True
    ctx = dict(task.context or {})
    ctx["design_phase"] = "approved"
    task.context = ctx
    flag_modified(task, "context")
    await session.commit()
    return {"status": "approved", "task_id": task.id}


async def _build_history(session: AsyncSession, conv_id: str) -> list[dict]:
    q = (
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv_id)
        .order_by(ConversationMessage.created_at)
    )
    result = await session.execute(q)
    msgs = []
    for m in result.scalars():
        role = "assistant" if m.sender_type == "agent" else "user"
        msgs.append({"role": role, "content": m.content})
    return msgs


async def _find_agent(session: AsyncSession, project_id: str, agent_id: str):
    q = select(Agent).where(Agent.project_id == project_id, Agent.id == agent_id)
    result = await session.execute(q)
    return result.scalar_one_or_none()


async def _get_session():
    from app.core.database import async_session
    return async_session()


class TaskCancelled(Exception):
    pass


async def _update_step(session: AsyncSession, task: GenerationTask, index: int, status: str):
    await session.refresh(task)
    if task.status == "cancelled":
        raise TaskCancelled("Task cancelled by user")
    task.steps[index]["status"] = status
    task.progress = int((sum(1 for s in task.steps if s["status"] == "completed") / len(task.steps)) * 100)
    flag_modified(task, "steps")
    await session.commit()


def _build_conversation_markdown(conv: Conversation, messages: list[ConversationMessage], summary: str) -> str:
    lines = [
        f"# 深度对话归档：{conv.topic or conv.id}",
        "",
        "## 会话总结",
        "",
        summary.strip() or "（无总结）",
        "",
        "## 会话原始记录",
        "",
    ]
    for m in messages:
        sender = "Agent" if m.sender_type == "agent" else "Human"
        ts = m.created_at.isoformat() if m.created_at else ""
        lines.extend([
            f"### [{sender}] {m.sender_id}  ({ts})",
            "",
            m.content or "",
            "",
        ])
    return "\n".join(lines).strip()


def _build_requirement_prompt() -> str:
    return (
        "请基于深度对话内容提炼“需求草案”（Markdown），结构固定为：\n"
        "## 目标\n## 功能范围\n## 用户故事\n## 验收标准\n## 边界与约束\n## 非功能要求\n"
        "要求：只输出草案正文，不要解释。缺失信息明确标注“待确认”。"
    )


def _build_design_prompt() -> str:
    return (
        "请基于深度对话内容提炼“详细设计草案”（Markdown），结构固定为：\n"
        "## 技术方案概述\n## 数据模型\n## 接口设计\n## 流程图\n## 时序图\n## 状态机（如适用）\n"
        "## 风险与回滚策略\n## 文件清单\n"
        "其中流程图/时序图/状态机必须使用 Mermaid 代码块（```mermaid）。\n"
        "要求：只输出草案正文，不要解释。缺失信息明确标注“待确认”。"
    )


async def _run_archive_generation_task(task_id: str, project_id: str, conv_id: str):
    async with (await _get_session()) as session:
        task = await session.get(GenerationTask, task_id)
        conv = await session.get(Conversation, conv_id)
        project = await session.get(Project, project_id)
        if not task or not conv or not project:
            return
        try:
            await _update_step(session, task, 0, "running")
            q = (
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conv_id)
                .order_by(ConversationMessage.created_at)
            )
            msgs_result = await session.execute(q)
            msgs = list(msgs_result.scalars())
            await _update_step(session, task, 0, "completed")

            await _update_step(session, task, 1, "running")
            transcript = "\n".join(
                f"[{m.sender_type}] {m.sender_id}: {m.content}"
                for m in msgs[-120:]  # 控制上下文长度
            )
            prompt = (
                "请基于以下深度对话内容输出结构化总结（Markdown）：\n"
                "1) 关键结论\n2) 已确认决策\n3) 待办与责任人\n4) 未决问题与风险\n"
                "注意：仅输出总结正文，不要输出解释。"
            )
            summary = await ai_leader._call(prompt, transcript, max_tokens=2048, auto_continue=True, temperature=0.2)
            await _update_step(session, task, 1, "completed")

            requirement_doc = None
            design_doc = None
            linked_task = await session.get(Task, conv.task_id) if conv.task_id else None
            iter_seq = "default"
            if project.current_iteration_id:
                it = await session.get(Iteration, project.current_iteration_id)
                if it:
                    iter_seq = it.seq

            await _update_step(session, task, 2, "running")
            if linked_task:
                requirement_md = await ai_leader._call(
                    _build_requirement_prompt(),
                    transcript,
                    max_tokens=2600,
                    auto_continue=True,
                    temperature=0.2,
                )
                requirement_doc = await task_docs.archive(
                    session,
                    project_id=project_id,
                    iteration_id=project.current_iteration_id,
                    iteration_seq=iter_seq,
                    task_id=conv.task_id,
                    ref_id=f"conv-{conv.id}",
                    doc_type="requirement_draft",
                    title=f"需求草案_{conv.topic or conv.id}",
                    content=requirement_md,
                    summary=(requirement_md or "")[:500],
                    tags=["conversation", "requirement", "draft"],
                    metadata={
                        "conversation_id": conv.id,
                        "agent_id": conv.agent_id,
                        "task_id": conv.task_id or "",
                    },
                )
            await _update_step(session, task, 2, "completed")

            await _update_step(session, task, 3, "running")
            if linked_task:
                design_md = await ai_leader._call(
                    _build_design_prompt(),
                    transcript,
                    max_tokens=3200,
                    auto_continue=True,
                    temperature=0.2,
                )
                design_doc = await task_docs.archive(
                    session,
                    project_id=project_id,
                    iteration_id=project.current_iteration_id,
                    iteration_seq=iter_seq,
                    task_id=conv.task_id,
                    ref_id=f"conv-{conv.id}",
                    doc_type="design_draft",
                    title=f"详细设计草案_{conv.topic or conv.id}",
                    content=design_md,
                    summary=(design_md or "")[:500],
                    tags=["conversation", "design", "draft"],
                    metadata={
                        "conversation_id": conv.id,
                        "agent_id": conv.agent_id,
                        "task_id": conv.task_id or "",
                    },
                )
            await _update_step(session, task, 3, "completed")

            await _update_step(session, task, 4, "running")
            title = f"深度对话总结_{conv.topic or conv.id}"
            content = _build_conversation_markdown(conv, msgs, summary)
            archived_doc = await task_docs.archive(
                session,
                project_id=project_id,
                iteration_id=project.current_iteration_id,
                iteration_seq=iter_seq,
                task_id=conv.task_id,
                ref_id=f"conv-{conv.id}",
                doc_type="deep_conversation_summary",
                title=title,
                content=content,
                summary=(summary or "")[:500],
                tags=["conversation", "deep_chat", "summary"],
                metadata={
                    "conversation_id": conv.id,
                    "agent_id": conv.agent_id,
                    "task_id": conv.task_id or "",
                    "status": "pending_review",
                },
            )
            git_synced = False
            git_path = ""
            if project.git_repo:
                try:
                    await git_repo.ensure_repo(project.id, project.git_repo)
                    safe_topic = (conv.topic or conv.id).replace("/", "_").replace(" ", "_")[:60]
                    git_path = f"docs/20_conversations/{safe_topic}_{conv.id}.md"
                    commit_msg = git_repo.build_commit_message(
                        "docs",
                        f"archive deep conversation {conv.id}",
                        scope="conversations",
                        iteration=f"v{iter_seq}",
                        stage=9,
                        author="ai/leader",
                    )
                    result = await git_repo.commit_and_push(
                        project.id,
                        git_path,
                        content,
                        message=commit_msg,
                    )
                    git_synced = bool(result.get("ok"))
                except Exception as e:
                    logger.warning(f"Conversation git sync failed (non-blocking): {e}")
            if git_synced or git_path:
                meta = dict(archived_doc.metadata_ or {})
                meta["git_synced"] = git_synced
                meta["git_path"] = git_path
                archived_doc.metadata_ = meta
            await session.commit()
            await _update_step(session, task, 4, "completed")

            await _update_step(session, task, 5, "running")
            if linked_task:
                task_ctx = dict(linked_task.context or {})
                task_ctx["design_phase"] = "needs_review"
                docs = list(task_ctx.get("design_docs") or [])
                if requirement_doc:
                    docs.append({
                        "doc_id": requirement_doc.id,
                        "doc_type": "requirement_draft",
                        "title": requirement_doc.title,
                        "created_at": requirement_doc.created_at.isoformat() if requirement_doc.created_at else "",
                    })
                if design_doc:
                    docs.append({
                        "doc_id": design_doc.id,
                        "doc_type": "design_draft",
                        "title": design_doc.title,
                        "created_at": design_doc.created_at.isoformat() if design_doc.created_at else "",
                    })
                task_ctx["design_docs"] = docs
                linked_task.context = task_ctx
                linked_task.requires_design_review = True
                linked_task.design_approved = False
                linked_task.design_approved_by = ""
                linked_task.design_approved_at = None
                flag_modified(linked_task, "context")
                await session.commit()
            await _update_step(session, task, 5, "completed")

            await _update_step(session, task, 6, "running")
            conv.conclusion_doc = summary or ""
            conv.status = "archived"
            conv.updated_at = datetime.now(timezone.utc)
            await _update_step(session, task, 6, "completed")

            task.status = "completed"
            task.progress = 100
            task.error_message = json.dumps({
                "conversation_id": conv.id,
                "task_document_id": archived_doc.id,
                "doc_type": "deep_conversation_summary",
                "requirement_doc_id": requirement_doc.id if requirement_doc else "",
                "design_doc_id": design_doc.id if design_doc else "",
                "git_synced": git_synced,
                "git_path": git_path,
            }, ensure_ascii=False)
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()
        except TaskCancelled:
            logger.info(f"Conversation archive task {task_id} cancelled by user")
            return
        except Exception as e:
            logger.error(f"Conversation archive task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            flag_modified(task, "steps")
            await session.commit()


def _conv_dict(c: Conversation) -> dict:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "agent_id": c.agent_id,
        "task_id": c.task_id,
        "topic": c.topic,
        "status": c.status,
        "conclusion_doc": c.conclusion_doc,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


def _msg_dict(m: ConversationMessage) -> dict:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "sender_type": m.sender_type,
        "sender_id": m.sender_id,
        "content": m.content,
        "file_ids": m.file_ids or [],
        "created_at": m.created_at.isoformat(),
    }
