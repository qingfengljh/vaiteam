"""
上下文压缩器 — 独立的 Token 优化工具层

对业务层透明：输入原始内容，输出压缩后的内容。
不优化时原样返回，优化后返回摘要或结构化数据。

用法：
    from app.services.context_compressor import compress, compress_docs, compress_code

    # 自动选策略
    result = await compress(long_text, max_tokens=4000)

    # 指定策略
    result = await compress(long_text, strategy="truncate", max_tokens=2000)

    # 文档专用（保留结构）
    result = await compress_docs(requirements=req, prototype=proto, technical=tech, budget=6000)

    # 代码 diff 专用
    result = compress_code_diff(diff_text, max_tokens=3000)
"""

import logging
from enum import Enum

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 2.5


class Strategy(str, Enum):
    PASSTHROUGH = "passthrough"
    TRUNCATE = "truncate"
    EXTRACT = "extract"
    SUMMARIZE = "summarize"
    STRUCTURAL = "structural"


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _tokens_to_chars(tokens: int) -> int:
    return int(tokens * CHARS_PER_TOKEN)


def truncate(text: str, max_tokens: int, tail: str = "\n...(truncated)") -> str:
    """硬截断，保留头部"""
    if _estimate_tokens(text) <= max_tokens:
        return text
    limit = _tokens_to_chars(max_tokens) - len(tail)
    return text[:limit] + tail


def truncate_middle(text: str, max_tokens: int, keep_head: float = 0.7) -> str:
    """保留头尾，截掉中间"""
    if _estimate_tokens(text) <= max_tokens:
        return text
    total_chars = _tokens_to_chars(max_tokens)
    head_chars = int(total_chars * keep_head)
    tail_chars = total_chars - head_chars - 30
    if tail_chars < 100:
        return text[:total_chars] + "\n...(truncated)"
    return text[:head_chars] + "\n\n...(middle truncated)...\n\n" + text[-tail_chars:]


def extract_sections(doc: str, keywords: list[str], max_tokens: int = 3000) -> str:
    """从文档中提取与关键词匹配的段落"""
    if _estimate_tokens(doc) <= max_tokens:
        return doc

    sections = doc.split("\n## ")
    if len(sections) <= 1:
        sections = doc.split("\n# ")

    kw_lower = [k.lower() for k in keywords]
    scored = []
    for sec in sections:
        score = sum(1 for kw in kw_lower if kw in sec.lower())
        scored.append((score, sec))

    scored.sort(key=lambda x: -x[0])

    result = []
    total = 0
    char_limit = _tokens_to_chars(max_tokens)
    for score, sec in scored:
        if total + len(sec) > char_limit:
            remain = char_limit - total
            if remain > 200:
                result.append(sec[:remain] + "...")
            break
        result.append(sec)
        total += len(sec)

    return "\n## ".join(result) or truncate(doc, max_tokens)


async def summarize(text: str, max_tokens: int = 1500, hint: str = "") -> str:
    """用 AI 摘要（优先本地模型）。失败时降级为截断。"""
    if _estimate_tokens(text) <= max_tokens:
        return text

    try:
        from app.services.ai_leader import _call
        system = "用简洁条目形式压缩以下内容，保留所有关键信息（技术选型、数字指标、接口定义）。"
        if hint:
            system += f" 重点保留与「{hint}」相关的内容。"
        prompt = text

        result = await _call(system, prompt, max_tokens=max_tokens)
        logger.info(f"Context compressed via LLM: {len(text)}→{len(result)} chars")
        return result
    except Exception as e:
        logger.warning(f"Summarization failed, falling back to truncate: {e}")
        return truncate(text, max_tokens)


async def compress(
    text: str,
    max_tokens: int = 4000,
    strategy: str = "auto",
    keywords: list[str] | None = None,
    hint: str = "",
) -> str:
    """
    通用入口。

    strategy:
      - passthrough: 原样返回
      - truncate: 硬截断
      - extract: 按关键词提取段落
      - summarize: AI 摘要
      - auto: 根据内容长度自动选择
    """
    if strategy == Strategy.PASSTHROUGH or not text:
        return text

    current_tokens = _estimate_tokens(text)
    if current_tokens <= max_tokens:
        return text

    if strategy == "auto":
        ratio = current_tokens / max_tokens
        if ratio < 2:
            return truncate(text, max_tokens)
        if keywords and ratio < 5:
            return extract_sections(text, keywords, max_tokens)
        if ratio < 4:
            return truncate_middle(text, max_tokens)
        return await summarize(text, max_tokens, hint=hint)

    if strategy == Strategy.TRUNCATE:
        return truncate(text, max_tokens)
    if strategy == Strategy.EXTRACT:
        return extract_sections(text, keywords or [], max_tokens)
    if strategy == Strategy.SUMMARIZE:
        return await summarize(text, max_tokens, hint=hint)

    return truncate(text, max_tokens)


def compress_docs(
    requirements: str = "",
    prototype: str = "",
    technical: str = "",
    budget: int = 6000,
    focus_keywords: list[str] | None = None,
) -> dict[str, str]:
    """
    多文档联合压缩，按重要性分配 token 预算。
    返回 {"requirements": ..., "prototype": ..., "technical": ...}
    """
    docs = {"requirements": requirements, "prototype": prototype, "technical": technical}
    total_tokens = sum(_estimate_tokens(v) for v in docs.values() if v)

    if total_tokens <= budget:
        return docs

    weights = {"technical": 0.5, "requirements": 0.3, "prototype": 0.2}
    result = {}
    for key, text in docs.items():
        if not text:
            result[key] = ""
            continue
        doc_budget = int(budget * weights.get(key, 0.3))
        if focus_keywords and key == "technical":
            result[key] = extract_sections(text, focus_keywords, doc_budget)
        else:
            result[key] = truncate(text, doc_budget)

    return result


def compress_code_diff(diff: str, max_tokens: int = 3000) -> str:
    """代码 diff 压缩：保留文件头和关键变更，去掉大段未改动的上下文"""
    if _estimate_tokens(diff) <= max_tokens:
        return diff

    lines = diff.split("\n")
    result = []
    total = 0
    char_limit = _tokens_to_chars(max_tokens) - 30
    skipping = False

    for line in lines:
        is_header = line.startswith("diff ") or line.startswith("---") or line.startswith("+++") or line.startswith("@@")
        is_change = line.startswith("+") or line.startswith("-")

        if is_header or is_change:
            if skipping:
                result.append("  ... (context omitted)")
                skipping = False
            if total + len(line) > char_limit:
                result.append("...(diff truncated)")
                break
            result.append(line)
            total += len(line) + 1
        else:
            skipping = True

    return "\n".join(result)


def compress_task_list(tasks: list[dict], max_tokens: int = 2000) -> str:
    """任务列表压缩：只保留关键字段"""
    lines = []
    total = 0
    char_limit = _tokens_to_chars(max_tokens)
    for t in tasks:
        line = f"- [{t.get('ref_id', '?')}] {t.get('title', '')} | {t.get('status', '')} | {t.get('suggested_role', '')}"
        if total + len(line) > char_limit:
            lines.append(f"...(+{len(tasks) - len(lines)} more)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)
