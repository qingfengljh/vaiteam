"""
统一备份管理

调度器负责备份/恢复 OpenClaw 的完整 .openclaw 目录（含 workspace、sessions、memory、continuity）。
备份：通过 Redis 下发备份请求，Connector 打包后 HTTP 上传到 Dispatcher。
备份存储：BACKUP_DIR（需 volume 映射到宿主机），按 项目/agent/时间戳 组织。
"""

import asyncio
import os
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Agent, Backup
from app.services import mq, openclaw

logger = logging.getLogger(__name__)

# 等待 Connector 上传完成的 Event，key=request_id
_pending_backup_events: dict[str, asyncio.Event] = {}
_pending_backup_results: dict[str, Backup | None] = {}

BACKUP_MODE_METADATA_ONLY = "metadata_only"
BACKUP_MODE_FULL_SOURCE = "full_source"
VALID_BACKUP_MODES = {BACKUP_MODE_METADATA_ONLY, BACKUP_MODE_FULL_SOURCE}
READY_HEARTBEAT_STATUSES = {"online", "busy"}


def normalize_backup_mode(backup_mode: str | None) -> str:
    mode = (backup_mode or BACKUP_MODE_METADATA_ONLY).strip().lower()
    if mode not in VALID_BACKUP_MODES:
        return BACKUP_MODE_METADATA_ONLY
    return mode


def mode_includes_workspace(backup_mode: str | None) -> bool:
    return normalize_backup_mode(backup_mode) == BACKUP_MODE_FULL_SOURCE


def check_agent_backup_readiness(agent: Agent) -> tuple[bool, str]:
    """检查 Agent 是否满足备份前置条件"""
    if not agent.workspace_path:
        return False, "workspace_path 为空"
    status = (agent.last_heartbeat_status or "").strip().lower()
    if status not in READY_HEARTBEAT_STATUSES:
        return False, f"heartbeat_status={status or 'unknown'}"
    return True, ""


def _backup_path(project_id: str, agent_id: str, timestamp: str) -> str:
    return os.path.join(settings.BACKUP_DIR, project_id, agent_id, f"{timestamp}.tar.gz")


def backup_file_path(project_id: str, agent_id: str, timestamp: str) -> str:
    """与 webhook 上传一致的落盘路径（供导入/运维复用）。"""
    return _backup_path(project_id, agent_id, timestamp)


async def backup_agent(
    session: AsyncSession,
    agent: Agent,
    *,
    backup_mode: str = BACKUP_MODE_METADATA_ONLY,
) -> Backup | None:
    """备份 agent 数据：默认不含源码，显式 full_source 才包含 workspace"""
    ready, reason = check_agent_backup_readiness(agent)
    if not ready:
        logger.warning("Agent %s not ready for backup: %s", agent.id, reason)
        return None

    mode = normalize_backup_mode(backup_mode)
    include_workspace = mode_includes_workspace(mode)
    request_id = f"backup-{agent.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    event = asyncio.Event()
    _pending_backup_events[request_id] = event
    _pending_backup_results[request_id] = None

    try:
        await mq.publish_backup(
            agent.id,
            agent.project_id,
            request_id,
            include_workspace=include_workspace,
            backup_mode=mode,
        )
        logger.info(
            "Backup request published for %s (mode=%s include_workspace=%s), waiting for upload...",
            agent.id,
            mode,
            include_workspace,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=120)
        except asyncio.TimeoutError:
            logger.error(f"Backup upload timeout for {agent.id} (120s)")
            return None

        result = _pending_backup_results.get(request_id)
        if result:
            await session.refresh(result)
        return result
    finally:
        _pending_backup_events.pop(request_id, None)
        _pending_backup_results.pop(request_id, None)


def complete_backup_upload(request_id: str, backup: Backup | None):
    """备份上传完成时调用，唤醒等待的 backup_agent"""
    _pending_backup_results[request_id] = backup
    event = _pending_backup_events.get(request_id)
    if event:
        event.set()


