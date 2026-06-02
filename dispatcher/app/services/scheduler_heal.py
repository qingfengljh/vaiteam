"""任务调度自愈：检测并自动修复常见的阻塞状态。

在 scheduler_loop 中每 60s 执行一轮，确保任务流不因状态漂移而停滞。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, Agent, Project

logger = logging.getLogger(__name__)

STUCK_TIMEOUT_MINUTES = 10
ESCALATION_COOLDOWN_MINUTES = 30


async def heal_stuck_agents(session: AsyncSession, project_id: str) -> int:
    """释放卡在 busy 状态但任务已完成的 Agent。

    场景：任务 done/failed 后 Agent 未释放（busy+abandoned）。
    """
    q = await session.execute(
        select(Agent).where(
            Agent.project_id == project_id,
            Agent.status.in_(["busy", "idle"]),
            Agent.last_heartbeat_status.in_(["abandoned", "dead", "start_failed"]),
        )
    )
    healed = 0
    for agent in q.scalars():
        # 确认其 current_task 已结束
        if agent.current_task_id:
            task = await session.get(Task, agent.current_task_id)
            if task and task.status in ("executing", "assigned"):
                continue  # 任务还在执行中，不释放
        agent.status = "idle"
        agent.current_task_id = None
        agent.last_heartbeat_status = "online"
        healed += 1
        logger.info(f"[heal] Released stuck agent {agent.id} ({agent.role})")

    if healed:
        await session.commit()
    return healed


async def heal_stuck_tasks(session: AsyncSession, project_id: str) -> int:
    """重置卡在 executing/assigned 状态超过阈值的任务。

    条件：Agent 失联（无心跳）或任务执行超时。
    返回修复数量。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_TIMEOUT_MINUTES)

    q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(["executing", "assigned"]),
            Task.updated_at < cutoff,
        )
    )
    stuck = list(q.scalars())
    healed = 0

    for task in stuck:
        agent = await session.get(Agent, task.assigned_agent) if task.assigned_agent else None
        if agent:
            last_hb = agent.last_heartbeat
            if last_hb and last_hb > cutoff:
                continue  # Agent 有心跳，可能在执行长任务
            # 释放 Agent
            agent.status = "idle"
            agent.current_task_id = None

        task.assigned_agent = None
        task.status = "pending"
        healed += 1
        logger.info(f"[heal] Reset stuck task {task.id[:8]} ({task.status}→pending)")

    if healed:
        await session.commit()
    return healed


async def heal_bootstrap_gate(session: AsyncSession, project_id: str) -> int:
    """检测并修复架构师 bootstrap 门禁。

    如果 bootstrap 任务已 done，但 architect agent 的 config 未标记，
    自动设置 architect_bootstrap_done = true。
    """
    # 检查 bootstrap 任务是否已完成
    bq = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["architect_bootstrap"].astext == "true",
            Task.status == "done",
        ).limit(1)
    )
    if not bq.scalar_one_or_none():
        return 0

    # 检查 architect agent 标记
    aq = await session.execute(
        select(Agent).where(
            Agent.project_id == project_id,
            Agent.role == "architect",
        )
    )
    healed = 0
    for agent in aq.scalars():
        cfg = dict(agent.config or {})
        if cfg.get("architect_bootstrap_done"):
            continue
        cfg["architect_bootstrap_done"] = True
        agent.config = cfg
        healed += 1
        logger.info(f"[heal] Fixed bootstrap_done flag for {agent.id}")

    if healed:
        await session.commit()
    return healed


async def heal_escalation_overflow(session: AsyncSession, project_id: str) -> int:
    """降级长期无人介入的 Level-3 任务。

    escalation_level=3（需人类介入）超过冷却时间无人处理时，
    重置为 level=0 放回 pending 队列。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ESCALATION_COOLDOWN_MINUTES)

    q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status == "pending",
            Task.escalation_level >= 3,
            Task.updated_at < cutoff,
        )
    )
    healed = 0
    for task in q.scalars():
        task.escalation_level = 0
        task.retry_count = 0
        healed += 1
        logger.info(f"[heal] Reset escalation for {task.id[:8]} (level=3→0)")

    if healed:
        await session.commit()
    return healed


async def heal_all(session: AsyncSession, project_id: str) -> dict:
    """执行全部自愈检查，返回修复统计。"""
    actions = {}

    for name, fn in [
        ("stuck_agents", heal_stuck_agents),
        ("stuck_tasks", heal_stuck_tasks),
        ("bootstrap_gate", heal_bootstrap_gate),
        ("escalation_overflow", heal_escalation_overflow),
    ]:
        try:
            count = await fn(session, project_id)
            if count:
                actions[name] = count
        except Exception as e:
            logger.warning(f"[heal] {name} failed: {e}")
            # 回滚失败的事务，避免影响同一 session 中的后续操作
            try:
                await session.rollback()
            except Exception:
                pass

    if actions:
        logger.info(f"[heal] Project {project_id}: {actions}")
    return actions
