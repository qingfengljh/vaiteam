"""
全局经验库服务

跨项目的经验积累与复用：踩坑记录、最佳实践、代码模板、架构决策。
三层检索：jieba 分词 tsvector 粗筛 → pgvector 语义精排 → LIKE 兜底。
按技术栈分类，全局共用。

生成期自动贴标：项目上下文继承 + 代码静态分析 + LLM 语义推断 + 交叉验证。
检索期严格过滤：语义搜索层强制技术栈重叠（通用经验除外）。
"""

import logging
import re
from sqlalchemy import select, func, or_, text, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Experience, FailurePattern
from app.services import ai_leader
from app.services.task_docs import extract_keywords, segment_for_tsv

logger = logging.getLogger(__name__)

CATEGORIES = [
    "pitfall",       # 踩坑记录
    "best_practice", # 最佳实践
    "code_template", # 代码模板
    "architecture",  # 架构决策
    "debug_pattern", # 调试模式
    "performance",   # 性能优化
    "security",      # 安全相关
    "devops",        # 运维经验
]

TECH_DOMAINS = [
    "frontend",       # Vue, React, Angular...
    "backend",        # FastAPI, Spring, Express...
    "database",       # PostgreSQL, MongoDB, Redis...
    "infrastructure", # Docker, K8s, CI/CD...
    "language",       # Python, TypeScript, Go...
    "general",        # 跨技术域通用
]

# 技术栈 → 技术域映射
TECH_DOMAIN_MAP = {
    # 前端
    "vue": "frontend", "react": "frontend", "angular": "frontend",
    "pinia": "frontend", "vuex": "frontend", "redux": "frontend",
    "nextjs": "frontend", "nuxt": "frontend",
    # 后端
    "fastapi": "backend", "flask": "backend", "django": "backend",
    "spring": "backend", "spring-boot": "backend", "express": "backend",
    "nestjs": "backend",
    # 数据库
    "postgresql": "database", "mysql": "database", "mongodb": "database",
    "redis": "database", "elasticsearch": "database",
    "sqlalchemy": "database", "prisma": "database", "typeorm": "database",
    # 基础设施
    "docker": "infrastructure", "kubernetes": "infrastructure",
    "k8s": "infrastructure", "jenkins": "infrastructure",
    "github-actions": "infrastructure", "gitlab-ci": "infrastructure",
    "nginx": "infrastructure", "apisix": "infrastructure",
    # 语言
    "python": "language", "typescript": "language", "javascript": "language",
    "go": "language", "rust": "language", "java": "language",
}

# 代码片段中检测技术栈的正则映射
CODE_TECH_PATTERNS = {
    # 前端框架
    r"from\s+['\"]?vue['\"]?|import\s+.*from\s+['\"]vue['\"]|createApp\(": ["vue"],
    r"from\s+['\"]?react['\"]?|import\s+.*React|useState\(|useEffect\(": ["react"],
    r"@Component|NgModule|Injectable\(": ["angular"],
    r"from\s+['\"]?pinia['\"]?|defineStore\(": ["pinia"],
    r"from\s+['\"]?next['\"]?|next/head|next/router": ["nextjs"],
    # 后端框架
    r"from\s+['\"]?fastapi['\"]?|FastAPI|@app\.|APIRouter\(": ["fastapi"],
    r"from\s+['\"]?django['\"]?|from\s+['\"]?flask['\"]?": ["django", "flask"],
    r"SpringBoot|@RestController|@RequestMapping|@Service": ["spring-boot"],
    r"from\s+['\"]?express['\"]?|express\(\)|router\.": ["express"],
    # 数据库
    r"from\s+['\"]?sqlalchemy['\"]?|create_engine\(|\.query\(|declarative_base\(": ["sqlalchemy"],
    r"prisma\.|@prisma/client|PrismaClient": ["prisma"],
    r"from\s+['\"]?redis['\"]?|Redis\(": ["redis"],
    r"from\s+['\"]?pymongo['\"]?|MongoClient": ["mongodb"],
    # 语言特征
    r"async\s+def\s+|def\s+.*\(.*\)\s+->\s+\w+|^\s*import\s+\w+|^\s*from\s+\w+\s+import": ["python"],
    r"interface\s+\w+|type\s+\w+\s*=|const\s+\w+:\s*\w+|\w+\?\s*:": ["typescript"],
    r"package\s+main|func\s+\w+\(|import\s+\(": ["go"],
    # 基础设施
    r"FROM\s+\w+.*:|docker\s+|docker-compose|COPY\s+.*\s+.*": ["docker"],
    r"apiVersion:\s*v1|kind:\s*Deployment|metadata:\s*": ["kubernetes"],
}

