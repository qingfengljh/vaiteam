"""过程文档 API：目录浏览、搜索、读取、embedding 补全"""

import json
import io
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.models import Project
from app.services import task_docs

router = APIRouter(prefix="/api", tags=["task-documents"])
SUPPORTED_FIGMA_SCHEMA_VERSIONS = {"1.0"}


class SearchBody(BaseModel):
    query: str
    doc_type: str = ""
    readiness_band: str = "all"
    sort_by: str = "relevance"
    limit: int = 10
    match_mode: str = "balanced"


async def _get_git_web_url(session: AsyncSession, project_id: str) -> str:
    project = await session.get(Project, project_id)
    return (project.git_web_url or "").rstrip("/") if project else ""


def _inject_git_url(docs: list[dict], git_web_url: str) -> list[dict]:
    if not git_web_url:
        return docs
    base = git_web_url if git_web_url.endswith("/") else f"{git_web_url}/"
    for d in docs:
        if not d.get("git_synced"):
            continue
        git_path = d.get("git_path", "")
        if not git_path:
            continue
        d["git_url"] = f"{base}main/{git_path}"
    return docs


@router.get("/projects/{project_id}/docs")
async def list_documents(
    project_id: str,
    iteration_id: str | None = None,
    task_id: str | None = None,
    doc_type: str = "",
    readiness_band: str = "",
    sort_by: str = "created_at",
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    docs = await task_docs.list_docs(
        session, project_id=project_id, iteration_id=iteration_id,
        task_id=task_id, doc_type=doc_type, readiness_band=readiness_band,
        sort_by=sort_by, limit=limit,
    )
    git_web_url = await _get_git_web_url(session, project_id)
    return _inject_git_url(docs, git_web_url)


@router.get("/projects/{project_id}/docs/stats")
async def doc_stats(project_id: str, session: AsyncSession = Depends(get_session)):
    return await task_docs.get_doc_stats(session, project_id)


@router.post("/projects/{project_id}/docs/search")
async def search_documents(project_id: str, body: SearchBody, session: AsyncSession = Depends(get_session)):
    """混合检索：tsvector 粗筛 → pgvector 精排 → LIKE 兜底"""
    results = await task_docs.search_hybrid(
        session, project_id=project_id, query=body.query,
        doc_type=body.doc_type, readiness_band=body.readiness_band,
        sort_by=body.sort_by, limit=body.limit, match_mode=body.match_mode,
    )
    git_web_url = await _get_git_web_url(session, project_id)
    return {"results": _inject_git_url(results, git_web_url), "count": len(results)}


@router.get("/docs/{doc_id}")
async def get_document(doc_id: str, session: AsyncSession = Depends(get_session)):
    from app.models import TaskDocument
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        raise HTTPException(404)
    content = task_docs.read_doc_content(doc.file_path)
    d = task_docs._doc_dict(doc)
    d["content"] = content
    d["has_embedding"] = doc.embedding is not None
    return d


@router.get("/docs/{doc_id}/related")
async def get_related(doc_id: str, limit: int = 5, session: AsyncSession = Depends(get_session)):
    results = await task_docs.find_related(session, doc_id=doc_id, limit=limit)
    return {"results": results, "count": len(results)}


def _load_prototype_spec(content: str) -> dict:
    try:
        spec = json.loads(content)
    except Exception:
        raise HTTPException(400, "prototype_spec is not valid JSON")
    if not isinstance(spec, dict):
        raise HTTPException(400, "prototype_spec JSON must be object")
    return spec


def _validate_top_level_strict(spec: dict, raw_pages: list, errors: list[str]) -> None:
    required_top = ("version", "pages", "tokens")
    for k in required_top:
        if k not in spec:
            errors.append(f"missing top-level field: {k}")
    if not raw_pages:
        errors.append("pages must be non-empty array")
    if "tokens" in spec and not isinstance(spec.get("tokens"), dict):
        errors.append("tokens must be object")


def _validate_pages_strict(raw_pages: list, errors: list[str]) -> None:
    seen_routes: set[str] = set()
    for idx, p in enumerate(raw_pages):
        page = p if isinstance(p, dict) else {}
        required_page = ("id", "name", "route", "layout", "nodes", "states")
        for k in required_page:
            if k not in page:
                errors.append(f"page[{idx}] missing field: {k}")
        route = str(page.get("route") or "")
        if route == "":
            errors.append(f"page[{idx}] route must not be empty")
        elif route in seen_routes:
            errors.append(f"page[{idx}] duplicated route: {route}")
        else:
            seen_routes.add(route)
        if not isinstance(page.get("nodes"), list):
            errors.append(f"page[{idx}] nodes must be array")


def _validate_prototype_spec_strict(spec: dict) -> None:
    raw_pages = spec.get("pages") if isinstance(spec.get("pages"), list) else []
    validation_errors: list[str] = []
    _validate_top_level_strict(spec, raw_pages, validation_errors)
    _validate_pages_strict(raw_pages, validation_errors)
    if validation_errors:
        raise HTTPException(400, {"message": "prototype_spec failed strict validation", "errors": validation_errors})


def _build_figma_template(spec: dict, doc, schema_version: str, strict: bool) -> dict:
    raw_pages = spec.get("pages") if isinstance(spec.get("pages"), list) else []
    pages = []
    for idx, p in enumerate(raw_pages):
        page = p if isinstance(p, dict) else {}
        name = str(page.get("name") or f"Page {idx + 1}")
        route = str(page.get("route") or "/")
        nodes = page.get("nodes") if isinstance(page.get("nodes"), list) else []
        states = page.get("states") if isinstance(page.get("states"), dict) else {}
        pages.append({
            "id": str(page.get("id") or f"page_{idx + 1}"),
            "name": name,
            "route": route,
            "frame": {
                "name": f"{name} ({route})",
                "layout": str(page.get("layout") or "auto"),
                "nodes": nodes,
                "states": states,
            },
        })
    tokens = spec.get("tokens") if isinstance(spec.get("tokens"), dict) else {}
    return {
        "format": "figma-import-template",
        "version": schema_version,
        "source": "prototype_spec",
        "metadata": {
            "projectId": doc.project_id,
            "sourceDocId": doc.id,
            "sourceTitle": doc.title,
            "schemaVersion": schema_version,
            "strict": strict,
        },
        "tokens": tokens,
        "pages": pages,
    }


def _to_component_name(route: str, idx: int) -> str:
    parts = [p for p in route.strip("/").split("/") if p]
    raw = "".join(ch if ch.isalnum() else "_" for ch in "_".join(parts)) or f"page_{idx + 1}"
    words = [w for w in raw.split("_") if w]
    base = "".join(w[:1].upper() + w[1:] for w in words) or f"Page{idx + 1}"
    return f"{base}View"


def _to_safe_package_name(title: str) -> str:
    lower = (title or "prototype-vue3").strip().lower()
    raw = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in lower)
    compact = "-".join([x for x in raw.replace("_", "-").split("-") if x]) or "prototype-vue3"
    return compact[:80]


