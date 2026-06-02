import pytest
from starlette.requests import Request

from app.core.config import settings
from app.core.demo_host import request_host_is_demo


def _req(host: str) -> Request:
    return Request({"type": "http", "headers": [(b"host", host.encode())]})


def test_default_allows_only_demo_vaiteam_cn(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_LOGIN_HOSTS", "demo.vaiteam.cn")
    assert request_host_is_demo(_req("demo.vaiteam.cn")) is True
    assert request_host_is_demo(_req("demo.vaiteam.cn:8443")) is True
    assert request_host_is_demo(_req("demo.evil.com")) is False
    assert request_host_is_demo(_req("www.demo.vaiteam.cn")) is False


def test_x_forwarded_host_first_value(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_LOGIN_HOSTS", "demo.vaiteam.cn")
    r = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-host", b"demo.vaiteam.cn, other.internal")],
        }
    )
    assert request_host_is_demo(r) is True


def test_custom_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_LOGIN_HOSTS", "a.example.com, b.example.com")
    assert request_host_is_demo(_req("a.example.com")) is True
    assert request_host_is_demo(_req("B.EXAMPLE.COM")) is True
    assert request_host_is_demo(_req("demo.vaiteam.cn")) is False


def test_empty_setting_matches_no_host(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_LOGIN_HOSTS", "")
    assert request_host_is_demo(_req("demo.vaiteam.cn")) is False
