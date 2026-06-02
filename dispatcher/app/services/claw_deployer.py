"""
OpenClaw 实例部署器

根据角色生成 openclaw.json 配置和 docker-compose.yml，
然后通过 Docker API 或 SSH 部署到目标宿主机。

每个 OpenClaw 实例 = 一个项目中的一个角色。
部署目录：{deploy_root}/{project_id}/{role}/
"""

import json
import os
import secrets
import shutil
import logging
from datetime import datetime, timezone
from string import Template

from app.core.config import settings

logger = logging.getLogger(__name__)

OPENCLAW_VERSION = "cc-ubuntu-22.04-v1"
OPENCLAW_IMAGE = os.getenv("OPENCLAW_AGENT_IMAGE", f"openclaw/agent-runtime:{OPENCLAW_VERSION}")

ROLE_TOOL_PROFILES = {
    "architect": "full",
    "senior": "full",
    "mid": "full",
    "junior": "full",
    "devops": "full",
}

ROLE_DESCRIPTIONS = {
    "architect": "架构师 - 负责项目骨架、接口定义、代码审查、技术难题攻关",
    "senior": "高级工程师 - 全栈开发，处理复杂业务逻辑、架构级任务",
    "mid": "中级工程师 - 全栈开发，完成常规业务功能（含前后端+测试）",
    "junior": "初级工程师 - 全栈开发，完成简单 CRUD、配置、UI 调整",
    "devops": "运维工程师 - 负责 Dockerfile、CI/CD、部署脚本、监控",
}

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "docs")
APP_DIR = os.path.dirname(os.path.dirname(__file__))
SKILL_PACKS_DIR = os.path.join(APP_DIR, "skill_packs")
DEFAULT_SKILL_PACK = "programming"
DEPLOY_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "deploy"
)


# ── openclaw.json 生成（基于 docs/openclaw.json 模板） ──

def generate_openclaw_config(
    role: str,
    project_id: str,
    model_provider: str,
    model_id: str,
    gateway_token: str | None = None,
    api_base_url: str = "",
    api_key: str = "",
    allowed_origin: str = "",
) -> dict:
    """基于 docs/openclaw.json 模板生成 Agent 配置"""
    token = gateway_token or secrets.token_hex(24)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    template_path = os.path.join(DOCS_DIR, "openclaw.json")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

    # 覆盖动态字段
    config["wizard"] = {
        "lastRunAt": now,
        "lastRunVersion": OPENCLAW_VERSION,
        "lastRunCommand": "onboard",
        "lastRunMode": "local",
    }

    config["models"] = {
        "providers": {
            model_provider: {
                "baseUrl": api_base_url,
                "apiKey": api_key or f"{model_provider}-key",
                "api": "openai-completions",
                "models": [
                    {
                        "id": model_id,
                        "name": model_id,
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 128000,
                        "maxTokens": 8192,
                    }
                ],
            }
        }
    }

    config["agents"] = {
        "defaults": {
            "model": {"primary": f"{model_provider}/{model_id}"},
            "compaction": {"mode": "safeguard"},
            "maxConcurrent": 4,
            "subagents": {"maxConcurrent": 8},
        }
    }

    config["tools"] = {"profile": ROLE_TOOL_PROFILES.get(role, "coding")}

    config["gateway"] = {
        "port": 18789,
        "mode": "local",
        "bind": "lan",
        "controlUi": {
            "allowedOrigins": [allowed_origin] if allowed_origin else [],
            "allowInsecureAuth": True,
            "dangerouslyDisableDeviceAuth": True,
        },
        "auth": {"mode": "token", "token": token},
        "tailscale": {"mode": "off", "resetOnExit": False},
        "nodes": {
            "denyCommands": [
                "camera.snap", "camera.clip", "screen.record",
                "contacts.add", "calendar.add", "reminders.add", "sms.send",
            ]
        },
    }

    config.setdefault("messages", {"ackReactionScope": "group-mentions"})
    config.setdefault("commands", {"native": "auto", "nativeSkills": "auto", "restart": True, "ownerDisplay": "raw"})
    config.setdefault("session", {"dmScope": "per-channel-peer"})

    config["meta"] = {
        "lastTouchedVersion": OPENCLAW_VERSION,
        "lastTouchedAt": now,
    }

    return config


