"""
OpenClaw 配置加载

从 openclaw.json 读取 LLM 供应商、基础设施、CI/CD 等配置。
支持模板变量和环境变量覆盖。
"""

import json
import os
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_config: dict[str, Any] = {}

CONFIG_PATHS = [
    Path(__file__).parent.parent.parent / "config" / "openclaw.json",
    Path("/etc/openclaw/openclaw.json"),
]


def _resolve_env(value: str) -> str:
    """支持 ${ENV_VAR} 和 ${ENV_VAR:-default} 语法"""
    if not isinstance(value, str) or "${" not in value:
        return value
    import re
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        if ":-" in var:
            name, default = var.split(":-", 1)
            return os.environ.get(name, default)
        return os.environ.get(var, m.group(0))
    return re.sub(r"\$\{([^}]+)}", _replace, value)


def _resolve_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_recursive(v) for v in obj]
    return obj


def load(path: str | Path | None = None) -> dict[str, Any]:
    """加载配置文件，支持环境变量模板替换"""
    global _config

    if path:
        config_path = Path(path)
    else:
        config_path = None
        for p in CONFIG_PATHS:
            if p.exists():
                config_path = p
                break

    if not config_path or not config_path.exists():
        logger.warning("openclaw.json not found, using defaults from .env")
        _config = {}
        return _config

    raw = config_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    _config = _resolve_recursive(data)
    logger.info(f"Loaded openclaw config from {config_path}")
    return _config


def get(key: str, default: Any = None) -> Any:
    """点号分隔的路径取值：get('infrastructure.mode') -> 'docker-compose'"""
    obj = _config
    for part in key.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return default
        if obj is None:
            return default
    return obj


def get_llm_providers() -> dict:
    return _config.get("llm_providers", {})


def get_role_model_map() -> dict[str, str]:
    return _config.get("role_model_map", {})


def get_model_upgrade_chain() -> dict[str, str]:
    return _config.get("model_upgrade_chain", {})


def get_infra_mode() -> str:
    return get("infrastructure.mode", "docker-compose")


def get_infra_config() -> dict:
    mode = get_infra_mode()
    return get(f"infrastructure.{mode.replace('-', '_')}", {})


def get_gitlab_config() -> dict:
    return _config.get("gitlab", {})


def get_cicd_config() -> dict:
    return _config.get("cicd", {})


def save_role_model_map(mapping: dict[str, str]):
    """更新内存中的角色→模型映射（持久化由调用方写数据库完成）"""
    _config["role_model_map"] = mapping


def update_infra_ssh(host: str, port: int, user: str, key_file: str, project_dir: str = "", compose_file: str = "") -> bool:
    """更新 infrastructure.ssh 配置并持久化，供 Agent 启动使用。返回是否写入成功"""
    global _config
    if not project_dir:
        from app.core.config import settings
        project_dir = settings.AGENT_DEPLOY_ROOT
    if "infrastructure" not in _config:
        _config["infrastructure"] = {}
    _config["infrastructure"]["mode"] = "ssh"
    _config["infrastructure"]["ssh"] = {
        "host": host,
        "port": port,
        "user": user,
        "key_file": key_file,
        "project_dir": project_dir,
        "compose_file": compose_file or f"{project_dir}/docker-compose.agents.yml",
    }
    for p in CONFIG_PATHS:
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8")
                data = json.loads(raw)
            except Exception:
                data = {}
            data.setdefault("infrastructure", {})
            data["infrastructure"]["mode"] = "ssh"
            data["infrastructure"]["ssh"] = _config["infrastructure"]["ssh"]
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            from app.services import infra
            infra.init()
            return True
    return False
