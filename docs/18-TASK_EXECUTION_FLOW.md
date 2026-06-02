# 任务推进流程与 Git 多人协作

## 一、全局视角

```
Stage 0-3: 文档驱动阶段（人 + Leader AI 协作）
    业务方案 → 需求分析 → 产品原型 → 技术方案
    每阶段产出文档，经审核冻结后流入下一阶段

Stage 4: 任务分解（Leader AI + 架构师 AI）
    技术方案 → 模块级拆分 → 编码级拆分 → 人工审核任务清单

Stage 5: 编码实现（架构师主导，全栈 AI 执行）
    调度器按依赖关系分配 → Agent 编码 → 架构师/人工审核代码 → 解锁下游
    角色行为由 Skill Pack 约束（能力全集 + 角色治理焦点）

Stage 6: 测试验证（测试 AI + 人类验收）
    创建 release 分支 → 自动化测试 → 人类集成验收

Stage 7: 部署交付（运维 AI + 人类确认）
    合并到 main → 打 tag → 部署生产
```

---

## 二、任务生命周期

### 2.1 完整状态流转

```
                    ┌──────────────────────────────────────────────────────────┐
                    │                                                          │
draft ──[人工审核通过]──→ pending ──[调度器分配]──→ assigned ──[Agent完成]──→ reviewing
                                     ↑                                         │
                                     │                          ┌──────────────┤
                                     │                          │              │
                                     │                   AI架构师审核      人工审核
                                     │                          │              │
                                     │                     ┌────┴────┐    ┌────┴────┐
                                     │                   通过      不通过  通过     不通过
                                     │                     │         │     │         │
                                     │                   done        │   done        │
                                     │                     │         │               │
                                     │              解锁下游任务      └───────┬───────┘
                                     │                                       │
                                     └───────────── pending (打回重做) ←──────┘
                                                         │
                                                  [不通过超过2次]
                                                         │
                                                         ▼
                                              pending (升级为 Opus 模型)
                                                         │
                                                  [Opus 也失败]
                                                         │
                                                         ▼
                                                      blocked
                                                    (人类介入)
```

### 2.2 状态说明

| 状态 | 含义 | 谁负责推进 |
|------|------|-----------|
| `draft` | AI 生成的任务草案，等待人工确认 | 人类点击「通过」或「驳回」 |
| `pending` | 已确认，等待调度器分配给空闲 Agent | 调度器自动处理 |
| `assigned` | 已分配给 Agent，正在编码 | Agent 编码中 |
| `reviewing` | Agent 提交代码，等待审核 | AI 架构师自动审核 / 人工手动审核 |
| `done` | 审核通过，任务完成 | — |
| `failed` | 最终失败（极少出现） | 人类决策 |
| `blocked` | AI 多轮重试失败，需要人类介入 | 人类处理 |
| `cancelled` | 已取消（迭代终止或变更请求） | 系统自动 / 人类确认 |
| `superseded` | 已被新任务取代（变更后） | 人类确认 |

---

## 三、依赖管理与并行调度

### 3.1 依赖定义

AI 在任务分解时为每个任务标注依赖关系。依赖使用数组索引表示，系统自动转换为真实的任务 UUID。

```json
{
  "tasks": [
    {"title": "数据模型定义",    "dependencies": []},
    {"title": "数据库迁移脚本",   "dependencies": []},
    {"title": "后端 API 实现",   "dependencies": [0, 1]},
    {"title": "前端页面",        "dependencies": [2]},
    {"title": "单元测试",        "dependencies": [2]}
  ]
}
```

### 3.2 调度规则

调度器 `get_ready_tasks()` 的逻辑：

1. 查询所有 `status = "done"` 的任务 ID 集合 `done_ids`
2. 查询所有 `status = "pending"` 的任务，按优先级降序
3. 过滤：只保留 `dependencies` 全部在 `done_ids` 中的任务
4. 这些就是「就绪任务」，可以被分配

```
任务 0: 数据模型定义        dependencies: []        → 立即就绪
任务 1: 数据库迁移脚本       dependencies: []        → 立即就绪（与 0 并行）
任务 2: 后端 API 实现       dependencies: [0, 1]    → 等 0 和 1 都 done
任务 3: 前端页面             dependencies: [2]       → 等 2 done
任务 4: 单元测试             dependencies: [2]       → 等 2 done（与 3 并行）
```

### 3.3 自动分配策略

