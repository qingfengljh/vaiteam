"""
上下文管理器 — 智能管理 AI 对话的上下文窗口

核心策略：
1. 按 token 估算（而非条数）判断是否需要压缩
2. 超过阈值时，将早期对话压缩为结构化摘要
3. 最终发给模型的是 [摘要] + [最近N条原始对话]
"""

import logging

logger = logging.getLogger(__name__)

FALLBACK_CONTEXT_WINDOWS = {
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-3": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "deepseek-chat": 131_072,
    "deepseek-coder": 131_072,
    "deepseek-reasoner": 131_072,
    "qwen-max": 32_000,
    "qwen-plus": 32_000,
    "glm-4": 128_000,
}

DEFAULT_CONTEXT_WINDOW = 32_000
CHARS_PER_TOKEN = 2.5
CONTEXT_USAGE_THRESHOLD = 0.5
RESERVED_FOR_RESPONSE = 4096
KEEP_RECENT_MESSAGES = 6


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    total += 1500  # 图片约 1000-2000 token
        total += 4
    return total


def get_context_window(model: str) -> int:
    """获取模型上下文窗口：优先从供应商配置读取，兜底用硬编码默认值"""
    try:
        from app.services.model_pool import get_context_window as pool_get_window
        dynamic = pool_get_window(model)
        if dynamic:
            return dynamic
    except Exception:
        pass

    for prefix, window in FALLBACK_CONTEXT_WINDOWS.items():
        if model.startswith(prefix):
            return window
    return DEFAULT_CONTEXT_WINDOW


SUMMARY_SYSTEM = """你是一个对话摘要专家。请将以下对话压缩为结构化摘要，保留所有关键信息。

输出格式：
## 对话摘要

### 已确认的决策
- （列出所有已达成共识的决策点）

### 用户明确提出的要求（若对话中有）
- 单独保留：用户或助理已对齐的**报价/商务边界**、文档章节模板、必须写或不写的内容等；生成文档依赖摘要时不得丢失，不得合并成泛泛一句
- 若出现 **待论证 / 待补充** 类约定（缺什么输入、后续如何迭代），摘要须逐条保留，便于补信息后再次生成或审阅对话深化

### 已约定的文档结构（若对话中出现）
- 必须逐条保留：章节标题与顺序、分篇文档清单、助理给出的统一写作模板/大纲要点；摘要中不得合并或丢弃，便于后续按结构生成正式文档
- 若约定「报价」=业务基线（范围、交付物、工作量评估维度、不写单价总价），摘要须保留该含义与模板要点，不得合并成一句空话
- 若对话出现**具体**估算数字、人天或费用区间，摘要须保留这些数字与假设

### 关键需求/要点
- （列出讨论中明确的需求、约束、目标）

### 待确认事项
- （列出尚未确定的问题）

### 讨论要点
- （其他重要的讨论内容摘要）

要求：
- 不遗漏任何关键决策和需求细节
- 用简洁的条目形式，不要长段落
- 保留具体的技术选型、数字指标等细节
- 凡可能影响「生成文档」的约定（章节、清单、约束、表格列、必须写/不写），须**逐条**列出，禁止用「等相关内容」一笔带过，以免后续生成缺斤少两"""


async def prepare_messages(
    messages: list[dict],
    system_prompt: str,
    model: str | None = None,
    keep_recent: int | None = None,
) -> list[dict]:
    """
    智能准备发送给模型的消息列表。
    如果对话过长，自动压缩早期消息为摘要。

    keep_recent: 压缩时保留的尾部原始消息条数；None 则用默认值（对话场景较省，文档生成可传更大值）。

    返回处理后的 messages 列表（不含 system prompt）。
    """
    if not messages:
        return messages

    from app.services.model_pool import resolve_model
    model_name = model or resolve_model("leader")
    context_window = get_context_window(model_name)
    available_tokens = context_window - RESERVED_FOR_RESPONSE - estimate_tokens(system_prompt)
    msg_tokens = estimate_messages_tokens(messages)

    threshold = int(available_tokens * CONTEXT_USAGE_THRESHOLD)

    if msg_tokens <= threshold:
        return messages

    k_recent = keep_recent if keep_recent is not None else KEEP_RECENT_MESSAGES

    logger.info(
        f"Context management triggered: {msg_tokens} tokens > threshold {threshold} "
        f"(window={context_window}, model={model_name}, msgs={len(messages)}, keep_recent={k_recent})"
    )

    if len(messages) <= k_recent:
        return messages

    early_messages = messages[:-k_recent]
    recent_messages = messages[-k_recent:]

    summary = await _summarize_messages(early_messages, model_name)

    return [
        {"role": "system", "content": f"[以下是之前对话的摘要]\n\n{summary}\n\n[摘要结束，以下是最近的对话]"},
    ] + recent_messages


async def _summarize_messages(messages: list[dict], model: str) -> str:
    conversation = []
    for m in messages:
        role_label = "用户" if m["role"] == "user" else "AI"
        conversation.append(f"**{role_label}**: {m['content']}")
    conversation_text = "\n\n".join(conversation)

    prompt = f"请摘要以下对话（共 {len(messages)} 条消息）：\n\n{conversation_text}"

    from app.services.ai_leader import _call
    try:
        result = await _call(SUMMARY_SYSTEM, prompt, max_tokens=2048, model=model)
        logger.info(f"Conversation summarized: {len(messages)} msgs -> {len(result)} chars")
        return result
    except Exception as e:
        logger.warning(f"Summarization failed, falling back to truncation: {e}")
        return _fallback_truncate(messages)


def _fallback_truncate(messages: list[dict]) -> str:
    """摘要失败时的降级方案：取早期消息的前 200 字拼接"""
    from app.services.context_compressor import truncate
    parts = []
    for m in messages[:10]:
        role_label = "用户" if m["role"] == "user" else "AI"
        content = truncate(m["content"], max_tokens=80)
        parts.append(f"- {role_label}: {content}")
    return "## 早期对话概要（自动截取）\n\n" + "\n".join(parts)
