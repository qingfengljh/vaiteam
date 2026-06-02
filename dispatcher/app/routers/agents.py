import asyncio
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from app.core.database import get_session, async_session
from app.core.config import settings
from app.models import Agent, AgentTeam, Backup, Task, TaskLog, ModelProvider, ModelConfig, Project, InfraNode, InfraGroup, InfraGroupNode, GenerationTask, AgentBootReport, AgentRehydrationJob
from app.services import openclaw, backup as backup_svc, heartbeat, infra, model_pool, global_knowledge, scheduler, task_lifecycle, deployment_recovery, agent_lifecycle, deploy_manager, global_knowledge_notice
from app.services.project_access import raise_if_expired_for_write
from app.routers.infra_nodes import _do_setup_ssh_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

_bg_tasks: set[asyncio.Task] = set()

async def _release_agent_tasks(session: AsyncSession, agent_id: str) -> list[str]:
    """将 Agent 持有的 assigned/reviewing 任务释放回 pending"""
    q = await session.execute(
        select(Task).where(Task.assigned_agent == agent_id, Task.status.in_(["assigned", "reviewing"]))
    )
    released = []
    for task in q.scalars():
        task.assigned_agent = None
        session.add(task_lifecycle.transition(
            task,
            event="release",
            actor=agent_id,
            reason="agent destroyed/reset",
        ))
        session.add(TaskLog(task_id=task.id, agent_id=agent_id, action="released", message="Agent 被销毁/重置，任务释放回待分配"))
        released.append(task.id)
    return released


ROLE_SUPERVISOR = {
    "architect": "leader",
    "senior": "architect",
    "mid": "architect",
    "junior": "architect",
    "devops": "architect",
    "tester": "leader",
}


async def _sync_team_supervisors(session: AsyncSession, project_id: str, team_id: str | None = None) -> int:
    team_q = select(AgentTeam).where(AgentTeam.project_id == project_id)
    if team_id:
        team_q = team_q.where(AgentTeam.id == team_id)
    teams = list((await session.execute(team_q)).scalars())
    updated = 0
    for team in teams:
        architect_q = await session.execute(
            select(Agent).where(
                Agent.project_id == project_id,
                Agent.team_id == team.id,
                Agent.role == "architect",
            ).order_by(Agent.created_at.asc()).limit(1)
        )
        architect = architect_q.scalar_one_or_none()
        if not architect:
            continue
        member_q = await session.execute(
            select(Agent).where(
                Agent.project_id == project_id,
                Agent.team_id == team.id,
                Agent.role.in_(["senior", "mid", "junior", "devops"]),
            )
        )
        for member in member_q.scalars():
            if member.supervisor_id == architect.id:
                continue
            member.supervisor_id = architect.id
            updated += 1
    return updated


def _count_roles(agents: list[Agent]) -> dict[str, int]:
    result: dict[str, int] = {}
    for a in agents:
        result[a.role] = result.get(a.role, 0) + 1
    return result


def _is_online_heartbeat(status: str | None) -> bool:
    return (status or "") in ("online", "busy")


def _count_online_roles(agents: list[Agent]) -> dict[str, int]:
    result: dict[str, int] = {}
    for a in agents:
        if not _is_online_heartbeat(a.last_heartbeat_status):
            continue
        result[a.role] = result.get(a.role, 0) + 1
    return result


def _public_agent_config(cfg: dict) -> dict:
    """
    对外视图隐藏 dispatcher 内部调度语义：
    - 继任/前任关系仅用于 dispatcher 资源分配，不作为架构师管理视图的一部分。
    """
    data = dict(cfg or {})
    data.pop("role_relationship", None)
    data.pop("successor_inherit", None)
    return data


def _is_global_knowledge_ready(
    required_revision: int,
    required_version: str,
    ack_revision: int,
    ack_version: str,
) -> bool:
    if required_revision > 0:
        return ack_revision >= required_revision
    if required_version:
        return ack_version == required_version
    # 未配置补训版本时，默认视为未补训，避免“已补训”误导。
    return False


class AgentRegister(BaseModel):
    id: str = ""
    project_id: str
    role: str
    model: str = ""
    container_id: str = ""
    workspace_path: str = ""
    webhook_url: str = ""
    supervisor_id: str | None = None


class AgentMessage(BaseModel):
    content: str


class HeartbeatBody(BaseModel):
    agent_id: str
    project_id: str | None = None
    supervisor_id: str | None = None
    role: str | None = None
    status: str | None = None
    current_task_id: str | None = None
    system_info: dict | None = None
    container_id: str | None = None
    boot_id: str | None = None
    session_fingerprint: str | None = None
    context_versions: dict | None = None
    retriever_ready: bool | None = None
    local_checkpoint: dict | None = None
    challenge_reply: str | None = None


class GlobalKnowledgeAckBody(BaseModel):
    version: str | None = None


class ChallengeBody(BaseModel):
    agent_id: str
    ttl_seconds: int = 90


class DispatcherIntentBody(BaseModel):
    project_id: str
    architect_agent_id: str
    intent: str = "dispatch_now"  # dispatch_now | requeue_task
    task_id: str | None = None
    reason: str = ""
    metadata: dict = {}


class RestartBootstrapBody(BaseModel):
    project_id: str
    reason: str = ""


# ── 固定路径端点（必须在 /{agent_id} 之前） ──

@router.post("/heartbeat")
async def receive_heartbeat_http(body: HeartbeatBody, session: AsyncSession = Depends(get_session)):
    ok = await heartbeat.receive_heartbeat(
        session,
        agent_id=body.agent_id,
        project_id=body.project_id,
        supervisor_id=body.supervisor_id,
        role=body.role,
        status=body.status,
        current_task_id=body.current_task_id,
        system_info=body.system_info,
        container_id=body.container_id,
        boot_id=body.boot_id,
        session_fingerprint=body.session_fingerprint,
        context_versions=body.context_versions,
        retriever_ready=body.retriever_ready,
        local_checkpoint=body.local_checkpoint,
        challenge_reply=body.challenge_reply,
    )
    if not ok:
        raise HTTPException(404, "Agent not found")
    return {"status": "ok"}