```python
for task in ready_tasks:
    agent = idle_agents.pop(task.suggested_role)  # 优先匹配角色
    if not agent:
        agent = idle_agents.popitem()              # 无精确匹配则任意空闲
    assign(task, agent)
```

多个就绪任务 + 多个空闲 Agent = 并行执行。

### 3.4 完成后自动解锁

每当一个任务审核通过（`done`），调度器立即重新执行 `auto_assign`：

1. 重新计算 `done_ids`（新增了刚完成的任务）
2. 重新筛选就绪任务（可能有新任务的依赖被满足了）
3. 分配给空闲 Agent

这形成了一个自驱动的流水线：**完成一个 → 解锁下游 → 分配 → 完成 → 解锁更多**。

---

## 四、架构师代码审核机制

### 4.1 审核触发

Agent 完成编码后，任务进入 `reviewing` 状态。系统自动触发 AI 架构师审核：

```
Agent 提交 → reviewing → AI 架构师自动审核
                              │
                    ┌─────────┴─────────┐
                  通过                 不通过
                    │                    │
                  done              pending (打回)
```

### 4.2 AI 审核内容

AI 架构师审核时评估：

- **功能完整性**：是否满足任务描述和验收标准
- **代码质量**：是否有严重问题（critical issues）
- **评分**：1-10 分

审核结果分为：
- `approved = true` 且无 critical issue → 自动通过
- `approved = false` 或有 critical issue → 自动打回

### 4.3 打回重做

打回时，审核意见存入 `task.context.last_review_comments`，Agent 重做时会参考。

### 4.4 升级机制

| 打回次数 | 处理方式 |
|---------|---------|
| 第 1 次 | 打回给原 Agent，附审核意见 |
| 第 2 次 | 打回给原 Agent，附审核意见 |
| 第 3 次（超限） | **升级为 Opus 模型**：任务模型切换为 Opus，重新分配给工程师 |

> **设计原则**：架构师始终不下场编码，保持全局视角专注于分析、分解和审核。
> 升级时通过切换更强的模型（Opus）来解决问题，而非让架构师承担编码工作。

Opus 模型工程师接手后的流程：
- 完成 → 同样进入 `reviewing`（AI 架构师审核或人工审核）
- Opus 也失败 → `blocked`，升级给人类

### 4.5 人工审核

人类可以在前端任务看板中手动审核 `reviewing` 状态的任务：
- 点击「审核代码」→ 填写意见 → 通过 / 不通过
- 人工审核优先级高于 AI 自动审核

---

## 五、Git 多人协作流程

### 5.1 三层分支模型（生产隔离）

```
main                    ← 生产分支（production），只接受人工验收后的发布/热修复
  │
  └── develop           ← 集成分支（integration），架构师审核通过后合并到此
        │
        ├── feat/TASK-001-user-model        (编码 AI 工作分支)
        ├── feat/TASK-002-user-api          (编码 AI 工作分支)
        ├── feat/TASK-003-login-page        (编码 AI 工作分支)
  └── fix/TASK-010-login-bug                (热修复分支，从 main 检出)
  │
  └── release/iter-1    ← 测试分支，从 develop 拉出
                           AI 跑自动化测试 + 人类做集成验收
                           验收通过 → merge 到 main + 回合到 develop
```

### 5.2 分支职责

| 分支 | 维护者 | 用途 |
|------|--------|------|
| `feat/{ref_id}-{slug}` | 全栈编码 AI | 单个任务工作分支，从 `develop` 检出，审核后回合 `develop` |
| `fix/{ref_id}-{slug}` | 全栈编码 AI | Bug/热修复分支，从 `main`（production）检出，审核后回合 `main` |
| `develop` | 架构师 AI | 汇聚所有审核通过的代码，解决合并冲突 |
| `release/iter-{seq}` | 测试 AI + 人类 | 集成测试分支，从 develop 拉出 |
| `main` | 人类 | 生产分支，只有验收通过的 release 才能合并 |

### 5.3 分支命名规范

任务创建时自动生成分支名：

```
{type}/{ref_id}-{slug}

示例：
  feat/TASK-001-user-model
  feat/TASK-002-user-api
  fix/TASK-010-login-validation-bug
```

### 5.4 Commit 规范

```
[{ref_id}] {简要描述}

Refs: {关联的需求文档标题}
Iteration: {迭代标题}
```

示例：

```
[TASK-001] 创建 User 数据模型和迁移脚本

Refs: 需求规范 - 用户认证模块
Iteration: v1.0 初始版本
```

