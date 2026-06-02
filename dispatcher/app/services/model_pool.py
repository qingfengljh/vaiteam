"""
模型代理池 - 统一管理所有 LLM 供应商

所有模型调用统一经过这里。
支持多个代理平台，任务分配时按角色/模型名灵活路由。

优先从 openclaw.json 加载，DB 的 model_providers 表作为覆盖层。
"""

import logging
from openai import AsyncOpenAI

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelProvider as ModelProviderORM, ModelConfig
from app.core import openclaw_config

logger = logging.getLogger(__name__)

_clients: dict[str, AsyncOpenAI] = {}
_providers: dict[str, dict] = {}
_model_to_provider: dict[str, str] = {}
_model_params: dict[str, dict] = {}  # {"model_name": {"context_window": 128000, ...}}

ROLE_MODEL_MAP: dict[str, str] = {
    "leader": "deepseek-chat",
    "architect": "deepseek-chat",
    "senior": "deepseek-chat",
    "mid": "deepseek-chat",
    "junior": "deepseek-chat",
    "devops": "deepseek-chat",
    "tester": "deepseek-chat",
}

# 兼容旧配置，优先走基于 tier 的动态升级
MODEL_UPGRADE_CHAIN: dict[str, str] = {}

# 阶段→推荐能力等级：1=顶级 2=强 3=标准 4=基础
# 用户可在设置中覆盖具体模型
STAGE_TIER_MAP: dict[int, int] = {
    0: 3,  # 业务方案：标准即可
    1: 3,  # 需求规范：标准即可
    2: 2,  # 产品原型：需要较强的格式化和空间推理
    3: 2,  # 技术方案：需要架构设计能力
}

# 阶段→指定模型（优先级最高，管理员可配置）
STAGE_MODEL_MAP: dict[int, str] = {}


def _load_from_json():
    """从 openclaw.json 加载 LLM 供应商"""
    json_providers = openclaw_config.get_llm_providers()
    for name, cfg in json_providers.items():
        _providers[name] = cfg
        _clients[name] = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"])
        for model in cfg.get("models", []):
            _model_to_provider[model] = name

    role_map = openclaw_config.get_role_model_map()
    if role_map:
        ROLE_MODEL_MAP.update(role_map)

    upgrade_chain = openclaw_config.get_model_upgrade_chain()
    if upgrade_chain:
        MODEL_UPGRADE_CHAIN.update(upgrade_chain)


async def load_providers(session: AsyncSession):
    """加载供应商：先 JSON，再 DB 覆盖。模型参数从 model_configs 表加载。"""
    global _providers, _model_to_provider, _clients, _model_params

    _providers.clear()
    _model_to_provider.clear()
    _clients.clear()
    _model_params.clear()

    _load_from_json()

    q = await session.execute(select(ModelProviderORM).where(ModelProviderORM.enabled == True))  # noqa: E712
    for p in q.scalars():
        _providers[p.name] = p
        _clients[p.name] = AsyncOpenAI(api_key=p.api_key, base_url=p.api_base)
        for model in (p.models or []):
            _model_to_provider[model] = p.name

    # 从 model_configs 表加载模型参数
    mc_q = await session.execute(
        select(ModelConfig, ModelProviderORM.name)
        .join(ModelProviderORM, ModelConfig.provider_id == ModelProviderORM.id)
        .where(
            ModelConfig.enabled == True,  # noqa: E712
            ModelProviderORM.enabled == True,  # noqa: E712
        )
    )
    for mc, provider_name in mc_q.all():
        _model_to_provider[mc.model_name] = provider_name
        _model_params[mc.model_name] = {
            "context_window": mc.context_window,
            "max_output_tokens": mc.max_output_tokens,
            "supports_vision": mc.supports_vision,
            "vision_fallback": mc.vision_fallback,
            "input_price": mc.input_price,
            "output_price": mc.output_price,
            "cache_read_price": getattr(mc, "cache_read_price", 0) or 0,
            "capability_tier": mc.capability_tier,
        }

    # 从数据库加载角色→模型映射（覆盖硬编码默认值）
    from app.models import SystemConfig
    from app.core.constants import VALID_ROLES as _VALID_ROLES, ROLE_MIGRATION as _ROLE_MIGRATION
    try:
        cfg = await session.get(SystemConfig, "role_model_map")
        if cfg and cfg.value:
            cleaned = {}
            for k, v in cfg.value.items():
                if k in _VALID_ROLES:
                    cleaned[k] = v
                elif k in _ROLE_MIGRATION:
                    new_key = _ROLE_MIGRATION[k]
                    if new_key not in cleaned:
                        cleaned[new_key] = v
            ROLE_MODEL_MAP.update(cleaned)
            if set(cfg.value.keys()) != set(cleaned.keys()):
                cfg.value = dict(ROLE_MODEL_MAP)
                await session.commit()
                logger.info(f"Migrated role_model_map, removed legacy roles: {set(cfg.value.keys()) - _VALID_ROLES}")
            logger.info(f"Loaded role_model_map from DB: {cleaned}")
    except Exception as e:
        logger.debug(f"role_model_map not in DB yet: {e}")

    logger.info(f"Loaded {len(_providers)} providers, {len(_model_to_provider)} models, {len(_model_params)} model configs")


