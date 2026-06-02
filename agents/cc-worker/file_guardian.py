"""
文件指纹保护 — 防止 AI 擅自修改关键配置文件

核心机制：
1. 任务执行前，对关键配置文件计算哈希快照
2. 任务执行后，对比哈希检测是否被修改
3. 若被修改，自动回滚到原始内容
4. 违规记录上报，任务标记为 blocked

保护范围：配置文件、环境变量、依赖清单、Git 配置等
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认受保护的文件模式（正则）
# 这些文件无论是否在任务范围内，默认都不允许 AI 修改
DEFAULT_PROTECTED_PATTERNS: list[str] = [
    r"\.env$",
    r"\.env\..*$",
    r"config\.ya?ml$",
    r"config\.json$",
    r"docker-compose\.ya?ml$",
    r"Dockerfile$",
    r"\.dockerignore$",
    r"package\.json$",
    r"package-lock\.json$",
    r"yarn\.lock$",
    r"requirements\.txt$",
    r"pyproject\.toml$",
    r"setup\.py$",
    r"setup\.cfg$",
    r"Pipfile$",
    r"Pipfile\.lock$",
    r"\.gitignore$",
    r"\.gitattributes$",
    r"\.editorconfig$",
    r"Makefile$",
    r"makefile$",
    r"CMakeLists\.txt$",
    r"tsconfig\.json$",
    r"vite\.config\..*$",
    r"webpack\.config\..*$",
    r"babel\.config\..*$",
    r"eslint\.config\..*$",
    r"prettier\.config\..*$",
    r"\.prettierrc.*$",
    r"\.eslintrc.*$",
    r"nginx\.conf$",
    r"\.htaccess$",
    r"kubernetes/.*\.ya?ml$",
    r"k8s/.*\.ya?ml$",
    r"\.github/.*\.ya?ml$",
    r"\.gitlab-ci\.ya?ml$",
    r"jenkins.*\.ya?ml$",
    r"terraform/.*\.tf$",
    r"ansible/.*\.ya?ml$",
]

# 即使任务显式包含这些路径，也需要额外确认才能修改
CRITICAL_CONFIG_PATTERNS: list[str] = [
    r"\.env$",
    r"\.env\..*$",
    r"docker-compose\.ya?ml$",
    r"Dockerfile$",
    r"requirements\.txt$",
    r"package\.json$",
]


@dataclass
class FileSnapshot:
    """文件快照：记录文件路径、哈希和内容（小文件）"""
    path: str
    relative_path: str
    sha256: str
    size: int
    content: bytes = field(repr=False)  # 用于回滚，大文件不存储

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass
class FileViolation:
    """文件违规记录"""
    path: str
    relative_path: str
    violation_type: str  # "modified" | "deleted" | "added_unexpected"
    original_hash: str = ""
    new_hash: str = ""
    original_size: int = 0
    new_size: int = 0
    diff_summary: str = ""
    severity: str = "error"  # "error" | "warning"
    restored: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "violation_type": self.violation_type,
            "original_hash": self.original_hash,
            "new_hash": self.new_hash,
            "severity": self.severity,
            "restored": self.restored,
        }


@dataclass
class GuardianReport:
    """保护报告"""
    scanned: int
    protected: int
    violations: list[FileViolation]
    restored: int
    passed: bool

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "protected": self.protected,
            "violations": [v.to_dict() for v in self.violations],
            "restored": self.restored,
            "passed": self.passed,
        }


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """编译正则模式列表"""
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Invalid pattern '{p}': {e}")
    return compiled


def _is_protected_file(relative_path: str, patterns: list[re.Pattern]) -> bool:
    """检查文件路径是否匹配保护模式"""
    path_normalized = relative_path.replace("\\", "/")
    for pattern in patterns:
        if pattern.search(path_normalized):
            return True
    return False


def _sha256_file(filepath: Path) -> str:
    """计算文件 SHA256 哈希"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_file_limited(filepath: Path, max_size: int = 1024 * 1024) -> bytes:
    """读取文件内容，大文件只读前 max_size 字节"""
    size = filepath.stat().st_size
    if size > max_size:
        with open(filepath, "rb") as f:
            return f.read(max_size)
    with open(filepath, "rb") as f:
        return f.read()


async def snapshot_protected_files(
    workspace: str,
    extra_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_file_size: int = 5 * 1024 * 1024,  # 5MB，超过不存内容
) -> dict[str, FileSnapshot]:
    """
    扫描工作目录，对所有受保护文件建立哈希快照。

    Args:
        workspace: 工作目录路径
        extra_patterns: 额外的保护模式（正则字符串列表）
        exclude_patterns: 排除的模式（正则字符串列表）
        max_file_size: 最大存储内容的文件大小

    Returns:
        dict[relative_path, FileSnapshot]
    """
    workspace_path = Path(workspace).resolve()
    if not workspace_path.exists():
        logger.warning(f"Workspace does not exist: {workspace}")
        return {}

    patterns = _compile_patterns(DEFAULT_PROTECTED_PATTERNS)
    if extra_patterns:
        patterns.extend(_compile_patterns(extra_patterns))

    exclude_compiled = _compile_patterns(exclude_patterns or [])

    snapshots: dict[str, FileSnapshot] = {}
    scanned = 0

    for filepath in workspace_path.rglob("*"):
        if not filepath.is_file():
            continue
        scanned += 1

        try:
            relative = filepath.relative_to(workspace_path).as_posix()
        except ValueError:
            continue

        # 检查排除模式
        if any(p.search(relative) for p in exclude_compiled):
            continue

        if not _is_protected_file(relative, patterns):
            continue

        try:
            sha = _sha256_file(filepath)
            size = filepath.stat().st_size
            content = b""
            if size <= max_file_size:
                content = filepath.read_bytes()

            snapshots[relative] = FileSnapshot(
                path=str(filepath),
                relative_path=relative,
                sha256=sha,
                size=size,
                content=content,
            )
        except (OSError, PermissionError) as e:
            logger.debug(f"Cannot snapshot {relative}: {e}")

    logger.info(
        f"File guardian snapshot: workspace={workspace}, "
        f"scanned={scanned}, protected={len(snapshots)}"
    )
    return snapshots


