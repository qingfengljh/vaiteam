#!/usr/bin/env python3
"""
JSON 任务包 → 可选调用本机 Claude Code CLI → 收集 diff / exit code / 日志路径。
默认 dry-run（不调用 claude），用于 CI 与无 CLI 环境。

环境变量：
  CC_TASK_PACK   任务包 JSON 文件路径（默认与本脚本同目录的 sample-task-pack.v1.json）
  CC_WORKSPACE   Git 工作区根（默认 openclaw-team 仓库根：本脚本所在 cc-worker 上三级）
  CC_RUN_MODE    dry-run | invoke（invoke 时需本机已安装 `claude` 且非交互策略由本地配置决定）
  CC_CLAUDE_BIN  覆盖 claude 可执行文件名
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_CC_DIR = str(Path(__file__).resolve().parent)
if _CC_DIR not in sys.path:
    sys.path.insert(0, _CC_DIR)

from task_pack_adapter import strip_internal_keys

REQUIRED_VERSION = 1


def _repo_root() -> Path:
    # dispatcher/tools/cc-worker/<this>.py -> parents[3] = openclaw-team 根
    return Path(__file__).resolve().parents[3]


def _load_pack(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return normalize_pack(data)


def normalize_pack(data: dict) -> dict:
    """去掉适配层内部键并校验 task_pack_version 1。"""
    pack = dict(strip_internal_keys(dict(data)))
    ver = pack.get("task_pack_version")
    if ver != REQUIRED_VERSION:
        raise SystemExit(f"unsupported task_pack_version: {ver!r}, need {REQUIRED_VERSION}")
    for k in ("ref_id", "instruction", "executor_hint", "actor_type"):
        if k not in pack:
            raise SystemExit(f"missing required field: {k}")
    return pack


def execute_pack(pack: dict, root: Path, mode: str | None = None) -> tuple[int, dict]:
    """
    执行已规范化的任务包；返回 (进程语义 exit code, 摘要 dict)。
    summary 含 exit_code、log_path、diff_stat 或 error。
    """
    mode = (mode or os.environ.get("CC_RUN_MODE") or "dry-run").strip().lower()
    log_dir = root / ".cc-worker-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    log_path = log_dir / f"run-{pack['ref_id']}-{ts}.log"
    lines = [
        f"task_pack_version={pack['task_pack_version']}",
        f"ref_id={pack['ref_id']}",
        f"executor_hint={pack['executor_hint']}",
        f"actor_type={pack['actor_type']}",
        f"mode={mode}",
        "",
        "--- instruction ---",
        pack["instruction"],
        "",
    ]
    if mode == "dry-run":
        lines.append("dry-run: skipped claude invoke")
        lines.append("diff_stat:")
        lines.append(_git_diff_stat(root))
        log_path.write_text("\n".join(lines), encoding="utf-8")
        out = {"exit_code": 0, "log_path": str(log_path), "diff_stat": _git_diff_stat(root).strip()}
        return 0, out

    if mode != "invoke":
        return 2, {"exit_code": 2, "error": f"unknown CC_RUN_MODE={mode!r}"}

    claude_bin = os.environ.get("CC_CLAUDE_BIN") or "claude"
    prompt = pack["instruction"]
    cmd = [claude_bin, "-p", prompt]
    if os.environ.get("CC_CLAUDE_BARE") == "1":
        cmd.insert(1, "--bare")
    r = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    lines.append(f"claude_exit={r.returncode}")
    lines.append("stdout:")
    lines.append(r.stdout or "")
    lines.append("stderr:")
    lines.append(r.stderr or "")
    lines.append("diff_stat:")
    lines.append(_git_diff_stat(root))
    log_path.write_text("\n".join(lines), encoding="utf-8")
    exit_code = 0 if r.returncode == 0 else min(r.returncode, 125)
    summary = {"exit_code": r.returncode, "log_path": str(log_path), "diff_stat": _git_diff_stat(root).strip()}
    return exit_code, summary


def _git_diff_stat(cwd: Path) -> str:
    r = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return (r.stdout or "") + (r.stderr or "")


def main() -> int:
    root = Path(os.environ.get("CC_WORKSPACE") or _repo_root())
    pack_path = Path(os.environ.get("CC_TASK_PACK") or Path(__file__).parent / "sample-task-pack.v1.json")
    mode = (os.environ.get("CC_RUN_MODE") or "dry-run").strip().lower()
    pack = _load_pack(pack_path)
    code, summary = execute_pack(pack, root, mode)
    print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as e:
        raise
    except Exception as e:
        print(json.dumps({"exit_code": 99, "error": str(e)}), file=sys.stderr)
        raise SystemExit(99)
