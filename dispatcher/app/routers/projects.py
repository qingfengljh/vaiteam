import asyncio
import json as json_mod
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, File, Form, UploadFile
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import settings
from app.core.database import get_session, async_session
from app.models import (
    Project, InfraGroup, Iteration, Task, TaskLog, StageProgress, Agent, Backup,
    Message, Document, GenerationTask, TokenUsageLog, AgentMessage, new_id, utcnow,
)
from app.services.backup import backup_file_path
from app.services.project_db_export import (
    PROJECT_DB_EXPORT_AGENT_ID,
    backup_row_summary,
    create_project_db_backup,
)
from app.services.project_db_import import decode_project_db_upload, import_project_database_payload
from app.services import (
    backup as backup_svc,
    experience,
    global_knowledge,
    infra,
    project_git_auth,
    git_repo,
    project_review,
    task_lifecycle,
    model_config_notice,
)
from app.services import global_knowledge_notice
from app.services.project_ref import prepare_code_for_create, prepare_code_for_update, resolve_project
from app.services import project_access

router = APIRouter(prefix="/api/projects", tags=["projects"])

from app.core.constants import VALID_ROLES as _VALID_ROLES, ROLE_MIGRATION as _ROLE_MIGRATION

# 与 portal_internal.bootstrap_managed_infra 中 InfraGroup.purpose 一致；多组时取最早创建的一条作租户默认运行环境
_PLATFORM_INFRA_GROUP_PURPOSE = "platform"


async def _default_platform_infra_group_id(session: AsyncSession) -> str | None:
    q = await session.execute(
        select(InfraGroup.id)
        .where(InfraGroup.purpose == _PLATFORM_INFRA_GROUP_PURPOSE)
        .order_by(InfraGroup.created_at.asc())
        .limit(1)
    )
    return q.scalar_one_or_none()


def _clean_role_map(m: dict | None) -> dict | None:
    if not m:
        return m
    cleaned = {}
    for k, v in m.items():
        if k in _VALID_ROLES:
            cleaned[k] = v
        elif k in _ROLE_MIGRATION and _ROLE_MIGRATION[k] not in cleaned:
            cleaned[_ROLE_MIGRATION[k]] = v
    return cleaned or None


class ProjectCreate(BaseModel):
    name: str
    code: str | None = None
    project_type: str = "new"  # new | maintain | legacy_rewrite
    description: str = ""
    git_repo: str = ""
    git_web_url: str = ""
    rewrite_reason: str = ""
    target_tech_stack: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    description: str | None = None
    status: str | None = None
    current_stage: int | None = None
    git_repo: str | None = None
    git_web_url: str | None = None
    infra_group_id: str | None = None
    role_model_map: dict | None = None
    target_tech_stack: str | None = None
    prototype_fast_track: bool | None = None
    mock_data_mode: bool | None = None


class GlobalKnowledgeNotifyBody(BaseModel):
    sender_id: str = "human"
    summary: str = ""


class GlobalKnowledgeUpsertBody(BaseModel):
    content: str
    sender_id: str = "architect"


class ReviewSummaryGenerateBody(BaseModel):
    generated_by: str = "human"


class ReviewSummaryPublishExperienceBody(BaseModel):
    title: str = ""
    category: str = "best_practice"
    tags: list[str] = []
    quality_score: float = 7.0


class ProjectGitAuthTestBody(BaseModel):
    git_repo: str | None = None
    target: str = "auto"  # auto | agent | dispatcher


class QuickGitRepoTestBody(BaseModel):
    git_repo: str


class ProjectGitAuthTokenBody(BaseModel):
    token: str
    token_username: str = "oauth2"
    sender_id: str = "human"


class ProjectGitAuthGenerateBody(BaseModel):
    force: bool = False
    sender_id: str = "human"


class BackupRequest(BaseModel):
    backup_mode: str = backup_svc.BACKUP_MODE_METADATA_ONLY  # metadata_only | full_source


class RestoreRequest(BaseModel):
    restore_mode: str = "latest_full_source"  # latest_full_source


class BackupTaskStartBody(BaseModel):
    backup_mode: str = backup_svc.BACKUP_MODE_METADATA_ONLY
    auto_start_agents: bool = True


class RestoreTaskStartBody(BaseModel):
    restore_mode: str = "latest_full_source"
    auto_start_agents: bool = True
    # 若填写：按该次已完成的「项目备份」任务快照恢复（仅含源码条目）
    from_backup_task_id: str | None = None


def _in_execution_stage(project: Project) -> bool:
    return (project.current_stage or 0) >= 5


PROJECT_TERMINATE_STAGE = -2
PROJECT_TERMINATE_TITLE = "[LIFECYCLE] 项目终止"
PROJECT_BACKUP_STAGE = -3
PROJECT_BACKUP_TITLE = "[LIFECYCLE] 项目备份"
PROJECT_RESTORE_STAGE = -4
PROJECT_RESTORE_TITLE = "[LIFECYCLE] 项目恢复"


async def _release_agent_tasks(session: AsyncSession, agent_id: str) -> list[str]:
    q = await session.execute(
        select(Task).where(Task.assigned_agent == agent_id, Task.status.in_(["assigned", "executing", "reviewing"]))
    )
    released = []
    for task in q.scalars():
        task.assigned_agent = None
        session.add(task_lifecycle.transition(
            task,
            event="release",
            actor=agent_id,
            reason="project lifecycle state changed",
        ))
        session.add(TaskLog(task_id=task.id, agent_id=agent_id, action="released", message="项目状态变更，任务释放回待分配"))
        released.append(task.id)
    return released


async def _resolve_agent_backend(session: AsyncSession, agent: Agent) -> tuple:
    project = await session.get(Project, agent.project_id) if agent.project_id else None
    if not project:
        cfg = {**(agent.config or {}), "service_name": infra.agent_service_name(agent.project_id, agent.role)}
        return infra.get_backend(), cfg
    node = None
    if project.infra_group_id:
        grp = await session.get(
            InfraGroup,
            project.infra_group_id,
            options=[selectinload(InfraGroup.nodes)],
        )
        if grp and grp.nodes:
            node = grp.nodes[0]
    return infra.get_backend_for_agent(agent, node)


async def _update_gen_task_step(session: AsyncSession, task: GenerationTask, index: int, status: str):
    if index < 0 or index >= len(task.steps):
        return
    task.steps[index]["status"] = status
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "steps")
    done = sum(1 for s in task.steps if s.get("status") == "completed")
    task.progress = int(done / max(len(task.steps), 1) * 100)
    await session.commit()


async def _is_gen_task_cancelled(session: AsyncSession, task_id: str) -> bool:
    t = await session.get(GenerationTask, task_id)
    if not t:
        return False
    await session.refresh(t, ["status"])
    return t.status == "cancelled"


async def _wait_agent_ready(agent_id: str, timeout_seconds: int = 60) -> bool:
    deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
    while datetime.now(timezone.utc).timestamp() < deadline:
        async with async_session() as s:
            agent = await s.get(Agent, agent_id)
            if agent:
                ok, _ = backup_svc.check_agent_backup_readiness(agent)
                if ok:
                    return True
        await asyncio.sleep(2)
    return False


