from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_session
from app.models import ModelProvider, ModelConfig, SystemConfig
from app.services import model_pool, model_config_notice
from app.services import token_tracker

router = APIRouter(prefix="/api/providers", tags=["model-providers"])


def _mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    prefix = "sk-" if key.startswith("sk-") else ""
    body = key[3:] if prefix else key
    if not body:
        return f"{prefix}***"
    start = body[:4]
    end = body[-2:] if len(body) >= 2 else ""
    return f"{prefix}{start}***{end}"


class ModelConfigDTO(BaseModel):
    model_name: str
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_price: float = 0.0
    context_window: int = 128000
    max_output_tokens: int = 4096
    supports_vision: bool = False
    vision_fallback: str = ""
    capability_tier: int = 3
    enabled: bool = True


class ProviderCreate(BaseModel):
    name: str
    api_base: str
    api_key: str = ""
    credential_source: str = "byok"  # byok | platform
    models: list[str] = []
    model_configs: list[ModelConfigDTO] = []
    model_prices: dict[str, dict[str, float]] = {}
    model_params: dict[str, dict] = {}
    is_default: bool = False
    enabled: bool = True
    # platform 托管：token_tracker.record 在 calc_cost 后再乘该倍率（可<1，如 0.75/0.45）；byok 不乘，仅作估算
    cost_multiplier: float = 1.0
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    cache_read_price_per_mtok: float = 0.0
    notes: str = ""


async def _sync_model_configs(session: AsyncSession, provider_id: str, configs: list[ModelConfigDTO], models: list[str]):
    """同步模型配置到 model_configs 表"""
    existing_q = await session.execute(
        select(ModelConfig).where(ModelConfig.provider_id == provider_id)
    )
    existing = {mc.model_name: mc for mc in existing_q.scalars()}

    seen = set()
    for cfg in configs:
        seen.add(cfg.model_name)
        if cfg.model_name in existing:
            mc = existing[cfg.model_name]
            for k, v in cfg.model_dump(exclude={"model_name"}).items():
                setattr(mc, k, v)
        else:
            mc = ModelConfig(provider_id=provider_id, **cfg.model_dump())
            session.add(mc)

    for model in models:
        if model not in seen and model not in existing:
            session.add(ModelConfig(provider_id=provider_id, model_name=model))

    for name, mc in existing.items():
        if name not in seen and name not in models:
            await session.delete(mc)


@router.post("")
async def create_provider(body: ProviderCreate, session: AsyncSession = Depends(get_session)):
    src = (body.credential_source or "byok").strip().lower()
    if src not in ("byok", "platform"):
        raise HTTPException(400, detail="credential_source 须为 byok 或 platform")
    if src == "byok" and not (body.api_key or "").strip():
        raise HTTPException(400, detail="自备 Key 时须填写 api_key")
    if src == "platform" and not (body.api_key or "").strip():
        raise HTTPException(
            400,
            detail="平台托管供应商创建时仍须在服务端提供 api_key（或由开通脚本写入）；用户端不应收集该密钥",
        )
    data = body.model_dump(exclude={"model_configs", "model_prices", "model_params"})
    data["credential_source"] = src
    p = ModelProvider(**data)
    session.add(p)
    await session.flush()

    configs = body.model_configs
    if not configs and body.models:
        configs = _configs_from_legacy(
            body.models, body.model_prices, body.model_params,
            body.input_price_per_mtok, body.output_price_per_mtok, body.cache_read_price_per_mtok,
        )
    await _sync_model_configs(session, p.id, configs, body.models)

    await session.commit()
    await model_pool.load_providers(session)
    await model_config_notice.notify_model_config_changed(
        session,
        scope="global_default",
        reason="provider_created",
    )
    return {"id": p.id, "name": p.name}