def _render_view_vue(page_name: str, route: str, data_mode: str) -> str:
    return f"""<template>
  <div style="padding: 20px">
    <n-card title="{page_name}" size="small">
      <p>route: <code>{route}</code></p>
      <p>data mode: <code>{data_mode}</code></p>
      <p>这是从 prototype_spec 自动生成的页面骨架，可直接继续开发。</p>
    </n-card>
  </div>
</template>
"""


def _build_vue3_scaffold_files(spec: dict, title: str, mock_data_mode: bool) -> tuple[dict[str, str], str]:
    pages = spec.get("pages") if isinstance(spec.get("pages"), list) else []
    if not pages:
        raise HTTPException(400, "prototype_spec.pages is empty")
    package_name = _to_safe_package_name(title)
    data_mode = "mock" if mock_data_mode else "api"

    routes = []
    imports = []
    view_files: dict[str, str] = {}
    for idx, p in enumerate(pages):
        page = p if isinstance(p, dict) else {}
        route = str(page.get("route") or f"/page-{idx + 1}")
        page_name = str(page.get("name") or f"Page {idx + 1}")
        component = _to_component_name(route, idx)
        imports.append(f"import {component} from './views/{component}.vue'")
        routes.append(f"  {{ path: '{route}', name: '{component}', component: {component} }},")
        view_files[f"src/views/{component}.vue"] = _render_view_vue(page_name, route, data_mode)

    router_ts = "\n".join([
        "import { createRouter, createWebHistory } from 'vue-router'",
        *imports,
        "",
        "const routes = [",
        *routes,
        "]",
        "",
        "export default createRouter({",
        "  history: createWebHistory(),",
        "  routes,",
        "})",
        "",
    ])

    files: dict[str, str] = {
        "package.json": json.dumps({
            "name": package_name,
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {
                "dev": "vite",
                "build": "vue-tsc -b && vite build",
            },
            "dependencies": {
                "vue": "^3.5.0",
                "vue-router": "^4.5.0",
                "naive-ui": "^2.40.0",
            },
            "devDependencies": {
                "vite": "^8.0.0",
                "typescript": "^5.9.0",
                "vue-tsc": "^3.0.0",
                "@vitejs/plugin-vue": "^6.0.0",
            },
        }, ensure_ascii=False, indent=2),
        "index.html": """<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" /><title>Prototype</title></head>
  <body><div id="app"></div><script type="module" src="/src/main.ts"></script></body>
</html>
""",
        "vite.config.ts": """import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
})
""",
        "tsconfig.json": """{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "strict": true,
    "jsx": "preserve",
    "types": ["vite/client"]
  },
  "include": ["src/**/*.ts", "src/**/*.vue"]
}
""",
        "src/main.ts": """import { createApp } from 'vue'
import { createDiscreteApi } from 'naive-ui'
import App from './App.vue'
import router from './router'

createDiscreteApi(['message'])
createApp(App).use(router).mount('#app')
""",
        "src/App.vue": """<template>
  <n-config-provider>
    <router-view />
  </n-config-provider>
</template>
""",
        "src/router.ts": router_ts,
        "README.md": f"""# {title}

由 prototype_spec 自动生成的 Vue3 原型骨架。

## 运行

```bash
npm install
npm run dev
```

## 说明

- data mode: `{data_mode}`
- 可直接在 `src/views` 内继续开发页面。
""",
    }
    files.update(view_files)
    return files, package_name


