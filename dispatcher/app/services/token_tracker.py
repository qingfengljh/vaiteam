"""
Token 消耗追踪服务

记录每次 AI 调用的 token 用量和成本，支持按项目/模型/角色统计。
"""

import logging
import os
import time
from contextvars import ContextVar
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TokenUsageLog, ModelProvider

logger = logging.getLogger(__name__)

_current_project_id: ContextVar[str | None] = ContextVar("project_id", default=None)
_current_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)

# model_name 或 provider_name -> (input ￥/M, output ￥/M, cache_read ￥/M)
_price_cache: dict[str, tuple[float, float, float]] = {}


def set_context(project_id: str | None = None, task_id: str | None = None):
    if project_id is not None:
        _current_project_id.set(project_id)
    if task_id is not None:
        _current_task_id.set(task_id)


def clear_context():
    _current_project_id.set(None)
    _current_task_id.set(None)


def _usage_get(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_cache_read_tokens_from_usage(usage) -> int:
    """从 chat.completions / Claude 桥接 usage 取 prompt cache **读命中** token 数。

    优先级说明：
    - **Claude / Anthropic 及多数国内 OpenAI 兼容网关**：`cache_read_input_tokens` 在 **usage 顶层**，
      与 OpenAI 的 `prompt_tokens_details.cached_tokens` 二选一或并存；必须先读顶层，否则命中恒为 0、
      整段按原价输入计费（与平台账单差数倍）。
    - OpenAI：`usage.prompt_tokens_details.cached_tokens`（SDK 可能为对象非 dict）。
    """
    if not usage:
        return 0
    for key in ("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_prompt_tokens"):
        try:
            n = int(_usage_get(usage, key, 0) or 0)
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    details = _usage_get(usage, "prompt_tokens_details")
    if details is not None:
        for dk in ("cached_tokens", "cached", "cachedTokens"):
            n = _usage_get(details, dk)
            try:
                n = int(n or 0)
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue
    return 0


def extract_prompt_tokens_for_billing(usage) -> int:
    """用于计费的 prompt 侧总 token（与 cache_read 搭配做拆分）。

    - OpenAI：`prompt_tokens` 一般为整段 prompt 总量。
    - 仅返回 Anthropic 形字段时：`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`（无 prompt_tokens 时）。
    """
    if not usage:
        return 0
    p = _usage_get(usage, "prompt_tokens")
    try:
        pi = int(p or 0)
    except (TypeError, ValueError):
        pi = 0
    if pi > 0:
        return pi
    unc = int(_usage_get(usage, "input_tokens", 0) or 0)
    cr = int(_usage_get(usage, "cache_read_input_tokens", 0) or 0)
    cc = int(_usage_get(usage, "cache_creation_input_tokens", 0) or 0)
    s = unc + cr + cc
    if s > 0:
        return s
    return 0


def extract_completion_tokens_from_usage(usage) -> int:
    if not usage:
        return 0
    for key in ("completion_tokens", "output_tokens"):
        try:
            n = int(_usage_get(usage, key, 0) or 0)
            if n >= 0:
                return n
        except (TypeError, ValueError):
            continue
    return 0


def extract_cache_creation_tokens_from_usage(usage) -> int:
    """prompt cache **写入**（创建）token；OpenAI 兼容网关常见 claude_cache_creation_* 分项。"""
    if not usage:
        return 0
    for key in ("cache_creation_input_tokens",):
        try:
            n = int(_usage_get(usage, key, 0) or 0)
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    s = 0
    for key in ("claude_cache_creation_5_m_tokens", "claude_cache_creation_1_h_tokens"):
        try:
            s += int(_usage_get(usage, key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return max(0, s)


def _prompt_cost_mode() -> str:
    return os.getenv("DISPATCHER_PROMPT_COST_MODE", "split").strip().lower()


def _cache_creation_price_per_mtok(p_in: float) -> float:
    return p_in * float(os.getenv("DISPATCHER_CACHE_CREATION_INPUT_MULT", "1.25"))


async def load_prices(session: AsyncSession):
    """从 model_configs / model_providers 加载单价（含 prompt cache read）。"""
    from app.models import ModelConfig
    mc_q = await session.execute(
        select(ModelConfig, ModelProvider)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id)
        .where(ModelConfig.enabled == True)  # noqa: E712
    )
    for mc, p in mc_q.all():
        pin = mc.input_price if mc.input_price > 0 else p.input_price_per_mtok
        pout = mc.output_price if mc.output_price > 0 else p.output_price_per_mtok
        pcache = mc.cache_read_price if mc.cache_read_price > 0 else (p.cache_read_price_per_mtok or 0.0)
        _price_cache[mc.model_name] = (pin, pout, pcache)
        _price_cache[p.name] = (
            p.input_price_per_mtok,
            p.output_price_per_mtok,
            p.cache_read_price_per_mtok or 0.0,
        )


def _resolve_model_prices(model: str) -> tuple[float, float, float] | None:
    """按模型解析 (input, output, cache_read) ￥/M；精确匹配优先。"""
    if not model or not _price_cache:
        return None
    if model in _price_cache:
        return _price_cache[model]
    tail = model.rsplit("/", 1)[-1]
    if tail in _price_cache:
        return _price_cache[tail]
    best: tuple[float, float, float] | None = None
    best_len = -1
    for key, val in _price_cache.items():
        if key == tail or ("/" in key and key.endswith("/" + tail)):
            if len(key) > best_len:
                best_len, best = len(key), val
    if best is not None:
        return best
    fb_len = 0
    fb_val: tuple[float, float, float] | None = None
    for key, val in _price_cache.items():
        if len(key) < 18:
            continue
        if not (tail in key or key in tail or key in model or model in key):
            continue
        if len(key) > fb_len:
            fb_len, fb_val = len(key), val
    return fb_val


def _cache_read_vs_input_price_multiplier() -> float:
    """未配置 cache_read ￥/M 时，用 input 价 × 该倍率估算 cache read（与 Anthropic 低价读缓存同量级）。

    环境变量 DISPATCHER_CACHE_READ_INPUT_PRICE_RATIO：默认 0.1；设为 1 或 off 则 cache 按 input 同价（旧行为、易高估）。
    """
    raw = os.environ.get("DISPATCHER_CACHE_READ_INPUT_PRICE_RATIO", "0.1").strip().lower()
    if raw in ("", "1", "off", "disable"):
        return 1.0
    return float(raw)


def calc_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    *,
    cache_creation_price_per_mtok: float | None = None,
) -> float:
    """按 ￥/M（与库内配置一致）估算成本；cache read 可单独配置，否则用倍率×input。

    DISPATCHER_PROMPT_COST_MODE：
    - split（默认）：prompt 总量扣命中后 fresh×input价 + 命中×cache_read 价（与 Anthropic/OpenAI 常见「不重复计命中」一致）。
    - additive_cn：提示全量×input + 命中×cache_read + 缓存创建×创建价（与部分国内网关账单明细一致）。
    """
    triple = _resolve_model_prices(model)
    if not triple:
        return 0.0
    p_in, p_out, p_cache = triple[0], triple[1], triple[2]
    ir = max(0, cache_read_tokens)
    ip = max(0, input_tokens)
    cc = max(0, cache_creation_tokens)
    p_cc = (
        float(cache_creation_price_per_mtok)
        if cache_creation_price_per_mtok is not None and cache_creation_price_per_mtok >= 0
        else _cache_creation_price_per_mtok(p_in)
    )
    mode = _prompt_cost_mode()
    if mode == "additive_cn":
        p_cr = p_cache if p_cache > 0 else p_in * _cache_read_vs_input_price_multiplier()
        input_side = ip * p_in + ir * p_cr + cc * p_cc
    else:
        # OpenAI：prompt_tokens 含命中部分，cache_read ≤ prompt。Anthropic 桥：input_tokens 常仅为未缓存段，
        # cache_read_input_tokens 另计且可能大于 input_tokens，此时不得用 min 把命中截断为 0。
        if ir > ip:
            fresh, cache = ip, ir
        else:
            cache = min(ir, ip)
            fresh = max(0, ip - cache)
        if cache > 0:
            if p_cache > 0:
                input_side = fresh * p_in + cache * p_cache
            else:
                mult = _cache_read_vs_input_price_multiplier()
                input_side = fresh * p_in + cache * p_in * mult
        else:
            input_side = input_tokens * p_in
        if cc > 0 and os.getenv("DISPATCHER_SPLIT_ADD_CACHE_CREATION", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            input_side += cc * p_cc
    return (input_side + output_tokens * p_out) / 1_000_000


def _qualified_model(model: str) -> str:
    """将纯模型名补全为 provider/model 格式，用于统计区分不同供应商"""
    if "/" in model:
        return model
    from app.services.model_pool import _model_to_provider
    provider = _model_to_provider.get(model)
    return f"{provider}/{model}" if provider else model


def apply_platform_billing_markup(
    base_cost: float, credential_source: str, cost_multiplier: float
) -> float:
    """byok：日志成本即估算，不乘倍率。platform（托管）：成本 × cost_multiplier（可小于 1，与上游分组折扣一致）；<=0 按 1 防异常。

    cost_multiplier 为运营自设，须与「本库￥/M 所表示的基准」及上游账单同一语义：若￥/M 已是美元官方价×汇率后的等价，
    而上游对「官方 1.7 倍」另有 ÷7 等归一，则此处应填与对账一致的整体系数（常见约 1.7/7），或调低￥/M，避免重复折算。
    """
    src = (credential_source or "byok").strip().lower()
    if src != "platform":
        return base_cost
    m = float(cost_multiplier or 1.0)
    if m <= 0:
        m = 1.0
    return base_cost * m


async def _resolve_provider_billing_row(
    session: AsyncSession, model: str
) -> tuple[str, float] | None:
    """返回 (credential_source, cost_multiplier)；无匹配配置时 None。"""
    from app.models import ModelConfig, ModelProvider

    qualified = _qualified_model(model)
    if "/" in qualified:
        prov_hint, tail = qualified.split("/", 1)
        prov_hint, tail = prov_hint.strip(), tail.strip()
    else:
        prov_hint, tail = None, qualified.strip()

    stmt = (
        select(ModelProvider.credential_source, ModelProvider.cost_multiplier)
        .join(ModelConfig, ModelConfig.provider_id == ModelProvider.id)
        .where(
            ModelConfig.model_name == tail,
            ModelConfig.enabled == True,  # noqa: E712
            ModelProvider.enabled == True,  # noqa: E712
        )
    )
    if prov_hint:
        stmt = stmt.where(ModelProvider.name == prov_hint)
    stmt = stmt.order_by(ModelProvider.name).limit(1)
    row = (await session.execute(stmt)).first()
    if not row:
        return None
    return (str(row[0] or "byok"), float(row[1] or 1.0))


async def record(
    session: AsyncSession,
    *,
    model: str,
    caller: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    duration_ms: int = 0,
    project_id: str | None = None,
    task_id: str | None = None,
    metadata: dict | None = None,
):
    pid = project_id or _current_project_id.get()
    tid = task_id or _current_task_id.get()
    qualified = _qualified_model(model)
    base = calc_cost(
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )
    meta = dict(metadata or {})
    if cache_creation_tokens > 0:
        meta["cache_creation_tokens"] = cache_creation_tokens

    row = await _resolve_provider_billing_row(session, model)
    if row:
        csrc, cmult = row
        cost = apply_platform_billing_markup(base, csrc, cmult)
        meta["billing"] = {
            "credential_source": csrc,
            "base_cost_est": round(base, 8),
            "platform_multiplier": cmult if csrc == "platform" else 1.0,
        }
    else:
        cost = base

    log = TokenUsageLog(
        project_id=pid, task_id=tid,
        caller=caller, model=qualified,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost, duration_ms=duration_ms,
        metadata_=meta,
    )
    session.add(log)
    await session.commit()
    return log


async def record_from_webhook(
    session: AsyncSession,
    *,
    project_id: str | None,
    task_id: str | None,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    total_cost_usd: float = 0.0,
    duration_ms: int = 0,
):
    """从 webhook 上报的 token 数据记录"""
    qualified = _qualified_model(model)
    cost = total_cost_usd or calc_cost(
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )
    log = TokenUsageLog(
        project_id=project_id, task_id=task_id,
        caller="agent", model=qualified,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost_usd=cost, duration_ms=duration_ms,
    )
    session.add(log)
    await session.commit()


# ── 统计查询 ──

async def project_cost_summary(session: AsyncSession, project_id: str) -> dict:
    """项目级成本汇总"""
    q = await session.execute(
        select(
            TokenUsageLog.model,
            func.count().label("calls"),
            func.sum(TokenUsageLog.input_tokens).label("input_tokens"),
            func.sum(TokenUsageLog.output_tokens).label("output_tokens"),
            func.sum(TokenUsageLog.cost_usd).label("cost_usd"),
        )
        .where(TokenUsageLog.project_id == project_id)
        .group_by(TokenUsageLog.model)
    )
    by_model = {}
    total_cost = 0.0
    total_input = 0
    total_output = 0
    for row in q:
        by_model[row.model] = {
            "calls": row.calls,
            "input_tokens": row.input_tokens or 0,
            "output_tokens": row.output_tokens or 0,
            "cost_usd": round(row.cost_usd or 0, 4),
        }
        total_cost += row.cost_usd or 0
        total_input += row.input_tokens or 0
        total_output += row.output_tokens or 0

    return {
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_calls": sum(m["calls"] for m in by_model.values()),
        "by_model": by_model,
    }


async def project_cost_daily(session: AsyncSession, project_id: str) -> list[dict]:
    """按天的成本趋势"""
    q = await session.execute(text("""
        SELECT date_trunc('day', created_at)::date AS day,
               model,
               count(*) AS calls,
               sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens,
               sum(cost_usd) AS cost_usd
        FROM token_usage_logs
        WHERE project_id = :pid
        GROUP BY day, model
        ORDER BY day
    """), {"pid": project_id})

    results = []
    for row in q:
        results.append({
            "date": row.day.isoformat(),
            "model": row.model,
            "calls": row.calls,
            "input_tokens": row.input_tokens or 0,
            "output_tokens": row.output_tokens or 0,
            "cost_usd": round(row.cost_usd or 0, 4),
        })
    return results


async def project_cost_hourly(session: AsyncSession, project_id: str) -> list[dict]:
    """按小时+模型粒度的成本/调用统计（用于趋势图和消耗分布图）"""
    q = await session.execute(text("""
        SELECT date_trunc('hour', created_at) AS hour,
               model,
               count(*) AS calls,
               sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens,
               sum(cost_usd) AS cost_usd
        FROM token_usage_logs
        WHERE project_id = :pid
        GROUP BY hour, model
        ORDER BY hour
    """), {"pid": project_id})
    return [
        {
            "hour": row.hour.isoformat(),
            "model": row.model,
            "calls": row.calls,
            "input_tokens": row.input_tokens or 0,
            "output_tokens": row.output_tokens or 0,
            "cost_usd": round(row.cost_usd or 0, 4),
        }
        for row in q
    ]


async def project_cost_by_caller(session: AsyncSession, project_id: str) -> dict:
    """按调用者分类的成本"""
    q = await session.execute(
        select(
            TokenUsageLog.caller,
            func.count().label("calls"),
            func.sum(TokenUsageLog.cost_usd).label("cost_usd"),
        )
        .where(TokenUsageLog.project_id == project_id)
        .group_by(TokenUsageLog.caller)
    )
    return {row.caller: {"calls": row.calls, "cost_usd": round(row.cost_usd or 0, 4)} for row in q}
