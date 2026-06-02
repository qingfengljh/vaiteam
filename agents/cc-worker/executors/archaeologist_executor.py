"""代码考古角色执行器。

只读分析流程：设置工作区 → 执行 CC → 解析分析报告。
不涉及 Git、文件保护、交付物清单。
"""

from __future__ import annotations

import logging
import os

from .base import BaseExecutor

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
DRY_RUN = os.environ.get("CC_DRY_RUN", "0") == "1"


class ArchaeologistExecutor(BaseExecutor):
    """代码考古执行器。只读分析，不修改代码，不提交 Git。"""

    def _prepare_execution(
        self,
        task: dict,
        skill: dict,
        workspace: str,
        repo_url: str,
        branch: str,
        base_branch: str,
    ) -> dict | None:
        """预执行：只设置 Git 仓库（用于读取代码），不做文件保护。"""
        if repo_url:
            from git_setup import setup_repo
            logger.info(f"Task {task.get('id', '')}: git setup for read-only analysis {repo_url} @ {branch}")
            git_r = setup_repo(repo_url, workspace, branch=branch, base_branch=base_branch)
            if not git_r["ok"]:
                return {"error": f"Git setup failed: {git_r['error']}"}
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
        """后执行：只读角色，不做 Git commit、文件保护检查。"""
        # 只读角色：不做任何文件操作验证
        # 分析结果已经在 stdout 中，外层会解析
        task_id = task.get("id", task.get("task_id", ""))
        logger.info(f"Task {task_id}: archaeologist analysis completed (read-only, no git operations)")

    def _construct_prompt(self, task: dict, skill: dict) -> str:
        """构造考古角色的 prompt（不含 Git 要求、交付物清单）。"""
        from skill_loader import get_system_prompt

        task_ctx = {
            "goal": task.get("title", ""),
            "scope": task.get("scope", ""),
            "acceptance": task.get("acceptance", ""),
            "context_keys": task.get("context_keys", []),
        }
        # 注意：不传入 forbidden_paths 和 allowed_paths，因为考古角色不限于特定路径
        system = get_system_prompt(skill, task_ctx)

        instruction = task.get("instruction", task.get("description", ""))
        repo_url = task.get("git_repo", "")

        parts = [system, "", "---", "", f"## 分析任务\n\n{instruction}", ""]

        if repo_url:
            parts.append(f"**代码库**: {repo_url}")

        parts.append("")
        parts.append(
            "## 执行要求（只读分析）\n"
            "1. 在工作目录中浏览和阅读代码，**绝不修改任何文件**\n"
            "2. 按分析目标逐项完成\n"
            "3. 不需要执行 git add / git commit / git push\n"
            "4. 验证假设时只能运行只读命令（如测试、构建），不能修改配置\n"
        )

        # 全自动模式声明（通用）
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
            "任务不会被标记为失败，你的分析结果会被保留，疑问会在后续迭代中得到解答。\n"
        )

        # 强调只读约束（在角色 system_prompt 中已有，这里再强化一次）
        parts.append("")
        parts.append(
            "## 只读约束提醒\n"
            "- 你正在执行**代码考古**任务，不是编码任务\n"
            "- 任何 Write、Edit、Bash(rm/drop 等破坏性命令) 都是严重违规\n"
            "- 你的输出只有一份分析报告，不包含任何代码修改\n"
            "- 报告中的建议应通过 NEED_CLARIFICATION 上报，不能自行实施\n"
        )

        return "\n".join(parts)

    def _progress_message(self) -> str:
        return "分析完成，整理报告"