def _build_zip_bytes(files: dict[str, str]) -> bytes:
    buff = io.BytesIO()
    with zipfile.ZipFile(buff, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buff.getvalue()


def _evaluate_vue3_production_readiness(spec: dict) -> dict:
    checks: list[dict] = []
    pages = spec.get("pages") if isinstance(spec.get("pages"), list) else []
    tokens = spec.get("tokens") if isinstance(spec.get("tokens"), dict) else {}
    version = str(spec.get("version") or "").strip()
    score = 100

    checks.append({
        "id": "version",
        "label": "顶层 version",
        "ok": bool(version),
        "message": "已提供" if version else "缺少 version",
        "impact": 10,
    })
    if not version:
        score -= 10

    checks.append({
        "id": "pages_non_empty",
        "label": "至少一个页面",
        "ok": len(pages) > 0,
        "message": f"pages={len(pages)}",
        "impact": 30,
    })
    if not pages:
        score -= 30

    checks.append({
        "id": "tokens",
        "label": "设计令牌 tokens",
        "ok": bool(tokens),
        "message": "已配置" if tokens else "缺少 tokens，后续样式统一成本会变高",
        "impact": 10,
    })
    if not tokens:
        score -= 10

    routes: dict[str, int] = {}
    page_missing = 0
    page_empty_nodes = 0
    for idx, p in enumerate(pages):
        page = p if isinstance(p, dict) else {}
        route = str(page.get("route") or "").strip()
        name = str(page.get("name") or "").strip()
        nodes = page.get("nodes") if isinstance(page.get("nodes"), list) else []
        if route:
            routes[route] = routes.get(route, 0) + 1
        if not route or not name:
            page_missing += 1
        if len(nodes) == 0:
            page_empty_nodes += 1

    duplicated_routes = [r for r, c in routes.items() if c > 1]
    ok_identity = page_missing == 0 and len(duplicated_routes) == 0 and len(pages) > 0
    checks.append({
        "id": "page_identity",
        "label": "页面标识完整（name/route 且 route 唯一）",
        "ok": ok_identity,
        "message": (
            "通过"
            if ok_identity
            else f"缺失标识页面={page_missing}，重复 route={len(duplicated_routes)}"
        ),
        "impact": 30,
    })
    if not ok_identity:
        score -= 30

    ok_nodes = page_empty_nodes == 0 and len(pages) > 0
    checks.append({
        "id": "page_nodes",
        "label": "页面节点可渲染（nodes 非空）",
        "ok": ok_nodes,
        "message": "通过" if ok_nodes else f"空 nodes 页面={page_empty_nodes}",
        "impact": 20,
    })
    if not ok_nodes:
        score -= 20

    issues = [c["message"] for c in checks if not c["ok"]]
    if score >= 85:
        grade = "ready"
    elif score >= 60:
        grade = "almost_ready"
    else:
        grade = "needs_work"
    return {
        "score": max(0, score),
        "grade": grade,
        "checks": checks,
        "issues": issues,
        "summary": f"转正就绪度 {max(0, score)} / 100",
    }


def _readiness_tags(grade: str, score: int) -> list[str]:
    band = int(max(0, min(100, score)) // 10 * 10)
    if grade == "ready":
        grade_tag = "prod_ready"
    elif grade == "almost_ready":
        grade_tag = "prod_almost_ready"
    else:
        grade_tag = "prod_needs_work"
    return [grade_tag, f"readiness_score_{band}"]


def _merge_non_readiness_tags(tags: list[str], new_tags: list[str]) -> list[str]:
    kept = [t for t in (tags or []) if not (t.startswith("prod_") or t.startswith("readiness_score_"))]
    return list(dict.fromkeys(kept + new_tags))


def _apply_readiness_to_doc(doc, report: dict) -> dict:
    score = int(report.get("score") or 0)
    grade = str(report.get("grade") or "needs_work")
    summary = str(report.get("summary") or "")
    doc.tags = _merge_non_readiness_tags(doc.tags or [], _readiness_tags(grade, score))
    doc.summary = summary
    meta = dict(doc.metadata_ or {})
    meta["vue3_readiness"] = {
        "score": score,
        "grade": grade,
        "summary": summary,
        "issues": report.get("issues") or [],
    }
    doc.metadata_ = meta
    return report


@router.get("/docs/{doc_id}/figma-template")
async def get_figma_template(
    doc_id: str,
    schema_version: str = Query("1.0"),
    strict: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    from app.models import TaskDocument
    if schema_version not in SUPPORTED_FIGMA_SCHEMA_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_FIGMA_SCHEMA_VERSIONS))
        raise HTTPException(400, f"Unsupported schema_version: {schema_version}. Supported: {supported}")
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if (doc.doc_type or "") != "prototype_spec":
        raise HTTPException(400, "Only prototype_spec supports figma template export")

    content = task_docs.read_doc_content(doc.file_path)
    if not content:
        raise HTTPException(400, "Document content is empty")
    spec = _load_prototype_spec(content)
    if strict:
        _validate_prototype_spec_strict(spec)
    template = _build_figma_template(spec, doc, schema_version, strict)
    safe_title = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (doc.title or "prototype_spec"))[:80]
    filename = f"{safe_title}.figma-template.json"
    return {"template": template, "filename": filename}


