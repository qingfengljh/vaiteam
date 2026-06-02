"""
统一知识检索工具 — 跨所有知识源的一站式搜索

支持三种检索模式：
1. 关键字精确匹配（AND/OR/NOT、正则）
2. tsvector 全文检索（jieba 分词，零 token）
3. 语义向量搜索（本地 embedding 优先，云端 fallback）

搜索范围：
- Document（阶段文档）
- Experience（全局经验库）
- ProjectAsset（代码分析 / API 规范）
- TaskDocument（过程文档索引）

返回统一格式的结果列表，按分数排序。
"""

import re
import logging
from dataclasses import dataclass, field
from sqlalchemy import select, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Experience, ProjectAsset, TaskDocument
from app.services.task_docs import extract_keywords, segment_for_tsv

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    id: str
    source: str          # "document" | "experience" | "asset" | "task_document"
    title: str
    summary: str         # 前 200 字摘要
    score: float         # 0-1 归一化分数，越高越相关
    stage: int | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"id": self.id, "source": self.source, "title": self.title,
             "summary": self.summary, "score": round(self.score, 3)}
        if self.stage is not None:
            d["stage"] = self.stage
        if self.metadata:
            d["metadata"] = self.metadata
        return d


async def search(
    session: AsyncSession,
    query: str,
    *,
    project_id: str | None = None,
    sources: list[str] | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    mode: str = "auto",
    access_level_max: int = 3,
    limit: int = 10,
) -> list[SearchResult]:
    """
    统一知识检索入口。

    Args:
        query: 搜索文本
        project_id: 限定项目（None 则搜全局）
        sources: 限定搜索范围，如 ["document", "experience"]，None 搜全部
        category: 按文档类型过滤（plan/spec/design/analysis/task/review/test/deploy/log/meeting）
        tags: 按标签过滤（GIN 倒排索引，AND 逻辑）
        mode: "keyword" | "fulltext" | "semantic" | "auto"（自动组合）
        access_level_max: 经验访问级别上限（0=公开 1=内部 2=敏感 3=机密）
        limit: 最大返回数量

    Returns:
        按 score 降序排列的 SearchResult 列表
    """
    if not query.strip():
        return []

    sources = sources or ["document", "experience", "asset", "task_document"]

    # auto 模式：查询重写 → RRF 融合三种检索结果
    if mode == "auto":
        from app.services.query_rewriter import rewrite_query
        expanded_queries = await rewrite_query(query)
        all_kw: list[list[SearchResult]] = []
        all_ft: list[list[SearchResult]] = []
        all_sem: list[list[SearchResult]] = []
        for q in expanded_queries:
            all_kw.append(await _keyword_search(session, q, project_id, sources, limit * 2, category=category, tags=tags, access_level_max=access_level_max))
            all_ft.append(await _fulltext_search(session, q, project_id, sources, limit * 2, access_level_max=access_level_max))
            all_sem.append(await _semantic_search(session, q, project_id, sources, limit * 2, access_level_max=access_level_max))
        # 合并各查询的结果后 RRF 融合
        kw_merged = _dedup_and_merge(all_kw)
        ft_merged = _dedup_and_merge(all_ft)
        sem_merged = _dedup_and_merge(all_sem)
        return _rrf_fuse([kw_merged, ft_merged, sem_merged])[:limit]

    # 单一模式：保持原有逻辑
    results: dict[str, SearchResult] = {}
    if mode == "keyword":
        kw_results = await _keyword_search(session, query, project_id, sources, limit, category=category, tags=tags, access_level_max=access_level_max)
        for r in kw_results:
            results[f"{r.source}:{r.id}"] = r
    elif mode == "fulltext":
        ft_results = await _fulltext_search(session, query, project_id, sources, limit, access_level_max=access_level_max)
        for r in ft_results:
            results[f"{r.source}:{r.id}"] = r
    elif mode == "semantic":
        sem_results = await _semantic_search(session, query, project_id, sources, limit, access_level_max=access_level_max)
        for r in sem_results:
            results[f"{r.source}:{r.id}"] = r

    sorted_results = sorted(results.values(), key=lambda r: r.score, reverse=True)
    return sorted_results[:limit]


def _dedup_and_merge(result_lists: list[list[SearchResult]]) -> list[SearchResult]:
    """合并多个结果列表，去重并按最高分数保留"""
    best: dict[str, SearchResult] = {}
    for results in result_lists:
        for r in results:
            key = f"{r.source}:{r.id}"
            if key not in best or r.score > best[key].score:
                best[key] = r
    # 按分数降序排列
    return sorted(best.values(), key=lambda r: r.score, reverse=True)


