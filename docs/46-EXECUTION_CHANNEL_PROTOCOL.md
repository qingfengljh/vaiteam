# 执行通道协议（MQ + Git）落地说明

## 目标

把“代码走 Git、任务走 MQ”从约定升级为执行约束，避免出现：

- 代码未推送却被标记任务完成
- 任务状态绕过消息队列，导致链路不可观测

## 用户数据资产与 Dispatcher 故障

- **代码真源在 Git**：已 **commit 且 push 成功** 到团队可见远程的内容，以 **Git 托管侧** 为准；Dispatcher / MQ 承载的是**编排、门控与观测**，不是代码的唯一存储。
- **Dispatcher 重大故障时**：只要远程仓库与本地克隆仍可用，**已落地的实现**仍可从分支与提交历史恢复、对比与继续开发；因此必须坚持 **无 push 则任务不得成功收口**（见下文「代码协作通道」），避免「编排库坏了、代码从未离开本机」。
- **原型等非主仓路径的产出**：同样应进入 **可 `git add` 的路径**（如 `prototype-workshop/artifacts/`），使资产与正式编码一样可被远程与备份策略覆盖；详见该目录说明。

## 约束规则

1. **任务状态通道**
   - Connector 回传结果优先走 `via-mq` 端点
   - Dispatcher 统一从 `task:callback` 消费并驱动状态机

2. **代码协作通道**
   - 任务存在 `git_branch` 时，必须可用 Git 仓库
   - 任务不存在 `git_branch` 时，Connector 直接失败（禁止无分支执行）
   - 分支切换后会再次校验当前分支，必须与任务分支一致
   - 执行成功后必须 `commit + push`
   - `push` 失败时，任务按失败态回传，不允许成功收口

3. **执行进度**
   - Connector 在执行开始、执行结束时通过 MQ 回传 `task_update`
   - 调度层统一处理为 `executing/reviewing` 过程状态

## 本次实现

- `deploy/connector/connector.mjs`
  - `reportResult` 默认走 `/api/webhook/via-mq/*`
  - `ensureGitRepoReady(required)`：有分支但无仓库配置直接失败
  - `gitCheckoutBranch(branch, baseBranch)`：按 `git_base_branch` 建立/切换工作分支并二次校验
  - `gitCommitAndPush`：提交前校验“当前分支 == 任务分支”
  - `git push` 失败强制转任务失败
  - 新增 `reportTaskUpdate`，执行过程走 MQ 回传

- `dispatcher/app/routers/webhook.py`
  - 新增 `/api/webhook/via-mq/task-update`

- `dispatcher/app/services/scheduler.py`
  - 派单 metadata 与 inbox 消息新增 `git_base_branch` 透传
  - **P0 扩展**：`assign_task` inbox 载荷与 MQ `metadata` 增加 **`executor_hint`**（`connector` \| `claude_code` \| **`prototype_cc`** \| **`stub`**，默认 `connector`）、**`actor_type`**（含 **`prototype_cc`**、**`stub`** 等，派单时按任务上下文与认领 Agent 解析）。任务对象 API 同名字段由 `task.context` 派生，旧客户端不传则行为与旧版一致。

## 验收建议

- 构造一个有 `git_branch` 的任务，临时断开远端 Git 推送权限，确认任务最终为 failed
- 查看 `task:callback` 中存在 `task_update/task_complete/task_failed` 事件
- 验证任务状态推进链路由 MQ worker 统一处理
