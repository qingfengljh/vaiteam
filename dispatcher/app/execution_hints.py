"""派单与任务包用的 executor_hint / actor_type（与 46/50/74 文档一致，默认兼容旧 connector 路径）。"""

from __future__ import annotations

# 协议层可扩展；未知值在入口拒绝，避免写库脏枚举
EXECUTOR_HINTS = frozenset({"connector", "claude_code", "prototype_cc", "stub"})
# openclaw 为历史别名，落库统一为 connector
EXECUTOR_ALIASES = {"openclaw": "connector"}

ACTOR_TYPES = frozenset({"human", "agent", "connector", "claude_code", "prototype_cc", "stub"})

DEFAULT_EXECUTOR_HINT = "connector"


class ExecutionHintError(ValueError):
    pass


def normalize_executor_hint(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    v = str(value).strip().lower()
    v = EXECUTOR_ALIASES.get(v, v)
    if v not in EXECUTOR_HINTS:
        raise ExecutionHintError(
            f"executor_hint must be one of {sorted(EXECUTOR_HINTS)} or alias openclaw, got {value!r}"
        )
    return v


def normalize_actor_type(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    v = str(value).strip().lower()
    if v not in ACTOR_TYPES:
        raise ExecutionHintError(f"actor_type must be one of {sorted(ACTOR_TYPES)}, got {value!r}")
    return v


def resolved_executor_hint(context: dict | None) -> str:
    ctx = context or {}
    raw = ctx.get("executor_hint")
    if raw is None or raw == "":
        return DEFAULT_EXECUTOR_HINT
    return normalize_executor_hint(str(raw)) or DEFAULT_EXECUTOR_HINT


def resolve_actor_type_for_assign(
    context: dict | None,
    *,
    agent_id: str,
    executor_hint: str,
) -> str:
    ctx = context or {}
    explicit = normalize_actor_type(ctx.get("actor_type"))
    if explicit:
        return explicit
    if (agent_id or "").startswith("human:"):
        return "human"
    if executor_hint == "claude_code":
        return "claude_code"
    if executor_hint == "prototype_cc":
        return "prototype_cc"
    if executor_hint == "stub":
        return "stub"
    return "agent"


def merge_assign_hints_into_context(
    context: dict | None,
    *,
    agent_id: str,
) -> tuple[dict, str, str]:
    """返回 (新 context, executor_hint, actor_type)。"""
    ctx = dict(context or {})
    eh = resolved_executor_hint(ctx)
    at = resolve_actor_type_for_assign(ctx, agent_id=agent_id, executor_hint=eh)
    ctx["executor_hint"] = eh
    ctx["actor_type"] = at
    return ctx, eh, at
