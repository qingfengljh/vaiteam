#!/usr/bin/env python3
"""
只读：从 Dispatcher 拉单个任务 JSON，生成供 Claude Code / 人类使用的启动摘要（stdout）。

环境变量：
  VAI_DISPATCHER_URL   例如 http://localhost:8000（无尾斜杠）
  VAI_JWT              Bearer 与 Web 相同的 JWT（/api/auth/login 获取）
  VAI_TASK_ID          任务 id
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    base = (os.environ.get("VAI_DISPATCHER_URL") or "").rstrip("/")
    token = os.environ.get("VAI_JWT") or ""
    task_id = os.environ.get("VAI_TASK_ID") or ""
    if not base or not token or not task_id:
        print("need VAI_DISPATCHER_URL, VAI_JWT, VAI_TASK_ID", file=sys.stderr)
        return 2
    url = f"{base}/api/tasks/{task_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            task = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    pack = {
        "task_pack_version": 1,
        "ref_id": task.get("ref_id") or "",
        "title": task.get("title") or "",
        "instruction": (task.get("description") or "")[:8000],
        "branch": task.get("git_branch") or "",
        "git_base_branch": (task.get("context") or {}).get("git_base_branch") or "",
        "executor_hint": task.get("executor_hint") or "connector",
        "actor_type": task.get("actor_type") or "agent",
        "context_keys": [],
    }
    summary = (
        f"# 任务 {pack['ref_id']}\n"
        f"- executor_hint: {pack['executor_hint']}\n"
        f"- actor_type: {pack['actor_type']}\n"
        f"- branch: {pack['branch']}\n"
        f"- git_base_branch: {pack['git_base_branch']}\n\n"
        f"## 说明\n{pack['title']}\n\n## 指令摘要\n{pack['instruction'][:2000]}\n\n"
        f"## CC 启动前建议\n"
        f"在仓库根执行 `git checkout {pack['branch']}`（若分支已存在）。\n"
        f"将下列 JSON 存为 task-pack.json 后（在 dispatcher/ 下）跑 `CC_TASK_PACK=... python3 tools/cc-worker/run_task_pack.py`。\n\n"
        f"```json\n{json.dumps(pack, ensure_ascii=False, indent=2)}\n```\n"
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
