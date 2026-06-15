"""Regression tests for the extracted settings/model API router."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from nymeria.config.model_capabilities import get_context_limit, get_model_defaults
from nymeria.config.settings import DEFAULT_LLM_FALLBACK_MODELS
from nymeria.core.accounts import AccountsRepo
from nymeria.triggers import api as api_module


@dataclass
class FakeLLMConfig:
    provider: str = "anthropic"
    model: str = "claude-test"
    max_tokens: int | None = 4096
    api_key: str | None = None
    provider_route: str | None = None


@dataclass
class FakeSettings:
    project_root: Path
    data_dir: Path
    llm_provider: str = "anthropic"
    llm_model: str = "claude-test"
    llm_fast_model: str | None = None
    llm_smart_model: str | None = None
    llm_fallback_models: str = DEFAULT_LLM_FALLBACK_MODELS
    llm_temperature: float = 1.0
    llm_max_tokens: int | None = None
    llm_top_p: float | None = None
    llm_top_k: int | None = None
    llm_frequency_penalty: float | None = None
    llm_presence_penalty: float | None = None
    llm_reasoning_effort: str | None = None
    llm_extended_thinking: bool = False
    dynamic_tool_binding: bool = False
    llm_use_model_defaults: bool = False
    llm_base_url: str | None = None
    llm_context_length: int | None = None
    llm_ollama_num_ctx: int | None = None
    llm_provider_route: str | None = None
    openai_api_mode: str | None = "responses"
    llm_stream_max_retries: int = 2
    llm_stream_retry_initial_delay: float = 1.0
    llm_stream_retry_max_delay: float = 8.0
    llm_fallback_hold_seconds: int = 7200
    context_management: str = "none"
    compact_threshold: float = 0.8
    compact_threshold_mode: str = "percentage"
    compact_threshold_tokens: int = 100_000
    compact_keep_messages: int = 4
    compact_model: str | None = None
    fetch_summary_provider: str | None = None
    fetch_summary_model: str | None = None
    fetch_summary_base_url: str | None = None
    sliding_window_cycles: int = 20
    tool_output_max_chars: int = 100000
    memory_char_limit: int = 8000
    memory_max_entries: int = 100
    memory_value_max_chars: int = 1000
    agent_max_iterations: int = 500
    log_level: str = "INFO"
    watchdog_enabled: bool = True
    watchdog_interval_minutes: int = 5
    todo_staleness_minutes: int = 20
    activity_retention_hours: int = 12
    dream_default_min_interval_hours: int = 6
    dream_default_min_idle_minutes: int = 30
    dream_default_min_turns_since_last: int = 10
    dream_default_model: str | None = None
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
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int | None = None
    rag_retrieval_mode: str = "hybrid"
    rag_embed_tool_results: bool = True
    rag_rerank_enabled: bool = False
    rag_rerank_provider: str = "llm"
    rag_rerank_model: str | None = None
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

    @property
    def soul_path(self) -> Path:
        return self.data_dir / "soul.md"

    @property
    def system_prompt_override_path(self) -> Path:
        return self.data_dir / "system_prompt.md"

    def load_soul(self) -> str:
        override = self.system_prompt_override_path
        if override.exists():
            text = override.read_text(encoding="utf-8").strip()
            if text:
                return text
        if self.soul_path.exists():
            return self.soul_path.read_text(encoding="utf-8")
        return "You are Nymeria, a helpful AI assistant."

    @property
    def dream_prompt_path(self) -> Path:
        return self.data_dir / "dream_prompt_default.md"

    @property
    def dream_prompt_override_path(self) -> Path:
        return self.data_dir / "dream_prompt.md"

    @property
    def dream_kickoff_path(self) -> Path:
        return self.data_dir / "dream_kickoff_default.md"

    @property
    def dream_kickoff_override_path(self) -> Path:
        return self.data_dir / "dream_kickoff.md"

    def _load_with_override(self, default_path: Path, override_path: Path, fallback: str) -> str:
        if override_path.exists():
            text = override_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        if default_path.exists():
            return default_path.read_text(encoding="utf-8")
        return fallback

    def load_dream_prompt(self) -> str:
        return self._load_with_override(
            self.dream_prompt_path, self.dream_prompt_override_path, "default-dream-sys"
        )

    def load_dream_kickoff_prompt(self) -> str:
        return self._load_with_override(
            self.dream_kickoff_path,
            self.dream_kickoff_override_path,
            "default-dream-kickoff",
        )


class FakeSettingsProvider:
    def __init__(self, settings: FakeSettings):
        self.settings = settings
        self.cache_clear_count = 0

    def __call__(self) -> FakeSettings:
        return self.settings

    def cache_clear(self) -> None:
        self.cache_clear_count += 1

        def env_float(name: str, default: float) -> float:
            value = os.environ.get(name)
            return default if value is None else float(value)

        def env_int(name: str, default: int) -> int:
            value = os.environ.get(name)
            return default if value is None else int(value)

        def env_optional_int(name: str, default: int | None) -> int | None:
            value = os.environ.get(name)
            return default if value is None else int(value) if value else None

        def env_optional_str(name: str, default: str | None) -> str | None:
            value = os.environ.get(name)
            return default if value is None else (value or None)

        self.settings = replace(
            self.settings,
            llm_model=os.environ.get("LLM_MODEL", self.settings.llm_model),
            llm_reasoning_effort=env_optional_str(
                "LLM_REASONING_EFFORT",
                self.settings.llm_reasoning_effort,
            ),
            llm_fast_model=os.environ.get(
                "LLM_FAST_MODEL",
                self.settings.llm_fast_model,
            ),
            llm_fallback_models=os.environ.get(
                "LLM_FALLBACK_MODELS",
                self.settings.llm_fallback_models,
            ),
            tts_provider=os.environ.get("TTS_PROVIDER", self.settings.tts_provider),
            compact_threshold=env_float(
                "COMPACT_THRESHOLD",
                self.settings.compact_threshold,
            ),
            compact_threshold_mode=os.environ.get(
                "COMPACT_THRESHOLD_MODE",
                self.settings.compact_threshold_mode,
            ),
            compact_threshold_tokens=env_int(
                "COMPACT_THRESHOLD_TOKENS",
                self.settings.compact_threshold_tokens,
            ),
            llm_context_length=env_optional_int(
                "LLM_CONTEXT_LENGTH",
                self.settings.llm_context_length,
            ),
            llm_ollama_num_ctx=env_optional_int(
                "LLM_OLLAMA_NUM_CTX",
                self.settings.llm_ollama_num_ctx,
            ),
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
            memory_char_limit=env_int(
                "MEMORY_CHAR_LIMIT",
                self.settings.memory_char_limit,
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
        self.prompt_reloads = 0

    def reload_base_system_prompt(self) -> str:
        if self.settings is not None:
            self._base_system_prompt = self.settings.load_soul()
        self.prompt_reloads += 1
        return self._base_system_prompt

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

    async def get(self, url: str, *, headers: dict):
        self.calls.append({
            "url": url,
            "headers": headers,
            "timeout": self.timeout,
        })
        request = httpx.Request("GET", url)
        return httpx.Response(
            self.response_status,
            json=self.response_body or {"data": []},
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
        "provider_route": "native",
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


def test_get_llm_provider_catalog_includes_openai_compatible_providers(
    tmp_path: Path,
    monkeypatch,
):
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get("/settings/llm/providers", headers=_auth(token))

    assert response.status_code == 200
    providers = {entry["id"]: entry for entry in response.json()}
    assert providers["openai"]["supports_responses"] is True
    assert providers["google"]["supported_routes"] == ["native", "openai_compat"]
    assert providers["google"]["default_route"] == "native"
    assert providers["ollama"]["supported_routes"] == ["native", "openai_compat"]
    assert providers["ollama"]["openai_compat_base_url"] == "http://localhost:11434/v1"
    assert providers["groq"]["api_format"] == "openai_chat"
    assert "GROQ_API_KEY" in providers["groq"]["api_key_env_vars"]
    assert providers["cohere"]["default_base_url"] == "https://api.cohere.ai/compatibility/v1"
    assert providers["stepfun"]["default_base_url"] == "https://api.stepfun.ai/v1"
    assert providers["alibaba-coding-plan"]["default_base_url"] == "https://coding-intl.dashscope.aliyuncs.com/v1"
    assert providers["byteplus"]["default_base_url"] == "https://ark.ap-southeast.bytepluses.com/api/v3"
    assert providers["v0"]["docs_url"] == "https://vercel.com/docs/v0/api"


def test_available_models_uses_provider_endpoint_and_caches_metadata(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {
        "data": [
            {
                "id": "provider/test-context-model",
                "name": "Test Context Model",
                "context_length": 64000,
                "top_provider": {"max_completion_tokens": 4096},
                "supported_parameters": ["tools", "temperature"],
                "default_parameters": {
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "frequency_penalty": 0.2,
                },
                "architecture": {
                    "input_modalities": ["text"],
                    "tokenizer": "GPT",
                },
            }
        ]
    }
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=lmstudio&base_url=http://localhost:1234/v1",
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert FakeAsyncClient.calls[0]["url"] == "http://localhost:1234/v1/models"
    assert body[0]["id"] == "provider/test-context-model"
    assert body[0]["context_length"] == 64000
    assert body[0]["max_completion_tokens"] == 4096
    assert body[0]["supported_parameters"] == ["tools", "temperature"]
    assert body[0]["default_temperature"] == 0.7
    assert body[0]["default_top_p"] == 0.95
    assert body[0]["default_frequency_penalty"] == 0.2
    # Effort ladder computed with the effective provider of the listing
    # (lmstudio here: unknown family, conservative default ladder).
    assert body[0]["supported_reasoning_efforts"] == [
        "off", "low", "medium", "high",
    ]
    assert body[0]["max_reasoning_effort"] == "high"
    assert get_context_limit("provider/test-context-model") == 64000
    assert get_model_defaults("provider/test-context-model") == {
        "temperature": 0.7,
        "top_p": 0.95,
        "frequency_penalty": 0.2,
    }


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


def test_patch_settings_hot_reloads_fallback_models(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text("LLM_FALLBACK_MODELS=old-model\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_fallback_models": DEFAULT_LLM_FALLBACK_MODELS},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["llm_fallback_models"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"LLM_FALLBACK_MODELS={DEFAULT_LLM_FALLBACK_MODELS}" in env_text
    assert os.environ["LLM_FALLBACK_MODELS"] == DEFAULT_LLM_FALLBACK_MODELS
    assert provider.cache_clear_count == 1
    assert agent.settings.llm_fallback_models == DEFAULT_LLM_FALLBACK_MODELS
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_hot_reloads_compact_token_threshold(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "COMPACT_THRESHOLD_MODE=percentage\nCOMPACT_THRESHOLD_TOKENS=100000\n",
        encoding="utf-8",
    )
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={
            "compact_threshold_mode": "tokens",
            "compact_threshold_tokens": 250000,
            "compact_threshold": 0.5,
        },
    )

    assert response.status_code == 200
    assert response.json()["updated"] == [
        "compact_threshold",
        "compact_threshold_mode",
        "compact_threshold_tokens",
    ]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "COMPACT_THRESHOLD_MODE=tokens" in env_text
    assert "COMPACT_THRESHOLD_TOKENS=250000" in env_text
    assert "COMPACT_THRESHOLD=0.5" in env_text
    assert os.environ["COMPACT_THRESHOLD_MODE"] == "tokens"
    assert os.environ["COMPACT_THRESHOLD_TOKENS"] == "250000"
    assert provider.cache_clear_count == 1
    assert agent.settings.compact_threshold_mode == "tokens"
    assert agent.settings.compact_threshold_tokens == 250000
    assert agent.settings.compact_threshold == 0.5
    assert agent.graph_rebuilds == []


def test_patch_settings_hot_reloads_memory_char_limit(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text("MEMORY_CHAR_LIMIT=8000\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"memory_char_limit": 12000},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["memory_char_limit"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MEMORY_CHAR_LIMIT=12000" in env_text
    assert os.environ["MEMORY_CHAR_LIMIT"] == "12000"
    assert provider.cache_clear_count == 1
    assert agent.settings.memory_char_limit == 12000
    assert agent.graph_rebuilds == []


def test_patch_settings_can_clear_local_llm_context_overrides(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "LLM_CONTEXT_LENGTH=64000\nLLM_OLLAMA_NUM_CTX=32000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_CONTEXT_LENGTH", "64000")
    monkeypatch.setenv("LLM_OLLAMA_NUM_CTX", "32000")
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        llm_context_length=64000,
        llm_ollama_num_ctx=32000,
    )
    client, agent, token, provider = _client(
        monkeypatch,
        tmp_path,
        settings=settings,
    )

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_context_length": None, "llm_ollama_num_ctx": None},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["llm_context_length", "llm_ollama_num_ctx"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LLM_CONTEXT_LENGTH=" in env_text
    assert "LLM_OLLAMA_NUM_CTX=" in env_text
    assert os.environ["LLM_CONTEXT_LENGTH"] == ""
    assert os.environ["LLM_OLLAMA_NUM_CTX"] == ""
    assert provider.cache_clear_count == 1
    assert agent.settings.llm_context_length is None
    assert agent.settings.llm_ollama_num_ctx is None
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_can_clear_reasoning_effort(
    tmp_path: Path,
    monkeypatch,
):
    """Explicit null resets a saved effort (e.g. "off") to provider defaults."""
    (tmp_path / ".env").write_text("LLM_REASONING_EFFORT=off\n", encoding="utf-8")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "off")
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        llm_reasoning_effort="off",
    )
    client, agent, token, provider = _client(
        monkeypatch,
        tmp_path,
        settings=settings,
    )

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_reasoning_effort": None},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["llm_reasoning_effort"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "LLM_REASONING_EFFORT=off" not in env_text
    assert "LLM_REASONING_EFFORT=" in env_text
    assert os.environ["LLM_REASONING_EFFORT"] == ""
    assert provider.cache_clear_count == 1
    assert agent.settings.llm_reasoning_effort is None
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_rejects_invalid_compact_token_threshold(
    tmp_path: Path,
    monkeypatch,
):
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={
            "compact_threshold_mode": "tokens",
            "compact_threshold_tokens": 999,
        },
    )

    assert response.status_code == 422
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


def test_patch_settings_preserves_untouched_lines_and_writes_0600(
    tmp_path: Path,
    monkeypatch,
):
    # Reconfigure must overlay only the changed key onto the existing file: the
    # hand-written comment, the credential-vault key, and unrelated lines survive
    # verbatim, and the env file (which holds secrets) is rewritten 0600. Both go
    # through the shared writer in `config/env_file.py`.
    monkeypatch.delenv("LLM_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# hand-written config, keep me\n"
        "NYMERIA_SECRETS_KEY=k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=\n"
        "LLM_MODEL=old-model\n"
        "SOME_UNMANAGED_KEY=leave-this-alone\n",
        encoding="utf-8",
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings", headers=_auth(token), json={"llm_model": "new-model"}
    )

    assert response.status_code == 200
    env_text = env_path.read_text(encoding="utf-8")
    assert "LLM_MODEL=new-model" in env_text
    assert "LLM_MODEL=old-model" not in env_text
    assert "# hand-written config, keep me" in env_text
    assert "NYMERIA_SECRETS_KEY=k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=" in env_text
    assert "SOME_UNMANAGED_KEY=leave-this-alone" in env_text
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_patch_settings_quotes_values_with_spaces(
    tmp_path: Path,
    monkeypatch,
):
    # A value with a space must be quoted so python-dotenv and compose parse it
    # whole. The pre-consolidation handler wrote it unquoted (a latent bug); this
    # is the regression guard. setenv registers LLM_MODEL for teardown cleanup.
    monkeypatch.setenv("LLM_MODEL", "old")
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=old\n", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings", headers=_auth(token), json={"llm_model": "model with space"}
    )

    assert response.status_code == 200
    env_text = env_path.read_text(encoding="utf-8")
    assert 'LLM_MODEL="model with space"' in env_text
    # The quoted file value must reach os.environ UN-quoted, else the hot-reloaded
    # Settings (env source outranks dotenv) would carry literal quotes.
    assert os.environ["LLM_MODEL"] == "model with space"


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


def test_get_settings_is_admin_only(
    tmp_path: Path,
    monkeypatch,
):
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    user_token = agent.accounts_repo.issue_token("alice")

    response = client.get("/settings", headers=_auth(user_token))

    assert response.status_code == 403


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


def test_system_prompt_override_lifecycle(tmp_path: Path, monkeypatch):
    client, agent, admin_token, provider = _client(monkeypatch, tmp_path)
    agent.settings = provider.settings
    provider.settings.soul_path.write_text("DEFAULT SOUL", encoding="utf-8")

    # GET: the shipped default is in effect, no override.
    got = client.get("/settings/system-prompt", headers=_auth(admin_token))
    assert got.status_code == 200
    body = got.json()
    assert body["content"] == "DEFAULT SOUL"
    assert body["default_content"] == "DEFAULT SOUL"
    assert body["is_override"] is False

    # PUT: set an override and hot-reload the agent.
    put = client.put(
        "/settings/system-prompt",
        headers=_auth(admin_token),
        json={"content": "CUSTOM PERSONA"},
    )
    assert put.status_code == 200
    assert put.json()["content"] == "CUSTOM PERSONA"
    assert put.json()["is_override"] is True
    assert (
        provider.settings.system_prompt_override_path.read_text(encoding="utf-8")
        == "CUSTOM PERSONA"
    )
    assert agent.prompt_reloads == 1
    assert agent._base_system_prompt == "CUSTOM PERSONA"

    # GET reflects the override.
    assert (
        client.get("/settings/system-prompt", headers=_auth(admin_token)).json()["content"]
        == "CUSTOM PERSONA"
    )

    # DELETE: drop the override, restore the shipped default, reload again.
    deleted = client.delete("/settings/system-prompt", headers=_auth(admin_token))
    assert deleted.status_code == 200
    assert deleted.json()["is_override"] is False
    assert deleted.json()["content"] == "DEFAULT SOUL"
    assert not provider.settings.system_prompt_override_path.exists()
    assert agent.prompt_reloads == 2
    assert agent._base_system_prompt == "DEFAULT SOUL"

    # Blank PUT also clears the override.
    client.put(
        "/settings/system-prompt",
        headers=_auth(admin_token),
        json={"content": "X"},
    )
    blanked = client.put(
        "/settings/system-prompt",
        headers=_auth(admin_token),
        json={"content": "   "},
    )
    assert blanked.status_code == 200
    assert blanked.json()["is_override"] is False
    assert not provider.settings.system_prompt_override_path.exists()


def test_system_prompt_is_admin_only(tmp_path: Path, monkeypatch):
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    user_token = agent.accounts_repo.issue_token("alice")

    assert client.get("/settings/system-prompt", headers=_auth(user_token)).status_code == 403
    put = client.put(
        "/settings/system-prompt",
        headers=_auth(user_token),
        json={"content": "nope"},
    )
    assert put.status_code == 403


# -- H-4: env-var secret masking ------------------------------------------


def test_is_secret_setting_key_derivation():
    from nymeria.api.routers.settings import _is_secret_setting_key

    # Suffix-derived secrets not necessarily in the explicit allowlist.
    for key in (
        "jwt_secret",
        "stripe_secret_key",
        "s3_secret_access_key",
        "twilio_auth_token",
        "sendgrid_api_key",
        "github_token",
        "hubspot_access_token",
        "crypto_sign_private_key",
        "some_app_password",
        "x_refresh_token",
    ):
        assert _is_secret_setting_key(key) is True, key

    # Explicit allowlist entries whose names lack a credential suffix.
    for key in ("postgres_uri", "redis_url", "discord_webhook_url"):
        assert _is_secret_setting_key(key) is True, key

    # Non-secret keys must NOT be masked (suffix matching, not substring).
    for key in (
        "llm_max_tokens",
        "max_output_tokens",
        "llm_base_url",
        "llm_model",
        "embedding_model",
        "watchdog_interval_minutes",
    ):
        assert _is_secret_setting_key(key) is False, key


def test_get_env_var_masks_secret_by_default(monkeypatch, tmp_path):
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        openai_api_key="sk-secret-abcdef1234567890",
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path, settings=settings)

    resp = client.get("/settings/env/openai_api_key", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_secret"] is True
    assert body["value"] != "sk-secret-abcdef1234567890"
    assert "..." in body["value"]


def test_get_env_var_reveals_with_explicit_flag(monkeypatch, tmp_path):
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        openai_api_key="sk-secret-abcdef1234567890",
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path, settings=settings)

    resp = client.get(
        "/settings/env/openai_api_key", params={"reveal": "true"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == "sk-secret-abcdef1234567890"


def test_get_dream_prompts_returns_defaults_when_no_override(monkeypatch, tmp_path):
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    resp = client.get("/settings/dream-prompts", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["system"]["is_override"] is False
    assert body["kickoff"]["is_override"] is False
    # Falls back to the loader's hardcoded default when no file is present.
    assert body["system"]["content"] == "default-dream-sys"
    assert body["kickoff"]["content"] == "default-dream-kickoff"


def test_update_dream_prompts_writes_and_clears_overrides(monkeypatch, tmp_path):
    client, _agent, token, provider = _client(monkeypatch, tmp_path)

    # Write the system override; leave kickoff untouched (omitted field).
    resp = client.put(
        "/settings/dream-prompts",
        json={"system": "MY DREAM SYSTEM"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["system"]["content"] == "MY DREAM SYSTEM"
    assert body["system"]["is_override"] is True
    assert body["kickoff"]["is_override"] is False
    assert provider.settings.dream_prompt_override_path.exists()

    # Blank clears the override (resets to default).
    resp = client.put(
        "/settings/dream-prompts", json={"system": "  "}, headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["system"]["is_override"] is False
    assert body["system"]["content"] == "default-dream-sys"
    assert not provider.settings.dream_prompt_override_path.exists()


def test_dream_prompts_require_admin(monkeypatch, tmp_path):
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user(
        "bob", "bob@example.com", "Bob", role="user"
    )
    user_token = agent.accounts_repo.issue_token("bob")

    resp = client.get("/settings/dream-prompts", headers=_auth(user_token))
    assert resp.status_code == 403


def test_patch_settings_s3_credential_writes_aws_env_var(tmp_path: Path, monkeypatch):
    # The S3 fields map to AWS SDK names (the override table), and the Settings model
    # now reads them back from the same names. End-to-end: PATCHing s3_access_key_id
    # must land as AWS_ACCESS_KEY_ID in the env file, not S3_ACCESS_KEY_ID.
    # setenv (not delenv) so the os.environ writes the applier makes are reverted on
    # teardown and cannot leak into other tests that load real Settings. _sync_process_env
    # re-syncs EVERY mapped var present in the merged file, so the seeded LLM_MODEL line
    # is written to os.environ too and must also be registered for cleanup.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("LLM_MODEL", "keep")
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=keep\n", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings", headers=_auth(token), json={"s3_access_key_id": "AKIA-test-123"}
    )

    assert response.status_code == 200
    env_text = env_path.read_text(encoding="utf-8")
    assert "AWS_ACCESS_KEY_ID=AKIA-test-123" in env_text
    assert "S3_ACCESS_KEY_ID=" not in env_text
    assert "LLM_MODEL=keep" in env_text


def _backend_client(tmp_path: Path, settings: FakeSettings | None = None):
    """A CommandBackendClient (the in-process slash-command path) wired to fakes."""
    from nymeria.core.command_service import CommandBackendClient, _CommandBackendUser

    settings = settings or FakeSettings(project_root=tmp_path, data_dir=tmp_path)
    provider = FakeSettingsProvider(settings)
    agent = FakeAgent(tmp_path)
    user = _CommandBackendUser(id="admin", role="admin")
    return CommandBackendClient(agent, user=user, settings_fn=provider), agent


def test_command_backend_update_settings_writes_atomic_quoted_0600(
    tmp_path: Path, monkeypatch
):
    # The slash-command settings path (CommandBackendClient.update_settings) now shares
    # the PATCH applier, so it gains the Phase-1 guarantees its old inline write_text()
    # lacked: atomic 0600, quoted special-char values, untouched lines and the secrets
    # key preserved. setenv (not delenv) registers LLM_MODEL for teardown so the
    # applier's os.environ write is reverted and cannot leak into other tests.
    monkeypatch.setenv("LLM_MODEL", "old")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep me\n"
        "NYMERIA_SECRETS_KEY=k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=\n"
        "LLM_MODEL=old\n",
        encoding="utf-8",
    )
    backend, _agent = _backend_client(tmp_path)

    result = asyncio.run(backend.update_settings(llm_model="model with space"))

    assert result["restart_required"] is False
    assert result["updated"] == ["llm_model"]
    env_text = env_path.read_text(encoding="utf-8")
    assert 'LLM_MODEL="model with space"' in env_text
    assert "LLM_MODEL=old" not in env_text
    assert "# keep me" in env_text
    assert "NYMERIA_SECRETS_KEY=k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=" in env_text
    assert env_path.stat().st_mode & 0o777 == 0o600
    # The slash path shares the applier's os.environ sync, so the quoted file value
    # is un-quoted before reload (the bug this commit also fixes for this path).
    assert os.environ["LLM_MODEL"] == "model with space"


def test_command_backend_update_settings_reports_restart_and_rebuilds_graph(
    tmp_path: Path, monkeypatch
):
    # A restart-required key (embedding_provider) is flagged; an LLM field rebuilds the
    # agent graph, identical to the PATCH route (they share one applier now). setenv
    # registers both written vars for teardown so the applier's os.environ writes
    # (EMBEDDING_PROVIDER=cohere, LLM_MODEL=m2) cannot leak into Settings-loading tests.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "m1")
    (tmp_path / ".env").write_text("EMBEDDING_PROVIDER=openai\n", encoding="utf-8")
    backend, agent = _backend_client(tmp_path)

    result = asyncio.run(
        backend.update_settings(embedding_provider="cohere", llm_model="m2")
    )

    assert result["restart_required"] is True
    assert agent.graph_rebuilds == ["sync", "async"]


def test_get_cached_models_includes_reasoning_effort_ladder(
    tmp_path: Path, monkeypatch
):
    """GET /models surfaces the static effort ladder for frontend warnings."""
    from nymeria.api.routers import settings as settings_router
    from nymeria.config.model_capabilities import ModelInfo

    monkeypatch.setattr(
        settings_router,
        "list_all_models",
        lambda: [
            ModelInfo(id="openai/gpt-5.5", name="GPT-5.5"),
            ModelInfo(id="x-ai/grok-4", name="Grok 4"),
            ModelInfo(id="claude-opus-4-8", name="Claude Opus 4.8"),
        ],
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get("/models", headers=_auth(token))

    assert response.status_code == 200
    body = {entry["id"]: entry for entry in response.json()}
    # Provider-qualified ids are OpenRouter catalog entries: the ladder must
    # match the runtime clamp for openrouter threads (unified config), not
    # the native family ladder. x-ai/grok-4 would otherwise advertise a
    # max effort of "off" and trip a false frontend warning.
    assert body["openai/gpt-5.5"]["supported_reasoning_efforts"] == [
        "off", "low", "medium", "high", "xhigh",
    ]
    assert body["openai/gpt-5.5"]["max_reasoning_effort"] == "xhigh"
    assert body["x-ai/grok-4"]["supported_reasoning_efforts"] == [
        "off", "low", "medium", "high", "xhigh",
    ]
    assert body["x-ai/grok-4"]["max_reasoning_effort"] == "xhigh"
    # Bare ids (native catalogs, e.g. Anthropic) keep the family ladder.
    assert body["claude-opus-4-8"]["supported_reasoning_efforts"] == [
        "off", "low", "medium", "high", "xhigh", "max",
    ]
    assert body["claude-opus-4-8"]["max_reasoning_effort"] == "max"
