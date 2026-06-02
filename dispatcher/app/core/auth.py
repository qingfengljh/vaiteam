"""JWT 认证中间件"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from jose import jwt, JWTError

from app.core.config import settings

OPEN_PATHS = {"/health", "/api/auth/login", "/docs", "/openapi.json", "/redoc"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # BaseHTTPMiddleware 不兼容 WebSocket，跳过；WS 端点自行验证
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        if path in OPEN_PATHS or path.startswith("/api/auth/") or path.startswith("/api/help/"):
            return await call_next(request)

        if path.startswith("/api/internal/portal"):
            return await call_next(request)



        # Agent Connector / Worker 调用的端点免认证（通过内部网络访问）
        if path in ("/api/agents", "/api/agents/heartbeat", "/api/agents/model-config") or path.startswith("/api/webhook/") or path.endswith("/agent-reply"):
            return await call_next(request)

        # CC Worker 启动时拉取 Agent Provider 配置（按角色获取，无 session token）
        if path == "/api/agent-providers/active" or path.startswith("/worker/"):
            return await call_next(request)

        # 原型 Worker 拉 task-pack：凭 X-Prototype-Run-Secret 校验，路由内鉴权
        if path.startswith("/api/prototype-workshop/worker/"):
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.startswith("Bearer "):
            token = auth[7:]
        else:
            token = request.query_params.get("token", "")

        if not token:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        try:
            jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        except JWTError:
            return JSONResponse({"detail": "登录已过期，请重新登录"}, status_code=401)

        return await call_next(request)