async def restore_agent(session: AsyncSession, agent: Agent, backup: Backup) -> bool:
    """从备份恢复 agent 的 workspace（需 container_id，同机或 SSH 模式）"""
    if not os.path.exists(backup.file_path):
        logger.error(f"Backup file not found: {backup.file_path}")
        return False

    metadata = dict(backup.metadata_ or {})
    include_workspace = bool(metadata.get("include_workspace", True))
    if not include_workspace:
        logger.error("Backup %s is metadata_only and cannot restore source workspace", backup.id)
        return False

    if not agent.container_id:
        logger.error(f"Agent {agent.id} has no container_id, restore requires exec/copy")
        return False

    copy_result = await openclaw.copy_to_agent(agent.container_id, backup.file_path, "/tmp/restore.tar.gz")
    if copy_result.get("error"):
        logger.error(f"Copy restore failed: {copy_result}")
        return False

    openclaw_home = "/home/node/.openclaw"
    result = await openclaw.exec_in_agent(
        agent.container_id,
        f"mkdir -p {openclaw_home} && tar xzf /tmp/restore.tar.gz -C {openclaw_home}",
    )
    if result.get("error"):
        logger.error(f"Restore failed for {agent.id}: {result}")
        return False

    logger.info(f"Restored {agent.id} from {backup.file_path}")
    return True


async def backup_project_agents(
    session: AsyncSession,
    project_id: str,
    *,
    backup_mode: str = BACKUP_MODE_METADATA_ONLY,
) -> list[Backup]:
    """备份项目下所有 agent 数据；默认 metadata_only（不含源码）"""
    q = await session.execute(select(Agent).where(Agent.project_id == project_id))
    agents = q.scalars().all()

    backups = []
    for agent in agents:
        b = await backup_agent(session, agent, backup_mode=backup_mode)
        if b:
            backups.append(b)
    return backups


# ── 定时自动备份 ──

BACKUP_INTERVAL = 6 * 3600  # 每 6 小时
MAX_BACKUPS_PER_AGENT = 5   # 每个 Agent 保留最近 5 个备份

_running = False
_task: asyncio.Task | None = None


async def _auto_backup_loop():
    """定时备份所有在线 Agent"""
    from app.core.database import async_session as get_session

    while _running:
        try:
            await asyncio.sleep(BACKUP_INTERVAL)
            async with get_session() as session:
                q = await session.execute(
                    select(Agent).where(Agent.last_heartbeat_status.in_(["online", "busy"]))
                )
                agents = list(q.scalars())
                if not agents:
                    continue

                logger.info(f"Auto-backup: {len(agents)} online agents")
                for agent in agents:
                    try:
                        await backup_agent(session, agent, backup_mode=BACKUP_MODE_METADATA_ONLY)
                        await _cleanup_old_backups(session, agent.id)
                    except Exception as e:
                        logger.error(f"Auto-backup {agent.id} failed: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Auto-backup loop error: {e}")


async def _cleanup_old_backups(session: AsyncSession, agent_id: str):
    """保留最近 N 个备份，删除旧的"""
    q = await session.execute(
        select(Backup).where(Backup.agent_id == agent_id).order_by(Backup.created_at.desc())
    )
    backups = list(q.scalars())
    for old in backups[MAX_BACKUPS_PER_AGENT:]:
        try:
            if os.path.exists(old.file_path):
                os.remove(old.file_path)
        except Exception:
            pass
        await session.delete(old)
    if len(backups) > MAX_BACKUPS_PER_AGENT:
        await session.commit()


async def start():
    global _running, _task
    if _running:
        return
    _running = True
    _task = asyncio.create_task(_auto_backup_loop())
    logger.info(f"Auto-backup started (interval={BACKUP_INTERVAL}s, keep={MAX_BACKUPS_PER_AGENT})")


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
