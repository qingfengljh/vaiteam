"""工作台（dispatcher）侧发信：忘记密码等；与 Portal 共用同名 SMTP 环境变量便于运维。"""

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import settings


def smtp_ready() -> bool:
    if not settings.SMTP_ENABLED:
        return False
    return bool(settings.SMTP_HOST and settings.SMTP_PORT and settings.SMTP_FROM_EMAIL)


def _build_message(to_email: str, subject: str, html_body: str, text_body: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    if text_body:
        msg.set_content(text_body)
    else:
        msg.set_content("请使用支持 HTML 的邮箱客户端查看此邮件。")
    msg.add_alternative(html_body, subtype="html")
    return msg


def _send_sync(msg: EmailMessage) -> None:
    if settings.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS
        ) as smtp:
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(msg)


async def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    if not smtp_ready():
        return False
    subject = "VAI TEAM 工作台密码重置"
    text = f"请点击以下链接重置工作台（admin）登录密码（有效期较短）：\n{reset_url}\n\n如非本人操作请忽略本邮件。"
    html = (
        f"<p>请点击以下链接重置<strong>工作台</strong>（admin）登录密码：</p>"
        f"<p><a href=\"{reset_url}\">{reset_url}</a></p>"
        f"<p style=\"color:#64748b;font-size:12px;\">链接有效期较短；如非本人操作请忽略。</p>"
    )
    msg = _build_message(to_email, subject, html, text)
    await asyncio.to_thread(_send_sync, msg)
    return True