@router.get("/docs/{doc_id}/vue3-scaffold-zip")
async def get_vue3_scaffold_zip(
    doc_id: str,
    mock_data_mode: bool = Query(True),
    session: AsyncSession = Depends(get_session),
):
    from app.models import TaskDocument
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if (doc.doc_type or "") != "prototype_spec":
        raise HTTPException(400, "Only prototype_spec supports vue3 scaffold export")
    content = task_docs.read_doc_content(doc.file_path)
    if not content:
        raise HTTPException(400, "Document content is empty")
    spec = _load_prototype_spec(content)
    files, package_name = _build_vue3_scaffold_files(spec, doc.title or "prototype", mock_data_mode=mock_data_mode)
    zip_bytes = _build_zip_bytes(files)
    safe_title = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (doc.title or "prototype_spec"))[:80]
    filename = f"{safe_title}-{package_name}.vue3-prototype.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


@router.get("/docs/{doc_id}/vue3-production-readiness")
async def get_vue3_production_readiness(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
):
    from app.models import TaskDocument
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if (doc.doc_type or "") != "prototype_spec":
        raise HTTPException(400, "Only prototype_spec supports readiness check")
    content = task_docs.read_doc_content(doc.file_path)
    if not content:
        raise HTTPException(400, "Document content is empty")
    spec = _load_prototype_spec(content)
    report = _evaluate_vue3_production_readiness(spec)
    return {
        "doc_id": doc.id,
        "doc_title": doc.title,
        **report,
    }