# 通用技术（不受项目技术栈限制）
GENERAL_TECH = {"git", "http", "rest", "json", "yaml", "markdown", "linux", "bash"}


def detect_tech_from_code(code: str) -> list[str]:
    """从代码片段中静态分析检测技术栈"""
    if not code:
        return []
    detected = set()
    for pattern, techs in CODE_TECH_PATTERNS.items():
        if re.search(pattern, code, re.IGNORECASE | re.MULTILINE):
            detected.update(techs)
    return list(detected)


def derive_tech_domains(tech_stack: list[str]) -> list[str]:
    """从技术栈推导技术域"""
    domains = set()
    for tech in tech_stack:
        domain = TECH_DOMAIN_MAP.get(tech.lower())
        if domain:
            domains.add(domain)
    if not domains:
        domains.add("general")
    return list(domains)


def _build_exp_text(exp_data: dict) -> str:
    """拼接经验的全文用于分词和 embedding"""
    parts = [
        exp_data.get("title", ""),
        exp_data.get("problem", ""),
        exp_data.get("root_cause", ""),
        exp_data.get("solution", ""),
        " ".join(exp_data.get("tech_stack", [])),
    ]
    return " ".join(p for p in parts if p)


async def create(session: AsyncSession, **kwargs) -> Experience:
    full_text = _build_exp_text(kwargs)
    kwargs.setdefault("keywords", extract_keywords(full_text))

    # 自动推导 tech_domain
    tech_stack = kwargs.get("tech_stack", [])
    if tech_stack and not kwargs.get("tech_domain"):
        domains = derive_tech_domains(tech_stack)
        kwargs["tech_domain"] = domains[0] if domains else "general"

    exp = Experience(**kwargs)
    session.add(exp)
    await session.flush()

    tsv_text = segment_for_tsv(full_text)
    await session.execute(
        text("UPDATE experiences SET tsv = to_tsvector('simple', :tsv_text) WHERE id = :eid"),
        {"tsv_text": tsv_text, "eid": exp.id},
    )

    # 计算并写入 embedding，使语义搜索可用
    try:
        from app.services import knowledge_search
        embedding = await knowledge_search._get_query_embedding(full_text)
        if embedding:
            exp.embedding = embedding
    except Exception as e:
        logger.warning(f"Experience embedding calculation failed (non-blocking): {e}")

    await session.commit()
    await session.refresh(exp)
    return exp


async def get(session: AsyncSession, exp_id: str) -> Experience | None:
    return await session.get(Experience, exp_id)


async def update(session: AsyncSession, exp_id: str, **kwargs) -> Experience | None:
    exp = await session.get(Experience, exp_id)
    if not exp:
        return None
    for k, v in kwargs.items():
        if hasattr(exp, k):
            setattr(exp, k, v)
    await session.commit()
    await session.refresh(exp)
    return exp


async def delete(session: AsyncSession, exp_id: str) -> bool:
    exp = await session.get(Experience, exp_id)
    if not exp:
        return False
    await session.delete(exp)
    await session.commit()
    return True