async def _ensure_agents_ready_for_backup(
    session: AsyncSession,
    project: Project,
    auto_start_agents: bool,
) -> tuple[list[dict], list[dict]]:
    q_agents = await session.execute(select(Agent).where(Agent.project_id == project.id))
    agents = list(q_agents.scalars())
    if not _in_execution_stage(project):
        return [], [{"mode": "skip_check", "reason": "项目未到编程阶段，跳过 Agent 可用性检查"}]
    if not agents:
        return [], [{"mode": "skip_check", "reason": "项目下无 Agent"}]

    blocked: list[dict] = []
    actions: list[dict] = []
    for agent in agents:
        ready, reason = backup_svc.check_agent_backup_readiness(agent)
        if ready:
            continue
        if not auto_start_agents:
            blocked.append({"agent_id": agent.id, "reason": reason})
            continue
        try:
            backend, agent_config = await _resolve_agent_backend(session, agent)
            result = await backend.start_agent(agent.id, agent.role, agent_config)
            actions.append({
                "agent_id": agent.id,
                "action": "start_agent",
                "result": result,
            })
            wait_ok = await _wait_agent_ready(agent.id, timeout_seconds=70)
            if not wait_ok:
                blocked.append({"agent_id": agent.id, "reason": "自动启动后仍未就绪"})
        except Exception as e:
            blocked.append({"agent_id": agent.id, "reason": f"自动启动失败: {e}"})
    return blocked, actions


def _load_global_knowledge_text(project: Project, project_id: str) -> tuple[str, str, float | None]:
    """
    优先从项目配置读取公告板；若为空则兼容读取旧文件入口（迁移兜底）。
    返回: text, source, updated_at(unix seconds)
    """
    cfg = dict(project.config or {})
    text = (cfg.get("global_knowledge_content") or "").strip()
    updated_at = cfg.get("global_knowledge_updated_at")
    if text:
        try:
            updated_ts = float(updated_at) if updated_at is not None else None
        except Exception:
            updated_ts = None
        return text, "database", updated_ts
    entry = global_knowledge.resolve_entry_path(project_id)
    if not entry:
        return "", "none", None
    try:
        text = entry.read_text(encoding="utf-8")
        return text, "file", entry.stat().st_mtime
    except Exception:
        return "", "none", None


@router.post("")
async def create_project(body: ProjectCreate, session: AsyncSession = Depends(get_session)):
    if body.project_type not in ("new", "maintain", "legacy_rewrite"):
        raise HTTPException(400, "project_type must be: new, maintain, legacy_rewrite")
    pid = new_id()
    code_val = await prepare_code_for_create(
        session, code_in=body.code, name=body.name, project_id=pid
    )
    project = Project(
        id=pid,
        name=body.name,
        code=code_val,
        project_type=body.project_type,
        description=body.description, git_repo=body.git_repo,
        git_web_url=body.git_web_url,
        rewrite_reason=body.rewrite_reason, target_tech_stack=body.target_tech_stack,
        access_until=utcnow() + timedelta(days=settings.PROJECT_ACCESS_DAYS),
    )
    session.add(project)
    await session.flush()

    iteration = Iteration(
        project_id=project.id, seq=1, title="v1.0",
        description="初始迭代", start_stage=0, current_stage=0, status="active",
    )
    session.add(iteration)
    await session.flush()

    project.current_iteration_id = iteration.id

    for stage in range(8):
        session.add(StageProgress(
            project_id=project.id, iteration_id=iteration.id,
            stage=stage, status="pending" if stage > 0 else "in_progress",
        ))
    default_ig = await _default_platform_infra_group_id(session)
    if default_ig:
        project.infra_group_id = default_ig
    await session.commit()
    await session.refresh(project)
    return project


@router.get("")
async def list_projects(session: AsyncSession = Depends(get_session)):
    q = await session.execute(select(Project).order_by(Project.created_at.desc()))
    projects = q.scalars().all()
    result = []
    for p in projects:
        count_q = await session.execute(select(func.count()).where(Task.project_id == p.id))
        done_q = await session.execute(select(func.count()).where(Task.project_id == p.id, Task.status == "done"))
        agent_q = await session.execute(select(func.count()).where(Agent.project_id == p.id))
        result.append({
            "id": p.id, "code": p.code, "name": p.name, "project_type": p.project_type,
            "description": p.description,
            "status": p.status, "current_stage": p.current_stage,
            "current_iteration_id": p.current_iteration_id,
            "git_repo": p.git_repo, "git_web_url": p.git_web_url,
            "infra_group_id": p.infra_group_id,
            "created_at": p.created_at.isoformat(),
            "task_count": count_q.scalar(), "done_count": done_q.scalar(),
            "agent_count": agent_q.scalar(),
            "access_until": project_access.effective_access_until(p).isoformat(),
            "access_expired": project_access.is_access_expired(p),
            "access_window_days": settings.PROJECT_ACCESS_DAYS,
        })
    return result


@router.get("/{project_id}")
async def get_project(project_id: str, session: AsyncSession = Depends(get_session)):
    p0 = await resolve_project(session, project_id)
    if not p0:
        raise HTTPException(404, "Project not found")
    r = await session.execute(
        select(Project).options(selectinload(Project.infra_group)).where(Project.id == p0.id)
    )
    project = r.unique().scalar_one()
    pid = project.id
    stages_q = await session.execute(
        select(StageProgress).where(
            StageProgress.project_id == pid,
            StageProgress.iteration_id == project.current_iteration_id,
        ).order_by(StageProgress.stage)
    )
    agents_q = await session.execute(
        select(Agent).where(Agent.project_id == pid).order_by(Agent.role)
    )
    # 自动修正：已有编码任务在执行但 current_stage 还停在 4 / status 还停在 planning
    corrected = False
    if project.current_stage == 4 or project.status == "planning":
        coding_q = await session.execute(
            select(Task.id).where(
                Task.project_id == pid,
                Task.status.in_(["assigned", "reviewing", "done"]),
                Task.parent_task_id != None,  # noqa: E711
            ).limit(1)
        )
        if coding_q.scalar_one_or_none():
            if project.current_stage == 4:
                project.current_stage = 5
                corrected = True
            if project.current_iteration_id:
                iter_obj = await session.get(Iteration, project.current_iteration_id)
                if iter_obj:
                    iter_obj.current_stage = 5
            if project.status == "planning":
                project.status = "active"
                corrected = True
            if corrected:
                await session.commit()

    ig = project.infra_group
    return {
        "id": project.id, "code": project.code, "name": project.name, "project_type": project.project_type,
        "description": project.description,
        "rewrite_reason": project.rewrite_reason, "target_tech_stack": project.target_tech_stack,
        "status": project.status, "current_stage": project.current_stage,
        "current_iteration_id": project.current_iteration_id,
        "git_repo": project.git_repo, "git_web_url": project.git_web_url,
        "infra_group_id": project.infra_group_id,
        "infra_group": {"id": ig.id, "name": ig.name} if ig else None,
        "role_model_map": _clean_role_map(project.role_model_map),
        "config": project_git_auth.redact_project_config(project.config),
        "created_at": project.created_at.isoformat(),
        "access_until": project_access.effective_access_until(project).isoformat(),
        "access_expired": project_access.is_access_expired(project),
        "access_window_days": settings.PROJECT_ACCESS_DAYS,
        "stages": [{"stage": s.stage, "status": s.status, "documents": s.documents} for s in stages_q.scalars()],
        "agents": [{"id": a.id, "role": a.role, "model": a.model, "status": a.status, "heartbeat_status": a.last_heartbeat_status} for a in agents_q.scalars()],
    }


