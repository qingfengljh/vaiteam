"""
基础设施节点管理

CRUD + 连通性测试 + SSH 免密配置 + WebSocket Terminal
支持四种节点类型：vm (SSH)、docker、kubernetes、gitlab
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import shlex
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.demo_host import request_host_is_demo
from app.core.database import get_session
from app.models import InfraNode, InfraGroup, InfraGroupNode, SystemConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/infra", tags=["infrastructure"])
DISPATCHER_SHARED_SSH_KEY = "/root/.ssh/id_ed25519"
SHARED_SSH_KEY_MISSING_ERROR = "dispatcher 共享私钥不存在：/root/.ssh/id_ed25519"
DEFAULT_SSH_USER = "root"
PLATFORM_GROUP_PURPOSE = "platform"

TERMINAL_GATE_CONFIG_KEY = "infra_terminal_gate"
TERMINAL_GATE_PBKDF2_ITERS = 310_000
TERMINAL_GATE_MAX_ATTEMPTS = 5


def _terminal_gate_doc(cfg_row: SystemConfig | None) -> dict:
    if not cfg_row or not isinstance(cfg_row.value, dict):
        return {}
    return cfg_row.value


def _terminal_gate_configured(doc: dict) -> bool:
    return bool(doc.get("salt")) and bool(doc.get("hash"))


def _terminal_gate_enabled(doc: dict) -> bool:
    return _terminal_gate_configured(doc) and bool(doc.get("enabled", True))


def _pbkdf2_hash(password: str, salt_hex: str, iterations: int) -> str:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return dk.hex()


def _verify_terminal_gate_password(doc: dict, password: str) -> bool:
    if not _terminal_gate_configured(doc):
        return False
    iters = int(doc.get("iterations") or TERMINAL_GATE_PBKDF2_ITERS)
    expect = (doc.get("hash") or "").strip().lower()
    got = _pbkdf2_hash(password, doc["salt"], iters).lower()
    return len(expect) == len(got) and hmac.compare_digest(expect, got)


async def _load_terminal_gate(session: AsyncSession) -> dict:
    row = await session.get(SystemConfig, TERMINAL_GATE_CONFIG_KEY)
    return _terminal_gate_doc(row)


def _is_platform_managed_node(n: InfraNode) -> bool:
    return bool(isinstance(n.config, dict) and n.config.get("platform_managed"))


def _is_platform_group(g: InfraGroup) -> bool:
    return (g.purpose or "").strip() == PLATFORM_GROUP_PURPOSE


def _shared_ssh_key_path() -> str:
    if os.path.exists(DISPATCHER_SHARED_SSH_KEY):
        return DISPATCHER_SHARED_SSH_KEY
    fallback = "/app/.ssh/id_ed25519"
    if os.path.exists(fallback):
        return fallback
    return ""


def _coerce_ssh_user(user: str | None) -> str:
    u = (user or "").strip()
    return u or DEFAULT_SSH_USER


def _ssh_username(node: InfraNode) -> str:
    return _coerce_ssh_user(node.user)


def _normalize_ssh_error(error: str) -> str:
    if "Permission denied" in error:
        return "SSH 认证失败：目标机未授权 dispatcher 公钥，或该用户禁止密钥/密码登录"
    return error


# ── Pydantic models ──

class NodeCreate(BaseModel):
    name: str
    type: str = "linux"
    host: str
    port: int = 22
    user: str = "root"
    config: dict = {}


class NodeUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    config: dict | None = None


def _node_dict(n: InfraNode) -> dict:
    pm = _is_platform_managed_node(n)
    return {
        "id": n.id, "name": n.name, "type": n.type,
        "host": n.host, "port": n.port, "user": n.user,
        "auth_method": n.auth_method, "status": n.status,
        "roles": n.roles or ["AGENT"],
        "group_ids": [g.id for g in n.groups],
        "config": n.config,
        "platform_managed": pm,
        "last_connected": n.last_connected.isoformat() if n.last_connected else None,
        "created_at": n.created_at.isoformat(),
    }


# ── 配置接口 ──

@router.get("/config")
async def infra_config():
    """返回节点类型和角色的配置（前端动态加载）"""
    from app.core.constants import INFRA_NODE_TYPES, INFRA_NODE_ROLES
    return {
        "node_types": INFRA_NODE_TYPES,
        "node_roles": INFRA_NODE_ROLES,
    }


class TerminalGateStatusResp(BaseModel):
    """与登录密码独立；启用后进入节点 Web 终端前须在连接上校验一次。"""

    configured: bool
    enabled: bool


@router.get("/terminal-gate", response_model=TerminalGateStatusResp)
async def get_terminal_gate_status(session: AsyncSession = Depends(get_session)):
    doc = await _load_terminal_gate(session)
    return TerminalGateStatusResp(
        configured=_terminal_gate_configured(doc),
        enabled=_terminal_gate_enabled(doc),
    )


class TerminalGateSetBody(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)
    current_password: str | None = Field(None, max_length=128)


@router.put("/terminal-gate")
async def set_terminal_gate_password(
    request: Request,
    body: TerminalGateSetBody,
    session: AsyncSession = Depends(get_session),
):
    if request_host_is_demo(request):
        raise HTTPException(403, "演示环境不允许修改或设置终端操作密码")
    doc = await _load_terminal_gate(session)
    if _terminal_gate_configured(doc):
        if not body.current_password or not _verify_terminal_gate_password(doc, body.current_password):
            raise HTTPException(status_code=400, detail="当前终端操作密码错误")
    salt = secrets.token_hex(16)
    h = _pbkdf2_hash(body.new_password, salt, TERMINAL_GATE_PBKDF2_ITERS)
    new_doc = {
        "enabled": True,
        "salt": salt,
        "hash": h,
        "iterations": TERMINAL_GATE_PBKDF2_ITERS,
    }
    row = await session.get(SystemConfig, TERMINAL_GATE_CONFIG_KEY)
    if row:
        row.value = new_doc
    else:
        session.add(SystemConfig(key=TERMINAL_GATE_CONFIG_KEY, value=new_doc))
    await session.commit()
    return {"ok": True}


class TerminalGateEnabledBody(BaseModel):
    enabled: bool


@router.patch("/terminal-gate/enabled")
async def patch_terminal_gate_enabled(
    request: Request,
    body: TerminalGateEnabledBody,
    session: AsyncSession = Depends(get_session),
):
    if request_host_is_demo(request):
        raise HTTPException(403, "演示环境不允许开关终端操作密码校验")
    doc = await _load_terminal_gate(session)
    if not _terminal_gate_configured(doc):
        raise HTTPException(status_code=400, detail="请先设置终端操作密码")
    row = await session.get(SystemConfig, TERMINAL_GATE_CONFIG_KEY)
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    d = dict(row.value)
    d["enabled"] = bool(body.enabled)
    row.value = d
    await session.commit()
    return {"ok": True}


class TerminalGateClearBody(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)


@router.post("/terminal-gate/clear")
async def clear_terminal_gate(
    request: Request,
    body: TerminalGateClearBody,
    session: AsyncSession = Depends(get_session),
):
    if request_host_is_demo(request):
        raise HTTPException(403, "演示环境不允许清除终端操作密码")
    doc = await _load_terminal_gate(session)
    if not _verify_terminal_gate_password(doc, body.current_password):
        raise HTTPException(status_code=400, detail="终端操作密码错误")
    row = await session.get(SystemConfig, TERMINAL_GATE_CONFIG_KEY)
    if row:
        await session.delete(row)
    await session.commit()
    return {"ok": True}


# ── CRUD ──

@router.get("/nodes")
async def list_nodes(
    role: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(InfraNode).options(selectinload(InfraNode.groups)).order_by(InfraNode.created_at)
    result = await session.execute(q)
    nodes = [n for n in result.scalars() if n.type != "service"]
    if role:
        role_upper = role.upper()
        nodes = [n for n in nodes if role_upper in [r.upper() for r in (n.roles or [])]]
    return [_node_dict(n) for n in nodes]


@router.post("/nodes")
async def create_node(body: NodeCreate, session: AsyncSession = Depends(get_session)):
    cfg = dict(body.config or {})
    cfg.pop("platform_managed", None)
    node = InfraNode(
        name=body.name, type=body.type,
        host=body.host, port=body.port, user=_coerce_ssh_user(body.user),
        config=cfg,
    )
    session.add(node)
    await session.commit()
    r = await session.execute(
        select(InfraNode).options(selectinload(InfraNode.groups)).where(InfraNode.id == node.id)
    )
    return _node_dict(r.scalar_one())


@router.put("/nodes/{node_id}")
async def update_node(node_id: str, body: NodeUpdate, session: AsyncSession = Depends(get_session)):
    r = await session.execute(
        select(InfraNode).options(selectinload(InfraNode.groups)).where(InfraNode.id == node_id)
    )
    node = r.scalar_one_or_none()
    if not node:
        raise HTTPException(404)
    was_platform = _is_platform_managed_node(node)
    data = body.model_dump(exclude_unset=True)
    if "user" in data:
        data["user"] = _coerce_ssh_user(data["user"] if isinstance(data.get("user"), str) else None)
    if not was_platform and "config" in data and isinstance(data["config"], dict):
        data["config"].pop("platform_managed", None)
    for k, v in data.items():
        setattr(node, k, v)
    if was_platform:
        c = dict(node.config or {})
        c["platform_managed"] = True
        node.config = c
    await session.commit()
    return _node_dict(node)


@router.delete("/nodes/{node_id}")
async def delete_node(node_id: str, session: AsyncSession = Depends(get_session)):
    node = await session.get(InfraNode, node_id)
    if not node:
        raise HTTPException(404)
    if _is_platform_managed_node(node):
        raise HTTPException(403, "平台分配的节点不可删除，可继续添加自建节点与环境组")
    ref = await session.execute(
        select(InfraGroupNode).where(InfraGroupNode.node_id == node_id).limit(1)
    )
    if ref.scalar_one_or_none():
        raise HTTPException(409, "该节点已被环境组引用，请先从环境组中移出")
    await session.delete(node)
    await session.commit()
    return {"ok": True}


# ── 环境组 CRUD ──

class GroupCreate(BaseModel):
    name: str
    description: str = ""


@router.get("/groups")
async def list_groups(session: AsyncSession = Depends(get_session)):
    q = await session.execute(
        select(InfraGroup)
        .options(
            selectinload(InfraGroup.node_assocs),
            selectinload(InfraGroup.nodes).selectinload(InfraNode.groups),
        )
        .order_by(InfraGroup.created_at)
    )
    groups = []
    for g in q.scalars():
        assoc_map = {a.node_id: (a.roles or ["AGENT"]) for a in g.node_assocs}
        node_list = []
        for n in g.nodes:
            nd = _node_dict(n)
            nd["group_roles"] = assoc_map.get(n.id, n.roles or ["AGENT"])
            node_list.append(nd)
        groups.append({
            "id": g.id, "name": g.name, "description": g.description,
            "purpose": g.purpose,
            "platform_managed": _is_platform_group(g),
            "nodes": node_list,
            "created_at": g.created_at.isoformat(),
        })
    return groups


@router.post("/groups")
async def create_group(body: GroupCreate, session: AsyncSession = Depends(get_session)):
    g = InfraGroup(name=body.name, description=body.description)
    session.add(g)
    await session.commit()
    return {"id": g.id, "name": g.name}


@router.put("/groups/{group_id}")
async def update_group(group_id: str, body: GroupCreate, session: AsyncSession = Depends(get_session)):
    g = await session.get(InfraGroup, group_id)
    if not g:
        raise HTTPException(404)
    g.name = body.name
    g.description = body.description
    await session.commit()
    return {"id": g.id, "name": g.name}


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str, session: AsyncSession = Depends(get_session)):
    g = await session.get(InfraGroup, group_id)
    if not g:
        raise HTTPException(404)
    if _is_platform_group(g):
        raise HTTPException(403, "平台分配的环境组不可删除，可新建其他环境组使用")
    await session.delete(g)
    await session.commit()
    return {"ok": True}


class GroupNodeBody(BaseModel):
    node_id: str
    roles: list[str] = ["AGENT"]


@router.post("/groups/{group_id}/nodes")
async def add_node_to_group(group_id: str, body: GroupNodeBody, session: AsyncSession = Depends(get_session)):
    g = await session.get(InfraGroup, group_id)
    if not g:
        raise HTTPException(404, "环境组不存在")
    node = await session.get(InfraNode, body.node_id)
    if not node:
        raise HTTPException(404, "节点不存在")
    existing = await session.get(InfraGroupNode, (group_id, body.node_id))
    if existing:
        existing.roles = body.roles or ["AGENT"]
    else:
        session.add(InfraGroupNode(group_id=group_id, node_id=body.node_id, roles=body.roles or ["AGENT"]))
    await session.commit()
    return {"ok": True}


@router.put("/groups/{group_id}/nodes/{node_id}/roles")
async def update_node_roles_in_group(group_id: str, node_id: str, body: dict, session: AsyncSession = Depends(get_session)):
    assoc = await session.get(InfraGroupNode, (group_id, node_id))
    if not assoc:
        raise HTTPException(404, "节点不在该环境组中")
    assoc.roles = body.get("roles", ["AGENT"])
    await session.commit()
    return {"ok": True}


@router.delete("/groups/{group_id}/nodes/{node_id}")
async def remove_node_from_group(group_id: str, node_id: str, session: AsyncSession = Depends(get_session)):
    node = await session.get(InfraNode, node_id)
    if node and _is_platform_managed_node(node):
        raise HTTPException(403, "平台分配的节点不可从环境组移出")
    assoc = await session.get(InfraGroupNode, (group_id, node_id))
    if assoc:
        await session.delete(assoc)
        await session.commit()
    return {"ok": True}


# ── 操作提示（各节点类型连接成功后的下一步引导） ──

@router.get("/nodes/{node_id}/setup-guide")
async def get_setup_guide(node_id: str, session: AsyncSession = Depends(get_session)):
    """返回该节点类型的配置引导步骤"""
    node = await session.get(InfraNode, node_id)
    if not node:
        raise HTTPException(404)
    return _build_setup_guide(node)


def _build_setup_guide(node: InfraNode) -> dict:
    steps: list[dict] = []
    auto_actions: list[str] = []

    if node.type in ("linux", "vm", "docker"):
        if node.auth_method != "key":
            steps.append({"title": "配置 SSH 免密", "description": "点击「配免密」按钮，输入一次密码即可永久免密", "action": "setup-key", "auto": True})
        steps.append({"title": "安装 Docker", "description": "Agent 运行在 Docker 容器中", "command": "curl -fsSL https://get.docker.com | sh", "auto": False})
        steps.append({"title": "验证 Docker", "description": "确认 Docker 已安装并运行", "command": "docker info", "auto": False})

    elif node.type == "windows":
        if node.auth_method != "key":
            steps.append({"title": "配置 SSH 免密", "description": "点击「配免密」按钮，输入一次密码即可永久免密", "action": "setup-key", "auto": True})
        steps.append({"title": "确认 SSH 服务", "description": "Windows 需要启用 OpenSSH Server", "auto": False})

    elif node.type == "kubernetes":
        steps.append({"title": "应用 RBAC 配置", "description": "给 Dispatcher 创建 ServiceAccount 和权限", "command": "kubectl apply -f k8s/dispatcher-rbac.yaml", "auto": False})
        steps.append({"title": "验证权限", "description": "确认可以创建 Pod", "command": f"kubectl --kubeconfig {node.config.get('kubeconfig', '~/.kube/config')} auth can-i create pods -n {node.config.get('namespace', 'openclaw-agents')}", "auto": False})

    elif node.type == "gitlab":
        auto_actions.append("setup-webhook")
        steps.append({"title": "配置 Webhook", "description": "让 GitLab 在 push/merge 时通知 Dispatcher", "action": "setup-webhook", "auto": True})
        steps.append({"title": "配置 CI Runner", "description": "需要在 GitLab 中注册 Runner（如果还没有）", "auto": False,
                       "hint": "进入 GitLab → Settings → CI/CD → Runners → 注册新 Runner"})
        steps.append({"title": "创建 Deploy Token", "description": "用于 CI 推送镜像到 Registry", "auto": False,
                       "hint": "进入 GitLab → Settings → Repository → Deploy Tokens → 创建"})

    roles_upper = {r.upper() for r in (node.roles or [])}
    if "OLLAMA" in roles_upper:
        svc_url = node.config.get("service_url", f"http://{node.host}:11434")
        steps.append({"title": "安装 Ollama", "description": "curl -fsSL https://ollama.com/install.sh | sh", "command": "curl -fsSL https://ollama.com/install.sh | sh", "auto": False})
        steps.append({"title": "启动 Ollama", "description": "systemctl enable --now ollama", "command": "systemctl enable --now ollama", "auto": False})
        steps.append({"title": "拉取 Embedding 模型", "description": "ollama pull bge-m3", "command": "ollama pull bge-m3", "auto": False})
        steps.append({"title": "验证 Ollama", "description": f"curl {svc_url}/api/tags", "command": f"curl {svc_url}/api/tags", "auto": False})

    return {"type": node.type, "status": node.status, "steps": steps, "auto_actions": auto_actions}


# ── GitLab Webhook 自动配置 ──

class GitlabWebhookBody(BaseModel):
    project_path: str
    events: list[str] = ["push_events", "merge_requests_events", "pipeline_events"]


@router.post("/nodes/{node_id}/setup-gitlab-webhook")
async def setup_gitlab_webhook(node_id: str, body: GitlabWebhookBody, session: AsyncSession = Depends(get_session)):
    """通过 GitLab API 自动配置 Webhook"""
    node = await session.get(InfraNode, node_id)
    if not node or node.type != "gitlab":
        raise HTTPException(400, "仅 GitLab 节点支持此操作")

    token = node.config.get("token", "")
    if not token:
        return {"ok": False, "error": "未配置 GitLab Token"}

    import httpx
    from app.core.config import settings

    gitlab_url = node.host.rstrip("/")
    webhook_url = f"{settings.OPENCLAW_GATEWAY_URL.replace('localhost', node.host.split('//')[1].split('/')[0] if '//' in node.host else 'localhost')}/api/webhook/ci"
    # 实际应该用 Dispatcher 的外部可达地址
    dispatcher_url = node.config.get("dispatcher_callback_url", webhook_url)

    project_path_encoded = body.project_path.replace("/", "%2F")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 检查是否已有 webhook
            resp = await client.get(
                f"{gitlab_url}/api/v4/projects/{project_path_encoded}/hooks",
                headers={"PRIVATE-TOKEN": token},
            )
            existing = resp.json() if resp.status_code == 200 else []
            for hook in existing:
                if dispatcher_url in hook.get("url", ""):
                    return {"ok": True, "message": "Webhook 已存在", "hook_id": hook["id"], "url": hook["url"]}

            # 创建 webhook
            hook_data = {"url": dispatcher_url, "token": settings.OPENCLAW_HOOK_TOKEN}
            for event in body.events:
                hook_data[event] = True

            resp = await client.post(
                f"{gitlab_url}/api/v4/projects/{project_path_encoded}/hooks",
                headers={"PRIVATE-TOKEN": token},
                json=hook_data,
            )
            if resp.status_code in (200, 201):
                hook = resp.json()
                return {"ok": True, "message": "Webhook 配置成功", "hook_id": hook.get("id"), "url": dispatcher_url}
            return {"ok": False, "error": f"GitLab API 返回 {resp.status_code}: {resp.text[:300]}"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 按角色查找节点（通用） ──

def _find_nodes_by_role(nodes: list[InfraNode], role_key: str) -> list[InfraNode]:
    role_upper = role_key.upper()
    return [n for n in nodes if role_upper in {r.upper() for r in (n.roles or [])}]


def _get_service_url(node: InfraNode, role_key: str) -> str:
    """获取服务类节点的 URL。优先用 config.service_url，否则按角色默认端口拼接"""
    from app.core.constants import INFRA_ROLE_HEALTH
    url = node.config.get("service_url")
    if url:
        return url.rstrip("/")
    health_cfg = INFRA_ROLE_HEALTH.get(role_key.upper(), {})
    port = health_cfg.get("default_port", 80)
    return f"http://{node.host}:{port}"


async def _test_service_health(service_url: str, health_path: str = "/") -> dict:
    """通用 HTTP 健康检查"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{service_url}{health_path}")
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json() if "json" in resp.headers.get("content-type", "") else resp.text[:200]}
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 服务角色状态（按角色 key 查询） ──