# ── docker-compose.yml 生成 ──

def generate_docker_compose(
    role: str,
    project_id: str,
    gateway_token: str,
    agent_id: str = "",
    container_name: str | None = None,
    port: int = 18789,
    host_ip: str = "AGENT_HOST_PLACEHOLDER",
    dispatcher_url: str = "http://DISPATCHER_HOST_PLACEHOLDER:8080",
    redis_url: str = "redis://DISPATCHER_HOST_PLACEHOLDER:6379/0",
    api_key_env: str = "ANTHROPIC_API_KEY",
    api_base_env: str = "ANTHROPIC_BASE_URL",
    model: str = "",
    supervisor_id: str = "",
    proxy_env: dict | None = None,
    git_repo: str = "",
) -> str:
    """生成单个 OpenClaw 实例的 docker-compose.yml（含 Connector）"""
    name = container_name or f"claw-{project_id[:8]}-{role}"
    aid = agent_id or f"{role}-{project_id[:8]}"
    webhook_url = f"http://{host_ip}:{port}"

    env_lines = [
        f"      {api_key_env}: ${{{api_key_env}}}",
        f"      {api_base_env}: ${{{api_base_env}:-https://api.anthropic.com}}",
        f"      OPENCLAW_HOME: /home/node",
        f"      AGENT_ID: {aid}",
        f"      PROJECT_ID: {project_id}",
        f"      AGENT_ROLE: {role}",
        f"      AGENT_MODEL: {model}",
        f"      DISPATCHER_URL: {dispatcher_url}",
        f"      REDIS_URL: {redis_url}",
        f"      WEBHOOK_URL: {webhook_url}",
        f"      SUPERVISOR_ID: {supervisor_id}",
        "      AGENT_REGISTRATION_MODE: heartbeat_lazy",
        "      ALLOW_DELAYED_INJECTION: '1'",
        "      HEARTBEAT_INTERVAL: '20'",
        "      POLL_INTERVAL: '10'",
    ]
    if git_repo:
        env_lines.append(f"      GIT_REPO_URL: {git_repo}")
    if proxy_env:
        for k, v in proxy_env.items():
            env_lines.append(f"      {k}: {v}")

    env_block = "\n".join(env_lines)

    return f"""services:
  {name}:
    image: {OPENCLAW_IMAGE}
    container_name: {name}
    restart: unless-stopped
    environment:
{env_block}
    volumes:
      - ./.openclaw:/home/node/.openclaw
      - ./connector:/opt/connector:ro
      - ./entrypoint.sh:/opt/entrypoint.sh:ro
      - /etc/localtime:/etc/localtime:ro
      - ./setup-env.sh:/opt/setup-env.sh:ro
      - ./git_ssh_key:/opt/git_ssh_key:ro
    tmpfs:
      - /tmp
    entrypoint: ["/bin/bash", "/opt/entrypoint.sh"]
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
        max-file: "5"

  claude-cli:
    image: {OPENCLAW_IMAGE}
    profiles: ["tools"]
    volumes:
      - ./.openclaw:/home/node/.openclaw
      - /etc/localtime:/etc/localtime:ro
    entrypoint: claude
"""


def generate_env_file(api_key: str, api_base: str = "https://api.147api.com") -> str:
    return f"""ANTHROPIC_API_KEY={api_key}
ANTHROPIC_BASE_URL={api_base}
"""


# ── 角色模板拆分 ──

def _split_role_template(role: str) -> tuple[str, str, str]:
    """从角色模板文件中拆分出 IDENTITY、SOUL、SKILLS"""
    pack = (os.getenv("OPENCLAW_SKILL_PACK") or DEFAULT_SKILL_PACK).strip() or DEFAULT_SKILL_PACK
    pack_roles_dir = os.path.join(SKILL_PACKS_DIR, pack, "roles")
    if not os.path.isdir(pack_roles_dir):
        logger.error("Skill pack roles dir not found: %s", pack_roles_dir)
        return "", "", ""
    template_path = os.path.join(pack_roles_dir, f"{role}.md")
    if not os.path.exists(template_path):
        return "", "", ""

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    sections = {"identity": "", "soul": "", "skills": ""}
    current = None

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "# IDENTITY":
            current = "identity"
            continue
        elif stripped == "# SOUL":
            current = "soul"
            continue
        elif stripped == "# SKILLS":
            current = "skills"
            continue
        elif stripped == "---":
            continue

        if current:
            sections[current] += line + "\n"

    return sections["identity"].strip(), sections["soul"].strip(), sections["skills"].strip()


