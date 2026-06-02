import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.services import experience

router = APIRouter(prefix="/api/experiences", tags=["experiences"])

EXPORT_VERSION = "1.0"


class ExperienceCreate(BaseModel):
    title: str
    category: str = "best_practice"
    tech_stack: list[str] = []
    tags: list[str] = []
    problem: str = ""
    root_cause: str = ""
    solution: str = ""
    code_snippet: str = ""
    source_project: str = ""
    quality_score: float = 5.0


class ExperienceUpdate(BaseModel):
    title: str | None = None
    category: str | None = None
    tech_stack: list[str] | None = None
    tags: list[str] | None = None
    problem: str | None = None
    root_cause: str | None = None
    solution: str | None = None
    code_snippet: str | None = None
    quality_score: float | None = None


def _to_dict(exp):
    return {
        "id": exp.id,
        "title": exp.title,
        "category": exp.category,
        "tech_stack": exp.tech_stack,
        "tags": exp.tags,
        "problem": exp.problem,
        "root_cause": exp.root_cause,
        "solution": exp.solution,
        "code_snippet": exp.code_snippet,
        "source_project": exp.source_project,
        "source_task_id": exp.source_task_id,
        "quality_score": exp.quality_score,
        "use_count": exp.use_count,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
    }


@router.post("")
async def create_experience(body: ExperienceCreate, session: AsyncSession = Depends(get_session)):
    exp = await experience.create(session, **body.model_dump())
    return _to_dict(exp)


@router.get("")
async def list_experiences(
    keyword: str = "",
    category: str = "",
    tech_stack: str = Query("", description="逗号分隔"),
    tags: str = Query("", description="逗号分隔"),
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    ts = [s.strip() for s in tech_stack.split(",") if s.strip()] or None
    tg = [s.strip() for s in tags.split(",") if s.strip()] or None
    items = await experience.search(
        session, keyword=keyword, category=category,
        tech_stack=ts, tags=tg, limit=limit, offset=offset,
    )
    return [_to_dict(e) for e in items]


@router.get("/categories")
async def get_categories():
    return experience.CATEGORIES


@router.get("/stats")
async def experience_stats(session: AsyncSession = Depends(get_session)):
    """经验库统计：按分类、技术栈的数量分布"""
    all_exp = await experience.search(session, limit=100000, offset=0)

    by_category: dict[str, int] = {}
    by_tech: dict[str, int] = {}
    total_score = 0.0
    total_uses = 0

    for e in all_exp:
        by_category[e.category] = by_category.get(e.category, 0) + 1
        for t in (e.tech_stack or []):
            by_tech[t] = by_tech.get(t, 0) + 1
        total_score += e.quality_score or 0
        total_uses += e.use_count or 0

    return {
        "total": len(all_exp),
        "avg_score": round(total_score / len(all_exp), 1) if all_exp else 0,
        "total_uses": total_uses,
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
        "by_tech_stack": dict(sorted(by_tech.items(), key=lambda x: -x[1])[:20]),
    }


@router.get("/{exp_id}")
async def get_experience(exp_id: str, session: AsyncSession = Depends(get_session)):
    exp = await experience.get(session, exp_id)
    if not exp:
        raise HTTPException(404)
    return _to_dict(exp)


@router.put("/{exp_id}")
async def update_experience(exp_id: str, body: ExperienceUpdate, session: AsyncSession = Depends(get_session)):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    exp = await experience.update(session, exp_id, **data)
    if not exp:
        raise HTTPException(404)
    return _to_dict(exp)


@router.delete("/{exp_id}")
async def delete_experience(exp_id: str, session: AsyncSession = Depends(get_session)):
    ok = await experience.delete(session, exp_id)
    if not ok:
        raise HTTPException(404)
    return {"status": "deleted"}


# ── 经验包导出/导入 ──

def _export_dict(exp) -> dict:
    """导出用的完整字段，不含内部 id 和向量"""
    return {
        "title": exp.title,
        "category": exp.category,
        "tech_stack": exp.tech_stack or [],
        "tags": exp.tags or [],
        "problem": exp.problem or "",
        "root_cause": exp.root_cause or "",
        "solution": exp.solution or "",
        "code_snippet": exp.code_snippet or "",
        "source_project": exp.source_project or "",
        "quality_score": exp.quality_score or 5.0,
        "keywords": exp.keywords or [],
    }


@router.post("/export")
async def export_experiences(
    category: str = "",
    tech_stack: str = Query("", description="逗号分隔"),
    min_score: float = 0.0,
    session: AsyncSession = Depends(get_session),
):
    """导出经验包为 JSON 文件下载"""
    ts = [s.strip() for s in tech_stack.split(",") if s.strip()] or None

    items = await experience.search(
        session, category=category, tech_stack=ts, limit=10000, offset=0,
    )
    if min_score > 0:
        items = [e for e in items if (e.quality_score or 0) >= min_score]

    pack = {
        "format": "openclaw-experience-pack",
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "filters": {"category": category or "all", "tech_stack": tech_stack or "all", "min_score": min_score},
        "count": len(items),
        "experiences": [_export_dict(e) for e in items],
    }

    content = json.dumps(pack, ensure_ascii=False, indent=2)
    ts_label = tech_stack.replace(",", "-") if tech_stack else "all"
    filename = f"experience-pack-{ts_label}-{datetime.now().strftime('%Y%m%d')}.json"

    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ImportResult(BaseModel):
    total: int = 0
    imported: int = 0
    skipped: int = 0
    errors: list[str] = []


@router.post("/import", response_model=ImportResult)
async def import_experiences(
    file: UploadFile = File(...),
    skip_duplicates: bool = True,
    min_score: float = 0.0,
    session: AsyncSession = Depends(get_session),
):
    """导入经验包 JSON 文件"""
    content = await file.read()
    try:
        pack = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "无效的 JSON 文件")

    if pack.get("format") != "openclaw-experience-pack":
        raise HTTPException(400, "不是有效的经验包文件（缺少 format 标识）")

    items = pack.get("experiences", [])
    result = ImportResult(total=len(items))

    existing_titles: set[str] = set()
    if skip_duplicates:
        all_exp = await experience.search(session, limit=100000, offset=0)
        existing_titles = {e.title for e in all_exp}

    for i, item in enumerate(items):
        title = item.get("title", "").strip()
        if not title:
            result.errors.append(f"#{i+1}: 缺少 title")
            continue

        if skip_duplicates and title in existing_titles:
            result.skipped += 1
            continue

        score = item.get("quality_score", 5.0)
        if score < min_score:
            result.skipped += 1
            continue

        try:
            await experience.create(
                session,
                title=title,
                category=item.get("category", "best_practice"),
                tech_stack=item.get("tech_stack", []),
                tags=item.get("tags", []),
                problem=item.get("problem", ""),
                root_cause=item.get("root_cause", ""),
                solution=item.get("solution", ""),
                code_snippet=item.get("code_snippet", ""),
                source_project=item.get("source_project", ""),
                quality_score=score,
            )
            result.imported += 1
            existing_titles.add(title)
        except Exception as e:
            result.errors.append(f"#{i+1} {title}: {str(e)[:100]}")

    return result