def _provider_is_default(p) -> bool:
    if isinstance(p, dict):
        return bool(p.get("is_default"))
    return bool(getattr(p, "is_default", False))


def _active_provider_for_model(model_name: str) -> str | None:
    """返回当前在 _clients 中可用的供应商名；无则 None（含供应商被禁用）。"""
    pn = _model_to_provider.get(model_name)
    if pn and pn in _clients:
        return pn
    return None


def parse_provider_model(qualified: str) -> tuple[str | None, str]:
    """解析 'provider/model' 格式，返回 (provider_name, model_name)。
    纯模型名返回 (None, model_name)。"""
    if "/" in qualified:
        provider, model = qualified.split("/", 1)
        return provider, model
    return None, qualified


def get_client(model: str) -> tuple[AsyncOpenAI, str]:
    """根据模型名获取对应的 client 和实际模型名。
    支持 'provider/model' 格式指定供应商。
    若模型原映射的供应商已禁用，则按能力等级选同级/降级且仍可用的模型。"""
    provider_name, actual_model = parse_provider_model(model)

    if provider_name and provider_name in _clients:
        return _clients[provider_name], actual_model

    found = _model_to_provider.get(actual_model)
    if found and found in _clients:
        return _clients[found], actual_model

    need_tier = _get_tier(actual_model)
    for cap in range(need_tier, 6):
        alt = find_model_for_tier(cap)
        if not alt:
            continue
        pn = _model_to_provider.get(alt)
        if pn and pn in _clients:
            if alt != actual_model or (found and found not in _clients):
                logger.warning(
                    "Model %s 无可用供应商映射（原供应商=%s）；改用 %s（tier_cap=%s）",
                    actual_model, found or provider_name or "-", alt, cap,
                )
            return _clients[pn], alt

    for name, p in _providers.items():
        if _provider_is_default(p) and name in _clients:
            logger.warning("Model %s 无匹配路由，使用默认供应商 %s 发送（模型名仍为 %s）", actual_model, name, actual_model)
            return _clients[name], actual_model

    raise ValueError(f"No provider found for model: {model}")


MODEL_ALIAS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-20250514",
    "deepseek": "deepseek-chat",
}


# --------------- 能力等级体系 ---------------

# 按模型名前缀推断能力等级（兜底，优先用数据库中配置的 capability_tier）
MODEL_TIER_PATTERNS: list[tuple[str, int]] = [
    ("claude-opus", 1),
    ("gpt-4o", 1),
    ("claude-sonnet", 2),
    ("gpt-4", 2),
    ("claude-haiku", 3),
    ("qwen", 3),
    ("deepseek", 3),
    ("gpt-3", 4),
]


def _bare_model(model: str) -> str:
    """从 'provider/model' 格式中提取纯模型名"""
    _, m = parse_provider_model(model)
    return m