def _rrf_fuse(result_lists: list[list[SearchResult]], k: int = 60) -> list[SearchResult]:
    """
    Reciprocal Rank Fusion: 融合多个有序结果列表。

    score = sum(1 / (k + rank)) for each list where item appears
    k=60 是论文推荐值，平衡高排名敏感度和长尾覆盖。
    """
    scores: dict[str, float] = {}
    result_map: dict[str, SearchResult] = {}

    for results in result_lists:
        for rank, r in enumerate(results, start=1):
            key = f"{r.source}:{r.id}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in result_map:
                result_map[key] = r

    fused: list[SearchResult] = []
    for key, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        r = result_map[key]
        r.score = round(score, 4)
        fused.append(r)

    return fused


def _truncate(text_content: str, max_len: int = 200) -> str:
    if not text_content:
        return ""
    if len(text_content) <= max_len:
        return text_content
    return text_content[:max_len] + "..."


def _parse_query_terms(query: str) -> tuple[list[str], list[str], list[str]]:
    """解析查询中的 AND/OR/NOT 和正则模式。

    支持：
    - `+term`  必须包含（AND）
    - `-term`  必须不包含（NOT）
    - 其他    普通关键词（OR）
    - `/regex/` 正则模式
    """
    must = []
    must_not = []
    should = []

    tokens = query.split()
    for token in tokens:
        if token.startswith("+"):
            must.append(token[1:])
        elif token.startswith("-"):
            must_not.append(token[1:])
        elif token.startswith("/") and token.endswith("/") and len(token) > 2:
            should.append(token)
        else:
            should.append(token)

    return must, must_not, should


def _matches_keyword(text_content: str, must: list[str], must_not: list[str], should: list[str]) -> float:
    """检查文本是否匹配关键字条件，返回 0-1 分数"""
    text_lower = text_content.lower()

    for term in must:
        if term.lower() not in text_lower:
            return 0.0
    for term in must_not:
        if term.lower() in text_lower:
            return 0.0

    if not should:
        return 0.8 if must else 0.0

    matched = 0
    for term in should:
        if term.startswith("/") and term.endswith("/"):
            pattern = term[1:-1]
            try:
                if re.search(pattern, text_content, re.IGNORECASE):
                    matched += 1
            except re.error:
                pass
        elif term.lower() in text_lower:
            matched += 1

    if matched == 0 and not must:
        return 0.0
    base = 0.8 if must else 0.0
    return min(1.0, base + 0.2 * matched / max(len(should), 1))


# ── 关键字搜索 ──