@router.post("/dispatcher-intent")
async def dispatch_intent(body: DispatcherIntentBody, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    architect = await session.get(Agent, body.architect_agent_id)
    if not architect or architect.project_id != body.project_id:
        raise HTTPException(404, "Architect agent not found")
    if architect.role != "architect":
        raise HTTPException(400, "Only architect can submit dispatcher intent")

    intent = (body.intent or "").strip()
    if intent == "dispatch_now":
        assigned = await scheduler.auto_assign(session, body.project_id, actor_role="architect")
        return {
            "status": "ok",
            "intent": intent,
            "project_id": body.project_id,
            "assigned": assigned,
            "count": len(assigned or []),
        }

    if intent == "requeue_task":
        if not body.task_id:
            raise HTTPException(400, "task_id is required for requeue_task")
        task = await session.get(Task, body.task_id)
        if not task or task.project_id != body.project_id:
            raise HTTPException(404, "Task not found")
        task.assigned_agent = None
        try:
            session.add(task_lifecycle.transition(
                task,
                event="architect_intent_requeue",
                actor=architect.id,
                reason=body.reason or "architect requested requeue",
                metadata={"intent": intent, **(body.metadata or {})},
            ))
        except ValueError as e:
            raise HTTPException(400, str(e))
        await session.commit()
        return {
            "status": "ok",
            "intent": intent,
            "task_id": task.id,
            "task_status": task.status,
        }

    raise HTTPException(400, f"Unsupported intent: {intent}")


@router.get("/model-config")
async def get_model_config(
    agent_id: str,
    target_model: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """返回 Agent 的模型配置（Connector 调用）。

    - 不传 target_model：返回角色默认模型 + 升级链
    - 传 target_model：返回指定模型的 provider 配置（用于升级场景）
    """
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    project = await session.get(Project, agent.project_id) if agent.project_id else None
    project_map = project.role_model_map if project and project.role_model_map else None

    if target_model:
        model_name = target_model
    else:
        model_name = model_pool.resolve_model(agent.role, project_map=project_map)

    explicit_provider, bare_model = model_pool.parse_provider_model(model_name)

    upgrade_chain = []
    if not target_model:
        cur = model_name
        for _ in range(3):
            up = model_pool.upgrade_model(cur)
            if not up:
                break
            upgrade_chain.append(up)
            cur = up

    bare_needed = {bare_model} | {model_pool.parse_provider_model(m)[1] for m in upgrade_chain}

    # 查出所有 enabled provider，筛选出包含所需模型的
    q = await session.execute(
        select(ModelProvider).where(ModelProvider.enabled == True)  # noqa: E712
    )
    all_providers = list(q.scalars())

    providers_out = {}
    for p in all_providers:
        provider_models = set(p.models or [])
        if explicit_provider:
            if p.name != explicit_provider and not (provider_models & bare_needed):
                continue
        elif not (provider_models & bare_needed):
            continue

        mc_q = await session.execute(
            select(ModelConfig).where(
                ModelConfig.provider_id == p.id,
                ModelConfig.enabled == True,  # noqa: E712
            )
        )
        models_list = []
        for mc in mc_q.scalars():
            models_list.append({
                "id": mc.model_name, "name": mc.model_name,
                "reasoning": False,
                "input": ["text", "image"] if mc.supports_vision else ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": mc.context_window or 128000,
                "maxTokens": mc.max_output_tokens or 8192,
            })
        if not models_list:
            for m in (p.models or []):
                models_list.append({
                    "id": m, "name": m, "reasoning": False, "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 128000, "maxTokens": 8192,
                })

        providers_out[p.name] = {
            "baseUrl": p.api_base,
            "apiKey": p.api_key,
            "api": "openai-completions",
            "models": models_list,
        }

    provider_name = explicit_provider or model_pool._model_to_provider.get(bare_model, "")

    return {
        "model": bare_model,
        "provider": provider_name,
        "primary": f"{provider_name}/{bare_model}" if provider_name else bare_model,
        "upgrade_chain": [model_pool.parse_provider_model(m)[1] for m in upgrade_chain],
        "providers": providers_out,
    }


@router.get("/status-summary")
async def status_summary(project_id: str | None = None, session: AsyncSession = Depends(get_session)):
    return await heartbeat.get_status_summary(session, project_id)


@router.get("/recovery-events")
async def recovery_events(
    project_id: str,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    n = max(1, min(limit, 100))
    q = await session.execute(
        select(TaskLog, Task)
        .join(Task, Task.id == TaskLog.task_id)
        .where(
            Task.project_id == project_id,
            TaskLog.action == "dispatcher_recover",
        )
        .order_by(TaskLog.created_at.desc())
        .limit(n)
    )
    items = []
    for log, task in q.all():
        meta = log.metadata_ or {}
        assigned_q = await session.execute(
            select(TaskLog)
            .where(
                TaskLog.task_id == task.id,
                TaskLog.action == "assigned",
                TaskLog.created_at >= log.created_at,
            )
            .order_by(TaskLog.created_at.asc())
            .limit(1)
        )
        assigned_log = assigned_q.scalar_one_or_none()
        reassign_status = "pending"
        reassigned_to = ""
        reassigned_role = ""
        reassigned_at = None
        reassign_error = ""
        if assigned_log:
            reassign_status = "success"
            reassigned_to = assigned_log.agent_id or ""
            reassigned_at = assigned_log.created_at.isoformat()
            if reassigned_to:
                assigned_agent = await session.get(Agent, reassigned_to)
                reassigned_role = assigned_agent.role if assigned_agent else ""
        elif task.status in ("blocked", "failed", "cancelled", "superseded"):
            reassign_status = "failed"
            reassign_error = f"task status={task.status}"
        items.append({
            "id": log.id,
            "task_id": task.id,
            "task_ref": task.ref_id or "",
            "task_title": task.title or "",
            "message": log.message or "",
            "from_agent_id": meta.get("from_agent_id", ""),
            "from_agent_role": meta.get("from_agent_role", ""),
            "from_heartbeat_status": meta.get("from_heartbeat_status", ""),
            "reassign_status": reassign_status,
            "reassigned_to": reassigned_to,
            "reassigned_role": reassigned_role,
            "reassigned_at": reassigned_at,
            "reassign_error": reassign_error,
            "created_at": log.created_at.isoformat(),
        })
    return {"items": items}


@router.get("/boot-reports")
async def boot_reports(
    project_id: str,
    agent_id: str | None = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    n = max(1, min(limit, 200))
    q = select(AgentBootReport).where(AgentBootReport.project_id == project_id)
    if agent_id:
        q = q.where(AgentBootReport.agent_id == agent_id)
    q = q.order_by(AgentBootReport.created_at.desc()).limit(n)
    result = await session.execute(q)
    items = []
    for r in result.scalars():
        items.append({
            "id": r.id,
            "agent_id": r.agent_id,
            "project_id": r.project_id,
            "boot_id": r.boot_id,
            "session_fingerprint": r.session_fingerprint,
            "recovery_mode": r.recovery_mode,
            "retriever_ready": r.retriever_ready,
            "context_versions": r.context_versions or {},
            "metadata": r.metadata_ or {},
            "created_at": r.created_at.isoformat(),
        })
    return {"items": items}


@router.get("/rehydration-jobs")
async def rehydration_jobs(
    project_id: str,
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    n = max(1, min(limit, 200))
    q = select(AgentRehydrationJob).where(AgentRehydrationJob.project_id == project_id)
    if agent_id:
        q = q.where(AgentRehydrationJob.agent_id == agent_id)
    if status:
        q = q.where(AgentRehydrationJob.status == status)
    q = q.order_by(AgentRehydrationJob.created_at.desc()).limit(n)
    result = await session.execute(q)
    items = []
    for r in result.scalars():
        items.append({
            "id": r.id,
            "agent_id": r.agent_id,
            "project_id": r.project_id,
            "mode": r.mode,
            "reason": r.reason,
            "status": r.status,
            "snapshot": r.snapshot or {},
            "result": r.result or {},
            "created_at": r.created_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        })
    return {"items": items}


@router.post("/health-challenge")
async def issue_health_challenge(body: ChallengeBody, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, body.agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    ttl = max(15, min(body.ttl_seconds, 300))
    challenge = secrets.token_urlsafe(18)
    cfg = dict(agent.config or {})
    cfg["health_challenge"] = challenge
    cfg["health_challenge_expire_at"] = int(datetime.now(timezone.utc).timestamp()) + ttl
    cfg["health_challenge_issued_at"] = datetime.now(timezone.utc).isoformat()
    agent.config = cfg
    await session.commit()
    return {
        "agent_id": agent.id,
        "challenge": challenge,
        "expire_in": ttl,
    }


async def _get_or_create_default_team(session: AsyncSession, project_id: str) -> AgentTeam:
    q = await session.execute(
        select(AgentTeam).where(AgentTeam.project_id == project_id, AgentTeam.is_default == True)  # noqa: E712
    )
    team = q.scalar_one_or_none()
    if not team:
        team = AgentTeam(
            project_id=project_id,
            name="默认团队",
            is_default=True,
            module_task_ids=[],
            default_review_policy={
                "auto_review_enabled": True,
                "require_human_review_complexities": ["critical"],
                "require_human_review_task_types": [],
            },
        )
        session.add(team)
        await session.flush()
    elif not team.default_review_policy:
        team.default_review_policy = {
            "auto_review_enabled": True,
            "require_human_review_complexities": ["critical"],
            "require_human_review_task_types": [],
        }
    return team


@router.get("/team-init-task")
async def get_team_init_task(project_id: str, session: AsyncSession = Depends(get_session)):
    """查询项目当前是否有进行中的团队初始化/创建任务"""
    q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == -1,
            GenerationTask.status.in_(["pending", "running"]),
        ).order_by(GenerationTask.created_at.desc()).limit(1)
    )
    gt = q.scalar_one_or_none()
    if not gt:
        return {"task_id": None}
    return {
        "task_id": gt.id,
        "status": gt.status,
        "progress": gt.progress,
        "doc_title": gt.doc_title,
    }


@router.get("/architect-bootstrap-task")
async def get_architect_bootstrap_task(project_id: str, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    cfg = dict(project.config or {})
    task_id = (cfg.get("architect_bootstrap_task_id") or "").strip()
    task: Task | None = None
    if task_id:
        task = await session.get(Task, task_id)
        if task and task.project_id != project_id:
            task = None

    if not task:
        q = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.context["architect_bootstrap"].astext == "true",
            ).order_by(Task.created_at.asc()).limit(1)
        )
        task = q.scalar_one_or_none()
        if task:
            cfg["architect_bootstrap_created"] = True
            cfg["architect_bootstrap_task_id"] = task.id
            project.config = cfg
            await session.commit()

    if not task:
        return {
            "created": bool(cfg.get("architect_bootstrap_created")),
            "task_id": "",
            "status": "missing",
            "assigned_agent": "",
            "assigned_role": "",
            "is_completed": False,
            "is_one_time": True,
        }

    assigned_role = ""
    assigned_agent = task.assigned_agent or ""
    if not assigned_agent:
        assigned_log_q = await session.execute(
            select(TaskLog).where(
                TaskLog.task_id == task.id,
                TaskLog.action == "assigned",
            ).order_by(TaskLog.created_at.desc()).limit(1)
        )
        assigned_log = assigned_log_q.scalar_one_or_none()
        if assigned_log and assigned_log.agent_id:
            assigned_agent = assigned_log.agent_id
    if assigned_agent:
        assigned = await session.get(Agent, assigned_agent)
        assigned_role = assigned.role if assigned else ""
    is_completed = task.status in ("done", "cancelled", "superseded")
    return {
        "created": True,
        "task_id": task.id,
        "ref_id": task.ref_id or "",
        "title": task.title or "",
        "status": task.status,
        "assigned_agent": assigned_agent,
        "assigned_role": assigned_role,
        "is_completed": is_completed,
        "is_one_time": True,
    }


@router.post("/architect-bootstrap-task/restart")
async def restart_architect_bootstrap_task(body: RestartBootstrapBody, session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    q = await session.execute(
        select(Task).where(
            Task.project_id == body.project_id,
            Task.context["architect_bootstrap"].astext == "true",
        ).order_by(Task.created_at.asc()).limit(1)
    )
    task = q.scalar_one_or_none()
    if not task:
        await _ensure_architect_bootstrap_task(session, body.project_id)
        q2 = await session.execute(
            select(Task).where(
                Task.project_id == body.project_id,
                Task.context["architect_bootstrap"].astext == "true",
            ).order_by(Task.created_at.asc()).limit(1)
        )
        task = q2.scalar_one_or_none()
        if not task:
            raise HTTPException(500, "Bootstrap task creation failed")

    reason = (body.reason or "").strip() or "manual restart bootstrap task"
    if task.status in ("done", "cancelled", "superseded"):
        project.task_seq = (project.task_seq or 0) + 1
        ref_id = f"TASK-{project.task_seq:03d}"
        title, description, acceptance, extra_ctx = _architect_bootstrap_blueprint(project)
        retry_task = Task(
            project_id=body.project_id,
            iteration_id=project.current_iteration_id,
            ref_id=ref_id,
            title=f"{title}（重做）",
            description=description,
            type="docs",
            priority=100,
            status="pending",
            suggested_role="architect",
            complexity="medium",
            estimated_hours=0.5,
            dependencies=[],
            acceptance_criteria=acceptance,
            context={
                "architect_bootstrap": True,
                "bootstrap_retry_of": task.id,
                "owner_role": "architect",
                "needs_global_view": True,
                "use_unified_retriever_first": True,
                "project_type": project.project_type or "new",
                "reentry_requested": True,
                "continuation_policy": {
                    "prefer_resume": True,
                    "allow_partial_restart": True,
                    "human_fallback": True,
                },
                **extra_ctx,
            },
        )
        session.add(retry_task)
        cfg = dict(project.config or {})
        cfg["architect_bootstrap_created"] = True
        cfg["architect_bootstrap_task_id"] = retry_task.id
        project.config = cfg
        task = retry_task
    else:
        task.assigned_agent = None
        try:
            session.add(task_lifecycle.transition(
                task,
                event="architect_intent_requeue",
                actor="human",
                reason=reason,
                metadata={"manual_restart": True},
            ))
        except ValueError as e:
            raise HTTPException(400, str(e))
        ctx = dict(task.context or {})
        ctx["reentry_requested"] = True
        task.context = ctx

    await session.commit()
    await _try_direct_assign_architect_bootstrap(session, body.project_id)
    await session.refresh(task)

    assigned_role = ""
    if task.assigned_agent:
        assigned = await session.get(Agent, task.assigned_agent)
        assigned_role = assigned.role if assigned else ""

    return {
        "status": "ok",
        "task_id": task.id,
        "task_status": task.status,
        "assigned_agent": task.assigned_agent or "",
        "assigned_role": assigned_role,
    }


@router.post("/ensure-team")
async def ensure_team(project_id: str, bg: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """确保项目有默认小组且所有角色 Agent 都已部署（异步，返回进度任务 ID）。"""
    proj = await session.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    raise_if_expired_for_write(proj)

    exists_q = await session.execute(
        select(Agent.id).where(Agent.project_id == project_id).limit(1)
    )
    if exists_q.scalar_one_or_none():
        raise HTTPException(409, "团队已存在，禁止重复初始化。请使用启动/重启/重部署功能。")

    default_team = await _get_or_create_default_team(session, project_id)
    required_roles = ["architect", "senior", "mid", "junior", "devops"]
    result = await session.execute(
        select(Agent).where(Agent.project_id == project_id, Agent.team_id == default_team.id)
    )
    existing = {a.role: a for a in result.scalars()}
    missing = [r for r in required_roles if r not in existing]

    if not missing:
        restart_targets = [
            a for a in existing.values()
            if (a.last_heartbeat_status or "offline") not in ("online", "busy", "starting")
        ]
        if not restart_targets:
            return {"task_id": None, "existing": list(existing.keys()), "team_id": default_team.id}
        role_labels = {"architect": "架构师", "senior": "高级工程师", "mid": "中级工程师", "junior": "初级工程师", "devops": "运维"}
        steps = [{"name": f"启动 {role_labels.get(a.role, a.role)} ({a.id})", "status": "pending"} for a in restart_targets]
        steps.append({"name": "刷新调度", "status": "pending"})
        gen_task = GenerationTask(
            project_id=project_id, stage=-1, doc_title="启动团队",
            status="running", progress=0, steps=steps,
        )
        session.add(gen_task)
        await session.commit()
        bg.add_task(_bg_start_team_agents, gen_task.id, project_id, default_team.id, [a.id for a in restart_targets])
        return {
            "task_id": gen_task.id,
            "existing": list(existing.keys()),
            "team_id": default_team.id,
            "restarting": [a.id for a in restart_targets],
        }

    from app.core.constants import VALID_ROLES
    role_labels = {"architect": "架构师", "senior": "高级工程师", "mid": "中级工程师", "junior": "初级工程师", "devops": "运维"}
    steps = [{"name": f"部署 {role_labels.get(r, r)}", "status": "pending"} for r in missing]
    steps.append({"name": "启动全部容器", "status": "pending"})
    steps.append({"name": "提交全局知识入口并发送补训通知", "status": "pending"})

    gen_task = GenerationTask(
        project_id=project_id, stage=-1, doc_title="初始化团队",
        status="running", progress=0, steps=steps,
    )
    session.add(gen_task)
    await session.commit()
    task_id = gen_task.id

    bg.add_task(_bg_ensure_team, task_id, project_id, default_team.id, missing)
    return {"task_id": task_id, "existing": list(existing.keys()), "team_id": default_team.id}


async def _bg_ensure_team(task_id: str, project_id: str, team_id: str, roles: list[str]):
    """后台逐步部署 Agent 并更新 GenerationTask 进度"""
    created_ids: list[str] = []
    deferred_push_roles: list[str] = []
    try:
        total_steps = len(roles) + 2
        for i, role in enumerate(roles):
            async with async_session() as session:
                gt = await session.get(GenerationTask, task_id)
                if not gt:
                    return
                gt.steps[i]["status"] = "running"
                gt.progress = int(i / total_steps * 100)
                await _flush_gen_task(session, gt)

            try:
                async with async_session() as session:
                    project = await session.get(Project, project_id)
                    project_map = project.role_model_map if project else None
                    from app.services import model_pool
                    model_id = (project_map or {}).get(role, model_pool.ROLE_MODEL_MAP.get(role, ""))
                    deploy_result = await deploy_manager.generate_agent_deploy(
                        session,
                        project_id=project_id,
                        role=role,
                        model_id=model_id,
                        allow_push_failure=True,
                    )
                    agent_id = f"{role}-{project_id[:8]}"
                    agent = await session.get(Agent, agent_id)
                    if agent and not agent.team_id:
                        agent.team_id = team_id
                    await session.commit()
                    created_ids.append(agent_id)
                    if deploy_result.get("push_warning"):
                        deferred_push_roles.append(role)

                async with async_session() as session:
                    gt = await session.get(GenerationTask, task_id)
                    gt.steps[i]["status"] = "completed"
                    await _flush_gen_task(session, gt)
            except Exception as e:
                logger.warning(f"ensure-team deploy {role}: {e}")
                async with async_session() as session:
                    gt = await session.get(GenerationTask, task_id)
                    gt.steps[i]["status"] = "failed"
                    await _flush_gen_task(session, gt)

        # 启动全部容器
        start_step_idx = len(roles)
        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            gt.steps[start_step_idx]["status"] = "running"
            gt.progress = int(start_step_idx / total_steps * 100)
            await _flush_gen_task(session, gt)

        for aid in created_ids:
            try:
                async with async_session() as session:
                    agent = await session.get(Agent, aid)
                    if not agent:
                        continue
                    if deployment_recovery.is_pending_remote_push(agent):
                        logger.info("ensure-team skip start for %s: pending remote push", aid)
                        continue
                    agent_lifecycle.before_mount(agent, trigger="bg:ensure-team-start", pending_remote_push=False)
                    backend, agent_config = await _get_agent_backend(session, agent)
                    res = await backend.start_agent(aid, agent.role, agent_config)
                    if res.get("status") != "start_failed":
                        _mark_agent_starting(agent)
                        await session.commit()
            except Exception as e:
                logger.warning(f"ensure-team start {aid}: {e}")

        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            if not gt:
                return
            await _sync_team_supervisors(session, project_id, team_id)
            await _ensure_architect_bootstrap_task(session, project_id)
            await _try_direct_assign_architect_bootstrap(session, project_id)
            notify_step_idx = len(roles) + 1
            gt.steps[notify_step_idx]["status"] = "running"
            gt.progress = int(notify_step_idx / total_steps * 100)
            await _flush_gen_task(session, gt)
            await global_knowledge_notice.notify_project_agents(
                session,
                project_id=project_id,
                sender_id="dispatcher",
                summary="团队初始化完成，请全员完成首次补训。",
            )
            try:
                await scheduler.auto_assign(session, project_id, actor_role="dispatcher")
            except Exception as assign_err:
                logger.warning(f"ensure-team auto-assign failed: {assign_err}")
            await session.refresh(gt)
            gt.steps[start_step_idx]["status"] = "completed"
            gt.steps[notify_step_idx]["status"] = "completed"
            gt.status = "completed"
            gt.progress = 100
            gt.completed_at = datetime.now(timezone.utc)
            gt.error_message = deployment_recovery.ensure_team_result_meta(
                created=len(created_ids),
                roles=list(roles),
                deferred_push_roles=list(deferred_push_roles),
            )
            await _flush_gen_task(session, gt)

    except Exception as e:
        logger.error(f"ensure-team background failed: {e}")
        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            if gt:
                gt.status = "failed"
                gt.error_message = str(e)
                gt.completed_at = datetime.now(timezone.utc)
                await _flush_gen_task(session, gt)


async def _bg_start_team_agents(task_id: str, project_id: str, team_id: str, agent_ids: list[str]):
    started = 0
    failed: list[str] = []
    skipped: list[str] = []
    try:
        total_steps = len(agent_ids) + 1
        for i, aid in enumerate(agent_ids):
            async with async_session() as session:
                gt = await session.get(GenerationTask, task_id)
                if not gt:
                    return
                gt.steps[i]["status"] = "running"
                gt.progress = int(i / total_steps * 100)
                await _flush_gen_task(session, gt)
            try:
                async with async_session() as session:
                    agent = await session.get(Agent, aid)
                    if not agent:
                        failed.append(aid)
                    else:
                        pending_push = deployment_recovery.is_pending_remote_push(agent)
                        if pending_push:
                            pending_err = deployment_recovery.pending_remote_push_error(agent)
                            if _is_ssh_auth_error(pending_err):
                                skipped.append(aid)
                                await session.commit()
                                async with async_session() as s2:
                                    gt2 = await s2.get(GenerationTask, task_id)
                                    gt2.steps[i]["status"] = "completed"
                                    await _flush_gen_task(s2, gt2)
                                continue
                        if pending_push:
                            deployed = await _auto_deploy_if_needed(session, agent)
                            if deployed:
                                await session.refresh(agent)
                            pending_push = deployment_recovery.is_pending_remote_push(agent)
                            if pending_push:
                                skipped.append(aid)
                                await session.commit()
                                async with async_session() as s2:
                                    gt2 = await s2.get(GenerationTask, task_id)
                                    gt2.steps[i]["status"] = "completed"
                                    await _flush_gen_task(s2, gt2)
                                continue
                        agent_lifecycle.before_mount(agent, trigger="bg:ensure-team-restart", pending_remote_push=pending_push)
                        backend, agent_config = await _get_agent_backend(session, agent)
                        res = await backend.start_agent(aid, agent.role, agent_config)
                        if res.get("status") == "start_failed":
                            failed.append(aid)
                        else:
                            _mark_agent_starting(agent)
                            started += 1
                        await session.commit()
                async with async_session() as session:
                    gt = await session.get(GenerationTask, task_id)
                    gt.steps[i]["status"] = "completed"
                    await _flush_gen_task(session, gt)
            except Exception:
                failed.append(aid)
                async with async_session() as session:
                    gt = await session.get(GenerationTask, task_id)
                    gt.steps[i]["status"] = "failed"
                    await _flush_gen_task(session, gt)

        final_step = len(agent_ids)
        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            if not gt:
                return
            gt.steps[final_step]["status"] = "running"
            gt.progress = int(final_step / total_steps * 100)
            await _flush_gen_task(session, gt)
            await _sync_team_supervisors(session, project_id, team_id)
            try:
                await scheduler.auto_assign(session, project_id, actor_role="dispatcher")
            except Exception as assign_err:
                logger.warning(f"ensure-team restart auto-assign failed: {assign_err}")
            await session.refresh(gt)
            gt.steps[final_step]["status"] = "completed"
            gt.status = "completed"
            gt.progress = 100
            gt.completed_at = datetime.now(timezone.utc)
            gt.error_message = '{"started": %d, "failed": %d, "skipped": %d}' % (
                started, len(set(failed)), len(set(skipped))
            )
            await _flush_gen_task(session, gt)
    except Exception as e:
        logger.error(f"ensure-team restart background failed: {e}")
        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            if gt:
                gt.status = "failed"
                gt.error_message = str(e)
                gt.completed_at = datetime.now(timezone.utc)
                await _flush_gen_task(session, gt)


async def _flush_gen_task(session: AsyncSession, gt: GenerationTask):
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(gt, "steps")
    await session.commit()


def _architect_bootstrap_blueprint(project: Project) -> tuple[str, str, list[str], dict]:
    """
    根据项目类型生成架构师初始化任务模板：
    - new / legacy_rewrite: 新建基线（可重写）
    - maintain: 保留现有价值，先克隆与可编译验证
    """
    project_type = (project.project_type or "new").strip().lower()
    if project_type == "maintain":
        return (
            "架构师初始化维护项目基线与执行约束",
            (
                "目标：完成维护项目初始化（保留原有代码价值优先）。\n"
                "1) 从 Git 仓库克隆代码并完成基础编译/测试连通验证；\n"
                "2) 识别可复用模块、核心约束与不可破坏接口；\n"
                "3) 输出维护边界与改造红线（先保留、再增强）；\n"
                "4) 产出团队环境基线与缺库补装指导（各成员独立环境可复现）；\n"
                "5) 建立任务池并标注可直接修改、需谨慎修改、禁止修改区域。"
            ),
            [
                "已完成仓库克隆与基础编译/测试验证",
                "已输出可复用模块与关键约束清单",
                "已形成维护边界（保留优先）文档",
                "已形成团队环境基线与缺库补装指导",
                "已给出任务池与风险分层策略",
            ],
            {
                "bootstrap_mode": "maintenance",
                "preserve_existing_value": True,
                "require_clone_and_build": True,
            },
        )
    return (
        "架构师初始化项目基线与任务池",
        (
            "目标：完成架构师入场初始化并建立可执行基线。\n"
            "1) 确认并学习项目全局知识入口与最新版本；\n"
            "2) 创建/对齐项目代码基线与目录约定；\n"
            "3) 产出团队环境基线与缺库补装指导（各成员独立环境可复现）；\n"
            "4) 建立模块任务池管理视图（仅模块级，子任务按需查询）；\n"
            "5) 输出当前调度阻塞条件与下一步派工建议。"
        ),
        [
            "已确认全局知识版本并记录",
            "已建立代码基线与目录约定",
            "已形成团队环境基线与缺库补装指导",
            "已形成模块任务池视图",
            "已给出阻塞清单与调度建议",
        ],
        {
            "bootstrap_mode": "greenfield",
            "preserve_existing_value": project_type == "legacy_rewrite",
            "require_clone_and_build": False,
        },
    )


async def _ensure_architect_bootstrap_task(session: AsyncSession, project_id: str):
    project = await session.get(Project, project_id)
    if not project:
        return
    cfg = dict(project.config or {})
    if cfg.get("architect_bootstrap_created"):
        return

    existing_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["architect_bootstrap"].astext == "true",
        ).limit(1)
    )
    existing = existing_q.scalar_one_or_none()
    if existing:
        cfg["architect_bootstrap_created"] = True
        cfg["architect_bootstrap_task_id"] = existing.id
        project.config = cfg
        await session.commit()
        return

    default_team_q = await session.execute(
        select(AgentTeam).where(
            AgentTeam.project_id == project_id,
            AgentTeam.is_default == True,  # noqa: E712
        ).limit(1)
    )
    default_team = default_team_q.scalar_one_or_none()
    bootstrap_team_id = default_team.id if default_team else ""
    project.task_seq = (project.task_seq or 0) + 1
    ref_id = f"TASK-{project.task_seq:03d}"
    title, description, acceptance, extra_ctx = _architect_bootstrap_blueprint(project)

    bootstrap = Task(
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        ref_id=ref_id,
        title=title,
        description=description,
        type="docs",
        priority=100,
        status="pending",
        suggested_role="architect",
        complexity="medium",
        estimated_hours=0.5,
        dependencies=[],
        acceptance_criteria=acceptance,
        context={
            "architect_bootstrap": True,
            "owner_role": "architect",
            "needs_global_view": True,
            "use_unified_retriever_first": True,
            "bootstrap_team_id": bootstrap_team_id,
            "project_type": project.project_type or "new",
            "continuation_policy": {
                "prefer_resume": True,
                "allow_partial_restart": True,
                "human_fallback": True,
            },
            **extra_ctx,
        },
    )
    session.add(bootstrap)
    cfg["architect_bootstrap_created"] = True
    cfg["architect_bootstrap_task_id"] = bootstrap.id
    project.config = cfg
    await session.commit()


async def _try_direct_assign_architect_bootstrap(session: AsyncSession, project_id: str):
    bootstrap_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["architect_bootstrap"].astext == "true",
            Task.status == "pending",
            Task.assigned_agent == None,  # noqa: E711
        ).order_by(Task.created_at.asc()).limit(1)
    )
    bootstrap = bootstrap_q.scalar_one_or_none()
    if not bootstrap:
        return

    team_id = (bootstrap.context or {}).get("bootstrap_team_id")
    if not team_id:
        return

    arch_q = await session.execute(
        select(Agent).where(
            Agent.project_id == project_id,
            Agent.team_id == team_id,
            Agent.role == "architect",
            Agent.status == "idle",
            Agent.last_heartbeat_status.in_(["online", "busy"]),
        ).limit(1)
    )
    architect = arch_q.scalar_one_or_none()
    if not architect:
        return
    await scheduler.assign_task(session, bootstrap, architect)


DEFAULT_TEAM_ROLES = ["architect", "senior", "mid", "junior", "devops"]


class CreateTeamBody(BaseModel):
    project_id: str
    name: str
    agent_count: int = 3  # 工程师 Agent 数量（不含架构师）
    role_model_map: dict[str, str] | None = None


class TeamReviewPolicyBody(BaseModel):
    auto_review_enabled: bool = True
    require_human_review_complexities: list[str] = []
    require_human_review_task_types: list[str] = []


class AssignModuleBody(BaseModel):
    module_task_id: str


def _default_team_review_policy() -> dict:
    return {
        "auto_review_enabled": True,
        "require_human_review_complexities": ["critical"],
        "require_human_review_task_types": [],
    }


def _normalize_team_review_policy(policy: dict | None) -> dict:
    src = dict(policy or {})
    return {
        "auto_review_enabled": bool(src.get("auto_review_enabled", True)),
        "require_human_review_complexities": sorted({
            str(x).strip().lower()
            for x in (src.get("require_human_review_complexities") or [])
            if str(x).strip()
        }),
        "require_human_review_task_types": sorted({
            str(x).strip().lower()
            for x in (src.get("require_human_review_task_types") or [])
            if str(x).strip()
        }),
    }


# ── 小组 CRUD ──

@router.get("/teams")
async def list_teams(project_id: str, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(AgentTeam).where(AgentTeam.project_id == project_id).order_by(AgentTeam.is_default.desc(), AgentTeam.created_at)
    )
    teams = list(q.scalars())
    result = []
    for t in teams:
        aq = await session.execute(select(Agent).where(Agent.team_id == t.id).order_by(Agent.role))
        agents = list(aq.scalars())
        subordinate_by_supervisor: dict[str, list[Agent]] = {}
        for member in agents:
            sup = member.supervisor_id or ""
            if not sup:
                continue
            subordinate_by_supervisor.setdefault(sup, []).append(member)
        result.append({
            "id": t.id, "name": t.name, "is_default": t.is_default,
            "module_task_ids": t.module_task_ids or [],
            "default_review_policy": _normalize_team_review_policy(t.default_review_policy or _default_team_review_policy()),
            "agents": [{
                "id": a.id, "role": a.role, "model": a.model, "status": a.status,
                "heartbeat_status": a.last_heartbeat_status,
                "is_online": _is_online_heartbeat(a.last_heartbeat_status),
                "current_task_id": a.current_task_id,
                "supervisor_id": a.supervisor_id,
                "subordinate_count": len(subordinate_by_supervisor.get(a.id, [])) if a.role == "architect" else 0,
                "subordinate_online_count": (
                    len([m for m in subordinate_by_supervisor.get(a.id, []) if _is_online_heartbeat(m.last_heartbeat_status)])
                    if a.role == "architect" else 0
                ),
                "subordinate_role_counts": (
                    _count_roles(subordinate_by_supervisor.get(a.id, [])) if a.role == "architect" else {}
                ),
                "subordinate_online_role_counts": (
                    _count_online_roles(subordinate_by_supervisor.get(a.id, [])) if a.role == "architect" else {}
                ),
            } for a in agents],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return result


@router.post("/teams")
async def create_team(body: CreateTeamBody, bg: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """创建开发小组（异步，返回进度任务 ID）"""
    project = await session.get(Project, body.project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    raise_if_expired_for_write(project)

    team = AgentTeam(
        project_id=body.project_id,
        name=body.name,
        is_default=False,
        module_task_ids=[],
        default_review_policy=_default_team_review_policy(),
    )
    session.add(team)
    await session.flush()

    project_map = project.role_model_map or {}
    model_map = body.role_model_map or project_map
    team_suffix = team.id[:6]

    eng_count = max(1, body.agent_count)
    roles = ["architect"] + ["mid"] * eng_count

    agent_ids = []
    eng_idx = 0
    for role in roles:
        if role in ("senior", "mid", "junior"):
            eng_idx += 1
            agent_id = f"{role}{eng_idx}-{body.project_id[:8]}-{team_suffix}"
        else:
            agent_id = f"{role}-{body.project_id[:8]}-{team_suffix}"
        model_id = model_map.get(role, model_pool.ROLE_MODEL_MAP.get(role, ""))
        sup_role = ROLE_SUPERVISOR.get(role)
        sup_id = f"architect-{body.project_id[:8]}-{team_suffix}" if sup_role == "architect" else ("leader" if sup_role == "leader" else None)
        session.add(Agent(
            id=agent_id, project_id=body.project_id, team_id=team.id,
            role=role, model=model_id, status="idle", supervisor_id=sup_id,
        ))
        agent_ids.append((agent_id, role))

    role_labels = {"architect": "架构师", "senior": "高级工程师", "mid": "中级工程师", "junior": "初级工程师", "devops": "运维"}
    steps = [{"name": f"部署 {role_labels.get(r, r)} ({aid})", "status": "pending"} for aid, r in agent_ids]
    steps.append({"name": "启动全部容器", "status": "pending"})

    gen_task = GenerationTask(
        project_id=body.project_id, stage=-1,
        doc_title=f"创建小组: {body.name}",
        status="running", progress=0, steps=steps,
    )
    session.add(gen_task)
    await session.commit()

    bg.add_task(_bg_create_team, gen_task.id, body.project_id, [aid for aid, _ in agent_ids])
    return {
        "task_id": gen_task.id,
        "team_id": team.id,
        "name": body.name,
        "default_review_policy": _normalize_team_review_policy(team.default_review_policy),
    }


@router.patch("/teams/{team_id}/default-review-policy")
async def update_team_default_review_policy(
    team_id: str,
    body: TeamReviewPolicyBody,
    session: AsyncSession = Depends(get_session),
):
    team = await session.get(AgentTeam, team_id)
    if not team:
        raise HTTPException(404, "小组不存在")
    policy = _normalize_team_review_policy({
        "auto_review_enabled": body.auto_review_enabled,
        "require_human_review_complexities": body.require_human_review_complexities,
        "require_human_review_task_types": body.require_human_review_task_types,
    })
    team.default_review_policy = policy
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(team, "default_review_policy")
    await session.commit()
    return {"team_id": team_id, "default_review_policy": policy}


async def _bg_create_team(task_id: str, project_id: str, agent_ids: list[str]):
    """后台部署并启动小组 Agent"""
    deployed_ids: list[str] = []
    try:
        total_steps = len(agent_ids) + 1
        for i, aid in enumerate(agent_ids):
            async with async_session() as session:
                gt = await session.get(GenerationTask, task_id)
                gt.steps[i]["status"] = "running"
                gt.progress = int(i / total_steps * 100)
                await _flush_gen_task(session, gt)

            try:
                async with async_session() as session:
                    agent = await session.get(Agent, aid)
                    if not agent:
                        raise ValueError(f"Agent {aid} not found")
                    ok = await _auto_deploy_if_needed(session, agent)
                    if not ok:
                        raise ValueError("deploy returned False")
                    deployed_ids.append(aid)

                async with async_session() as session:
                    gt = await session.get(GenerationTask, task_id)
                    gt.steps[i]["status"] = "completed"
                    await _flush_gen_task(session, gt)
            except Exception as e:
                logger.warning(f"create-team deploy {aid}: {e}")
                async with async_session() as session:
                    gt = await session.get(GenerationTask, task_id)
                    gt.steps[i]["status"] = "failed"
                    await _flush_gen_task(session, gt)

        start_step_idx = len(agent_ids)
        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            gt.steps[start_step_idx]["status"] = "running"
            gt.progress = int(start_step_idx / total_steps * 100)
            await _flush_gen_task(session, gt)

        for aid in deployed_ids:
            try:
                async with async_session() as session:
                    agent = await session.get(Agent, aid)
                    if not agent:
                        continue
                    agent_lifecycle.before_mount(
                        agent,
                        trigger="bg:create-team-start",
                        pending_remote_push=deployment_recovery.is_pending_remote_push(agent),
                    )
                    backend, agent_config = await _get_agent_backend(session, agent)
                    res = await backend.start_agent(aid, agent.role, agent_config)
                    if res.get("status") != "start_failed":
                        _mark_agent_starting(agent)
                        await session.commit()
            except Exception as e:
                logger.warning(f"create-team start {aid}: {e}")

        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            gt.steps[start_step_idx]["status"] = "completed"
            gt.status = "completed"
            gt.progress = 100
            gt.completed_at = datetime.now(timezone.utc)
            gt.error_message = f'{{"created": {len(deployed_ids)}, "total": {len(agent_ids)}}}'
            await _flush_gen_task(session, gt)

    except Exception as e:
        logger.error(f"create-team background failed: {e}")
        async with async_session() as session:
            gt = await session.get(GenerationTask, task_id)
            if gt:
                gt.status = "failed"
                gt.error_message = str(e)
                gt.completed_at = datetime.now(timezone.utc)
                await _flush_gen_task(session, gt)


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, session: AsyncSession = Depends(get_session)):
    """销毁小组：释放任务、停止容器、删除 Agent 和小组记录"""
    team = await session.get(AgentTeam, team_id)
    if not team:
        raise HTTPException(404, "小组不存在")
    if team.is_default:
        raise HTTPException(400, "默认团队不能删除")

    aq = await session.execute(select(Agent).where(Agent.team_id == team_id))
    agents = list(aq.scalars())

    destroyed = []
    released = []
    failed = []
    for agent in agents:
        rel = await _release_agent_tasks(session, agent.id)
        released.extend(rel)
        try:
            backend, agent_config = await _get_agent_backend(session, agent)
            await backend.destroy_agent(agent.id, agent.container_id or "", agent_config)
        except Exception as e:
            failed.append({"agent_id": agent.id, "error": str(e)})
        await session.delete(agent)
        destroyed.append(agent.id)

    await session.delete(team)
    await session.commit()
    return {"destroyed": destroyed, "released_tasks": released, "failed_cleanup": failed}


@router.post("/teams/{team_id}/assign-module")
async def assign_module_to_team(team_id: str, body: AssignModuleBody, session: AsyncSession = Depends(get_session)):
    """将模块分配给指定小组"""
    team = await session.get(AgentTeam, team_id)
    if not team:
        raise HTTPException(404, "小组不存在")

    module = await session.get(Task, body.module_task_id)
    if not module or not (module.context or {}).get("is_module"):
        raise HTTPException(400, "无效的模块任务")

    # 从其他小组移除该模块
    all_teams_q = await session.execute(
        select(AgentTeam).where(AgentTeam.project_id == team.project_id)
    )
    for t in all_teams_q.scalars():
        mids = list(t.module_task_ids or [])
        if body.module_task_id in mids:
            mids.remove(body.module_task_id)
            t.module_task_ids = mids
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(t, "module_task_ids")

    # 添加到目标小组
    mids = list(team.module_task_ids or [])
    if body.module_task_id not in mids:
        mids.append(body.module_task_id)
    team.module_task_ids = mids
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(team, "module_task_ids")

    await session.commit()
    return {"team_id": team_id, "module_task_ids": team.module_task_ids}


@router.post("/teams/{team_id}/remove-module")
async def remove_module_from_team(team_id: str, body: AssignModuleBody, session: AsyncSession = Depends(get_session)):
    """将模块从小组移除（回归默认团队管辖）"""
    team = await session.get(AgentTeam, team_id)
    if not team:
        raise HTTPException(404, "小组不存在")

    mids = list(team.module_task_ids or [])
    if body.module_task_id in mids:
        mids.remove(body.module_task_id)
        team.module_task_ids = mids
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(team, "module_task_ids")
        await session.commit()

    return {"team_id": team_id, "module_task_ids": team.module_task_ids}


# ── 模块依赖管理 ──

class DependencyBody(BaseModel):
    dependency_id: str


@router.post("/tasks/{task_id}/add-dependency")
async def add_task_dependency(task_id: str, body: DependencyBody, session: AsyncSession = Depends(get_session)):
    """人工添加依赖关系"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if body.dependency_id == task_id:
        raise HTTPException(400, "不能依赖自己")

    dep_task = await session.get(Task, body.dependency_id)
    if not dep_task:
        raise HTTPException(404, "依赖的任务不存在")

    deps = list(task.dependencies or [])
    if body.dependency_id in deps:
        return {"task_id": task_id, "dependencies": deps}

    deps.append(body.dependency_id)
    task.dependencies = deps
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "dependencies")

    session.add(TaskLog(
        task_id=task_id, agent_id=None, action="dependency_added",
        message=f"人工添加依赖: {dep_task.ref_id or body.dependency_id}",
    ))
    await session.commit()
    return {"task_id": task_id, "dependencies": task.dependencies}


@router.post("/tasks/{task_id}/remove-dependency")
async def remove_task_dependency(task_id: str, body: DependencyBody, session: AsyncSession = Depends(get_session)):
    """人工解除依赖关系"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    deps = list(task.dependencies or [])
    if body.dependency_id not in deps:
        raise HTTPException(400, "该依赖不存在")

    deps.remove(body.dependency_id)
    task.dependencies = deps
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(task, "dependencies")

    session.add(TaskLog(
        task_id=task_id, agent_id=None, action="dependency_removed",
        message=f"人工解除依赖: {body.dependency_id}",
    ))
    await session.commit()
    return {"task_id": task_id, "dependencies": task.dependencies}


class QuickAddBody(BaseModel):
    project_id: str
    host: str
    port: int = 22
    user: str = "root"
    password: str
    name: str = ""
    role: str = "mid"
    model: str = ""


@router.post("/quick-add")
async def quick_add_agent(body: QuickAddBody, session: AsyncSession = Depends(get_session)):
    """一键添加 Agent 机器：创建节点、配免密、创建环境组、关联项目、创建 Agent、更新 infra 配置"""
    project = await session.get(Project, body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    node_name = body.name or f"Agent-{body.project_id[:8]}"
    node = InfraNode(name=node_name, type="linux", host=body.host, port=body.port, user=body.user)
    session.add(node)
    await session.flush()

    ok, err = await _do_setup_ssh_key(node, body.password, session)
    if not ok:
        await session.rollback()
        raise HTTPException(400, f"SSH 免密配置失败: {err}")

    grp = None
    if project.infra_group_id:
        grp = await session.get(InfraGroup, project.infra_group_id, options=[selectinload(InfraGroup.nodes)])
    if not grp or not grp.nodes:
        grp_q = await session.execute(select(InfraGroup).options(selectinload(InfraGroup.nodes)).limit(1))
        grp = grp_q.scalar_one_or_none()
        if not grp:
            grp = InfraGroup(name="默认", description="快速添加创建")
            session.add(grp)
            await session.flush()
        project.infra_group_id = grp.id
    node_ids = [n.id for n in grp.nodes]
    if node.id not in node_ids:
        session.add(InfraGroupNode(group_id=grp.id, node_id=node.id, roles=["AGENT"]))

    agent_id = f"{body.role}-{body.project_id[:8]}"
    sup_role = ROLE_SUPERVISOR.get(body.role)
    sup_id = "leader" if sup_role == "leader" else None
    if sup_role and sup_role != "leader":
        q = await session.execute(
            select(Agent).where(Agent.project_id == body.project_id, Agent.role == sup_role).limit(1)
        )
        sup = q.scalar_one_or_none()
        if sup:
            sup_id = sup.id

    agent = await session.get(Agent, agent_id)
    if not agent:
        session.add(Agent(
            id=agent_id, project_id=body.project_id, role=body.role, model=body.model,
            supervisor_id=sup_id,
        ))
    else:
        agent.model = body.model

    await session.commit()
    return {
        "agent_id": agent_id,
        "node_id": node.id,
        "message": "已创建节点、配置免密、创建 Agent。请使用「部署」生成配置并复制到目标机后点击「启动」。",
    }


# ── CRUD ──

@router.post("")
async def register_agent(body: AgentRegister, session: AsyncSession = Depends(get_session)):
    proj = await session.get(Project, body.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    raise_if_expired_for_write(proj)

    existing = await session.get(Agent, body.id)

    supervisor_id = body.supervisor_id
    if not supervisor_id:
        sup_role = ROLE_SUPERVISOR.get(body.role)
        if sup_role and sup_role != "leader":
            q = await session.execute(
                select(Agent).where(
                    Agent.project_id == body.project_id,
                    Agent.role == sup_role,
                ).limit(1)
            )
            sup = q.scalar_one_or_none()
            if sup:
                supervisor_id = sup.id
        elif sup_role == "leader":
            supervisor_id = "leader"

    if existing:
        existing.container_id = body.container_id
        existing.workspace_path = body.workspace_path
        existing.webhook_url = body.webhook_url
        existing.role = body.role
        existing.status = "idle"
        existing.supervisor_id = supervisor_id
        if body.model and body.model != "unknown" and not existing.model:
            existing.model = body.model
    else:
        agent_data = body.model_dump()
        agent_data["supervisor_id"] = supervisor_id
        session.add(Agent(**agent_data))

    await session.flush()
    await _sync_team_supervisors(session, body.project_id)
    await session.commit()
    return {"status": "ok", "agent_id": body.id, "supervisor_id": supervisor_id}


def _compute_display_names(agents: list[Agent]) -> dict[str, str]:
    """为同项目同角色的 Agent 分配序号，生成显示名称。"""
    from collections import defaultdict
    from app.core.constants import VALID_ROLES
    ROLE_CN: dict[str, str] = {r: r for r in VALID_ROLES}
    ROLE_CN.update({
        "architect": "架构师", "senior": "高级工程师", "mid": "中级工程师",
        "junior": "初级工程师", "devops": "运维工程师", "tester": "测试工程师",
    })
    groups: dict[tuple[str, str], list[Agent]] = defaultdict(list)
    for a in agents:
        groups[(a.project_id, a.role)].append(a)
    names: dict[str, str] = {}
    for (pid, role), group in groups.items():
        group.sort(key=lambda a: a.created_at or datetime.min.replace(tzinfo=timezone.utc))
        for i, a in enumerate(group, 1):
            cfg = a.config or {}
            if cfg.get("display_name"):
                names[a.id] = cfg["display_name"]
                continue
            role_cn = ROLE_CN.get(role, role)
            if len(group) > 1:
                names[a.id] = f"{role_cn}#{i}"
            else:
                names[a.id] = role_cn
    return names


@router.get("")
async def list_agents(project_id: str | None = None, session: AsyncSession = Depends(get_session)):
    q = select(Agent).order_by(Agent.role)
    if project_id:
        q = q.where(Agent.project_id == project_id)
    result = await session.execute(q)
    versions: dict[str, str] = {}
    rows = []
    all_agents = list(result.scalars())
    display_names = _compute_display_names(all_agents)
    project_cfg_cache: dict[str, dict] = {}
    for a in all_agents:
        if a.project_id not in versions:
            project = await session.get(Project, a.project_id)
            project_cfg_cache[a.project_id] = dict(project.config or {}) if project else {}
            rev = global_knowledge.to_revision(project_cfg_cache[a.project_id].get("global_knowledge_revision"))
            versions[a.project_id] = global_knowledge.format_revision(rev) if rev > 0 else ""
        required_version = versions[a.project_id]
        required_revision = global_knowledge.to_revision(project_cfg_cache[a.project_id].get("global_knowledge_revision"))
        cfg = a.config or {}
        ack_version = cfg.get("global_knowledge_ack_version")
        ack_revision = global_knowledge.to_revision(cfg.get("global_knowledge_ack_revision"))
        ready = _is_global_knowledge_ready(
            required_revision=required_revision,
            required_version=required_version,
            ack_revision=ack_revision,
            ack_version=ack_version or "",
        )
        rows.append({
            "id": a.id, "project_id": a.project_id, "role": a.role,
            "display_name": display_names.get(a.id, a.role), "model": a.model,
            "status": a.status, "current_task_id": a.current_task_id,
            "container_id": a.container_id, "workspace_path": a.workspace_path,
            "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            "heartbeat_status": a.last_heartbeat_status,
            "supervisor_id": a.supervisor_id,
            "team_id": a.team_id,
            "global_knowledge_ack_version": ack_version,
            "global_knowledge_ack_revision": ack_revision,
            "global_knowledge_ack_at": cfg.get("global_knowledge_ack_at"),
            "global_knowledge_pending_version": cfg.get("global_knowledge_pending_version"),
            "global_knowledge_pending_revision": cfg.get("global_knowledge_pending_revision"),
            "global_knowledge_required_version": required_version,
            "global_knowledge_required_revision": required_revision,
            "global_knowledge_ready": ready,
        })
    return rows


# ── 动态路径端点（/{agent_id} 系列） ──

@router.get("/{agent_id}")
async def get_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    project = await session.get(Project, agent.project_id)
    project_cfg = dict(project.config or {}) if project else {}
    required_revision = global_knowledge.to_revision(project_cfg.get("global_knowledge_revision"))
    required_version = global_knowledge.format_revision(required_revision) if required_revision > 0 else ""
    cfg = dict(agent.config or {})
    public_cfg = _public_agent_config(cfg)
    ack_version = cfg.get("global_knowledge_ack_version")
    ack_revision = global_knowledge.to_revision(cfg.get("global_knowledge_ack_revision"))
    ready = _is_global_knowledge_ready(
        required_revision=required_revision,
        required_version=required_version,
        ack_revision=ack_revision,
        ack_version=ack_version or "",
    )
    dn = _compute_display_names([agent]).get(agent.id, agent.role)
    return {
        "id": agent.id, "project_id": agent.project_id, "role": agent.role,
        "display_name": dn, "model": agent.model, "status": agent.status,
        "current_task_id": agent.current_task_id,
        "container_id": agent.container_id, "workspace_path": agent.workspace_path,
        "config": public_cfg,
        "heartbeat_status": agent.last_heartbeat_status,
        "supervisor_id": agent.supervisor_id,
        "team_id": agent.team_id,
        "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        "global_knowledge_ack_version": ack_version,
        "global_knowledge_ack_revision": ack_revision,
        "global_knowledge_ack_at": cfg.get("global_knowledge_ack_at"),
        "global_knowledge_pending_version": cfg.get("global_knowledge_pending_version"),
        "global_knowledge_pending_revision": cfg.get("global_knowledge_pending_revision"),
        "global_knowledge_required_version": required_version,
        "global_knowledge_required_revision": required_revision,
        "global_knowledge_ready": ready,
    }


@router.post("/{agent_id}/global-knowledge/ack")
async def acknowledge_global_knowledge(
    agent_id: str,
    body: GlobalKnowledgeAckBody,
    session: AsyncSession = Depends(get_session),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    project = await session.get(Project, agent.project_id)
    project_cfg = dict(project.config or {}) if project else {}
    required_revision = global_knowledge.to_revision(project_cfg.get("global_knowledge_revision"))
    required_version = global_knowledge.format_revision(required_revision) if required_revision > 0 else ""
    if required_revision <= 0:
        raise HTTPException(400, "当前无可确认的全局知识版本")
    target_version = body.version or required_version
    if target_version and target_version != required_version:
        raise HTTPException(409, f"版本已更新，请确认最新版本 {required_version}")
    cfg = dict(agent.config or {})
    cfg["global_knowledge_ack_version"] = required_version
    cfg["global_knowledge_ack_revision"] = required_revision
    cfg["global_knowledge_ack_at"] = datetime.now(timezone.utc).isoformat()
    cfg.pop("global_knowledge_pending_version", None)
    cfg.pop("global_knowledge_pending_revision", None)
    agent.config = cfg
    await session.commit()
    return {
        "status": "ok",
        "agent_id": agent.id,
        "ack_version": required_version,
        "ack_revision": required_revision,
        "required_version": required_version,
        "required_revision": required_revision,
        "ready": True,
    }


class RenameBody(BaseModel):
    display_name: str

@router.patch("/{agent_id}/rename")
async def rename_agent(agent_id: str, body: RenameBody, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    name = body.display_name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    cfg = dict(agent.config or {})
    cfg["display_name"] = name
    agent.config = cfg
    await session.commit()
    return {"id": agent_id, "display_name": name}

@router.get("/{agent_id}/subordinates")
async def get_subordinates(agent_id: str, session: AsyncSession = Depends(get_session)):
    return await heartbeat.get_subordinates(session, agent_id)


@router.get("/{agent_id}/subordinates/summary")
async def get_subordinate_summary(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    subordinates = await heartbeat.get_subordinates(session, agent_id)
    role_counts: dict[str, int] = {}
    online_counts: dict[str, int] = {}
    online_subordinates: list[dict] = []
    for item in subordinates:
        role = item.get("role") or "unknown"
        role_counts[role] = role_counts.get(role, 0) + 1
        if item.get("heartbeat_status") in ("online", "busy"):
            online_counts[role] = online_counts.get(role, 0) + 1
            online_subordinates.append(item)
    return {
        "agent_id": agent_id,
        "role": agent.role,
        "subordinate_count": len(subordinates),
        "subordinate_online_count": len(online_subordinates),
        "subordinate_role_counts": role_counts,
        "subordinate_online_role_counts": online_counts,
        "online_subordinates": online_subordinates,
        "subordinates": subordinates,
    }


async def _get_agent_backend(session: AsyncSession, agent: Agent) -> tuple:
    """按项目 infra_group 解析 backend 与 agent_config，优先选组内 role=agent 的节点"""
    project = await session.get(Project, agent.project_id) if agent.project_id else None
    if not project:
        cfg = {**(agent.config or {}), "service_name": infra.agent_service_name(agent.project_id, agent.role)}
        return infra.get_backend(), cfg
    node = None
    if project.infra_group_id:
        from app.models import InfraGroupNode
        grp = await session.get(InfraGroup, project.infra_group_id, options=[
            selectinload(InfraGroup.nodes),
            selectinload(InfraGroup.node_assocs),
        ])
        if grp and grp.nodes:
            assoc_roles = {a.node_id: [r.upper() for r in (a.roles or ["AGENT"])] for a in grp.node_assocs}
            agent_nodes = [n for n in grp.nodes if "AGENT" in assoc_roles.get(n.id, ["AGENT"])]
            node = agent_nodes[0] if agent_nodes else grp.nodes[0]
    return infra.get_backend_for_agent(agent, node)


async def _auto_deploy_if_needed(session: AsyncSession, agent: Agent) -> bool:
    """若远程部署目录不存在，自动生成配置并推送，返回是否做了部署"""
    try:
        node = await deploy_manager.get_infra_node(session, agent.project_id)
        if not node:
            return False
        project = await session.get(Project, agent.project_id)
        project_map = project.role_model_map if project else None
        model_id = agent.model or (project_map or {}).get(agent.role, model_pool.ROLE_MODEL_MAP.get(agent.role, ""))
        await deploy_manager.generate_agent_deploy(
            session,
            project_id=agent.project_id,
            role=agent.role,
            model_id=model_id,
            allow_push_failure=True,
            agent_id_override=agent.id,
        )
        await session.refresh(agent)
        return not deployment_recovery.is_pending_remote_push(agent)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Auto-deploy for {agent.id} failed: {e}")
        return False


def _mark_agent_starting(agent: Agent):
    agent.last_heartbeat_status = "starting"
    agent.last_started_at = datetime.now(timezone.utc)
    agent.last_heartbeat = None
    agent.auto_restart_count = 0


def _should_try_auto_deploy(error: str) -> bool:
    msg = (error or "").lower()
    if "no such file or directory" not in msg:
        return False
    if _is_ssh_auth_error(msg):
        return False
    return True


def _is_ssh_auth_error(error: str) -> bool:
    msg = (error or "").lower()
    return (
        "identity file" in msg
        or "permission denied" in msg
        or "publickey" in msg
    )


def _compact_error_message(error: str, max_len: int = 220) -> str:
    text = " ".join((error or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


async def _cleanup_remote_dir(session: AsyncSession, agent: Agent):
    """删除远程部署目录"""
    try:
        from app.core.config import settings
        import asyncio
        node = await deploy_manager.get_infra_node(session, agent.project_id)
        if not node:
            return
        remote_dir = f"{settings.AGENT_DEPLOY_ROOT}/{agent.project_id}/{agent.id}"
        key_file = node.ssh_key_path or "~/.ssh/id_rsa"
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes",
            "-i", key_file, "-p", str(node.port),
            f"{node.user}@{node.host}", f"rm -rf {remote_dir}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception:
        pass


@router.post("/{agent_id}/start")
async def start_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """启动 Agent 容器（严格顺序：先确保部署目录就绪，再启动）"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    pending_push = deployment_recovery.is_pending_remote_push(agent)
    agent_lifecycle.before_mount(agent, trigger="api:start", pending_remote_push=pending_push)
    if pending_push:
        deployed = await _auto_deploy_if_needed(session, agent)
        if deployed:
            await session.refresh(agent)
        if deployment_recovery.is_pending_remote_push(agent):
            pending_err = deployment_recovery.pending_remote_push_error(agent)
            if _is_ssh_auth_error(pending_err):
                raise HTTPException(
                    409,
                    f"SSH 密钥对错误（私钥不可用或目标机未授权公钥）。{_compact_error_message(pending_err)}",
                )
            raise HTTPException(
                409,
                "部署目录尚未就绪，请先修复 SSH 并执行 reconcile-deploy，目录推送成功后再启动。",
            )
    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.start_agent(agent_id, agent.role, agent_config)

    if result.get("status") == "start_failed":
        err = result.get("error", "")
        if _should_try_auto_deploy(err):
            deployed = await _auto_deploy_if_needed(session, agent)
            if deployed:
                backend, agent_config = await _get_agent_backend(session, agent)
                result = await backend.start_agent(agent_id, agent.role, agent_config)

    if result.get("status") == "start_failed":
        agent.last_heartbeat_status = "start_failed"
        await session.commit()
        raise HTTPException(500, result.get("error", "容器启动命令执行失败"))

    _mark_agent_starting(agent)
    await session.commit()
    return {
        "status": "starting",
        "agent_id": agent_id,
        "message": "容器启动命令已发送，等待 Agent 报到（首次心跳）后才算 online",
    }


@router.post("/{agent_id}/stop")
async def stop_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.stop_agent(agent_id, agent.role, agent_config)
    released = await _release_agent_tasks(session, agent_id)
    agent.status = "idle"
    agent.current_task_id = None
    agent.last_heartbeat_status = "offline"
    await session.commit()
    return {**result, "released_tasks": released}


@router.post("/{agent_id}/restart")
async def restart_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.restart_agent(agent_id, agent.role, agent_config)
    _mark_agent_starting(agent)
    await session.commit()
    return result


@router.post("/{agent_id}/destroy")
async def destroy_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """销毁 Agent 容器（docker compose rm -f -s），释放其持有的任务"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    backend, agent_config = await _get_agent_backend(session, agent)
    ok = await backend.destroy_agent(agent_id, agent.container_id or "", agent_config)
    released = await _release_agent_tasks(session, agent_id)
    agent.status = "idle"
    agent.current_task_id = None
    agent.container_id = ""
    agent.last_heartbeat_status = "offline"
    await session.commit()
    return {"destroyed": ok, "agent_id": agent_id, "released_tasks": released}


@router.post("/{agent_id}/cc-spawn")
async def cc_spawn_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """手动启动 CC Worker 容器（用于 dispatcher 自动编排失败时的手动恢复）。"""
    from app.services import infra as infra_svc
    from sqlalchemy import select
    from app.models import InfraNode, InfraGroupNode, Project

    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent.role not in ("senior", "mid", "junior", "architect", "devops", "tester", "archaeologist"):
        raise HTTPException(400, f"Agent role {agent.role} is not a CC Worker role")

    # 查询项目关联的环境组节点
    node = None
    project = await session.get(Project, agent.project_id)
    if project and project.infra_group_id:
        result = await session.execute(
            select(InfraNode)
            .join(InfraGroupNode, InfraGroupNode.node_id == InfraNode.id)
            .where(InfraGroupNode.group_id == project.infra_group_id)
            .where(InfraNode.status == "connected")
        )
        nodes = result.scalars().all()
        if nodes:
            node = nodes[0]

    spawn_r = await infra_svc.spawn_cc_worker(
        agent_id=agent.id,
        role=agent.role,
        project_id=agent.project_id,
        node=node,
    )
    if spawn_r.get("ok"):
        agent.container_id = spawn_r.get("container_id", "")
        if node and spawn_r.get("node_id"):
            agent.config = {**(agent.config or {}), "infra_node_id": node.id, "infra_node_host": node.host}
        agent.status = "idle"
        agent.last_heartbeat_status = "starting"
        await session.commit()
    return {
        "ok": spawn_r.get("ok"),
        "agent_id": agent_id,
        "container_id": spawn_r.get("container_id", ""),
        "node_id": spawn_r.get("node_id", ""),
        "error": spawn_r.get("error", ""),
    }


@router.post("/{agent_id}/cc-destroy")
async def cc_destroy_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """手动销毁 CC Worker 容器。"""
    from app.services import infra as infra_svc
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    # 如果 Worker 运行在远程节点上，获取节点信息
    node = None
    node_id = (agent.config or {}).get("infra_node_id")
    if node_id:
        from app.models import InfraNode
        node = await session.get(InfraNode, node_id)

    result = await infra_svc.destroy_cc_worker(agent_id, node=node)
    released = await _release_agent_tasks(session, agent_id)
    agent.status = "idle"
    agent.current_task_id = None
    agent.container_id = ""
    agent.last_heartbeat_status = "offline"
    await session.commit()
    return {"destroyed": result.get("ok"), "agent_id": agent_id, "released_tasks": released}


@router.get("/{agent_id}/cc-status")
@router.post("/{agent_id}/cc-status")
async def cc_agent_status(agent_id: str, session: AsyncSession = Depends(get_session)):
    """查询 CC Worker 容器状态。"""
    from app.services import infra as infra_svc
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    status = await infra_svc.get_cc_worker_status(agent_id)
    return {
        "agent_id": agent_id,
        "role": agent.role,
        "container_id": agent.container_id,
        "db_status": agent.status,
        "heartbeat_status": agent.last_heartbeat_status,
        "container_status": status.get("status"),
        "container_running": status.get("running"),
    }


@router.post("/{agent_id}/redeploy")
async def redeploy_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """重新生成配置并推送到远程节点，然后重启容器"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    # 先停止
    try:
        backend, agent_config = await _get_agent_backend(session, agent)
        await backend.stop_agent(agent_id, agent.role, agent_config)
    except Exception:
        pass
    # 重新部署
    agent_lifecycle.before_mount(
        agent,
        trigger="api:redeploy",
        pending_remote_push=deployment_recovery.is_pending_remote_push(agent),
    )
    deployed = await _auto_deploy_if_needed(session, agent)
    if not deployed:
        raise HTTPException(500, "重新部署失败，请检查基础设施节点配置")
    # 启动
    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.start_agent(agent_id, agent.role, agent_config)
    _mark_agent_starting(agent)
    await session.commit()
    return {"status": "redeployed", "agent_id": agent_id, "start_result": result}


@router.post("/{agent_id}/reconcile-deploy")
async def reconcile_deploy(agent_id: str, session: AsyncSession = Depends(get_session)):
    """
    补偿型部署：用于 pending_remote_push 场景。
    先重推部署目录，再启动容器。
    """
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)

    agent_lifecycle.before_mount(
        agent,
        trigger="api:reconcile-deploy",
        pending_remote_push=deployment_recovery.is_pending_remote_push(agent),
    )
    deployed = await _auto_deploy_if_needed(session, agent)
    if not deployed:
        pending_err = deployment_recovery.pending_remote_push_error(agent)
        if _is_ssh_auth_error(pending_err):
            raise HTTPException(
                409,
                f"SSH 密钥对错误（私钥不可用或目标机未授权公钥）。{_compact_error_message(pending_err)}",
            )
        raise HTTPException(500, "补偿部署失败，请检查节点连通性与 SSH 免密配置")

    await session.refresh(agent)
    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.start_agent(agent_id, agent.role, agent_config)
    if result.get("status") == "start_failed":
        agent.last_heartbeat_status = "start_failed"
        await session.commit()
        raise HTTPException(500, result.get("error", "容器启动命令执行失败"))

    _mark_agent_starting(agent)
    await session.commit()
    return {
        "status": "reconciling",
        "agent_id": agent_id,
        "message": "已完成补偿部署并触发启动，等待首次心跳确认 online",
        "start_result": result,
    }


@router.post("/{agent_id}/hot-update")
async def hot_update_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """热更新：只更新 connector/entrypoint 并重启容器，保留所有 Agent 数据"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)

    from app.services.claw_deployer import push_connector_update
    node = await deploy_manager.get_infra_node(session, agent.project_id)
    if not node:
        raise HTTPException(400, "未配置基础设施节点")

    remote_dir = f"{settings.AGENT_DEPLOY_ROOT}/{agent.project_id}/{agent.id}"
    push_result = await push_connector_update(
        host=node.host,
        remote_dir=remote_dir,
        user=node.user,
        port=node.port,
        key_file=node.ssh_key_path or "~/.ssh/id_rsa",
    )
    if push_result.get("exit_code") != 0:
        raise HTTPException(500, f"推送失败: {push_result.get('error', '')}")

    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.restart_agent(agent_id, agent.role, agent_config)
    _mark_agent_starting(agent)
    await session.commit()
    return {
        "status": "hot_updated",
        "agent_id": agent_id,
        "updated_files": push_result.get("updated_files", 0),
        "restart_result": result,
    }


@router.post("/batch-hot-update/{project_id}")
async def batch_hot_update(project_id: str, session: AsyncSession = Depends(get_session)):
    """批量热更新项目所有 Agent 的 connector，保留所有数据"""
    q = await session.execute(select(Agent).where(Agent.project_id == project_id))
    agents = q.scalars().all()
    if not agents:
        raise HTTPException(404, "该项目无 Agent")

    from app.services.claw_deployer import push_connector_update
    node = await deploy_manager.get_infra_node(session, project_id)
    if not node:
        raise HTTPException(400, "未配置基础设施节点")

    results = []
    for agent in agents:
        try:
            remote_dir = f"{settings.AGENT_DEPLOY_ROOT}/{project_id}/{agent.id}"
            push_result = await push_connector_update(
                host=node.host, remote_dir=remote_dir,
                user=node.user, port=node.port,
                key_file=node.ssh_key_path or "~/.ssh/id_rsa",
            )
            if push_result.get("exit_code") != 0:
                results.append({"agent_id": agent.id, "status": "push_failed", "error": push_result.get("error")})
                continue

            backend, agent_config = await _get_agent_backend(session, agent)
            await backend.restart_agent(agent.id, agent.role, agent_config)
            _mark_agent_starting(agent)
            results.append({"agent_id": agent.id, "status": "hot_updated"})
        except Exception as e:
            results.append({"agent_id": agent.id, "status": "error", "error": str(e)})

    await session.commit()
    return {"project_id": project_id, "total": len(agents), "results": results}


async def _background_cleanup(agent_id: str, project_id: str, container_id: str, config: dict | None):
    """后台清理：销毁容器 + 删除远程目录，不阻塞 API 响应"""
    import logging as _log
    logger = _log.getLogger(__name__)
    try:
        from app.core.database import async_session
        async with async_session() as session:
            agent_stub = await session.get(Agent, agent_id)
            if agent_stub:
                return
            node = await deploy_manager.get_infra_node(session, project_id)
            if not node:
                return
            backend_cfg = {
                "host": node.host, "port": node.port, "user": node.user,
                "key_file": node.ssh_key_path or "~/.ssh/id_rsa",
                "project_dir": f"{settings.AGENT_DEPLOY_ROOT}/{project_id}/{agent_id}",
                "compose_file": f"{settings.AGENT_DEPLOY_ROOT}/{project_id}/{agent_id}/docker-compose.yml",
            }
            backend = infra.SSHBackend(backend_cfg)
            svc = config.get("service_name", agent_id) if config else agent_id
            await backend.destroy_agent(agent_id, container_id, {"service_name": svc})
            remote_dir = f"{settings.AGENT_DEPLOY_ROOT}/{project_id}/{agent_id}"
            key_file = node.ssh_key_path or "~/.ssh/id_rsa"
            cmd = [
                "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes",
                "-i", key_file, "-p", str(node.port),
                f"{node.user}@{node.host}", f"rm -rf {remote_dir}",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            logger.info(f"Background cleanup done: {agent_id}")
    except Exception as e:
        logger.warning(f"Background cleanup {agent_id}: {e}")


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    """立即删除 Agent 记录并释放任务，容器销毁和目录清理在后台异步执行。"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    released = await _release_agent_tasks(session, agent_id)
    project_id = agent.project_id
    container_id = agent.container_id or ""
    agent_config = dict(agent.config or {})
    await session.delete(agent)
    await session.commit()
    t = asyncio.create_task(_background_cleanup(agent_id, project_id, container_id, agent_config))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return {"deleted": agent_id, "released_tasks": released}


@router.get("/{agent_id}/health")
async def agent_health(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    backend, agent_config = await _get_agent_backend(session, agent)
    container_alive = await backend.health_check(agent_id, agent_config)
    return {
        "agent_id": agent_id,
        "heartbeat_status": agent.last_heartbeat_status,
        "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        "container_alive": container_alive,
        "status": agent.status,
    }


@router.post("/{agent_id}/rebuild-context")
async def rebuild_context(agent_id: str, session: AsyncSession = Depends(get_session)):
    """从数据库重建 Agent 的工作上下文（用于会话数据丢失后恢复）"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)

    tasks_q = await session.execute(
        select(Task).where(
            Task.assigned_agent == agent_id,
        ).order_by(Task.updated_at.desc()).limit(5)
    )
    recent_tasks = list(tasks_q.scalars())

    logs_q = await session.execute(
        select(TaskLog).where(
            TaskLog.agent_id == agent_id,
        ).order_by(TaskLog.created_at.desc()).limit(20)
    )
    recent_logs = list(logs_q.scalars())

    context_parts = [
        f"# Agent 上下文恢复 - {agent.role}",
        f"\n## 角色: {agent.role}",
        f"## 项目: {agent.project_id}",
    ]

    if recent_tasks:
        context_parts.append("\n## 最近任务")
        for t in recent_tasks:
            context_parts.append(
                f"- [{t.status}] {t.title}: {(t.description or '')[:200]}"
            )

    if recent_logs:
        context_parts.append("\n## 最近操作日志")
        for log in reversed(recent_logs):
            context_parts.append(
                f"- [{log.action}] {log.message[:200]}"
            )

    context_doc = "\n".join(context_parts)

    result = await openclaw.send_message(
        agent_id, f"[系统恢复] 你的会话数据已重建，以下是你的工作上下文：\n\n{context_doc}"
    )

    return {
        "status": "ok",
        "context_length": len(context_doc),
        "tasks_recovered": len(recent_tasks),
        "logs_recovered": len(recent_logs),
        "send_result": result,
    }


@router.post("/{agent_id}/message")
async def send_agent_message(agent_id: str, body: AgentMessage, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    return await openclaw.send_message(agent_id, body.content)


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    agent.status = "paused"
    await session.commit()
    return {"status": "paused"}


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    released = await _release_agent_tasks(session, agent_id)
    agent.status = "idle"
    agent.current_task_id = None
    await session.commit()
    return {"status": "idle", "released_tasks": released}


@router.post("/reset-team/{project_id}")
async def reset_team(project_id: str, session: AsyncSession = Depends(get_session)):
    """重置项目所有 Agent 为 idle，释放其持有的任务，用于团队卡死时恢复"""
    q = await session.execute(select(Agent).where(Agent.project_id == project_id))
    agents = q.scalars().all()
    if not agents:
        raise HTTPException(404, "该项目无 Agent")
    reset = []
    all_released = []
    for agent in agents:
        released = await _release_agent_tasks(session, agent.id)
        all_released.extend(released)
        agent.status = "idle"
        agent.current_task_id = None
        agent.last_heartbeat_status = "offline"
        reset.append(agent.id)
    await session.commit()
    return {"reset": reset, "count": len(reset), "released_tasks": all_released}


@router.post("/stop-team/{project_id}")
async def stop_team(project_id: str, force: bool = False, session: AsyncSession = Depends(get_session)):
    """关闭团队。默认优雅关闭（等任务完成），force=true 强制释放任务并停容器"""
    q = await session.execute(select(Agent).where(Agent.project_id == project_id))
    agents = q.scalars().all()
    if not agents:
        raise HTTPException(404, "该项目无 Agent")

    stopped = []
    waiting = []
    released = []
    errors = []

    for agent in agents:
        has_task = agent.current_task_id and agent.status == "busy"

        if has_task and not force:
            # 优雅模式：标记为 draining（不再接新任务），等当前任务完成
            agent.status = "draining"
            waiting.append({"agent_id": agent.id, "task_id": agent.current_task_id})
        else:
            # 强制模式或空闲 Agent：释放任务 → 停容器
            rel = await _release_agent_tasks(session, agent.id)
            released.extend(rel)
            agent.status = "idle"
            agent.current_task_id = None
            try:
                backend, agent_config = await _get_agent_backend(session, agent)
                await backend.stop_agent(agent.id, agent.role, agent_config)
                agent.last_heartbeat_status = "offline"
                stopped.append(agent.id)
            except Exception as e:
                errors.append({"agent_id": agent.id, "error": str(e)})

    await session.commit()
    return {
        "stopped": stopped,
        "waiting": waiting,
        "released_tasks": released,
        "errors": errors,
        "message": f"已停止 {len(stopped)} 个，{len(waiting)} 个等待任务完成后自动停止" if waiting else None,
    }


class ExecBody(BaseModel):
    command: str
    timeout: int = 60


@router.post("/{agent_id}/exec")
async def exec_in_agent(agent_id: str, body: ExecBody, session: AsyncSession = Depends(get_session)):
    """在 Agent 容器内执行命令（需配置 infrastructure）"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    backend, agent_config = await _get_agent_backend(session, agent)
    result = await backend.exec_command(agent_id, body.command, body.timeout, agent_config)
    if result.get("exit_code") != 0 and "no such file or directory" in result.get("stderr", "").lower():
        raise HTTPException(400, f"Agent 尚未部署到远程节点，请先点击「启动」（会自动部署）。原始错误: {result['stderr']}")
    return result


@router.get("/{agent_id}/logs")
async def agent_logs(agent_id: str, tail: int = 100, session: AsyncSession = Depends(get_session)):
    """获取 Agent 容器的 stdout/stderr 日志"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    backend, agent_config = await _get_agent_backend(session, agent)
    output = await backend.logs(agent_id, tail=tail, config=agent_config)
    if isinstance(output, str) and "no such file or directory" in output.lower():
        raise HTTPException(400, "Agent 尚未部署到远程节点，请先点击「启动」（会自动部署）")
    return {"agent_id": agent_id, "tail": tail, "logs": output}


# ── 备份/恢复 ──

class AgentBackupRequest(BaseModel):
    backup_mode: str = backup_svc.BACKUP_MODE_METADATA_ONLY  # metadata_only | full_source

@router.post("/{agent_id}/backup")
async def backup_agent_workspace(
    agent_id: str,
    body: AgentBackupRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404)
    if not agent.workspace_path:
        raise HTTPException(400, "Agent 未配置 workspace_path，请先完成部署")
    backup_mode = backup_svc.normalize_backup_mode(body.backup_mode if body else None)
    b = await backup_svc.backup_agent(session, agent, backup_mode=backup_mode)
    if not b:
        raise HTTPException(504, "备份超时或 Connector 未响应，请确认 Agent 在线且能连接 Redis")
    contains_source_code = bool((b.metadata_ or {}).get("include_workspace", True))
    return {
        "backup_id": b.id,
        "file_path": b.file_path,
        "file_size": b.file_size,
        "backup_mode": (b.metadata_ or {}).get("backup_mode", backup_mode),
        "contains_source_code": contains_source_code,
        "warning": (
            "当前备份包含源码（workspace）。请确认已获得用户授权并满足合规要求。"
            if contains_source_code else
            "当前备份不包含源码（metadata_only）。请勿将其当作源码灾备。"
        ),
    }


@router.post("/{agent_id}/restore/{backup_id}")
async def restore_agent_workspace(agent_id: str, backup_id: str, session: AsyncSession = Depends(get_session)):
    agent = await session.get(Agent, agent_id)
    backup = await session.get(Backup, backup_id)
    if not agent or not backup:
        raise HTTPException(404)
    ok = await backup_svc.restore_agent(session, agent, backup)
    if not ok:
        contains_source_code = bool((backup.metadata_ or {}).get("include_workspace", True))
        if not contains_source_code:
            raise HTTPException(400, "该备份为 metadata_only，不包含源码，无法恢复 workspace")
        raise HTTPException(500, "Restore failed")
    return {"status": "restored"}


@router.get("/{agent_id}/backups")
async def list_agent_backups(agent_id: str, session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(Backup).where(Backup.agent_id == agent_id).order_by(Backup.created_at.desc())
    )
    return [{
        "id": b.id, "backup_type": b.backup_type, "file_path": b.file_path,
        "file_size": b.file_size,
        "backup_mode": (b.metadata_ or {}).get("backup_mode", "unknown"),
        "contains_source_code": bool((b.metadata_ or {}).get("include_workspace", True)),
        "created_at": b.created_at.isoformat(),
    } for b in q.scalars()]