TECH_STACK_PRESETS: dict[str, dict] = {
    "python": {
        "apk": ["python3", "py3-pip", "py3-virtualenv", "python3-dev", "libffi-dev", "openssl-dev"],
        "pip": ["uv", "httpie", "pytest"],
        "label": "Python",
    },
    "fastapi": {
        "pip": ["fastapi", "uvicorn[standard]", "sqlalchemy", "alembic", "pydantic", "httpx"],
        "requires": ["python"],
        "label": "FastAPI",
    },
    "django": {
        "pip": ["django", "djangorestframework", "django-cors-headers", "psycopg2-binary"],
        "requires": ["python"],
        "label": "Django",
    },
    "flask": {
        "pip": ["flask", "flask-sqlalchemy", "flask-cors"],
        "requires": ["python"],
        "label": "Flask",
    },
    "java": {
        "apk": ["openjdk17-jdk"],
        "label": "Java 17",
    },
    "springboot": {
        "apk": ["maven"],
        "requires": ["java"],
        "label": "Spring Boot (Maven)",
    },
    "gradle": {
        "requires": ["java"],
        "custom": r"""
if ! command -v gradle &>/dev/null; then
  GRADLE_VER=8.12
  wget -q "https://services.gradle.org/distributions/gradle-${GRADLE_VER}-bin.zip" -O /tmp/gradle.zip && \
    unzip -q /tmp/gradle.zip -d /opt && \
    ln -sf /opt/gradle-${GRADLE_VER}/bin/gradle /usr/local/bin/gradle && \
    rm /tmp/gradle.zip || echo "[setup-env] Gradle 安装失败，跳过"
fi
""",
        "label": "Gradle",
    },
    "vue": {
        "fnm": True,
        "label": "Vue.js",
    },
    "react": {
        "fnm": True,
        "label": "React",
    },
    "go": {
        "apk": ["go"],
        "label": "Go",
    },
    "rust": {
        "apk": ["rust", "cargo"],
        "label": "Rust",
    },
}

# 标准化输入：用户可能写 "Spring Boot"、"FastAPI"、"vue" 等各种格式
_STACK_ALIASES: dict[str, str] = {
    "spring": "springboot", "spring boot": "springboot", "spring-boot": "springboot",
    "fastapi": "fastapi", "fast-api": "fastapi", "fast api": "fastapi",
    "django": "django", "flask": "flask",
    "vue": "vue", "vuejs": "vue", "vue.js": "vue",
    "react": "react", "reactjs": "react", "react.js": "react",
    "python": "python", "py": "python",
    "java": "java", "jdk": "java",
    "springboot": "springboot",
    "gradle": "gradle",
    "go": "go", "golang": "go",
    "rust": "rust",
    "node": "vue", "nodejs": "vue", "node.js": "vue",
}


def _normalize_tech_stack(raw: str | list | None) -> list[str]:
    """将用户输入的技术栈标识规范化为内部 key 列表"""
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [s.strip().lower() for s in raw.replace(",", " ").replace("、", " ").replace("+", " ").split() if s.strip()]
    else:
        parts = [str(s).strip().lower() for s in raw if s]

    result: list[str] = []
    for p in parts:
        key = _STACK_ALIASES.get(p, p)
        if key in TECH_STACK_PRESETS and key not in result:
            result.append(key)
    return result


def _resolve_deps(stacks: list[str]) -> list[str]:
    """解析技术栈依赖，确保前置项先安装"""
    resolved: list[str] = []
    for s in stacks:
        preset = TECH_STACK_PRESETS.get(s)
        if not preset:
            continue
        for dep in preset.get("requires", []):
            if dep not in resolved:
                resolved.append(dep)
        if s not in resolved:
            resolved.append(s)
    return resolved


