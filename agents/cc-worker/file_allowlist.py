"""
文件白名单机制 — 限制 AI 只能修改指定范围内的文件

核心机制：
1. 任务包中携带 allowed_paths（白名单）
2. CC Worker 执行前，对非白名单文件设置只读权限
3. 白名单中的文件保持可写
4. 执行完成后恢复原始权限（可选）

与 file_guardian.py 的关系：
- file_guardian: 事后检测 + 回滚（保护特定配置文件）
- file_allowlist: 事前限制（控制 AI 可修改的文件范围）
两者互补，共同构成文件访问控制的双层防御。
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# 全局开关
ALLOWLIST_ENABLED = os.environ.get("FILE_ALLOWLIST_ENABLED", "1") == "1"

# 某些路径即使在白名单外，也不应设为只读（如 .git 目录）
# 但这些路径仍然受到 file_guardian 的保护
EXCLUDE_FROM_READONLY = {
    ".git",
    ".git/objects",
    ".git/refs",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".venv",
    "venv",
}


def _should_be_readonly(relative_path: str, allowed_paths: list[str]) -> bool:
    """
    判断一个文件是否应该设为只读。

    规则：
    1. 如果文件匹配 allowed_paths 中的任一模式 → 可写
    2. 如果文件在 EXCLUDE_FROM_READONLY 中 → 保持原权限（不处理）
    3. 其他所有文件 → 只读
    """
    # 规范化路径
    rel = relative_path.replace("\\", "/").lstrip("/")

    # 检查是否在排除列表中
    parts = rel.split("/")
    for part in parts:
        if part in EXCLUDE_FROM_READONLY:
            return False  # 不处理，保持原权限

    # 检查是否在白名单中
    for allowed in allowed_paths:
        allowed_norm = allowed.replace("\\", "/").lstrip("/")
        # 精确匹配
        if rel == allowed_norm:
            return False  # 可写
        # 目录前缀匹配（白名单是目录，文件在该目录下）
        if rel.startswith(allowed_norm + "/"):
            return False  # 可写
        # glob 匹配（简单的 * 通配）
        if "*" in allowed_norm:
            import fnmatch
            if fnmatch.fnmatch(rel, allowed_norm):
                return False  # 可写

    return True  # 不在白名单中 → 只读


def apply_allowlist(workspace: str, allowed_paths: list[str]) -> dict:
    """
    对工作目录应用白名单权限控制。

    Args:
        workspace: 工作目录路径
        allowed_paths: 白名单路径列表（相对于工作目录）

    Returns:
        应用报告 {"readonly": int, "writable": int, "skipped": int}
    """
    if not ALLOWLIST_ENABLED:
        logger.info("File allowlist is disabled")
        return {"readonly": 0, "writable": 0, "skipped": 0, "enabled": False}

    workspace_path = Path(workspace).resolve()
    if not workspace_path.exists():
        logger.warning(f"Workspace does not exist: {workspace}")
        return {"readonly": 0, "writable": 0, "skipped": 0, "enabled": True, "error": "workspace not found"}

    if not allowed_paths:
        logger.warning("No allowed_paths provided, all files will be readonly")

    stats = {"readonly": 0, "writable": 0, "skipped": 0, "enabled": True}

    for filepath in workspace_path.rglob("*"):
        if not filepath.is_file():
            continue

        try:
            relative = filepath.relative_to(workspace_path).as_posix()
        except ValueError:
            continue

        if _should_be_readonly(relative, allowed_paths):
            # 设为只读：去掉写权限
            try:
                current_mode = filepath.stat().st_mode
                readonly_mode = current_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                filepath.chmod(readonly_mode)
                stats["readonly"] += 1
            except (OSError, PermissionError) as e:
                logger.debug(f"Cannot set readonly for {relative}: {e}")
                stats["skipped"] += 1
        else:
            # 确保可写
            try:
                current_mode = filepath.stat().st_mode
                writable_mode = current_mode | stat.S_IWUSR
                filepath.chmod(writable_mode)
                stats["writable"] += 1
            except (OSError, PermissionError) as e:
                logger.debug(f"Cannot set writable for {relative}: {e}")
                stats["skipped"] += 1

    logger.info(
        f"File allowlist applied: workspace={workspace}, "
        f"readonly={stats['readonly']}, writable={stats['writable']}, skipped={stats['skipped']}"
    )
    return stats


def restore_permissions(workspace: str) -> dict:
    """
    恢复工作目录中所有文件的写权限。
    在任务执行完成后调用，确保后续操作不受限制。

    Args:
        workspace: 工作目录路径

    Returns:
        恢复报告 {"restored": int, "failed": int}
    """
    workspace_path = Path(workspace).resolve()
    if not workspace_path.exists():
        return {"restored": 0, "failed": 0}

    stats = {"restored": 0, "failed": 0}

    for filepath in workspace_path.rglob("*"):
        if not filepath.is_file():
            continue

        try:
            current_mode = filepath.stat().st_mode
            # 恢复用户写权限
            writable_mode = current_mode | stat.S_IWUSR
            if current_mode != writable_mode:
                filepath.chmod(writable_mode)
                stats["restored"] += 1
        except (OSError, PermissionError) as e:
            logger.debug(f"Cannot restore permission for {filepath}: {e}")
            stats["failed"] += 1

    logger.info(
        f"File permissions restored: restored={stats['restored']}, failed={stats['failed']}"
    )
    return stats


def validate_changes(workspace: str, allowed_paths: list[str]) -> list[dict]:
    """
    验证 git diff 中的变更是否都在白名单范围内。
    返回越界变更列表。

    Args:
        workspace: 工作目录路径
        allowed_paths: 白名单路径列表

    Returns:
        越界变更列表，每项包含 {"path": str, "change_type": str}
    """
    import subprocess

    violations = []

    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(f"git diff failed: {result.stderr}")
            return violations

        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            change_type = parts[0]  # A/M/D/R...
            filepath = parts[1]

            if _should_be_readonly(filepath, allowed_paths):
                violations.append({
                    "path": filepath,
                    "change_type": change_type,
                    "reason": "not_in_allowlist",
                })

    except Exception as e:
        logger.warning(f"Validate changes failed: {e}")

    return violations
