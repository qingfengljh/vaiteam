# 虚拟编程团队：CC 执行器 + MQ 协作 + 架构师工作中心

> **定位**：在不大改产品愿景（Leader 编排、阶段门控、人在环）的前提下，把**工程推进重心**从「OpenClaw/connector 半成品」迁到 **Claude Code（CC）作默认编码执行器**，并用 **MQ 作为团队运转的脊柱**，由 **架构师角色** 持有全局技术视图与任务上下文出口。  
> **关联**：`73-THREE_ENGINEERING_WORKSTREAMS.md` 主线 1、`50-CLAUDE_CODE_WORKER.md`、`46-EXECUTION_CHANNEL_PROTOCOL.md`、`32-LEADER_ARCHITECT_GOVERNANCE.md`、`30-TASK_DECOMPOSE_TO_EXECUTION.md`、**`PROTOTYPE_CC_RUN_PIPELINE.md`**（原型 CC：runs/start + webhook，与 MQ 主回调解耦）。

**工程优先级（共识）**：先落地 **已定稿的沟通 / MQ / 派发机制**，并推进 **OpenClaw connector → CC** 的执行器替换；二者与 **快速原型**支线可并行，最后再衔接。沟通层与编排 **包裹在执行器之外**，壳内实现可为 OpenClaw、CC 或其它适配（见 **`PROTOTYPE_CC_RUN_PIPELINE.md`**「两条主线并行」）。

---

## 1. 为什么要借 CC 的「衡量方式」

Claude Code 类产品在工程上通常强调几件事，与本仓库已有方向**一致**，可直接吸收为**设计约束**（不是换口号）：

| CC 侧常见约束 | 映射到 VAI TEAM |
|----------------|-----------------|
| **有界工作区**：在明确目录与分支上改，可复现 | 继续强制 **Git 分支 + 任务包**（46 号），CC 只多一种 **Worker 实现**，不新开「口头完成」通道 |
| **单次会话目标清晰**：输入契约可读、输出可验收 | 架构师 / Leader 产出 **`task_context`**：目标、范围、`context_keys[]`、验收；执行端**窄上下文**（见 `AGENTS.md`） |
| **人机位**：授权、密钥、网络在终端侧 | Dispatcher **不**在服务端代跑 CC；无人 CC 仅在有策略的客户机或隔离容器内（50 号 P1–P3） |
| **可审计**：命令、diff、日志 | MQ 回传 + Git 历史 +（可选）artifact；与现有 webhook / inbox 对齐 |

**结论**：CC 不是「再做一个大脑」，而是 **更接地、可验收的编码执行器**；编排与架构治理仍在一处（Dispatcher + 治理规则）。

---

## 2. OpenClaw → CC：迁移语义（避免大方向漂移）

- **保留**：`assign_task` 状态机、`connector`/`human` 通道、install-agent 交付链、SaaS 安装任务等**已跑通能力**。  
- **收敛**：**新建/改代码类任务**默认目标执行器 = **CC Worker**（容器内 headless、或人机协同的 P2 工具链），与 **同构任务包** 对齐。  
- **过渡期**：客户机仍可走 `connector.mjs`（历史称 OpenClaw 链）；文档与配置里用 **`executor` / `actor_type`** 区分，避免把品牌名写死在协议里。  
- **非目标**：CC **不**替代 Leader 文档生成、架构师对话、计费路由（50 号「非目标」保持）。

---

## 3. MQ 驱动下的「架构师为中心」协作（有条不紊的含义）

**有条不紊** = 少旁路、少私聊上下文；**状态与意图**优先落在 **Dispatcher 已持久化模型 + 消息**，而不是各 Agent 各自拉全库。

建议的**权力边界**（与 `32-LEADER_ARCHITECT_GOVERNANCE.md` 互补）：

1. **架构师（或调度器代填架构师模板）**  
   - 维护与本迭代相关的 **技术决策摘要**（ADR 级短文档或结构化字段）。  
   - 为每个子任务签发 **`task_context`**：**依赖顺序、接口契约、禁止修改路径、验收口径**。  
   - 对 **NEED_CONTEXT** / 扩上下文请求做 **批准或驳回**（防执行端偷偷加宽上下文）。

2. **MQ 脊柱**（与 46 号一致，此处强调「团队节奏」）  
   - **派单**：`assign_task` → inbox（或等价队列）→ 唯一消费方认领。  
   - **进度**：heartbeat / 进度消息可观测，避免「只有最终 PR 才知道死活」。  
   - **完成**：**仅** 通过约定 API（webhook / 完成消息）推进状态机；禁止 Worker 私改 DB 任务状态。  
   - **并行**：多 Worker 时，**依赖边**由架构师任务图或调度器拓扑排序决定，避免抢写同一分支（可配合分支命名规范）。

3. **Leader**  
   - 偏产品阶段、跨角色优先级、与人类 Owner 对齐；**不**与架构师抢「接口与目录级」执行细节。模糊时人类在双会话里澄清（见 `session-handoff/`）。

4. **执行成员（CC / human / connector）**  
   - 只消费 **已分配** 任务；阻塞时上报 **结构化 blocked**（原因类型、缺什么上下文），回到架构师或调度器。

---

## 4. 分阶段实施（与 50 号文 P0–P3 对齐，补充「架构师 + MQ」动作）

| 阶段 | 执行器侧重 | 架构师 / MQ 侧重 |
|------|------------|------------------|
| **P0** | 字段与枚举：`executor_hint` / `actor_type` 预留 `claude_code`（**已实现**：`dispatcher/app/execution_hints.py`、创建/更新任务 DTO、`assign_task` inbox + MQ metadata） | 文档与看板：本文件 + `00-README` 进度行；代码评审门禁不变 |
| **P1** | 本地/CI wrapper：JSON 任务包 → CC → diff + exit code | 任务包模板固定：`context_keys`、验收列表必填 |
| **P2** | Cursor 插件或小工具：拉任务 → 生成 CC 启动参数 → **经 MQ 回传** | 架构师会话产出与 **HANDOFF** 同步；减少非结构化粘贴 |
| **P3** | 客户机可选 CC 与 connector **二选一或串行** | MQ 健康看板（46 号「下一步」）：backlog、失败率、Git push 成功率 |

---

## 5. 当前工作中心建议（切换焦点）

当 **SaaS / install-agent / dispatcher 安装** 等交付线达到「可重复成功」后，建议把日常研发重心切到：

1. **协议与字段**：任务包 schema、完成语义、executor 枚举（P0）。  
2. **CC 适配层**：P1 脚本 + P2 与人协同路径，**不**扩散到 Portal 核心业务耦合。  
3. **可观测**：MQ + Git 联合看板，支撑架构师判断「卡在哪一环」。  
4. **原型工坊（主线 3）**：在 1）的 P0 稳定后并行，避免双协议。

详细三条主线索引见 **`73-THREE_ENGINEERING_WORKSTREAMS.md`**。

---

## 6. 文档维护约定

- 实现 CC 路径或改派单载荷时：**同步** `50-CLAUDE_CODE_WORKER.md`、`46-EXECUTION_CHANNEL_PROTOCOL.md` 与本文件相关小节。  
- 若产品重定义「架构师」与「Leader」分工：先改 **`32-LEADER_ARCHITECT_GOVERNANCE.md`**，再改本文件第二节表格，避免两处打架。
