"""
Task lifecycle hooks (Vue-like).

This module keeps task transition traces predictable and reusable.
"""

from datetime import datetime, timezone

from app.models import Task, TaskLog
from app.services import task_lifecycle_profiles

TASK_LIFECYCLE_STEPS = (
    "created",
    "ready",
    "assigned",
    "running",
    "reviewing",
    "done",
    "failed",
    "blocked",
    "cancelled",
    "need_clarification",
)

EVENT_TO_TRANSITION: dict[str, tuple[str, str]] = {
    "ready": ("pending", "onReady"),
    "assign": ("assigned", "beforeTaskStart"),
    "start_execute": ("executing", "onTaskRunning"),
    "submit_review": ("reviewing", "onReviewing"),
    "assign_prepare_failed": ("pending", "onAssignPrepareFailed"),
    "requeue": ("pending", "onRequeue"),
    "rework": ("pending", "onRework"),
    "retry": ("pending", "onRetry"),
    "escalate": ("pending", "onEscalate"),
    "need_clarification": ("need_clarification", "onNeedClarification"),
    "clarification_resolved": ("pending", "onClarificationResolved"),
    "clarification_approved": ("pending", "onClarificationApproved"),
    "terminal_done": ("done", "afterTaskEnd"),
    "terminal_failed": ("failed", "afterTaskEnd"),
    "terminal_blocked": ("blocked", "afterTaskEnd"),
    "terminal_cancelled": ("cancelled", "afterTaskEnd"),
    "release": ("pending", "onReleased"),
    "reset": ("pending", "onReset"),
    "recover": ("pending", "onRecover"),
    "startup_recover": ("pending", "onStartupRecovery"),
    "architect_intent_requeue": ("pending", "onArchitectIntent"),
    "rehydrate_release": ("pending", "onRehydrateRelease"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_ctx(task: Task) -> dict:
    ctx = dict(task.context or {})
    task.context = ctx
    return ctx


def _task_profile(task: Task) -> str:
    return task_lifecycle_profiles.resolve_profile(task)


def on_status_change(
    task: Task,
    *,
    to_status: str,
    hook: str,
    actor: str = "system",
    reason: str = "",
    metadata: dict | None = None,
) -> TaskLog:
    from_status = task.status
    profile = _task_profile(task)
    ok, reason_hint = task_lifecycle_profiles.is_transition_allowed(profile, from_status, to_status)
    if not ok:
        raise ValueError(f"invalid lifecycle transition: {reason_hint}")
    hook, reason, normalized_meta = task_lifecycle_profiles.adapt_transition(
        task,
        profile=profile,
        hook=hook,
        reason=reason,
        metadata=metadata,
    )
    task.status = to_status
    task.updated_at = datetime.now(timezone.utc)

    ctx = _ensure_ctx(task)
    lifecycle = dict(ctx.get("lifecycle") or {})
    lifecycle["profile"] = profile
    lifecycle["last_hook"] = hook
    lifecycle["last_transition"] = f"{from_status}->{to_status}"
    lifecycle["updated_at"] = _now_iso()
    ctx["lifecycle"] = lifecycle
    task.context = ctx

    return TaskLog(
        task_id=task.id,
        agent_id=actor,
        action=f"lifecycle_{hook}",
        message=reason or f"{from_status}->{to_status}",
        metadata_={
            "from_status": from_status,
            "to_status": to_status,
            **normalized_meta,
        },
    )


def transition(
    task: Task,
    *,
    event: str,
    actor: str = "system",
    reason: str = "",
    metadata: dict | None = None,
    to_status: str | None = None,
    hook: str | None = None,
) -> TaskLog:
    mapped = EVENT_TO_TRANSITION.get(event)
    next_status = to_status or (mapped[0] if mapped else "")
    next_hook = hook or (mapped[1] if mapped else "")
    if not next_status or not next_hook:
        raise ValueError(f"unknown lifecycle event: {event}")
    merged_meta = {"event": event, **(metadata or {})}
    return on_status_change(
        task,
        to_status=next_status,
        hook=next_hook,
        actor=actor,
        reason=reason,
        metadata=merged_meta,
    )


def on_assigned(task: Task, agent_id: str, model: str = "", actor: str = "scheduler") -> TaskLog:
    return transition(
        task,
        event="assign",
        actor=actor,
        reason=f"assigned to {agent_id}",
        metadata={"assigned_agent": agent_id, "model": model},
    )


def on_reviewing(task: Task, actor: str, reason: str = "") -> TaskLog:
    return transition(
        task,
        event="submit_review",
        actor=actor,
        reason=reason or "submit for review",
    )


def on_terminal(task: Task, status: str, actor: str, reason: str = "") -> TaskLog:
    event_map = {
        "done": "terminal_done",
        "failed": "terminal_failed",
        "blocked": "terminal_blocked",
        "cancelled": "terminal_cancelled",
    }
    event = event_map.get(status, "")
    if not event:
        raise ValueError(f"unsupported terminal status: {status}")
    return transition(
        task,
        event=event,
        actor=actor,
        reason=reason or f"terminal: {status}",
    )
