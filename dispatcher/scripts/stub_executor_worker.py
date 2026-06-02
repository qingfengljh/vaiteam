#!/usr/bin/env python3
"""
Stub 执行器：与 Connector 相同方式消费 Redis ``task:dispatch``（消费者组 ``connector``、
消费者名为本 Stub 的 ``STUB_AGENT_ID``），匹配 ``agent_id`` 后打印摘要，再经
``POST /api/webhook/via-mq/task-complete`` 写入 ``task:callback``，由 dispatcher mq_worker 收口。

环境变量：
  STUB_AGENT_ID     必填，与 dispatcher 中 Agent.id 一致（与 POST /api/agents 注册一致）
  REDIS_URL         默认 redis://localhost:6379/0
  DISPATCHER_URL    默认 http://127.0.0.1:8000
  STUB_RELAX_HINT   若设为 1，则不过滤 metadata.executor_hint（仅按 agent_id 匹配）

与 OpenClaw Connector 一致：非本 agent 的消息不 ACK，留在 pending，由目标 Agent 的消费端处理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [stub] %(message)s")
log = logging.getLogger("stub")

STREAM = "task:dispatch"
GROUP = "connector"


async def ensure_group(r: redis.Redis) -> None:
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        log.info("created stream/group %s/%s", STREAM, GROUP)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def _parse_entry(msg_id: str, fields: dict) -> tuple[dict | None, bool]:
    """返回 (data, need_ack_bad)。need_ack_bad 表示应 ACK 并丢弃。"""
    raw = fields.get("data") if isinstance(fields, dict) else None
    if not raw:
        return None, True
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        log.warning("bad json msg=%s", msg_id)
        return None, True


async def _complete(
    client: httpx.AsyncClient,
    base: str,
    agent_id: str,
    task_id: str,
    attempt_id: str,
) -> bool:
    url = f"{base}/api/webhook/via-mq/task-complete"
    body = {
        "task_id": task_id,
        "agent_id": agent_id,
        "result": "stub_done",
        "files_changed": [],
        "attempt_id": attempt_id,
        "duration_ms": 0,
    }
    r2 = await client.post(url, json=body)
    r2.raise_for_status()
    return True


async def _handle_message(
    r: redis.Redis,
    client: httpx.AsyncClient,
    base: str,
    agent_id: str,
    relax: bool,
    msg_id: str,
    data: dict,
) -> bool:
    """若应 ACK 返回 True；不 ACK 返回 False。"""
    if data.get("type") != "send_task":
        await r.xack(STREAM, GROUP, msg_id)
        return True

    if data.get("agent_id") != agent_id:
        return False

    meta = data.get("metadata") or {}
    hint = (meta.get("executor_hint") or "").strip().lower()
    if not relax and hint != "stub":
        log.info(
            "skip msg=%s task=%s executor_hint=%r (set STUB_RELAX_HINT=1 to accept)",
            msg_id,
            meta.get("task_id"),
            hint or None,
        )
        return False

    task_id = meta.get("task_id", "")
    attempt_id = meta.get("attempt_id") or ""
    instruction = (data.get("instruction") or "")[:400]
    log.info(
        "task=%s ref_id=%s attempt_id=%s executor_hint=%s actor_type=%s instruction_preview=%r",
        task_id,
        meta.get("ref_id"),
        attempt_id,
        meta.get("executor_hint"),
        meta.get("actor_type"),
        instruction,
    )

    try:
        await _complete(client, base, agent_id, task_id, attempt_id)
    except Exception as e:
        log.error("task-complete failed: %s", e)
        return False

    await r.xack(STREAM, GROUP, msg_id)
    log.info("acked msg=%s via-mq task-complete queued", msg_id)
    return True


async def run() -> None:
    agent_id = (os.environ.get("STUB_AGENT_ID") or "").strip()
    if not agent_id:
        log.error("STUB_AGENT_ID is required")
        sys.exit(1)
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    base = (os.environ.get("DISPATCHER_URL") or "http://127.0.0.1:8000").rstrip("/")
    relax = os.environ.get("STUB_RELAX_HINT", "").strip() in ("1", "true", "yes")

    r = redis.from_url(redis_url, decode_responses=True)
    await ensure_group(r)
    log.info("stub agent=%s redis=%s dispatcher=%s relax_hint=%s", agent_id, redis_url, base, relax)

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                resp = await r.xreadgroup(
                    GROUP,
                    agent_id,
                    streams={STREAM: ">"},
                    count=1,
                    block=8000,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("xreadgroup: %s", e)
                await asyncio.sleep(2)
                continue

            if not resp:
                continue

            _stream, entries = resp[0]
            if not entries:
                continue
            msg_id, fields = entries[0]
            data, bad = _parse_entry(msg_id, fields)
            if bad or data is None:
                await r.xack(STREAM, GROUP, msg_id)
                continue

            await _handle_message(r, client, base, agent_id, relax, msg_id, data)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
