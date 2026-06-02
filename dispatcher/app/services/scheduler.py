"""
任务调度引擎

职责：依赖分析、自动分配、状态流转。
Agent 按项目隔离，模型通过 model_pool 解析。
所有关键节点写入结构化埋点数据（TaskLog.metadata），为后续智能模型选择积累数据。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, Agent, AgentTeam, TaskLog, Project, Iteration, GenerationTask, new_id
from app.execution_hints import merge_assign_hints_into_context
from app.services import openclaw, ai_leader, model_pool, experience, task_docs, role_context, mq, git_repo, token_tracker, global_knowledge, task_lifecycle, agent_lifecycle, deploy_manager, agent_provider_pool

logger = logging.getLogger(__name__)

DISPATCH_MODE = "architect_only"
DISPATCH_ALLOWED_ROLES = {"architect", "human", "dispatcher"}
ENV_SELF_HEAL_MAX_ATTEMPTS = 3


def _can_dispatch(actor_role: str) -> bool:
    role = (actor_role or "").lower()
    if DISPATCH_MODE != "architect_only":
        return True
    return role in DISPATCH_ALLOWED_ROLES


def _build_allowed_paths(task: Task) -> list[str]:
    """
    根据任务属性构建文件白名单（allowed_paths）。

    白名单逻辑：
    1. 明确指定的 input_files 和 output_files
    2. 根据任务类型推断的标准路径（如测试任务包含 tests/）
    3. 任务 scope 中提到的路径
    4. 排除 forbidden_paths

    不在白名单中的文件，CC Worker 执行时会被设为只读。
    """
    allowed: set[str] = set()

    # 1. 明确指定的文件
    for f in (task.input_files or []):
        if f:
            allowed.add(f)
    for f in (task.output_files or []):
        if f:
            allowed.add(f)

    # 2. 根据任务类型推断标准路径
    task_type = (task.type or "").lower()
    if task_type in ("test", "testing", "qa"):
        allowed.add("tests")
        allowed.add("test")
        allowed.add("*_test.py")
        allowed.add("*.spec.js")
        allowed.add("*.test.ts")
    elif task_type in ("feature", "feat", "bug", "fix", "hotfix"):
        # 功能开发：允许修改 src/ app/ lib/ 等常见源码目录
        allowed.add("src")
        allowed.add("app")
        allowed.add("lib")
        allowed.add("components")
        allowed.add("pages")
        allowed.add("api")
        allowed.add("services")
        allowed.add("models")
    elif task_type in ("doc", "docs", "document"):
        allowed.add("docs")
        allowed.add("README*")
        allowed.add("*.md")
    elif task_type in ("deploy", "devops", "infra"):
        allowed.add("deploy")
        allowed.add("docker")
        allowed.add("k8s")
        allowed.add("kubernetes")
        allowed.add("terraform")
        allowed.add("ansible")
        allowed.add("scripts")
        allowed.add("*.yml")
        allowed.add("*.yaml")

    # 3. 从 task.scope 中提取路径线索
    scope = (task.context or {}).get("scope", "") if task.context else ""
    if scope:
        # 简单启发式：scope 中提到的文件路径
        import re
        path_patterns = re.findall(r"[\w\-/.]+\.(py|js|ts|tsx|jsx|vue|go|rs|java|kt|swift|cpp|c|h|yaml|yml|json|md)", scope)
        for p in path_patterns:
            allowed.add(p)

    # 4. 根据任务标题推断（简单启发式）
    title = task.title or ""
    if title:
        import re
        path_patterns = re.findall(r"[\w\-/.]+\.(py|js|ts|tsx|jsx|vue|go|rs|java|kt|swift|cpp|c|h)", title)
        for p in path_patterns:
            allowed.add(p)

    # 5. 排除 forbidden_paths
    forbidden = set(task.context.get("forbidden_paths", [])) if task.context else set()
    allowed = allowed - forbidden

    return sorted(allowed)


def _git_branch_names(project: Project | None) -> tuple[str, str]:
    cfg = (project.config or {}) if project else {}
    git_cfg = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    integration = (git_cfg.get("integration_branch") or "develop").strip()
    production = (git_cfg.get("production_branch") or "main").strip()
    return integration or "develop", production or "main"


def _task_git_strategy(task: Task, project: Project | None) -> tuple[str, str]:
    """
    返回 (base_branch, merge_target)：
    - 常规开发：develop → develop
    - bug/hotfix：production(main) → production(main)
    """
    integration, production = _git_branch_names(project)
    t = (task.type or "").lower()
    if t in ("bug", "hotfix", "fix"):
        return production, production
    return integration, integration


def _env_gate_block(supervisor_id: str) -> str:
    target = (supervisor_id or "").strip() or "architect"
    return (
        "若环境问题在有限尝试后仍无法解决，必须立即阻塞当前任务并上报，"
        f"上报对象为 `{target}`。禁止跳过环境问题继续编码。"
    )


def _build_env_gate_instruction(agent: Agent) -> str:
    return (
        "## 执行前置门禁（环境）\n"
        "在编码前先执行环境自检：依赖安装、构建工具、运行时版本、仓库状态、关键命令可用性。\n"
        f"若发现环境问题，先进行智能自修复，最多尝试 {ENV_SELF_HEAL_MAX_ATTEMPTS} 次。\n"
        f"{_env_gate_block(agent.supervisor_id or '')}\n"
        "只有在环境门禁通过后，才允许进入编码/测试/提交。"
    )


def _build_env_collaboration_instruction(agent: Agent) -> str:
    if agent.role == "architect":
        return (
            "## 团队环境指导职责（架构师）\n"
            "所有成员都在独立环境执行任务，预注入环境仅是基础层。\n"
            "你需要给出团队可执行的环境基线（语言版本、包管理器、核心依赖、启动命令）。\n"
            "当成员反馈缺库或环境差异时，给出最小补装方案（安装命令、版本约束、验证命令），并要求可复现。\n"
            "每次补装方案必须同步沉淀到过程文档，避免后续成员重复踩坑。"
        )
    return (
        "## 环境协作要求（成员）\n"
        "你在独立环境中执行任务，预注入环境仅提供基础能力。\n"
        "编码前先遵循架构师给出的环境基线；若仍缺库，先本地补装并验证，再继续编码。\n"
        f"补装失败时按环境门禁流程最多尝试 {ENV_SELF_HEAL_MAX_ATTEMPTS} 次，并将缺失依赖、安装命令、报错信息上报架构师。\n"
        "补装成功后，在结果中补充新增依赖与版本，供架构师更新团队环境指导。"
    )


def _is_reentry_task(task: Task) -> bool:
    ctx = task.context or {}
    return bool(
        (task.retry_count or 0) > 0
        or (task.escalation_level or 0) > 0
        or ctx.get("reentry_requested") is True
    )


def _build_reentry_instruction(task: Task, agent: Agent) -> str:
    reviewer = (agent.supervisor_id or "").strip() or "architect"
    ref = task.ref_id or task.id[:8]
    return (
        "## 任务重入与幂等执行要求\n"
        f"当前任务：`{ref}`。\n"
        "优先尝试在原分支与已有产物上续做：先阅读已有提交、结果与上下文，再决定下一步。\n"
        "若你确认能理解并有把握，请继续完成；提交时明确标注“续跑完成”。\n"
        "若续跑尝试失败，允许放弃部分无效中间产物并局部重做，但必须在结果里说明保留/丢弃范围。\n"
        f"若仍无法继续，必须将任务置为 blocked 并上报 `{reviewer}`，最终可由人类兜底。"
    )


def _build_execution_loop_protocol(agent: Agent) -> str:
    supervisor = (agent.supervisor_id or "").strip() or "architect"
    return (
        "## 任务执行循环协作协议\n"
        "你现在处于正式任务执行循环：前置初始化已完成，后续围绕任务流推进。\n"
        "全局知识仅作为入口通知循环，与具体任务执行循环相互独立；不要将其当作任务状态机的一部分。\n"
        "协作链路：架构师 ↔ 成员 ↔ Dispatcher。你需要把关键决策、阻塞、变更通过消息链路同步给架构师/上级。\n"
        f"上报默认对象：`{supervisor}`。人类可随时直接干预任何任务，人工指令优先级高于常规自动调度。\n"
        "代码协作只通过 Git 分支/提交/合并；任务状态与指令交互只通过消息队列（MQ）回传，不要混用。"
    )


def _build_human_guided_collaboration_instruction(task: Task) -> str:
    approved = bool((task.context or {}).get("human_execute_approved"))
    if approved:
        return (
            "## 人类主导协作模式（执行阶段）\n"
            "本任务已获得人类最终批准。请按批准方案执行实现、测试与提交，并在结果中回顾方案与落地差异。"
        )
    return (
        "## 人类主导协作模式（方案阶段）\n"
        "本轮目标是产出详细设计方案与执行建议，不进入最终代码提交闭环。\n"
        "请输出：方案选项、风险、推荐方案、实施步骤、验证计划。\n"
        "在结果中明确写出“等待人类决策（批准执行/要求重做方案）”。"
    )


async def _after_bootstrap_done_notify_global_knowledge(
    session: AsyncSession,
    task: Task,
    reviewer: str,
) -> dict | None:
    ctx = task.context or {}
    if not bool(ctx.get("architect_bootstrap")):
        return None
    try:
        result = await global_knowledge_notice.notify_project_agents(
            session,
            project_id=task.project_id,
            sender_id=reviewer or "architect",
            summary="架构师首任务已完成，进入任务执行循环。请全员完成补训后开始按任务推进。",
        )
        session.add(TaskLog(
            task_id=task.id,
            agent_id="system",
            action="bootstrap_global_knowledge_notified",
            message=(
                "架构师首任务完成后已自动发送全局知识补训通知："
                f"version={result.get('version', '')} pending={result.get('pending_ack_agents', 0)}"
            ),
            metadata_={
                "version": result.get("version", ""),
                "revision": result.get("revision", 0),
                "pending_ack_agents": result.get("pending_ack_agents", 0),
                "version_changed": bool(result.get("version_changed", False)),
            },
        ))
        await session.commit()
        return result
    except Exception as e:
        logger.warning(f"Bootstrap global knowledge notify failed: task={task.id} err={e}")
        session.add(TaskLog(
            task_id=task.id,
            agent_id="system",
            action="bootstrap_global_knowledge_notify_failed",
            message=f"架构师首任务完成后自动补训通知失败: {e}",
        ))
        await session.commit()
        return None


def _attempt_matches(task: Task, attempt_id: str | None) -> bool:
    current = str((task.context or {}).get("current_attempt_id") or "").strip()
    incoming = str(attempt_id or "").strip()
    if not current:
        return True
    if not incoming:
        return True  # 兼容尚未升级的旧 Connector
    return incoming == current


async def _iter_seq(session: AsyncSession, iteration_id: str | None) -> int | str:
    if not iteration_id:
        return "default"
    it = await session.get(Iteration, iteration_id)
    return it.seq if it else "default"


async def _archive_safe(session: AsyncSession, **kwargs):
    """非阻塞归档，失败不影响主流程"""
    try:
        doc = await task_docs.archive(session, **kwargs)

        project_id = kwargs.get("project_id")
        if not project_id:
            return

        project = await session.get(Project, project_id)
        if not project or not project.git_repo:
            return

        repo_prefix = f"{project_id}/"
        git_path = doc.file_path[len(repo_prefix):] if doc.file_path.startswith(repo_prefix) else doc.file_path
        if not git_path:
            return

        content = task_docs.read_doc_content(doc.file_path)
        if not content:
            return

        await git_repo.ensure_repo(project_id, project.git_repo)
        iter_seq = kwargs.get("iteration_seq", "default")
        commit_msg = git_repo.build_commit_message(
            "docs",
            f"{doc.title}",
            scope="task-docs",
            task_ref=kwargs.get("ref_id", ""),
            iteration=str(iter_seq),
            author="ai/agent",
        )
        result = await git_repo.commit_and_push(
            project_id,
            git_path,
            content=content,
            message=commit_msg,
        )
        if result.get("ok"):
            meta = doc.metadata_ or {}
            meta.update({"git_synced": True, "git_path": git_path})
            doc.metadata_ = meta
            await session.commit()
    except Exception as e:
        logger.warning(f"Doc archive failed (non-blocking): {e}")


TERMINAL_STATUSES = {"done", "cancelled", "superseded"}


async def is_module_completed(session: AsyncSession, module_task_id: str) -> bool:
    children_q = await session.execute(
        select(Task.status).where(Task.parent_task_id == module_task_id)
    )
    statuses = [row[0] for row in children_q.all()]
    return bool(statuses) and all(s in TERMINAL_STATUSES for s in statuses)


async def check_and_complete_module(session: AsyncSession, task: Task):
    """子任务达到终态时，检查并自动标记模块完成，触发下游调度"""
    if not task.parent_task_id:
        return
    parent = await session.get(Task, task.parent_task_id)
    if not parent or not (parent.context or {}).get("is_module"):
        return
    if parent.status in TERMINAL_STATUSES:
        return
    if await is_module_completed(session, parent.id):
        session.add(task_lifecycle.on_terminal(
            parent,
            status="done",
            actor="system",
            reason="all child subtasks reached terminal state",
        ))
        session.add(TaskLog(
            task_id=parent.id, agent_id="system",
            action="module_completed",
            message=f"模块 {parent.ref_id} 所有子任务已完成",
        ))
        logger.info(f"Module {parent.ref_id} auto-completed")


async def resolve_clarification(
    session: AsyncSession,
    task_id: str,
    answers: list[str],
    *,
    resolved_by: str = "human",
    action: str = "release",
) -> dict:
    """人类回复 Agent 的澄清请求。

    Args:
        task_id: 任务 ID
        answers: 澄清问题的回答列表
        resolved_by: 回复者标识
        action: "approve" — 批准并直接分配给提出问题的 Agent 继续执行
                "release" — 释放任务回 pending 队列
                "reply"  — 仅回复问题，等待 Agent 继续讨论（任务保持 need_clarification）

    Returns:
        {"task_id": ..., "status": ..., "action": ...}
    """
    task = await session.get(Task, task_id)
    if not task:
        logger.warning(f"resolve_clarification: task {task_id} not found")
        return {"error": "not found"}

    if task.status != "need_clarification":
        logger.warning(
            f"resolve_clarification: task {task_id} status is {task.status}, "
            f"expected need_clarification"
        )
        return {"error": f"status is {task.status}, expected need_clarification"}

    # 将回答写入 task.context
    ctx = dict(task.context or {})
    ctx["clarification_answers"] = answers
    ctx["clarification_resolved_by"] = resolved_by
    ctx["clarification_resolved_at"] = datetime.now(timezone.utc).isoformat()

    if action == "reply":
        # 仅追加回复，任务保持 need_clarification 等待 Agent 继续讨论
        replies = list(ctx.get("clarification_replies", []))
        replies.append({"answers": answers, "by": resolved_by, "at": datetime.now(timezone.utc).isoformat()})
        ctx["clarification_replies"] = replies
        task.context = ctx
        session.add(TaskLog(
            task_id=task.id,
            agent_id=resolved_by,
            action="clarification_reply",
            message=f"人类回复（继续讨论）：{'; '.join(answers[:3])}",
        ))
        await session.commit()
        return {"task_id": task.id, "status": "need_clarification", "action": "reply"}

    # approve 或 release：清除待处理澄清
    ctx.pop("pending_clarifications", None)
    task.context = ctx

    req_agent = task.assigned_agent  # 提出问题的人

    if action == "approve":
        # 人类批准：直接放行，任务分配给原 Agent 继续执行
        session.add(task_lifecycle.transition(
            task,
            event="clarification_approved",
            actor=resolved_by,
            reason=f"human approved: {'; '.join(answers[:3])}",
            metadata={"answers": answers, "resolved_by": resolved_by, "action": "approve"},
        ))
        # 尝试重新分配给原 Agent
        if req_agent:
            original_agent = await session.get(Agent, req_agent)
            if original_agent and original_agent.status in ("idle", "awaiting_review"):
                ok = await assign_task(session, task, original_agent)
                if ok:
                    logger.info("Clarify approved: re-assigned task %s to original agent %s", task.ref_id or task.id, req_agent)
                else:
                    logger.info("Clarify approved: task %s released to queue (agent %s unavailable)", task.ref_id or task.id, req_agent)
            else:
                logger.info("Clarify approved: task %s released to queue (original agent not idle)", task.ref_id or task.id)
    else:
        # release：释放任务回 pending 队列
        session.add(task_lifecycle.transition(
            task,
            event="clarification_resolved",
            actor=resolved_by,
            reason="clarification resolved, task released",
            metadata={"answers": answers, "resolved_by": resolved_by, "action": "release"},
        ))

    session.add(TaskLog(
        task_id=task.id,
        agent_id=resolved_by,
        action="clarification_resolved",
        message=f"澄清已回复（{len(answers)} 条），任务恢复待调度",
        metadata_={"answers": answers, "resolved_by": resolved_by},
    ))

    await session.commit()
    logger.info(f"Task {task.ref_id} clarification resolved by {resolved_by}, back to pending")
    return task


async def get_ready_tasks(session: AsyncSession, project_id: str) -> list[Task]:
    project = await session.get(Project, project_id)
    project_cfg = dict(project.config or {}) if project else {}
    prototype_fast_track = bool(project_cfg.get("prototype_fast_track"))
    # 强制前置门禁：架构师初始化任务未完成前，只允许流转该初始化任务。
    bootstrap_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["architect_bootstrap"].astext == "true",
        ).order_by(Task.created_at.asc()).limit(1)
    )
    bootstrap = bootstrap_q.scalar_one_or_none()
    bootstrap_unfinished = bool(
        bootstrap and (bootstrap.status not in TERMINAL_STATUSES and bootstrap.status != "done")
    )
    # 若 bootstrap 长期卡在 pending/assigned（无 architect 可取），放宽门禁避免死锁
    BOOTSTRAP_STUCK_TIMEOUT = timedelta(minutes=30)
    bootstrap_stuck = False
    if bootstrap_unfinished and bootstrap and bootstrap.status in ("pending", "assigned"):
        now = datetime.now(timezone.utc)
        if bootstrap.updated_at and (now - bootstrap.updated_at) > BOOTSTRAP_STUCK_TIMEOUT:
            bootstrap_stuck = True
            logger.warning(
                "Bootstrap task %s stuck in %s for >30min, relaxing gate to allow other tasks",
                bootstrap.ref_id or bootstrap.id,
                bootstrap.status,
            )

    done_q = await session.execute(
        select(Task.id).where(Task.project_id == project_id, Task.status == "done")
    )
    done_ids = {row[0] for row in done_q.all()}

    # 收集所有已完成的模块 ID（所有子任务都在终态）
    modules_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.parent_task_id == None,  # noqa: E711
            Task.context["is_module"].astext == "true",
        )
    )
    modules = {m.id: m for m in modules_q.scalars()}
    completed_module_ids: set[str] = set()
    for mid in modules:
        if mid in done_ids or await is_module_completed(session, mid):
            completed_module_ids.add(mid)

    # 模块依赖未满足 → 整个模块的子任务都不可调度
    blocked_module_ids: set[str] = set()
    for mid, mod in modules.items():
        for dep_id in (mod.dependencies or []):
            if dep_id not in completed_module_ids:
                blocked_module_ids.add(mid)
                break

    pending_q = await session.execute(
        select(Task).where(Task.project_id == project_id, Task.status == "pending").order_by(Task.priority.desc())
    )
    pending = pending_q.scalars().all()

    ready = []
    for t in pending:
        if bootstrap_unfinished and not bootstrap_stuck and not (t.context or {}).get("architect_bootstrap"):
            continue
        # 模块任务不参与编码分配
        if (t.context or {}).get("is_module"):
            continue
        # 子任务依赖未满足
        if not all(d in done_ids for d in (t.dependencies or [])):
            continue
        # 所属模块被阻塞
        if t.parent_task_id and t.parent_task_id in blocked_module_ids:
            continue
        # 设计门禁：需要人类讨论的任务始终阻塞，不论 fast_track 模式
        design_phase = str((t.context or {}).get("design_phase") or "").strip().lower()
        if design_phase and design_phase not in ("none", "approved"):
            continue
        if not prototype_fast_track:
            if getattr(t, "requires_design_review", False) and not getattr(t, "design_approved", False):
                continue
        ready.append(t)
    return ready


async def get_idle_agents(session: AsyncSession, project_id: str, team_id: str | None = None) -> list[Agent]:
    q = select(Agent).where(Agent.project_id == project_id, Agent.status == "idle")
    if team_id:
        q = q.where(Agent.team_id == team_id)
    result = await session.execute(q)
    agents = list(result.scalars().all())
    retriever_ready_agents = [a for a in agents if (a.config or {}).get("retriever_ready", True) is not False]
    if len(retriever_ready_agents) != len(agents):
        logger.info(
            "Retriever gate filtered idle agents: project=%s, ready=%s/%s",
            project_id,
            len(retriever_ready_agents),
            len(agents),
        )
    agents = retriever_ready_agents
    project = await session.get(Project, project_id)
    project_cfg = dict(project.config or {}) if project else {}
    required_revision = global_knowledge.to_revision(project_cfg.get("global_knowledge_revision"))
    required_version = project_cfg.get("global_knowledge_version") or ""
    fallback_version = global_knowledge.current_version(project_id)
    if required_revision <= 0 and not required_version and not fallback_version:
        return agents

    trained: list[Agent] = []
    for a in agents:
        cfg = a.config or {}
        ack_revision = global_knowledge.to_revision(cfg.get("global_knowledge_ack_revision"))
        ack_version = cfg.get("global_knowledge_ack_version") or ""
        if required_revision > 0:
            if ack_revision >= required_revision:
                trained.append(a)
            continue
        if required_version and ack_version == required_version:
            trained.append(a)
            continue
        if fallback_version and ack_version == fallback_version:
            trained.append(a)
    if len(trained) != len(agents):
        logger.info(
            f"Global knowledge gate filtered idle agents: project={project_id}, "
            f"trained={len(trained)}/{len(agents)}, required_version={required_version}, required_revision={required_revision}"
        )
    return trained


async def _build_escalation_context(session: AsyncSession, task: Task) -> str:
    """构建升级上下文：之前的错误历史 + 人类备注"""
    parts = []
    level = task.escalation_level or 0
    if level > 0:
        parts.append(f"⚠️ 此任务已升级到 {ESCALATION_LABELS.get(level, f'Level {level}')} 层级处理。")

    retry_logs = await session.execute(
        select(TaskLog).where(
            TaskLog.task_id == task.id,
            TaskLog.action.in_(["retry", "failed", "escalated", "unblocked"]),
        ).order_by(TaskLog.created_at.desc()).limit(5)
    )
    logs = list(retry_logs.scalars())
    if logs:
        parts.append("\n之前的错误/升级记录（最近5条）：")
        for log in reversed(logs):
            parts.append(f"- [{log.action}] {log.message}")

    for h in (task.escalation_history or []):
        if h.get("action") == "human_resolved" and h.get("note"):
            parts.append(f"\n人类提供的修复思路：{h['note']}")

    return "\n".join(parts) if parts else ""


async def _build_architect_module_context(session: AsyncSession, task: Task, agent: Agent) -> str:
    """为架构师构建其小组负责的模块上下文 + 跨组依赖模块的接口契约"""
    if not agent.team_id:
        return ""

    team = await session.get(AgentTeam, agent.team_id)
    if not team or not team.module_task_ids:
        return ""

    modules = [m for mid in team.module_task_ids if (m := await session.get(Task, mid))]
    if not modules:
        return ""

    parts: list[str] = [f"# 你所在的小组：{team.name}"]
    parts.append(f"\n## 你负责的模块（共 {len(modules)} 个）\n")

    for m in modules:
        is_current = (task.parent_task_id == m.id) or (task.id == m.id)
        parts.extend(await _render_own_module(session, task, m, is_current))

    cross_parts = await _render_cross_team_deps(
        session, modules, set(team.module_task_ids), task.project_id,
    )
    parts.extend(cross_parts)

    return "\n".join(parts)


async def _render_own_module(
    session: AsyncSession, task: Task, m: Task, is_current: bool,
) -> list[str]:
    """渲染架构师自有模块的描述、接口和架构决策"""
    parts: list[str] = []
    marker = " ← 【当前任务所属模块】" if is_current else ""
    desc = m.description[:300] if len(m.description) > 300 else m.description
    interfaces = (m.context or {}).get("interfaces", [])
    parts.append(f"### {m.ref_id} {m.title}{marker}\n{desc}")
    if interfaces:
        parts.append("**接口契约**: " + " | ".join(str(i) for i in interfaces))
    parts.append("")

    if is_current:
        from app.models import TaskDocument
        arch_q = await session.execute(
            select(TaskDocument).where(
                TaskDocument.project_id == task.project_id,
                TaskDocument.doc_type == "architecture_decision",
                TaskDocument.metadata_["module_ref_id"].astext == m.ref_id,
            ).order_by(TaskDocument.created_at.desc()).limit(5)
        )
        for doc in arch_q.scalars():
            parts.append(f"**架构决策 — {doc.title}**: {doc.summary or ''}")
    return parts


async def _render_cross_team_deps(
    session: AsyncSession, modules: list[Task], my_ids: set[str], project_id: str,
) -> list[str]:
    """渲染跨组依赖模块的接口契约"""
    dep_ids: set[str] = set()
    for m in modules:
        for d in (m.dependencies or []):
            if d not in my_ids:
                dep_ids.add(d)

    if not dep_ids:
        return []

    parts = ["\n## 跨组依赖模块（其他小组负责，仅供接口参考）\n"]
    for dep_id in dep_ids:
        dep_mod = await session.get(Task, dep_id)
        if not dep_mod:
            continue
        dep_team = await _find_team_for_module(session, dep_id, project_id)
        team_label = f"（{dep_team.name}）" if dep_team else ""
        interfaces = (dep_mod.context or {}).get("interfaces", [])
        parts.append(f"### {dep_mod.ref_id} {dep_mod.title}{team_label}")
        if interfaces:
            parts.append("**接口契约**: " + " | ".join(str(i) for i in interfaces))
        else:
            desc = dep_mod.description[:200] if len(dep_mod.description) > 200 else dep_mod.description
            parts.append(desc)
        parts.append("")
    return parts


async def _find_team_for_module(session: AsyncSession, module_id: str, project_id: str) -> AgentTeam | None:
    """查找负责指定模块的小组"""
    teams_q = await session.execute(
        select(AgentTeam).where(AgentTeam.project_id == project_id)
    )
    for team in teams_q.scalars():
        if module_id in (team.module_task_ids or []):
            return team
    return None


async def _build_related_docs_context(session: AsyncSession, task: Task) -> str:
    """加载任务关联的过程文档，传递分解阶段的决策上下文给执行阶段"""
    from app.models import TaskDocument

    parts: list[str] = []

    # 如果任务有父模块，加载父模块的描述作为全局上下文
    if task.parent_task_id:
        parent = await session.get(Task, task.parent_task_id)
        if parent and parent.context and parent.context.get("is_module"):
            parts.append(f"## 所属模块\n**{parent.title}**\n{parent.description[:500]}")

    # 加载该项目的技术方案文档（分解时的输入）
    tech_doc = await session.execute(
        select(TaskDocument).where(
            TaskDocument.project_id == task.project_id,
            TaskDocument.doc_type == "stage_document",
        ).order_by(TaskDocument.created_at.desc()).limit(3)
    )
    for doc in tech_doc.scalars():
        summary = doc.summary or doc.title
        if summary:
            parts.append(f"## {doc.title}\n{summary[:300]}")

    if not parts:
        return ""
    return "# 项目过程文档（分解阶段产出）\n\n" + "\n\n".join(parts)


def _get_module_role_model_map(parent: Task | None) -> dict | None:
    """若任务属于模块，返回模块的 role_model_map"""
    if not parent or not parent.context:
        return None
    if not parent.context.get("is_module"):
        return None
    return parent.context.get("role_model_map") or None


async def assign_task(session: AsyncSession, task: Task, agent: Agent) -> bool:
    # 刷新状态，防止并发重复分配
    await session.refresh(task)
    await session.refresh(agent)
    if task.status != "pending":
        logger.info(f"Task {task.ref_id} already {task.status}, skip assign")
        return False
    if agent.status != "idle":
        logger.info(f"Agent {agent.id} already {agent.status}, skip assign")
        return False
    if (task.context or {}).get("architect_bootstrap") is True and agent.role != "architect":
        logger.info(f"Bootstrap task must be assigned to architect: task={task.id}, agent={agent.id}")
        return False
    allowed, block_reason = agent_lifecycle.can_start_task(agent, task)
    if not allowed:
        logger.info(f"Agent lifecycle blocked assign: agent={agent.id} task={task.id} reason={block_reason}")
        return False
    if not await _architect_bootstrap_priority_guard(session, agent, task):
        return False

    # ── CC Worker 自动容器编排 ──
    # 若 Agent 是 CC Worker 角色且容器未运行，自动 spawn
    if agent.role in ("senior", "mid", "junior", "architect", "devops", "tester", "archaeologist"):
        from app.services import infra as infra_svc

        # 查询项目关联的环境组，获取可用节点
        node = None
        project_for_spawn = await session.get(Project, agent.project_id)
        if project_for_spawn and project_for_spawn.infra_group_id:
            from sqlalchemy import select
            from app.models import InfraNode, InfraGroupNode
            result = await session.execute(
                select(InfraNode)
                .join(InfraGroupNode, InfraGroupNode.node_id == InfraNode.id)
                .where(InfraGroupNode.group_id == project_for_spawn.infra_group_id)
                .where(InfraNode.status == "connected")
            )
            nodes = result.scalars().all()
            if nodes:
                # 简单策略：选第一个可用节点（TODO: 负载均衡）
                node = nodes[0]
                logger.info(f"Agent {agent.id} will spawn on remote node {node.host} ({node.name})")

        cc_status = await infra_svc.get_cc_worker_status(agent.id, node=node)
        if not cc_status.get("running"):
            logger.info(f"Agent {agent.id} (role={agent.role}) container not running, spawning...")
            spawn_r = await infra_svc.spawn_cc_worker(
                agent_id=agent.id,
                role=agent.role,
                project_id=agent.project_id,
                node=node,
            )
            if not spawn_r.get("ok"):
                logger.error(f"Failed to spawn container for {agent.id}: {spawn_r.get('error')}")
                # 记录失败但不阻塞：可能容器在其他地方管理
                # 如果是 CC Worker 专属角色，spawn 失败则无法分配
                if not agent.container_id:
                    return False
            else:
                agent.container_id = spawn_r.get("container_id", "")
                # 记录 Worker 运行在哪个节点上
                if node and spawn_r.get("node_id"):
                    agent.config = {**(agent.config or {}), "infra_node_id": node.id, "infra_node_host": node.host}
                session.add(agent)
                await session.commit()
                logger.info(f"Agent {agent.id} container spawned: {agent.container_id[:12] if agent.container_id else 'unknown'}")

    project = await session.get(Project, task.project_id)
    team_id = await _find_team_for_task(session, task)
    team = await session.get(AgentTeam, team_id) if team_id else None
    team_policy = (team.default_review_policy if team else None) or {
        "auto_review_enabled": True,
        "require_human_review_complexities": ["critical"],
        "require_human_review_task_types": [],
    }
    ctx_policy = dict(task.context or {})
    if "require_human_review" not in ctx_policy:
        ctx_policy["require_human_review"] = _should_require_human_review_by_team_policy(task, team_policy)
    p_norm = _normalize_team_review_policy(team_policy)
    ctx_policy["team_review_policy_snapshot"] = {
        "team_id": team.id if team else None,
        "auto_review_enabled": bool(p_norm["auto_review_enabled"]),
        "require_human_review_complexities": sorted(p_norm["require_human_review_complexities"]),
        "require_human_review_task_types": sorted(p_norm["require_human_review_task_types"]),
    }
    task.context = ctx_policy
    project_map = project.role_model_map if project else None
    prefer_local = False

    if agent.role in ("senior", "mid", "junior"):
        complexity = getattr(task, "complexity", "medium") or "medium"
        actual_model, prefer_local = model_pool.resolve_model_by_complexity(
            complexity, role=agent.role, override=task.suggested_model, project_map=project_map,
        )
        logger.info(f"Engineer routing: task={task.ref_id} role={agent.role} complexity={complexity} → model={actual_model} local={prefer_local}")
    else:
        module_map = None
        if task.parent_task_id:
            parent = await session.get(Task, task.parent_task_id)
            module_map = _get_module_role_model_map(parent)
        actual_model = model_pool.resolve_model(agent.role, task.suggested_model, module_map=module_map, project_map=project_map)

    # 硬性检查：任务有最低模型等级要求时，模型不够格则自动升级
    min_tier = getattr(task, "min_tier", 0) or 0
    if min_tier > 0 and not model_pool.model_meets_tier(actual_model, min_tier):
        upgraded = model_pool.find_model_for_tier(min_tier)
        if upgraded:
            logger.info(f"Task {task.ref_id} requires tier≤{min_tier}, upgrading {actual_model} → {upgraded}")
            actual_model = upgraded
        else:
            logger.warning(f"Task {task.ref_id} requires tier≤{min_tier}, no qualified model available")
            return False

    # ── 任务级模型配置注入（CC Worker 协议适配）──
    # 根据角色和任务解析 Agent Provider 配置，注入 model_config 供 Worker 使用
    resolved_model, provider_cfg = agent_provider_pool.resolve_agent_model(
        agent.role, task.suggested_model or actual_model,
    )
    model_config = {
        "provider_name": provider_cfg.get("name", ""),
        "agent_type": provider_cfg.get("agent_type", "claude_code"),
        "protocol_adapter": provider_cfg.get("protocol_adapter", "anthropic_direct"),
        "litellm_config": provider_cfg.get("litellm_config", {}),
        "credential_env_name": provider_cfg.get("credential_env_name", "ANTHROPIC_API_KEY"),
        "api_base": provider_cfg.get("api_base", ""),
        "api_key": provider_cfg.get("api_key", ""),
        "model_mapping": provider_cfg.get("model_mapping", {}),
        "default_model": provider_cfg.get("default_model", ""),
        "supports_1m_context": provider_cfg.get("supports_1m_context", False),
        "resolved_model": resolved_model,
        "role_tier": agent_provider_pool.ROLE_DEFAULT_TIER.get(agent.role, "sonnet"),
    }
    ctx = dict(task.context or {})
    ctx["model_config"] = model_config
    task.context = ctx
    logger.info(
        f"Task {task.ref_id} model_config injected: adapter={model_config['protocol_adapter']}, "
        f"model={resolved_model}, provider={model_config['provider_name']}"
    )

    keywords = task.title.split()[:5]
    tech_stack = task.context.get("tech_stack") if task.context else None
    from app.services.knowledge_service import knowledge_svc
    relevant_exp: list = []
    exp_ctx = ""
    try:
        relevant_exp = await knowledge_svc.find_relevant_experiences(
            session, task_type=task.type, tech_stack=tech_stack, keywords=keywords,
            role=agent.role,
        )
        exp_ctx = knowledge_svc.format_experiences(relevant_exp)
        for exp_item in relevant_exp:
            await knowledge_svc.record_experience_use(session, exp_item.id)
    except Exception as e:
        logger.debug(f"Experience search skipped: {e}")

    escalation_ctx = await _build_escalation_context(session, task)

    # 加载任务关联的过程文档（分解阶段的架构决策、技术方案等）
    related_docs_ctx = await _build_related_docs_context(session, task)

    # ── Phase 2-4: Token 预算管理 ──
    # 按优先级分配知识上下文空间：架构约束 > 相关经验 > 通用知识
    from app.services.token_budget import KnowledgeSlice, allocate_budget

    # 从项目配置读取预算，默认 12000 字符 (~3000 tokens)
    project_cfg = dict(project.config or {}) if project and project.config else {}
    knowledge_budget = project_cfg.get("knowledge_budget", 12000)

    # 构建 context_keys，供 Worker 按需加载结构化知识块
    context_keys = knowledge_svc.build_context_keys(
        relevant_exp, has_parent_module=bool(task.parent_task_id),
    )

    # ── Phase 2-1: Leader 主动推送知识摘要（推模式）──
    knowledge_snippets = ""
    if context_keys:
        try:
            snippets = await knowledge_svc.get_snippets(
                session, context_keys, project_id=task.project_id,
            )
            if snippets:
                knowledge_snippets = f"## 相关上下文摘要\n{snippets}"
        except Exception as e:
            logger.debug(f"Knowledge snippets push skipped: {e}")

    knowledge_slices = [
        KnowledgeSlice(escalation_ctx, priority=1, label="escalation"),
        KnowledgeSlice(exp_ctx, priority=2, label="experience"),
        KnowledgeSlice(knowledge_snippets, priority=2, label="snippets"),
        KnowledgeSlice(related_docs_ctx, priority=3, label="docs"),
    ]

    # 知识图谱上下文（可选，需配置 CODEBASE_MEMORY_DB_PATH 并安装 codebase-memory-mcp）
    kg_ctx = ""
    if task.input_files:
        from app.core.config import settings
        kg_db_path = settings.CODEBASE_MEMORY_DB_PATH
        if kg_db_path:
            try:
                from app.services import knowledge_graph
                kg_ctx = await asyncio.get_event_loop().run_in_executor(
                    None,
                    knowledge_graph.get_task_context,
                    kg_db_path,
                    task.description,
                    task.input_files,
                )
            except Exception as e:
                logger.debug(f"Knowledge graph context skipped: {e}")
    if kg_ctx:
        knowledge_slices.append(KnowledgeSlice(kg_ctx, priority=3, label="kg"))

    knowledge_parts = allocate_budget(knowledge_slices, max_chars=knowledge_budget)

    ctx = dict(task.context or {})
    ctx["context_keys"] = context_keys
    task.context = ctx

    token_tracker.set_context(project_id=task.project_id)
    try:
        instruction = await ai_leader.generate_task_instruction(
            task_title=task.title,
            task_description=task.description,
            acceptance_criteria=task.acceptance_criteria or [],
            knowledge_ctx="\n\n".join(p for p in knowledge_parts if p),
            ref_id=task.ref_id or "",
            git_branch=task.git_branch or "",
            suggested_role=task.suggested_role or "",
            complexity=getattr(task, "complexity", "medium") or "medium",
        )
    except Exception as e:
        logger.error(f"Task instruction generation failed for {task.ref_id}: {e}")
        task.assigned_agent = None
        agent.status = "idle"
        agent.current_task_id = None
        session.add(task_lifecycle.transition(
            task,
            event="assign_prepare_failed",
            actor=agent.id,
            reason=f"instruction generation failed: {e}",
        ))
        session.add(TaskLog(
            task_id=task.id, agent_id=agent.id, action="assign_failed",
            message=f"任务指令生成失败: {e}",
        ))
        await session.commit()
        return False

    instruction = f"{instruction}\n\n{_build_env_gate_instruction(agent)}"
    instruction = f"{instruction}\n\n{_build_env_collaboration_instruction(agent)}"
    instruction = f"{instruction}\n\n{_build_execution_loop_protocol(agent)}"
    if (task.context or {}).get("collaboration_mode") == "human_guided":
        instruction = f"{instruction}\n\n{_build_human_guided_collaboration_instruction(task)}"
    if _is_reentry_task(task):
        instruction = f"{instruction}\n\n{_build_reentry_instruction(task, agent)}"
        ctx = dict(task.context or {})
        ctx["needs_focus_review"] = True
        task.context = ctx

    # 自动确保任务分支存在（项目有 git_repo）
    if task.ref_id:
        try:
            project = await session.get(Project, task.project_id)
            if project and project.git_repo:
                branch = task.git_branch or git_repo.task_branch_name(task.ref_id, task.title)
                base_branch, merge_target = _task_git_strategy(task, project)
                task.git_branch = branch
                ctx = dict(task.context or {})
                ctx["git_base_branch"] = base_branch
                ctx["git_merge_target"] = merge_target
                task.context = ctx
                await git_repo.ensure_repo(project.id, project.git_repo)
                await git_repo.create_branch(project.id, branch, base=base_branch)
                logger.info(f"Ensured branch {branch} from {base_branch} for task {task.ref_id}")
        except Exception as e:
            logger.warning(f"Git branch ensure failed (non-blocking): {e}")

    project_ctx = await role_context.build_project_context(session, task.project_id)

    # 架构师注入分解阶段的完整会话上下文 + 所属模块上下文
    stage_history_ctx = ""
    if agent.role == "architect":
        iteration_id = task.iteration_id or (task.context or {}).get("iteration_id")
        module_ctx = await _build_architect_module_context(session, task, agent)
        stage_history_ctx = await role_context.build_stage_history_context(
            session, task.project_id, iteration_id, max_total_chars=120000,
        )
        if module_ctx:
            stage_history_ctx = (stage_history_ctx + "\n\n" + module_ctx) if stage_history_ctx else module_ctx

    full_prompt = role_context.build_agent_prompt(
        role=agent.role,
        instruction=instruction,
        project_context=project_ctx,
        stage_history_context=stage_history_ctx,
        git_branch=task.git_branch or "",
        task_ref=task.ref_id or "",
    )
    agent_lifecycle.before_task_start(agent, task)
    attempt_id = uuid.uuid4().hex

    ctx_hints, executor_hint_resolved, actor_type_resolved = merge_assign_hints_into_context(
        task.context, agent_id=agent.id,
    )
    task.context = ctx_hints

    task_metadata = {
        "task_id": task.id,
        "project_id": task.project_id,
        "model": actual_model,
        "git_branch": task.git_branch or "",
        "git_base_branch": str((task.context or {}).get("git_base_branch") or ""),
        "git_merge_target": str((task.context or {}).get("git_merge_target") or ""),
        "ref_id": task.ref_id or "",
        "attempt_id": attempt_id,
        "executor_hint": executor_hint_resolved,
        "actor_type": actor_type_resolved,
    }

    try:
        await mq.publish_dispatch(
            agent_id=agent.id,
            instruction=full_prompt,
            metadata=task_metadata,
            model=actual_model,
        )
    except Exception as mq_err:
        logger.warning(f"MQ publish failed, falling back to HTTP: {mq_err}")
        result = await openclaw.send_task(
            agent_id=agent.id,
            instruction=full_prompt,
            metadata=task_metadata,
            model=actual_model,
            gateway_url=agent.webhook_url or "",
        )
        if "error" in result:
            logger.error(f"Failed to assign {task.id} to {agent.id}: {result}")
            return False

    now = datetime.now(timezone.utc)
    task.assigned_agent = agent.id
    task.suggested_model = actual_model
    ctx = dict(task.context or {})
    ctx["current_attempt_id"] = attempt_id
    ctx["execution_loop_protocol"] = {
        "global_notice_loop_independent": True,
        "code_channel": "git",
        "task_channel": "mq",
        "human_override_enabled": True,
        "default_supervisor": agent.supervisor_id or "architect",
    }
    ctx["environment_collaboration"] = {
        "independent_workspace": True,
        "pre_injected_env_is_baseline_only": True,
        "requires_architect_guidance": True,
        "self_heal_max_attempts": ENV_SELF_HEAL_MAX_ATTEMPTS,
    }
    # ── Phase 2-5: 文件白名单 ──
    # 计算并注入 allowed_paths，限制 CC Worker 可修改的文件范围
    allowed_paths = _build_allowed_paths(task)
    ctx["allowed_paths"] = allowed_paths
    task.context = ctx
    logger.info(f"Task {task.ref_id} allowed_paths: {allowed_paths}")
    agent.status = "busy"
    agent.current_task_id = task.id
    if (task.context or {}).get("architect_bootstrap") is True:
        cfg = dict(agent.config or {})
        cfg["architect_bootstrap_task_id"] = task.id
        agent.config = cfg
    session.add(task_lifecycle.on_assigned(
        task,
        agent_id=agent.id,
        model=actual_model,
        actor=agent.id,
    ))

    session.add(TaskLog(
        task_id=task.id,
        agent_id=agent.id,
        action="assigned",
        message=f"model={actual_model} level={task.escalation_level or 0} branch={task.git_branch or 'N/A'}",
        metadata_={
            "model": actual_model,
            "role": agent.role,
            "task_type": task.type,
            "suggested_role": task.suggested_role,
            "retry_count": task.retry_count,
            "escalation_level": task.escalation_level or 0,
            "git_branch": task.git_branch or "",
            "assigned_at": now.isoformat(),
            "attempt_id": attempt_id,
            "code_channel": "git",
            "task_channel": "mq",
            "human_override_enabled": True,
            "executor_hint": executor_hint_resolved,
            "actor_type": actor_type_resolved,
        },
    ))

    # 自动推进 current_stage：任务开始执行说明已进入编码阶段
    if project and project.current_stage == 4:
        project.current_stage = 5
        iteration = await session.get(Iteration, project.current_iteration_id) if project.current_iteration_id else None
        if iteration:
            iteration.current_stage = 5
        logger.info(f"Auto-advanced project {project.id} to Stage 5 (coding)")
    # 执行态一致化：一旦进入任务执行，项目从 planning 自动转 active
    if project and project.status == "planning":
        project.status = "active"
        logger.info(f"Auto-promoted project {project.id} status to active by task assignment")

    await session.commit()

    # 同时通过 inbox 消息通道发送 assign_task（面向任务的通信记录）
    try:
        from app.services import messaging
        await messaging.send(
            session,
            task_id=task.id,
            project_id=task.project_id,
            from_id="leader",
            to_id=agent.id,
            msg_type="assign_task",
            payload={
                "instruction": instruction[:500],
                "model": actual_model,
                "branch": task.git_branch or "",
                "git_base_branch": str((task.context or {}).get("git_base_branch") or ""),
                "ref_id": task.ref_id or "",
                "title": task.title,
                "attempt_id": attempt_id,
                "executor_hint": executor_hint_resolved,
                "actor_type": actor_type_resolved,
            },
        )
    except Exception as e:
        logger.warning(f"Inbox assign_task message failed (non-blocking): {e}")

    # CC Worker：与 OpenClaw 并列时落一条「全量执行指令」inbox，供 consume_cc_dispatch 或 UI 侧查看
    if executor_hint_resolved == "claude_code":
        try:
            from app.services import messaging

            await messaging.send(
                session,
                task_id=task.id,
                project_id=task.project_id,
                from_id="leader",
                to_id=agent.id,
                msg_type="cc_task_dispatch",
                payload={
                    "instruction": full_prompt,
                    "metadata": task_metadata,
                    "model": actual_model,
                },
            )
        except Exception as e:
            logger.warning(f"Inbox cc_task_dispatch message failed (non-blocking): {e}")

    return True


async def _record_agent_task_end(session: AsyncSession, agent_id: str, task: Task, outcome: str):
    if not agent_id or agent_id.startswith("human:"):
        return
    agent = await session.get(Agent, agent_id)
    if not agent:
        return
    agent_lifecycle.after_task_end(agent, task, outcome)


async def _auto_create_bug_task(session: AsyncSession, task: Task, agent: Agent, error_summary: str):
    """任务首次失败时自动创建 Bug 任务，分配给架构师评估。"""
    try:
        bug_task = Task(
            id=new_id(),
            project_id=task.project_id,
            iteration_id=task.iteration_id,
            title=f"[Bug] {task.title[:80]}",
            description=f"任务 {task.ref_id} 执行失败，需架构师评估是否为 Bug。\n\n"
                        f"失败 Agent: {agent.id} ({agent.role})\n"
                        f"错误摘要: {error_summary[:300]}\n"
                        f"重试次数: {task.retry_count}/{task.max_retries or 2}",
            type="bug",
            status="pending",
            complexity="medium",
            suggested_role="architect",
            estimated_hours=0.5,
            parent_task_id=task.id,
            context={"bug_source": "execution_failure", "failed_task_id": task.id, "failed_agent_id": agent.id},
        )
        session.add(bug_task)
        await session.flush()
        session.add(TaskLog(
            task_id=bug_task.id, agent_id="system", action="auto_created",
            message=f"自动创建 Bug 任务: {task.ref_id} 执行失败",
        ))
        logger.info("Auto-created bug task %s for failed task %s", bug_task.id, task.ref_id or task.id)
    except Exception as e:
        logger.warning("Failed to auto-create bug task: %s", e)


async def _release_agent_after_review(session: AsyncSession, agent_id: str):
    """审核完成后释放 Agent：清除 awaiting_review 状态和 last_submitted_task_id。"""
    if not agent_id:
        return
    agent = await session.get(Agent, agent_id)
    if not agent:
        return
    if agent.status == "awaiting_review":
        agent.status = "idle"
        cfg = dict(agent.config or {})
        cfg.pop("last_submitted_task_id", None)
        agent.config = cfg
        logger.info("Agent %s released from awaiting_review → idle", agent_id)


async def _architect_bootstrap_priority_guard(session: AsyncSession, agent: Agent, task: Task) -> bool:
    if agent.role != "architect":
        return True
    if (task.context or {}).get("architect_bootstrap") is True:
        return True
    q = await session.execute(
        select(Task).where(
            Task.project_id == agent.project_id,
            Task.context["architect_bootstrap"].astext == "true",
            Task.status == "pending",
            Task.assigned_agent == None,  # noqa: E711
        ).order_by(Task.created_at.asc()).limit(1)
    )
    bootstrap = q.scalar_one_or_none()
    if not bootstrap:
        return True
    # 若 bootstrap 卡在 pending 超过 30 分钟，不再阻塞 architect 取其他任务
    now = datetime.now(timezone.utc)
    if bootstrap.updated_at and (now - bootstrap.updated_at) > timedelta(minutes=30):
        logger.warning(
            "Bootstrap task %s pending >30min, allowing architect %s to take other tasks",
            bootstrap.ref_id or bootstrap.id,
            agent.id,
        )
        return True
    bootstrap_team_id = (bootstrap.context or {}).get("bootstrap_team_id")
    if bootstrap_team_id and agent.team_id != bootstrap_team_id:
        return True
    logger.info(
        "Architect bootstrap guard blocked assign: architect=%s task=%s pending_bootstrap=%s",
        agent.id,
        task.id,
        bootstrap.id,
    )
    return False


ROLE_SUPERVISOR = {
    "architect": "leader",
    "senior": "architect",
    "mid": "architect",
    "junior": "architect",
    "devops": "architect",
    "tester": "leader",
}


async def _ensure_agent_for_role(session: AsyncSession, project_id: str, role: str) -> Agent | None:
    """若某角色的 Agent 不存在，先部署成功再创建记录。部署失败不创建。"""
    agent_id = f"{role}-{project_id}"
    existing = await session.get(Agent, agent_id)
    if existing:
        return None

    try:
        node = await deploy_manager.get_infra_node(session, project_id)
        if not node:
            logger.warning(f"No infra node for project {project_id}, skip auto-create {role}")
            return None
        project = await session.get(Project, project_id)
        project_map = project.role_model_map if project else None
        model_id = (project_map or {}).get(role, model_pool.ROLE_MODEL_MAP.get(role, ""))
        await deploy_manager.generate_agent_deploy(
            session,
            project_id=project_id,
            role=role,
            model_id=model_id,
        )
        logger.info(f"Auto-deployed Agent {agent_id} to node {node.host}")
        agent = await session.get(Agent, agent_id)
        return agent
    except Exception as e:
        logger.warning(f"Auto-deploy Agent {agent_id} failed, not creating record: {e}")
        return None


async def _find_team_for_task(session: AsyncSession, task: Task) -> str | None:
    """根据任务所属模块找到负责的小组 team_id。未分配模块返回默认小组。"""
    mid = task.parent_task_id
    if mid:
        # 查所有小组，看哪个小组的 module_task_ids 包含此模块
        teams_q = await session.execute(
            select(AgentTeam).where(AgentTeam.project_id == task.project_id)
        )
        for team in teams_q.scalars():
            if mid in (team.module_task_ids or []):
                return team.id

    # 没有分配到特定小组 → 用默认小组
    default_q = await session.execute(
        select(AgentTeam).where(
            AgentTeam.project_id == task.project_id,
            AgentTeam.is_default == True,  # noqa: E712
        )
    )
    default_team = default_q.scalar_one_or_none()
    return default_team.id if default_team else None


def _normalize_team_review_policy(policy: dict | None) -> dict:
    src = dict(policy or {})
    return {
        "auto_review_enabled": bool(src.get("auto_review_enabled", True)),
        "require_human_review_complexities": {
            str(x).strip().lower()
            for x in (src.get("require_human_review_complexities") or [])
            if str(x).strip()
        },
        "require_human_review_task_types": {
            str(x).strip().lower()
            for x in (src.get("require_human_review_task_types") or [])
            if str(x).strip()
        },
    }


def _should_require_human_review_by_team_policy(task: Task, policy: dict | None) -> bool:
    p = _normalize_team_review_policy(policy)
    if not p["auto_review_enabled"]:
        return True
    complexity = str((task.complexity or "medium")).strip().lower()
    task_type = str((task.type or "feature")).strip().lower()
    return (
        complexity in p["require_human_review_complexities"]
        or task_type in p["require_human_review_task_types"]
    )


_assign_locks: dict[str, asyncio.Lock] = {}


async def _get_task_for_update(session: AsyncSession, task_id: str) -> Task | None:
    q = await session.execute(
        select(Task).where(Task.id == task_id).with_for_update()
    )
    return q.scalar_one_or_none()


def _project_role_model_map(project: Project | None) -> dict:
    data = (project.role_model_map or {}) if project else {}
    return data if isinstance(data, dict) else {}


async def _is_architect_model_unavailable(session: AsyncSession, project_id: str) -> tuple[bool, str]:
    """检查架构师模型是否可用。不可用时自动推进需要暂停，等待人工放行。"""
    project = await session.get(Project, project_id)
    if not project:
        return True, "项目不存在"
    ok, reason = model_pool.check_role_model_availability(
        "architect",
        project_map=_project_role_model_map(project),
    )
    return (not ok, reason)


async def dispatch_readiness(session: AsyncSession, project_id: str) -> dict:
    """
    调度前置条件检查（用于后台调度与前端可视化）。
    返回 blockers 列表，便于用户理解“正在等待什么”。
    """
    blockers: list[dict] = []
    project = await session.get(Project, project_id)
    project_cfg = dict(project.config or {})
    prototype_fast_track = bool(project_cfg.get("prototype_fast_track"))
    if not project:
        return {
            "ready": False,
            "project_id": project_id,
            "blockers": [{"code": "PROJECT_NOT_FOUND", "message": "项目不存在"}],
            "metrics": {},
        }

    if project.status in ("paused", "terminated", "archived"):
        blockers.append({
            "code": "PROJECT_STATUS_BLOCKED",
            "message": f"项目状态为 {project.status}，当前不可调度",
        })

    bootstrap_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.context["architect_bootstrap"].astext == "true",
        ).order_by(Task.created_at.asc()).limit(1)
    )
    bootstrap_task = bootstrap_q.scalar_one_or_none()
    bootstrap_unfinished = bool(
        bootstrap_task and (bootstrap_task.status not in TERMINAL_STATUSES and bootstrap_task.status != "done")
    )

    if (project.current_stage or 0) < 5 and not bootstrap_unfinished:
        blockers.append({
            "code": "STAGE_NOT_READY",
            "message": f"当前阶段为 {project.current_stage}，需进入编码阶段（>=5）后才能调度",
        })
    if (project.current_stage or 0) >= 5 and not str(project.git_repo or "").strip():
        blockers.append({
            "code": "GIT_REPO_MISSING",
            "message": "项目未配置 git_repo，禁止进入执行派单。请先补全仓库配置。",
        })

    default_team_q = await session.execute(
        select(AgentTeam).where(
            AgentTeam.project_id == project_id,
            AgentTeam.is_default == True,  # noqa: E712
        )
    )
    default_team = default_team_q.scalar_one_or_none()
    if not default_team:
        blockers.append({
            "code": "DEFAULT_TEAM_MISSING",
            "message": "默认团队不存在，请先初始化团队",
        })

    teams_q = await session.execute(select(AgentTeam).where(AgentTeam.project_id == project_id))
    teams = list(teams_q.scalars())
    for team in teams:
        aq = await session.execute(select(Agent).where(Agent.team_id == team.id, Agent.role == "architect"))
        architects = list(aq.scalars())
        if len(architects) == 0:
            blockers.append({
                "code": "TEAM_ARCHITECT_MISSING",
                "message": f"团队「{team.name}」缺少架构师，代码审核将无法自动进行",
            })
        elif len(architects) > 1:
            logger.info("Team %s has %d architects (expected 1), auto-assign continues", team.name, len(architects))

    if default_team:
        rq = await session.execute(select(Agent).where(Agent.team_id == default_team.id))
        agents_in_team = list(rq.scalars())
        role_set = {a.role for a in agents_in_team}
        required_roles = {"architect", "mid"} if prototype_fast_track else {"architect", "senior", "mid", "junior", "devops"}
        missing_roles = sorted(required_roles - role_set)
        # 只有完全没有可用 Agent 时才硬阻塞；角色不齐仅警告，不阻止调度
        if not agents_in_team:
            blockers.append({
                "code": "DEFAULT_TEAM_EMPTY",
                "message": "默认团队没有任何 Agent，无法调度",
            })
        elif missing_roles:
            logger.warning(
                "Default team missing roles: %s (have: %s). Dispatch continues with available agents.",
                ", ".join(missing_roles),
                ", ".join(sorted(role_set)),
            )

        # 架构师状态检查（降级为警告，不阻塞派单）
        # Agent 可以先干活，架构师后续审核即可
        arch_q = await session.execute(
            select(Agent).where(
                Agent.team_id == default_team.id,
                Agent.role == "architect",
            )
        )
        architect = arch_q.scalar_one_or_none()
        if not architect:
            logger.warning("Default team has no architect agent registered, review will be delayed")
        elif architect.last_heartbeat_status not in ("online", "busy"):
            logger.warning(
                "Architect %s is %s (not online/busy), tasks can be worked on but review will be delayed",
                architect.id,
                architect.last_heartbeat_status,
            )
        elif (architect.config or {}).get("retriever_ready", True) is False:
            logger.warning("Architect %s retriever not ready, tasks can be worked on", architect.id)
        else:
            challenge_status = (architect.config or {}).get("health_challenge_status")
            if challenge_status in ("expired", "mismatch"):
                logger.warning(
                    "Architect %s health challenge %s, tasks can be worked on but review may be delayed",
                    architect.id,
                    challenge_status,
                )

    init_task_q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == -1,
            GenerationTask.status.in_(["pending", "running"]),
        ).limit(1)
    )
    if init_task_q.scalar_one_or_none():
        blockers.append({
            "code": "TEAM_INIT_RUNNING",
            "message": "团队初始化任务仍在进行中，请等待完成",
        })

    breakdown_q = await session.execute(
        select(GenerationTask).where(
            GenerationTask.project_id == project_id,
            GenerationTask.stage == 4,
            GenerationTask.status.in_(["pending", "running"]),
        ).limit(1)
    )
    if breakdown_q.scalar_one_or_none():
        blockers.append({
            "code": "BREAKDOWN_RUNNING",
            "message": "任务分解尚未结束，分配条件未成熟",
        })

    ready_tasks = await get_ready_tasks(session, project_id)
    if not ready_tasks:
        blockers.append({
            "code": "NO_READY_TASK",
            "message": "暂无可分配任务（pending 且依赖满足）",
        })
    if not prototype_fast_track:
        pending_design_q = await session.execute(
            select(func.count()).where(
                Task.project_id == project_id,
                Task.status == "pending",
                Task.context["design_phase"].astext.in_(["needs_discussion", "needs_design_doc", "needs_review", "rejected"]),
            )
        )
        pending_design_count = int(pending_design_q.scalar() or 0)
        if pending_design_count > 0:
            blockers.append({
                "code": "DESIGN_PHASE_NOT_APPROVED",
                "message": f"有 {pending_design_count} 个任务处于设计流程中（未通过设计审核）",
            })

    idle_agents = await get_idle_agents(session, project_id)
    if not idle_agents:
        blockers.append({
            "code": "NO_IDLE_AGENT",
            "message": "暂无可用空闲 Agent（含全局知识门控）",
        })

    return {
        "ready": len(blockers) == 0,
        "project_id": project_id,
        "blockers": blockers,
        "metrics": {
            "ready_task_count": len(ready_tasks),
            "idle_agent_count": len(idle_agents),
            "current_stage": project.current_stage,
            "project_status": project.status,
            "human_override_allowed": True,
        },
    }


async def auto_assign(
    session: AsyncSession,
    project_id: str,
    actor_role: str = "system",
    task_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[str]:
    if not _can_dispatch(actor_role):
        logger.debug(
            f"Auto-assign skipped by dispatch policy: project={project_id}, actor_role={actor_role}, mode={DISPATCH_MODE}"
        )
        return []
    readiness = await dispatch_readiness(session, project_id)
    if not readiness.get("ready"):
        logger.debug(
            "Auto-assign blocked by readiness checks: project=%s, blockers=%s",
            project_id,
            [b.get("code") for b in readiness.get("blockers", [])],
        )
        return []
    lock = _assign_locks.setdefault(project_id, asyncio.Lock())
    if lock.locked():
        return []
    async with lock:
        return await _do_auto_assign(session, project_id, task_ids=task_ids, limit=limit)


async def _do_auto_assign(
    session: AsyncSession,
    project_id: str,
    task_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[str]:
    ready = await get_ready_tasks(session, project_id)
    if not ready:
        return []
    if task_ids:
        allow = set(task_ids)
        ready = [t for t in ready if t.id in allow]
        if not ready:
            return []

    # 按小组分组任务
    tasks_by_team: dict[str | None, list[Task]] = {}
    for t in ready:
        tid = await _find_team_for_task(session, t)
        tasks_by_team.setdefault(tid, []).append(t)

    assigned = []

    for team_id, tasks in tasks_by_team.items():
        if not team_id:
            logger.warning(
                "Auto-assign: %d tasks have no team (no default team in project %s), skipped. "
                "Please initialize a default team.",
                len(tasks),
                project_id,
            )
            continue

        idle_list = await get_idle_agents(session, project_id, team_id=team_id)
        if not idle_list:
            continue

        online_idle = [a for a in idle_list if a.last_heartbeat_status in ("online", "busy")]
        if not online_idle:
            logger.debug(f"Team {team_id}: all idle agents are offline, skipping assignment")
            continue

        for task in tasks:
            if not online_idle:
                break
            # 优先匹配 suggested_role
            agent = None
            for i, a in enumerate(online_idle):
                if a.role == task.suggested_role:
                    agent = online_idle.pop(i)
                    break
            # fallback: 任意工程师角色
            if not agent:
                for i, a in enumerate(online_idle):
                    if a.role in ("senior", "mid", "junior"):
                        agent = online_idle.pop(i)
                        break
            # fallback: 任意空闲 Agent
            if not agent:
                agent = online_idle.pop(0)
            if await assign_task(session, task, agent):
                assigned.append(task.id)
                if limit and len(assigned) >= limit:
                    return assigned

    return assigned


async def complete_task(
    session: AsyncSession,
    task_id: str,
    agent_id: str,
    result: str = "",
    token_usage: dict | None = None,
    duration_ms: int | None = None,
    attempt_id: str | None = None,
) -> dict:
    """Agent 完成编码后进入 reviewing 状态，等待架构师审核"""
    task = await session.get(Task, task_id)
    agent = await session.get(Agent, agent_id)
    if not task or not agent:
        return {"error": "not found"}

    if task.status in TERMINAL_STATUSES | {"reviewing"}:
        logger.info(f"Task {task.ref_id or task_id} already {task.status}, skip duplicate complete")
        return {"status": task.status, "skipped": True}
    if not _attempt_matches(task, attempt_id):
        logger.info(
            "Ignore stale complete callback: task=%s current_attempt=%s incoming_attempt=%s agent=%s",
            task.ref_id or task_id,
            (task.context or {}).get("current_attempt_id"),
            attempt_id,
            agent_id,
        )
        return {"status": task.status, "skipped": True, "reason": "stale_attempt"}

    now = datetime.now(timezone.utc)
    task.result = result
    profile = role_context.get_role_profile(agent.role if agent else "")
    if profile:
        ctx_with_gate = dict(task.context or {})
        ctx_with_gate["gate_evidence"] = {
            "role": agent.role if agent else "",
            "required_outputs": profile.get("required_outputs") or [],
            "completion_checks": profile.get("gate_rules", {}).get("completion_check") or [],
            "forbidden": profile.get("forbidden") or [],
        }
        task.context = ctx_with_gate
    if (task.context or {}).get("collaboration_mode") == "human_guided" and not bool((task.context or {}).get("human_execute_approved")):
        task.assigned_agent = None
        ctx_wait = dict(task.context or {})
        ctx_wait["human_decision_required"] = True
        task.context = ctx_wait
        session.add(task_lifecycle.on_terminal(
            task,
            status="blocked",
            actor=agent_id,
            reason="human decision required in collaboration mode",
        ))
        was_draining = agent.status == "draining"
        agent.status = "idle"
        agent.current_task_id = None
        session.add(TaskLog(
            task_id=task_id,
            agent_id=agent_id,
            action="collaboration_wait_human_decision",
            message="人类主导模式：方案阶段完成，等待人类批准执行",
            metadata_={"collaboration_mode": "human_guided", "phase": "design"},
        ))
        await session.commit()
        if was_draining:
            logger.info(f"Agent {agent_id} was draining, stopping after collaboration design phase")
            try:
                from app.routers.agents import _get_agent_backend
                backend, agent_config = await _get_agent_backend(session, agent)
                await backend.stop_agent(agent_id, agent.role, agent_config)
                agent.last_heartbeat_status = "offline"
                await session.commit()
            except Exception as e:
                logger.warning(f"Failed to stop draining agent {agent_id}: {e}")
        return {"status": "blocked", "task_id": task_id, "needs_human_decision": True}
    session.add(task_lifecycle.on_reviewing(
        task,
        actor=agent_id,
        reason=result or "agent submit for review",
    ))
    was_draining = agent.status == "draining"
    # 提交后不立即变 idle，等待审核结果。审核通过再释放，打回则优先返工
    agent.status = "awaiting_review"
    agent.current_task_id = None
    # 记录最后提交的任务，审核打回时可优先分配给原 Agent
    cfg = dict(agent.config or {})
    cfg["last_submitted_task_id"] = task_id
    agent.config = cfg

    from sqlalchemy import select as sa_select
    q = await session.execute(
        sa_select(TaskLog).where(
            TaskLog.task_id == task_id,
            TaskLog.action == "assigned",
        ).order_by(TaskLog.created_at.desc()).limit(1)
    )
    assign_log = q.scalar_one_or_none()

    elapsed_s = None
    if assign_log:
        elapsed_s = (now - assign_log.created_at).total_seconds()

    used_model = assign_log.metadata_.get("model") if assign_log and assign_log.metadata_ else None

    session.add(TaskLog(
        task_id=task_id,
        agent_id=agent_id,
        action="submitted",
        message=f"代码已提交，等待架构师审核。{result[:300] if result else ''}",
        metadata_={
            "model": used_model,
            "role": agent.role,
            "task_type": task.type,
            "retry_count": task.retry_count,
            "elapsed_seconds": elapsed_s,
            "duration_ms": duration_ms,
            "token_usage": token_usage,
        },
    ))
    await session.commit()

    # 角色边界：Leader 不做技术审核，任务提交后默认等待人类审核。
    # 仅当任务显式标记 auto_review_allowed 或团队策略标记可自动审核时，才跳过人类确认。
    focus_review = bool((task.context or {}).get("needs_focus_review"))
    auto_review_allowed = bool((task.context or {}).get("auto_review_allowed"))
    team_require_human = bool((task.context or {}).get("require_human_review"))
    architect_model_unavailable, model_reason = await _is_architect_model_unavailable(session, task.project_id)
    # 人类审核的触发条件：团队策略要求 OR 未显式允许自动审核 OR 架构师模型不可用
    require_human_review = team_require_human or not auto_review_allowed
    if architect_model_unavailable:
        require_human_review = True
    auto_review_enabled = not require_human_review
    if require_human_review:
        if architect_model_unavailable:
            wait_reason = (
                f"已提交，架构师模型不可用（{model_reason}），系统自动推进暂停；"
                "请由人类手工审核放行。"
                + ("；该任务为重入续跑，请重点复核关键改动与边界" if focus_review else "")
            )
        else:
            wait_reason = (
                "已提交，等待架构师审核（Leader 不参与代码评审）"
                + "；该任务已标记必须人工审核"
                + ("；该任务为重入续跑，请重点复核关键改动与边界" if focus_review else "")
            )
        session.add(TaskLog(
            task_id=task.id,
            agent_id="system",
            action="review_waiting_architect",
            message=wait_reason,
        ))
    else:
        session.add(TaskLog(
            task_id=task.id,
            agent_id="system",
            action="review_auto_mode",
            message="已提交，未标记人工强审，进入无人值守自动审核",
        ))
    await session.commit()

    if auto_review_enabled:
        auto_result = await review_completed_task(
            session,
            task_id=task_id,
            action="approve",
            reviewer="system:auto",
            comments="无人值守模式自动审核通过（未标记人工强审）",
        )
        auto_result["auto_review"] = True
        auto_result["require_human_review"] = False
        return auto_result

    # 通过 inbox 消息通道通知架构师审核
    try:
        architect_id = agent.supervisor_id
        if architect_id:
            from app.services import messaging
            await messaging.send(
                session,
                task_id=task_id,
                project_id=task.project_id,
                from_id=agent_id,
                to_id=architect_id,
                msg_type="task_completed",
                payload={
                    "result": result[:500] if result else "",
                    "ref_id": task.ref_id or "",
                    "title": task.title,
                },
            )
    except Exception as e:
        logger.warning(f"Inbox task_completed message failed (non-blocking): {e}")

    # draining 模式：任务完成后自动停止 Agent 容器
    if was_draining:
        logger.info(f"Agent {agent_id} was draining, stopping after task completion")
        try:
            from app.routers.agents import _get_agent_backend
            backend, agent_config = await _get_agent_backend(session, agent)
            await backend.stop_agent(agent_id, agent.role, agent_config)
            agent.last_heartbeat_status = "offline"
            await session.commit()
        except Exception as e:
            logger.warning(f"Failed to stop draining agent {agent_id}: {e}")

    return {"status": "reviewing", "task_id": task_id, "require_human_review": True}


async def update_task_progress(
    session: AsyncSession,
    task_id: str,
    agent_id: str,
    status: str = "",
    message: str = "",
    progress: int = 0,
) -> dict:
    """
    统一处理执行过程回调（task_update）。
    仅允许通过生命周期事件推进非终态：
      assigned -> executing -> reviewing
    终态由 complete_task / fail_task / need_help 专用入口处理。
    """
    task = await session.get(Task, task_id)
    if not task:
        return {"error": "task not found"}

    next_status = (status or "").strip().lower()
    if next_status and next_status != task.status:
        try:
            if next_status == "executing":
                session.add(task_lifecycle.transition(
                    task,
                    event="start_execute",
                    actor=agent_id or "agent",
                    reason=message or "task execution started",
                    metadata={"source": "task_update"},
                ))
            elif next_status == "reviewing":
                session.add(task_lifecycle.transition(
                    task,
                    event="submit_review",
                    actor=agent_id or "agent",
                    reason=message or "task submitted for review",
                    metadata={"source": "task_update"},
                ))
            elif next_status in ("done", "failed", "blocked", "cancelled"):
                logger.info(
                    "Ignore terminal status in task_update: task=%s status=%s agent=%s",
                    task_id, next_status, agent_id,
                )
            else:
                logger.info(
                    "Ignore unsupported task_update status: task=%s from=%s to=%s",
                    task_id, task.status, next_status,
                )
        except ValueError as e:
            logger.warning(
                "Progress transition rejected: task=%s from=%s to=%s reason=%s",
                task_id, task.status, next_status, e,
            )

    session.add(TaskLog(
        task_id=task_id,
        agent_id=agent_id,
        action="progress",
        message=message or "",
        metadata_={
            "progress": progress,
            "reported_status": next_status or task.status,
        },
    ))
    await session.commit()
    return {"status": task.status, "task_id": task_id}


MAX_REVIEW_REJECTS = 2
MAX_REVIEW_ESCALATIONS = 3  # 审核升级最多几轮：1=顶级模型 2=架构师 3+=blocked


async def review_completed_task(
    session: AsyncSession,
    task_id: str,
    action: str,
    reviewer: str = "architect",
    comments: str = "",
) -> dict:
    """架构师审核已完成的任务：approve → done，reject → 打回重做"""
    task = await _get_task_for_update(session, task_id)
    if not task:
        return {"error": "not found"}
    if task.status != "reviewing":
        return {"error": f"任务状态为 {task.status}，不是 reviewing"}
    prev_assigned_agent = task.assigned_agent or ""

    # 保险阀：若历史升级轮次已超阈值，直接维持/设置为 blocked，防止异常循环继续放大
    existing_rounds = int((task.context or {}).get("review_escalation_rounds", 0) or 0)
    if existing_rounds >= MAX_REVIEW_ESCALATIONS:
        task.assigned_agent = None
        task.escalation_level = 3
        session.add(task_lifecycle.on_terminal(
            task,
            status="blocked",
            actor=reviewer,
            reason=f"review escalation exhausted: {existing_rounds}",
        ))
        session.add(TaskLog(
            task_id=task_id,
            agent_id=reviewer,
            action="escalation_guard",
            message=f"审核升级轮次已达上限({existing_rounds})，任务强制维持 blocked，等待人类处理",
        ))
        await _record_agent_task_end(session, prev_assigned_agent, task, "blocked")
        await session.commit()
        return {"status": "blocked", "escalation_level": 3, "review_escalation_rounds": existing_rounds}

    now = datetime.now(timezone.utc)
    task.updated_at = now
    review_count = (task.context or {}).get("review_reject_count", 0)

    if action == "approve":
        gate_evidence = (task.context or {}).get("gate_evidence") or {}
        session.add(task_lifecycle.on_terminal(
            task,
            status="done",
            actor=reviewer,
            reason=comments or "review approved",
        ))
        session.add(TaskLog(
            task_id=task_id, agent_id=reviewer, action="review_approved",
            message=comments or "代码审核通过",
            metadata_={"gate_evidence": gate_evidence} if gate_evidence else {},
        ))
        await _record_agent_task_end(session, prev_assigned_agent, task, "done")
        # 审核通过 → 释放原 Agent 为 idle
        await _release_agent_after_review(session, prev_assigned_agent)

        await check_and_complete_module(session, task)

        if task.git_branch:
            try:
                project = await session.get(Project, task.project_id)
                if project and project.git_repo:
                    _, merge_target = _task_git_strategy(task, project)
                    commits = await git_repo.get_branch_commits(project.id, task.git_branch, limit=50)
                    task.git_commits = commits

                    # 架构师确保 workspace 存在后再合并
                    await git_repo.ensure_repo(project.id, project.git_repo)
                    merge_result = await git_repo.merge_branch(project.id, task.git_branch, target=merge_target)
                    task.merge_status = "merged" if merge_result["ok"] else "conflict"
                    task.merge_commit = merge_result.get("commit", "")
                    if not merge_result["ok"]:
                        logger.warning(f"Branch merge failed for {task.ref_id}: {merge_result.get('error')}")
            except Exception as e:
                logger.warning(f"Git merge failed (non-blocking): {e}")
                task.merge_status = "error"

        await session.commit()

        await _post_review_approve(session, task, reviewer)

        bootstrap_notice = await _after_bootstrap_done_notify_global_knowledge(session, task, reviewer)
        newly = await auto_assign(session, task.project_id, actor_role=reviewer or "architect")

        all_done = await _check_all_tasks_done(session, task.project_id)
        resp: dict = {"status": "done", "newly_assigned": newly}
        if bootstrap_notice:
            resp["bootstrap_global_notice"] = bootstrap_notice
        if task.git_branch:
            resp["merge_status"] = task.merge_status
        if all_done:
            resp["all_tasks_done"] = True
            resp["ready_for_integration_test"] = True
            try:
                await _on_all_tasks_done(session, task.project_id)
            except Exception as e:
                logger.warning(f"Integration test trigger failed (non-blocking): {e}")
        return resp

    # reject
    review_count += 1
    ctx = dict(task.context or {})
    ctx["review_reject_count"] = review_count
    ctx["last_review_comments"] = comments
    task.context = ctx

    if review_count >= MAX_REVIEW_REJECTS:
        escalation_rounds = (task.context or {}).get("review_escalation_rounds", 0) + 1
        ctx["review_escalation_rounds"] = escalation_rounds
        ctx["review_reject_count"] = 0  # 重置审核计数，进入新一轮
        task.context = ctx

        history = list(task.escalation_history or [])
        history.append({
            "action": "review_escalated",
            "reason": f"代码审核 {review_count} 次不通过（第 {escalation_rounds} 轮升级）",
            "comments": comments,
            "timestamp": now.isoformat(),
        })
        task.escalation_history = history

        if escalation_rounds >= MAX_REVIEW_ESCALATIONS:
            task.assigned_agent = None
            task.escalation_level = 3
            session.add(task_lifecycle.on_terminal(
                task,
                status="blocked",
                actor=reviewer,
                reason=f"review escalation blocked: {comments}",
            ))

            session.add(TaskLog(
                task_id=task_id, agent_id=reviewer, action="escalated",
                message=f"审核已升级 {escalation_rounds} 轮仍未通过，需要人类介入: {comments}",
            ))
            await _record_agent_task_end(session, prev_assigned_agent, task, "blocked")
            await session.commit()
            logger.warning(f"Task {task.ref_id or task_id} review escalation exhausted → blocked")
            return {"status": "blocked", "escalation_level": 3, "review_escalation_rounds": escalation_rounds}

        task.assigned_agent = None
        task.retry_count = 0
        session.add(task_lifecycle.transition(
            task,
            event="requeue",
            actor=reviewer,
            reason=f"review escalated requeue: {comments}",
        ))

        if escalation_rounds == 1:
            task.suggested_model = "opus"
            task.escalation_level = max(task.escalation_level or 0, 1)
            escalation_target = "顶级模型"
        else:
            task.suggested_role = "architect"
            task.suggested_model = "opus"
            task.escalation_level = max(task.escalation_level or 0, 2)
            escalation_target = "架构师"

        session.add(TaskLog(
            task_id=task_id, agent_id=reviewer, action="review_escalated",
            message=f"代码审核 {review_count} 次不通过，升级为{escalation_target}执行（第 {escalation_rounds} 轮）: {comments}",
        ))
        await session.commit()

        await auto_assign(session, task.project_id, actor_role=reviewer or "architect")
        return {"status": f"escalated_to_{escalation_target}", "review_count": review_count, "escalation_round": escalation_rounds}

    # 普通打回：优先让原 Agent 返工（熟悉代码上下文，无需切换）
    original_agent = await session.get(Agent, prev_assigned_agent) if prev_assigned_agent else None
    task.assigned_agent = None
    task.retry_count = (task.retry_count or 0) + 1
    session.add(task_lifecycle.transition(
        task,
        event="rework",
        actor=reviewer,
        reason=f"review rejected: {comments}",
    ))
    # 释放原 Agent 并重新分配：原 Agent 优先
    if original_agent:
        await _release_agent_after_review(session, prev_assigned_agent)
        await session.flush()
        if not await assign_task(session, task, original_agent):
            # 原 Agent 不可用，放回队列
            logger.info("Original agent %s unavailable for rework, requeuing task %s", prev_assigned_agent, task.ref_id or task.id)
        else:
            logger.info("Re-assigned task %s to original agent %s for rework", task.ref_id or task.id, prev_assigned_agent)

    session.add(TaskLog(
        task_id=task_id, agent_id=reviewer, action="review_rejected",
        message=f"代码审核不通过 ({review_count}/{MAX_REVIEW_REJECTS}): {comments}",
    ))
    await session.commit()

    seq = await _iter_seq(session, task.iteration_id)
    await _archive_safe(
        session, project_id=task.project_id, iteration_id=task.iteration_id,
        iteration_seq=seq, task_id=task_id, ref_id=task.ref_id or "",
        doc_type="review_record",
        title=f"[{task.ref_id}] 审核不通过 ({review_count}/{MAX_REVIEW_REJECTS})",
        content=f"# [{task.ref_id}] {task.title} - 代码审核\n\n"
                f"- 审核人: {reviewer}\n- 结果: 不通过\n- 次数: {review_count}/{MAX_REVIEW_REJECTS}\n\n"
                f"## 审核意见\n{comments}\n",
        tags=["review", "rejected", task.ref_id or ""],
    )

    # 通过 inbox 通知工程师修改（审核打回消息）
    try:
        from app.services import messaging
        # 找到最近一次执行该任务的 Agent
        last_assigned_q = await session.execute(
            select(TaskLog).where(
                TaskLog.task_id == task_id, TaskLog.action == "assigned",
            ).order_by(TaskLog.created_at.desc()).limit(1)
        )
        last_assigned = last_assigned_q.scalar_one_or_none()
        target_agent = last_assigned.agent_id if last_assigned else None
        if target_agent:
            await messaging.send(
                session,
                task_id=task_id,
                project_id=task.project_id,
                from_id=reviewer,
                to_id=target_agent,
                msg_type="revise_request",
                payload={
                    "comments": comments,
                    "review_count": review_count,
                    "max_rejects": MAX_REVIEW_REJECTS,
                    "branch": task.git_branch or "",
                    "git_base_branch": str((task.context or {}).get("git_base_branch") or ""),
                    "ref_id": task.ref_id or "",
                },
            )
    except Exception as e:
        logger.warning(f"Inbox revise_request message failed (non-blocking): {e}")

    await auto_assign(session, task.project_id, actor_role=reviewer or "architect")
    return {"status": "rejected", "review_count": review_count, "max": MAX_REVIEW_REJECTS}


async def _post_review_approve(session: AsyncSession, task: Task, reviewer: str):
    """审核通过后：经验提取 + 归档执行报告"""
    task_id = task.id

    if task.retry_count and task.retry_count > 0:
        retry_logs = await session.execute(
            select(TaskLog).where(
                TaskLog.task_id == task_id,
                TaskLog.action.in_(["retry", "failed", "review_rejected"]),
            ).order_by(TaskLog.created_at)
        )
        error_history = [
            log.metadata_.get("error_summary", log.message)
            for log in retry_logs.scalars()
            if log.metadata_.get("error_summary") or log.message
        ]
        if error_history:
            project = await session.get(Project, task.project_id)
            project_name = project.name if project else ""
            project_tech_stack = []
            if project and project.config:
                project_tech_stack = project.config.get("tech_stack", []) if isinstance(project.config, dict) else []
            try:
                await knowledge_svc.extract_experience_from_retry(
                    session,
                    task_title=task.title,
                    task_description=task.description,
                    error_history=error_history,
                    final_result=task.result or "",
                    retry_count=task.retry_count,
                    used_model="",
                    project_name=project_name,
                    task_id=task_id,
                    project_tech_stack=project_tech_stack,
                )
            except Exception as e:
                logger.warning(f"Experience extraction failed (non-blocking): {e}")

    seq = await _iter_seq(session, task.iteration_id)
    report_content = (
        f"# [{task.ref_id}] {task.title} - 执行报告\n\n"
        f"- 状态: 审核通过\n- 审核人: {reviewer}\n"
        f"- 重试次数: {task.retry_count}\n- 升级层级: {task.escalation_level}\n\n"
        f"## 执行结果\n{(task.result or '')[:3000]}\n"
    )
    await _archive_safe(
        session, project_id=task.project_id, iteration_id=task.iteration_id,
        iteration_seq=seq, task_id=task_id, ref_id=task.ref_id or "",
        doc_type="task_report", title=f"[{task.ref_id}] 执行报告",
        content=report_content, tags=["report", task.ref_id or "", "completed"],
    )


"""
升级链（escalation_level）:
  0 = 工程师执行（重试 max_retries 次，默认2次，最后一次尝试升级模型）
  1 = 工程师/顶级模型执行（升级为 tier-1 模型，重试 max_retries 次）
  2 = 架构师亲自执行（全局视角，用顶级模型，重试 max_retries 次）
  3 = 人类介入（标记 blocked，等待人类认领）
