"""向 Dispatcher 既有 `/api/webhook/task-complete` / `task-failed` 收口（与 openclaw/mq_worker 回调对齐）。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def _post(base: str, path: str, body: dict, timeout: int = 120) -> dict:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def post_task_complete(
    *,
    dispatcher_base: str,
    task_id: str,
    agent_id: str,
    result: str,
    attempt_id: str = "",
    token_usage: dict | None = None,
    duration_ms: int | None = None,
) -> dict:
    body: dict = {
        "task_id": task_id,
        "agent_id": agent_id,
        "result": result,
        "attempt_id": attempt_id or "",
        "files_changed": [],
    }
    if token_usage:
        body["token_usage"] = token_usage
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    return _post(dispatcher_base, "/api/webhook/task-complete", body)


def post_task_failed(
    *,
    dispatcher_base: str,
    task_id: str,
    agent_id: str,
    error: str,
    attempt_id: str = "",
    token_usage: dict | None = None,
    duration_ms: int | None = None,
) -> dict:
    body: dict = {
        "task_id": task_id,
        "agent_id": agent_id,
        "error": error,
        "attempt_id": attempt_id or "",
    }
    if token_usage:
        body["token_usage"] = token_usage
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    return _post(dispatcher_base, "/api/webhook/task-failed", body)


def post_from_env_succeeded(summary: dict, meta: dict, agent_id: str) -> dict:
    base = (os.environ.get("VAI_DISPATCHER_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("VAI_DISPATCHER_URL required for webhook")
    task_id = meta.get("task_id") or ""
    attempt_id = meta.get("attempt_id") or ""
    result = json.dumps(summary, ensure_ascii=False)
    return post_task_complete(
        dispatcher_base=base,
        task_id=task_id,
        agent_id=agent_id,
        result=result,
        attempt_id=attempt_id,
    )


def post_from_env_failed(err: str, meta: dict, agent_id: str) -> dict:
    base = (os.environ.get("VAI_DISPATCHER_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("VAI_DISPATCHER_URL required for webhook")
    task_id = meta.get("task_id") or ""
    attempt_id = meta.get("attempt_id") or ""
    return post_task_failed(
        dispatcher_base=base,
        task_id=task_id,
        agent_id=agent_id,
        error=err[:20000],
        attempt_id=attempt_id,
    )