@router.get("")
async def list_providers(session: AsyncSession = Depends(get_session)):
    q = await session.execute(select(ModelProvider).order_by(ModelProvider.name))
    result = []
    for p in q.scalars():
        mc_q = await session.execute(
            select(ModelConfig).where(ModelConfig.provider_id == p.id).order_by(ModelConfig.model_name)
        )
        configs = [{
            "model_name": mc.model_name,
            "input_price": mc.input_price,
            "output_price": mc.output_price,
            "cache_read_price": getattr(mc, "cache_read_price", 0) or 0,
            "context_window": mc.context_window,
            "max_output_tokens": mc.max_output_tokens,
            "supports_vision": mc.supports_vision,
            "vision_fallback": mc.vision_fallback,
            "capability_tier": mc.capability_tier,
            "enabled": mc.enabled,
        } for mc in mc_q.scalars()]

        csrc = (getattr(p, "credential_source", None) or "byok").strip().lower()
        masked = _mask_api_key(p.api_key)
        if csrc == "platform":
            masked = "平台托管（密钥由平台注入，无需在此填写）"
        result.append({
            "id": p.id, "name": p.name, "api_base": p.api_base,
            "credential_source": csrc,
            "models": p.models,
            "model_configs": configs,
            "api_key_masked": masked,
            "is_default": p.is_default,
            "enabled": p.enabled, "cost_multiplier": p.cost_multiplier,
            "input_price_per_mtok": p.input_price_per_mtok,
            "output_price_per_mtok": p.output_price_per_mtok,
            "cache_read_price_per_mtok": getattr(p, "cache_read_price_per_mtok", 0) or 0,
            "notes": p.notes,
        })
    return result


def _configs_from_legacy(
    models, prices, params, default_input, default_output, default_cache_read: float = 0.0,
) -> list[ModelConfigDTO]:
    """兼容旧格式：从 model_prices + model_params 构造 ModelConfigDTO"""
    configs = []
    for m in models:
        mp = prices.get(m, {})
        pp = params.get(m, {})
        cr = mp.get("cache_read", mp.get("cache_read_price", default_cache_read))
        try:
            cr = float(cr or 0)
        except (TypeError, ValueError):
            cr = 0.0
        configs.append(ModelConfigDTO(
            model_name=m,
            input_price=mp.get("input", default_input),
            output_price=mp.get("output", default_output),
            cache_read_price=cr,
            context_window=pp.get("context_window", 128000),
            max_output_tokens=pp.get("max_output_tokens", 4096),
            supports_vision=bool(pp.get("supports_vision", False)),
            vision_fallback=pp.get("vision_fallback", ""),
            capability_tier=pp.get("capability_tier", 3),
        ))
    return configs


from app.core.constants import VALID_ROLES as _VALID_ROLES


@router.get("/role-model-map")
async def get_role_model_map():
    return {k: v for k, v in model_pool.ROLE_MODEL_MAP.items() if k in _VALID_ROLES}


class RoleModelMapUpdate(BaseModel):
    mapping: dict[str, str]


@router.put("/role-model-map")
async def update_role_model_map(body: RoleModelMapUpdate, session: AsyncSession = Depends(get_session)):
    cleaned = {k: v for k, v in body.mapping.items() if k in _VALID_ROLES}
    model_pool.ROLE_MODEL_MAP.update(cleaned)
    # 清除内存中残留的旧角色
    for old_key in list(model_pool.ROLE_MODEL_MAP):
        if old_key not in _VALID_ROLES:
            del model_pool.ROLE_MODEL_MAP[old_key]
    from app.models import SystemConfig
    cfg = await session.get(SystemConfig, "role_model_map")
    if cfg:
        cfg.value = dict(model_pool.ROLE_MODEL_MAP)
    else:
        session.add(SystemConfig(key="role_model_map", value=dict(model_pool.ROLE_MODEL_MAP)))
    await session.commit()
    await model_config_notice.notify_model_config_changed(
        session,
        scope="global_default",
        reason="role_model_map_updated",
    )
    return model_pool.ROLE_MODEL_MAP


@router.get("/stage-model-config")
async def get_stage_model_config():
    """获取阶段→模型推荐配置"""
    from app.services.model_pool import STAGE_TIER_MAP, STAGE_MODEL_MAP, TIER_LABELS, get_all_models_with_tier
    stages = {}
    for i in range(8):
        tier = STAGE_TIER_MAP.get(i, 3)
        stages[str(i)] = {
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier, "标准"),
            "explicit_model": STAGE_MODEL_MAP.get(i, ""),
        }
    return {"stages": stages, "available_models": get_all_models_with_tier()}


class StageModelConfigUpdate(BaseModel):
    stage_tiers: dict[str, int] = {}
    stage_models: dict[str, str] = {}


@router.put("/stage-model-config")
async def update_stage_model_config(body: StageModelConfigUpdate, session: AsyncSession = Depends(get_session)):
    """更新阶段→模型推荐配置"""
    from app.services.model_pool import STAGE_TIER_MAP, STAGE_MODEL_MAP
    for k, v in body.stage_tiers.items():
        STAGE_TIER_MAP[int(k)] = v
    for k, v in body.stage_models.items():
        if v:
            STAGE_MODEL_MAP[int(k)] = v
        else:
            STAGE_MODEL_MAP.pop(int(k), None)
    await model_config_notice.notify_model_config_changed(
        session,
        scope="global_default",
        reason="stage_model_config_updated",
    )
    return {"status": "updated"}