@router.get("/roles/{role_key}/status")
async def role_status(role_key: str, session: AsyncSession = Depends(get_session)):
    """按角色 key 查询所有该角色节点的服务状态"""
    from app.core.constants import INFRA_ROLE_HEALTH
    q = await session.execute(select(InfraNode).options(selectinload(InfraNode.groups)))
    nodes = _find_nodes_by_role(list(q.scalars()), role_key)

    if not nodes:
        return {"role": role_key, "configured": False, "nodes": []}

    health_cfg = INFRA_ROLE_HEALTH.get(role_key.upper(), {})
    health_path = health_cfg.get("health_path", "/")
    results = []
    for node in nodes:
        svc_url = _get_service_url(node, role_key)
        status = await _test_service_health(svc_url, health_path)
        entry = {"node_id": node.id, "node_name": node.name, "host": node.host, "service_url": svc_url, **status}
        if role_key.upper() == "OLLAMA" and status.get("ok"):
            data = status.get("data", {})
            if isinstance(data, dict):
                entry["models"] = [m.get("name", "") for m in data.get("models", [])]
        results.append(entry)
    return {"role": role_key, "configured": True, "nodes": results}


@router.post("/roles/{role_key}/apply")
async def apply_role_config(role_key: str, session: AsyncSession = Depends(get_session)):
    """将第一个可用的该角色节点配置到运行时"""
    from app.core.constants import INFRA_ROLE_HEALTH
    q = await session.execute(select(InfraNode))
    nodes = _find_nodes_by_role(list(q.scalars()), role_key)

    health_cfg = INFRA_ROLE_HEALTH.get(role_key.upper(), {})
    health_path = health_cfg.get("health_path", "/")

    for node in nodes:
        svc_url = _get_service_url(node, role_key)
        status = await _test_service_health(svc_url, health_path)
        if not status.get("ok"):
            continue
        if role_key.upper() == "OLLAMA":
            from app.core.config import settings
            settings.OLLAMA_BASE_URL = svc_url
            settings.OLLAMA_ENABLED = True
            data = status.get("data", {})
            models = [m.get("name", "") for m in data.get("models", [])] if isinstance(data, dict) else []
            return {"ok": True, "service_url": svc_url, "models": models}
        return {"ok": True, "service_url": svc_url}
    return {"ok": False, "error": f"没有可用的 {role_key} 节点"}


