"""Agent 工具供应商配置。

与 model_providers 完全独立：
- model_providers: Dispatcher AI Leader 用 OpenAI 协议 (/v1/chat/completions)
- agent_providers: Agent Worker 用各工具原生协议（Claude Code 用 Anthropic /v1/messages）

支持多供应商、角色级路由：
- 不同角色可用完全不同的供应商（Senior→OpenRouter, Mid→DeepSeek）
- 每个供应商内按能力等级(opus/sonnet/haiku)映射实际模型名

未来可扩展 agent_type: claude_code | codex | ...
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.models import AgentProvider, SystemConfig, ModelProvider
from app.core.constants import VALID_ROLES as _VALID_ROLES

router = APIRouter(prefix="/api/agent-providers", tags=["agent-providers"])


def _mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    prefix = "sk-" if key.startswith("sk-") else ""
    body = key[3:] if prefix else key
    if not body:
        return f"{prefix}***"
    start = body[:4]
    end = body[-2:] if len(body) >= 2 else ""
    return f"{prefix}{start}***{end}"


class AgentProviderCreate(BaseModel):
    name: str
    display_name: str = ""
    agent_type: str = "claude_code"
    # 关联 ModelProvider（统一 Token 配置）
    source_provider_id: str | None = None
    # 协议适配：anthropic_direct | openai_via_litellm | codex
    protocol_adapter: str = "anthropic_direct"
    litellm_config: dict = {}
    # 以下字段在 source_provider_id 为空时必填
    api_base: str = ""
    api_key: str = ""
    credential_env_name: str = "ANTHROPIC_API_KEY"
    credential_source: str = "byok"
    model_mapping: dict[str, str] = {}
    default_model: str = ""
    supports_1m_context: bool = False
    enabled: bool = True
    is_default: bool = False
    notes: str = ""


class RoleAgentProviderMapUpdate(BaseModel):
    mapping: dict[str, str]


# ── CRUD ──

@router.post("")
async def create_agent_provider(body: AgentProviderCreate, session: AsyncSession = Depends(get_session)):
    src = (body.credential_source or "byok").strip().lower()
    if src not in ("byok", "platform"):
        raise HTTPException(400, detail="credential_source 须为 byok 或 platform")

    data = body.model_dump()
    data["credential_source"] = src

    # ── source_provider_id 继承逻辑 ──
    source_id = data.get("source_provider_id")
    if source_id:
        mp = await session.get(ModelProvider, source_id)
        if not mp:
            raise HTTPException(400, detail=f"关联的 ModelProvider '{source_id}' 不存在")
        # 继承 api_base / api_key（用户未填写时）
        if not data.get("api_base"):
            data["api_base"] = mp.api_base
        if not data.get("api_key"):
            data["api_key"] = mp.api_key
        # 继承 credential_source
        if not data.get("credential_source"):
            data["credential_source"] = mp.credential_source
    else:
        # 无关联 ModelProvider 时，api_base / api_key 必填
        if not (data.get("api_base") or "").strip():
            raise HTTPException(400, detail="未关联 ModelProvider 时须填写 api_base")
        if src == "byok" and not (data.get("api_key") or "").strip():
            raise HTTPException(400, detail="自备 Key 时须填写 api_key")

    p = AgentProvider(**data)
    session.add(p)
    await session.commit()
    return {"id": p.id, "name": p.name}


@router.get("")
async def list_agent_providers(session: AsyncSession = Depends(get_session)):
    from sqlalchemy.orm import joinedload

    q = await session.execute(
        select(AgentProvider)
        .options(joinedload(AgentProvider.source_provider))
        .order_by(AgentProvider.name)
    )
    result = []
    for p in q.unique().scalars():
        csrc = (getattr(p, "credential_source", None) or "byok").strip().lower()
        masked = _mask_api_key(p.api_key)
        if csrc == "platform":
            masked = "平台托管（密钥由平台注入）"

        # 继承信息
        source_info = {}
        if p.source_provider_id:
            mp = p.source_provider
            if mp:
                source_info = {
                    "source_provider_id": p.source_provider_id,
                    "source_provider_name": mp.name,
                    "inherited_api_base": mp.api_base if not p.api_base else None,
                    "inherited_api_key_masked": _mask_api_key(mp.api_key) if not p.api_key else None,
                }

        result.append({
            "id": p.id, "name": p.name, "display_name": p.display_name,
            "agent_type": p.agent_type,
            "source_provider_id": p.source_provider_id,
            "protocol_adapter": p.protocol_adapter,
            "litellm_config": p.litellm_config or {},
            "api_base": p.api_base, "credential_env_name": p.credential_env_name,
            "credential_source": csrc,
            "model_mapping": p.model_mapping or {},
            "default_model": p.default_model or "",
            "supports_1m_context": p.supports_1m_context,
            "enabled": p.enabled, "is_default": p.is_default,
            "api_key_masked": masked,
            "notes": p.notes,
            **source_info,
        })
    return result

# ── 角色映射 ──

@router.get("/role-map")
async def get_role_agent_provider_map():
    """获取角色 → Agent Provider 的映射。"""
    from app.services.agent_provider_pool import ROLE_AGENT_PROVIDER_MAP
    return {k: v for k, v in ROLE_AGENT_PROVIDER_MAP.items() if k in _VALID_ROLES}


@router.put("/role-map")
async def update_role_agent_provider_map(
    body: RoleAgentProviderMapUpdate, session: AsyncSession = Depends(get_session)
):
    """更新角色 → Agent Provider 映射。value 为 agent_provider 的 name。"""
    from app.services import agent_provider_pool

    cleaned = {k: v for k, v in body.mapping.items() if k in _VALID_ROLES}
    agent_provider_pool.ROLE_AGENT_PROVIDER_MAP.update(cleaned)
    # 清除旧角色
    for old_key in list(agent_provider_pool.ROLE_AGENT_PROVIDER_MAP):
        if old_key not in _VALID_ROLES:
            del agent_provider_pool.ROLE_AGENT_PROVIDER_MAP[old_key]

    cfg = await session.get(SystemConfig, "role_agent_provider_map")
    if cfg:
        cfg.value = dict(agent_provider_pool.ROLE_AGENT_PROVIDER_MAP)
    else:
        session.add(SystemConfig(
            key="role_agent_provider_map",
            value=dict(agent_provider_pool.ROLE_AGENT_PROVIDER_MAP)
        ))
    await session.commit()
    return agent_provider_pool.ROLE_AGENT_PROVIDER_MAP


# ── Agent Worker 启动时调用 ──

@router.get("/active")
async def get_active_agent_provider_config(
    role: str = "",
    agent_type: str = "claude_code",
    session: AsyncSession = Depends(get_session),
):
    """Agent Worker 启动时调用：根据角色获取对应的 Agent Provider 配置。

    Query param:
        role: 角色名（senior/mid/junior/architect/devops/tester）
        agent_type: Agent 工具类型（当前仅 claude_code）

    返回 Agent 工具所需的配置：
        - credential_env_name（ANTHROPIC_API_KEY 等）
        - api_key
        - api_base
        - model_mapping
        - default_model
    """
    from app.services import agent_provider_pool

    await agent_provider_pool.load_agent_providers(session)

    provider = agent_provider_pool.resolve_agent_provider(role, agent_type)
    if not provider:
        raise HTTPException(404, detail=f"角色 [{role}] 未找到可用的 Agent Provider 配置")

    return {
        "agent_type": provider.get("agent_type", "claude_code"),
        "protocol_adapter": provider.get("protocol_adapter", "anthropic_direct"),
        "litellm_config": provider.get("litellm_config", {}),
        "credential_env_name": provider.get("credential_env_name", "ANTHROPIC_API_KEY"),
        "api_key": provider.get("api_key", ""),
        "api_base": provider.get("api_base", ""),
        "model_mapping": provider.get("model_mapping", {}),
        "default_model": provider.get("default_model", ""),
        "supports_1m_context": provider.get("supports_1m_context", False),
        "provider_name": provider.get("name", ""),
    }


# ── 测试 ──

@router.post("/{provider_id}/test")
async def test_agent_provider(provider_id: str, session: AsyncSession = Depends(get_session)):
    """测试 Agent Provider 的接口连通性。当前仅支持 claude_code (Anthropic SDK)。"""
    import time

    p = await session.get(AgentProvider, provider_id)
    if not p:
        raise HTTPException(404)

    if p.agent_type == "claude_code":
        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=p.api_key, base_url=p.api_base)
            model = p.default_model or (list(p.model_mapping.values())[0] if p.model_mapping else "")
            if not model:
                return {"ok": False, "error": "未配置 default_model 或 model_mapping"}

            t0 = time.monotonic()
            resp = client.messages.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi, reply with 'ok'"}],
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            content = ""
            if resp.content:
                content = resp.content[0].text if hasattr(resp.content[0], "text") else str(resp.content[0])
            return {
                "ok": True,
                "model": model,
                "response": content.strip()[:100],
                "latency_ms": elapsed,
                "usage": {
                    "input_tokens": resp.usage.input_tokens if resp.usage else 0,
                    "output_tokens": resp.usage.output_tokens if resp.usage else 0,
                },
            }
        except Exception as e:
            err = str(e)[:300]
            return {"ok": False, "error": err}

    return {"ok": False, "error": f"暂不支持测试 agent_type={p.agent_type}"}



@router.put("/{provider_id}")
async def update_agent_provider(
    provider_id: str, body: AgentProviderCreate, session: AsyncSession = Depends(get_session)
):
    p = await session.get(AgentProvider, provider_id)
    if not p:
        raise HTTPException(404)
    payload = body.model_dump()
    src = (payload.get("credential_source") or p.credential_source or "byok").strip().lower()
    if src not in ("byok", "platform"):
        raise HTTPException(400, detail="credential_source 须为 byok 或 platform")
    payload["credential_source"] = src

    # ── source_provider_id 变更时的继承逻辑 ──
    new_source_id = payload.get("source_provider_id")
    if new_source_id and new_source_id != getattr(p, "source_provider_id", None):
        mp = await session.get(ModelProvider, new_source_id)
        if not mp:
            raise HTTPException(400, detail=f"关联的 ModelProvider '{new_source_id}' 不存在")
        # 新关联时，若字段为空则继承
        if not payload.get("api_base"):
            payload["api_base"] = mp.api_base
        if not payload.get("api_key"):
            payload["api_key"] = mp.api_key
    elif not new_source_id and not getattr(p, "source_provider_id", None):
        # 始终独立配置：校验必填
        if not (payload.get("api_base") or p.api_base or "").strip():
            raise HTTPException(400, detail="未关联 ModelProvider 时须填写 api_base")

    for k, v in payload.items():
        if k == "api_key" and not v:
            continue
        if k == "source_provider_id" and not v:
            setattr(p, k, None)
            continue
        setattr(p, k, v)
    await session.commit()
    return {"id": p.id, "name": p.name}


@router.delete("/{provider_id}")
async def delete_agent_provider(provider_id: str, session: AsyncSession = Depends(get_session)):
    p = await session.get(AgentProvider, provider_id)
    if not p:
        raise HTTPException(404)
    await session.delete(p)
    await session.commit()
    return {"status": "deleted"}


@router.patch("/{provider_id}/enabled")
async def patch_agent_provider_enabled(
    provider_id: str, body: AgentProviderCreate, session: AsyncSession = Depends(get_session)
):
    p = await session.get(AgentProvider, provider_id)
    if not p:
        raise HTTPException(404)
    p.enabled = body.enabled
    await session.commit()
    return {"id": p.id, "enabled": p.enabled}


@router.post("/{provider_id}/set-default")
async def set_default_agent_provider(provider_id: str, session: AsyncSession = Depends(get_session)):
    """将指定 Agent Provider 设为默认。同时取消其他 provider 的默认状态。"""
    p = await session.get(AgentProvider, provider_id)
    if not p:
        raise HTTPException(404)

    # 取消其他 provider 的默认状态
    q = await session.execute(select(AgentProvider).where(AgentProvider.is_default == True))  # noqa: E712
    for other in q.scalars():
        other.is_default = False

    p.is_default = True
    await session.commit()
    return {"id": p.id, "is_default": True}