def infer_tier(model: str) -> int:
    """根据模型名前缀推断能力等级，未匹配返回 3（标准）"""
    m = _bare_model(model).lower()
    for prefix, tier in MODEL_TIER_PATTERNS:
        if m.startswith(prefix):
            return tier
    return 3


def _get_tier(model: str) -> int:
    """获取模型能力等级：优先数据库配置，其次按名称前缀推断"""
    bare = _bare_model(model)
    params = _model_params.get(bare, {})
    db_tier = params.get("capability_tier")
    if db_tier is not None:
        return db_tier
    return infer_tier(bare)


# 编码阶段最低能力等级要求（tier 越小越强）
CODING_MIN_TIER = 2
CODING_ROLES = {"architect", "senior", "mid", "junior", "devops"}

# 角色→最低能力等级要求（tier 越小越强：1=顶级 2=强 3=标准 4=基础）
ROLE_MIN_TIER: dict[str, int] = {
    "architect": 2,
    "senior": 2,
    "mid": 3,
    "junior": 4,
    "devops": 3,
    "tester": 3,
}

# 任务类型 + 复杂度 → 最低模型等级
# 0 = 不限制（用角色默认模型），1 = 顶级，2 = 强，3 = 标准
TASK_TYPE_MIN_TIER: dict[str, int] = {
    "e2e_test": 2,
    "integration_test": 2,
    "architecture": 1,
    "security": 2,
    "performance": 2,
}

COMPLEXITY_MIN_TIER: dict[str, int] = {
    "high": 2,
    "critical": 1,
}

# 复杂度 → 模型路由策略
# low→初级(本地/DeepSeek) medium→中级(DeepSeek/Sonnet) high→高级(Sonnet) critical→顶级(Opus)
COMPLEXITY_MODEL_STRATEGY: dict[str, dict] = {
    "low": {"prefer_local": True, "min_tier": 4},
    "medium": {"prefer_local": False, "min_tier": 3},
    "high": {"prefer_local": False, "min_tier": 2},
    "critical": {"prefer_local": False, "min_tier": 1},
}


def task_min_tier(task_type: str, complexity: str = "") -> int:
    """根据任务类型和复杂度计算最低模型等级，0 表示不限"""
    t = TASK_TYPE_MIN_TIER.get(task_type, 0)
    c = COMPLEXITY_MIN_TIER.get(complexity, 0)
    if t == 0 and c == 0:
        return 0
    if t == 0:
        return c
    if c == 0:
        return t
    return min(t, c)


def model_meets_tier(model: str, min_tier: int) -> bool:
    """检查模型是否满足最低等级要求（tier 越小越强）"""
    if min_tier <= 0:
        return True
    return _get_tier(model) <= min_tier


def find_model_for_tier(min_tier: int) -> str | None:
    """找到满足最低等级要求的可用模型（优先选最便宜的）"""
    candidates = []
    for model in _model_to_provider:
        if not _active_provider_for_model(model):
            continue
        t = _get_tier(model)
        if t <= min_tier:
            price = _model_params.get(model, {}).get("input_price", 999)
            candidates.append((model, t, price))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[1], x[2]))
    return candidates[0][0]


# --------------- 模型解析与升级 ---------------


def resolve_model(
    role: str,
    override: str | None = None,
    module_map: dict | None = None,
    project_map: dict | None = None,
) -> str:
    """根据角色解析模型。优先级：任务级覆盖 > 模块级映射 > 项目级映射 > 全局默认"""
    if override:
        return MODEL_ALIAS.get(override, override)
    if module_map and role in module_map:
        return module_map[role]
    if project_map and role in project_map:
        return project_map[role]
    return ROLE_MODEL_MAP.get(role, ROLE_MODEL_MAP.get("mid", "deepseek-chat"))


