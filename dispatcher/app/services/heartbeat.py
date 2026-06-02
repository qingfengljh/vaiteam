"""
心跳服务 — Agent 状态监控

职责：
  - 接收 Agent 心跳上报，更新 last_heartbeat 和 status
  - 定时巡检，标记 offline / dead
  - 触发故障恢复流程
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Task, TaskLog, AgentBootReport, AgentRehydrationJob, Project, AgentTeam
from app.core.database import async_session
from app.services import agent_lifecycle, task_lifecycle, rehydration, model_pool

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30       # Agent 上报间隔（秒）
OFFLINE_THRESHOLD = 90        # 超过此时间标记 offline
DEAD_THRESHOLD = 300          # 超过此时间标记 dead
AGENT_START_TIMEOUT = 60      # 启动后未报到超时（秒）
CHECK_INTERVAL = 60           # 巡检周期（秒）
MAX_AUTO_RESTART = 3          # 最大自动重启次数
AUTO_RESTART_COOLDOWN = 120   # 自动重启冷却时间（秒）

_running = False
_task: asyncio.Task | None = None
RECOVERY_HEARTBEAT_STATES = ("dead", "start_failed", "abandoned")
INHERITABLE_CONFIG_KEYS = (
    "context_versions",
    "retriever_ready",
    "local_checkpoint",
    "global_knowledge_version",
    "global_knowledge_revision",
)


def _normalize_current_task_id(value: str | None) -> str | None:
    task_id = (value or "").strip()
    if not task_id:
        return None
    if task_id.startswith(("human-", "review-", "help-", "backup-")):
        return None
    return task_id


async def receive_heartbeat(
    session: AsyncSession,
    agent_id: str,
    project_id: str | None = None,
    supervisor_id: str | None = None,
    role: str | None = None,
    status: str | None = None,
    current_task_id: str | None = None,
    system_info: dict | None = None,
    container_id: str | None = None,
    boot_id: str | None = None,
    session_fingerprint: str | None = None,
    context_versions: dict | None = None,
    retriever_ready: bool | None = None,
    local_checkpoint: dict | None = None,
    challenge_reply: str | None = None,
):
    """处理一条心跳上报，含自愈逻辑"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        agent = await _register_agent_from_heartbeat(
            session,
            agent_id=agent_id,
            project_id=project_id,
            role=role,
            supervisor_id=supervisor_id,
            status=status,
        )
        if not agent:
            logger.warning(
                "Heartbeat from unknown agent: %s (not in DB and lazy-register failed; see prior Lazy-register logs)",
                agent_id,
            )
            return False

    was_offline = agent.last_heartbeat_status in ("offline", "dead", "starting", "start_failed")
    now = datetime.now(timezone.utc)
    agent.last_heartbeat = now

    if supervisor_id is not None:
        agent.supervisor_id = supervisor_id
    if status and status in ("idle", "busy", "error"):
        # 若心跳上报 idle 但 Agent 仍有当前任务，不降级（任务在跑，只是心跳 payload 滞后）
        if status == "idle" and agent.current_task_id:
            pass  # keep current status, don't downgrade
        # 等待审核中的 Agent 不被心跳降级为 idle
        elif agent.status == "awaiting_review":
            pass
        else:
            agent.status = status
    if current_task_id is not None:
        agent.current_task_id = _normalize_current_task_id(current_task_id)
    if container_id is not None and container_id:
        agent.container_id = container_id

    agent.last_heartbeat_status = "busy" if agent.current_task_id else "online"
    agent.auto_restart_count = 0
    if system_info:
        cfg = dict(agent.config or {})
        cfg["system_info"] = system_info
        agent.config = cfg

    await _record_boot_report(
        session,
        agent,
        boot_id=boot_id,
        session_fingerprint=session_fingerprint,
        context_versions=context_versions,
        retriever_ready=retriever_ready,
        local_checkpoint=local_checkpoint,
    )
    agent_lifecycle.on_created(
        agent,
        boot_id=boot_id,
        recovery_mode=(agent.config or {}).get("recovery_mode", ""),
    )
    if was_offline:
        agent_lifecycle.on_mounted(agent)
    _update_challenge_status(agent, challenge_reply)

    # ── 自愈：Agent 从离线恢复上线 ──
    if was_offline and not agent.current_task_id:
        await _self_heal(session, agent)
    if agent_lifecycle.should_try_bootstrap_on_mounted(agent):
        await _try_assign_architect_bootstrap(session, agent)

    await session.commit()
    return True


