# Leader 与 Architect 治理规则

> 目标：明确 Leader、Architect、人类三者职责边界，防止角色越权与无限消耗。

---

## 一、角色边界

- **Leader**：业务推进、节奏管理、任务编排、状态通知（不做代码技术评审）。
- **Architect**：技术方案、任务分解、代码审核、技术把关；在 **CC 默认执行器 + MQ 协作** 路线下，还负责为执行侧签发结构化 **`task_context`**（目标、范围、`context_keys[]`、验收）并维护与本迭代相关的技术决策摘要，与 **`docs/74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md`** 第二节表格一致（Leader 不抢接口与目录级执行细节）。
- **Human**：最终裁决者，处理 `blocked` 任务。
- **Dispatcher**：平台治理者，负责全部 Agent 的心跳巡检、失联恢复、自动重启与任务回收重派。

### 全栈优先组织原则（AI 时代）

- 默认采用**全栈团队设计**，避免“前后端严格分治”导致的跨角色沟通损耗。
- 每个 Agent 都是**全功能能力体**（可读写代码、调试、测试、文档），角色仅决定其治理职责与决策边界。
- `architect/senior/mid/junior/devops` 等角色标签是**执行焦点**，不是能力阉割或工具权限割裂。
- 派单优先按任务焦点选择角色，但允许全栈 Agent 在单任务内跨前后端闭环交付，以减少上下文切换成本。
- 治理原则采用：**Capability-Complete, Role-Constrained**（能力全集，角色约束）。
- 角色约束通过 `dispatcher/app/skill_packs/programming/roles/*.md` 的 Skill Profile frontmatter 定义，调度与审核按该约束执行。
- Skill Pack 路径采用单轨策略：仅使用 `skill_packs/<pack>/roles`，不再兼容旧 `app/roles`。

---

## 二、Leader 模型策略

- 默认模型：`deepseek-chat`（成本优先）。
- 人类可临时指定更强模型（如 `opus`）串场。
- 串场只影响本次调用的 `model_used`，不改变会话归属。

**会话归属规则：**
- Stage 0-3 所有管理/分析会话，`owner_role` 固定为 `leader`。
- 每条消息记录 `model_used` 与 `model_override`，用于追溯。

---

## 三、架构师前期介入与继承

- Stage 0-3 中，架构师以预聘身份参与技术分析。
- Stage 4+ 正式 Architect Agent 入场后，继承 Stage 0-3 全量会话与文档上下文。
- 继承目标是“无缝恢复工作”，避免前后割裂。

---

## 四、Blocked 处理规则

- 架构师无法解决时，任务进入 `blocked`。
- `blocked` 的责任人是人类，不是 Leader。
- Leader 仅做通知，不做技术接管。
- 解除 `blocked` 仅允许人类执行（认领或重分配）。

---

## 五、实现约束（当前）

- Dispatcher 不再进行 Leader 自动代码审核。
- 任务进入 `reviewing` 后，只等待 Architect Agent 或人工审核。
- 人类工程师提交的编码结果同样必须进入 `reviewing`，默认由 Architect 审核后才算 `done`。
- 人类可临时以 `boss` 身份执行越权通过（`force_override=true`），该操作仅允许 `approve` 且必须留痕。
- 自动派发采用 `architect_only`：仅 `architect/human` 可触发全局自动指派。
- 心跳异常不由 Architect 兜底：全部由 Dispatcher 恢复链路处理（回收 stuck 任务并重派）。
- 文档 `AI 审核` 默认模型改为 `architect`（Opus 口径），用户可手动覆盖。
- Stage 会话消息带 `metadata.owner_role=leader` 与 `metadata.model_used`。
- `resolve_blocked_task` 要求 `actor_role=human`，非人类请求拒绝。

---

## 六、任务执行稳定性护栏

- `task_failed` 增加状态机校验：仅 `assigned/executing` 允许进入失败重试链。
- 延迟或重复失败回调按 `stale callback` 忽略，避免重试计数异常膨胀。
- 审核升级增加保险阀：当 `review_escalation_rounds >= MAX_REVIEW_ESCALATIONS` 时，任务直接维持 `blocked`，等待人类处理。
- Agent 失联达到 `dead/start_failed/abandoned` 时，Dispatcher 自动回收其 `assigned/executing` 任务到 `pending`，并触发重派。
- 团队成员页展示 `Dispatcher 自动恢复记录`，用于追踪失联回收与重派动作。
- Git 协作采用“人类团队规范”：每个任务独立分支；架构师审核通过后才合并；常规任务合并到 `develop`，`bug/hotfix` 从 `main` 分支创建并回合 `main`。
- 项目全局知识固定入口：`docs/00-GLOBAL_KNOWLEDGE_INDEX.md`，允许入口文档引用更多细则文档；任务派发默认注入该入口上下文。
- 入口文档更新后，由人类/架构师发送“补训通知”，广播给全体 Agent，确保制度变更显式生效。
- 新增硬门禁：Agent 未确认当前全局知识版本时，`scheduler` 不会将任务自动派发给该 Agent。
- 通知发送时会自动计算并标记“待补训成员”，便于确认版本更新后的执行闭环。
- 版本语义采用“递增修订号”：当 `ack_revision < required_revision` 时一律视为未补训，完成一次最新版本确认即可跨越中间版本。
