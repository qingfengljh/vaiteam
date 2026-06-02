"""认证路由：登录获取 JWT、修改密码。用户名和密码均存数据库，重启不丢失。"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hashlib
import secrets
from urllib.parse import quote, urlencode
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.demo_host import request_host_is_demo
from app.routers.infra_nodes import _load_terminal_gate, _terminal_gate_configured
from app.core.redis import get_redis
from app.models import SystemConfig
from app.services.auth_owner_contact import get_owner_contact_email, normalize_owner_email
from app.services.mail_dispatch import send_password_reset_email, smtp_ready

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

class LoginReq(BaseModel):
    username: str
    password: str
    captcha_id: str
    captcha_answer: str


class LoginResp(BaseModel):
    token: str
    username: str
    expires_at: str


class CaptchaResp(BaseModel):
    captcha_id: str
    challenge: str
    captcha_svg: str
    expires_at: str


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str


class DemoHintsResp(BaseModel):
    login_username: str
    login_password: str
    terminal_gate_password: str


class ForgotPasswordReq(BaseModel):
    email: str
    captcha_id: str
    captcha_answer: str


class ResetPasswordFromTokenReq(BaseModel):
    token: str = Field(..., min_length=10, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=256)
    captcha_id: str
    captcha_answer: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _no_db_auth_placeholder_password() -> str:
    """库内尚无 auth_credentials 时禁止弱默认口令；与 JWT_SECRET 绑定且各 worker 一致（不可从外猜）。"""
    h = hashlib.sha256()
    h.update(settings.JWT_SECRET.encode())
    h.update(b"\nauth_credentials_row_missing")
    return h.hexdigest()


def _login_lock_retry_message(until: datetime, now: datetime) -> str:
    """429 文案：剩余分钟 + 配置时区本地时刻 + UTC，避免把 UTC 钟点当本地钟点。"""
    sec = max(0, int((until - now).total_seconds()))
    mins = max(1, (sec + 59) // 60)
    utc_s = until.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tz_name = (settings.AUTH_LOGIN_LOCK_MESSAGE_TIMEZONE or "").strip()
    if not tz_name:
        return f"登录失败次数过多，请约 {mins} 分钟后再试（解锁时间 {utc_s}）"
    try:
        local_s = until.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
        label = "北京时间" if tz_name == "Asia/Shanghai" else tz_name
        return f"登录失败次数过多，请约 {mins} 分钟后再试（{label} {local_s}，{utc_s}）"
    except ZoneInfoNotFoundError:
        return f"登录失败次数过多，请约 {mins} 分钟后再试（解锁时间 {utc_s}）"


def _extract_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
    if xff:
        return xff
    real_ip = (request.headers.get("x-real-ip", "") or "").strip()
    if real_ip:
        return real_ip
    client = request.client
    return client.host if client and client.host else "unknown"


async def _is_ip_locked(ip: str, now: datetime) -> datetime | None:
    redis = await get_redis()
    ttl = await redis.ttl(f"auth:login:lock:{ip}")
    if ttl and ttl > 0:
        return now + timedelta(seconds=ttl)
    return None


async def _record_login_failure(ip: str, now: datetime) -> datetime | None:
    redis = await get_redis()
    fail_key = f"auth:login:fail:{ip}"
    count = await redis.incr(fail_key)
    if count == 1:
        await redis.expire(fail_key, settings.AUTH_LOGIN_LOCK_SECONDS)
    if count >= settings.AUTH_LOGIN_MAX_FAILED_ATTEMPTS:
        await redis.delete(fail_key)
        await redis.setex(f"auth:login:lock:{ip}", settings.AUTH_LOGIN_LOCK_SECONDS, "1")
        return now + timedelta(seconds=settings.AUTH_LOGIN_LOCK_SECONDS)
    return None


async def _clear_login_failure(ip: str) -> None:
    redis = await get_redis()
    await redis.delete(f"auth:login:fail:{ip}")
    await redis.delete(f"auth:login:lock:{ip}")


def _public_base_url(request: Request) -> str:
    s = (settings.AUTH_PASSWORD_RESET_PUBLIC_BASE_URL or "").strip()
    if s:
        return s.rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("host") or request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = request.url.netloc or ""
    return f"{proto}://{host}".rstrip("/")


async def clear_all_login_locks_and_fail_counters() -> int:
    """清除所有客户端 IP 的登录失败计数与锁定（单机 Redis）。Portal 重置 admin 口令后调用。"""
    redis = await get_redis()
    n = 0
    for pattern in ("auth:login:lock:*", "auth:login:fail:*"):
        async for key in redis.scan_iter(match=pattern):
            await redis.delete(key)
            n += 1
    return n


async def _new_captcha(now: datetime) -> CaptchaResp:
    a = secrets.randbelow(9) + 1
    b = secrets.randbelow(9) + 1
    challenge = f"{a} + {b} = ?"
    captcha_id = secrets.token_urlsafe(18)
    expires_at = now + timedelta(seconds=settings.AUTH_CAPTCHA_TTL_SECONDS)
    redis = await get_redis()
    await redis.setex(f"auth:captcha:{captcha_id}", settings.AUTH_CAPTCHA_TTL_SECONDS, str(a + b))
    return CaptchaResp(
        captcha_id=captcha_id,
        challenge=challenge,
        captcha_svg=_render_captcha_svg(challenge),
        expires_at=expires_at.isoformat(),
    )


async def _check_captcha(captcha_id: str, captcha_answer: str) -> bool:
    redis = await get_redis()
    key = f"auth:captcha:{captcha_id}"
    answer = await redis.get(key)
    await redis.delete(key)
    if not answer:
        return False
    return captcha_answer.strip() == str(answer).strip()


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _render_captcha_svg(challenge: str) -> str:
    width = 176
    height = 52
    lines: list[str] = []
    for _ in range(7):
        x1 = secrets.randbelow(width)
        y1 = secrets.randbelow(height)
        x2 = secrets.randbelow(width)
        y2 = secrets.randbelow(height)
        color = f"rgba({90 + secrets.randbelow(120)},{120 + secrets.randbelow(100)},{170 + secrets.randbelow(80)},0.55)"
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{1 + secrets.randbelow(2)}" />'
        )

    dots: list[str] = []
    for _ in range(42):
        cx = secrets.randbelow(width)
        cy = secrets.randbelow(height)
        r = 1 + secrets.randbelow(2)
        color = f"rgba({140 + secrets.randbelow(90)},{140 + secrets.randbelow(90)},{220 + secrets.randbelow(30)},0.7)"
        dots.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}" />')

    text_parts: list[str] = []
    start_x = 18
    for ch in challenge:
        rotation = secrets.randbelow(21) - 10
        y = 33 + secrets.randbelow(9) - 4
        text_parts.append(
            f'<text x="{start_x}" y="{y}" font-size="26" font-family="monospace" '
            f'font-weight="700" fill="#F6FBFF" transform="rotate({rotation} {start_x} {y})">{_svg_escape(ch)}</text>'
        )
        start_x += 15 if ch == " " else 17

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect x="0" y="0" width="100%" height="100%" rx="8" ry="8" fill="#13203f" />'
        '<rect x="1" y="1" width="174" height="50" rx="7" ry="7" fill="none" stroke="#4E7BFF" stroke-opacity="0.55" />'
        + "".join(lines)
        + "".join(dots)
        + "".join(text_parts)
        + "</svg>"
    )
    return f"data:image/svg+xml;utf8,{quote(svg)}"


async def _get_credentials(session: AsyncSession) -> dict:
    """从数据库读取用户名和密码，无则返回初始值"""
    cfg = await session.get(SystemConfig, "auth_credentials")
    if cfg and isinstance(cfg.value, dict) and cfg.value.get("username") and cfg.value.get("password"):
        return cfg.value
    # 兼容旧版 auth_password
    old = await session.get(SystemConfig, "auth_password")
    if old and isinstance(old.value, dict) and old.value.get("password"):
        creds = {"username": "admin", "password": old.value["password"]}
        session.add(SystemConfig(key="auth_credentials", value=creds))
        await session.delete(old)
        await session.commit()
        return creds
    return {"username": "admin", "password": _no_db_auth_placeholder_password()}


@router.get("/captcha", response_model=CaptchaResp)
async def get_captcha():
    now = _utc_now()
    return await _new_captcha(now)


@router.get("/demo-hints", response_model=DemoHintsResp)
async def demo_hints(request: Request, session: AsyncSession = Depends(get_session)):
    """仅演示站 Host 可访问：展示与库内一致的登录账号与终端门说明。"""
    if not request_host_is_demo(request):
        raise HTTPException(404, "非演示环境")
    creds = await _get_credentials(session)
    gate_doc = await _load_terminal_gate(session)
    gate_hint = (
        settings.DEMO_PUBLIC_TERMINAL_GATE_PASSWORD
        if not _terminal_gate_configured(gate_doc)
        else "（终端口令已在系统中配置，无法在此展示明文）"
    )
    return DemoHintsResp(
        login_username=creds["username"],
        login_password=creds["password"],
        terminal_gate_password=gate_hint,
    )


@router.post("/login", response_model=LoginResp)
async def login(body: LoginReq, request: Request, session: AsyncSession = Depends(get_session)):
    now = _utc_now()
    ip = _extract_client_ip(request)
    locked_until = await _is_ip_locked(ip, now)
    if locked_until:
        raise HTTPException(429, _login_lock_retry_message(locked_until, now))

    if not body.captcha_id.strip() or not body.captcha_answer.strip():
        await _record_login_failure(ip, now)
        raise HTTPException(400, "验证码不能为空")
    if not await _check_captcha(body.captcha_id.strip(), body.captcha_answer.strip()):
        locked_at = await _record_login_failure(ip, now)
        if locked_at:
            raise HTTPException(429, _login_lock_retry_message(locked_at, now))
        raise HTTPException(400, "验证码错误或已过期")

    creds = await _get_credentials(session)
    if body.username != creds["username"] or body.password != creds["password"]:
        locked_at = await _record_login_failure(ip, now)
        if locked_at:
            raise HTTPException(429, _login_lock_retry_message(locked_at, now))
        raise HTTPException(401, "用户名或密码错误")

    await _clear_login_failure(ip)
    expires = now + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {"sub": body.username, "exp": expires}
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")

    return LoginResp(token=token, username=body.username, expires_at=expires.isoformat())


@router.get("/me")
async def me(request: Request):
    """从 JWT 中解析当前用户名"""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(401, "未登录")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        return {"username": payload.get("sub", "admin")}
    except JWTError:
        raise HTTPException(401, "登录已过期")


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordReq,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """向 Portal 同步的 owner 邮箱发送重置链接（须配置 SMTP；防枚举：邮箱不匹配也返回相同提示）。"""
    if request_host_is_demo(request):
        raise HTTPException(403, "演示环境不支持邮件找回密码")
    ip = _extract_client_ip(request)
    redis = await get_redis()
    rl_key = f"auth:forgot:rl:{ip}"
    n = await redis.incr(rl_key)
    if n == 1:
        await redis.expire(rl_key, 3600)
    if n > settings.AUTH_FORGOT_PASSWORD_MAX_PER_HOUR_PER_IP:
        raise HTTPException(429, "找回密码请求过于频繁，请约一小时后再试")

    if not body.captcha_id.strip() or not body.captcha_answer.strip():
        raise HTTPException(400, "验证码不能为空")
    if not await _check_captcha(body.captcha_id.strip(), body.captcha_answer.strip()):
        raise HTTPException(400, "验证码错误或已过期")

    owner = await get_owner_contact_email(session)
    if not owner:
        raise HTTPException(
            400,
            "尚未登记用于找回密码的邮箱。请使用 Portal 控制台进入该实例「安装代理」等页面以触发与 Portal 的同步，或联系管理员。",
        )
    if not smtp_ready():
        raise HTTPException(503, "工作台未配置发信（SMTP），无法发送重置邮件，请联系管理员在 dispatcher 环境配置 SMTP_* 变量")

    want = normalize_owner_email(body.email)
    if not want:
        raise HTTPException(400, "请输入有效邮箱")

    msg_ok = "若该邮箱已在系统中登记且发信已配置，您将很快收到重置邮件，请查收垃圾箱"
    if want != owner:
        return {"message": msg_ok}

    token = secrets.token_urlsafe(32)
    tok_key = f"auth:pwreset:tok:{token}"
    await redis.setex(tok_key, settings.AUTH_PASSWORD_RESET_TOKEN_TTL_SECONDS, "1")

    base = _public_base_url(request)
    reset_url = f"{base}/reset-password?{urlencode({'token': token, 'redirect': '/projects'})}"
    try:
        sent = await send_password_reset_email(owner, reset_url)
    except Exception:
        logger.exception("send_password_reset_email failed to=%s", owner)
        raise HTTPException(503, "邮件发送失败，请稍后再试") from None
    if not sent:
        raise HTTPException(503, "邮件发送失败，请稍后再试")
    return {"message": msg_ok}


@router.post("/reset-password-from-token")
async def reset_password_from_token(
    body: ResetPasswordFromTokenReq,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if request_host_is_demo(request):
        raise HTTPException(403, "演示环境不支持通过链接重置密码")
    if not body.captcha_id.strip() or not body.captcha_answer.strip():
        raise HTTPException(400, "验证码不能为空")
    if not await _check_captcha(body.captcha_id.strip(), body.captcha_answer.strip()):
        raise HTTPException(400, "验证码错误或已过期")

    redis = await get_redis()
    tok_key = f"auth:pwreset:tok:{body.token.strip()}"
    ok = await redis.get(tok_key)
    if not ok:
        raise HTTPException(400, "重置链接无效或已过期，请重新申请找回密码")
    await redis.delete(tok_key)

    creds = await _get_credentials(session)
    username = str(creds.get("username") or "admin")
    cfg = await session.get(SystemConfig, "auth_credentials")
    new_value = {**creds, "password": body.new_password}
    if cfg:
        cfg.value = new_value
    else:
        session.add(SystemConfig(key="auth_credentials", value=new_value))
    await session.commit()
    await clear_all_login_locks_and_fail_counters()
    return {"message": "密码已重置，请使用新密码登录", "username": username}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordReq,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if request_host_is_demo(request):
        raise HTTPException(403, "演示环境不允许修改登录密码")
    creds = await _get_credentials(session)
    if body.old_password != creds["password"]:
        raise HTTPException(400, "原密码错误")
    if len(body.new_password) < 4:
        raise HTTPException(400, "新密码至少 4 位")

    cfg = await session.get(SystemConfig, "auth_credentials")
    new_value = {**creds, "password": body.new_password}
    if cfg:
        cfg.value = new_value
    else:
        session.add(SystemConfig(key="auth_credentials", value=new_value))
    await session.commit()

    return {"message": "密码修改成功"}