@router.post("/docs/{doc_id}/vue3-production-readiness/sync")
async def sync_vue3_production_readiness(
    doc_id: str,
    session: AsyncSession = Depends(get_session),
):
    from app.models import TaskDocument
    doc = await session.get(TaskDocument, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if (doc.doc_type or "") != "prototype_spec":
        raise HTTPException(400, "Only prototype_spec supports readiness sync")
    content = task_docs.read_doc_content(doc.file_path)
    if not content:
        raise HTTPException(400, "Document content is empty")
    spec = _load_prototype_spec(content)
    report = _apply_readiness_to_doc(doc, _evaluate_vue3_production_readiness(spec))
    await session.commit()
    return {"status": "ok", "doc_id": doc.id, **report}


@router.post("/projects/{project_id}/docs/vue3-production-readiness/sync")
async def sync_vue3_production_readiness_batch(
    project_id: str,
    iteration_id: str | None = None,
    task_id: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
):
    from app.models import TaskDocument
    q = select(TaskDocument).where(
        TaskDocument.project_id == project_id,
        TaskDocument.doc_type == "prototype_spec",
    ).order_by(TaskDocument.created_at.desc()).limit(limit)
    if iteration_id:
        q = q.where(TaskDocument.iteration_id == iteration_id)
    if task_id:
        q = q.where(TaskDocument.task_id == task_id)
    rows = await session.execute(q)
    docs = list(rows.scalars())
    updated = 0
    failed = 0
    items = []
    for doc in docs:
        content = task_docs.read_doc_content(doc.file_path)
        if not content:
            failed += 1
            items.append({"doc_id": doc.id, "title": doc.title, "ok": False, "error": "empty content"})
            continue
        try:
            spec = _load_prototype_spec(content)
            report = _apply_readiness_to_doc(doc, _evaluate_vue3_production_readiness(spec))
            updated += 1
            items.append({
                "doc_id": doc.id,
                "title": doc.title,
                "ok": True,
                "score": report.get("score"),
                "grade": report.get("grade"),
            })
        except Exception as e:
            failed += 1
            items.append({"doc_id": doc.id, "title": doc.title, "ok": False, "error": str(e)})
    await session.commit()
    return {
        "status": "ok",
        "project_id": project_id,
        "total": len(docs),
        "updated": updated,
        "failed": failed,
        "items": items,
    }


@router.post("/projects/{project_id}/docs/backfill-embeddings")
async def backfill(project_id: str, limit: int = 50, session: AsyncSession = Depends(get_session)):
    """手动触发 embedding 补全"""
    count = await task_docs.backfill_embeddings(session, project_id=project_id, limit=limit)
    return {"processed": count}