@router.put("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    data = body.model_dump(exclude_none=True)
    if "code" in data:
        new_c = data.pop("code")
        if new_c is not None and new_c != project.code:
            project.code = await prepare_code_for_update(
                session, code_in=new_c, exclude_project_id=project.id
            )
    old_role_model_map = _clean_role_map(project.role_model_map)
    if "git_repo" in data:
        new_repo = (data.get("git_repo") or "").strip()
        if not new_repo:
            if _in_execution_stage(project):
                raise HTTPException(409, "执行阶段禁止清空 git_repo；请先暂停/终止项目后再修改。")
            active_task_q = await session.execute(
                select(Task.id).where(
                    Task.project_id == project_id,
                    Task.status.in_(["pending", "assigned", "executing", "reviewing", "blocked"]),
                ).limit(1)
            )
            if active_task_q.scalar_one_or_none():
                raise HTTPException(409, "存在未收口任务，禁止清空 git_repo。")
        data["git_repo"] = new_repo
    if "role_model_map" in data:
        data["role_model_map"] = _clean_role_map(data["role_model_map"])
    cfg = dict(project.config or {})
    if "prototype_fast_track" in data:
        cfg["prototype_fast_track"] = bool(data.pop("prototype_fast_track"))
    if "mock_data_mode" in data:
        cfg["mock_data_mode"] = bool(data.pop("mock_data_mode"))
    if cfg != dict(project.config or {}):
        project.config = cfg
    for k, v in data.items():
        setattr(project, k, v)
    await session.commit()
    new_role_model_map = _clean_role_map(project.role_model_map)
    if "role_model_map" in data and new_role_model_map != old_role_model_map:
        await model_config_notice.notify_model_config_changed(
            session,
            scope="project",
            project_id=project_id,
            reason="project_role_model_map_updated",
        )
    return {"status": "ok"}


@router.post("/git-auth/test-quick")
async def quick_test_git_repo(body: QuickGitRepoTestBody):
    repo = (body.git_repo or "").strip()
    if not repo:
        raise HTTPException(400, "请先填写 git_repo")
    result = await git_repo.verify_remote_repo(repo)
    return {
        "ok": bool(result.get("ok")),
        "git_repo": repo,
        "error": result.get("error", ""),
        "output": result.get("output", ""),
        "hint": "新建项目后可生成项目级公钥，并将其配置到仓库 Deploy Keys / SSH Keys。",
    }


@router.get("/{project_id}/git-auth")
async def get_project_git_auth(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    info = project_git_auth.get_public_auth_info(project)
    return {
        **info,
        "hint": "请将公钥配置到 Git 仓库的 Deploy Keys 或 SSH Keys。完成后可点击“测试连接”。",
    }


@router.post("/{project_id}/git-auth/generate")
async def generate_project_git_auth_key(
    project_id: str,
    body: ProjectGitAuthGenerateBody = ProjectGitAuthGenerateBody(),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if body.force:
        result = project_git_auth.regenerate_ssh_keypair(project, updated_by=body.sender_id or "human")
    else:
        result = project_git_auth.generate_ssh_keypair(project, updated_by=body.sender_id or "human")
    await session.commit()
    info = project_git_auth.get_public_auth_info(project)
    return {
        **info,
        "generated": result["generated"],
    }


@router.post("/{project_id}/git-auth/token")
async def set_project_git_token(
    project_id: str,
    body: ProjectGitAuthTokenBody,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(400, "token 不能为空")
    project_git_auth.set_token(
        project,
        token=token,
        token_username=body.token_username or "oauth2",
        updated_by=body.sender_id or "human",
    )
    await session.commit()
    return project_git_auth.get_public_auth_info(project)


@router.post("/{project_id}/git-auth/test")
async def test_project_git_repo(
    project_id: str,
    body: ProjectGitAuthTestBody,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    git_repo_url = (body.git_repo or project.git_repo or "").strip()
    if not git_repo_url:
        raise HTTPException(400, "请先填写 git_repo")
    auth_info = project_git_auth.get_public_auth_info(project)
    if not auth_info["has_ssh_key"]:
        project_git_auth.generate_ssh_keypair(project, updated_by="human")
        await session.commit()
        await session.refresh(project, ["config"])
    private_key = project_git_auth.get_private_key(project)
    if not private_key:
        raise HTTPException(400, "项目级 Git 私钥缺失，请重新生成密钥对")
    target = (body.target or "auto").strip().lower()
    if target not in ("auto", "dispatcher"):
        raise HTTPException(400, "项目 Git 测试仅支持 dispatcher（target: auto | dispatcher）")
    result = await project_git_auth.verify_project_repo_access(project, git_repo_url)

    return {
        "ok": bool(result.get("ok")),
        "git_repo": git_repo_url,
        "error": result.get("error", ""),
        "hint": result.get("hint", ""),
        "output": result.get("output", ""),
        "warning": "",
        "target_used": "dispatcher_project_key",
        "agent_id": None,
        "auth": project_git_auth.get_public_auth_info(project),
    }


@router.get("/{project_id}/global-knowledge")
async def get_global_knowledge(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    text, source, updated_at = _load_global_knowledge_text(project, project_id)
    if not text:
        return {
            "exists": False,
            "entry_path": global_knowledge.GLOBAL_KNOWLEDGE_ENTRY,
            "version": "",
            "updated_at": None,
            "refs": [],
        }
    content_hash = global_knowledge.calc_version(text)
    project_cfg = dict(project.config or {})
    revision = global_knowledge.to_revision(project_cfg.get("global_knowledge_revision"))
    version = global_knowledge.format_revision(revision) if revision > 0 else ""
    return {
        "exists": True,
        "entry_path": global_knowledge.GLOBAL_KNOWLEDGE_ENTRY,
        "source": source,
        "version": version,
        "revision": revision,
        "content_hash": content_hash,
        "updated_at": updated_at,
        "refs": global_knowledge.extract_local_refs(text)[:20],
    }


@router.post("/{project_id}/global-knowledge/content")
async def upsert_global_knowledge_content(
    project_id: str,
    body: GlobalKnowledgeUpsertBody,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    text = (body.content or "").strip()
    if not text:
        raise HTTPException(400, "公告板内容不能为空")

    cfg = dict(project.config or {})
    cfg["global_knowledge_content"] = text
    cfg["global_knowledge_entry_path"] = global_knowledge.GLOBAL_KNOWLEDGE_ENTRY
    cfg["global_knowledge_updated_at"] = datetime.now(timezone.utc).timestamp()
    cfg["global_knowledge_updated_by"] = body.sender_id or "architect"
    cfg["global_knowledge_content_hash"] = global_knowledge.calc_version(text)
    if global_knowledge.to_revision(cfg.get("global_knowledge_revision")) <= 0:
        cfg["global_knowledge_revision"] = 1
        cfg["global_knowledge_version"] = global_knowledge.format_revision(1)
    project.config = cfg
    await session.commit()

    return {
        "status": "ok",
        "entry_path": global_knowledge.GLOBAL_KNOWLEDGE_ENTRY,
        "content_hash": cfg["global_knowledge_content_hash"],
        "version": cfg.get("global_knowledge_version", ""),
        "revision": global_knowledge.to_revision(cfg.get("global_knowledge_revision")),
    }


@router.post("/{project_id}/global-knowledge/notify")
async def notify_global_knowledge(
    project_id: str,
    body: GlobalKnowledgeNotifyBody,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await global_knowledge_notice.notify_project_agents(
            session,
            project_id=project_id,
            sender_id=body.sender_id or "human",
            summary=body.summary or "",
        )
    except ValueError as e:
        msg = str(e)
        if msg == "Project not found":
            raise HTTPException(404, msg)
        raise HTTPException(400, msg)
    await session.commit()
    return result


@router.post("/{project_id}/review-summary/generate")
async def generate_project_review_summary(
    project_id: str,
    body: ReviewSummaryGenerateBody = ReviewSummaryGenerateBody(),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    generated_by = (body.generated_by or "human").strip() or "human"
    summary = await project_review.generate_review_summary(session, project, generated_by=generated_by)
    review_doc = await project_review.save_review_document(session, project, summary)

    cfg = dict(project.config or {})
    version = int((cfg.get("review_summary") or {}).get("version", 0) or 0) + 1
    cfg["review_summary"] = {
        "version": version,
        "generated_at": summary["generated_at"],
        "generated_by": generated_by,
        "document_id": review_doc.id,
        "document_title": review_doc.title,
        "stage_snapshot": int(project.current_stage or 0),
        "early_warning": bool(int(project.current_stage or 0) < 6),
    }
    project.config = cfg
    await session.commit()
    return {
        "status": "ok",
        "review_summary": cfg["review_summary"],
        "task_stats": summary["task_stats"],
        "token_stats": summary["token_stats"],
        "document": {
            "id": review_doc.id,
            "title": review_doc.title,
            "category": review_doc.category,
            "stage": review_doc.stage,
            "status": review_doc.status,
        },
    }


@router.get("/{project_id}/review-summary/latest")
async def get_latest_project_review_summary(
    project_id: str,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    summary_cfg = dict((project.config or {}).get("review_summary") or {})
    if not summary_cfg:
        return {"exists": False}
    doc_id = str(summary_cfg.get("document_id") or "").strip()
    if not doc_id:
        return {"exists": False}
    doc = await session.get(Document, doc_id)
    if not doc:
        return {"exists": False}
    return {
        "exists": True,
        "review_summary": summary_cfg,
        "document": {
            "id": doc.id,
            "title": doc.title,
            "category": doc.category,
            "stage": doc.stage,
            "status": doc.status,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "content": doc.content,
        },
    }


@router.post("/{project_id}/review-summary/publish-experience")
async def publish_project_review_to_experience(
    project_id: str,
    body: ReviewSummaryPublishExperienceBody,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    summary_cfg = dict((project.config or {}).get("review_summary") or {})
    doc_id = str(summary_cfg.get("document_id") or "").strip()
    if not doc_id:
        raise HTTPException(400, "尚未生成项目复盘报告")
    doc = await session.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "复盘文档不存在")

    title = (body.title or "").strip() or f"{project.name} 项目复盘经验"
    tags = sorted(set((body.tags or []) + ["project-review"]))

    exp = await experience.create(
        session,
        title=title,
        category=body.category or "best_practice",
        tech_stack=[],
        tags=tags,
        problem=f"项目 {project.name} 执行过程中的关键问题与风险复盘",
        root_cause="见复盘报告中的质量与风险、协作与流程观察章节",
        solution=doc.content,
        code_snippet="",
        source_project=project.name,
        quality_score=float(body.quality_score or 7.0),
    )
    return {
        "status": "ok",
        "experience": {
            "id": exp.id,
            "title": exp.title,
            "category": exp.category,
            "quality_score": exp.quality_score,
        },
    }


@router.delete("/{project_id}")
async def delete_project(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.status != "terminated":
        raise HTTPException(400, f"项目当前状态为 {project.status}，请先终止项目再删除")

    deleted_counts: dict[str, int] = {}
    for model in [GenerationTask, Document, Message, TokenUsageLog, AgentMessage]:
        q = select(model).where(model.project_id == project_id)
        result = await session.execute(q)
        rows = list(result.scalars())
        deleted_counts[model.__tablename__] = len(rows)
        for row in rows:
            await session.delete(row)

    await session.delete(project)
    await session.commit()
    return {"deleted": True, "unrecoverable": True, "deleted_counts": deleted_counts}


# ── 项目生命周期 ──

@router.post("/{project_id}/backup")
async def backup_project(
    project_id: str,
    body: BackupRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """备份项目：Connector 打包各 Agent workspace（tar）+ Dispatcher 导出本项目相关库表（json.gz）。"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    backup_mode = backup_svc.normalize_backup_mode(body.backup_mode if body else None)
    backups = await backup_svc.backup_project_agents(session, project_id, backup_mode=backup_mode)
    db_bkp = await create_project_db_backup(session, project_id, backup_mode=backup_mode)
    if db_bkp:
        backups.append(db_bkp)
    if len(backups) == 0:
        raise HTTPException(
            503,
            "未收到任何备份：Connector 未上传 tar 且项目库表导出失败。请检查 Agent/Connector、/api/webhook/backup-upload、BACKUP_DIR 与数据库连通性；详见 docs/OPS_DATA_AND_BACKUP_PATHS.md",
        )
    contains_source_code = backup_svc.mode_includes_workspace(backup_mode)
    return {
        "project_id": project_id,
        "backup_mode": backup_mode,
        "contains_source_code": contains_source_code,
        "warning": (
            "当前备份包含源码（workspace）。请确认已获得用户授权并满足合规要求。"
            if contains_source_code else
            "当前备份不包含源码（metadata_only）。请勿将其当作源码灾备。"
        ),
        "backups": [backup_row_summary(b) for b in backups],
    }


@router.post("/{project_id}/backup/start")
async def start_backup_project_task(
    project_id: str,
    background_tasks: BackgroundTasks,
    body: BackupTaskStartBody | None = None,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    running_q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == PROJECT_BACKUP_STAGE,
            GenerationTask.doc_title == PROJECT_BACKUP_TITLE,
            GenerationTask.status.in_(["pending", "running"]),
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    running = running_q.scalar_one_or_none()
    if running:
        return {"status": running.status, "task_id": running.id}

    backup_mode = backup_svc.normalize_backup_mode(body.backup_mode if body else None)
    auto_start_agents = True if body is None else bool(body.auto_start_agents)
    task = GenerationTask(
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        stage=PROJECT_BACKUP_STAGE,
        doc_title=PROJECT_BACKUP_TITLE,
        status="running",
        progress=0,
        steps=[
            {"name": "检查备份模式与项目状态", "status": "pending"},
            {"name": "检查并准备 Agent 可用性", "status": "pending"},
            {"name": "执行备份", "status": "pending"},
            {"name": "写入结果", "status": "pending"},
        ],
        error_message=json_mod.dumps({
            "backup_mode": backup_mode,
            "auto_start_agents": auto_start_agents,
        }, ensure_ascii=False),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    background_tasks.add_task(_run_backup_project_task, task.id, project_id, backup_mode, auto_start_agents)
    return {"status": "running", "task_id": task.id}


@router.get("/{project_id}/backup/tasks/{task_id}")
async def get_backup_project_task(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id or task.stage != PROJECT_BACKUP_STAGE:
        raise HTTPException(404, "Backup task not found")
    return {
        "id": task.id,
        "status": task.status,
        "progress": task.progress,
        "steps": task.steps,
        "error_message": task.error_message,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.get("/{project_id}/backup/tasks/{task_id}/stream")
async def stream_backup_project_task(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id or task.stage != PROJECT_BACKUP_STAGE:
        raise HTTPException(404, "Backup task not found")

    async def event_generator():
        while True:
            async with async_session() as s:
                t = await s.get(GenerationTask, task_id)
                if not t:
                    return
                payload = {
                    "id": t.id,
                    "status": t.status,
                    "progress": t.progress,
                    "steps": t.steps,
                    "error_message": t.error_message,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                }
                if t.status == "completed":
                    yield f"event: completed\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                if t.status in ("failed", "cancelled"):
                    yield f"event: failed\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                yield f"event: progress\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_backup_project_task(task_id: str, project_id: str, backup_mode: str, auto_start_agents: bool):
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        project = await session.get(Project, project_id)
        if not task or not project:
            return
        try:
            await _update_gen_task_step(session, task, 0, "running")
            if await _is_gen_task_cancelled(session, task_id):
                return
            await _update_gen_task_step(session, task, 0, "completed")

            await _update_gen_task_step(session, task, 1, "running")
            blocked, actions = await _ensure_agents_ready_for_backup(session, project, auto_start_agents)
            if blocked:
                await _update_gen_task_step(session, task, 1, "failed")
                task.status = "failed"
                task.error_message = json_mod.dumps({
                    "message": "存在不可用 Agent，备份已终止",
                    "blocked_agents": blocked,
                    "actions": actions,
                    "backup_mode": backup_mode,
                }, ensure_ascii=False)
                task.completed_at = datetime.now(timezone.utc)
                await session.commit()
                return
            await _update_gen_task_step(session, task, 1, "completed")

            await _update_gen_task_step(session, task, 2, "running")
            backups = await backup_svc.backup_project_agents(
                session,
                project_id,
                backup_mode=backup_mode,
            )
            db_bkp = await create_project_db_backup(session, project_id, backup_mode=backup_mode)
            if db_bkp:
                backups.append(db_bkp)
            if len(backups) == 0:
                await _update_gen_task_step(session, task, 2, "failed")
                task.status = "failed"
                task.progress = 0
                task.completed_at = datetime.now(timezone.utc)
                task.error_message = json_mod.dumps({
                    "message": (
                        "未生成任何可下载备份：未收到 Connector 上传 tar，且项目库表导出失败。"
                        "请查 Dispatcher 日志、Agent 心跳、/api/webhook/backup-upload、BACKUP_DIR 与数据库。"
                    ),
                    "backup_mode": backup_mode,
                    "actions": actions,
                    "backups": [],
                    "doc": "docs/OPS_DATA_AND_BACKUP_PATHS.md",
                }, ensure_ascii=False)
                await session.commit()
                return
            await _update_gen_task_step(session, task, 2, "completed")

            await _update_gen_task_step(session, task, 3, "running")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = json_mod.dumps({
                "backup_mode": backup_mode,
                "contains_source_code": backup_svc.mode_includes_workspace(backup_mode),
                "actions": actions,
                "backups": [backup_row_summary(b) for b in backups],
            }, ensure_ascii=False)
            await _update_gen_task_step(session, task, 3, "completed")
            await session.commit()
        except Exception as e:
            task.status = "failed"
            task.error_message = json_mod.dumps({"error": str(e)}, ensure_ascii=False)
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()


@router.get("/{project_id}/backup/logs")
async def list_project_backup_logs(
    project_id: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    """异步「项目备份」任务流水，便于审计与按版本恢复。"""
    if not await session.get(Project, project_id):
        raise HTTPException(404, "Project not found")
    lim = min(max(limit, 1), 100)
    q = await session.execute(
        select(GenerationTask)
        .where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == PROJECT_BACKUP_STAGE,
            GenerationTask.doc_title == PROJECT_BACKUP_TITLE,
        )
        .order_by(GenerationTask.created_at.desc())
        .limit(lim)
    )
    return {"items": [_backup_log_item(t) for t in q.scalars()]}


@router.get("/{project_id}/backups")
async def list_project_backups(project_id: str, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(Backup).where(Backup.project_id == project_id).order_by(Backup.created_at.desc())
    )
    items = []
    for b in q.scalars():
        s = backup_row_summary(b)
        items.append({
            "id": b.id,
            "agent_id": b.agent_id,
            "backup_type": b.backup_type,
            "file_path": b.file_path,
            "file_size": b.file_size,
            "contains_source_code": s["contains_source_code"],
            "backup_mode": s["backup_mode"],
            "export_kind": s["export_kind"],
            "created_at": b.created_at.isoformat(),
        })
    return items


def _resolved_backup_file_path(project_id: str, file_path: str) -> Path:
    """确保备份文件落在 BACKUP_DIR/<project_id>/ 下，防止路径穿越。"""
    base = (Path(settings.BACKUP_DIR).resolve() / project_id).resolve()
    raw = Path((file_path or "").strip())
    fp = (base / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        fp.relative_to(base)
    except ValueError as e:
        raise HTTPException(400, "Invalid backup path") from e
    if not fp.is_file():
        raise HTTPException(404, "Backup file missing on server")
    return fp


@router.get("/{project_id}/backups/{backup_id}/download")
async def download_project_backup(
    project_id: str,
    backup_id: str,
    session: AsyncSession = Depends(get_session),
):
    b = await session.get(Backup, backup_id)
    if not b or b.project_id != project_id:
        raise HTTPException(404, "Backup not found")
    if not b.file_path:
        raise HTTPException(404, "No file for this backup")
    path = _resolved_backup_file_path(project_id, b.file_path)
    name = os.path.basename(b.file_path) or f"backup-{backup_id}.tar.gz"
    return FileResponse(str(path), filename=name, media_type="application/gzip")


@router.post("/{project_id}/backups/import")
async def import_project_backup(
    project_id: str,
    agent_id: str = Form(...),
    backup_mode: str = Form("full_source"),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """用户上传此前导出的 tar.gz，登记为该项目下某 Agent 的一条备份记录（可随后下载或按版本恢复）。"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    agent = await session.get(Agent, agent_id)
    if not agent or agent.project_id != project_id:
        raise HTTPException(400, "agent_id 不属于该项目")

    mode = (backup_mode or "full_source").strip().lower()
    if mode not in ("metadata_only", "full_source"):
        mode = "full_source"
    include_source = mode == "full_source"

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backup_file_path(project_id, agent_id, ts)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    max_size = settings.BACKUP_MAX_SIZE
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    if os.path.exists(dest):
                        os.remove(dest)
                    raise HTTPException(413, f"Backup exceeds max size {max_size} bytes")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(500, f"save failed: {e}") from e

    file_size = os.path.getsize(dest)
    backup = Backup(
        project_id=project_id,
        agent_id=agent_id,
        backup_type="full_source" if include_source else "metadata_only",
        file_path=dest,
        file_size=file_size,
        metadata_={
            "workspace_path": agent.workspace_path,
            "imported": True,
            "include_workspace": include_source,
            "backup_mode": mode,
            "contains_source_code": include_source,
            "original_filename": file.filename or "",
        },
    )
    session.add(backup)
    await session.commit()
    await session.refresh(backup)
    return {
        "backup_id": backup.id,
        "file_path": dest,
        "file_size": file_size,
        "backup_mode": mode,
        "contains_source_code": include_source,
    }


@router.post("/{project_id}/backups/import-database")
async def import_project_database(
    project_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """
    上传 Dispatcher 导出的 project_db json.gz：校验包内 project id 与路径一致后，
    清空本项目导出范围内的库表并写入包内数据；落盘一份副本并登记 backups（便于审计与再次下载）。
    """
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    max_size = settings.BACKUP_MAX_SIZE
    total = 0
    chunks: list[bytes] = []
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(413, f"Backup exceeds max size {max_size} bytes")
            chunks.append(chunk)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}") from e

    raw = b"".join(chunks)
    try:
        payload = decode_project_db_upload(raw, project_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(
        settings.BACKUP_DIR,
        project_id,
        PROJECT_DB_EXPORT_AGENT_ID,
        f"{ts}_imported.json.gz",
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with open(dest, "wb") as out:
            out.write(raw)
        counts = await import_project_database_payload(session, project_id, payload)
        backup = Backup(
            project_id=project_id,
            agent_id=PROJECT_DB_EXPORT_AGENT_ID,
            backup_type="project_database",
            file_path=dest,
            file_size=len(raw),
            metadata_={
                "kind": "project_db_import",
                "format": "json.gz",
                "imported": True,
                "original_filename": file.filename or "",
                "row_counts": counts,
                "include_workspace": False,
                "contains_source_code": False,
            },
        )
        session.add(backup)
        await session.commit()
        await session.refresh(backup)
    except Exception as e:
        await session.rollback()
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        raise HTTPException(500, f"import failed: {e}") from e

    return {
        "status": "ok",
        "row_counts": counts,
        **backup_row_summary(backup),
    }


@router.post("/{project_id}/restore")
async def restore_project(
    project_id: str,
    body: RestoreRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    restore_mode = (body.restore_mode if body else "latest_full_source").strip().lower()
    if restore_mode != "latest_full_source":
        raise HTTPException(400, "restore_mode must be latest_full_source")

    q_agents = await session.execute(select(Agent).where(Agent.project_id == project_id))
    agents = list(q_agents.scalars())
    if not agents:
        return {
            "project_id": project_id,
            "restore_mode": restore_mode,
            "restored": [],
            "skipped": [],
            "warning": "项目下无 Agent，未执行恢复。",
        }

    restored: list[dict] = []
    skipped: list[dict] = []
    for agent in agents:
        q_backup = await session.execute(
            select(Backup)
            .where(
                and_(
                    Backup.project_id == project_id,
                    Backup.agent_id == agent.id,
                )
            )
            .order_by(Backup.created_at.desc())
        )
        backups = list(q_backup.scalars())
        latest_source_backup = next(
            (b for b in backups if bool((b.metadata_ or {}).get("include_workspace", True))),
            None,
        )
        if not latest_source_backup:
            skipped.append({
                "agent_id": agent.id,
                "reason": "无可用源码备份（full_source）",
            })
            continue

        ok = await backup_svc.restore_agent(session, agent, latest_source_backup)
        if ok:
            restored.append({
                "agent_id": agent.id,
                "backup_id": latest_source_backup.id,
                "file_path": latest_source_backup.file_path,
            })
        else:
            skipped.append({
                "agent_id": agent.id,
                "reason": f"恢复失败（backup_id={latest_source_backup.id}）",
            })

    return {
        "project_id": project_id,
        "restore_mode": restore_mode,
        "restored": restored,
        "skipped": skipped,
        "warning": (
            "仅恢复包含源码的 full_source 备份；metadata_only 备份无法恢复 workspace。"
        ),
    }


@router.post("/{project_id}/restore/start")
async def start_restore_project_task(
    project_id: str,
    background_tasks: BackgroundTasks,
    body: RestoreTaskStartBody | None = None,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    from_tid = ((body.from_backup_task_id if body else None) or "").strip() or None
    if from_tid:
        ref = await session.get(GenerationTask, from_tid)
        if (
            not ref
            or ref.project_id != project_id
            or ref.stage != PROJECT_BACKUP_STAGE
            or ref.doc_title != PROJECT_BACKUP_TITLE
        ):
            raise HTTPException(400, "from_backup_task_id 无效或不属于本项目")
        if ref.status != "completed":
            raise HTTPException(400, "所选备份任务尚未成功完成，无法按版本恢复")
        restore_mode = "by_snapshot"
    else:
        restore_mode = (body.restore_mode if body else "latest_full_source").strip().lower()
        if restore_mode != "latest_full_source":
            raise HTTPException(400, "restore_mode must be latest_full_source")
    auto_start_agents = True if body is None else bool(body.auto_start_agents)

    running_q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == PROJECT_RESTORE_STAGE,
            GenerationTask.doc_title == PROJECT_RESTORE_TITLE,
            GenerationTask.status.in_(["pending", "running"]),
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    running = running_q.scalar_one_or_none()
    if running:
        return {"status": running.status, "task_id": running.id}

    task = GenerationTask(
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        stage=PROJECT_RESTORE_STAGE,
        doc_title=PROJECT_RESTORE_TITLE,
        status="running",
        progress=0,
        steps=[
            {"name": "检查恢复模式与项目状态", "status": "pending"},
            {"name": "检查并准备 Agent 可用性", "status": "pending"},
            {"name": "执行恢复", "status": "pending"},
            {"name": "写入结果", "status": "pending"},
        ],
        error_message=json_mod.dumps({
            "restore_mode": restore_mode,
            "from_backup_task_id": from_tid,
            "auto_start_agents": auto_start_agents,
        }, ensure_ascii=False),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    background_tasks.add_task(_run_restore_project_task, task.id, project_id, restore_mode, auto_start_agents, from_tid)
    return {"status": "running", "task_id": task.id}


@router.get("/{project_id}/restore/tasks/{task_id}")
async def get_restore_project_task(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id or task.stage != PROJECT_RESTORE_STAGE:
        raise HTTPException(404, "Restore task not found")
    return {
        "id": task.id,
        "status": task.status,
        "progress": task.progress,
        "steps": task.steps,
        "error_message": task.error_message,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.get("/{project_id}/restore/tasks/{task_id}/stream")
async def stream_restore_project_task(project_id: str, task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(GenerationTask, task_id)
    if not task or task.project_id != project_id or task.stage != PROJECT_RESTORE_STAGE:
        raise HTTPException(404, "Restore task not found")

    async def event_generator():
        while True:
            async with async_session() as s:
                t = await s.get(GenerationTask, task_id)
                if not t:
                    return
                payload = {
                    "id": t.id,
                    "status": t.status,
                    "progress": t.progress,
                    "steps": t.steps,
                    "error_message": t.error_message,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                }
                if t.status == "completed":
                    yield f"event: completed\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                if t.status in ("failed", "cancelled"):
                    yield f"event: failed\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                yield f"event: progress\ndata: {json_mod.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _backup_log_item(t: GenerationTask) -> dict:
    item = {
        "task_id": t.id,
        "status": t.status,
        "progress": t.progress,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "backup_mode": None,
        "agent_count": 0,
        "has_full_source": False,
        "artifacts": [],
    }
    try:
        d = json_mod.loads(t.error_message or "{}")
    except Exception:
        return item
    item["backup_mode"] = d.get("backup_mode")
    bs = d.get("backups")
    if isinstance(bs, list):
        artifacts: list[dict] = []
        for x in bs:
            if not isinstance(x, dict):
                continue
            bid = x.get("backup_id")
            if not bid:
                continue
            ek = x.get("export_kind")
            if ek not in ("project_database", "connector_workspace"):
                ek = (
                    "project_database"
                    if str(x.get("agent_id") or "") == PROJECT_DB_EXPORT_AGENT_ID
                    else "connector_workspace"
                )
            artifacts.append(
                {
                    "backup_id": str(bid),
                    "agent_id": str(x.get("agent_id") or ""),
                    "export_kind": ek,
                    "contains_source_code": bool(x.get("contains_source_code")),
                }
            )
        item["artifacts"] = artifacts
        agent_like = [
            x for x in bs
            if isinstance(x, dict) and x.get("export_kind") != "project_database"
        ]
        item["agent_count"] = len(agent_like)
        item["has_full_source"] = any(
            bool(x.get("contains_source_code")) for x in agent_like
        )
    return item


def _snapshot_plan_from_backup_task(ref: GenerationTask) -> dict[str, str]:
    """agent_id -> backup_id，仅含源码备份。"""
    try:
        snap = json_mod.loads(ref.error_message or "{}")
    except Exception:
        return {}
    rows = snap.get("backups") if isinstance(snap.get("backups"), list) else []
    plan: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        aid = row.get("agent_id")
        bid = row.get("backup_id")
        if aid and bid and row.get("contains_source_code"):
            plan[str(aid)] = str(bid)
    return plan


async def _run_restore_project_task(
    task_id: str,
    project_id: str,
    restore_mode: str,
    auto_start_agents: bool,
    from_backup_task_id: str | None = None,
):
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        project = await session.get(Project, project_id)
        if not task or not project:
            return
        try:
            await _update_gen_task_step(session, task, 0, "running")
            if await _is_gen_task_cancelled(session, task_id):
                return
            await _update_gen_task_step(session, task, 0, "completed")

            await _update_gen_task_step(session, task, 1, "running")
            blocked, actions = await _ensure_agents_ready_for_backup(session, project, auto_start_agents)
            if blocked:
                await _update_gen_task_step(session, task, 1, "failed")
                task.status = "failed"
                task.error_message = json_mod.dumps({
                    "message": "存在不可用 Agent，恢复已终止",
                    "blocked_agents": blocked,
                    "actions": actions,
                    "restore_mode": restore_mode,
                    "from_backup_task_id": from_backup_task_id,
                }, ensure_ascii=False)
                task.completed_at = datetime.now(timezone.utc)
                await session.commit()
                return
            await _update_gen_task_step(session, task, 1, "completed")

            await _update_gen_task_step(session, task, 2, "running")
            q_agents = await session.execute(select(Agent).where(Agent.project_id == project_id))
            agents = list(q_agents.scalars())
            restored: list[dict] = []
            skipped: list[dict] = []

            plan: dict[str, str] = {}
            if from_backup_task_id:
                ref = await session.get(GenerationTask, from_backup_task_id)
                if not ref or ref.project_id != project_id or ref.stage != PROJECT_BACKUP_STAGE:
                    await _update_gen_task_step(session, task, 2, "failed")
                    task.status = "failed"
                    task.error_message = json_mod.dumps({"message": "备份快照任务无效"}, ensure_ascii=False)
                    task.completed_at = datetime.now(timezone.utc)
                    await session.commit()
                    return
                plan = _snapshot_plan_from_backup_task(ref)
                if not plan:
                    await _update_gen_task_step(session, task, 2, "failed")
                    task.status = "failed"
                    task.error_message = json_mod.dumps({
                        "message": "所选备份任务快照中无可恢复的含源码条目（可能为不含源码备份）",
                    }, ensure_ascii=False)
                    task.completed_at = datetime.now(timezone.utc)
                    await session.commit()
                    return

            for agent in agents:
                if from_backup_task_id:
                    bid = plan.get(agent.id)
                    if not bid:
                        skipped.append({"agent_id": agent.id, "reason": "该快照中无此 Agent 的含源码备份"})
                        continue
                    bkp = await session.get(Backup, bid)
                    if not bkp or bkp.agent_id != agent.id or bkp.project_id != project_id:
                        skipped.append({"agent_id": agent.id, "reason": "备份记录不存在或不匹配"})
                        continue
                    if not bool((bkp.metadata_ or {}).get("include_workspace", True)):
                        skipped.append({"agent_id": agent.id, "reason": "所选备份不含源码"})
                        continue
                    ok = await backup_svc.restore_agent(session, agent, bkp)
                    if ok:
                        restored.append({
                            "agent_id": agent.id,
                            "backup_id": bkp.id,
                            "file_path": bkp.file_path,
                        })
                    else:
                        skipped.append({"agent_id": agent.id, "reason": f"恢复失败（backup_id={bkp.id}）"})
                    continue

                q_backup = await session.execute(
                    select(Backup)
                    .where(
                        and_(
                            Backup.project_id == project_id,
                            Backup.agent_id == agent.id,
                        )
                    )
                    .order_by(Backup.created_at.desc())
                )
                backups = list(q_backup.scalars())
                latest_source_backup = next(
                    (b for b in backups if bool((b.metadata_ or {}).get("include_workspace", True))),
                    None,
                )
                if not latest_source_backup:
                    skipped.append({"agent_id": agent.id, "reason": "无可用源码备份（full_source）"})
                    continue
                ok = await backup_svc.restore_agent(session, agent, latest_source_backup)
                if ok:
                    restored.append({
                        "agent_id": agent.id,
                        "backup_id": latest_source_backup.id,
                        "file_path": latest_source_backup.file_path,
                    })
                else:
                    skipped.append({"agent_id": agent.id, "reason": f"恢复失败（backup_id={latest_source_backup.id}）"})
            await _update_gen_task_step(session, task, 2, "completed")

            await _update_gen_task_step(session, task, 3, "running")
            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            warn = (
                "已按选定备份任务快照恢复各 Agent workspace。"
                if from_backup_task_id else
                "已按各 Agent 最近一次含源码备份恢复 workspace。"
            )
            task.error_message = json_mod.dumps({
                "restore_mode": restore_mode,
                "from_backup_task_id": from_backup_task_id,
                "actions": actions,
                "restored": restored,
                "skipped": skipped,
                "warning": warn + " metadata_only 无法恢复源码目录。",
            }, ensure_ascii=False)
            await _update_gen_task_step(session, task, 3, "completed")
            await session.commit()
        except Exception as e:
            task.status = "failed"
            task.error_message = json_mod.dumps({"error": str(e)}, ensure_ascii=False)
            task.completed_at = datetime.now(timezone.utc)
            await session.commit()


@router.post("/{project_id}/archive")
async def archive_project(project_id: str, session: AsyncSession = Depends(get_session)):
    """归档项目：备份 workspace → 沉淀经验 → 标记 archived"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404)

    backups = await backup_svc.backup_project_agents(
        session,
        project_id,
        backup_mode=backup_svc.BACKUP_MODE_METADATA_ONLY,
    )

    logs_q = await session.execute(
        select(TaskLog).join(Task).where(
            Task.project_id == project_id,
            TaskLog.action.in_(["completed", "failed", "retry"]),
        ).order_by(TaskLog.created_at)
    )
    logs = logs_q.scalars().all()
    task_summaries = "\n".join(
        f"[{log.action}] {log.message[:200]}" for log in logs[-50:]
    )

    settled = []
    if task_summaries:
        try:
            settled = await experience.settle_from_project(
                session, project_name=project.name, task_summaries=task_summaries,
            )
        except Exception:
            pass

    project.status = "archived"
    await session.commit()
    return {
        "status": "archived",
        "backups": [{"agent_id": b.agent_id, "file_path": b.file_path} for b in backups],
        "experiences_settled": [{"id": e.id, "title": e.title} for e in settled],
    }


@router.post("/{project_id}/pause")
async def pause_project(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.status in ("archived", "terminated"):
        raise HTTPException(400, f"项目状态为 {project.status}，不可暂停")
    if project.status == "paused":
        return {"status": "paused", "stopped_agents": 0, "released_tasks": 0}

    stopped_agents = []
    released_tasks = []
    errors = []
    if _in_execution_stage(project):
        q = await session.execute(select(Agent).where(Agent.project_id == project_id))
        agents = list(q.scalars())
        for agent in agents:
            released = await _release_agent_tasks(session, agent.id)
            released_tasks.extend(released)
            agent.current_task_id = None
            agent.status = "idle"
            try:
                backend, agent_config = await _resolve_agent_backend(session, agent)
                await backend.stop_agent(agent.id, agent.role, agent_config)
                agent.last_heartbeat_status = "offline"
                stopped_agents.append(agent.id)
            except Exception as e:
                errors.append({"agent_id": agent.id, "error": str(e)})

    cfg = dict(project.config or {})
    cfg["paused_at"] = datetime.now(timezone.utc).isoformat()
    project.config = cfg
    project.status = "paused"
    await session.commit()
    return {
        "status": "paused",
        "stopped_agents": len(stopped_agents),
        "released_tasks": len(released_tasks),
        "errors": errors,
    }


@router.post("/{project_id}/resume")
async def resume_project(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.status == "archived":
        raise HTTPException(400, "已归档项目不可恢复")
    if project.status == "terminated":
        raise HTTPException(400, "已终止项目不可恢复")
    if project.status != "paused":
        raise HTTPException(400, f"项目状态为 {project.status}，只有 paused 可恢复")

    cfg = dict(project.config or {})
    cfg["resumed_at"] = datetime.now(timezone.utc).isoformat()
    project.config = cfg
    project.status = "active"
    await session.commit()
    return {"status": "active"}


@router.post("/{project_id}/terminate")
async def terminate_project(project_id: str, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.status == "archived":
        raise HTTPException(400, "已归档项目不可终止")
    if project.status == "terminated":
        return {"status": "terminated", "task_id": None}

    running_q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == PROJECT_TERMINATE_STAGE,
            GenerationTask.doc_title == PROJECT_TERMINATE_TITLE,
            GenerationTask.status.in_(["pending", "running"]),
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    running = running_q.scalar_one_or_none()
    if running:
        return {"status": running.status, "task_id": running.id}

    task = GenerationTask(
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        stage=PROJECT_TERMINATE_STAGE,
        doc_title=PROJECT_TERMINATE_TITLE,
        status="running",
        progress=0,
        steps=[
            {"name": "检查项目状态", "status": "pending"},
            {"name": "回收并销毁团队 Agent", "status": "pending"},
            {"name": "取消任务并标记可删除", "status": "pending"},
            {"name": "写入终止状态", "status": "pending"},
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    background_tasks.add_task(_run_terminate_project_task, task.id, project_id)
    return {"status": "running", "task_id": task.id}


async def _run_terminate_project_task(task_id: str, project_id: str):
    async with async_session() as session:
        task = await session.get(GenerationTask, task_id)
        project = await session.get(Project, project_id)
        if not task or not project:
            return
        try:
            await _update_gen_task_step(session, task, 0, "running")
            if await _is_gen_task_cancelled(session, task_id):
                return
            if project.status == "terminated":
                await _update_gen_task_step(session, task, 0, "completed")
                task.status = "completed"
                task.progress = 100
                task.completed_at = datetime.now(timezone.utc)
                task.error_message = json_mod.dumps({"already_terminated": True}, ensure_ascii=False)
                await session.commit()
                return
            await _update_gen_task_step(session, task, 0, "completed")

            destroyed_agents = []
            released_tasks = []
            destroy_errors = []
            await _update_gen_task_step(session, task, 1, "running")
            if _in_execution_stage(project):
                q = await session.execute(select(Agent).where(Agent.project_id == project_id))
                agents = list(q.scalars())
                for agent in agents:
                    if await _is_gen_task_cancelled(session, task_id):
                        return
                    released = await _release_agent_tasks(session, agent.id)
                    released_tasks.extend(released)
                    try:
                        backend, agent_config = await _resolve_agent_backend(session, agent)
                        await backend.destroy_agent(agent.id, agent.container_id or "", agent_config)
                    except Exception as e:
                        destroy_errors.append({"agent_id": agent.id, "error": str(e)})
                    await session.delete(agent)
                    destroyed_agents.append(agent.id)
                await session.commit()
            await _update_gen_task_step(session, task, 1, "completed")

            await _update_gen_task_step(session, task, 2, "running")
            q_tasks = await session.execute(select(Task).where(Task.project_id == project_id))
            tasks = list(q_tasks.scalars())
            cancelled = 0
            marked_deletable = 0
            for item in tasks:
                if await _is_gen_task_cancelled(session, task_id):
                    return
                if item.status not in ("done", "cancelled", "superseded"):
                    item.status = "cancelled"
                    item.assigned_agent = None
                    cancelled += 1
                    session.add(TaskLog(task_id=item.id, agent_id="system", action="terminated", message="项目终止，任务已取消并可删除"))
                ctx = dict(item.context or {})
                ctx["deletable"] = True
                ctx["project_terminated"] = True
                item.context = ctx
                marked_deletable += 1
            await session.commit()
            await _update_gen_task_step(session, task, 2, "completed")

            await _update_gen_task_step(session, task, 3, "running")
            if project.current_iteration_id:
                current_it = await session.get(Iteration, project.current_iteration_id)
                if current_it:
                    current_it.status = "terminated"
            cfg = dict(project.config or {})
            cfg["terminated_at"] = datetime.now(timezone.utc).isoformat()
            project.config = cfg
            project.status = "terminated"
            await session.commit()
            await _update_gen_task_step(session, task, 3, "completed")

            task.status = "completed"
            task.progress = 100
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = json_mod.dumps({
                "destroyed_agents": len(destroyed_agents),
                "released_tasks": len(released_tasks),
                "cancelled_tasks": cancelled,
                "deletable_tasks": marked_deletable,
                "errors": destroy_errors,
            }, ensure_ascii=False)
            await session.commit()
        except Exception as e:
            task.status = "failed"
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = f"{type(e).__name__}: {e}"
            for i, step in enumerate(task.steps):
                if step["status"] == "running":
                    task.steps[i]["status"] = "failed"
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "steps")
            await session.commit()


# ── 端口分配 ──

@router.post("/{project_id}/ports/allocate")
async def allocate_project_ports(project_id: str, session: AsyncSession = Depends(get_session)):
    from app.services.port_manager import allocate_ports, get_project_ports
    await allocate_ports(session, project_id)
    await session.commit()
    return await get_project_ports(session, project_id)


@router.get("/{project_id}/ports")
async def get_project_port_info(project_id: str, session: AsyncSession = Depends(get_session)):
    from app.services.port_manager import get_project_ports
    return await get_project_ports(session, project_id)


# ── 知识检索 ──

@router.get("/{project_id}/search")
async def search_knowledge(
    project_id: str,
    q: str,
    mode: str = "auto",
    sources: str | None = None,
    category: str | None = None,
    tags: str | None = None,
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
):
    from app.services.knowledge_search import search
    src_list = sources.split(",") if sources else None
    tag_list = tags.split(",") if tags else None
    results = await search(
        session, q, project_id=project_id, sources=src_list,
        category=category, tags=tag_list, mode=mode, limit=limit,
    )
    return [r.to_dict() for r in results]


@router.get("/{project_id}/doc-categories")
async def list_doc_categories(project_id: str):
    from app.core.constants import DOC_CATEGORIES
    return DOC_CATEGORIES


# ── 自愈 ──

@router.post("/{project_id}/heal")
async def trigger_project_heal(project_id: str, session: AsyncSession = Depends(get_session)):
    """手动触发项目自愈检查，返回修复统计"""
    from app.services import scheduler_heal, heartbeat as heartbeat_svc
    heal_actions = await scheduler_heal.heal_all(session, project_id)
    await heartbeat_svc.check_agents()
    summary = await heartbeat_svc.get_status_summary(session, project_id)
    return {"project_id": project_id, "heal_actions": heal_actions, "agent_summary": summary}


@router.get("/{project_id}/health")
async def project_health_report(project_id: str, session: AsyncSession = Depends(get_session)):
    """项目健康报告：Agent 状态 + 阻塞条件 + 卡住任务"""
    from app.services import scheduler_heal, heartbeat as heartbeat_svc, scheduler as sch
    readiness = await sch.dispatch_readiness(session, project_id)
    agent_summary = await heartbeat_svc.get_status_summary(session, project_id)
    # 统计卡住任务
    stuck_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(["executing", "assigned"]),
        )
    )
    stuck_tasks = [{"id": t.id, "ref_id": t.ref_id, "status": t.status, "title": t.title} for t in stuck_q.scalars()]
    # 统计升级任务
    esc_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.escalation_level >= 3,
            Task.status == "pending",
        )
    )
    escalated = [{"id": t.id, "ref_id": t.ref_id, "level": t.escalation_level} for t in esc_q.scalars()]
    return {
        "project_id": project_id,
        "agent_summary": agent_summary,
        "readiness": readiness,
        "stuck_tasks": stuck_tasks,
        "stuck_count": len(stuck_tasks),
        "escalated_tasks": escalated,
        "escalated_count": len(escalated),
    }
