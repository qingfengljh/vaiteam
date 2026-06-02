# 三件工程主线

与产品讨论对齐的三条**并行可规划、部分有依赖**的大线；实施时拆 issue，本文件只做**索引与顺序建议**。

---

## 当前工作中心（切换焦点后）

交付线（SaaS 安装、dispatcher 装机、install-agent 闭环）达到**可重复成功**后，日常研发重心建议切到 **主线 1**：**CC 作默认编码执行器** + **MQ 状态机不变** + **架构师签发 `task_context`**。路线、阶段与角色边界见 **`docs/74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md`**（与 `50-CLAUDE_CODE_WORKER.md`、`46-EXECUTION_CHANNEL_PROTOCOL.md` 一起读）。

---

## 1）Agent Worker 与 Dispatcher 如何交互（OpenClaw 半成品 → CC → 继续演进）

**现状**：已通过 **OpenClaw / install-agent / connector、MQ + Git** 等跑通**一版半成品**（派单、回传、客户机执行链部分能力）。

**目标**：在**同一套「Agent Worker」心智**下，把编码类执行收敛到 **Claude Code（CC）**（与同构任务包、状态机语义一致）；**CC 换桩完成后**，继续在统一模型上迭代（`human` / `connector` / **CC** 并列、鉴权与审计、任务包 schema）。

**必读入口**

- `docs/74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md` — CC + MQ + 架构师工作中心（总览）  
- `docs/46-EXECUTION_CHANNEL_PROTOCOL.md` — Git / MQ / 完成语义  
- `docs/50-CLAUDE_CODE_WORKER.md` — CC 作 Worker 的边界与分期  
- `dispatcher/app/services/scheduler.py` — 派单与载荷  
- `saas/install-agent/`、`deploy/` — 客户机与部署形态  

---

## 2）SaaS：把 Dispatcher 安装到用户或平台准备的环境

**目标**：在 **SaaS** 产品流程里，完成将 **dispatcher（及依赖：DB、Redis、可选 web）** 安装到**租户自备环境**或**平台托管环境**，并与 Portal、APISIX、`meta.upstream_*`、FRP 等**对齐联调**（安装、升级、可观测）。

**必读入口**

- `saas/README.md`  
- **`saas/docs/SAAS_DOMAIN_TLS_ACME_LOOP.md`**（域名迁移 + Let's Encrypt + APISIX 续期）  
- **`saas/docs/INSTALL_AGENT_CLOSED_LOOP.md`**（install-agent 闭环验收）  
- 根目录 `package.sh`（交付包形态）  
- `saas/install-agent/README.md`、`saas/docs/`（如 FRP 穿透）  

---

## 3）原型工坊：专用快速原型的 Agent Worker

**目标**：**不另起执行哲学**——在 1）的 **Agent Worker / 任务包 / 回传** 之上，增加**角色特化**的一支：**独立 Docker 内的 CC**，消费**阶段文档 + 项目知识**，产出 **mock 可运行前端**，供最终用户与 Owner 评需求；实现落在 **`prototype-workshop/`**。

**必读入口**

- `prototype-workshop/README.md`  
- `docs/50-CLAUDE_CODE_WORKER.md`（与 CC Worker 同谱系说明）  

---

## 建议顺序

| 关系 | 说明 |
|------|------|
| **1 与 2** | 可按人力**并行**；2 偏交付与运维，1 偏协议与执行器。 |
| **3 依赖 1** | 至少等 **任务包字段、回传通道、executor/任务类型** 的 P0 共识稳定后再全力开 3，避免原型域与编码域各写一套协议。 |
