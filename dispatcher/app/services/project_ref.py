"""项目 URL 路径 code：小写字母、数字、减号；与内部 id（8 位 hex）并存。"""

from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

RESERVED_CODES = frozenset({
    "login", "projects", "settings", "experiences", "help", "api", "assets",
    "manual-images", "favicon", "index", "robots", "sitemap", "infra",
})

_CODE_CHARS = re.compile(r"^[a-z0-9-]{1,64}$")
_ID_HEX = re.compile(r"^[a-f0-9]{8}$")


def default_code_from_name(name: str, project_id: str) -> str:
    buf: list[str] = []
    for ch in name.lower():
        if ch.isascii() and (ch.isalnum() or ch in " -"):
            buf.append(ch)
    raw = "".join(buf)
    s = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if len(s) < 2:
        s = f"p-{project_id}"
    if s in RESERVED_CODES:
        s = f"p-{project_id}"
    return s[:64]


def _validate_shape(code: str) -> None:
    if not _CODE_CHARS.match(code):
        raise HTTPException(
            status_code=400,
            detail="项目 code 仅允许小写字母、数字、减号，长度 1–64",
        )
    if code.startswith("-") or code.endswith("-"):
        raise HTTPException(status_code=400, detail="项目 code 不能以减号开头或结尾")
    if "--" in code:
        raise HTTPException(status_code=400, detail="项目 code 不能包含连续减号")
    if code in RESERVED_CODES:
        raise HTTPException(status_code=400, detail="该 code 为系统保留路径，请更换")


async def assert_code_unique(
    session: AsyncSession,
    code: str,
    *,
    exclude_project_id: str | None,
) -> None:
    from app.models import Project

    q = select(Project.id).where(func.lower(Project.code) == code.lower())
    if exclude_project_id:
        q = q.where(Project.id != exclude_project_id)
    r = await session.execute(q)
    if r.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="项目 code 已被占用")


async def assert_code_not_project_id_collision(session: AsyncSession, code: str) -> None:
    """避免 code 与任意项目 id 相同，防止解析歧义。"""
    from app.models import Project

    if not _ID_HEX.match(code):
        return
    row = await session.get(Project, code)
    if row:
        raise HTTPException(status_code=400, detail="code 不能与已有项目 id 同形（8 位十六进制），请更换")


async def resolve_project(session: AsyncSession, ref: str):
    from app.models import Project

    ref = (ref or "").strip()
    if not ref:
        return None
    if _ID_HEX.match(ref):
        p = await session.get(Project, ref)
        if p:
            return p
    r = await session.execute(select(Project).where(func.lower(Project.code) == ref.lower()))
    return r.scalar_one_or_none()


def normalize_incoming_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip().lower()
    return s or None


async def prepare_code_for_create(
    session: AsyncSession,
    *,
    code_in: str | None,
    name: str,
    project_id: str,
) -> str:
    n = normalize_incoming_code(code_in)
    code = n if n else default_code_from_name(name, project_id)
    _validate_shape(code)
    await assert_code_not_project_id_collision(session, code)
    await assert_code_unique(session, code, exclude_project_id=None)
    return code


async def prepare_code_for_update(
    session: AsyncSession,
    *,
    code_in: str,
    exclude_project_id: str,
) -> str:
    code = normalize_incoming_code(code_in)
    if not code:
        raise HTTPException(status_code=400, detail="项目 code 不能为空")
    _validate_shape(code)
    await assert_code_not_project_id_collision(session, code)
    await assert_code_unique(session, code, exclude_project_id=exclude_project_id)
    return code
