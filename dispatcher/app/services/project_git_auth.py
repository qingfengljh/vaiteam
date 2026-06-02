from __future__ import annotations

import os
import shlex
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import Project
from app.services import git_repo


PROJECT_GIT_AUTH_KEY = "project_git_auth"


def _utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _read_cfg(project: Project) -> dict[str, Any]:
    return dict(project.config or {})


def _write_cfg(project: Project, cfg: dict[str, Any]) -> None:
    project.config = cfg


def get_git_auth(project: Project) -> dict[str, Any]:
    cfg = _read_cfg(project)
    auth = dict(cfg.get(PROJECT_GIT_AUTH_KEY) or {})
    mode = (auth.get("mode") or "ssh").strip() or "ssh"
    return {
        "mode": mode,
        "ssh_public_key": (auth.get("ssh_public_key") or "").strip(),
        "ssh_private_key": auth.get("ssh_private_key") or "",
        "token": auth.get("token") or "",
        "token_username": (auth.get("token_username") or "oauth2").strip() or "oauth2",
        "updated_at": auth.get("updated_at"),
        "updated_by": auth.get("updated_by") or "",
    }


def get_public_auth_info(project: Project) -> dict[str, Any]:
    auth = get_git_auth(project)
    return {
        "mode": auth["mode"],
        "has_ssh_key": bool(auth["ssh_private_key"]),
        "ssh_public_key": auth["ssh_public_key"],
        "has_token": bool(auth["token"]),
        "token_username": auth["token_username"],
        "updated_at": auth["updated_at"],
        "updated_by": auth["updated_by"],
    }


def get_private_key(project: Project) -> str:
    auth = get_git_auth(project)
    return auth["ssh_private_key"]


def set_token(project: Project, token: str, *, token_username: str = "oauth2", updated_by: str = "human") -> None:
    cfg = _read_cfg(project)
    auth = dict(cfg.get(PROJECT_GIT_AUTH_KEY) or {})
    auth["mode"] = "token"
    auth["token"] = (token or "").strip()
    auth["token_username"] = (token_username or "oauth2").strip() or "oauth2"
    auth["updated_at"] = _utc_ts()
    auth["updated_by"] = updated_by
    cfg[PROJECT_GIT_AUTH_KEY] = auth
    _write_cfg(project, cfg)


def generate_ssh_keypair(project: Project, *, comment: str = "", updated_by: str = "human") -> dict[str, Any]:
    cfg = _read_cfg(project)
    auth = dict(cfg.get(PROJECT_GIT_AUTH_KEY) or {})
    private = (auth.get("ssh_private_key") or "").strip()
    public = (auth.get("ssh_public_key") or "").strip()
    if private and public:
        auth["mode"] = auth.get("mode") or "ssh"
        cfg[PROJECT_GIT_AUTH_KEY] = auth
        _write_cfg(project, cfg)
        return {"public_key": public, "generated": False}

    import subprocess

    with tempfile.TemporaryDirectory(prefix="proj-git-key-") as td:
        key_path = Path(td) / "id_ed25519"
        key_comment = comment or f"openclaw-project-{project.id}"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", key_comment],
            check=True,
            capture_output=True,
            text=True,
        )
        private_key = key_path.read_text(encoding="utf-8")
        public_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()

    auth["mode"] = "ssh"
    auth["ssh_private_key"] = private_key
    auth["ssh_public_key"] = public_key
    auth.pop("token", None)
    auth["updated_at"] = _utc_ts()
    auth["updated_by"] = updated_by
    cfg[PROJECT_GIT_AUTH_KEY] = auth
    _write_cfg(project, cfg)
    return {"public_key": public_key, "generated": True}


def regenerate_ssh_keypair(project: Project, *, comment: str = "", updated_by: str = "human") -> dict[str, Any]:
    cfg = _read_cfg(project)
    auth = dict(cfg.get(PROJECT_GIT_AUTH_KEY) or {})

    import subprocess

    with tempfile.TemporaryDirectory(prefix="proj-git-key-") as td:
        key_path = Path(td) / "id_ed25519"
        key_comment = comment or f"openclaw-project-{project.id}"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", key_comment],
            check=True,
            capture_output=True,
            text=True,
        )
        private_key = key_path.read_text(encoding="utf-8")
        public_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()

    auth["mode"] = "ssh"
    auth["ssh_private_key"] = private_key
    auth["ssh_public_key"] = public_key
    auth.pop("token", None)
    auth["updated_at"] = _utc_ts()
    auth["updated_by"] = updated_by
    cfg[PROJECT_GIT_AUTH_KEY] = auth
    _write_cfg(project, cfg)
    return {"public_key": public_key, "generated": True}


