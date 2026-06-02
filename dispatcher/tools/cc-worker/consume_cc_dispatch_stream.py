#!/usr/bin/env python3
"""
独立进程：用 Redis 消费者组 `cc_dispatch` 订阅 ``task:dispatch``，仅处理 ``executor_hint=claude_code`` 的 ``send_task``，
调用 pipeline_cc_dispatch.run_one_dispatch_message（工作区 + webhook）。

依赖：pip install redis（与 dispatcher 相同 major）

环境变量：
  REDIS_URL           默认 redis://127.0.0.1:6379/0
  VAI_DISPATCHER_URL / VAI_AGENT_ID / CC_WORKSPACE / CC_RUN_MODE  同 pipeline_cc_dispatch.py
  CC_CONSUMER_NAME    默认 cc-worker-1
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_CC_DIR = str(Path(__file__).resolve().parent)
if _CC_DIR not in sys.path:
    sys.path.insert(0, _CC_DIR)

from pipeline_cc_dispatch import run_one_dispatch_message  # noqa: E402

STREAM = "task:dispatch"
GROUP = "cc_dispatch"


def main() -> int:
    try:
        import redis
    except ImportError:
        print("need: pip install redis", file=sys.stderr)
        return 2

    url = os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0"
    consumer = os.environ.get("CC_CONSUMER_NAME") or "cc-worker-1"
    r = redis.from_url(url, decode_responses=True)

    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    print(f"cc_dispatch consumer listening {STREAM} group={GROUP} name={consumer}", flush=True)
    while True:
        xs = r.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=3, block=8000)
        if not xs:
            continue
        for _sname, batch in xs:
            for msg_id, fields in batch:
                try:
                    raw = fields.get("data") or "{}"
                    data = json.loads(raw)
                    run_one_dispatch_message(data)
                except Exception as e:
                    print(f"{msg_id} handler error: {e}", file=sys.stderr)
                finally:
                    r.xack(STREAM, GROUP, msg_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
