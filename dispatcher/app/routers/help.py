from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from app.core.config import settings

router = APIRouter(prefix="/api/help", tags=["help"])

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"
IMAGES_DIRS = {
    "manual": DOCS_DIR / "manual-images",
    "demo": DOCS_DIR / "demo-images",
}
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
}


@router.get("/server-version")
async def get_server_version():
    """供 Web 展示当前 Dispatcher 发布版本（免登录，与 /health 字段一致）。"""
    return {
        "release_version": settings.VAITEAM_RELEASE_VERSION or None,
        "git_sha": settings.VAITEAM_GIT_SHA or None,
    }


@router.get("/quick-links")
async def get_quick_links():
    """获取快速链接和基础帮助信息"""
    content = """# 快速帮助

## VAI TEAM 是什么？

VAI TEAM 让你像管理一支真实开发团队一样指挥 AI 编码——你负责方向和决策，AI Agent 负责执行。详见「项目信息」标签页。

## 🚀 新手入门

如果您是第一次使用 VAI TEAM，建议按以下顺序操作：

1. **配置 AI 模型**：设置 → 模型供应商 → 添加 API Key
2. **分配角色模型**：设置 → 角色模型 → 为各角色选择模型
3. **创建第一个项目**：项目列表 → 新建项目
4. **查看完整教程**：[第一个项目完整指南](https://ai-orchestration.cn/tutorial/first-project.html)

## 📚 完整文档

- **官方文档**：[https://ai-orchestration.cn](https://ai-orchestration.cn)
- **快速开始**：[https://ai-orchestration.cn/docs/quickstart.html](https://ai-orchestration.cn/docs/quickstart.html)
- **概念说明**：[https://ai-orchestration.cn/docs/concepts/](https://ai-orchestration.cn/docs/concepts/)
- **配置指南**：[https://ai-orchestration.cn/docs/config/](https://ai-orchestration.cn/docs/config/)

## 🔧 常见问题

### Q: 如何添加 AI 模型？
进入「设置 → 模型供应商」，点击「添加供应商」，填入 API Key 和基础 URL。

### Q: 项目创建后没有反应？
检查「设置 → 角色模型」是否已为各角色分配了模型。

### Q: Agent 执行失败怎么办？
查看任务详情中的错误日志，通常是 API Key 配额不足或网络问题。

### Q: 如何查看项目进度？
在项目概览页可以看到 8 个阶段的完成情况和当前任务。

**更多问题**：[查看完整 FAQ](https://ai-orchestration.cn/docs/faq.html)

## 💬 获取支持

- **GitHub Issues**：[https://github.com/qingfengljh/vaiteam/issues](https://github.com/qingfengljh/vaiteam/issues)
- **Gitee Issues**：[https://gitee.com/qingfengljh/vaiteam/issues](https://gitee.com/qingfengljh/vaiteam/issues)
- **技术支持**：support@vaiteam.cn
"""
    return {"content": content}


