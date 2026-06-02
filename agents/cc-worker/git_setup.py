"""Git 仓库设置：克隆、切换分支、配置作者。"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"


def setup_repo(
    repo_url: str,
    workspace: str,
    *,
    branch: str | None = None,
    base_branch: str | None = None,
    git_user_name: str = "VAI CC Worker",
    git_user_email: str = "cc-worker@vaiteam.local",
) -> dict:
    """克隆仓库并切换到指定分支。返回 {"ok": bool, "branch": str, "error": str}。"""
    result: dict = {"ok": False, "branch": branch or "", "error": ""}

    # 1. 克隆（如果目录不存在）
    if not os.path.isdir(os.path.join(workspace, ".git")):
        logger.info(f"Cloning {repo_url} -> {workspace}")
        code, out, err = _run(["git", "clone", repo_url, workspace], timeout=120)
        if code != 0:
            result["error"] = f"git clone failed: {err}"
            return result

    # 2. 配置 git 作者
    _run(["git", "config", "user.name", git_user_name], cwd=workspace)
    _run(["git", "config", "user.email", git_user_email], cwd=workspace)

    # 3. 空库初始化：如果没有提交，创建初始 commit
    code, out, err = _run(["git", "rev-parse", "HEAD"], cwd=workspace)
    if code != 0:
        # 空仓库 — 创建一个空初始提交作为所有分支的 base
        _run(["git", "checkout", "-b", "develop"], cwd=workspace)
        code, out, err = _run(["git", "commit", "--allow-empty", "-m", "chore: initial empty commit"], cwd=workspace)
        if code == 0:
            _run(["git", "push", "origin", "develop"], cwd=workspace)
            logger.info("Initialized empty repo with develop branch")

    # 4. 获取远程更新
    code, out, err = _run(["git", "fetch", "origin"], cwd=workspace, timeout=60)
    if code != 0:
        result["error"] = f"git fetch failed: {err}"
        return result

    # 5. 切换分支
    target = (branch or "").strip()
    if target:
        # 检查分支是否存在（本地或远程）
        code, out, err = _run(["git", "branch", "-a"], cwd=workspace)
        branches = out.split("\n")
        has_local = any(b.strip().lstrip("* ").strip() == target for b in branches)
        has_remote = any(f"remotes/origin/{target}" in b for b in branches)

        if has_local:
            code, out, err = _run(["git", "checkout", target], cwd=workspace)
        elif has_remote:
            code, out, err = _run(["git", "checkout", "-b", target, f"origin/{target}"], cwd=workspace)
        else:
            # 基于 base_branch 创建新分支
            base = (base_branch or "develop").strip()
            code, out, err = _run(["git", "checkout", base], cwd=workspace)
            if code != 0:
                result["error"] = f"git checkout {base} failed: {err}"
                return result
            code, out, err = _run(["git", "checkout", "-b", target], cwd=workspace)

        if code != 0:
            result["error"] = f"git checkout branch {target} failed: {err}"
            return result

        # 二次校验
        code, out, err = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace)
        actual = out.strip()
        if actual != target:
            result["error"] = f"branch mismatch: expected {target}, got {actual}"
            return result

        result["branch"] = actual

    result["ok"] = True
    logger.info(f"Git setup ok: branch={result['branch']}, workspace={workspace}")
    return result


def commit_and_push(workspace: str, message: str) -> dict:
    """提交并推送。返回 {"ok": bool, "commit": str, "error": str}。"""
    result: dict = {"ok": False, "commit": "", "error": ""}

    # 1. 检查是否有变更
    code, out, err = _run(["git", "status", "--porcelain"], cwd=workspace)
    if not out.strip():
        logger.info("No changes to commit")
        result["ok"] = True
        return result

    # 2. add
    code, out, err = _run(["git", "add", "-A"], cwd=workspace)
    if code != 0:
        result["error"] = f"git add failed: {err}"
        return result

    # 3. commit
    code, out, err = _run(["git", "commit", "-m", message], cwd=workspace)
    if code != 0:
        result["error"] = f"git commit failed: {err}"
        return result

    # 4. push
    code, out, err = _run(["git", "push", "origin", "HEAD"], cwd=workspace, timeout=60)
    if code != 0:
        result["error"] = f"git push failed: {err}"
        return result

    # 5. 获取 commit hash
    code, out, err = _run(["git", "rev-parse", "HEAD"], cwd=workspace)
    result["commit"] = out.strip()
    result["ok"] = True
    logger.info(f"Commit and push ok: {result['commit']}")
    return result


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    repo = os.environ.get("GIT_REPO_URL", "")
    ws = os.environ.get("WORKSPACE", "/workspace/repo")
    if not repo:
        print("Need GIT_REPO_URL", file=sys.stderr)
        sys.exit(1)
    r = setup_repo(repo, ws, branch=os.environ.get("GIT_BRANCH"))
    print(json.dumps(r, ensure_ascii=False, indent=2))
