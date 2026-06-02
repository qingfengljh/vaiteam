"""Agent 工具供应商池。

与 model_pool 协作：
- model_pool: Dispatcher AI Leader 用 OpenAI 协议
- agent_provider_pool: Agent Worker（如 Claude Code）用各工具原生协议

支持角色级路由：不同角色可用完全不同的供应商。
支持 source_provider_id 继承：关联 ModelProvider 统一配置 Token。

TODO（未来）: 支持定时任务/周期性任务调度配置
- 当前：架构师调用一次性任务
- 未来：某个虚拟员工可配置为定时执行特定任务（如每日代码审查、周报生成等）
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentProvider, SystemConfig, ModelProvider

logger = logging.getLogger(__name__)

_providers: dict[str, dict] = {}

# 角色 → Agent Provider 名称映射（内存缓存，启动时从 DB 加载）
ROLE_AGENT_PROVIDER_MAP: dict[str, str] = {
    "architect": "",
    "senior": "",
    "mid": "",
    "junior": "",
    "devops": "",
    "tester": "",
}

# 角色 → 默认能力等级（用于选择 model_mapping 中的模型）
ROLE_DEFAULT_TIER: dict[str, str] = {
    "architect": "opus",
    "senior": "opus",
    "mid": "sonnet",
    "junior": "haiku",
    "devops": "sonnet",
    "tester": "haiku",
}


async def load_agent_providers(session: AsyncSession):
    """从数据库加载 Agent Provider 配置和角色映射。"""
    global _providers

    _providers.clear()

    # 预加载 ModelProvider 用于继承解析
    mp_map: dict[str, ModelProvider] = {}
    mp_q = await session.execute(select(ModelProvider))
    for mp in mp_q.scalars():
        mp_map[mp.id] = mp

    q = await session.execute(select(AgentProvider).where(AgentProvider.enabled == True))  # noqa: E712
    for p in q.scalars():
        cfg = {
            "id": p.id,
            "name": p.name,
            "display_name": p.display_name,
            "agent_type": p.agent_type,
            "source_provider_id": p.source_provider_id,
            "protocol_adapter": p.protocol_adapter or "anthropic_direct",
            "litellm_config": p.litellm_config or {},
            "api_base": p.api_base,
            "api_key": p.api_key,
            "credential_env_name": p.credential_env_name,
            "credential_source": p.credential_source,
            "model_mapping": p.model_mapping or {},
            "default_model": p.default_model or "",
            "supports_1m_context": p.supports_1m_context,
            "enabled": p.enabled,
            "is_default": p.is_default,
        }

        # ── source_provider_id 继承 ──
        if p.source_provider_id and p.source_provider_id in mp_map:
            mp = mp_map[p.source_provider_id]
            # api_base / api_key 继承（AgentProvider 未填写时）
            if not cfg["api_base"] and mp.api_base:
                cfg["api_base"] = mp.api_base
                cfg["_inherited_api_base"] = True
            if not cfg["api_key"] and mp.api_key:
                cfg["api_key"] = mp.api_key
                cfg["_inherited_api_key"] = True
            # credential_source 继承
            if not cfg.get("credential_source") and mp.credential_source:
                cfg["credential_source"] = mp.credential_source

        _providers[p.name] = cfg

    # 加载角色映射
    await _load_role_map(session)

    logger.info(f"Loaded {len(_providers)} agent providers, role_map={ROLE_AGENT_PROVIDER_MAP}")


async def _load_role_map(session: AsyncSession):
    """从 system_configs 加载角色 → Agent Provider 映射。"""
    global ROLE_AGENT_PROVIDER_MAP

    cfg = await session.get(SystemConfig, "role_agent_provider_map")
    if cfg and cfg.value:
        from app.core.constants import VALID_ROLES as _VALID_ROLES
        cleaned = {}
        for k, v in cfg.value.items():
            if k in _VALID_ROLES:
                cleaned[k] = v
        ROLE_AGENT_PROVIDER_MAP.update(cleaned)


def resolve_agent_provider(role: str = "", agent_type: str = "claude_code") -> dict | None:
    """根据角色和 Agent 工具类型解析 Provider 配置。

    优先级：
    1. 角色映射 ROLE_AGENT_PROVIDER_MAP[role]
    2. 匹配 agent_type 的默认 provider
    3. 任意 enabled 的 provider（取第一个）
    4. None（未配置）
    """
    # 1. 角色映射
    if role:
        provider_name = ROLE_AGENT_PROVIDER_MAP.get(role)
        if provider_name and provider_name in _providers:
            p = _providers[provider_name]
            if p.get("agent_type") == agent_type:
                return p

    # 2. 找匹配 agent_type 的默认 provider
    for name, p in _providers.items():
        if p.get("agent_type") == agent_type and p.get("is_default"):
            return p

    # 3. 任意匹配 agent_type 的 provider
    for name, p in _providers.items():
        if p.get("agent_type") == agent_type:
            return p

    # 4. 任意可用 provider（兜底）
    if _providers:
        return next(iter(_providers.values()))

    return None


def resolve_agent_model(role: str = "", requested_model: str = "") -> tuple[str, dict]:
    """解析角色对应的实际模型名。

    返回 (actual_model, provider_config)
    """
    provider = resolve_agent_provider(role)
    if not provider:
        return "", {}

    mapping = provider.get("model_mapping", {})

    # 如果请求了具体模型名，尝试匹配
    if requested_model:
        # 直接匹配
        if requested_model in mapping:
            return mapping[requested_model], provider
        # 按能力等级匹配（如 "claude-sonnet-4-6" → sonnet 等级）
        tier = _infer_tier_from_model_name(requested_model)
        if tier and tier in mapping:
            return mapping[tier], provider

    # 按角色默认等级匹配
    tier = ROLE_DEFAULT_TIER.get(role, "sonnet")
    if tier in mapping:
        return mapping[tier], provider

    # 兜底
    default = provider.get("default_model", "")
    if default:
        return default, provider

    # 最后的兜底：mapping 中的第一个
    if mapping:
        return next(iter(mapping.values())), provider

    return "", provider


def _infer_tier_from_model_name(model_name: str) -> str:
    """从 Claude 模型名推断能力等级。"""
    m = model_name.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return ""


def get_all_agent_providers() -> list[dict]:
    """返回所有已加载的 Agent Provider。"""
    return list(_providers.values())


def check_agent_provider_readiness() -> dict:
    """检查 Agent Worker 供应商配置是否就绪。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not _providers:
        errors.append("未配置任何 Agent Provider（Agent 工具供应商）")
        return {"ready": False, "errors": errors, "warnings": warnings}

    for role in ROLE_AGENT_PROVIDER_MAP:
        provider_name = ROLE_AGENT_PROVIDER_MAP.get(role)
        if not provider_name:
            warnings.append(f"角色 [{role}] 未配置 Agent Provider（将使用默认供应商）")
            continue
        if provider_name not in _providers:
            errors.append(f"角色 [{role}] 映射的 Agent Provider '{provider_name}' 不存在或被禁用")
            continue
        p = _providers[provider_name]
        if not p.get("api_key"):
            errors.append(f"Agent Provider '{provider_name}'（角色 {role}）未配置 API Key")
        if not p.get("model_mapping") and not p.get("default_model"):
            warnings.append(f"Agent Provider '{provider_name}'（角色 {role}）未配置模型映射或兜底模型")

    return {
        "ready": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
