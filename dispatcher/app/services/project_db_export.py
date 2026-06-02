"""
将 Dispatcher 库内与项目相关的行导出为 JSON（gzip），登记为 backups 表一条记录，
与 Connector 上传的 tar 并列，供 GET .../backups 列表与下载。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    Agent,
    AgentBootReport,
    AgentMessage,
    AgentRehydrationJob,
    AgentTeam,
    Backup,
    ChangeRequest,
    Conversation,
    ConversationMessage,
    Document,
    GenerationTask,
    Iteration,
    Message,
    Project,
    ProjectAsset,
    PrototypeRun,
    StageProgress,
    Task,
    TaskComment,
    TaskDocument,
    TaskLog,
    TeamChat,
    TokenUsageLog,
    UploadedFile,
)

logger = logging.getLogger(__name__)

EXPORT_VERSION = 1
# backups.agent_id 非空；占位，非 agents 表主键
PROJECT_DB_EXPORT_AGENT_ID = "__dispatcher_db__"


def _jsonable(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (dict, list, str, int, float, bool)):
        return v
    if isinstance(v, bytes):
        return v.hex()
    if hasattr(v, "tolist"):
        return v.tolist()
    return str(v)


def _row_dict(obj) -> dict:
    return {c.key: _jsonable(getattr(obj, c.key)) for c in obj.__table__.columns}


def _dump_path(project_id: str, ts: str) -> str:
    return os.path.join(
        settings.BACKUP_DIR,
        project_id,
        PROJECT_DB_EXPORT_AGENT_ID,
        f"{ts}_project_db.json.gz",
    )


async def _rows(session: AsyncSession, stmt):
    r = await session.execute(stmt)
    return list(r.scalars())


async def build_project_export_payload(session: AsyncSession, project_id: str) -> dict:
    proj = await session.get(Project, project_id)
    if not proj:
        return {}

    task_id_sq = select(Task.id).where(Task.project_id == project_id)

    tables: dict[str, list] = {
        "projects": [_row_dict(proj)],
        "iterations": [_row_dict(x) for x in await _rows(session, select(Iteration).where(Iteration.project_id == project_id))],
        "change_requests": [_row_dict(x) for x in await _rows(session, select(ChangeRequest).where(ChangeRequest.project_id == project_id))],
        "stage_progress": [_row_dict(x) for x in await _rows(session, select(StageProgress).where(StageProgress.project_id == project_id))],
        "tasks": [_row_dict(x) for x in await _rows(session, select(Task).where(Task.project_id == project_id))],
        "agent_teams": [_row_dict(x) for x in await _rows(session, select(AgentTeam).where(AgentTeam.project_id == project_id))],
        "agents": [_row_dict(x) for x in await _rows(session, select(Agent).where(Agent.project_id == project_id))],
        "agent_boot_reports": [_row_dict(x) for x in await _rows(session, select(AgentBootReport).where(AgentBootReport.project_id == project_id))],
        "agent_rehydration_jobs": [_row_dict(x) for x in await _rows(session, select(AgentRehydrationJob).where(AgentRehydrationJob.project_id == project_id))],
        "token_usage_logs": [_row_dict(x) for x in await _rows(session, select(TokenUsageLog).where(TokenUsageLog.project_id == project_id))],
        "prototype_runs": [_row_dict(x) for x in await _rows(session, select(PrototypeRun).where(PrototypeRun.project_id == project_id))],
        "project_assets": [_row_dict(x) for x in await _rows(session, select(ProjectAsset).where(ProjectAsset.project_id == project_id))],
        "documents": [_row_dict(x) for x in await _rows(session, select(Document).where(Document.project_id == project_id))],
        "generation_tasks": [_row_dict(x) for x in await _rows(session, select(GenerationTask).where(GenerationTask.project_id == project_id))],
        "messages": [_row_dict(x) for x in await _rows(session, select(Message).where(Message.project_id == project_id))],
        "task_documents": [_row_dict(x) for x in await _rows(session, select(TaskDocument).where(TaskDocument.project_id == project_id))],
        "agent_messages": [_row_dict(x) for x in await _rows(session, select(AgentMessage).where(AgentMessage.project_id == project_id))],
        "team_chats": [_row_dict(x) for x in await _rows(session, select(TeamChat).where(TeamChat.project_id == project_id))],
        "uploaded_files": [_row_dict(x) for x in await _rows(session, select(UploadedFile).where(UploadedFile.project_id == project_id))],
        "conversations": [_row_dict(x) for x in await _rows(session, select(Conversation).where(Conversation.project_id == project_id))],
        "task_comments": [_row_dict(x) for x in await _rows(session, select(TaskComment).where(TaskComment.task_id.in_(task_id_sq)))],
        "task_logs": [_row_dict(x) for x in await _rows(session, select(TaskLog).where(TaskLog.task_id.in_(task_id_sq)))],
    }

    conv_ids = [
        r[0]
        for r in (
            await session.execute(select(Conversation.id).where(Conversation.project_id == project_id))
        ).all()
    ]
    if conv_ids:
        cm = await _rows(
            session,
            select(ConversationMessage).where(ConversationMessage.conversation_id.in_(conv_ids)),
        )
        tables["conversation_messages"] = [_row_dict(x) for x in cm]
    else:
        tables["conversation_messages"] = []

    return {
        "export_version": EXPORT_VERSION,
        "project_id": project_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


async def create_project_db_backup(
    session: AsyncSession,
    project_id: str,
    *,
    backup_mode: str,
) -> Backup | None:
    """导出项目相关库表为 gzip JSON，写入 BACKUP_DIR 并插入 backups。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = _dump_path(project_id, ts)
    try:
        payload = await build_project_export_payload(session, project_id)
        if not payload:
            logger.warning("project_db_export: project %s not found", project_id)
            return None

        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with gzip.open(path, "wb", mtime=0) as gz:
            gz.write(raw)

        file_size = os.path.getsize(path)
        if file_size > settings.BACKUP_MAX_SIZE:
            os.remove(path)
            logger.error(
                "project_db_export: size %s exceeds BACKUP_MAX_SIZE for project %s",
                file_size,
                project_id,
            )
            return None

        backup = Backup(
            project_id=project_id,
            agent_id=PROJECT_DB_EXPORT_AGENT_ID,
            backup_type="project_database",
            file_path=path,
            file_size=file_size,
            metadata_={
                "kind": "project_db_export",
                "format": "json.gz",
                "export_version": EXPORT_VERSION,
                "backup_mode": backup_mode,
                "include_workspace": False,
                "contains_source_code": False,
            },
        )
        session.add(backup)
        await session.flush()
        logger.info(
            "project_db_export: wrote %s bytes for project %s backup_id=%s",
            file_size,
            project_id,
            backup.id,
        )
        return backup
    except Exception as e:
        logger.exception("project_db_export failed for %s: %s", project_id, e)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return None


def backup_row_summary(b: Backup) -> dict:
    meta = dict(b.metadata_ or {})
    kind = meta.get("kind")
    export_kind = (
        "project_database"
        if kind in ("project_db_export", "project_db_import")
        else "connector_workspace"
    )
    return {
        "backup_id": b.id,
        "agent_id": b.agent_id,
        "file_path": b.file_path,
        "file_size": b.file_size,
        "contains_source_code": bool(meta.get("include_workspace", True)),
        "backup_mode": meta.get("backup_mode", "unknown"),
        "export_kind": export_kind,
    }
