"""Regression tests for the extracted settings/model API router."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

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
    api_docs_enabled: bool = False
    cors_origins_list: list[str] | None = None

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = ["*"]

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
            tts_provider=os.environ.get("TTS_PROVIDER", self.settings.tts_provider),
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
