"""
将 MQ task:dispatch / cc_task_dispatch inbox 载荷映射为 task_pack_version 1（与 execution_hints、scheduler task_metadata 对齐）。
"""
from __future__ import annotations

REQUIRED_VERSION = 1


def task_pack_from_dispatch_message(msg: dict) -> dict:
    """
    `task:dispatch` 中 type=send_task 的一条消息（见 mq.publish_dispatch）。
    """
    meta = dict(msg.get("metadata") or {})
    instruction = msg.get("instruction") or ""
    return {
        "task_pack_version": REQUIRED_VERSION,
        "ref_id": meta.get("ref_id") or "",
        "title": "",
        "instruction": instruction,
        "branch": meta.get("git_branch") or "",
        "git_base_branch": meta.get("git_base_branch") or "",
        "executor_hint": meta.get("executor_hint") or "connector",
        "actor_type": meta.get("actor_type") or "agent",
        "context_keys": [],
        "_dispatch_metadata": meta,
    }


def task_pack_from_cc_inbox_payload(payload: dict) -> dict:
    """messaging cc_task_dispatch 的 payload（含完整 instruction + task_metadata）。"""
    meta = dict(payload.get("metadata") or {})
    instruction = payload.get("instruction") or ""
    return {
        "task_pack_version": REQUIRED_VERSION,
        "ref_id": meta.get("ref_id") or "",
        "title": "",
        "instruction": instruction,
        "branch": meta.get("git_branch") or "",
        "git_base_branch": meta.get("git_base_branch") or "",
        "executor_hint": meta.get("executor_hint") or "claude_code",
        "actor_type": meta.get("actor_type") or "claude_code",
        "context_keys": [],
        "_dispatch_metadata": meta,
    }


def strip_internal_keys(pack: dict) -> dict:
    """写入磁盘给 run_task_pack 前去掉非 schema 键。"""
    return {k: v for k, v in pack.items() if not k.startswith("_")}
