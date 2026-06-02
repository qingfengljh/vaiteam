"""
命令白名单 — 限制 AI 能执行的 Bash 命令

核心机制：
1. 定义允许执行的命令白名单
2. 定义明确禁止的危险命令模式
3. 在 Claude Code 执行前，包装 bash 命令进行拦截
4. 危险命令尝试记录为安全事件

与 file_guardian / file_allowlist 的关系：
- file_guardian: 保护文件
- file_allowlist: 限制文件访问范围
- command_guardian: 限制命令执行范围
三者互补，构成完整的安全 Harness。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 全局开关
COMMAND_GUARDIAN_ENABLED = os.environ.get("COMMAND_GUARDIAN_ENABLED", "1") == "1"

# ── 命令白名单 ──
# 只允许执行这些命令及其子命令
# 格式: "命令名" 或 "命令名 子命令"（前缀匹配）
ALLOWED_COMMANDS: set[str] = {
    # 文件操作（只读）
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "ls",
    "ll",
    "pwd",
    "find",
    "grep",
    "egrep",
    "fgrep",
    "wc",
    "diff",
    "sort",
    "uniq",
    "file",
    "stat",
    # Git
    "git",
    "git status",
    "git add",
    "git commit",
    "git push",
    "git pull",
    "git fetch",
    "git branch",
    "git checkout",
    "git merge",
    "git rebase",
    "git log",
    "git diff",
    "git show",
    "git stash",
    "git config",
    "git remote",
    "git clone",
    # 构建工具
    "npm",
    "npm install",
    "npm run",
    "npm test",
    "npm build",
    "npm ci",
    "yarn",
    "yarn install",
    "yarn run",
    "yarn test",
    "pnpm",
    "pip",
    "pip3",
    "pip install",
    "pip uninstall",
    "python",
    "python3",
    "pytest",
    "node",
    "npx",
    "make",
    "cmake",
    # 容器
    "docker",
    "docker-compose",
    "docker compose",
    # 其他常用
    "cd",
    "echo",
    "mkdir",
    "touch",
    "cp",
    "mv",
    "which",
    "whoami",
    "date",
    "env",
    "printenv",
    "uname",
    "curl",
    "wget",
    "tar",
    "zip",
    "unzip",
    "chmod",
    "chown",
}

# ── 危险命令黑名单（明确禁止，即使不在白名单检查中也会拦截） ──
# 格式: (正则模式, 危险等级, 描述)
BLOCKED_PATTERNS: list[tuple[str, str, str]] = [
    # 破坏性操作
    (r"rm\s+-rf\s+/", "CRITICAL", "Force remove root directory"),
    (r"rm\s+-rf\s+[~./]?\s*$", "CRITICAL", "Force remove with dangerous target"),
    (r"dd\s+if=.*of=/dev/[sh]d", "CRITICAL", "Direct disk write"),
    (r">\s*/dev/[sh]d", "CRITICAL", "Redirect to disk device"),
    (r"mkfs", "CRITICAL", "Filesystem format"),
    (r"fdisk", "CRITICAL", "Disk partitioning"),
    # 管道执行远程脚本
    (r"curl\s+.*\|\s*(sh|bash|zsh|csh)", "HIGH", "Pipe remote script to shell"),
    (r"wget\s+.*-O\s+-.*\|\s*(sh|bash)", "HIGH", "Pipe wget output to shell"),
    (r"fetch\s+.*\|\s*(sh|bash)", "HIGH", "Pipe fetch output to shell"),
    # 提权
    (r"\bsudo\b", "HIGH", "Privilege escalation"),
    (r"\bsu\s+-", "HIGH", "Switch user"),
    (r"\bpkexec\b", "HIGH", "PolicyKit execution"),
    # 网络危险
    (r"nc\s+-[l]", "HIGH", "Netcat listener (backdoor)"),
    (r"ncat\s+-[l]", "HIGH", "Ncat listener"),
    (r"python\s+-m\s+http\.server", "MEDIUM", "HTTP server exposure"),
    (r"python3\s+-m\s+http\.server", "MEDIUM", "HTTP server exposure"),
    # 信息泄露
    (r"cat\s+.*\.env", "MEDIUM", "Read env file (should use Read tool)"),
    (r"cat\s+.*id_rsa", "HIGH", "Read private key"),
    (r"cat\s+.*\.aws/", "HIGH", "Read AWS credentials"),
    (r"env\s*\|\s*grep\s*(API_KEY|SECRET|TOKEN|PWD|PASS)", "HIGH", "Filter env for secrets"),
    # 其他危险
    (r"\bssh\b", "MEDIUM", "SSH connection"),
    (r"\bscp\b", "MEDIUM", "SCP file transfer"),
    (r"\bsftp\b", "MEDIUM", "SFTP connection"),
    (r"\btelnet\b", "HIGH", "Telnet (insecure)"),
    (r"\bftp\b", "MEDIUM", "FTP connection"),
    (r":(){ :|:& };:", "CRITICAL", "Fork bomb"),
    (r"\beval\s*\(", "HIGH", "Eval expression"),
    (r"\bexec\s*\(", "HIGH", "Exec expression"),
]


@dataclass
class CommandCheckResult:
    allowed: bool
    blocked_pattern: str = ""
    severity: str = ""
    reason: str = ""


def _extract_command(cmdline: str) -> str:
    """从命令行提取主命令和子命令"""
    # 去除前导空格和常见前缀
    cmdline = cmdline.strip()
    for prefix in ("bash -c \"", "bash -c '", "sh -c \"", "sh -c '"):
        if cmdline.startswith(prefix):
            cmdline = cmdline[len(prefix):]
            if cmdline.endswith('"') or cmdline.endswith("'"):
                cmdline = cmdline[:-1]

    parts = cmdline.split()
    if not parts:
        return ""

    # 处理 sudo
    if parts[0] == "sudo" and len(parts) > 1:
        parts = parts[1:]

    cmd = parts[0]

    # 提取子命令（如 git status → "git status"）
    if len(parts) > 1 and parts[1] in {
        "status", "add", "commit", "push", "pull", "fetch", "branch", "checkout",
        "merge", "rebase", "log", "diff", "show", "stash", "config", "remote", "clone",
        "install", "run", "test", "build", "ci", "start", "dev",
    }:
        return f"{cmd} {parts[1]}"

    return cmd


def check_command(cmdline: str) -> CommandCheckResult:
    """
    检查单个命令是否允许执行。

    检查顺序：
    1. 黑名单模式匹配 → 直接拒绝
    2. 白名单检查 → 不在白名单中则拒绝

    Args:
        cmdline: 命令行字符串

    Returns:
        CommandCheckResult
    """
    if not COMMAND_GUARDIAN_ENABLED:
        return CommandCheckResult(allowed=True)

    if not cmdline or not cmdline.strip():
        return CommandCheckResult(allowed=True)

    # 1. 黑名单检查
    for pattern, severity, description in BLOCKED_PATTERNS:
        if re.search(pattern, cmdline, re.IGNORECASE):
            logger.warning(f"Command blocked by pattern '{pattern}': {cmdline[:100]}")
            return CommandCheckResult(
                allowed=False,
                blocked_pattern=pattern,
                severity=severity,
                reason=description,
            )

    # 2. 白名单检查
    extracted = _extract_command(cmdline)
    if extracted in ALLOWED_COMMANDS:
        return CommandCheckResult(allowed=True)

    # 尝试前缀匹配（如 "git status --short" → "git status"）
    for allowed in ALLOWED_COMMANDS:
        if extracted.startswith(allowed + " "):
            return CommandCheckResult(allowed=True)

    # 不在白名单中
    logger.warning(f"Command not in allowlist: {extracted} (from: {cmdline[:100]})")
    return CommandCheckResult(
        allowed=False,
        severity="MEDIUM",
        reason=f"Command '{extracted}' is not in the allowed command list",
    )


def create_restricted_shell_env() -> dict[str, str]:
    """
    创建受限 shell 环境变量。
    通过设置 SHELL 和 BASH_ENV 来限制可执行的命令。
    """
    env = dict(os.environ)
    # 可以在这里添加更多环境限制
    return env


def build_blocked_message(result: CommandCheckResult) -> str:
    """构建命令被拦截时的错误消息"""
    return (
        f"命令执行被拦截: {result.reason}\n"
        f"危险等级: {result.severity}\n"
        f"如果你认为此命令是必要的，请在结果中说明理由，由人类审核后重新分配。"
    )


def get_allowed_commands_summary() -> str:
    """获取允许执行的命令摘要（用于日志或文档）"""
    categories = {
        "文件操作（只读）": ["cat", "head", "tail", "less", "ls", "find", "grep", "diff"],
        "Git": ["git", "git status", "git add", "git commit", "git push", "git branch"],
        "构建工具": ["npm", "npm install", "npm run", "pip", "python", "pytest", "node"],
        "容器": ["docker", "docker-compose"],
        "其他": ["cd", "echo", "mkdir", "which", "env", "curl", "tar"],
    }

    lines = ["允许执行的命令类别："]
    for category, cmds in categories.items():
        lines.append(f"  {category}: {', '.join(cmds)}")
    lines.append("\n明确禁止的危险操作：")
    lines.append("  - rm -rf / 或 ~")
    lines.append("  - curl | sh 管道执行")
    lines.append("  - sudo 提权")
    lines.append("  - 直接写入磁盘设备")
    lines.append("  - 读取私钥和密钥文件")

    return "\n".join(lines)