async def check_protected_files(
    workspace: str,
    snapshots: dict[str, FileSnapshot],
    auto_restore: bool = True,
) -> GuardianReport:
    """
    对比快照，检测受保护文件是否被修改/删除。

    Args:
        workspace: 工作目录路径
        snapshots: 执行前建立的快照
        auto_restore: 是否自动回滚被修改的文件

    Returns:
        GuardianReport
    """
    workspace_path = Path(workspace).resolve()
    violations: list[FileViolation] = []
    restored_count = 0

    # 检查原有保护文件是否被修改或删除
    for relative, snapshot in snapshots.items():
        current_path = workspace_path / relative

        if not current_path.exists():
            # 文件被删除
            violations.append(FileViolation(
                path=str(current_path),
                relative_path=relative,
                violation_type="deleted",
                original_hash=snapshot.sha256,
                original_size=snapshot.size,
                severity="error",
            ))
            if auto_restore and snapshot.content:
                try:
                    current_path.parent.mkdir(parents=True, exist_ok=True)
                    current_path.write_bytes(snapshot.content)
                    restored_count += 1
                    violations[-1].restored = True
                    logger.warning(f"Restored deleted file: {relative}")
                except OSError as e:
                    logger.error(f"Failed to restore {relative}: {e}")
            continue

        try:
            current_sha = _sha256_file(current_path)
        except OSError as e:
            logger.warning(f"Cannot hash {relative}: {e}")
            continue

        if current_sha != snapshot.sha256:
            # 文件被修改
            current_size = current_path.stat().st_size
            diff_summary = f"size: {snapshot.size} -> {current_size}"

            # 对文本文件尝试生成 diff 摘要
            try:
                if snapshot.content:
                    original_text = snapshot.content.decode("utf-8", errors="replace")
                    current_text = current_path.read_text(encoding="utf-8", errors="replace")
                    # 简单摘要：统计行数变化
                    orig_lines = original_text.count("\n")
                    curr_lines = current_text.count("\n")
                    diff_summary += f", lines: {orig_lines} -> {curr_lines}"
            except Exception:
                pass

            # 判断是否为关键配置文件（需要更高 severity）
            critical_patterns = _compile_patterns(CRITICAL_CONFIG_PATTERNS)
            is_critical = _is_protected_file(relative, critical_patterns)

            violations.append(FileViolation(
                path=str(current_path),
                relative_path=relative,
                violation_type="modified",
                original_hash=snapshot.sha256,
                new_hash=current_sha,
                original_size=snapshot.size,
                new_size=current_size,
                diff_summary=diff_summary,
                severity="error" if is_critical else "warning",
            ))

            if auto_restore and snapshot.content:
                try:
                    current_path.write_bytes(snapshot.content)
                    restored_count += 1
                    violations[-1].restored = True
                    logger.warning(
                        f"Restored modified file: {relative} ({diff_summary})"
                    )
                except OSError as e:
                    logger.error(f"Failed to restore {relative}: {e}")

    # 检查是否有新增的非预期保护文件（如 AI 新建了 .env.local）
    current_patterns = _compile_patterns(DEFAULT_PROTECTED_PATTERNS)
    for filepath in workspace_path.rglob("*"):
        if not filepath.is_file():
            continue
        try:
            relative = filepath.relative_to(workspace_path).as_posix()
        except ValueError:
            continue

        if relative in snapshots:
            continue  # 已知的保护文件，上面已检查

        if not _is_protected_file(relative, current_patterns):
            continue

        # 新增的保护文件 — 这也是违规
        try:
            current_sha = _sha256_file(filepath)
            current_size = filepath.stat().st_size
            violations.append(FileViolation(
                path=str(filepath),
                relative_path=relative,
                violation_type="added_unexpected",
                new_hash=current_sha,
                new_size=current_size,
                severity="warning",
            ))
            logger.warning(f"Unexpected protected file created: {relative}")
        except OSError:
            pass

    passed = len(violations) == 0

    if violations:
        error_count = sum(1 for v in violations if v.severity == "error")
        warn_count = len(violations) - error_count
        logger.warning(
            f"File guardian violations: errors={error_count}, warnings={warn_count}, "
            f"restored={restored_count}"
        )

    return GuardianReport(
        scanned=len(snapshots),
        protected=len(snapshots),
        violations=violations,
        restored=restored_count,
        passed=passed,
    )


def build_protection_summary(violations: list[FileViolation]) -> str:
    """将违规列表格式化为人类可读的摘要文本"""
    if not violations:
        return ""

    lines = ["## 文件保护违规检测", ""]

    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if errors:
        lines.append(f"### 严重违规（{len(errors)} 项）")
        for v in errors:
            status = "已回滚" if v.restored else "未回滚"
            lines.append(
                f"- `{v.relative_path}`: {v.violation_type} ({status})"
            )
            if v.diff_summary:
                lines.append(f"  变更: {v.diff_summary}")
        lines.append("")

    if warnings:
        lines.append(f"### 警告（{len(warnings)} 项）")
        for v in warnings:
            lines.append(f"- `{v.relative_path}`: {v.violation_type}")
        lines.append("")

    return "\n".join(lines)
