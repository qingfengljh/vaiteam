"""演示站 Host 判定：仅当配置了 DEMO_BYPASS_LOGIN_HOSTS（逗号分隔完整域名，不含端口）时命中。

空配置不再回退 demo.vaiteam.cn，避免「只要把 Host 指到任意实例」即获得演示站逻辑与固定口令。
"""

from fastapi import Request

from app.core.config import settings


def _request_hostname(request: Request) -> str:
    raw = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not raw:
        raw = (request.headers.get("host") or "").strip()
    return raw.split(":")[0].strip().lower()


def _demo_host_allowlist() -> set[str]:
    raw = (settings.DEMO_BYPASS_LOGIN_HOSTS or "").strip()
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def request_host_is_demo(request: Request) -> bool:
    host = _request_hostname(request)
    if not host:
        return False
    return host in _demo_host_allowlist()