"""

ESCALATION_LABELS = {0: "工程师", 1: "工程师/顶级模型", 2: "架构师", 3: "人类"}


async def fail_task(
    session: AsyncSession,
    task_id: str,
    agent_id: str,
    error: str = "",
    token_usage: dict | None = None,
    duration_ms: int | None = None,
    attempt_id: str | None = None,
) -> dict:
    task = await session.get(Task, task_id)
    agent = await session.get(Agent, agent_id)
    if not task or not agent:
        return {"error": "not found"}

    # 幂等/状态机保护：仅执行态任务允许进入失败重试链，避免重复回调把任务反复打回
    if task.status not in ("assigned", "executing"):
        logger.info(f"Ignore stale fail callback: task={task.ref_id or task_id}, status={task.status}, agent={agent_id}")
        return {"status": task.status, "skipped": True}
    if not _attempt_matches(task, attempt_id):
        logger.info(
            "Ignore stale fail callback by attempt: task=%s current_attempt=%s incoming_attempt=%s agent=%s",
            task.ref_id or task_id,
            (task.context or {}).get("current_attempt_id"),
            attempt_id,
            agent_id,
        )
        return {"status": task.status, "skipped": True, "reason": "stale_attempt"}

    task.retry_count = (task.retry_count or 0) + 1
    now = datetime.now(timezone.utc)
    max_retries = task.max_retries or 2
    level = task.escalation_level or 0

    # 首次失败 → 自动创建 Bug 任务供架构师评估
    if task.retry_count == 1 and level == 0:
        await _auto_create_bug_task(session, task, agent, error[:500])

    from sqlalchemy import select as sa_select
    q = await session.execute(
        sa_select(TaskLog).where(
            TaskLog.task_id == task_id,
            TaskLog.action == "assigned",
        ).order_by(TaskLog.created_at.desc()).limit(1)
    )
    assign_log = q.scalar_one_or_none()
    used_model = assign_log.metadata_.get("model") if assign_log and assign_log.metadata_ else None

    log_meta = {
        "model_used": used_model,
        "role": agent.role,
        "task_type": task.type,
        "retry_count": task.retry_count,
        "escalation_level": level,
        "error_summary": error[:500],
        "token_usage": token_usage,
        "duration_ms": duration_ms,
    }

    agent.status = "idle"
    agent.current_task_id = None
    task.updated_at = now

    # 当前层级还有重试次数
    if task.retry_count <= max_retries:
        task.assigned_agent = None
        session.add(task_lifecycle.transition(
            task,
            event="retry",
            actor=agent_id,
            reason=f"retry {task.retry_count}/{max_retries}",
        ))

        upgraded = None
        if task.retry_count == max_retries and task.suggested_model:
            upgraded = model_pool.upgrade_model(task.suggested_model)
            if upgraded:
                task.suggested_model = upgraded

        msg = f"[{ESCALATION_LABELS.get(level, '?')}] 重试 {task.retry_count}/{max_retries}: {error[:200]}"
        if upgraded:
            msg += f" | 模型升级为 {upgraded}"
        log_meta["model_upgraded_to"] = upgraded

        session.add(TaskLog(task_id=task_id, agent_id=agent_id, action="retry", message=msg, metadata_=log_meta))
        await session.commit()

        seq = await _iter_seq(session, task.iteration_id)
        await _archive_safe(
            session, project_id=task.project_id, iteration_id=task.iteration_id,
            iteration_seq=seq, task_id=task_id, ref_id=task.ref_id or "",
            doc_type="error_log",
            title=f"[{task.ref_id}] 重试 {task.retry_count}/{max_retries}",
            content=f"# [{task.ref_id}] {task.title} - 错误记录\n\n"
                    f"- 层级: {ESCALATION_LABELS.get(level, '?')}\n"
                    f"- 重试: {task.retry_count}/{max_retries}\n"
                    f"- 模型: {used_model}\n\n## 错误信息\n{error[:2000]}\n",
            tags=["error", task.ref_id or ""],
        )

        await auto_assign(session, task.project_id, actor_role="architect")
        return {"status": "retrying", "retry_count": task.retry_count, "escalation_level": level, "model_upgraded": upgraded}

    # 当前层级重试耗尽 → 升级
    history = list(task.escalation_history or [])
    history.append({
        "from_level": level, "to_level": level + 1,
        "reason": error[:500], "retry_count": task.retry_count,
        "model_used": used_model, "timestamp": now.isoformat(),
    })
    task.escalation_history = history
    seq = await _iter_seq(session, task.iteration_id)

    if level == 0:
        # 工程师失败 → 升级为顶级模型重做
        task.escalation_level = 1
        task.retry_count = 0
        task.assigned_agent = None
        task.suggested_model = "opus"
        session.add(task_lifecycle.transition(
            task,
            event="escalate",
            actor=agent_id,
            reason="escalate to top-tier model",
        ))

        session.add(TaskLog(
            task_id=task_id, agent_id=agent_id, action="escalated",
            message=f"工程师 {max_retries}次重试失败，升级为顶级模型重做: {error[:200]}",
            metadata_=log_meta,
        ))
        await session.commit()
        logger.warning(f"Task {task.ref_id or task_id} escalated to top-tier model")

        await _archive_safe(
            session, project_id=task.project_id, iteration_id=task.iteration_id,
            iteration_seq=seq, task_id=task_id, ref_id=task.ref_id or "",
            doc_type="escalation_record",
            title=f"[{task.ref_id}] 升级: 工程师 → 顶级模型",
            content=f"# [{task.ref_id}] {task.title}\n\n"
                    f"## 升级路径\n工程师 ({max_retries}次重试) → **顶级模型**\n\n"
                    f"## 失败原因\n{error[:2000]}\n",
            tags=["escalation", task.ref_id or "", "top-model"],
        )

        await auto_assign(session, task.project_id, actor_role="architect")
        return {"status": "escalated", "escalation_level": 1, "escalated_to": "top-model"}

    elif level == 1:
        # 顶级模型工程师也失败 → 架构师亲自执行
        task.escalation_level = 2
        task.retry_count = 0
        task.assigned_agent = None
        task.suggested_role = "architect"
        task.suggested_model = "opus"
        session.add(task_lifecycle.transition(
            task,
            event="escalate",
            actor=agent_id,
            reason="escalate to architect",
        ))

        session.add(TaskLog(
            task_id=task_id, agent_id=agent_id, action="escalated",
            message=f"顶级模型 {max_retries}次重试失败，升级为架构师亲自执行: {error[:200]}",
            metadata_=log_meta,
        ))
        await session.commit()
        logger.warning(f"Task {task.ref_id or task_id} escalated to ARCHITECT")

        await _archive_safe(
            session, project_id=task.project_id, iteration_id=task.iteration_id,
            iteration_seq=seq, task_id=task_id, ref_id=task.ref_id or "",
            doc_type="escalation_record",
            title=f"[{task.ref_id}] 升级: 顶级模型 → 架构师",
            content=f"# [{task.ref_id}] {task.title}\n\n"
                    f"## 升级路径\n工程师 → 顶级模型 ({max_retries}次重试) → **架构师亲自执行**\n\n"
                    f"## 失败原因\n{error[:2000]}\n",
            tags=["escalation", task.ref_id or "", "architect"],
        )

        await auto_assign(session, task.project_id, actor_role="architect")
        return {"status": "escalated", "escalation_level": 2, "escalated_to": "architect"}

    elif level == 2:
        # 架构师也失败 → 人类介入
        task.escalation_level = 3
        task.retry_count = 0
        task.assigned_agent = None
        if _is_reentry_task(task):
            ctx = dict(task.context or {})
            chain = list(ctx.get("reentry_chain") or [])
            chain.append({
                "stage": "successive_run_failed",
                "timestamp": now.isoformat(),
                "actor": agent_id,
                "error": error[:500],
            })
            ctx["reentry_chain"] = chain[-30:]
            task.context = ctx
        session.add(task_lifecycle.on_terminal(
            task,
            status="blocked",
            actor=agent_id,
            reason="escalate to human intervention",
        ))

        session.add(TaskLog(
            task_id=task_id, agent_id=agent_id, action="escalated",
            message=f"架构师 {max_retries}次重试失败，需要人类介入: {error[:200]}",
            metadata_=log_meta,
        ))
        if _is_reentry_task(task):
            session.add(TaskLog(
                task_id=task_id, agent_id=agent_id, action="reentry_successive_run_failed",
                message=f"续跑连续失败，进入人工兜底前阻塞: {error[:200]}",
                metadata_={"error": error[:500], "stage": "successive_run_failed"},
            ))
        await session.commit()
        logger.warning(f"Task {task.ref_id or task_id} escalated to HUMAN - blocked")

        await _archive_safe(
            session, project_id=task.project_id, iteration_id=task.iteration_id,
            iteration_seq=seq, task_id=task_id, ref_id=task.ref_id or "",
            doc_type="escalation_record",
            title=f"[{task.ref_id}] 升级: 架构师 → 人类介入",
            content=f"# [{task.ref_id}] {task.title}\n\n"
                    f"## 升级路径\n工程师 → 顶级模型 → 架构师 ({max_retries}次重试) → **人类介入**\n\n"
                    f"## 失败原因\n{error[:2000]}\n\n"
                    f"## 升级历史\n" +
                    "\n".join(f"- {h.get('timestamp','')}: L{h.get('from_level')}→L{h.get('to_level')}" for h in history) + "\n",
            tags=["escalation", task.ref_id or "", "human", "blocked"],
        )

        return {"status": "blocked", "escalation_level": 3, "escalated_to": "human", "needs_attention": True}

    else:
        session.add(task_lifecycle.on_terminal(
            task,
            status="failed",
            actor=agent_id,
            reason="all escalation paths exhausted",
        ))
        task.result = error

        session.add(TaskLog(
            task_id=task_id, agent_id=agent_id, action="failed",
            message=f"所有升级路径耗尽: {error[:200]}",
            metadata_=log_meta,
        ))
        await session.commit()
        return {"status": "failed", "escalation_level": level, "needs_attention": True}


async def _check_all_tasks_done(session: AsyncSession, project_id: str) -> bool:
    """检查项目所有非模块级编码任务是否全部 done"""
    from sqlalchemy import func as sa_func
    total_q = await session.execute(
        select(sa_func.count()).where(
            Task.project_id == project_id,
            Task.parent_task_id != None,  # noqa: E711
        )
    )
    total = total_q.scalar() or 0
    if total == 0:
        return False

    done_q = await session.execute(
        select(sa_func.count()).where(
            Task.project_id == project_id,
            Task.parent_task_id != None,  # noqa: E711
            Task.status == "done",
        )
    )
    done = done_q.scalar() or 0
    return done >= total


async def _on_all_tasks_done(session: AsyncSession, project_id: str):
    """所有编码任务完成后：生成评估并自动收口项目状态。"""
    project = await session.get(Project, project_id)
    if not project:
        return

    # 收集所有已完成任务的摘要
    tasks_q = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.parent_task_id != None,  # noqa: E711
            Task.status == "done",
        ).order_by(Task.created_at)
    )
    tasks = list(tasks_q.scalars())

    task_summaries = []
    for t in tasks:
        summary = f"- [{t.ref_id}] {t.title} ({t.suggested_role})"
        if t.result:
            summary += f": {t.result[:100]}"
        task_summaries.append(summary)

    prompt = f"""项目「{project.name}」的所有 {len(tasks)} 个编码任务已全部完成并通过代码审核。

