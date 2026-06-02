#!/usr/bin/env python3
"""只读拉取原型 task-pack 并写入 JSON 文件。"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_worker_dir = str(Path(__file__).resolve().parent)
if _worker_dir not in sys.path:
    sys.path.insert(0, _worker_dir)
from git_preflight import GitPreflightError, require_git_remote_ok  # noqa: E402


def main() -> int:
    try:
        require_git_remote_ok()
    except GitPreflightError as e:
        print(f"git preflight: {e}", file=sys.stderr)
        return 5

    base = (os.environ.get("VAI_DISPATCHER_URL") or "").rstrip("/")
    token = os.environ.get("VAI_JWT") or ""
    project_id = os.environ.get("VAI_PROJECT_ID") or ""
    out = sys.argv[1] if len(sys.argv) > 1 else "pack.json"
    if not base or not token or not project_id:
        print("need VAI_DISPATCHER_URL, VAI_JWT, VAI_PROJECT_ID", file=sys.stderr)
        return 2
    url = f"{base}/api/prototype-workshop/projects/{project_id}/task-pack"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
