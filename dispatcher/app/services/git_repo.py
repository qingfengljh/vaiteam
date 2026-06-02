"""
Git 仓库管理服务

职责：
  - clone / pull 项目仓库到本地工作目录
  - 文档审核通过后同步 commit + push 到仓库
  - 任务分支创建、commit 记录、合并
  - 提供 git_web_url 拼接工具
  - 规范化 commit message（AI 严格 / 人类宽松，分支名做硬关联）

信任源：数据库 → Git（单向推送）

Commit Message 规范:
  AI 生成:  <type>(<scope>): <summary>\n\nTask: TASK-xxx\nIteration: vN\nStage: NN-阶段名
  人类:     分支名已包含 TASK-xxx，commit message 建议但不强制
  分支命名:  task/TASK-001-简短描述
"""

import asyncio
import logging
import re
import os
import tempfile
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

WORKSPACE = Path(settings.PROJECTS_DIR).resolve()

COMMIT_TYPES = ("feat", "fix", "docs", "refactor", "test", "deploy", "chore", "style", "perf")


def _repo_dir(project_id: str) -> Path:
    return WORKSPACE / project_id


async def _run(cmd: str, cwd: Path | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace").strip()
    return proc.returncode, output


async def _run_exec(args: list[str], cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace").strip()
    return proc.returncode, output


def _build_repo_with_token(repo: str, token: str, token_username: str = "oauth2") -> str:
    if not token or not repo.startswith(("http://", "https://")):
        return repo
    safe_token = token.replace("@", "%40")
    if repo.startswith("https://"):
        return repo.replace("https://", f"https://{token_username}:{safe_token}@", 1)
    return repo.replace("http://", f"http://{token_username}:{safe_token}@", 1)


async def verify_remote_repo(
    git_repo: str,
    *,
    ssh_private_key: str | None = None,
    token: str | None = None,
    token_username: str = "oauth2",
) -> dict:
    """
    校验远程仓库是否可访问。
    - 空仓库也会返回成功（ls-remote 退出码为 0）
    - 仅做连通与权限验证，不要求已有分支
    """
    repo = (git_repo or "").strip()
    if not repo:
        return {"ok": False, "error": "git_repo 为空"}
    target_repo = _build_repo_with_token(repo, (token or "").strip(), token_username)
    if ssh_private_key:
        with tempfile.TemporaryDirectory(prefix="verify-git-key-") as td:
            key_path = Path(td) / "id_ed25519"
            key_path.write_text(ssh_private_key, encoding="utf-8")
            os.chmod(key_path, 0o600)
            env = dict(os.environ)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -o StrictHostKeyChecking=accept-new "
                f"-o IdentitiesOnly=yes "
                f"-o PreferredAuthentications=publickey "
                f"-o PasswordAuthentication=no "
                f"-o BatchMode=yes "
                f"-i {key_path}"
            )
            code, out = await _run_exec(["git", "ls-remote", target_repo], env=env)
    else:
        code, out = await _run_exec(["git", "ls-remote", target_repo])
    if code != 0:
        return {"ok": False, "error": out}
    return {"ok": True, "output": out}


async def clone(project_id: str, git_repo: str) -> dict:
    """Clone 项目仓库到本地工作目录，完成后自动安装 commit-msg hook"""
    repo_path = _repo_dir(project_id)
    if repo_path.exists() and (repo_path / ".git").exists():
        code, out = await _run("git pull --ff-only", cwd=repo_path)
        return {"ok": code == 0, "action": "pull", "output": out}

    # 目录存在但无 .git → 先清空再 clone
    import shutil
    if repo_path.exists() and not (repo_path / ".git").exists():
        shutil.rmtree(repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)
    code, out = await _run(f"git clone {git_repo} .", cwd=repo_path)
    if code == 0:
        await init_commit_msg_hook(project_id)
    return {"ok": code == 0, "action": "clone", "output": out}


async def ensure_repo(project_id: str, git_repo: str) -> Path:
    """确保本地有仓库，返回路径"""
    repo_path = _repo_dir(project_id)
    if not (repo_path / ".git").exists():
        result = await clone(project_id, git_repo)
        if not result["ok"]:
            raise RuntimeError(f"Git clone failed: {result['output']}")
    return repo_path


async def pull(project_id: str) -> dict:
    repo_path = _repo_dir(project_id)
    if not (repo_path / ".git").exists():
        return {"ok": False, "error": "仓库不存在，请先 clone"}
    code, out = await _run("git pull --ff-only", cwd=repo_path)
    return {"ok": code == 0, "output": out}


async def commit_and_push(
    project_id: str,
    file_path: str | list[str],
    content: str | None = None,
    message: str = "",
    branch: str = "main",
) -> dict:
    """
    写入文件并 commit + push。

    file_path: 单个路径（配合 content 写入）或路径列表（仅 git add）
    content: 如果提供，写入 file_path（此时 file_path 必须是 str）
    message: 完整的 commit message（建议用 build_commit_message 构建）
    """
    repo_path = _repo_dir(project_id)
    if not (repo_path / ".git").exists():
        return {"ok": False, "error": "仓库不存在"}

    code, out = await _run(f"git checkout {branch}", cwd=repo_path)
    if code != 0:
        code, out = await _run(f"git checkout -b {branch}", cwd=repo_path)
        if code != 0:
            return {"ok": False, "error": f"切换分支失败: {out}"}

    if content is not None and isinstance(file_path, str):
        full_path = repo_path / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    paths = [file_path] if isinstance(file_path, str) else file_path
    for p in paths:
        code, out = await _run(f"git add {p}", cwd=repo_path)
        if code != 0:
            return {"ok": False, "error": f"git add {p} 失败: {out}"}

    code, out = await _run("git diff --cached --quiet", cwd=repo_path)
    if code == 0:
        return {"ok": True, "action": "no_change", "message": "文件内容无变化"}

    msg_file = repo_path / ".git" / "COMMIT_MSG_TMP"
    msg_file.write_text(message or "chore: update", encoding="utf-8")
    code, out = await _run(f'git commit -F "{msg_file}"', cwd=repo_path)
    msg_file.unlink(missing_ok=True)
    if code != 0:
        return {"ok": False, "error": f"git commit 失败: {out}"}

    commit_hash = ""
    m = re.search(r"\[[\w/]+ ([a-f0-9]+)\]", out)
    if m:
        commit_hash = m.group(1)

    code, push_out = await _run(f"git push origin {branch}", cwd=repo_path)
    if code != 0:
        return {"ok": False, "error": f"git push 失败: {push_out}", "commit": commit_hash}

    return {"ok": True, "action": "pushed", "commit": commit_hash, "branch": branch}


async def create_branch(project_id: str, branch: str, base: str = "main") -> dict:
    """从 base 创建新分支"""
    repo_path = _repo_dir(project_id)
    if not (repo_path / ".git").exists():
        return {"ok": False, "error": "仓库不存在"}

    await _run("git fetch --all --prune", cwd=repo_path)
    code, _ = await _run(f"git checkout {base}", cwd=repo_path)
    if code != 0:
        code, out = await _run(f"git checkout -b {base} origin/{base}", cwd=repo_path)
        if code != 0:
            return {"ok": False, "error": f"切换 base 分支失败: {out}"}
    await _run("git pull --ff-only", cwd=repo_path)
    code, out = await _run(f"git checkout -b {branch}", cwd=repo_path)
    if code != 0:
        code2, _ = await _run(f"git checkout {branch}", cwd=repo_path)
        if code2 == 0:
            return {"ok": True, "action": "exists", "branch": branch}
        return {"ok": False, "error": out}

    code, out = await _run(f"git push -u origin {branch}", cwd=repo_path)
    return {"ok": True, "action": "created", "branch": branch, "push": out if code != 0 else "ok"}


async def merge_branch(project_id: str, branch: str, target: str = "main") -> dict:
    """将分支合并到目标分支"""
    repo_path = _repo_dir(project_id)
    if not (repo_path / ".git").exists():
        return {"ok": False, "error": "仓库不存在"}

    await _run(f"git checkout {target}", cwd=repo_path)
    await _run("git pull --ff-only", cwd=repo_path)
    code, out = await _run(f"git merge --no-ff {branch} -m \"Merge {branch} into {target}\"", cwd=repo_path)
    if code != 0:
        await _run("git merge --abort", cwd=repo_path)
        return {"ok": False, "error": f"合并冲突: {out}"}

    code, push_out = await _run(f"git push origin {target}", cwd=repo_path)
    if code != 0:
        return {"ok": False, "error": f"push 失败: {push_out}"}

    return {"ok": True, "action": "merged", "branch": branch, "target": target}


async def get_branch_commits(project_id: str, branch: str, limit: int = 20) -> list[dict]:
    """获取分支上的 commit 列表"""
    repo_path = _repo_dir(project_id)
    if not (repo_path / ".git").exists():
        return []

    code, out = await _run(
        f"git log {branch} --format='%H|%h|%s|%an|%aI' -n {limit}",
        cwd=repo_path,
    )
    if code != 0:
        return []

    commits = []
    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) >= 5:
            commits.append({
                "hash": parts[0], "short": parts[1],
                "message": parts[2], "author": parts[3], "date": parts[4],
            })
    return commits


