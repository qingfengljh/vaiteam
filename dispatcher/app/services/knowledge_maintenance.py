"""
知识维护模块 — 自动降级 + 审计报告

Phase 3-5: 知识自动降级机制
  - 6 个月未被引用 → DEPRECATED
  - valid_until 过期 → DEPRECATED
  - 对应代码已不存在 → ARCHIVED（需外部触发）

Phase 4-4: 知识审计报告
  - 每月自动生成知识库健康度报告
  - 总条目数、过期比例、孤儿条目数、未审核数、零命中条目数
  - 清理建议清单

使用方式：
    from app.services.knowledge_maintenance import auto_deprecate, generate_audit_report

    deprecated = await auto_deprecate(session)
    report = await generate_audit_report(session)
"""

import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Experience, FailurePattern, Document

logger = logging.getLogger(__name__)

# 降级规则配置
DEPRECATE_AFTER_DAYS_UNUSED = 180  # 6 个月未使用
SUSPICIOUS_USE_COUNT_THRESHOLD = 10  # 引用次数低于此值不判定为可疑（样本不足）
SUSPICIOUS_SUCCESS_RATE_THRESHOLD = 0.30  # 成功率低于 30% 标记为可疑


async def auto_deprecate(session: AsyncSession) -> dict:
    """
    自动降级过期知识。

    规则：
    1. valid_until 已过期且当前为 published → DEPRECATED
    2. 超过 DEPRECATE_AFTER_DAYS_UNUSED 天未被引用且当前为 published → DEPRECATED
    3. 当前为 draft/reviewed 且创建超过 90 天仍未 published → archived（清理草稿）

    Returns:
        {"deprecated": int, "archived": int, "flagged_suspicious": int}
    """
    now = datetime.now(timezone.utc)
    deprecated_count = 0
    archived_count = 0
    suspicious_count = 0

    # ── 规则 1: valid_until 过期 ──
    expired_q = await session.execute(
        select(Experience).where(
            Experience.status == "published",
            Experience.valid_until.isnot(None),
            Experience.valid_until < now,
        )
    )
    for exp in expired_q.scalars():
        exp.status = "deprecated"
        logger.info(f"Auto-deprecated (expired): {exp.title} ({exp.id})")
        deprecated_count += 1

    # ── 规则 2: 长期未使用 ──
    cutoff = now - timedelta(days=DEPRECATE_AFTER_DAYS_UNUSED)
    unused_q = await session.execute(
        select(Experience).where(
            Experience.status == "published",
            Experience.use_count == 0,
            Experience.updated_at < cutoff,
        )
    )
    for exp in unused_q.scalars():
        exp.status = "deprecated"
        logger.info(f"Auto-deprecated (unused {DEPRECATE_AFTER_DAYS_UNUSED}d): {exp.title} ({exp.id})")
        deprecated_count += 1

    # ── 规则 3: 长期未发布的草稿/审核中 ──
    draft_cutoff = now - timedelta(days=90)
    draft_q = await session.execute(
        select(Experience).where(
            Experience.status.in_(["draft", "reviewed"]),
            Experience.created_at < draft_cutoff,
        )
    )
    for exp in draft_q.scalars():
        exp.status = "archived"
        logger.info(f"Auto-archived (stale draft): {exp.title} ({exp.id})")
        archived_count += 1

    # ── 规则 4: 标记可疑（低成功率）──
    # 通过 metadata 中的 success_rate 字段判断
    suspicious_q = await session.execute(
        select(Experience).where(
            Experience.status == "published",
            Experience.use_count >= SUSPICIOUS_USE_COUNT_THRESHOLD,
        )
    )
    for exp in suspicious_q.scalars():
        meta = exp.metadata_ or {}
        success_rate = meta.get("success_rate", 1.0)
        if success_rate < SUSPICIOUS_SUCCESS_RATE_THRESHOLD:
            meta["flagged"] = "suspicious_low_success"
            meta["flagged_at"] = now.isoformat()
            meta["success_rate"] = success_rate
            exp.metadata_ = meta
            logger.info(
                f"Flagged suspicious (success_rate={success_rate:.0%}): {exp.title} ({exp.id})"
            )
            suspicious_count += 1

    if deprecated_count or archived_count or suspicious_count:
        await session.commit()
        logger.info(
            f"Auto-deprecate completed: deprecated={deprecated_count}, "
            f"archived={archived_count}, suspicious={suspicious_count}"
        )

    return {
        "deprecated": deprecated_count,
        "archived": archived_count,
        "flagged_suspicious": suspicious_count,
    }


