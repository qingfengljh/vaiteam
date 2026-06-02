"""SaaS Portal 调用的内部接口（凭共享 Token，不走用户 JWT）。"""

import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.constants import INFRA_NODE_ROLES
from app.models import InfraGroup, InfraGroupNode, InfraNode, SystemConfig
from app.routers.auth import clear_all_login_locks_and_fail_counters
from app.services.auth_owner_contact import upsert_owner_contact_email

router = APIRouter(prefix="/api/internal/portal", tags=["portal-internal"])


def _portal_token_or_403(x_portal_service_token: str | None) -> None:
    expected = (settings.VAITEAM_PORTAL_SERVICE_TOKEN or "").strip()
    if not expected:
        raise HTTPException(503, "未配置 VAITEAM_PORTAL_SERVICE_TOKEN，拒绝 Portal 调用")
    if (x_portal_service_token or "").strip() != expected:
        raise HTTPException(403, "无效的服务令牌")


class SetAdminPasswordBody(BaseModel):
    password: str = Field(..., min_length=8, max_length=256)
    # Portal 租户 owner 注册邮箱；非空时写入 system_configs，供登录页「忘记密码」发链
    owner_contact_email: str | None = Field(None, max_length=320)


@router.post("/set-admin-password")
async def set_admin_password(
    body: SetAdminPasswordBody,
    session: AsyncSession = Depends(get_session),
    x_portal_service_token: str | None = Header(default=None, alias="X-Portal-Service-Token"),
):
    _portal_token_or_403(x_portal_service_token)

    creds = {"username": "admin", "password": body.password}
    cfg = await session.get(SystemConfig, "auth_credentials")
    if cfg:
        cfg.value = creds
    else:
        session.add(SystemConfig(key="auth_credentials", value=creds))
    if body.owner_contact_email is not None and body.owner_contact_email.strip():
        await upsert_owner_contact_email(session, body.owner_contact_email)
    await session.commit()
    cleared = await clear_all_login_locks_and_fail_counters()
    return {"ok": True, "login_lock_keys_cleared": cleared}


class BootstrapManagedInfraBody(BaseModel):
    """全托管开通后由 Portal 写入默认「节点 + 环境组」；自备机租户不调用。"""

    idempotency_key: str = Field(..., min_length=1, max_length=96)
    ssh_host: str = Field(..., min_length=1, max_length=200)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    node_name: str = Field(..., min_length=1, max_length=100)
    group_name: str = Field(..., min_length=1, max_length=100)
    group_description: str = Field(default="", max_length=500)


@router.post("/bootstrap-managed-infra")
async def bootstrap_managed_infra(
    body: BootstrapManagedInfraBody,
    session: AsyncSession = Depends(get_session),
    x_portal_service_token: str | None = Header(default=None, alias="X-Portal-Service-Token"),
):
    _portal_token_or_403(x_portal_service_token)

    h = hashlib.sha256(body.idempotency_key.encode()).hexdigest()
    cfg_key = f"mi:{h}"
    existing = await session.get(SystemConfig, cfg_key)
    if existing and isinstance(existing.value, dict) and existing.value.get("group_id"):
        return {
            "ok": True,
            "already": True,
            "group_id": existing.value.get("group_id"),
            "node_id": existing.value.get("node_id"),
        }

    roles = list(INFRA_NODE_ROLES.keys())
    node = InfraNode(
        name=body.node_name,
        type="linux",
        host=body.ssh_host.strip(),
        port=body.ssh_port,
        user="root",
        config={"platform_managed": True},
    )
    session.add(node)
    await session.flush()

    group = InfraGroup(
        name=body.group_name,
        description=body.group_description or "SaaS 全托管自动创建",
        purpose="platform",
    )
    session.add(group)
    await session.flush()

    session.add(InfraGroupNode(group_id=group.id, node_id=node.id, roles=roles))
    session.add(SystemConfig(key=cfg_key, value={"group_id": group.id, "node_id": node.id}))
    await session.commit()
    return {"ok": True, "already": False, "group_id": group.id, "node_id": node.id}
