#!/usr/bin/env sh
# 只读：Redis Streams 长度与消费者组 lag（包 D）
set -eu
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

echo "=== XLEN (approx backlog) ==="
redis-cli -u "$REDIS_URL" XLEN task:dispatch || true
redis-cli -u "$REDIS_URL" XLEN task:callback || true

echo "=== task:callback group dispatcher (if exists) ==="
redis-cli -u "$REDIS_URL" XINFO GROUPS task:callback 2>/dev/null | sed -n '1,120p' || echo "(no stream or no groups)"
