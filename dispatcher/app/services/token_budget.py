"""
Token 预算管理 — 按优先级分配知识上下文空间

Phase 2-4: 给每个任务设定 knowledge_budget，按优先级分配上下文空间。
优先级：架构约束 > 相关经验 > 通用知识

使用方式：
    from app.services.token_budget import allocate_budget, KnowledgeSlice

    slices = [
        KnowledgeSlice(content=escalation_ctx, priority=1, label="escalation"),
        KnowledgeSlice(content=exp_ctx, priority=2, label="experience"),
        KnowledgeSlice(content=snippets, priority=2, label="snippets"),
        KnowledgeSlice(content=related_docs, priority=3, label="docs"),
    ]
    allocated = allocate_budget(slices, max_chars=8000)
"""

import logging

logger = logging.getLogger(__name__)

# 默认预算值（字符数，约等于 token 数 × 4）
DEFAULT_KNOWLEDGE_BUDGET = 12000  # ~3000 tokens


class KnowledgeSlice:
    """知识片段：包含内容、优先级和标签"""

    def __init__(self, content: str, priority: int, label: str = ""):
        self.content = content or ""
        self.priority = priority  # 数字越小优先级越高（1=最高）
        self.label = label

    @property
    def length(self) -> int:
        return len(self.content)


def _truncate(text: str, max_len: int) -> str:
    """按段落截断，尽量保留完整段落"""
    if len(text) <= max_len:
        return text
    # 尝试在最后一个完整段落处截断
    trunc = text[:max_len]
    last_para = trunc.rfind("\n\n")
    if last_para > max_len * 0.5:
        return trunc[:last_para] + "\n\n..."
    last_line = trunc.rfind("\n")
    if last_line > max_len * 0.5:
        return trunc[:last_line] + "\n..."
    return trunc + "..."


def allocate_budget(
    slices: list[KnowledgeSlice],
    max_chars: int = DEFAULT_KNOWLEDGE_BUDGET,
    min_per_slice: int = 200,
) -> list[str]:
    """
    按优先级分配知识预算。

    策略：
    1. 按 priority 分组（1=最高）
    2. 同优先级均分剩余预算
    3. 每片内容按 budget 截断
    4. 返回分配后的内容列表（过滤空内容）

    Args:
        slices: 知识片段列表
        max_chars: 总字符预算
        min_per_slice: 每片最小保留字符（低于此值的内容被跳过）

    Returns:
        分配后的内容字符串列表
    """
    if not slices:
        return []

    # 过滤空内容
    non_empty = [s for s in slices if s.length > 0]
    if not non_empty:
        return []

    # 按优先级分组
    groups: dict[int, list[KnowledgeSlice]] = {}
    for s in non_empty:
        groups.setdefault(s.priority, []).append(s)

    remaining = max_chars
    result: list[str] = []

    for prio in sorted(groups.keys()):
        group = groups[prio]
        group_total = sum(s.length for s in group)

        if remaining <= 0:
            logger.debug(f"Token budget exhausted before priority {prio}")
            break

        if group_total <= remaining:
            # 整组都能放下
            for s in group:
                result.append(s.content)
            remaining -= group_total
            continue

        # 需要截断：同优先级均分
        budget_per_slice = remaining // len(group)
        if budget_per_slice < min_per_slice:
            # 剩余空间太小，只保留第一片的最小内容
            budget_per_slice = min(remaining, min_per_slice)
            for i, s in enumerate(group):
                alloc = budget_per_slice if i == 0 else 0
                if alloc > 0 and s.length > 0:
                    result.append(_truncate(s.content, alloc))
                remaining -= alloc
            break

        for s in group:
            if s.length <= budget_per_slice:
                result.append(s.content)
                remaining -= s.length
            else:
                result.append(_truncate(s.content, budget_per_slice))
                remaining -= budget_per_slice

    total_used = sum(len(r) for r in result)
    logger.debug(
        f"Knowledge budget allocated: {total_used}/{max_chars} chars, "
        f"slices={len(result)}/{len(non_empty)}"
    )
    return result
