"""
消息队列消费者

两个后台协程：
  dispatch_worker — 从 task:dispatch 读取，转发到 OpenClaw Agent（HTTP）
  callback_worker — 从 task:callback 读取，调用 scheduler 处理结果

在 FastAPI lifespan 中作为后台任务启动。

说明：原型工坊 CC 的一次性运行收口走 HTTP ``POST /api/webhook/prototype-run``（见 docs/PROTOTYPE_CC_RUN_PIPELINE.md），
不经过本模块的 callback stream。``executor_hint`` 为 ``claude_code`` / ``prototype_cc`` / ``stub`` 时下发不经 OpenClaw hooks（见 ``dispatch_routing``）。
"""

import asyncio
import logging

from app.services import mq, openclaw, scheduler, task_lifecycle, dispatch_routing
from app.services.mq import STREAM_DISPATCH, STREAM_CALLBACK, GROUP_CONNECTOR, GROUP_DISPATCHER
from app.core.database import async_session

logger = logging.getLogger(__name__)

_running = False
_tasks: list[asyncio.Task] = []


async def dispatch_worker(consumer_name: str = "dispatcher-1"):
    """消费 task:dispatch，转发任务到 OpenClaw Agent"""
    logger.info(f"Dispatch worker started: {consumer_name}")
    try:
        while _running:
            try:
                messages = await mq.consume(STREAM_DISPATCH, GROUP_CONNECTOR, consumer_name, count=5, block_ms=3000)
                for msg_id, data in messages:
                    try:
                        await _handle_dispatch(data)
                    except Exception as e:
                        logger.error(f"Dispatch handler error for {msg_id}: {e}")
                    finally:
                        await mq.ack(STREAM_DISPATCH, GROUP_CONNECTOR, msg_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Dispatch worker error: {e}")
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    logger.info("Dispatch worker stopped")


async def _handle_dispatch(data: dict):
    """处理一条任务下发消息"""
    msg_type = data.get("type")
    if msg_type != "send_task":
        logger.warning(f"Unknown dispatch message type: {msg_type}")
        return

    agent_id = data["agent_id"]
    instruction = data["instruction"]
    metadata = data.get("metadata", {})
    model = data.get("model")

    if dispatch_routing.should_skip_openclaw_for_dispatch(metadata):
        task_id = metadata.get("task_id", "?")
        eh = (metadata or {}).get("executor_hint")
        logger.info(
            "Dispatch skip OpenClaw (executor_hint=%s task=%s agent=%s); CC/prototype worker reports via webhook",
            eh,
            task_id,
            agent_id,
        )
        return

    result = await openclaw.send_task(agent_id, instruction, metadata, model=model)
    if "error" in result:
        task_id = metadata.get("task_id", "?")
        logger.error(f"OpenClaw send_task failed for task {task_id}: {result['error']}")
        # 发送失败回调，让 scheduler 处理重试
        await mq.publish_callback("task_failed", {
            "task_id": metadata.get("task_id", ""),
            "agent_id": agent_id,
            "error": f"Agent delivery failed: {result.get('error', 'unknown')}",
        })


async def callback_worker(consumer_name: str = "dispatcher-1"):
    """消费 task:callback，处理 Agent 回调结果"""
    logger.info(f"Callback worker started: {consumer_name}")
    try:
        while _running:
            try:
                messages = await mq.consume(STREAM_CALLBACK, GROUP_DISPATCHER, consumer_name, count=5, block_ms=3000)
                for msg_id, data in messages:
                    try:
                        await _handle_callback(data)
                    except Exception as e:
                        logger.error(f"Callback handler error for {msg_id}: {e}")
                    finally:
                        await mq.ack(STREAM_CALLBACK, GROUP_DISPATCHER, msg_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Callback worker error: {e}")
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    logger.info("Callback worker stopped")


async def _handle_callback(data: dict):
    """处理一条 Agent 回调消息"""
    action = data.get("action")
    task_id = data.get("task_id", "")
    agent_id = data.get("agent_id", "")

    async with async_session() as session:
        if action == "task_complete":
            token_usage = data.get("token_usage")
            if token_usage:
                from app.services import token_tracker
                await _record_tokens(session, task_id, token_usage, data.get("duration_ms"))
            await scheduler.complete_task(
                session, task_id, agent_id, data.get("result", ""),
                attempt_id=data.get("attempt_id"),
                token_usage=token_usage,
                duration_ms=data.get("duration_ms"),
            )

        elif action == "task_failed":
            token_usage = data.get("token_usage")
            if token_usage:
                await _record_tokens(session, task_id, token_usage, data.get("duration_ms"))
            await scheduler.fail_task(
                session, task_id, agent_id, data.get("error", ""),
                attempt_id=data.get("attempt_id"),
                token_usage=token_usage,
                duration_ms=data.get("duration_ms"),
            )

        elif action == "task_update":
            await scheduler.update_task_progress(
                session,
                task_id,
                agent_id,
                status=data.get("status", ""),
                message=data.get("message", ""),
                progress=data.get("progress", 0),
            )

        elif action == "need_help":
            from app.models import Task, TaskLog
            task = await session.get(Task, task_id)
            if task:
                session.add(task_lifecycle.on_terminal(
                    task,
                    status="blocked",
                    actor=agent_id,
                    reason=data.get("issue", "agent needs help"),
                ))
                session.add(TaskLog(
                    task_id=task_id, agent_id=agent_id,
                    action="need_help", message=data.get("issue", ""),
                ))
                await session.commit()

        elif action == "heartbeat":
            from app.services import heartbeat
            await heartbeat.receive_heartbeat(
                session,
                agent_id=agent_id,
                supervisor_id=data.get("supervisor_id"),
                role=data.get("role"),
                status=data.get("status"),
                current_task_id=data.get("current_task_id"),
                system_info=data.get("system_info"),
            )

        else:
            logger.warning(f"Unknown callback action: {action}")


async def leader_inbox_worker(consumer_name: str = "leader"):
    """消费 leader 的 inbox stream，处理架构师的回复消息"""
    logger.info("Leader inbox worker started")
    try:
        await mq.ensure_inbox_group("leader")
    except Exception as e:
        logger.warning(f"Failed to create leader inbox group: {e}")
        return

    try:
        while _running:
            try:
                messages = await mq.consume_inbox("leader", consumer_name, count=5, block_ms=3000)
                for stream_msg_id, data in messages:
                    try:
                        await _handle_leader_inbox(data)
                    except Exception as e:
                        logger.error(f"Leader inbox handler error: {e}")
                    finally:
                        await mq.ack_inbox("leader", stream_msg_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Leader inbox worker error: {e}")
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    logger.info("Leader inbox worker stopped")


async def _handle_leader_inbox(data: dict):
    """处理 Leader 收到的消息（架构师回复的审核结果等）"""
    msg_type = data.get("type")
    task_id = data.get("task_id", "")
    from_id = data.get("from", "")
    payload = data.get("payload", {})

    async with async_session() as session:
        if msg_type == "review_result":
            approved = payload.get("approved", False)
            score = payload.get("score", 0)
            summary = payload.get("summary", "")
            issues = payload.get("issues", [])

            comments = f"架构师审核评分: {score}/10\n{summary}"
            if issues:
                comments += "\n\n问题列表:\n"
                for issue in issues[:10]:
                    sev = issue.get("severity", "info")
                    desc = issue.get("description", "")
                    comments += f"- [{sev}] {desc}\n"

            action = "approve" if approved else "reject"
            await scheduler.review_completed_task(
                session, task_id, action,
                reviewer=from_id, comments=comments,
            )
            logger.info(f"Review result for {task_id}: {action} (score={score})")

        elif msg_type == "task_completed":
            await scheduler.complete_task(
                session, task_id, from_id, payload.get("result", ""),
            )

        elif msg_type == "task_failed":
            await scheduler.fail_task(
                session, task_id, from_id, payload.get("error", ""),
            )

        elif msg_type == "task_accepted":
            from app.models import TaskLog
            session.add(TaskLog(
                task_id=task_id, agent_id=from_id,
                action="accepted", message=payload.get("plan", "任务已接受"),
            ))
            await session.commit()

        elif msg_type == "task_progress":
            from app.models import Task, TaskLog
            task = await session.get(Task, task_id)
            if task:
                session.add(TaskLog(
                    task_id=task_id, agent_id=from_id,
                    action="progress", message=payload.get("message", ""),
                    metadata_={"progress": payload.get("progress", 0)},
                ))
                await session.commit()

        else:
            logger.debug(f"Leader inbox: unhandled type {msg_type} from {from_id}")


async def _record_tokens(session, task_id: str, token_usage: dict, duration_ms: int | None):
    try:
        from app.models import Task
        from app.services import token_tracker
        task = await session.get(Task, task_id)
        project_id = task.project_id if task else None
        model = task.suggested_model or "unknown" if task else "unknown"
        await token_tracker.record_from_webhook(
            session,
            project_id=project_id, task_id=task_id, model=model,
            input_tokens=token_usage.get("input_tokens", 0),
            output_tokens=token_usage.get("output_tokens", 0),
            cache_read_tokens=token_usage.get("cache_read_tokens", 0),
            total_cost_usd=token_usage.get("total_cost_usd", 0),
            duration_ms=duration_ms or 0,
        )
    except Exception as e:
        logger.warning(f"Token recording failed: {e}")


async def start():
    """启动所有消费者 worker"""
    global _running
    if _running:
        return
    _running = True

    from app.core.config import settings
    logger.info(f"Connecting to Redis: {settings.REDIS_URL}")

    try:
        await mq.ensure_groups()
    except Exception as e:
        logger.warning(f"Redis not available, MQ workers disabled: {e}")
        _running = False
        return

    _tasks.append(asyncio.create_task(callback_worker()))
    _tasks.append(asyncio.create_task(leader_inbox_worker()))
    logger.info("MQ workers started (callback + leader inbox)")


async def stop():
    """停止所有消费者 worker"""
    global _running
    _running = False
    for t in _tasks:
        t.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    logger.info("MQ workers stopped")
