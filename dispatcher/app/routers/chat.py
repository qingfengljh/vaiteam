"""团队群聊 API — 人类与 AI Agent 实时沟通"""

import re
import logging
import asyncio
import json as json_mod
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session, async_session
from app.models import TeamChat, Agent, Task, new_id
from app.services import mq

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

MENTION_RE = re.compile(r"@([\w\u4e00-\u9fff\-]+)")
TASK_REF_RE = re.compile(r"#(TASK-\w+|\w{6,8})")

ROLE_ALIAS = {
    "架构师": "architect", "architect": "architect",
    "高级": "senior", "senior": "senior",
    "中级": "mid", "mid": "mid",
    "初级": "junior", "junior": "junior",
    "运维": "devops", "devops": "devops",
    "测试": "tester", "tester": "tester",
    "全部": "__all__", "all": "__all__",
}


class SendChatBody(BaseModel):
    content: str
    sender_id: str = "admin"
    reply_to: str | None = None


def _parse_mentions(content: str) -> list[str]:
    return MENTION_RE.findall(content)


def _parse_task_ref(content: str) -> str:
    m = TASK_REF_RE.search(content)
    return m.group(1) if m else ""


async def _resolve_agent(
    session: AsyncSession, project_id: str, mention: str
) -> Agent | None:
    """将 @mention 解析为具体 Agent：先匹配 ID，再匹配角色别名"""
    agent = await session.get(Agent, mention)
    if agent and agent.project_id == project_id:
        return agent

    role = ROLE_ALIAS.get(mention)
    if role and role != "__all__":
        # 优先选择默认团队的成员
        from app.models import AgentTeam
        dt = await session.execute(
            select(AgentTeam.id).where(
                AgentTeam.project_id == project_id, AgentTeam.is_default == True
            ).limit(1)
        )
        default_team_id = dt.scalar_one_or_none()
        q = await session.execute(
            select(Agent).where(
                Agent.project_id == project_id, Agent.role == role
            ).order_by(
                # 默认团队成员优先
                Agent.team_id != default_team_id if default_team_id else True,
                Agent.id
            ).limit(1)
        )
        return q.scalar_one_or_none()
    return None


async def _find_architect(session: AsyncSession, project_id: str) -> Agent | None:
    q = await session.execute(
        select(Agent).where(
            Agent.project_id == project_id, Agent.role == "architect"
        ).limit(1)
    )
    return q.scalar_one_or_none()


def _chat_to_dict(c: TeamChat) -> dict:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "sender_type": c.sender_type,
        "sender_id": c.sender_id,
        "mentions": c.mentions,
        "task_ref": c.task_ref,
        "content": c.content,
        "reply_to": c.reply_to,
        "status": c.status,
        "metadata": c.metadata_,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.post("/{project_id}")
async def send_chat(
    project_id: str,
    body: SendChatBody,
    session: AsyncSession = Depends(get_session),
):
    if not body.content.strip():
        raise HTTPException(400, "消息不能为空")

    raw_mentions = _parse_mentions(body.content)
    task_ref = _parse_task_ref(body.content)

    resolved_agents: list[Agent] = []
    mention_ids: list[str] = []

    for m in raw_mentions:
        alias = ROLE_ALIAS.get(m)
        if alias == "__all__":
            arch = await _find_architect(session, project_id)
            if arch:
                resolved_agents.append(arch)
                mention_ids.append(arch.id)
            break
        agent = await _resolve_agent(session, project_id, m)
        if agent and agent.id not in mention_ids:
            resolved_agents.append(agent)
            mention_ids.append(agent.id)

    if not resolved_agents:
        arch = await _find_architect(session, project_id)
        if arch:
            resolved_agents.append(arch)
            mention_ids.append(arch.id)

    chat = TeamChat(
        id=new_id(),
        project_id=project_id,
        sender_type="human",
        sender_id=body.sender_id,
        mentions=mention_ids,
        task_ref=task_ref,
        content=body.content,
        reply_to=body.reply_to,
        status="sent",
    )
    session.add(chat)
    await session.flush()

    plain_content = body.content
    for m in raw_mentions:
        plain_content = plain_content.replace(f"@{m}", "", 1)
    plain_content = TASK_REF_RE.sub("", plain_content).strip()

    if task_ref:
        q = await session.execute(
            select(Task).where(Task.ref_id == task_ref).limit(1)
        )
        task = q.scalar_one_or_none()
        if not task:
            q = await session.execute(
                select(Task).where(Task.id == task_ref).limit(1)
            )
            task = q.scalar_one_or_none()
    else:
        task = None

    delivered = False
    for agent in resolved_agents:
        try:
            await mq.ensure_inbox_group(agent.id)
            await mq.publish_to_inbox(agent.id, {
                "msg_id": chat.id,
                "task_id": task.id if task else "",
                "project_id": project_id,
                "from": f"human:{body.sender_id}",
                "to": agent.id,
                "type": "human_message",
                "payload": {
                    "content": plain_content or body.content,
                    "task_ref": task_ref,
                    "chat_msg_id": chat.id,
                },
            })
            delivered = True
        except Exception as e:
            logger.warning(f"Failed to deliver chat to {agent.id}: {e}")

    chat.status = "delivered" if delivered else "failed"
    await session.commit()

    return _chat_to_dict(chat)


