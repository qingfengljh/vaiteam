"""
消息队列服务 — 基于 Redis Streams

Stream 一览：
  task:dispatch            — Dispatcher → Agent（任务下发，旧通道，保留兼容）
  task:callback            — Agent → Dispatcher（结果回调）
  agent:inbox:{agent_id}   — 点对点消息通道（面向任务的异步通信）
"""

import json
import logging
from typing import Any

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

STREAM_DISPATCH = "task:dispatch"
STREAM_CALLBACK = "task:callback"
INBOX_PREFIX = "agent:inbox:"

GROUP_DISPATCHER = "dispatcher"
GROUP_CONNECTOR = "connector"
# 与 Connector 组并存：仅供 CC Worker 进程消费 `executor_hint=claude_code` 的下发（见 dispatcher/tools/cc-worker/consume_cc_dispatch_stream.py）
GROUP_CC_DISPATCH = "cc_dispatch"
GROUP_INBOX = "inbox"


def inbox_stream(agent_id: str) -> str:
    return f"{INBOX_PREFIX}{agent_id}"


async def ensure_groups():
    """确保消费者组存在，启动时调用一次"""
    r = await get_redis()
    for stream, group in [
        (STREAM_DISPATCH, GROUP_CONNECTOR),
        (STREAM_DISPATCH, GROUP_CC_DISPATCH),
        (STREAM_CALLBACK, GROUP_DISPATCHER),
    ]:
        try:
            await r.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info(f"Created consumer group {group} on {stream}")
        except Exception as e:
            if "BUSYGROUP" in str(e):
                pass
            else:
                logger.warning(f"xgroup_create {stream}/{group}: {e}")


async def ensure_inbox_group(agent_id: str):
    """为某个 Agent 的 inbox stream 创建消费者组"""
    r = await get_redis()
    stream = inbox_stream(agent_id)
    try:
        await r.xgroup_create(stream, GROUP_INBOX, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.warning(f"xgroup_create {stream}/{GROUP_INBOX}: {e}")


async def publish(stream: str, data: dict[str, Any]) -> str:
    """发布消息到 Stream，返回消息 ID"""
    r = await get_redis()
    payload = {"data": json.dumps(data, ensure_ascii=False)}
    msg_id = await r.xadd(stream, payload, maxlen=10000)
    logger.debug(f"Published to {stream}: {msg_id}")
    return msg_id


async def consume(
    stream: str,
    group: str,
    consumer: str,
    count: int = 10,
    block_ms: int = 2000,
) -> list[tuple[str, dict]]:
    """从消费者组读取消息，返回 [(msg_id, data), ...]"""
    r = await get_redis()
    results = await r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
    messages = []
    for _stream_name, entries in results:
        for msg_id, fields in entries:
            try:
                data = json.loads(fields.get("data", "{}"))
                messages.append((msg_id, data))
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in {stream}/{msg_id}, acking and skipping")
                await r.xack(stream, group, msg_id)
    return messages


async def ack(stream: str, group: str, msg_id: str):
    """确认消息已处理"""
    r = await get_redis()
    await r.xack(stream, group, msg_id)


async def publish_dispatch(
    agent_id: str,
    instruction: str,
    metadata: dict | None = None,
    model: str | None = None,
) -> str:
    """发布任务下发消息"""
    msg: dict = {
        "type": "send_task",
        "agent_id": agent_id,
        "instruction": instruction,
        "metadata": metadata or {},
    }
    if model:
        msg["model"] = model
    return await publish(STREAM_DISPATCH, msg)


async def publish_backup(
    agent_id: str,
    project_id: str,
    request_id: str,
    *,
    include_workspace: bool = False,
    backup_mode: str = "metadata_only",
) -> str:
    """发布备份请求，Connector 消费后打包并 HTTP 上传"""
    return await publish(STREAM_DISPATCH, {
        "type": "backup",
        "agent_id": agent_id,
        "project_id": project_id,
        "request_id": request_id,
        "include_workspace": include_workspace,
        "backup_mode": backup_mode,
    })


async def publish_callback(
    action: str,
    payload: dict,
) -> str:
    """发布 Agent 回调消息"""
    return await publish(STREAM_CALLBACK, {
        "action": action,
        **payload,
    })


async def publish_to_inbox(to_id: str, message: dict) -> str:
    """发送一条消息到目标 Agent 的 inbox stream"""
    stream = inbox_stream(to_id)
    return await publish(stream, message)


async def consume_inbox(
    agent_id: str,
    consumer: str | None = None,
    count: int = 5,
    block_ms: int = 3000,
) -> list[tuple[str, dict]]:
    """从 Agent 的 inbox stream 消费消息"""
    stream = inbox_stream(agent_id)
    return await consume(stream, GROUP_INBOX, consumer or agent_id, count, block_ms)


async def ack_inbox(agent_id: str, msg_id: str):
    """确认 inbox 消息已处理"""
    stream = inbox_stream(agent_id)
    await ack(stream, GROUP_INBOX, msg_id)


async def stream_stats() -> dict:
    """获取 Stream 状态，用于监控（含 inbox streams）"""
    r = await get_redis()
    stats = {}
    streams = [STREAM_DISPATCH, STREAM_CALLBACK]

    # 扫描所有 inbox streams
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match=f"{INBOX_PREFIX}*", count=100)
        streams.extend(keys)
        if cursor == 0:
            break

    for stream in streams:
        try:
            info = await r.xinfo_stream(stream)
            groups = await r.xinfo_groups(stream)
            stats[stream] = {
                "length": info.get("length", 0),
                "groups": [
                    {
                        "name": g.get("name"),
                        "consumers": g.get("consumers"),
                        "pending": g.get("pending"),
                        "last_delivered_id": g.get("last-delivered-id"),
                    }
                    for g in groups
                ],
            }
        except Exception:
            stats[stream] = {"length": 0, "groups": []}
    return stats
