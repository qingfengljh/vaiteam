import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, Text, DateTime, Boolean, ForeignKey, Index, Column, Table
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.core.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())[:8]


# ── 系统配置（KV） ──

class SystemConfig(Base):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ── 模型供应商 ──

class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    api_base: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # byok：用户自备 Key；platform：平台侧注入并托管，用户界面不填写、不展示真实 Key
    credential_source: Mapped[str] = mapped_column(String(16), default="byok")
    models: Mapped[list] = mapped_column(JSONB, default=list)
    model_prices: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"model_name": {"input": x, "output": y}}
    model_params: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"model_name": {"context_window": 128000, "max_output_tokens": 4096}}
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_multiplier: Mapped[float] = mapped_column(
        Float, default=1.0
    )  # 仅 credential_source=platform 时用于日志 cost × 倍率（可<1）；byok 不乘；见 token_tracker.apply_platform_billing_markup
    input_price_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)   # 供应商默认价（兜底）
    output_price_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    cache_read_price_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)  # prompt cache read ￥/M（与 input 同币种口径）
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ── Agent 工具供应商（独立协议）──
# 与 model_providers 完全独立：
# - model_providers: Dispatcher AI Leader 用 OpenAI 协议 (/v1/chat/completions)
# - agent_providers: Agent Worker 用各工具原生协议（如 Claude Code 用 Anthropic /v1/messages）
#
# 未来可扩展：claude_code | codex | ...
# 设计：角色 → Agent Provider → 模型映射
# - 不同角色可用完全不同的供应商（Senior→OpenRouter/Claude, Mid→DeepSeek, Junior→本地）
# - 每个 provider 内按能力等级(opus/sonnet/haiku)映射到实际模型名

class AgentProvider(Base):
    __tablename__ = "agent_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    # Agent 工具类型：claude_code（Anthropic 协议）| codex（OpenAI 协议）
    agent_type: Mapped[str] = mapped_column(String(32), default="claude_code")
    # ── 关联 ModelProvider（统一配置）──
    # 当 source_provider_id 有值时，api_base/api_key 优先从关联的 model_providers 读取
    # 实现「一套 Token 多处使用」：Dispatcher OpenAI + CC Worker Anthropic/Codex 共用
    source_provider_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("model_providers.id", ondelete="SET NULL"), nullable=True
    )
    source_provider: Mapped["ModelProvider | None"] = relationship(
        "ModelProvider", foreign_keys=[source_provider_id], lazy="selectin"
    )
    # ── 协议适配方式 ──
    # anthropic_direct: 供应商原生支持 Anthropic 协议（如 DeepSeek /anthropic 端点）
    # openai_via_litellm: 供应商仅支持 OpenAI 协议，CC Worker 容器内启动 litellm 代理转换
    # codex: 使用 OpenAI Codex（原生 OpenAI 协议，无需转换）
    protocol_adapter: Mapped[str] = mapped_column(String(32), default="anthropic_direct")
    # litellm 代理配置（仅 protocol_adapter=openai_via_litellm 时使用）
    # 如 {"proxy_model_name": "gpt-4o", "litellm_model_alias": "claude-sonnet"}
    litellm_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 以下字段在 source_provider_id 为空时必填；有值时从 model_providers 继承
    api_base: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # 认证字段名：写入 Agent 工具配置的环境变量名
    credential_env_name: Mapped[str] = mapped_column(String(64), default="ANTHROPIC_API_KEY")
    credential_source: Mapped[str] = mapped_column(String(16), default="byok")
    # 模型映射（按能力等级）：{"opus": "deepseek-chat", "sonnet": "deepseek-chat", "haiku": "deepseek-chat"}
    # key 为能力等级（opus=顶级, sonnet=高级, haiku=中初级），value 为供应商实际模型名
    model_mapping: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 当 Agent 请求的模型不在 mapping 中时使用的兜底模型
    default_model: Mapped[str] = mapped_column(String(100), default="")
    # 是否声明支持 1M 上下文（影响 Claude Code 的上下文策略）
    supports_1m_context: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ── 模型配置（独立表） ──

class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(String(32), ForeignKey("model_providers.id", ondelete="CASCADE"), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    input_price: Mapped[float] = mapped_column(Float, default=0.0)
    output_price: Mapped[float] = mapped_column(Float, default=0.0)
    cache_read_price: Mapped[float] = mapped_column(Float, default=0.0)  # ￥/M；0 表示未配，计费时可用环境倍率×input
    context_window: Mapped[int] = mapped_column(Integer, default=128000)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    vision_fallback: Mapped[str] = mapped_column(String(200), default="")
    capability_tier: Mapped[int] = mapped_column(Integer, default=3)  # 1=顶级 2=强 3=标准 4=基础
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("idx_mc_provider_model", "provider_id", "model_name", unique=True),
    )