@router.get("/coding-readiness")
async def coding_readiness():
    """检查编码阶段所需模型是否就绪"""
    return model_pool.check_coding_readiness()


@router.post("/reload")
async def reload_providers(session: AsyncSession = Depends(get_session)):
    await model_pool.load_providers(session)
    await token_tracker.load_prices(session)
    return {"status": "reloaded", "providers": list(model_pool._providers.keys())}


# --- 以下路由含路径参数 {provider_id}，必须放在固定路径之后 ---


class ProviderEnabledBody(BaseModel):
    enabled: bool


@router.patch("/{provider_id}/enabled")
async def patch_provider_enabled(
    provider_id: str,
    body: ProviderEnabledBody,
    session: AsyncSession = Depends(get_session),
):
    """临时禁用/启用供应商；禁用后模型池不再加载该供应商，调用时走降级路由。"""
    p = await session.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    p.enabled = body.enabled
    await session.commit()
    await model_pool.load_providers(session)
    await token_tracker.load_prices(session)
    await model_config_notice.notify_model_config_changed(
        session,
        scope="global_default",
        reason="provider_enabled_toggled",
    )
    return {"id": p.id, "enabled": p.enabled}


@router.put("/{provider_id}")
async def update_provider(provider_id: str, body: ProviderCreate, session: AsyncSession = Depends(get_session)):
    p = await session.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    payload = body.model_dump(exclude={"model_configs", "model_prices", "model_params"})
    src = (payload.get("credential_source") or p.credential_source or "byok").strip().lower()
    if src not in ("byok", "platform"):
        raise HTTPException(400, detail="credential_source 须为 byok 或 platform")
    payload["credential_source"] = src
    for k, v in payload.items():
        if k == "api_key" and not v:
            continue
        setattr(p, k, v)

    configs = body.model_configs
    if not configs and body.models:
        configs = _configs_from_legacy(
            body.models, body.model_prices, body.model_params,
            body.input_price_per_mtok, body.output_price_per_mtok, body.cache_read_price_per_mtok,
        )
    await _sync_model_configs(session, p.id, configs, body.models)

    await session.commit()
    await model_pool.load_providers(session)
    await token_tracker.load_prices(session)
    await model_config_notice.notify_model_config_changed(
        session,
        scope="global_default",
        reason="provider_updated",
    )
    return {"id": p.id, "name": p.name}


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str, session: AsyncSession = Depends(get_session)):
    p = await session.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    await session.delete(p)
    await session.commit()
    await model_pool.load_providers(session)
    await model_config_notice.notify_model_config_changed(
        session,
        scope="global_default",
        reason="provider_deleted",
    )
    return {"status": "deleted"}


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, session: AsyncSession = Depends(get_session)):
    p = await session.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)

    import asyncio
    import time
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=p.api_key, base_url=p.api_base)
    models = p.models or []
    if not models:
        return {"results": [], "summary": "无模型可测试"}

    async def _test_one(model_name: str) -> dict:
        t0 = time.monotonic()
        try:
            resp = await client.chat.completions.create(
                model=model_name,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi, reply with 'ok'"}],
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            if isinstance(resp, str):
                hint = "请检查 API 地址是否正确（应以 /v1 结尾）" if "html" in resp[:200].lower() else ""
                return {"ok": False, "model": model_name, "error": f"API 返回非标准格式。{hint}", "latency_ms": elapsed}
            content = ""
            if hasattr(resp, "choices") and resp.choices:
                msg = resp.choices[0].message
                content = msg.content if hasattr(msg, "content") and msg.content else ""
            usage = getattr(resp, "usage", None)
            return {
                "ok": True, "model": model_name,
                "response": content.strip()[:100], "latency_ms": elapsed,
                "tokens": {"input": getattr(usage, "prompt_tokens", 0) if usage else 0,
                           "output": getattr(usage, "completion_tokens", 0) if usage else 0},
            }
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            err = str(e)[:200]
            if "404" in err or "not found" in err.lower():
                err = f"模型不存在或供应商不支持此模型: {err}"
            elif "401" in err or "auth" in err.lower():
                err = f"API Key 无效或已过期: {err}"
            return {"ok": False, "model": model_name, "error": err, "latency_ms": elapsed}

    # 尝试拉取供应商支持的模型列表
    available_models: list[str] = []
    try:
        model_list = await client.models.list()
        available_models = sorted(m.id for m in model_list.data)[:50]
    except Exception:
        pass

    results = await asyncio.gather(*[_test_one(m) for m in models])

    # 对失败的模型，尝试从可用列表中找相似的建议
    if available_models:
        for r in results:
            if not r["ok"] and "model" in r:
                prefix = r["model"].rsplit("-", 1)[0]  # e.g. claude-opus-4
                similar = [m for m in available_models if m.startswith(prefix)]
                if similar:
                    r["hint"] = f"供应商可能支持: {', '.join(similar[:5])}"

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "results": list(results),
        "summary": f"{ok_count}/{len(models)} 模型可用",
        "available_models": available_models[:30] if available_models else None,
    }


