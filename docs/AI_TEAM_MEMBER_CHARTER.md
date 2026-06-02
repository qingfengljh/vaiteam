# AI 团队成员总体约束宪章

> **适用范围**：凡在本仓库以自动化方式参与工程的角色（**Claude Code** 会话、**Cursor** 内 Agent、其他接入同一 Git 的 AI 协作者），均视为「AI 成员」，须遵守本文与 **`AGENTS.md`**；冲突时以 **既有产品文档（46 / 50 / 74 号等）与代码事实** 为准，本文负责把共识压成可执行条款。  
> **Claude Code 加载方式**：见仓库根 **`CLAUDE.md`**（官方项目记忆入口，使用 `@` 引用本文与 `AGENTS.md`）。

---

## 1. 仓库与上下文边界

- 工作目录以 **`openclaw-team/` 仓库根** 为准；**不要**默认读取上一层 `ai-orchestration/` 下的材料，除非人类点名路径。
- **不必通读 `docs/`**：先读 **`REPO_MAP.md`**、**`AGENTS.md`**、**`docs/README.md`**，再按任务打开编号文档。
- 根目录 `ai-orchestration` 内 **`00xx*.md`**：未点名则视为标杆/规格参考，**不**替代本仓实现与接口。

---

## 2. 角色与职责（与产品一致）

| 角色 | 职责边界（对 AI 成员的含义） |
|------|------------------------------|
| **Leader / 编排** | 阶段、优先级、人在环门控；AI **不**替人类自动跨越已约定门禁。 |
| **架构师** | 全局技术视图；为执行侧提供结构化 **`task_context`**（目标、范围、`context_keys[]`、验收、可含禁止修改路径）；**集成分支合并与冲突收口**默认由架构师（或 Owner 指定收口人）执行。 |
| **执行成员（含 CC）** | 仅在已分配任务与 **`task_context`** 范围内改动；默认 **窄上下文**；阻塞时走结构化上报，不私自扩权拉全库。 |

---

## 3. 执行通道与状态（硬约束）

- 任务状态推进只走既有 **Dispatcher / webhook / inbox / MQ** 路径。
- **禁止**在 Worker、脚本或客户端侧**直接写数据库**篡改任务状态以「冒充完成」；完成 / 失败只经 **约定 API 或消息** 与状态机对齐（见 **`docs/46-EXECUTION_CHANNEL_PROTOCOL.md`**）。
- 编码类交付继续 **Git 分支 + 任务包**；与 **46 / 50** 号文的 `commit+push`、回传语义一致，不发明第三套「口头完成」。

---

## 4. 协议与命名

- 协议与持久化字段优先使用 **`executor` / `actor_type`** 等中性名；枚举可含 **`claude_code`** 等执行器类型。
- **避免**把特定商业产品名写死在协议常量中；历史 **`connector`** 等名称在过渡期保持兼容即可。

---

## 5. 改动范围与非目标

- **少动** `saas/portal-api`、`saas/portal-web` **核心业务**；与租户/计费/控制面强相关的改动须可评审、可回滚。
- **CC 适配、wrapper、CLI、小工具** 放在 **`dispatcher/` 侧工具目录**、`scripts/`、`tools/` 等约定位置，**不**把 CC 编排逻辑塞进 Portal 仅为图省事。
- **不做**：在 Dispatcher 服务端代跑 CC；本迭代 **不大改** SaaS / install-agent **交付主链**（除非与已评审的 P0 协议字段明确冲突）。

---

## 6. 工程习惯

- **最小必要改动**：不无关重构、不扩大 PR 范围。
- **说明语言**：与用户协作默认 **中文**；提交信息遵循本仓库既有习惯。
- **跨多个顶层目录**（如 `dispatcher` + `web` + `saas`）时，在回复或 PR 中**分段说明**各自职责，避免混称「一个服务」。

---

## 7. Git：分阶段提交推送、成员分支、合并归架构师

- **分阶段完成须提交并推送**：每个可对外交接的小阶段结束时 **`git commit`**；在允许他人基于你的成果继续前 **`git push` 到 `origin`**（与 **`docs/46-EXECUTION_CHANNEL_PROTOCOL.md`** 中「代码真源在 Git、无 push 不得假完成」一致）。仅本地临时探索须在 HANDOFF 写明且不得冒充已交付。
- **各成员自有分支**：不同 AI 成员（及人类协作者）在**各自任务分支或约定的成员命名分支**上工作（如 Dispatcher 派发的 **`git_branch`** / `task/REF-…`），**避免**多人直接在同一集成分支上交错提交导致不可读历史。
- **合并由架构师收口**：向 **`develop` / `main` / 项目约定集成支`** 的 **merge、冲突解决、是否 squash、是否允许快进** 由 **架构师**执行（或 **人类 Owner 书面指定**的同一收口人）；执行侧 Agent **不**擅自合并他人分支进主线、**不**对受保护分支 **`--force` 推送**。

---

## 8. 文档与看板联动

凡触及 **派单载荷、执行器、任务包 schema、MQ 完成语义** 的实现，须同步更新：

- **`docs/50-CLAUDE_CODE_WORKER.md`**
- **`docs/46-EXECUTION_CHANNEL_PROTOCOL.md`**
- **`docs/74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md`**
- 若影响文档化能力：**`docs/00-README.md`** 看板对应行。

若与 **Leader / 架构师** 分工表述冲突：先改 **`docs/32-LEADER_ARCHITECT_GOVERNANCE.md`**，再改 74 号（见 74 第六节）。

### 生成文档与「DB → 磁盘 Git」

- **需要提交 Git**：凡属**交付物、门控依据、可审计资产**的**生成文档**（阶段 Markdown、规格、评审结论导出、原型产物等），在可验收时应落在 **被版本库跟踪的路径**并完成 **`git commit` + `git push`**；与第七节代码分支策略一致，**不以「只存在于会话或仅 DB」当作已交付**。
- **数据库与磁盘**：Dispatcher / Portal 中的**原始数据与运行态**可在库内持续更新；其中需**长期保留、可比对、可灾备**的内容，应通过产品已有的**导出、同步或写入仓库路径**的机制，**更新到磁盘上的 git 工作区并纳入提交**（例如 `docs/`、项目文档树、`prototype-workshop/artifacts/` 等约定位置），使 **Git 与托管远程**成为与 **46 号**一致的资产层。
- **例外**（可不立即入库、或仅 DB）：须由 **Owner 或架构师**在 HANDOFF / 轻量 ADR 中写明范围与保留期限，避免默认「生成即丢」。

---

## 9. 双栈规范入口（工具对齐）

| 工具 | 规范入口 |
|------|----------|
| **Claude Code** | 仓库根 **`CLAUDE.md`**（`@` 引用本宪章与 `AGENTS.md`）；可选 **`.claude/rules/*.md`** 做路径细分。 |
| **Cursor** | **`.cursor/rules/*.mdc`**（`alwaysApply` / `globs`）；说明见 **`docs/AI_TEAM_RULES_CURSOR_STYLE.md`**。 |

人类 Owner 为新会话粘贴约束时，可只贴：**本宪章全文** + 当次 **`docs/session-handoff/`** 任务说明。

---

## 10. 必读索引（按任务扩展）

- 虚拟团队总览：**`docs/74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md`**
- CC Worker 边界与分期：**`docs/50-CLAUDE_CODE_WORKER.md`**
- Anthropic 对项目记忆的说明：<https://docs.anthropic.com/en/docs/claude-code/memory>
