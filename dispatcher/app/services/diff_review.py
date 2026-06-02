"""
变更预审 — 在任务完成后审查 git diff，检测越界变更

核心机制：
1. 获取任务的 git diff（变更文件列表 + 变更内容）
2. 对比 allowed_paths / forbidden_paths 检测越界
3. 检测未授权的依赖文件修改（package.json, requirements.txt 等）
4. 生成审查报告，决定：通过 / warning / error

与 file_guardian / file_allowlist 的关系：
- file_guardian: 保护特定配置文件（事后回滚）
- file_allowlist: 限制可修改的文件范围（事前权限控制）
- diff_review: 审查变更内容（事后内容审查）
三者互补，构成完整的文件访问控制体系。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ReviewSeverity(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    ERROR = "error"


class ViolationType(str, Enum):
    OUT_OF_ALLOWLIST = "out_of_allowlist"      # 修改了白名单外的文件
    FORBIDDEN_PATH = "forbidden_path"          # 修改了明确禁止的文件
    DEPENDENCY_CHANGED = "dependency_changed"  # 修改了依赖清单
    CONFIG_CHANGED = "config_changed"          # 修改了配置文件
    LARGE_DIFF = "large_diff"                  # 变更过大（可能范围失控）
    UNEXPECTED_DELETE = "unexpected_delete"    # 意外删除文件
    BINARY_ADDED = "binary_added"              # 新增了二进制文件


# 需要特别关注的文件模式（修改时需要 warning 级别审查）
SENSITIVE_FILE_PATTERNS: list[str] = [
    r"\.env",
    r"config\.",
    r"docker-compose",
    r"Dockerfile",
    r"package\.json",
    r"requirements\.txt",
    r"pyproject\.toml",
    r"setup\.py",
    r"Makefile",
    r"\.github/",
    r"\.gitlab",
    r"jenkins",
    r"terraform/",
    r"ansible/",
]

# 依赖清单文件（修改时需要 warning）
DEPENDENCY_FILE_PATTERNS: list[str] = [
    r"package\.json$",
    r"package-lock\.json$",
    r"yarn\.lock$",
    r"requirements\.txt$",
    r"pyproject\.toml$",
    r"setup\.py$",
    r"Pipfile$",
    r"Pipfile\.lock$",
    r"poetry\.lock$",
    r"Cargo\.toml$",
    r"Cargo\.lock$",
    r"go\.mod$",
    r"go\.sum$",
    r"Gemfile$",
    r"Gemfile\.lock$",
]

# 默认的最大允许变更行数（超过则 warning）
DEFAULT_MAX_DIFF_LINES = 500


@dataclass
class DiffViolation:
    file_path: str
    violation_type: ViolationType
    severity: ReviewSeverity
    message: str = ""
    old_path: str = ""
    change_type: str = ""  # A/M/D/R...
    diff_lines: int = 0

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "violation_type": self.violation_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "change_type": self.change_type,
            "diff_lines": self.diff_lines,
        }


@dataclass
class DiffReviewReport:
    passed: bool
    total_files_changed: int
    total_lines_changed: int
    violations: list[DiffViolation] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == ReviewSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == ReviewSeverity.WARNING)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total_files_changed": self.total_files_changed,
            "total_lines_changed": self.total_lines_changed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [v.to_dict() for v in self.violations],
        }


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.warning(f"Invalid pattern '{p}': {e}")
    return compiled


def _match_any_pattern(path: str, patterns: list[re.Pattern]) -> bool:
    for pattern in patterns:
        if pattern.search(path):
            return True
    return False


def _is_in_allowlist(path: str, allowed_paths: list[str]) -> bool:
    """检查文件路径是否在白名单内"""
    if not allowed_paths:
        return True  # 没有白名单 = 全部允许

    path_norm = path.replace("\\", "/").lstrip("/")

    for allowed in allowed_paths:
        allowed_norm = allowed.replace("\\", "/").lstrip("/")
        if path_norm == allowed_norm:
            return True
        if path_norm.startswith(allowed_norm + "/"):
            return True
        if "*" in allowed_norm:
            import fnmatch
            if fnmatch.fnmatch(path_norm, allowed_norm):
                return True

    return False


async def review_diff_from_git(
    project_id: str,
    branch: str,
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
) -> DiffReviewReport:
    """
    审查指定分支的 git diff，生成变更预审报告。

    Args:
        project_id: 项目 ID（用于定位 git 仓库）
        branch: 要审查的分支
        allowed_paths: 白名单路径列表
        forbidden_paths: 黑名单路径列表
        max_diff_lines: 最大允许变更行数

    Returns:
        DiffReviewReport
    """
    from app.services import git_repo

    violations: list[DiffViolation] = []
    total_files = 0
    total_lines = 0

    try:
        # 获取 diff 统计
        diff_stat = await git_repo.get_diff_stat(project_id, branch)
        changed_files = diff_stat.get("files", [])
        total_files = len(changed_files)
        total_lines = diff_stat.get("total_lines", 0)

        sensitive_patterns = _compile_patterns(SENSITIVE_FILE_PATTERNS)
        dependency_patterns = _compile_patterns(DEPENDENCY_FILE_PATTERNS)

        for file_info in changed_files:
            path = file_info.get("path", "")
            change_type = file_info.get("change_type", "M")  # A/M/D/R
            diff_lines = file_info.get("diff_lines", 0)

            # 1. 检查 forbidden_paths
            if forbidden_paths:
                for fp in forbidden_paths:
                    if path.startswith(fp) or path == fp:
                        violations.append(DiffViolation(
                            file_path=path,
                            violation_type=ViolationType.FORBIDDEN_PATH,
                            severity=ReviewSeverity.ERROR,
                            message=f"Modified forbidden path: {fp}",
                            change_type=change_type,
                            diff_lines=diff_lines,
                        ))

            # 2. 检查白名单
            if allowed_paths and not _is_in_allowlist(path, allowed_paths):
                violations.append(DiffViolation(
                    file_path=path,
                    violation_type=ViolationType.OUT_OF_ALLOWLIST,
                    severity=ReviewSeverity.ERROR,
                    message=f"File not in allowed_paths: {path}",
                    change_type=change_type,
                    diff_lines=diff_lines,
                ))

            # 3. 检查依赖文件修改
            if _match_any_pattern(path, dependency_patterns):
                violations.append(DiffViolation(
                    file_path=path,
                    violation_type=ViolationType.DEPENDENCY_CHANGED,
                    severity=ReviewSeverity.WARNING,
                    message=f"Dependency file modified: {path}. Review required.",
                    change_type=change_type,
                    diff_lines=diff_lines,
                ))

            # 4. 检查敏感配置文件修改
            elif _match_any_pattern(path, sensitive_patterns):
                violations.append(DiffViolation(
                    file_path=path,
                    violation_type=ViolationType.CONFIG_CHANGED,
                    severity=ReviewSeverity.WARNING,
                    message=f"Config file modified: {path}. Review required.",
                    change_type=change_type,
                    diff_lines=diff_lines,
                ))

            # 5. 检查意外删除
            if change_type == "D":
                violations.append(DiffViolation(
                    file_path=path,
                    violation_type=ViolationType.UNEXPECTED_DELETE,
                    severity=ReviewSeverity.WARNING,
                    message=f"File deleted: {path}",
                    change_type=change_type,
                    diff_lines=diff_lines,
                ))

        # 6. 检查总变更行数
        if total_lines > max_diff_lines:
            violations.append(DiffViolation(
                file_path="",
                violation_type=ViolationType.LARGE_DIFF,
                severity=ReviewSeverity.WARNING,
                message=f"Total diff lines ({total_lines}) exceeds threshold ({max_diff_lines}). Review recommended.",
                diff_lines=total_lines,
            ))

    except Exception as e:
        logger.warning(f"Diff review failed: {e}")
        # 审查失败时返回 warning，不阻塞任务
        violations.append(DiffViolation(
            file_path="",
            violation_type=ViolationType.OUT_OF_ALLOWLIST,
            severity=ReviewSeverity.WARNING,
            message=f"Diff review could not complete: {e}",
        ))

    passed = not any(v.severity == ReviewSeverity.ERROR for v in violations)

    return DiffReviewReport(
        passed=passed,
        total_files_changed=total_files,
        total_lines_changed=total_lines,
        violations=violations,
    )


async def review_task_result(
    task,
    project_id: str,
) -> DiffReviewReport:
    """
    对已完成任务的变更进行预审。

    Args:
        task: Task 对象
        project_id: 项目 ID

    Returns:
        DiffReviewReport
    """
    ctx = task.context or {}
    allowed_paths = ctx.get("allowed_paths", [])
    forbidden_paths = ctx.get("forbidden_paths", [])

    return await review_diff_from_git(
        project_id=project_id,
        branch=task.git_branch or "",
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
    )


def build_review_summary(report: DiffReviewReport) -> str:
    """将审查报告格式化为人类可读的摘要"""
    if report.passed and not report.violations:
        return f"变更预审通过：{report.total_files_changed} 个文件，{report.total_lines_changed} 行变更。"

    lines = [
        f"## 变更预审报告",
        f"",
        f"文件变更：{report.total_files_changed} 个，总变更行数：{report.total_lines_changed}",
        f"",
    ]

    errors = [v for v in report.violations if v.severity == ReviewSeverity.ERROR]
    warnings = [v for v in report.violations if v.severity == ReviewSeverity.WARNING]

    if errors:
        lines.append(f"### 错误（{len(errors)} 项）— 必须修正")
        for v in errors:
            lines.append(f"- `{v.file_path}`: {v.message}")
        lines.append("")

    if warnings:
        lines.append(f"### 警告（{len(warnings)} 项）— 建议审查")
        for v in warnings:
            lines.append(f"- `{v.file_path or '整体'}`: {v.message}")
        lines.append("")

    if report.passed:
        lines.append("**结论**：无严重违规，但存在警告项，建议审查。")
    else:
        lines.append("**结论**：存在严重违规，任务需要修正后重新提交。")

    return "\n".join(lines)