async def _register_agent_from_heartbeat(
    session: AsyncSession,
    *,
    agent_id: str,
    project_id: str | None,
    role: str | None,
    supervisor_id: str | None,
    status: str | None,
) -> Agent | None:
    """
    兜底延迟注册：
    - 优先使用心跳上报 role
    - project_id 优先用心跳上报，缺失时再从 agent_id 解析（role-project 或 role-project-suffix）
    """
    heartbeat_project_id = project_id
    parts = [p for p in (agent_id or "").split("-") if p]
    if len(parts) < 2 and not heartbeat_project_id:
        logger.warning(
            "Lazy-register skipped: need project_id in body or agent_id like role-<project>[-suffix], got agent_id=%r",
            agent_id,
        )
        return None
    resolved_role = (role or (parts[0] if parts else "") or "").strip()
    merged_project_id = (heartbeat_project_id or (parts[1] if len(parts) >= 2 else "") or "").strip()
    if not resolved_role or not merged_project_id:
        logger.warning(
            "Lazy-register skipped: empty role or project_id (agent_id=%r merged_project_id=%r heartbeat.project_id=%r role=%r)",
            agent_id,
            merged_project_id,
            heartbeat_project_id,
            role,
        )
        return None

    project = await session.get(Project, merged_project_id)
    if not project:
        logger.warning(
            "Lazy-register skipped: no project %r in dispatcher DB (agent_id=%r). "
            "若期望的 project 在库中但 id 不同：请核对 compose/启动参数中的 PROJECT_ID 与 projects.id；"
            "同一套 Agent 进程要服务多个项目时，不必写死 env 文件，但须在每次心跳 JSON 里传 project_id（优先于从 agent_id 第二段解析）。",
            merged_project_id,
            agent_id,
        )
        return None
    project_cfg = dict(project.config or {})
    relation_mode = str(project_cfg.get("same_role_relation_mode") or "auto").strip().lower()
    if relation_mode not in ("auto", "peer", "successor"):
        relation_mode = "auto"
    enable_successor_inherit = bool(project_cfg.get("enable_successor_inherit", True))

    project_map = project.role_model_map or {}
    model = project_map.get(resolved_role, model_pool.ROLE_MODEL_MAP.get(resolved_role, ""))
    resolved_status = status if status in ("idle", "busy", "error") else "idle"

    predecessor_id = ""
    predecessor_cfg: dict = {}
    if relation_mode in ("auto", "successor"):
        same_role_q = await session.execute(
            select(Agent).where(
                Agent.project_id == merged_project_id,
                Agent.role == resolved_role,
                Agent.id != agent_id,
            )
        )
        same_role_agents = list(same_role_q.scalars())
        if same_role_agents:
            terminal = {"offline", "dead", "start_failed", "abandoned"}
            candidates = [
                a for a in same_role_agents
                if (a.last_heartbeat_status or "offline") in terminal and not a.current_task_id
            ]
            if candidates:
                candidates.sort(key=lambda a: (a.created_at.timestamp() if a.created_at else 0), reverse=True)
                predecessor_id = candidates[0].id
                predecessor_cfg = dict(candidates[0].config or {})

    default_team = (
        await session.execute(
            select(AgentTeam).where(
                AgentTeam.project_id == merged_project_id,
                AgentTeam.is_default == True,  # noqa: E712
            ).limit(1)
        )
    ).scalar_one_or_none()
    default_team_id = default_team.id if default_team else None

    successor_snapshot: dict = {}
    if enable_successor_inherit and predecessor_id:
        for key in INHERITABLE_CONFIG_KEYS:
            val = predecessor_cfg.get(key)
            if val is None:
                continue
            successor_snapshot[key] = val

    agent = Agent(
        id=agent_id,
        project_id=merged_project_id,
        role=resolved_role,
        model=model,
        status=resolved_status,
        supervisor_id=supervisor_id,
        team_id=default_team_id,
        config={
            "registration_mode": "lazy_heartbeat",
            "allow_delayed_injection": True,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "role_relationship": {
                "mode": relation_mode,
                "is_successor": bool(predecessor_id and relation_mode in ("auto", "successor")),
                "predecessor_agent_id": predecessor_id,
            },
            "successor_inherit": (
                {"from_agent_id": predecessor_id, "snapshot": successor_snapshot}
                if successor_snapshot
                else {}
            ),
        },
    )
    session.add(agent)
    await session.flush()
    if successor_snapshot and relation_mode in ("auto", "successor"):
        session.add(AgentRehydrationJob(
            agent_id=agent.id,
            project_id=merged_project_id,
            mode="partial_rehydrate",
            reason=f"successor inherit from {predecessor_id}",
            snapshot=successor_snapshot,
            status="pending",
        ))
    logger.info("Lazy-registered agent from heartbeat: %s role=%s project=%s", agent_id, resolved_role, merged_project_id)
    return agent


