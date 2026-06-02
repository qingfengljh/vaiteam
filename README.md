# VAI TEAM — AI 编程团队编排平台

**你的 AI 开发团队，由你指挥。**

VAI TEAM 让一个人管理一支 AI 工程团队。你提供需求与决策，Dispatcher 自动拆解任务、分配给 AI Agent 并行编码、架构师自动审核代码。

## 开源范围

本仓库包含 VAI TEAM 的开源核心：

| 目录 | 说明 |
|------|------|
| `dispatcher/` | AI 编排引擎（FastAPI），项目/阶段/任务生命周期管理、AI 任务分解与审核 |
| `agents/cc-worker/` | Claude Code Worker，Docker 容器内的无人值守 AI 编码执行器 |

## 快速开始

```bash
# 安装依赖
cd dispatcher && pip install -r requirements.txt

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