# ── 连通性测试 ──

class TestConnBody(BaseModel):
    password: str | None = None


@router.post("/nodes/{node_id}/test")
async def test_connection(node_id: str, body: TestConnBody | None = None, session: AsyncSession = Depends(get_session)):
    """测试节点连通性"""
    node = await session.get(InfraNode, node_id)
    if not node:
        raise HTTPException(404)

    password = body.password if body else None

    if node.type == "service":
        result = await _test_service_node(node)
    elif node.type in ("linux", "windows", "vm", "docker"):
        result = await _test_ssh(node, password)
    elif node.type == "kubernetes":
        result = await _test_k8s(node)
    elif node.type == "gitlab":
        result = await _test_gitlab(node)
    else:
        result = {"ok": False, "error": f"Unknown type: {node.type}"}

    if result.get("ok"):
        node.status = "connected"
        node.last_connected = datetime.now(timezone.utc)
    else:
        node.status = "error"
    await session.commit()

    return {**result, "status": node.status}


async def _test_service_node(node: InfraNode) -> dict:
    """测试服务类节点——遍历其角色，按健康检查配置逐个测试"""
    from app.core.constants import INFRA_ROLE_HEALTH
    roles_upper = {r.upper() for r in (node.roles or [])}
    results = {}
    for role_key in roles_upper:
        health_cfg = INFRA_ROLE_HEALTH.get(role_key)
        if not health_cfg:
            continue
        svc_url = _get_service_url(node, role_key)
        health_path = health_cfg.get("health_path", "/")
        status = await _test_service_health(svc_url, health_path)
        results[role_key] = {**status, "service_url": svc_url}
    if not results:
        svc_url = node.config.get("service_url", f"http://{node.host}")
        status = await _test_service_health(svc_url)
        results["default"] = {**status, "service_url": svc_url}
    all_ok = any(r.get("ok") for r in results.values())
    return {"ok": all_ok, "services": results}


