"""
调度循环（独立组件）

- Dispatcher 启动后常驻运行
- 周期性触发各项目 auto_assign（由 scheduler 内部做条件门控）
- 无可分配任务时空转
"""

import asyncio
import logging
from sqlalchemy import update, select

from app.models import Agent, Task, Project
from app.core.database import async_session
from app.services import scheduler, task_lifecycle, scheduler_heal

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_running = False
CHECK_INTERVAL_ACTIVE = 30
CHECK_INTERVAL_IDLE = 60
IDLE_BACKOFF_THRESHOLD = 5


async def startup_recovery():
    """Dispatcher 重启后一次性恢复：修正状态"""
    async with async_session() as session:
        result = await session.execute(
            update(Agent)
            .where(Agent.last_heartbeat_status.in_(["online", "busy", "starting", "start_failed", "dead"]))
            .values(last_heartbeat_status="offline", last_heartbeat=None, status="idle", current_task_id=None)
        )
        if result.rowcount:
            logger.info(f"Startup recovery: reset {result.rowcount} agents to offline/idle")

        task_q = await session.execute(
            select(Task).where(Task.status == "assigned")
        )
        released_count = 0
        for task in task_q.scalars():
            task.assigned_agent = None
            session.add(task_lifecycle.transition(
                task,
                event="startup_recover",
                actor="dispatcher",
                reason="dispatcher startup recovery",
            ))
            released_count += 1
        if released_count:
            logger.info(f"Startup recovery: released {released_count} assigned tasks to pending")

        await session.commit()

    logger.info("Startup recovery completed")


async def _boot():
    await asyncio.sleep(5)
    try:
        await startup_recovery()
    except Exception as e:
        logger.error(f"Startup recovery failed: {e}")
    await _loop()


async def _loop():
    idle_cycles = 0
    while _running:
        try:
            total_assigned = 0
            async with async_session() as session:
                q = await session.execute(
                    select(Project.id).where(Project.status.in_(["planning", "active"]))
                )
                project_ids = [row[0] for row in q.all()]

            for project_id in project_ids:
                try:
                    async with async_session() as s:
                        await scheduler_heal.heal_all(s, project_id)
                        assigned = await scheduler.auto_assign(s, project_id, actor_role="dispatcher")
                        total_assigned += len(assigned or [])
                except Exception as assign_err:
                    logger.warning(f"Scheduler loop assign failed: project={project_id}, error={assign_err}")
            if total_assigned > 0:
                idle_cycles = 0
            else:
                idle_cycles += 1
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        interval = CHECK_INTERVAL_IDLE if idle_cycles >= IDLE_BACKOFF_THRESHOLD else CHECK_INTERVAL_ACTIVE
        await asyncio.sleep(interval)


async def start():
    global _task, _running
    if _task or _running:
        return
    _running = True
    _task = asyncio.create_task(_boot())
    logger.info(
        "Scheduler loop started (active=%ss, idle=%ss, backoff_after=%s cycles)",
        CHECK_INTERVAL_ACTIVE,
        CHECK_INTERVAL_IDLE,
        IDLE_BACKOFF_THRESHOLD,
    )


async def stop():
    global _task, _running
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("Scheduler loop stopped")
