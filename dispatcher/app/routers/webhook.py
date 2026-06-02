"""Webhook 回调路由：接收 OpenClaw Agent 的汇报

支持两种通道：
1. HTTP POST（原有方式，保持兼容）
2. Redis Stream（通过 mq_worker 消费后调用同样的处理逻辑）

备份上传：Connector 打包 workspace 后 POST 到此端点。
"""

import os
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.config import settings
from app.models import Agent, Backup
from app.services import prototype_run as prototype_run_svc
from app.services import scheduler, mq, task_lifecycle
from app.services import token_tracker
from app.services.backup import _backup_path, complete_backup_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhook", tags=["webhook"])


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_cost_usd: float = 0.0


class TaskUpdatePayload(BaseModel):
    task_id: str
    agent_id: str
    status: str
    message: str = ""
    progress: int = 0


class TaskCompletePayload(BaseModel):
    task_id: str
    agent_id: str
    result: str = ""
    files_changed: list[str] = Field(default_factory=list)
    attempt_id: str = ""
    token_usage: TokenUsage | None = None
    duration_ms: int | None = None


class TaskFailedPayload(BaseModel):
    task_id: str
    agent_id: str
    error: str = ""
    attempt_id: str = ""
    token_usage: TokenUsage | None = None
    duration_ms: int | None = None


class NeedHelpPayload(BaseModel):
    task_id: str
    agent_id: str
    issue: str
    context: str = ""


class TaskClarificationPayload(BaseModel):
    """CC Worker 在执行过程中发现需要澄清时上报"""
    task_id: str
    agent_id: str
    questions: list[str] = Field(default_factory=list)
    context: str = ""
    attempt_id: str = ""


class PrototypeRunWebhookBody(BaseModel):
    run_id: str
    status: str
    exit_code: int | None = None
    summary: str = ""
    error: str = ""
    artifact_ref: str = ""


@router.post("/task-update")
async def on_task_update(body: TaskUpdatePayload, session: AsyncSession = Depends(get_session)):
    return await scheduler.update_task_progress(
        session,
        body.task_id,
        body.agent_id,
        status=body.status,
        message=body.message,
        progress=body.progress,
    )


@router.post("/task-complete")
async def on_task_complete(body: TaskCompletePayload, session: AsyncSession = Depends(get_session)):
    if body.token_usage:
        await _record_agent_tokens(session, body.task_id, body.token_usage, body.duration_ms)
    return await scheduler.complete_task(
        session, body.task_id, body.agent_id, body.result,
        attempt_id=body.attempt_id or None,
        token_usage=body.token_usage.model_dump() if body.token_usage else None,
        duration_ms=body.duration_ms,
    )


@router.post("/task-failed")
async def on_task_failed(body: TaskFailedPayload, session: AsyncSession = Depends(get_session)):
    if body.token_usage:
        await _record_agent_tokens(session, body.task_id, body.token_usage, body.duration_ms)
    return await scheduler.fail_task(
        session, body.task_id, body.agent_id, body.error,
        attempt_id=body.attempt_id or None,
        token_usage=body.token_usage.model_dump() if body.token_usage else None,
        duration_ms=body.duration_ms,
    )


async def _record_agent_tokens(session: AsyncSession, task_id: str, usage: TokenUsage, duration_ms: int | None):
    try:
        from app.models import Task
        task = await session.get(Task, task_id)
        project_id = task.project_id if task else None
        model = task.suggested_model or "unknown" if task else "unknown"
        await token_tracker.record_from_webhook(
            session,
            project_id=project_id, task_id=task_id, model=model,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens, total_cost_usd=usage.total_cost_usd,
            duration_ms=duration_ms or 0,
        )
    except Exception as e:
        logger.warning(f"Failed to record agent token usage: {e}")


@router.post("/prototype-run")
async def on_prototype_run_complete(
    request: Request,
    body: PrototypeRunWebhookBody,
    session: AsyncSession = Depends(get_session),
):
    """CC wrapper 结束任务后上报；鉴权为每 run 一次性 secret（见 docs/PROTOTYPE_CC_RUN_PIPELINE.md）。"""
    secret = (request.headers.get("X-Prototype-Run-Secret") or "").strip()
    if not secret:
        raise HTTPException(401, "missing X-Prototype-Run-Secret")
    st = (body.status or "").strip().lower()
    if st not in ("succeeded", "failed"):
        raise HTTPException(400, "status must be succeeded or failed")
    try:
        await prototype_run_svc.complete_run_via_webhook(
            session,
            run_id=body.run_id.strip(),
            secret_plain=secret,
            status=st,
            exit_code=body.exit_code,
            summary=body.summary or "",
            error=body.error or "",
            artifact_ref=body.artifact_ref or "",
        )
        await session.commit()
    except ValueError as e:
        msg = str(e)
        if "invalid" in msg.lower():
            raise HTTPException(401, msg) from e
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from e
        raise HTTPException(400, msg) from e
    return {"status": "ok"}


@router.post("/need-help")
async def on_need_help(body: NeedHelpPayload, session: AsyncSession = Depends(get_session)):
    from app.models import Task, TaskLog
    task = await session.get(Task, body.task_id)
    if task:
        session.add(task_lifecycle.on_terminal(
            task,
            status="blocked",
            actor=body.agent_id,
            reason=body.issue or "agent needs help",
        ))
        session.add(TaskLog(task_id=body.task_id, agent_id=body.agent_id, action="need_help", message=body.issue))
        await session.commit()
    return {"status": "ok", "needs_attention": True}


