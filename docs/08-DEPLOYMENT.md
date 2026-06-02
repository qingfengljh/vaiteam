# 部署方案

## 部署理念：控制面与数据面可分离

系统设计支持**控制面**（Dispatcher + Web UI）和**数据面**（Git 仓库 + 部署服务器）的灵活组合。用户最关心的资产——源码、数据库、部署产物——可以完全保留在用户自己的基础设施上，平台仅负责 AI 编排调度。详细的架构理念参见 [22-SAAS_ARCHITECTURE.md](22-SAAS_ARCHITECTURE.md) 的「控制面与数据面分离」章节。

## 用户全自托管

用户可完全使用自有服务器，实现控制面和数据面均在用户侧：

- **Dispatcher**（控制面）：在自有服务器上运行 `docker-compose.prod.yml`（PostgreSQL、Redis、Dispatcher、Web）
- **Agent 机器**（数据面）：通过「快速添加」或「基础设施」加入自有 Linux/Windows 服务器，系统在其上部署 Agent
- **Git 服务器**（数据面）：项目 `git_repo`、`git_web_url` 指向自有 GitLab/Gitea/GitHub

三者可独立选择，支持混合部署（如 Dispatcher 用公有云、Agent 用自有机房、Git 用公司 GitLab）。即使控制面使用平台服务，源码和数据仍在用户自己的服务器上。

---

## 部署架构

```
宿主机（一台物理机或 Linux 服务器）
  │
  ├── 控制面（docker-compose，常驻）
  │   ├── dispatcher    调度器 (FastAPI)     :8080
  │   ├── postgres      数据库               :5432
  │   └── milvus        向量数据库           :19530
  │
  └── OpenClaw 实例（每个角色独立 docker-compose，按项目动态创建）
      ├── claw-proj1-architect   :18790
      ├── claw-proj1-backend     :18791
      ├── claw-proj1-frontend    :18792
      ├── claw-proj1-tester      :18793
      └── ...按需增减

可选：独立服务器用于需要完整环境的调试场景
  └── 服务器上跑 Docker，系统通过 SSH 远程管理
```

## 环境要求

- Docker Engine (≥ 20.10) + Docker Compose (≥ 2.0)
- 最低 8GB RAM（Milvus 4GB + 其余共享）
- 20GB+ 磁盘空间
- DeepSeek API Key（Leader 用）
- Claude API Key（通过代理平台，如 147API）

## 部署流程

### 1. 启动基础设施

```bash
cd vaiteam/deploy

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LEADER_API_KEY、ANTHROPIC_API_KEY 等

# 启动基础设施
docker compose up -d

# 验证
curl http://localhost:8080/health
```

### 2. 创建项目并生成团队配置

通过调度器 API 创建项目后，调用部署接口生成团队配置：

```bash
# 创建项目
curl -X POST http://localhost:8080/api/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "my-project", "description": "..."}'

# 一键生成团队部署配置
curl -X POST http://localhost:8080/api/deploy/generate-team \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "xxxxxxxx",
    "roles": ["architect", "backend", "frontend", "tester"],
    "architect_model": "claude-opus-4-20250514",
    "engineer_model": "claude-sonnet-4-20250514",
    "base_port": 18790,
    "api_key": "your-147api-key",
    "api_base": "https://api.147api.com"
  }'
```

这会在 `projects/{project_id}/` 下为每个角色生成：
```
projects/{project_id}/{role}/
  ├── config/
  │   └── openclaw.json      # OpenClaw 配置（模型、工具、网关）
  ├── workspace/              # 工作目录（代码、记忆体）
  ├── docker-compose.yml      # 独立的 compose 文件
  └── .env                    # API Key 等敏感信息
```

### 3. 启动 OpenClaw 实例

```bash
# 逐个启动（或写脚本批量启动）
cd projects/{project_id}/architect && docker compose up -d
cd projects/{project_id}/backend && docker compose up -d
cd projects/{project_id}/frontend && docker compose up -d
cd projects/{project_id}/tester && docker compose up -d
```

### 4. 创建 Agent 到调度器

部署接口已自动在 DB 中创建 Agent。启动后调度器即可向其分发任务。

## OpenClaw 实例配置说明

基于管理员手册（openclaw管理员手册.md）的实际经验：

### 配置文件结构

```
config/
  ├── openclaw.json     # 主配置（模型、工具、网关、安全）
  ├── plugins/          # 插件目录
  └── tokens/           # 认证令牌
```

### 关键配置项

| 配置 | 说明 | 角色差异 |
|------|------|---------|
| `tools.profile` | 工具权限 | architect/devops 用 `full`，其他用 `coding` |
| `agents.defaults.model.primary` | 主模型 | architect 用 Opus，其他用 Sonnet |
| `agents.defaults.workspace` | 工作目录 | 统一 `/workspace` |
| `gateway.auth.token` | 网关令牌 | 每个实例独立生成 |
| `agents.defaults.maxConcurrent` | 最大并发 | 默认 4 |

### 安全加固（沿用管理员手册）

```yaml
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
cap_add:
  - NET_BIND_SERVICE
tmpfs:
  - /tmp
```

### 目录挂载

```yaml
volumes:
  - ./config:/home/node/.openclaw       # 配置持久化
  - ./workspace:/workspace              # 工作目录持久化
  - {skills}:/skills:ro                 # 角色技能（只读）
  - {knowledge/global}:/knowledge/global:ro  # 全局知识库（只读）
  - {knowledge/project}:/knowledge/project:ro  # 项目知识库（只读）
  - /etc/localtime:/etc/localtime:ro    # 时区同步
```

## 项目生命周期

```
创建项目
  → 调度器生成配置文件
  → 启动 OpenClaw 容器
  → 分配任务，工程师工作
  → ...
  
归档项目
  → 调度器备份所有 Agent 的 workspace
  → 备份存储到 backups/{project_id}/{agent_id}/{timestamp}.tar.gz
  → 停止并删除 OpenClaw 容器
  → 项目状态标记为 archived
  
恢复项目（后期维护）
  → 重新生成配置文件
  → 启动新的 OpenClaw 容器
  → 从备份导入 workspace（含记忆体）
  → Agent 继续工作，上下文完整
  
销毁项目
  → 确认备份已保存
  → 删除部署目录
  → 清理 Milvus 项目级 Collection
  → 清理项目知识库目录
```

## 独立服务器调试环境

对于需要完整 Linux/Windows 环境的场景（编译、集成测试、数据库调试等）：

```
独立服务器（Linux/Windows，提前准备好环境）
  ├── Docker Engine + Compose（Linux）或 OpenSSH Server（Windows）
  ├── 开发工具链（编译器、运行时等）
  └── 通过 SSH 接入系统

使用方式：
  1. 在「基础设施」中注册服务器节点
  2. 配置 SSH 免密
  3. 调度器通过 SSH 在目标服务器上部署和管理 Agent
  4. 调试完成后结果推回 Git
```

适用于：
- 需要 `cap_add` 更多权限的任务
- 需要运行数据库、中间件等重服务
- 需要完整的系统调用能力
- 安全敏感的隔离环境
- Windows 目标部署环境（如工业应用）

## K8s 部署（后续扩展）

当需要更强的隔离性或跨机器扩展时：
- 每个 OpenClaw 实例拆为独立 Pod
- 通过 Service 发现和网络策略管理通信
- Milvus 使用集群模式
- 暂不需要，Docker Compose 足够个人使用
