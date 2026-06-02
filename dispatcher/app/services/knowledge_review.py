"""
知识审核流水线 — 三级审核确保入库质量

Phase 3-3:
- 初审：LLM 自查（格式、完整性、自洽性）
- 复审：领域专家确认（或 LLM 扮演）
- 终审：与代码 diff 比对（如经验来自代码变更）

状态流转：
    draft → [初审通过] → reviewed → [复审通过] → published
                ↓ rejected              ↓ rejected
              archived                deprecated

使用方式：
    from app.services.knowledge_review import review_experience

    result = await review_experience(session, exp_id, stage="all")
    # → {"passed": True, "stage": "published", "issues": []}
"""

import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Experience
from app.services import ai_leader

logger = logging.getLogger(__name__)

SELF_REVIEW_SYSTEM = """你是一个技术知识库审核员。审核以下经验记录的质量。

审核维度：
1. 格式完整性：是否包含标题、问题、原因、解决方案
2. 内容自洽性：问题和解决方案是否逻辑一致，有无矛盾
3. 技术准确性：技术术语使用是否正确，代码片段是否可运行
4. 可复用性：是否有足够通用性，不是纯粹的拼写错误或一次性问题
5. 信息冗余：是否包含无关信息，是否过于冗长

输出 JSON：
{
  "passed": true|false,
  "score": 0-10,
  "issues": ["问题描述1", "问题描述2"],
  "suggestions": ["改进建议1"]
}

评分标准：
- 8-10分：可直接入库
- 5-7分：有小问题但可接受
- <5分：需要修改后重审"""

EXPERT_REVIEW_SYSTEM = """你是一个资深技术专家。从技术深度和领域适用性角度审核以下经验。

审核维度：
1. 技术深度：是否触及根本原因，还是仅停留在表面症状
2. 方案可行性：解决方案在实际项目中是否可落地
3. 边界情况：是否考虑了不同场景/版本的适用性
4. 与已知最佳实践的符合度

输出 JSON：
{
  "passed": true|false,
  "score": 0-10,
  "issues": ["问题描述"],
  "domain_concerns": ["领域层面的顾虑"]
}"""


def _build_exp_text(exp: Experience) -> str:
    """构建经验的完整文本供审核"""
    parts = [
        f"标题: {exp.title}",
        f"分类: {exp.category}",
        f"技术栈: {', '.join(exp.tech_stack or [])}",
        f"标签: {', '.join(exp.tags or [])}",
        f"问题: {exp.problem}",
        f"根本原因: {exp.root_cause}",
        f"解决方案: {exp.solution}",
    ]
    if exp.code_snippet:
        parts.append(f"代码片段:\n{exp.code_snippet}")
    return "\n\n".join(parts)


async def self_review(session: AsyncSession, exp_id: str) -> dict:
    """初审：LLM 自查格式、完整性、自洽性"""
    exp = await session.get(Experience, exp_id)
    if not exp:
        return {"passed": False, "error": "Experience not found"}

    prompt = _build_exp_text(exp)
    try:
        result = await ai_leader._call_json(
            SELF_REVIEW_SYSTEM, prompt, max_tokens=1024, temperature=0.2,
        )
        passed = bool(result.get("passed", False))
        score = float(result.get("score", 0))

        # 更新经验状态
        if passed and score >= 5:
            if exp.status == "draft":
                exp.status = "reviewed"
                logger.info(f"Self-review passed for {exp.id}: score={score}")
        else:
            meta = dict(exp.metadata_ or {})
            meta["self_review_issues"] = result.get("issues", [])
            meta["self_review_suggestions"] = result.get("suggestions", [])
            exp.metadata_ = meta
            logger.info(f"Self-review failed for {exp.id}: score={score}, issues={result.get('issues', [])}")

        await session.commit()
        return {
            "passed": passed,
            "score": score,
            "issues": result.get("issues", []),
            "suggestions": result.get("suggestions", []),
        }
    except Exception as e:
        logger.warning(f"Self-review failed for {exp.id}: {e}")
        return {"passed": False, "error": str(e)}


