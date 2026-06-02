#!/usr/bin/env python3
"""对任意 OpenAI 兼容网关发一条极短 chat，打印原始 usage 与 token_tracker 解析结果。

勿在聊天/仓库中粘贴 API Key。本机：

  cd dispatcher && source .venv/bin/activate   # 或你的 venv
  # Key 三选一：PROBE_OPENAI_API_KEY、XINGJIABI_INTEGRATION_API_KEY、OPENAI_API_KEY
  export PROBE_OPENAI_API_KEY='你的key'
  # Base：PROBE / XINGJIABI / 默认性价比（不读 OPENAI_BASE_URL，避免与官方端点混用）
  export PROBE_OPENAI_BASE_URL='https://xingjiabiapi.org/v1'       # 以文档为准，可改
  export PROBE_MODEL='claude-opus-4-6'                          # 性价比测试 Key 常见仅开通此模型
  # 可选：input,output,cache_read（￥/M）；或与网关一致再加第 4 项 cache_creation（￥/M）
  export PROBE_PRICE_CNY_PER_M='5,25,0.5,6.25'
  # 与性价比账单对齐时用 additive_cn（dispatcher 生产环境用同名环境变量）
  export PROBE_PROMPT_COST_MODE='additive_cn'

  python scripts/probe_gateway_usage.py
"""
from __future__ import annotations

import json
import os
import sys


def _dispatcher_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _usage_to_dict(usage) -> dict:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    md = getattr(usage, "model_dump", None)
    if callable(md):
        return md()
    return {}


def main() -> int:
    root = _dispatcher_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    api_key = (
        os.getenv("PROBE_OPENAI_API_KEY")
        or os.getenv("XINGJIABI_INTEGRATION_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    # 不用 OPENAI_BASE_URL：本机常为官方 api.openai.com，与性价比 Key 组合会 401
    base_url = (
        os.getenv("PROBE_OPENAI_BASE_URL")
        or os.getenv("XINGJIABI_INTEGRATION_BASE_URL")
        or "https://xingjiabiapi.org/v1"
    ).rstrip("/")
    model = (
        os.getenv("PROBE_MODEL")
        or os.getenv("XINGJIABI_INTEGRATION_MODEL")
        or "claude-opus-4-6"
    )

    if not api_key:
        print(
            "请设置 API Key：PROBE_OPENAI_API_KEY 或 XINGJIABI_INTEGRATION_API_KEY 或 OPENAI_API_KEY",
            file=sys.stderr,
        )
        return 2

    try:
        from openai import OpenAI
    except ImportError:
        print("需要 openai 包：在 dispatcher 目录下激活 venv 后重试", file=sys.stderr)
        return 2

    from app.services import token_tracker as tt

    client = OpenAI(api_key=api_key, base_url=base_url)
    comp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        max_tokens=8,
    )
    usage = _usage_to_dict(comp.usage)
    print("=== model / base_url ===")
    print(json.dumps({"model": model, "base_url": base_url}, ensure_ascii=False, indent=2))
    print("\n=== raw usage (JSON) ===")
    print(json.dumps(usage, ensure_ascii=False, indent=2, default=str))

    pr = tt.extract_prompt_tokens_for_billing(usage)
    cr = tt.extract_cache_read_tokens_from_usage(usage)
    co = tt.extract_completion_tokens_from_usage(usage)
    cc = tt.extract_cache_creation_tokens_from_usage(usage)
    print("\n=== token_tracker extractors ===")
    print(
        json.dumps(
            {
                "prompt_tokens_for_billing": pr,
                "cache_read_tokens": cr,
                "cache_creation_tokens": cc,
                "completion_tokens": co,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    raw_price = os.getenv("PROBE_PRICE_CNY_PER_M", "").strip()
    if raw_price:
        parts = [p.strip() for p in raw_price.split(",")]
        if len(parts) in (3, 4):
            p_in, p_out, p_cache = map(float, parts[:3])
            p_cc_override = float(parts[3]) if len(parts) == 4 else None
            tt._price_cache.clear()
            tt._price_cache[model] = (p_in, p_out, p_cache)
            mode_prev = os.environ.get("DISPATCHER_PROMPT_COST_MODE")
            mode_probe = os.getenv("PROBE_PROMPT_COST_MODE", "additive_cn")
            os.environ["DISPATCHER_PROMPT_COST_MODE"] = mode_probe
            try:
                cost = tt.calc_cost(
                    model,
                    pr,
                    co,
                    cache_read_tokens=cr,
                    cache_creation_tokens=cc,
                    cache_creation_price_per_mtok=p_cc_override,
                )
            finally:
                if mode_prev is None:
                    os.environ.pop("DISPATCHER_PROMPT_COST_MODE", None)
                else:
                    os.environ["DISPATCHER_PROMPT_COST_MODE"] = mode_prev
            print("\n=== calc_cost (￥, PROBE_PRICE + DISPATCHER_PROMPT_COST_MODE) ===")
            _mr = os.getenv("DISPATCHER_CACHE_READ_INPUT_PRICE_RATIO", "0.1").strip().lower()
            _m = 1.0 if _mr in ("", "1", "off", "disable") else float(_mr)
            p_cr_eff = p_cache if p_cache > 0 else p_in * _m
            p_cc_eff = (
                p_cc_override
                if p_cc_override is not None
                else p_in * float(os.getenv("DISPATCHER_CACHE_CREATION_INPUT_MULT", "1.25"))
            )
            print(
                json.dumps(
                    {
                        "cost_cny": cost,
                        "prompt_cost_mode": mode_probe,
                        "breakdown_additive_cn_hint": {
                            "prompt_at_input_rate": pr * p_in / 1e6,
                            "cache_read_at_effective_rate": cr * p_cr_eff / 1e6,
                            "cache_creation_at_effective_rate": cc * p_cc_eff / 1e6,
                            "completion_at_output_rate": co * p_out / 1e6,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            tt._price_cache.clear()
        else:
            print(
                "\nPROBE_PRICE_CNY_PER_M 应为 3 或 4 个数：input,output,cache_read[,cache_creation]（￥/M）",
                file=sys.stderr,
            )
    else:
        print("\n（未设置 PROBE_PRICE_CNY_PER_M，跳过试算；生产环境单价来自数据库）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
