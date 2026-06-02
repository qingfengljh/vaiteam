from datetime import datetime, timezone, timedelta
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, field_validator

from app.core.database import get_session
from app.execution_hints import (
    ExecutionHintError,
    merge_assign_hints_into_context,
    normalize_actor_type,
    normalize_executor_hint,
    resolved_executor_hint,
)
from app.models import Project, Task, TaskLog, Iteration, Agent, AgentTeam, new_id
from app.services import scheduler, task_lifecycle, model_config_notice
from sqlalchemy.orm.attributes import flag_modified

router = APIRouter(prefix="/api", tags=["tasks"])


def _is_prototype_fast_track(project: Project | None) -> bool:
    cfg = (project.config or {}) if project else {}
    return bool(cfg.get("prototype_fast_track"))


def _is_mock_data_mode(project: Project | None) -> bool:
    cfg = (project.config or {}) if project else {}
    return bool(cfg.get("mock_data_mode"))


async def _get_task_for_update(session: AsyncSession, task_id: str) -> Task | None:
    q = await session.execute(
        select(Task).where(Task.id == task_id).with_for_update()
    )
    return q.scalar_one_or_none()


def _project_git_branches(project: Project | None) -> tuple[str, str]:
    cfg = (project.config or {}) if project else {}
    git_cfg = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    integration = (git_cfg.get("integration_branch") or "develop").strip() or "develop"
    production = (git_cfg.get("production_branch") or "main").strip() or "main"
    return integration, production


def _task_base_branch(task: Task, project: Project | None) -> str:
    ctx = task.context or {}
    if isinstance(ctx, dict) and ctx.get("git_base_branch"):
        return str(ctx.get("git_base_branch"))
    integration, production = _project_git_branches(project)
    return production if (task.type or "").lower() in ("bug", "hotfix", "fix") else integration


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    type: str = "feature"
    priority: int = 0
    suggested_role: str = "mid"
    suggested_model: str = "sonnet"
    complexity: str = "medium"
    estimated_hours: float = 0.5
    dependencies: list[str] = Field(default_factory=list)
    input_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    executor_hint: str | None = None
    actor_type: str | None = None

    @field_validator("executor_hint", mode="before")
    @classmethod
    def _v_executor(cls, v):
        if v is None or v == "":
            return None
        return normalize_executor_hint(v)

    @field_validator("actor_type", mode="before")
    @classmethod
    def _v_actor(cls, v):
        if v is None or v == "":
            return None
        return normalize_actor_type(v)


class TaskBatchCreate(BaseModel):
    tasks: list[TaskCreate]


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    suggested_role: str | None = None
    suggested_model: str | None = None
    estimated_hours: float | None = None
    acceptance_criteria: list[str] | None = None
    executor_hint: str | None = None
    actor_type: str | None = None

    @field_validator("executor_hint", mode="before")
    @classmethod
    def _vu_executor(cls, v):
        if v is None or v == "":
            return None
        return normalize_executor_hint(v)

    @field_validator("actor_type", mode="before")
    @classmethod
    def _vu_actor(cls, v):
        if v is None or v == "":
            return None
        return normalize_actor_type(v)

class TaskReviewBody(BaseModel):
    action: str = "approve"  # approve | reject
    comments: str = ""
    task_ids: list[str] = Field(default_factory=list)


class DesignReviewBody(BaseModel):
    action: str = "approve"  # approve | reject
    comments: str = ""
    reviewer: str = "human"


class HumanClaimBody(BaseModel):
    username: str = ""


class AutoAssignBody(BaseModel):
    task_ids: list[str] = Field(default_factory=list)
    limit: int = 0


class ManualAssignBody(BaseModel):
    agent_id: str
    actor: str = "human"
    note: str = ""


class HumanCompleteBody(BaseModel):
    username: str = ""
    summary: str = ""
    commits: list[dict] = Field(default_factory=list)


class ResolveBlockedBody(BaseModel):
    action: str = "reassign_ai"  # reassign_ai | claim_human
    username: str = ""
    actor_role: str = "human"
    note: str = ""
    reset_to_level: int = 0  # reassign_ai 时重置到哪个层级（0=编码AI, 1=架构师）


class ModuleRoleModelMapBody(BaseModel):
    role_model_map: dict[str, str]


class ReentryPolicyBody(BaseModel):
    reentry_requested: bool | None = None
    prefer_resume: bool | None = None
    allow_partial_restart: bool | None = None
    human_fallback: bool | None = None
    note: str = ""
    actor: str = "human"


class ReviewPolicyBody(BaseModel):
    require_human_review: bool
    note: str = ""
    actor: str = "human"


class CollaborationModeBody(BaseModel):
    mode: str = "autonomous"  # autonomous | human_guided
    actor: str = "human"
    note: str = ""


class CollaborationDecisionBody(BaseModel):
    decision: str = "approve_execute"  # approve_execute | revise_plan
    actor: str = "human"
    note: str = ""


def _append_reentry_chain(task: Task, stage: str, payload: dict | None = None):
    ctx = dict(task.context or {})
    chain = list(ctx.get("reentry_chain") or [])
    chain.append({
        "stage": stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **(payload or {}),
    })
    ctx["reentry_chain"] = chain[-30:]
    task.context = ctx


