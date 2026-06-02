"""CC Worker 供应商池。

与 model_pool 完全独立：
- model_pool: Dispatcher AI Leader 用 OpenAI 协议
- cc_provider_pool: CC Worker/Claude Code 用 Anthropic 协议

支持角色级路由：不同角色可用完全不同的供应商。
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CCProvider, SystemConfig

logger = logging.getLogger(__name__)

_providers: dict[str, dict] = {}

# 角色 → CC Provider 名称映射（内存缓存，启动时从 DB 加载）
ROLE_CC_PROVIDER_MAP: dict[str, str] = {
    "architect": "",
    "senior": "",
    "mid": "",
    "junior": "",
    "devops": "",
    "tester": "",
}

# 角色 → 默认能力等级（用于选择 model_mapping 中的模型）
# Claude Code 内部用模型名（claude-opus-4-6 等），但映射按能力等级
ROLE_DEFAULT_TIER: dict[str, str] = {
    "architect": "opus",
    "senior": "opus",
    "mid": "sonnet",
    "junior": "haiku",
    "devops": "sonnet",
    "tester": "haiku",
}


async def load_cc_providers(session: AsyncSession):
    """从数据库加载 CC Provider 配置和角色映射。"""
    global _providers

    _providers.clear()

    q = await session.execute(select(CCProvider).where(CCProvider.enabled == True))  # noqa: E712
    for p in q.scalars():
        _providers[p.name] = {
            "id": p.id,
            "name": p.name,
            "display_name": p.display_name,
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

    # 加载角色映射
    await _load_role_map(session)

    logger.info(f"Loaded {len(_providers)} CC providers, role_map={ROLE_CC_PROVIDER_MAP}")


async def _load_role_map(session: AsyncSession):
    """从 system_configs 加载角色 → CC Provider 映射。"""
    global ROLE_CC_PROVIDER_MAP

    cfg = await session.get(SystemConfig, "role_cc_provider_map")
    if cfg and cfg.value:
        from app.core.constants import VALID_ROLES as _VALID_ROLES
        cleaned = {}
        for k, v in cfg.value.items():
            if k in _VALID_ROLES:
                cleaned[k] = v
        ROLE_CC_PROVIDER_MAP.update(cleaned)


def resolve_cc_provider(role: str = "") -> dict | None:
    """根据角色解析 CC Provider 配置。

    优先级：
    1. 角色映射 ROLE_CC_PROVIDER_MAP[role]
    2. 任意 enabled 的 provider（取第一个）
    3. None（未配置）
    """
    # 1. 角色映射
    if role:
        provider_name = ROLE_CC_PROVIDER_MAP.get(role)
        if provider_name and provider_name in _providers:
            return _providers[provider_name]

    # 2. 找默认 provider
    for name, p in _providers.items():
        if p.get("is_default"):
            return p

    # 3. 任意可用 provider
    if _providers:
        return next(iter(_providers.values()))

    return None


def resolve_cc_model(role: str = "", requested_model: str = "") -> tuple[str, dict]:
    """解析角色对应的实际模型名。

    返回 (actual_model, provider_config)
    """
    provider = resolve_cc_provider(role)
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


def get_all_cc_providers() -> list[dict]:
    """返回所有已加载的 CC Provider。"""
    return list(_providers.values())


def check_cc_provider_readiness() -> dict:
    """检查 CC Worker 供应商配置是否就绪。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not _providers:
        errors.append("未配置任何 CC Provider（Anthropic 协议供应商）")
        return {"ready": False, "errors": errors, "warnings": warnings}

    for role in ROLE_CC_PROVIDER_MAP:
        provider_name = ROLE_CC_PROVIDER_MAP.get(role)
        if not provider_name:
            warnings.append(f"角色 [{role}] 未配置 CC Provider（将使用默认供应商）")
            continue
        if provider_name not in _providers:
            errors.append(f"角色 [{role}] 映射的 CC Provider '{provider_name}' 不存在或被禁用")
            continue
        p = _providers[provider_name]
        if not p.get("api_key"):
            errors.append(f"CC Provider '{provider_name}'（角色 {role}）未配置 API Key")
        if not p.get("model_mapping") and not p.get("default_model"):
            warnings.append(f"CC Provider '{provider_name}'（角色 {role}）未配置模型映射或兜底模型")

    return {
        "ready": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
