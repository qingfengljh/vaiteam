# Claude Code（CC）作为编码 Worker

> **总览与协作模型**（架构师 + MQ 工作中心、OpenClaw→CC 迁移语义）：见 **[74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md](./74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md)**。
> **侧车 Agent vs CC（谁主控沟通/Git/知识库，CC 仅作编码工具）**：见 **[CC_SIDECAR_AGENT_MODEL.md](./CC_SIDECAR_AGENT_MODEL.md)**。

## 名词

- **CC**：本仓库与产品讨论中的 **Claude Code**（Anthropic 终端/IDE 侧编码代理），**不是**「泛指一切编码工具」的缩写。
- **OpenClaw / install-agent / connector**：现有客户机执行链（MQ + Git + `connector.mjs`），偏通用自动化。
- **Dispatcher**：编排、派单、状态机、审核入口；不因引入 CC 而承担「在服务器上替你跑 Claude Code」的职责（后者缺人机位与授权模型）。

## 目标（与 HANDOFF / AGENTS 共识一致）

1. **编码类任务**：在需要「接地仓库、多文件编辑、测试、提交」时，优先由 **Claude Code 会话**消费与 **人类 + Cursor 插件**同构的 **`task_context`**（或派单消息的子集），而不是仅依赖远端轻量 Agent 盲改。
2. **编排层**：继续只做 **结构化任务包、分支、验收、范围**；CC 是 **Worker 实现之一**，与 `human`、`connector` 并列演进，而不是替代 Leader/文档生成等全部 LLM 入口。**原型工坊**（`prototype-workshop/`）即 **角色特化的一种 CC Agent Worker**：同一套任务包与回传语义，仅任务类型与产物形态（mock 前端、artifact）不同；见 **`prototype-workshop/README.md`** 与 **`PROTOTYPE_CC_RUN_PIPELINE.md`**（runs/start、专用 webhook，与核心任务 MQ 回调分离）。
3. **文档/阶段生成、路由、计费**：仍走 **dispatcher API + 模型直连**；**不**要求「所有与 AI 打交道的地方」都经 CC（见前文架构讨论）。

## 与现有协议的关系

- **[46-EXECUTION_CHANNEL_PROTOCOL.md](./46-EXECUTION_CHANNEL_PROTOCOL.md)**：Git 分支、`commit+push`、MQ 回传仍适用；CC 路径若落地，应 **复用同一状态机语义**（开始 / 更新 / 完成 / 失败），避免第三套「口头完成」。
- **派单载荷**：`scheduler.assign_task` 经 inbox 下发的 `assign_task` 已含 `instruction`、`model`、`branch`、`git_base_branch`、`ref_id`、`title`、`attempt_id`、**`executor_hint`**、**`actor_type`** 等；**`executor_hint`** 含 **`prototype_cc`**（原型工坊专用 Worker，见 **`prototype-workshop/README.md`** 与 **`GET .../prototype-workshop/.../task-pack`**）。CC 侧适配器的第一步是 **把这些字段 + 任务包扩展字段** 稳定映射为 Claude Code 可读的 **单文件提示或 stdin 契约**（实现时定稿，本文只锁边界）。

## 执行器能力

### 自纠错循环（Self-Correction）

编码执行器在执行失败后，**不立即上报 Dispatcher**，而是进入自纠错循环：

1. **诊断**：自动运行 `python -m py_compile`、`npx tsc --noEmit`、测试命令等，收集错误信息。
2. **修复**：生成修复 prompt（含错误上下文），重新调用 `claude -p` 执行。
3. **重试上限**：默认最多 `MAX_SELF_RETRY=2` 次，环境变量 `CC_MAX_SELF_RETRY` 可调。
4. ** exhausted 后上报**：若自纠错仍未成功，才将最终错误上报 Dispatcher，触发升级链。

自纠错期间，Worker 通过 `report_progress` 上报进度（如「自纠错第 1 次尝试」），避免 Dispatcher 误判为「卡住」。

### 动态工具权限（Dynamic Tool Permissions）

CC Worker 不再固定 `--allowedTools Read,Bash,Edit,Write,Glob,Grep`，而是根据 **任务类型** 动态分配：

| 任务类型 | 允许工具 | 说明 |
|----------|----------|------|
| `default` | `Read,Bash,Edit,Write,Glob,Grep` | 默认全开 |
| `api_development` | `Read,Bash,Edit,Write,Glob,Grep` | API 开发 |
| `database_migration` | `Read,Bash,Edit,Glob,Grep` | 禁止 `Write`（防止随意创建文件） |
| `refactoring` | `Read,Edit,Glob,Grep` | 禁止 `Bash`（防止跑测试/构建干扰） |
| `testing` | `Read,Bash,Glob,Grep` | 禁止 `Edit/Write`（只读测试分析） |
| `configuration` | `Read,Edit,Glob,Grep` | 禁止 `Bash/Write`（配置修改需谨慎） |
| `hotfix` | `Read,Bash,Edit,Write,Glob,Grep` | 紧急修复全开 |

角色也可覆盖工具权限（如 `archaeologist` 仅 `Read,Bash,Glob,Grep`）。

任务包通过 `task_type` 字段声明类型，Dispatcher 在 `assign_task` 时填入。

