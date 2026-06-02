"""环境自检 + 智能修复。失败则上报，禁止跳过环境问题继续编码。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = int(os.environ.get("ENV_SELF_HEAL_MAX_ATTEMPTS", "3"))


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"


def check_git() -> tuple[bool, str]:
    code, out, err = _run(["git", "--version"])
    if code == 0:
        return True, out.strip()
    return False, f"git not available: {err}"


def check_python() -> tuple[bool, str]:
    code, out, err = _run(["python3", "--version"])
    if code == 0:
        return True, out.strip()
    return False, f"python3 not available: {err}"


def check_node() -> tuple[bool, str]:
    code, out, err = _run(["node", "--version"])
    if code == 0:
        return True, out.strip()
    return False, f"node not available: {err}"


def check_npm() -> tuple[bool, str]:
    code, out, err = _run(["npm", "--version"])
    if code == 0:
        return True, out.strip()
    return False, f"npm not available: {err}"


def check_claude() -> tuple[bool, str]:
    code, out, err = _run(["claude", "--version"])
    if code == 0:
        return True, out.strip()
    return False, f"claude not available: {err}"


def install_apt(packages: list[str]) -> tuple[bool, str]:
    """尝试用 apt 安装缺失包。"""
    logger.info(f"Attempting apt install: {packages}")
    code, out, err = _run(["apt-get", "update", "-qq"], timeout=60)
    if code != 0:
        return False, f"apt-get update failed: {err}"
    code, out, err = _run(
        ["apt-get", "install", "-y", "--no-install-recommends"] + packages,
        timeout=120,
    )
    if code != 0:
        return False, f"apt-get install failed: {err}"
    return True, f"installed: {packages}"


def install_pip(packages: list[str]) -> tuple[bool, str]:
    """尝试用 pip 安装缺失包。"""
    logger.info(f"Attempting pip install: {packages}")
    code, out, err = _run(
        ["pip3", "install", "--no-cache-dir"] + packages,
        timeout=120,
    )
    if code != 0:
        return False, f"pip install failed: {err}"
    return True, f"installed: {packages}"


def install_npm_global(packages: list[str]) -> tuple[bool, str]:
    """尝试用 npm 全局安装缺失包。"""
    logger.info(f"Attempting npm install -g: {packages}")
    code, out, err = _run(
        ["npm", "install", "-g"] + packages,
        timeout=120,
    )
    if code != 0:
        return False, f"npm install failed: {err}"
    return True, f"installed: {packages}"


# 检查项 → (检查函数, 修复策略)
CHECK_REGISTRY: list[tuple[str, callable, list[callable]]] = [
    ("git", check_git, [lambda: install_apt(["git"])]),
    ("python3", check_python, [lambda: install_apt(["python3", "python3-pip"])]),
    ("node", check_node, [lambda: install_apt(["nodejs"])]),
    ("npm", check_npm, [lambda: install_apt(["npm"])]),
    ("claude", check_claude, [lambda: install_npm_global(["@anthropic-ai/claude-code"])]),
]


def self_heal(toolchain: list[str] | None = None) -> dict:
    """执行环境自检和修复。返回报告字典。

    若某检查在 MAX_ATTEMPTS 次尝试后仍失败，报告为 failed，调用方应阻塞任务。
    """
    report: dict = {"passed": [], "fixed": [], "failed": []}
    checks = CHECK_REGISTRY

    # 如果指定了 toolchain，只检查相关项
    if toolchain:
        tc_set = set(toolchain)
        checks = [c for c in CHECK_REGISTRY if c[0] in tc_set]

    for name, check_fn, fix_fns in checks:
        ok, msg = check_fn()
        if ok:
            report["passed"].append({"tool": name, "version": msg})
            continue

        logger.warning(f"{name} missing: {msg}")
        fixed = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            for fix_fn in fix_fns:
                fix_ok, fix_msg = fix_fn()
                if fix_ok:
                    # 修复后再次检查
                    ok2, msg2 = check_fn()
                    if ok2:
                        report["fixed"].append({
                            "tool": name,
                            "attempt": attempt,
                            "detail": fix_msg,
                            "version": msg2,
                        })
                        fixed = True
                        break
                    else:
                        logger.warning(f"{name} still missing after fix: {msg2}")
            if fixed:
                break
            logger.warning(f"{name} fix attempt {attempt}/{MAX_ATTEMPTS} failed")

        if not fixed:
            report["failed"].append({"tool": name, "error": msg})

    report["overall"] = "ok" if not report["failed"] else "failed"
    return report


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = self_heal()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["overall"] == "ok" else 1)
