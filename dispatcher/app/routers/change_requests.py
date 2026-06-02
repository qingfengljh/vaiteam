"""变更请求路由：提出变更 → AI 影响分析 → 人工决策 → 执行"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.models import (
    Project, Iteration, Task, ChangeRequest, StageProgress,
)
from app.services import ai_leader, token_tracker

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/change-requests",
    tags=["change-requests"],
)


class ChangeRequestCreate(BaseModel):
    description: str


class ChangeRequestDecision(BaseModel):
    decision: str  # append | terminate_and_new
    new_iteration_title: str = ""
    new_iteration_desc: str = ""
    cancel_task_ids: list[str] = []


@router.get("")
async def list_change_requests(project_id: str, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(ChangeRequest)
        .where(ChangeRequest.project_id == project_id)
        .order_by(ChangeRequest.created_at.desc())
    )
    return [_cr_to_dict(cr) for cr in q.scalars()]


@router.post("")
async def create_change_request(
    project_id: str, body: ChangeRequestCreate,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    iteration_id = project.current_iteration_id
    if not iteration_id:
        raise HTTPException(400, "项目没有活跃的迭代")

    cr = ChangeRequest(
        project_id=project_id,
        iteration_id=iteration_id,
        description=body.description,
    )
    session.add(cr)
    await session.flush()

    tasks_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.iteration_id == iteration_id,
        )
    )
    tasks = tasks_q.scalars().all()

    task_summary = []
    for t in tasks:
        task_summary.append({
            "id": t.id, "ref_id": t.ref_id, "title": t.title,
            "status": t.status, "suggested_role": t.suggested_role,
            "output_files": t.output_files or [],
        })

    total = len(task_summary)
    done = sum(1 for t in task_summary if t["status"] == "done")
    in_progress = sum(1 for t in task_summary if t["status"] in ("assigned", "reviewing"))
    pending = sum(1 for t in task_summary if t["status"] in ("draft", "pending"))

    token_tracker.set_context(project_id=project_id)
    try:
        analysis = await ai_leader.analyze_change_impact(
            change_description=body.description,
            tasks=task_summary,
            project_name=project.name,
            project_type=project.project_type or "new",
        )
    except Exception as e:
        logger.warning(f"Change impact analysis failed: {e}")
        analysis = {
            "affected_task_ids": [],
            "affected_ratio": 0,
            "recommendation": "append",
            "reason": f"AI 分析失败，请人工判断: {str(e)[:200]}",
            "details": "",
        }

    cr.impact_analysis = {
        "total_tasks": total,
        "done_tasks": done,
        "in_progress_tasks": in_progress,
        "pending_tasks": pending,
        **analysis,
    }
    cr.affected_tasks = analysis.get("affected_task_ids", [])

    await session.commit()
    await session.refresh(cr)
    return _cr_to_dict(cr)


@router.post("/{cr_id}/decide")
async def decide_change_request(
    project_id: str, cr_id: str, body: ChangeRequestDecision,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    cr = await session.get(ChangeRequest, cr_id)
    if not cr or cr.project_id != project_id:
        raise HTTPException(404, "ChangeRequest not found")

    if cr.decision != "pending":
        raise HTTPException(400, "变更请求已处理")

    if body.decision not in ("append", "terminate_and_new", "rejected"):
        raise HTTPException(400, "decision must be: append | terminate_and_new | rejected")

    cr.decision = body.decision

    if body.decision == "rejected":
        await session.commit()
        return {"status": "rejected"}

    if body.decision == "append":
        cancelled = 0
        for task_id in body.cancel_task_ids:
            task = await session.get(Task, task_id)
            if task and task.project_id == project_id and task.status not in ("done", "cancelled"):
                task.status = "cancelled"
                cancelled += 1
        await session.commit()
        return {"status": "appended", "cancelled_tasks": cancelled}

    if body.decision == "terminate_and_new":
        iteration = await session.get(Iteration, cr.iteration_id)
        if not iteration:
            raise HTTPException(400, "原迭代不存在")

        cancelled = 0
        tasks_q = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.iteration_id == cr.iteration_id,
                Task.status.in_(["draft", "pending", "assigned", "reviewing", "blocked"]),
            )
        )
        for task in tasks_q.scalars():
            task.status = "cancelled"
            cancelled += 1

        iteration.status = "terminated"

        max_seq = await session.scalar(
            select(func.max(Iteration.seq)).where(Iteration.project_id == project_id)
        ) or 0

        new_iter = Iteration(
            project_id=project_id,
            seq=max_seq + 1,
            title=body.new_iteration_title or f"v{max_seq + 1}.0",
            description=body.new_iteration_desc or f"基于变更请求创建，继承迭代 #{iteration.seq}",
            start_stage=0,
            current_stage=0,
            status="planning",
            parent_iteration_id=iteration.id,
        )
        session.add(new_iter)
        await session.flush()

        for stage in range(8):
            session.add(StageProgress(
                project_id=project_id,
                iteration_id=new_iter.id,
                stage=stage,
                status="in_progress" if stage == 0 else "pending",
            ))

        cr.new_iteration_id = new_iter.id

        project.current_iteration_id = new_iter.id
        project.current_stage = 0

        await session.commit()
        return {
            "status": "terminated_and_created",
            "cancelled_tasks": cancelled,
            "new_iteration_id": new_iter.id,
            "new_iteration_seq": new_iter.seq,
        }


def _cr_to_dict(cr: ChangeRequest) -> dict:
    return {
        "id": cr.id,
        "project_id": cr.project_id,
        "iteration_id": cr.iteration_id,
        "description": cr.description,
        "impact_analysis": cr.impact_analysis,
        "decision": cr.decision,
        "new_iteration_id": cr.new_iteration_id,
        "affected_tasks": cr.affected_tasks,
        "created_at": cr.created_at.isoformat() if cr.created_at else None,
    }
