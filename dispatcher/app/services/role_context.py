"""
角色上下文服务

加载角色模板（SOUL/IDENTITY/Skills）+ 注入项目上下文，
拼接成完整的 system prompt 发给 OpenClaw Agent。

分解阶段（Stage 0-3）的会话历史和已审批文档会被持久化注入给架构师 Agent，
确保执行阶段的架构师拥有完整的项目认知。

# Skill Profile concept inspired by gstack (MIT License, https://github.com/garrytan/gstack)
"""

import logging
import os
import time
from pathlib import Path
from functools import lru_cache

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, TaskDocument, Message, Document
from app.services import global_knowledge

logger = logging.getLogger(__name__)

_project_ctx_cache: dict[str, tuple[float, str]] = {}
PROJECT_CTX_TTL = 300  # 5 分钟缓存
FIELD_MAX_CHARS = 1500
GLOBAL_REF_LIMIT = 6
GLOBAL_REF_MAX_CHARS = 1200

APP_DIR = Path(__file__).parent.parent
SKILL_PACKS_DIR = APP_DIR / "skill_packs"
DEFAULT_SKILL_PACK = "programming"

ROLE_FILES = {
    "architect": "architect.md",
    "senior": "senior.md",
    "mid": "mid.md",
    "junior": "junior.md",
    "devops": "devops.md",
}


@lru_cache(maxsize=1)
def _active_skill_pack() -> str:
    return (os.getenv("OPENCLAW_SKILL_PACK") or DEFAULT_SKILL_PACK).strip() or DEFAULT_SKILL_PACK


@lru_cache(maxsize=1)
def _resolve_roles_dir() -> Path:
    pack = _active_skill_pack()
    pack_roles_dir = SKILL_PACKS_DIR / pack / "roles"
    if pack_roles_dir.exists():
        return pack_roles_dir
    raise FileNotFoundError(f"Skill pack roles dir not found: {pack_roles_dir}")


@lru_cache(maxsize=16)
def _load_role_template(role: str) -> str:
    filename = ROLE_FILES.get(role)
    if not filename:
        return f"你是一个 {role} 工程师。请按照任务指令完成工作。"
    try:
        path = _resolve_roles_dir() / filename
    except FileNotFoundError as e:
        logger.error(str(e))
        return f"你是一个 {role} 工程师。请按照任务指令完成工作。"
    if not path.exists():
        logger.warning(f"Role template not found: {path}")
        return f"你是一个 {role} 工程师。请按照任务指令完成工作。"
    content = path.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].lstrip("\n")
    return content


@lru_cache(maxsize=16)
def _load_role_profile(role: str) -> dict:
    """解析 roles/*.md 的 YAML frontmatter，返回结构化 Skill Profile。"""
    filename = ROLE_FILES.get(role)
    if not filename:
        return {}
    try:
        path = _resolve_roles_dir() / filename
    except FileNotFoundError as e:
        logger.error(str(e))
        return {}
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        logger.warning(f"Failed to parse frontmatter for role: {role}")
        return {}


def get_role_profile(role: str) -> dict:
    """供 scheduler 等外部模块读取角色 Skill Profile。"""
    return _load_role_profile(role)


def get_active_skill_pack() -> str:
    """返回当前启用的 skill pack 名称。"""
    return _active_skill_pack()


def reload_role_context_cache() -> dict:
    """清理角色模板与项目上下文缓存，供运维/调试触发热刷新。"""
    project_ctx_entries = len(_project_ctx_cache)
    _project_ctx_cache.clear()
    _active_skill_pack.cache_clear()
    _resolve_roles_dir.cache_clear()
    _load_role_template.cache_clear()
    _load_role_profile.cache_clear()
    return {
        "project_context_entries_cleared": project_ctx_entries,
        "role_template_cache_cleared": True,
        "role_profile_cache_cleared": True,
    }


def _trim(text: str, max_len: int = FIELD_MAX_CHARS) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…(已截断)"


