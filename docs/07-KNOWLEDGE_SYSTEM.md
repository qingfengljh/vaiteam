# 知识库与知识图谱设计

## 为什么需要知识系统

AI 编程团队面临的核心问题：
- 每个 OpenClaw 实例的上下文窗口有限，不可能把整个项目塞进去
- 工程师需要了解编码规范、架构决策、API 契约等项目知识
- 跨项目的经验（踩过的坑、最佳实践）需要积累和复用（实测：30 分钟 → 5 分钟）
- Leader 做任务分解时需要理解代码库结构和依赖关系
- **所有 AI 对话场景（阶段聊天、文档讨论、文档生成、AI 审核）都需要项目级的基础知识**

## 知识管理三层架构（运行时注入）

除了存储层的四层架构外，系统在运行时按三个层次向 AI 注入知识：

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: 跨租户经验池（未来 SaaS）                           │
│  - 高质量脱敏经验自动共享                                     │
│  - AI 检索不区分租户，人类查看仅限本租户                       │
│  → 状态：设计完成，待 SaaS 多租户架构确定后实现                │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: 全局经验库（跨项目）                                │
│  - Experience 表：踩坑记录、最佳实践、代码模板                 │
│  - 三层检索：tsvector 粗筛 → JSONB 匹配 → LIKE 兜底           │
│  - 阶段聊天按需注入、Agent 编程任务注入                       │
│  → 状态：✅ 已实现                                           │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: 项目级知识上下文（project_context.py）              │
│  - 项目元数据（名称/类型/Git仓库/技术栈/重写原因）            │
│  - 代码分析摘要（ProjectAsset.summary）                      │
│  - 已审核文档摘要（Document.status=approved）                │
│  → 状态：✅ 已实现，统一注入所有 AI 调用                      │
└─────────────────────────────────────────────────────────────┘
```

### Layer 1: 项目级知识上下文

**核心问题**：文档讨论的 AI 不知道代码在哪里、项目用什么技术栈、已有哪些分析结论——因为这些信息没有注入到 system prompt。

**解决方案**：统一的项目知识上下文服务 `dispatcher/app/services/project_context.py`

```python
async def get_project_context(
    session, project_id,
    include_assets=True,        # 代码分析摘要
    include_approved_docs=False  # 已审核文档摘要
) -> str
```

构建的上下文包含：

| 信息类型 | 来源 | 示例 |
|----------|------|------|
| 项目名称 | Project.name | "池州大气监测系统" |
| 项目类型 | Project.project_type | 维护迭代 / 新项目 / 旧系统重写 |
| Git 仓库 | Project.git_repo | git@xxx/repo.git |
| 技术栈 | Project.target_tech_stack | Vue 3 + FastAPI + TDEngine |
| 重写原因 | Project.rewrite_reason | 性能瓶颈、技术债务 |
| 代码分析 | ProjectAsset.summary | 目录结构、核心数据流、改进建议 |
| 已审核文档 | Document(approved) | 各阶段关键交付物摘要 |

**注入点**（所有 AI 调用均可按需获取）：

| AI 调用场景 | 注入方式 | 实现状态 |
|-------------|----------|----------|
| 阶段聊天 | STAGE_CHAT_SYSTEM 的 {asset_context} | ✅ 已实现 |
| 文档讨论 | DOC_CHAT_SYSTEM 的 {project_context} | ✅ 已实现 |
| 文档应用修改 | DOC_APPLY_SYSTEM 的 {project_context} | ✅ 已实现 |
| 文档生成 | STAGE_DOC_SYSTEM 的 {asset_context} | ✅ 已实现 |
| AI 审核 | REVIEW_SYSTEM 的上下文 | 待接入 |
| Agent 编程 | role_context 的 experience_context | ✅ 已实现 |

### Layer 2: 全局经验库注入

**核心问题**：经验库已有完整的 CRUD + 三层检索 + 自动提取机制，但仅在 Agent 编程任务中使用，阶段聊天和文档讨论中完全没有注入。

**注入策略**：不是每次都注入，按需检索：

1. 从用户消息中提取关键词（技术栈、问题类型）
2. 调用 `experience.find_relevant()` 三层混合检索
3. 通过 `experience.format_for_context()` 格式化
4. 仅当确实找到相关经验时才注入 system prompt，避免噪音

```python
# stages.py 阶段聊天注入逻辑
if user_mentions_code or is_first_message:
    relevant = await experience.find_relevant(session, tech_stack=keywords, limit=3)
    exp_ctx = experience.format_for_context(relevant, max_chars=2000)
```

**注入点**：

| AI 调用场景 | 触发条件 | 实现状态 |
|-------------|----------|----------|
| 阶段聊天 | 用户提到代码/技术关键词 或 首轮对话 | ✅ 已实现 |
| 阶段推进 | 无条件注入 | ✅ 已实现 |
| Agent 编程 | 任务分配时自动检索 | ✅ 已有 |
| 文档讨论 | 待实现 | 待接入 |
| 文档生成 | 待实现 | 待接入 |

**经验来源**（自动沉淀）：

| 触发点 | 实现函数 | 状态 |
|--------|----------|------|
| Agent 任务重试成功 | experience.extract_from_retry | ✅ 已实现 |
| 项目归档 | experience.settle_from_project | ✅ 已实现 |
| 文档审核发现重大问题 | 待实现 | 规划中 |
| 阶段推进时总结经验 | 待实现 | 规划中 |
| 用户手动标记 | 待实现（前端按钮） | 规划中 |

### Layer 3: SaaS 跨租户经验共享（未来）

**设计原则**：AI 共享一切，人类隔离自己的。

**数据模型扩展**（Experience 表增加字段）：

| 字段 | 类型 | 说明 |
|------|------|------|
| tenant_id | String | 租户标识 |
| visibility | String | "private" / "shared" |
| is_anonymized | Boolean | 是否已脱敏 |
| original_exp_id | String | 共享来源（跨租户引用） |

**共享规则**：

1. **AI 检索**：不区分租户，所有经验都可被 AI 检索和引用
2. **人类查看**：仅能看到本租户的经验，导出也只导本租户的
3. **自动共享**：`quality_score >= 8.0` 的经验自动脱敏后进入共享池
4. **脱敏规则**：去除项目名称、域名/IP、内部系统名等敏感信息

**检索层次**：

```
find_relevant() 检索顺序：
1. 本租户经验（优先，权重更高）
2. 共享池经验（补充）
3. 按 quality_score 排序去重
```

**实施策略**：暂不编码，等 SaaS 多租户架构确定后统一实现。当前 `source_project` 字段已记录来源，未来可据此脱敏。

---

## 存储层四层知识架构

```
┌─────────────────────────────────────────────────┐
│  Layer 4: 全局经验库（结构化经验记录）             │
│  - 踩坑记录、最佳实践、代码模板、调试模式          │
│  - PostgreSQL JSONB，跨项目积累                   │
│  → 用于：Opus 踩的坑下次 DeepSeek 也能解决        │
├─────────────────────────────────────────────────┤
│  Layer 3: 知识图谱（代码结构与关系）              │
│  - 模块/类/函数的依赖关系                        │
│  - API 调用链、数据流向                          │
│  → 用于：Leader 任务分解、影响范围分析            │
├─────────────────────────────────────────────────┤
│  Layer 2: RAG 检索（语义搜索）                    │
│  - 向量化的代码和文档片段                         │
│  - 语义相似度检索                                 │
│  → 用于：工程师查找相关代码/文档、Leader 审查参考   │
├─────────────────────────────────────────────────┤
│  Layer 1: 文档知识库（Markdown 文件）             │
│  - 编码规范、架构决策、API 契约                   │
│  - 直接读取，零成本                               │
│  → 用于：所有角色的基础参考                       │
└─────────────────────────────────────────────────┘
```

---

## 知识库作用域

```
知识库分两个作用域，生命周期完全不同：

