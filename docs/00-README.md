# AI Dev Team 文档索引

## 阅读顺序

```
00-README.md              本文件
01-VISION.md              项目愿景和核心原则
02-ARCHITECTURE.md        系统架构（两层架构、技术选型）
03-ROLES.md               角色定义与分工（Leader + 工程师团队）
04-WORKFLOW.md            工作流程、阶段门控、输入输出物定义
05-TASK_DESIGN.md         任务分解与调度策略
06-COMMUNICATION.md       通信协议与汇报机制
07-KNOWLEDGE_SYSTEM.md    知识库设计（文档 + RAG + 知识图谱）
08-DEPLOYMENT.md          部署方案（Docker Compose）
09-BOOTSTRAP.md           自举路径
10-ITERATION_GIT_TRACKING.md  迭代 + Git 协作 + 测试验证 数据模型设计
11-TESTING_STRATEGY.md    测试策略
15-IDE_PLUGIN.md         ★ Cursor 插件设计（OpenClaw Bridge，含 SaaS 模式）
16-TOKEN_COST_ANALYSIS.md Token 成本分析
17-LEGACY_REWRITE.md      旧系统重写流程
18-TASK_EXECUTION_FLOW.md ★ 任务推进流程与 Git 多人协作（最新完整版）
19-USER_MANUAL.md         ★ 使用手册（管理员 + 用户，含截图）
20-DEMO_WALKTHROUGH.md    ★ 操作演示（完整项目流程截图教程）
21-PUBLISH_NETWORK.md     ★ 发布网络配置（n2n + APISIX 公网映射）
22-SAAS_ARCHITECTURE.md   ★ SaaS 架构设计（多租户、计费、网络拓扑、演进路径）
23-CONTEXT_MANAGEMENT.md  ★ AI 对话上下文管理（token 计数、自动摘要、窗口适配）
27-MODULE_TEAM_DESIGN.md  ★ 模块小组设计（大项目分组开发、模块隔离、依赖解除）
28-TOKEN_OPTIMIZATION.md  ★ Token 消耗优化（裁剪、去重、缓存、降级路线图）
30-TASK_DECOMPOSE_TO_EXECUTION.md  任务分解到执行阶段（固定路径、指令粒度、MQ 目标）
31-AI_DOCUMENT_SEARCH_FLOW.md      AI 文档搜索流程（五步：意图→关键字→粗排→本地重排→精读 Top M）
32-LEADER_ARCHITECT_GOVERNANCE.md  Leader/架构师治理规则（边界、模型策略、blocked 责任）
74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md ★ CC 默认执行器、MQ 协作脊柱、架构师工作中心（路线与阶段）
43-HUMAN_GUIDED_COLLAB_AND_REVIEW_POLICY.md 人类主导协作与无人值守审核策略（过程文档）
44-PROJECT_REVIEW_SUMMARY_MVP.md 项目复盘报告 MVP（过程文档）
45-ENV_SETUP_COLLAB_PROTOCOL.md 团队独立环境协作协议（过程文档）
46-EXECUTION_CHANNEL_PROTOCOL.md 执行通道协议（MQ + Git，过程文档）
47-RAG_AND_EXPERIENCE_GOVERNANCE_DIRECTION.md 项目型RAG与经验库治理方向（执行参考）
49-RUNTIME_IMAGE_AND_HARBOR_UPGRADE_PLAN.md ★ 执行环境镜像化升级方案（Harbor + 预构建 + 团队级选择）
50-DESIGN_FIRST_WORKFLOW_AND_PROTOTYPE_UPGRADE.md ★ 设计先行工作流与原型规范升级方案
51-INDUSTRIAL_PLC_CROSS_BRAND_TRANSLATION_PLAN.md ★ 工业自动化 PLC 跨品牌翻译平台落地方案
52-LAUNCH_AND_GROWTH_PLAN.md       ★ 产品上线与增长计划（开源/官网/引流/交付/国际化）
53-SOFTWARE_COPYRIGHT_APPLICATION_CHECKLIST.md ★ 软著申报材料清单（AI 协同开发版）
54-AI_COLLAB_IP_EVIDENCE_SOP.md    ★ AI 参与开发的软著与证据留存 SOP
55-PROJECT_CHECKPOINT_BACKUP_GUIDE.md ★ 单项目阶段备份与回归录屏指南
56-SALES_AND_MARKETING_AI_HANDOFF.md ★ 销售体系与营销建设 AI 会话交接文档
57-SAAS_PORTAL_IMPLEMENTATION.md ★ SaaS Portal 实施方案（控制面实现、方案C落地、Phase 分阶段）
58-DOMAIN_MIGRATION_CHECKLIST.md ★ 域名迁移检查清单（历史子域 → vaiteam.cn，含 301 与回滚）
59-AGENT_PRIVATE_DEPLOY_AND_OFFLINE_GRACE.md ★ Agent 私有化部署与断连降级策略（方案设计，暂未实现）
60-EXPERIENCE_PACK_GLOBAL_SHARING.md ★ 经验包全局共享体系（三层架构 + 合规 + 定价杠杆，暂未实现）
61-PORTAL_SECURITY_HARDENING.md ★ Portal 安全加固清单（验证码 + 暴力破解防护 + API 限流，待实现）
67-OPERATIONS_COMPLIANCE_CHECKLIST.md ★ 番茄公司运营 VAI TEAM 的合规清单（起步版）
```