@router.get("/{project_id}")
async def get_chat_history(
    project_id: str,
    limit: int = Query(50, le=200),
    before: str | None = None,
    agent_id: str | None = None,
    task_ref: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(TeamChat).where(TeamChat.project_id == project_id)

    if before:
        ref = await session.get(TeamChat, before)
        if ref:
            stmt = stmt.where(TeamChat.created_at < ref.created_at)

    if agent_id:
        stmt = stmt.where(
            (TeamChat.sender_id == agent_id)
            | (TeamChat.mentions.contains([agent_id]))
        )

    if task_ref:
        stmt = stmt.where(TeamChat.task_ref == task_ref)

    stmt = stmt.order_by(TeamChat.created_at.desc()).limit(limit + 1)
    q = await session.execute(stmt)
    rows = list(q.scalars().all())

    has_more = len(rows) > limit
    messages = rows[:limit]
    messages.reverse()

    return {
        "messages": [_chat_to_dict(m) for m in messages],
        "has_more": has_more,
    }


@router.get("/{project_id}/poll")
async def poll_new_messages(
    project_id: str,
    after: str,
    session: AsyncSession = Depends(get_session),
):
    ref = await session.get(TeamChat, after)
    if not ref:
        return {"messages": []}

    stmt = (
        select(TeamChat)
        .where(
            TeamChat.project_id == project_id,
            TeamChat.created_at > ref.created_at,
        )
        .order_by(TeamChat.created_at)
        .limit(100)
    )
    q = await session.execute(stmt)
    rows = list(q.scalars().all())
    return {"messages": [_chat_to_dict(m) for m in rows]}


@router.get("/{project_id}/stream")
async def stream_new_messages(
    project_id: str,
    request: Request,
    after: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    last_created_at = None
    if after:
        ref = await session.get(TeamChat, after)
        if ref and ref.project_id == project_id:
            last_created_at = ref.created_at

    async def event_generator():
        nonlocal last_created_at
        while True:
            if await request.is_disconnected():
                return
            async with async_session() as s:
                stmt = select(TeamChat).where(TeamChat.project_id == project_id)
                if last_created_at:
                    stmt = stmt.where(TeamChat.created_at > last_created_at)
                stmt = stmt.order_by(TeamChat.created_at).limit(100)
                q = await s.execute(stmt)
                rows = list(q.scalars().all())
                if rows:
                    last_created_at = rows[-1].created_at
                    payload = [_chat_to_dict(m) for m in rows]
                    yield f"event: messages\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
                else:
                    yield "event: ping\ndata: {}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class AgentReplyBody(BaseModel):
    agent_id: str
    content: str
    chat_msg_id: str | None = None


@router.post("/{project_id}/agent-reply")
async def agent_reply(
    project_id: str,
    body: AgentReplyBody,
    session: AsyncSession = Depends(get_session),
):
    """Agent 回复写入群聊（由 Connector 回调）"""
    if body.chat_msg_id:
        orig = await session.get(TeamChat, body.chat_msg_id)
        if orig:
            orig.status = "replied"

    agent = await session.get(Agent, body.agent_id)
    chat = TeamChat(
        id=new_id(),
        project_id=project_id,
        sender_type="agent",
        sender_id=body.agent_id,
        mentions=[],
        content=body.content,
        reply_to=body.chat_msg_id,
        status="sent",
        metadata_={"role": agent.role if agent else "unknown"},
    )
    session.add(chat)
    await session.commit()
    return _chat_to_dict(chat)