async def search(
    session: AsyncSession,
    *,
    keyword: str = "",
    category: str = "",
    tech_stack: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[Experience]:
    q = select(Experience)

    if keyword:
        like = f"%{keyword}%"
        q = q.where(or_(
            Experience.title.ilike(like),
            Experience.problem.ilike(like),
            Experience.solution.ilike(like),
        ))

    if category:
        q = q.where(Experience.category == category)

    if tech_stack:
        q = q.where(Experience.tech_stack.contains(tech_stack))

    if tags:
        q = q.where(Experience.tags.contains(tags))

    q = q.order_by(Experience.quality_score.desc(), Experience.use_count.desc())
    q = q.offset(offset).limit(limit)

    result = await session.execute(q)
    return list(result.scalars().all())


async def find_relevant(
    session: AsyncSession,
    *,
    task_type: str = "",
    tech_stack: list[str] | None = None,
    keywords: list[str] | None = None,
    domain: str = "",
    type: str = "",
    scope: str = "",
    freshness: str = "",
    access_level_max: int = 3,
    limit: int = 5,
) -> list[Experience]:
    """四层混合检索：tsvector → 关键词 JSONB → LIKE 兜底 → 语义搜索（embedding）

    支持按分类体系（taxonomy）维度过滤：domain, type, scope, freshness。
    检索期严格过滤：非通用经验必须技术栈重叠。
    """
    seen_ids: set[str] = set()
    results: list[Experience] = []

    # 构建通用的 taxonomy + 权限 + 技术栈过滤条件
    def _apply_taxonomy(q):
        if domain:
            q = q.where(Experience.domain == domain)
        if type:
            q = q.where(Experience.type == type)
        if scope:
            q = q.where(Experience.scope == scope)
        if freshness:
            q = q.where(Experience.freshness == freshness)
        q = q.where(Experience.access_level <= access_level_max)
        return q

    def _apply_tech_stack_filter(sql_text: str, params: dict) -> tuple[str, dict]:
        """在 SQL 中附加严格技术栈过滤：通用经验（空数组）或通过重叠检测"""
        if tech_stack:
            # 经验 tech_stack 为空数组 → 通用经验，允许通过
            # 经验 tech_stack 与任务 tech_stack 有重叠 → 允许通过
            ts_list = [t.lower() for t in tech_stack[:5] if t]
            params["tech_filter_arr"] = ts_list
            sql_text += """
                AND (
                    jsonb_array_length(tech_stack) = 0
                    OR tech_stack ?| :tech_filter_arr
                )
            """
        return sql_text, params

    # 第一层：tsvector 全文检索（零 token）
    if keywords:
        terms = [kw for kw in keywords[:5] if len(kw) > 1]
        if terms:
            # 清理 tsquery 非法字符：仅保留字母/数字/CJK/空格，再按空格拆分为原子词
            import re as _re
            _ts_safe_terms = []
            for kw in terms:
                _clean = _re.sub(r'[^\w一-鿿\s]', '', kw).strip()
                for _word in _clean.split():
                    if len(_word) > 1:
                        _ts_safe_terms.append(_word)
            if not _ts_safe_terms:
                # 极端情况：全部被过滤，取每个 term 前 20 个安全字符
                for kw in terms:
                    _w = _re.sub(r'[^\w一-鿿]', '', kw)[:20]
                    if len(_w) > 1:
                        _ts_safe_terms.append(_w)
            tsquery = " | ".join(_ts_safe_terms)
            domain_clause = "AND domain = :domain" if domain else ""
            type_clause = "AND type = :type" if type else ""
            scope_clause = "AND scope = :scope" if scope else ""
            freshness_clause = "AND freshness = :freshness" if freshness else ""
            ts_sql = f"""
                SELECT id, ts_rank(tsv, to_tsquery('simple', :q)) AS rank
                FROM experiences
                WHERE tsv @@ to_tsquery('simple', :q)
                  AND quality_score >= 3.0
                  AND status = 'published'
                  AND access_level <= :al_max
                  {domain_clause} {type_clause} {scope_clause} {freshness_clause}
            """
            params = {"q": tsquery, "lim": limit, "al_max": access_level_max}
            if domain:
                params["domain"] = domain
            if type:
                params["type"] = type
            if scope:
                params["scope"] = scope
            if freshness:
                params["freshness"] = freshness

            ts_sql, params = _apply_tech_stack_filter(ts_sql, params)
            ts_sql += " ORDER BY rank DESC LIMIT :lim"

            try:
                ts_result = await session.execute(text(ts_sql), params)
                for r in ts_result.fetchall():
                    if r.id not in seen_ids:
                        exp = await session.get(Experience, r.id)
                        if exp:
                            results.append(exp)
                            seen_ids.add(r.id)
            except Exception as _ts_err:
                logger.warning("tsquery search failed (non-blocking): %s", _ts_err)
                try:
                    await session.rollback()
                except Exception:
                    pass

    # 第二层：技术栈 + 关键词 JSONB 包含
    if len(results) < limit:
        q = select(Experience).where(
            Experience.quality_score >= 3.0,
            Experience.status == "published",
            Experience.id.notin_(seen_ids) if seen_ids else True,
        )
        q = _apply_taxonomy(q)

        # 严格技术栈过滤：通用经验 或 技术栈重叠
        if tech_stack:
            ts_list = [t.lower() for t in tech_stack[:5] if t]
            if ts_list:
                q = q.where(
                    or_(
                        Experience.tech_stack == [],  # 通用经验
                        or_(*[Experience.tech_stack.contains([t]) for t in ts_list]),
                    )
                )

        conditions = []
        if keywords:
            kw_list = [kw for kw in keywords[:5] if kw and len(str(kw)) > 1]
            if kw_list:
                conditions.append(or_(*[Experience.keywords.contains([kw]) for kw in kw_list]))
        if conditions:
            q = q.where(or_(*conditions))
        q = q.order_by(Experience.quality_score.desc()).limit(limit - len(results))
        kw_result = await session.execute(q)
        for exp in kw_result.scalars():
            if exp.id not in seen_ids:
                results.append(exp)
                seen_ids.add(exp.id)

    # 第三层：LIKE 兜底
    if len(results) < limit and keywords:
        q = select(Experience).where(
            Experience.quality_score >= 3.0,
            Experience.status == "published",
            Experience.id.notin_(seen_ids) if seen_ids else True,
        )
        q = _apply_taxonomy(q)

        # 严格技术栈过滤
        if tech_stack:
            ts_list = [t.lower() for t in tech_stack[:5] if t]
            if ts_list:
                q = q.where(
                    or_(
                        Experience.tech_stack == [],
                        or_(*[Experience.tech_stack.contains([t]) for t in ts_list]),
                    )
                )

        like_conds = []
        for kw in keywords[:3]:
            like = f"%{kw}%"
            like_conds.append(or_(Experience.title.ilike(like), Experience.problem.ilike(like)))
        if like_conds:
            q = q.where(or_(*like_conds))
        q = q.order_by(Experience.quality_score.desc()).limit(limit - len(results))
        like_result = await session.execute(q)
        for exp in like_result.scalars():
            if exp.id not in seen_ids:
                results.append(exp)
                seen_ids.add(exp.id)

    # 第四层：语义搜索（embedding 相似度）
    if len(results) < limit and keywords:
        try:
            from app.services import knowledge_search
            query_text = " ".join(keywords[:5])
            query_vec = await knowledge_search._get_query_embedding(query_text)
            if query_vec:
                fetch_limit = limit - len(results) + len(seen_ids)
                domain_clause = "AND domain = :domain" if domain else ""
                type_clause = "AND type = :type" if type else ""
                scope_clause = "AND scope = :scope" if scope else ""
                freshness_clause = "AND freshness = :freshness" if freshness else ""
                sem_sql = f"""
                    SELECT id, embedding <=> :vec AS distance
                    FROM experiences
                    WHERE embedding IS NOT NULL
                      AND quality_score >= 3.0
                      AND status = 'published'
                      AND access_level <= :al_max
                      {domain_clause} {type_clause} {scope_clause} {freshness_clause}
                """
                params = {"vec": str(query_vec), "lim": fetch_limit, "al_max": access_level_max}
                if domain:
                    params["domain"] = domain
                if type:
                    params["type"] = type
                if scope:
                    params["scope"] = scope
                if freshness:
                    params["freshness"] = freshness

                sem_sql, params = _apply_tech_stack_filter(sem_sql, params)
                sem_sql += " ORDER BY embedding <=> :vec LIMIT :lim"
                params["vec"] = str(query_vec)
                params["lim"] = fetch_limit

                sem_result = await session.execute(text(sem_sql), params)
                for r in sem_result.fetchall():
                    if r.id not in seen_ids:
                        exp = await session.get(Experience, r.id)
                        if exp:
                            results.append(exp)
                            seen_ids.add(exp.id)
                            if len(results) >= limit:
                                break
        except Exception as e:
            logger.debug(f"Semantic search layer skipped: {e}")

    return results[:limit]


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def format_for_context(experiences: list[Experience], max_chars: int = 3000) -> str:
    """将经验列表格式化为可注入 prompt 的文本，总长度不超过 max_chars"""
    if not experiences:
        return ""

    lines = ["## 相关经验\n"]
    total = 0
    for i, exp in enumerate(experiences, 1):
        entry = [f"### {i}. {exp.title}"]
        if exp.problem:
            entry.append(f"问题: {_truncate(exp.problem, 200)}")
        if exp.solution:
            entry.append(f"方案: {_truncate(exp.solution, 300)}")
        if exp.code_snippet:
            entry.append(f"```\n{_truncate(exp.code_snippet, 200)}\n```")
        entry.append("")
        block = "\n".join(entry)
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)
    return "\n".join(lines)


