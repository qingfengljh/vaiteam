from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Message, Project, Task, TaskLog, TokenUsageLog


REVIEW_CATEGORY = "project_review"
REVIEW_TAG = "project-review"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percent(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((part / total) * 100, 1)


async def _task_status_counter(session: AsyncSession, project_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(Task.status, func.count())
        .where(Task.project_id == project_id)
        .group_by(Task.status)
    )
    return {str(status or "unknown"): int(count or 0) for status, count in rows.all()}


async def _task_complexity_counter(session: AsyncSession, project_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(Task.complexity, func.count())
        .where(Task.project_id == project_id)
        .group_by(Task.complexity)
    )
    return {str(level or "unknown"): int(count or 0) for level, count in rows.all()}


async def _token_stats(session: AsyncSession, project_id: str) -> dict:
    totals = await session.execute(
        select(
            func.coalesce(func.sum(TokenUsageLog.input_tokens), 0),
            func.coalesce(func.sum(TokenUsageLog.output_tokens), 0),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0.0),
        ).where(TokenUsageLog.project_id == project_id)
    )
    input_tokens, output_tokens, cost_usd = totals.one()

    by_model_q = await session.execute(
        select(
            TokenUsageLog.model,
            func.coalesce(func.sum(TokenUsageLog.input_tokens + TokenUsageLog.output_tokens), 0).label("tokens"),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0.0).label("cost"),
        )
        .where(TokenUsageLog.project_id == project_id)
        .group_by(TokenUsageLog.model)
        .order_by(func.coalesce(func.sum(TokenUsageLog.cost_usd), 0.0).desc())
        .limit(8)
    )
    by_model = [
        {"model": str(model or "unknown"), "tokens": int(tokens or 0), "cost_usd": float(cost or 0.0)}
        for model, tokens, cost in by_model_q.all()
    ]
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cost_usd": round(float(cost_usd or 0.0), 6),
        "by_model": by_model,
    }


async def _log_counter(session: AsyncSession, project_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(TaskLog.action, func.count())
        .join(Task, Task.id == TaskLog.task_id)
        .where(Task.project_id == project_id)
        .group_by(TaskLog.action)
    )
    return {str(action or "unknown"): int(count or 0) for action, count in rows.all()}


async def _top_blockers(session: AsyncSession, project_id: str) -> list[dict]:
    q = await session.execute(
        select(Task.ref_id, Task.title, Task.updated_at, Task.context)
        .where(Task.project_id == project_id, Task.status == "blocked")
        .order_by(Task.updated_at.desc())
        .limit(10)
    )
    items = []
    for ref_id, title, updated_at, ctx in q.all():
        context = ctx or {}
        items.append(
            {
                "ref_id": ref_id or "",
                "title": title or "",
                "reason": str(context.get("blocked_reason") or context.get("last_review_comments") or "").strip(),
                "updated_at": updated_at.isoformat() if updated_at else None,
            }
        )
    return items


async def _decision_timeline(session: AsyncSession, project_id: str) -> list[dict]:
    q = await session.execute(
        select(Message.role, Message.content, Message.created_at)
        .where(Message.project_id == project_id)
        .order_by(Message.created_at.desc())
        .limit(120)
    )
    decision_markers = ("决定", "决策", "批准", "拒绝", "风险", "方案", "review")
    timeline = []
    for role, content, created_at in q.all():
        text = (content or "").strip()
        if not text:
            continue
        if not any(marker in text for marker in decision_markers):
            continue
        timeline.append(
            {
                "at": created_at.isoformat() if created_at else None,
                "role": role or "unknown",
                "content": text[:220],
            }
        )
        if len(timeline) >= 12:
            break
    return timeline


def _recommendations(status_counter: dict[str, int], action_counter: dict[str, int], blocked_count: int) -> list[str]:
    recs: list[str] = []
    review_rejected = int(action_counter.get("review_rejected", 0))
    reentry_failed = int(action_counter.get("reentry_successive_run_failed", 0))
    failed = int(status_counter.get("failed", 0))
    done = int(status_counter.get("done", 0))

    if blocked_count > 0:
        recs.append("优先清理 blocked 队列，要求每个阻塞项绑定负责人和截止时间。")
    if review_rejected >= 3:
        recs.append("审核拒绝次数偏高，建议在编码前固化验收清单并执行自检脚本。")
    if reentry_failed > 0:
        recs.append("存在续跑失败链路，建议把环境自检和恢复策略前置到任务启动阶段。")
    if failed > 0 and done > 0 and failed >= max(2, int(done * 0.2)):
        recs.append("失败比例偏高，建议收紧任务粒度并拆分高复杂度任务。")
    if not recs:
        recs.append("当前执行质量稳定，下一轮重点优化成本与自动化覆盖率。")
    return recs