## 已完成任务清单
{chr(10).join(task_summaries)}

请作为架构师进行集成测试就绪评估，输出：

1. **集成风险点**：哪些模块之间的集成可能有问题
2. **集成测试计划**：需要测试的关键集成场景（按优先级排列）
3. **回归测试建议**：需要重点回归的功能
4. **是否建议进入集成测试阶段**：给出明确的 Yes/No 和理由"""

    token_tracker.set_context(project_id=project_id)
    report = await ai_leader._call(
        "你是项目架构师，负责评估项目是否可以从编码阶段进入集成测试阶段。",
        prompt, max_tokens=4096,
    )

    seq = await _iter_seq(session, project.current_iteration_id)
    await _archive_safe(
        session, project_id=project_id, iteration_id=project.current_iteration_id,
        iteration_seq=seq, doc_type="integration_assessment",
        title="集成测试就绪评估",
        content=f"# 集成测试就绪评估\n\n{report}",
        tags=["integration", "assessment", "architect"],
    )

    session.add(TaskLog(
        task_id=tasks[-1].id if tasks else "",
        agent_id="ai:architect",
        action="integration_ready",
        message=f"所有 {len(tasks)} 个编码任务已完成，集成测试评估报告已生成",
    ))
    # 业务规则：所有任务完成 → 确保 workspace → 合并到生产分支 → 自动创建部署任务
    _, production = _git_branch_names(project)
    merge_ok = False
    if project.git_repo:
        try:
            # 架构师确保 workspace 存在
            repo_path = await git_repo.ensure_repo(project.id, project.git_repo)
            integration, _ = _git_branch_names(project)
            # 确保 production 分支存在（首次运行可能只有 develop）
            if not production:
                production = integration
            code, _ = await git_repo._run(f"git rev-parse --verify {production}", cwd=repo_path)
            if code != 0:
                # 生产分支不存在，从 integration 创建
                await git_repo._run(f"git checkout -b {production} origin/{integration}", cwd=repo_path)
                await git_repo._run(f"git push origin {production}", cwd=repo_path)
            merge_result = await git_repo.merge_branch(project.id, integration, target=production)
            merge_ok = merge_result.get("ok", False)
            logger.info(f"All tasks done: merge {integration}→{production} {'OK' if merge_ok else 'FAILED'}: {merge_result.get('error', '')}")
        except Exception as e:
            logger.warning(f"Production merge failed (non-blocking): {e}")

    # 创建部署任务（devops 执行）
    deploy_task_id = ""
    if merge_ok or not project.git_repo:
        try:
            deploy_task = Task(
                id=new_id(),
                project_id=project_id,
                iteration_id=project.current_iteration_id,
                title=f"部署项目「{project.name}」到生产环境",
                description=f"所有编码任务已完成并通过审核。\n请执行生产环境部署。\n\n项目：{project.name}\n分支：{production}",
                type="deploy",
                status="pending",
                complexity="medium",
                suggested_role="devops",
                estimated_hours=2.0,
            )
            session.add(deploy_task)
            await session.flush()
            deploy_task_id = deploy_task.id
            logger.info(f"Created deploy task {deploy_task.id} for devops")
        except Exception as e:
            logger.warning(f"Failed to create deploy task: {e}")

    # ── 架构师开发总结 ──
    try:
        # 统计各状态任务
        done_q = await session.execute(
            select(sa_func.count()).where(Task.project_id == project_id, Task.status == "done")
        )
        failed_q = await session.execute(
            select(sa_func.count()).where(Task.project_id == project_id, Task.status == "failed")
        )
        blocked_q = await session.execute(
            select(sa_func.count()).where(Task.project_id == project_id, Task.status == "blocked")
        )
        done_n = done_q.scalar() or 0
        failed_n = failed_q.scalar() or 0
        blocked_n = blocked_q.scalar() or 0

        summary_prompt = f"""项目「{project.name}」的所有 {len(tasks)} 个编码任务已全部完成并通过代码审核。