### 5.5 完整协作时序

```
Stage 5 编码阶段：

  调度器                  编码 AI              架构师 AI             Git
    │                       │                     │                  │
    │──分配任务+分支名──→   │                     │                  │
    │                       │──checkout -b feat/TASK-001──────────→  │
    │                       │     编码 + 单元测试                    │
    │                       │──commit + push──────────────────────→  │
    │                       │──webhook: 完成──→   │                  │
    │                       │                     │                  │
    │   任务 → reviewing    │                     │                  │
    │                       │                     │──fetch + review diff
    │                       │                     │                  │
    │                       │              ┌──审核通过──┐             │
    │                       │              │           │             │
    │                       │              │   merge feat→develop──→ │
    │   任务 → done         │              │           │             │
    │   解锁下游任务        │              │           │             │
    │                       │              │           │             │
    │                       │              └──审核不通过─┘             │
    │                       │                     │                  │
    │   任务 → pending      │←──审核意见──────────│                  │
    │──重新分配──────────→  │                     │                  │
    │                       │     修改代码                           │
    │                       │──commit + push──────────────────────→  │
    │                       │                     │                  │
    │                       │              ┌──再次审核──┐             │
    │                       │              │    ...    │             │

所有任务 done 后：

  架构师 AI 自动生成「集成测试就绪评估报告」
    - 集成风险点
    - 集成测试计划
    - 回归测试建议
    - 是否建议进入测试阶段

Stage 5 → Stage 6 推进条件：
  所有子任务必须 status = "done"（系统强制检查）

Stage 6 测试阶段：

  架构师 AI             测试 AI              人类                  Git
    │                     │                   │                    │
    │──从develop创建release/iter-1─────────────────────────────→  │
    │                     │                   │                    │
    │                     │──在release分支跑集成测试               │
    │                     │──在release分支跑E2E测试                │
    │                     │                   │                    │
    │              ┌──测试通过──┐              │                    │
    │              │           │              │                    │
    │              │  部署到测试环境           │                    │
    │              │           │──────────→   │                    │
    │              │           │         人类集成验收               │
    │              │           │              │                    │
    │              │    ┌──验收通过──┐         │                    │
    │              │    │          │         │                    │
    │              │    │  merge release→main─────────────────→   │
    │              │    │  tag v1.0───────────────────────────→   │
    │              │    │  merge release→develop──────────────→   │
    │              │    │          │         │                    │
    │              │    └──验收不通过─┘         │                    │
    │              │           │              │                    │
    │              │  创建修复任务             │                    │
    │              │  编码AI在release分支修复   │                    │
    │              │  重新测试                 │                    │
    │              └───────────┘              │                    │
```

---

## 六、升级链（Escalation Chain）

任务执行失败时的完整升级路径：

```
Level 0: 工程师/Sonnet 执行
    ├── 成功 → reviewing → 架构师审核
    └── 失败 → 自动重试（最多 2 次，末次可升级模型）
         └── 重试耗尽 → 升级到 Level 1

Level 1: 工程师/Opus 执行（升级模型，角色不变）
    ├── 成功 → reviewing → 架构师审核
    └── 失败 → 自动重试（最多 2 次）
         └── 重试耗尽 → 升级到 Level 2

Level 2: 人类介入
    任务标记为 blocked
    人类选择：
      ├── 提供修复思路，重新分配给 AI（可选重置到 Level 0 或 Level 1）
      └── 自己认领处理
```

### 模型升级策略

重试过程中，最后一次重试前可自动升级模型：

```
deepseek-chat → claude-sonnet → claude-opus
```

升级链可在 `openclaw.json` 中配置。

---

## 七、任务分解的两级结构

### 7.1 模块级分解（Leader AI）

Leader 将项目按功能模块拆分：

```
MOD-001: 项目初始化（脚手架）
MOD-002: 用户认证模块
MOD-003: 数据管理模块
MOD-004: 前端页面
```

每个模块 1-3 天工作量，标注模块间依赖关系。

### 7.2 编码级分解（架构师 AI）

架构师将每个模块拆分为 15-60 分钟的编码任务：

```
MOD-002 用户认证模块：
  TASK-001: 创建 User 数据模型          dependencies: []
  TASK-002: 实现注册 API                dependencies: [TASK-001]
  TASK-003: 实现登录 API                dependencies: [TASK-001]
  TASK-004: 实现 JWT 中间件             dependencies: []
  TASK-005: 前端登录页面                dependencies: [TASK-003, TASK-004]
  TASK-006: 单元测试                    dependencies: [TASK-002, TASK-003]
```

