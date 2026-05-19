"""Tests for the embedded MCP ASGI factory + standalone HTTP path config."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nymeria import mcp_server


@pytest.fixture(autouse=True)
def _reset_mcp_path_state():
    """Restore MCP module state between tests."""
    original_path = mcp_server.mcp.settings.streamable_http_path
    original_url_override = mcp_server._backend_url_override
    original_service_token_override = mcp_server._service_token_override
    original_client = mcp_server._client
    yield
    mcp_server.mcp.settings.streamable_http_path = original_path
    mcp_server._backend_url_override = original_url_override
    mcp_server._service_token_override = original_service_token_override
    mcp_server._client = original_client


def test_create_mcp_asgi_app_sets_root_path() -> None:
    mcp_server.mcp.settings.streamable_http_path = "/mcp"

    app = mcp_server.create_mcp_asgi_app(
        api_url="http://127.0.0.1:8000",
        service_token="nym_test-token",
    )

    assert mcp_server.mcp.settings.streamable_http_path == "/"
    assert app is not None
    # Configure_backend must have stored both overrides so the embedded client
    # uses them on the first call.
    assert mcp_server._backend_url_override == "http://127.0.0.1:8000"
    assert mcp_server._service_token_override == "nym_test-token"


def test_create_mcp_asgi_app_service_token_override_used_by_client() -> None:
    mcp_server.create_mcp_asgi_app(
        api_url="http://127.0.0.1:8000",
        service_token="nym_override-token",
    )

    captured: dict[str, str] = {}

    class _StubClient:
        def __init__(self, base_url: str, service_token: str) -> None:
            captured["base_url"] = base_url
            captured["service_token"] = service_token
            self.base_url = base_url
            self.service_token = service_token

    with patch.object(mcp_server, "NymeriaBackendClient", _StubClient):
        client = mcp_server._get_client()

    assert captured["service_token"] == "nym_override-token"
    assert captured["base_url"] == "http://127.0.0.1:8000"
    assert client.service_token == "nym_override-token"


def test_run_http_restores_mcp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_http`` should reset the streamable HTTP path back to ``/mcp``."""
    # Simulate prior embedded usage that set the path to "/".
    mcp_server.mcp.settings.streamable_http_path = "/"

    called: dict[str, object] = {}

    def _fake_run(**kwargs: object) -> None:
        called["transport"] = kwargs.get("transport")
        called["streamable_http_path"] = mcp_server.mcp.settings.streamable_http_path
        called["host"] = mcp_server.mcp.settings.host
        called["port"] = mcp_server.mcp.settings.port

    monkeypatch.setattr(mcp_server.mcp, "run", _fake_run)

    mcp_server.run_http(host="127.0.0.1", port=8001)

    assert called["transport"] == "streamable-http"
    assert called["streamable_http_path"] == "/mcp"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8001