def generate_setup_env_script(tech_stack: str | list | None = None) -> str:
    """根据技术栈生成定制化的环境初始化脚本"""
    stacks = _normalize_tech_stack(tech_stack)
    if not stacks:
        stacks = ["python", "fastapi", "java", "springboot", "vue"]

    stacks = _resolve_deps(stacks)

    all_apk: list[str] = ["git", "curl", "wget", "bash", "jq", "tar", "unzip",
                           "gcc", "g++", "make", "musl-dev", "linux-headers", "docker-cli"]
    all_pip: list[str] = []
    custom_blocks: list[str] = []
    labels: list[str] = []

    for s in stacks:
        preset = TECH_STACK_PRESETS.get(s, {})
        all_apk.extend(preset.get("apk", []))
        all_pip.extend(preset.get("pip", []))
        if preset.get("custom"):
            custom_blocks.append(preset["custom"])
        if preset.get("label"):
            labels.append(preset["label"])

    # 去重保序
    seen_apk: set[str] = set()
    unique_apk = []
    for p in all_apk:
        if p not in seen_apk:
            seen_apk.add(p)
            unique_apk.append(p)

    seen_pip: set[str] = set()
    unique_pip = []
    for p in all_pip:
        if p not in seen_pip:
            seen_pip.add(p)
            unique_pip.append(p)

    lines = [
        "#!/bin/bash",
        f"# 项目环境初始化脚本 — 技术栈: {', '.join(labels)}",
        "# 容器首次启动时由 entrypoint.sh 调用",
        "",
        f'echo "[setup-env] 技术栈: {", ".join(labels)}"',
        "",
        "# 确保 community 仓库已启用",
        "if ! grep -q 'community' /etc/apk/repositories 2>/dev/null; then",
        "  ALPINE_VER=$(cat /etc/alpine-release 2>/dev/null | cut -d. -f1,2)",
        '  echo "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER:-3.20}/community" >> /etc/apk/repositories',
        "fi",
        "apk update",
        "",
    ]

    # apk 安装
    lines.append("apk add --no-cache \\")
    for i, pkg in enumerate(unique_apk):
        suffix = " \\" if i < len(unique_apk) - 1 else ""
        lines.append(f"  {pkg}{suffix}")
    lines.append('  || echo "[setup-env] 部分包安装失败，继续..."')
    lines.append("")

    # pip 安装
    if unique_pip:
        pip_pkgs = " ".join(f'"{p}"' if "[" in p else p for p in unique_pip)
        lines.extend([
            f"python3 -m pip install --break-system-packages --no-cache-dir \\",
            f"  {pip_pkgs} \\",
            '  2>&1 | tail -5 || true',
            "",
        ])

    # 自定义块
    for block in custom_blocks:
        lines.append(block.strip())
        lines.append("")

    # fnm (Fast Node Manager) — 前端技术栈需要隔离 Node 版本
    need_fnm = any(TECH_STACK_PRESETS.get(s, {}).get("fnm") for s in stacks)
    if need_fnm:
        lines.extend([
            '# fnm — 项目 Node.js 版本管理（不影响 OpenClaw 系统自带的 Node）',
            'FNM_DIR="/opt/fnm"',
            'if [ ! -f "$FNM_DIR/fnm" ]; then',
            '  echo "[setup-env] 安装 fnm..."',
            '  mkdir -p "$FNM_DIR"',
            '  curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir "$FNM_DIR" --skip-shell 2>&1 | tail -3 || true',
            'fi',
            'if [ -f "$FNM_DIR/fnm" ]; then',
            '  export PATH="$FNM_DIR:$PATH"',
            '  eval "$(fnm env --shell bash)"',
            '  # 安装项目推荐的 Node LTS（如果 workspace 有 .node-version 或 .nvmrc 则用那个）',
            '  WORKSPACE="$HOME/.openclaw/workspace"',
            '  if [ -f "$WORKSPACE/.node-version" ] || [ -f "$WORKSPACE/.nvmrc" ]; then',
            '    cd "$WORKSPACE" && fnm install && fnm use 2>&1 | tail -3 || true',
            '    echo "[setup-env] 项目 Node: $(fnm current 2>/dev/null)"',
            '  else',
            '    fnm install --lts 2>&1 | tail -3 || true',
            '    fnm default lts-latest 2>&1 || true',
            '    echo "[setup-env] Node LTS: $(fnm current 2>/dev/null)"',
            '  fi',
            '  # 写入 profile 供后续 shell 使用',
            '  PROFILE="$HOME/.bashrc"',
            '  if ! grep -q "fnm env" "$PROFILE" 2>/dev/null; then',
            '    echo \'export PATH="/opt/fnm:$PATH"\' >> "$PROFILE"',
            '    echo \'eval "$(fnm env --shell bash)"\' >> "$PROFILE"',
            '  fi',
            'fi',
            '',
        ])

    # 验证
    check_parts = []
    if any(s in stacks for s in ("python", "fastapi", "django", "flask")):
        check_parts.append("$(python3 --version 2>&1)")
    if any(s in stacks for s in ("java", "springboot")):
        check_parts.append("$(java -version 2>&1 | head -1)")
    if need_fnm:
        check_parts.append("fnm-node $(fnm current 2>/dev/null || echo 'N/A')")
    check_parts.append("system-node $(node --version 2>&1)")
    check_parts.append("$(git --version 2>&1)")

    lines.append(f'echo "[setup-env] 完成 — {", ".join(check_parts)}"')
    lines.append("")

    return "\n".join(lines)


