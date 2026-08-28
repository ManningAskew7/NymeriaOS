"""CORS behavior for browser-extension origins (backlog 12 entry 35).

The nymeria-browser extension calls the API from a chrome-extension://<id>
origin, where the id varies per unpacked install. These tests pin the app's
CORS contract: any well-formed extension origin passes preflight and gets its
origin echoed, malformed lookalikes and unlisted web origins are refused, and
the configured exact-match list keeps working beside the pattern.

Teeth: with the `allow_origin_regex` line removed from create_api_app, every
`test_extension_origin_*` test here goes red (verified by mutation at
introduction); the refusal tests pin the anchor so a widened pattern fails.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.triggers import api as api_module

# A well-formed Chrome extension id: exactly 32 chars from the a-p alphabet.
EXT_ORIGIN = "chrome-extension://" + "abcdefghijklmnop" * 2


@dataclass
class _Settings:
    data_dir: Path
    nymeria_api_key: str | None = None
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    context_management: str = "none"
    sliding_window_cycles: int = 20
    todo_auto_archive_days: int = 7
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
    cors_origins_list: list[str] | None = None
    nymeria_public_url: str | None = "https://nymeria.example.test"

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = ["http://localhost"]


class _ThreadMetadataManager:
    def get_store(self, user_id: str):
        _ = user_id
        return type("_Store", (), {"threads": {}})()


class _Agent:
    def __init__(self, data_dir: Path) -> None:
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = _ThreadMetadataManager()
        self.thread_config_manager = None
        self._graph_cache_lock = threading.Lock()
        self._user_graphs: dict = {}
        self._async_user_graphs: dict = {}
        self._base_system_prompt = "base"
        self._default_graph = None
        self._default_async_graph = None

    def _build_graph_with_prompt(self, prompt: str):
        return object()

    def _build_async_graph_with_prompt(self, prompt: str):
        return object()

    def sync_agent_tools(self) -> None:
        return None


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = _Settings(data_dir=tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    import nymeria.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    api_module._reset_auth_failure_rate_limiter_for_tests()
    agent = _Agent(tmp_path)
    yield TestClient(api_module.create_api_app(agent))  # type: ignore[bad-argument-type]
    api_module._reset_auth_failure_rate_limiter_for_tests()


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_extension_origin_passes_preflight(client) -> None:
    response = _preflight(client, EXT_ORIGIN)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == EXT_ORIGIN
    assert "authorization" in response.headers.get(
        "access-control-allow-headers", ""
    ).lower()


def test_extension_origin_allowed_on_simple_request(client) -> None:
    response = client.get("/health", headers={"Origin": EXT_ORIGIN})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == EXT_ORIGIN


def test_extension_origin_does_not_bypass_auth(client) -> None:
    # An allowed ORIGIN is not an allowed CALLER: /me without a bearer token
    # still refuses. The pattern only widens who may read responses they can
    # already elicit, never who is authenticated.
    response = client.get("/me", headers={"Origin": EXT_ORIGIN})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        # Wrong alphabet: Chrome ids use a-p only.
        "chrome-extension://" + "z" * 32,
        # Wrong lengths.
        "chrome-extension://" + "a" * 31,
        "chrome-extension://" + "a" * 33,
        # Uppercase is not a Chrome id.
        "chrome-extension://" + "ABCDEFGHIJKLMNOP" * 2,
        # Anchoring: no trailing path, no lookalike host embedding.
        EXT_ORIGIN + "/evil",
        "https://chrome-extension.example.com",
        # An unlisted ordinary web origin.
        "https://evil.example.com",
    ],
)
def test_malformed_or_unlisted_origins_get_no_cors_approval(client, origin) -> None:
    response = _preflight(client, origin)
    assert response.headers.get("access-control-allow-origin") is None
    assert response.status_code == 400


def test_configured_exact_origin_still_allowed(client) -> None:
    response = _preflight(client, "http://localhost")
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost"
