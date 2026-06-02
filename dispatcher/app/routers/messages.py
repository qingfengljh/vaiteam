"""Agent 间通信消息 API"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services import messaging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/messages", tags=["messages"])


class SendMessageBody(BaseModel):
    task_id: str
    project_id: str
    from_id: str
    to_id: str
    msg_type: str
    payload: dict = {}
    ref_msg_id: str | None = None


@router.post("")
async def send_message(body: SendMessageBody, session: AsyncSession = Depends(get_session)):
    if body.msg_type not in messaging.MSG_TYPES:
        raise HTTPException(400, f"未知消息类型: {body.msg_type}")

    msg = await messaging.send(
        session,
        task_id=body.task_id,
        project_id=body.project_id,
        from_id=body.from_id,
        to_id=body.to_id,
        msg_type=body.msg_type,
        payload=body.payload,
        ref_msg_id=body.ref_msg_id,
    )
    return messaging.msg_to_dict(msg)


@router.get("/task/{task_id}")
async def get_task_messages(
    task_id: str,
    limit: int = Query(100, le=500),
    session: AsyncSession = Depends(get_session),
):
    """获取某任务的全部通信记录"""
    msgs = await messaging.get_task_messages(session, task_id, limit)
    return [messaging.msg_to_dict(m) for m in msgs]


@router.get("/agent/{agent_id}/pending")
async def get_agent_pending(
    agent_id: str,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    """获取某 Agent 待回复的消息"""
    msgs = await messaging.get_agent_pending(session, agent_id, limit)
    return [messaging.msg_to_dict(m) for m in msgs]


@router.get("/{msg_id}/thread")
async def get_message_thread(
    msg_id: str,
    session: AsyncSession = Depends(get_session),
):
    """获取消息问答链"""
    msgs = await messaging.get_thread(session, msg_id)
    if not msgs:
        raise HTTPException(404, "消息不存在")
    return [messaging.msg_to_dict(m) for m in msgs]


@router.get("/project/{project_id}")
async def get_project_messages(
    project_id: str,
    agent_id: str | None = None,
    msg_type: str | None = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """获取项目级消息列表"""
    msgs = await messaging.get_project_messages(
        session, project_id, agent_id, msg_type, limit, offset
    )
    return [messaging.msg_to_dict(m) for m in msgs]
