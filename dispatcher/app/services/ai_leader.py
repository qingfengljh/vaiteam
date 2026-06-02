"""
AI Leader - 编排系统的核心 AI 能力

所有模型配置统一走 model_pool（数据库供应商 + 角色映射），不依赖 .env。
"""

import asyncio
import json
import logging
import re
import time
import ast
from collections.abc import AsyncIterator
from openai import AsyncOpenAI, APIStatusError, APIConnectionError, APITimeoutError, RateLimitError, AuthenticationError

from app.core.doc_time import doc_time_prompt_block
from app.services import model_pool

logger = logging.getLogger(__name__)

# 重试配置
MAX_RETRIES = 5
MAX_GATEWAY_RETRIES = 2   # gateway 504/502 最多重试 2 次，连续失败说明代理不可用
MAX_CONTINUATIONS = 3     # 截断续写最多追加次数（通用）
MAX_DOC_CONTINUATION_ROUNDS = 8  # 阶段正式文档：长文 + 代理易 length，多续几轮
MAX_DOC_STAGE2_CONTINUATION_ROUNDS = 14  # Stage 2：逐页表 + 第 4 章 + JSON，length 截断时多续几轮
BASE_DELAY = 1.0          # 初始退避（秒）
MAX_DELAY = 60.0          # 最大退避（秒）
BACKOFF_FACTOR = 2.0

# ── 供应商熔断器 ──
_CIRCUIT_BREAKER_THRESHOLD = 3       # 连续 fatal N 次触发熔断
_CIRCUIT_BREAKER_COOLDOWN = 300.0    # 熔断冷却期（秒）
_circuit_failures: dict[str, int] = {}        # api_base → 连续失败次数
_circuit_open_until: dict[str, float] = {}    # api_base → 熔断解除时间戳


class CircuitOpenError(Exception):
    """供应商被熔断，拒绝请求"""


def _circuit_breaker_check(api_base: str):
    """调用前检查熔断状态，如果熔断中则抛异常"""
    open_until = _circuit_open_until.get(api_base, 0)
    if open_until > time.monotonic():
        remaining = int(open_until - time.monotonic())
        raise CircuitOpenError(
            f"供应商 {api_base} 已熔断（连续 {_CIRCUIT_BREAKER_THRESHOLD} 次 fatal），"
            f"剩余 {remaining}s 后自动恢复"
        )
    elif open_until > 0:
        _circuit_open_until.pop(api_base, None)
        _circuit_failures.pop(api_base, None)
        logger.info(f"Circuit breaker reset for {api_base}")


def _circuit_breaker_record_success(api_base: str):
    _circuit_failures.pop(api_base, None)


def _circuit_breaker_record_fatal(api_base: str):
    count = _circuit_failures.get(api_base, 0) + 1
    _circuit_failures[api_base] = count
    if count >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open_until[api_base] = time.monotonic() + _CIRCUIT_BREAKER_COOLDOWN
        logger.error(
            f"Circuit breaker OPEN for {api_base}: {count} consecutive fatal errors, "
            f"blocking requests for {_CIRCUIT_BREAKER_COOLDOWN}s"
        )


def _classify_error(e: Exception) -> str:
    """分类错误类型，决定重试策略"""
    if isinstance(e, AuthenticationError):
        return "fatal"
    if isinstance(e, APIStatusError):
        code = e.status_code
        msg = str(e).lower()
        if code == 401 or code == 403:
            return "fatal"
        if "insufficient" in msg or "quota" in msg or "balance" in msg or "billing" in msg:
            return "fatal"
        if code == 429:
            return "rate_limit"
        if code in (502, 504):
            return "gateway"
        if code >= 500:
            return "transient"
        if code == 408:
            return "transient"
        return "fatal"
    if isinstance(e, (APIConnectionError, APITimeoutError, ConnectionError, TimeoutError, OSError)):
        return "transient"
    if isinstance(e, RateLimitError):
        return "rate_limit"
    return "unknown"


async def _retry_delay(attempt: int, error_type: str, retry_after: float | None = None):
    """计算并执行退避等待"""
    if retry_after and retry_after > 0:
        delay = min(retry_after, MAX_DELAY)
    elif error_type == "rate_limit":
        delay = min(BASE_DELAY * (BACKOFF_FACTOR ** attempt) * 2, MAX_DELAY)
    elif error_type == "gateway":
        delay = min(10.0 * (BACKOFF_FACTOR ** attempt), 120.0)
    else:
        delay = min(BASE_DELAY * (BACKOFF_FACTOR ** attempt), MAX_DELAY)
    logger.info(f"Retry after {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES}, type={error_type})")
    await asyncio.sleep(delay)


async def _track_usage(model: str, usage, duration_ms: int, caller: str = "leader"):
    """非阻塞记录 token 消耗"""
    if not usage:
        return
    def _uget(obj, key: str, default=0):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    try:
        from app.core.database import async_session
        from app.services import token_tracker
        async with async_session() as session:
            await token_tracker.record(
                session, model=model, caller=caller,
                input_tokens=token_tracker.extract_prompt_tokens_for_billing(usage),
                output_tokens=token_tracker.extract_completion_tokens_from_usage(usage),
                cache_read_tokens=token_tracker.extract_cache_read_tokens_from_usage(usage),
                cache_creation_tokens=token_tracker.extract_cache_creation_tokens_from_usage(
                    usage
                ),
                duration_ms=duration_ms,
            )
    except Exception as e:
        logger.debug(f"Token tracking failed (non-blocking): {e}")


def _extract_text_and_finish(resp) -> tuple[str, str, object | None]:
    """兼容不同代理返回格式：OpenAI对象 / dict / 纯字符串。"""
    if isinstance(resp, str):
        return resp, "stop", None

    usage = None
    if isinstance(resp, dict):
        usage = resp.get("usage")
        choices = resp.get("choices") or []
        if choices:
            first = choices[0] or {}
            message = first.get("message") or {}
            content = message.get("content") or ""
            finish = first.get("finish_reason") or "stop"
            return (content if isinstance(content, str) else str(content)), finish, usage
        return str(resp), "stop", usage

    usage = getattr(resp, "usage", None)
    choices = getattr(resp, "choices", None) or []
    if choices:
        first = choices[0]
        message = getattr(first, "message", None)
        content = getattr(message, "content", "") if message else ""
        finish = getattr(first, "finish_reason", None) or "stop"
        return (content if isinstance(content, str) else str(content)), finish, usage

    return str(resp), "stop", usage

def _resolve_client(model: str | None = None) -> tuple[AsyncOpenAI, str]:
    """统一从 model_pool 解析 client。优先用指定模型，否则用 leader 角色的默认模型。"""
    target = model or model_pool.resolve_model("leader")
    return model_pool.get_client(target)


_DEFAULT_MAX_OUTPUT = 8192
# 模型未配置 max_output_tokens 时，Stage 2 等产品长文档请求的兜底上限（避免 16K/32K 被误压到 8K 必截断）
_DOC_LONG_OUTPUT_FALLBACK_CAP = 32768
# Stage 2：stop 后结构仍不完整（缺第 4 章 / JSON 未闭合）时的追加补全次数
MAX_STAGE2_INCOMPLETE_FIXUPS = 6
# 阶段会话流式/非流式：自动续写轮数（单次 max_tokens 用满仍 length 时由服务端衔接，减少用户手动「继续」）
MAX_CHAT_CONTINUATION_ROUNDS = 16
_MIN_MESSAGES_KEEP = 3
_MSG_MAX_CHARS = 50_000      # 单条消息内容上限（约 20K token）
_TOTAL_MAX_CHARS = 200_000   # 所有消息内容总量上限（约 80K token）
# 文档评审：待审正文单独放宽（背景由 build_ai_review_background 已控总长）
_REVIEW_DOCUMENT_BODY_MAX_CHARS = 400_000


def _effective_stage_chat_max_tokens(model: str | None) -> int:
    """阶段侧边会话的 max_tokens：完全使用后台为该模型填写的 max_output_tokens（代理不稳时可照旧填小）；未配置才兜底。"""
    from app.services import model_pool
    name = model_pool.resolve_model(model or "leader")
    lim = model_pool.get_model_params(name).get("max_output_tokens", 0)
    try:
        lim = int(lim)
    except (TypeError, ValueError):
        lim = 0
    if lim > 0:
        return lim
    return _DEFAULT_MAX_OUTPUT


def _total_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content", "")) for m in messages)


