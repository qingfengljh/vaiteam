"""启动时从环境变量引导 admin 口令（仅 DB 尚无凭据时）。"""

from app.core.config import settings
from app.core.database import async_session
from app.models import SystemConfig


async def bootstrap_initial_auth_from_env() -> None:
    pwd = (settings.VAITEAM_INITIAL_ADMIN_PASSWORD or "").strip()
    if not pwd:
        return
    async with async_session() as session:
        cfg = await session.get(SystemConfig, "auth_credentials")
        if cfg and isinstance(cfg.value, dict) and cfg.value.get("password"):
            return
        old = await session.get(SystemConfig, "auth_password")
        if old and isinstance(old.value, dict) and old.value.get("password"):
            return
        session.add(
            SystemConfig(
                key="auth_credentials",
                value={"username": "admin", "password": pwd},
            )
        )
        await session.commit()
