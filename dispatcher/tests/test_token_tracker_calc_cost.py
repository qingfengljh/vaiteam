from app.services import token_tracker as tt


def test_apply_platform_billing_markup_byok_unchanged():
    assert tt.apply_platform_billing_markup(0.01, "byok", 1.5) == 0.01
    assert tt.apply_platform_billing_markup(0.01, "BYOK", 2.0) == 0.01


def test_apply_platform_billing_markup_platform_fractions():
    assert tt.apply_platform_billing_markup(0.01, "platform", 1.2) == 0.012
    assert tt.apply_platform_billing_markup(0.01, "platform", 0.5) == 0.005
    assert tt.apply_platform_billing_markup(1.0, "platform", 0.75) == 0.75
    assert tt.apply_platform_billing_markup(1.0, "platform", 0.45) == 0.45
    assert tt.apply_platform_billing_markup(0.01, "platform", 0) == 0.01


class _NS:
    __slots__ = ("cached_tokens",)

    def __init__(self, cached_tokens: int):
        self.cached_tokens = cached_tokens


def test_extract_cache_read_dict():
    u = {"prompt_tokens_details": {"cached_tokens": 1200}}
    assert tt.extract_cache_read_tokens_from_usage(u) == 1200


def test_extract_cache_read_sdk_object():
    u = {"prompt_tokens_details": _NS(900)}
    assert tt.extract_cache_read_tokens_from_usage(u) == 900


def test_extract_cache_read_top_level_alias():
    u = {"prompt_cache_hit_tokens": 333}
    assert tt.extract_cache_read_tokens_from_usage(u) == 333


def test_extract_cache_read_claude_top_level_first():
    u = {
        "prompt_tokens": 100_000,
        "prompt_tokens_details": {"cached_tokens": 1},
        "cache_read_input_tokens": 90_000,
    }
    assert tt.extract_cache_read_tokens_from_usage(u) == 90_000


def test_extract_prompt_tokens_anthropic_sum():
    u = {"input_tokens": 10_000, "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 5_000}
    assert tt.extract_prompt_tokens_for_billing(u) == 105_000


def test_calc_cost_cache_read_exceeds_prompt_uncached_branch():
    tt._price_cache.clear()
    tt._price_cache["m"] = (10.0, 20.0, 1.0)
    try:
        # 未缓存 1 万 + 命中 9 万 @1 ￥/M，输出 0
        c = tt.calc_cost("m", 10_000, 0, cache_read_tokens=90_000)
        assert abs(c - (0.01 * 10.0 + 0.09 * 1.0)) < 1e-9
    finally:
        tt._price_cache.clear()


def test_calc_cost_exact_model_not_short_prefix():
    tt._price_cache.clear()
    tt._price_cache["claude-opus"] = (999.0, 999.0, 0.0)
    tt._price_cache["claude-opus-4-6"] = (5.0, 25.0, 0.0)
    try:
        c = tt.calc_cost("claude-opus-4-6", 1_000_000, 1_000_000)
        assert c == 5.0 + 25.0
    finally:
        tt._price_cache.clear()


def test_calc_cost_qualified_suffix():
    tt._price_cache.clear()
    tt._price_cache["p/claude-opus-4-6"] = (3.0, 30.0, 0.0)
    try:
        c = tt.calc_cost("p/claude-opus-4-6", 1_000_000, 0)
        assert c == 3.0
    finally:
        tt._price_cache.clear()


def test_calc_cost_separate_cache_read_price():
    tt._price_cache.clear()
    tt._price_cache["opus"] = (10.0, 20.0, 1.0)
    try:
        # 500k fresh input + 500k cache read @ 1 ￥/M + 0 output
        c = tt.calc_cost("opus", 1_000_000, 0, cache_read_tokens=500_000)
        assert abs(c - (0.5 * 10.0 + 0.5 * 1.0)) < 1e-6
    finally:
        tt._price_cache.clear()


def test_extract_cache_creation_claude_fields():
    u = {"claude_cache_creation_5_m_tokens": 100, "claude_cache_creation_1_h_tokens": 270}
    assert tt.extract_cache_creation_tokens_from_usage(u) == 370


def test_extract_cache_creation_top_level():
    u = {"cache_creation_input_tokens": 42}
    assert tt.extract_cache_creation_tokens_from_usage(u) == 42


def test_calc_cost_additive_cn_matches_gateway_bill(monkeypatch):
    monkeypatch.setenv("DISPATCHER_PROMPT_COST_MODE", "additive_cn")
    tt._price_cache.clear()
    tt._price_cache["m"] = (5.0, 25.0, 0.5)
    try:
        c = tt.calc_cost("m", 1435, 10, 248, 370)
        assert abs(c - 0.0098615) < 1e-9
    finally:
        tt._price_cache.clear()
        monkeypatch.delenv("DISPATCHER_PROMPT_COST_MODE", raising=False)


def test_calc_cost_additive_cn_only_creation_no_cache_read(monkeypatch):
    """xingjiabi 样例：仅有提示 + 缓存创建 + 补全（无缓存命中行）。"""
    monkeypatch.setenv("DISPATCHER_PROMPT_COST_MODE", "additive_cn")
    tt._price_cache.clear()
    tt._price_cache["m"] = (5.0, 25.0, 0.5)
    try:
        c = tt.calc_cost("m", 1819, 10, 0, 234)
        assert abs(c - 0.0108075) < 1e-9
    finally:
        tt._price_cache.clear()
        monkeypatch.delenv("DISPATCHER_PROMPT_COST_MODE", raising=False)


def test_calc_cost_additive_cn_prompt_cache_and_creation(monkeypatch):
    """xingjiabi 样例：提示 + 缓存命中 + 缓存创建 + 补全。"""
    monkeypatch.setenv("DISPATCHER_PROMPT_COST_MODE", "additive_cn")
    tt._price_cache.clear()
    tt._price_cache["m"] = (5.0, 25.0, 0.5)
    try:
        c = tt.calc_cost("m", 1361, 10, 425, 267)
        assert abs(c - 0.00893625) < 1e-9
    finally:
        tt._price_cache.clear()
        monkeypatch.delenv("DISPATCHER_PROMPT_COST_MODE", raising=False)
