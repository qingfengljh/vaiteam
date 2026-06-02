"""Portal 同步的租户管理员邮箱，用于工作台「忘记密码」发重置链接。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemConfig

AUTH_OWNER_CONTACT_KEY = "auth_owner_contact"


def normalize_owner_email(s: str | None) -> str | None:
    if not s or not isinstance(s, str):
        return None
    e = s.strip().lower()
    if not e or "@" not in e or len(e) > 319:
        return None
    return e


async def get_owner_contact_email(session: AsyncSession) -> str | None:
    cfg = await session.get(SystemConfig, AUTH_OWNER_CONTACT_KEY)
    if not cfg or not isinstance(cfg.value, dict):
        return None
    raw = cfg.value.get("email")
    return normalize_owner_email(str(raw) if raw is not None else None)


async def upsert_owner_contact_email(session: AsyncSession, email: str | None) -> None:
    n = normalize_owner_email(email) if email else None
    cfg = await session.get(SystemConfig, AUTH_OWNER_CONTACT_KEY)
    if not n:
        if cfg:
            await session.delete(cfg)
        return
    if cfg:
        cfg.value = {"email": n}
    else:
        session.add(SystemConfig(key=AUTH_OWNER_CONTACT_KEY, value={"email": n}))
