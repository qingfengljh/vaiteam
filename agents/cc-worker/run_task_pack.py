#!/usr/bin/env python3
"""CC Worker 核心：轮询任务 → 按角色分发执行器 → 回传。

复用 46 号协议：
- 拉任务: GET /api/worker/task-poll?agent_id=xxx
- 进度:   POST /api/webhook/task-update
- 完成:   POST /api/webhook/task-complete
- 失败:   POST /api/webhook/task-failed
"""
from __future__ import annotations

import logging
import os
import sys
import time
import traceback

from skill_loader import load_skill
from report_result import report, report_progress, report_clarification

logger = logging.getLogger(__name__)

# ── 配置（环境变量） ──
AGENT_ID = os.environ.get("AGENT_ID", "").strip()
AGENT_ROLE = os.environ.get("AGENT_ROLE", "mid").strip()
DISPATCHER_BASE = os.environ.get("DISPATCHER_BASE", "http://dispatcher:8080").rstrip("/")
API_TOKEN = os.environ.get("AGENT_API_TOKEN", "").strip()
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))  # 秒
IDLE_MAX = int(os.environ.get("IDLE_MAX_CYCLES", "720"))  # 约1小时后退出
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "15"))


def _http_json(url: str, method: str = "GET", data: dict | None = None, timeout: int = 30) -> dict:
    import json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8")}
    except HTTPError as e:
        err = e.read().decode("utf-8") if e.fp else ""
        return {"ok": False, "status": e.code, "error": err}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def poll_task() -> dict | None:
    """轮询获取分配给本 Agent 的任务。"""
    if not AGENT_ID:
        logger.error("AGENT_ID not set")
        return None

    url = f"{DISPATCHER_BASE}/worker/task-poll?agent_id={AGENT_ID}"
    resp = _http_json(url, timeout=30)

    if not resp["ok"]:
        if resp["status"] == 404:
            logger.debug("No task available")
        else:
            logger.warning(f"Poll failed: {resp.get('error', resp['status'])}")
        return None

    try:
        import json
        body = json.loads(resp["body"])
        if not body.get("task"):
            return None
        return body["task"]
    except json.JSONDecodeError:
        logger.warning(f"Invalid JSON in poll response: {resp['body'][:200]}")
        return None


def poll_chat() -> list[dict]:
    """轮询 @mention 本 Agent 的群聊消息。"""
    if not AGENT_ID:
        return []
    url = f"{DISPATCHER_BASE}/worker/chat-poll?agent_id={AGENT_ID}"
    resp = _http_json(url, timeout=10)
    if not resp["ok"]:
        return []
    try:
        import json
        return json.loads(resp["body"]).get("messages", [])
    except Exception:
        return []


def send_chat_reply(dispatcher_base: str, agent_id: str, content: str, reply_to: str) -> bool:
    """发送群聊回复。"""
    url = f"{dispatcher_base}/worker/chat-reply"
    resp = _http_json(url, method="POST", data={
        "agent_id": agent_id,
        "content": content,
        "reply_to": reply_to,
    }, timeout=10)
    return resp["ok"]


_current_task_id: str | None = None

def heartbeat() -> None:
    """发送心跳，携带当前任务状态。"""
    url = f"{DISPATCHER_BASE}/worker/heartbeat"
    payload: dict = {
        "agent_id": AGENT_ID,
        "role": AGENT_ROLE,
        "status": "busy" if _current_task_id else "idle",
    }
    if _current_task_id:
        payload["current_task_id"] = _current_task_id
    _http_json(url, method="POST", data=payload, timeout=10)


def main() -> int:
    global _current_task_id
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not AGENT_ID:
        logger.error("AGENT_ID environment variable is required")
        return 1

    logger.info(f"CC Worker starting: agent_id={AGENT_ID}, role={AGENT_ROLE}, dispatcher={DISPATCHER_BASE}")

    # 加载 skill
    roles_dir = os.path.join(os.path.dirname(__file__), "..", "roles")
    skill = load_skill(AGENT_ROLE, roles_dir)
    logger.info(f"Loaded skill: {skill['name']} - {skill['description']}")

    # 加载执行器
    from executors import get_executor
    executor = get_executor(AGENT_ID, AGENT_ROLE, DISPATCHER_BASE)
    logger.info(f"Loaded executor: {executor.__class__.__name__}")

    idle_cycles = 0
    while idle_cycles < IDLE_MAX:
        try:
            # 每次 poll 前发心跳（保持状态及时更新）
            heartbeat()

            # 轮询群聊 @mention
            chat_msgs = poll_chat()
            for msg in chat_msgs:
                try:
                    reply = executor.reply_to_chat(msg)
                    if reply:
                        send_chat_reply(DISPATCHER_BASE, AGENT_ID, reply, msg["id"])
                        logger.info(f"Replied to chat {msg['id'][:8]}: {reply[:50]}...")
                except Exception as e:
                    logger.warning(f"Chat reply failed: {e}")

            # 轮询任务
            task = poll_task()
            if task is None:
                idle_cycles += 1
                time.sleep(POLL_INTERVAL)
                continue

            idle_cycles = 0
            _current_task_id = task_id = task.get("id", task.get("task_id", "unknown"))
            logger.info(f"Got task: {task_id}")

            # 执行
            try:
                report_progress(DISPATCHER_BASE, task_id, 5, "Agent 已认领，开始环境检查", agent_id=AGENT_ID)
                exec_r = executor.execute(task, skill)
            except Exception as e:
                logger.exception(f"Task {task_id} execution error")
                exec_r = {
                    "task_id": task_id,
                    "status": "failed",
                    "error": f"Internal error: {e}\n{traceback.format_exc()}",
                    "commit": "",
                }

            # 回传——执行阶段统一走 completed/failed
            report(
                DISPATCHER_BASE,
                task_id,
                exec_r["status"],
                agent_id=AGENT_ID,
                summary=exec_r.get("summary", ""),
                error=exec_r.get("error", ""),
                commit_hash=exec_r.get("commit", ""),
                token_usage=exec_r.get("token_usage"),
                model=exec_r.get("model", ""),
                duration_ms=exec_r.get("duration_ms", 0),
            )
            logger.info(f"Task {task_id} reported: {exec_r['status']} "
                       f"model={exec_r.get('model','?')} "
                       f"tokens={exec_r.get('token_usage')} "
                       f"duration={exec_r.get('duration_ms',0)}ms")
            _current_task_id = None

        except Exception as e:
            logger.exception("Main loop error")
            time.sleep(POLL_INTERVAL)

    logger.info(f"Idle for {IDLE_MAX} cycles, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