@router.get("/about")
async def get_about():
    """获取项目信息、开源链接、社区支持等"""
    content = """# 关于 VAI TEAM

## 一句话定位

**VAI TEAM 将 Claude Code 这样的 AI 超级个体，编排成一个可指挥、可审核、可协作的超级开发团队。**

你不是在用一个 AI 工具写代码——你在领导一支 AI 开发团队。你制定方向、把控质量、做关键决策；AI Agent 按你的意图独立完成编码任务、提交代码、通过审核门控。

---

## 为什么是 VAI TEAM？

### 单打独斗 vs 团队作战

Claude Code、Cursor、Copilot 是强大的 AI 编程工具，但它们本质上是**个人工具**——一次只能做一件事，没有分工，没有审核，没有项目全局视角。

VAI TEAM 让多个 Claude Code 实例（以及兼容的编码 Agent）以**角色化团队**的形式协作：架构师负责方案设计与任务拆解，高级/中级/初级工程师各司其职完成编码，测试工程师验证质量，运维工程师处理部署。人（Boss）在关键节点审核与决策，而不是被 AI 替代。

### 一键黑盒 vs 人控编排

Devin、Bolt.new 等产品追求"输入一句话，输出整个应用"，适合快速原型。但当项目变得复杂——需要架构决策、代码审核、多人协作时，黑盒模式就会失控。

VAI TEAM 走另一条路：**人始终在回路中**。8 个阶段门控（商业方案→需求→原型→技术方案→任务拆解→编码→测试→部署），每个阶段由 AI 辅助但人可审核、可调整。不是"信任 AI 的魔法"，而是"用 AI 放大你的工程能力"。

### 通用 Agent vs 角色专精

一般的 AI Agent 平台提供通用对话能力，但**编码**这件事需要特定上下文：Git 分支、文件结构、依赖关系、代码规范、测试覆盖。VAI TEAM 的 Agent 是专门为软件工程设计的——每个 Agent 理解自己的角色边界、遵循分支规范、在指定的文件范围内工作。

---

## 典型场景

| 场景 | 适用方式 |
|------|---------|
| 技术负责人管理多个项目 | AI 团队并行推进不同模块，你审核关键决策点 |
| 独立开发者做全栈产品 | AI 架构师拆解需求→AI 工程师实现→你验证交付 |
| 小型团队缺资源 | AI 填补开发人力缺口，人类负责设计与审核 |
| 遗留系统重构 | AI 考古学家分析旧代码→架构师规划→工程师执行 |
| 快速原型验证 | 原型工坊从文档自动生成可交互 Mock |

---

## 核心特性

- **AI 角色团队** - 架构师、高级/中级/初级全栈工程师、测试、运维，各角色可一键拉起
- **8 阶段门控流程** - 从商业方案到部署交付，每阶段可审核、可中断、可回溯
- **Claude Code 原生集成** - 调用 Claude Code CLI 作为编码 Worker，支持 Anthropic 兼容的第三方模型
- **双层任务拆解** - AI Leader 模块级拆分 + 架构师编码级展开，batch 模式降低 API 成本
- **代码审核门控** - AI 自动审查 + 可选人工审查 + 架构师合并审批
- **知识沉淀系统** - 全局经验库 + 角色级知识访问控制，跨项目复用最佳实践
- **迭代管理** - 版本控制、变更请求、进度跟踪，符合真实软件项目管理习惯
- **多模型支持** - OpenAI / Anthropic / DeepSeek 等主流模型均可接入
- **Docker 一键部署** - 5 分钟启动完整开发环境
- **完全开源** - AGPL v3.0 许可，代码透明可审计

---

### 开源仓库

- **GitHub**: [https://github.com/qingfengljh/vaiteam](https://github.com/qingfengljh/vaiteam)
- **Gitee**: [https://gitee.com/qingfengljh/vaiteam](https://gitee.com/qingfengljh/vaiteam)

### 许可证

- **开源版本**: AGPL v3.0（个人和开源项目免费使用）
- **商业许可**: 企业商用需购买商业许可证

### 社区支持

- **GitHub Issues**: [提交问题](https://github.com/qingfengljh/vaiteam/issues)
- **Gitee Issues**: [提交问题](https://gitee.com/qingfengljh/vaiteam/issues)
- **官方网站**: [https://vaiteam.cn](https://vaiteam.cn)
- **技术文档**: [https://ai-orchestration.cn](https://ai-orchestration.cn)

### 联系我们

- **作者**: 青锋 ([@qingfengljh](https://github.com/qingfengljh))
- **商务合作**: business@vaiteam.cn
- **技术支持**: support@vaiteam.cn

### 快速开始

```bash
# GitHub
git clone https://github.com/qingfengljh/vaiteam.git
# Gitee（国内推荐）
# git clone https://gitee.com/qingfengljh/vaiteam.git

cd vaiteam
docker compose up -d
# 浏览器打开 http://localhost:3000
```

### 版本信息

- **当前版本**: v2.0
- **发布日期**: 2025年3月
- **更新日志**: 查看 [GitHub Releases](https://github.com/qingfengljh/vaiteam/releases)
"""
    return {"content": content}


@router.get("/images/{category}/{filename}")
async def get_help_image(category: str, filename: str):
    """提供帮助文档图片: /api/help/images/manual/xx.png 或 /api/help/images/demo/xx.png"""
    img_dir = IMAGES_DIRS.get(category)
    if not img_dir:
        raise HTTPException(404, "Category not found")
    safe_name = Path(filename).name
    file_path = img_dir / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(file_path, media_type=MEDIA_TYPES.get(file_path.suffix.lower(), "application/octet-stream"))


@router.get("/images/{filename}")
async def get_manual_image_compat(filename: str):
    """兼容旧路径: /api/help/images/xx.png → manual-images/"""
    safe_name = Path(filename).name
    file_path = IMAGES_DIRS["manual"] / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(file_path, media_type=MEDIA_TYPES.get(file_path.suffix.lower(), "application/octet-stream"))