### 7.3 任务结构

```json
{
  "title": "实现用户注册 API",
  "description": "完整的自包含描述，包含所有执行者需要的信息",
  "type": "feature",
  "priority": 2,
  "suggested_role": "mid",
  "suggested_model": "sonnet",
  "estimated_hours": 0.5,
  "dependencies": [0],
  "input_files": ["src/models/user.py"],
  "output_files": ["src/api/users.py"],
  "acceptance_criteria": [
    "POST /api/users 可创建用户",
    "重复邮箱返回 409",
    "密码经过 bcrypt 加密存储"
  ]
}
```

---

## 八、进入集成测试的条件

### 8.1 自动检查

Stage 5 → Stage 6 推进时，系统强制检查：

```python
# 所有子任务（非模块级）必须 status = "done"
if not all_subtasks_done:
    raise "编码任务未全部完成（15/20），需要所有任务审核通过后才能进入测试阶段"
```

### 8.2 架构师集成评估

当最后一个编码任务审核通过时，架构师 AI 自动生成「集成测试就绪评估报告」：

1. **集成风险点**：哪些模块之间的集成可能有问题
2. **集成测试计划**：需要测试的关键集成场景（按优先级排列）
3. **回归测试建议**：需要重点回归的功能
4. **是否建议进入集成测试阶段**：明确的 Yes/No 和理由

该报告归档到项目文档系统，可搜索。

### 8.3 人工确认

即使系统检查通过，推进到 Stage 6 仍需人工点击「进入下一阶段」按钮。人类可以参考架构师的评估报告决定是否推进。

---

## 九、测试验证流程（Stage 6）

### 9.1 三阶段测试

| 阶段 | 执行者 | 内容 | 分支 |
|------|--------|------|------|
| 单元测试 | 编码 AI（Stage 5） | 每个任务提交时自带 | feat/TASK-xxx |
| 集成测试 + E2E | 测试 AI | API 集成 + 端到端 | release/iter-{seq} |
| 人类集成验收 | 人类 | 功能性验收测试 | release/iter-{seq} |

### 9.2 测试状态流转

```
coding → auto_testing → human_testing → approved → released
           ↑                  │
           │                  ▼ (不通过)
           └── bug_fixing ←───┘
```

### 9.3 Bug 修复闭环

测试发现 Bug 时：
1. 系统自动创建修复任务（关联原始任务）
2. 编码 AI 在 release 分支上修复
3. 修复后重新跑测试
4. 循环直到全部通过

---

## 十、前端看板展示

### 10.1 看板列

```
待审核(draft) | 待分配(pending) | 执行中(assigned) | 代码审核(reviewing) | 已完成(done) | 失败(failed) | 阻塞(blocked)
```

### 10.2 依赖关系展示

- 每个任务卡片上显示依赖标签：`← TASK-001, TASK-002`
- 依赖未满足：橙色 warning 标签
- 依赖已满足：灰色标签

### 10.3 操作按钮

| 状态 | 可用操作 |
|------|---------|
| `draft` | 通过 / 驳回 / 批量通过 |
| `pending` | 认领（人工） |
| `assigned` + 人工任务 | 提交完成 |
| `reviewing` | 审核代码（通过/不通过） |
| `blocked` | 处理阻塞（重新分配AI / 自己认领） |

---

## 十一、内置 CI/CD（无需 Jenkins）

> **设计决策**：系统内置完整的 CI/CD 能力，不依赖 Jenkins、GitLab Runner 等外部工具。
> AI Agent 在编码阶段已完成 TDD（写测试 → 写代码 → 跑测试），Architect AI 审核时检查测试覆盖，
> 系统通过 SSH 直接在基础设施节点上执行集成测试和部署，形成完整闭环。

### 11.1 为什么不需要 Jenkins

| 传统 Jenkins 职责 | AI 编排系统如何替代 |
|-------------------|-------------------|
| 代码提交触发构建 | Agent 提交时已构建并测试通过 |
| 跑单元测试 | Agent 编码时 TDD，测试通过才提交 |
| 静态代码分析（Sonar） | Architect AI 审核，理解业务上下文 |
| 质量门控 | AI 审核不通过则驳回，不合并 |
| 构建制品 | Agent/DevOps AI 直接构建 |
| 部署到环境 | Stage 7 通过 SSH/Docker 直接部署 |