async def _keyword_search(
    session: AsyncSession, query: str, project_id: str | None,
    sources: list[str], limit: int,
    category: str | None = None, tags: list[str] | None = None,
    access_level_max: int = 3,
) -> list[SearchResult]:
    must, must_not, should = _parse_query_terms(query)
    all_terms = must + should
    if not all_terms:
        return []

    results: list[SearchResult] = []

    if "document" in sources:
        q = select(Document).where(Document.status == "approved")
        if project_id:
            q = q.where(Document.project_id == project_id)
        if category:
            q = q.where(Document.category == category)
        if tags:
            q = q.where(Document.tags.contains(tags))
        like_conds = [or_(Document.title.ilike(f"%{t}%"), Document.content.ilike(f"%{t}%")) for t in all_terms[:5]]
        if like_conds:
            q = q.where(or_(*like_conds))
        q = q.limit(limit)
        rows = await session.execute(q)
        for doc in rows.scalars():
            full_text = f"{doc.title} {doc.content}"
            score = _matches_keyword(full_text, must, must_not, should)
            if score > 0:
                results.append(SearchResult(
                    id=doc.id, source="document", title=doc.title,
                    summary=_truncate(doc.content), score=score * 0.9,
                    stage=doc.stage,
                    metadata={"status": doc.status, "version": doc.version, "category": doc.category, "tags": doc.tags},
                ))

    if "experience" in sources:
        q = select(Experience).where(
            Experience.status == "published",
            Experience.access_level <= access_level_max,
        )
        like_conds = [or_(Experience.title.ilike(f"%{t}%"), Experience.problem.ilike(f"%{t}%"),
                          Experience.solution.ilike(f"%{t}%")) for t in all_terms[:5]]
        if like_conds:
            q = q.where(or_(*like_conds))
        q = q.limit(limit)
        rows = await session.execute(q)
        for exp in rows.scalars():
            full_text = f"{exp.title} {exp.problem} {exp.solution}"
            score = _matches_keyword(full_text, must, must_not, should)
            if score > 0:
                results.append(SearchResult(
                    id=exp.id, source="experience", title=exp.title,
                    summary=_truncate(exp.solution or exp.problem), score=score * 0.85,
                    metadata={"category": exp.category, "quality": exp.quality_score, "access_level": exp.access_level},
                ))

    if "asset" in sources and project_id:
        q = select(ProjectAsset).where(
            ProjectAsset.project_id == project_id, ProjectAsset.status == "analyzed",
        )
        rows = await session.execute(q)
        for asset in rows.scalars():
            full_text = f"{asset.filename} {asset.summary}"
            score = _matches_keyword(full_text, must, must_not, should)
            if score > 0:
                results.append(SearchResult(
                    id=asset.id, source="asset", title=f"{asset.asset_type}: {asset.filename}",
                    summary=_truncate(asset.summary), score=score * 0.8,
                    metadata={"type": asset.asset_type, "purpose": asset.purpose},
                ))

    if "task_document" in sources and project_id:
        q = select(TaskDocument).where(TaskDocument.project_id == project_id)
        like_conds = [or_(TaskDocument.title.ilike(f"%{t}%"), TaskDocument.summary.ilike(f"%{t}%")) for t in all_terms[:5]]
        if like_conds:
            q = q.where(or_(*like_conds))
        q = q.limit(limit)
        rows = await session.execute(q)
        for td in rows.scalars():
            full_text = f"{td.title} {td.summary}"
            score = _matches_keyword(full_text, must, must_not, should)
            if score > 0:
                results.append(SearchResult(
                    id=td.id, source="task_document", title=td.title,
                    summary=_truncate(td.summary), score=score * 0.75,
                    metadata={"doc_type": td.doc_type},
                ))

    return results


# ── 全文检索（tsvector） ──

async def _fulltext_search(
    session: AsyncSession, query: str, project_id: str | None,
    sources: list[str], limit: int,
    access_level_max: int = 3,
) -> list[SearchResult]:
    keywords = extract_keywords(query, topk=5)
    if not keywords:
        return []

    terms = [kw for kw in keywords if len(kw) > 1]
    if not terms:
        return []

    # 清理 tsquery 非法字符（同 experience.py）
    import re as _re
    _safe_terms = []
    for kw in terms:
        _clean = _re.sub(r'[^\w一-鿿\s]', '', kw).strip()
        for _word in _clean.split():
            if len(_word) > 1:
                _safe_terms.append(_word)
    if not _safe_terms:
        for kw in terms:
            _w = _re.sub(r'[^\w一-鿿]', '', kw)[:20]
            if len(_w) > 1:
                _safe_terms.append(_w)
    tsquery = " | ".join(_safe_terms)
    results: list[SearchResult] = []

    if "experience" in sources:
        sql = text("""
            SELECT id, ts_rank(tsv, to_tsquery('simple', :q)) AS rank
            FROM experiences
            WHERE tsv @@ to_tsquery('simple', :q)
              AND status = 'published'
              AND access_level <= :al_max
            ORDER BY rank DESC LIMIT :lim
        """)
        try:
            rows = await session.execute(sql, {"q": tsquery, "lim": limit, "al_max": access_level_max})
            for r in rows.fetchall():
                exp = await session.get(Experience, r.id)
                if exp:
                    results.append(SearchResult(
                        id=exp.id, source="experience", title=exp.title,
                        summary=_truncate(exp.solution or exp.problem),
                        score=min(1.0, float(r.rank) * 0.5 + 0.3),
                        metadata={"category": exp.category, "quality": exp.quality_score, "access_level": exp.access_level},
                    ))
        except Exception as _e:
            logger.warning("tsquery experience search failed (non-blocking): %s", _e)
            try:
                await session.rollback()
            except Exception:
                pass

    if "task_document" in sources and project_id:
        pid_clause = "AND project_id = :pid"
        sql = text(f"""
            SELECT id, ts_rank(tsv, to_tsquery('simple', :q)) AS rank
            FROM task_documents
            WHERE tsv @@ to_tsquery('simple', :q) {pid_clause}
            ORDER BY rank DESC LIMIT :lim
        """)
        try:
            rows = await session.execute(sql, {"q": tsquery, "pid": project_id, "lim": limit})
            for r in rows.fetchall():
                td = await session.get(TaskDocument, r.id)
                if td:
                    results.append(SearchResult(
                        id=td.id, source="task_document", title=td.title,
                        summary=_truncate(td.summary),
                        score=min(1.0, float(r.rank) * 0.5 + 0.3),
                        metadata={"doc_type": td.doc_type},
                    ))
        except Exception as _e:
            logger.warning("tsquery task_document search failed (non-blocking): %s", _e)
            try:
                await session.rollback()
            except Exception:
                pass

    return results