def build_web_url(git_web_url: str, file_path: str, branch: str = "main") -> str:
    """拼接文件的 Web 浏览链接。git_web_url 以 / 结尾，直接拼 branchName/filePath"""
    if not git_web_url:
        return ""
    base = git_web_url if git_web_url.endswith("/") else git_web_url + "/"
    return f"{base}{branch}/{file_path}"


STAGE_DOC_NAMES = [
    "00-业务方案", "01-需求规范", "02-产品原型", "03-技术方案",
    "04-任务分解", "05-编码开发", "06-测试验证", "07-部署交付",
]


def doc_file_path(stage: int, title: str, iteration_seq: str | int = "v1") -> str:
    """生成文档在仓库中的存储路径"""
    prefix = STAGE_DOC_NAMES[stage] if stage < len(STAGE_DOC_NAMES) else f"stage-{stage}"
    safe_title = re.sub(r'[^\w\u4e00-\u9fff-]', '_', title)[:50]
    return f"docs/iter-{iteration_seq}/{prefix}-{safe_title}.md"


# ── Commit Message 规范化 ──

def build_commit_message(
    commit_type: str,
    summary: str,
    *,
    scope: str = "",
    task_ref: str = "",
    iteration: str = "",
    stage: int | None = None,
    author: str = "ai",
) -> str:
    """
    构建规范化的 commit message。

    AI commit 示例:
      docs(stage-01): 需求规范文档 v2

      Task: TASK-003
      Iteration: v1
      Stage: 01-需求规范
      Author: ai/architect
    """
    if commit_type not in COMMIT_TYPES:
        commit_type = "chore"

    scope_part = f"({scope})" if scope else ""
    first_line = f"{commit_type}{scope_part}: {summary}"

    trailer_lines = []
    if task_ref:
        trailer_lines.append(f"Task: {task_ref}")
    if iteration:
        trailer_lines.append(f"Iteration: {iteration}")
    if stage is not None and stage < len(STAGE_DOC_NAMES):
        trailer_lines.append(f"Stage: {STAGE_DOC_NAMES[stage]}")
    if author:
        trailer_lines.append(f"Author: {author}")

    if trailer_lines:
        return first_line + "\n\n" + "\n".join(trailer_lines)
    return first_line


