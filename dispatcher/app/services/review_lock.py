"""
文档 AI 审核锁巡检

目标：
- 审核锁使用 Document.updated_at 作为持久化时间戳
- 超时自动解锁，避免 under_review 长期卡死
- 仅在存在 under_review 文档时高频巡检，否则低频空转
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.core.database import async_session
from app.models import Document

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_SECONDS = 10 * 60
CHECK_INTERVAL_ACTIVE = 10
CHECK_INTERVAL_IDLE = 120

_running = False
_task: asyncio.Task | None = None


def _is_review_finished(doc: Document) -> bool:
    rr = doc.review_result or {}
    return isinstance(rr, dict) and bool(rr)


def _is_lock_timeout(doc: Document, now: datetime) -> bool:
    ts = doc.updated_at or doc.created_at or now
    return (now - ts).total_seconds() >= LOCK_TIMEOUT_SECONDS


async def unlock_stale_review_locks() -> int:
    """解锁异常/超时的 under_review 文档，返回解锁数量。"""
    async with async_session() as session:
        q = await session.execute(
            select(Document).where(Document.status == "under_review")
        )
        docs = list(q.scalars())
        if not docs:
            return 0

        now = datetime.now(timezone.utc)
        unlocked = 0
        for doc in docs:
            if _is_review_finished(doc) or _is_lock_timeout(doc, now):
                doc.status = "draft"
                doc.review_result = {}
                doc.reviewed_by = ""
                doc.is_selected = False
                unlocked += 1

        if unlocked:
            await session.commit()
            logger.info(f"Unlocked {unlocked} stale review locks")
        return unlocked


async def _patrol_loop():
    while _running:
        try:
            unlocked = await unlock_stale_review_locks()
            interval = CHECK_INTERVAL_ACTIVE if unlocked > 0 else CHECK_INTERVAL_IDLE
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Review lock patrol error: {e}")
            interval = CHECK_INTERVAL_IDLE
        await asyncio.sleep(interval)


async def start():
    global _running, _task
    if _running:
        return
    _running = True
    _task = asyncio.create_task(_patrol_loop())
    logger.info("Review lock patrol started")


async def stop():
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("Review lock patrol stopped")