# ── 部署目录准备 ──

def prepare_deploy_dir(
    deploy_root: str,
    project_id: str,
    role: str,
    openclaw_config: dict,
    compose_content: str,
    env_content: str = "",
    sub_dir: str = "",
    tech_stack: str | list | None = None,
    git_private_key: str = "",
) -> str:
    """准备部署目录，写入所有配置文件。sub_dir 默认为 role，可传 agent_id 以支持同角色多实例。"""
    deploy_dir = os.path.join(deploy_root, project_id, sub_dir or role)
    config_dir = os.path.join(deploy_dir, ".openclaw")
    connector_dir = os.path.join(deploy_dir, "connector")

    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(connector_dir, exist_ok=True)

    # openclaw.json
    with open(os.path.join(config_dir, "openclaw.json"), "w") as f:
        json.dump(openclaw_config, f, indent=2, ensure_ascii=False)

    # IDENTITY.md / SOUL.md / SKILLS.md → .openclaw/workspace/ 目录
    workspace_dir = os.path.join(config_dir, "workspace")
    project_workspace_dir = os.path.join(workspace_dir, project_id)
    os.makedirs(workspace_dir, exist_ok=True)
    os.makedirs(project_workspace_dir, exist_ok=True)
    identity, soul, skills = _split_role_template(role)
    for filename, content in [("IDENTITY.md", identity), ("SOUL.md", soul), ("SKILLS.md", skills)]:
        if content:
            with open(os.path.join(workspace_dir, filename), "w", encoding="utf-8") as f:
                f.write(content)

    # 人类可读路径：deploy_dir/workspace -> .openclaw/workspace/<project_id>
    # 便于定位代码目录，避免误把 deploy_dir/workspace 当作真实工作目录。
    workspace_link = os.path.join(deploy_dir, "workspace")
    if not os.path.exists(workspace_link):
        try:
            os.symlink(os.path.join(".openclaw", "workspace", project_id), workspace_link)
        except OSError:
            # 某些环境不支持符号链接，退化为普通目录（不影响运行）
            os.makedirs(workspace_link, exist_ok=True)

    # Connector 文件
    src_connector = os.path.join(DEPLOY_ASSETS_DIR, "connector")
    if os.path.isdir(src_connector):
        for fname in ["connector.mjs", "package.json"]:
            src = os.path.join(src_connector, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(connector_dir, fname))

    # entrypoint.sh
    src_entrypoint = os.path.join(DEPLOY_ASSETS_DIR, "entrypoint.sh")
    if os.path.exists(src_entrypoint):
        shutil.copy2(src_entrypoint, os.path.join(deploy_dir, "entrypoint.sh"))

    # setup-env.sh（项目级环境初始化脚本，根据技术栈定制）
    setup_env_path = os.path.join(deploy_dir, "setup-env.sh")
    with open(setup_env_path, "w") as f:
        f.write(generate_setup_env_script(tech_stack))

    # Git SSH 私钥：只使用项目级密钥（同项目所有 Agent 共享）
    ssh_dest = os.path.join(deploy_dir, "git_ssh_key")
    if (git_private_key or "").strip():
        with open(ssh_dest, "w", encoding="utf-8") as f:
            f.write(git_private_key)
        os.chmod(ssh_dest, 0o600)

    # docker-compose.yml
    with open(os.path.join(deploy_dir, "docker-compose.yml"), "w") as f:
        f.write(compose_content)

    # .env
    if env_content:
        with open(os.path.join(deploy_dir, ".env"), "w") as f:
            f.write(env_content)

    logger.info(
        f"Deploy dir prepared: {deploy_dir} "
        f"(IDENTITY: {'yes' if identity else 'no'}, "
        f"SOUL: {'yes' if soul else 'no'}, "
        f"SKILLS: {'yes' if skills else 'no'}, "
        f"Connector: {'yes' if os.path.isdir(src_connector) else 'no'})"
    )
    return deploy_dir


