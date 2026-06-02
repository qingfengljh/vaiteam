"""编码角色执行器。

覆盖完整的编码流程：Git 设置 → 文件保护 → 执行 CC → 文件保护检查 → Git commit + push。
角色：senior / mid / junior / architect / devops / tester
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .base import BaseExecutor

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
DRY_RUN = os.environ.get("CC_DRY_RUN", "0") == "1"


class CodingExecutor(BaseExecutor):
    """编码角色执行器。包含 Git、文件保护、交付物清单等完整流程。"""

    def _prepare_execution(
        self,
        task: dict,
        skill: dict,
        workspace: str,
        repo_url: str,
        branch: str,
        base_branch: str,
    ) -> dict | None:
        """预执行：Git 设置 + 文件保护快照 + 文件白名单。"""
        # 1. Git 设置
        if repo_url:
            from git_setup import setup_repo
            logger.info(f"Task {task.get('id', '')}: git setup {repo_url} @ {branch}")
            git_r = setup_repo(repo_url, workspace, branch=branch, base_branch=base_branch)
            if not git_r["ok"]:
                return {"error": f"Git setup failed: {git_r['error']}"}

        # 2. 文件保护快照
        file_guardian_enabled = os.environ.get("FILE_GUARDIAN_ENABLED", "1") == "1"
        self._guardian_snapshot = None
        if file_guardian_enabled and repo_url:
            from file_guardian import snapshot_protected_files
            logger.info(f"Task {task.get('id', '')}: taking file guardian snapshot")
            try:
                self._guardian_snapshot = asyncio.run(snapshot_protected_files(workspace))
                logger.info(f"Task {task.get('id', '')}: snapshot done, {len(self._guardian_snapshot)} files protected")
            except Exception as e:
                logger.warning(f"Task {task.get('id', '')}: file guardian snapshot failed (non-blocking): {e}")

        # 3. 文件白名单
        allowed_paths = task.get("allowed_paths", [])
        self._allowlist_report = None
        if allowed_paths and repo_url:
            from file_allowlist import apply_allowlist
            logger.info(f"Task {task.get('id', '')}: applying file allowlist: {allowed_paths}")
            try:
                self._allowlist_report = apply_allowlist(workspace, allowed_paths)
                logger.info(
                    f"Task {task.get('id', '')}: allowlist applied, "
                    f"readonly={self._allowlist_report.get('readonly', 0)}, "
                    f"writable={self._allowlist_report.get('writable', 0)}"
                )
            except Exception as e:
                logger.warning(f"Task {task.get('id', '')}: file allowlist failed (non-blocking): {e}")

        return None

    def _post_execution(
        self,
        task: dict,
        skill: dict,
        workspace: str,
        repo_url: str,
        claude_r: dict,
        result: dict,
    ) -> None:
        """后执行：文件保护检查 + 恢复权限 + 白名单验证 + Git commit + push。"""
        task_id = task.get("id", task.get("task_id", ""))

        # 1. 文件保护检查
        if self._guardian_snapshot:
            from file_guardian import check_protected_files, build_protection_summary
            logger.info(f"Task {task_id}: checking file guardian violations")
            try:
                guardian_report = asyncio.run(
                    check_protected_files(workspace, self._guardian_snapshot, auto_restore=True)
                )
                if not guardian_report.passed:
                    violation_summary = build_protection_summary(guardian_report.violations)
                    logger.error(f"Task {task_id}: file guardian violations detected\n{violation_summary}")
                    result["guardian_violations"] = [v.to_dict() for v in guardian_report.violations]
                    result["guardian_passed"] = False
                    error_violations = [v for v in guardian_report.violations if v.severity == "error"]
                    if error_violations:
                        result["status"] = "blocked"
                        result["error"] = (
                            f"任务执行过程中检测到受保护文件被修改（已自动回滚 {guardian_report.restored} 个）。\n"
                            f"违规文件: {', '.join(v.relative_path for v in error_violations)}\n"
                            f"请检查任务范围是否包含这些文件，或是否需要人类审核后重新分配。"
                        )
                else:
                    result["guardian_passed"] = True
            except Exception as e:
                logger.warning(f"Task {task_id}: file guardian check failed (non-blocking): {e}")

        # 2. 恢复文件权限
        if self._allowlist_report:
            from file_allowlist import restore_permissions
            try:
                restore_report = restore_permissions(workspace)
                logger.info(f"Task {task_id}: permissions restored: {restore_report}")
            except Exception as e:
                logger.warning(f"Task {task_id}: permission restore failed (non-blocking): {e}")

        # 3. 白名单变更验证
        allowed_paths = task.get("allowed_paths", [])
        if allowed_paths and repo_url:
            from file_allowlist import validate_changes
            try:
                allowlist_violations = validate_changes(workspace, allowed_paths)
                if allowlist_violations:
                    logger.error(
                        f"Task {task_id}: allowlist violations detected: "
                        f"{[v['path'] for v in allowlist_violations]}"
                    )
                    result["allowlist_violations"] = allowlist_violations
            except Exception as e:
                logger.warning(f"Task {task_id}: allowlist validation failed (non-blocking): {e}")

        # 4. Git commit + push
        if repo_url and not DRY_RUN:
            from git_setup import commit_and_push
            logger.info(f"Task {task_id}: git commit + push")
            commit_msg = f"feat: {task.get('title', 'task')}\n\nTask: {task_id}\nAgent: {self.agent_id}"
            if result.get("guardian_passed") is False:
                violation_files = ", ".join(
                    v["relative_path"] for v in (result.get("guardian_violations") or [])
                )
                commit_msg += f"\n\n[GUARDIAN] Protected files were modified and auto-restored: {violation_files}"
            allowlist_v = result.get("allowlist_violations", [])
            if allowlist_v:
                violation_files = ", ".join(v["path"] for v in allowlist_v)
                commit_msg += f"\n\n[ALLOWLIST] Changes outside allowed paths: {violation_files}"
            push_r = commit_and_push(workspace, commit_msg)
            if push_r["ok"]:
                result["commit"] = push_r.get("commit", "")
            else:
                result["error"] = f"Git push failed: {push_r['error']}"
                # 注意：这里不 return，让外层根据 result["status"] 判断

    def _construct_prompt(self, task: dict, skill: dict) -> str:
        """构造编码角色的 prompt（含 Git 要求、交付物清单）。"""
        from skill_loader import get_system_prompt

        task_ctx = {
            "goal": task.get("title", ""),
            "scope": task.get("scope", ""),
            "acceptance": task.get("acceptance", ""),
            "forbidden_paths": task.get("forbidden_paths", []),
            "context_keys": task.get("context_keys", []),
            "allowed_paths": task.get("allowed_paths", []),
        }
        system = get_system_prompt(skill, task_ctx)

        instruction = task.get("instruction", task.get("description", ""))
        git_branch = task.get("git_branch", "")
        git_base = task.get("git_base_branch", "develop")
        files = task.get("files", [])

        parts = [system, "", "---", "", f"## 任务指令\n\n{instruction}", ""]

        if git_branch:
            parts.append(f"**分支**: {git_branch} (基于 {git_base})")
        if files:
            parts.append(f"**涉及文件**: {', '.join(files)}")

        parts.append("")
        parts.append(
            "## 执行要求\n"
            "1. 直接在工作目录中修改代码\n"
            "2. 按验收标准逐项验证\n"
            "3. 完成后执行 git add + git commit + git push\n"
            "4. 如有测试要求，先跑测试确认通过\n"
        )

        # 全自动模式声明
        parts.append("")
        parts.append(
            "## 全自动执行模式（重要）\n"
            "你当前处于**全自动无人值守模式**。这意味着：\n"
            "- 你不能停下来等待人类回答你的问题\n"
            "- 你不能打开浏览器搜索外部信息\n"
            "- 所有沟通必须通过本系统完成\n"
            "\n"
            "如果你有疑问或不确定的地方，**不要停下来**，继续按你的最佳理解执行。\n"
            "同时，在最终输出的末尾，以如下格式列出你的疑问：\n"
            "\n"
            "```\n"
            "NEED_CLARIFICATION:\n"
            "1. [具体问题1]\n"
            "2. [具体问题2]\n"
            "```\n"
            "\n"
            "系统会自动提取这些疑问并上报给架构师/人类。\n"
            "任务不会被标记为失败，你的执行结果会被保留，疑问会在后续迭代中得到解答。\n"
        )

        # 文件白名单
        allowed_paths = task.get("allowed_paths", [])
        if allowed_paths:
            parts.append("")
            parts.append("## 文件修改白名单（严格执行）")
            parts.append("你只能修改以下路径范围内的文件。不在此列表中的文件已被系统设为只读，你**无法**修改它们：")
            for p in allowed_paths:
                parts.append(f"- `{p}`")
            parts.append("")
            parts.append("如果你发现需要修改白名单外的文件才能完成任务，**不要擅自修改**，在结果中说明理由，由架构师/人类审核后重新分配。")

        # 文件保护声明
        parts.append("")
        parts.append(
            "## 文件保护声明（不可违反）\n"
            "以下类型的文件受系统保护，任何情况下都不得修改、删除或创建：\n"
            "- 配置文件：.env, .env.*, config.yaml, config.json\n"
            "- 部署配置：docker-compose.yml, Dockerfile, nginx.conf\n"
            "- 依赖清单：package.json, requirements.txt, pyproject.toml\n"
            "- 版本控制：.gitignore, .gitattributes\n"
            "- 构建配置：Makefile, tsconfig.json, vite.config.*\n"
            "- CI/CD：.github/*.yml, .gitlab-ci.yml, Jenkinsfile\n"
            "\n"
            "违反此规则将导致任务被判定为失败，所有修改自动回滚。\n"
            "如果你认为某个配置文件需要修改，在结果中说明理由，由人类审核决定。\n"
        )

        # 交付物清单
        parts.append("")
        parts.append(
            "## 交付物清单（必须填写，系统会自动校验）\n"
            "任务完成后，你必须在最终输出中提供以下清单。遗漏或虚假声明将导致任务被判定为失败：\n"
            "\n"
            "### 修改的文件\n"
            "列出所有你修改过的文件（一行一个）：\n"
            "- `path/to/file1.py` — 修改内容简述\n"
            "- `path/to/file2.js` — 修改内容简述\n"
            "\n"
            "### 新增的文件\n"
            "列出所有你创建的新文件：\n"
            "- `path/to/new_file.py` — 用途简述\n"
            "\n"
            "### 删除的文件\n"
            "列出所有你删除的文件（如有）：\n"
            "- `path/to/deleted_file.py` — 删除原因\n"
            "\n"
            "### 执行的命令\n"
            "列出你执行过的关键命令：\n"
            "- `pytest tests/test_xxx.py` — 测试通过\n"
            "- `npm run build` — 构建通过\n"
            "\n"
            "### 未修改的声明\n"
            "明确声明以下文件**未**被修改（如有涉及）：\n"
            "- `.env` — 未修改\n"
            "- `package.json` — 未修改\n"
            "\n"
            "⚠️ 系统会将此清单与实际的 git diff 交叉验证。\n"
            "任何未声明的变更或虚假声明都会导致任务被阻塞并上报。\n"
        )

        return "\n".join(parts)

    def _progress_message(self) -> str:
        return "编码完成，准备提交"
