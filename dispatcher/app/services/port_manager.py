"""端口分配管理器 — 为每个项目自动分配不冲突的端口段"""

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Project

PORT_RANGE_START = 10000
PORT_RANGE_END = 60000
PORTS_PER_PROJECT = 10


async def allocate_ports(session: AsyncSession, project_id: str) -> int:
    """为项目分配端口段，返回起始端口号"""
    project = await session.get(Project, project_id)
    if project and project.port_range_start:
        return project.port_range_start

    result = await session.execute(
        select(func.max(Project.port_range_start))
    )
    max_start = result.scalar() or (PORT_RANGE_START - PORTS_PER_PROJECT)
    new_start = max_start + PORTS_PER_PROJECT

    if new_start + PORTS_PER_PROJECT > PORT_RANGE_END:
        raise ValueError(f"端口池已耗尽（{PORT_RANGE_START}-{PORT_RANGE_END}）")

    if project:
        project.port_range_start = new_start
        await session.flush()

    return new_start


async def get_project_ports(session: AsyncSession, project_id: str) -> dict:
    """获取项目的端口分配信息"""
    project = await session.get(Project, project_id)
    if not project or not project.port_range_start:
        return {"allocated": False}

    start = project.port_range_start
    return {
        "allocated": True,
        "start": start,
        "end": start + PORTS_PER_PROJECT - 1,
        "mapping": {
            "architect": start,
            "senior": start + 1,
            "mid": start + 2,
            "junior": start + 3,
            "devops": start + 4,
            "reserved": list(range(start + 5, start + PORTS_PER_PROJECT)),
        },
    }


async def release_ports(session: AsyncSession, project_id: str):
    """释放项目的端口分配"""
    project = await session.get(Project, project_id)
    if project:
        project.port_range_start = None
        await session.flush()