全局知识库（永久存在）
  - 所有项目共享
  - 编码规范、最佳实践、经验教训
  - 挂载到所有 OpenClaw 实例（只读）
  - 项目结束后的经验沉淀也写入这里

项目知识库（随项目生命周期）
  - 项目创建时自动初始化
  - 项目进行中持续更新
  - 项目归档时随 workspace 一起备份
  - 项目销毁时清理（备份已保存）
  - 项目恢复时从备份还原
```

## Layer 1: 文档知识库（Markdown）

最简单也最实用。Markdown 文件，直接读取。

### 全局知识库（所有项目共享）

```
knowledge/global/
├── coding-standards.md      # 编码规范
├── git-workflow.md           # Git 工作流
├── api-conventions.md        # API 设计约定
├── testing-standards.md      # 测试规范
├── security-checklist.md     # 安全检查清单
└── lessons-learned.md        # 经验教训（跨项目积累）
```

挂载方式：所有 OpenClaw 实例挂载 `knowledge/global` 为只读。

### 项目知识库（项目级隔离）

```
knowledge/projects/{project-id}/
├── architecture.md           # 架构决策记录
├── api-contracts.md          # API 契约
├── data-model.md             # 数据模型
├── conventions.md            # 项目特定约定
├── known-issues.md           # 已知问题
└── context.md                # 项目上下文摘要
```

挂载方式：该项目的 OpenClaw 实例挂载对应项目目录。

### 生命周期管理

```
项目创建 → 初始化 knowledge/projects/{id}/ 目录
项目进行 → 工程师和 Leader 持续更新项目知识
项目归档 → 备份项目知识库 + 沉淀经验到全局
项目销毁 → 删除项目知识目录（备份已在 backups/ 中）
项目恢复 → 从备份还原项目知识目录
```

### 经验沉淀

项目归档时，Leader 自动执行经验沉淀：
1. 分析项目中的 known-issues.md 和关键决策
2. 提取可复用的经验教训
3. 追加到 `knowledge/global/lessons-learned.md`

---

## Layer 2: RAG 检索（基于 ljsafe-ai-server 改造）

### 现有资产

ljsafe-ai-server 已有成熟的 RAG 实现：

```
已有能力（直接复用）：
  ✅ Milvus 向量存储和检索 (milvus_client.py, rag_retrieval_service.py)
  ✅ BGE Embedding 服务 (embedding_service.py, sentence-transformers)
  ✅ 7 步 RAG 工作流引擎 (rag_workflow_engine.py)
  ✅ 相似度过滤 + Rerank (rerank_service.py, rrf_fusion.py)
  ✅ 质量评估 (answer_quality_evaluator.py)
  ✅ 问题分类框架 (question_classifier.py)
  ✅ 上下文构建 (context_expansion_service.py)
  ✅ BM25 稀疏检索 (bm25_retriever.py)
```

### 改造点

#### 1. 问题分类器：海事 → 编程

```
原有 8 种海事问题类型：
  knowledge_query, emergency, operation_guide, regulation, ...

改为编程场景类型：
  find_implementation    # 查找某功能的实现代码
  understand_architecture # 理解模块/系统架构
  debug_issue            # 排查 Bug，查找相关错误处理
  find_api               # 查找 API 定义和用法
  check_convention       # 查询编码规范和项目约定
  find_test              # 查找测试用例和覆盖情况
  change_impact          # 评估修改的影响范围
  find_similar           # 查找类似的实现（避免重复造轮子）
```

#### 2. 文档切分策略：段落 → AST

```
原有：按段落/章节切分文档
改为：

源代码（Tree-sitter AST 解析）：
  - 按函数/方法切分（含签名、docstring、实现）
  - 按类切分（含类定义、方法列表）
  - 保留文件级 import 和模块说明

Markdown 文档：
  - 按 ## 章节切分（保持原有逻辑）

API 定义：
  - 按 endpoint 切分

配置文件：
  - 整文件作为一个 chunk
```

#### 3. 多 Collection 支持

```
Milvus Collections（全局，永久）：
  global_knowledge     # 全局知识库（编码规范、经验教训等）

Milvus Collections（项目级，随项目生命周期）：
  {project_id}_code    # 源代码片段（函数/类级别）
  {project_id}_docs    # 设计文档和知识库 Markdown
  {project_id}_api     # API 定义（OpenAPI/接口文档）
  {project_id}_tests   # 测试用例

项目归档时：保留 Collection（不占多少空间）
项目销毁时：删除项目级 Collection
```

#### 4. 提示词模板：海事 → 编程

```
原有模板：海事安全领域问答
改为按编程场景的模板：

