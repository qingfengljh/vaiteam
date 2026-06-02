# CC Worker 工具链（包 B + 包 C）

与仓库根 **`docs/50-CLAUDE_CODE_WORKER.md`**、**`docs/46-EXECUTION_CHANNEL_PROTOCOL.md`**、`task_pack_version: 1` 对齐。本文路径均相对 **`dispatcher/`**（与本目录同属 Dispatcher 应用树）。

## 包 B：样例任务包 + 本地/CI

```bash
cd "$(git rev-parse --show-toplevel)/dispatcher"   # openclaw-team/dispatcher
export CC_RUN_MODE=dry-run
export CC_TASK_PACK=tools/cc-worker/sample-task-pack.v1.json
python3 tools/cc-worker/run_task_pack.py
```

输出为一行 JSON：`exit_code`、`log_path`、`diff_stat`。`dry-run` **不**调用 `claude`，适合 CI。

真实调用（需本机已安装 `claude` 且已登录 / 已配置密钥，见官方 [headless](https://docs.anthropic.com/en/docs/claude-code/headless)）：

```bash
export CC_RUN_MODE=invoke
# 可选 CC_CLAUDE_BARE=1  CC_CLAUDE_BIN=claude
python3 tools/cc-worker/run_task_pack.py
```

自 **openclaw-team 仓库根** 也可用：`python3 dispatcher/tools/cc-worker/run_task_pack.py`，并设 `CC_TASK_PACK=dispatcher/tools/cc-worker/sample-task-pack.v1.json`。

## 包 C：从 Dispatcher 只读拉任务 → 生成 CC 提示

1. 取得 JWT：`POST /api/auth/login`（与 Web 相同）。
2. 在 **`dispatcher/`** 下：

```bash
export VAI_DISPATCHER_URL=http://localhost:8000
export VAI_JWT='...'
export VAI_TASK_ID='...'
python3 tools/cc-worker/pull_assigned_task.py > handoff.md
```

stdout 为 Markdown + 与包 B 一致的 **`task_pack_version: 1`** JSON 块。完成执行后仍须走既有 **MQ / webhook** 回传（不在此脚本内实现，避免与 46 号状态机重复）。

## 字段说明（task_pack_version 1）

| 字段 | 含义 |
|------|------|
| `executor_hint` | `connector` \| `claude_code` \| `prototype_cc`（原型工坊见 `prototype-workshop/`） |
| `actor_type` | `human` \| `agent` \| `connector` \| `claude_code` |
| `ref_id` / `branch` / `git_base_branch` | 与派单 / Git 协议一致 |

Dispatcher 侧已在 `TaskCreate` / `assign_task` inbox 与 MQ metadata 中透出 `executor_hint` / `actor_type`（见 **`app/execution_hints.py`**）。

## 包 D：与编排一致的 MQ 下发 → 工作区执行 → webhook 收口（`executor_hint=claude_code`）

1. **Dispatcher**（`app/services/mq_worker.py`）：对 `task:dispatch` 中 `metadata.executor_hint` 为 **`claude_code`** 或 **`prototype_cc`** 的任务 **不再** 调用 OpenClaw hooks；**`prototype_cc`** 仍走原型工坊专用链路；**`claude_code`** 由本机/侧车 Worker 消费并回报。
2. **`cc_dispatch` 消费者组**（`app/services/mq.py`）：与 `connector` 组并存于 `task:dispatch`，供 CC 侧独立进程订阅（消息在两队列各投递一次，属 Redis Streams 语义）。
3. **scheduler**（`claude_code`）：除 `assign_task` inbox 外追加一条 **`cc_task_dispatch`**（全量 `instruction` + `metadata`），便于从消息 API 对账。

端到端（Redis 侧车 + webhook），在 **`dispatcher/`**：

```bash
export REDIS_URL=redis://127.0.0.1:6379/0
export VAI_DISPATCHER_URL=http://127.0.0.1:8000
export VAI_AGENT_ID=<与派单目标 Agent 一致>
export CC_WORKSPACE=/path/to/git/clone
export CC_RUN_MODE=dry-run
python3 tools/cc-worker/consume_cc_dispatch_stream.py
```

单条消息手工灌入（调试用）：

```bash
export CC_DISPATCH_JSON=/tmp/dispatch.json
python3 tools/cc-worker/pipeline_cc_dispatch.py < /tmp/dispatch.json
```

回传：HTTP **`POST /api/webhook/task-complete`** / **`/api/webhook/task-failed`**，Body 与 **`app/routers/webhook.py`** 中 `TaskCompletePayload` / `TaskFailedPayload` 一致（可选 `token_usage`、`duration_ms`）。实现见同目录 **`report_task_webhook.py`**。

环境变量摘要：**`VAI_DISPATCHER_URL`**、**`VAI_AGENT_ID`**、**`CC_WORKSPACE`**、**`CC_RUN_MODE`**、**`REDIS_URL`**（仅流式消费时）。