def resolve_model_by_complexity(
    complexity: str,
    role: str = "mid",
    override: str | None = None,
    project_map: dict | None = None,
) -> tuple[str, bool]:
    """按任务复杂度路由模型。返回 (model_name, prefer_local)。

    low → 本地32B优先，cloud fallback
    medium → 云端标准模型（Sonnet/DeepSeek）
    high → 云端强模型（Sonnet）
    critical → 顶级模型（Opus）
    """
    if override:
        return MODEL_ALIAS.get(override, override), False

    strategy = COMPLEXITY_MODEL_STRATEGY.get(complexity, COMPLEXITY_MODEL_STRATEGY["medium"])
    prefer_local = strategy["prefer_local"]
    min_tier = strategy["min_tier"]

    if project_map and role in project_map:
        model = project_map[role]
        if model_meets_tier(model, min_tier):
            return model, prefer_local

    model = find_model_for_tier(min_tier)
    if model:
        return model, prefer_local

    return ROLE_MODEL_MAP.get(role, ROLE_MODEL_MAP.get("mid", "deepseek-chat")), prefer_local


def upgrade_model(current_model: str) -> str | None:
    """失败重试时升级到更强的模型，返回 None 表示已经是最强。
    策略：当前模型 tier=N → 找一个 tier=N-1 且可用的模型。
    如果 MODEL_UPGRADE_CHAIN 有显式配置则优先使用。
    """
    explicit = MODEL_UPGRADE_CHAIN.get(current_model)
    if explicit and _active_provider_for_model(explicit):
        return explicit

    current_tier = _get_tier(current_model)
    if current_tier <= 1:
        return None

    target_tier = current_tier - 1
    for model, params in _model_params.items():
        if model == current_model or not _active_provider_for_model(model):
            continue
        t = params.get("capability_tier")
        if t is not None and t == target_tier:
            return model

    for model in list(_model_to_provider.keys()):
        if model == current_model or not _active_provider_for_model(model):
            continue
        if infer_tier(model) == target_tier:
            return model

    return None