@router.post("/projects/{project_id}/tasks")
async def create_tasks(project_id: str, body: TaskBatchCreate, session: AsyncSession = Depends(get_session)):
    created = []
    project = await session.get(Project, project_id)
    fast_track = _is_prototype_fast_track(project)
    mock_mode = _is_mock_data_mode(project)
    for t in body.tasks:
        payload = t.model_dump()
        executor_hint = payload.pop("executor_hint", None)
        actor_type = payload.pop("actor_type", None)
        complexity = (payload.get("complexity") or "medium").strip().lower()
        payload["complexity"] = complexity
        task = Task(project_id=project_id, **payload)
        if complexity in ("medium", "high", "critical") and not fast_track:
            ctx = dict(task.context or {})
            if not ctx.get("design_phase"):
                ctx["design_phase"] = "needs_discussion"
            task.context = ctx
            task.requires_design_review = True
        else:
            ctx = dict(task.context or {})
            if fast_track:
                ctx["design_phase"] = "none"
                ctx["prototype_fast_track"] = True
            ctx["data_mode"] = "mock" if mock_mode else "api"
            task.context = ctx
            task.requires_design_review = False
        if executor_hint is not None:
            ctx = dict(task.context or {})
            ctx["executor_hint"] = executor_hint
            task.context = ctx
        if actor_type is not None:
            ctx = dict(task.context or {})
            ctx["actor_type"] = actor_type
            task.context = ctx
        session.add(task)
        await session.flush()
        created.append(task.id)
    await session.commit()
    return {"created": created, "count": len(created)}


@router.get("/projects/{project_id}/tasks")
async def list_tasks(
    project_id: str,
    status: str | None = None,
    role: str | None = None,
    parent_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_session),
):
    q = select(Task).where(Task.project_id == project_id).order_by(Task.priority.desc(), Task.created_at)
    if status:
        q = q.where(Task.status == status)
    if role:
        q = q.where(Task.suggested_role == role)
    if parent_id:
        q = q.where(Task.parent_task_id == parent_id)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await session.execute(count_q)).scalar() or 0

    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(q)
    items = [_task_dict(t) for t in result.scalars()]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/projects/{project_id}/tasks/grouped")
async def list_tasks_grouped(project_id: str, page: int = 1, page_size: int = 20, session: AsyncSession = Depends(get_session)):
    """按模块分组的任务列表，分页按子任务条数"""
    modules_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.parent_task_id == None,  # noqa: E711
            Task.context["is_module"].astext == "true",
        ).order_by(Task.priority.desc(), Task.created_at)
    )
    modules = list(modules_q.scalars())

    subtasks_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.parent_task_id != None,  # noqa: E711
        ).order_by(Task.parent_task_id, Task.priority.desc(), Task.created_at)
    )
    all_subtasks = list(subtasks_q.scalars())

    terminal = {"done", "cancelled", "superseded"}
    mod_ref_map = {m.id: m.ref_id for m in modules}

    # 计算每个模块的完成状态
    module_completed: dict[str, bool] = {}
    for mod in modules:
        children_statuses = [t.status for t in all_subtasks if t.parent_task_id == mod.id]
        module_completed[mod.id] = bool(children_statuses) and all(s in terminal for s in children_statuses)

    total_subtasks = len(all_subtasks)
    start = (page - 1) * page_size
    paged_subtasks = all_subtasks[start:start + page_size]

    groups = []
    for mod in modules:
        children = [_task_dict(t) for t in paged_subtasks if t.parent_task_id == mod.id]
        mod_dict = _task_dict(mod)
        mod_dict["children"] = children
        mod_dict["total_children"] = sum(1 for t in all_subtasks if t.parent_task_id == mod.id)
        mod_dict["done_children"] = sum(1 for t in all_subtasks if t.parent_task_id == mod.id and t.status in terminal)
        mod_dict["module_completed"] = module_completed.get(mod.id, False)
        # 标记被哪些模块阻塞
        blocked_by = [mod_ref_map.get(d, d[:8]) for d in (mod.dependencies or []) if not module_completed.get(d, False)]
        mod_dict["blocked_by"] = blocked_by
        groups.append(mod_dict)

    orphans = [_task_dict(t) for t in paged_subtasks if not t.parent_task_id]
    if orphans:
        groups.append({"id": None, "title": "未分组", "children": orphans, "total_children": len(orphans), "done_children": 0})

    return {"groups": groups, "total": total_subtasks, "page": page, "page_size": page_size}


