"""项目访问窗口校验：仅限制 APISIX 公网域名进来的请求。

设计意图：规避 ICP 运营证要求——用户自建站点通过公网域名访问时限制30天；
内网 IP 直接访问（如 http://192.168.x.x:13000）和 CC Worker 内部调用不受限。
"""

from __future__ import annotations

import ipaddress
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.database import async_session
from app.models import Agent, Document, Project, Task
from app.services.project_access import is_access_expired, json_body_for_middleware
from app.services.project_ref import resolve_project

# 内网 IP 段
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("::1/128"),    # IPv6 loopback
]


def _is_internal_request(request: Request) -> bool:
    """判断请求是否来自内网（直接访问，未经 APISIX 公网代理）。"""
    # X-Real-IP 是 APISIX 透传的客户端真实 IP
    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        try:
            addr = ipaddress.ip_address(real_ip.strip())
            return any(addr in net for net in _PRIVATE_NETS)
        except ValueError:
            pass
        # 如果是公网 IP（经 APISIX 进来的），返回 False，需要校验
        return False

    # 没有 X-Real-IP，通过 client host 判断
    client_host = request.client.host if request.client else ""
    if client_host:
        try:
            addr = ipaddress.ip_address(client_host)
            return any(addr in net for net in _PRIVATE_NETS)
        except ValueError:
            pass

    # 兜底：无法判断来源，视为内网（宽松策略，避免误伤）
    return True


def _skip_path(path: str) -> bool:
    if path in ("/health", "/docs", "/openapi.json", "/redoc"):
        return True
    if path.startswith("/api/auth/") or path.startswith("/api/help/"):
        return True
    if path.startswith("/api/internal/"):
        return True
    # Agent/Worker 内部调用，不受项目访问窗口限制
    if path.startswith("/api/webhook/"):
        return True
    if path.startswith("/api/worker/"):
        return True
    if path.startswith("/api/agent-providers/"):
        return True
    if path.endswith("/agent-reply"):
        return True
    if not path.startswith("/api/"):
        return True
    # 全局配置类接口，不绑定特定项目
    if path.startswith("/api/providers") or path.startswith("/api/experiences"):
        return True
    if path.startswith("/api/private/"):
        return True
    if path.startswith("/api/infra/"):
        return True
    if path.startswith("/api/agents/"):
        return True
    return False


_RE_PROJECT_SUB = re.compile(r"^/api/projects/([^/]+)/")
_RE_PROJECT_TOP = re.compile(r"^/api/projects/([^/]+)$")
_RE_CHAT = re.compile(r"^/api/chat/([^/]+)")
_RE_MSG_PROJ = re.compile(r"^/api/messages/project/([^/]+)")
_RE_TASK = re.compile(r"^/api/tasks/([^/]+)")
_RE_DOC = re.compile(r"^/api/docs/([^/]+)")
_RE_AGENT = re.compile(r"^/api/agents/([a-f0-9]{8})(?:/|$)")
_RE_AGENT_PROJECT_ACTION = re.compile(
    r"^/api/agents/(?:batch-hot-update|reset-team|stop-team)/([^/]+)"
)


class ProjectAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        path = request.url.path
        if _skip_path(path):
            return await call_next(request)

        # 内网 IP 直接访问不受限（仅 APISIX 公网域名入口需要校验）
        if _is_internal_request(request):
            return await call_next(request)

        method = request.method.upper()
        if method == "OPTIONS":
            return await call_next(request)

        if method == "GET" and (path == "/api/projects" or path.startswith("/api/projects?")):
            return await call_next(request)
        if method == "GET" and _RE_PROJECT_TOP.match(path):
            return await call_next(request)

        async with async_session() as session:
            project: Project | None = None

            m = _RE_PROJECT_SUB.match(path) or _RE_PROJECT_TOP.match(path)
            if m:
                project = await resolve_project(session, m.group(1))
            else:
                m = _RE_CHAT.match(path)
                if m:
                    project = await resolve_project(session, m.group(1))
                else:
                    m = _RE_MSG_PROJ.match(path)
                    if m:
                        project = await resolve_project(session, m.group(1))
                    else:
                        m = _RE_TASK.match(path)
                        if m:
                            task = await session.get(Task, m.group(1))
                            if task:
                                project = await session.get(Project, task.project_id)
                        else:
                            m = _RE_DOC.match(path)
                            if m:
                                doc = await session.get(Document, m.group(1))
                                if doc:
                                    project = await session.get(Project, doc.project_id)
                            else:
                                m = _RE_AGENT.match(path)
                                if m:
                                    agent = await session.get(Agent, m.group(1))
                                    if agent:
                                        project = await session.get(Project, agent.project_id)
                                else:
                                    m = _RE_AGENT_PROJECT_ACTION.match(path)
                                    if m:
                                        project = await session.get(Project, m.group(1))

            if path.startswith("/api/agents") and not project:
                qpid = request.query_params.get("project_id")
                if qpid:
                    p = await session.get(Project, qpid)
                    if p and is_access_expired(p):
                        return JSONResponse(json_body_for_middleware(p), status_code=403)

            if project and is_access_expired(project):
                return JSONResponse(
                    json_body_for_middleware(project),
                    status_code=403,
                )

        return await call_next(request)