async def generate_audit_report(session: AsyncSession) -> dict:
    """
    生成知识库健康度审计报告。

    Returns:
        {
            "generated_at": str,
            "experiences": {
                "total": int,
                "by_status": {"published": n, "draft": n, ...},
                "by_category": {"pitfall": n, ...},
                "deprecated_ratio": float,
                "zero_hit_count": int,
                "avg_quality_score": float,
                "orphan_count": int,  # 无 tech_stack、无 tags、无 keywords
            },
            "failure_patterns": {
                "total": int,
                "by_type": {"syntax_error": n, ...},
            },
            "documents": {
                "total": int,
                "by_status": {"draft": n, "approved": n, ...},
            },
            "recommendations": [str],
        }
    """
    now = datetime.now(timezone.utc)

    # ── 经验库统计 ──
    total_exp = await session.scalar(select(func.count()).select_from(Experience))

    status_counts = {}
    for status in ["draft", "reviewed", "published", "deprecated", "archived"]:
        cnt = await session.scalar(
            select(func.count()).where(Experience.status == status)
        )
        status_counts[status] = cnt or 0

    category_counts = {}
    cat_rows = await session.execute(
        select(Experience.category, func.count())
        .group_by(Experience.category)
    )
    for cat, cnt in cat_rows.all():
        category_counts[cat] = cnt

    zero_hit = await session.scalar(
        select(func.count()).where(Experience.use_count == 0)
    )

    avg_quality = await session.scalar(
        select(func.avg(Experience.quality_score))
    )

    # 孤儿条目：无 tech_stack、无 tags、无 keywords
    orphan = await session.scalar(
        select(func.count()).where(
            Experience.tech_stack == [],
            Experience.tags == [],
            Experience.keywords == [],
        )
    )

    deprecated_ratio = (
        status_counts.get("deprecated", 0) / total_exp if total_exp else 0.0
    )

    # ── 失败模式统计 ──
    total_fp = await session.scalar(select(func.count()).select_from(FailurePattern))

    fp_type_counts = {}
    fp_rows = await session.execute(
        select(FailurePattern.pattern_type, func.count())
        .group_by(FailurePattern.pattern_type)
    )
    for pt, cnt in fp_rows.all():
        fp_type_counts[pt] = cnt

    # ── 文档统计 ──
    total_doc = await session.scalar(select(func.count()).select_from(Document))

    doc_status_counts = {}
    for status in ["draft", "approved", "rejected"]:
        cnt = await session.scalar(
            select(func.count()).where(Document.status == status)
        )
        doc_status_counts[status] = cnt or 0

    # ── 生成建议 ──
    recommendations: list[str] = []

    if status_counts.get("draft", 0) > 10:
        recommendations.append(
            f"有 {status_counts['draft']} 条草稿经验长期未审核，"
            f"建议启动审核流水线或自动归档。"
        )

    if deprecated_ratio > 0.3:
        recommendations.append(
            f"过期知识占比 {deprecated_ratio:.0%}，建议执行清理或重新审核。"
        )

    if zero_hit > 20:
        recommendations.append(
            f"有 {zero_hit} 条经验从未被引用，建议检查质量或归档。"
        )

    if orphan:
        recommendations.append(
            f"有 {orphan} 条孤儿经验（无技术栈/标签/关键词），建议补充元数据。"
        )

    if not recommendations:
        recommendations.append("知识库整体健康，继续保持。")

    return {
        "generated_at": now.isoformat(),
        "experiences": {
            "total": total_exp or 0,
            "by_status": status_counts,
            "by_category": category_counts,
            "deprecated_ratio": round(deprecated_ratio, 4),
            "zero_hit_count": zero_hit or 0,
            "avg_quality_score": round(float(avg_quality or 0), 2),
            "orphan_count": orphan or 0,
        },
        "failure_patterns": {
            "total": total_fp or 0,
            "by_type": fp_type_counts,
        },
        "documents": {
            "total": total_doc or 0,
            "by_status": doc_status_counts,
        },
        "recommendations": recommendations,
    }