@router.get("/projects/{project_id}/architect-task-pool")
async def architect_task_pool(
    project_id: str,
    team_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """
    架构师任务池视图（模块级）：
    - 默认返回默认团队架构师负责的模块池
    - 可传 team_id 查看指定团队模块池
    """
    team: AgentTeam | None = None
    if team_id:
        team = await session.get(AgentTeam, team_id)
        if not team or team.project_id != project_id:
            raise HTTPException(404, "团队不存在")
    else:
        default_team_q = await session.execute(
            select(AgentTeam).where(
                AgentTeam.project_id == project_id,
                AgentTeam.is_default == True,  # noqa: E712
            ).limit(1)
        )
        team = default_team_q.scalar_one_or_none()
        if not team:
            raise HTTPException(404, "默认团队不存在")

    arch_q = await session.execute(
        select(Agent).where(Agent.team_id == team.id, Agent.role == "architect").limit(1)
    )
    architect = arch_q.scalar_one_or_none()

    module_ids = team.module_task_ids or []
    modules: list[Task] = []
    for module_id in module_ids:
        mod = await session.get(Task, module_id)
        if mod and mod.project_id == project_id:
            modules.append(mod)

    module_items = []
    terminal = {"done", "cancelled", "superseded"}
    for mod in modules:
        child_q = await session.execute(
            select(Task).where(Task.parent_task_id == mod.id)
        )
        children = list(child_q.scalars())
        done_count = sum(1 for c in children if c.status in terminal)
        pending_count = sum(1 for c in children if c.status == "pending")
        draft_count = sum(1 for c in children if c.status == "draft")
        completed = bool(children) and done_count == len(children)

        mod_ctx = mod.context or {}
        manifest = mod_ctx.get("subtask_manifest") if isinstance(mod_ctx.get("subtask_manifest"), dict) else {}
        manifest_items = manifest.get("items") if isinstance(manifest, dict) and isinstance(manifest.get("items"), list) else []
        unfinished_manifest = sum(1 for item in manifest_items if item.get("status") != "done")

        module_items.append({
            "id": mod.id,
            "ref_id": mod.ref_id,
            "title": mod.title,
            "status": mod.status,
            "dependencies": mod.dependencies or [],
            "subtask_breakdown_completed": bool(mod_ctx.get("subtask_breakdown_completed")),
            "unfinished_manifest_count": unfinished_manifest,
            "subtask_total": len(children),
            "subtask_done": done_count,
            "subtask_pending": pending_count,
            "subtask_draft": draft_count,
            "module_completed": completed,
        })

    return {
        "project_id": project_id,
        "team": {
            "id": team.id,
            "name": team.name,
            "is_default": bool(team.is_default),
        },
        "architect": {
            "id": architect.id if architect else "",
            "status": architect.status if architect else "missing",
            "heartbeat_status": architect.last_heartbeat_status if architect else "missing",
        },
        "module_count": len(module_items),
        "modules": module_items,
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    logs_q = await session.execute(
        select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at)
    )
    d = _task_dict(task)
    d["logs"] = [{"action": l.action, "agent_id": l.agent_id, "message": l.message, "created_at": l.created_at.isoformat()} for l in logs_q.scalars()]
    return d


FROZEN_STATUSES = {"pending", "assigned", "executing", "reviewing", "done", "blocked", "failed"}


@router.put("/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdate, session: AsyncSession = Depends(get_session)):
    """只有 draft 状态的任务可编辑，审核通过后冻结"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)

    if task.status in FROZEN_STATUSES:
        raise HTTPException(403, f"任务已{task.status}，不可修改。draft 状态才可编辑。")

    data = body.model_dump(exclude_none=True)
    executor_hint = data.pop("executor_hint", None)
    actor_type = data.pop("actor_type", None)
    for k, v in data.items():
        setattr(task, k, v)
    if executor_hint is not None or actor_type is not None:
        ctx = dict(task.context or {})
        if executor_hint is not None:
            ctx["executor_hint"] = executor_hint
        if actor_type is not None:
            ctx["actor_type"] = actor_type
        task.context = ctx
        flag_modified(task, "context")
    await session.commit()
    return _task_dict(task)


from app.core.constants import VALID_ROLES as _VALID_ROLES, ROLE_MIGRATION as _ROLE_MIGRATION


def _clean_role_map(m: dict) -> dict:
    cleaned = {}
    for k, v in m.items():
        if k in _VALID_ROLES:
            cleaned[k] = v
        elif k in _ROLE_MIGRATION and _ROLE_MIGRATION[k] not in cleaned:
            cleaned[_ROLE_MIGRATION[k]] = v
    return cleaned


@router.patch("/tasks/{task_id}/role_model_map")
async def update_module_role_model_map(
    task_id: str, body: ModuleRoleModelMapBody, session: AsyncSession = Depends(get_session)
):
    """仅模块任务可配置 role_model_map，用于该模块下所有子任务的模型选择"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    if not (task.context or {}).get("is_module"):
        raise HTTPException(400, "仅模块任务（Stage 4 拆出的 MOD-xxx）可配置 role_model_map")
    task.context = dict(task.context or {})
    old_map = _clean_role_map(task.context.get("role_model_map") or {})
    task.context["role_model_map"] = _clean_role_map(body.role_model_map)
    new_map = _clean_role_map(task.context.get("role_model_map") or {})
    flag_modified(task, "context")
    await session.commit()
    if new_map != old_map:
        await model_config_notice.notify_model_config_changed(
            session,
            scope="module",
            project_id=task.project_id,
            module_task_id=task.id,
            reason="module_role_model_map_updated",
        )
    return _task_dict(task)


@router.patch("/tasks/{task_id}/reentry-policy")
async def update_reentry_policy(
    task_id: str,
    body: ReentryPolicyBody,
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    ctx = dict(task.context or {})
    if body.reentry_requested is not None:
        ctx["reentry_requested"] = body.reentry_requested
    policy = dict(ctx.get("continuation_policy") or {})
    if body.prefer_resume is not None:
        policy["prefer_resume"] = body.prefer_resume
    if body.allow_partial_restart is not None:
        policy["allow_partial_restart"] = body.allow_partial_restart
    if body.human_fallback is not None:
        policy["human_fallback"] = body.human_fallback
    if policy:
        ctx["continuation_policy"] = policy
    task.context = ctx
    _append_reentry_chain(task, "policy_updated", {
        "actor": body.actor or "human",
        "reentry_requested": ctx.get("reentry_requested"),
        "continuation_policy": policy,
        "note": body.note or "",
    })
    session.add(TaskLog(
        task_id=task.id,
        agent_id=body.actor or "human",
        action="reentry_policy_updated",
        message="已更新重入/续跑策略" + (f": {body.note}" if body.note else ""),
        metadata_={
            "reentry_requested": ctx.get("reentry_requested"),
            "continuation_policy": policy,
        },
    ))
    await session.commit()
    return _task_dict(task)


@router.patch("/tasks/{task_id}/review-policy")
async def update_review_policy(
    task_id: str,
    body: ReviewPolicyBody,
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    ctx = dict(task.context or {})
    ctx["require_human_review"] = bool(body.require_human_review)
    tags = set(ctx.get("review_tags") or [])
    if body.require_human_review:
        tags.add("human_required")
    else:
        tags.discard("human_required")
    ctx["review_tags"] = sorted(tags)
    task.context = ctx
    session.add(TaskLog(
        task_id=task.id,
        agent_id=body.actor or "human",
        action="review_policy_updated",
        message=(
            "已标记为必须人工审核"
            if body.require_human_review
            else "已取消必须人工审核，恢复无人值守自动审核"
        ) + (f": {body.note}" if body.note else ""),
        metadata_={
            "require_human_review": bool(body.require_human_review),
            "review_tags": ctx["review_tags"],
        },
    ))
    await session.commit()
    return _task_dict(task)


@router.patch("/tasks/{task_id}/collaboration-mode")
async def update_collaboration_mode(
    task_id: str,
    body: CollaborationModeBody,
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    mode = (body.mode or "autonomous").strip().lower()
    if mode not in ("autonomous", "human_guided"):
        raise HTTPException(400, "mode must be autonomous or human_guided")
    ctx = dict(task.context or {})
    ctx["collaboration_mode"] = mode
    if mode == "autonomous":
        ctx.pop("human_execute_approved", None)
        ctx.pop("human_decision_required", None)
        ctx.pop("human_last_decision", None)
    else:
        ctx["human_execute_approved"] = False
    task.context = ctx
    session.add(TaskLog(
        task_id=task.id,
        agent_id=body.actor or "human",
        action="collaboration_mode_updated",
        message=("已切换到人类主导协作模式" if mode == "human_guided" else "已切换到自主执行模式")
        + (f": {body.note}" if body.note else ""),
        metadata_={"collaboration_mode": mode},
    ))
    await session.commit()
    return _task_dict(task)


@router.post("/tasks/{task_id}/collaboration-decision")
async def submit_collaboration_decision(
    task_id: str,
    body: CollaborationDecisionBody,
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    ctx = dict(task.context or {})
    if ctx.get("collaboration_mode") != "human_guided":
        raise HTTPException(400, "task is not in human_guided mode")
    decision = (body.decision or "").strip().lower()
    if decision not in ("approve_execute", "revise_plan"):
        raise HTTPException(400, "decision must be approve_execute or revise_plan")
    if task.status not in ("blocked", "reviewing", "pending", "assigned", "executing"):
        raise HTTPException(400, f"任务状态为 {task.status}，当前不可做协作决策")

    ctx["human_decision_required"] = False
    ctx["human_last_decision"] = decision
    ctx["human_last_decision_note"] = body.note or ""
    ctx["human_last_decision_by"] = body.actor or "human"
    ctx["human_last_decision_at"] = datetime.now(timezone.utc).isoformat()
    if decision == "approve_execute":
        ctx["human_execute_approved"] = True
        msg = "人类已批准执行，任务回到调度队列"
    else:
        ctx["human_execute_approved"] = False
        msg = "人类要求重做方案，任务回到调度队列"
    task.context = ctx
    task.assigned_agent = None
    session.add(task_lifecycle.transition(
        task,
        event="requeue",
        actor=body.actor or "human",
        reason=f"collaboration decision: {decision}",
    ))
    session.add(TaskLog(
        task_id=task.id,
        agent_id=body.actor or "human",
        action="collaboration_decision",
        message=msg + (f": {body.note}" if body.note else ""),
        metadata_={"decision": decision},
    ))
    await session.commit()
    await scheduler.auto_assign(session, task.project_id, actor_role="human")
    return _task_dict(task)


@router.post("/tasks/{task_id}/review")
async def review_task(task_id: str, body: TaskReviewBody, session: AsyncSession = Depends(get_session)):
    """审核单个任务：approve → pending（可分派），reject → 打回 draft"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    if task.status != "draft":
        raise HTTPException(400, f"只有 draft 状态的任务可审核，当前: {task.status}")

    if body.action == "approve":
        session.add(task_lifecycle.transition(
            task,
            event="ready",
            actor="human",
            reason="review approved by human",
        ))
        session.add(TaskLog(task_id=task_id, agent_id="human", action="approved",
                            message=body.comments or "审核通过，任务可分派"))
    else:
        session.add(TaskLog(task_id=task_id, agent_id="human", action="rejected",
                            message=body.comments or "审核未通过，请修改"))
    await session.commit()

    if body.action == "approve":
        await scheduler.auto_assign(session, task.project_id, actor_role="human")

    return {"status": task.status, "task_id": task_id}


@router.post("/projects/{project_id}/tasks/batch-review")
async def batch_review_tasks(project_id: str, body: TaskReviewBody, session: AsyncSession = Depends(get_session)):
    """批量审核：一键通过/驳回多个 draft 任务"""
    if not body.task_ids:
        q = await session.execute(
            select(Task).where(Task.project_id == project_id, Task.status == "draft")
        )
        tasks = list(q.scalars())
    else:
        tasks = []
        for tid in body.task_ids:
            t = await session.get(Task, tid)
            if t and t.project_id == project_id and t.status == "draft":
                tasks.append(t)

    reviewed = []
    for task in tasks:
        if body.action == "approve":
            session.add(task_lifecycle.transition(
                task,
                event="ready",
                actor="human",
                reason="batch review approved",
            ))
            session.add(TaskLog(task_id=task.id, agent_id="human", action="approved",
                                message=body.comments or "批量审核通过"))
        else:
            session.add(TaskLog(task_id=task.id, agent_id="human", action="rejected",
                                message=body.comments or "批量驳回"))
        reviewed.append(task.id)

    await session.commit()

    if body.action == "approve" and reviewed:
        await scheduler.auto_assign(session, project_id, actor_role="human")

    return {"reviewed": reviewed, "count": len(reviewed), "action": body.action}


@router.post("/tasks/{task_id}/claim")
async def human_claim_task(task_id: str, body: HumanClaimBody, session: AsyncSession = Depends(get_session)):
    """人类认领任务：标记为人类接手，显示 Git 分支指引"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    if task.status not in ("pending", "failed", "blocked"):
        raise HTTPException(400, f"任务状态为 {task.status}，无法认领")

    username = body.username or "human"
    task.assigned_agent = f"human:{username}"
    session.add(task_lifecycle.on_assigned(
        task,
        agent_id=task.assigned_agent,
        model="human",
        actor=task.assigned_agent,
    ))

    session.add(TaskLog(
        task_id=task_id,
        agent_id=f"human:{username}",
        action="claimed",
        message=f"人类 {username} 认领了此任务",
    ))
    project = await session.get(Project, task.project_id)
    if project:
        if project.current_stage == 4:
            project.current_stage = 5
            if project.current_iteration_id:
                it = await session.get(Iteration, project.current_iteration_id)
                if it:
                    it.current_stage = 5
        if project.status == "planning":
            project.status = "active"
    await session.commit()
    base_branch = _task_base_branch(task, project)

    return {
        "status": "ok",
        "git_branch": task.git_branch,
        "git_base_branch": base_branch,
        "instructions": f"""请在本地执行以下操作：

```bash
git checkout {base_branch}
git pull
git checkout {task.git_branch} || git checkout -b {task.git_branch}
```

完成编码后：
```bash
git add .
git commit -m "[{task.ref_id}] 你的修改描述"
git push -u origin {task.git_branch}
```

然后回到系统点击「提交完成」。""",
    }


@router.post("/tasks/{task_id}/toggle-design-review")
async def toggle_design_review(task_id: str, session: AsyncSession = Depends(get_session)):
    """标记/取消标记任务需要详细设计审核"""
    task = await _get_task_for_update(session, task_id)
    if not task:
        raise HTTPException(404)
    ctx = dict(task.context or {})
    task.requires_design_review = not task.requires_design_review
    if task.requires_design_review:
        if not ctx.get("design_phase"):
            ctx["design_phase"] = "needs_discussion"
    else:
        task.design_approved = False
        task.design_approved_by = ""
        task.design_approved_at = None
        ctx.pop("design_phase", None)
    task.context = ctx
    flag_modified(task, "context")
    await session.commit()
    return {
        "requires_design_review": task.requires_design_review,
        "design_phase": ctx.get("design_phase"),
    }


@router.post("/projects/{project_id}/tasks/{task_id}/design-review")
async def design_review_task(
    project_id: str,
    task_id: str,
    body: DesignReviewBody,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_for_update(session, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "任务不存在")
    action = (body.action or "").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")

    ctx = dict(task.context or {})
    reviewer = (body.reviewer or "human").strip() or "human"
    now = datetime.now(timezone.utc)
    if action == "approve":
        ctx["design_phase"] = "approved"
        task.design_approved = True
        task.design_approved_by = reviewer
        task.design_approved_at = now
        task.requires_design_review = True
    else:
        ctx["design_phase"] = "rejected"
        task.design_approved = False
        task.design_approved_by = ""
        task.design_approved_at = None
        task.requires_design_review = True

    task.context = ctx
    flag_modified(task, "context")
    session.add(TaskLog(
        task_id=task.id,
        agent_id=f"human:{reviewer}",
        action=f"design_{action}",
        message=(body.comments or ("设计审核通过" if action == "approve" else "设计审核打回")),
        metadata_={"design_phase": ctx.get("design_phase")},
    ))
    await session.commit()
    return _task_dict(task)


@router.post("/tasks/{task_id}/manual-assign")
async def manual_assign_task(task_id: str, body: ManualAssignBody, session: AsyncSession = Depends(get_session)):
    """人工指派任务给指定 Agent（用于自动调度暂停时的人类放行）。"""
    task = await _get_task_for_update(session, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    if task.status not in ("pending", "failed", "blocked"):
        raise HTTPException(400, f"任务状态为 {task.status}，仅 pending/failed/blocked 可人工指派")

    agent = await session.get(Agent, body.agent_id)
    if not agent or agent.project_id != task.project_id:
        raise HTTPException(404, "目标 Agent 不存在或不属于该项目")

    actor = f"human:{(body.actor or 'human').strip() or 'human'}"

    # failed/blocked 先回到 pending，再进入人工指派流程
    if task.status != "pending":
        session.add(task_lifecycle.transition(
            task,
            event="requeue",
            actor=actor,
            reason="manual assign requeue",
        ))
        session.add(TaskLog(
            task_id=task.id,
            agent_id=actor,
            action="manual_requeue",
            message=f"人工放行任务回到 pending（原状态: {task.status}）" + (f": {body.note}" if body.note else ""),
        ))
        await session.commit()
        task = await _get_task_for_update(session, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")

    ok = await scheduler.assign_task(session, task, agent)
    if not ok:
        raise HTTPException(409, "人工指派失败：任务非 pending 或目标 Agent 不可用，请刷新后重试")

    session.add(TaskLog(
        task_id=task.id,
        agent_id=actor,
        action="manual_assigned",
        message=f"人工指派到 {agent.id}" + (f": {body.note}" if body.note else ""),
        metadata_={"target_agent": agent.id, "target_role": agent.role},
    ))
    await session.commit()
    return {"status": "ok", "task_id": task.id, "assigned_agent": agent.id, "by": actor}


@router.post("/tasks/{task_id}/human-complete")
async def human_complete_task(task_id: str, body: HumanCompleteBody, session: AsyncSession = Depends(get_session)):
    """人类完成任务：记录 commits，标记为 done，等待 review"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    if task.status != "assigned":
        raise HTTPException(400, f"任务状态为 {task.status}，无法提交完成")
    if not task.assigned_agent or not task.assigned_agent.startswith("human:"):
        raise HTTPException(400, "此任务不是人类认领的")

    if body.commits:
        existing = list(task.git_commits or [])
        existing.extend(body.commits)
        task.git_commits = existing

    task.result = body.summary or "人类完成"
    session.add(task_lifecycle.on_reviewing(
        task,
        actor=f"human:{body.username or 'human'}",
        reason=body.summary or "human submit",
    ))

    username = body.username or task.assigned_agent.split(":", 1)[-1]
    session.add(TaskLog(
        task_id=task_id,
        agent_id=f"human:{username}",
        action="submitted",
        message=f"人类 {username} 提交完成，等待审核" + (f": {body.summary}" if body.summary else ""),
    ))
    await session.commit()

    return {"status": "reviewing", "merge_status": task.merge_status}


class CodeReviewBody(BaseModel):
    action: str = "approve"  # approve | reject
    reviewer: str = "architect"
    comments: str = ""
    actor_role: str = "architect"  # architect | boss
    force_override: bool = False


@router.post("/tasks/{task_id}/code-review")
async def code_review_task(task_id: str, body: CodeReviewBody, session: AsyncSession = Depends(get_session)):
    """架构师审核已完成的代码：approve → done，reject → 打回重做，多次不通过 → 架构师下场"""
    actor_role = (body.actor_role or "architect").lower().strip()
    is_boss_override = actor_role == "boss" and body.force_override
    if not is_boss_override and actor_role != "architect":
        raise HTTPException(403, "代码审核默认仅架构师可执行；如需越权，请使用 boss + force_override")
    if is_boss_override and body.action != "approve":
        raise HTTPException(400, "BOSS 越权仅支持 approve")

    reviewer = body.reviewer or actor_role
    comments = body.comments or ""
    if is_boss_override:
        reviewer = f"boss:{reviewer}"
        comments = (f"[BOSS越权{body.action}] " + comments).strip()

    result = await scheduler.review_completed_task(
        session, task_id=task_id,
        action=body.action, reviewer=reviewer, comments=comments,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/tasks/{task_id}/resolve")
async def resolve_blocked_task(task_id: str, body: ResolveBlockedBody, session: AsyncSession = Depends(get_session)):
    """处理 blocked 任务：仅人类可解除阻塞（认领或重分配）"""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404)
    if task.status != "blocked":
        raise HTTPException(400, f"任务状态为 {task.status}，仅 blocked 任务可操作")
    if (body.actor_role or "").lower() != "human":
        raise HTTPException(403, "blocked 任务只能由人类处理，Leader 仅通知")

    username = body.username or "human"

    if body.action == "claim_human":
        task.assigned_agent = f"human:{username}"
        _append_reentry_chain(task, "human_fallback_taken", {
            "actor": f"human:{username}",
            "note": body.note or "",
        })
        session.add(task_lifecycle.on_assigned(
            task,
            agent_id=task.assigned_agent,
            model="human",
            actor=task.assigned_agent,
        ))
        session.add(TaskLog(
            task_id=task_id, agent_id=f"human:{username}", action="claimed",
            message=f"人类 {username} 认领了 blocked 任务" + (f": {body.note}" if body.note else ""),
        ))
        session.add(TaskLog(
            task_id=task_id, agent_id=f"human:{username}", action="reentry_human_fallback",
            message="续跑链路进入人工兜底",
            metadata_={"note": body.note or "", "stage": "human_fallback_taken"},
        ))
        await session.commit()
        return {
            "status": "ok",
            "action": "claim_human",
            "git_branch": task.git_branch,
            "escalation_history": task.escalation_history,
        }

    # reassign_ai: 重置重试计数，降级回指定层级
    reset_level = max(0, min(body.reset_to_level, 2))
    task.escalation_level = reset_level
    task.retry_count = 0
    task.assigned_agent = None
    level_roles = {0: "mid", 1: "senior", 2: "architect"}
    task.suggested_role = level_roles.get(reset_level, "mid")
    session.add(task_lifecycle.transition(
        task,
        event="requeue",
        actor=f"human:{username}",
        reason=f"resolve blocked to level {reset_level}",
    ))
    _append_reentry_chain(task, "local_rebuild_requeued", {
        "actor": f"human:{username}",
        "reset_to_level": reset_level,
        "note": body.note or "",
    })
    if reset_level >= 1:
        task.suggested_model = "opus"

    history = list(task.escalation_history or [])
    history.append({
        "action": "human_resolved",
        "reset_to_level": reset_level,
        "note": body.note,
        "by": username,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    task.escalation_history = history

    level_labels = {0: "工程师", 1: "工程师/顶级模型", 2: "架构师"}
    session.add(TaskLog(
        task_id=task_id, agent_id=f"human:{username}", action="unblocked",
        message=f"人类 {username} 解除阻塞，重置到 {level_labels.get(reset_level, '工程师')} 层级" + (f": {body.note}" if body.note else ""),
    ))
    session.add(TaskLog(
        task_id=task_id, agent_id=f"human:{username}", action="reentry_local_rebuild",
        message=f"续跑失败后触发局部重建，重置到层级 {reset_level}",
        metadata_={"reset_to_level": reset_level, "note": body.note or "", "stage": "local_rebuild_requeued"},
    ))
    await session.commit()
    await scheduler.auto_assign(session, task.project_id, actor_role="human")
    return {
        "status": "ok",
        "action": "reassign_ai",
        "reset_to_level": reset_level,
        "escalation_history": task.escalation_history,
    }


@router.post("/projects/{project_id}/tasks/auto-assign")
async def auto_assign_tasks(
    project_id: str,
    body: AutoAssignBody = AutoAssignBody(),
    session: AsyncSession = Depends(get_session),
):
    assigned = await scheduler.auto_assign(
        session,
        project_id,
        actor_role="architect",
        task_ids=body.task_ids or None,
        limit=body.limit if body.limit > 0 else None,
    )
    return {"assigned": assigned, "count": len(assigned)}


class ClarifyBody(BaseModel):
    answers: list[str]
    resolved_by: str = "human"
    action: str = "release"  # "approve" | "release" | "reply"


@router.post("/tasks/{task_id}/clarify")
async def clarify_task(task_id: str, body: ClarifyBody, session: AsyncSession = Depends(get_session)):
    """人类回复 Agent 的澄清问题。

    action:
      - "approve": 批准并直接分配给原 Agent 继续执行
      - "release": 释放任务回 pending 队列
      - "reply":   仅回复，等待 Agent 继续讨论
    """
    result = await scheduler.resolve_clarification(
        session, task_id, body.answers, resolved_by=body.resolved_by, action=body.action,
    )
    if not result or "error" in result:
        raise HTTPException(404, result.get("error", "任务不存在") if result else "任务不存在")
    return result


@router.get("/projects/{project_id}/tasks/dispatch-readiness")
async def dispatch_readiness(project_id: str, session: AsyncSession = Depends(get_session)):
    return await scheduler.dispatch_readiness(session, project_id)


@router.post("/projects/{project_id}/tasks/reset-stuck")
async def reset_stuck_tasks(project_id: str, session: AsyncSession = Depends(get_session)):
    """强制释放项目中所有卡在 assigned/executing/reviewing 的任务回 pending"""
    from app.models import Agent
    q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(["assigned", "executing", "reviewing"]),
        )
    )
    tasks = list(q.scalars())
    reset_ids = []
    for task in tasks:
        if task.assigned_agent:
            agent = await session.get(Agent, task.assigned_agent)
            if agent and agent.status == "busy":
                continue
        task.assigned_agent = None
        session.add(task_lifecycle.transition(
            task,
            event="reset",
            actor="system",
            reason="force reset stuck task",
        ))
        session.add(TaskLog(
            task_id=task.id, agent_id="system", action="reset",
            message="强制重置：任务从卡住状态释放回 pending",
        ))
        reset_ids.append(task.id)
    await session.commit()
    return {"reset": reset_ids, "count": len(reset_ids)}


@router.post("/projects/{project_id}/merge-and-deploy")
async def merge_and_deploy(project_id: str, session: AsyncSession = Depends(get_session)):
    """手动触发：合并到 production 并创建部署任务。编码全部完成后自动触发，也可手动调用。"""
    from app.services.scheduler import _git_branch_names, _on_all_tasks_done, _check_all_tasks_done
    from app.services import git_repo as git_svc

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    if not project.git_repo:
        raise HTTPException(400, "项目未配置 git_repo")

    integration, production = _git_branch_names(project)
    result = {"project_id": project_id, "integration": integration, "production": production}

    # 1. 确保 workspace + 合并到 production
    try:
        await git_svc.ensure_repo(project.id, project.git_repo)
        merge_r = await git_svc.merge_branch(project.id, integration, target=production)
        result["merge"] = {"ok": merge_r.get("ok", False), "commit": merge_r.get("commit", ""), "error": merge_r.get("error", "")}
    except Exception as e:
        result["merge"] = {"ok": False, "error": str(e)}
        await session.commit()
        return result

    # 2. 创建部署任务
    if result["merge"]["ok"]:
        deploy_task = Task(
            id=new_id(),
            project_id=project_id,
            iteration_id=project.current_iteration_id,
            title=f"部署项目「{project.name}」到生产环境",
            description=f"分支 {production} 已就绪，请执行生产环境部署。",
            type="deploy",
            status="pending",
            complexity="medium",
            suggested_role="devops",
            estimated_hours=2.0,
        )
        session.add(deploy_task)
        await session.flush()
        result["deploy_task_id"] = deploy_task.id

        # 尝试分配
        from app.services import scheduler as sch
        assigned = await sch.auto_assign(session, project_id, actor_role="human", task_ids=[deploy_task.id])
        result["assigned"] = len(assigned or []) > 0

    # 检查是否全部完成
    all_done = await _check_all_tasks_done(session, project_id)
    if all_done and not result["merge"]["ok"]:
        result["hint"] = "合并失败，请检查 git 仓库配置和分支状态"

    await session.commit()
    return result


# ── 燃尽图数据 ──

@router.get("/projects/{project_id}/tasks/burndown")
async def task_burndown(project_id: str, session: AsyncSession = Depends(get_session)):
    """燃尽图：按总时长自适应横轴 — 不满1天按小时，不满7天按4小时，否则按天，最长15天"""
    q = await session.execute(
        select(Task).where(Task.project_id == project_id, Task.status != "draft")
    )
    tasks = list(q.scalars())
    if not tasks:
        return {"points": [], "summary": {}}

    total_hours = sum(t.estimated_hours or 0.5 for t in tasks)
    now = datetime.now(timezone.utc)

    done_logs_q = await session.execute(
        select(TaskLog.task_id, func.min(TaskLog.created_at).label("done_at"))
        .where(
            TaskLog.task_id.in_([t.id for t in tasks]),
            TaskLog.action.in_(["review_approved", "completed", "done", "module_completed"]),
        )
        .group_by(TaskLog.task_id)
    )
    done_map: dict[str, datetime] = {}
    for row in done_logs_q:
        done_map[row.task_id] = row.done_at
    for t in tasks:
        if t.status == "done" and t.id not in done_map:
            done_map[t.id] = t.updated_at

    task_hours = {t.id: (t.estimated_hours or 0.5) for t in tasks}

    # 从第一个任务开始时间算起
    start_dt = min(t.created_at for t in tasks)
    total_seconds = max((now - start_dt).total_seconds(), 1)

    # 自适应分桶：目标 ~30 个点
    if total_seconds <= 86400:          # ≤1天 → 每小时
        bucket_secs, label_fmt = 3600, "%m-%d %Hh"
    elif total_seconds <= 604800:       # ≤7天 → 每4小时
        bucket_secs, label_fmt = 14400, "%m-%d %Hh"
    elif total_seconds <= 1296000:      # ≤15天 → 每12小时
        bucket_secs, label_fmt = 43200, "%m-%d"
    else:                               # >15天 → 每天，取最近15天
        bucket_secs, label_fmt = 86400, "%m-%d"
        start_dt = max(start_dt, now - timedelta(days=15))

    num_buckets = max(int((now - start_dt).total_seconds() / bucket_secs), 1)
    points = []
    cumulative_done = 0.0

    for i in range(num_buckets + 1):
        bucket_end = start_dt + timedelta(seconds=bucket_secs * (i + 1))
        # 累计该桶内完成的任务
        for tid, done_at in list(done_map.items()):
            if done_at <= bucket_end and tid in task_hours:
                cumulative_done += task_hours.pop(tid)
        remaining = total_hours - cumulative_done
        ideal = max(0, total_hours * (1 - (i + 1) / max(num_buckets, 1)))
        points.append({
            "hour": bucket_end.isoformat(),
            "remaining": round(remaining, 1),
            "done": round(cumulative_done, 1),
            "ideal": round(ideal, 1),
        })

    done_count = sum(1 for t in tasks if t.status == "done")
    active_count = sum(1 for t in tasks if t.status in ("assigned", "executing", "pending"))
    blocked_count = sum(1 for t in tasks if t.status in ("blocked", "failed"))

    total_hours_span = max(total_seconds / 3600, 0.1)
    velocity = cumulative_done / max(total_hours_span, 0.1)
    remaining_hours = total_hours - cumulative_done
    eta_hours = int(remaining_hours / velocity) if velocity > 0 else None

    return {
        "points": points,
        "summary": {
            "total_tasks": len(tasks),
            "done_tasks": done_count,
            "active_tasks": active_count,
            "blocked_tasks": blocked_count,
            "total_hours": round(total_hours, 1),
            "done_hours": round(cumulative_done, 1),
            "remaining_hours": round(remaining_hours, 1),
            "velocity_per_hour": round(velocity, 2),
            "eta_hours": eta_hours,
            "eta_time": (now + timedelta(hours=eta_hours)).isoformat() if eta_hours else None,
            "start_time": start_dt.isoformat(),
            "progress_pct": round(cumulative_done / total_hours * 100, 1) if total_hours else 0,
            "duration_hours": round(total_hours_span, 1),
        },
    }


# ── E2E 测试触发 ──

class StartE2ETestBody(BaseModel):
    target_url: str = ""       # 可选：测试目标 URL
    test_scope: str = "full"   # full | smoke | regression
    test_framework: str = ""   # playwright | cypress | pytest，留空自动选择


@router.post("/projects/{project_id}/start-e2e-test")
async def start_e2e_test(project_id: str, body: StartE2ETestBody, session: AsyncSession = Depends(get_session)):
    """人工触发：创建 E2E 测试任务，分配给 tester Agent 执行。"""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    # 创建测试任务
    e2e_task = Task(
        id=new_id(),
        project_id=project_id,
        iteration_id=project.current_iteration_id,
        title=f"E2E 测试 - {project.name}",
        description=f"对项目「{project.name}」进行端到端测试。\n目标 URL: {body.target_url or '待定'}\n测试范围: {body.test_scope}",
        type="testing",
        status="pending",
        complexity="high",
        suggested_role="tester",
        estimated_hours=4.0,
        context={
            "test_scope": body.test_scope,
            "target_url": body.target_url,
            "test_framework": body.test_framework or "auto",
            "e2e_test": True,
        },
    )
    session.add(e2e_task)
    await session.flush()

    # 尝试分配
    from app.services import scheduler as sch
    assigned = await sch.auto_assign(session, project_id, actor_role="human", task_ids=[e2e_task.id])

    await session.commit()

    return {
        "task_id": e2e_task.id,
        "ref_id": e2e_task.ref_id,
        "title": e2e_task.title,
        "status": e2e_task.status,
        "assigned": len(assigned or []) > 0,
        "message": "E2E 测试任务已创建，tester Agent 将自动执行测试并生成报告",
    }


@router.get("/projects/{project_id}/tasks/stats")
async def task_stats(project_id: str, session: AsyncSession = Depends(get_session)):
    """任务统计概览"""
    q = await session.execute(
        select(Task.status, func.count(), func.sum(Task.estimated_hours))
        .where(Task.project_id == project_id)
        .group_by(Task.status)
    )
    by_status = {}
    total = 0
    total_hours = 0.0
    for status, count, hours in q:
        by_status[status] = {"count": count, "hours": round(hours or 0, 1)}
        total += count
        total_hours += hours or 0

    return {
        "total": total,
        "total_hours": round(total_hours, 1),
        "by_status": by_status,
    }


def _clean_task_context(ctx: dict | None) -> dict:
    ctx = dict(ctx or {})
    if "role_model_map" in ctx:
        ctx["role_model_map"] = _clean_role_map(ctx["role_model_map"])
    return ctx


def _task_dict(t: Task) -> dict:
    ctx = t.context or {}
    return {
        "id": t.id, "project_id": t.project_id, "parent_task_id": t.parent_task_id,
        "iteration_id": t.iteration_id, "ref_id": t.ref_id,
        "title": t.title, "description": t.description, "type": t.type,
        "assigned_agent": t.assigned_agent, "status": t.status, "priority": t.priority,
        "suggested_role": t.suggested_role, "suggested_model": t.suggested_model,
        "complexity": t.complexity,
        "estimated_hours": t.estimated_hours, "dependencies": t.dependencies,
        "input_files": t.input_files, "output_files": t.output_files,
        "acceptance_criteria": t.acceptance_criteria, "result": t.result,
        "ref_docs": t.ref_docs, "git_branch": t.git_branch,
        "git_commits": t.git_commits, "merge_status": t.merge_status,
        "merge_commit": t.merge_commit, "test_status": t.test_status,
        "test_results": t.test_results,
        "retry_count": t.retry_count, "max_retries": t.max_retries,
        "escalation_level": t.escalation_level,
        "escalation_history": t.escalation_history,
        "executor_hint": resolved_executor_hint(ctx),
        "actor_type": ctx.get("actor_type"),
        "context": _clean_task_context(t.context),
        "requires_design_review": getattr(t, "requires_design_review", False),
        "design_conversation_id": getattr(t, "design_conversation_id", None),
        "design_approved": getattr(t, "design_approved", False),
        "design_approved_by": getattr(t, "design_approved_by", ""),
        "design_approved_at": t.design_approved_at.isoformat() if getattr(t, "design_approved_at", None) else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }
