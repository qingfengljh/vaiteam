"""项目访问窗口：自创建起固定天数内可使用控制台/API；到期后仅允许拉取项目元信息。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.core.config import settings
from app.models import Project

ACCESS_EXPIRED_CODE = "project_access_expired"

ACCESS_EXPIRED_MESSAGE = (
    f"项目演示期（{settings.PROJECT_ACCESS_DAYS} 天）已到期，"
    "在线操作功能暂时关闭。代码仓库不受影响。"
    "如需继续使用，请联系管理员延长有效期。"
)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def effective_access_until(project: Project) -> datetime:
    if project.access_until:
        return _aware(project.access_until)
    return _aware(project.created_at) + timedelta(days=settings.PROJECT_ACCESS_DAYS)


def is_access_expired(project: Project) -> bool:
    return datetime.now(timezone.utc) >= effective_access_until(project)


def raise_if_expired_for_write(project: Project) -> None:
    if not is_access_expired(project):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "message": ACCESS_EXPIRED_MESSAGE,
            "code": ACCESS_EXPIRED_CODE,
            "access_until": effective_access_until(project).isoformat(),
        },
    )


def json_body_for_middleware(project: Project) -> dict:
    end = effective_access_until(project)
    return {
        "detail": ACCESS_EXPIRED_MESSAGE,
        "code": ACCESS_EXPIRED_CODE,
        "access_until": end.isoformat(),
    }
