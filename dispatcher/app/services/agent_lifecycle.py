"""
Agent lifecycle hooks (Vue-like).

Design target:
- Most roles share the same lifecycle hooks.
- Role-specific behavior is injected via payload/context, not branch-heavy code.
"""

from datetime import datetime, timezone

from app.models import Agent, Task
from app.services import agent_lifecycle_roles

AGENT_LIFECYCLE_STEPS = (
    "beforeCreate",
    "created",
    "beforeMount",
    "mounted",
    "beforeTaskStart",
    "afterTaskEnd",
    "beforeUnmount",
    "unmounted",
)

TRACE_LIMIT = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_trace(agent: Agent, step: str, payload: dict | None = None):
    cfg = dict(agent.config or {})
    trace = list(cfg.get("lifecycle_trace") or [])
    trace.append({
        "step": step,
        "at": _now_iso(),
        "payload": payload or {},
    })
    if len(trace) > TRACE_LIMIT:
        trace = trace[-TRACE_LIMIT:]
    cfg["lifecycle_step"] = step
    cfg["lifecycle_trace"] = trace
    agent.config = cfg


def on_created(agent: Agent, boot_id: str | None = None, recovery_mode: str | None = None):
    cfg = dict(agent.config or {})
    clean_boot = (boot_id or "").strip()
    last_boot = (cfg.get("lifecycle_last_boot_id") or "").strip()
    if clean_boot and clean_boot == last_boot:
        return
    resolved_recovery = recovery_mode or cfg.get("recovery_mode", "")
    payload = agent_lifecycle_roles.role_created_payload(agent, clean_boot, resolved_recovery)
    _append_trace(agent, "created", payload)
    cfg = dict(agent.config or {})
    cfg["lifecycle_last_boot_id"] = clean_boot
    agent.config = cfg


def on_mounted(agent: Agent):
    role_payload = agent_lifecycle_roles.role_mounted_payload(agent)
    _append_trace(agent, "mounted", {
        "heartbeat_status": agent.last_heartbeat_status,
        "status": agent.status,
        **role_payload,
    })


def before_mount(agent: Agent, trigger: str, pending_remote_push: bool = False):
    _append_trace(agent, "beforeMount", {
        "trigger": trigger,
        "pending_remote_push": pending_remote_push,
        "heartbeat_status": agent.last_heartbeat_status,
        "status": agent.status,
    })


def before_task_start(agent: Agent, task: Task):
    _append_trace(agent, "beforeTaskStart", {
        "task_id": task.id,
        "task_ref": task.ref_id or "",
    })


def after_task_end(agent: Agent, task: Task, outcome: str):
    _append_trace(agent, "afterTaskEnd", {
        "task_id": task.id,
        "task_ref": task.ref_id or "",
        "outcome": outcome,
    })
    cfg = dict(agent.config or {})
    task_ctx = task.context or {}
    if task_ctx.get("architect_bootstrap") and outcome in ("done", "cancelled", "superseded"):
        cfg["architect_bootstrap_done"] = True
        agent.config = cfg


def can_start_task(agent: Agent, task: Task) -> tuple[bool, str]:
    return agent_lifecycle_roles.can_start_task(agent, task)


def should_try_bootstrap_on_mounted(agent: Agent) -> bool:
    return agent_lifecycle_roles.should_try_bootstrap_on_mounted(agent)