def _load_global_knowledge_context(project_id: str, project_cfg: dict | None = None) -> str:
    """
    固化入口文档模式：
    - 固定读取 docs/00-GLOBAL_KNOWLEDGE_INDEX.md
    - 支持解析入口中的本地引用，附带短摘要，避免反复口口相传
    """
    repo_dir = global_knowledge.project_repo_dir(project_id)
    cfg = project_cfg or {}
    cfg_text = (cfg.get("global_knowledge_content") or "").strip()
    if cfg_text:
        try:
            parts = [
                "## 全局知识入口（必读）",
                f"入口文档: `{global_knowledge.GLOBAL_KNOWLEDGE_ENTRY}`（由项目 API 公告板维护）",
                _trim(cfg_text, 3000),
            ]
            refs = global_knowledge.extract_local_refs(cfg_text)
            if refs:
                parts.append("## 入口引用摘要\n" + "\n".join(f"- {r}" for r in refs[:GLOBAL_REF_LIMIT]))
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning(f"Load global knowledge from project config failed: {e}")

    entry = global_knowledge.resolve_entry_path(project_id)
    if not entry:
        return (
            "## 全局知识入口（缺失）\n"
            "当前项目尚未初始化公告板内容。请由架构师先在项目仓库创建 "
            f"`{global_knowledge.GLOBAL_KNOWLEDGE_ENTRY}` 并通过项目 API 提交后再执行关键任务。"
        )
    try:
        entry_text = entry.read_text(encoding="utf-8")
        parts = [
            "## 全局知识入口（必读）",
            f"入口文档: `{global_knowledge.GLOBAL_KNOWLEDGE_ENTRY}`",
            _trim(entry_text, 3000),
        ]
        refs = global_knowledge.extract_local_refs(entry_text)
        ref_parts: list[str] = []
        for rel in refs[:GLOBAL_REF_LIMIT]:
            path = (entry.parent / rel).resolve()
            try:
                path.relative_to(repo_dir.resolve())
            except ValueError:
                continue
            if not path.exists() or not path.is_file():
                continue
            if path.suffix.lower() not in (".md", ".txt", ".json", ".yaml", ".yml"):
                continue
            ref_text = path.read_text(encoding="utf-8")
            rel_display = str(path.relative_to(repo_dir))
            ref_parts.append(f"### 引用: {rel_display}\n{_trim(ref_text, GLOBAL_REF_MAX_CHARS)}")
        if ref_parts:
            parts.append("## 入口引用摘要\n" + "\n\n".join(ref_parts))
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Load global knowledge entry failed: {e}")
        return ""


async def build_project_context(session: AsyncSession, project_id: str) -> str:
    """从项目配置中提取技术栈、代码结构等上下文，带 TTL 缓存"""
    cached = _project_ctx_cache.get(project_id)
    if cached and (time.monotonic() - cached[0]) < PROJECT_CTX_TTL:
        return cached[1]

    project = await session.get(Project, project_id)
    if not project:
        return ""

    config = project.config or {}
    parts = []

    if project.description:
        parts.append(f"## 项目简介\n{_trim(project.description, 500)}")

    tech_stack = config.get("tech_stack")
    if tech_stack:
        if isinstance(tech_stack, list):
            parts.append("## 技术栈\n" + ", ".join(tech_stack))
        else:
            parts.append(f"## 技术栈\n{_trim(tech_stack, 300)}")

    code_structure = config.get("code_structure")
    if code_structure:
        parts.append(f"## 代码结构\n```\n{_trim(code_structure)}\n```")

    api_conventions = config.get("api_conventions")
    if api_conventions:
        parts.append(f"## API 约定\n{_trim(api_conventions)}")

    coding_style = config.get("coding_style")
    if coding_style:
        parts.append(f"## 编码规范\n{_trim(coding_style)}")

    git_repo = project.git_repo
    if git_repo:
        parts.append(f"## Git 仓库\n{git_repo}")

    gk_context = _load_global_knowledge_context(project_id, config)
    if gk_context:
        parts.append(gk_context)

    # 加载架构决策（分解阶段产出，传递给执行阶段的 Agent）
    arch_docs = await session.execute(
        select(TaskDocument).where(
            TaskDocument.project_id == project_id,
            TaskDocument.doc_type == "architecture_decision",
            TaskDocument.title == "架构决策汇总",
        ).order_by(TaskDocument.created_at.desc()).limit(1)
    )
    arch_summary = arch_docs.scalar_one_or_none()
    if arch_summary:
        parts.append(f"## 架构决策\n{_trim(arch_summary.summary or _read_doc_content(arch_summary.file_path), 2000)}")

    if not parts:
        result = ""
    else:
        result = "# 项目上下文\n\n" + "\n\n".join(parts)

    _project_ctx_cache[project_id] = (time.monotonic(), result)
    return result


