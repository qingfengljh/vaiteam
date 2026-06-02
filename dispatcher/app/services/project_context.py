"""
项目级知识上下文 — 为所有 AI 调用提供统一的项目背景信息

两种模式：
1. 全量注入（get_project_context）：文档生成、AI 审核等一次性输出场景
2. 索引 + 按需加载（build_knowledge_index / load_knowledge_block）：对话交互场景
"""

import re
import logging
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ProjectAsset, Document, Experience

logger = logging.getLogger(__name__)

NEED_CONTEXT_RE = re.compile(r"\[NEED_CONTEXT:([\w:./-]+)\]")

MAX_ASSET_SUMMARY_LEN = 3000
MAX_DOC_SUMMARY_LEN = 1500

# AI 文档评审：优先完整上下文，仅在单稿/总长极大时截断（避免误杀；极端情况再碰 provider 上限）
AI_REVIEW_ASSET_SUMMARY_CAP = 120_000
AI_REVIEW_STAGE_BODY_CAP = 400_000
AI_REVIEW_PEER_BODY_CAP = 400_000
AI_REVIEW_BG_TOTAL_CAP = 900_000
AI_REVIEW_PEER_DOC_LIMIT = 8


async def get_project_context(
    session: AsyncSession,
    project_id: str,
    include_assets: bool = True,
    include_approved_docs: bool = False,
    *,
    asset_summary_max_len: int | None = None,
) -> str:
    """构建项目级知识上下文文本，供注入到任意 AI system prompt"""
    project = await session.get(Project, project_id)
    if not project:
        return ""

    parts = [_build_project_info(project)]

    if include_assets:
        asset_ctx = await _build_asset_context(session, project_id, summary_max_len=asset_summary_max_len)
        if asset_ctx:
            parts.append(asset_ctx)

    if include_approved_docs:
        doc_ctx = await _build_approved_docs_context(session, project_id)
        if doc_ctx:
            parts.append(doc_ctx)

    return "\n\n".join(parts)


async def build_ai_review_background(
    session: AsyncSession,
    project_id: str,
    *,
    current_stage: int,
    exclude_document_id: str | None = None,
    iteration_id: str | None = None,
) -> str:
    """
    AI 文档评审专用背景：含代码/资产分析 + 上游阶段正文（不要求已 approved）+ 同阶段其它交付物。
    解决「只审孤立文稿、未读技术方案/需求」导致的误杀。
    """
    parts: list[str] = []

    base = await get_project_context(
        session,
        project_id,
        include_assets=True,
        include_approved_docs=False,
        asset_summary_max_len=AI_REVIEW_ASSET_SUMMARY_CAP,
    )
    if base.strip():
        parts.append("【项目与代码/资产分析】\n" + base.strip())

    def _base_doc_filters():
        f = [
            Document.project_id == project_id,
            Document.status != "rejected",
        ]
        if iteration_id:
            f.append(Document.iteration_id == iteration_id)
        return f

    per_stage_cap = AI_REVIEW_STAGE_BODY_CAP
    for s in range(0, max(0, current_stage)):
        q = await session.execute(
            select(Document)
            .where(
                *_base_doc_filters(),
                Document.stage == s,
            )
            .order_by(Document.is_selected.desc(), Document.updated_at.desc(), Document.created_at.desc())
            .limit(1)
        )
        d = q.scalar_one_or_none()
        if not d or not (d.content or "").strip():
            continue
        body = (d.content or "").strip()
        if len(body) > per_stage_cap:
            body = body[:per_stage_cap] + "\n\n...(该阶段文档过长已截断；单稿上限约 {} 字)".format(per_stage_cap)
        sn = STAGE_NAMES_MAP.get(s, f"阶段{s}")
        parts.append(
            f"【上游·{sn}】{d.title}（status={d.status}, selected={d.is_selected}, v{d.version}）\n\n{body}"
        )

    if exclude_document_id and current_stage >= 0:
        peer_cap_each = AI_REVIEW_PEER_BODY_CAP
        q = await session.execute(
            select(Document)
            .where(
                *_base_doc_filters(),
                Document.stage == current_stage,
                Document.id != exclude_document_id,
            )
            .order_by(Document.is_selected.desc(), Document.updated_at.desc())
            .limit(AI_REVIEW_PEER_DOC_LIMIT)
        )
        peers = list(q.scalars())
        if peers:
            chunks = []
            for d in peers:
                body = (d.content or "").strip()
                if not body:
                    continue
                if len(body) > peer_cap_each:
                    body = body[:peer_cap_each] + "\n\n...(已截断；单篇上限约 {} 字)".format(peer_cap_each)
                chunks.append(f"### {d.title}（status={d.status}, selected={d.is_selected}）\n\n{body}")
            if chunks:
                parts.append(
                    "【同阶段其它文档（与待审稿并列，用于对齐技术方案/口径；非合并验收）】\n"
                    + "\n\n---\n\n".join(chunks)
                )

    out = "\n\n========\n\n".join(parts)
    max_bg = AI_REVIEW_BG_TOTAL_CAP
    if len(out) > max_bg:
        out = out[:max_bg] + "\n\n...(评审背景总长已截断；总上限约 {} 字)".format(max_bg)
    return out


