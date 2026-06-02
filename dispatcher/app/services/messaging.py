"""
面向任务的 Agent 间异步通信服务

每条消息必须关联一个 task_id，所有通信围绕具体任务展开。
消息通过 Redis Stream 投递（agent:inbox:{agent_id}），同时持久化到 PostgreSQL。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentMessage, new_id
from app.services import mq

logger = logging.getLogger(__name__)

MSG_TYPES = {
    "assign_task", "task_accepted", "task_progress", "task_completed", "task_failed",
    "review_request", "review_result", "request_help", "help_response",
    "revise_request", "human_message", "ack",
    "cc_task_dispatch",
}


async def send(
    session: AsyncSession,
    *,
    task_id: str,
    project_id: str,
    from_id: str,
    to_id: str,
    msg_type: str,
    payload: dict | None = None,
    ref_msg_id: str | None = None,
) -> AgentMessage:
    """发送一条消息：写 DB + 推 Redis inbox"""
    msg = AgentMessage(
        id=new_id(),
        task_id=task_id,
        project_id=project_id,
        from_id=from_id,
        to_id=to_id,
        msg_type=msg_type,
        payload=payload or {},
        ref_msg_id=ref_msg_id,
        status="pending",
    )
    session.add(msg)
    await session.flush()

    # 如果是回复，标记原消息为 replied
    if ref_msg_id:
        orig = await session.get(AgentMessage, ref_msg_id)
        if orig and orig.status == "pending":
            orig.status = "replied"
            orig.replied_at = datetime.now(timezone.utc)

    await session.commit()

    # 推送到 Redis inbox
    await mq.ensure_inbox_group(to_id)
    await mq.publish_to_inbox(to_id, {
        "msg_id": msg.id,
        "task_id": task_id,
        "project_id": project_id,
        "from": from_id,
        "to": to_id,
        "type": msg_type,
        "payload": payload or {},
        "ref_msg_id": ref_msg_id,
    })

    logger.info(f"Message {msg.id}: {from_id} -> {to_id} [{msg_type}] task={task_id}")
    return msg


async def get_task_messages(
    session: AsyncSession,
    task_id: str,
    limit: int = 100,
) -> list[AgentMessage]:
    """获取某个任务的全部通信记录（按时间正序）"""
    q = await session.execute(
        select(AgentMessage)
        .where(AgentMessage.task_id == task_id)
        .order_by(AgentMessage.created_at)
        .limit(limit)
    )
    return list(q.scalars().all())


async def get_agent_pending(
    session: AsyncSession,
    agent_id: str,
    limit: int = 50,
) -> list[AgentMessage]:
    """获取某个 Agent 未回复的消息"""
    q = await session.execute(
        select(AgentMessage)
        .where(AgentMessage.to_id == agent_id, AgentMessage.status == "pending")
        .order_by(AgentMessage.created_at)
        .limit(limit)
    )
    return list(q.scalars().all())


async def get_thread(
    session: AsyncSession,
    msg_id: str,
) -> list[AgentMessage]:
    """获取一条消息的问答链（原始消息 + 所有回复）"""
    origin = await session.get(AgentMessage, msg_id)
    if not origin:
        return []

    root_id = origin.ref_msg_id or origin.id

    q = await session.execute(
        select(AgentMessage)
        .where(
            (AgentMessage.id == root_id) | (AgentMessage.ref_msg_id == root_id)
        )
        .order_by(AgentMessage.created_at)
    )
    return list(q.scalars().all())


async def get_project_messages(
    session: AsyncSession,
    project_id: str,
    agent_id: str | None = None,
    msg_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AgentMessage]:
    """获取项目级消息列表，可按 agent 或 type 过滤"""
    stmt = select(AgentMessage).where(AgentMessage.project_id == project_id)
    if agent_id:
        stmt = stmt.where(
            (AgentMessage.from_id == agent_id) | (AgentMessage.to_id == agent_id)
        )
    if msg_type:
        stmt = stmt.where(AgentMessage.msg_type == msg_type)
    stmt = stmt.order_by(AgentMessage.created_at.desc()).offset(offset).limit(limit)
    q = await session.execute(stmt)
    return list(q.scalars().all())


def msg_to_dict(m: AgentMessage) -> dict:
    return {
        "id": m.id,
        "task_id": m.task_id,
        "project_id": m.project_id,
        "from": m.from_id,
        "to": m.to_id,
        "type": m.msg_type,
        "payload": m.payload,
        "ref_msg_id": m.ref_msg_id,
        "status": m.status,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "replied_at": m.replied_at.isoformat() if m.replied_at else None,
    }
