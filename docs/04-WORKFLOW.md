# 工作流程、阶段门控与输入输出物

## 核心原则

每个阶段有明确的输入物和输出物。
上一阶段的输出物是下一阶段的输入物。
没有通过门控审核的输出物不能流入下一阶段。

## 阶段总览

```
Stage 0        Stage 1        Stage 2        Stage 3        Stage 4        Stage 5        Stage 6        Stage 7
业务方案  ───→  需求分析  ───→  产品原型  ───→  技术方案  ───→  任务分解  ───→  代码实现  ───→  测试验证  ───→  部署交付
  │              │              │              │              │              │              │              │
  ▼              ▼              ▼              ▼              ▼              ▼              ▼              ▼
业务方案文档   需求规范文档   原型设计文档   技术方案文档   结构化任务清单   源代码+PR      测试报告       部署完成
```

---

## Stage 0：业务方案

**主导者**：你 + Leader
**AI 自动化**：10%

| | 内容 |
|---|---|
| **输入** | 你的想法、灵感、问题描述 |
| **过程** | Leader 帮你梳理为结构化方案 |
| **输出** | `00-business-plan.md` |
| **门控** | 你审批 |

**输出物内容**：
```
00-business-plan.md
├── 项目背景和目标
├── 核心功能列表（优先级排序）
├── 目标用户
├── 成功标准
└── 约束条件（时间、预算、技术限制）
```

---

## Stage 1：需求分析

**主导者**：Leader
**AI 自动化**：30%

| | 内容 |
|---|---|
| **输入** | `00-business-plan.md` |
| **过程** | Leader 将业务方案细化为需求规范 |
| **输出** | `01-requirements.md` |
| **门控** | Leader 审核 + 你可选审核 |

**输出物内容**：
```
01-requirements.md
├── 功能需求列表（编号、描述、优先级）
├── 用户故事（As a... I want... So that...）
├── 非功能需求（性能、安全、可用性）
├── 数据需求（需要什么数据、数据来源）
├── 接口需求（与外部系统的交互）
└── 验收标准（每个功能的验收条件）
```

---

## Stage 2：产品原型

**主导者**：Leader
**AI 自动化**：40%

| | 内容 |
|---|---|
| **输入** | `00-business-plan.md` + `01-requirements.md` |
| **过程** | Leader 生成产品原型设计 |
| **输出** | `02-prototype.md` |
| **门控** | Leader 审核 + 你可选审核 |

**输出物内容**：
```
02-prototype.md
├── 页面清单（每个页面的用途）
├── 页面布局（ASCII 或 Mermaid 描述）
├── 页面流程（用户操作路径）
├── 交互逻辑（按钮点击后发生什么）
├── API 接口草案（前后端交互点）
└── 数据展示（每个页面展示什么数据）
```

---

## Stage 3：技术方案

**主导者**：Leader
**AI 自动化**：50%

| | 内容 |
|---|---|
| **输入** | `00-business-plan.md` + `01-requirements.md` + `02-prototype.md` |
| **过程** | Leader 生成技术方案 |
| **输出** | `03-technical-design.md` |
| **门控** | 你审批（架构决策必须人工确认） |

**输出物内容**：
```
03-technical-design.md
├── 技术栈选择（语言、框架、数据库、中间件）
├── 系统架构图（模块划分、层次结构）
├── 数据库设计（表结构、ER 图、索引策略）
├── API 详细设计（每个接口的 URL、方法、参数、返回值）
├── 目录结构（项目文件组织）
├── 第三方依赖（需要引入的库和服务）
├── 安全设计（认证、授权、数据保护）
└── 架构决策记录（ADR：为什么选 A 不选 B）
```

---

## Stage 4：任务分解

**主导者**：Leader
**AI 自动化**：70%

| | 内容 |
|---|---|
| **输入** | `01-requirements.md` + `02-prototype.md` + `03-technical-design.md` + 知识库上下文 |
| **过程** | Leader 将技术方案分解为可执行任务 |
| **输出** | `04-task-breakdown.json` + `04-task-breakdown.md`（可读版） |
| **门控** | 你审批（确认分解合理性） |

**输出物内容**：
```json
{
  "project": "项目名",
  "total_tasks": 25,
  "estimated_hours": 18,
  "cost_estimate": {"sonnet": "$X", "opus": "$Y", "total": "$Z"},
  "tasks": [
    {
      "id": "T001",
      "title": "创建 User 数据模型",
      "description": "根据 03-technical-design.md 中的数据库设计...",
      "type": "feature",
      "priority": 1,
      "suggested_role": "backend",
      "suggested_model": "sonnet",
      "estimated_hours": 0.5,
      "dependencies": [],
      "input_files": ["03-technical-design.md#数据库设计"],
      "output_files": ["src/models/user.py", "migrations/001_create_users.sql"],
      "acceptance_criteria": [
        "User 模型包含所有必需字段",
        "迁移脚本可正确执行",
        "包含字段验证"
      ]
    }
  ]
}
```

**关键点**：
- 每个任务引用它依赖的设计文档章节（`input_files`）
- 每个任务明确产出文件（`output_files`）
- 每个任务有验收标准（`acceptance_criteria`）
- 粒度 0.3-1 小时

---

## Stage 5：代码实现