# ── Git SSH 公钥管理 ──

import os
from pathlib import Path

_SSH_DIR = Path.home() / ".ssh"
_KEY_PATH = _SSH_DIR / "id_ed25519"

@router.get("/git-ssh-key")
async def get_git_ssh_public_key(session: AsyncSession = Depends(get_session)):
    """获取 Dispatcher 的 Git SSH 公钥（用于配置免密访问私有仓库）"""
    pub_path = Path(f"{_KEY_PATH}.pub")
    if pub_path.exists():
        return {"public_key": pub_path.read_text().strip(), "exists": True}
    cfg = await session.get(SystemConfig, "git_ssh_keys")
    if cfg and cfg.value.get("public_key"):
        return {"public_key": cfg.value["public_key"], "exists": True}
    return {"public_key": "", "exists": False}


@router.post("/git-ssh-key/generate")
async def generate_git_ssh_key(session: AsyncSession = Depends(get_session)):
    """生成 Dispatcher 的 Git SSH 密钥对（ed25519），同时持久化到数据库"""
    import subprocess

    _SSH_DIR.mkdir(mode=0o700, exist_ok=True)

    if _KEY_PATH.exists():
        pub = Path(f"{_KEY_PATH}.pub").read_text().strip()
        private = _KEY_PATH.read_text()
        await _save_ssh_keys_to_db(session, private, pub)
        return {"public_key": pub, "exists": True, "generated": False}

    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(_KEY_PATH), "-N", "", "-C", "openclaw-dispatcher"],
        check=True, capture_output=True,
    )
    os.chmod(_KEY_PATH, 0o600)

    known_hosts = _SSH_DIR / "known_hosts"
    if not known_hosts.exists():
        known_hosts.touch(mode=0o644)

    ssh_config = _SSH_DIR / "config"
    if not ssh_config.exists():
        ssh_config.write_text("Host *\n  StrictHostKeyChecking accept-new\n  UserKnownHostsFile ~/.ssh/known_hosts\n")

    pub = Path(f"{_KEY_PATH}.pub").read_text().strip()
    private = _KEY_PATH.read_text()
    await _save_ssh_keys_to_db(session, private, pub)
    return {"public_key": pub, "exists": True, "generated": True}


async def _save_ssh_keys_to_db(session: AsyncSession, private_key: str, public_key: str):
    """将 SSH 密钥对持久化到 SystemConfig"""
    cfg = await session.get(SystemConfig, "git_ssh_keys")
    if cfg:
        cfg.value = {"private_key": private_key, "public_key": public_key}
    else:
        session.add(SystemConfig(key="git_ssh_keys", value={"private_key": private_key, "public_key": public_key}))
    await session.commit()


async def restore_ssh_keys_from_db(session: AsyncSession):
    """Dispatcher 启动时从数据库恢复 SSH 密钥到文件系统"""
    if _KEY_PATH.exists():
        return

    cfg = await session.get(SystemConfig, "git_ssh_keys")
    if not cfg or not cfg.value.get("private_key"):
        return

    import logging
    logger = logging.getLogger(__name__)

    _SSH_DIR.mkdir(mode=0o700, exist_ok=True)

    _KEY_PATH.write_text(cfg.value["private_key"])
    os.chmod(_KEY_PATH, 0o600)

    pub_key = cfg.value.get("public_key", "")
    if pub_key:
        Path(f"{_KEY_PATH}.pub").write_text(pub_key + "\n")
        os.chmod(Path(f"{_KEY_PATH}.pub"), 0o644)

    known_hosts = _SSH_DIR / "known_hosts"
    if not known_hosts.exists():
        known_hosts.touch(mode=0o644)

    ssh_config = _SSH_DIR / "config"
    if not ssh_config.exists():
        ssh_config.write_text("Host *\n  StrictHostKeyChecking accept-new\n  UserKnownHostsFile ~/.ssh/known_hosts\n")

    logger.info("SSH keys restored from database to %s", _KEY_PATH)