find_implementation 模板：
  "根据以下代码片段，回答关于 {query} 的实现问题。
   代码上下文：{retrieved_code}
   项目约定：{conventions}"

debug_issue 模板：
  "根据以下代码和错误信息，分析问题原因。
   相关代码：{retrieved_code}
   错误信息：{error_context}
   已知问题：{known_issues}"
```

#### 5. 新增：代码 AST 解析器

```python
# 用 Tree-sitter 解析代码，按函数/类粒度切分
# 支持的语言：Python, TypeScript, JavaScript, Java, Go, Rust 等

class CodeChunker:
    def chunk_file(file_path: str) -> list[CodeChunk]:
        """将源代码文件切分为函数/类级别的 chunk"""

    def chunk_project(project_path: str) -> list[CodeChunk]:
        """索引整个项目"""
```

#### 6. 新增：Git 增量索引

```
Git commit 触发：
  1. 检测变更文件列表 (git diff)
  2. 对变更文件重新 AST 解析和切分
  3. 删除旧向量，插入新向量
  4. 增量更新，不需要全量重建
```

### 改造后的工作流

```
原有 7 步（保持骨架）：
  1. 问题分类      → 改为编程场景分类
  2. 选择模板      → 改为编程场景模板
  3. 知识检索      → 改为多 Collection 检索（代码 + 文档 + API）
  4. 上下文构建    → 改为代码上下文构建（含 import、调用关系）
  5. LLM 生成      → 不变（换成 Claude）
  6. 答案后处理    → 改为代码格式化
  7. 质量评估      → 改为代码相关性评估
```

### 集成到团队的方式

```
工程师接到任务
    ↓
Leader 调用 RAG 检索相关代码和文档
    ↓
将检索结果注入任务指令（作为上下文）
    ↓
工程师基于丰富的上下文编码
    ↓
代码提交后 Git hook 触发增量索引更新
```

### 技术选型调整

| 组件 | ljsafe 原方案 | 编程团队方案 | 理由 |
|------|-------------|------------|------|
| 向量库 | Milvus (远程) | Milvus (Docker 本地) | 个人项目，本地部署够用 |
| Embedding | BGE-large-zh-v1.5 | BGE-M3 或 CodeBERT | BGE-M3 多语言多粒度；CodeBERT 代码专用 |
| 代码解析 | 无 | Tree-sitter | AST 级别切分，精确到函数/类 |
| LLM | Ollama (qwen) | Claude API | 编码能力更强 |
| 稀疏检索 | BM25 | BM25（复用） | 关键词匹配对代码搜索很有效 |
| 混合检索 | RRF 融合 | RRF 融合（复用） | 向量 + BM25 混合效果最好 |

---

## Layer 3: 知识图谱（代码结构与关系）

知识图谱解决 RAG 解决不了的问题：**理解代码之间的关系**。

RAG 能找到"相关的代码片段"，但不能回答：
- "修改这个函数会影响哪些模块？"
- "这个 API 的完整调用链是什么？"
- "哪些任务可以并行，哪些有依赖？"

### 技术选型：codebase-memory-mcp v0.4.6

| 特性 | 说明 |
|------|------|
| 部署 | 单 Go 二进制，预装在所有 OpenClaw 镜像中 |
| 语言 | 支持 64 种编程语言（tree-sitter 解析） |
| 存储 | SQLite（/workspace/.graph/codebase.db，随 workspace 备份） |
| 接口 | MCP 协议（OpenClaw 直接调用 12 个工具） |
| 更新 | Git diff 增量更新（毫秒级） |
| 查询 | sub-millisecond，99% fewer tokens than grep |
| 分析 | 模块边界检测、社区结构、架构概览、影响范围风险分级 |

### 图谱节点和边

```
节点：Module, Class, Function, API, Table, File
边：  IMPORTS, CALLS, EXTENDS, IMPLEMENTS, USES_TABLE, DEPENDS_ON
```

### 集成方式

工程师端（OpenClaw MCP 插件，直接调用）：
```
openclaw.json 中配置：
  "mcpServers": {
    "codebase-memory": {
      "command": "/usr/local/bin/codebase-memory-mcp",
      "args": ["--db", "/workspace/.graph/codebase.db"]
    }
  }
```

调度器端（Leader 通过 knowledge_graph.py 调用）：
```
dispatcher/app/services/knowledge_graph.py
  - get_task_context()   → 任务分解时注入图谱上下文
  - get_review_context() → 代码审查时注入影响分析
```

### 使用场景（8 个）

Leader 场景：
1. 任务分解：理解模块依赖，生成正确的任务依赖关系
2. 代码审查：detect_changes 返回影响范围 + 风险分级（CRITICAL/HIGH/MEDIUM/LOW）
3. 并行判断：两个任务涉及的模块是否有交集，无交集可并行

工程师场景：
4. Debug：trace_call_path 追踪调用链，定位问题根因
5. 安全修改：get_dependents 查出所有调用方，避免破坏性修改
6. 避免重复：search_symbol 查找已有的类似实现

系统级场景：
7. 测试范围：detect_changes 分析影响范围，只跑受影响的测试
8. 架构感知：get_architecture 一次调用返回完整架构概览

### 图谱数据生命周期

```
项目初始化 → 全量索引（首次，几秒到几分钟）
代码提交后 → Git diff 增量更新（毫秒级）
项目归档时 → 图谱数据在 workspace 内，随 workspace 一起备份
项目恢复时 → 从备份还原，图谱立即可用
```

---

## Layer 4: 全局经验库（PostgreSQL 结构化存储）

RAG 做语义检索，知识图谱做结构分析，但都不擅长存储**结构化的经验记录**。
全局经验库解决的核心问题：**Opus 踩过的坑，下次 DeepSeek 也能解决**。

### 数据模型

```
experiences 表：
  id              # 主键
  title           # 简明标题（如：Spring Boot @Transactional 在 private 方法上不生效）
  category        # 分类：pitfall / best_practice / code_template / architecture / debug_pattern / performance / security / devops
  tech_stack      # JSONB 数组（如 ["spring-boot", "jpa"]）
  tech_domain     # 技术域：frontend / backend / database / infrastructure / language / general
  tags            # JSONB 数组（如 ["transaction", "aop"]）
  problem         # 遇到的问题
  root_cause      # 根本原因
  solution        # 解决方案
  code_snippet    # 关键代码片段
  source_project  # 来源项目
  source_task_id  # 来源任务
  quality_score   # 质量评分（5-10，越高越通用）
  use_count       # 被引用次数（越多越有价值）
  metadata        # 扩展信息（提取方式、重试次数、使用的模型等）