### 11.2 内置 CI/CD 流程

```
Stage 5 编码阶段（每个任务）:
  Agent 编码 → TDD 写测试 → 跑测试通过 → Git push → Architect AI 审核
    │                                                    │
    │                                              审核不通过 → 驳回重做
    │                                              审核通过 → merge 到 develop
    ▼
Stage 6 测试验证阶段:
  1. 系统在节点上执行全量测试（SSH: make test）← 不消耗 Token
  2. AI 生成代码质量报告（基于审核记录）← 轻量 Token
  3. 人类验收
    ▼
Stage 7 部署交付阶段:
  1. DevOps AI 生成部署配置
  2. 系统在节点上执行部署（SSH: docker compose up）
  3. 健康检查验证
```

### 11.3 TDD 编码规范

每个编码任务的指令中强制要求 TDD：

1. **先写测试**：根据验收标准编写单元测试
2. **再写实现**：编写代码使测试通过
3. **全部通过才提交**：`make test` 或 `npm test` 必须全部通过

Architect AI 审核时检查：
- 是否有对应的测试文件
- 测试是否覆盖核心逻辑
- 没有测试 → 直接驳回

### 11.4 Stage 6 质量报告

Stage 5 → Stage 6 推进时，系统自动生成 AI 代码质量报告（类 SonarQube 风格）：

- **五维度评分**：可靠性、安全性、可维护性、测试覆盖率、代码重复度
- **总评分**：A / B / C / D / E
- **问题列表**：按严重程度排列
- **改进建议**

报告基于所有任务的代码审核记录生成，不需要部署 SonarQube。

### 11.5 Git 分支策略

| Git 分支 | 用途 | 操作者 |
|----------|------|--------|
| `feat/TASK-*` | 编码任务分支 | 编码 AI Agent |
| `develop` | 集成分支 | Architect AI merge |
| `release/iter-*` | 发布分支 | 系统自动创建 |
| `main` | 生产分支 | release 验收通过后 merge |

### 11.6 完整事件流

```
1. 编码 AI push feat/TASK-001 分支
   │
   └─→ Dispatcher 记录 commits
         │
         └─→ Architect AI 审核代码（含测试覆盖检查）
               │
               ├─ 通过 → merge feat → develop
               └─ 驳回 → Agent 修改后重新提交

2. 所有任务完成，进入 Stage 6
   │
   └─→ 系统在节点上执行全量测试（SSH）
         │
         ├─→ AI 生成质量报告
         └─→ 人类验收

3. 验收通过，进入 Stage 7
   │
   └─→ 系统创建 release/iter-1 分支
         │
         └─→ DevOps AI 生成部署配置 → 系统在节点上执行部署
               │
               └─→ 健康检查通过 → merge release → main → 迭代完成
```

### 11.7 兼容外部 CI/CD（可选）

如果用户项目已有 Jenkins/GitLab CI 等外部 CI/CD 系统，可通过 Webhook 对接：

- Dispatcher 的 `/api/webhook/ci` 端点接收外部 CI 回调
- 支持 GitLab Webhook 自动配置（在基础设施管理中操作）
- 外部 CI 结果会更新迭代的测试状态

这是"兼容"能力，不是"依赖"。系统本身不需要外部 CI/CD 即可完成全流程。

---

## 十二、迭代管理与变更请求

### 12.1 迭代生命周期

每个迭代是一条完整的 Stage 0-7 流水线，产出一个可交付版本。

```
迭代状态流转：

  planning ──[Stage 3→4 门禁通过]──→ active ──[Stage 7 完成]──→ completed
      │                                  │
      │                                  └──[人工终止]──→ terminated
      │
      └──[人工终止]──→ terminated
```

| 状态 | 含义 | 允许的操作 |
|------|------|-----------|
| `planning` | 规划中（Stage 0-3） | 人类 + Leader AI 讨论方案、生成文档 |
| `active` | 执行中（Stage 4-7） | Architect 分解任务、Agent 编码、测试、部署 |
| `completed` | 已完成 | 只读 |
| `terminated` | 已终止 | 只读，未完成任务自动标记为 cancelled |

### 12.2 规划并行，执行串行

核心规则：**多个迭代可以同时处于 `planning` 状态，但同一时间只能有一个 `active` 迭代。**

```
迭代 v1.0 (active, Stage 5 编码中)
    │  AI Agent 团队正在执行编码任务
    │
    同时...
    │
迭代 v1.1 (planning, Stage 2 架构设计)
    │  人类 + Leader AI 在讨论下一轮需求
    │
    同时...
    │
迭代 v1.2 (planning, Stage 0 需求分析)
    │  用户刚提了新需求
```