async def _test_ssh(node: InfraNode, password: str | None = None) -> dict:
    try:
        import asyncssh
        username = _ssh_username(node)
        pw = (password or "").strip() or None
        conn_kw: dict = {
            "host": node.host,
            "port": node.port,
            "username": username,
            "known_hosts": None,
        }
        if pw:
            async with asyncssh.connect(**conn_kw, password=pw) as conn:
                result = await conn.run("hostname && uname -a", check=True)
                return {"ok": True, "output": result.stdout.strip()}
        key_path = _shared_ssh_key_path()
        if not key_path:
            return {"ok": False, "error": SHARED_SSH_KEY_MISSING_ERROR}
        async with asyncssh.connect(**conn_kw, client_keys=[key_path]) as conn:
            result = await conn.run("hostname && uname -a", check=True)
            return {"ok": True, "output": result.stdout.strip()}
    except Exception as e:
        return {"ok": False, "error": _normalize_ssh_error(str(e))}


async def _test_docker(node: InfraNode) -> dict:
    host = node.config.get("docker_host", "unix:///var/run/docker.sock")
    if host.startswith("unix://"):
        cmd = ["docker", "info", "--format", "{{.ServerVersion}}"]
    else:
        cmd = ["docker", "-H", host, "info", "--format", "{{.ServerVersion}}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return {"ok": True, "output": f"Docker {stdout.decode().strip()}"}
        return {"ok": False, "error": stderr.decode().strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _test_k8s(node: InfraNode) -> dict:
    kubeconfig = node.config.get("kubeconfig", "~/.kube/config")
    namespace = node.config.get("namespace", "default")
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "--kubeconfig", kubeconfig, "-n", namespace,
            "cluster-info",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            first_line = stdout.decode().strip().split("\n")[0]
            return {"ok": True, "output": first_line}
        return {"ok": False, "error": stderr.decode().strip()[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _test_gitlab(node: InfraNode) -> dict:
    import httpx
    url = node.host.rstrip("/")
    token = node.config.get("token", "")
    if not token:
        return {"ok": False, "error": "需要配置 GitLab Token (config.token)"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url}/api/v4/version",
                headers={"PRIVATE-TOKEN": token},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "output": f"GitLab {data.get('version', '?')} ({data.get('revision', '?')})"}
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── SSH 免密配置 ──

class SetupKeyBody(BaseModel):
    password: str


async def _do_setup_ssh_key(node: InfraNode, _password: str, session: AsyncSession) -> tuple[bool, str]:
    """使用目标机密码写入 dispatcher 共享公钥，并验证免密连通。"""
    try:
        import asyncssh
        key_path = _shared_ssh_key_path()
        if not key_path:
            return (False, SHARED_SSH_KEY_MISSING_ERROR)
        if not _password:
            return (False, "请填写 SSH 密码以配置免密（须能登录节点上配置的用户名）")

        private_key = asyncssh.read_private_key(key_path)
        pub_key_data = private_key.export_public_key().decode().strip()
        escaped_pub_key = shlex.quote(pub_key_data)

        # 用密码登录一次，把 dispatcher 共享公钥幂等写入该用户 ~/.ssh/authorized_keys。
        try:
            async with asyncssh.connect(
                host=node.host, port=node.port, username=_ssh_username(node),
                password=_password, known_hosts=None,
            ) as conn:
                await conn.run(
                    "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                    "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                    f"(grep -Fvx -- {escaped_pub_key} ~/.ssh/authorized_keys || true) > ~/.ssh/authorized_keys.tmp && "
                    f"printf '%s\\n' {escaped_pub_key} >> ~/.ssh/authorized_keys.tmp && "
                    "mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys",
                    check=True,
                )
        except Exception as auth_err:
            return (False, f"无法使用密码登录目标机：{_normalize_ssh_error(str(auth_err))}")

        # 再用共享私钥验证免密可用。
        async with asyncssh.connect(
            host=node.host, port=node.port, username=_ssh_username(node),
            client_keys=[key_path], known_hosts=None,
        ) as conn:
            result = await conn.run("echo ok", check=True)
            if result.stdout.strip() != "ok":
                return (False, "共享私钥连通验证失败")
        node.auth_method = "key"
        node.ssh_key_path = DISPATCHER_SHARED_SSH_KEY
        node.status = "connected"
        node.last_connected = datetime.now(timezone.utc)
        await session.commit()
        return (True, "")
    except Exception as e:
        return (False, _normalize_ssh_error(str(e)))


@router.post("/nodes/{node_id}/setup-key")
async def setup_ssh_key(node_id: str, body: SetupKeyBody, session: AsyncSession = Depends(get_session)):
    """用密码登录一次，自动配置 SSH 免密（幂等：先检查现有密钥，不重复添加）"""
    node = await session.get(InfraNode, node_id)
    if not node:
        raise HTTPException(404)
    if node.type not in ("linux", "windows", "vm", "docker"):
        raise HTTPException(400, "仅 Linux/Windows 类型节点支持 SSH 免密配置")
    ok, err = await _do_setup_ssh_key(node, body.password, session)
    if ok:
        return {"ok": True, "message": "SSH 免密配置成功"}
    return {"ok": False, "error": err}


# ── 执行命令 ──

class ExecBody(BaseModel):
    command: str
    timeout: int = 30


@router.post("/nodes/{node_id}/exec")
async def exec_on_node(node_id: str, body: ExecBody, session: AsyncSession = Depends(get_session)):
    """在节点上执行命令"""
    node = await session.get(InfraNode, node_id)
    if not node:
        raise HTTPException(404)

    if node.type in ("linux", "windows", "vm", "docker"):
        return await _exec_ssh(node, body.command, body.timeout)
    elif node.type == "kubernetes":
        return await _exec_k8s(node, body.command, body.timeout)
    raise HTTPException(400, f"节点类型 {node.type} 不支持命令执行")


async def _exec_ssh(node: InfraNode, command: str, timeout: int) -> dict:
    import asyncssh
    key_path = _shared_ssh_key_path()
    if not key_path:
        return {"exit_code": -1, "error": SHARED_SSH_KEY_MISSING_ERROR}
    connect_args: dict = {
        "host": node.host, "port": node.port,
        "username": _ssh_username(node), "known_hosts": None,
        "client_keys": [key_path],
        "connect_timeout": 10,
    }

    try:
        async with asyncssh.connect(**connect_args) as conn:
            result = await asyncio.wait_for(conn.run(command), timeout=timeout)
            return {
                "exit_code": result.exit_status,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
    except Exception as e:
        return {"exit_code": -1, "error": str(e)}


async def _exec_docker(node: InfraNode, command: str, timeout: int) -> dict:
    container = node.config.get("container", "")
    if not container:
        return {"exit_code": -1, "error": "需要配置 config.container"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container, "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"exit_code": proc.returncode, "stdout": stdout.decode(), "stderr": stderr.decode()}
    except Exception as e:
        return {"exit_code": -1, "error": str(e)}


async def _exec_k8s(node: InfraNode, command: str, timeout: int) -> dict:
    kubeconfig = node.config.get("kubeconfig", "~/.kube/config")
    namespace = node.config.get("namespace", "default")
    pod = node.config.get("pod", "")
    if not pod:
        return {"exit_code": -1, "error": "需要配置 config.pod"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "--kubeconfig", kubeconfig, "-n", namespace,
            "exec", pod, "--", "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"exit_code": proc.returncode, "stdout": stdout.decode(), "stderr": stderr.decode()}
    except Exception as e:
        return {"exit_code": -1, "error": str(e)}


# ── WebSocket Terminal ──

@router.websocket("/nodes/{node_id}/terminal")
async def ws_terminal(websocket: WebSocket, node_id: str):
    """WebSocket SSH Terminal — 前端用 xterm.js 连接"""
    from jose import jwt, JWTError
    from app.core.config import settings as _s
    token = websocket.query_params.get("token", "")
    if token:
        try:
            jwt.decode(token, _s.JWT_SECRET, algorithms=["HS256"])
        except JWTError:
            await websocket.close(code=4001, reason="token expired")
            return
    else:
        await websocket.close(code=4001, reason="no token")
        return

    await websocket.accept()

    from app.core.database import async_session
    async with async_session() as session:
        gate_doc = await _load_terminal_gate(session)
        gate = _terminal_gate_enabled(gate_doc)
        node = await session.get(InfraNode, node_id)

    if not node:
        await websocket.send_json({"type": "error", "data": "节点不存在"})
        await websocket.close()
        return

    if gate:
        await websocket.send_json({"type": "auth_required"})
        for _ in range(TERMINAL_GATE_MAX_ATTEMPTS):
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=300.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "data": "终端操作密码验证超时"})
                await websocket.close()
                return
            if msg.get("type") == "close":
                await websocket.close()
                return
            if msg.get("type") != "auth":
                continue
            pw = msg.get("password")
            if not isinstance(pw, str):
                pw = ""
            async with async_session() as session:
                doc = await _load_terminal_gate(session)
                ok = _terminal_gate_enabled(doc) and _verify_terminal_gate_password(doc, pw)
            if ok:
                break
            await websocket.send_json({"type": "terminal_gate_denied", "data": "终端操作密码错误"})
        else:
            await websocket.send_json({"type": "error", "data": "尝试次数过多，请重新连接"})
            await websocket.close()
            return

    if node.type in ("linux", "windows", "vm", "docker"):
        await _ws_ssh_terminal(websocket, node)
    elif node.type == "kubernetes":
        await _ws_k8s_terminal(websocket, node)
    else:
        await websocket.send_json({"type": "error", "data": f"节点类型 {node.type} 不支持 Terminal"})
        await websocket.close()


async def _ws_ssh_terminal(websocket: WebSocket, node: InfraNode):
    import asyncssh
    key_path = _shared_ssh_key_path()
    if not key_path:
        await websocket.send_json({"type": "error", "data": SHARED_SSH_KEY_MISSING_ERROR})
        return
    connect_args: dict = {
        "host": node.host, "port": node.port,
        "username": _ssh_username(node), "known_hosts": None,
        "client_keys": [key_path],
        "connect_timeout": 10,
    }

    try:
        async with asyncssh.connect(**connect_args) as conn:
            async with conn.create_process(
                term_type="xterm-256color",
                term_size=(120, 40),
                encoding=None,
            ) as process:
                await websocket.send_json({"type": "connected"})

                async def _read_output():
                    try:
                        while True:
                            data = await process.stdout.read(65536)
                            if not data:
                                break
                            await websocket.send_json({
                                "type": "output",
                                "data": data.decode("utf-8", errors="replace"),
                            })
                    except (asyncssh.Error, WebSocketDisconnect):
                        pass

                async def _read_stderr():
                    try:
                        while True:
                            data = await process.stderr.read(65536)
                            if not data:
                                break
                            await websocket.send_json({
                                "type": "output",
                                "data": data.decode("utf-8", errors="replace"),
                            })
                    except (asyncssh.Error, WebSocketDisconnect):
                        pass

                async def _read_input():
                    try:
                        while True:
                            msg = await websocket.receive_json()
                            msg_type = msg.get("type")
                            if msg_type == "close":
                                try:
                                    process.stdin.write(b"exit\n")
                                except Exception:
                                    pass
                                break
                            if msg_type == "input":
                                raw = msg["data"]
                                process.stdin.write(raw.encode("utf-8") if isinstance(raw, str) else raw)
                            elif msg_type == "resize":
                                process.change_terminal_size(
                                    msg.get("cols", 120), msg.get("rows", 40),
                                )
                    except (WebSocketDisconnect, RuntimeError):
                        pass

                tasks = [
                    asyncio.create_task(_read_output()),
                    asyncio.create_task(_read_stderr()),
                    asyncio.create_task(_read_input()),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()

    except asyncssh.Error as e:
        await websocket.send_json({"type": "error", "data": f"SSH 连接失败: {e}"})
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _ws_docker_terminal(websocket: WebSocket, node: InfraNode):
    container = node.config.get("container", "")
    if not container:
        await websocket.send_json({"type": "error", "data": "未配置 container"})
        await websocket.close()
        return

    try:
        await websocket.send_json({"type": "connected"})
        await _ws_pty_process(websocket, ["docker", "exec", "-it", container, "/bin/bash"])
    except Exception as e:
        await websocket.send_json({"type": "error", "data": str(e)})
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _ws_k8s_terminal(websocket: WebSocket, node: InfraNode):
    kubeconfig = node.config.get("kubeconfig", "~/.kube/config")
    namespace = node.config.get("namespace", "default")
    pod = node.config.get("pod", "")
    if not pod:
        await websocket.send_json({"type": "error", "data": "未配置 pod"})
        await websocket.close()
        return

    try:
        await websocket.send_json({"type": "connected"})
        await _ws_pty_process(websocket, [
            "kubectl", "--kubeconfig", kubeconfig, "-n", namespace,
            "exec", "-it", pod, "--", "/bin/bash",
        ])
    except Exception as e:
        await websocket.send_json({"type": "error", "data": str(e)})
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _ws_pty_process(websocket: WebSocket, cmd: list[str]):
    """WebSocket ↔ PTY subprocess — 提供完整终端交互（回显、行编辑等）"""
    import fcntl
    import os
    import pty
    import signal
    import struct
    import termios

    master_fd, slave_fd = pty.openpty()

    # 默认大小 120x40，前端 resize 后会更新
    winsize = struct.pack("HHHH", 40, 120, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvp(cmd[0], cmd)

    os.close(slave_fd)
    loop = asyncio.get_event_loop()

    async def _read():
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                await websocket.send_json({"type": "output", "data": data.decode(errors="replace")})
        except (OSError, WebSocketDisconnect, RuntimeError):
            pass

    async def _write():
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "input":
                    await loop.run_in_executor(None, os.write, master_fd, msg["data"].encode())
                elif msg.get("type") == "resize":
                    cols = msg.get("cols", 120)
                    rows = msg.get("rows", 40)
                    ws = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
                    os.kill(pid, signal.SIGWINCH)
        except (OSError, WebSocketDisconnect, RuntimeError):
            pass

    tasks = [asyncio.create_task(_read()), asyncio.create_task(_write())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()

    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
        await loop.run_in_executor(None, os.waitpid, pid, 0)
    except ChildProcessError:
        pass


# ── CC Worker 镜像分发 ──

@router.post("/nodes/{node_id}/distribute-cc-worker-image")
async def distribute_cc_worker_image(node_id: str, session: AsyncSession = Depends(get_session)):
    """手动将 CC Worker 镜像分发到远程节点。

    流程：
    1. 检查远程节点是否已有镜像
    2. 查找本地预打包 tar（install 包内 /images/cc-worker-image.tar）
    3. 没有预打包则现场 docker save
    4. scp 传输到远程节点 /tmp/
    5. 远程 docker load
    """
    node = await session.get(InfraNode, node_id)
    if not node:
        raise HTTPException(404)
    if node.status != "connected":
        raise HTTPException(400, detail="节点未连接，请先配置 SSH 免密")

    from app.services.infra import _distribute_cc_worker_image

    result = await _distribute_cc_worker_image(node)
    return {
        "ok": result.get("ok"),
        "action": result.get("action"),
        "error": result.get("error", ""),
        "node_id": node.id,
        "node_host": node.host,
    }
