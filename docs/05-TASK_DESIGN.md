# 任务分解与调度策略

## 任务分解的输入物

任务分解（Stage 4）不是凭空拆分，它的输入是前面所有阶段的累积产出：

```
输入：
  01-requirements.md      功能列表、用户故事、验收标准
  02-prototype.md         页面结构、交互逻辑、API 草案
  03-technical-design.md  架构设计、数据库设计、API 详细定义、目录结构
  + 知识库上下文          编码规范、项目约定、已有代码结构（RAG 检索）

输出：
  04-task-breakdown.json  结构化任务清单
```

Leader 做任务分解时，必须综合参考所有输入物：
- 从需求文档知道"要做什么"
- 从原型文档知道"长什么样"
- 从技术方案知道"怎么做"
- 从知识库知道"项目的约定和已有代码"

## 两级 Plan 机制

任务分解的思路类似 Cursor 的 Plan 模式，但分为两级：

```
Leader 的"大 Plan"（项目级）：
  理解全部设计文档 → 拆分为 0.3-1h 的任务 → 标注依赖和角色 → 分发

工程师的"小 Plan"（任务级）：
  理解任务指令 + 上下文 → 规划实现步骤 → 逐步编码
```

Leader 的大 Plan 关注"做什么、谁来做、什么顺序"。
工程师的小 Plan 关注"怎么实现、改哪些文件、怎么测试"。

## 任务粒度原则

**目标粒度：0.3-1 小时/任务**

```
太粗（3h+）：工程师容易跑偏，失败成本高
太细（<10min）：调度开销大，上下文切换频繁
合适（0.3-1h）：可用经济模型，失败了重做成本低
```

### 分解示例

```
❌ 错误粒度：
  T1: 实现用户管理模块（8h）

✅ 正确粒度：
  T1.1: 创建 User 数据模型和迁移脚本（0.5h）
  T1.2: 实现 POST /api/users 创建用户（0.5h）
  T1.3: 实现 GET /api/users 用户列表（0.3h）
  T1.4: 实现 GET /api/users/{id} 用户详情（0.3h）
  T1.5: 实现 PUT /api/users/{id} 更新用户（0.3h）
  T1.6: 实现 DELETE /api/users/{id} 删除用户（0.3h）
  T1.7: 实现手机号唯一性校验（0.3h）
  T1.8: 编写用户 API 单元测试（0.5h）
```

## 任务结构

每个任务必须包含：

```json
{
  "id": "T001",
  "title": "任务标题",
  "description": "详细描述",
  "type": "feature|bug|test|deploy|refactor|docs",
  "priority": 1,
  "suggested_role": "backend|frontend|tester|devops",
  "suggested_model": "sonnet|opus",
  "estimated_hours": 0.5,
  "dependencies": ["T000"],
  "input_files": ["03-technical-design.md#数据库设计"],
  "output_files": ["src/models/user.py"],
  "acceptance_criteria": ["模型包含所有必需字段", "迁移脚本可执行"]
}
```

**input_files**：这个任务需要参考的设计文档章节，Leader 分发时会提取这些内容作为上下文。
**output_files**：这个任务应该产出的文件，用于验收和后续任务的依赖检查。
**acceptance_criteria**：验收标准，工程师自检 + Leader 审查的依据。

## 任务分类与模型选择

| 任务类型 | 推荐模型 | 示例 |
|---------|---------|------|
| CRUD 接口 | Sonnet | 增删改查 API |
| 数据模型 | Sonnet | Entity、DTO、Migration |
| 配置文件 | Sonnet | Docker、CI/CD |
| 页面组件 | Sonnet | 表单、列表、详情页 |
| 单元测试 | Sonnet | 标准测试用例 |
| 复杂业务逻辑 | Opus | 权限系统、工作流、状态机 |
| 算法实现 | Opus | 搜索、排序、图算法 |
| Bug 排查 | Opus | 跨模块问题定位 |
| 性能优化 | Opus | 查询优化、缓存策略 |

**目标比例：80% Sonnet + 20% Opus**

## 任务状态流转

```
pending → assigned → in_progress → review → done
                         │            │
                       failed      testing → done
                         │            │
                       blocked      failed
```

## 依赖管理

调度器自动识别可并行的任务：

```json
{
  "tasks": [
    {"id": "T1", "title": "数据模型",  "dependencies": []},
    {"id": "T2", "title": "后端 API",  "dependencies": ["T1"]},
    {"id": "T3", "title": "前端页面",  "dependencies": ["T2"]},
    {"id": "T4", "title": "单元测试",  "dependencies": ["T2"]},
    {"id": "T5", "title": "集成测试",  "dependencies": ["T3", "T4"]}
  ]
}

执行顺序：T1 → T2 → (T3 || T4) → T5
T3 和 T4 无互相依赖，可并行执行。
```

## 自动分配策略

```
1. 获取所有 pending 且依赖已满足的任务
2. 获取所有 idle 的工程师
3. 按 suggested_role 匹配
4. 无精确匹配时分配给任意空闲工程师
5. 所有工程师都忙时排队等待
```

## 失败重试与升级

```
工程师执行失败
    ↓
自动重试（最多 3 次）
    ↓
3 次失败 → 生成升级报告
    ↓
Leader 分析
    ├── 能解决 → 调整任务描述，重新分配
    └── 不能解决 → 升级给你
```