**Stage 3→4 门禁**：当 planning 迭代完成 Stage 3 要进入 Stage 4 时：
- 如果没有其他 active 迭代 → 正常进入 Stage 4，状态变为 active
- 如果有其他 active 迭代 → 阻止推进，提示等待前一个迭代完成

**自动激活**：当 active 迭代完成（completed）或终止（terminated）时，系统自动检查是否有 planning 迭代已完成 Stage 3，如果有则自动激活。

### 12.3 迭代继承

新迭代从 Stage 0 开始，但通过 `parent_iteration_id` 继承前一个迭代的成果：

| 阶段 | 首次迭代（慢） | 后续迭代（快） |
|------|--------------|---------------|
| Stage 0 需求分析 | 从零讨论 | AI 已知父迭代需求，只讨论增量 |
| Stage 1 需求规范 | 全新编写 | 基于父迭代文档修订 |
| Stage 2 架构设计 | 全新设计 | 基于父迭代架构调整 |
| Stage 3 技术方案 | 全新方案 | 大部分复用，只改变更部分 |
| Stage 4 任务分解 | 全部新建 | 只新建增量/变更任务 |
| Stage 5 编码 | 全部新写 | 大部分代码已有，只改增量 |

### 12.4 变更请求流程

用户在任何阶段都可以提出变更请求（不受当前阶段冻结限制）：

```
用户点击「提出变更」
    │
    ▼
描述变更内容
    │
    ▼
Leader AI 分析变更影响
    ├── 识别受影响的任务
    ├── 计算影响比例
    └── 给出建议（追加 / 终止并新建）
    │
    ▼
生成「变更影响分析报告」
    ├── 总任务数 / 已完成 / 进行中 / 受影响比例
    ├── 每个受影响任务的分析
    └── AI 建议
    │
    ▼
人类最终决策（三选一）
    │
    ├── 驳回变更 → 不做任何改变
    │
    ├── 在当前迭代追加
    │     ├── 受影响的未完成任务 → cancelled
    │     └── 新增替代任务到当前迭代
    │
    └── 终止并新建迭代
          ├── 当前迭代 → terminated
          ├── 未完成任务 → cancelled
          └── 新建迭代从 Stage 0 开始（继承当前迭代）
```

**AI 建议阈值**：
- 受影响 < 20%：建议追加
- 受影响 20%-50%：建议追加但提醒风险
- 受影响 > 50%：建议终止并新建

### 12.5 任务状态扩展

为支持变更请求，任务新增两种终态：

| 状态 | 含义 | 触发条件 |
|------|------|---------|
| `cancelled` | 已取消 | 迭代终止 或 变更请求中人工确认取消 |
| `superseded` | 已被取代 | 已完成的任务被新的变更任务替代 |

---

## 十三、关键设计原则

1. **架构师是质量守门人**：所有代码必须经过架构师审核才算完成，下游任务才能启动
2. **依赖驱动并行**：无依赖的任务自动并行，有依赖的严格串行
3. **自驱动流水线**：任务完成 → 解锁下游 → 自动分配 → 无需人工干预
4. **渐进式升级**：Sonnet 工程师 → Opus 工程师 → 人类，逐级升级模型处理难题（架构师不下场编码）
5. **全程可追溯**：每次审核、打回、升级都有日志和归档文档
6. **系统强制门控**：Stage 5 → 6 必须所有任务 done，不可跳过
7. **CI/CD 闭环**：Git push → CI 测试 → 结果回报 Dispatcher → 自动决策下一步
8. **规划并行，执行串行**：多个迭代可同时规划（Stage 0-3），但同一时间只有一个迭代在执行（Stage 4+）
9. **变更请求人机协作**：AI 分析影响范围，人类做最终决策
10. **迭代不并行执行**：每个迭代是完整的流水线，串行执行保证代码一致性
11. **全栈优先执行**：默认由全功能 Agent 端到端交付任务，角色标签用于治理与焦点，不做前后端能力割裂
12. **Skill Pack 单一事实源**：角色定义仅来自 `dispatcher/app/skill_packs/<pack>/roles/*.md`，不使用旧路径兜底
13. **能力全集、角色约束**：每个 OpenClaw Agent 默认全能力，角色通过 `forbidden/required_outputs/gate_rules` 进行治理