def _trim_messages(messages: list[dict]) -> list[dict] | None:
    """上下文超长时多轮截断：先截单条过长消息，再减消息条数，最后截总量。"""
    changed = False

    # 1) 截断每条消息内容到上限
    for m in messages:
        content = m.get("content", "")
        if len(content) > _MSG_MAX_CHARS:
            m["content"] = content[:_MSG_MAX_CHARS] + "\n\n...(内容过长已截断)"
            logger.info(f"Trimmed {m.get('role')} message: {len(content)} -> {_MSG_MAX_CHARS} chars")
            changed = True

    # 2) 减少非 system 消息条数
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) > _MIN_MESSAGES_KEEP and _total_chars(messages) > _TOTAL_MAX_CHARS:
        keep = max(_MIN_MESSAGES_KEEP, len(non_system) // 2)
        non_system = non_system[-keep:]
        logger.info(f"Trimmed messages to {keep} recent non-system")
        changed = True

    # 3) 如果仍然超总量，进一步截断最长的消息
    result = system + non_system
    if _total_chars(result) > _TOTAL_MAX_CHARS:
        sorted_by_len = sorted(result, key=lambda m: len(m.get("content", "")), reverse=True)
        for m in sorted_by_len:
            content = m.get("content", "")
            if len(content) > _MSG_MAX_CHARS // 2:
                target = _MSG_MAX_CHARS // 2
                m["content"] = content[:target] + "\n\n...(内容过长已截断)"
                logger.info(f"Further trimmed {m.get('role')} message: {len(content)} -> {target} chars")
                changed = True
                if _total_chars(result) <= _TOTAL_MAX_CHARS:
                    break

    return result if changed else None


def _cap_max_tokens_for_model(actual_model: str, requested: int, *, doc_long_output: bool = False) -> int:
    p = model_pool.get_model_params(actual_model) or {}
    lim = p.get("max_output_tokens", 0) or 0
    if not lim:
        _, bare = model_pool.parse_provider_model(actual_model)
        if bare and bare != actual_model:
            p2 = model_pool.get_model_params(bare) or {}
            lim = p2.get("max_output_tokens", 0) or 0
    try:
        lim = int(lim)
    except (TypeError, ValueError):
        lim = 0
    if lim > 0:
        return min(requested, lim)
    fallback = _DOC_LONG_OUTPUT_FALLBACK_CAP if doc_long_output else _DEFAULT_MAX_OUTPUT
    return min(requested, fallback)


def _stage2_doc_incomplete_reason(text: str | None) -> str | None:
    """产品原型稿硬性结构是否缺失（用于 stop 后补全，与 finish_reason=length 续写互补）。"""
    if not text or not text.strip():
        return "正文为空或仅空白"
    t = text.strip()
    if not re.search(r"^##\s*4[.\s、]", t, re.MULTILINE):
        return "缺少「## 4. 核心交互流程」独立章节（二级标题）"
    low = t.lower()
    if "```json" not in low:
        return "缺少文末 UI Spec 的 ```json 代码块起始标记"
    idx = low.find("```json")
    after = t[idx + 7 :]
    if not re.search(r"\n```\s*(\n|$)", after):
        return "UI Spec ```json 代码块未正常闭合（缺尾部 ```），或 JSON 被截断"
    return None


async def _finalize_stage2_product_doc(
    system: str,
    msgs: list[dict],
    content: str,
    doc_max: int,
    model: str | None,
    doc_cont: int,
) -> str:
    """在模型已 stop 但缺章/JSON 未闭合时追加补全，复杂站点与大型 App 原型仍须可收口。"""
    out = content
    for fix_i in range(MAX_STAGE2_INCOMPLETE_FIXUPS):
        reason = _stage2_doc_incomplete_reason(out)
        if not reason:
            break
        logger.info(f"Stage2 doc incomplete fixup {fix_i + 1}/{MAX_STAGE2_INCOMPLETE_FIXUPS}: {reason}")
        if len(out) <= 80_000:
            assist_body = out
        else:
            assist_body = out[:15_000] + "\n\n...(中段已省略，请结合缺漏说明与尾部衔接)...\n\n" + out[-65_000:]
        fix_user = (
            f"【系统检出】{reason}。\n\n"
            "上文 assistant 消息为**当前已生成文档**（可能含省略）。请**只输出需追加在文末**的补全内容：从截断或缺漏处无缝续写，"
            "直至「## 4. 核心交互流程」完整、且 `` `json` `` 代码块以 `` ` `` 正确闭合；禁止重复已有大段章节与整张表格；禁止写开场白。"
        )
        more = await _call_multi(
            system,
            msgs + [{"role": "assistant", "content": assist_body}, {"role": "user", "content": fix_user}],
            max_tokens=doc_max,
            model=model,
            auto_continue=True,
            temperature=0.35,
            max_continuation_rounds=doc_cont,
            doc_long_output=True,
        )
        out = out.rstrip() + "\n\n" + more.strip()
    still = _stage2_doc_incomplete_reason(out)
    if still:
        logger.warning(f"Stage2 doc still incomplete after fixups: {still}")
    return out


async def _call_with_retry(
    client: AsyncOpenAI, actual_model: str, messages: list[dict],
    max_tokens: int, auto_continue: bool = False, temperature: float | None = None,
    max_continuation_rounds: int | None = None,
    doc_long_output: bool = False,
) -> str:
    """带错误分类和指数退避的 AI 调用，可选自动续写"""
    max_tokens = _cap_max_tokens_for_model(actual_model, max_tokens, doc_long_output=doc_long_output)

    content, finish_reason = await _single_call(client, actual_model, messages, max_tokens, temperature)

    if not auto_continue:
        return content

    cont_cap = max_continuation_rounds if max_continuation_rounds is not None else MAX_CONTINUATIONS
    full_output = content
    cur_messages = list(messages)
    for cont in range(cont_cap):
        if finish_reason != "length":
            break
        logger.info(f"Auto-continue {cont+1}/{cont_cap}: model={actual_model}, accumulated={len(full_output)} chars")
        cur_messages = cur_messages + [
            {"role": "assistant", "content": full_output},
            {"role": "user", "content": "请继续输出，从上次截断处无缝衔接，不要重复已输出的内容，不要加任何过渡语句。"},
        ]
        chunk, finish_reason = await _single_call(client, actual_model, cur_messages, max_tokens, temperature)
        full_output += chunk

    return full_output


async def _single_call(client: AsyncOpenAI, actual_model: str, messages: list[dict], max_tokens: int, temperature: float | None = None) -> tuple[str, str]:
    """单次 AI 调用，带重试。返回 (content, finish_reason)"""
    api_base = str(client.base_url) if hasattr(client, "base_url") else "unknown"
    _circuit_breaker_check(api_base)

    last_error = None
    gateway_count = 0
    for attempt in range(MAX_RETRIES):
        t0 = time.monotonic()
        try:
            kwargs: dict = {"model": actual_model, "max_tokens": max_tokens, "messages": messages}
            if temperature is not None:
                kwargs["temperature"] = temperature
            resp = await client.chat.completions.create(**kwargs)
            content, finish_reason, usage = _extract_text_and_finish(resp)
            elapsed = int((time.monotonic() - t0) * 1000)
            if attempt > 0:
                logger.info(f"AI call succeeded after {attempt} retries: model={actual_model}")
            logger.info(f"AI response: model={actual_model}, {len(content)} chars, {elapsed}ms, finish={finish_reason}")
            await _track_usage(actual_model, usage, elapsed)
            _circuit_breaker_record_success(api_base)
            return content, finish_reason
        except Exception as e:
            last_error = e
            error_type = _classify_error(e)
            msg_lower = str(e).lower()
            logger.warning(f"AI call error: model={actual_model}, type={error_type}, {type(e).__name__}: {e}")

            if error_type == "fatal" and "max_tokens" in msg_lower:
                if max_tokens > 16384:
                    max_tokens = 16384
                elif max_tokens > 8192:
                    max_tokens = 8192
                elif max_tokens > 4096:
                    max_tokens = 4096
                else:
                    max_tokens = 2048
                logger.info(f"max_tokens rejected by API, backoff to {max_tokens} and retry")
                continue

            if error_type == "fatal" and "context length" in msg_lower:
                trimmed = _trim_messages(messages)
                if trimmed is not None:
                    messages = trimmed
                    logger.info(f"Context too long, trimmed to {len(messages)} messages and retry")
                    continue
                logger.error(f"Context too long and cannot trim further: {e}")
                raise

            if error_type == "fatal":
                logger.error(f"Fatal AI error (no retry): {e}")
                _circuit_breaker_record_fatal(api_base)
                raise

            if error_type == "gateway":
                gateway_count += 1
                if gateway_count >= MAX_GATEWAY_RETRIES:
                    logger.error(f"Gateway error {gateway_count} times, giving up: {e}")
                    raise

            if attempt < MAX_RETRIES - 1:
                retry_after = None
                if isinstance(e, APIStatusError) and hasattr(e, "response"):
                    retry_after_str = e.response.headers.get("retry-after")
                    if retry_after_str:
                        try:
                            retry_after = float(retry_after_str)
                        except ValueError:
                            pass
                await _retry_delay(attempt, error_type, retry_after)

    logger.error(f"AI call failed after {MAX_RETRIES} retries: {last_error}")
    raise last_error


async def _call(
    system: str, user_msg: str, max_tokens: int = 4096, model: str | None = None, auto_continue: bool = False,
    temperature: float | None = None, max_continuation_rounds: int | None = None, doc_long_output: bool = False,
) -> str:
    client, actual_model = _resolve_client(model)
    logger.info(f"AI call: model={actual_model}, max_tokens={max_tokens}, prompt_len={len(user_msg)}")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
    return await _call_with_retry(
        client, actual_model, messages, max_tokens,
        auto_continue=auto_continue, temperature=temperature, max_continuation_rounds=max_continuation_rounds,
        doc_long_output=doc_long_output,
    )


async def _call_multi(
    system: str, messages: list[dict], max_tokens: int = 4096, model: str | None = None, auto_continue: bool = False,
    temperature: float | None = None, max_continuation_rounds: int | None = None, doc_long_output: bool = False,
) -> str:
    """多轮消息调用"""
    client, actual_model = _resolve_client(model)
    api_messages = [{"role": "system", "content": system}] + messages
    return await _call_with_retry(
        client, actual_model, api_messages, max_tokens,
        auto_continue=auto_continue, temperature=temperature, max_continuation_rounds=max_continuation_rounds,
        doc_long_output=doc_long_output,
    )


async def _stream_multi(
    system: str, messages: list[dict], max_tokens: int = 4096, model: str | None = None,
    temperature: float | None = None, auto_continue: bool = False,
    max_continue_rounds: int | None = None,
) -> AsyncIterator[str]:
    """多轮消息流式调用，yield 每个 token。连接阶段带重试。可选截断自动续写（与 _call auto_continue 语义一致）。"""
    client, actual_model = _resolve_client(model)
    api_base = str(client.base_url) if hasattr(client, "base_url") else "unknown"
    _circuit_breaker_check(api_base)

    model_limit = model_pool.get_model_params(actual_model).get("max_output_tokens", 0)
    max_tokens = min(max_tokens, model_limit if model_limit > 0 else _DEFAULT_MAX_OUTPUT)

    cur_messages: list[dict] = [{"role": "system", "content": system}] + messages
    cont_cap = max_continue_rounds if max_continue_rounds is not None else MAX_CONTINUATIONS
    logger.info(
        f"AI stream-call: model={actual_model}, msg_count={len(cur_messages)}, "
        f"auto_continue={auto_continue}, max_tokens={max_tokens}, continue_cap={cont_cap}",
    )
    t0 = time.monotonic()
    last_usage = None

    try:
        for round_idx in range(cont_cap + 1):
            kwargs: dict = {
                "model": actual_model, "max_tokens": max_tokens, "messages": cur_messages,
                "stream": True, "stream_options": {"include_usage": True},
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            gateway_count = 0
            stream = None
            for attempt in range(MAX_RETRIES):
                try:
                    stream = await client.chat.completions.create(**kwargs)
                    _circuit_breaker_record_success(api_base)
                    break
                except Exception as e:
                    error_type = _classify_error(e)
                    msg_lower = str(e).lower()
                    if error_type == "fatal" and "context length" in msg_lower:
                        trimmed = _trim_messages(kwargs["messages"])
                        if trimmed is not None:
                            kwargs["messages"] = trimmed
                            cur_messages = kwargs["messages"]
                            logger.info(f"Stream: context too long, trimmed to {len(trimmed)} messages")
                            continue
                    if error_type == "gateway":
                        gateway_count += 1
                        if gateway_count >= MAX_GATEWAY_RETRIES:
                            raise
                    if error_type == "fatal":
                        _circuit_breaker_record_fatal(api_base)
                        raise
                    if attempt >= MAX_RETRIES - 1:
                        raise
                    await _retry_delay(attempt, error_type)

            segment_buf: list[str] = []
            finish_reason: str | None = None
            async for chunk in stream:
                if chunk.usage:
                    last_usage = chunk.usage
                choice0 = chunk.choices[0] if chunk.choices else None
                if choice0:
                    if choice0.finish_reason:
                        finish_reason = choice0.finish_reason
                    delta = choice0.delta
                    if delta and delta.content:
                        segment_buf.append(delta.content)
                        yield delta.content

            if not auto_continue or finish_reason != "length" or round_idx >= cont_cap:
                break
            full_seg = "".join(segment_buf)
            logger.info(
                f"Stream auto-continue {round_idx + 1}/{cont_cap}: model={actual_model}, segment_len={len(full_seg)}",
            )
            cur_messages = cur_messages + [
                {"role": "assistant", "content": full_seg},
                {
                    "role": "user",
                    "content": "请继续输出，从上次截断处无缝衔接，不要重复已输出的内容，不要加任何过渡语句。",
                },
            ]
    finally:
        elapsed = int((time.monotonic() - t0) * 1000)
        if last_usage:
            await _track_usage(actual_model, last_usage, elapsed)


MAX_KNOWLEDGE_LOADS = 2
_SEARCH_RE = re.compile(r"\[SEARCH:(.+?)\]")


async def stream_with_knowledge(
    system: str, messages: list[dict], *,
    project_id: str, session,
    active_stage: int | None = None,
    max_tokens: int = 2048, model: str | None = None, temperature: float | None = None,
    stream_auto_continue: bool = False,
    max_continue_rounds: int | None = None,
) -> AsyncIterator[str]:
    """带按需知识加载的流式对话。

    支持两种标记：
    - [NEED_CONTEXT:key]  按索引 key 加载知识块
    - [SEARCH:查询文本]    用统一检索工具搜索
    """
    from app.services.project_context import extract_need_context_keys, load_knowledge_block
    from app.services.knowledge_search import search_for_context

    collected: list[str] = []
    async for token in _stream_multi(
        system, messages, max_tokens=max_tokens, model=model, temperature=temperature,
        auto_continue=stream_auto_continue, max_continue_rounds=max_continue_rounds,
    ):
        collected.append(token)
        yield token

    full_output = "".join(collected)

    blocks: list[str] = []

    ctx_keys = extract_need_context_keys(full_output)
    for key in ctx_keys[:MAX_KNOWLEDGE_LOADS]:
        block = await load_knowledge_block(session, project_id, key, active_stage=active_stage)
        if block:
            blocks.append(f"[知识块 {key}]\n{block}")

    search_queries = _SEARCH_RE.findall(full_output)
    for query in search_queries[:MAX_KNOWLEDGE_LOADS]:
        result = await search_for_context(session, query.strip(), project_id, limit=3)
        if result:
            blocks.append(result)

    if not blocks:
        return

    knowledge_text = "\n\n---\n\n".join(blocks)
    messages_with_ctx = list(messages)
    messages_with_ctx.append({"role": "assistant", "content": full_output})
    messages_with_ctx.append({"role": "user", "content": f"系统已加载相关知识：\n\n{knowledge_text}\n\n请基于以上信息继续回答。"})

    yield "\n\n---\n*（已加载相关知识，继续回答）*\n\n"
    async for token in _stream_multi(
        system, messages_with_ctx, max_tokens=max_tokens, model=model, temperature=temperature,
        auto_continue=stream_auto_continue, max_continue_rounds=max_continue_rounds,
    ):
        yield token


async def _call_json(system: str, user_msg: str, max_tokens: int = 4096, model: str | None = None, temperature: float | None = None) -> dict:
    def _extract_json_candidate(text: str) -> str:
        s = (text or "").strip()
        fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", s, flags=re.IGNORECASE)
        if fenced:
            return fenced[-1].strip()
        obj_start = s.find("{")
        obj_end = s.rfind("}") + 1
        if obj_start >= 0 and obj_end > obj_start:
            return s[obj_start:obj_end].strip()
        arr_start = s.find("[")
        arr_end = s.rfind("]") + 1
        if arr_start >= 0 and arr_end > arr_start:
            return s[arr_start:arr_end].strip()
        return s

    def _parse_json_relaxed(candidate: str):
        c = (candidate or "").strip()
        if not c:
            raise ValueError("empty json candidate")

        def _auto_close_json(src: str) -> str:
            # 对模型偶发的“括号未闭合”做最小修复，避免直接失败
            stack: list[str] = []
            in_str = False
            escaped = False
            for ch in src:
                if in_str:
                    if escaped:
                        escaped = False
                        continue
                    if ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    stack.append("}")
                elif ch == "[":
                    stack.append("]")
                elif ch in ("}", "]"):
                    if stack and stack[-1] == ch:
                        stack.pop()
            return src + "".join(reversed(stack))

        try:
            return json.loads(c)
        except Exception:
            c2 = re.sub(r",(\s*[}\]])", r"\1", c)
            try:
                return json.loads(c2)
            except Exception:
                c3 = _auto_close_json(c2)
                try:
                    return json.loads(c3)
                except Exception:
                    py_obj = ast.literal_eval(c3)
                    if isinstance(py_obj, (dict, list)):
                        return py_obj
                    raise ValueError("parsed value is not dict/list")

    text = await _call(system, user_msg, max_tokens, model, temperature=temperature)
    candidate = _extract_json_candidate(text)

    try:
        parsed = _parse_json_relaxed(candidate)
        return {"items": parsed} if isinstance(parsed, list) else parsed
    except Exception as first_err:
        repair_prompt = (
            "请将下面内容修复为严格 JSON。"
            "要求：只输出 JSON 本体；不要解释；不要 markdown 代码块；"
            "键和值必须满足 JSON 语法；补全缺失逗号或引号。"
        )
        repaired = await _call(
            "你是 JSON 修复器。",
            f"{repair_prompt}\n\n原始内容：\n{candidate}",
            max_tokens=2048,
            model=model,
            temperature=0,
        )
        repaired_candidate = _extract_json_candidate(repaired)
        try:
            parsed = _parse_json_relaxed(repaired_candidate)
            return {"items": parsed} if isinstance(parsed, list) else parsed
        except Exception as second_err:
            # 最后一跳：让同一系统角色重新生成一次，显式要求严格 JSON
            retry_text = await _call(
                f"{system}\n\n【强约束】你必须只输出合法 JSON，不允许出现任何解释、注释、markdown 或多余文本。",
                f"{user_msg}\n\n请重新输出完整 JSON。",
                max_tokens=max_tokens,
                model=model,
                temperature=0,
            )
            retry_candidate = _extract_json_candidate(retry_text)
            try:
                parsed = _parse_json_relaxed(retry_candidate)
                return {"items": parsed} if isinstance(parsed, list) else parsed
            except Exception as third_err:
                raise ValueError(
                    f"json parse failed after retries: first={type(first_err).__name__}, "
                    f"repair={type(second_err).__name__}, retry={type(third_err).__name__}"
                ) from third_err


# ── Stage 0-3: 对话 + 文档生成（按项目类型差异化） ──

STAGE_ROLES = {
    "new": {0: "资深产品经理", 1: "资深需求分析师", 2: "资深产品设计师", 3: "资深架构师"},
    "maintain": {0: "资深架构师", 1: "资深需求分析师", 2: "资深产品设计师", 3: "资深架构师"},
    "legacy_rewrite": {0: "资深代码审计专家", 1: "资深需求分析师", 2: "资深产品设计师", 3: "资深架构师"},
}

STAGE_GOALS = {
    "new": {
        0: "Stage 0 **主任务**：围绕方案做**技术经济论证**——技术上是否可行、投入与产出是否匹配（是否划算），并**挖掘、阐明建设目的与核心价值点**；把共识固化为**后续需求分析（Stage 1）的约束与输入**（范围边界、非目标、成功判据、经济性前提等），不在此展开详细功能规格或架构设计。"
           "是否讨论外包形态、报价/商务基线、工作量表等**仅以主会话为准**，禁止主观臆测项目类型；会话已谈到的口径、模板须在生成文档时**完整落实**，未谈到的商务内容不要硬加。",
        1: "帮助用户细化需求规范（功能需求、用户故事、非功能需求、数据需求、接口需求、验收标准）",
        2: "帮助用户设计产品原型（站点地图与路由、逐页区域布局表、核心交互分步流程、全局 Header/断点/状态；粗粒度能力说明即可，不要求完整业务 API 目录）。"
           "产出应便于**人类用 Cursor 直接开工**，并便于**设计师在 Figma 中按页建 Frame、按区拆组件**（结构化、命名与路由全文一致，无需任何 Figma API）。",
        3: "帮助用户制定技术方案（技术栈、架构、数据库设计、目录结构、安全设计）。"
           "其中与 HTTP/API 相关的部分只写**全局约定**：统一响应包装、错误码体系与分段规则、版本与鉴权、分页/幂等等跨接口规则；"
           "**不要**试图列出或设计完整业务接口目录（如上传、注册、各业务 CRUD 全表）。具体路由在详细设计/编码阶段按需增加，由实现与架构师现场敲定。",
    },
    "maintain": {
        0: "对项目全部资产进行维护性分析，产出《项目维护分析报告》（项目现状识别、代码结构分析、文档资产分析、风险与雷区、完整性检查、待确认问题）",
        1: "基于代码分析结果，细化增量需求规范（新增功能需求、改进需求、兼容性需求、回归测试需求）",
        2: "基于现有系统设计增量原型（新增/修改页面、交互变更、与变更对应的粗粒度能力说明）。"
             "交付物结构化：站点与路由、逐页区域表、分步流程、文末 UI Spec 与正文一致，便于 Cursor 与 Figma 人工落地。",
        3: "基于现有架构制定增量技术方案（影响分析、改动范围、数据库变更、API **约定与兼容策略**（非全量新业务接口目录）、兼容性方案）",
    },
    "legacy_rewrite": {
        0: "深度审计旧系统代码，识别缺陷和痛点，逆向提取业务逻辑（缺陷分析报告 + 业务逻辑文档）",
        1: "基于缺陷分析和业务逻辑文档，设计全新系统的需求规范（功能需求、非功能需求改进目标、数据迁移需求）",
        2: "设计全新系统的产品原型（全新 UI、改进的交互流程、与页面/数据流对应的粗粒度能力说明）。"
             "交付物结构化：站点与路由、逐页区域表、分步流程、文末 UI Spec 与正文一致，便于 Cursor 与 Figma 人工落地。",
        3: "制定全新系统的技术方案（新技术栈、新架构、数据库重新设计、数据迁移方案、性能优化方案）。"
           "HTTP/API 部分只写**全局约定**（包装器、错误码、鉴权与版本等），不写完整业务接口清单；具体端点在编码阶段按需补充。",
    },
}


STAGE_NAMES = ['业务方案', '需求分析', '产品原型', '技术方案', '任务分解', '编码实现', '测试验证', '部署交付']

STAGE_BOUNDARIES = {
    0: "本阶段聚焦**商业价值**：背景与价值、用户与角色、功能边界、成功标准与约束等。"
       "是否涉及**外包交付、报价、商务基线、分期与工作量评估**等，**只由用户在主会话中说明**，不在此预设；用户谈到则纳入本阶段讨论与文档，未谈到则不强加。"
       "用户已表达的成本、报价口径、模板等须写进共识与文档，不得忽视。"
       "避免下沉到技术选型、数据库、API 等实现细节；合同盖章的最终价目可在后续商务阶段定稿。",
    1: "本阶段只细化功能需求、用户故事、非功能需求与验收标准。严禁输出技术方案详细设计（数据库表结构、接口字段定义、部署方案、目录结构、中间件选型）。",
    2: "本阶段只做产品原型设计（页面、流程、交互）。不要做技术选型和架构设计。"
       "交付物应达到**可执行说明**水准：站点与路由自洽、每页有布局表、关键路径有分步流程，便于下一阶段的实现与视觉对照。",
    3: "本阶段做技术方案设计（架构、数据库、**API 全局约定**）。必须严格基于前几个阶段已确定的需求文档。"
       "API 层只沉淀包装器、错误码、鉴权与版本等**横切约定**，不写全量业务接口表；具体接口在详细设计/编码阶段按需扩展。",
}

STAGE_BOUNDARIES_BY_TYPE = {
    "maintain": {
        0: "本阶段是维护项目的技术分析阶段。必须深入代码结构、配置、部署、文档与依赖细节，形成可维护性的现状理解；新功能需求放到下一阶段讨论。",
    },
    "legacy_rewrite": {
        0: "本阶段是旧系统重写前的技术审计阶段。必须深入审计代码、数据模型、依赖与历史兼容逻辑，识别风险和迁移约束。",
    },
}


STAGE_CHAT_GUARDS = {
    0: (
        "不要**在未依据聊天记录的情况下**假定项目是外包、或必须把报价当话题；先基于用户已说的内容回应。"
        "用户**一旦**在对话中谈到外包/实施方式、ROI、成本、报价、分期、业务基线或文档模板，必须**高度重视**并在后续生成文档时**原样落实**，禁止「留给以后」、禁止与用户定义相矛盾。"
        "若用户追问技术实现细节，提示属后续阶段，把问题回收为业务目标与约束澄清。"
        "禁止给出数据库表结构、接口字段、部署命令。"
    ),
    1: (
        "你是需求分析，不是技术方案设计。"
        "当用户要求技术细节时，只能给需求层表达：业务规则、输入输出行为、验收标准、非功能指标。"
        "禁止输出数据库表DDL、接口字段清单、技术架构图、部署拓扑。"
        "可用一句话模板：『这个点在当前阶段先定义为需求约束，技术实现会在技术方案阶段展开。』"
    ),
    2: (
        "本阶段聚焦页面/交互原型，不展开架构与数据库细节。"
        "若用户越界，先给交互层结论，再提示技术细节留到下一阶段。"
        "讨论与生成时优先**结构化**（路由、表格化页面区、分步流程），避免只有散文式形容，便于用户将对话结果固化为高质量原型文档。"
    ),
    3: (
        "本阶段先判断复杂度再决定详细设计深度：一般复杂度给可执行的架构方案和模块边界即可，不强制字段级详细设计。"
        "不要引导用户一次性罗列全部业务 API；需要哪些接口在编码时与架构师/实现 AI 按需商定。"
        "复杂需求可进入详细设计；详细设计可由架构师主导，或由直接负责程序员对接补充。"
        "无论与谁对接，结论必须沉淀为文档，禁止只停留在口头/会话。"
    ),
}


def _get_role(stage: int, project_type: str = "new") -> str:
    return STAGE_ROLES.get(project_type, STAGE_ROLES["new"]).get(stage, "资深技术经理")


def _get_goal(stage: int, project_type: str = "new") -> str:
    return STAGE_GOALS.get(project_type, STAGE_GOALS["new"]).get(stage, "项目规划")


def _get_stage_boundary(stage: int, project_type: str = "new") -> str:
    custom = STAGE_BOUNDARIES_BY_TYPE.get(project_type, {}).get(stage)
    if custom:
        return custom
    return STAGE_BOUNDARIES.get(stage, "")


def _get_stage_chat_guard(stage: int) -> str:
    return STAGE_CHAT_GUARDS.get(stage, "")


_MAINTAIN_STAGE0_ANALYSIS = """这是一个维护/迭代项目。Stage 0 的核心任务是对项目全部资产进行**全面的维护性分析**，产出《项目维护分析报告》，为后续的需求分析、架构整理、改造规划等阶段提供基础信息。

## 核心原则
- **现状优先**：以项目当前实际存在的实现、目录、配置和文档为准，不以理想架构代替现状
- **维护导向**：重点关注"如何理解、接手、维护和安全修改"，而非优先讨论"大改""重构""升级"
- **文档代码同等**：代码和文档都是理解项目的重要依据，必须同等重视
- **尊重原始**：项目背景、业务知识、术语定义等内容，应尽量尊重原始文档表述
- **最小假设**：对仓库中没有明确证据支持的信息，不做过度脑补
- **完整性检查**：不仅看"有什么"，还要判断"按项目形态本应有什么但当前没有"
- **主动提问**：发现明显缺失项时，应在报告中单独列出待确认问题
- **谨慎评价**：不因项目老旧、分层不理想就简单下负面结论，先解释现状、作用和维护影响

## 6 项分析任务
1. **项目现状识别**：用途/业务目标、系统类型（单体/前后端分离/微服务）、技术栈、模块划分、入口点与运行方式、外部依赖痕迹
2. **代码结构分析**：主要目录和模块职责、核心层次结构、关键入口/组件/服务/模型、代码约定与隐含规则
3. **文档资产分析**：重要文档清单及主题/作用/位置、项目背景/业务知识/架构说明/接口规范/部署运维/数据字典提取
4. **风险与维护雷区**：高耦合区域、不宜轻易修改的公共逻辑、配置陷阱、历史兼容逻辑、缺乏测试保护的核心路径、依赖隐式业务规则的模块
5. **完整性检查**：前后端项目缺前端？有数据库访问但缺建表脚本？有部署文件但缺说明？文档提及某子系统但仓库未体现？
6. **待确认问题整理**：指出怀疑依据 + 说明影响 + 明确需要操作者补充什么

## 特殊情况处理
- **文档与代码冲突**：以代码实际行为为准，但记录文档描述差异
- **信息不足**：明确标注"不足以确认"的部分，不强行补全
- **多技术栈混合**：分别分析各技术栈，说明交互方式和技术栈边界

"""


def _get_project_type_context(project_type: str, stage: int, rewrite_reason: str = "", tech_stack: str = "") -> str:
    """生成项目类型相关的额外上下文"""
    if project_type == "maintain":
        if stage == 0:
            from app.services.style_guide import match_code_style
            style_ref = match_code_style(tech_stack)
            base = _MAINTAIN_STAGE0_ANALYSIS
            if style_ref:
                base += f"分析代码时，请以以下目标代码风格作为参照基准，指出现有代码与目标风格的差异：\n\n{style_ref}\n\n"
            return base
        return "这是维护迭代项目，请基于现有代码分析结果进行工作。\n\n"
    if project_type == "legacy_rewrite":
        if stage == 0:
            from app.services.style_guide import match_code_style
            style_ref = match_code_style(tech_stack)
            base = "这是一个旧系统重写项目。请深度审计代码，重点识别：性能瓶颈、技术债务、可维护性问题、AI 协作困难、扩展性限制。同时逆向提取完整的业务逻辑。\n\n"
            if style_ref:
                base += f"审计代码时，请以以下目标代码风格作为参照基准，对比旧代码与目标风格的差距：\n\n{style_ref}\n\n"
            return base
        return "这是旧系统重写项目，请基于缺陷分析和业务逻辑文档设计全新系统。\n\n"
    return ""


STAGE_CHAT_SYSTEM = """你是一个{role}，正在和用户讨论项目的{goal}。

当前所处阶段：Stage {stage_index}
{stage_boundary}

{project_type_context}你的职责：
- 通过对话帮助用户理清思路，主动提问引导用户补充关键信息
- 如果用户的描述模糊，追问细节
- 回复简洁自然，像同事聊天一样，不要每次都输出完整文档
- 可以给建议、举例子、指出潜在问题

严格遵守的阶段边界（非常重要）：
- 你只负责当前阶段（Stage {stage_index} - {stage_name}）的工作
- 每次回答前先判断“这是不是当前阶段要解决的问题”，不是就明确回收范围，避免跑题
- 如果用户讨论的内容超出了当前阶段范围，必须明确提醒用户："我们当前处于【{stage_name}】阶段，主要解决的是XX问题。你提到的YY属于后续【ZZ】阶段的内容，建议我们先把当前阶段的工作完成。"
- 不要建议用户进入下一步、不要给出"下一步建议"、不要输出"建议的下一步"，阶段推进由用户自己决定
- 不要输出技术任务书、实施计划、开发任务清单等超出当前阶段范围的内容
- 每隔几轮对话，可以简要总结当前阶段还有哪些关键问题需要讨论
{stage_guard}

{asset_context}{experience_context}{previous_context}"""

STAGE_DOC_SYSTEM = """你是一个{role}。基于与用户的讨论内容，生成本阶段的正式文档。

{project_type_context}输出结构化的 Markdown 文档，覆盖：{goal}。

{style_guides}

要求：
- **忠实于聊天记录（硬性）：** 上文对话里用户与助理已约定的**每一条**可写入文档的内容——章节与小节标题、清单枚举、约束条款、量化指标、表格/列定义、术语口径、阶段边界说明等——须在正文中**逐项落实**，**禁止缺斤少两**；禁止用泛泛概括或「等重点」替代已逐条列出的事项，禁止擅自合并删减子项。对话未出现的不强行脑补。
- 综合所有讨论内容，不遗漏关键信息
- **用户与助理在主会话中已明确表达的意见、定义与模板优先**：不得因系统默认「偏实施」而忽略；凡对话中出现过的约束（含是否写报价、报价指业务基线还是含金额等），正文必须一致体现
- **禁止主观推断**：不得根据 project 类型、产品形态等臆测「是否外包」「是否必须出报价」；是否含报价、写到何种粒度，**仅以主会话记录为准**
- **立场与读者（无先验默认）：** 使用方可能是**甲方**（立项、内评、招标需求、验收口径）也可能是**乙方**（投标、交付与商务）。主会话未写明时**不默认**立场；须按对话中的读者与用途组织重点——甲方常见侧重目标约束、选型风险、里程碑与验收；乙方/外包投标常见侧重可交付范围、排除项、假设与配合点、商务与方案级报价。**两类用户都会用本系统**，勿把「偏实施」偷换成「一律乙方投标体例」。
- **内部/自研（主会话或补充说明已表明时）：** 若用户说明为内部项目、编制内人力、**不单独核算外包开发成本**、**不需要对外报价或人天单价**，则不要强行写对外方案报价表；可侧重规划、架构、里程碑与资源安排，价格相关按对话处理或用「待论证」集中列出。
- **乙方/外包可执行方案与报价（主会话或「用户对本次生成的补充要求」中明确时）：** 若出现「外包」「乙方立场」「可执行」「方案与报价」「投标」等表述（与上条不矛盾——须**先由用户或补充说明写出来**），文档须按**乙方交付/比选**口径撰写：交付范围与**明确排除项**、前提假设与甲方配合点、分期或里程碑、**按市场行情的方案级报价表与合计**、风险与变更边界；单列待论证项。不得写成仅适合甲方内部备忘、缺少商务可执行性的简报。
- 若对话中已约定章节标题、顺序、分篇清单或统一写作模板，必须严格按该约定成文；与下方默认覆盖范围冲突时以对话为准
- 主会话**已**约定的报价口径、模板或「不写确定合同价」等，须按会话落实，禁止省略或用「商务另议」敷衍
- **「报价」与「报价单」呈现：** 仅当**主会话已出现**相关约定时适用——例如对话或模板中含「报价依据」「商务基线」「工作量评估」、章节名含「报价」，或用户/助理明确要求表格化估算。此时除文字外**必须**给出 **Markdown 表格**（小节标题可用「### 方案级报价单（估算）」）；列建议含：序号、工作包/交付项、范围说明、计价口径、**估算（人天/人月或费用）**、备注。主会话**未**谈报价时不要仅因臆测「外包」而加表。
- **按市场行情做真实报价（采信由用户）：** 一旦写方案级报价，须结合**公开市场常见行情**（外包/定制软件常见人天或人月单价量级、同类规模项目费用量级等，可简要注明「参考国内市场惯例」）给出**尽量具体、可对比的数字**——如各行**人天或人月区间**、可选 **× 参考单价 → 人民币费用区间**，并附**合计区间**；也可用 S/M/L 档位并在表下说明档位对应人天/万元。**禁止**用满篇「待核定」或空话代替数字。**法律效力与最终合同价以用户后续商务流程为准；此处数字是否采信由用户自行判断**，不必用冗长免责替代估算本身。
- **禁止「全员待核定」：** 报价表中**禁止**超过半数行仅有「待核定」而无具体区间或档位；须能横向比较、排序与加总。
- **待论证 / 待补充（信息不足时）：** 主会话未给足依据、无法负责任写死的内容（边界、指标、报价前提等），**勿**在正文里假装已定或用一句「待确认」糊弄；应集中单列小节 **「待论证事项」** 或 **「待补充信息与后续迭代」**，**逐条**写清：缺什么输入、需论证什么、补全后文档哪几处可收紧。**目的**：用户补信息后可用「重新生成」更新整篇，或在**单篇文档审阅对话**里多轮深化后再「应用讨论」定稿。
- 结构清晰，内容完整
- 文档内容只覆盖当前阶段要解决的问题，不跨阶段展开
- 输出必须直接从文档正文开始，不要写任何对话复述、过程说明、前言废话
- 第一行必须是一级标题（# 标题），标题前不允许有任何文字
{title_instruction}

{asset_context}{previous_context}"""


_MAINTAIN_STAGE0_DOC_SYSTEM = """你是一个{role}。基于与用户的讨论内容和已上传的项目资料分析结果，生成《项目维护分析报告》。

{project_type_context}

## 报告结构（必须严格按此结构输出 Markdown 文档）

### 1. 项目概览
- 项目用途/业务目标
- 系统形态判断（单体/前后端分离/微服务等）
- 技术栈概览
- 当前仓库包含的内容

### 2. 项目资产盘点
**2.1 代码资产**：主要模块/目录、入口点、核心组件、关键依赖
**2.2 文档资产**：重要文档列表、主题/作用/位置、最关键的资料

### 3. 现有结构与关键约定
- 模块职责、层次结构、核心调用链
- 代码中的约定、文档定义的关键规则/术语

### 4. 维护风险与注意事项
- 高风险模块、不宜改动的位置、隐式依赖
- 配置风险、历史兼容风险、知识断层风险

### 5. 缺失信息与待确认问题
逐条列出：缺失项 | 怀疑依据 | 对维护的影响 | 需要补充的资料

### 6. 给后续维护者的建议
- 推荐优先阅读的内容、建议先理解的模块
- 修改前应先确认的约束
- 后续阶段可继续生成的文档

## 质量要求
- 若对话中对报告结构或要点有额外约定，须与下述固定结构一并完整体现，**禁止缺斤少两**
- 结论有依据，不空泛；区分"已确认"和"推断"
- 覆盖代码和文档两方面，不遗漏重要部分
- 不把缺失当不存在，务实导向，面向维护者
- 重点突出"可维护性理解"而非"理想化设计评审"
{title_instruction}

{style_guides}

{asset_context}{previous_context}"""


STAGE_REGEN_SYSTEM = """你是一个{role}。用户对之前生成的文档提出了修改意见，请根据意见修改文档。

{style_guides}

原始文档：
{original_doc}

修改意见：
{feedback}

要求：
- 根据修改意见调整文档，保留没有问题的部分
- 若用户已补充原「待论证/待补充」条目中的信息，须将结论并入正文并**删减或更新**对应待办条，避免长期滞留已解决项
- 输出完整的修改后文档（Markdown 格式）
- 不要输出多余的解释，只输出文档内容
- 保持文档的层次和粒度：技术方案中的 API 部分只保留**全局约定**（包装器、错误码、鉴权与版本等），不要写成完整业务接口目录，不要展开逐字段 Schema"""


def _get_chat_temperature(stage: int, project_type: str) -> float:
    """根据阶段和项目类型决定聊天 temperature"""
    if stage == 0:
        return 0.8 if project_type == "new" else 0.3
    if stage <= 2:
        return 0.6
    return 0.3


async def chat_in_stage(stage: int, history: list[dict], previous_docs: str = "", model: str | None = None, asset_context: str = "", project_type: str = "new", rewrite_reason: str = "", experience_context: str = "", tech_stack: str = "") -> str:
    from app.services.context_manager import prepare_messages

    role = _get_role(stage, project_type)
    goal = _get_goal(stage, project_type)
    prev_ctx = f"前置阶段文档（供参考）：\n{previous_docs}" if previous_docs else ""
    asset_ctx = f"项目资料（已上传并分析）：\n{asset_context}\n\n" if asset_context else ""
    exp_ctx = f"\n\n{experience_context}\n\n" if experience_context else ""
    project_type_ctx = _get_project_type_context(project_type, stage, rewrite_reason, tech_stack)

    stage_boundary = _get_stage_boundary(stage, project_type)
    stage_guard = _get_stage_chat_guard(stage)
    stage_name = STAGE_NAMES[stage] if stage < len(STAGE_NAMES) else f"Stage {stage}"
    system = STAGE_CHAT_SYSTEM.format(
        role=role, goal=goal, stage_index=stage, stage_boundary=stage_boundary,
        stage_name=stage_name,
        previous_context=prev_ctx, asset_context=asset_ctx, experience_context=exp_ctx,
        project_type_context=project_type_ctx, stage_guard=stage_guard,
    )

    msgs = [m for m in history if m["role"] in ("user", "assistant")]
    msgs = await prepare_messages(msgs, system, model)

    chat_budget = _effective_stage_chat_max_tokens(model)
    return await _call_multi(
        system, msgs, max_tokens=chat_budget, model=model,
        auto_continue=True, max_continuation_rounds=MAX_CHAT_CONTINUATION_ROUNDS,
        temperature=_get_chat_temperature(stage, project_type),
    )


async def chat_in_stage_stream(
    stage: int, history: list[dict], previous_docs: str = "", model: str | None = None,
    asset_context: str = "", project_type: str = "new", rewrite_reason: str = "",
    experience_context: str = "", knowledge_index: str = "",
    project_id: str = "", session=None, tech_stack: str = "",
) -> AsyncIterator[str]:
    """流式版对话，yield 每个 token。支持知识索引+按需加载。"""
    from app.services.context_manager import prepare_messages

    role = _get_role(stage, project_type)
    goal = _get_goal(stage, project_type)
    prev_ctx = f"前置阶段文档（供参考）：\n{previous_docs}" if previous_docs else ""
    if knowledge_index:
        asset_ctx = f"{knowledge_index}\n\n"
    elif asset_context:
        asset_ctx = f"项目资料（已上传并分析）：\n{asset_context}\n\n"
    else:
        asset_ctx = ""
    exp_ctx = f"\n\n{experience_context}\n\n" if experience_context else ""
    project_type_ctx = _get_project_type_context(project_type, stage, rewrite_reason, tech_stack)

    stage_boundary = _get_stage_boundary(stage, project_type)
    stage_guard = _get_stage_chat_guard(stage)
    stage_name = STAGE_NAMES[stage] if stage < len(STAGE_NAMES) else f"Stage {stage}"
    system = STAGE_CHAT_SYSTEM.format(
        role=role, goal=goal, stage_index=stage, stage_boundary=stage_boundary,
        stage_name=stage_name,
        previous_context=prev_ctx, asset_context=asset_ctx, experience_context=exp_ctx,
        project_type_context=project_type_ctx, stage_guard=stage_guard,
    )

    msgs = [m for m in history if m["role"] in ("user", "assistant")]
    msgs = await prepare_messages(msgs, system, model)

    chat_budget = _effective_stage_chat_max_tokens(model)
    cont = MAX_CHAT_CONTINUATION_ROUNDS
    if project_id and session and knowledge_index:
        async for token in stream_with_knowledge(
            system, msgs, project_id=project_id, session=session,
            active_stage=stage,
            max_tokens=chat_budget, model=model, temperature=_get_chat_temperature(stage, project_type),
            stream_auto_continue=True, max_continue_rounds=cont,
        ):
            yield token
    else:
        async for token in _stream_multi(
            system, msgs, max_tokens=chat_budget, model=model,
            temperature=_get_chat_temperature(stage, project_type),
            auto_continue=True, max_continue_rounds=cont,
        ):
            yield token


async def generate_stage_document(
    stage: int,
    history: list[dict],
    previous_docs: str = "",
    title: str = "",
    model: str | None = None,
    asset_context: str = "",
    project_type: str = "new",
    rewrite_reason: str = "",
    tech_stack: str = "",
    generation_hints: str = "",
) -> str:
    from app.services.style_guide import match_guides
    role = _get_role(stage, project_type)
    goal = _get_goal(stage, project_type)
    prev_ctx = f"前置阶段文档：\n{previous_docs}" if previous_docs else ""
    if title:
        title_instr = (
            f"- 文档标题必须使用：{title}\n"
            f"- 第一行固定输出：# {title}\n"
            "- 禁止在标题前输出任何说明性文字"
        )
    else:
        title_instr = "- 第一行必须是一级标题（# 标题），禁止在标题前输出任何说明性文字"
    asset_ctx = f"项目资料（已上传并分析）：\n{asset_context}\n\n" if asset_context else ""
    project_type_ctx = _get_project_type_context(project_type, stage, rewrite_reason, tech_stack)
    guides = match_guides(tech_stack, stage, project_type)

    if project_type == "maintain" and stage == 0:
        system = _MAINTAIN_STAGE0_DOC_SYSTEM.format(
            role=role, previous_context=prev_ctx, title_instruction=title_instr,
            asset_context=asset_ctx, project_type_context=project_type_ctx, style_guides=guides,
        )
    else:
        system = STAGE_DOC_SYSTEM.format(role=role, goal=goal, previous_context=prev_ctx, title_instruction=title_instr, asset_context=asset_ctx, project_type_context=project_type_ctx, style_guides=guides)

    system = f"{system.rstrip()}\n\n{doc_time_prompt_block()}"

    from app.services.context_manager import prepare_messages
    msgs = [m for m in history if m["role"] in ("user", "assistant")]
    msgs = await prepare_messages(msgs, system, model, keep_recent=32)
    doc_instruction = "请基于以上讨论和代码分析结果，生成《项目维护分析报告》。" if (project_type == "maintain" and stage == 0) else "请基于以上讨论，生成本阶段的正式文档。"
    doc_instruction += (
        "\n\n**输出形态（硬性，与 DeepSeek 等纯文本通道一致）：** 只输出最终 Markdown 正文；"
        "禁止输出英文工作笔记、禁止 `{\"name\": \"fsWrite\"` 或任何工具调用 JSON、禁止「文档已生成至…」类说明。"
        "正文第一行起必须是 `# ` 开头的标题或按模板规定的标题行。\n"
    )
    doc_instruction += (
        "\n\n**忠实于聊天记录（硬性）：** 上文消息中已对齐的每一条可写入文档的要求（章节与清单、约束、指标、表格、定义等）必须**全部体现**，**禁止缺斤少两**；"
        "禁止用概括段代替对话里已逐条列出的事项。输出前在内心按对话逐条自检是否漏项。\n"
        "**信息不足时：** 须明确写出 **「待论证事项」或「待补充信息与后续迭代」** 小节（逐条：缺什么、论证什么、补全后如何更新文档），便于用户补信息后再次生成或在单篇审阅对话里继续深化；勿在正文里假装已定。\n"
    )
    if not (project_type == "maintain" and stage == 0):
        doc_instruction += (
            "\n**结构约定（务必遵守）：**\n"
            "- **主会话中用户与助理已对齐的意见优先**：只要对话里谈过（含报价范围、章节模板、是否含金额等），生成时必须写到位，禁止擅自降级或与用户表述矛盾。\n"
            "- 对话里若已列出章节结构、统一模板或多篇文档清单，生成时必须逐项落实，禁止擅自删减小节或改用另一套大纲。\n"
            "- **勿根据 project 类型推断外包或是否报价**：仅依据**上方对话**。若对话或模板已要求分期/交付边界、报价依据、商务基线、工作量评估等，须写具体，且按系统提示在需要时含**报价单式 Markdown 表格**（不得整节空话）；对话**明确不要**商务/报价则不要写。\n"
            "- **报价单：** 按**市场行情**给出具体人天/人月或费用区间与合计（见系统提示）；采信与否由用户；禁止各行仅「待核定」。\n"
            "- 系统提示中的默认覆盖范围仅在对话未明确约定时作兜底；有约定时以对话为准。"
        )
    if project_type == "new" and stage == 0:
        doc_instruction += (
            "\n\n**Stage 0·报价单表格（仅当对话已涉及）：** **不得**主观认定外包或必须报价。仅当上文主会话**已出现**报价/商务基线/工作量评估/「报价依据」类章节模板等约定时，"
            "须在对应位置增加 **「### 方案级报价单（估算）」**（或放入已约定章节名内）及**至少一张 Markdown 表格**（列：序号、工作包/交付项、范围、计价口径、估算量级或区间、备注）；"
            "禁止整节只有套话、无表格；**须按市场行情写具体估算**（人天/人月或人民币区间+合计），禁止半数以上行只有「待核定」。主会话未谈报价时不要硬加。"
        )
    if stage == 2:
        doc_instruction += (
            "\n\n额外要求：文档末尾必须附加一个 ````json` 代码块，输出 UI Spec JSON。"
            "格式要求：包含 `version`、`pages`（数组）、`tokens`。"
            "每个 page 至少包含 `id`、`name`、`route`、`layout`、`nodes`、`states`。"
            "除该 JSON 代码块外，文档正文仍保持正常 Markdown。"
        )
        doc_instruction += (
            "\n\n**Stage 2·交付质量（对齐高水准产品原型文档）：**\n"
            "- **信息架构**：站点结构树 + 路由与多语言 URL 规则须完整，后文各页路径与之严格一致。\n"
            "- **逐页规格**：每个主要前台/后台页用 **Markdown 表格** 写清区域（建议列：区域序号、名称、内容说明、桌面/平板/移动端布局）；避免单页仅一段描述。\n"
            "- **全局**：Header/Footer、核心断点、加载中/空状态/错误提示等跨页约定须单独成节或表。\n"
            "- **第 4 章·核心交互流程（硬性，不得省略）：** 在「逐页布局表」之后、**UI Spec JSON 代码块之前**，必须包含独立章节，二级标题固定为 **`## 4. 核心交互流程`**（若主会话已约定等价标题可替换，但不得把流程仅散落在第 3 章表格文案里代替）。\n"
            "  - 按上文对话已涉及的功能写全；至少覆盖：**语言切换**、**咨询/线索表单**（含频控、验证码、成功/失败与后端失败不落前端的约定若对话有）、**知识库搜索**、**SLA/关键模态框** 等前台关键路径；若对话含后台 AI 辅助（生成/润色/翻译），须各给 **``` 文本步骤图**（或等价分步列表）。\n"
            "  - **禁止**写完第 3 章最后一页后直接接 JSON；**禁止**以「详见某页表格」一句话顶替整章流程。\n"
            "- **成文顺序建议**：站点与全局 → 逐页布局表 → **务必写完第 4 章** → 最后 JSON。\n"
            "- **信息密度（与「被截断」区分）：** 先前长稿多为 **length 截断** 而非不应写细；**禁止**为「一次写完」而主动压缩——不得删减对话/前置文档已列的子项、不得把多行表格收成一句概括、不得以「等」「从略」代替已对齐清单。"
            "若单次输出因 **length** 触顶，系统会**自动续写**多轮直至用尽续写上限；你应优先写全对话事实，不必自行预判总篇幅而省字。\n"
            "- **实现与视觉衔接**：正文中的页面名、`route`、英文 `id` 建议全文一致；文末 UI Spec 中 `pages[].id`/`route`/`name` 须与正文页面清单一一对应；"
            "`nodes` 宜细化到可作为前端组件边界的粒度（如 Hero、ServiceCard、LeadForm）；`states` 须覆盖加载、空、错误等关键 UI 状态。"
            "目标：人类可直接以本文档 + 技术方案为输入在 **Cursor** 中生成前端代码；设计师可在 **Figma** 中按页拆 Frame，无需臆测口径。\n"
        )
    if stage == 3:
        doc_instruction += (
            "\n\n**技术方案中 HTTP/API 的粒度（务必遵守）：**\n"
            "- 只写**全局约定**：统一响应包装（envelope）、错误码分段与示例、版本与鉴权策略、分页/幂等/时间格式等横切规则。\n"
            "- **禁止**输出完整业务接口目录或逐条设计具体业务路由（上传、注册、各域 CRUD 等）；这些在详细设计/编码阶段按需增补。\n"
            "- 若需示意约定如何落地，全文最多 **1～2 条**匿名化或极简示例路径，不得扩展为「接口全景」。"
        )
    gh = (generation_hints or "").strip()
    if gh:
        doc_instruction += f"\n\n**用户对本次生成的补充要求：**\n{gh}"
        if stage == 0 and any(k in gh for k in ("外包", "乙方", "投标")):
            doc_instruction += (
                "\n（上述补充若明确外包/乙方/投标口径：须按系统提示「乙方/外包可执行方案与报价」条落实，含报价表与排除项。）"
            )
    msgs.append({"role": "user", "content": doc_instruction})

    # Stage 2：请求 32K 输出上限（经 _cap 与模型配置裁剪）；未配置 max_output 时不再误压到 8K；另见 _finalize 补全缺章/未闭合 JSON。
    if project_type == "new" and stage == 2:
        doc_max = 32768
    elif project_type == "new" and stage in (0, 3):
        doc_max = 8192
    else:
        doc_max = 4096
    doc_cont = MAX_DOC_STAGE2_CONTINUATION_ROUNDS if (project_type == "new" and stage == 2) else MAX_DOC_CONTINUATION_ROUNDS
    is_s2_long = project_type == "new" and stage == 2
    content = await _call_multi(
        system, msgs, max_tokens=doc_max, model=model,
        auto_continue=True, temperature=0.4, max_continuation_rounds=doc_cont,
        doc_long_output=is_s2_long,
    )
    if is_s2_long:
        content = await _finalize_stage2_product_doc(system, msgs, content, doc_max, model, doc_cont)
    return content


async def regenerate_document(stage: int, original_content: str, feedback: str, model: str | None = None, project_type: str = "new", tech_stack: str = "") -> str:
    from app.services.style_guide import match_guides
    role = _get_role(stage, project_type)
    doc = original_content[:_MSG_MAX_CHARS] + "\n\n...(文档过长已截断)" if len(original_content) > _MSG_MAX_CHARS else original_content
    guides = match_guides(tech_stack, stage, project_type)
    system = f"{STAGE_REGEN_SYSTEM.format(role=role, original_doc=doc, feedback=feedback, style_guides=guides).rstrip()}\n\n{doc_time_prompt_block()}"
    user_prompt = "请根据修改意见重新生成文档。"
    if stage == 2:
        user_prompt += (
            "\n并在文档末尾保留一个 ````json` UI Spec 代码块（包含 version/pages/tokens；"
            "page 至少包含 id/name/route/layout/nodes/states）。"
            "\n正文须保持 Stage 2 交付质量：站点与路由自洽、主要页面有区域布局表、**须含 `## 4. 核心交互流程`**（多组 ``` 步骤图）、UI Spec 与正文页面一一对应。"
        )
    if stage == 3:
        user_prompt += (
            "\n技术方案中的 API 部分：只保留全局约定（包装器、错误码、鉴权与版本等），"
            "不要写成完整业务接口目录或逐字段 Schema。"
        )
    regen_max = 32768 if stage == 2 else 8192
    regen_cont = MAX_DOC_STAGE2_CONTINUATION_ROUNDS if stage == 2 else MAX_DOC_CONTINUATION_ROUNDS
    content = await _call(
        system, user_prompt, max_tokens=regen_max, model=model,
        auto_continue=True, temperature=0.4, max_continuation_rounds=regen_cont,
        doc_long_output=(stage == 2),
    )
    if stage == 2:
        content = await _finalize_stage2_product_doc(
            system, [{"role": "user", "content": user_prompt}], content, regen_max, model, regen_cont,
        )
    return content


# ── 文档审核（找茬模式） ──

_STAGE_REVIEW_HINTS = {
    0: "重点：需求边界是否清晰、痛点是否具体、是否有可量化的验收标准。",
    1: "重点：功能列表是否完整、用户故事是否可测试、验收标准是否可执行。",
    2: "重点：站点地图与正文路由是否一致；主要页面是否有区域布局表；**是否包含独立章节 `## 4. 核心交互流程`**（含语言切换、表单、搜索、SLA/模态、后台 AI 等关键路径的 ``` 分步流程，缺整章为重大缺项）；全局 Header/断点/空状态是否交代；文末 UI Spec JSON 是否与正文页面 id/route 对应、nodes 是否具组件粒度、states 是否覆盖关键 UI 状态（勿要求完整业务 API 目录）。",
    3: "重点（概要设计口径）：系统边界与模块划分、与上游需求对齐、数据与集成关系、关键技术选型与非功能需求、风险与约束。"
    "**不要**以缺少完整业务接口表或 OpenAPI 级清单为由判高严重度；具体接口属编码阶段按需敲定。",
}

REVIEW_SYSTEM = """你是技术评审。只评审**这一份**文档，且范围**仅限**下文「审核口径」；不要假设必须把其它阶段的文档并进来一起验收。

核心规则：
- **评审范围 = 审核口径**。流水线「阶段号」只作背景，**不得**用别阶段的交付标准强加本稿（例如：接口约定稿没有写系统架构/邮件 CDN/数据库选型 → 不算缺项）。
- 不得用 A 类文档的清单去挑 B 类文档（接口规范 ≠ 概要设计全文）。
- 下文「项目背景」含代码/资产摘要及上游/同阶段文档节选。**给出 critical 或 warning 前必须核对是否与背景矛盾**；若背景已说明某块为静态托管、无需落库，则不得据此判「缺业务表」为高严重度，至多 suggestion 建议补充文稿内说明。
- 若有「审核方补充说明」，**必须优先遵守**；再次审核时须结合补充说明重新判断，若与先前典型结论冲突，以补充说明与项目背景为准，并在 summary 中简要说明采纳情况。

在口径内看：完整性、可行性、一致性、具体性、是否缺**本口径**该写的约定/章节。
若文档**诚实列出**「待论证/待补充」且与当前信息不足相符，**不得**仅因此判 critical；可用 suggestion 提示用户补哪些输入后可收紧，并肯定其结构化待定方式。

少客套。无 critical 与 warning 时 approved=true；可附 suggestion。

输出JSON: {"approved":bool,"recommendation":"approve/reject","score":1-10,"issues":[{"description":"","severity":"critical/warning/suggestion","section":""}],"summary":""}"""


def _is_api_spec_artifact(title: str, category: str) -> bool:
    """与阶段无关：只要是「接口规范/约定」类单稿，就不按概要设计全文评审。"""
    raw = title or ""
    t = raw.lower()
    c = (category or "").lower()
    if c == "spec":
        return True
    if "接口规范" in raw:
        return True
    if "openapi" in t and ("规范" in raw or "spec" in c):
        return True
    if "api" in t and "规范" in raw:
        return True
    return False


def _review_profile(stage: int, title: str, category: str) -> tuple[str, str]:
    t = (title or "").lower()
    c = (category or "").lower()

    if "代码风格" in title or "style" in t:
        return (
            "代码风格规范文档",
            "按代码风格规范审核：可执行规则、示例、前后端差异。不要要求需求边界、验收 KPI。",
        )
    # 接口约定稿优先于「阶段3+design」，避免同一阶段下多份文档被误判成概要设计
    if _is_api_spec_artifact(title or "", category or ""):
        return (
            "API/接口规范（约定层）",
            "只审全局约定：响应信封、错误码规则、鉴权与版本、分页/幂等等是否清晰可执行。"
            "不要求业务端点大全、不要求概要设计里的架构/集成/选型。缺具体 CRUD 不算缺陷；大段业务路径可 suggestion 迁出。",
        )
    raw_title = title or ""
    if (
        "数据库" in raw_title
        or "数据模型" in raw_title
        or "database" in t
        or "ddl" in t
    ):
        return (
            "数据库/数据模型设计",
            "评审表结构、字段类型、约束、索引、迁移与**项目背景中的需求/技术方案（尤其内容策略：静态 vs 动态落库）是否一致**。"
            "若背景写明部分栏目为 Markdown/SSG/JSON 等静态托管、仅知识库/案例等需动态与数据库，则**不得**将「缺少服务/页面等业务表」列为 critical。"
            "仍可审：动态范围内的表是否覆盖需求、命名与宽表/多语言约定、索引与外键说明是否可落地。",
        )
    if stage == 3 and (c == "design" or "技术方案" in raw_title):
        return (
            "技术方案（概要设计）",
            "审架构边界、模块职责、与需求对齐、数据与集成、技术选型、风险与非功能。"
            "不得以缺完整业务 API 目录或 OpenAPI 为 warning/critical；HTTP 小节以横切约定为主。",
        )
    if c == "analysis" and stage == 0:
        return (
            "现状分析文档",
            "按现状分析文档标准审核：关注证据充分性、覆盖范围、风险识别、结论与证据的一致性。"
            "不要按“新项目需求定义文档”口径强行要求业务验收条款。",
        )
    return ("阶段文档", "按当前阶段文档标准审核。")


def _review_stage_supplement(profile_name: str, stage: int) -> str:
    """阶段提示仅在与审核档案一致时附加，禁止把阶段3概要设计清单叠到接口规范单稿上。"""
    if profile_name == "API/接口规范（约定层）":
        return (
            "\n\n【范围锁定】本文档仅为接口约定；"
            "禁止以缺系统架构、模块划分、邮件/CDN/数据库选型、上下游集成全景等为由给 warning/critical。"
        )
    if profile_name == "数据库/数据模型设计":
        return (
            "\n\n【范围锁定】本文稿为库表/DDL 向交付物；"
            "勿按完整「概要设计」全文要求系统边界、模块职责长文、全量风险分析——除非背景明确要求本文件承担该职责。"
            "与内容策略相关的争议须引用项目背景中的静态/动态划分，不得默认全站 CMS 化。"
        )
    if profile_name == "代码风格规范文档":
        return ""
    if profile_name == "技术方案（概要设计）":
        h = _STAGE_REVIEW_HINTS.get(3, "")
        return f"\n\n概要设计补充关注点：{h}" if h else ""
    if profile_name == "现状分析文档" and stage == 0:
        h = _STAGE_REVIEW_HINTS.get(0, "")
        return f"\n\n本阶段额外关注：{h}" if h else ""
    h = _STAGE_REVIEW_HINTS.get(stage, "")
    return f"\n\n本阶段额外关注：{h}" if h else ""


async def review_document(
    stage: int,
    document: str,
    model: str | None = None,
    *,
    title: str = "",
    category: str = "",
    project_context: str = "",
    reviewer_context: str = "",
) -> dict:
    cap = _REVIEW_DOCUMENT_BODY_MAX_CHARS
    doc = document[:cap] + "\n\n...(文档过长已截断)" if len(document) > cap else document
    profile_name, profile_hint = _review_profile(stage, title, category)
    stage_extra = _review_stage_supplement(profile_name, stage)
    bg = (project_context or "").strip()
    bg_block = (
        f"【项目背景】\n{bg}\n\n"
        if bg
        else ""
    )
    uctx = (reviewer_context or "").strip()
    user_block = (
        f"【审核方补充说明（最高优先级；再次审核时须重读并重判，不得复述与补充说明矛盾的旧结论）】\n{uctx}\n\n"
        if uctx
        else ""
    )
    result = await _call_json(
        REVIEW_SYSTEM,
        "请以最严格标准审核以下文档，找出所有问题和不足。\n"
        f"{bg_block}{user_block}"
        f"文档标题：{title or '（未命名）'}\n"
        f"文档类别：{category or 'general'}\n"
        f"阶段编号：{stage}\n"
        f"审核档案：{profile_name}\n"
        f"审核口径：{profile_hint}\n"
        f"{stage_extra}\n\n---\n\n{doc}",
        model=model,
        temperature=0.2,
    )
    approved = bool(result.get("approved"))
    recommendation = result.get("recommendation")
    if recommendation not in ("approve", "reject"):
        recommendation = "approve" if approved else "reject"
    result["recommendation"] = recommendation
    result["recommendation_text"] = "建议通过（最终由用户决定）" if recommendation == "approve" else "建议拒绝（最终由用户决定）"
    return result


# ── Stage 4: 任务分解 ──

# ── Leader 分模块级大任务 ──

MODULE_BREAKDOWN_SYSTEM = """你是项目的技术负责人（Leader）。你的职责是将项目按业务功能模块拆分。

## 核心原则：按业务功能纵向拆分

每个模块代表一个**完整的业务功能域**（如"用户管理"、"订单系统"、"支付"），
而不是按技术层拆分（不要拆成"前端模块"、"后端模块"、"数据库模块"）。

每个模块后续会由架构师拆解为编码任务，由全栈工程师完成前后端+测试。

## 拆分原则
- 按业务功能域划分，每个模块包含该功能的完整前后端实现
- 模块粒度：1-3 天工作量（含设计+编码+测试）
- dependencies 填所依赖模块的从0开始的数组索引（整数），没有依赖填 `[]`
- 无依赖的模块可并行开发
- 描述包含：目标、范围、技术要点、与其他模块的接口契约

## 项目初始化（重要）
- 第一个模块必须是「项目初始化」，使用各技术栈的**官方脚手架**创建项目骨架
- 禁止让 AI 从空文件夹一行行写出项目结构，这极不可靠
- 初始化模块的描述中必须明确写出要使用的脚手架命令
- 初始化完成的标准：项目能正常启动（dev server 可运行）

## 架构决策
- 记录跨模块的关键技术决策
- 模块间的接口契约
- 技术选型和约定

输出 JSON：
{
  "architecture_decisions": [
    {"topic": "决策主题", "decision": "具体决策", "rationale": "原因"}
  ],
  "modules": [
    {
      "title": "模块名称",
      "description": "模块目标、范围、技术要点、与其他模块的接口",
      "type": "feature|refactor|deploy|docs",
      "priority": 1,
      "estimated_days": 2,
      "dependencies": [],
      "scope": ["涉及的文件/目录范围"],
      "interfaces": ["与其他模块的接口说明"]
    }
  ]
}"""


async def break_down_modules(requirements: str, prototype: str, technical_design: str, knowledge_ctx: str = "") -> dict:
    """Leader 分模块级大任务"""
    prompt = f"""需求文档：
{requirements}

产品原型：
{prototype}

技术方案：
{technical_design}

知识库上下文：
{knowledge_ctx}

请按功能模块拆分项目。每个模块是一个独立的功能单元，后续会由架构师进一步拆解为编码任务。"""
    return await _call_json(MODULE_BREAKDOWN_SYSTEM, prompt, max_tokens=8192, temperature=0.2)


# ── Architect 分编码级小任务 ──

TASK_BREAKDOWN_SYSTEM = """你是项目架构师，负责将模块拆解为编码任务并分配给全栈工程师执行。

## 核心原则：按业务功能纵向拆分

**严禁横向拆分**（不要把前端和后端分成不同任务）。
每个任务应该是一个**完整的业务功能切片**，包含该功能所需的全部代码：
后端模型/接口 + 前端页面/组件 + 必要的测试，由一个全栈工程师一次性完成。

✅ 正确示例："用户登录功能"= 后端登录API + 前端登录页面 + 测试
❌ 错误示例：把"后端登录API"和"前端登录页面"拆成两个任务

例外：纯基础设施任务（项目初始化、数据库迁移、部署配置）可以单独拆。

## 项目类型适配

根据项目类型采用不同拆分策略：

**BS 架构（Web）**：每个任务覆盖前后端+测试。示例："用户登录"=API+页面+测试。禁止前后端拆分。
**CS 架构（桌面）**：按功能模块拆，每个任务覆盖UI+逻辑+打包。示例："导出功能"=UI+格式转换+各平台打包。
**AI/ML**：按数据流水线拆：采集→清洗→训练→评估→部署。
**移动应用**：按用户故事拆，覆盖UI+状态+平台适配。
**基础设施/运维**：按服务拆，每个任务配置+验证+文档。

## 需求覆盖检查（必须逐项确认）

在拆解任务前，必须先列出需求文档中的所有功能点，确保每个功能点都有对应任务：
- 每个动态内容类型（文章、案例、产品等）必须同时包含 **前台展示API** 和 **后台管理增删改查API**
- 后台管理功能必须覆盖：列表、创建、编辑、删除、状态变更
- 如果某个需求不明确，标记 design_phase 为 "needs_discussion"

## 需要人类讨论的任务

以下情况必须标记 design_phase = "needs_discussion"：
- 技术选型有多种方案且各有利弊
- 需求描述模糊，AI 无法自行判断
- 涉及第三方服务集成，需要账号/密钥/审批
- 对现有架构有重大影响

## 拆分规则

### 项目初始化（如果需要）
- 第一个任务必须用官方脚手架初始化，不手动创建文件
- description 写明脚手架命令（如 `npm create vue@latest`）

### 任务粒度
- 每个任务 0.5-2 小时完成，一个任务 = 一个完整业务功能
- 过大的功能才进一步拆分（如"用户管理"拆为"注册"、"登录"、"权限"）

### 自包含性
- description 必须包含执行者需要的全部信息
- 明确写出：要创建/修改的文件（前后端都要列出）、数据结构、接口签名、前端组件结构
- 如果有跨模块接口约定，必须在 description 中注明

### 依赖管理
- dependencies 填所依赖任务的从0开始的数组索引（整数）
- 无依赖写 `[]`，可并行执行

### 架构决策
- 用 `[架构决策]` 前缀标记关键技术决策

### 验收标准
- 每个任务必须有可验证的验收标准，前后端都要覆盖

## 角色分配（按能力等级，不按技术方向）
- junior: 简单任务（CRUD、配置、UI微调）→ 用便宜模型
- mid: 常规业务功能（默认）→ 标准模型
- senior: 复杂任务（跨模块、复杂算法、架构变更）→ 强模型
- devops: 部署和基础设施任务

## 复杂度评估
对每个任务评估复杂度，这决定了使用哪个级别的模型：
- **low**: CRUD、配置修改、简单 UI 调整、文档 → 可用本地小模型
- **medium**: 常规业务逻辑、API 开发、组件开发 → 标准模型
- **high**: 涉及跨模块重构、复杂算法、数据迁移、架构级变更 → 强模型
- **critical**: 核心架构、安全关键、性能瓶颈 → 顶级模型

输出 JSON：
{
  "architecture_decisions": [
    {"topic": "决策主题", "decision": "具体决策", "rationale": "原因"}
  ],
  "tasks": [
    {
      "title": "简短明确的任务标题",
      "description": "完整的自包含描述",
      "type": "feature|bug|test|e2e_test|integration_test|deploy|refactor|docs|security|performance",
      "priority": 1,
      "suggested_role": "junior|mid|senior|devops",
      "complexity": "low|medium|high|critical",
      "estimated_hours": 0.5,
      "dependencies": [],
      "input_files": [],
      "output_files": [],
      "acceptance_criteria": []
    }
  ]
}"""


TASK_BREAKDOWN_TITLES_SYSTEM = """你是项目架构师。请先输出子任务条目清单及依赖关系（不写详情）。

输出严格 JSON：
{
  "items": [
    {"title": "简短明确的任务标题", "depends_on": []},
    {"title": "另一个任务", "depends_on": [0]}
  ]
}

要求：
1) 仅输出标题条目和依赖索引，不输出描述/验收标准/文件清单
2) 标题动作化、唯一、可执行
3) depends_on 使用 items 数组的从0开始的索引，无依赖填 []
4) 先输出无依赖或可并行的条目，再输出依赖它们的条目
5) 只输出 JSON 本体，不要解释
"""


TASK_BREAKDOWN_ITEM_SYSTEM = """你是项目架构师。请根据指定条目输出该条目的完整任务详情。

输出严格 JSON：
{
  "task": {
    "title": "与输入条目语义一致的标题",
    "description": "完整的自包含描述",
    "type": "feature|bug|test|e2e_test|integration_test|deploy|refactor|docs|security|performance",
    "priority": 1,
    "suggested_role": "junior|mid|senior|devops",
    "complexity": "low|medium|high|critical",
    "requires_design_review": false,
    "design_review_reason": "若需要详细设计，简述原因；否则可为空",
    "estimated_hours": 0.5,
    "input_files": [],
    "output_files": [],
    "acceptance_criteria": []
  }
}

要求：
1) title 与输入条目标题语义一致
2) requires_design_review 由你基于任务复杂度、跨模块影响、接口/数据结构变化风险来判断
3) 不输出 dependencies 字段（依赖关系由上游阶段统一解析）
4) 只输出 JSON 本体，不要解释
"""


async def break_down_tasks(
    module_title: str,
    module_description: str,
    technical_context: str = "",
    knowledge_ctx: str = "",
    requirements: str = "",
    prototype: str = "",
    all_modules_summary: str = "",
) -> dict:
    """Architect 将模块拆解为编码级小任务"""
    from app.services.context_compressor import compress_docs

    compressed = compress_docs(
        requirements=requirements, prototype=prototype, technical=technical_context,
        budget=6000, focus_keywords=[module_title],
    )
    parts = []

    if compressed["requirements"]:
        parts.append(f"## 需求文档（摘要）\n{compressed['requirements']}")
    if compressed["prototype"]:
        parts.append(f"## 产品原型（摘要）\n{compressed['prototype']}")
    if all_modules_summary:
        parts.append(f"## 全部模块概览（当前模块所处的整体架构）\n{all_modules_summary}")

    parts.append(f"## 当前要拆解的模块\n**{module_title}**\n\n{module_description}")

    if compressed["technical"]:
        parts.append(f"## 相关技术方案\n{compressed['technical']}")
    if knowledge_ctx:
        parts.append(f"## 相关经验\n{knowledge_ctx}")

    parts.append("请将上述模块拆解为编码级的小任务（每个 15-60 分钟）。注意与其他模块的接口衔接。")

    prompt = "\n\n".join(parts)
    return await _call_json(TASK_BREAKDOWN_SYSTEM, prompt, max_tokens=8192, temperature=0.2)


async def break_down_task_titles(
    module_title: str,
    module_description: str,
    technical_context: str = "",
    requirements: str = "",
    prototype: str = "",
    all_modules_summary: str = "",
    discussion_context: str = "",
    model: str | None = None,
) -> list[dict]:
    from app.services.context_compressor import compress_docs

    compressed = compress_docs(
        requirements=requirements, prototype=prototype, technical=technical_context,
        budget=5000, focus_keywords=[module_title],
    )
    parts = []
    if compressed["requirements"]:
        parts.append(f"## 需求文档（摘要）\n{compressed['requirements']}")
    if compressed["prototype"]:
        parts.append(f"## 产品原型（摘要）\n{compressed['prototype']}")
    if all_modules_summary:
        parts.append(f"## 全部模块概览\n{all_modules_summary}")
    if compressed["technical"]:
        parts.append(f"## 技术方案（摘要）\n{compressed['technical']}")
    if discussion_context:
        parts.append(f"## 前置阶段讨论要点\n{discussion_context}")
    parts.append(f"## 当前模块\n**{module_title}**\n\n{module_description}")
    parts.append("请只输出任务条目标题清单。")
    prompt = "\n\n".join(parts)

    result = await _call_json(
        TASK_BREAKDOWN_TITLES_SYSTEM,
        prompt,
        max_tokens=2048,
        temperature=0.1,
        model=model,
    )
    items = result.get("items") or []
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title", "") or "").strip()
        if title:
            cleaned.append({
                "title": title,
                "depends_on": item.get("depends_on", []),
            })
    return cleaned


async def expand_task_item(
    module_title: str,
    module_description: str,
    item_title: str,
    manifest_titles: list[str],
    technical_context: str = "",
    requirements: str = "",
    prototype: str = "",
    all_modules_summary: str = "",
    discussion_context: str = "",
    model: str | None = None,
) -> dict:
    from app.services.context_compressor import compress_docs

    compressed = compress_docs(
        requirements=requirements, prototype=prototype, technical=technical_context,
        budget=5000, focus_keywords=[module_title, item_title],
    )
    titles_text = "\n".join(f"- {t}" for t in manifest_titles)
    parts = [
        f"## 当前模块\n**{module_title}**\n\n{module_description}",
        f"## 条目清单（仅可引用这些标题作为依赖）\n{titles_text}",
        f"## 当前要展开的条目\n{item_title}",
    ]
    if compressed["requirements"]:
        parts.append(f"## 需求文档（摘要）\n{compressed['requirements']}")
    if compressed["prototype"]:
        parts.append(f"## 产品原型（摘要）\n{compressed['prototype']}")
    if all_modules_summary:
        parts.append(f"## 全部模块概览\n{all_modules_summary}")
    if compressed["technical"]:
        parts.append(f"## 技术方案（摘要）\n{compressed['technical']}")
    if discussion_context:
        parts.append(f"## 前置阶段讨论要点\n{discussion_context}")
    prompt = "\n\n".join(parts)

    result = await _call_json(
        TASK_BREAKDOWN_ITEM_SYSTEM,
        prompt,
        max_tokens=4096,
        temperature=0.1,
        model=model,
    )
    task = result.get("task") if isinstance(result, dict) else None
    if not isinstance(task, dict):
        raise ValueError("任务详情生成失败：返回格式缺少 task 对象")
    if not task.get("title"):
        task["title"] = item_title
    complexity = str(task.get("complexity") or "").strip().lower()
    ai_requires_design = task.get("requires_design_review")
    if isinstance(ai_requires_design, bool):
        task["requires_design_review"] = ai_requires_design
    else:
        # 兜底：如果模型未返回该字段，至少按复杂度给出合理初判
        task["requires_design_review"] = complexity in ("medium", "high", "critical")
    task["design_review_reason"] = str(task.get("design_review_reason") or "").strip()
    return task


TASK_BREAKDOWN_BATCH_SYSTEM = """你是项目架构师。请根据提供的条目清单，逐条输出完整的任务详情。

输出严格 JSON：
{
  "tasks": [
    {
      "title": "与输入条目一致的标题",
      "description": "完整的自包含描述",
      "type": "feature|bug|test|e2e_test|integration_test|deploy|refactor|docs|security|performance",
      "priority": 1,
      "suggested_role": "junior|mid|senior|devops",
      "complexity": "low|medium|high|critical",
      "requires_design_review": false,
      "design_review_reason": "",
      "estimated_hours": 0.5,
      "input_files": [],
      "output_files": [],
      "acceptance_criteria": []
    }
  ]
}

要求：
1) tasks 数组顺序与输入条目顺序一致，数量必须相同
2) 每个条目的 title 必须与输入完全一致
3) 每个条目自包含：描述、文件清单、验收标准都要完整
4) dependencies 留空（由调用方根据 Phase A 的 depends_on 索引统一解析）
5) 只输出 JSON 本体，不要解释
"""


async def expand_task_batch(
    module_title: str,
    module_description: str,
    item_titles: list[str],
    manifest_titles: list[str],
    technical_context: str = "",
    requirements: str = "",
    prototype: str = "",
    all_modules_summary: str = "",
    discussion_context: str = "",
    model: str | None = None,
) -> list[dict]:
    """Batch 展开：一次请求处理多个任务条目（推荐 3-5 条），降低 API 调用成本。

    返回与 item_titles 顺序一致的 tasks 列表。
    单条失败时返回该条目为 None，由调用方决定是否降级到 expand_task_item 单条重试。
    """
    from app.services.context_compressor import compress_docs

    compressed = compress_docs(
        requirements=requirements, prototype=prototype, technical=technical_context,
        budget=5000, focus_keywords=[module_title] + item_titles,
    )

    items_text = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(item_titles))
    manifest_text = "\n".join(f"- {t}" for t in manifest_titles)

    parts = [
        f"## 当前模块\n**{module_title}**\n\n{module_description}",
        f"## 全部条目清单（供参考，不可超出此范围）\n{manifest_text}",
        f"## 请展开以下 {len(item_titles)} 个条目\n{items_text}",
    ]
    if compressed["requirements"]:
        parts.append(f"## 需求文档（摘要）\n{compressed['requirements']}")
    if compressed["prototype"]:
        parts.append(f"## 产品原型（摘要）\n{compressed['prototype']}")
    if all_modules_summary:
        parts.append(f"## 全部模块概览\n{all_modules_summary}")
    if compressed["technical"]:
        parts.append(f"## 技术方案（摘要）\n{compressed['technical']}")
    if discussion_context:
        parts.append(f"## 前置阶段讨论要点\n{discussion_context}")

    prompt = "\n\n".join(parts)

    result = await _call_json(
        TASK_BREAKDOWN_BATCH_SYSTEM,
        prompt,
        max_tokens=min(8192, 2048 * len(item_titles)),
        temperature=0.1,
        model=model,
    )
    tasks = result.get("tasks") if isinstance(result, dict) else None
    if not isinstance(tasks, list):
        raise ValueError("batch 展开失败：返回格式缺少 tasks 数组")

    # 校验数量是否匹配，不匹配时尝试补全或报错
    if len(tasks) != len(item_titles):
        logger.warning(f"batch 展开数量不匹配：期望 {len(item_titles)} 条，实际 {len(tasks)} 条")
        # 尝试按 title 匹配
        title_to_task = {}
        for t in tasks:
            if isinstance(t, dict) and t.get("title"):
                title_to_task[t["title"].strip()] = t
        matched = []
        for title in item_titles:
            matched.append(title_to_task.get(title.strip()))
        return matched

    # 填充默认值 + 校验 design_review
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            tasks[i] = None
            continue
        expected_title = item_titles[i]
        if not task.get("title"):
            task["title"] = expected_title
        complexity = str(task.get("complexity") or "").strip().lower()
        ai_requires_design = task.get("requires_design_review")
        if isinstance(ai_requires_design, bool):
            pass
        else:
            task["requires_design_review"] = complexity in ("medium", "high", "critical")
        task["design_review_reason"] = str(task.get("design_review_reason") or "").strip()

    return tasks


# ── Stage 5+: 任务指令生成与代码审查 ──

TASK_INSTRUCTION_SYSTEM = """你是技术主管，为编码工程师准备自包含的任务指令（像工单一样明确具体）。
输出 Markdown，包含：任务目标、技术要求、文件清单、验收标准、质量要求（TDD+Lint+测试覆盖）、Git 规范。
只提供完成本任务所需的信息，不展开整体架构。团队共识（代码规范、架构约定）已在角色模板中，无需重复。"""

TASK_INSTRUCTION_JUNIOR = """你是技术主管，为初级工程师准备任务指令。任务较简单，指令需更具体：
- 明确列出要改动的文件/方法
- 给出关键约束（如必须校验参数、必须写测试）
- 验收标准要可逐条勾选
输出 Markdown，简洁明了。"""


async def generate_task_instruction(
    task_title: str, task_description: str, acceptance_criteria: list[str],
    input_docs: str = "", knowledge_ctx: str = "",
    ref_id: str = "", git_branch: str = "", ref_docs: list[dict] | None = None,
    iteration_title: str = "",
    suggested_role: str = "", complexity: str = "medium",
) -> str:
    """Leader 不做技术生成：任务指令改为模板化拼装，避免额外模型调用。"""
    header = f"[{ref_id}] {task_title}" if ref_id else task_title
    criteria = acceptance_criteria or []
    criteria_text = "\n".join(f"- {c}" for c in criteria) if criteria else "- 无（请补充）"
    branch_text = f"- 分支：`{git_branch}`" if git_branch else "- 分支：按项目约定创建"
    commit_text = f"- 提交信息：`[{ref_id}] <描述>`" if ref_id else "- 提交信息：按仓库规范"

    role_hint = suggested_role or "mid"
    complexity_hint = complexity or "medium"
    docs_text = input_docs.strip() if input_docs.strip() else "无"
    exp_text = knowledge_ctx.strip() if knowledge_ctx.strip() else "无"

    return (
        f"# 任务指令\n\n"
        f"## 任务\n"
        f"{header}\n\n"
        f"## 目标\n"
        f"{task_description}\n\n"
        f"## 执行约束\n"
        f"- 角色建议：`{role_hint}`\n"
        f"- 复杂度：`{complexity_hint}`\n"
        f"- 仅实现本任务范围，不扩散改动\n"
        f"- 保持与既有代码风格一致\n\n"
        f"## 相关资料\n"
        f"{docs_text}\n\n"
        f"## 经验参考\n"
        f"{exp_text}\n\n"
        f"## 验收标准\n"
        f"{criteria_text}\n\n"
        f"## Git 要求\n"
        f"{branch_text}\n"
        f"{commit_text}\n"
    )


_CODE_REVIEW_ROLE_HINTS = {
    "architect": "额外检查：架构一致性、模块边界、接口契约、可扩展性、技术债务。",
    "senior": "额外检查：端到端一致性（API → 页面 → 数据流）、边界条件、安全性、异常处理。",
    "mid": "额外检查：功能正确性、接口调用与响应格式、组件与 API 对齐、基础测试覆盖。",
    "junior": "额外检查：基础逻辑正确性、语法规范、简单边界条件、是否引入明显 bug。",
    "tester": "额外检查：测试覆盖关键路径、断言是否充分、边界条件、端到端场景。",
    "devops": "额外检查：配置安全性、环境变量、健康检查、依赖版本、部署脚本正确性。",
}

CODE_REVIEW_SYSTEM = """代码审查。审查维度：测试覆盖(无测试→critical)、正确性、安全性、代码质量、Lint。
输出JSON: {"approved":bool,"score":1-10,"has_tests":bool,"test_coverage_note":"","issues":[{"severity":"critical/warning/suggestion","description":"","file":"","suggestion":""}],"summary":""}"""


async def review_code(
    task_description: str
    , code_diff: str
    , acceptance_criteria: list[str] | None = None
    , suggested_role: str = "",
) -> dict:
    criteria_text = ""
    if acceptance_criteria:
        criteria_text = "\n验收标准：\n" + "\n".join(f"- {c}" for c in acceptance_criteria)
    role_hint = _CODE_REVIEW_ROLE_HINTS.get(suggested_role, "") if suggested_role else ""
    role_extra = f"\n\n本任务角色额外关注：{role_hint}" if role_hint else ""
    from app.services.context_compressor import compress_code_diff
    trimmed_diff = compress_code_diff(code_diff, max_tokens=3000)
    prompt = f"任务：{task_description}{criteria_text}{role_extra}\n\n代码变更：\n{trimmed_diff}"
    return await _call_json(CODE_REVIEW_SYSTEM, prompt)


CHANGE_IMPACT_SYSTEM = """你是一个资深架构师，负责分析需求变更对现有任务的影响。

给定变更描述和当前任务列表，你需要：
1. 识别哪些任务会受到影响
2. 评估影响比例
3. 给出建议：在当前迭代追加(append) 还是 终止当前迭代新建(terminate_and_new)

判断标准：
- 受影响任务 < 20%：建议 append
- 受影响任务 20%-50%：建议 append，但提醒风险
- 受影响任务 > 50%：建议 terminate_and_new

输出 JSON：
{
  "affected_task_ids": ["受影响的任务ID列表"],
  "affected_ratio": 0.35,
  "recommendation": "append 或 terminate_and_new",
  "reason": "建议原因（简洁）",
  "details": "详细分析（每个受影响任务为什么受影响）"
}"""


async def analyze_change_impact(
    change_description: str,
    tasks: list[dict],
    project_name: str,
    project_type: str,
) -> dict:
    tasks_text = "\n".join(
        f"- [{t['id']}] {t['ref_id']} | {t['title']} | 状态:{t['status']} | 角色:{t['suggested_role']} | 产出:{','.join(t.get('output_files', []))}"
        for t in tasks
    )
    prompt = f"""项目: {project_name} ({project_type})

## 变更描述
{change_description}

## 当前任务列表（共 {len(tasks)} 个）
{tasks_text}"""

    return await _call_json(CHANGE_IMPACT_SYSTEM, prompt)


# ── Stage 6: 代码质量报告 ──

QUALITY_REPORT_SYSTEM = """你是一个资深代码质量评估专家，负责生成类似 SonarQube 风格的代码质量报告。

基于项目的任务描述、代码审核记录和技术方案，评估整体代码质量。

评分等级：A（优秀）/ B（良好）/ C（一般）/ D（较差）/ E（差）

评估维度：
1. reliability（可靠性）：代码是否健壮，错误处理是否完善
2. security（安全性）：是否有安全漏洞、敏感信息泄露
3. maintainability（可维护性）：代码结构、命名规范、复杂度
4. coverage_estimate（测试覆盖率估算）：基于任务中测试相关的描述估算
5. duplication（代码重复度）：是否有重复逻辑

输出 JSON：
{
  "overall_score": "A",
  "summary": "总体评价（2-3句话）",
  "dimensions": {
    "reliability": {"score": "A", "issues": 0, "detail": "评价说明"},
    "security": {"score": "A", "issues": 0, "detail": "评价说明"},
    "maintainability": {"score": "B", "issues": 2, "detail": "评价说明"},
    "coverage_estimate": {"score": "B", "issues": 0, "detail": "评价说明"},
    "duplication": {"score": "A", "issues": 0, "detail": "评价说明"}
  },
  "issues": [
    {"severity": "minor", "type": "maintainability", "file": "相关文件", "description": "问题描述"}
  ],
  "recommendations": ["改进建议1", "改进建议2"]
}"""


async def generate_quality_report(
    tasks_summary: str,
    review_records: str,
    technical_doc: str = "",
) -> dict:
    prompt = f"""## 项目任务完成情况
{tasks_summary}

## 代码审核记录
{review_records}

## 技术方案（参考）
{technical_doc or '无'}

请基于以上信息生成代码质量评估报告。"""
    return await _call_json(QUALITY_REPORT_SYSTEM, prompt, max_tokens=4096)


# ── 自动测试计划生成 ──

TEST_PLAN_SYSTEM = """你是一个资深的测试工程师。根据提供的 API 路由定义和需求文档，生成完整的测试计划和可执行测试代码。

要求：
1. 生成 pytest 格式的 Python 测试代码
2. 覆盖所有 API 端点的正常流程和关键异常路径
3. 测试用例按业务流程组织（如注册→登录→操作→验证）
4. 使用 httpx 作为 HTTP 客户端
5. 包含 fixture 管理（如 base_url、auth_token）
6. 不要 mock，直接调真实 API（集成测试）

输出 JSON：
{
  "test_file": "完整的 pytest 测试代码（Python 字符串）",
  "test_cases": [
    {"name": "测试用例名", "description": "测试内容描述", "api_endpoints": ["涉及的 API"]}
  ],
  "estimated_duration": "预估执行时间（如 2-5 分钟）",
  "prerequisites": ["前置条件说明"]
}"""


async def generate_test_plan(
    api_routes: str,
    requirements: str = "",
    technical_doc: str = "",
) -> dict:
    prompt = f"""## API 路由定义
{api_routes}

## 需求文档
{requirements or '无'}

## 技术方案
{technical_doc or '无'}

请基于以上信息生成 API 集成测试计划和可执行代码。重点覆盖核心业务流程。"""
    return await _call_json(TEST_PLAN_SYSTEM, prompt, max_tokens=8192)


# ── E2E 测试用例生成（自然语言） ──

E2E_PLAN_SYSTEM = """你是一个资深的 QA 工程师。根据提供的前端页面信息和需求文档，生成自然语言描述的端到端测试用例。

每条用例需要：
1. 明确的操作步骤（自然语言描述，不是代码）
2. 每一步的期望结果
3. 标注哪些步骤需要截图验证

输出 JSON：
{
  "test_cases": [
    {
      "name": "测试用例名称",
      "priority": "P0/P1/P2",
      "steps": [
        {"action": "操作描述", "expected": "期望结果", "screenshot": true/false}
      ]
    }
  ],
  "estimated_duration_human": "人类手动执行预估时间",
  "estimated_duration_ai": "AI 自动执行预估时间",
  "estimated_tokens": "AI 执行预估 token 消耗"
}"""


async def generate_e2e_test_plan(
    pages_info: str,
    requirements: str = "",
) -> dict:
    prompt = f"""## 前端页面信息
{pages_info}

## 需求文档
{requirements or '无'}

请生成核心用户路径的端到端测试用例（5-10 条），按优先级排序。"""
    return await _call_json(E2E_PLAN_SYSTEM, prompt, max_tokens=8192)


# ── 文档骨架生成 ──

DOC_SKELETON_SYSTEM = """你是一个技术文档工程师。根据提供的项目代码结构信息，生成用户手册和管理员手册的 Markdown 骨架。

要求：
1. 从路由/API 代码推导所有操作页面和功能入口
2. 从组件代码提取表单字段名称、按钮文案、验证规则
3. 从 docker-compose / 环境变量推导部署配置
4. 在需要截图的位置使用 <!-- screenshot: 描述 --> 占位符
5. 操作步骤要具体，包含"点击XXX按钮"、"填写XXX"这种粒度
6. 分用户手册和管理员手册两个文档

输出 JSON：
{
  "user_manual": "用户手册完整 Markdown 内容",
  "admin_manual": "管理员手册完整 Markdown 内容"
}"""


async def generate_doc_skeleton(
    project_structure: str,
    api_summary: str = "",
    deploy_config: str = "",
    requirements: str = "",
) -> dict:
    prompt = f"""{doc_time_prompt_block()}
## 项目结构与页面信息
{project_structure}

## API 概要
{api_summary or '无'}

## 部署配置
{deploy_config or '无'}

## 需求文档
{requirements or '无'}

请生成用户手册和管理员手册的完整 Markdown 骨架，在需要截图的位置用 <!-- screenshot: 描述 --> 占位。"""
    return await _call_json(DOC_SKELETON_SYSTEM, prompt, max_tokens=16384)