async def record_use(session: AsyncSession, exp_id: str):
    """记录经验被使用，增加使用计数"""
    exp = await session.get(Experience, exp_id)
    if exp:
        exp.use_count = (exp.use_count or 0) + 1
        await session.commit()


# ── 生成期自动贴标：交叉验证与修正 ──

async def validate_and_correct_experience(
    session: AsyncSession,
    exp: Experience,
    project_tech_stack: list[str],
    task_title: str = "",
) -> Experience:
    """验证经验的标签是否合理，自动修正明显错误

    1. 技术栈验证：经验提到的技术必须在项目技术栈中，或者是通用技术
    2. 类别验证：如果代码里有 TODO/FIXME/deprecated，category 应该是 pitfall
    3. 质量分验证：太具体的问题（包含具体业务名词）降分
    4. tech_domain 自动推导
    """
    corrections = []

    # 1. 技术栈验证与修正
    valid_tech = {t.lower() for t in project_tech_stack}
    valid_tech.update(GENERAL_TECH)
    invalid_tech = [t for t in exp.tech_stack if t.lower() not in valid_tech]
    if invalid_tech:
        corrections.append(f"移除无关技术栈: {invalid_tech}")
        exp.tech_stack = [t for t in exp.tech_stack if t.lower() in valid_tech]

    # 代码静态分析补全 tech_stack
    if exp.code_snippet:
        detected = detect_tech_from_code(exp.code_snippet)
        for tech in detected:
            if tech not in [t.lower() for t in exp.tech_stack]:
                if tech in valid_tech:
                    exp.tech_stack.append(tech)
                    corrections.append(f"代码分析补全技术栈: {tech}")

    # 2. 推导 tech_domain
    if exp.tech_stack:
        domains = derive_tech_domains(exp.tech_stack)
        if domains and (not exp.tech_domain or exp.tech_domain not in domains):
            old_domain = exp.tech_domain
            exp.tech_domain = domains[0]
            corrections.append(f"推导技术域: {old_domain or '空'} -> {exp.tech_domain}")
    elif not exp.tech_domain:
        exp.tech_domain = "general"

    # 3. 类别验证：代码含警告信号 → pitfall
    if exp.code_snippet and exp.category != "pitfall":
        warning_signals = ['todo', 'fixme', 'deprecated', 'hack', 'workaround', 'xxx']
        if any(sig in exp.code_snippet.lower() for sig in warning_signals):
            corrections.append(f"类别修正: {exp.category} -> pitfall (代码含警告信号)")
            exp.category = "pitfall"

    # 4. 质量分验证：含业务专属名词降分
    if exp.quality_score > 6:
        # 业务名词可从任务标题推断，或从项目配置读取
        # 简单启发：如果标题含中文业务词且经验 title 也含，可能是业务专属
        business_indicators = ["订单", "支付", "用户", "库存", "商品", "会员", "优惠券"]
        if any(term in exp.title for term in business_indicators):
            exp.quality_score = min(exp.quality_score, 6.0)
            corrections.append("质量分下调 (含业务专属名词)")

    # 5. 质量分下限：重复提取检测
    if exp.source_task_id and exp.quality_score >= 7:
        # 检查同一任务是否已有相似经验
        similar = await session.execute(
            select(Experience).where(
                Experience.source_task_id == exp.source_task_id,
                Experience.id != exp.id,
                Experience.category == exp.category,
            ).limit(1)
        )
        if similar.scalar_one_or_none():
            exp.quality_score = min(exp.quality_score, 6.5)
            corrections.append("质量分下调 (同一任务已有相似经验)")

    if corrections:
        logger.info(f"Experience {exp.id} auto-corrected: {corrections}")
        await session.commit()

    return exp