# ── 语义向量搜索 ──

async def _get_query_embedding(query_text: str) -> list[float] | None:
    """获取查询文本的 embedding 向量。优先本地 Ollama，fallback 到云端。"""
    embedding = await _local_embedding(query_text)
    if embedding:
        return embedding
    return await _cloud_embedding(query_text)


async def _local_embedding(query_text: str) -> list[float] | None:
    """通过 Ollama embedding 模型生成向量（零 token 成本）"""
    from app.core.config import settings
    if not settings.OLLAMA_ENABLED:
        return None
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key="ollama", base_url=settings.ollama_embedding_url, timeout=30)
        resp = await client.embeddings.create(
            model=settings.OLLAMA_EMBEDDING_MODEL,
            input=query_text[:2000],
        )
        return resp.data[0].embedding
    except Exception as e:
        logger.debug(f"Local embedding failed: {e}")
        return None


async def _cloud_embedding(query_text: str) -> list[float] | None:
    """通过云端 API 生成 embedding（fallback）"""
    try:
        from app.services.task_docs import _get_embedding_client
        client = _get_embedding_client()
        resp = await client.embeddings.create(model="text-embedding-3-small", input=query_text[:8000])
        return resp.data[0].embedding
    except Exception as e:
        logger.debug(f"Cloud embedding failed: {e}")
        return None


async def _semantic_search(
    session: AsyncSession, query: str, project_id: str | None,
    sources: list[str], limit: int,
    access_level_max: int = 3,
) -> list[SearchResult]:
    query_vec = await _get_query_embedding(query)
    if not query_vec:
        return []

    results: list[SearchResult] = []
    vec_str = str(query_vec)

    if "task_document" in sources and project_id:
        sql = text("""
            SELECT id, embedding <=> :vec AS distance
            FROM task_documents
            WHERE project_id = :pid AND embedding IS NOT NULL
            ORDER BY embedding <=> :vec
            LIMIT :lim
        """)
        rows = await session.execute(sql, {"pid": project_id, "vec": vec_str, "lim": limit})
        for r in rows.fetchall():
            td = await session.get(TaskDocument, r.id)
            if td:
                score = max(0.0, 1.0 - float(r.distance))
                if score > 0.2:
                    results.append(SearchResult(
                        id=td.id, source="task_document", title=td.title,
                        summary=_truncate(td.summary), score=score,
                        metadata={"doc_type": td.doc_type, "distance": round(float(r.distance), 4)},
                    ))

    if "experience" in sources:
        sql = text("""
            SELECT id, embedding <=> :vec AS distance
            FROM experiences
            WHERE embedding IS NOT NULL
              AND status = 'published'
              AND access_level <= :al_max
            ORDER BY embedding <=> :vec
            LIMIT :lim
        """)
        rows = await session.execute(sql, {"vec": vec_str, "lim": limit, "al_max": access_level_max})
        for r in rows.fetchall():
            exp = await session.get(Experience, r.id)
            if exp:
                score = max(0.0, 1.0 - float(r.distance))
                if score > 0.2:
                    results.append(SearchResult(
                        id=exp.id, source="experience", title=exp.title,
                        summary=_truncate(exp.solution or exp.problem), score=score,
                        metadata={"category": exp.category, "quality": exp.quality_score, "access_level": exp.access_level},
                    ))

    return results


# ── 便捷入口 ──

async def search_for_context(
    session: AsyncSession, query: str, project_id: str, limit: int = 5,
) -> str:
    """检索后直接格式化为可注入 prompt 的文本"""
    results = await search(session, query, project_id=project_id, limit=limit)
    if not results:
        return ""
    lines = ["## 相关知识检索结果\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. [{r.source}] {r.title}（相关度: {r.score:.0%}）")
        lines.append(r.summary)
        lines.append("")
    return "\n".join(lines)
