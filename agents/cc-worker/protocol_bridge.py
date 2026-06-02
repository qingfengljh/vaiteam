#!/usr/bin/env python3
"""
Anthropic ↔ OpenAI 协议转换桥

运行在 CC Worker 容器内，当 protocol_adapter="openai_via_litellm" 时启动：
- 接受 Claude Code 发来的 Anthropic /v1/messages 请求
- 转换为 OpenAI /v1/chat/completions 格式
- 转发给上游（litellm proxy 或供应商 OpenAI 端点）
- 将 OpenAI 响应转换回 Anthropic 格式返回

支持：非流式 + 流式 (SSE) 响应

Anthropic 流式事件序列：
  message_start → content_block_start → [content_block_delta]* → content_block_stop → message_delta → message_stop
"""

import argparse
import json
import logging
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="[protocol-bridge] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Anthropic-to-OpenAI Protocol Bridge")

# 上游 OpenAI/litellm 端点
UPSTREAM_URL: str = "http://localhost:8000"


def _anthropic_to_openai_messages(anthropic_messages: list[dict]) -> list[dict]:
    """将 Anthropic messages 格式转换为 OpenAI 格式。"""
    openai_msgs = []
    for msg in anthropic_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif isinstance(part, str):
                    texts.append(part)
            content = "\n".join(texts)
        openai_msgs.append({"role": role, "content": content})
    return openai_msgs


def _build_openai_request(anthropic_body: dict) -> dict:
    """构建 OpenAI chat.completions 请求体。"""
    model = anthropic_body.get("model", "gpt-4o")
    max_tokens = anthropic_body.get("max_tokens", 4096)
    temperature = anthropic_body.get("temperature", 1.0)
    stream = anthropic_body.get("stream", False)
    system = anthropic_body.get("system", "")
    # 透传 top_p 等参数
    top_p = anthropic_body.get("top_p", 1.0)

    messages = _anthropic_to_openai_messages(anthropic_body.get("messages", []))
    if system:
        messages.insert(0, {"role": "system", "content": system})

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
        "top_p": top_p,
    }
    # Anthropic 的 thinking 参数映射到 OpenAI reasoning_effort
    thinking = anthropic_body.get("thinking")
    if thinking and isinstance(thinking, dict):
        body["reasoning_effort"] = thinking.get("budget_tokens", 32000)
    return body


def _openai_finish_to_anthropic(finish_reason: str | None) -> str | None:
    """OpenAI finish_reason → Anthropic stop_reason。"""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "content_filter",
        "tool_calls": "tool_use",
    }
    return mapping.get(finish_reason, finish_reason) if finish_reason else None


def _build_anthropic_response(openai_resp: dict, model: str) -> dict:
    """将 OpenAI 非流式响应转换为 Anthropic 格式。"""
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    content_text = message.get("content", "")
    if isinstance(content_text, list):
        texts = [p.get("text", "") for p in content_text if isinstance(p, dict)]
        content_text = "".join(texts)

    usage = openai_resp.get("usage", {})
    stop_reason = _openai_finish_to_anthropic(choice.get("finish_reason"))

    return {
        "id": f"msg_{openai_resp.get('id', uuid.uuid4().hex[:24])}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content_text}],
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ── SSE 流式转换 ──

async def _sse_forward(upstream_resp: httpx.Response, model: str) -> StreamingResponse:
    """将 OpenAI SSE 流转换为 Anthropic SSE 流。"""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    input_tokens = 0
    output_tokens = 0
    stop_reason: str | None = None
    seen_content = False
    block_index = 0

    async def generator():
        nonlocal input_tokens, output_tokens, stop_reason, seen_content, block_index

        # 1. message_start
        yield _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        # 2. content_block_start
        yield _sse_event("content_block_start", {
            "type": "content_block_start",
            "index": block_index,
            "content_block": {"type": "text", "text": ""},
        })

        # 3. 处理 OpenAI chunk 流
        async for line in upstream_resp.aiter_lines():
            if not line.strip():
                continue
            if not line.startswith("data: "):
                continue

            data = line[6:]
            if data == "[DONE]":
                break

            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            finish = choice.get("finish_reason")
            usage_chunk = chunk.get("usage")

            # 累加 usage
            if usage_chunk:
                input_tokens = usage_chunk.get("prompt_tokens", input_tokens)
                output_tokens = usage_chunk.get("completion_tokens", output_tokens)

            # 文本增量
            content = delta.get("content", "")
            if content:
                seen_content = True
                yield _sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": {"type": "text_delta", "text": content},
                })

            # finish_reason
            if finish:
                stop_reason = _openai_finish_to_anthropic(finish)

        # 4. content_block_stop
        yield _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": block_index,
        })

        # 5. message_delta (stop_reason + usage)
        yield _sse_event("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "usage": {
                    "output_tokens": output_tokens,
                },
            },
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        })

        # 6. message_stop
        yield _sse_event("message_stop", {
            "type": "message_stop",
        })

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _sse_event(event_type: str, data: dict | None = None) -> str:
    """构造 SSE 事件行。"""
    lines = [f"event: {event_type}"]
    if data is not None:
        lines.append(f"data: {json.dumps(data)}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ── HTTP 端点 ──

@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic /v1/messages 端点：转发到上游 OpenAI 端点。"""
    anthropic_body = await request.json()
    model = anthropic_body.get("model", "gpt-4o")
    stream = anthropic_body.get("stream", False)

    openai_body = _build_openai_request(anthropic_body)
    headers = {
        "Content-Type": "application/json",
        "Authorization": request.headers.get("Authorization", ""),
        "x-api-key": request.headers.get("x-api-key", ""),
    }

    logger.info(f"Forwarding request: model={model}, stream={stream}")

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            upstream_resp = await client.post(
                f"{UPSTREAM_URL}/v1/chat/completions",
                json=openai_body,
                headers=headers,
            )
        except httpx.RequestError as e:
            logger.error(f"Upstream request failed: {e}")
            return Response(
                content=json.dumps({"error": {"type": "api_error", "message": str(e)}}),
                status_code=502,
                media_type="application/json",
            )

    if upstream_resp.status_code != 200:
        logger.error(f"Upstream error: {upstream_resp.status_code} {upstream_resp.text[:200]}")
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type="application/json",
        )

    if stream:
        return await _sse_forward(upstream_resp, model)

    openai_data = upstream_resp.json()
    anthropic_data = _build_anthropic_response(openai_data, model)
    return Response(
        content=json.dumps(anthropic_data),
        status_code=200,
        media_type="application/json",
    )


@app.get("/health")
async def health():
    return {"status": "ok", "upstream": UPSTREAM_URL}


def main():
    parser = argparse.ArgumentParser(description="Anthropic-to-OpenAI Protocol Bridge")
    parser.add_argument("--upstream", default="http://localhost:8000", help="Upstream OpenAI/litellm URL")
    parser.add_argument("--port", type=int, default=8001, help="Listen port")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host")
    args = parser.parse_args()

    global UPSTREAM_URL
    UPSTREAM_URL = args.upstream.rstrip("/")
    logger.info(f"Starting protocol bridge on {args.host}:{args.port} -> upstream {UPSTREAM_URL}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
