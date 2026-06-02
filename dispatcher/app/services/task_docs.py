"""
过程文档归档与检索 —— 项目专用小型 RAG

写入管线：
  文档产生 → jieba 分词提取关键词（本地零 token）
           → 生成结构化摘要（短文档本地截取，长文档调 LLM）
           → 写 Markdown 文件（Git 可追踪）
           → 存 DB 索引（keywords, tags, tsvector）
           → 异步生成 embedding（pgvector）

检索管线：
  查询文本 → jieba 分词
           → tsvector 全文检索粗筛（零 token，毫秒级）
           → pgvector 向量精排（1 次 embedding API 调用）
           → 返回摘要 + 文件路径（AI 按需读原文）

文档类型（doc_type）：
  architecture_decision  架构师决策（最重要的资产）
  task_instruction       任务指令
  task_report            执行报告
  error_log              错误/重试记录
  escalation_record      升级记录
  code_review            代码审查结果
  stage_document         阶段文档
  requirement_draft      深聊提炼的需求草案
  design_draft           深聊提炼的详细设计草案（可含 mermaid）
  prototype_spec         原型规格（结构化 UI spec）
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import jieba
import jieba.analyse
from sqlalchemy import select, or_, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import TaskDocument

logger = logging.getLogger(__name__)

# pgvector cosine distance（<=>）：越小越相似；用于过滤弱语义命中（text-embedding-3-small 经验值）
VECTOR_COSINE_MAX_BALANCED = 0.62
VECTOR_COSINE_MAX_STRICT = 0.48

# jieba 初始化时加载一次，后续调用极快
jieba.setLogLevel(logging.WARNING)

STOPWORDS = frozenset(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这 "
    "他 她 它 们 那 被 从 把 让 用 对 为 与 等 但 而 或 如果 因为 所以 可以 这个 那个 什么 怎么 "
    "需要 使用 进行 通过 实现 支持 包含 提供 处理 完成 创建 定义 配置 设置 添加 修改 删除".split()
)


# ── 本地分词与关键词提取 ──

def extract_keywords(text_content: str, topk: int = 20) -> list[str]:
    """jieba TF-IDF 提取关键词，本地执行，零 API 调用"""
    clean = re.sub(r'[#`*\-_=\[\](){}|\\/<>]', ' ', text_content)
    keywords = jieba.analyse.extract_tags(clean, topK=topk, withWeight=False)
    return [kw for kw in keywords if kw not in STOPWORDS and len(kw) > 1]


def segment_for_tsv(text_content: str) -> str:
    """jieba 分词后用空格连接，用于 PostgreSQL tsvector"""
    clean = re.sub(r'[#`*\-_=\[\](){}|\\/<>]', ' ', text_content)
    words = jieba.cut(clean, cut_all=False)
    return " ".join(w.strip() for w in words if w.strip() and len(w.strip()) > 1 and w not in STOPWORDS)


def search_query_terms(query: str) -> list[str]:
    """检索用分词：与 tsv 构建一致；英文无空格时做退化切分。"""
    q = (query or "").strip()
    if not q:
        return []
    segmented = segment_for_tsv(q)
    terms = [t for t in segmented.split() if t]
    if not terms:
        parts = re.split(r"\s+", q)
        terms = [p.strip() for p in parts if len(p.strip()) >= 2][:20]
    return terms[:20]


def _sanitize_tsquery_token(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return ""
    for ch in ':|&!()*\'"':
        t = t.replace(ch, " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _build_tsquery_string(terms: list[str], match_mode: str) -> str:
    """构造 to_tsquery('simple', ...) 字符串。balanced/strict 倾向 AND 收紧；broad 保持 OR。"""
    safe: list[str] = []
    for t in terms:
        st = _sanitize_tsquery_token(t)
        if len(st) < 2:
            continue
        safe.append(st)
    if not safe:
        return ""
    mode = (match_mode or "balanced").strip().lower()
    if mode == "broad":
        return " | ".join(safe)
    if mode == "strict":
        return " & ".join(safe)
    if len(safe) >= 2:
        return " & ".join(safe)
    return safe[0]


def generate_summary_local(content: str, doc_type: str = "", max_len: int = 300) -> str:
    """本地生成摘要：提取首段 + 关键词。短文档直接截取，不调 API"""
    lines = [l.strip() for l in content.split("\n") if l.strip() and not l.startswith("#")]
    body = " ".join(lines)[:max_len * 2]

    keywords = extract_keywords(content, topk=8)
    kw_str = "、".join(keywords) if keywords else ""

    prefix = f"[{doc_type}] " if doc_type else ""
    summary = f"{prefix}{body[:max_len]}"
    if kw_str:
        summary += f"\n关键词: {kw_str}"
    return summary


# ── 文件系统 ──

def _doc_dir(project_id: str, iteration_seq: int | str = "default", task_ref_id: str = "") -> Path:
    base = Path(settings.PROJECTS_DIR) / project_id / "docs" / f"iter-{iteration_seq}"
    if task_ref_id:
        base = base / "tasks" / task_ref_id
    return base


def _safe_filename(doc_type: str, title: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in title[:40]).strip("_")
    return f"{ts}_{doc_type}_{slug}.md"


# ── 写入 ──

async def archive(
    session: AsyncSession,
    *,
    project_id: str,
    iteration_id: str | None = None,
    iteration_seq: int | str = "default",
    task_id: str | None = None,
    ref_id: str = "",
    doc_type: str,
    title: str,
    content: str,
    summary: str = "",
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> TaskDocument:
    """归档一篇文档：写文件 → 分词提取关键词 → 建索引 → 异步 embedding"""
    dir_path = _doc_dir(project_id, iteration_seq, ref_id)
    dir_path.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(doc_type, title)
    file_path = dir_path / filename
    file_path.write_text(content, encoding="utf-8")
    rel_path = str(file_path.relative_to(Path(settings.PROJECTS_DIR)))

    keywords = extract_keywords(content)
    if not summary:
        summary = generate_summary_local(content, doc_type)
    sum_plain = (summary or "").strip()
    tsv_text = segment_for_tsv(f"{title} {sum_plain} {content}")

    all_tags = list(set((tags or []) + keywords[:5]))

    doc = TaskDocument(
        project_id=project_id,
        iteration_id=iteration_id,
        task_id=task_id,
        ref_id=ref_id,
        doc_type=doc_type,
        title=title,
        summary=summary,
        file_path=rel_path,
        tags=all_tags,
        keywords=keywords,
        metadata_=metadata or {},
    )
    session.add(doc)
    await session.flush()

    # tsvector 用 SQL 直接写入（simple config 不做语言特定处理，适合中文分词后的结果）
    await session.execute(
        text("UPDATE task_documents SET tsv = to_tsvector('simple', :tsv_text) WHERE id = :doc_id"),
        {"tsv_text": tsv_text, "doc_id": doc.id},
    )
    await session.commit()
    await session.refresh(doc)

    logger.info(f"Archived [{doc_type}] {title} -> {rel_path} ({len(keywords)} keywords)")
    return doc


async def archive_architecture_decisions(
    session: AsyncSession,
    *,
    project_id: str,
    iteration_id: str | None = None,
    iteration_seq: int | str = "default",
    decisions: list[dict],
) -> list[TaskDocument]:
    """批量归档架构师决策"""
    if not decisions:
        return []

    docs = []
    lines = ["# 架构决策记录\n"]
    for i, d in enumerate(decisions, 1):
        topic = d.get("topic", f"决策{i}")
        decision = d.get("decision", "")
        rationale = d.get("rationale", "")

        content = f"# {topic}\n\n## 决策\n{decision}\n\n## 原因\n{rationale}\n"
        lines.append(f"## {i}. {topic}\n- **决策**: {decision}\n- **原因**: {rationale}\n")

        doc = await archive(
            session, project_id=project_id, iteration_id=iteration_id,
            iteration_seq=iteration_seq, doc_type="architecture_decision",
            title=topic, content=content,
            tags=["architecture", "decision"],
            metadata=d,
        )
        docs.append(doc)

    await archive(
        session, project_id=project_id, iteration_id=iteration_id,
        iteration_seq=iteration_seq, doc_type="architecture_decision",
        title="架构决策汇总", content="\n".join(lines),
        tags=["architecture", "decision", "summary"],
    )
    return docs


# ── Embedding（异步后台，不阻塞主流程） ──

def _get_embedding_client():
    """获取 embedding 用的 client，优先走 model_pool，fallback 到 .env"""
    from openai import AsyncOpenAI
    try:
        from app.services import model_pool
        client, _ = model_pool.get_client("text-embedding-3-small")
        return client
    except (ValueError, KeyError):
        pass
    raise ValueError("No embedding provider configured. Please add a provider in Settings.")


async def generate_embedding(session: AsyncSession, doc_id: str):
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        return

    embed_text = f"{doc.title}\n{doc.summary}\n{' '.join(doc.keywords or [])}"
    try:
        client = _get_embedding_client()
        resp = await client.embeddings.create(model="text-embedding-3-small", input=embed_text[:8000])
        doc.embedding = resp.data[0].embedding
        await session.commit()
    except Exception as e:
        logger.warning(f"Embedding failed for {doc_id}: {e}")


async def backfill_embeddings(session: AsyncSession, project_id: str | None = None, limit: int = 50) -> int:
    q = select(TaskDocument).where(TaskDocument.embedding.is_(None))
    if project_id:
        q = q.where(TaskDocument.project_id == project_id)
    q = q.limit(limit)
    result = await session.execute(q)
    count = 0
    for doc in result.scalars():
        await generate_embedding(session, doc.id)
        count += 1
    return count


# ── 检索层 ──

async def search_fulltext(
    session: AsyncSession,
    *,
    project_id: str,
    query: str,
    doc_type: str = "",
    limit: int = 20,
    match_mode: str = "balanced",
) -> list[tuple[TaskDocument, float]]:
    """tsvector 全文检索：分词 → tsquery（balanced/strict 多词 AND）→ 按 rank 排序，返回 (文档, rank)。"""
    terms = search_query_terms(query)
    tsquery = _build_tsquery_string(terms, match_mode)
    if not tsquery:
        return []

    type_clause = "AND doc_type = :dtype" if doc_type else ""
    sql = text(f"""
        SELECT id, ts_rank(tsv, to_tsquery('simple', :q)) AS rank
        FROM task_documents
        WHERE project_id = :pid AND tsv @@ to_tsquery('simple', :q) {type_clause}
        ORDER BY rank DESC
        LIMIT :lim
    """)
    params: dict = {"pid": project_id, "q": tsquery, "lim": limit}
    if doc_type:
        params["dtype"] = doc_type

    result = await session.execute(sql, params)
    rows = result.fetchall()
    if not rows:
        return []

    ids = [r.id for r in rows]
    docs_q = await session.execute(select(TaskDocument).where(TaskDocument.id.in_(ids)))
    docs_map = {d.id: d for d in docs_q.scalars()}
    out: list[tuple[TaskDocument, float]] = []
    for r in rows:
        d = docs_map.get(r.id)
        if not d:
            continue
        rk = float(r.rank) if getattr(r, "rank", None) is not None else 0.0
        out.append((d, rk))
    return out


async def search_by_keyword(
    session: AsyncSession,
    *,
    project_id: str,
    keyword: str = "",
    doc_type: str = "",
    tags: list[str] | None = None,
    limit: int = 20,
    match_all_tokens: bool = False,
) -> list[TaskDocument]:
    """LIKE + 标签/关键词 JSONB 过滤。match_all_tokens 时每个分词须同时在标题或摘要中出现（AND）。"""
    q = select(TaskDocument).where(TaskDocument.project_id == project_id)
    raw = (keyword or "").strip()
    if raw:
        use_and = match_all_tokens
        if use_and:
            terms = search_query_terms(raw) or ([raw[:64]] if len(raw) >= 2 else [])
            for t in terms[:12]:
                like = f"%{t}%"
                q = q.where(or_(TaskDocument.title.ilike(like), TaskDocument.summary.ilike(like)))
        else:
            like = f"%{raw[:120]}%"
            q = q.where(or_(TaskDocument.title.ilike(like), TaskDocument.summary.ilike(like)))
    if doc_type:
        q = q.where(TaskDocument.doc_type == doc_type)
    if tags:
        q = q.where(TaskDocument.tags.contains(tags))
    q = q.order_by(TaskDocument.created_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars())


async def search_by_vector(
    session: AsyncSession,
    *,
    project_id: str,
    query_text: str,
    doc_type: str = "",
    limit: int = 10,
    max_cosine_distance: float | None = None,
) -> list[dict]:
    """pgvector 语义检索；可选按余弦距离上限过滤弱命中。"""
    try:
        client = _get_embedding_client()
        resp = await client.embeddings.create(model="text-embedding-3-small", input=query_text[:8000])
        query_vec = resp.data[0].embedding
    except Exception as e:
        logger.warning(f"Embedding query failed, fallback to fulltext: {e}")
        pairs = await search_fulltext(
            session, project_id=project_id, query=query_text, doc_type=doc_type, limit=limit, match_mode="balanced",
        )
        return [_doc_dict(d) for d, _ in pairs]

    type_clause = "AND doc_type = :dtype" if doc_type else ""
    dist_filter = ""
    params: dict = {"pid": project_id, "vec": str(query_vec), "lim": limit}
    if max_cosine_distance is not None:
        dist_filter = " AND (embedding <=> :vec) <= :dmax "
        params["dmax"] = float(max_cosine_distance)

    sql = text(f"""
        SELECT id, project_id, iteration_id, task_id, ref_id, doc_type,
               title, summary, file_path, tags, keywords, metadata, created_at,
               embedding <=> :vec AS distance
        FROM task_documents
        WHERE project_id = :pid AND embedding IS NOT NULL {type_clause}
        {dist_filter}
        ORDER BY embedding <=> :vec
        LIMIT :lim
    """)
    if doc_type:
        params["dtype"] = doc_type

    result = await session.execute(sql, params)
    rows = result.fetchall()
    docs = []
    for r in rows:
        meta = r.metadata if isinstance(r.metadata, dict) else {}
        rmeta = meta.get("vue3_readiness") if isinstance(meta.get("vue3_readiness"), dict) else {}
        score = rmeta.get("score") if isinstance(rmeta.get("score"), int) else _readiness_score_from_tags(r.tags)
        grade = str(rmeta.get("grade") or _readiness_grade_from_tags(r.tags))
        doc_mid = meta.get("document_id")
        docs.append({
            "id": r.id, "project_id": r.project_id, "iteration_id": r.iteration_id,
            "task_id": r.task_id, "ref_id": r.ref_id, "doc_type": r.doc_type,
            "title": r.title, "summary": r.summary, "file_path": r.file_path,
            "tags": r.tags, "keywords": r.keywords, "distance": round(r.distance, 4),
            "readiness_score": score, "readiness_grade": grade,
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "document_id": doc_mid if isinstance(doc_mid, str) else None,
        })
    return docs


async def search_hybrid(
    session: AsyncSession,
    *,
    project_id: str,
    query: str,
    doc_type: str = "",
    readiness_band: str = "",
    sort_by: str = "relevance",
    limit: int = 10,
    match_mode: str = "balanced",
) -> list[dict]:
    """三层混合检索：tsvector 全文 → pgvector 语义 → LIKE 兜底"""
    q = (query or "").strip()
    if not q:
        return []

    mode = (match_mode or "balanced").strip().lower()
    if mode not in ("broad", "balanced", "strict"):
        mode = "balanced"

    eff_sort = (sort_by or "relevance").strip().lower()
    if eff_sort not in ("created_at", "readiness_desc", "readiness_asc", "relevance"):
        eff_sort = "relevance"

    seen: dict[str, dict] = {}
    ft_limit = max(limit * 4, limit, 20)

    ft_docs = await search_fulltext(
        session, project_id=project_id, query=q, doc_type=doc_type, limit=ft_limit, match_mode=mode,
    )
    for d, rank in ft_docs:
        if d.id not in seen:
            dd = _doc_dict(d)
            dd["_source"] = "fulltext"
            dd["_rank"] = float(rank)
            seen[d.id] = dd

    vmax: float | None = None
    if mode == "balanced":
        vmax = VECTOR_COSINE_MAX_BALANCED
    elif mode == "strict":
        vmax = VECTOR_COSINE_MAX_STRICT

    if len(seen) < limit:
        vec_lim = max(limit * 3, 15)
        vec_results = await search_by_vector(
            session,
            project_id=project_id,
            query_text=q,
            doc_type=doc_type,
            limit=vec_lim,
            max_cosine_distance=vmax,
        )
        for r in vec_results:
            rid = r["id"]
            dist = r.get("distance")
            if rid not in seen:
                r["_source"] = "vector"
                seen[rid] = r
            elif isinstance(dist, (int, float)):
                seen[rid]["_vec_distance"] = float(dist)

    need_kw = (mode == "broad" and len(seen) < max(1, limit // 2)) or (mode != "broad" and len(seen) < limit)
    if need_kw:
        kw_docs = await search_by_keyword(
            session,
            project_id=project_id,
            keyword=q[:500],
            doc_type=doc_type,
            limit=max(limit * 2, 20),
            match_all_tokens=(mode != "broad"),
        )
        for d in kw_docs:
            if d.id not in seen:
                dd = _doc_dict(d)
                dd["_source"] = "keyword"
                seen[d.id] = dd

    results = list(seen.values())
    results = _filter_docs_by_readiness_band(results, readiness_band)
    results = _sort_docs_by_mode(results, eff_sort)
    return results[:limit]


# ── 关联发现 ──

async def find_related(
    session: AsyncSession,
    *,
    doc_id: str,
    limit: int = 5,
) -> list[dict]:
    """通过共享关键词 + 向量相似度找到相关文档"""
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        return []

    results: dict[str, dict] = {}

    # 关键词重叠（JSONB @> 匹配）
    if doc.keywords:
        from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
        from sqlalchemy import type_coerce, literal
        kw_conditions = [
            TaskDocument.keywords.op("@>")(type_coerce([k], PG_JSONB))
            for k in doc.keywords[:5]
        ]
        kw_docs = await session.execute(
            select(TaskDocument).where(
                TaskDocument.project_id == doc.project_id,
                TaskDocument.id != doc.id,
                or_(*kw_conditions),
            ).limit(limit)
        )
        for d in kw_docs.scalars():
            overlap = set(d.keywords or []) & set(doc.keywords or [])
            dd = _doc_dict(d)
            dd["relation"] = "keyword_overlap"
            dd["shared_keywords"] = list(overlap)
            results[d.id] = dd

    # 向量相似
    if doc.embedding is not None and len(results) < limit:
        vec_sql = text("""
            SELECT id, embedding <=> (SELECT embedding FROM task_documents WHERE id = :did) AS distance
            FROM task_documents
            WHERE project_id = :pid AND id != :did AND embedding IS NOT NULL
            ORDER BY distance
            LIMIT :lim
        """)
        vec_rows = await session.execute(vec_sql, {"did": doc_id, "pid": doc.project_id, "lim": limit})
        for r in vec_rows.fetchall():
            if r.id not in results:
                related_doc = await session.get(TaskDocument, r.id)
                if related_doc:
                    dd = _doc_dict(related_doc)
                    dd["relation"] = "semantic_similar"
                    dd["distance"] = round(r.distance, 4)
                    results[r.id] = dd

    return list(results.values())[:limit]


# ── 格式化（注入 AI prompt） ──

def format_for_context(docs: list[dict], max_chars: int = 4000) -> str:
    """将检索结果格式化为可注入 prompt 的文本，节省 token"""
    if not docs:
        return ""
    lines = ["## 相关过程文档\n"]
    total = 0
    for i, d in enumerate(docs, 1):
        kw = "、".join((d.get("keywords") or d.get("tags") or [])[:5])
        entry = (
            f"### {i}. [{d.get('doc_type', '')}] {d.get('title', '')}\n"
            f"{d.get('summary', '')[:200]}\n"
            f"关键词: {kw}\n"
        )
        if total + len(entry) > max_chars:
            lines.append(f"\n... 还有 {len(docs) - i + 1} 篇相关文档")
            break
        lines.append(entry)
        total += len(entry)
    return "\n".join(lines)


# ── 读取原文 ──

def read_doc_content(file_path: str) -> str:
    full_path = Path(settings.PROJECTS_DIR) / file_path
    if not full_path.exists():
        return ""
    return full_path.read_text(encoding="utf-8")


# ── 目录浏览 ──


def _readiness_score_from_tags(tags: list[str] | None) -> int | None:
    raw = next((t for t in (tags or []) if t.startswith("readiness_score_")), "")
    if not raw:
        return None
    try:
        return int(raw.replace("readiness_score_", ""))
    except Exception:
        return None


def _readiness_grade_from_tags(tags: list[str] | None) -> str:
    arr = tags or []
    if "prod_ready" in arr:
        return "ready"
    if "prod_almost_ready" in arr:
        return "almost_ready"
    if "prod_needs_work" in arr:
        return "needs_work"
    return ""


def _filter_docs_by_readiness_band(docs: list[dict], readiness_band: str) -> list[dict]:
    band = (readiness_band or "").strip().lower()
    if band == "ready":
        return [d for d in docs if "prod_ready" in (d.get("tags") or [])]
    if band == "almost":
        return [d for d in docs if "prod_almost_ready" in (d.get("tags") or [])]
    if band == "needs":
        return [d for d in docs if "prod_needs_work" in (d.get("tags") or [])]
    return docs


def _sort_docs_by_mode(docs: list[dict], sort_by: str) -> list[dict]:
    if sort_by == "readiness_desc":
        docs.sort(key=lambda d: (d.get("readiness_score") is None, -(d.get("readiness_score") or 0), d.get("created_at") or ""))
    elif sort_by == "readiness_asc":
        docs.sort(key=lambda d: (d.get("readiness_score") is None, d.get("readiness_score") or 0, d.get("created_at") or ""))
    elif sort_by == "created_at":
        docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    elif sort_by == "relevance":
        def _rel_key(d: dict):
            src = d.get("_source") or ""
            rnk = float(d.get("_rank") or 0.0)
            dist = d.get("_vec_distance")
            if dist is None:
                dist = d.get("distance")
            dist_f = float(dist) if isinstance(dist, (int, float)) else 2.0
            tier = 0 if src == "fulltext" else (1 if src == "vector" else 2)
            return (tier, -rnk if src == "fulltext" else 0.0, dist_f)

        docs.sort(key=_rel_key)
    return docs

async def list_docs(
    session: AsyncSession,
    *,
    project_id: str,
    iteration_id: str | None = None,
    task_id: str | None = None,
    doc_type: str = "",
    readiness_band: str = "",
    sort_by: str = "created_at",
    limit: int = 100,
) -> list[dict]:
    q = select(TaskDocument).where(TaskDocument.project_id == project_id)
    if iteration_id:
        q = q.where(TaskDocument.iteration_id == iteration_id)
    if task_id:
        q = q.where(TaskDocument.task_id == task_id)
    if doc_type:
        q = q.where(TaskDocument.doc_type == doc_type)
    band = (readiness_band or "").strip().lower()
    if band == "ready":
        q = q.where(TaskDocument.tags.contains(["prod_ready"]))
    elif band == "almost":
        q = q.where(TaskDocument.tags.contains(["prod_almost_ready"]))
    elif band == "needs":
        q = q.where(TaskDocument.tags.contains(["prod_needs_work"]))

    fetch_size = max(limit, 300) if sort_by in ("readiness_desc", "readiness_asc") else limit
    q = q.order_by(TaskDocument.created_at.desc()).limit(fetch_size)
    result = await session.execute(q)
    docs = [_doc_dict(d) for d in result.scalars()]
    return _sort_docs_by_mode(docs, sort_by)[:limit]


async def get_doc_stats(session: AsyncSession, project_id: str) -> dict:
    """文档统计：按类型计数"""
    result = await session.execute(
        select(TaskDocument.doc_type, func.count(TaskDocument.id))
        .where(TaskDocument.project_id == project_id)
        .group_by(TaskDocument.doc_type)
    )
    type_counts = {r[0]: r[1] for r in result.fetchall()}

    total = sum(type_counts.values())
    embedded = await session.execute(
        select(func.count(TaskDocument.id))
        .where(TaskDocument.project_id == project_id, TaskDocument.embedding.isnot(None))
    )
    embedded_count = embedded.scalar() or 0

    return {
        "total": total,
        "embedded": embedded_count,
        "by_type": type_counts,
    }


def _doc_dict(d: TaskDocument) -> dict:
    meta = d.metadata_ or {}
    rmeta = meta.get("vue3_readiness") if isinstance(meta.get("vue3_readiness"), dict) else {}
    score = rmeta.get("score") if isinstance(rmeta.get("score"), int) else _readiness_score_from_tags(d.tags)
    grade = str(rmeta.get("grade") or _readiness_grade_from_tags(d.tags))
    doc_mid = meta.get("document_id")
    return {
        "id": d.id, "project_id": d.project_id, "iteration_id": d.iteration_id,
        "task_id": d.task_id, "ref_id": d.ref_id, "doc_type": d.doc_type,
        "title": d.title, "summary": d.summary[:300], "file_path": d.file_path,
        "tags": d.tags, "keywords": d.keywords,
        "readiness_score": score,
        "readiness_grade": grade,
        "git_path": meta.get("git_path", ""),
        "git_synced": bool(meta.get("git_synced", False)),
        "created_at": d.created_at.isoformat() if d.created_at else "",
        "document_id": doc_mid if isinstance(doc_mid, str) else None,
    }