def _read_doc_content(file_path: str, max_len: int = 2000) -> str:
    """读取文档文件内容"""
    try:
        p = Path(file_path)
        if p.exists():
            text = p.read_text(encoding="utf-8")
            return text[:max_len] if len(text) > max_len else text
    except Exception:
        pass
    return ""


# ── 分解阶段会话持久化注入 ──

STAGE_NAMES = {0: "业务方案", 1: "需求规范", 2: "产品原型", 3: "技术方案"}

_stage_ctx_cache: dict[str, tuple[float, str]] = {}
STAGE_CTX_TTL = 600  # 10 分钟缓存


async def build_stage_history_context(
    session: AsyncSession, project_id: str, iteration_id: str | None = None,
    max_total_chars: int = 120000,
) -> str:
    """加载 Stage 0-3 的完整过程资料（文档 + 全阶段会话），用于架构师无缝恢复上下文。"""
    cache_key = f"{project_id}:{iteration_id or 'default'}"
    cached = _stage_ctx_cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < STAGE_CTX_TTL:
        return cached[1]

    parts: list[str] = []
    budget = max_total_chars

    budget = await _load_stage_docs(session, project_id, iteration_id, parts, budget)

    if budget > 1000:
        budget = await _load_stage_dialogs(session, project_id, iteration_id, parts, budget)

    if not parts:
        result = ""
    else:
        result = "# 项目分析全过程（架构师前期完整会话与文档）\n\n" + "\n\n".join(parts)

    _stage_ctx_cache[cache_key] = (time.monotonic(), result)
    return result


async def _load_stage_docs(
    session: AsyncSession, project_id: str, iteration_id: str | None,
    parts: list[str], budget: int,
) -> int:
    """加载各阶段已审批的文档"""
    for stage in range(4):
        doc = await _find_approved_doc(session, project_id, stage, iteration_id)
        if not doc:
            continue
        stage_name = STAGE_NAMES.get(stage, f"阶段{stage}")
        content = doc.content or ""
        max_per_stage = budget // 4
        if len(content) > max_per_stage:
            content = content[:max_per_stage] + "\n…(已截断)"
        entry = f"## {stage_name}\n{content}\n"
        parts.append(entry)
        budget -= len(entry)
        if budget <= 0:
            break
    return budget


async def _load_stage_dialogs(
    session: AsyncSession, project_id: str, iteration_id: str | None,
    parts: list[str], budget: int,
) -> int:
    """加载 Stage 0-3 全阶段对话，按时间顺序注入。"""
    result = await session.execute(
        select(Message).where(
            Message.project_id == project_id,
            Message.iteration_id == iteration_id,
            Message.stage.in_([0, 1, 2, 3]),
        ).order_by(Message.created_at)
    )
    msgs = list(result.scalars())
    if not msgs:
        return budget

    dialog_lines: list[str] = []
    current_stage = None
    for m in msgs:
        if m.stage != current_stage:
            current_stage = m.stage
            stage_name = STAGE_NAMES.get(current_stage, f"阶段{current_stage}")
            header = f"\n### {stage_name} 会话\n"
            dialog_lines.append(header)
            budget -= len(header)
            if budget <= 0:
                dialog_lines.append("…(更多会话已省略)")
                break
        prefix = "用户" if m.role == "user" else "架构师"
        text = m.content or ""
        line = f"**{prefix}**: {text}"
        dialog_lines.append(line)
        budget -= len(line) + 2
        if budget <= 0:
            dialog_lines.append("…(更多对话已省略)")
            break
    parts.append("## Stage 0-3 全量会话记录\n" + "\n".join(dialog_lines))
    return budget


