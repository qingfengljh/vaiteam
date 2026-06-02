"""
Role-specific lifecycle strategy.

Keep branching logic out of scheduler/heartbeat.
"""

from app.models import Agent, Task


def role_created_payload(agent: Agent, boot_id: str, recovery_mode: str) -> dict:
    if agent.role == "architect":
        return {
            "role_policy": "architect-global-owner",
            "boot_id": boot_id,
            "recovery_mode": recovery_mode,
        }
    return {"role_policy": "default", "boot_id": boot_id, "recovery_mode": recovery_mode}


def role_mounted_payload(agent: Agent) -> dict:
    if agent.role == "architect":
        return {
            "role_policy": "architect-global-owner",
            "retriever_ready": bool((agent.config or {}).get("retriever_ready", True)),
        }
    return {"role_policy": "default"}


def can_start_task(agent: Agent, task: Task) -> tuple[bool, str]:
    if agent.role != "architect":
        return True, ""
    cfg = dict(agent.config or {})
    retriever_ready = cfg.get("retriever_ready", True)
    if retriever_ready is False:
        return False, "architect retriever not ready"

    task_ctx = task.context or {}
    if task_ctx.get("architect_bootstrap") is True:
        return True, ""

    # 架构师初始化任务未完成前，架构师只允许先执行这一个任务
    bootstrap_task_id = (cfg.get("architect_bootstrap_task_id") or "").strip()
    bootstrap_done = bool(cfg.get("architect_bootstrap_done"))
    if bootstrap_task_id and not bootstrap_done:
        return False, "architect bootstrap task not finished"
    return True, ""


def should_try_bootstrap_on_mounted(agent: Agent) -> bool:
    if agent.role != "architect":
        return False
    if agent.status != "idle" or agent.current_task_id:
        return False
    if (agent.config or {}).get("retriever_ready", True) is False:
        return False
    return True

