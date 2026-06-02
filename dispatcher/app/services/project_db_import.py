"""
将 project_db_export 产出的 gzip JSON 导入到「已有」项目：清空本项目导出范围内的表后按序重建行。
与 POST .../backups/import（单 Agent tar）互补，实现按项目库表维度的导入。
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

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

MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024


def _parse_dt(val: Any):
    if val is None or isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    return val


def _coerce_column(model_cls: type, key: str, val: Any) -> Any:
    if val is None:
        return None
    col = model_cls.__table__.columns.get(key)
    if col is None:
        return val
    if isinstance(col.type, DateTime):
        return _parse_dt(val)
    return val


def _row_to_model(model_cls: type, row: dict) -> Any:
    row = dict(row)
    if model_cls is TaskDocument:
        row.pop("tsv", None)
    kwargs = {}
    for k, v in row.items():
        if k not in model_cls.__table__.columns:
            continue
        kwargs[k] = _coerce_column(model_cls, k, v)
    return model_cls(**kwargs)


def decode_project_db_upload(compressed: bytes, expected_project_id: str) -> dict:
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ValueError(f"压缩包超过 {MAX_COMPRESSED_BYTES} 字节")
    raw = gzip.decompress(compressed)
    if len(raw) > MAX_DECOMPRESSED_BYTES:
        raise ValueError(f"解压后超过 {MAX_DECOMPRESSED_BYTES} 字节")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("根节点须为 JSON 对象")
    if int(payload.get("export_version") or 0) < 1:
        raise ValueError("不支持的 export_version")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("缺少 tables")
    prows = tables.get("projects")
    if not isinstance(prows, list) or len(prows) != 1:
        raise ValueError("tables.projects 须为且仅含一行")
    pid = str((prows[0] or {}).get("id") or "").strip()
    if pid != expected_project_id:
        raise ValueError(f"包内 project_id={pid!r} 与路径项目 {expected_project_id!r} 不一致")
    return payload


async def purge_project_export_tables(session: AsyncSession, project_id: str) -> None:
    task_ids = select(Task.id).where(Task.project_id == project_id)
    conv_ids = select(Conversation.id).where(Conversation.project_id == project_id)

    await session.execute(
        delete(ConversationMessage).where(ConversationMessage.conversation_id.in_(conv_ids))
    )
    await session.execute(delete(Conversation).where(Conversation.project_id == project_id))

    await session.execute(delete(TaskLog).where(TaskLog.task_id.in_(task_ids)))
    await session.execute(delete(TaskComment).where(TaskComment.task_id.in_(task_ids)))
    await session.execute(delete(AgentMessage).where(AgentMessage.project_id == project_id))

    await session.execute(delete(TaskDocument).where(TaskDocument.project_id == project_id))
    await session.execute(delete(Message).where(Message.project_id == project_id))
    await session.execute(delete(GenerationTask).where(GenerationTask.project_id == project_id))
    await session.execute(delete(Document).where(Document.project_id == project_id))
    await session.execute(delete(ProjectAsset).where(ProjectAsset.project_id == project_id))

    await session.execute(delete(TeamChat).where(TeamChat.project_id == project_id))
    await session.execute(delete(UploadedFile).where(UploadedFile.project_id == project_id))

    await session.execute(delete(TokenUsageLog).where(TokenUsageLog.project_id == project_id))
    await session.execute(delete(AgentBootReport).where(AgentBootReport.project_id == project_id))
    await session.execute(delete(AgentRehydrationJob).where(AgentRehydrationJob.project_id == project_id))

    await session.execute(delete(Backup).where(Backup.project_id == project_id))
    await session.execute(delete(PrototypeRun).where(PrototypeRun.project_id == project_id))

    await session.execute(delete(Task).where(Task.project_id == project_id))
    await session.execute(delete(Agent).where(Agent.project_id == project_id))
    await session.execute(delete(AgentTeam).where(AgentTeam.project_id == project_id))

    await session.execute(delete(ChangeRequest).where(ChangeRequest.project_id == project_id))
    await session.execute(delete(StageProgress).where(StageProgress.project_id == project_id))
    await session.execute(delete(Iteration).where(Iteration.project_id == project_id))


def _apply_project_row(project: Project, row: dict) -> None:
    for k, v in row.items():
        if k not in Project.__table__.columns:
            continue
        setattr(project, k, _coerce_column(Project, k, v))


async def import_project_database_payload(session: AsyncSession, project_id: str, payload: dict) -> dict[str, int]:
    project = await session.get(Project, project_id)
    if not project:
        raise ValueError("项目不存在")

    tables = payload["tables"]
    await purge_project_export_tables(session, project_id)
    counts: dict[str, int] = {}

    def add_many(key: str, model_cls, rows: list | None):
        if not isinstance(rows, list):
            rows = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            session.add(_row_to_model(model_cls, r))
        counts[key] = len(rows)

    add_many("iterations", Iteration, tables.get("iterations"))
    add_many("change_requests", ChangeRequest, tables.get("change_requests"))
    add_many("stage_progress", StageProgress, tables.get("stage_progress"))
    add_many("agent_teams", AgentTeam, tables.get("agent_teams"))
    add_many("agents", Agent, tables.get("agents"))
    add_many("tasks", Task, tables.get("tasks"))
    add_many("project_assets", ProjectAsset, tables.get("project_assets"))
    add_many("documents", Document, tables.get("documents"))
    add_many("generation_tasks", GenerationTask, tables.get("generation_tasks"))
    add_many("messages", Message, tables.get("messages"))
    add_many("task_documents", TaskDocument, tables.get("task_documents"))
    add_many("task_comments", TaskComment, tables.get("task_comments"))
    add_many("task_logs", TaskLog, tables.get("task_logs"))
    add_many("agent_messages", AgentMessage, tables.get("agent_messages"))
    add_many("team_chats", TeamChat, tables.get("team_chats"))
    add_many("uploaded_files", UploadedFile, tables.get("uploaded_files"))
    add_many("conversations", Conversation, tables.get("conversations"))
    add_many("conversation_messages", ConversationMessage, tables.get("conversation_messages"))
    add_many("agent_boot_reports", AgentBootReport, tables.get("agent_boot_reports"))
    add_many("agent_rehydration_jobs", AgentRehydrationJob, tables.get("agent_rehydration_jobs"))
    add_many("token_usage_logs", TokenUsageLog, tables.get("token_usage_logs"))
    add_many("prototype_runs", PrototypeRun, tables.get("prototype_runs"))

    prows = tables.get("projects")
    if isinstance(prows, list) and prows and isinstance(prows[0], dict):
        _apply_project_row(project, prows[0])

    await session.flush()
    logger.info("project_db_import: project=%s counts=%s", project_id, counts)
    return counts