```

### 生成期自动贴标（4 层管道）

经验写入时自动完成标签和分类，无需人工干预：

```
Layer 1: 项目上下文继承
    ↓ 从任务/项目配置获取项目技术栈
Layer 2: 代码静态分析
    ↓ 从 code_snippet 中正则匹配 import/框架/语法特征
Layer 3: LLM 语义推断
    ↓ 提取 prompt 中注入项目技术栈约束，限制候选范围
Layer 4: 交叉验证与修正
    ↓ 自动修正明显错误（无关技术、错误分类、质量分调整）
```

**Layer 1：项目上下文继承**

提取经验时，从 `task.context["tech_stack"]` 或 `project.config["tech_stack""]` 获取项目技术栈列表，注入 LLM prompt：

```
项目技术栈：["fastapi", "vue", "postgresql", "sqlalchemy"]
tech_stack 规则：必须从项目技术栈中选择，不要添加项目未使用的技术
```

**Layer 2：代码静态分析**

从 `code_snippet` 中正则检测技术栈，与 LLM 推断结果交叉验证：

| 检测目标 | 正则模式示例 | 映射技术 |
|----------|-------------|---------|
| Vue 项目 | `from\s+['"]vue['"]` \| `createApp(` | `vue` |
| FastAPI | `from\s+['"]fastapi['"]` \| `@app.` | `fastapi` |
| SQLAlchemy | `from\s+['"]sqlalchemy['"]` \| `create_engine(` | `sqlalchemy` |
| TypeScript | `interface\s+\w+` \| `type\s+\w+\s*=` | `typescript` |

检测到的技术若不在项目技术栈中但属于通用技术（`git`, `http`, `json` 等），也允许保留。

**Layer 3：LLM 语义推断**

`extract_from_retry()` 调用 AI Leader 提取经验时，prompt 已包含项目技术栈约束。LLM 在有限候选池中推断 `tech_stack` 和 `category`。

**Layer 4：交叉验证与修正**

`validate_and_correct_experience()` 执行 5 条验证规则：

| 规则 | 触发条件 | 修正动作 |
|------|---------|---------|
| 技术栈过滤 | 经验含项目未使用的技术 | 移除无关技术 |
| 代码补全 | 静态分析发现新技术且属于项目 | 追加到 `tech_stack` |
| 域推导 | `tech_stack` 变更或缺失 | 自动推导 `tech_domain` |
| 类别修正 | `code_snippet` 含 TODO/FIXME/deprecated | 强制 `category = "pitfall"` |
| 质量降分 | title 含业务专属名词（订单/支付/会员等） | `quality_score` 下调至 ≤6 |

`tech_domain` 推导映射：

| 技术 | 技术域 |
|------|--------|
| vue, react, pinia, nextjs | `frontend` |
| fastapi, django, spring-boot | `backend` |
| postgresql, redis, sqlalchemy | `database` |
| docker, kubernetes, nginx | `infrastructure` |
| python, typescript, go | `language` |
| 无匹配 / 空数组 | `general` |

### 检索期严格过滤：技术栈重叠检测

`find_relevant()` 四层检索均强制执行技术栈过滤，避免无关经验污染：

```
通用经验（tech_stack = []）→ 始终允许通过
非通用经验 → 必须与任务技术栈有重叠（?| 操作符）
```

四层检索均应用相同规则：

1. **tsvector 层**：SQL `AND (jsonb_array_length(tech_stack) = 0 OR tech_stack ?| :tech_filter_arr)`
2. **JSONB 层**：SQLAlchemy `or_(Experience.tech_stack == [], Experience.tech_stack.contains([t]))`
3. **LIKE 兜底**：同上
4. **语义搜索**：SQL `AND (jsonb_array_length(tech_stack) = 0 OR tech_stack ?| :tech_filter_arr)`

> 通用经验（`tech_stack = []`）作为跨技术域的基础知识始终保留，但具体技术相关经验必须严格匹配。

### 自动沉淀机制（两个触发点）

**触发点 1：重试成功后自动提取**

```
任务第 1 次失败 → 记录错误
任务第 2 次失败 → 记录错误，升级模型
任务第 3 次成功 → 自动触发经验提取
    ↓
Leader AI 分析：失败历史 + 最终成功结果
    ↓
提取结构化经验 → 写入 experiences 表
    ↓
下次类似任务 → 检索到这条经验 → 注入上下文
    ↓
DeepSeek 拿到"问题 + 根因 + 解决方案"直接搞定
```

**触发点 2：项目归档时批量沉淀**

```
项目归档 API 调用
    ↓
收集项目所有任务日志（completed / failed / retry）
    ↓
Leader AI 批量分析，提取可复用经验
    ↓
写入全局经验库
```

### 经验注入（任务分配时）

```
调度器分配任务
    ↓
从任务标题/描述中提取关键词和技术栈
    ↓
查询 experiences 表（tsvector 全文 + JSONB 匹配 + LIKE 兜底）
    ↓
匹配到相关经验 → 格式化为上下文文本
    ↓
同时生成 context_keys（知识块索引）写入 task.context
    ↓
注入到 Leader 生成的任务指令中
    ↓
工程师拿到的指令已包含历史经验 + 可按需加载的知识块 key
```

**context_keys 机制**：

调度器在分配任务时，除了把经验文本直接注入 instruction，还会把相关知识的"索引 key"
写入 `task.context["context_keys"]`。Worker 收到任务包后可以按需精确加载：

| key 格式 | 含义 | 示例 |
|----------|------|------|
| `project_info` | 项目基础信息（名称、技术栈、Git 等） | 始终包含 |
| `exp_{id}` | 全局经验库中的某条经验 | `exp_a1b2c3d4` |
| `doc_s{N}` | 阶段 N 的文档 | `doc_s4`（任务分解阶段） |
| `code_analysis` | 代码分析摘要 | 按需 |
| `docs:{name}` | 项目设计文档 | `docs:07-KNOWLEDGE_SYSTEM` |

### 经验质量演进

```
quality_score：Leader AI 提取时初始评分
use_count：每次被引用时 +1
→ 高 use_count + 高 quality_score 的经验排在前面
→ 低质量经验自然沉底，不影响检索效率
→ 后续可加入人工评分和反馈机制
```

### 经验 Embedding 自动计算

经验写入 `experiences` 表时，系统自动计算 embedding 向量：

1. 拼接经验的 title + problem + root_cause + solution + tech_stack 为全文
2. 调用本地 Ollama（`bge-m3`）生成 1536 维向量，零 token 成本
3. 若 Ollama 不可用，自动 fallback 到云端 `text-embedding-3-small`
4. 向量写入 `embedding` 字段，供语义搜索使用

这使 `knowledge_search` 的 `semantic` 模式和 `auto` 模式都能正常命中经验。

### API 端点

```
POST   /api/experiences          # 手动创建经验
GET    /api/experiences          # 搜索（keyword, category, tech_stack, tags）
GET    /api/experiences/{id}     # 获取详情
PUT    /api/experiences/{id}     # 更新
DELETE /api/experiences/{id}     # 删除
GET    /api/experiences/categories  # 获取所有分类
```

---

## 四层联合查询

```
任务描述 / 查询问题
    ↓
┌──────────────────────────────────────────┐
│ 1. 全局经验库：匹配历史踩坑和解决方案      │
│ 2. 知识图谱：找到相关模块和依赖关系        │
│ 3. RAG：找到相关代码片段和文档             │
│ 4. 文档知识库：补充规范和约定              │
│ → 合并为完整上下文                        │
└──────────────────────────────────────────┘
    ↓
注入到 Leader / 工程师的 prompt
```

经验库优先级最高——如果已有精确匹配的历史经验，可能根本不需要 RAG 和图谱。
这也是"低端模型替代高端模型"的关键：经验越丰富，对模型能力的要求越低。

---

## 实施路径

### Phase 1：文档知识库（✅ 立即可用，0 成本）

创建 knowledge/ 目录，写入编码规范等 Markdown 文件。
在 Skill 中配置工程师读取知识文件。

### Phase 2：知识图谱（⚠️ 已封装，需配置启用）

1. ✅ codebase-memory-mcp 预装在所有 Docker 镜像中
2. ✅ OpenClaw 配置自动生成 MCP Server 配置
3. ⚠️ 调度器 `knowledge_graph.py` 已封装 8 个查询，**需配置 `CODEBASE_MEMORY_DB_PATH` 后自动启用**（任务分配时注入代码结构上下文）
4. ✅ 工程师 Skill 中配置图谱使用指南

**启用方式**：在 `.env` 或环境变量中设置 `CODEBASE_MEMORY_DB_PATH=/path/to/codebase.db`，
调度器分配任务时会自动调用 `get_task_context()` 将代码结构洞察注入任务指令。

### Phase 3：全局经验库（✅ 已完成基础实现，核心价值层）

实测验证：同类问题从 30 分钟 → 5 分钟（6 倍效率提升），手动流程已跑通，
现在系统实现全自动化：

1. ✅ Experience 数据模型（PostgreSQL JSONB + GIN 索引）
2. ✅ 经验 CRUD + 搜索 API
3. ✅ 重试成功后自动提取经验（scheduler → experience.extract_from_retry）
4. ✅ 项目归档时批量沉淀经验（archive → experience.settle_from_project）
5. ✅ 任务分配时注入相关经验到上下文（assign_task → experience.find_relevant）
6. ✅ 阶段聊天按需注入经验上下文（stages.py → experience.find_relevant）
7. ✅ 阶段推进时注入经验上下文

### Phase 3.5：项目级知识上下文（✅ 已实现）

统一的项目知识注入服务 `project_context.py`，让所有 AI 对话都能获取项目基础信息：

1. ✅ project_context.py 统一构建项目元数据 + 代码分析摘要 + 已审核文档
2. ✅ 文档讨论（DOC_CHAT_SYSTEM）注入项目上下文
3. ✅ 文档应用修改（DOC_APPLY_SYSTEM）注入项目上下文
4. ✅ 阶段聊天 stages.py 迁移到统一服务（get_asset_context）
5. 🔲 AI 审核注入项目上下文（待接入）

### Phase 4：智能模型选择（✅ 已完成基础规则）

基于规则的确定性选择，不用 AI 判断（避免误判）：
```
角色规则（确定性）：
  architect → 固定 Opus
  其他角色 → 固定 Sonnet
  任务标记 suggested_model → 优先使用

失败自动升级链：
  DeepSeek → Sonnet → Opus
  第 2 次重试自动升级到更强模型

后续演进（等数据积累后）：
  基于历史任务成功率调整默认模型
  基于经验命中率动态降级（经验丰富的领域 → DeepSeek 也能搞定）
```

### Phase 5：经验驱动的模型降级

1. 基于经验命中率动态调整模型选择
2. 经验质量反馈机制（使用经验后任务成功 → 提升评分，失败 → 降低评分）
3. 统计分析：哪些技术栈/问题类型的经验库已足够成熟，可以安全降级

### Phase 6：统一知识检索工具（✅ 已实现）

**模块**：`knowledge_search.py`

独立的检索工具模块，统一所有知识源的搜索入口。

**三种检索模式**：

1. **关键字匹配**（`keyword`）：支持 `+term`（AND）、`-term`（NOT）、`/regex/`（正则）
2. **全文检索**（`fulltext`）：jieba 分词 → tsvector，零 token 成本
3. **语义搜索**（`semantic`）：本地 embedding 优先，云端 fallback

**搜索范围**：Document、Experience、ProjectAsset、TaskDocument

所有表均支持 tsvector 全文检索 + pgvector 语义搜索：
- `Experience`：✅ tsv + embedding（写入时自动计算）
- `TaskDocument`：✅ tsv + embedding
- `Document`：✅ tsv + embedding（模型字段已添加，待数据库迁移）

**本地 Embedding 模型**：

为避免语义搜索的 token 消耗，embedding 计算优先使用本地 Ollama 部署的 mini 模型：
- 推荐模型：`bge-m3`（中文效果优秀，1024 维，568M 参数，纯 CPU 即可）
- 轻量备选：`nomic-embed-text`（768 维，170M 参数）
- 配置项：`OLLAMA_EMBEDDING_MODEL` in `config.py`
- Fallback：当 Ollama 不可用时自动降级到云端 `text-embedding-3-small`

**Ollama 作为基础设施**：

Ollama 服务和 PostgreSQL、Redis 一样是系统的核心基础设施，提供零 token 成本的 embedding 计算和本地文本生成能力。

```
┌─────────────────────────────────────────────┐
│  基础设施层                                    │
│                                              │
│  ┌──────────┐  ┌───────┐  ┌──────────────┐  │
│  │PostgreSQL│  │ Redis │  │   Ollama VM  │  │
│  │(pgvector)│  │       │  │  ┌─ bge-m3   │  │
│  │          │  │       │  │  └─ qwen2.5  │  │
│  └──────────┘  └───────┘  └──────────────┘  │
│       ↑             ↑            ↑           │
│       └─────────────┼────────────┘           │
│                     │                        │
│         ┌───────────┴───────────┐            │
│         │     Dispatcher        │            │
│         │  启动时自动发现 Ollama  │            │
│         │  InfraNode(role=ollama)│            │
│         └───────────────────────┘            │
└──────────────────────────────────────────────┘
```

**部署方式一：独立 VM（推荐，可多系统共用）**

```bash
# 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama

# 拉取模型
ollama pull bge-m3               # embedding ~1.2GB
ollama pull qwen2.5-coder:14b    # 文本生成 ~10GB（可选）

# 配置监听地址（允许远程访问）
# /etc/systemd/system/ollama.service.d/override.conf
# [Service]
# Environment="OLLAMA_HOST=0.0.0.0"
systemctl daemon-reload && systemctl restart ollama
```

**部署方式二：Docker Compose（和系统一起部署）**

```bash
# docker-compose.yml 中已包含 ollama 服务（profiles: ["ollama"]）
docker compose --profile ollama up -d
docker compose exec ollama ollama pull bge-m3
```

**部署方式三：Mac Mini（高性能本地推理）**

```bash
# macOS 原生安装 Ollama
brew install ollama
ollama serve &
ollama pull bge-m3
```

**在系统中注册为基础设施**：

通过「基础设施管理 → 添加节点」注册 Ollama 服务器：
- 类型：linux/vm
- 角色：选择 `ollama`
- 配置：`{"ollama_url": "http://172.16.210.147:11434"}`

系统启动时自动发现带 `ollama` 角色的 InfraNode，测试连通后自动配置 `OLLAMA_BASE_URL`。

手动触发：`POST /api/infra/ollama/apply` — 检测并应用可用的 Ollama 节点。

查看状态：`GET /api/infra/ollama/status` — 列出所有 Ollama 节点及其模型。

**配置项**（`config.py` / `.env`）：

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 服务地址（启动时可被自动发现覆盖） |
| `OLLAMA_EMBEDDING_MODEL` | `bge-m3` | embedding 模型名 |
| `OLLAMA_ENABLED` | `true` | 是否启用 Ollama |

资源需求：
- 仅 embedding：4C 4G 足够（bge-m3 ~1.5GB）
- embedding + 文本生成：4C 16G（同时加载时峰值 ~12GB）
- 升级路径：未来如需更强本地推理，可迁移到 Mac Mini（M 系列统一内存跑 LLM 效果优异）

**AI 对话集成**：

AI 在对话中可通过两种标记触发知识加载：
- `[NEED_CONTEXT:key]`：按索引 key 精确加载（project_info、code_analysis、doc_s0 等）
- `[SEARCH:查询文本]`：调用统一检索工具进行模糊搜索

**API 端点**：

- `GET /api/projects/{project_id}/search?q=关键词&mode=auto&category=design&tags=数据库`
- `GET /api/projects/{project_id}/doc-categories` — 获取分类列表

### Phase 6.1：多维度文档分类体系（✅ 已实现）

人和 AI 面对的是同一个知识库，解决的是同一组问题：**缺什么信息、去哪里找、怎么找到**。
区别只是人用界面点击筛选，AI 用标记触发检索。

**四个检索维度**：

| 维度 | 字段 | 索引类型 | 含义 | 示例 |
|------|------|---------|------|------|
| 阶段 | `stage` | B-Tree | 文档产生于哪个阶段 | 0=业务方案, 3=技术方案 |
| 类型 | `category` | B-Tree | 文档的本质用途 | plan/spec/design/analysis |
| 标签 | `tags` | **GIN 倒排索引** | 自由标注的领域标签 | ["数据库", "API", "安全"] |
| 状态 | `status` | B-Tree | 文档生命周期 | draft → under_review → approved |

**文档类型（category）定义**：

```
规划类（做什么、为什么做）
├── plan          方案规划    业务方案、需求文档、产品原型
└── spec          规范标准    API 规范、代码风格、接口契约

设计类（怎么做）
├── design        架构设计    技术方案、系统架构、数据库设计
└── decision      架构决策    ADR 记录、技术选型决策

分析类（现状是什么）
└── analysis      分析报告    代码分析、性能分析、安全审计

执行类（做了什么、结果如何）
├── task          任务文档    任务指令、执行报告
├── review        审查记录    代码审查、文档评审
├── test          测试文档    测试计划、测试报告
└── deploy        部署文档    部署方案、运维手册

过程类（项目管理和沟通）
├── log           过程日志    错误日志、升级记录、变更日志
├── meeting       会议纪要    讨论纪要、评审纪要
└── retrospective 项目复盘    经验总结、复盘报告

默认
└── general       通用文档    未分类
```

**标签（tags）采用 GIN 倒排索引**：

- 数据结构：JSONB 数组（`["数据库", "API", "安全"]`）
- 索引：PostgreSQL GIN（本质是倒排索引，与 Elasticsearch 原理一致）
- 查询方式：
  - 包含某标签：`tags @> '["数据库"]'`
  - 包含任一：`tags ?| array['数据库', 'API']`
- 性能：百万级文档毫秒级响应

**自动分类**：

- 阶段文档生成时按 `STAGE_DEFAULT_CATEGORY` 自动设置 category
- 代码分析报告自动标记为 `analysis` + 相关 tags
- TaskDocument 的 `doc_type` 通过 `TASK_DOCTYPE_TO_CATEGORY` 映射到统一 category

**与 TaskDocument.doc_type 的统一关系**：

```
TaskDocument.doc_type          →  统一 category
───────────────────────────────────────────────
architecture_decision          →  decision
task_instruction               →  task
task_report                    →  task
error_log                      →  log
escalation_record              →  log
code_review                    →  review
stage_document                 →  general
```

### Phase 6.2：AI 角色知识检索能力（✅ 已实现）

所有 AI 角色（架构师、高级/中级/初级工程师、运维）的提示词中增加了 **KNOWLEDGE** section，
让每个角色都"固化"知道：

1. **有知识库可用**：系统会提供知识索引
2. **什么时候该查**：做决策前、写代码前、遇到不确定问题时
3. **怎么查**：`[NEED_CONTEXT:key]` 精确加载 / `[SEARCH:关键词]` 模糊搜索

不同角色的检索场景不同：
- **架构师**：查架构文档、代码分析、经验库，做决策前必查
- **高级工程师**：查 API 规范、代码风格、经验库中的解决方案
- **中级/初级工程师**：查 API 规范、代码风格、任务引用的文档
- **运维工程师**：查部署方案、技术方案中的架构要求

**知识范围**（AI 可访问的全部知识源）：

| 来源 | 检索方式 | 说明 |
|------|---------|------|
| Document（阶段文档） | category + tags + 关键字 + 语义 | 用户和 AI 生成的项目文档 |
| Experience（经验库） | category + 关键字 + 语义 | 历史问题的解决方案 |
| ProjectAsset（代码分析） | 关键字 + 摘要 | 上传代码的分析结果 |
| TaskDocument（过程文档） | doc_type + 关键字 + 语义 | 任务执行过程中的产出 |
| **docs/（项目设计文档）** | **`[NEED_CONTEXT:docs:文件名]`** | **系统自身的架构和设计文档** |

### Phase 7（未来）：SaaS 跨租户经验共享

> 前置条件：SaaS 多租户架构确定后实施。

1. Experience 表增加 `tenant_id`、`visibility`、`is_anonymized`、`original_exp_id` 字段
2. AI 检索不区分租户（所有经验都可被 AI 引用）
3. 人类界面仅显示本租户经验，导出也仅限本租户
4. `quality_score >= 8.0` 的经验自动脱敏后进入共享池
5. 脱敏规则：去除项目名称、域名/IP、内部系统名等敏感信息
6. 检索优先级：本租户 > 共享池 > 按 quality_score 排序

---

## Phase 1：检索层升级（已完成）

### Hybrid Search（RRF 融合检索）

**问题**：单一检索模式各有盲区——关键词匹配不理解语义，语义搜索对专有名词不敏感，全文检索对短查询效果差。

**方案**：`knowledge_search.py` 的 `mode="auto"` 并行执行三种检索，用 Reciprocal Rank Fusion (RRF) 融合排名：

```
score = sum(1 / (k + rank)) for each result list where item appears
k = 60（论文推荐值）
```

实现：
- 关键词搜索（`keyword`）：支持 `+term`（AND）、`-term`（NOT）、`/regex/`（正则）
- 全文检索（`fulltext`）：jieba 分词 → tsvector → pgvector GIN 索引，零 token 成本
- 语义搜索（`semantic`）：本地 Ollama `bge-m3` 优先，云端 `text-embedding-3-small` fallback

融合结果的相关性优于任一单一模式，盲区被其他模式补齐。

### 查询重写（Query Rewriting）

**问题**：Worker 的原始查询往往口语化、模糊（如"怎么连数据库"），直接搜索召回率低。

**方案**：`query_rewriter.py` 将模糊查询扩展为 3-5 个精准技术搜索词：

```
原始: "怎么连数据库"
重写: ["PostgreSQL connection pool config", "SQLAlchemy asyncpg async connect",
       "database connection timeout handling"]
```

- 短查询（< 15 字）或不含技术术语时触发重写
- LLM 驱动，temperature=0.3，非阻塞（失败时回退到原查询）
- 集成到 `knowledge_search.py` auto 模式：扩展查询并行搜索后 dedup + RRF 融合

### Embedding 策略

经验创建时自动计算 embedding：
1. 拼接 title + problem + root_cause + solution + tech_stack 为全文
2. 调用本地 Ollama `bge-m3` 生成 1536 维向量，零 token 成本
3. 若 Ollama 不可用，自动 fallback 到云端 `text-embedding-3-small`
4. 向量写入 `embedding` 字段，供语义搜索使用

---

## Phase 2：架构层升级（已完成）

### 推模式：知识块原文嵌入 instruction

**问题**：只传 `context_keys`（key 列表）给 Worker，Worker 仍需自行加载知识，增加了执行时延和不确定性。

**方案**：`scheduler.py` 的 `assign_task()` 中，调用 `KnowledgeService.get_snippets()` 将相关知识块的原文摘要直接拼接到 instruction 文本中：

```python
knowledge_snippets = await knowledge_svc.get_snippets(session, context_keys, project_id)
# → instruction 中已包含 "## exp_xxx\n问题: ... 方案: ..."
```

Worker 收到的 instruction 中已包含必要上下文，无需额外检索即可开始工作。`context_keys` 仍然保留，Worker 可按需加载完整内容。

### Token 预算管理

**问题**：知识上下文无限制增长，可能挤占任务指令的空间。

**方案**：`token_budget.py` 按优先级分配知识上下文空间：

| 优先级 | 内容 | 说明 |
|--------|------|------|
| P1 | escalation_ctx | 升级上下文（最高优先级，必须保留） |
| P2 | exp_ctx, knowledge_snippets | 相关经验和推送摘要 |
| P3 | related_docs_ctx, kg_ctx | 过程文档和知识图谱 |

- 默认预算 12000 字符（~3000 tokens），可按项目配置 `knowledge_budget`
- 同优先级均分剩余预算，超限时按段落边界截断
- 确保关键上下文始终保留，低优先级内容自动裁剪

### KnowledgeService 抽象层

**目标**：隔离存储实现细节，未来替换存储后端（PostgreSQL → Milvus/Weaviate）时业务代码零改动。

`knowledge_service.py` 统一封装：
- 检索：`query()`, `search_for_context()`, `find_relevant_experiences()`
- 加载：`get_snippets()`, `build_knowledge_index()`
- 经验操作：`extract_experience_from_retry()`, `extract_failure_pattern_from_retry()`
- 审核：`review_experience()`, `batch_review_experiences()`
- 维护：`auto_deprecate()`, `generate_audit_report()`, `detect_conflicts()`, `analyze_knowledge_gaps()`
- 关联：`link_experiences()`, `find_related_experiences()`, `auto_discover_associations()`

---

## Phase 3：知识治理体系（已完成）

### 状态机

经验生命周期：`draft → reviewed → published → deprecated → archived`

| 状态 | 可检索 | 说明 |
|------|--------|------|
| draft | ❌ | AI 自动提取的原始记录 |
| reviewed | ❌ | 通过 LLM 自查（格式、完整性、自洽性） |
| published | ✅ | 通过专家复审，可被 Worker 检索 |
| deprecated | ❌ | 过期或长期未使用 |
| archived | ❌ | 已清理，仅保留记录 |

所有检索入口强制过滤 `status='published'`，确保 Worker 只能看到已审核的知识。

### 分类体系（Taxonomy）

四个分类维度：

| 维度 | 字段 | 取值示例 |
|------|------|---------|
| domain | `domain` | architecture / coding / devops / security / business |
| type | `type` | fact / rule / experience / incident / decision |
| scope | `scope` | global / team / project / module |
| freshness | `freshness` | permanent / medium / temporary |

`find_relevant()` 四层检索均支持按分类维度过滤，可按任意维度组合筛选结果。

### 审核流水线

`knowledge_review.py` 实现三级审核：

1. **初审（self_review）**：LLM 检查格式完整性、内容自洽性、技术准确性、可复用性
   - 评分 8-10：直接通过到 reviewed
   - 评分 5-7：有小问题但可接受
   - 评分 < 5：需要修改

2. **复审（expert_review）**：LLM 扮演技术专家，审核技术深度、方案可行性、边界情况
   - 评分 ≥ 6 且通过 → published
   - 否则保留 issues 和 concerns

3. **批量审核（batch_review）**：自动处理所有 draft → reviewed → published 的流转

### 冲突检测

`knowledge_maintenance.detect_conflicts()`：
1. 用 embedding 相似度筛选候选经验（阈值 0.75）
2. 对高相似度候选，用 LLM 判断是否在语义上冲突（"使用 asyncio" vs "不要使用 asyncio"）
3. 返回冲突列表 `{exp_id, title, similarity, reason}` 供人工处理

### 自动降级

`knowledge_maintenance.auto_deprecate()` 定时扫描规则：
- `valid_until` 已过期 → DEPRECATED
- 6 个月未被引用（`use_count=0`）→ DEPRECATED
- 90 天草稿/审核中仍未 published → ARCHIVED
- 引用 10 次成功率 < 30% → 标记 suspicious（metadata 中记录）

---

## Phase 4：高级特性与运营（已完成）

### 经验版本化

Experience 模型增加：
- `version_range`：适用版本范围（如 "Django 3.x-4.x"）
- `valid_until`：过期时间，auto_deprecate 自动处理

### 负样本记录（FailurePattern）

**核心理念**：「什么做法不行」比「什么做法行」更有价值——因为它能直接帮助其他人避免同样的错误。

`failure_patterns` 表：
- `pattern_type`：syntax_error / runtime_error / logic_error / dependency_conflict / config_error / test_failure / performance_issue
- `failure_symptom`：失败现象/错误信息
- `failed_approach`：尝试了但失败的方法
- `successful_approach`：最终成功的方法（对比）
- `source_experience_id`：关联的正面经验

自动提取：`experience.extract_failure_pattern_from_retry()` 从 retry 失败历史中自动提取失败模式，计算 embedding 和 tsv 供检索。

### 知识缺口分析

`knowledge_maintenance.analyze_knowledge_gaps()`：
1. 分析 `TaskLog` 中的 knowledge_search 动作
2. 统计高频搜索但低命中（< 30%）的主题
3. 返回缺口报告：`{topic, search_count, hit_rate, suggested_action}`

示例：
```
现象：Worker 在 auth 模块频繁搜索 "token 过期处理"
结论：auth 模块缺少相关知识
行动：自动生成知识创建任务
```

### 知识审计报告

`knowledge_maintenance.generate_audit_report()` 每月自动生成健康度报告：
- 总条目数、按 status/category 分布
- 过期比例、零命中条目数、孤儿条目数（无 tech_stack/tags/keywords）
- 平均质量分、失败模式分布
-  actionable 清理建议清单

### 经验关联图谱

Experience 模型增加 `related_exp_ids`（JSONB 数组）：

```
经验 A (解决 SQL 慢查询) --关联--> 知识块 (索引优化指南)
       |
       +--关联--> 经验 B (添加复合索引)
       +--关联--> 经验 C (避免 N+1 查询)
```

- `link_experiences()`：双向建立关联
- `find_related_experiences()`：查看一条经验时自动推荐相关知识和经验
- `auto_discover_associations()`：按 embedding 相似度自动发现潜在关联

---

## 新模块文件索引

| 模块 | 文件 | 职责 |
|------|------|------|
| 查询重写 | `app/services/query_rewriter.py` | 模糊查询 → 精准技术搜索词 |
| Token 预算 | `app/services/token_budget.py` | 按优先级分配知识上下文空间 |
| 知识维护 | `app/services/knowledge_maintenance.py` | 自动降级 + 审计 + 冲突检测 + 缺口分析 + 关联 |
| 知识审核 | `app/services/knowledge_review.py` | 三级审核流水线 |
| 知识服务 | `app/services/knowledge_service.py` | 统一抽象层，封装全部知识操作 |
| 统一检索 | `app/services/knowledge_search.py` | Hybrid Search (RRF) + 三种检索模式 |
| 经验库 | `app/services/experience.py` | CRUD + 四层检索 + 负样本提取 + taxonomy 过滤 + 自动贴标 + 严格过滤 |
