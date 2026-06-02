"""task:dispatch → Connector/OpenClaw 转发分流（与 execution_hints、46/50 文档一致）。"""

from __future__ import annotations

# 不经过 OpenClaw Agent hooks：由 Claude Code / 原型工坊 / Stub 等独立 Worker 收口 webhook
SKIP_OPENCLAW_FOR_EXECUTOR_HINTS = frozenset({"claude_code", "prototype_cc", "stub"})


def should_skip_openclaw_for_dispatch(metadata: dict | None) -> bool:
    eh = (metadata or {}).get("executor_hint") or ""
    return eh in SKIP_OPENCLAW_FOR_EXECUTOR_HINTS
