from pydantic import BaseModel
from pydantic_settings import BaseSettings


class ModelProvider(BaseModel):
    """一个模型供应商配置"""
    name: str
    api_base: str
    api_key: str
    models: list[str] = []
    is_default: bool = False


class Settings(BaseSettings):
    APP_NAME: str = "AI Dev Team Dispatcher"
    DATABASE_URL: str = "postgresql+asyncpg://devteam:devteam@localhost:5432/devteam"
    OPENCLAW_GATEWAY_URL: str = "http://localhost:18789"
    OPENCLAW_HOOK_TOKEN: str = "changeme"

    # 认证：凭据在 DB system_configs.auth_credentials；无行时不再默认 admin/admin（见 auth._get_credentials）
    JWT_SECRET: str = "please-change-this-secret-key"
    # 首次空库时写入 admin 密码（仅当 DB 尚无 auth_credentials）；单机/自举须配置；SaaS 由安装脚本注入或与 Portal set-admin-password 先后均可
    VAITEAM_INITIAL_ADMIN_PASSWORD: str = ""
    # Portal 调用 /api/internal/portal/set-admin-password 时携带的共享密钥；不配置则拒绝该接口
    VAITEAM_PORTAL_SERVICE_TOKEN: str = ""
    JWT_EXPIRE_HOURS: int = 72
    # 部署展示：与制品 release_version 对齐时填写（供 Web 登录页/侧栏与 /health 展示）
    VAITEAM_RELEASE_VERSION: str = ""
    # 可选：构建时注入的短 SHA，便于区分同版本不同构建
    VAITEAM_GIT_SHA: str = ""

    # 项目自创建起可使用的公网/API 访问窗口（天）；用于测试与合规边界，非「正式托管发布」承诺
    PROJECT_ACCESS_DAYS: int = 30
    AUTH_CAPTCHA_TTL_SECONDS: int = 300
    AUTH_LOGIN_MAX_FAILED_ATTEMPTS: int = 5
    AUTH_LOGIN_LOCK_SECONDS: int = 900
    # 登录锁定时 429 提示里展示的本地时区（IANA）；空字符串则只写 UTC。默认 Asia/Shanghai 便于国内用户对照手表时间
    AUTH_LOGIN_LOCK_MESSAGE_TIMEZONE: str = "Asia/Shanghai"
    # 忘记密码邮件内链接的站点根（须含协议）；空则按请求 X-Forwarded-Proto / Host 推断
    AUTH_PASSWORD_RESET_PUBLIC_BASE_URL: str = ""
    AUTH_PASSWORD_RESET_TOKEN_TTL_SECONDS: int = 3600
    AUTH_FORGOT_PASSWORD_MAX_PER_HOUR_PER_IP: int = 8

    # 与 Portal 对齐的 SMTP（工作台「忘记密码」发重置链接；未启用则相关接口返回说明）
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "VAI TEAM"
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    SMTP_TIMEOUT_SECONDS: int = 15

    # 逗号分隔的完整 Host（不含端口），如 demo.vaiteam.cn。**默认为空**：不命中任何演示站，须部署时显式填写。
    DEMO_BYPASS_LOGIN_HOSTS: str = ""
    # demo-hints：未在库内配置终端门哈希时，展示该占位说明（与真实终端校验无关，以库内为准）
    DEMO_PUBLIC_TERMINAL_GATE_PASSWORD: str = "admin"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # 备份存储根目录（需 volume 映射到宿主机）
    BACKUP_DIR: str = "/var/backups"
    # 单次备份上传最大大小（字节），默认 5GB
    BACKUP_MAX_SIZE: int = 5 * 1024 * 1024 * 1024

    KNOWLEDGE_DIR: str = "../knowledge"
    PROJECTS_DIR: str = "../projects"
    AGENT_DEPLOY_ROOT: str = "/opt/openclaw-agents"
    # 容器内 Worker 回调 Dispatcher、拉 task-pack 须用浏览器可达的公网/内网基址（含协议与端口）
    DISPATCHER_PUBLIC_BASE_URL: str = ""
    # 原型 CC 远程执行：`runs/start` 在环境组节点上 docker compose 拉起该镜像（须含 run_cc_wrapper.sh / claude 等由镜像构建决定）
    PROTOTYPE_CC_WORKER_IMAGE: str = "openclaw/prototype-cc-worker:latest"

    # Ollama 服务（Embedding）
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_EMBEDDING_MODEL: str = "bge-m3"
    OLLAMA_ENABLED: bool = True

    # 知识图谱（codebase-memory-mcp）数据库路径；空字符串则禁用图谱查询
    CODEBASE_MEMORY_DB_PATH: str = ""

    @property
    def ollama_embedding_url(self) -> str:
        return f"{self.OLLAMA_BASE_URL}/v1"

    # 任务重试与升级
    TASK_MAX_RETRIES: int = 2  # 每个层级最大重试次数（编码AI 2次 → 架构师 2次 → 人类）

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