def humanize_git_access_error(raw: str, *, auth_mode: str) -> str:
    """
    将 git ls-remote 等原始报错转写为可操作的说明（与 verify 链路一致：Dispatcher 用项目密钥访问）。
    """
    e = (raw or "").strip()
    low = e.lower()
    mode = (auth_mode or "ssh").strip().lower()

    if mode == "token" and (
        "authentication failed" in low
        or "could not read password" in low
        or "access denied" in low
        or "401" in e
        or "403" in e
    ):
        return (
            "HTTPS / 令牌认证失败：请在项目概览检查 Personal Access Token（或等价令牌）是否过期、"
            "是否具备该仓库的读取权限，并用「测试 Git 连接」复核。\n"
            f"----\n原始信息：{e}"
        )

    if "permission denied" in low or "publickey" in low:
        return (
            "Git 服务端拒绝了 SSH 公钥认证。编排平台从 Dispatcher 使用「项目专属 SSH 私钥」访问仓库，"
            "需先在 Git 侧完成免密：在项目概览复制 **SSH 公钥**，添加到仓库所在平台（如 GitLab「部署密钥 Deploy Key」"
            "或账号「SSH Keys」），并授予只读以上权限；确认地址为 `git@主机:组/仓库.git` 且账号有权访问后，"
            "再在概览页执行「测试 Git 连接」验证。\n"
            "补充：若 GitLab 添加 Deploy Key 时提示「Fingerprint sha256 已被使用」，表示这把公钥在本实例里已绑在**别的仓库**上，"
            "**不会**自动对当前仓库生效，Git 仍会拒绝。请在本项目概览「重新生成」密钥对并把**新**公钥加到**当前仓库**，"
            "或删掉旧仓库上的该 Deploy Key 后再添加，或改用有权限账号的「用户 SSH 密钥」。\n"
            f"----\n原始信息：{e}"
        )

    if "host key verification failed" in low:
        return (
            "SSH 主机指纹校验失败（常见于首次连接或 Git 主机密钥轮换）。请确认仓库主机名正确；"
            "若环境要求固定 known_hosts，请联系运维处理后再用「测试 Git 连接」验证。\n"
            f"----\n原始信息：{e}"
        )

    if "could not resolve host" in low or "name or service not known" in low:
        return f"无法解析 Git 主机名，请核对仓库 URL 与 DNS/网络。\n----\n原始信息：{e}"

    if "connection timed out" in low or "no route to host" in low or "network is unreachable" in low:
        return f"连接 Git 服务超时或网络不可达，请检查防火墙、路由与 Git 服务是否可达。\n----\n原始信息：{e}"

    if "repository not found" in low or "does not exist" in low:
        return f"仓库不存在，或当前凭据对该路径无访问权限；请核对组/仓库名与权限。\n----\n原始信息：{e}"

    return (
        "无法访问远程 Git。请先在项目概览配置访问方式：SSH 请在 Git 端登记项目公钥实现免密拉取，"
        "HTTPS 请配置有效令牌，并用「测试 Git 连接」排查。\n"
        f"----\n原始信息：{e}"
    )


def redact_project_config(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(config or {})
    auth = dict(cfg.get(PROJECT_GIT_AUTH_KEY) or {})
    if auth:
        if auth.get("ssh_private_key"):
            auth["ssh_private_key"] = "***"
        if auth.get("token"):
            auth["token"] = "***"
        cfg[PROJECT_GIT_AUTH_KEY] = auth
    return cfg


async def verify_project_repo_access(project: Project, git_repo_url: str | None = None) -> dict[str, Any]:
    repo = (git_repo_url or project.git_repo or "").strip()
    if not repo:
        return {
            "ok": False,
            "error": "git_repo 为空",
            "hint": "请先在项目概览填写远程 Git 仓库地址，再使用「测试 Git 连接」或开始任务分解。",
        }
    auth = get_git_auth(project)
    if auth["mode"] == "token" and auth["token"]:
        result = await git_repo.verify_remote_repo(
            repo,
            token=auth["token"],
            token_username=auth["token_username"],
        )
    elif not auth["ssh_private_key"]:
        return {
            "ok": False,
            "error": "项目未生成 Git SSH 密钥对，请先生成并配置公钥",
            "hint": (
                "请在项目概览的 Git 认证中生成 SSH 密钥对，将公钥添加到 Git 服务端（部署密钥或用户 SSH Keys），"
                "完成免密拉取配置后再试「测试 Git 连接」。"
            ),
        }
    else:
        result = await git_repo.verify_remote_repo(repo, ssh_private_key=auth["ssh_private_key"])

    if not result.get("ok"):
        raw = (result.get("error") or "").strip()
        result = dict(result)
        result["hint"] = humanize_git_access_error(raw, auth_mode=auth["mode"])
    return result


def write_private_key_to_file(private_key: str, target_path: str) -> None:
    Path(target_path).write_text(private_key, encoding="utf-8")
    os.chmod(target_path, 0o600)


def build_agent_git_ls_remote_command(repo_url: str, private_key: str) -> str:
    marker = f"OPENCLAW_GIT_KEY_{uuid.uuid4().hex}"
    repo_quoted = shlex.quote((repo_url or "").strip())
    key_text = private_key or ""
    return (
        'set -e; '
        'KEY_FILE="/tmp/.openclaw_project_git_key_$$"; '
        'trap \'if command -v shred >/dev/null 2>&1; then shred -u "$KEY_FILE"; else rm -f "$KEY_FILE"; fi\' EXIT; '
        f'cat > "$KEY_FILE" <<\'{marker}\'\n'
        f'{key_text}\n'
        f'{marker}\n'
        'chmod 600 "$KEY_FILE"; '
        'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o BatchMode=yes -i $KEY_FILE" '
        f'git ls-remote {repo_quoted}'
    )
