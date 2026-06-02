import json
from typing import Any

from app.models import Agent

PENDING_REMOTE_PUSH_KEY = "pending_remote_push"
PENDING_REMOTE_PUSH_ERROR_KEY = "pending_remote_push_error"


def is_pending_remote_push(agent: Agent) -> bool:
    cfg = agent.config or {}
    return bool(cfg.get(PENDING_REMOTE_PUSH_KEY))


def pending_remote_push_error(agent: Agent) -> str:
    cfg = agent.config or {}
    return str(cfg.get(PENDING_REMOTE_PUSH_ERROR_KEY) or "")


def apply_remote_push_result(agent: Agent, push_warning: str | None):
    cfg = dict(agent.config or {})
    if push_warning:
        cfg[PENDING_REMOTE_PUSH_KEY] = True
        cfg[PENDING_REMOTE_PUSH_ERROR_KEY] = push_warning
    else:
        cfg[PENDING_REMOTE_PUSH_KEY] = False
        cfg[PENDING_REMOTE_PUSH_ERROR_KEY] = ""
    agent.config = cfg


def ensure_team_result_meta(created: int, roles: list[str], deferred_push_roles: list[str]) -> str:
    payload: dict[str, Any] = {
        "created": created,
        "roles": roles,
        "deferred_push_roles": deferred_push_roles,
    }
    return json.dumps(payload, ensure_ascii=False)
