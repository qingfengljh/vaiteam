from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AgentRehydrationJob, Task
from app.services import task_lifecycle


async def run_pending_jobs(session: AsyncSession, limit: int = 20) -> list[str]:
    q = await session.execute(
        select(AgentRehydrationJob)
        .where(AgentRehydrationJob.status == "pending")
        .order_by(AgentRehydrationJob.created_at.asc())
        .limit(max(1, min(limit, 200)))
    )
    jobs = list(q.scalars())
    if not jobs:
        return []

    for job in jobs:
        job.status = "running"
    await session.commit()

    done_ids: list[str] = []
    for job in jobs:
        try:
            result = await _execute_job(session, job)
            job.status = "done"
            job.result = result
            job.finished_at = datetime.now(timezone.utc)
            done_ids.append(job.id)
        except Exception as e:
            job.status = "failed"
            job.result = {"error": f"{type(e).__name__}: {e}"}
            job.finished_at = datetime.now(timezone.utc)
    await session.commit()
    return done_ids


async def _execute_job(session: AsyncSession, job: AgentRehydrationJob) -> dict:
    agent = await session.get(Agent, job.agent_id)
    if not agent:
        raise ValueError(f"agent not found: {job.agent_id}")
    if agent.project_id != job.project_id:
        raise ValueError(f"project mismatch for agent {job.agent_id}")

    cfg = dict(agent.config or {})
    snapshot = dict(job.snapshot or {})
    steps: list[str] = []
    released_tasks: list[str] = []

    # 先清理当前 task 指针，避免 agent 端状态与调度状态不一致。
    if agent.current_task_id:
        task = await session.get(Task, agent.current_task_id)
        if task and task.assigned_agent == agent.id and task.status in ("assigned", "executing", "reviewing"):
            task.assigned_agent = None
            session.add(task_lifecycle.transition(
                task,
                event="rehydrate_release",
                actor="dispatcher",
                reason=f"rehydration job {job.id} release stale assignment",
            ))
            released_tasks.append(task.id)
        agent.current_task_id = None
        steps.append("clear_current_task_pointer")

    if job.mode == "cold_start":
        cfg["context_versions"] = {}
        cfg["retriever_ready"] = False
        cfg["recovery_mode"] = "cold_start"
        steps.append("reset_context_versions_for_cold_start")
    elif job.mode == "partial_rehydrate":
        current_versions = cfg.get("context_versions") if isinstance(cfg.get("context_versions"), dict) else {}
        incoming_versions = snapshot.get("context_versions") if isinstance(snapshot.get("context_versions"), dict) else {}
        merged_versions = dict(current_versions)
        merged_versions.update(incoming_versions)
        cfg["context_versions"] = merged_versions
        cfg["retriever_ready"] = bool(snapshot.get("retriever_ready", cfg.get("retriever_ready", True)))
        cfg["recovery_mode"] = "partial_rehydrate"
        steps.append("merge_context_versions")
    else:
        cfg["recovery_mode"] = job.mode or "unknown"
        steps.append("noop_for_unknown_mode")

    cfg["rehydration_last_job_id"] = job.id
    cfg["rehydration_last_reason"] = job.reason or ""
    cfg["rehydration_last_at"] = datetime.now(timezone.utc).isoformat()
    agent.config = cfg

    assigned_count = 0
    if not agent.current_task_id and cfg.get("retriever_ready", True):
        from app.services import scheduler
        assigned = await scheduler.auto_assign(session, agent.project_id, actor_role="dispatcher")
        assigned_count = len(assigned or [])
        if assigned_count > 0:
            steps.append("trigger_auto_assign")

    return {
        "mode": job.mode,
        "agent_id": agent.id,
        "released_tasks": released_tasks,
        "assigned_count": assigned_count,
        "steps": steps,
    }
