"""原型工坊 Worker：只读 task-pack，供独立进程/容器拉取 Stage2/3 上下文（与 prototype-workshop/README 一致）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models import Document, Project, PrototypeRun
from app.routers.documents import _extract_prototype_spec_json
from app.services import prototype_run as prototype_run_svc
from app.services.prototype_worker_launch import launch_prototype_cc_worker

router = APIRouter(prefix="/api/prototype-workshop", tags=["prototype-workshop"])
worker_router = APIRouter(prefix="/api/prototype-workshop/worker", tags=["prototype-workshop-worker"])


async def _primary_stage_document(
    session: AsyncSession,
    project_id: str,
    stage: int,
    iteration_id: str | None,
) -> Document | None:
    """与文档列表一致：按当前迭代 + 阶段取一条主文档（优先 is_selected）。"""
    q = (
        select(Document)
        .where(
            Document.project_id == project_id,
            Document.stage == stage,
            Document.iteration_id == iteration_id,
        )
        .order_by(Document.is_selected.desc(), Document.updated_at.desc())
        .limit(1)
    )
    r = await session.execute(q)
    return r.scalar_one_or_none()


async def _build_task_pack_for_project(session: AsyncSession, project_id: str) -> dict:
    """与 GET task-pack 相同 JSON；不存在文档时抛 404。"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")

    iter_id = project.current_iteration_id
    doc = await _primary_stage_document(session, project_id, 2, iter_id)
    if not doc:
        raise HTTPException(404, "no stage-2 document (产品原型) for this project/iteration")

    tech = await _primary_stage_document(session, project_id, 3, iter_id)

    spec = _extract_prototype_spec_json(doc.content or "")
    iteration_id = iter_id or ""

    payload: dict = {
        "task_pack_version": 1,
        "worker": "prototype_workshop",
        "executor_hint": "prototype_cc",
        "actor_type": "prototype_cc",
        "project_id": project_id,
        "iteration_id": iteration_id,
        "source_document_id": doc.id,
        "source_title": doc.title,
        "prototype_markdown_excerpt": (doc.content or "")[:120000],
        "prototype_spec": spec,
        "acceptance": [
            "产出可预览或可归档的 mock 前端/stub 包",
            "不回写任务状态机；完成收口走既有 webhook/MQ（由运维接线）",
        ],
        "context_keys": [
            f"document:{doc.id}",
            "prototype-workshop/README.md",
            "docs/PROTOTYPE_CC_RUN_PIPELINE.md",
            "docs/50-CLAUDE_CODE_WORKER.md",
        ],
    }

    if tech:
        payload["technical_scheme_document_id"] = tech.id
        payload["technical_scheme_title"] = tech.title
        payload["technical_scheme_markdown_excerpt"] = (tech.content or "")[:120000]
        payload["context_keys"].append(f"document:{tech.id}")

    return payload


