"""性价比类 OpenAI 兼容网关：实调一条 chat，校验 usage 可被 token_tracker 解析。

密钥只能通过环境变量注入（勿写入本文件或 git）。API Key 与 probe 脚本共用优先级（设其一即可）：

  XINGJIABI_INTEGRATION_API_KEY → PROBE_OPENAI_API_KEY
  （不回退 OPENAI_API_KEY，避免与本机全局官方 OpenAI 配置混用）

  Base URL：XINGJIABI_INTEGRATION_BASE_URL → PROBE_OPENAI_BASE_URL
  默认 https://xingjiabiapi.org/v1

  Model：XINGJIABI_INTEGRATION_MODEL → PROBE_MODEL（默认 claude-opus-4-6，与 /v1/models 常见测试 Key 一致）

  cd dispatcher && pytest tests/test_xingjiabi_gateway_usage_integration.py -m integration -q

未设置任一 Key 时测试会自动 skip，CI 默认不跑外网。
"""
from __future__ import annotations

import os

import pytest

from app.services import token_tracker as tt

pytestmark = pytest.mark.integration


def _env_api_key() -> str | None:
    return os.environ.get("XINGJIABI_INTEGRATION_API_KEY") or os.environ.get(
        "PROBE_OPENAI_API_KEY"
    )


def _env_base_url() -> str:
    return (
        os.environ.get("XINGJIABI_INTEGRATION_BASE_URL")
        or os.environ.get("PROBE_OPENAI_BASE_URL")
        or "https://xingjiabiapi.org/v1"
    ).rstrip("/")


def _env_model() -> str:
    return (
        os.environ.get("XINGJIABI_INTEGRATION_MODEL")
        or os.environ.get("PROBE_MODEL")
        or "claude-opus-4-6"
    )


def _usage_to_dict(usage) -> dict:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    md = getattr(usage, "model_dump", None)
    if callable(md):
        return md()
    return {}


@pytest.mark.skipif(
    not _env_api_key(),
    reason="未设置 XINGJIABI_INTEGRATION_API_KEY 或 PROBE_OPENAI_API_KEY，跳过实网",
)
def test_xingjiabi_chat_usage_shape_and_extractors():
    from openai import APIStatusError, OpenAI

    api_key = _env_api_key() or ""
    base = _env_base_url()
    model = _env_model()

    client = OpenAI(api_key=api_key, base_url=base)
    try:
        comp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
            max_tokens=8,
        )
    except APIStatusError as e:
        sc = getattr(e, "status_code", None)
        if sc == 403:
            pytest.skip(
                f"当前密钥无权使用该模型（model={model}）。请在 ~/.zshenv 设置 "
                f"PROBE_MODEL 或 XINGJIABI_INTEGRATION_MODEL 为账户可用模型。原始错误: {e}"
            )
        if sc == 503:
            pytest.skip(
                f"网关暂无该模型可用渠道（model={model}）。请到性价比控制台检查分组/上游线路。原始错误: {e}"
            )
        raise
    usage = _usage_to_dict(comp.usage)
    assert usage, "响应应包含 usage"

    pr = tt.extract_prompt_tokens_for_billing(usage)
    cr = tt.extract_cache_read_tokens_from_usage(usage)
    co = tt.extract_completion_tokens_from_usage(usage)

    assert pr >= 0 and co >= 0
    assert cr >= 0
    # 至少应有 prompt 或（Anthropic 形）拆字段之一
    assert pr > 0 or cr > 0 or int(usage.get("input_tokens") or 0) > 0
