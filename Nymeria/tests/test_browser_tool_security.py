import base64
import importlib.util
from pathlib import Path

import pytest
import requests

from nymeria.core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY


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


def test_truncate_caps_content_at_limit():
    short = "x" * (browser_module.MAX_CONTENT_CHARS - 1)
    assert browser_module._truncate(short) == short

    long = "y" * (browser_module.MAX_CONTENT_CHARS + 500)
    truncated = browser_module._truncate(long)
    assert truncated == "y" * browser_module.MAX_CONTENT_CHARS + "\n...[truncated]"


def _fake_browser(result):
    """Build a stub browser whose screenshot command yields `result`."""

    class FakeBrowser:
        def execute(self, command, args):
            assert command == "screenshot"
            return result

    return FakeBrowser()


def test_browser_screenshot_surfaces_native_image_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))

    png_b64 = base64.b64encode(b"fake-screenshot-bytes").decode("ascii")
    monkeypatch.setattr(
        browser_module,
        "_get_browser",
        lambda: _fake_browser((True, {"url": "https://example.com", "b64": png_b64})),
    )

    # InjectedToolArg config is supplied directly to the wrapped function.
    content, artifact = browser_module.browser_screenshot.func(
        config={"configurable": {"user_id": "owner@example.com"}}
    )

    assert "[attach:" in content
    assert "https://example.com" in content  # page URL threaded through
    assert artifact[NATIVE_IMAGE_ARTIFACT_KEY]["source"] == "browser_screenshot"
    files = list(
        (tmp_path / "images" / "screenshots" / "owner-example.com").glob("*.png")
    )
    assert len(files) == 1
    assert files[0].read_bytes() == b"fake-screenshot-bytes"


def test_browser_screenshot_success_without_config(tmp_path, monkeypatch):
    # config=None must still return a 2-tuple and write the file (under the
    # default user dir), not crash.
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))

    png_b64 = base64.b64encode(b"shot").decode("ascii")
    monkeypatch.setattr(
        browser_module,
        "_get_browser",
        lambda: _fake_browser((True, {"url": "", "b64": png_b64})),
    )

    content, artifact = browser_module.browser_screenshot.func(config=None)

    assert "[attach:" in content
    assert artifact[NATIVE_IMAGE_ARTIFACT_KEY]["source"] == "browser_screenshot"
    files = list((tmp_path / "images" / "screenshots").rglob("*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"shot"


def test_browser_screenshot_decode_failure_returns_error_tuple(monkeypatch):
    # A non-base64 payload must degrade to an [Error] 2-tuple, never raise.
    monkeypatch.setattr(
        browser_module,
        "_get_browser",
        lambda: _fake_browser((True, {"url": "x", "b64": "a"})),
    )

    content, artifact = browser_module.browser_screenshot.func(config=None)
    assert content.startswith("[Error]:")
    assert artifact == {}


def test_browser_screenshot_returns_error_tuple_on_failure(monkeypatch):
    class FailingBrowser:
        def execute(self, command, args):
            return False, "browser not running"

    monkeypatch.setattr(browser_module, "_get_browser", lambda: FailingBrowser())

    content, artifact = browser_module.browser_screenshot.func(config=None)
    assert content.startswith("[Error]:")
    assert artifact == {}
