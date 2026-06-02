from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services import role_context
from app.services import model_pool, token_tracker, model_config_notice

router = APIRouter(prefix="/api/private/runtime", tags=["private-runtime"])


@router.post("/role-context/reload")
async def reload_role_context_runtime_cache():
    result = role_context.reload_role_context_cache()
    return {
        "status": "ok",
        "target": "role_context",
        **result,
    }


class ReloadModelConfigBody(BaseModel):
    broadcast: bool = True
    reason: str = "manual_private_reload"


@router.post("/model-config/reload")
async def reload_model_config_runtime(
    body: ReloadModelConfigBody,
    session: AsyncSession = Depends(get_session),
):
    await model_pool.load_providers(session)
    await token_tracker.load_prices(session)
    notify_result = None
    if body.broadcast:
        notify_result = await model_config_notice.notify_model_config_changed(
            session,
            scope="global_default",
            reason=body.reason or "manual_private_reload",
        )
    return {
        "status": "ok",
        "target": "model_config",
        "providers": len(model_pool._providers),
        "models": len(model_pool._model_to_provider),
        "model_params": len(model_pool._model_params),
        "broadcast": bool(body.broadcast),
        "notify": notify_result,
    }