async def detect_conflicts(
    session: AsyncSession,
    new_exp_id: str,
    similarity_threshold: float = 0.75,
) -> list[dict]:
    """
    检测新经验是否与已有 published 经验存在语义冲突。

    策略：
    1. 同类技术栈的经验中，搜索 embedding 相似度高的
    2. 对高相似度的经验，用 LLM 判断是否语义冲突

    Args:
        new_exp_id: 新经验的 ID
        similarity_threshold: embedding 相似度阈值

    Returns:
        冲突列表 [{"exp_id": str, "title": str, "reason": str}]
    """
    new_exp = await session.get(Experience, new_exp_id)
    if not new_exp or not new_exp.embedding:
        return []

    # 查找相似度高的已有经验
    vec_str = str(new_exp.embedding)
    sql = text("""
        SELECT id, embedding <=> :vec AS distance
        FROM experiences
        WHERE id != :eid
          AND status = 'published'
          AND embedding IS NOT NULL
        ORDER BY embedding <=> :vec
        LIMIT 10
    """)
    rows = await session.execute(sql, {"vec": vec_str, "eid": new_exp_id})

    candidates: list[tuple[str, float]] = []
    for r in rows.fetchall():
        sim = max(0.0, 1.0 - float(r.distance))
        if sim >= similarity_threshold:
            candidates.append((r.id, sim))

    if not candidates:
        return []

    # 对高相似度候选，用 LLM 判断冲突
    from app.services import ai_leader

    CONFLICT_CHECK_SYSTEM = """你是一个技术知识库冲突检测专家。
比较以下两条经验，判断它们是否在语义上冲突（即建议的做法相互矛盾）。

输出 JSON：
{
  "conflict": true|false,
  "reason": "冲突原因简述（如果 conflict=true）"
}

注意：仅当建议的做法真正矛盾时才标记冲突。不同角度的补充说明不算冲突。"""

    new_text = f"标题: {new_exp.title}\n问题: {new_exp.problem}\n解决方案: {new_exp.solution}"
    conflicts: list[dict] = []

    for cand_id, sim in candidates:
        cand = await session.get(Experience, cand_id)
        if not cand:
            continue
        cand_text = f"标题: {cand.title}\n问题: {cand.problem}\n解决方案: {cand.solution}"
        prompt = f"【新经验】\n{new_text}\n\n【已有经验】\n{cand_text}"
        try:
            result = await ai_leader._call_json(
                CONFLICT_CHECK_SYSTEM, prompt, max_tokens=512, temperature=0.1,
            )
            if result.get("conflict"):
                conflicts.append({
                    "exp_id": cand_id,
                    "title": cand.title,
                    "similarity": round(sim, 3),
                    "reason": result.get("reason", ""),
                })
                logger.info(
                    f"Conflict detected: new={new_exp_id} vs existing={cand_id} "
                    f"sim={sim:.3f} reason={result.get('reason', '')}"
                )
        except Exception as e:
            logger.debug(f"Conflict check failed for {cand_id}: {e}")

    return conflicts


async def analyze_knowledge_gaps(
    session: AsyncSession,
    project_id: str | None = None,
    min_search_count: int = 3,
) -> list[dict]:
    """
    分析知识缺口：哪些主题被频繁搜索但没有足够经验覆盖。

    通过分析 TaskLog 中 knowledge_search 动作，统计高频搜索但低命中的主题。

    Returns:
        缺口列表 [{"topic": str, "search_count": int, "hit_rate": float,
                   "suggested_action": str}]
    """
    from app.models import TaskLog

    # 搜索知识检索相关的日志
    log_q = await session.execute(
        select(TaskLog).where(
            TaskLog.action.in_([
                "knowledge_search", "experience_search", "find_relevant",
            ]),
        ).order_by(TaskLog.created_at.desc()).limit(500)
    )
    logs = list(log_q.scalars())

    # 统计搜索主题
    from collections import Counter
    search_topics: Counter[str] = Counter()
    hit_counts: dict[str, int] = {}

    for log in logs:
        meta = log.metadata_ or {}
        query = meta.get("query", "")
        if query:
            # 简单提取关键词（取前 2 个词作为主题）
            words = query.split()[:2]
            topic = " ".join(words).lower()
            search_topics[topic] += 1
            hit_counts[topic] = hit_counts.get(topic, 0) + (1 if meta.get("results_count", 0) > 0 else 0)

    gaps: list[dict] = []
    for topic, count in search_topics.most_common(30):
        if count < min_search_count:
            continue
        hits = hit_counts.get(topic, 0)
        hit_rate = hits / count if count > 0 else 0
        if hit_rate < 0.3:  # 命中率低于 30% 视为缺口
            gaps.append({
                "topic": topic,
                "search_count": count,
                "hit_rate": round(hit_rate, 2),
                "suggested_action": f"为 '{topic}' 创建经验记录或补充相关文档",
            })

    logger.info(f"Knowledge gap analysis: found {len(gaps)} gaps from {len(logs)} search logs")
    return gaps


