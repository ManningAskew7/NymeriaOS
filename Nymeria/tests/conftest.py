from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Suite hermeticity, layer 1 (backlog #101 entry 20): pin the project root to
# this checkout BEFORE the first `nymeria` import below. Importing any
# `nymeria.*` module runs `configure_project_root()` and freezes both
# `config.settings.PROJECT_ROOT` and the Settings dotenv paths at import time,
# so an ambient NYMERIA_PROJECT_ROOT (e.g. a multi-instance operator export)
# would silently point the whole suite at another instance's config.env.
os.environ["NYMERIA_PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

from nymeria.config.settings import Settings as _Settings  # noqa: E402
from nymeria.triggers import api as api_module  # noqa: E402

# Suite hermeticity, layer 2: with the root pinned, the dotenv chain still
# reads any repo-local .env / config.env / .env.docker (a stray .env.docker is
# exactly why `_offline_tool_search_singleton` below exists). pydantic-settings
# resolves `env_file` from model_config per instantiation, so emptying it here
# makes every Settings() the suite builds resolve from real env vars and field
# defaults only. Real env vars keep their normal precedence; this only removes
# the dotenv fallthrough. Composes with the suite-wide `clear_settings_cache`
# fixture below.
_Settings.model_config["env_file"] = ()


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
    default_executor_max_workers: int = 32
    max_concurrent_interactive: int = 0
    interactive_admission_wait_seconds: float = 0.0
    hooks_run_command_enabled: bool = False
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_base_url: str = "https://graph.facebook.com/v19.0"
    whatsapp_webhook_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_show_tool_events: bool = False
    teams_bot_app_id: str | None = None
    teams_bot_app_password: str | None = None
    teams_bot_tenant_id: str | None = None
    teams_bot_respond_mode: str = "mention"
    teams_bot_validate_auth: bool = False
    teams_bot_show_tool_events: bool = False
    teams_bot_token_url: str = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    teams_bot_openid_config_url: str = "https://login.botframework.com/v1/.well-known/openidconfiguration"
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
    ``settings.embedding_api_key``. Historically a developer shell or a stray
    ``.env.docker`` on the path populated that key, so the first semantic
    tool search made a live OpenAI embeddings call (the dotenv half of that
    leak is now closed by the module-level env-file neutralization above; a
    shell-exported key still applies). Without a client timeout that hung for
    the SDK default and tripped pytest-timeout; even with the new bounded
    timeout, unit tests should never reach the network.

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


@pytest.fixture(autouse=True)
def _offline_environment_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `nymeria init` runs from probing the real host.

    Every init run (`setup_main`/`run_init`) caches a detection report on the
    wizard state: loopback socket probes, /proc/meminfo, docker/systemd
    markers, and in interactive mode real `docker info` subprocesses. Stubbed
    suite-wide so no test depends on this machine's docker/systemd/port state
    (a `--hosting docker` test must not fail on a host without docker).
    Gating tests re-patch `runner.detect_environment` with crafted reports;
    detection unit tests call `nymeria.setup.environment` directly, which this
    does not touch.
    """
    from nymeria.onboarding import HostingOption
    from nymeria.setup import runner as runner_module
    from nymeria.setup.environment import EnvironmentReport

    monkeypatch.setattr(
        runner_module,
        "detect_environment",
        lambda **_kw: EnvironmentReport(
            os_label="Linux test",
            is_windows=False,
            docker_available=True,
            recommended_hosting=HostingOption.LOCAL,
        ),
    )


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Reset the cached ``get_settings()`` between tests, suite-wide.

    Previously duplicated as a byte-identical per-file autouse fixture in ~34
    test modules (optimization slice 34 F1). ``get_settings`` is a side-effect
    free ``lru_cache``, so clearing it at every test boundary only increases
    isolation. Modules that need to clear additional caches (e.g. a tool's token
    cache) define their own same-named autouse fixture, which overrides this one
    for that module.
    """
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def api_client_builder(monkeypatch: pytest.MonkeyPatch) -> ApiTestClientBuilder:
    api_module._reset_auth_failure_rate_limiter_for_tests()
    yield ApiTestClientBuilder(monkeypatch)
    api_module._reset_auth_failure_rate_limiter_for_tests()