async def get_asset_context(session: AsyncSession, project_id: str) -> str:
    """兼容旧调用：仅返回资产分析上下文"""
    return await _build_asset_context(session, project_id)


def _build_project_info(project: Project) -> str:
    lines = [f"- 项目名称：{project.name}"]

    type_map = {"new": "新项目", "maintain": "维护迭代", "legacy_rewrite": "旧系统重写"}
    lines.append(f"- 项目类型：{type_map.get(project.project_type, project.project_type)}")

    if project.description:
        lines.append(f"- 项目简介：{project.description}")
    if project.git_repo:
        lines.append(f"- Git 仓库：{project.git_repo}")
    if project.git_web_url:
        lines.append(f"- Git Web：{project.git_web_url}")
    if project.target_tech_stack:
        lines.append(f"- 目标技术栈：{project.target_tech_stack}")
    if project.rewrite_reason:
        lines.append(f"- 重写原因：{project.rewrite_reason}")

    return "\n".join(lines)


async def _build_asset_context(
    session: AsyncSession,
    project_id: str,
    *,
    summary_max_len: int | None = None,
) -> str:
    cap = summary_max_len if summary_max_len is not None else MAX_ASSET_SUMMARY_LEN
    q = await session.execute(
        select(ProjectAsset).where(
            ProjectAsset.project_id == project_id,
            ProjectAsset.status == "analyzed",
        )
    )
    parts = []
    for asset in q.scalars():
        label = "项目代码分析" if asset.asset_type == "code" else "API 规范审核"
        purpose = ""
        if asset.asset_type == "code":
            purpose = "（目的：维护此项目）" if asset.purpose == "maintain" else "（目的：学习代码风格）"
        summary = asset.summary
        if len(summary) > cap:
            summary = summary[:cap] + "\n...(已截断)"
        parts.append(f"### {label}{purpose}\n\n{summary}")
    return "\n\n---\n\n".join(parts)


async def _build_approved_docs_context(session: AsyncSession, project_id: str) -> str:
    q = await session.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.status == "approved",
        ).order_by(Document.stage, Document.created_at)
    )
    parts = []
    for doc in q.scalars():
        content = doc.content
        if len(content) > MAX_DOC_SUMMARY_LEN:
            content = content[:MAX_DOC_SUMMARY_LEN] + "\n...(已截断)"
        parts.append(f"### [Stage {doc.stage}] {doc.title}\n\n{content}")

    if not parts:
        return ""
    return "### 已审核通过的文档\n\n" + "\n\n---\n\n".join(parts)


