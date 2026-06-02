# 全局知识入口（项目级）

> 目的：把影响全体工程师的规则固化在一个固定入口，避免重复口头传递。

## 必读规则

- 所有任务使用独立分支，禁止直接在主分支上开发
- 常规开发分支：从 `develop` 检出并回合 `develop`
- Bug/Hotfix 分支：从 `main` 检出 `fix/*`，审核通过后回合 `main`
- 架构师负责技术审核与关键决策，Leader 不做技术评审
- Dispatcher 负责 Agent 心跳治理与失联恢复

## 全局文档引用

- [Leader 与 Architect 治理规则](32-LEADER_ARCHITECT_GOVERNANCE.md)
- [任务执行流与 Git 协作](18-TASK_EXECUTION_FLOW.md)
- [迭代 Git 跟踪与发布流](10-ITERATION_GIT_TRACKING.md)
- [用户手册（团队与任务）](19-USER_MANUAL.md)

## 变更通知要求

- 任何影响全局的规则变更，先更新本入口文档，再更新被引用文档
- 合并后必须发送一次“全局变更通知”到团队