def task_branch_name(task_ref: str, title: str = "") -> str:
    """
    生成任务分支名: task/TASK-001-简短描述

    分支名本身包含任务号，即使 commit message 不规范也能追溯。
    """
    safe = re.sub(r'[^\w\u4e00-\u9fff-]', '-', title)[:30].strip("-")
    if safe:
        return f"task/{task_ref}-{safe}"
    return f"task/{task_ref}"


def parse_task_ref_from_branch(branch: str) -> str | None:
    """从分支名中提取任务号"""
    m = re.search(r"task/(TASK-\d+)", branch)
    return m.group(1) if m else None


async def init_commit_msg_hook(project_id: str) -> dict:
    """
    在仓库中安装 commit-msg hook，提示人类 commit 时包含 TASK-xxx。
    仅 warning 不阻断。
    """
    repo_path = _repo_dir(project_id)
    hook_dir = repo_path / ".git" / "hooks"
    if not hook_dir.exists():
        return {"ok": False, "error": "仓库不存在"}

    hook_path = hook_dir / "commit-msg"
    hook_content = '''#!/bin/sh
# AI Dev Team: 建议 commit message 包含任务号 TASK-xxx
MSG=$(cat "$1")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

# 如果在任务分支上，自动追加任务号
if echo "$BRANCH" | grep -qE "^task/TASK-[0-9]+"; then
    TASK_REF=$(echo "$BRANCH" | grep -oE "TASK-[0-9]+")
    if ! echo "$MSG" | grep -q "$TASK_REF"; then
        echo "" >> "$1"
        echo "Task: $TASK_REF" >> "$1"
    fi
fi

# 提示（不阻断）
if ! echo "$MSG" | grep -qE "TASK-[0-9]+"; then
    echo "[提示] commit message 中未包含任务号 (TASK-xxx)，建议关联任务以便追溯"
fi
'''
    hook_path.write_text(hook_content, encoding="utf-8")
    hook_path.chmod(0o755)
    return {"ok": True, "hook": str(hook_path)}