# ────────────────────────────────────────────────
# 知识索引（轻量摘要）+ 按需加载（完整内容）
# ────────────────────────────────────────────────

KNOWLEDGE_INDEX_HEADER = """## 项目知识索引

你可以访问用户上传的代码分析报告、API 规范、阶段文档等所有项目知识。

获取详细知识的方式：
1. **精确加载**：使用 [NEED_CONTEXT:key] 加载下方列出的知识块（key 是方括号中的标识）
2. **模糊检索**：使用 [SEARCH:关键词或问题] 搜索所有知识（支持语义搜索）

重要：
- 如果用户询问代码相关的问题，使用 [NEED_CONTEXT:code_analysis] 获取代码分析报告
- 如果摘要信息已足够回答，直接回答即可
- 每次最多请求 2 个知识块/搜索
- 不要告诉用户你"无法访问代码"，你可以通过上述方式获取代码分析内容
"""

STAGE_NAMES_MAP = {0: "业务方案", 1: "需求规范", 2: "产品原型", 3: "技术方案", 4: "任务分解"}

TYPE_MAP = {"new": "新项目", "maintain": "维护迭代", "legacy_rewrite": "旧系统重写"}


async def build_knowledge_index(
    session: AsyncSession,
    project_id: str,
    include_experiences: bool = True,
    active_stage: int | None = None,
) -> str:
    """构建轻量级知识索引目录，约 500-800 token，用于对话场景注入 system prompt"""
    project = await session.get(Project, project_id)
    if not project:
        return ""

    lines = [KNOWLEDGE_INDEX_HEADER, "### 项目基础"]
    lines.append(_index_project_info(project))

    assets = await _load_assets(session, project_id)
    for asset in assets:
        lines.append(_index_asset(asset))

    docs = await _load_index_docs(session, project_id, active_stage=active_stage)
    if docs:
        lines.append("\n### 阶段文档（会话可读）")
        for doc in docs:
            lines.append(_index_doc(doc))

    project_docs = _scan_project_docs(project)
    if project_docs:
        lines.append("\n### 项目设计文档")
        for name, title in project_docs:
            lines.append(f"- [docs:{name}] {title}")

    if include_experiences:
        from app.services import experience as exp_svc
        exps = await exp_svc.find_relevant(session, limit=5)
        if exps:
            lines.append("\n### 全局经验")
            for i, exp in enumerate(exps, 1):
                lines.append(_index_exp(exp, i))

    return "\n".join(lines)


async def load_knowledge_block(
    session: AsyncSession,
    project_id: str,
    key: str,
    active_stage: int | None = None,
) -> str:
    """按 key 加载具体的知识块全文，返回格式化文本"""
    if key == "project_info":
        project = await session.get(Project, project_id)
        return _build_project_info(project) if project else ""

    if key == "code_analysis":
        assets = await _load_assets(session, project_id)
        parts = []
        for a in assets:
            if a.asset_type == "code":
                path_info = f"源码存放路径：{a.file_path}\n\n" if a.file_path else ""
                parts.append(f"### 代码分析报告\n\n{path_info}{a.summary}")
        return "\n\n".join(parts)

    if key == "api_spec":
        return await _load_doc_by_title(session, project_id, "API")

    if key == "code_style":
        return await _load_doc_by_title(session, project_id, "代码风格")

    if key.startswith("doc_s"):
        stage = int(key[5:]) if key[5:].isdigit() else -1
        if 0 <= stage <= 4:
            return await _load_stage_doc(session, project_id, stage, active_stage=active_stage)
        return ""

    if key.startswith("exp_"):
        exp_id = key[4:]
        return await _load_experience(session, exp_id)

    if key.startswith("docs:"):
        filename = key[5:]
        return _load_project_doc_file(filename)

    return f"未知的知识块: {key}"


