"""阶段文档标题序号前缀：10、20… 递增，便于目录排序与中间插入。"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document

_PREFIX_RE = re.compile(r"^(\d{4})\s+")


def strip_doc_title_order_prefix(title: str) -> str:
    if not title:
        return ""
    t = title.strip()
    m = _PREFIX_RE.match(t)
    if m:
        return t[m.end() :].strip()
    return t


def collect_used_order_slots(titles: list[str]) -> set[int]:
    used: set[int] = set()
    for t in titles:
        if not t:
            continue
        m = _PREFIX_RE.match(t.strip())
        if m:
            used.add(int(m.group(1)))
    return used


def alloc_next_order_slots(used: set[int], count: int) -> list[int]:
    out: list[int] = []
    n = 10
    for _ in range(count):
        while n in used:
            n += 10
        out.append(n)
        used.add(n)
        n += 10
    return out


async def next_prefixed_titles_for_stage(
    session: AsyncSession,
    project_id: str,
    iteration_id: str | None,
    stage: int,
    cores: list[str],
) -> list[str]:
    """cores 为已去掉旧前缀的展示名，非空。"""
    q = await session.execute(
        select(Document.title).where(
            Document.project_id == project_id,
            Document.iteration_id == iteration_id,
            Document.stage == stage,
        )
    )
    titles = [r[0] for r in q.all()]
    used = collect_used_order_slots(titles)
    slots = alloc_next_order_slots(used, len(cores))
    return [f"{s:04d} {c}" for s, c in zip(slots, cores, strict=True)]
