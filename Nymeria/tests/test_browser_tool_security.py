import importlib.util
from pathlib import Path

import pytest
import requests


_BROWSER_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "nymeria" / "tools" / "browser.py"
)
_BROWSER_SPEC = importlib.util.spec_from_file_location(
    "browser_tool_under_test",
    _BROWSER_MODULE_PATH,
)
assert _BROWSER_SPEC is not None
assert _BROWSER_SPEC.loader is not None
browser_module = importlib.util.module_from_spec(_BROWSER_SPEC)
_BROWSER_SPEC.loader.exec_module(browser_module)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/plain,hello",
        "//example.com",
        "example.com",
        "",
    ],
)
def test_browser_navigate_rejects_non_http_urls_before_browser_start(monkeypatch, url):
    def fail_get_browser():
        raise AssertionError("browser should not start for invalid URLs")

    monkeypatch.setattr(browser_module, "_use_fallback_mode", lambda: False)
    monkeypatch.setattr(browser_module, "_get_browser", fail_get_browser)

    result = browser_module.browser_navigate.invoke({"url": url})

    assert result.startswith("[Error]:")
    assert "http://" in result
    assert "https://" in result


def test_browser_navigate_rejects_private_network_before_browser_start(monkeypatch):
    def fail_get_browser():
        raise AssertionError("browser should not start for blocked URLs")

    monkeypatch.setattr(browser_module, "_use_fallback_mode", lambda: False)
    monkeypatch.setattr(browser_module, "_get_browser", fail_get_browser)

    result = browser_module.browser_navigate.invoke({"url": "http://127.0.0.1:8000/admin"})

    assert result.startswith("[Error]:")
    assert "blocked by HTTP egress policy" in result


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, True),
        ("true", True),
        ("1", True),
        ("false", True),
        ("0", True),
        ("invalid", True),
    ],
)
def test_browser_verify_ssl_is_always_enforced(
    monkeypatch,
    env_value,
    expected,
):
    if env_value is None:
        monkeypatch.delenv("BROWSER_VERIFY_SSL", raising=False)
    else:
        monkeypatch.setenv("BROWSER_VERIFY_SSL", env_value)

    assert browser_module._browser_verify_ssl() is expected


def test_fallback_requests_verify_tls_by_default(monkeypatch):
    calls = []
    response = object()

    def fake_get(
        url,
        *,
        headers,
        params=None,
        stream=False,
        timeout,
        verify,
        allow_redirects,
    ):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "stream": stream,
                "timeout": timeout,
                "verify": verify,
                "allow_redirects": allow_redirects,
            }
        )
        return response

    monkeypatch.delenv("BROWSER_VERIFY_SSL", raising=False)
    monkeypatch.setattr(requests, "get", fake_get)

    assert (
        browser_module._requests_get("https://example.com", {"User-Agent": "test"})
        is response
    )
    assert calls == [
        {
            "url": "https://example.com",
            "headers": {"User-Agent": "test"},
            "params": None,
            "stream": False,
            "timeout": 30,
            "verify": True,
            "allow_redirects": False,
        }
    ]


def test_fallback_requests_ignore_tls_disable_env(monkeypatch):
    calls = []
    response = object()

    def fake_get(
        url,
        *,
        headers,
        params=None,
        stream=False,
        timeout,
        verify,
        allow_redirects,
    ):
        calls.append(verify)
        return response

    monkeypatch.setenv("BROWSER_VERIFY_SSL", "false")
    monkeypatch.setattr(requests, "get", fake_get)

    assert browser_module._requests_get("https://example.com", {}) is response
    assert calls == [True]