## 无人值守运行（Headless / YOLO 模式）

CC Worker 在 Docker 容器中完全无人值守运行，必须避免所有交互式阻塞。

### 首次运行防阻塞

Claude Code CLI 首次运行时会交互式询问条款同意、API key 等。CC Worker 通过三层机制避免阻塞：

**1. `--bare` 最小化模式**

调用参数中始终包含 `--bare`：
- 跳过 hooks、LSP、plugin sync、attribution
- 跳过 auto-memory、background prefetches、keychain reads
- 跳过 CLAUDE.md auto-discovery
- Anthropic auth 严格使用 `ANTHROPIC_API_KEY` 环境变量（不尝试 OAuth/keychain 交互）

**2. 预创建 `~/.claude/settings.json`**

容器启动时（`entrypoint.sh`）预创建配置文件：
```json
{
  "skipAutoPermissionPrompt": true,
  "skipDangerousModePermissionPrompt": true
}
```

**3. 环境变量检查**

`entrypoint.sh` 检查 `ANTHROPIC_API_KEY` 是否存在，缺失时输出警告但继续运行（任务执行时会在 `_run_claude` 中返回错误）。

### 运行时环境变量

| 环境变量 | 必需 | 说明 |
|----------|------|------|
| `AGENT_ID` | ✅ | Agent 唯一标识 |
| `CC_ANTHROPIC_API_KEY` | ✅ | Claude Code CLI 专用 API key（Anthropic SDK 格式） |
| `CC_ANTHROPIC_BASE_URL` | ❌ | CC Worker 专用 base_url，如 `https://api.deepseek.com/anthropic` |
| `AGENT_API_TOKEN` | ❌ | Dispatcher API 鉴权 token |
| `DISPATCHER_BASE` | ❌ | Dispatcher 地址，默认 `http://dispatcher:8080` |
| `SKIP_CLAUDE` | ❌ | `1` = 跳过 claude 执行（测试用） |
| `CC_DRY_RUN` | ❌ | `1` = 只写提示词不执行 |
| `CC_MAX_SELF_RETRY` | ❌ | 自纠错最大重试次数，默认 `2` |

**注意：两套独立的 LLM 配置**

Dispatcher 调用 LLM（`model_providers.api_base`）与 CC Worker 容器中的 Claude Code CLI 调用 LLM 是**完全独立的两个通道**：

```
Dispatcher ──AsyncOpenAI──▶ https://api.xxx.com/v1          (OpenAI 格式 /chat/completions)
CC Worker  ──AnthropicSDK──▶ https://api.xxx.com/anthropic  (Anthropic 格式 /v1/messages)
```

- `CC_ANTHROPIC_API_KEY` / `CC_ANTHROPIC_BASE_URL`：专给 CC Worker 容器中的 `claude` CLI 使用
- 未设置 `CC_ANTHROPIC_*` 时自动回退到 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`
- 如果中转代理同时提供两种格式，CC Worker 必须走 Anthropic 兼容端点（通常带 `/anthropic` 路径）

## 分阶段落地（建议）

| 阶段 | 内容 | 产出 |
|------|------|------|
| P0 | 术语与边界写进 AGENTS / HANDOFF；**CC 官方入口**：仓库根 **`CLAUDE.md`**（`@` 引用 **`docs/AI_TEAM_MEMBER_CHARTER.md`**）；任务包 schema 预留 `executor_hint: claude_code`（可选） | 文档 + 字段共识 |
| P1 | 本地/CI：`claude` CLI 或官方 headless 入口被 **wrapper** 调用，输入为 JSON 任务包，输出为 diff 摘要 + exit code；支持自纠错 + 动态工具权限 | **`agents/cc-worker/executors/`**（`CodingExecutor`、`ArchaeologistExecutor`）+ **`agents/cc-worker/run_task_pack.py`**；与编排 MQ 对齐时另见 **`agents/cc-worker/pipeline_cc_dispatch.py`** / **`consume_cc_dispatch_stream.py`**（`executor_hint=claude_code`，收口 **`/api/webhook/task-complete`**） |
| P2 | 与人类 Worker 对齐：Cursor 插件或独立小工具从 Dispatcher **拉任务** → 生成 CC 启动参数 → 人确认后执行 → **经现有 webhook/MQ** 回传 | **`cursor/src/commands/runWithCC.ts`**（CC 执行命令）+ **`cursor/src/prompt/generator.ts`**（task-pack 生成）；完整插件仍与 `48-CURSOR_PLUGIN_PROPOSAL.md` 同构演进 |
| P3 | 可选：客户机 connector 分支上 **可选**拉起 CC（仅当客户许可安装且策略允许），与 OpenClaw 二选一或串行 | 需安全评审与版本钉死 |

## 非目标

- 用 CC **替换** 阶段文档生成（Stage 0–3）、架构师聊天、token 计费等服务端 LLM 调用。
- 在 **无 Git 分支** 的任务上强行走 CC 又绕过 46 号文中的分支约束。

## 待决（实现前需产品拍板）

- Claude Code **许可、席位、日志留存** 与多租户审计的对齐方式。
- **无人值守** 全自动化程度上限（与 AGENTS.md 中「前端优先 human + IDE」一致时，UI 任务仍不应默认无人 CC）。