def _build_markdown(payload: dict) -> str:
    project = payload["project"]
    task_stats = payload["task_stats"]
    token_stats = payload["token_stats"]
    timeline = payload["timeline"]
    blockers = payload["blockers"]
    recs = payload["recommendations"]

    model_lines = "\n".join(
        f"- `{m['model']}`: tokens={m['tokens']}, cost=￥{m['cost_usd']:.6f}" for m in token_stats["by_model"]
    ) or "- 无模型调用记录"
    timeline_lines = "\n".join(
        f"- {item['at']} [{item['role']}] {item['content']}" for item in timeline
    ) or "- 未提取到明确决策记录"
    blocker_lines = "\n".join(
        f"- [{item['ref_id']}] {item['title']} | {item['reason'] or '未记录原因'}" for item in blockers
    ) or "- 当前无 blocked 任务"
    rec_lines = "\n".join(f"- {line}" for line in recs)

    return (
        f"# 项目复盘报告\n\n"
        f"## 1. 项目概览\n"
        f"- 项目: {project['name']} (`{project['id']}`)\n"
        f"- 类型: {project['project_type']}\n"
        f"- 当前阶段: {project['current_stage']}\n"
        f"- 生成时间: {payload['generated_at']}\n"
        f"- 生成人: {payload['generated_by']}\n\n"
        f"## 2. 目标与结果对照\n"
        f"- 任务总数: {task_stats['total']}\n"
        f"- 已完成: {task_stats['done']} ({task_stats['done_rate']}%)\n"
        f"- 失败: {task_stats['failed']}\n"
        f"- 阻塞: {task_stats['blocked']}\n"
        f"- 待处理: {task_stats['pending_like']}\n\n"
        f"## 3. 关键决策时间线\n"
        f"{timeline_lines}\n\n"
        f"## 4. 质量与风险\n"
        f"- 审核拒绝次数: {payload['action_stats'].get('review_rejected', 0)}\n"
        f"- 重入失败次数: {payload['action_stats'].get('reentry_successive_run_failed', 0)}\n"
        f"- 自动审核次数: {payload['action_stats'].get('review_auto_mode', 0)}\n"
        f"- 人工审核等待次数: {payload['action_stats'].get('review_waiting_architect', 0)}\n\n"
        f"### 当前阻塞项\n"
        f"{blocker_lines}\n\n"
        f"## 5. 协作与流程观察\n"
        f"- 任务复杂度分布: {payload['complexity_stats']}\n"
        f"- 人类主导等待决策: {payload['action_stats'].get('collaboration_wait_human_decision', 0)}\n"
        f"- 任务重试次数: {payload['action_stats'].get('retry', 0)}\n\n"
        f"## 6. 成本复盘\n"
        f"- 输入 tokens: {token_stats['input_tokens']}\n"
        f"- 输出 tokens: {token_stats['output_tokens']}\n"
        f"- 总成本(￥): ￥{token_stats['cost_usd']:.6f}\n"
        f"### 模型成本分布\n"
        f"{model_lines}\n\n"
        f"## 7. 下一步行动项\n"
        f"{rec_lines}\n"
    )


async def generate_review_summary(
    session: AsyncSession,
    project: Project,
    generated_by: str = "human",
) -> dict:
    status_counter = await _task_status_counter(session, project.id)
    complexity_counter = await _task_complexity_counter(session, project.id)
    action_counter = await _log_counter(session, project.id)
    token_stats = await _token_stats(session, project.id)
    blockers = await _top_blockers(session, project.id)
    timeline = await _decision_timeline(session, project.id)

    total = int(sum(status_counter.values()))
    done = int(status_counter.get("done", 0))
    failed = int(status_counter.get("failed", 0))
    blocked = int(status_counter.get("blocked", 0))
    pending_like = total - done - failed - blocked

    payload = {
        "generated_at": _now_iso(),
        "generated_by": generated_by,
        "project": {
            "id": project.id,
            "name": project.name,
            "project_type": project.project_type,
            "current_stage": int(project.current_stage or 0),
        },
        "task_stats": {
            "total": total,
            "done": done,
            "failed": failed,
            "blocked": blocked,
            "pending_like": max(pending_like, 0),
            "done_rate": _percent(done, total),
            "status_counter": status_counter,
        },
        "complexity_stats": complexity_counter,
        "action_stats": dict(Counter(action_counter)),
        "token_stats": token_stats,
        "timeline": timeline,
        "blockers": blockers,
    }
    payload["recommendations"] = _recommendations(status_counter, action_counter, blocked)
    payload["markdown"] = _build_markdown(payload)
    return payload


async def save_review_document(
    session: AsyncSession,
    project: Project,
    summary: dict,
) -> Document:
    stage_snapshot = int(project.current_stage or 0)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    doc = Document(
        project_id=project.id,
        iteration_id=project.current_iteration_id,
        stage=stage_snapshot,
        category=REVIEW_CATEGORY,
        tags=[REVIEW_TAG, f"stage-{stage_snapshot}"],
        title=f"项目复盘报告-{ts}",
        content=summary["markdown"],
        status="approved",
        reviewed_by="system:auto",
        is_selected=False,
        review_result={"auto": True, "reason": "project review summary"},
        generated_model="rule-based-summary",
    )
    session.add(doc)
    await session.flush()
    return doc