# ── 远程推送 ──

async def push_deploy_to_node(
    local_dir: str,
    host: str,
    remote_dir: str,
    user: str = "root",
    port: int = 22,
    key_file: str = "~/.ssh/id_rsa",
) -> dict:
    """通过 SSH+rsync 将本地部署目录推送到远程节点"""
    import asyncio

    resolved_key = os.path.expanduser((key_file or "").strip())
    if not resolved_key or not os.path.exists(resolved_key):
        return {
            "exit_code": -2,
            "stdout": "",
            "stderr": f"SSH key not found: {key_file}",
        }

    mkdir_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes",
        "-i", resolved_key, "-p", str(port),
        f"{user}@{host}", f"mkdir -p {remote_dir}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *mkdir_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    local_src = local_dir.rstrip("/") + "/"
    rsync_cmd = [
        "rsync", "-avz", "--delete",
        "--exclude", ".openclaw/sessions/",
        "--exclude", ".openclaw/memory/",
        "--exclude", ".openclaw/continuity/",
        "--exclude", ".openclaw/workspace/.task-result/",
        "--exclude", ".openclaw/workspace/.git/",
        "-e", f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o IdentitiesOnly=yes -i {resolved_key} -p {port}",
        local_src, f"{user}@{host}:{remote_dir}/",
    ]
    proc = await asyncio.create_subprocess_exec(
        *rsync_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode == 0:
        chown_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes",
            "-i", resolved_key, "-p", str(port),
            f"{user}@{host}",
            f"chown -R 1000:1000 {remote_dir}/.openclaw",
        ]
        chown_proc = await asyncio.create_subprocess_exec(
            *chown_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await chown_proc.communicate()
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
    }


async def push_connector_update(
    host: str,
    remote_dir: str,
    user: str = "root",
    port: int = 22,
    key_file: str = "~/.ssh/id_rsa",
) -> dict:
    """只更新 connector + entrypoint，不动 .openclaw 数据目录"""
    import asyncio

    src_connector = os.path.join(DEPLOY_ASSETS_DIR, "connector")
    src_entrypoint = os.path.join(DEPLOY_ASSETS_DIR, "entrypoint.sh")
    resolved_key = os.path.expanduser((key_file or "").strip())
    if not resolved_key or not os.path.exists(resolved_key):
        return {
            "exit_code": -2,
            "error": f"SSH key not found: {key_file}",
            "file": "",
        }

    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "IdentitiesOnly=yes",
        "-i", resolved_key,
        "-P", str(port),  # scp 端口参数必须是大写 -P
    ]
    files_to_push = []

    for fname in ["connector.mjs", "package.json"]:
        src = os.path.join(src_connector, fname)
        if os.path.exists(src):
            files_to_push.append((src, f"{remote_dir}/connector/{fname}"))

    if os.path.exists(src_entrypoint):
        files_to_push.append((src_entrypoint, f"{remote_dir}/entrypoint.sh"))

    for local_path, remote_path in files_to_push:
        scp_cmd = [
            "scp", *ssh_opts,
            local_path, f"{user}@{host}:{remote_path}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *scp_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return {"exit_code": proc.returncode, "error": stderr.decode(), "file": remote_path}

    return {"exit_code": 0, "updated_files": len(files_to_push)}
