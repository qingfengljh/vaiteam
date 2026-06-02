VALID_ROLES = {"leader", "architect", "senior", "mid", "junior", "devops", "tester"}

# TODO(deprecated): 旧角色名迁移映射，待清理历史数据后移除
# 新系统使用全栈角色体系 (architect/senior/mid/junior/devops/tester)
ROLE_MIGRATION = {"backend": "mid", "frontend": "mid", "fullstack": "mid"}

# ── 基础设施节点类型（连接方式） ──

INFRA_NODE_TYPES = {
    "linux":      "Linux 服务器",
    "windows":    "Windows 服务器",
    "kubernetes": "Kubernetes 集群",
}

# ── 基础设施节点角色（用途，一个节点可以有多个角色） ──
# key 是固定标识，代码中按 key 查找某类节点；value 是显示名称
# 增减角色只需修改此字典，前端从 API 动态加载

INFRA_NODE_ROLES = {
    "DEPLOY":  "目标部署",
    "AGENT":   "Agent 运行",
}

# 角色 → 健康检查配置（服务类角色需要）
INFRA_ROLE_HEALTH = {
    "OLLAMA": {
        "default_port": 11434,
        "health_path": "/api/tags",
        "url_config_key": "service_url",
    },
}

# 文档分类体系（多维度）
#
# 维度1: stage（已有）      — 文档产生于哪个阶段（0-7）
# 维度2: category（新增）   — 文档的本质类型（下方定义）
# 维度3: status（已有）     — 生命周期状态（draft/under_review/approved）
# 维度4: tags（新增）       — 自由标签（领域/技术栈，如 数据库、前端、安全）
#
# category 按文档的用途分类，与阶段无关

DOC_CATEGORIES = {
    # ── 规划类：描述"做什么、为什么做" ──
    "plan":             "方案规划",       # 业务方案、需求文档、产品原型
    "spec":             "规范标准",       # API 规范、代码风格、接口契约
    # ── 设计类：描述"怎么做" ──
    "design":           "架构设计",       # 技术方案、系统架构、数据库设计
    "decision":         "架构决策",       # ADR 记录、技术选型决策
    # ── 分析类：描述"现状是什么" ──
    "analysis":         "分析报告",       # 代码分析、性能分析、安全审计
    # ── 执行类：描述"做了什么、结果如何" ──
    "task":             "任务文档",       # 任务指令、执行报告
    "review":           "审查记录",       # 代码审查、文档评审
    "test":             "测试文档",       # 测试计划、测试报告
    "deploy":           "部署文档",       # 部署方案、运维手册
    # ── 过程类：项目管理和沟通 ──
    "log":              "过程日志",       # 错误日志、升级记录、变更日志
    "meeting":          "会议纪要",       # 讨论纪要、评审纪要
    "retrospective":    "项目复盘",       # 经验总结、复盘报告
    # ── 通用 ──
    "general":          "通用文档",       # 未分类
}

# 阶段文档的默认 category 映射
STAGE_DEFAULT_CATEGORY = {
    0: "plan",       # 业务方案
    1: "plan",       # 需求规范
    2: "plan",       # 产品原型
    3: "design",     # 技术方案
    4: "task",       # 任务分解
    5: "task",       # 编码执行
    6: "test",       # 测试
    7: "deploy",     # 部署上线
}

# TaskDocument.doc_type → category 的映射（统一旧数据）
TASK_DOCTYPE_TO_CATEGORY = {
    "architecture_decision": "decision",
    "task_instruction":      "task",
    "task_report":           "task",
    "error_log":             "log",
    "escalation_record":     "log",
    "code_review":           "review",
    "stage_document":        "general",
}
