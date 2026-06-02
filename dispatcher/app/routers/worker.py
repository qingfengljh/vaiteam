"""Worker API — CC Worker 轮询任务与心跳。

端点：
- GET  /api/worker/task-poll?agent_id=xxx   — Worker 拉取分配给自己的任务
- POST /api/worker/heartbeat                — Worker 心跳上报
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models import Agent, Project, Task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/worker", tags=["worker"])


@router.get("/task-poll")
async def task_poll(
    agent_id: str = Query(..., description="Agent ID"),
    session: AsyncSession = Depends(get_session),
):
    """Worker 轮询获取分配给自己的任务。

    查找 assigned_agent == agent_id 且 status == "assigned" 的任务。
    找到后更新 status 为 "executing"，并返回任务包。
    """
    if not agent_id:
        raise HTTPException(400, "agent_id required")

    # 1. 查找 Agent
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")

    # 2. 查找分配给该 Agent 且状态为 assigned 的任务
    result = await session.execute(
        select(Task)
        .where(Task.assigned_agent == agent_id)
        .where(Task.status == "assigned")
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(1)
    )
    task = result.scalar_one_or_none()

    if task is None:
        return {"task": None}

    # 3. 更新状态为 executing
    task.status = "executing"
    task.updated_at = datetime.now(timezone.utc)
    agent.status = "busy"
    agent.current_task_id = task.id
    agent.last_started_at = datetime.now(timezone.utc)
    await session.commit()

    # 4. 获取项目信息（用于 git_repo）
    project = await session.get(Project, task.project_id)
    git_repo_url = project.git_repo if project else ""

    # 5. 构造任务包
    ctx = task.context or {}

    # ── 任务级模型配置（从 Agent Provider 解析）──
    model_config = ctx.get("model_config", {})

    task_pack = {
        "id": task.id,
        "task_id": task.id,
        "ref_id": task.ref_id or "",
        "title": task.title,
        "description": task.description or "",
        "instruction": ctx.get("instruction", task.description or ""),
        "type": task.type or "feature",
        "status": task.status,
        "scope": ctx.get("scope", ""),
        "acceptance": ctx.get("acceptance", "") or "\n".join(task.acceptance_criteria or []),
        "forbidden_paths": ctx.get("forbidden_paths", []),
        "allowed_paths": ctx.get("allowed_paths", []),
        "context_keys": ctx.get("context_keys", []),
        "git_repo": git_repo_url,
        "git_branch": task.git_branch or "",
        "git_base_branch": ctx.get("git_base_branch", "develop"),
        "git_merge_target": ctx.get("git_merge_target", "develop"),
        "files": task.input_files or [],
        "output_files": task.output_files or [],
        "suggested_role": task.suggested_role or agent.role,
        "suggested_model": task.suggested_model or "",
        "complexity": getattr(task, "complexity", "medium") or "medium",
        "requires_human": ctx.get("requires_human_review", False) or ctx.get("actor_type") == "human",
        "executor_hint": ctx.get("executor_hint", "claude_code"),
        "actor_type": ctx.get("actor_type", "claude_code"),
        # ── 任务级模型配置 ──
        "model_config": model_config,
        "metadata": {
            "attempt_id": ctx.get("current_attempt_id", ""),
            "project_id": task.project_id,
            "iteration_id": task.iteration_id,
        },
    }

    logger.info(f"Task {task.id} polled by {agent_id}, status -> executing")
    return {"task": task_pack}


class HeartbeatBody:
    def __init__(
        self,
        agent_id: str,
        role: str = "",
        status: str = "idle",
        current_task_id: str = "",
        metadata: dict | None = None,
    ):
        self.agent_id = agent_id
        self.role = role
        self.status = status
        self.current_task_id = current_task_id
        self.metadata = metadata or {}


@router.post("/heartbeat")
async def worker_heartbeat(
    body: dict,
    session: AsyncSession = Depends(get_session),
):
    """Worker 心跳上报。更新 Agent 心跳状态。"""
    agent_id = body.get("agent_id", "").strip()
    if not agent_id:
        raise HTTPException(400, "agent_id required")

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")

    now = datetime.now(timezone.utc)
    agent.last_heartbeat = now
    agent.last_heartbeat_status = "online"

    # 可选：更新当前状态
    status = body.get("status", "").strip()
    if status:
        agent.status = status

    current_task_id = body.get("current_task_id", "").strip()
    if current_task_id:
        agent.current_task_id = current_task_id
    elif status == "idle":
        agent.current_task_id = None

    await session.commit()

    return {
        "ok": True,
        "agent_id": agent_id,
        "heartbeat_at": now.isoformat(),
        "status": agent.status,
    }


# ── 群聊 @mention ──

@router.get("/chat-poll")
async def chat_poll(
    agent_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Worker 轮询获取 @mention 自己的群聊消息（未回复的）。"""
    from app.models import TeamChat

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")

    # 查该 Agent 被 @ 的消息：sent 或 24h 内 delivered 但未被该 Agent 回复过
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    q = await session.execute(
        select(TeamChat).where(
            TeamChat.project_id == agent.project_id,
            TeamChat.mentions.contains([agent_id]),
            TeamChat.created_at >= cutoff,
            TeamChat.status.in_(["sent", "delivered"]),
        ).order_by(TeamChat.created_at.asc()).limit(10)
    )
    candidates = list(q.scalars())

    # 过滤掉已经有该 Agent 回复的消息
    replied_ids: set[str] = set()
    if candidates:
        candidate_ids = [m.id for m in candidates]
        replies_q = await session.execute(
            select(TeamChat.reply_to).where(
                TeamChat.reply_to.in_(candidate_ids),
                TeamChat.sender_id == agent_id,
            )
        )
        replied_ids = {row[0] for row in replies_q if row[0]}

    messages = []
    for msg in candidates:
        if msg.id in replied_ids:
            continue
        messages.append({
            "id": msg.id,
            "content": msg.content,
            "sender_id": msg.sender_id,
            "sender_type": msg.sender_type,
            "task_ref": msg.task_ref,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        })
        if msg.status == "sent":
            msg.status = "delivered"

    await session.commit()
    return {"messages": messages}


class ChatReplyBody(BaseModel):
    agent_id: str
    content: str
    reply_to: str  # 原消息 ID


@router.post("/chat-reply")
async def chat_reply(body: ChatReplyBody, session: AsyncSession = Depends(get_session)):
    """Worker 回复群聊消息。"""
    agent = await session.get(Agent, body.agent_id)
    if not agent:
        raise HTTPException(404)

    from app.models import TeamChat, new_id
    reply = TeamChat(
        id=new_id(),
        project_id=agent.project_id,
        sender_type="agent",
        sender_id=body.agent_id,
        content=body.content,
        reply_to=body.reply_to,
        status="sent",
    )
    session.add(reply)
    await session.commit()
    return {"id": reply.id, "status": "sent"}
