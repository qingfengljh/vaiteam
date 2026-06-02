"""
Task lifecycle profile strategy.

Profile-specific differences are centralized here, so task_lifecycle.py
keeps a stable generic pipeline.
"""

from app.models import Task

TERMINAL_STATUSES = {"done", "failed", "blocked", "cancelled", "superseded"}

BASE_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending", "cancelled"},
    "pending": {"assigned", "reviewing", "executing", "blocked", "failed", "cancelled"},
    "assigned": {"executing", "reviewing", "pending", "blocked", "failed", "cancelled"},
    "executing": {"reviewing", "pending", "blocked", "failed", "cancelled", "need_clarification"},
    "reviewing": {"done", "pending", "blocked", "failed", "cancelled"},
    "need_clarification": {"pending", "blocked", "cancelled"},
    "blocked": {"pending", "assigned", "failed", "cancelled"},
    "failed": {"pending", "assigned", "cancelled"},
    "done": {"superseded"},
    "cancelled": set(),
    "superseded": set(),
}

PROFILE_EXTRA_ALLOWED: dict[str, dict[str, set[str]]] = {
    "architect_bootstrap": {
        "pending": {"assigned", "failed", "cancelled"},
        "reviewing": {"done", "blocked", "failed"},
    },
    "module": {
        "pending": {"done"},
        "assigned": {"done"},
    },
    "subtask": {},
    "general": {},
}


def resolve_profile(task: Task) -> str:
    ctx = task.context or {}
    if ctx.get("architect_bootstrap") is True:
        return "architect_bootstrap"
    if ctx.get("is_module") is True:
        return "module"
    if task.parent_task_id:
        return "subtask"
    return "general"


def adapt_transition(
    task: Task,
    *,
    profile: str,
    hook: str,
    reason: str,
    metadata: dict | None,
) -> tuple[str, str, dict]:
    meta = dict(metadata or {})

    if profile == "architect_bootstrap":
        hook_alias = {
            "onReady": "onBootstrapReady",
            "beforeTaskStart": "beforeBootstrapStart",
            "onReviewing": "onBootstrapReviewing",
            "onRequeue": "onBootstrapRequeue",
            "afterTaskEnd": "afterBootstrapEnd",
        }
        hook = hook_alias.get(hook, hook)
        meta.setdefault("scope", "architect_bootstrap")
        meta.setdefault("one_time", True)
        if not reason:
            reason = "architect bootstrap transition"
        return hook, reason, meta

    if profile == "module":
        meta.setdefault("scope", "module")
        meta.setdefault("module_id", task.id)
        if not reason:
            reason = "module task transition"
        return hook, reason, meta

    if profile == "subtask":
        meta.setdefault("scope", "subtask")
        meta.setdefault("parent_task_id", task.parent_task_id or "")
        if not reason:
            reason = "subtask transition"
        return hook, reason, meta

    meta.setdefault("scope", "general")
    return hook, reason, meta


def is_transition_allowed(profile: str, from_status: str, to_status: str) -> tuple[bool, str]:
    if from_status == to_status:
        return True, ""

    base_allowed = BASE_ALLOWED_TRANSITIONS.get(from_status, set())
    profile_extra = PROFILE_EXTRA_ALLOWED.get(profile, {})
    extra_allowed = profile_extra.get(from_status, set())
    allowed = base_allowed | extra_allowed
    if to_status in allowed:
        return True, ""

    if from_status in TERMINAL_STATUSES:
        return False, f"terminal status {from_status} cannot transition to {to_status}"
    return False, f"transition {from_status}->{to_status} not allowed for profile {profile}"