def check_coding_readiness() -> dict:
    """检查编码阶段所需模型是否就绪。
    返回 {ready: bool, errors: [...], warnings: [...]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    for role in CODING_ROLES:
        model = ROLE_MODEL_MAP.get(role)
        if not model:
            errors.append(f"角色 [{role}] 未配置模型")
            continue

        bare = _bare_model(model)
        provider, _ = parse_provider_model(model)
        registered = (provider and provider in _clients) or (_active_provider_for_model(bare) is not None)
        if not registered:
            errors.append(f"角色 [{role}] 的模型 {model} 未在任何供应商中注册")
            continue

        tier = _get_tier(model)
        min_tier = ROLE_MIN_TIER.get(role, CODING_MIN_TIER)
        if tier > min_tier:
            tier_label = TIER_LABELS.get(tier, str(tier))
            min_label = TIER_LABELS.get(min_tier, str(min_tier))
            warnings.append(
                f"角色 [{role}] 使用 {model}（等级: {tier_label}），"
                f"建议至少 {min_label} 级模型"
            )

    return {
        "ready": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def get_default_model() -> str | None:
    """返回默认供应商的第一个可用模型，未配置返回 None"""
    for name, p in _providers.items():
        if not _provider_is_default(p) or name not in _clients:
            continue
        models = getattr(p, "models", None) or (p.get("models") if isinstance(p, dict) else None) or []
        if models:
            return models[0]
    return None


def get_model_params(model: str) -> dict:
    """获取模型参数（context_window, max_output_tokens 等）"""
    return _model_params.get(model, {})


def get_context_window(model: str) -> int | None:
    """获取模型的上下文窗口大小，未配置返回 None"""
    params = _model_params.get(model, {})
    return params.get("context_window")


def is_model_registered(model: str) -> bool:
    """模型是否在当前已加载且启用的供应商池中可用。"""
    provider, bare = parse_provider_model(model)
    if provider:
        return provider in _clients
    return _active_provider_for_model(bare) is not None


def check_role_model_availability(role: str, project_map: dict | None = None) -> tuple[bool, str]:
    """检查角色模型是否可用，返回 (ok, reason)。"""
    model = resolve_model(role, project_map=project_map)
    if not model:
        return False, f"角色 [{role}] 未配置模型"
    if not is_model_registered(model):
        return False, f"角色 [{role}] 的模型 {model} 未在可用供应商中注册"
    return True, ""


def supports_vision(model: str) -> bool:
    """模型是否支持图片输入"""
    params = _model_params.get(model, {})
    return bool(params.get("supports_vision"))


def resolve_vision_model(model: str) -> str | None:
    """当模型不支持 vision 时，返回 vision_fallback 或用户标记的 vision 模型。
    没有任何标记的 vision 模型时返回 None，绝不盲选。"""
    if supports_vision(model):
        return model
    params = _model_params.get(model, {})
    fallback = params.get("vision_fallback")
    if fallback and _active_provider_for_model(fallback):
        return fallback
    candidates = []
    for m, p in _model_params.items():
        if p.get("supports_vision") and _active_provider_for_model(m):
            price = p.get("input_price", 999)
            candidates.append((m, price))
    if candidates:
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]
    return None


def recommend_model_for_stage(stage_index: int) -> dict | None:
    """根据阶段推荐最合适的模型，返回 {model, tier, reason} 或 None"""
    explicit = STAGE_MODEL_MAP.get(stage_index)
    if explicit and _active_provider_for_model(explicit):
        tier = _model_params.get(explicit, {}).get("capability_tier", 3)
        return {"model": explicit, "provider": _model_to_provider[explicit], "tier": tier, "reason": "管理员指定"}

    target_tier = STAGE_TIER_MAP.get(stage_index, 3)
    candidates = []
    for model, params in _model_params.items():
        t = params.get("capability_tier", 3)
        if _active_provider_for_model(model):
            candidates.append((model, t))

    if not candidates:
        return None

    exact = [(m, t) for m, t in candidates if t == target_tier]
    if exact:
        exact.sort(key=lambda x: _model_params.get(x[0], {}).get("input_price", 999))
        m0 = exact[0][0]
        return {"model": m0, "provider": _model_to_provider.get(m0, ""), "tier": exact[0][1], "reason": f"阶段{stage_index}推荐等级{target_tier}"}

    stronger = [(m, t) for m, t in candidates if t < target_tier]
    if stronger:
        stronger.sort(key=lambda x: (-x[1], _model_params.get(x[0], {}).get("input_price", 999)))
        m0 = stronger[0][0]
        return {"model": m0, "provider": _model_to_provider.get(m0, ""), "tier": stronger[0][1], "reason": f"阶段{stage_index}推荐等级{target_tier}，最接近可用"}

    weaker = [(m, t) for m, t in candidates if t > target_tier]
    weaker.sort(key=lambda x: (x[1], _model_params.get(x[0], {}).get("input_price", 999)))
    m0 = weaker[0][0]
    return {"model": m0, "provider": _model_to_provider.get(m0, ""), "tier": weaker[0][1], "reason": f"阶段{stage_index}推荐等级{target_tier}，降级使用"}


TIER_LABELS = {1: "顶级", 2: "强", 3: "标准", 4: "基础"}


def get_all_models_with_tier() -> list[dict]:
    """返回所有可用模型及其能力等级（已禁用供应商下的模型不出现）"""
    result = []
    for model, params in _model_params.items():
        if _active_provider_for_model(model):
            tier = params.get("capability_tier", 3)
            result.append({
                "model": model,
                "tier": tier,
                "tier_label": TIER_LABELS.get(tier, "未知"),
                "context_window": params.get("context_window", 0),
            })
    result.sort(key=lambda x: (x["tier"], x["model"]))
    return result


async def chat(model: str, messages: list[dict], max_tokens: int = 4096) -> str:
    """统一聊天接口"""
    client, actual_model = get_client(model)
    resp = await client.chat.completions.create(
        model=actual_model,
        max_tokens=max_tokens,
        messages=messages,
    )
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            msg = (choices[0] or {}).get("message") or {}
            content = msg.get("content") or ""
            return content if isinstance(content, str) else str(content)
        return str(resp)
    choices = getattr(resp, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", "") if message else ""
        return content if isinstance(content, str) else str(content)
    return str(resp)