async def _find_approved_doc(
    session: AsyncSession, project_id: str, stage: int, iteration_id: str | None,
) -> Document | None:
    """查找指定阶段的已审核且选中的文档"""
    q = select(Document).where(
        Document.project_id == project_id,
        Document.stage == stage,
        Document.status == "approved",
        Document.is_selected == True,
    )
    if iteration_id:
        q = q.where(Document.iteration_id == iteration_id)
    q = q.order_by(Document.created_at.desc()).limit(1)
    result = await session.execute(q)
    return result.scalar_one_or_none()


def build_agent_prompt(
    role: str,
    instruction: str,
    project_context: str = "",
    experience_context: str = "",
    stage_history_context: str = "",
    git_branch: str = "",
    task_ref: str = "",
) -> str:
    """拼接完整的 agent prompt：角色模板 + 项目上下文 + 分解阶段会话 + 经验 + Git 信息 + 任务指令"""
    sections = [_load_role_template(role)]
    profile = _load_role_profile(role)
    if profile:
        constraint_parts: list[str] = []
        forbidden = profile.get("forbidden") or []
        if forbidden:
            constraint_parts.append("## 禁止行为（硬约束）\n" + "\n".join(f"- {item}" for item in forbidden))
        required_outputs = profile.get("required_outputs") or []
        if required_outputs:
            items: list[str] = []
            for output in required_outputs:
                if not isinstance(output, dict):
                    continue
                output_type = str(output.get("type") or "").strip()
                output_desc = str(output.get("description") or "").strip()
                if not output_type and not output_desc:
                    continue
                line = f"- **{output_type or 'output'}**: {output_desc or '未说明'}"
                condition = str(output.get("condition") or "").strip()
                if condition:
                    line += f"（条件: {condition}）"
                items.append(line)
            if items:
                constraint_parts.append("## 必须交付物\n" + "\n".join(items))
        completion_checks = profile.get("gate_rules", {}).get("completion_check") or []
        if completion_checks:
            constraint_parts.append("## 完成检查清单\n" + "\n".join(f"- [ ] {item}" for item in completion_checks))
        if constraint_parts:
            sections.append("# 角色约束（Skill Profile）\n\n" + "\n\n".join(constraint_parts))
    sections.append(
        "# 统一检索规则（强约束）\n\n"
        "当你缺少项目信息时，必须先使用系统统一检索器检索；"
        "检索不足再请求 Dispatcher 补充上下文；补充后继续通过统一检索器验证，不允许绕过检索直接臆断。"
    )

    if project_context:
        sections.append(project_context)

    if stage_history_context:
        sections.append(stage_history_context)

    if experience_context:
        sections.append(f"# 相关经验参考\n\n{experience_context}")

    if git_branch:
        git_section = "# Git 工作分支\n\n"
        git_section += f"- 分支: `{git_branch}`\n"
        git_section += f"- 开始工作前先执行: `git checkout {git_branch}`\n"
        if task_ref:
            git_section += f"- 任务号: `{task_ref}`\n"
            git_section += f"- commit message 必须包含: `Task: {task_ref}`\n"
            git_section += f"- 示例: `feat(module): 实现功能描述\\n\\nTask: {task_ref}`\n"
        git_section += "- 完成后 push 到远程，不要合并到 main"
        sections.append(git_section)

    sections.append(f"# 当前任务\n\n{instruction}")

    return "\n\n---\n\n".join(sections)