# ── 提取 Prompt（增强版，含自动贴标约束） ──

EXTRACT_FROM_RETRY_SYSTEM = """你是一个资深技术经理。一个任务经过多次重试后终于成功了。
分析失败原因和最终的解决方案，提取一条可复用的经验记录。

输出 JSON：
{
  "title": "简明标题（问题 + 解法，如：Spring Boot @Transactional 在 private 方法上不生效）",
  "category": "pitfall|debug_pattern|best_practice|performance|security",
  "tech_stack": ["从项目技术栈中选择，不要添加项目未使用的技术"],
  "tags": ["3-5个关键词，必须包含具体技术名和问题类型"],
  "problem": "遇到了什么问题",
  "root_cause": "根本原因是什么",
  "solution": "最终怎么解决的",
  "code_snippet": "关键代码片段（可选，没有就空字符串）",
  "quality_score": 7.0
}

category 选择规则：
- pitfall: 踩坑（"如果这样做会失败"，含反模式、已废弃的写法）
- best_practice: 最佳实践（"推荐这样做"）
- debug_pattern: 调试模式（"如何排查此类问题"）
- performance: 性能优化（"这样做更快/更省内存"）
- security: 安全相关（"这样做有漏洞/更安全"）
- architecture: 架构决策（影响多个模块的设计选择，通常不是单任务经验）

tech_stack 规则（重要）：
- 必须从项目技术栈中选择，不要添加项目未使用的技术
- 如果涉及代码，优先从代码片段中使用的 import/框架判断
- 通用技术（git/http/json）不需要列出
- 如果问题和具体技术无关（如纯算法/设计模式），留空数组 []

tags 规则：
- 必须包含具体技术名（如 pinia, asyncio, sqlalchemy）
- 必须包含问题类型（如 deadlock, memory_leak, race_condition）
- 不要包含宽泛词（如 "bug", "fix", "error"）

quality_score 评分标准：
- 5-6: 一般（问题较具体，但解法有参考价值）
- 7-8: 有价值（问题有代表性，解法可直接复用）
- 9-10: 非常通用（跨项目、跨团队都能复用）

要求：
- 只提取有复用价值的经验，如果问题太特殊（比如纯粹的拼写错误、业务逻辑错误）就返回 {"skip": true}
- title 要具体，让人一看就知道是什么问题
- 不要包含项目专属的业务名词（如具体客户名、内部系统名）"""