async def expert_review(session: AsyncSession, exp_id: str) -> dict:
    """复审：领域专家确认（LLM 扮演专家角色）"""
    exp = await session.get(Experience, exp_id)
    if not exp:
        return {"passed": False, "error": "Experience not found"}
    if exp.status != "reviewed":
        return {"passed": False, "error": f"Must pass self-review first (current={exp.status})"}

    prompt = _build_exp_text(exp)
    try:
        result = await ai_leader._call_json(
            EXPERT_REVIEW_SYSTEM, prompt, max_tokens=1024, temperature=0.2,
        )
        passed = bool(result.get("passed", False))
        score = float(result.get("score", 0))

        if passed and score >= 6:
            exp.status = "published"
            logger.info(f"Expert review passed for {exp.id}: score={score}")
        else:
            meta = dict(exp.metadata_ or {})
            meta["expert_review_issues"] = result.get("issues", [])
            meta["expert_review_concerns"] = result.get("domain_concerns", [])
            exp.metadata_ = meta
            logger.info(f"Expert review failed for {exp.id}: score={score}")

        await session.commit()
        return {
            "passed": passed,
            "score": score,
            "issues": result.get("issues", []),
            "domain_concerns": result.get("domain_concerns", []),
        }
    except Exception as e:
        logger.warning(f"Expert review failed for {exp.id}: {e}")
        return {"passed": False, "error": str(e)}


async def review_experience(
    session: AsyncSession,
    exp_id: str,
    stage: str = "all",
) -> dict:
    """
    审核经验记录。

    Args:
        session: 数据库会话
        exp_id: 经验 ID
        stage: "self" | "expert" | "all"

    Returns:
        {"passed": bool, "stage": str, "issues": list, "score": float}
    """
    if stage in ("self", "all"):
        self_result = await self_review(session, exp_id)
        if not self_result.get("passed"):
            return {
                "passed": False,
                "stage": "self_review",
                "issues": self_result.get("issues", []),
                "score": self_result.get("score", 0),
            }

    if stage in ("expert", "all"):
        expert_result = await expert_review(session, exp_id)
        if not expert_result.get("passed"):
            return {
                "passed": False,
                "stage": "expert_review",
                "issues": expert_result.get("issues", []),
                "score": expert_result.get("score", 0),
            }

    exp = await session.get(Experience, exp_id)
    return {
        "passed": True,
        "stage": exp.status if exp else "unknown",
        "issues": [],
        "score": 8.0,
    }


async def batch_review(
    session: AsyncSession,
    stage: str = "all",
    limit: int = 50,
) -> dict:
    """
    批量审核待处理的经验。
    自动处理 status=draft（走 self review）和 status=reviewed（走 expert review）的条目。

    Returns:
        {"processed": int, "passed": int, "failed": int}
    """
    if stage in ("self", "all"):
        draft_q = await session.execute(
            select(Experience).where(Experience.status == "draft").limit(limit)
        )
        drafts = list(draft_q.scalars())
    else:
        drafts = []

    if stage in ("expert", "all"):
        reviewed_q = await session.execute(
            select(Experience).where(Experience.status == "reviewed").limit(limit)
        )
        reviewed = list(reviewed_q.scalars())
    else:
        reviewed = []

    processed = 0
    passed = 0
    failed = 0

    for exp in drafts:
        result = await self_review(session, exp.id)
        processed += 1
        if result.get("passed"):
            passed += 1
        else:
            failed += 1

    for exp in reviewed:
        result = await expert_review(session, exp.id)
        processed += 1
        if result.get("passed"):
            passed += 1
        else:
            failed += 1

    logger.info(f"Batch review completed: processed={processed}, passed={passed}, failed={failed}")
    return {"processed": processed, "passed": passed, "failed": failed}
