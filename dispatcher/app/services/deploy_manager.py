import logging
import os
import secrets
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models import Agent, AgentRehydrationJob, InfraGroup, InfraNode, Project
from app.services import claw_deployer, deployment_recovery, model_pool, project_git_auth
from app.services.infra import agent_service_name

logger = logging.getLogger(__name__)

ROLE_SUPERVISOR = {
    "architect": "leader",
    "senior": "architect",
    "mid": "architect",
    "junior": "architect",
    "devops": "architect",
    "tester": "leader",
}

BASE_PORT = 18789
INHERITABLE_CONFIG_KEYS = (
    "context_versions",
    "retriever_ready",
    "local_checkpoint",
    "global_knowledge_version",
    "global_knowledge_revision",
)
DISPATCHER_SHARED_SSH_KEY = "/root/.ssh/id_ed25519"


def _metric_score(node: InfraNode) -> float:
    metrics = node.last_metrics or {}
    cpu = float(metrics.get("cpu_percent", 0))
    mem = float(metrics.get("mem_percent", 0))
    return cpu * 0.5 + mem * 0.5


def _is_hot_node(node: InfraNode, cpu_threshold: float = 80, mem_threshold: float = 80) -> bool:
    metrics = node.last_metrics or {}
    cpu = float(metrics.get("cpu_percent", 0))
    mem = float(metrics.get("mem_percent", 0))
    return cpu > cpu_threshold or mem > mem_threshold


async def _project_node_agent_counts(session: AsyncSession, project_id: str) -> dict[str, int]:
    result = await session.execute(select(Agent).where(Agent.project_id == project_id))
    counts: dict[str, int] = {}
    for agent in result.scalars():
        node_id = str((agent.config or {}).get("node_id") or "")
        if not node_id:
            continue
        counts[node_id] = counts.get(node_id, 0) + 1
    return counts


def _build_inherit_snapshot(source_cfg: dict) -> dict:
    snapshot: dict[str, Any] = {}
    for key in INHERITABLE_CONFIG_KEYS:
        val = source_cfg.get(key)
        if val is None:
            continue
        snapshot[key] = val
    return snapshot


def _resolve_node_ssh_key(node: InfraNode) -> str:
    # 严格模式：仅允许 dispatcher 与宿主机共享的 root 私钥。
    if os.path.exists(DISPATCHER_SHARED_SSH_KEY):
        return DISPATCHER_SHARED_SSH_KEY
    return ""


async def _resolve_predecessor(
    session: AsyncSession,
    project_id: str,
    role: str,
    agent_id: str,
    relation_mode: str,
) -> tuple[str, dict]:
    q = await session.execute(
        select(Agent).where(
            Agent.project_id == project_id,
            Agent.role == role,
            Agent.id != agent_id,
        )
    )
    same_role_agents = list(q.scalars())
    predecessor = ""
    if same_role_agents and relation_mode in ("successor", "auto"):
        terminal = {"offline", "dead", "start_failed", "abandoned"}
        candidates = [
            a for a in same_role_agents
            if (a.last_heartbeat_status or "offline") in terminal and not a.current_task_id
        ]
        if candidates:
            candidates.sort(key=lambda a: (a.created_at.timestamp() if a.created_at else 0), reverse=True)
            predecessor = candidates[0].id

    predecessor_cfg = {}
    if predecessor:
        prev = next((a for a in same_role_agents if a.id == predecessor), None)
        predecessor_cfg = dict(prev.config or {}) if prev else {}
    return predecessor, predecessor_cfg


async def get_infra_node(
    session: AsyncSession,
    project_id: str,
    role: str = "agent",
    policy: str | None = None,
) -> InfraNode | None:
    project = await session.get(Project, project_id)
    if not project or not project.infra_group_id:
        return None
    scheduler_policy = (policy or (project.config or {}).get("scheduler_policy") or "balanced").strip().lower()
    grp = await session.get(InfraGroup, project.infra_group_id, options=[
        selectinload(InfraGroup.nodes),
        selectinload(InfraGroup.node_assocs),
    ])
    if not grp or not grp.nodes:
        return None

    assoc_roles = {a.node_id: [r.upper() for r in (a.roles or ["AGENT"])] for a in grp.node_assocs}
    # 调度硬前提：节点必须在线（连通测试通过）
    available = [n for n in grp.nodes if (n.status or "").lower() == "connected"]
    if not available:
        return None

    role_upper = role.upper()
    candidates = [n for n in available if role_upper in assoc_roles.get(n.id, ["AGENT"])] or available
    if len(candidates) == 1:
        return candidates[0]

    cool_nodes = [n for n in candidates if not _is_hot_node(n)]
    node_pool = cool_nodes or candidates

    if scheduler_policy == "spread":
        counts = await _project_node_agent_counts(session, project_id)
        return min(node_pool, key=lambda n: (counts.get(n.id, 0), _metric_score(n)))

    if scheduler_policy == "binpack":
        counts = await _project_node_agent_counts(session, project_id)
        return max(node_pool, key=lambda n: (counts.get(n.id, 0), -_metric_score(n)))

    # balanced: 默认按资源评分选择最优
    return min(node_pool, key=_metric_score)


