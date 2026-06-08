from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nymeria.triggers import api as api_module


@dataclass
class ApiTestSettings:
    data_dir: Path
    nymeria_api_key: str | None = "legacy-secret"
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    context_management: str = "none"
    sliding_window_cycles: int = 20
    memory_char_limit: int = 8000
    todo_auto_archive_days: int = 7
    scheduler_missed_work_policy: str = "run"
    scheduler_active_execution_stale_minutes: int = 1440
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_base_url: str = "https://graph.facebook.com/v19.0"
    whatsapp_webhook_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_show_tool_events: bool = False
    messenger_page_access_token: str | None = None
    messenger_page_id: str | None = None
    messenger_webhook_verify_token: str | None = None
    messenger_app_secret: str | None = None
    messenger_graph_api_base_url: str = "https://graph.facebook.com/v23.0"
    messenger_show_tool_events: bool = False
    instagram_access_token: str | None = None
    instagram_ig_user_id: str | None = None
    instagram_webhook_verify_token: str | None = None
    instagram_app_secret: str | None = None
    instagram_graph_api_base_url: str = "https://graph.instagram.com/v23.0"
    instagram_show_tool_events: bool = False
    webex_access_token: str | None = None
    webex_base_url: str = "https://webexapis.com/v1"
    webex_webhook_secret: str | None = None
    webex_bot_person_id: str | None = None
    webex_bot_email: str | None = None
    webex_show_tool_events: bool = False
    mattermost_access_token: str | None = None
    mattermost_base_url: str | None = None
    mattermost_respond_mode: str = "mention"
    mattermost_show_tool_events: bool = False
    zulip_api_key: str | None = None
    zulip_email: str | None = None
    zulip_base_url: str | None = None
    zulip_respond_mode: str = "mention"
    zulip_show_tool_events: bool = False
    rocketchat_auth_token: str | None = None
    rocketchat_user_id: str | None = None
    rocketchat_base_url: str | None = None
    rocketchat_respond_mode: str = "mention"
    rocketchat_show_tool_events: bool = False
    teams_bot_app_id: str | None = None
    teams_bot_app_password: str | None = None
    teams_bot_tenant_id: str | None = None
    teams_bot_respond_mode: str = "mention"
    teams_bot_validate_auth: bool = False
    teams_bot_show_tool_events: bool = False
    teams_bot_token_url: str = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    teams_bot_openid_config_url: str = "https://login.botframework.com/v1/.well-known/openidconfiguration"
    google_chat_service_account_json: str | None = None
    google_chat_service_account_file: str | None = None
    google_chat_project_number: str | None = None
    google_chat_auth_audience: str | None = None
    google_chat_auth_audience_type: str = "app-url"
    google_chat_bot_name: str = "Nymeria"
    google_chat_respond_mode: str = "mention"
    google_chat_validate_auth: bool = False
    google_chat_show_tool_events: bool = False
    google_chat_api_base_url: str = "https://chat.googleapis.com/v1"
    google_chat_use_adc: bool = False
    line_channel_access_token: str | None = None
    line_channel_secret: str | None = None
    line_bot_user_id: str | None = None
    line_bot_name: str = "Nymeria"
    line_respond_mode: str = "mention"
    line_validate_signature: bool = False
    line_show_tool_events: bool = False
    line_api_base_url: str = "https://api.line.me/v2/bot"
    signal_http_url: str | None = None
    signal_account: str | None = None
    signal_account_uuid: str | None = None
    signal_respond_mode: str = "mention"
    signal_allowed_users: str = ""
    signal_allowed_groups: str = ""
    signal_show_tool_events: bool = False
    signal_http_timeout: float = 30.0
    cors_origins_list: list[str] | None = None

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = [
                "http://localhost:1420",
                "tauri://localhost",
                "http://tauri.localhost",
                "https://tauri.localhost",
                "http://localhost:8000",
            ]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nymeria.db"


class ApiTestClientBuilder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self._monkeypatch = monkeypatch

    def settings(self, data_dir: Path, **overrides: Any) -> ApiTestSettings:
        return ApiTestSettings(data_dir=data_dir, **overrides)

    def create_checkpoint_table(self, settings: ApiTestSettings) -> None:
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
            conn.commit()

    def client(self, agent: Any, settings: ApiTestSettings) -> TestClient:
        self._monkeypatch.setattr(api_module, "get_settings", lambda: settings)
        return TestClient(api_module.create_api_app(agent))

    def authenticated_client(
        self,
        agent: Any,
        settings: ApiTestSettings,
        *,
        user_id: str = "owner",
        email: str | None = None,
        display_name: str | None = None,
        role: str = "user",
    ) -> tuple[TestClient, str]:
        client = self.client(agent, settings)
        agent.accounts_repo.create_user(
            user_id,
            email or f"{user_id}@example.com",
            display_name or user_id.title(),
            role=role,
        )
        return client, agent.accounts_repo.issue_token(user_id)

    @staticmethod
    def auth(token: str, **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", **headers}


@pytest.fixture(autouse=True)
def _offline_tool_search_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the shared tool-search catalog offline during the suite.

    ``get_tool_search_index()`` builds a process singleton from
    ``settings.embedding_api_key``. On a developer shell (or with a stray
    ``.env.docker`` on the path) that key is populated, so the first semantic
    tool search makes a live OpenAI embeddings call. Without a client timeout
    that hung for the SDK default and tripped pytest-timeout; even with the new
    bounded timeout, unit tests should never reach the network.

    Pre-seed the singleton with a keyless index so the real
    ``is_semantic_available`` logic returns False and search degrades to the
    in-process lexical ranking instantly. Tests that construct their own
    ``ToolSearchIndex`` are unaffected; a test that wants semantic mode can
    re-patch the singleton with a fake embedder.
    """
    from nymeria.core import tool_search_index as tsi

    monkeypatch.setattr(
        tsi, "_DEFAULT_INDEX", tsi.ToolSearchIndex(openai_api_key=None)
    )


@pytest.fixture
def api_client_builder(monkeypatch: pytest.MonkeyPatch) -> ApiTestClientBuilder:
    api_module._reset_auth_failure_rate_limiter_for_tests()
    yield ApiTestClientBuilder(monkeypatch)
    api_module._reset_auth_failure_rate_limiter_for_tests()
