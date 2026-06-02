"""加载角色 Skill 配置，构造 CC 系统提示词。

模板方法模式：
- 基础模板（base/workflow-template.md）：所有编码角色共享的硬约束与工作流
- 角色覆盖（roles/{role}/system-prompt.md）：角色的差异化能力
- 任务上下文（task_context）：本次任务的特定信息
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)

# 内嵌默认 skill（镜像外无 roles/ 目录时兜底）
DEFAULT_SKILLS: dict[str, dict] = {
    "senior": {
        "name": "senior",
        "description": "高级工程师，处理复杂业务功能，全栈闭环交付",
        "fullstack_capable": True,
        "context_quota": {"need_context_per_task": 3, "search_per_task": 3},
        "toolchain": ["git", "python3", "node", "npm"],
        "system_prompt": (
            "## 角色特定能力\n"
            "- 处理复杂业务逻辑：跨模块交互、复杂算法、性能优化\n"
            "- 全栈一体实现（服务端 API + 用户界面 + 数据层），确保接口一致、数据流通顺畅\n"
            "- DDD 优先：按领域模型直接实现；关键路径（支付/权限/状态机等高风险逻辑）走 TDD\n"
            "- 代码简洁，函数职责单一，错误处理清晰\n"
            "- loading / 空状态 / 错误状态都要处理\n"
        ),
    },
    "mid": {
        "name": "mid",
        "description": "中级工程师，完成常规业务功能的全栈实现",
        "fullstack_capable": True,
        "context_quota": {"need_context_per_task": 2, "search_per_task": 2},
        "toolchain": ["git", "python3", "node", "npm"],
        "system_prompt": (
            "## 角色特定能力\n"
            "- 完成常规业务功能的服务端 API、用户界面和测试\n"
            "- 严格执行任务描述，不自由发挥\n"
            "- 确保服务端接口与用户界面一致\n"
            "- 代码简洁，组件 props 接口清晰\n"
        ),
    },
    "junior": {
        "name": "junior",
        "description": "初级工程师，完成简单开发任务",
        "fullstack_capable": True,
        "context_quota": {"need_context_per_task": 1, "search_per_task": 1},
        "toolchain": ["git", "python3", "node", "npm"],
        "system_prompt": (
            "## 角色特定能力\n"
            "- 完成简单的开发任务，严格按照任务描述执行\n"
            "- 创建/修改指定的文件，实现指定的接口和组件\n"
            "- 任何不确定的地方都要标记 NEED_CLARIFICATION，不猜测\n"
        ),
    },
    "architect": {
        "name": "architect",
        "description": "架构师，技术总管，持有全局视图",
        "fullstack_capable": True,
        "context_quota": {"need_context_per_task": 5, "search_per_task": 5},
        "toolchain": ["git", "python3", "node", "npm", "docker"],
        "system_prompt": (
            "你是架构师。你持有全局技术视图，为执行侧提供结构化任务上下文。\n"
            "负责：技术方案设计、模块边界定义、接口契约制定、代码审查收口。\n"
            "不直接写业务代码，专注于架构决策和质量把控。\n"
            "给执行成员下发任务时，须包含：目标、验收标准、依赖知识 key、范围边界。\n"
            "## 角色特定能力\n"
            "- 审核其他角色的代码和决策\n"
            "- 处理其他角色上报的技术问题\n"
            "- 向 AI Leader 汇报无法决策的问题\n"
        ),
    },
    "devops": {
        "name": "devops",
        "description": "运维工程师，负责部署、配置和基础设施",
        "fullstack_capable": False,
        "context_quota": {"need_context_per_task": 2, "search_per_task": 2},
        "toolchain": ["git", "docker", "docker-compose", "ssh"],
        "system_prompt": (
            "## 角色特定能力\n"
            "- 负责部署配置、CI/CD、基础设施管理\n"
            "- 关注配置安全性、环境变量管理、健康检查、依赖版本控制\n"
            "- 不修改业务代码，专注于部署脚本和运维配置\n"
        ),
    },
    "tester": {
        "name": "tester",
        "description": "测试工程师，负责测试覆盖和质量保障",
        "fullstack_capable": False,
        "context_quota": {"need_context_per_task": 2, "search_per_task": 2},
        "toolchain": ["git", "python3", "node", "npm"],
        "system_prompt": (
            "## 角色特定能力\n"
            "- 负责测试覆盖、质量保障和缺陷发现\n"
            "- 端到端测试：覆盖 API 和 UI 的关键路径\n"
            "- 边界条件：覆盖异常输入、空状态、并发场景\n"
            "- 不修改业务代码，只编写测试和报告问题\n"
        ),
    },
    "archaeologist": {
        "name": "archaeologist",
        "description": "代码考古学家，深入分析旧代码库的结构、数据流和依赖关系",
        "fullstack_capable": True,
        "context_quota": {"need_context_per_task": 5, "search_per_task": 5},
        "toolchain": ["git", "python3", "node", "npm"],
        "system_prompt": (
            "你是代码考古学家。你的职责是深入理解旧代码库，提取结构知识、数据流和依赖关系，绝不修改任何代码。\n"
            "## 角色特定能力\n"
            "- 代码库结构分析：目录组织、模块划分、分层架构\n"
            "- 数据流追踪：从入口到存储的完整数据路径\n"
            "- 依赖关系映射：模块间调用、接口契约、数据模型\n"
            "- 技术债务识别：重复代码、过时的模式、潜在风险点\n"
            "- 运行时行为理解：通过测试和日志推断实际行为\n"
        ),
    },
}


def _roles_dir_path(given: str | None = None) -> Path | None:
    """确定 roles 目录路径。"""
    if given:
        return Path(given)
    # 默认：与 skill_loader.py 同级的 ../roles/
    here = Path(__file__).resolve().parent
    default = here.parent / "roles"
    if default.is_dir():
        return default
    return None


def _load_base_workflow(roles_dir: Path | None = None) -> str:
    """加载基础工作流模板（所有编码角色共享）。"""
    rd = roles_dir or _roles_dir_path()
    if not rd:
        return ""

    base_path = rd / "base" / "workflow-template.md"
    if base_path.is_file():
        try:
            return base_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to load base workflow template: {e}")

    return ""


def load_skill(role: str, roles_dir: str | None = None) -> dict:
    """加载指定角色的 skill 配置。"""
    role_clean = (role or "mid").lower().strip()
    rd = _roles_dir_path(roles_dir)

    # 1. 尝试从外部 roles/ 目录加载
    if rd and yaml:
        yaml_path = rd / role_clean / "skill.yaml"
        prompt_path = rd / role_clean / "system-prompt.md"
        if yaml_path.is_file():
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    skill = yaml.safe_load(f) or {}
                if prompt_path.is_file():
                    skill["system_prompt"] = prompt_path.read_text(encoding="utf-8")
                logger.info(f"Loaded skill for '{role_clean}' from {yaml_path}")
                return skill
            except Exception as e:
                logger.warning(f"Failed to load skill yaml for {role_clean}: {e}")

    # 2. 使用内置默认
    if role_clean in DEFAULT_SKILLS:
        logger.info(f"Using built-in skill for '{role_clean}'")
        return dict(DEFAULT_SKILLS[role_clean])

    # 3. fallback 到 mid
    logger.warning(f"Unknown role '{role_clean}', falling back to 'mid'")
    return dict(DEFAULT_SKILLS["mid"])


def get_system_prompt(skill: dict, task_context: dict | None = None, roles_dir: str | None = None) -> str:
    """构造完整的 CC 系统提示词（基础模板 + 角色覆盖 + 任务上下文）。"""
    role = skill.get("name", "mid")

    parts: list[str] = []

    # ── 1. 任务上下文（最高优先级，放在最前让 CC 先看到） ──
    if task_context:
        ctx_lines: list[str] = ["## 当前任务上下文"]
        goal = task_context.get("goal") or task_context.get("title", "")
        if goal:
            ctx_lines.append(f"目标：{goal}")
        scope = task_context.get("scope", "")
        if scope:
            ctx_lines.append(f"范围：{scope}")
        acceptance = task_context.get("acceptance", "")
        if acceptance:
            ctx_lines.append(f"验收：{acceptance}")
        forbidden_paths = task_context.get("forbidden_paths", [])
        if forbidden_paths:
            ctx_lines.append(f"禁止修改：{', '.join(forbidden_paths)}")
        context_keys = task_context.get("context_keys", [])
        if context_keys:
            ctx_lines.append(f"必读知识：{', '.join(context_keys)}")
        allowed_paths = task_context.get("allowed_paths", [])
        if allowed_paths:
            ctx_lines.append(f"允许修改：{', '.join(allowed_paths)}")

        # 上下文配额
        quota = skill.get("context_quota", {})
        if quota:
            ctx_lines.append("")
            ctx_lines.append(f"- 每次任务最多请求 {quota.get('need_context_per_task', 2)} 个 NEED_CONTEXT")
            ctx_lines.append(f"- 每次任务最多 {quota.get('search_per_task', 2)} 次 SEARCH")

        parts.append("\n".join(ctx_lines))

    # ── 2. 基础工作流模板（编码角色共享，独立工作流角色除外） ──
    # architect / archaeologist 有自己的完整工作流，不加载编码基础模板
    has_independent_workflow = role in ("architect", "archaeologist")
    if not has_independent_workflow:
        base_workflow = _load_base_workflow(_roles_dir_path(roles_dir))
        if base_workflow:
            parts.append(base_workflow)
        else:
            # 兜底：基础模板加载失败时的最小硬约束
            parts.append(
                "## 硬约束\n"
                "- 只完成当前任务，不修改任务范围外的代码\n"
                "- 不自行做架构决策，遇到需要决策的问题上报\n"
                "- 阶段性完成须 git commit 并 git push\n"
                "- commit message 规范: `<type>(<scope>): <summary>\\n\\nTask: TASK-xxx`\n"
            )

    # ── 3. 角色特定覆盖 ──
    role_prompt = skill.get("system_prompt", "")
    if role_prompt:
        parts.append(role_prompt)

    return "\n\n".join(parts)


if __name__ == "__main__":
    import json

    role = os.environ.get("AGENT_ROLE", "mid")
    skill = load_skill(role)
    print(json.dumps(skill, ensure_ascii=False, indent=2))