async def _try_assign_architect_bootstrap(session: AsyncSession, agent: Agent):
    if agent.status != "idle" or agent.current_task_id:
        return
    if (agent.config or {}).get("retriever_ready", True) is False:
        return

    q = await session.execute(
        select(Task).where(
            Task.project_id == agent.project_id,
            Task.context["architect_bootstrap"].astext == "true",
            Task.status == "pending",
            Task.assigned_agent == None,  # noqa: E711
        ).order_by(Task.created_at.asc()).limit(1)
    )
    bootstrap = q.scalar_one_or_none()
    if not bootstrap:
        return
    bootstrap_team_id = (bootstrap.context or {}).get("bootstrap_team_id")
    if bootstrap_team_id and agent.team_id != bootstrap_team_id:
        return

    from app.services import scheduler
    ok = await scheduler.assign_task(session, bootstrap, agent)
    if ok:
        logger.info("Assigned architect bootstrap task %s to %s", bootstrap.id, agent.id)


def _update_challenge_status(agent: Agent, challenge_reply: str | None):
    cfg = dict(agent.config or {})
    expected = (cfg.get("health_challenge") or "").strip()
    expire_at = int(cfg.get("health_challenge_expire_at") or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if not expected:
        cfg["health_challenge_status"] = "not_issued"
        agent.config = cfg
        return

    if now_ts > expire_at > 0:
        cfg["health_challenge_status"] = "expired"
        cfg["health_challenge_last_at"] = datetime.now(timezone.utc).isoformat()
        agent.config = cfg
        return

    if (challenge_reply or "").strip() == expected:
        cfg["health_challenge_status"] = "ok"
        cfg["health_challenge_last_at"] = datetime.now(timezone.utc).isoformat()
        cfg["health_challenge"] = ""
        cfg["health_challenge_expire_at"] = 0
    else:
        cfg["health_challenge_status"] = "pending" if not challenge_reply else "mismatch"
        cfg["health_challenge_last_at"] = datetime.now(timezone.utc).isoformat()
    agent.config = cfg


def _decide_recovery_mode(prev_cfg: dict, boot_id: str, session_fingerprint: str, context_versions: dict) -> tuple[str, str]:
    prev_boot = (prev_cfg.get("boot_id") or "").strip()
    prev_fp = (prev_cfg.get("session_fingerprint") or "").strip()
    prev_versions = prev_cfg.get("context_versions") if isinstance(prev_cfg.get("context_versions"), dict) else {}

    if not prev_boot:
        return "cold_start", "first_boot_or_missing_history"
    if boot_id and prev_boot == boot_id:
        return "fast_resume", "same_boot_id"
    if session_fingerprint and prev_fp and session_fingerprint == prev_fp:
        return "fast_resume", "same_session_fingerprint"
    if context_versions and prev_versions and any(k in prev_versions for k in context_versions):
        return "partial_rehydrate", "context_version_overlap"
    return "cold_start", "fingerprint_or_versions_mismatch"


async def _record_boot_report(
    session: AsyncSession,
    agent: Agent,
    *,
    boot_id: str | None,
    session_fingerprint: str | None,
    context_versions: dict | None,
    retriever_ready: bool | None,
    local_checkpoint: dict | None,
):
    cfg = dict(agent.config or {})
    clean_boot_id = (boot_id or "").strip()
    clean_fp = (session_fingerprint or "").strip()
    versions = context_versions if isinstance(context_versions, dict) else {}
    checkpoint = local_checkpoint if isinstance(local_checkpoint, dict) else {}
    rr = True if retriever_ready is None else bool(retriever_ready)

    recovery_mode, reason = _decide_recovery_mode(cfg, clean_boot_id, clean_fp, versions)
    if not rr and recovery_mode == "fast_resume":
        recovery_mode, reason = "partial_rehydrate", "retriever_not_ready"

    report = AgentBootReport(
        agent_id=agent.id,
        project_id=agent.project_id,
        boot_id=clean_boot_id,
        session_fingerprint=clean_fp,
        recovery_mode=recovery_mode,
        retriever_ready=rr,
        context_versions=versions,
        metadata_={"reason": reason, "local_checkpoint": checkpoint},
    )
    session.add(report)

    cfg["boot_id"] = clean_boot_id
    cfg["session_fingerprint"] = clean_fp
    cfg["context_versions"] = versions
    cfg["retriever_ready"] = rr
    cfg["recovery_mode"] = recovery_mode
    cfg["recovery_reason"] = reason
    cfg["local_checkpoint"] = checkpoint
    agent.config = cfg

    if recovery_mode != "fast_resume":
        session.add(AgentRehydrationJob(
            agent_id=agent.id,
            project_id=agent.project_id,
            mode=recovery_mode,
            reason=reason,
            status="pending",
            snapshot={
                "boot_id": clean_boot_id,
                "session_fingerprint": clean_fp,
                "context_versions": versions,
                "retriever_ready": rr,
            },
        ))


async def _self_heal(session: AsyncSession, agent: Agent):
    """Agent 恢复上线后自愈：释放孤立任务 → 触发重新分配"""
    # 1. 仍挂在此 Agent 名下的 assigned 任务 → 释放回 pending（容器重建丢失上下文）
    stale_q = await session.execute(
        select(Task).where(Task.assigned_agent == agent.id, Task.status == "assigned")
    )
    released = []
    for task in stale_q.scalars():
        task.assigned_agent = None
        session.add(task_lifecycle.transition(
            task,
            event="recover",
            actor="dispatcher",
            reason=f"self heal release from {agent.id}",
        ))
        released.append(task.id)

    if released:
        agent.status = "idle"
        agent.current_task_id = None
        logger.info(f"Self-heal {agent.id}: released {len(released)} stale tasks → pending: {released}")

    # Agent 恢复上线且空闲 → 尝试分配待执行任务
    if agent.status == "idle" and agent.project_id:
        from app.services import scheduler
        assigned = await scheduler.auto_assign(
            session,
            agent.project_id,
            actor_role="dispatcher",
        )
        if assigned:
            logger.info(f"Self-heal {agent.id}: auto-assigned {assigned}")


async def _recover_unhealthy_agent_tasks(session: AsyncSession):
    """
    Dispatcher 兜底恢复：当 Agent 已 dead/start_failed/abandoned，
    回收其 assigned/executing 任务，避免任务卡死在失联 Agent 上。
    """
    unhealthy_q = await session.execute(
        select(Agent).where(Agent.last_heartbeat_status.in_(RECOVERY_HEARTBEAT_STATES))
    )
    unhealthy_agents = list(unhealthy_q.scalars())
    if not unhealthy_agents:
        return

    recovered_projects: set[str] = set()
    for agent in unhealthy_agents:
        task_q = await session.execute(
            select(Task).where(
                Task.assigned_agent == agent.id,
                Task.status.in_(["assigned", "executing"]),
            )
        )
        stuck_tasks = list(task_q.scalars())
        if not stuck_tasks:
            continue

        for task in stuck_tasks:
            task.assigned_agent = None
            session.add(task_lifecycle.transition(
                task,
                event="recover",
                actor="dispatcher",
                reason=f"recover unhealthy agent {agent.id}",
            ))
            recovered_projects.add(task.project_id)
            session.add(TaskLog(
                task_id=task.id,
                agent_id="dispatcher",
                action="dispatcher_recover",
                message=(
                    f"Dispatcher 检测到 Agent({agent.id}) 心跳异常({agent.last_heartbeat_status})，"
                    "自动回收任务并重新排队"
                ),
                metadata_={
                    "from_agent_id": agent.id,
                    "from_agent_role": agent.role,
                    "from_heartbeat_status": agent.last_heartbeat_status,
                    "recovery_mode": "auto-requeue",
                },
            ))

        if agent.current_task_id:
            agent.current_task_id = None

        logger.warning(
            f"Dispatcher recovered {len(stuck_tasks)} stuck tasks from unhealthy agent "
            f"{agent.id} ({agent.role}, {agent.last_heartbeat_status})"
        )

    if not recovered_projects:
        return

    await session.commit()

    from app.services import scheduler
    for project_id in recovered_projects:
        await scheduler.auto_assign(session, project_id, actor_role="dispatcher")


async def check_agents():
    """巡检所有 Agent 的心跳状态"""
    async with async_session() as session:
        now = datetime.now(timezone.utc)
        offline_cutoff = now - timedelta(seconds=OFFLINE_THRESHOLD)
        dead_cutoff = now - timedelta(seconds=DEAD_THRESHOLD)
        start_timeout_cutoff = now - timedelta(seconds=AGENT_START_TIMEOUT)

        # starting 超时 → start_failed（容器启动了但 Agent 没报到）
        await session.execute(
            update(Agent)
            .where(
                Agent.last_heartbeat_status == "starting",
                Agent.last_heartbeat.is_(None),
                Agent.last_started_at.isnot(None),
                Agent.last_started_at < start_timeout_cutoff,
            )
            .values(last_heartbeat_status="start_failed")
        )

        # 有过心跳但超过 dead 阈值 → dead
        await session.execute(
            update(Agent)
            .where(
                Agent.last_heartbeat.isnot(None),
                Agent.last_heartbeat < dead_cutoff,
                Agent.last_heartbeat_status.in_(["online", "busy", "offline"]),
            )
            .values(last_heartbeat_status="dead")
        )

        # 有过心跳但超过 offline 阈值 → offline
        await session.execute(
            update(Agent)
            .where(
                Agent.last_heartbeat.isnot(None),
                Agent.last_heartbeat < offline_cutoff,
                Agent.last_heartbeat >= dead_cutoff,
                Agent.last_heartbeat_status.in_(["online", "busy"]),
            )
            .values(last_heartbeat_status="offline")
        )

        await session.commit()

        dead_q = await session.execute(
            select(Agent).where(Agent.last_heartbeat_status.in_(["dead", "start_failed"]))
        )
        for agent in dead_q.scalars():
            logger.warning(
                f"Agent {agent.id} ({agent.role}) status={agent.last_heartbeat_status} "
                f"(last heartbeat: {agent.last_heartbeat})"
            )

        await _recover_unhealthy_agent_tasks(session)
        await _auto_restart_agents(session, now)
        await rehydration.run_pending_jobs(session)


async def _patrol_loop():
    """后台巡检循环"""
    while _running:
        try:
            await check_agents()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat patrol error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


async def start():
    """启动心跳巡检"""
    global _running, _task
    if _running:
        return
    _running = True
    _task = asyncio.create_task(_patrol_loop())
    logger.info(f"Heartbeat patrol started (check every {CHECK_INTERVAL}s)")


async def stop():
    """停止心跳巡检"""
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("Heartbeat patrol stopped")


async def get_status_summary(session: AsyncSession, project_id: str | None = None) -> dict:
    """获取 Agent 状态汇总"""
    q = select(Agent)
    if project_id:
        q = q.where(Agent.project_id == project_id)
    result = await session.execute(q)
    agents = list(result.scalars())

    summary = {"total": len(agents), "online": 0, "busy": 0, "offline": 0, "dead": 0}
    for a in agents:
        s = a.last_heartbeat_status or "offline"
        if s in summary:
            summary[s] += 1
        elif s in ("starting", "start_failed"):
            summary["offline"] += 1

    return summary


async def get_subordinates(session: AsyncSession, agent_id: str) -> list[dict]:
    """查询某个 Agent 的所有下属状态"""
    result = await session.execute(
        select(Agent).where(Agent.supervisor_id == agent_id)
    )
    return [
        {
            "id": a.id,
            "role": a.role,
            "status": a.status,
            "heartbeat_status": a.last_heartbeat_status,
            "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            "current_task_id": a.current_task_id,
            "model": a.model,
        }
        for a in result.scalars()
    ]


async def _auto_restart_agents(session: AsyncSession, now: datetime):
    """自动重启离线/失联/启动失败的 Agent，超过最大次数标记 abandoned"""
    restart_q = await session.execute(
        select(Agent).where(
            Agent.last_heartbeat_status.in_(["dead", "start_failed", "offline"]),
            Agent.auto_restart_count < MAX_AUTO_RESTART,
        )
    )
    cooldown_cutoff = now - timedelta(seconds=AUTO_RESTART_COOLDOWN)

    for agent in restart_q.scalars():
        if agent.last_started_at and agent.last_started_at > cooldown_cutoff:
            continue
        try:
            from app.routers.agents import _get_agent_backend, _auto_deploy_if_needed, _should_try_auto_deploy
            backend, agent_config = await _get_agent_backend(session, agent)

            # 先 destroy 旧容器（释放端口等资源），失败也继续
            try:
                await backend.destroy_agent(agent.id, agent.container_id or "", agent_config)
            except Exception:
                pass

            result = await backend.start_agent(agent.id, agent.role, agent_config)

            if result.get("status") == "start_failed":
                err = result.get("error", "")
                if _should_try_auto_deploy(err):
                    deployed = await _auto_deploy_if_needed(session, agent)
                    if deployed:
                        backend, agent_config = await _get_agent_backend(session, agent)
                        result = await backend.start_agent(agent.id, agent.role, agent_config)

            agent.auto_restart_count = (agent.auto_restart_count or 0) + 1
            if result.get("status") == "start_failed":
                agent.last_heartbeat_status = "start_failed"
                logger.warning(f"Auto-restart {agent.id} failed (attempt {agent.auto_restart_count}/{MAX_AUTO_RESTART})")
            else:
                agent.last_heartbeat_status = "starting"
                agent.last_started_at = now
                agent.last_heartbeat = None
                logger.info(f"Auto-restart {agent.id} sent (attempt {agent.auto_restart_count}/{MAX_AUTO_RESTART})")
        except Exception as e:
            agent.auto_restart_count = (agent.auto_restart_count or 0) + 1
            logger.error(f"Auto-restart {agent.id} exception: {e}")

    # 超过最大次数 → abandoned
    await session.execute(
        update(Agent)
        .where(
            Agent.last_heartbeat_status.in_(["dead", "start_failed", "offline"]),
            Agent.auto_restart_count >= MAX_AUTO_RESTART,
        )
        .values(last_heartbeat_status="abandoned")
    )

    await session.commit()