@router.get("/projects/{project_id}/quick-prototype/status")
async def quick_prototype_status(project_id: str, session: AsyncSession = Depends(get_session)):
    """供 Web「快速原型」入口：是否具备产品原型文档；技术方案可选。"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")

    iter_id = project.current_iteration_id
    proto = await _primary_stage_document(session, project_id, 2, iter_id)
    tech = await _primary_stage_document(session, project_id, 3, iter_id)

    def brief(d: Document | None) -> dict | None:
        if not d:
            return None
        return {
            "id": d.id,
            "title": d.title,
            "status": d.status,
            "stage": d.stage,
            "is_selected": bool(d.is_selected),
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        }

    eligible = proto is not None
    msg = (
        "可以拉取任务包：已绑定当前迭代下的产品原型文档。"
        if eligible
        else "当前迭代下尚无产品原型（Stage 2）文档，请先在「阶段文档」中生成或撰写后再试。"
    )
    if eligible and not tech:
        msg += " 尚未检测到技术方案（Stage 3），任务包将仅携带产品原型与 prototype_spec。"

    active = await prototype_run_svc.find_active_running(session, project_id)
    active_run = None
    if active:
        snap = active.snapshot or {}
        launch = snap.get("remote_launch", {})
        active_run = {
            "id": active.id,
            "created_at": active.created_at.isoformat() if active.created_at else None,
            "preview_url": launch.get("preview_url", ""),
        }

    return {
        "eligible": eligible,
        "message": msg,
        "iteration_id": iter_id,
        "prototype_document": brief(proto),
        "technical_document": brief(tech),
        "technical_optional": True,
        "task_pack_url": f"/api/prototype-workshop/projects/{project_id}/task-pack",
        "active_run": active_run,
    }


@router.get("/projects/{project_id}/task-pack")
async def get_prototype_task_pack(project_id: str, session: AsyncSession = Depends(get_session)):
    """聚合 Stage2 产品原型 + 可选 Stage3 技术方案 + prototype_spec JSON，供原型工坊 CC Worker 消费。"""
    return await _build_task_pack_for_project(session, project_id)


@router.post("/projects/{project_id}/runs/start")
async def start_prototype_run(project_id: str, session: AsyncSession = Depends(get_session)):
    """
    登记一次「原型 CC」运行：返回 run_id 与一次性 run_secret；wrapper 拉 task-pack 后跑 claude，
    结束时 POST /api/webhook/prototype-run（见 docs/PROTOTYPE_CC_RUN_PIPELINE.md）。
    """
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")

    iter_id = project.current_iteration_id
    proto = await _primary_stage_document(session, project_id, 2, iter_id)
    if not proto:
        raise HTTPException(
            400,
            "当前迭代下无产品原型（Stage 2）文档，无法启动运行",
        )
    tech = await _primary_stage_document(session, project_id, 3, iter_id)

    existing = await prototype_run_svc.find_active_running(session, project_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "prototype_run_already_active",
                "run_id": existing.id,
                "message": "已有进行中的原型运行；请先在本机执行 wrapper 并完成 webhook 回调，或待该次结束后（成功/失败）再启动新运行。",
            },
        )

    run, secret = await prototype_run_svc.start_run(
        session,
        project=project,
        prototype_doc=proto,
        technical_doc=tech,
    )
    launch = await launch_prototype_cc_worker(
        session,
        project_id=project.id,
        run_id=run.id,
        run_secret=secret,
    )
    snap = dict(run.snapshot or {})
    snap["remote_launch"] = launch
    run.snapshot = snap
    await session.commit()
    await session.refresh(run)
    # 预览地址：内网直连 + 公网域名（经 APISIX，受30天限制）
    preview_url = launch.get("preview_url", "") if isinstance(launch, dict) else ""
    public_url = ""
    if settings.DISPATCHER_PUBLIC_BASE_URL:
        public_url = f"{settings.DISPATCHER_PUBLIC_BASE_URL.rstrip('/')}/prototype-preview/{project.id}/{run.id}"

    return {
        "run_id": run.id,
        "run_secret": secret,
        "status": run.status,
        "snapshot": run.snapshot,
        "task_pack_url": f"/api/prototype-workshop/projects/{project_id}/task-pack",
        "worker_task_pack_url": f"/api/prototype-workshop/worker/runs/{run.id}/task-pack",
        "webhook_path": "/api/webhook/prototype-run",
        "webhook_header": "X-Prototype-Run-Secret: <run_secret>",
        "remote_launch": launch,
        "preview_url": preview_url,
        "public_preview_url": public_url or None,
    }


@worker_router.get("/runs/{run_id}/task-pack")
async def worker_task_pack_by_run(
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """远程 Worker 拉包：凭 X-Prototype-Run-Secret，无需用户 JWT。"""
    secret = (request.headers.get("X-Prototype-Run-Secret") or "").strip()
    if not secret:
        raise HTTPException(401, "missing X-Prototype-Run-Secret")
    run = await session.get(PrototypeRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if not prototype_run_svc.verify_run_secret(secret, run.secret_hash):
        raise HTTPException(403, "invalid run secret")
    return await _build_task_pack_for_project(session, run.project_id)


@router.get("/projects/{project_id}/runs")
async def list_prototype_runs(
    project_id: str,
    limit: int = 30,
    session: AsyncSession = Depends(get_session),
):
    if not await session.get(Project, project_id):
        raise HTTPException(404, "project not found")
    rows = await prototype_run_svc.list_runs(session, project_id, limit)
    return [
        {
            "id": r.id,
            "status": r.status,
            "iteration_id": r.iteration_id,
            "prototype_document_id": r.prototype_document_id,
            "technical_document_id": r.technical_document_id,
            "snapshot": r.snapshot,
            "result": r.result,
            "error_message": r.error_message,
            "exit_code": r.exit_code,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "preview_url": (r.snapshot or {}).get("remote_launch", {}).get("preview_url", ""),
            "public_preview_url": f"{settings.DISPATCHER_PUBLIC_BASE_URL.rstrip('/')}/prototype-preview/{project_id}/{r.id}" if settings.DISPATCHER_PUBLIC_BASE_URL else "",
        }
        for r in rows
    ]