# ── 项目 ──

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 浏览器路径 /{code}/... 用；小写、字母数字减号；与 id 分离，名称可中文
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_type: Mapped[str] = mapped_column(String(32), default="new")  # new | maintain | legacy_rewrite
    description: Mapped[str] = mapped_column(Text, default="")
    rewrite_reason: Mapped[str] = mapped_column(Text, default="")  # 旧系统重写原因/痛点
    target_tech_stack: Mapped[str] = mapped_column(Text, default="")  # 目标技术栈（重写时可选）
    status: Mapped[str] = mapped_column(String(32), default="planning")
    current_stage: Mapped[int] = mapped_column(Integer, default=0)
    current_iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_seq: Mapped[int] = mapped_column(Integer, default=0)
    git_repo: Mapped[str] = mapped_column(String(500), default="")
    git_web_url: Mapped[str] = mapped_column(String(500), default="")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    port_range_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    infra_group_id: Mapped[str | None] = mapped_column(ForeignKey("infra_groups.id", ondelete="SET NULL"), nullable=True)
    role_model_map: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # 访问窗口截止（UTC）；到期后 API 拒绝写操作类请求，引导用户走自备发布或续约流程
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    infra_group: Mapped["InfraGroup | None"] = relationship(lazy="selectin")
    iterations: Mapped[list["Iteration"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    stages: Mapped[list["StageProgress"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    teams: Mapped[list["AgentTeam"]] = relationship(cascade="all, delete-orphan")
    agents: Mapped[list["Agent"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    backups: Mapped[list["Backup"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    assets: Mapped[list["ProjectAsset"]] = relationship(cascade="all, delete-orphan")
    prototype_runs: Mapped[list["PrototypeRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


# ── 迭代 ──

class Iteration(Base):
    __tablename__ = "iterations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    start_stage: Mapped[int] = mapped_column(Integer, default=0)
    current_stage: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="planning")  # planning | active | completed | terminated
    parent_iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    release_branch: Mapped[str] = mapped_column(String(200), default="")
    release_tag: Mapped[str] = mapped_column(String(100), default="")
    release_status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped["Project"] = relationship(back_populates="iterations")

    __table_args__ = (
        Index("idx_iter_project", "project_id"),
        Index("idx_iter_project_seq", "project_id", "seq", unique=True),
    )


# ── 变更请求 ──

class ChangeRequest(Base):
    __tablename__ = "change_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iteration_id: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    impact_analysis: Mapped[dict] = mapped_column(JSONB, default=dict)
    decision: Mapped[str] = mapped_column(String(32), default="pending")  # pending | append | terminate_and_new | rejected
    new_iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    affected_tasks: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_cr_project", "project_id"),
        Index("idx_cr_iteration", "iteration_id"),
    )


class StageProgress(Base):
    __tablename__ = "stage_progress"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    documents: Mapped[dict] = mapped_column(JSONB, default=dict)
    review_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped["Project"] = relationship(back_populates="stages")

    __table_args__ = (
        Index("idx_stage_project_iter_stage", "project_id", "iteration_id", "stage", unique=True),
    )


# ── 任务 ──

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str] = mapped_column(String(32), default="")
    parent_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    type: Mapped[str] = mapped_column(String(32), default="feature")
    assigned_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    suggested_role: Mapped[str] = mapped_column(String(32), default="mid")
    suggested_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_tier: Mapped[int] = mapped_column(Integer, default=0)
    complexity: Mapped[str] = mapped_column(String(16), default="medium")
    estimated_hours: Mapped[float] = mapped_column(default=0.5)
    dependencies: Mapped[list] = mapped_column(JSONB, default=list)
    input_files: Mapped[list] = mapped_column(JSONB, default=list)
    output_files: Mapped[list] = mapped_column(JSONB, default=list)
    acceptance_criteria: Mapped[list] = mapped_column(JSONB, default=list)
    ref_docs: Mapped[list] = mapped_column(JSONB, default=list)
    git_branch: Mapped[str] = mapped_column(String(200), default="")
    git_commits: Mapped[list] = mapped_column(JSONB, default=list)
    merge_status: Mapped[str] = mapped_column(String(32), default="pending")
    merge_commit: Mapped[str] = mapped_column(String(64), default="")
    test_status: Mapped[str] = mapped_column(String(32), default="pending")
    test_results: Mapped[list] = mapped_column(JSONB, default=list)
    result: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    escalation_history: Mapped[list] = mapped_column(JSONB, default=list)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    requires_design_review: Mapped[bool] = mapped_column(Boolean, default=False)
    design_conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    design_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    design_approved_by: Mapped[str] = mapped_column(String(100), default="")
    design_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped["Project"] = relationship(back_populates="tasks")
    logs: Mapped[list["TaskLog"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    comments: Mapped[list["TaskComment"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_tasks_project", "project_id"),
        Index("idx_tasks_iteration", "project_id", "iteration_id"),
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_agent", "assigned_agent"),
        Index("idx_tasks_ref_id", "project_id", "ref_id"),
    )


# ── 开发小组 ──

class AgentTeam(Base):
    __tablename__ = "agent_teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agents: Mapped[list["Agent"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    module_task_ids: Mapped[list] = mapped_column(JSONB, default=list)
    default_review_policy: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("idx_team_project", "project_id"),
    )


# ── Agent（项目级） ──

class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("agent_teams.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="idle")
    current_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    container_id: Mapped[str] = mapped_column(String(128), default="")
    workspace_path: Mapped[str] = mapped_column(String(500), default="")
    webhook_url: Mapped[str] = mapped_column(String(500), default="")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_status: Mapped[str] = mapped_column(String(32), default="offline")
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_restart_count: Mapped[int] = mapped_column(Integer, default=0)
    supervisor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="agents")
    team: Mapped["AgentTeam | None"] = relationship(back_populates="agents")

    __table_args__ = (
        Index("idx_agents_project", "project_id"),
        Index("idx_agents_team", "team_id"),
        Index("idx_agents_supervisor", "supervisor_id"),
    )


# ── Agent 启动自报与重注入记录 ──

class AgentBootReport(Base):
    __tablename__ = "agent_boot_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(128), default="")
    session_fingerprint: Mapped[str] = mapped_column(String(256), default="")
    recovery_mode: Mapped[str] = mapped_column(String(32), default="fast_resume")
    retriever_ready: Mapped[bool] = mapped_column(Boolean, default=True)
    context_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_boot_reports_agent", "agent_id"),
        Index("idx_boot_reports_project", "project_id"),
        Index("idx_boot_reports_created", "created_at"),
    )


class AgentRehydrationJob(Base):
    __tablename__ = "agent_rehydration_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default="partial_rehydrate")
    reason: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_rehydrate_agent", "agent_id"),
        Index("idx_rehydrate_project", "project_id"),
        Index("idx_rehydrate_status", "status"),
        Index("idx_rehydrate_created", "created_at"),
    )


# ── 备份记录 ──

class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    backup_type: Mapped[str] = mapped_column(String(32), default="workspace")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="backups")

    __table_args__ = (
        Index("idx_backups_project", "project_id"),
    )


# ── 原型工坊 CC 运行记录（与 task-pack / webhook 对齐；不入核心任务状态机）──

class PrototypeRun(Base):
    __tablename__ = "prototype_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    prototype_document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    technical_document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="prototype_runs")

    __table_args__ = (
        Index("idx_proto_runs_project", "project_id"),
        Index("idx_proto_runs_status", "status"),
        Index("idx_proto_runs_created", "created_at"),
    )


# ── 日志 ──

class TaskLog(Base):
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped["Task"] = relationship(back_populates="logs")

    __table_args__ = (
        Index("idx_task_logs_task", "task_id"),
    )


# ── Token 消耗记录 ──

class TokenUsageLog(Base):
    __tablename__ = "token_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caller: Mapped[str] = mapped_column(String(64), nullable=False)  # leader/agent/embedding
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_token_project", "project_id"),
        Index("idx_token_task", "task_id"),
        Index("idx_token_model", "model"),
        Index("idx_token_caller", "caller"),
        Index("idx_token_created", "created_at"),
    )


# ── 全局经验库 ──

class Experience(Base):
    __tablename__ = "experiences"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    tech_stack: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    problem: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    solution: Mapped[str] = mapped_column(Text, default="")
    code_snippet: Mapped[str] = mapped_column(Text, default="")
    source_project: Mapped[str] = mapped_column(String(200), default="")
    source_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # 知识治理：状态机
    status: Mapped[str] = mapped_column(String(32), default="published")
    # draft | reviewed | published | deprecated | archived

    # 知识治理：分类体系
    domain: Mapped[str] = mapped_column(String(64), default="")  # 架构设计/代码规范/运维部署/安全合规/业务逻辑
    type: Mapped[str] = mapped_column(String(64), default="experience")  # 事实型/规则型/经验型/故障型/决策型
    scope: Mapped[str] = mapped_column(String(64), default="global")  # 全局/团队/项目/模块
    freshness: Mapped[str] = mapped_column(String(32), default="permanent")  # 持久/中期/临时
    tech_domain: Mapped[str] = mapped_column(String(64), default="")  # frontend/backend/database/infrastructure/language/general

    # 知识治理：版本与有效期
    version_range: Mapped[str] = mapped_column(String(200), default="")  # 适用版本范围
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 知识治理：权限分级（Phase 3-6）
    # 0=公开(所有角色) | 1=内部(senior+) | 2=敏感(architect+) | 3=机密(architect+human)
    access_level: Mapped[int] = mapped_column(Integer, default=0)

    # 经验关联图谱（Phase 4-5）
    related_exp_ids: Mapped[list] = mapped_column(JSONB, default=list)  # 关联的经验 ID 列表

    tsv = Column(TSVECTOR)
    embedding = mapped_column(Vector(1536), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("idx_exp_category", "category"),
        Index("idx_exp_tech_stack", "tech_stack", postgresql_using="gin"),
        Index("idx_exp_tags", "tags", postgresql_using="gin"),
        Index("idx_exp_keywords", "keywords", postgresql_using="gin"),
        Index("idx_exp_quality", "quality_score"),
        Index("idx_exp_tsv", "tsv", postgresql_using="gin"),
        Index("idx_exp_status", "status"),
        Index("idx_exp_domain", "domain"),
        Index("idx_exp_type", "type"),
        Index("idx_exp_scope", "scope"),
        Index("idx_exp_freshness", "freshness"),
        Index("idx_exp_access_level", "access_level"),
    )


# ── 项目资料（代码/API规范） ──

class ProjectAsset(Base):
    __tablename__ = "project_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)  # code | api_spec
    purpose: Mapped[str] = mapped_column(String(32), default="")  # maintain | learn_style (仅 code)
    filename: Mapped[str] = mapped_column(String(500), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="uploaded")  # uploaded | analyzed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_asset_project", "project_id"),
        Index("idx_asset_type", "project_id", "asset_type"),
    )


# ── 阶段文档 ──

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="general")
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    review_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    generated_model: Mapped[str] = mapped_column(String(128), default="")
    git_path: Mapped[str] = mapped_column(String(500), default="")

    tsv = Column(TSVECTOR)
    embedding = mapped_column(Vector(1536), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("idx_doc_project_iter_stage", "project_id", "iteration_id", "stage"),
        Index("idx_doc_status", "status"),
        Index("idx_doc_category", "category"),
        Index("idx_doc_tags", "tags", postgresql_using="gin"),
        Index("idx_doc_tsv", "tsv", postgresql_using="gin"),
    )


# ── 文档生成任务（异步长任务） ──

class GenerationTask(Base):
    __tablename__ = "generation_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_title: Mapped[str] = mapped_column(String(500), default="")
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    model_used: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_gentask_project_iter_stage", "project_id", "iteration_id", "stage"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_messages_project_iter_stage", "project_id", "iteration_id", "stage"),
    )


# ── 任务评论 ──

class TaskComment(Base):
    __tablename__ = "task_comments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    author: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    comment_type: Mapped[str] = mapped_column(String(32), default="discussion")
    attachments: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped["Task"] = relationship(back_populates="comments")

    __table_args__ = (
        Index("idx_task_comments_task", "task_id"),
    )


# ── 过程文档索引（文件系统 + 向量检索） ──

class TaskDocument(Base):
    __tablename__ = "task_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iteration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str] = mapped_column(String(32), default="")

    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    tsv = Column(TSVECTOR)
    embedding = mapped_column(Vector(1536), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_taskdoc_project", "project_id"),
        Index("idx_taskdoc_project_iter", "project_id", "iteration_id"),
        Index("idx_taskdoc_task", "task_id"),
        Index("idx_taskdoc_type", "doc_type"),
        Index("idx_taskdoc_tags", "tags", postgresql_using="gin"),
        Index("idx_taskdoc_keywords", "keywords", postgresql_using="gin"),
        Index("idx_taskdoc_tsv", "tsv", postgresql_using="gin"),
    )


# ── Agent 间通信消息 ──

class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    from_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_id: Mapped[str] = mapped_column(String(64), nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    ref_msg_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / replied / expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_amsg_task", "task_id"),
        Index("idx_amsg_project", "project_id"),
        Index("idx_amsg_to", "to_id"),
        Index("idx_amsg_ref", "ref_msg_id"),
        Index("idx_amsg_status", "status"),
    )


# ── 团队群聊 ──

class TeamChat(Base):
    __tablename__ = "team_chats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)  # human | agent
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mentions: Mapped[list] = mapped_column(JSONB, default=list)
    task_ref: Mapped[str] = mapped_column(String(32), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | delivered | replied | failed
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_tc_project", "project_id"),
        Index("idx_tc_project_created", "project_id", "created_at"),
        Index("idx_tc_task", "task_ref"),
    )


# ── 基础设施 ──

# 环境组 ↔ 节点 多对多中间表（带角色）
class InfraGroupNode(Base):
    __tablename__ = "infra_group_nodes"
    group_id: Mapped[str] = mapped_column(String(32), ForeignKey("infra_groups.id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("infra_nodes.id", ondelete="CASCADE"), primary_key=True)
    roles: Mapped[list] = mapped_column(JSONB, default=lambda: ["AGENT"])


class InfraGroup(Base):
    """环境组：一组节点的部署环境，可被多个项目共用"""
    __tablename__ = "infra_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    purpose: Mapped[str] = mapped_column(String(32), default="agent")  # agent | deploy
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    node_assocs: Mapped[list["InfraGroupNode"]] = relationship(foreign_keys=[InfraGroupNode.group_id], lazy="selectin")
    nodes: Mapped[list["InfraNode"]] = relationship(secondary=InfraGroupNode.__table__, back_populates="groups", lazy="selectin", viewonly=True)


class InfraNode(Base):
    """基础设施节点：VM / Docker 宿主机 / K8s 集群 / GitLab"""
    __tablename__ = "infra_nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="vm")
    host: Mapped[str] = mapped_column(String(200), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22)
    user: Mapped[str] = mapped_column(String(64), default="root")
    auth_method: Mapped[str] = mapped_column(String(32), default="password")
    status: Mapped[str] = mapped_column(String(32), default="unconfigured")
    ssh_key_path: Mapped[str] = mapped_column(String(500), default="")
    roles: Mapped[list] = mapped_column(JSONB, default=lambda: ["AGENT"])
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_connected: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    groups: Mapped[list["InfraGroup"]] = relationship(secondary=InfraGroupNode.__table__, back_populates="nodes", lazy="selectin", viewonly=True)


# ── 通用文件上传记录 ──

class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    uploader: Mapped[str] = mapped_column(String(100), default="human")
    original_name: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[str] = mapped_column(String(32), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    is_image: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_upload_project", "project_id"),
        Index("idx_upload_created", "created_at"),
    )


# ── 深度对话（私聊） ──

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | archived
    conclusion_doc: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages: Mapped[list["ConversationMessage"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_conv_project", "project_id"),
        Index("idx_conv_agent", "agent_id"),
        Index("idx_conv_task", "task_id"),
        Index("idx_conv_status", "status"),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)  # human | agent | system
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    file_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("idx_cmsg_conversation", "conversation_id"),
        Index("idx_cmsg_created", "created_at"),
    )


# ── 失败模式（负样本库）──

class FailurePattern(Base):
    """负样本库：记录「什么做法不行」比「什么做法行」更有价值。

    Worker 遇到已知失败模式时，系统能主动提醒避免。
    从任务 retry 前的失败历史中自动提取。
    """
    __tablename__ = "failure_patterns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 失败分类
    pattern_type: Mapped[str] = mapped_column(String(64), default="")  # syntax_error | runtime_error | logic_error | dependency_conflict | config_error | test_failure | performance_issue
    tech_stack: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)

    # 失败内容
    failure_symptom: Mapped[str] = mapped_column(Text, default="")  # 失败现象/错误信息
    root_cause: Mapped[str] = mapped_column(Text, default="")  # 根本原因
    failed_approach: Mapped[str] = mapped_column(Text, default="")  # 尝试了但失败的方法
    successful_approach: Mapped[str] = mapped_column(Text, default="")  # 最终成功的方法（对比）

    # 检索支持
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    tsv = Column(TSVECTOR)
    embedding = mapped_column(Vector(1536), nullable=True)

    # 治理
    status: Mapped[str] = mapped_column(String(32), default="published")  # published | deprecated
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)

    # 来源追溯
    source_experience_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 关联的正面经验
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("idx_fp_project", "project_id"),
        Index("idx_fp_type", "pattern_type"),
        Index("idx_fp_tech_stack", "tech_stack", postgresql_using="gin"),
        Index("idx_fp_tags", "tags", postgresql_using="gin"),
        Index("idx_fp_keywords", "keywords", postgresql_using="gin"),
        Index("idx_fp_tsv", "tsv", postgresql_using="gin"),
        Index("idx_fp_status", "status"),
    )
