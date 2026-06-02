#!/usr/bin/env python3
"""
从一条 MQ `send_task` JSON（stdin 或 CC_DISPATCH_JSON 文件）映射任务包 → 工作区执行 → webhook 收口。

环境变量：
  VAI_DISPATCHER_URL   Dispatcher 基址（webhook）
  VAI_AGENT_ID        与派单 agent_id 一致
  CC_WORKSPACE        Git 工作区根
  CC_RUN_MODE         dry-run | invoke（同 run_task_pack.py）
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
from pathlib import Path

_CC_DIR = str(Path(__file__).resolve().parent)
if _CC_DIR not in sys.path:
    sys.path.insert(0, _CC_DIR)

from report_task_webhook import post_task_complete, post_task_failed  # noqa: E402
from run_task_pack import execute_pack, normalize_pack  # noqa: E402
from task_pack_adapter import task_pack_from_dispatch_message  # noqa: E402


def _repo_root() -> Path:
    # dispatcher/tools/cc-worker/<this>.py -> parents[3] = openclaw-team 根（与 run_task_pack 一致）
    return Path(__file__).resolve().parents[3]


def run_one_dispatch_message(data: dict) -> int:
    if (data.get("type") or "") != "send_task":
        return 0
    meta = data.get("metadata") or {}
    eh = (meta.get("executor_hint") or "").strip().lower()
    if eh != "claude_code":
        return 0

    agent_id = data.get("agent_id") or os.environ.get("VAI_AGENT_ID") or ""
    if not agent_id:
        print("missing agent_id on message and VAI_AGENT_ID", file=sys.stderr)
        return 2

    root = Path(os.environ.get("CC_WORKSPACE") or _repo_root())
    mode = (os.environ.get("CC_RUN_MODE") or "dry-run").strip().lower()

    pack = normalize_pack(task_pack_from_dispatch_message(data))
    t0 = time.perf_counter()
    code, summary = execute_pack(pack, root, mode)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    base = (os.environ.get("VAI_DISPATCHER_URL") or "").rstrip("/")
    if not base:
        print(json.dumps({"skipped_webhook": True, "summary": summary}, ensure_ascii=False))
        return code

    task_id = meta.get("task_id") or ""
    attempt_id = meta.get("attempt_id") or ""
    result_text = json.dumps(summary, ensure_ascii=False)

    try:
        if code == 0:
            post_task_complete(
                dispatcher_base=base,
                task_id=task_id,
                agent_id=agent_id,
                result=result_text,
                attempt_id=attempt_id,
                duration_ms=duration_ms,
            )
        else:
            post_task_failed(
                dispatcher_base=base,
                task_id=task_id,
                agent_id=agent_id,
                error=result_text,
                attempt_id=attempt_id,
                duration_ms=duration_ms,
            )
    except urllib.error.URLError as e:
        print(str(e), file=sys.stderr)
        return 3
    return code


def main() -> int:
    path = os.environ.get("CC_DISPATCH_JSON")
    if path:
        raw = Path(path).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        print("need stdin JSON or CC_DISPATCH_JSON file", file=sys.stderr)
        return 2
    data = json.loads(raw)
    return run_one_dispatch_message(data)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(99)