async def extract_from_retry(
    session: AsyncSession,
    task_title: str,
    task_description: str,
    error_history: list[str],
    final_result: str,
    retry_count: int,
    used_model: str = "",
    project_name: str = "",
    task_id: str | None = None,
    project_tech_stack: list[str] | None = None,
) -> Experience | None:
    """任务重试成功后，自动提取经验。Opus 踩过的坑 → 下次 DeepSeek 也能解决

    生成期自动贴标：注入项目技术栈上下文，提取后交叉验证。
    """
    errors_text = "\n---\n".join(f"第{i+1}次失败：{e}" for i, e in enumerate(error_history))

    # 获取项目技术栈（如果未传入，尝试从任务/项目获取）
    candidate_stack = list(project_tech_stack or [])
    if task_id and not candidate_stack:
        from app.models import Task, Project
        task = await session.get(Task, task_id)
        if task:
            if task.context and task.context.get("tech_stack"):
                candidate_stack.extend(task.context.get("tech_stack", []))
            project = await session.get(Project, task.project_id)
            if project and project.config and project.config.get("tech_stack"):
                candidate_stack.extend(project.config.get("tech_stack", []))
        candidate_stack = list(dict.fromkeys(candidate_stack))  # 去重保持顺序

    tech_stack_hint = f"项目技术栈：{candidate_stack}" if candidate_stack else "项目技术栈未配置"

    prompt = f"""任务：{task_title}
描述：{task_description}
使用模型：{used_model}
重试次数：{retry_count}

{tech_stack_hint}

失败历史：
{errors_text}

最终成功结果：
{final_result[:2000]}

请提取经验。"""

    try:
        result = await ai_leader._call_json(EXTRACT_FROM_RETRY_SYSTEM, prompt, max_tokens=2048)
    except Exception as e:
        logger.warning(f"Failed to extract experience from retry: {e}")
        return None

    if result.get("skip") or not result.get("title"):
        return None

    exp = await create(
        session,
        title=result["title"],
        category=result.get("category", "pitfall"),
        tech_stack=result.get("tech_stack", []),
        tags=result.get("tags", []),
        problem=result.get("problem", ""),
        root_cause=result.get("root_cause", ""),
        solution=result.get("solution", ""),
        code_snippet=result.get("code_snippet", ""),
        source_project=project_name,
        source_task_id=task_id,
        quality_score=result.get("quality_score", 6.0),
        metadata_={"extracted_from": "retry", "retry_count": retry_count, "model": used_model},
    )

    # 交叉验证与自动修正
    if candidate_stack:
        await validate_and_correct_experience(
            session, exp, candidate_stack, task_title=task_title
        )

    logger.info(f"Auto-extracted experience: {exp.title} (from {retry_count} retries)")
    return exp