@router.post("/task-clarification")
async def on_task_clarification(body: TaskClarificationPayload, session: AsyncSession = Depends(get_session)):
    """接收 CC Worker 的澄清请求，暂停任务等待人类/架构师回复。"""
    from app.models import Task, TaskLog
    task = await session.get(Task, body.task_id)
    if not task:
        raise HTTPException(404, f"Task {body.task_id} not found")

    # 将任务状态改为 need_clarification
    session.add(task_lifecycle.transition(
        task,
        event="need_clarification",
        actor=body.agent_id,
        reason="agent needs clarification",
        metadata={
            "questions": body.questions,
            "context": body.context,
            "attempt_id": body.attempt_id,
        },
    ))

    # 将澄清问题写入 task.context，方便后续展示给人类
    ctx = dict(task.context or {})
    ctx["pending_clarifications"] = body.questions
    ctx["clarification_context"] = body.context
    ctx["clarification_requested_at"] = datetime.now(timezone.utc).isoformat()
    ctx["clarification_agent_id"] = body.agent_id
    task.context = ctx

    # 释放 agent（agent 可以去执行其他任务）
    agent = await session.get(Agent, body.agent_id)
    if agent:
        agent.status = "idle"
        agent.current_task_id = None

    # 记录日志
    questions_text = "\n".join(f"- {q}" for q in body.questions)
    session.add(TaskLog(
        task_id=body.task_id,
        agent_id=body.agent_id,
        action="need_clarification",
        message=f"Agent 请求澄清:\n{questions_text}",
        metadata_={
            "questions": body.questions,
            "context": body.context,
            "attempt_id": body.attempt_id,
        },
    ))

    await session.commit()
    logger.info(f"Task {body.task_id} paused for clarification from {body.agent_id}")

    # 通知架构师/上级（非阻塞）
    try:
        await mq.publish_callback("need_clarification", {
            "task_id": body.task_id,
            "agent_id": body.agent_id,
            "questions": body.questions,
        })
    except Exception as e:
        logger.debug(f"MQ notification failed (non-blocking): {e}")

    return {
        "status": "ok",
        "task_id": body.task_id,
        "state": "need_clarification",
        "questions_count": len(body.questions),
    }


# ── Agent 通过 Redis Stream 回调的入口 ──

@router.post("/via-mq/task-complete")
async def mq_task_complete(body: TaskCompletePayload):
    """Agent 将完成结果发到 Redis Stream（由 mq_worker 消费处理）"""
    await mq.publish_callback("task_complete", {
        "task_id": body.task_id,
        "agent_id": body.agent_id,
        "result": body.result,
        "files_changed": body.files_changed,
        "attempt_id": body.attempt_id,
        "token_usage": body.token_usage.model_dump() if body.token_usage else None,
        "duration_ms": body.duration_ms,
    })
    return {"status": "queued"}


@router.post("/via-mq/task-failed")
async def mq_task_failed(body: TaskFailedPayload):
    """Agent 将失败结果发到 Redis Stream"""
    await mq.publish_callback("task_failed", {
        "task_id": body.task_id,
        "agent_id": body.agent_id,
        "error": body.error,
        "attempt_id": body.attempt_id,
        "token_usage": body.token_usage.model_dump() if body.token_usage else None,
        "duration_ms": body.duration_ms,
    })
    return {"status": "queued"}


@router.post("/via-mq/task-update")
async def mq_task_update(body: TaskUpdatePayload):
    """Agent 将执行进度发到 Redis Stream"""
    await mq.publish_callback("task_update", {
        "task_id": body.task_id,
        "agent_id": body.agent_id,
        "status": body.status,
        "message": body.message,
        "progress": body.progress,
    })
    return {"status": "queued"}


@router.get("/mq-stats")
async def mq_stats():
    """消息队列状态监控"""
    return await mq.stream_stats()


# ── 备份上传（Connector 调用） ──

@router.post("/backup-upload")
async def backup_upload(
    request: Request,
    agent_id: str,
    request_id: str,
    include_workspace: str | None = None,
    backup_mode: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """接收 Connector 上传的备份（tar.gz），支持含源码与不含源码两种模式"""
    agent = await session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = _backup_path(agent.project_id, agent_id, ts)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    max_size = settings.BACKUP_MAX_SIZE
    total = 0

    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_size:
                    os.remove(dest)
                    raise HTTPException(413, f"Backup exceeds max size {max_size} bytes")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Backup upload failed for {agent_id}: {e}")
        if os.path.exists(dest):
            os.remove(dest)
        complete_backup_upload(request_id, None)
        raise HTTPException(500, str(e))

    file_size = os.path.getsize(dest)
    mode = (backup_mode or "").strip().lower()
    if mode not in ("metadata_only", "full_source"):
        mode = "full_source"  # 兼容旧 connector：未携带 mode 时按含源码处理，避免误判
    if include_workspace is None:
        include_source = mode == "full_source"
    else:
        include_source = str(include_workspace).lower() in ("1", "true", "yes", "on")

    backup = Backup(
        project_id=agent.project_id,
        agent_id=agent_id,
        backup_type="full_source" if include_source else "metadata_only",
        file_path=dest,
        file_size=file_size,
        metadata_={
            "workspace_path": agent.workspace_path,
            "request_id": request_id,
            "include_workspace": include_source,
            "backup_mode": mode,
            "contains_source_code": include_source,
        },
    )
    session.add(backup)
    await session.commit()
    await session.refresh(backup)

    complete_backup_upload(request_id, backup)
    logger.info(
        "Backup uploaded: %s (%s bytes) include_workspace=%s mode=%s",
        dest, file_size, include_source, mode,
    )
    return {
        "status": "ok",
        "backup_id": backup.id,
        "file_size": file_size,
        "contains_source_code": include_source,
        "backup_mode": mode,
    }
