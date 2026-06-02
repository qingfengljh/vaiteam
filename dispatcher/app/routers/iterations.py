"""迭代管理路由：CRUD + 激活 + 终止 + 列表"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.models import Project, Iteration, StageProgress, Task, Document, Message, GenerationTask

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/iterations", tags=["iterations"])

PLANNING_STAGES = {0, 1, 2, 3}


class IterationCreate(BaseModel):
    title: str
    description: str = ""
    start_stage: int = 0
    parent_iteration_id: str | None = None


class IterationUpdate(BaseModel):
    title: str | None = None
    description: str | None = None


@router.get("")
async def list_iterations(project_id: str, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(Iteration)
        .where(Iteration.project_id == project_id)
        .order_by(Iteration.seq)
    )
    iters = q.scalars().all()
    result = []
    for it in iters:
        task_count = await session.scalar(
            select(func.count(Task.id)).where(Task.iteration_id == it.id)
        ) or 0
        doc_count = await session.scalar(
            select(func.count(Document.id)).where(
                Document.iteration_id == it.id,
                Document.project_id == project_id,
            )
        ) or 0
        result.append(_iter_to_dict(it, task_count=task_count, doc_count=doc_count))
    return result


@router.get("/{iteration_id}")
async def get_iteration(project_id: str, iteration_id: str, session: AsyncSession = Depends(get_session)):
    it = await session.get(Iteration, iteration_id)
    if not it or it.project_id != project_id:
        raise HTTPException(404, "Iteration not found")
    task_count = await session.scalar(
        select(func.count(Task.id)).where(Task.iteration_id == it.id)
    ) or 0
    doc_count = await session.scalar(
        select(func.count(Document.id)).where(
            Document.iteration_id == it.id,
            Document.project_id == project_id,
        )
    ) or 0
    return _iter_to_dict(it, task_count=task_count, doc_count=doc_count)


@router.post("")
async def create_iteration(project_id: str, body: IterationCreate, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    max_seq = await session.scalar(
        select(func.max(Iteration.seq)).where(Iteration.project_id == project_id)
    ) or 0

    if body.parent_iteration_id:
        parent = await session.get(Iteration, body.parent_iteration_id)
        if not parent or parent.project_id != project_id:
            raise HTTPException(400, "父迭代不存在")

    it = Iteration(
        project_id=project_id,
        seq=max_seq + 1,
        title=body.title,
        description=body.description,
        start_stage=body.start_stage,
        current_stage=body.start_stage,
        status="planning",
        parent_iteration_id=body.parent_iteration_id,
    )
    session.add(it)

    for stage in range(body.start_stage, 8):
        session.add(StageProgress(
            project_id=project_id,
            iteration_id=it.id,
            stage=stage,
            status="in_progress" if stage == body.start_stage else "pending",
        ))

    await session.commit()
    await session.refresh(it)
    return _iter_to_dict(it)


@router.put("/{iteration_id}")
async def update_iteration(
    project_id: str, iteration_id: str, body: IterationUpdate,
    session: AsyncSession = Depends(get_session),
):
    it = await session.get(Iteration, iteration_id)
    if not it or it.project_id != project_id:
        raise HTTPException(404, "Iteration not found")
    if body.title is not None:
        it.title = body.title
    if body.description is not None:
        it.description = body.description
    await session.commit()
    return _iter_to_dict(it)


@router.post("/{iteration_id}/activate")
async def activate_iteration(project_id: str, iteration_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    it = await session.get(Iteration, iteration_id)
    if not it or it.project_id != project_id:
        raise HTTPException(404, "Iteration not found")

    if it.status in ("completed", "terminated"):
        raise HTTPException(400, "已完成或已终止的迭代不能重新激活")

    if it.current_stage >= 4:
        other_active = await session.execute(
            select(Iteration).where(
                Iteration.project_id == project_id,
                Iteration.status == "active",
                Iteration.id != iteration_id,
            )
        )
        if other_active.scalars().first():
            raise HTTPException(400, "已有执行中的迭代，同一时间只能有一个迭代处于执行阶段（Stage 4+）")

    q = await session.execute(
        select(Iteration).where(
            Iteration.project_id == project_id,
            Iteration.status == "active",
            Iteration.id != iteration_id,
        )
    )
    for other in q.scalars():
        other.status = "planning"

    it.status = "active"
    project.current_iteration_id = it.id
    project.current_stage = it.current_stage
    await session.commit()

    return {"activated": True, "iteration_id": it.id, "current_stage": it.current_stage}


@router.post("/{iteration_id}/terminate")
async def terminate_iteration(project_id: str, iteration_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    it = await session.get(Iteration, iteration_id)
    if not it or it.project_id != project_id:
        raise HTTPException(404, "Iteration not found")

    if it.status in ("completed", "terminated"):
        raise HTTPException(400, "迭代已经结束")

    cancelled_count = 0
    tasks_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.iteration_id == iteration_id,
            Task.status.in_(["draft", "pending", "assigned", "reviewing", "blocked"]),
        )
    )
    for task in tasks_q.scalars():
        task.status = "cancelled"
        cancelled_count += 1

    it.status = "terminated"

    if project.current_iteration_id == iteration_id:
        project.current_iteration_id = None
        project.current_stage = 0
        await _try_activate_next(session, project)

    await session.commit()
    logger.info(f"Iteration {it.seq} terminated, {cancelled_count} tasks cancelled")
    return {"terminated": True, "cancelled_tasks": cancelled_count}


async def _try_activate_next(session: AsyncSession, project: Project):
    """尝试激活下一个就绪的 planning 迭代（Stage 3 已完成）"""
    q = await session.execute(
        select(Iteration).where(
            Iteration.project_id == project.id,
            Iteration.status == "planning",
        ).order_by(Iteration.seq)
    )
    for candidate in q.scalars():
        if candidate.current_stage >= 3:
            sp_q = await session.execute(
                select(StageProgress).where(
                    StageProgress.project_id == project.id,
                    StageProgress.iteration_id == candidate.id,
                    StageProgress.stage == 3,
                    StageProgress.status == "completed",
                )
            )
            if sp_q.scalars().first():
                candidate.status = "active"
                project.current_iteration_id = candidate.id
                project.current_stage = candidate.current_stage
                logger.info(f"Auto-activated iteration {candidate.seq} for project {project.id}")
                return
    logger.info(f"No ready planning iteration to auto-activate for project {project.id}")


@router.delete("/{iteration_id}")
async def delete_iteration(project_id: str, iteration_id: str, session: AsyncSession = Depends(get_session)):
    it = await session.get(Iteration, iteration_id)
    if not it or it.project_id != project_id:
        raise HTTPException(404, "Iteration not found")

    if it.status == "active":
        raise HTTPException(400, "不能删除当前活跃的迭代")

    # 删除关联数据
    for model in (StageProgress, Document, Message, Task):
        q = await session.execute(
            select(model).where(
                model.project_id == project_id,
                model.iteration_id == iteration_id,
            )
        )
        for row in q.scalars():
            await session.delete(row)

    await session.delete(it)
    await session.commit()
    return {"deleted": True}


def _iter_to_dict(it: Iteration, task_count: int = 0, doc_count: int = 0) -> dict:
    return {
        "id": it.id,
        "project_id": it.project_id,
        "seq": it.seq,
        "title": it.title,
        "description": it.description,
        "start_stage": it.start_stage,
        "current_stage": it.current_stage,
        "status": it.status,
        "parent_iteration_id": it.parent_iteration_id,
        "release_branch": it.release_branch,
        "release_tag": it.release_tag,
        "release_status": it.release_status,
        "task_count": task_count,
        "doc_count": doc_count,
        "created_at": it.created_at.isoformat() if it.created_at else None,
        "updated_at": it.updated_at.isoformat() if it.updated_at else None,
    }