async def generate_agent_deploy(
    session: AsyncSession,
    *,
    project_id: str,
    role: str,
    model_provider: str = "147api",
    model_id: str = "",
    port: int = 0,
    api_key: str = "",
    api_base: str = "https://api.147api.com",
    proxy_env: dict | None = None,
    allow_push_failure: bool = False,
    agent_id_override: str = "",
) -> dict[str, Any]:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    scheduler_policy = ((project.config or {}).get("scheduler_policy") or "balanced").strip().lower()

    project_cfg = dict(project.config or {})
    resolved_model = model_id or (project.role_model_map or {}).get(role, model_pool.ROLE_MODEL_MAP.get(role, "deepseek-chat"))
    gateway_token = secrets.token_hex(24)
    agent_id = (agent_id_override or "").strip() or f"{role}-{project_id}"
    supervisor_id = ROLE_SUPERVISOR.get(role, "")
    if supervisor_id == "architect":
        supervisor_id = f"architect-{project_id}"
    relation_mode = str(project_cfg.get("same_role_relation_mode") or "auto").strip().lower()
    if relation_mode not in ("auto", "peer", "successor"):
        relation_mode = "auto"
    enable_successor_inherit = bool(project_cfg.get("enable_successor_inherit", True))

    node = await get_infra_node(session, project_id, role=role, policy=scheduler_policy)
    if project.infra_group_id and not node:
        raise HTTPException(409, "当前环境组无在线可用节点，请先完成节点连通后重试")
    host_ip = node.host if node else "127.0.0.1"

    resolved_port = int(port)
    if resolved_port <= 0:
        existing = await session.get(Agent, agent_id)
        existing_port = (existing.config or {}).get("port") if existing else None
        if existing_port:
            resolved_port = int(existing_port)
        else:
            used_ports = set()
            for a in (await session.execute(select(Agent))).scalars():
                p = (a.config or {}).get("port")
                if p:
                    used_ports.add(int(p))
            resolved_port = BASE_PORT
            while resolved_port in used_ports:
                resolved_port += 1

    allowed_origin = f"http://{host_ip}:{resolved_port}"
    openclaw_config = claw_deployer.generate_openclaw_config(
        role=role,
        project_id=project_id,
        model_provider=model_provider,
        model_id=resolved_model,
        gateway_token=gateway_token,
        api_base_url=api_base,
        api_key=api_key,
        allowed_origin=allowed_origin,
    )
    compose_content = claw_deployer.generate_docker_compose(
        role=role,
        project_id=project_id,
        gateway_token=gateway_token,
        agent_id=agent_id,
        port=resolved_port,
        host_ip=host_ip,
        model=resolved_model,
        supervisor_id=supervisor_id,
        proxy_env=proxy_env,
        git_repo=project.git_repo or "",
    )
    env_content = claw_deployer.generate_env_file(api_key, api_base)

    project_tech = project.target_tech_stack or (project.config or {}).get("tech_stack")
    project_git_key = project_git_auth.get_private_key(project)
    if not project_git_key:
        project_git_auth.generate_ssh_keypair(project, updated_by="system")
        await session.commit()
        await session.refresh(project, ["config"])
        project_git_key = project_git_auth.get_private_key(project)

    deploy_dir = claw_deployer.prepare_deploy_dir(
        deploy_root=settings.PROJECTS_DIR,
        project_id=project_id,
        sub_dir=agent_id,
        role=role,
        openclaw_config=openclaw_config,
        compose_content=compose_content,
        env_content=env_content,
        tech_stack=project_tech,
        git_private_key=project_git_key,
    )

    pushed_to = None
    push_warning = None
    if node:
        key_file = _resolve_node_ssh_key(node)
        if not key_file:
            msg = (
                f"推送到远程节点失败: SSH 私钥不可用（node={node.id}，configured={node.ssh_key_path or '<empty>'}）"
            )
            logger.error("Deploy push skipped for %s: %s", agent_id, msg)
            if allow_push_failure:
                push_warning = msg
            else:
                raise HTTPException(500, msg)

    if node and not push_warning:
        remote_dir = f"{settings.AGENT_DEPLOY_ROOT}/{project_id}/{agent_id}"
        result = await claw_deployer.push_deploy_to_node(
            local_dir=deploy_dir,
            host=node.host,
            remote_dir=remote_dir,
            user=node.user,
            port=node.port,
            key_file=key_file,
        )
        if result["exit_code"] == 0:
            pushed_to = f"{node.user}@{node.host}:{remote_dir}"
        else:
            logger.error("Deploy push failed for %s: %s", agent_id, result["stderr"])
            if allow_push_failure:
                push_warning = f"推送到远程节点失败: {result['stderr']}"
            else:
                raise HTTPException(500, f"推送到远程节点失败: {result['stderr']}")

    svc = agent_service_name(project_id, role)
    webhook_url = f"http://{host_ip}:{resolved_port}"
    labels = {
        "project_id": project_id,
        "role": role,
        "team": "default",
        "namespace": f"project-{project_id}",
    }
    deploy_meta = {
        "gateway_token": gateway_token,
        "port": resolved_port,
        "service_name": svc,
        "scheduler_policy": scheduler_policy,
        "node_id": node.id if node else "",
        "labels": labels,
    }
    predecessor_id, predecessor_cfg = await _resolve_predecessor(
        session, project_id, role, agent_id, relation_mode
    )
    successor_snapshot = (
        _build_inherit_snapshot(predecessor_cfg)
        if enable_successor_inherit and predecessor_id
        else {}
    )
    is_successor = bool(predecessor_id and relation_mode in ("auto", "successor"))
    relation_meta = {
        "mode": relation_mode,
        "is_successor": is_successor,
        "predecessor_agent_id": predecessor_id,
    }
    agent = await session.get(Agent, agent_id)
    if agent:
        agent.model = resolved_model
        agent.workspace_path = f"{deploy_dir}/.openclaw/workspace/{project_id}"
        agent.webhook_url = webhook_url
        merged_labels = {**((agent.config or {}).get("labels") or {}), **labels}
        base_cfg = dict(agent.config or {})
        agent.config = {
            **base_cfg,
            **deploy_meta,
            "labels": merged_labels,
            "role_relationship": relation_meta,
        }
        if successor_snapshot and is_successor:
            agent.config["successor_inherit"] = {
                "from_agent_id": predecessor_id,
                "snapshot": successor_snapshot,
            }
        else:
            agent.config["successor_inherit"] = {}
        deployment_recovery.apply_remote_push_result(agent, push_warning)
        agent.status = "idle"
        agent.current_task_id = None
        agent.last_heartbeat_status = "offline"
    else:
        agent = Agent(
            id=agent_id,
            project_id=project_id,
            role=role,
            model=resolved_model,
            status="idle",
            workspace_path=f"{deploy_dir}/.openclaw/workspace/{project_id}",
            webhook_url=webhook_url,
            supervisor_id=supervisor_id or None,
            config={
                **deploy_meta,
                "role_relationship": relation_meta,
                "successor_inherit": (
                    {"from_agent_id": predecessor_id, "snapshot": successor_snapshot}
                    if successor_snapshot and is_successor
                    else {}
                ),
            },
        )
        deployment_recovery.apply_remote_push_result(agent, push_warning)
        session.add(agent)
        if successor_snapshot and is_successor:
            session.add(AgentRehydrationJob(
                agent_id=agent.id,
                project_id=project_id,
                mode="partial_rehydrate",
                reason=f"successor inherit from {predecessor_id}",
                snapshot=successor_snapshot,
                status="pending",
            ))

    await session.commit()
    return {
        "agent_id": agent_id,
        "deploy_dir": deploy_dir,
        "gateway_token": gateway_token,
        "compose_file": compose_content,
        "openclaw_config": openclaw_config,
        "pushed_to": pushed_to,
        "push_warning": push_warning,
    }
