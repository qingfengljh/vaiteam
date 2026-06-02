# 角色定义与分工

## 组织架构

```
你（老板/技术总监）
  └── Leader AI（技术经理 + 产品经理）── DeepSeek
        ├── 架构师 (OpenClaw architect) ── Claude Opus
        ├── 后端工程师 (OpenClaw backend) ── Claude Sonnet
        ├── 前端工程师 (OpenClaw frontend) ── Claude Sonnet
        ├── 测试工程师 (OpenClaw tester) ── Claude Sonnet
        └── 运维工程师 (OpenClaw devops) ── Claude Sonnet
```

## 模型选择策略

| 角色 | 模型 | 理由 | 成本 |
|------|------|------|------|
| Leader | DeepSeek | 不需要编码能力，强在需求理解、中文处理、结构化输出 | ~¥1/百万token |
| 架构师 | Claude Opus | 需要最强的代码理解和设计能力 | $15/$75 每百万token |
| 工程师 | Claude Sonnet | 编码能力强，性价比高 | $3/$15 每百万token |

## Leader AI

**身份**：技术经理 + 产品经理
**运行环境**：编排系统内置，调用 DeepSeek API
**模型**：DeepSeek Chat

**核心能力**：
- 需求理解和结构化表达（DeepSeek 中文能力极强）
- JSON 格式输出（任务分解、评审结果）
- 文档生成（业务方案、需求规范、技术方案）

**职责**：
- 需求分析：理解你的意图，梳理为结构化需求
- 方案设计：技术选型、架构设计、API 设计
- 任务分解：将方案拆为 0.3-1h 粒度的可执行任务
- 任务分配：根据任务类型分配给合适的工程师
- 代码审查：审查工程师提交的代码，给出反馈
- 进度管理：跟踪整体进度，向你汇报
- 技术决策：工程师遇到分歧时做出裁决

**不做的事**：
- 不直接写代码（交给架构师和工程师）
- 不做低层级的技术细节决策（交给架构师）

## 架构师

**Agent ID**：`architect`
**运行环境**：OpenClaw Docker 实例
**模型**：Claude Opus（需要最强的代码理解能力）

**职责**：
- 项目骨架搭建（目录结构、基础配置）
- 核心接口定义（API 契约、数据模型）
- 复杂模块设计与实现
- 代码审查（Leader 审查宏观质量，架构师审查技术细节）
- 技术难题攻关
- 架构决策和技术选型的具体落地

**与 Leader 的分工**：
- Leader 说"用 PostgreSQL"，架构师决定"用 asyncpg + SQLAlchemy 2.0"
- Leader 说"需要缓存"，架构师决定"用 Redis + 什么缓存策略"
- Leader 审查"功能是否完整"，架构师审查"代码是否优雅"

## 后端工程师

**Agent ID**：`backend`
**运行环境**：OpenClaw Docker 实例
**模型**：Claude Sonnet

**职责**：
- API 接口实现
- 数据库设计与操作
- 业务逻辑编码
- 后端单元测试

**工作方式**：
- 在独立工作空间中编码
- 通过 Git 分支提交代码
- 完成后向 Leader 汇报

## 前端工程师

**Agent ID**：`frontend`
**运行环境**：OpenClaw Docker 实例
**模型**：Claude Sonnet

**职责**：
- 页面和组件开发
- 与后端 API 对接
- 样式和交互实现
- 前端单元测试

## 测试工程师

**Agent ID**：`tester`
**运行环境**：OpenClaw Docker 实例
**模型**：Claude Sonnet

**职责**：
- 编写测试用例（单元测试、集成测试）
- 执行测试并生成报告
- 发现 Bug 后创建 Bug 任务
- 回归测试

## 运维工程师

**Agent ID**：`devops`
**运行环境**：OpenClaw Docker 实例
**模型**：Claude Sonnet

**职责**：
- Dockerfile 和 docker-compose 编写
- CI/CD 配置
- 部署脚本
- 监控配置

## 角色扩展

角色数量不固定，按需增减：
- 简单项目：Leader + 1 个后端即可
- 标准项目：Leader + 架构师 + 后端 + 前端 + 测试
- 复杂项目：可增加数据库工程师、安全工程师等

## 升级机制

工程师遇到无法解决的问题时：
1. 最多自主尝试 3 次
2. 3 次失败后生成升级报告
3. 报告提交给 Leader
4. Leader 判断是否能解决，否则升级给你
5. 升级报告包含：问题描述、错误日志、尝试记录、建议方向