def extract_need_context_keys(text: str) -> list[str]:
    """从 AI 输出中提取 [NEED_CONTEXT:xxx] 标记的 key 列表"""
    return NEED_CONTEXT_RE.findall(text)


# ── 索引条目生成 ──

def _index_project_info(project: Project) -> str:
    ptype = TYPE_MAP.get(project.project_type, project.project_type)
    tech = project.target_tech_stack or ""
    git = project.git_repo.split("/")[-1].replace(".git", "") if project.git_repo else ""
    return f"- [project_info] {project.name} | {ptype} | {tech} | {git}"


def _index_asset(asset: ProjectAsset) -> str:
    first_line = (asset.summary or "").split("\n")[0][:100].lstrip("#").strip()
    if asset.asset_type == "code":
        path_hint = f"（路径: {asset.file_path}）" if asset.file_path else ""
        return f"- [code_analysis] 用户上传的代码分析报告{path_hint}：{first_line}"
    return f"- [api_spec] 用户上传的 API 规范分析：{first_line}"


def _index_doc(doc: Document) -> str:
    first_line = (doc.content or "").split("\n")[0][:80].lstrip("#").strip()
    stage_name = STAGE_NAMES_MAP.get(doc.stage, f"阶段{doc.stage}")
    date_str = doc.updated_at.strftime("%Y-%m-%d") if doc.updated_at else ""
    return f"- [doc_s{doc.stage}] {stage_name} - {doc.title}：{first_line}（v{doc.version}, {date_str}）"


def _index_exp(exp: Experience, idx: int) -> str:
    return f"- [exp_{exp.id}] {exp.title}（quality:{exp.quality_score}, used:{exp.use_count}）"


# ── 知识块加载辅助 ──

async def _load_assets(session: AsyncSession, project_id: str) -> list[ProjectAsset]:
    q = await session.execute(
        select(ProjectAsset).where(ProjectAsset.project_id == project_id, ProjectAsset.status == "analyzed")
    )
    return list(q.scalars())


async def _load_index_docs(
    session: AsyncSession,
    project_id: str,
    active_stage: int | None = None,
) -> list[Document]:
    """
    构建知识索引用的阶段文档集合：
    - 同阶段（active_stage）读取该阶段全部文档（含 draft/rejected）
    - 跨阶段仅读取 approved 文档
    """
    if isinstance(active_stage, int) and active_stage >= 0:
        same_stage_q = await session.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.stage == active_stage,
            ).order_by(Document.updated_at.desc(), Document.created_at.desc())
        )
        cross_stage_q = await session.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.stage < active_stage,
                Document.status == "approved",
            ).order_by(Document.stage, Document.updated_at.desc(), Document.created_at.desc())
        )
        return list(cross_stage_q.scalars()) + list(same_stage_q.scalars())

    q = await session.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.status == "approved",
        ).order_by(Document.stage, Document.updated_at.desc(), Document.created_at.desc())
    )
    return list(q.scalars())


async def _load_doc_by_title(session: AsyncSession, project_id: str, keyword: str) -> str:
    q = await session.execute(
        select(Document).where(
            Document.project_id == project_id,
            Document.title.ilike(f"%{keyword}%"),
            Document.status == "approved",
        ).order_by(Document.updated_at.desc()).limit(1)
    )
    doc = q.scalar_one_or_none()
    if not doc:
        return f"未找到包含「{keyword}」的已审核文档"
    return f"### {doc.title}（v{doc.version}）\n\n{doc.content}"


async def _load_stage_doc(
    session: AsyncSession,
    project_id: str,
    stage: int,
    active_stage: int | None = None,
) -> str:
    same_stage = isinstance(active_stage, int) and stage == active_stage
    filters = [
        Document.project_id == project_id,
        Document.stage == stage,
    ]
    if not same_stage:
        filters.append(Document.status == "approved")

    q = await session.execute(
        select(Document).where(*filters).order_by(Document.updated_at.desc(), Document.created_at.desc())
    )
    docs = list(q.scalars())
    stage_name = STAGE_NAMES_MAP.get(stage, f"阶段{stage}")
    if not docs:
        if same_stage:
            return f"阶段「{stage_name}」暂无文档"
        return f"阶段「{stage_name}」暂无已审核文档"

    mode_text = "同阶段全量（含未审核/被拒绝）" if same_stage else "跨阶段仅已审核"
    parts = [f"### [{stage_name}] 文档列表（{mode_text}）"]
    for idx, doc in enumerate(docs, 1):
        selected = "yes" if doc.is_selected else "no"
        parts.append(
            f"\n#### {idx}. {doc.title}（v{doc.version}, status={doc.status}, selected={selected}）\n\n{doc.content or ''}"
        )
    return "\n".join(parts)


# ── 项目 docs/ 目录扫描 ──

_DOCS_DIR = Path(__file__).parent.parent.parent.parent / "docs"

_DOC_TITLE_MAP = {
    "00-README": "项目总览",
    "01-VISION": "产品愿景",
    "02-ARCHITECTURE": "系统架构",
    "03-ROLES": "角色定义",
    "04-WORKFLOW": "工作流程",
    "05-TASK_DESIGN": "任务设计",
    "06-COMMUNICATION": "通信机制",
    "07-KNOWLEDGE_SYSTEM": "知识系统",
    "08-DEPLOYMENT": "部署方案",
    "09-BOOTSTRAP": "启动引导",
    "11-TESTING_STRATEGY": "测试策略",
    "12-CICD_INTEGRATION": "CI/CD集成",
    "17-LEGACY_REWRITE": "旧系统重写",
    "18-TASK_EXECUTION_FLOW": "任务执行流程",
    "22-SAAS_ARCHITECTURE": "SaaS架构",
    "23-CONTEXT_MANAGEMENT": "上下文管理",
    "24-AGENT_COMMAND_CHAIN": "Agent指挥链",
    "28-TOKEN_OPTIMIZATION": "Token优化",
}


def _scan_project_docs(project: Project) -> list[tuple[str, str]]:
    """扫描项目 docs/ 目录，返回 [(filename, title), ...]"""
    if not _DOCS_DIR.is_dir():
        return []
    results = []
    for f in sorted(_DOCS_DIR.glob("*.md")):
        stem = f.stem
        title = _DOC_TITLE_MAP.get(stem, stem.replace("-", " ").replace("_", " "))
        results.append((stem, title))
    return results[:20]


def _load_project_doc_file(filename: str) -> str:
    """加载 docs/ 目录下的文件全文"""
    filepath = _DOCS_DIR / f"{filename}.md"
    if not filepath.is_file():
        filepath = _DOCS_DIR / filename
    if not filepath.is_file():
        return f"未找到项目文档: {filename}"
    content = filepath.read_text(encoding="utf-8")
    if len(content) > 8000:
        content = content[:8000] + "\n\n... （文档过长，已截断）"
    return f"## 项目文档: {filename}\n\n{content}"


async def _load_experience(session: AsyncSession, exp_id_or_idx: str) -> str:
    """加载经验详情，支持按 ID 查找"""
    exp = await session.get(Experience, exp_id_or_idx)
    if not exp:
        from app.services import experience as exp_svc
        exps = await exp_svc.find_relevant(session, limit=10)
        for e in exps:
            if str(e.id) == str(exp_id_or_idx):
                exp = e
                break
    if not exp:
        return f"未找到经验 #{exp_id_or_idx}"
    parts = [f"### {exp.title}"]
    if exp.problem:
        parts.append(f"**问题**：{exp.problem}")
    if exp.root_cause:
        parts.append(f"**根因**：{exp.root_cause}")
    if exp.solution:
        parts.append(f"**方案**：{exp.solution}")
    if exp.code_snippet:
        parts.append(f"```\n{exp.code_snippet}\n```")
    return "\n\n".join(parts)
