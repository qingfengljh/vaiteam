"""原型 CC 运行：登记 run、校验 webhook secret、收口状态（与 webhook + worker 脚本对齐）。"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Project, PrototypeRun

logger = logging.getLogger(__name__)


def hash_run_secret(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_run_secret(plain: str, secret_hash: str) -> bool:
    if not plain or not secret_hash:
        return False
    try:
        return secrets.compare_digest(hash_run_secret(plain), secret_hash)
    except Exception:
        return False


async def start_run(
    session: AsyncSession,
    *,
    project: Project,
    prototype_doc: Document,
    technical_doc: Document | None,
) -> tuple[PrototypeRun, str]:
    """创建 running 记录并返回 (run, plaintext_secret)。secret 仅出现一次，由调用方返回给客户端。"""
    plain = secrets.token_urlsafe(32)
    snap: dict[str, Any] = {
        "prototype_document_id": prototype_doc.id,
        "prototype_title": prototype_doc.title,
        "technical_document_id": technical_doc.id if technical_doc else None,
        "technical_title": technical_doc.title if technical_doc else None,
        "iteration_id": project.current_iteration_id,
        "task_pack_path": f"/api/prototype-workshop/projects/{project.id}/task-pack",
    }
    run = PrototypeRun(
        project_id=project.id,
        iteration_id=project.current_iteration_id,
        status="running",
        prototype_document_id=prototype_doc.id,
        technical_document_id=technical_doc.id if technical_doc else None,
        secret_hash=hash_run_secret(plain),
        snapshot=snap,
    )
    session.add(run)
    await session.flush()
    logger.info("prototype_run started run_id=%s project_id=%s", run.id, project.id)
    return run, plain


async def complete_run_via_webhook(
    session: AsyncSession,
    *,
    run_id: str,
    secret_plain: str,
    status: str,
    exit_code: int | None,
    summary: str,
    error: str,
    artifact_ref: str,
) -> PrototypeRun:
    run = await session.get(PrototypeRun, run_id)
    if not run:
        raise ValueError("run not found")
    if not verify_run_secret(secret_plain, run.secret_hash):
        raise ValueError("invalid run secret")
    if run.status in ("succeeded", "failed"):
        return run
    if status not in ("succeeded", "failed"):
        raise ValueError("status must be succeeded or failed")
    run.status = status
    run.exit_code = exit_code
    run.error_message = (error or "")[:8000]
    run.result = {
        "summary": (summary or "")[:8000],
        "artifact_ref": (artifact_ref or "")[:2000],
        "exit_code": exit_code,
    }
    run.finished_at = datetime.now(timezone.utc)
    await session.flush()
    logger.info("prototype_run finished run_id=%s status=%s", run.id, status)
    return run


async def find_active_running(session: AsyncSession, project_id: str) -> PrototypeRun | None:
    """同一项目仅允许一条 status=running（未完成 webhook 前不可再 start）。"""
    q = await session.execute(
        select(PrototypeRun)
        .where(PrototypeRun.project_id == project_id, PrototypeRun.status == "running")
        .order_by(PrototypeRun.created_at.desc())
        .limit(1)
    )
    return q.scalar_one_or_none()


async def list_runs(session: AsyncSession, project_id: str, limit: int = 30) -> list[PrototypeRun]:
    lim = min(max(limit, 1), 100)
    q = await session.execute(
        select(PrototypeRun)
        .where(PrototypeRun.project_id == project_id)
        .order_by(PrototypeRun.created_at.desc())
        .limit(lim)
    )
    return list(q.scalars())