## 任务统计
- 完成: {done_n} 个
- 失败: {failed_n} 个
- 阻塞: {blocked_n} 个

## 已完成任务清单
{chr(10).join(task_summaries)}

请作为架构师撰写一份**项目开发总结报告**，包含以下章节：

1. **项目概述**：项目的目标、范围、技术栈
2. **开发过程回顾**：关键里程碑、迭代过程
3. **架构决策**：重要的技术选型和架构设计决策
4. **挑战与解决方案**：开发过程中遇到的主要问题及解决方式
5. **团队表现**：各角色的贡献与协作情况
6. **交付成果**：代码仓库、部署地址、文档清单
7. **经验教训**：可复用的经验和改进建议
8. **后续建议**：维护、优化、扩展方向

请用专业、详实的语言撰写，每个章节不少于3段。"""
        token_tracker.set_context(project_id=project_id)
        dev_summary = await ai_leader._call(
            "你是项目架构师，负责撰写项目开发总结报告。",
            summary_prompt, max_tokens=8192,
        )
        seq = await _iter_seq(session, project.current_iteration_id)
        await _archive_safe(
            session, project_id=project_id, iteration_id=project.current_iteration_id,
            iteration_seq=seq, doc_type="development_summary",
            title=f"项目开发总结 - {project.name}",
            content=f"# 项目开发总结：{project.name}\n\n{dev_summary}",
            tags=["summary", "development", "architect", "final"],
        )
        logger.info(f"Project {project_id}: development summary generated")
    except Exception as e:
        logger.warning(f"Development summary generation failed (non-blocking): {e}")

    if project.status not in ("completed", "terminated", "archived"):
        project.status = "completed"
        session.add(TaskLog(
            task_id=tasks[-1].id if tasks else "",
            agent_id="system",
            action="project_completed",
            message="所有执行任务已完成，项目自动标记为 completed",
            metadata_={"merge_production": merge_ok, "deploy_task_id": deploy_task_id},
        ))
    await session.commit()
    logger.info(f"Project {project_id}: all tasks done, merge={merge_ok}, deploy_task={deploy_task_id}")