EXTRACT_FAILURE_PATTERN_SYSTEM = """你是一个失败模式分析专家。从任务的重试历史中，
提取「什么做法不行」的负样本记录。这比「什么做法行」更有价值——
因为它能直接帮助其他人避免同样的错误。

分析角度：
1. 失败现象：报错信息、异常行为
2. 失败做法：尝试了但导致失败的方法
3. 根本原因：为什么这个做法会失败
4. 与成功做法的对比：成功做法的关键差异是什么

输出 JSON：
{
  "pattern_type": "syntax_error|runtime_error|logic_error|dependency_conflict|config_error|test_failure|performance_issue",
  "tech_stack": ["从项目技术栈中选择"],
  "tags": ["关键词标签"],
  "failure_symptom": "具体的失败现象/错误信息",
  "failed_approach": "尝试了但失败的方法",
  "root_cause": "为什么会失败",
  "successful_approach": "最终成功的方法（简要对比）",
  "quality_score": 6.0
}

tech_stack 规则（重要）：
- 必须从项目技术栈中选择，不要添加项目未使用的技术
- 通用技术不需要列出
- 如果失败和具体技术无关，留空数组 []

要求：
- 如果问题太特殊（比如纯粹的拼写错误）就返回 {"skip": true}
- 优先提取有普遍性的失败模式
- quality_score 根据通用性评分：5-6 一般，7-8 有价值，9-10 非常通用"""


async def extract_failure_pattern_from_retry(
    session: AsyncSession,
    task_title: str,
    task_description: str,
    error_history: list[str],
    final_result: str,
    retry_count: int,
    used_model: str = "",
    project_name: str = "",
    task_id: str | None = None,
    source_experience_id: str | None = None,
    project_tech_stack: list[str] | None = None,
):
    """从 retry 失败历史中自动提取失败模式（负样本）。

    生成期自动贴标：注入项目技术栈上下文。
    """
    if not error_history:
        return None

    errors_text = "\n---\n".join(f"第{i+1}次失败：{e}" for i, e in enumerate(error_history))

    # 获取项目技术栈
    candidate_stack = list(project_tech_stack or [])
    if task_id and not candidate_stack:
        from app.models import Task, Project
        task = await session.get(Task, task_id)
        if task:
            if task.context and task.context.get("tech_stack"):
                candidate_stack.extend(task.context.get("tech_stack", []))
            project = await session.get(Project, task.project_id)
            if project and project.config and project.config.get("tech_stack"):
                candidate_stack.extend(project.config.get("tech_stack", []))
        candidate_stack = list(dict.fromkeys(candidate_stack))

    tech_stack_hint = f"项目技术栈：{candidate_stack}" if candidate_stack else "项目技术栈未配置"

    prompt = f"""任务：{task_title}
描述：{task_description}
使用模型：{used_model}
重试次数：{retry_count}

{tech_stack_hint}

失败历史：
{errors_text}

最终成功结果：
{final_result[:2000]}

请提取失败模式。"""

    try:
        result = await ai_leader._call_json(
            EXTRACT_FAILURE_PATTERN_SYSTEM, prompt, max_tokens=2048,
        )
    except Exception as e:
        logger.warning(f"Failed to extract failure pattern from retry: {e}")
        return None

    if result.get("skip") or not result.get("failure_symptom"):
        return None

    full_text = " ".join([
        result.get("failure_symptom", ""),
        result.get("failed_approach", ""),
        result.get("root_cause", ""),
    ])

    from app.services import knowledge_search

    fp = FailurePattern(
        project_id=task_id,
        task_id=task_id,
        pattern_type=result.get("pattern_type", ""),
        tech_stack=result.get("tech_stack", []),
        tags=result.get("tags", []),
        failure_symptom=result.get("failure_symptom", ""),
        root_cause=result.get("root_cause", ""),
        failed_approach=result.get("failed_approach", ""),
        successful_approach=result.get("successful_approach", ""),
        source_experience_id=source_experience_id,
        quality_score=result.get("quality_score", 5.0),
        keywords=extract_keywords(full_text),
        metadata_={
            "extracted_from": "retry",
            "retry_count": retry_count,
            "model": used_model,
            "task_title": task_title,
        },
    )
    session.add(fp)
    await session.flush()

    # 计算 embedding
    try:
        embedding = await knowledge_search._get_query_embedding(full_text)
        if embedding:
            fp.embedding = embedding
    except Exception as e:
        logger.debug(f"FailurePattern embedding skipped: {e}")

    # 计算 tsv
    try:
        tsv_text = segment_for_tsv(full_text)
        await session.execute(
            text("UPDATE failure_patterns SET tsv = to_tsvector('simple', :tsv_text) WHERE id = :fpid"),
            {"tsv_text": tsv_text, "fpid": fp.id},
        )
    except Exception as e:
        logger.debug(f"FailurePattern tsv skipped: {e}")

    await session.commit()
    await session.refresh(fp)

    # 交叉验证：修正 failure pattern 的技术栈
    if candidate_stack:
        valid_tech = {t.lower() for t in candidate_stack}
        valid_tech.update(GENERAL_TECH)
        invalid = [t for t in fp.tech_stack if t.lower() not in valid_tech]
        if invalid:
            fp.tech_stack = [t for t in fp.tech_stack if t.lower() in valid_tech]
            await session.commit()
            logger.info(f"FailurePattern {fp.id} corrected tech_stack: removed {invalid}")

    logger.info(f"Auto-extracted failure pattern: {fp.failure_symptom[:80]}... (from {retry_count} retries)")
    return fp


SETTLE_SYSTEM = """你是一个资深技术经理。分析项目的任务日志和已知问题，提取可复用的经验。

输出 JSON 数组，每条经验包含：
{
  "title": "简明标题",
  "category": "pitfall|best_practice|code_template|architecture|debug_pattern|performance|security|devops",
  "tech_stack": ["python", "fastapi"],
  "tags": ["async", "database"],
  "problem": "遇到的问题（如果是踩坑类）",
  "root_cause": "根本原因",
  "solution": "解决方案",
  "code_snippet": "关键代码（可选）",
  "quality_score": 5.0
}

tech_stack 规则：
- 只能从项目技术栈中选择
- 不要添加项目未使用的技术
- 如果经验和技术无关，留空数组 []

只提取有复用价值的经验，不要流水账。"""


async def settle_from_project(
    session: AsyncSession,
    project_name: str,
    task_summaries: str,
    known_issues: str = "",
    project_tech_stack: list[str] | None = None,
) -> list[Experience]:
    """项目归档时，由 Leader AI 自动从项目经历中提取经验

    生成期自动贴标：注入项目技术栈上下文。
    """
    tech_hint = f"项目技术栈：{project_tech_stack}" if project_tech_stack else ""
    prompt = f"""项目：{project_name}

{tech_hint}

任务执行摘要：
{task_summaries}

已知问题：
{known_issues}

请提取可复用的经验。"""

    result = await ai_leader._call_json(SETTLE_SYSTEM, prompt, max_tokens=4096)
    items = result.get("items", [result] if "title" in result else [])

    created = []
    for item in items:
        if not item.get("title"):
            continue
        exp = await create(
            session,
            title=item["title"],
            category=item.get("category", "best_practice"),
            tech_stack=item.get("tech_stack", []),
            tags=item.get("tags", []),
            problem=item.get("problem", ""),
            root_cause=item.get("root_cause", ""),
            solution=item.get("solution", ""),
            code_snippet=item.get("code_snippet", ""),
            source_project=project_name,
            quality_score=item.get("quality_score", 5.0),
        )

        # 交叉验证
        if project_tech_stack:
            await validate_and_correct_experience(
                session, exp, project_tech_stack, task_title=project_name
            )

        created.append(exp)
    return created