async def record_experience_outcome(
    session: AsyncSession,
    exp_id: str,
    success: bool,
) -> None:
    """
    记录经验的引用结果，用于计算成功率。

    更新 metadata.success_rate 为加权移动平均：
    new_rate = (old_rate * old_count + (1 if success else 0)) / (old_count + 1)
    """
    exp = await session.get(Experience, exp_id)
    if not exp:
        return

    meta = dict(exp.metadata_ or {})
    old_rate = meta.get("success_rate", 1.0)
    old_count = meta.get("success_count", 0) + meta.get("failure_count", 0)

    if success:
        meta["success_count"] = meta.get("success_count", 0) + 1
    else:
        meta["failure_count"] = meta.get("failure_count", 0) + 1

    new_count = old_count + 1
    new_rate = (old_rate * old_count + (1.0 if success else 0.0)) / new_count
    meta["success_rate"] = round(new_rate, 4)

    exp.metadata_ = meta
    await session.commit()
    logger.debug(
        f"Recorded experience outcome: exp={exp_id} success={success} "
        f"rate={new_rate:.2%}"
    )


# ── 经验关联图谱（Phase 4-5）──

async def link_experiences(
    session: AsyncSession,
    exp_id_a: str,
    exp_id_b: str,
    relation: str = "related",
) -> bool:
    """双向关联两条经验。"""
    exp_a = await session.get(Experience, exp_id_a)
    exp_b = await session.get(Experience, exp_id_b)
    if not exp_a or not exp_b:
        return False

    ids_a = list(exp_a.related_exp_ids or [])
    ids_b = list(exp_b.related_exp_ids or [])

    if exp_id_b not in ids_a:
        ids_a.append(exp_id_b)
        exp_a.related_exp_ids = ids_a

    if exp_id_a not in ids_b:
        ids_b.append(exp_id_a)
        exp_b.related_exp_ids = ids_b

    await session.commit()
    logger.info(f"Linked experiences: {exp_id_a} <-> {exp_id_b} ({relation})")
    return True


async def find_related_experiences(
    session: AsyncSession,
    exp_id: str,
    include_similar: bool = True,
    similarity_limit: int = 3,
) -> list[Experience]:
    """
    查找与指定经验相关的其他经验。

    返回顺序：
    1. 已建立关联的经验（related_exp_ids）
    2. embedding 相似度高的经验（如果 include_similar=True）
    """
    exp = await session.get(Experience, exp_id)
    if not exp:
        return []

    related: list[Experience] = []
    seen = {exp_id}

    # 1. 已建立的关联
    for rid in (exp.related_exp_ids or []):
        if rid in seen:
            continue
        re = await session.get(Experience, rid)
        if re and re.status == "published":
            related.append(re)
            seen.add(rid)

    # 2. 通过 embedding 相似度发现
    if include_similar and exp.embedding and len(related) < similarity_limit + 3:
        vec_str = str(exp.embedding)
        sql = text("""
            SELECT id, embedding <=> :vec AS distance
            FROM experiences
            WHERE id != :eid
              AND status = 'published'
              AND embedding IS NOT NULL
            ORDER BY embedding <=> :vec
            LIMIT :lim
        """)
        rows = await session.execute(
            sql, {"vec": vec_str, "eid": exp_id, "lim": similarity_limit},
        )
        for r in rows.fetchall():
            if r.id in seen:
                continue
            sim_exp = await session.get(Experience, r.id)
            if sim_exp:
                related.append(sim_exp)
                seen.add(r.id)

    return related


async def auto_discover_associations(
    session: AsyncSession,
    similarity_threshold: float = 0.80,
    limit: int = 50,
) -> int:
    """
    自动发现经验之间的关联关系。

    对 embedding 相似度超过阈值的 published 经验对，自动建立关联。
    返回新建立的关联数量。
    """
    # 获取所有有 embedding 的 published 经验
    rows = await session.execute(
        select(Experience).where(
            Experience.status == "published",
            Experience.embedding.isnot(None),
        ).limit(limit)
    )
    experiences = list(rows.scalars())

    linked = 0
    for i, exp_a in enumerate(experiences):
        for exp_b in experiences[i + 1:]:
            if not exp_a.embedding or not exp_b.embedding:
                continue
            # 计算余弦相似度（pgvector 的 <=> 是欧氏距离，需转换）
            # 简单方案：用 SQL 计算相似度
            sim_sql = text("""
                SELECT 1 - (:a <=> :b) AS similarity
            """)
            sim_result = await session.execute(
                sim_sql, {"a": str(exp_a.embedding), "b": str(exp_b.embedding)},
            )
            similarity = float(sim_result.scalar() or 0)

            if similarity >= similarity_threshold:
                # 检查是否已关联
                if exp_b.id in (exp_a.related_exp_ids or []):
                    continue
                await link_experiences(session, exp_a.id, exp_b.id)
                linked += 1

    logger.info(f"Auto-discovered {linked} experience associations")
    return linked
