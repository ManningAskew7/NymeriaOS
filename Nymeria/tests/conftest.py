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
    todo_auto_archive_days: int = 7
    api_docs_enabled: bool = False
    cors_origins_list: list[str] | None = None

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = ["*"]

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


@pytest.fixture
def api_client_builder(monkeypatch: pytest.MonkeyPatch) -> ApiTestClientBuilder:
    return ApiTestClientBuilder(monkeypatch)
