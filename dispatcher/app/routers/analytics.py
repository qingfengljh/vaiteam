"""项目分析仪表盘：进展图、成本统计、质量指标"""

from datetime import datetime, timezone, timedelta
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models import Task, TaskLog, TokenUsageLog
from app.services import token_tracker

router = APIRouter(prefix="/api/projects/{project_id}/analytics", tags=["analytics"])


@router.get("/progress")
async def progress_chart(project_id: str, session: AsyncSession = Depends(get_session)):
    """任务完成进展折线图：累计完成数 + 累计工时随时间"""
    q = await session.execute(
        select(Task).where(Task.project_id == project_id, Task.status != "draft")
    )
    tasks = list(q.scalars())
    if not tasks:
        return {"points": [], "summary": {}}

    total_tasks = len(tasks)
    total_hours = sum(t.estimated_hours or 0.5 for t in tasks)
    start_date = min(t.created_at for t in tasks).date()
    today = datetime.now(timezone.utc).date()

    done_logs_q = await session.execute(
        select(TaskLog.task_id, func.min(TaskLog.created_at).label("done_at"))
        .where(
            TaskLog.task_id.in_([t.id for t in tasks]),
            TaskLog.action.in_(["review_approved", "completed", "done", "module_completed"]),
        )
        .group_by(TaskLog.task_id)
    )
    done_map: dict[str, datetime] = {r.task_id: r.done_at for r in done_logs_q}
    for t in tasks:
        if t.status == "done" and t.id not in done_map:
            done_map[t.id] = t.updated_at
    task_hours = {t.id: (t.estimated_hours or 0.5) for t in tasks}

    created_q = await session.execute(
        select(TaskLog.task_id, func.min(TaskLog.created_at).label("created_at"))
        .where(
            TaskLog.task_id.in_([t.id for t in tasks]),
            TaskLog.action == "approved",
        )
        .group_by(TaskLog.task_id)
    )
    approved_map: dict[str, datetime] = {r.task_id: r.created_at for r in created_q}

    total_days = max((today - start_date).days, 1)
    points = []
    cum_done_count = 0
    cum_done_hours = 0.0
    cum_approved_count = 0

    for day_offset in range(total_days + 1):
        d = start_date + timedelta(days=day_offset)

        for tid, done_at in done_map.items():
            if done_at.date() == d and tid in task_hours:
                cum_done_count += 1
                cum_done_hours += task_hours[tid]

        for tid, approved_at in approved_map.items():
            if approved_at.date() == d:
                cum_approved_count += 1

        points.append({
            "date": d.isoformat(),
            "done_count": cum_done_count,
            "done_hours": round(cum_done_hours, 1),
            "approved_count": cum_approved_count,
            "total_tasks": total_tasks,
            "total_hours": round(total_hours, 1),
            "done_pct": round(cum_done_count / total_tasks * 100, 1),
        })

    by_stage = defaultdict(lambda: {"count": 0, "done": 0, "hours": 0.0})
    for t in tasks:
        stage = t.type or "feature"
        by_stage[stage]["count"] += 1
        by_stage[stage]["hours"] += t.estimated_hours or 0.5
        if t.status == "done":
            by_stage[stage]["done"] += 1

    return {
        "points": points,
        "summary": {
            "total_tasks": total_tasks,
            "done_tasks": cum_done_count,
            "total_hours": round(total_hours, 1),
            "done_hours": round(cum_done_hours, 1),
            "by_type": dict(by_stage),
        },
    }


@router.get("/cost")
async def cost_dashboard(project_id: str, session: AsyncSession = Depends(get_session)):
    """成本仪表盘：按模型/调用者的费用统计"""
    summary = await token_tracker.project_cost_summary(session, project_id)
    daily = await token_tracker.project_cost_daily(session, project_id)
    hourly = await token_tracker.project_cost_hourly(session, project_id)
    by_caller = await token_tracker.project_cost_by_caller(session, project_id)

    q = await session.execute(
        select(func.count()).where(Task.project_id == project_id, Task.status == "done")
    )
    done_count = q.scalar() or 0
    total_calls = summary["total_calls"]
    total_tokens = summary["total_input_tokens"] + summary["total_output_tokens"]
    avg_cost = round(summary["total_cost_usd"] / done_count, 4) if done_count else 0
    avg_tpm = round(total_tokens / max(total_calls, 1))

    return {
        **summary,
        "daily": daily,
        "hourly": hourly,
        "by_caller": by_caller,
        "done_tasks": done_count,
        "avg_cost_per_task": avg_cost,
        "avg_tokens_per_call": avg_tpm,
    }


@router.get("/quality")
async def quality_metrics(project_id: str, session: AsyncSession = Depends(get_session)):
    """质量指标：一次通过率、升级率、平均重试次数"""
    q = await session.execute(
        select(Task).where(Task.project_id == project_id, Task.status != "draft")
    )
    tasks = list(q.scalars())
    if not tasks:
        return {"total": 0}

    total = len(tasks)
    done = [t for t in tasks if t.status == "done"]
    first_pass = sum(1 for t in done if t.retry_count == 0 and t.escalation_level == 0)
    escalated_to_architect = sum(1 for t in tasks if t.escalation_level >= 1)
    escalated_to_human = sum(1 for t in tasks if t.escalation_level >= 2)
    blocked = sum(1 for t in tasks if t.status in ("blocked", "failed"))
    total_retries = sum(t.retry_count for t in tasks)

    by_role = defaultdict(lambda: {"total": 0, "done": 0, "retries": 0, "escalated": 0})
    for t in tasks:
        role = t.suggested_role or "unknown"
        by_role[role]["total"] += 1
        by_role[role]["retries"] += t.retry_count
        if t.status == "done":
            by_role[role]["done"] += 1
        if t.escalation_level >= 1:
            by_role[role]["escalated"] += 1

    return {
        "total": total,
        "done": len(done),
        "first_pass_count": first_pass,
        "first_pass_rate": round(first_pass / len(done) * 100, 1) if done else 0,
        "escalation_to_architect": escalated_to_architect,
        "escalation_to_architect_rate": round(escalated_to_architect / total * 100, 1),
        "escalation_to_human": escalated_to_human,
        "escalation_to_human_rate": round(escalated_to_human / total * 100, 1),
        "blocked_or_failed": blocked,
        "avg_retries": round(total_retries / total, 2),
        "total_retries": total_retries,
        "by_role": dict(by_role),
    }
