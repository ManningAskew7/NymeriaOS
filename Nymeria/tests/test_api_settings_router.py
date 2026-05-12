"""Regression tests for the extracted settings/model API router."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.triggers import api as api_module


@dataclass
class FakeLLMConfig:
    provider: str = "anthropic"
    model: str = "claude-test"
    max_tokens: int | None = 4096
    api_key: str | None = None


@dataclass
class FakeSettings:
    project_root: Path
    data_dir: Path
    llm_provider: str = "anthropic"
    llm_model: str = "claude-test"
    llm_fast_model: str | None = None
    llm_temperature: float = 1.0
    llm_max_tokens: int | None = None
    llm_top_p: float | None = None
    llm_top_k: int | None = None
    llm_frequency_penalty: float | None = None
    llm_presence_penalty: float | None = None
    llm_reasoning_effort: str | None = None
    llm_extended_thinking: bool = False
    llm_use_model_defaults: bool = False
    llm_base_url: str | None = None
    openai_api_mode: str | None = "responses"
    llm_stream_max_retries: int = 2
    llm_stream_retry_initial_delay: float = 1.0
    llm_stream_retry_max_delay: float = 8.0
    context_management: str = "none"
    compact_threshold: float = 0.8
    compact_keep_messages: int = 4
    compact_model: str | None = None
    sliding_window_cycles: int = 20
    tool_output_max_chars: int = 100000
    log_level: str = "INFO"
    watchdog_enabled: bool = True
    watchdog_interval_minutes: int = 5
    todo_staleness_minutes: int = 20
    activity_retention_hours: int = 12
    tts_provider: str = "none"
    tts_base_url: str | None = None
    tts_api_key: str | None = None
    tts_model: str = "tts-1-hd"
    tts_voice: str = "nova"
    tts_output_format: str = "mp3"
    tts_speed: float = 1.0
    stt_provider: str = "none"
    stt_base_url: str | None = None
    stt_api_key: str | None = None
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_language: str | None = None
    voice_default_thread_id: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = "anthropic-token"
    anthropic_direct_api_key: str | None = None
    openrouter_api_key: str | None = None
    embedding_api_key: str | None = None
    perplexity_api_key: str | None = None
    perplexity_search_model: str = "sonar-pro"
    gemini_api_key: str | None = None
    gemini_extraction_model: str = "gemini-test"
    nymeria_api_key: str | None = "legacy-secret"
    nymeria_data_dir: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    postgres_uri: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
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

    def get_api_key_for_provider(self) -> str | None:
        if self.llm_provider == "openai":
            return self.openai_api_key
        if self.llm_provider == "anthropic":
            return self.anthropic_direct_api_key or self.anthropic_api_key
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key
        return None


class FakeSettingsProvider:
    def __init__(self, settings: FakeSettings):
        self.settings = settings
        self.cache_clear_count = 0

    def __call__(self) -> FakeSettings:
        return self.settings

    def cache_clear(self) -> None:
        self.cache_clear_count += 1
        self.settings = replace(
            self.settings,
            llm_model=os.environ.get("LLM_MODEL", self.settings.llm_model),
            llm_fast_model=os.environ.get(
                "LLM_FAST_MODEL",
                self.settings.llm_fast_model,
            ),
            tts_provider=os.environ.get("TTS_PROVIDER", self.settings.tts_provider),
            anthropic_api_key=os.environ.get(
                "ANTHROPIC_API_KEY",
                self.settings.anthropic_api_key,
            ),
            anthropic_direct_api_key=os.environ.get(
                "ANTHROPIC_DIRECT_API_KEY",
                self.settings.anthropic_direct_api_key,
            ),
            openai_api_key=os.environ.get(
                "OPENAI_API_KEY",
                self.settings.openai_api_key,
            ),
            openrouter_api_key=os.environ.get(
                "OPENROUTER_API_KEY",
                self.settings.openrouter_api_key,
            ),
            embedding_api_key=os.environ.get(
                "EMBEDDING_API_KEY",
                self.settings.embedding_api_key,
            ),
            gemini_api_key=os.environ.get(
                "GEMINI_API_KEY",
                self.settings.gemini_api_key,
            ),
            perplexity_api_key=os.environ.get(
                "PERPLEXITY_API_KEY",
                self.settings.perplexity_api_key,
            ),
        )


class FakeAgent:
    def __init__(self, data_dir: Path, llm_config: FakeLLMConfig | None = None):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0
        self.llm_config = llm_config or FakeLLMConfig()
        self.settings = None
        self._graph_cache_lock = threading.Lock()
        self._user_graphs = {"user": object()}
        self._async_user_graphs = {"user": object()}
        self._base_system_prompt = "base"
        self._default_graph = None
        self._default_async_graph = None
        self.graph_rebuilds: list[str] = []

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1

    def _get_llm_config_for_thread(self, thread_id: str) -> FakeLLMConfig:
        return self.llm_config

    def _build_graph_with_prompt(self, prompt: str):
        self.graph_rebuilds.append("sync")
        return {"prompt": prompt, "kind": "sync"}

    def _build_async_graph_with_prompt(self, prompt: str):
        self.graph_rebuilds.append("async")
        return {"prompt": prompt, "kind": "async"}


def _client(
    monkeypatch,
    tmp_path: Path,
    *,
    settings: FakeSettings | None = None,
    llm_config: FakeLLMConfig | None = None,
) -> tuple[TestClient, FakeAgent, str, FakeSettingsProvider]:
    settings = settings or FakeSettings(project_root=tmp_path, data_dir=tmp_path)
    provider = FakeSettingsProvider(settings)
    monkeypatch.setattr(api_module, "get_settings", provider)

    agent = FakeAgent(settings.data_dir, llm_config=llm_config)
    client = TestClient(api_module.create_api_app(agent))
    agent.accounts_repo.create_user(
        "admin",
        "admin@example.com",
        "Admin",
        role="admin",
    )
    token = agent.accounts_repo.issue_token("admin")
    return client, agent, token, provider


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeAsyncClient:
    response_status = 200
    response_body: dict | None = None
    calls: list[dict] = []

    def __init__(self, *, timeout: float):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append({
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": self.timeout,
        })
        request = httpx.Request("POST", url)
        return httpx.Response(
            self.response_status,
            json=self.response_body or {"ok": True},
            request=request,
        )


def test_llm_provider_test_is_admin_only_and_does_not_call_provider(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    _agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    user_token = _agent.accounts_repo.issue_token("alice")

    response = client.post(
        "/settings/llm/test",
        headers=_auth(user_token),
        json={
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "api_key": "sk-test",
        },
    )

    assert response.status_code == 403
    assert FakeAsyncClient.calls == []


def test_llm_provider_test_normalizes_openai_cliproxy_base_url(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"ok": True}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test",
        headers=_auth(token),
        json={
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "api_key": "cpx-secret-key",
            "llm_base_url": "http://localhost:8317",
            "openai_api_mode": "responses",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "provider": "openai",
        "model": "gpt-test",
        "message": "Provider test succeeded.",
        "openai_api_mode": "responses",
        "status_code": None,
        "error_type": None,
    }
    assert FakeAsyncClient.calls[0]["url"] == "http://localhost:8317/v1/responses"
    assert FakeAsyncClient.calls[0]["headers"]["Authorization"] == "Bearer cpx-secret-key"
    assert FakeAsyncClient.calls[0]["json"]["max_output_tokens"] == 16


def test_llm_provider_test_uses_anthropic_proxy_root_and_cloak_header(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"ok": True}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test",
        headers=_auth(token),
        json={
            "llm_provider": "anthropic",
            "llm_model": "claude-test",
            "api_key": "cpx-secret-key",
            "llm_base_url": "http://localhost:8318",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert FakeAsyncClient.calls[0]["url"] == "http://localhost:8318/v1/messages"
    assert FakeAsyncClient.calls[0]["headers"]["x-api-key"] == "cpx-secret-key"
    assert FakeAsyncClient.calls[0]["headers"]["User-Agent"] == "claude-cli/2.1.113"


def test_llm_provider_test_redacts_secret_from_failure_response(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.response_status = 401
    FakeAsyncClient.response_body = {
        "error": {"message": "Rejected gatekeeper key cpx-secret-key"}
    }
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test",
        headers=_auth(token),
        json={
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "api_key": "cpx-secret-key",
            "llm_base_url": "http://localhost:8317/v1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status_code"] == 401
    assert body["error_type"] == "http_error"
    assert "cpx-secret-key" not in body["message"]
    assert "[redacted]" in body["message"]


def test_llm_provider_test_does_not_persist_submitted_key(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"ok": True}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test",
        headers=_auth(token),
        json={
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "api_key": "sk-transient-test",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert not (tmp_path / ".env").exists()
    assert os.environ.get("OPENAI_API_KEY") is None
    assert provider.cache_clear_count == 0


def test_runtime_diagnostics_uses_configured_project_root_for_env_sources(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text("LLM_MODEL=from-test-root\n", encoding="utf-8")
    llm_config = FakeLLMConfig(provider="anthropic", model="claude-test")
    client, _agent, token, _provider = _client(
        monkeypatch,
        tmp_path,
        llm_config=llm_config,
    )

    response = client.get("/settings/llm/runtime", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    assert body["source_env_files"] == [str(tmp_path / ".env")]


def test_patch_settings_hot_reloads_env_and_rebuilds_graphs(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "LLM_MODEL=old-model\nTTS_PROVIDER=none\n",
        encoding="utf-8",
    )
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_model": "new-model", "tts_provider": "openai"},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["llm_model", "tts_provider"]
    assert provider.cache_clear_count == 1
    assert "LLM_MODEL=new-model" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert os.environ["LLM_MODEL"] == "new-model"
    assert agent.settings.llm_model == "new-model"
    assert agent._user_graphs == {}
    assert agent._async_user_graphs == {}
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_updates_existing_config_env_for_packaged_runtime(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "config.env").write_text(
        "LLM_MODEL=old-model\nTTS_PROVIDER=none\n",
        encoding="utf-8",
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_model": "new-model"},
    )

    assert response.status_code == 200
    config = (tmp_path / "config.env").read_text(encoding="utf-8")
    assert "LLM_MODEL=new-model" in config
    assert not (tmp_path / ".env").exists()


def test_patch_settings_accepts_provider_credentials_without_echoing_secrets(
    tmp_path: Path,
    monkeypatch,
):
    for env_var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_DIRECT_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "EMBEDDING_API_KEY",
        "GEMINI_API_KEY",
        "PERPLEXITY_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)

    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=old-openai-key\n",
        encoding="utf-8",
    )
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    payload = {
        "anthropic_api_key": "cpx-anthropic-new",
        "anthropic_direct_api_key": "sk-ant-direct-new",
        "openai_api_key": "sk-openai-new",
        "openrouter_api_key": "sk-or-new",
        "embedding_api_key": "sk-embedding-new",
        "gemini_api_key": "sk-gemini-new",
        "perplexity_api_key": "pplx-new",
    }

    response = client.patch("/settings", headers=_auth(token), json=payload)

    assert response.status_code == 200
    body = response.json()
    assert set(body["updated"]) == set(payload)
    for secret in payload.values():
        assert secret not in response.text

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=cpx-anthropic-new" in env_text
    assert "ANTHROPIC_DIRECT_API_KEY=sk-ant-direct-new" in env_text
    assert "OPENAI_API_KEY=sk-openai-new" in env_text
    assert "OPENROUTER_API_KEY=sk-or-new" in env_text
    assert "EMBEDDING_API_KEY=sk-embedding-new" in env_text
    assert "GEMINI_API_KEY=sk-gemini-new" in env_text
    assert "PERPLEXITY_API_KEY=pplx-new" in env_text
    assert os.environ["OPENAI_API_KEY"] == "sk-openai-new"
    assert provider.cache_clear_count == 1
    assert agent.settings.openai_api_key == "sk-openai-new"
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_provider_credentials_are_admin_only(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    user_token = agent.accounts_repo.issue_token("alice")

    response = client.patch(
        "/settings",
        headers=_auth(user_token),
        json={"openai_api_key": "sk-user-should-not-write"},
    )

    assert response.status_code == 403
    env_path = tmp_path / ".env"
    if env_path.exists():
        assert "sk-user-should-not-write" not in env_path.read_text(encoding="utf-8")


def test_get_settings_does_not_return_provider_secret_fields(
    tmp_path: Path,
    monkeypatch,
):
    secrets = {
        "openai_api_key": "sk-get-openai",
        "anthropic_api_key": "sk-get-anthropic",
        "anthropic_direct_api_key": "sk-get-anthropic-direct",
        "openrouter_api_key": "sk-get-openrouter",
        "embedding_api_key": "sk-get-embedding",
        "gemini_api_key": "sk-get-gemini",
        "perplexity_api_key": "pplx-get-secret",
    }
    settings = FakeSettings(project_root=tmp_path, data_dir=tmp_path, **secrets)
    client, _agent, token, _provider = _client(
        monkeypatch,
        tmp_path,
        settings=settings,
    )

    response = client.get("/settings", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    for field, secret in secrets.items():
        assert field not in body
        assert secret not in response.text


def test_env_settings_list_masks_provider_and_capability_keys(
    tmp_path: Path,
    monkeypatch,
):
    secrets = {
        "openai_api_key": "sk-env-openai-secret",
        "anthropic_api_key": "sk-env-anthropic-secret",
        "anthropic_direct_api_key": "sk-env-anthropic-direct-secret",
        "openrouter_api_key": "sk-env-openrouter-secret",
        "embedding_api_key": "sk-env-embedding-secret",
        "gemini_api_key": "sk-env-gemini-secret",
        "perplexity_api_key": "pplx-env-secret",
    }
    settings = FakeSettings(project_root=tmp_path, data_dir=tmp_path, **secrets)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path, settings=settings)

    response = client.get("/settings/env", headers=_auth(token))

    assert response.status_code == 200
    entries_by_name = {entry["name"]: entry for entry in response.json()["entries"]}
    for field, secret in secrets.items():
        entry = entries_by_name[field]
        assert entry["is_secret"] is True
        assert entry["is_set"] is True
        assert entry["value"] != secret
        assert entry["value"].startswith(secret[:4])
        assert entry["value"].endswith(secret[-3:])
        assert "..." in entry["value"]
        assert secret not in response.text