**主导者**：工程师团队（OpenClaw）
**AI 自动化**：90%

| | 内容 |
|---|---|
| **输入** | `04-task-breakdown.json` + 各任务引用的设计文档 + 知识库上下文（RAG 检索） |
| **过程** | Leader 分发任务，工程师编码，Leader 审查 |
| **输出** | 源代码 + Git commits + PR |
| **门控** | Leader 代码审查 + 你可选审核 |

**工程师接到的任务指令包含**：
```
1. 任务描述和目标
2. 相关设计文档片段（从 input_files 提取）
3. 相关代码上下文（RAG 检索结果）
4. 编码规范（从知识库读取）
5. 验收标准
6. 产出文件列表
```

**流程**：
```
Leader 分发任务（含完整上下文）
    ↓
工程师在 feature 分支编码
    ↓
工程师自检（对照验收标准）
    ↓
git push + 创建 PR
    ↓
Leader 代码审查
    ├── 通过 → 合并到 dev
    └── 不通过 → 反馈修改意见 → 工程师修改 → 重新审查
```

---

## Stage 6：测试验证

**主导者**：测试工程师（OpenClaw tester）
**AI 自动化**：95%

| | 内容 |
|---|---|
| **输入** | 源代码 + `01-requirements.md`（验收标准）+ `03-technical-design.md`（API 定义） |
| **过程** | 测试工程师编写并执行测试 |
| **输出** | `06-test-report.md` + 测试代码 |
| **门控** | Leader 审核测试报告 |

**输出物内容**：
```
06-test-report.md
├── 测试概要（通过/失败/跳过数量）
├── 覆盖率报告
├── 失败用例详情
├── Bug 列表（关联到任务 ID）
└── 建议（需要修复的问题）
```

**发现 Bug 时**：
```
测试工程师发现 Bug
    ↓
创建 Bug 任务（关联原始任务）
    ↓
Leader 分配给对应工程师修复
    ↓
修复后重新测试
```

---

## Stage 7：部署交付

**主导者**：运维工程师（OpenClaw devops）
**AI 自动化**：80%

| | 内容 |
|---|---|
| **输入** | 源代码 + `03-technical-design.md`（部署架构）+ `06-test-report.md`（测试通过） |
| **过程** | 运维工程师编写部署配置并执行 |
| **输出** | `07-deployment.md` + 部署配置文件 + 部署完成 |
| **门控** | 你验收 |

**输出物内容**：
```
07-deployment.md
├── 部署环境信息
├── 部署步骤
├── 验证结果
├── 访问地址
└── 回滚方案
```

---

## 阶段间数据流总图

```
你的想法
    │
    ▼
┌─ Stage 0 ─┐
│ 业务方案    │──→ 00-business-plan.md
└────────────┘         │
                       ▼
              ┌─ Stage 1 ─┐
              │ 需求分析    │──→ 01-requirements.md
              └────────────┘         │
                                     ▼
                            ┌─ Stage 2 ─┐
                            │ 产品原型    │──→ 02-prototype.md
                            └────────────┘         │
                                                   ▼
                                          ┌─ Stage 3 ─┐
                                          │ 技术方案    │──→ 03-technical-design.md
                                          └────────────┘         │
                                                                 ▼
                                                        ┌─ Stage 4 ─┐
                                                        │ 任务分解    │──→ 04-task-breakdown.json
                                                        └────────────┘         │
                                                                               ▼
                                                                      ┌─ Stage 5 ─┐
                                                                      │ 代码实现    │──→ 源代码 + PR
                                                                      └────────────┘         │
                                                                                             ▼
                                                                                    ┌─ Stage 6 ─┐
                                                                                    │ 测试验证    │──→ 06-test-report.md
                                                                                    └────────────┘         │
                                                                                                           ▼
                                                                                                  ┌─ Stage 7 ─┐
                                                                                                  │ 部署交付    │──→ 07-deployment.md
                                                                                                  └────────────┘

注意：每个阶段的输入不仅是上一阶段的输出，还包括之前所有阶段的累积输出。
例如 Stage 4 的输入是 Stage 1 + 2 + 3 的输出，不仅仅是 Stage 3。
```

## 门控审核层次

```
Level 1: 自动化检查（100%）
  - 输出物格式校验（JSON Schema、Markdown 结构）
  - 代码编译/lint 通过
  - 单元测试通过

Level 2: Leader AI 审核（100%）
  - 输出物内容质量
  - 与输入物的一致性
  - 代码审查

Level 3: 人工审核
  - Stage 0（业务方案）：必须
  - Stage 3（技术方案）：必须
  - Stage 4（任务分解）：必须
  - 其他阶段：可选
```

## 输出物存储约定

所有阶段输出物存储在项目目录中：

```
projects/{project-name}/
├── docs/
│   ├── 00-business-plan.md
│   ├── 01-requirements.md
│   ├── 02-prototype.md
│   ├── 03-technical-design.md
│   ├── 04-task-breakdown.json
│   ├── 04-task-breakdown.md
│   ├── 06-test-report.md
│   └── 07-deployment.md
├── src/                          # Stage 5 产出
├── tests/                        # Stage 6 产出
├── deploy/                       # Stage 7 产出
└── knowledge/                    # 项目知识库
    ├── architecture.md
    ├── api-contracts.md
    └── known-issues.md
```