## 核心理念

- Leader AI（编排系统）= 技术经理 + 产品经理，统筹全局
- Agent Worker = 有自主思考能力的执行单元；**编码类默认由 Claude Code（CC）** 接地执行，过渡期保留 **connector / install-agent** 链（见 `74-*`、`50-*`）
- 每个阶段有明确的输入物和输出物，上一阶段的输出是下一阶段的输入
- 文档即 Markdown 文件，无需额外管理系统
- RAG 知识库基于 ljsafe-ai-server 改造
- 知识图谱用 codebase-memory-mcp

## 相关资源

- ljsafe-ai-server RAG 实现：`/app/ai/ljsafe-ai-server/`
- 旧项目设计文档（仅参考）：`../../docs/`

---

## 文档实现进度看板（需持续维护）

状态定义：

- `已完成`：文档对应代码已落地并通过基本验证
- `部分完成`：已实现核心能力，仍有待补项
- `未开始`：仅文档方案，尚未进入编码

> 维护约定：每次合并涉及对应文档能力的代码后，同步更新本表状态与“下一步”。

| 文档 | 主题 | 当前状态 | 代码落地点（示例） | 下一步 |
|---|---|---|---|---|
| `18-TASK_EXECUTION_FLOW.md` | 任务执行与 Git 协作 | 部分完成 | `dispatcher/app/services/scheduler.py`、`dispatcher/app/routers/tasks.py` | 持续补充边界场景回归（重试、升级、人工接管） |
| `19-USER_MANUAL.md` | 用户操作手册 | 部分完成 | 多处功能已落地（项目状态控制、审核门禁、恢复日志等） | 保持与真实 UI/接口同步更新 |
| `32-LEADER_ARCHITECT_GOVERNANCE.md` | Leader/Architect 治理规则 | 部分完成 | `scheduler.py`、`heartbeat.py`、`tasks.py` | 补充新治理点（导出/导入闭环、权限边界） |
| `74-VIRTUAL_TEAM_CC_MQ_AND_ARCHITECT_WORK_CENTER.md` | CC 执行器 + MQ + 架构师工作中心 | 部分完成 | `dispatcher/app/execution_hints.py`、`scheduler.assign_task` 载荷；`dispatcher/tools/cc-worker/` | P1 CC wrapper 与 CI 常态化；P2 人机插件；与 46 号完成语义对齐 |
| `35-DOMAIN_MODEL_DISTILLATION_PLAN.md` | 领域模型蒸馏方案 | 未开始 | 无（方案文档） | 后续立项后拆分数据/训练/评估实施任务 |
| `36-CURSOR_FAKE_PLUGIN_OPENAI_GATEWAY_SPEC.md` | Cursor 假插件网关 | 未开始 | 无（协议草案） | 先做网关 PoC（路由+指令拦截+兼容回包） |
| `37-CURSOR_FAKE_PLUGIN_OPERATOR_GUIDE.md` | 假插件操作手册 | 未开始 | 无（操作规范） | 配合网关 PoC 后验证命令闭环 |
| `38-EXPERIENCE_PACKAGE_MARKET_V1_SPEC.md` | 经验包市场 v1 | 未开始 | 无（数据模型/API草案） | 进入需求排期后按表结构和接口分阶段实现 |
| `39-PROJECT_EXPORT_IMPORT_API_SPEC.md` | 导出/导入 API 规范 | 未开始 | 无（接口规范） | 与 40/41 联动立项实现 |
| `40-PROJECT_IMPORT_ROLLBACK_STRATEGY.md` | 导入失败回滚策略 | 未开始 | 无（策略规范） | 导入实现时同步落地 import_jobs 与重试链路 |
| `41-EXPORT_IMPORT_IMPLEMENTATION_TASK_BREAKDOWN.md` | 导出/导入任务拆解 | 未开始 | 无（实施清单） | 进入研发排期，按里程碑 M1/M2/M3 执行 |
| `43-HUMAN_GUIDED_COLLAB_AND_REVIEW_POLICY.md` | 人类主导协作与审核治理 | 已完成 | `dispatcher/app/services/scheduler.py`、`dispatcher/app/routers/tasks.py`、`web/src/views/project/TaskBoard.vue`、`web/src/views/project/AgentPanel.vue` | 下一步补“结构化协作会话 + 时间线审计” |
| `44-PROJECT_REVIEW_SUMMARY_MVP.md` | 项目复盘报告生成 | 已完成 | `dispatcher/app/services/project_review.py`、`dispatcher/app/routers/projects.py`、`web/src/views/project/Overview.vue`、`web/src/api/projects.ts` | 增加角色视角过滤与导出对比能力 |
| `45-ENV_SETUP_COLLAB_PROTOCOL.md` | 团队独立环境协作（架构师指导） | 已完成 | `dispatcher/app/services/scheduler.py`、`dispatcher/app/routers/agents.py` | 前端增加“环境协作状态与补装记录”可视化 |
| `46-EXECUTION_CHANNEL_PROTOCOL.md` | 执行链路协议（MQ + Git） | 已完成 | `deploy/connector/connector.mjs`、`dispatcher/app/routers/webhook.py` | 通道健康：`docs/EXECUTION_MQ_METRICS.md` + `dispatcher/scripts/mq_stream_snapshot.sh`；可选 Prometheus 后补 |
| `47-RAG_AND_EXPERIENCE_GOVERNANCE_DIRECTION.md` | 资料治理与经验库方向 | 已完成 | `docs/47-RAG_AND_EXPERIENCE_GOVERNANCE_DIRECTION.md` | 按 Phase 1 落地资料台账、生命周期与经验发布闭环 |
| `49-RUNTIME_IMAGE_AND_HARBOR_UPGRADE_PLAN.md` | 执行环境镜像化（Harbor + 预构建） | 未开始 | 无（升级方案） | V1：预置镜像 + Harbor 部署 + 项目级选择（预计 2-3 天） |
| `50-DESIGN_FIRST_WORKFLOW_AND_PROTOTYPE_UPGRADE.md` | 设计先行工作流与原型规范升级 | 部分完成 | `dispatcher/app/routers/tasks.py`、`dispatcher/app/services/scheduler.py`、`dispatcher/app/routers/conversations.py`、`web/src/views/project/TaskBoard.vue`、`web/src/utils/markdown.ts` | 补齐 prototype_spec 自动生成/入库与文档中心类型聚合展示 |
| `51-INDUSTRIAL_PLC_CROSS_BRAND_TRANSLATION_PLAN.md` | 工业自动化 PLC 跨品牌翻译平台 | 未开始 | 无（落地方案） | Phase 0：确定品牌+收集样本 → Phase 1：IR引擎+适配器开发 |
| `52-LAUNCH_AND_GROWTH_PLAN.md` | 产品上线与增长计划 | 未开始 | 无（规划方案） | 第一周：密钥清理 + License + README + Quick Start |
| `53-SOFTWARE_COPYRIGHT_APPLICATION_CHECKLIST.md` | 软著申报材料清单（AI 协同开发版） | 已完成 | `docs/53-SOFTWARE_COPYRIGHT_APPLICATION_CHECKLIST.md` | 按清单组装 V1.0 首次申报包并完成版本冻结 |
| `54-AI_COLLAB_IP_EVIDENCE_SOP.md` | AI 参与开发证据留存 SOP | 已完成 | `docs/54-AI_COLLAB_IP_EVIDENCE_SOP.md` | 建立 `evidence/` 目录并按发布版本执行留痕 |
| `55-PROJECT_CHECKPOINT_BACKUP_GUIDE.md` | 单项目阶段备份与回归录屏 | 已完成 | `scripts/checkpoint_create.sh`、`scripts/checkpoint_restore.sh`、`checkpoints/` | 按黄金案例建立 S0-S5 checkpoint 并每周抽检恢复 |
| `56-SALES_AND_MARKETING_AI_HANDOFF.md` | 销售体系与营销会话交接 | 已完成 | `docs/56-SALES_AND_MARKETING_AI_HANDOFF.md` | 启动独立 AI 会话按 P0/P1 清单产出销售与增长文档 |
| `57-SAAS_PORTAL_IMPLEMENTATION.md` | SaaS Portal 控制台实施方案 | 部分完成 | `saas/portal-api/`、`saas/portal-web/`、`saas/deploy/` | Phase 2 基础版落地：手动实例创建 + APISIX 路由 + 计费 |
