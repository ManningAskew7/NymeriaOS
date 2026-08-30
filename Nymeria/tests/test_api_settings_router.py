"""Regression tests for the extracted settings/model API router."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest
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
    llm_background_model: str | None = None
    llm_background_base_url: str | None = None
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
    sequential_tool_execution: bool = False
    hooks_enabled: bool = True
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
    llm_fallback_switch_mode: str = "auto"
    llm_fallback_prompt_timeout_seconds: int = 180
    llm_refusal_swap_mode: str = "ask"
    context_management: str = "none"
    compact_threshold: float = 0.8
    compact_threshold_mode: str = "percentage"
    compact_threshold_tokens: int = 100_000
    compact_keep_messages: int = 4
    compact_model: str | None = None
    compact_proactive_enabled: bool = False
    compact_proactive_idle_seconds: int = 210
    compact_proactive_min_pct: int = 85
    sliding_window_cycles: int = 20
    tool_output_max_chars: int = 100000
    tool_timeout: int = 300
    tool_timing_in_results: bool = False
    memory_char_limit: int = 8000
    memory_max_entries: int = 100
    memory_value_max_chars: int = 1000
    agent_max_iterations: int = 500
    user_timezone: str = "UTC"
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
    groq_api_key: str | None = None
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
    embedding_base_url: str | None = None
    embedding_input_type: str | None = None
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

        def env_bool(name: str, default: bool) -> bool:
            value = os.environ.get(name)
            return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")

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
            sequential_tool_execution=env_bool(
                "SEQUENTIAL_TOOL_EXECUTION",
                self.settings.sequential_tool_execution,
            ),
            hooks_enabled=env_bool(
                "HOOKS_ENABLED",
                self.settings.hooks_enabled,
            ),
        )


class FakeAgent:
    def __init__(self, data_dir: Path, llm_config: FakeLLMConfig | None = None):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0
        self.llm_config = llm_config or FakeLLMConfig()
        # Populated by the settings-apply path on first PATCH; typed as the
        # post-apply FakeSettings so the test assertions type-check.
        self.settings: FakeSettings = None  # type: ignore[bad-assignment]
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

    def _rebuild_default_graphs(self) -> None:
        # Mirror the real agent contract: clear the per-thread caches under the
        # lock, then rebuild the defaults via the build stubs (which record
        # "sync"/"async" in graph_rebuilds).
        with self._graph_cache_lock:
            self._user_graphs.clear()
            self._async_user_graphs.clear()
        self._default_graph = self._build_graph_with_prompt(self._base_system_prompt)
        self._default_async_graph = self._build_async_graph_with_prompt(
            self._base_system_prompt
        )

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
    client = TestClient(api_module.create_api_app(agent))  # type: ignore[bad-argument-type]
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


def test_llm_provider_test_uses_google_native_wire_on_cliproxy_root(
    tmp_path: Path,
    monkeypatch,
):
    """The antigravity native route (catalog 2026-08-07): provider google
    with a CLIProxy root base must probe the /v1beta generateContent
    surface with x-goog-api-key. The old fallthrough built
    {root}/chat/completions, which 404s at the proxy root, so credential
    verify was permanently inconclusive and the desktop wizard's Save
    (gated on a passing test) could not complete for antigravity."""
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
    }
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test",
        headers=_auth(token),
        json={
            "llm_provider": "google",
            "llm_model": "gemini-3.6-flash-high",
            "api_key": "cpx-secret-key",
            "llm_base_url": "http://localhost:8318",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (
        FakeAsyncClient.calls[0]["url"]
        == "http://localhost:8318/v1beta/models/gemini-3.6-flash-high:generateContent"
    )
    assert FakeAsyncClient.calls[0]["headers"]["x-goog-api-key"] == "cpx-secret-key"
    assert "Authorization" not in FakeAsyncClient.calls[0]["headers"]
    assert FakeAsyncClient.calls[0]["json"]["contents"][0]["parts"][0]["text"]


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


def test_available_models_google_native_lists_gemini_shape(
    tmp_path: Path,
    monkeypatch,
):
    """provider=google lists via the native /v1beta/models wire (W6, backlog
    close-out of the 2026-08-09 incident: this used to fall through to the
    unconditional empty return, and the resulting "No models returned"
    pushed a blind, unserved model pick)."""
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {
        "models": [
            {
                "name": "models/gemini-3.6-flash-high",
                "displayName": "Gemini 3.6 Flash High",
                "inputTokenLimit": 1048576,
                "outputTokenLimit": 65536,
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                # Not chat-pickable: filtered out by generation-method.
                "name": "models/gemini-embedding-001",
                "supportedGenerationMethods": ["embedContent"],
            },
            {
                # No declared methods: kept (lenient when the listing is bare).
                "name": "models/gemini-flash-latest",
            },
        ]
    }
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    try:
        response = client.get(
            "/models/available?provider=google&base_url=http://localhost:8318",
            headers=_auth(token),
        )
        assert response.status_code == 200
        body = response.json()
        call = FakeAsyncClient.calls[0]
        assert call["url"] == "http://localhost:8318/v1beta/models"
        # Gemini wire auth: x-goog-api-key, never a Bearer header.
        assert "x-goog-api-key" in call["headers"]
        assert "Authorization" not in call["headers"]
        assert [m["id"] for m in body] == [
            "gemini-3.6-flash-high",
            "gemini-flash-latest",
        ]
        assert body[0]["name"] == "Gemini 3.6 Flash High"
        assert body[0]["context_length"] == 1048576
        assert body[0]["max_completion_tokens"] == 65536
    finally:
        FakeAsyncClient.response_status = 200
        FakeAsyncClient.response_body = None
        FakeAsyncClient.calls = []


def test_available_models_google_never_sends_a_stored_key_to_a_named_host(
    tmp_path: Path,
    monkeypatch,
):
    """The new google listing branch honors the same caller-named-destination
    credential gate as the anthropic branch (differential, same rationale as
    the anthropic test above: the control leg proves the stored key DOES ride
    to the configured destination, so the attack leg's absence is the gate)."""
    stored_key = "gemini-stored-token"
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"models": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    settings = FakeSettings(
        project_root=tmp_path, data_dir=tmp_path, gemini_api_key=stored_key
    )
    client, agent, _admin_token, _provider = _client(
        monkeypatch, tmp_path, settings=settings
    )
    agent.accounts_repo.create_user("mallory", "mallory@example.com", "Mallory")
    user_token = agent.accounts_repo.issue_token("mallory")

    try:
        # Control: configured destination, the stored key is expected to ride.
        configured = client.get(
            "/models/available?provider=google", headers=_auth(user_token)
        )
        assert configured.status_code == 200
        control_call = FakeAsyncClient.calls[-1]
        assert "generativelanguage.googleapis.com" in control_call["url"]
        assert control_call["headers"].get("x-goog-api-key") == stored_key, (
            "control leg did not carry the stored key, so this test cannot "
            f"prove the gate does anything: {control_call['headers']}"
        )

        # The gate: same user, same provider, caller-named destination.
        before = len(FakeAsyncClient.calls)
        attacked = client.get(
            "/models/available"
            "?provider=google&base_url=http://attacker.invalid",
            headers=_auth(user_token),
        )
        assert attacked.status_code == 200
        new_calls = FakeAsyncClient.calls[before:]
        assert all(
            stored_key not in call["headers"].values() for call in new_calls
        ), f"stored google key leaked to a caller-named host: {new_calls}"
    finally:
        FakeAsyncClient.response_status = 200
        FakeAsyncClient.response_body = None
        FakeAsyncClient.calls = []


def test_available_models_returns_empty_on_provider_http_error(
    tmp_path: Path,
    monkeypatch,
):
    # A provider-side rejection (401 here) is logged distinctly but still
    # collapses to the "[] == no models" contract the frontend relies on
    # (optimization slice 10 F10).
    FakeAsyncClient.response_status = 401
    FakeAsyncClient.response_body = {"error": {"message": "invalid api key"}}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    try:
        response = client.get(
            "/models/available?provider=lmstudio&base_url=http://localhost:1234/v1",
            headers=_auth(token),
        )
        assert response.status_code == 200
        assert response.json() == []
        assert FakeAsyncClient.calls[0]["url"] == "http://localhost:1234/v1/models"
    finally:
        # Reset the shared class attribute so later tests see a clean 200.
        FakeAsyncClient.response_status = 200
        FakeAsyncClient.response_body = None


def test_available_models_post_uses_ephemeral_key_and_never_echoes_it(
    tmp_path: Path,
    monkeypatch,
):
    # The POST variant backs the /provider setup flow: a just-pasted key rides
    # the request body, wins over vault/settings resolution for that one
    # listing, and never appears in the response.
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": [{"id": "m-1", "name": "M One"}]}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/models/available",
        headers=_auth(token),
        json={
            "provider": "lmstudio",
            "base_url": "http://localhost:1234/v1",
            "api_key": "sk-ephemeral-test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [entry["id"] for entry in body] == ["m-1"]
    call = FakeAsyncClient.calls[0]
    assert call["url"] == "http://localhost:1234/v1/models"
    assert call["headers"]["Authorization"] == "Bearer sk-ephemeral-test"
    assert "sk-ephemeral-test" not in response.text


def test_available_models_get_never_sends_a_stored_key_to_a_named_host(
    tmp_path: Path,
    monkeypatch,
):
    """A non-admin can name a destination, but no stored key follows it there.

    Written as a DIFFERENTIAL test, and deliberately not as "the request to
    attacker.invalid does not happen". The request does still happen: loopback
    is a legitimate destination for the local-LLM flows, so this route cannot
    just refuse caller-supplied addresses. What must not happen is the server's
    configured provider credential riding along to one.

    The first leg is the control. Without it, a green result would prove
    nothing: if the fixture happened to resolve no key at all, an
    egress-free-of-keys assertion passes while defending nothing. The control
    establishes that this exact request DOES carry ``anthropic-token`` when the
    destination comes from configuration, so the second leg's absence is caused
    by the gate rather than by an empty environment.
    """
    stored_key = "anthropic-token"  # FakeSettings.anthropic_api_key
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user("mallory", "mallory@example.com", "Mallory")
    user_token = agent.accounts_repo.issue_token("mallory")

    # Control: configured destination, so the stored key is expected to ride.
    configured = client.get(
        "/models/available?provider=anthropic", headers=_auth(user_token)
    )
    assert configured.status_code == 200
    control_call = FakeAsyncClient.calls[-1]
    assert "api.anthropic.com" in control_call["url"]
    assert stored_key in control_call["headers"].values(), (
        "control leg did not carry the stored key, so this test cannot prove "
        f"the gate does anything: {control_call['headers']}"
    )

    # The gate: same user, same provider, caller-named destination.
    before = len(FakeAsyncClient.calls)
    attacked = client.get(
        "/models/available"
        "?provider=anthropic&base_url=http://attacker.invalid/v1",
        headers=_auth(user_token),
    )
    assert attacked.status_code == 200
    new_calls = FakeAsyncClient.calls[before:]
    # Whether a request happens at all is provider-shaped, so that is not the
    # invariant. Anthropic declares it requires a key, so with the stored one
    # withheld the route short-circuits and never dials out. A keyless-capable
    # provider (see the lmstudio leg below) does dial out. What must hold in
    # both cases is that nothing carrying the stored key leaves the process.
    assert all(
        stored_key not in call["headers"].values() for call in new_calls
    ), f"stored provider key leaked to a caller-named host: {new_calls}"

    # The capability this route exists for still works: a keyless local
    # endpoint lists models against the address the user is still typing.
    FakeAsyncClient.response_body = {"data": [{"id": "local-1"}]}
    local = client.get(
        "/models/available?provider=lmstudio&base_url=http://localhost:1234/v1",
        headers=_auth(user_token),
    )
    assert local.status_code == 200
    assert [entry["id"] for entry in local.json()] == ["local-1"]
    local_call = FakeAsyncClient.calls[-1]
    assert local_call["url"] == "http://localhost:1234/v1/models"
    assert stored_key not in local_call["headers"].values()


def test_available_models_get_does_not_exempt_admins(
    tmp_path: Path,
    monkeypatch,
):
    """The admin exemption is gone, and its removal is the point of this test.

    It used to be pinned here as a deliberate concession ("an admin already
    reaches destination-plus-key through the admin-gated POST twin"). That
    reasoning does not survive the deployment shape this ships as by default:
    a single-user install's only account IS an admin, so the exemption
    switched the control off in exactly the case it was written for. The
    sibling non-admin test above is unchanged; this one now asserts the same
    outcome for the other role.

    The request still happens, and it must: whether a keyless probe is useful
    is provider-shaped. What must not happen is the stored key riding to an
    address configuration never named.
    """
    stored_key = "anthropic-token"  # FakeSettings.anthropic_api_key
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, admin_token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=anthropic&base_url=https://gateway.example/v1",
        headers=_auth(admin_token),
    )

    assert response.status_code == 200
    assert all(
        stored_key not in call["headers"].values() for call in FakeAsyncClient.calls
    ), f"stored provider key leaked to an admin-named host: {FakeAsyncClient.calls}"


def test_available_models_get_serves_a_destination_the_deployment_configured(
    tmp_path: Path,
    monkeypatch,
):
    """Naming the deployment's OWN address is not a redirection.

    The earlier gate tested ``bool(base_url)``, so listing models against the
    very host the deployment is configured for suppressed stored credentials
    and returned an empty list, for no security benefit. This is the leg that
    keeps the shared predicate honest in the permissive direction: without it,
    tightening the gate to refuse everything would still pass the suite.
    """
    stored_key = "anthropic-token"  # FakeSettings.anthropic_api_key
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    settings = FakeSettings(project_root=tmp_path, data_dir=tmp_path)
    settings.llm_base_url = "https://gateway.example/v1"
    client, agent, _admin_token, _provider = _client(
        monkeypatch, tmp_path, settings=settings
    )
    agent.accounts_repo.create_user("mallory", "mallory@example.com", "Mallory")
    user_token = agent.accounts_repo.issue_token("mallory")

    response = client.get(
        "/models/available?provider=anthropic&base_url=https://gateway.example/v1",
        headers=_auth(user_token),
    )

    assert response.status_code == 200
    call = FakeAsyncClient.calls[-1]
    assert call["url"] == "https://gateway.example/v1/models"
    assert stored_key in call["headers"].values()


def test_available_models_post_is_admin_only(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, agent, _admin_token, _provider = _client(monkeypatch, tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice")
    user_token = agent.accounts_repo.issue_token("alice")

    response = client.post(
        "/models/available",
        headers=_auth(user_token),
        json={"provider": "lmstudio", "api_key": "sk-should-not-be-used"},
    )

    assert response.status_code == 403
    assert FakeAsyncClient.calls == []


def test_available_models_normalizes_openai_cliproxy_base_url_without_v1(
    tmp_path: Path,
    monkeypatch,
):
    # Slice 10 F14 regression: a CLIProxy openai base URL configured WITHOUT a
    # trailing /v1 must still list models (the live path used to fetch /models
    # and 404, even though the provider-test path normalized it to /v1).
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=openai&base_url=http://localhost:8317",
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert FakeAsyncClient.calls[0]["url"] == "http://localhost:8317/v1/models"


def test_available_models_cliproxy_base_url_with_v1_is_idempotent(
    tmp_path: Path,
    monkeypatch,
):
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=openai&base_url=http://localhost:8317/v1",
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert FakeAsyncClient.calls[0]["url"] == "http://localhost:8317/v1/models"


def test_available_models_anthropic_custom_base_sends_cloak_header(
    tmp_path: Path,
    monkeypatch,
):
    # Slice 10 F14: listing models through a custom (CLIProxy) anthropic base
    # must send the cloak-skip User-Agent the rest of the stack uses, matching
    # the provider-test path.
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=anthropic&base_url=http://localhost:8318",
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert FakeAsyncClient.calls[0]["url"] == "http://localhost:8318/v1/models"
    assert FakeAsyncClient.calls[0]["headers"]["User-Agent"] == "claude-cli/2.1.113"


def test_available_models_anthropic_public_base_omits_cloak_header(
    tmp_path: Path,
    monkeypatch,
):
    # Negative guard: the public api.anthropic.com endpoint (no custom base) must
    # NOT carry the cloak User-Agent.
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=anthropic",
        headers=_auth(token),
    )

    assert response.status_code == 200
    # The default FakeSettings.anthropic_api_key seeds the key, so the request
    # actually fires (otherwise the assertion below would be vacuous).
    assert FakeAsyncClient.calls, "expected the model fetch to issue an HTTP call"
    assert FakeAsyncClient.calls[0]["url"] == "https://api.anthropic.com/v1/models"
    assert "User-Agent" not in FakeAsyncClient.calls[0]["headers"]


def test_available_models_openrouter_includes_attribution_headers(
    tmp_path: Path,
    monkeypatch,
):
    # Slice 10 F14: the live openai-compatible path now sends OpenRouter
    # attribution headers, matching the provider-test path.
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"data": []}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/models/available?provider=openrouter",
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert FakeAsyncClient.calls, "expected the model fetch to issue an HTTP call"
    assert FakeAsyncClient.calls[0]["url"] == "https://openrouter.ai/api/v1/models"
    headers = FakeAsyncClient.calls[0]["headers"]
    assert headers["HTTP-Referer"] == "https://github.com/ManningAskew7/NymeriaOS"
    assert headers["X-Title"] == "Nymeria"


def test_llm_provider_test_openrouter_includes_attribution_headers(
    tmp_path: Path,
    monkeypatch,
):
    # Guards the provider-test path's OpenRouter attribution headers (no prior
    # test covered them; the F14 refactor routes them through the shared helper).
    FakeAsyncClient.response_status = 200
    FakeAsyncClient.response_body = {"ok": True}
    FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.post(
        "/settings/llm/test",
        headers=_auth(token),
        json={
            "llm_provider": "openrouter",
            "llm_model": "some/model",
            "api_key": "or-secret-key",
        },
    )

    assert response.status_code == 200
    headers = FakeAsyncClient.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer or-secret-key"
    assert headers["HTTP-Referer"] == "https://github.com/ManningAskew7/NymeriaOS"
    assert headers["X-Title"] == "Nymeria"


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


def test_runtime_diagnostics_resolves_the_ceiling_when_max_tokens_is_unset(
    tmp_path: Path,
    monkeypatch,
):
    """The branch the default fake never reached.

    FakeLLMConfig ships max_tokens=4096, so `if effective_max_tokens is None`
    never ran and the whole resolve-report path was unpinned. Unset it: the
    endpoint must report the model's real ceiling rather than echoing None.

    It reports the UNCLAMPED ceiling on purpose. Interactive turns stream, and
    streaming opts back up past the non-streaming clamp, so reporting the
    clamped 21333 here would tell the user their model is 6x smaller than the
    one they are actually talking to.
    """
    from nymeria.api.routers import settings as settings_router

    resolved: list = []

    def _fake_resolve(cfg, probe=False):
        resolved.append((cfg.provider, probe))
        return 128000

    monkeypatch.setattr(settings_router, "resolve_max_output_tokens", _fake_resolve)
    llm_config = FakeLLMConfig(
        provider="anthropic", model="claude-sonnet-5", max_tokens=None
    )
    client, _agent, token, _provider = _client(
        monkeypatch, tmp_path, llm_config=llm_config
    )

    response = client.get("/settings/llm/runtime", headers=_auth(token))

    assert response.status_code == 200
    assert response.json()["effective_max_tokens"] == 128000
    assert resolved == [("anthropic", True)], (
        "anthropic must be probed here: on a metadata-blind gateway the probe "
        f"is the only thing that knows the ceiling (got {resolved})"
    )


def test_runtime_diagnostics_does_not_probe_a_non_anthropic_provider(
    tmp_path: Path,
    monkeypatch,
):
    """The probe speaks Anthropic's dialect and carries the caller's API key.

    Enabling it for other providers would post THEIR credential to an Anthropic
    endpoint, so the route gates it on the provider. Pinned because the gate is
    a one-line comparison that reads like a tidy-up and is not.
    """
    from nymeria.api.routers import settings as settings_router

    resolved: list = []

    def _fake_resolve(cfg, probe=False):
        resolved.append((cfg.provider, probe))
        return None

    monkeypatch.setattr(settings_router, "resolve_max_output_tokens", _fake_resolve)
    llm_config = FakeLLMConfig(provider="openai", model="gpt-5.5", max_tokens=None)
    client, _agent, token, _provider = _client(
        monkeypatch, tmp_path, llm_config=llm_config
    )

    response = client.get("/settings/llm/runtime", headers=_auth(token))

    assert response.status_code == 200
    assert resolved == [("openai", False)], (
        f"a non-Anthropic provider must not be probed (got {resolved})"
    )


def test_runtime_diagnostics_survives_a_resolver_failure(
    tmp_path: Path,
    monkeypatch,
):
    """Diagnostics must degrade, never 500: it is what you call WHEN things break."""

    from nymeria.api.routers import settings as settings_router

    def _boom(cfg, probe=False):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(settings_router, "resolve_max_output_tokens", _boom)
    llm_config = FakeLLMConfig(
        provider="anthropic", model="claude-sonnet-5", max_tokens=None
    )
    client, _agent, token, _provider = _client(
        monkeypatch, tmp_path, llm_config=llm_config
    )

    response = client.get("/settings/llm/runtime", headers=_auth(token))

    assert response.status_code == 200
    assert response.json()["effective_max_tokens"] is None


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


def test_patch_settings_leaves_env_vars_the_request_did_not_name(
    tmp_path: Path,
    monkeypatch,
):
    # #299: the export is scoped to what THIS request wrote, never the whole
    # merged file. TWITCH_CHANNEL is a mapped patchable key, so a whole-file
    # re-export overwrites the value the live process deliberately holds with
    # whatever the file happens to say. The reason that matters is the very
    # next line of the applier: the settings cache is cleared, so a reloaded
    # Settings believes anything smuggled into os.environ here.
    (tmp_path / ".env").write_text(
        "LLM_MODEL=old-model\nTWITCH_CHANNEL=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TWITCH_CHANNEL", "pinned-in-process")
    # LLM_MODEL is the key this request DOES write, registered so the applier's
    # os.environ write is reverted at teardown (the file's convention; conftest
    # layer 3 would also catch it at the next test's setup).
    monkeypatch.setenv("LLM_MODEL", "old-model")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_model": "new-model"},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["llm_model"]
    assert os.environ["LLM_MODEL"] == "new-model"
    assert os.environ["TWITCH_CHANNEL"] == "pinned-in-process"
    # Narrowing the EXPORT must not narrow the merge-WRITE: untouched file
    # lines are still preserved verbatim.
    assert "TWITCH_CHANNEL=from-file" in (tmp_path / ".env").read_text(
        encoding="utf-8"
    )


def test_patch_settings_does_not_resurrect_env_vars_absent_from_the_process(
    tmp_path: Path,
    monkeypatch,
):
    # The other half of #299. run.py's slim and fat-CLI shapes POP keys out of
    # os.environ while leaving them in the env file (REDIS_URL), and a
    # whole-file re-export put the popped key back. Scope note, because
    # run.py's own comment overstates it and this assertion must not be read as
    # more than it is: popping REDIS_URL does not by itself keep the process
    # off the cross-process bus, since the dotenv source refills
    # `Settings.redis_url` on any reload (see `_sync_updated_env_vars`). The
    # gate is `redis_enabled and redis_url` in core/event_bus.py, held by the
    # REDIS_ENABLED=false pin. What is pinned here is narrower and still worth
    # pinning: a PATCH must not write process-wide env state for a key it was
    # never asked to touch.
    (tmp_path / ".env").write_text(
        "LLM_MODEL=old-model\nREDIS_URL=redis://from-file:6379/0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MODEL", "old-model")
    monkeypatch.delenv("REDIS_URL", raising=False)
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_model": "new-model"},
    )

    assert response.status_code == 200
    assert os.environ["LLM_MODEL"] == "new-model"
    assert "REDIS_URL" not in os.environ


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


def test_patch_settings_hot_reloads_sequential_tool_execution(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text("SEQUENTIAL_TOOL_EXECUTION=false\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"sequential_tool_execution": True},
    )

    assert response.status_code == 200
    assert "sequential_tool_execution" in response.json()["updated"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SEQUENTIAL_TOOL_EXECUTION=true" in env_text
    assert provider.cache_clear_count == 1
    assert agent.settings.sequential_tool_execution is True
    # Read at runtime from config per turn, NOT baked into the graph at build:
    # a PATCH must not trigger a graph rebuild.
    assert agent.graph_rebuilds == []


def test_patch_settings_updates_hooks_enabled_master_switch(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / ".env").write_text("HOOKS_ENABLED=true\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"hooks_enabled": False},
    )

    assert response.status_code == 200
    assert "hooks_enabled" in response.json()["updated"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HOOKS_ENABLED=false" in env_text
    assert agent.settings.hooks_enabled is False
    # Hooks resolve per turn, so flipping the master switch is not a graph rebuild.
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


def test_patch_settings_updates_user_timezone_without_graph_rebuild(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    (tmp_path / ".env").write_text("USER_TIMEZONE=UTC\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"user_timezone": "Europe/London"},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["user_timezone"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "USER_TIMEZONE=Europe/London" in env_text
    assert os.environ["USER_TIMEZONE"] == "Europe/London"
    assert provider.cache_clear_count == 1
    # Timestamps render per turn from settings; no graph rebuild involved.
    assert agent.graph_rebuilds == []


def test_patch_settings_rejects_unknown_timezone(
    tmp_path: Path,
    monkeypatch,
):
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"user_timezone": "Middle/Nowhere"},
    )

    assert response.status_code == 422
    assert not (tmp_path / ".env").exists()


def test_patch_settings_hot_reloads_tool_timeout_and_rebuilds_graphs(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("TOOL_TIMEOUT", raising=False)
    (tmp_path / ".env").write_text("TOOL_TIMEOUT=300\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"tool_timeout": 600},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == ["tool_timeout"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "TOOL_TIMEOUT=600" in env_text
    assert provider.cache_clear_count == 1
    # tool_timeout is baked into SafeToolNode at graph build (see
    # _GRAPH_REBUILD_FIELDS), so a hot PATCH must rebuild.
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_rejects_out_of_range_tool_timeout(
    tmp_path: Path,
    monkeypatch,
):
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"tool_timeout": 5},
    )

    assert response.status_code == 422
    assert not (tmp_path / ".env").exists()


def test_patch_settings_routes_generic_llm_api_key_to_provider_slot(
    tmp_path: Path,
    monkeypatch,
):
    # The generic slot must land in the SELECTED provider's declared key var
    # (GROQ_API_KEY here), mirroring the CLI finalize, and behave like every
    # other credential write: rebuild the graphs, never echo the secret.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    (tmp_path / ".env").write_text("LLM_MODEL=old-model\n", encoding="utf-8")
    client, agent, token, provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={
            "llm_provider": "groq",
            "llm_model": "llama-test",
            "llm_api_key": "gsk-secret-test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["updated"]) == {"llm_provider", "llm_model", "llm_api_key"}
    assert "gsk-secret-test" not in response.text
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "GROQ_API_KEY=gsk-secret-test" in env_text
    assert "LLM_API_KEY" not in env_text
    assert os.environ["GROQ_API_KEY"] == "gsk-secret-test"
    assert provider.cache_clear_count == 1
    assert agent.graph_rebuilds == ["sync", "async"]


def test_patch_settings_generic_llm_api_key_uses_current_provider_for_rotation(
    tmp_path: Path,
    monkeypatch,
):
    # Key-only rotation (no llm_provider in the payload) resolves against the
    # currently configured provider. FakeSettings defaults to anthropic, whose
    # first declared slot is the direct key.
    monkeypatch.delenv("ANTHROPIC_DIRECT_API_KEY", raising=False)
    (tmp_path / ".env").write_text("LLM_MODEL=claude-test\n", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_api_key": "sk-ant-rotated"},
    )

    assert response.status_code == 200
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_DIRECT_API_KEY=sk-ant-rotated" in env_text
    assert "sk-ant-rotated" not in response.text


def test_patch_settings_generic_llm_api_key_rejects_unknown_provider(
    tmp_path: Path,
    monkeypatch,
):
    # A provider outside the registry has no declared key slot, so storing the
    # key would write a dotenv line nothing reads. The applier must refuse with
    # a clear 400 instead of silently dropping the secret.
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_provider": "not-a-registered-provider", "llm_api_key": "sk-lost"},
    )

    assert response.status_code == 400
    assert "sk-lost" not in response.text
    env_path = tmp_path / ".env"
    if env_path.exists():
        assert "sk-lost" not in env_path.read_text(encoding="utf-8")


def test_get_rag_catalog_serves_the_setup_catalog(
    tmp_path: Path,
    monkeypatch,
):
    # One source of truth: the endpoint must serve the same options the CLI
    # wizard renders from setup/rag_catalog.py, so a GUI cannot drift.
    from nymeria.setup.rag_catalog import (
        EMBEDDERS,
        QUICKSTART_EMBEDDER,
        QUICKSTART_RERANKER,
        RERANKERS,
    )

    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get("/settings/rag/catalog", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert [e["id"] for e in body["embedders"]] == [opt.id for opt in EMBEDDERS]
    assert [r["id"] for r in body["rerankers"]] == [opt.id for opt in RERANKERS]
    cohere = next(e for e in body["embedders"] if e["id"] == "premium-cohere")
    assert cohere["provider"] == "cohere"
    assert cohere["model"] == "embed-v4.0"
    assert cohere["dimensions"] == 1024
    voyage = next(e for e in body["embedders"] if e["id"] == "premium-voyage-large")
    assert voyage["base_url"] == "https://api.voyageai.com/v1"
    assert voyage["input_type"] == "voyage"
    assert body["quickstart_embedder"] == QUICKSTART_EMBEDDER
    assert body["quickstart_reranker"] == QUICKSTART_RERANKER
    assert body["combos"], "recommended combos must be served"


def test_get_settings_reports_timezone_tool_timeout_and_embedding_endpoint(
    tmp_path: Path,
    monkeypatch,
):
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        user_timezone="Australia/Brisbane",
        tool_timeout=450,
        embedding_base_url="https://api.voyageai.com/v1",
        embedding_input_type="voyage",
    )
    client, _agent, token, _provider = _client(
        monkeypatch,
        tmp_path,
        settings=settings,
    )

    response = client.get("/settings", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["user_timezone"] == "Australia/Brisbane"
    assert body["tool_timeout"] == 450
    assert body["embedding_base_url"] == "https://api.voyageai.com/v1"
    assert body["embedding_input_type"] == "voyage"


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
    # setenv (not delenv) so the os.environ write the applier makes is reverted on
    # teardown and cannot leak into other tests that load real Settings. Only the
    # var this request actually writes needs that: the export is scoped to the
    # request's own changes (#299), so the seeded LLM_MODEL line below is written
    # to the FILE and never to os.environ.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")
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


def test_command_backend_get_settings_matches_route_payload(
    tmp_path: Path, monkeypatch
):
    # The two TurnExecutor shapes must agree: the in-process slash-command path
    # (CommandBackendClient.get_settings) and GET /settings now share one
    # serializer, so their payloads are identical. This is the durable guard that
    # the dict can never silently drift behind the route again (the F4 bug, where
    # it had fallen ~20 fields behind). Set non-default values on several of the
    # previously-missing fields so this proves value parity, not just shape.
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        llm_smart_model="anthropic:claude-smart",
        llm_background_model="openai:gpt-bg",
        llm_background_base_url="http://bg.example",
        dynamic_tool_binding=True,
        llm_fallback_hold_seconds=99,
        llm_fallback_switch_mode="ask",
        llm_fallback_prompt_timeout_seconds=120,
        llm_refusal_swap_mode="auto",
        dream_default_min_interval_hours=3,
        dream_default_model="anthropic:claude-dream",
        embedding_provider="cohere",
        embedding_model="embed-v3",
        embedding_dimensions=512,
        rag_retrieval_mode="vector",
        rag_rerank_enabled=True,
        rag_rerank_provider="cohere",
        rag_rerank_model="rerank-v3",
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path, settings=settings)
    backend, _backend_agent = _backend_client(tmp_path, settings=settings)

    route_json = client.get("/settings", headers=_auth(token)).json()
    backend_dict = asyncio.run(backend.get_settings())

    assert backend_dict == route_json


def test_command_backend_get_settings_key_set_matches_response_model(
    tmp_path: Path,
):
    # Tripwire independent of the route plumbing: the in-process dict must carry
    # exactly the ServerSettingsResponse field set. A future hand-built dict that
    # drops a field fails here even if no route test is wired for it.
    from nymeria.api.schemas.settings import ServerSettingsResponse

    backend, _agent = _backend_client(tmp_path)

    backend_dict = asyncio.run(backend.get_settings())

    assert set(backend_dict.keys()) == set(ServerSettingsResponse.model_fields)


def test_command_backend_get_settings_carries_previously_missing_fields(
    tmp_path: Path,
):
    # Direct proof the drift is closed: the fields the old 49-key dict omitted now
    # round-trip their real values through the in-process path (they used to
    # resolve to None for /model, /doctor, etc. in the slim shape).
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        llm_smart_model="anthropic:claude-smart",
        llm_background_model="openai:gpt-bg",
        dynamic_tool_binding=True,
        embedding_provider="cohere",
        rag_rerank_model="rerank-v3",
        dream_default_model="anthropic:claude-dream",
    )
    backend, _agent = _backend_client(tmp_path, settings=settings)

    backend_dict = asyncio.run(backend.get_settings())

    assert backend_dict["llm_smart_model"] == "anthropic:claude-smart"
    assert backend_dict["llm_background_model"] == "openai:gpt-bg"
    assert backend_dict["dynamic_tool_binding"] is True
    assert backend_dict["embedding_provider"] == "cohere"
    assert backend_dict["rag_rerank_model"] == "rerank-v3"
    assert backend_dict["dream_default_model"] == "anthropic:claude-dream"


def test_command_backend_get_env_vars_matches_route_payload(
    tmp_path: Path, monkeypatch
):
    # The two TurnExecutor shapes must agree: GET /settings/env (HTTP) and the
    # in-process CommandBackendClient.get_env_vars now share one serializer
    # (serialize_env_entries), so their payloads are byte-identical. Seed both a
    # suffix-only secret (groq_api_key) and a plain value so this proves masking +
    # label parity, not just shape.
    settings = FakeSettings(
        project_root=tmp_path,
        data_dir=tmp_path,
        groq_api_key="gsk-secret-abcdef1234567890",
        openai_api_key="sk-secret-abcdef1234567890",
        llm_model="claude-test-x",
    )
    client, _agent, token, _provider = _client(monkeypatch, tmp_path, settings=settings)
    backend, _backend_agent = _backend_client(tmp_path, settings=settings)

    route_json = client.get("/settings/env", headers=_auth(token)).json()
    backend_dict = asyncio.run(backend.get_env_vars())

    assert backend_dict == route_json


def test_command_backend_get_env_vars_masks_suffix_secret(tmp_path: Path):
    # Regression for the F4 leak: groq_api_key is a credential by *_api_key suffix
    # but is NOT in the explicit _SECRET_KEYS allowlist, so the old in-process body
    # (allowlist-only `key in _SECRET_KEYS`) printed it RAW while the HTTP route
    # masked it via the suffix-aware _is_secret_setting_key. The shared serializer
    # now masks it on both shapes.
    raw = "gsk-secret-abcdef1234567890"
    settings = FakeSettings(
        project_root=tmp_path, data_dir=tmp_path, groq_api_key=raw
    )
    backend, _agent = _backend_client(tmp_path, settings=settings)

    backend_dict = asyncio.run(backend.get_env_vars())
    by_name = {e["name"]: e for e in backend_dict["entries"]}

    groq = by_name["groq_api_key"]
    assert groq["is_secret"] is True
    assert groq["value"] != raw
    assert "..." in groq["value"]
    assert groq["env_var"] == "GROQ_API_KEY"


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


# ── Config-surface honesty (2026-08-10 outage pass) ─────────────────────────
# Three ways the surface lied about itself: unknown PATCH keys silently
# dropped with a success response, /env show advertising keys the update
# model could not write, and known-but-unset settings 404ing as "Unknown".


def test_patch_settings_unknown_key_is_rejected_not_ignored(
    tmp_path: Path, monkeypatch
):
    """An unknown field 400s with a near-match hint and applies NOTHING.

    Pydantic's extra="ignore" default silently dropped unknown keys, so the
    applier saw an empty update and every caller reported success for a write
    that never happened (the NYMERIA_PUBLIC_URL 74-day outage). All-or-nothing:
    the known sibling in the same request must not land either.
    """
    monkeypatch.setenv("LLM_MODEL", "old-model")
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=old-model\n", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"llm_model": "new-model", "nymeria_public_urll": "https://x.example"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "nymeria_public_urll" in detail
    assert "Did you mean" in detail
    assert "nymeria_public_url (NYMERIA_PUBLIC_URL)" in detail
    env_text = env_path.read_text(encoding="utf-8")
    assert "LLM_MODEL=old-model" in env_text
    assert "new-model" not in env_text


def test_patch_settings_env_show_advertised_key_is_settable(
    tmp_path: Path, monkeypatch
):
    """redis_url is advertised in /env show and listed restart-required, so a
    PATCH must actually persist it. It was one of 30 advertised keys missing
    from the update model, whose writes false-succeeded."""
    monkeypatch.setenv("REDIS_URL", "redis://old:6379/0")
    env_path = tmp_path / ".env"
    env_path.write_text("REDIS_URL=redis://old:6379/0\n", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings",
        headers=_auth(token),
        json={"redis_url": "redis://elsewhere:6379/0"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == ["redis_url"]
    assert body["restart_required"] is True
    assert "REDIS_URL=redis://elsewhere:6379/0" in env_path.read_text(encoding="utf-8")


def test_get_env_var_known_but_unset_is_not_unknown(tmp_path: Path, monkeypatch):
    """A real settings field that is unset returns value null, not 404.

    Unset and unknown are different answers; conflating them made /env get
    report "Unknown variable" for NYMERIA_PUBLIC_URL, hiding the one variable
    the outage turned on.
    """
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get(
        "/settings/env/NYMERIA_PUBLIC_URL", headers=_auth(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "nymeria_public_url"
    assert body["value"] is None

    unknown = client.get(
        "/settings/env/definitely_not_a_setting", headers=_auth(token)
    )
    assert unknown.status_code == 404


def test_get_env_var_hidden_setting_stays_unknown(tmp_path: Path, monkeypatch):
    """The unset-vs-unknown fix must not resurrect hidden settings."""
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get("/settings/env/nymeria_api_key", headers=_auth(token))

    assert response.status_code == 404


def test_env_listing_includes_nymeria_public_url(tmp_path: Path, monkeypatch):
    """/env show lists the field OAuth auth_code flows depend on."""
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get("/settings/env", headers=_auth(token))

    assert response.status_code == 200
    names = {e["name"] for e in response.json()["entries"]}
    assert "nymeria_public_url" in names


def test_patch_settings_public_url_warns_on_safelinks_wrapper(
    tmp_path: Path, monkeypatch
):
    """A Safe Links wrapper value persists but warns with the unwrapped URL.

    The outage fix itself nearly stored an Outlook Safe Links redirector as
    the public base URL; the write succeeds (warn, not block) but the response
    names the wrapper and the wrapped destination.
    """
    monkeypatch.setenv("NYMERIA_PUBLIC_URL", "")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    wrapped = (
        "https://aus01.safelinks.protection.outlook.com/?url="
        "https%3A%2F%2Fnymeria.example.com%2F&data=05%7C02"
    )
    response = client.patch(
        "/settings", headers=_auth(token), json={"nymeria_public_url": wrapped}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == ["nymeria_public_url"]
    assert body["warnings"], "expected a Safe Links warning"
    assert "Safe Links" in body["warnings"][0]
    assert "https://nymeria.example.com/" in body["warnings"][0]
    assert "NYMERIA_PUBLIC_URL=" in env_path.read_text(encoding="utf-8")

    clean = client.patch(
        "/settings",
        headers=_auth(token),
        json={"nymeria_public_url": "https://nymeria.example.com"},
    )
    assert clean.status_code == 200
    assert clean.json()["warnings"] == []


def test_command_backend_update_settings_rejects_unknown_key(
    tmp_path: Path, monkeypatch
):
    """The in-process slash-command path surfaces the same 400, converted to
    the httpx error shape the dispatcher renders (two-shape invariant)."""
    monkeypatch.setenv("LLM_MODEL", "old")
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=old\n", encoding="utf-8")
    backend, _agent = _backend_client(tmp_path)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(
            backend.update_settings(
                llm_model="new-model", nymeria_public_urll="https://x.example"
            )
        )

    assert excinfo.value.response.status_code == 400
    assert "nymeria_public_urll" in str(excinfo.value)
    # All-or-nothing on the in-process path too: the known sibling in the
    # same call must not land.
    env_text = env_path.read_text(encoding="utf-8")
    assert "LLM_MODEL=old" in env_text
    assert "new-model" not in env_text


def test_command_backend_get_env_var_known_but_unset(tmp_path: Path):
    """The in-process env reveal distinguishes unset from unknown too."""
    backend, _agent = _backend_client(tmp_path)

    result = asyncio.run(backend.get_env_var("NYMERIA_PUBLIC_URL"))

    assert result["name"] == "nymeria_public_url"
    assert result["value"] is None

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(backend.get_env_var("definitely_not_a_setting"))
    assert excinfo.value.response.status_code == 404


# ── Review fix round (same pass): value validation, warning robustness ──────


def test_patch_settings_out_of_range_value_rejected_before_write(
    tmp_path: Path, monkeypatch
):
    """Values violating the REAL Settings constraints 400 before the env write.

    The env write precedes the hot-reload; without the applier's
    Settings-field validation an out-of-range value persists and every
    subsequent get_settings() raises, bricking the API across restarts.
    llm_temperature (ge=0, le=2 on Settings, unconstrained on the update
    model) exercises the applier gate; twitch_buffer_size (constraint
    mirrored onto the update model) exercises the request-layer 422.
    """
    monkeypatch.setenv("LLM_TEMPERATURE", "1.0")
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_TEMPERATURE=1.0\n", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings", headers=_auth(token), json={"llm_temperature": 99}
    )

    assert response.status_code == 400
    assert "llm_temperature" in response.json()["detail"]
    assert "LLM_TEMPERATURE=1.0" in env_path.read_text(encoding="utf-8")

    mirrored = client.patch(
        "/settings", headers=_auth(token), json={"twitch_buffer_size": 10}
    )
    assert mirrored.status_code == 422
    assert "TWITCH_BUFFER_SIZE" not in env_path.read_text(encoding="utf-8")

    # The surface still works afterwards: nothing was persisted or bricked.
    follow_up = client.get("/settings/env", headers=_auth(token))
    assert follow_up.status_code == 200


def test_public_url_warning_survives_malformed_value(tmp_path: Path, monkeypatch):
    """The advisory helper never crashes on pasted junk (urlsplit ValueError)."""
    monkeypatch.setenv("NYMERIA_PUBLIC_URL", "")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.patch(
        "/settings", headers=_auth(token), json={"nymeria_public_url": "https://[::1"}
    )

    assert response.status_code == 200
    assert response.json()["warnings"], "expected an unparseable-URL warning"
    assert "does not look like" in response.json()["warnings"][0]


def test_public_url_warning_branches():
    """Direct unit sweep of the wrapper heuristics, dot-boundary included."""
    from nymeria.api.routers.settings import _public_url_warnings

    assert _public_url_warnings("nymeria.example.com")  # no scheme
    assert _public_url_warnings("ftp://nymeria.example.com")  # wrong scheme
    urldefense = _public_url_warnings(
        "https://urldefense.com/v3/__https://nymeria.example.com__;!!x"
    )
    assert urldefense and "urldefense" in urldefense[0]
    assert _public_url_warnings("https://sub.urldefense.proofpoint.com/v2/x")
    # Dot boundary: lookalike hosts must not trip the vendor patterns.
    assert _public_url_warnings("https://mysafelinks.protection.outlook.com/") == []
    # Userinfo trick: the wrapper host in the userinfo position is not the host.
    assert (
        _public_url_warnings("https://safelinks.protection.outlook.com@example.com/")
        == []
    )
    assert _public_url_warnings("https://nymeria.example.com/") == []


def test_get_env_var_rejects_model_methods(tmp_path: Path, monkeypatch):
    """Attribute probing is out: /env get model_dump must not dump Settings.

    hasattr answers True for BaseModel methods, and the bound-method repr
    embeds the full Settings state, every secret unmasked, labeled
    is_secret false. Only real model fields resolve.
    """
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    for probe in ("model_dump", "dict", "json", "copy", "model_copy"):
        response = client.get(f"/settings/env/{probe}", headers=_auth(token))
        assert response.status_code == 404, probe


def test_get_env_var_accepts_divergent_env_var_spelling(
    tmp_path: Path, monkeypatch
):
    """/env get speaks env-var names too: AWS_ACCESS_KEY_ID resolves to the
    s3_access_key_id field instead of answering Unknown (read/write symmetry
    with /env set)."""
    client, _agent, token, _provider = _client(monkeypatch, tmp_path)

    response = client.get("/settings/env/AWS_ACCESS_KEY_ID", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "s3_access_key_id"
    assert body["env_var"] == "AWS_ACCESS_KEY_ID"


def test_command_backend_update_settings_rejects_bad_typed_value(
    tmp_path: Path, monkeypatch
):
    """A wrong-typed value on the in-process path is a 400, not a stack trace."""
    backend, _agent = _backend_client(tmp_path)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(backend.update_settings(twitch_pulse_enabled="maybe"))

    assert excinfo.value.response.status_code == 400
    assert "twitch_pulse_enabled" in str(excinfo.value)


def test_command_backend_clearing_public_url(tmp_path: Path, monkeypatch):
    """nymeria_public_url is clearable: an explicit None writes an empty var.

    Pins the _CLEARABLE_NULL_SETTINGS entry; without it a wrong public URL
    could never be unset through the API.
    """
    monkeypatch.setenv("NYMERIA_PUBLIC_URL", "https://old.example.com")
    env_path = tmp_path / ".env"
    env_path.write_text("NYMERIA_PUBLIC_URL=https://old.example.com\n", encoding="utf-8")
    backend, _agent = _backend_client(tmp_path)

    result = asyncio.run(backend.update_settings(nymeria_public_url=None))

    assert result["updated"] == ["nymeria_public_url"]
    env_text = env_path.read_text(encoding="utf-8")
    assert "NYMERIA_PUBLIC_URL=https://old.example.com" not in env_text


def test_llm_provider_test_sends_billing_block_and_beta_on_cliproxy(
    tmp_path: Path,
    monkeypatch,
):
    """The anthropic probe mirrors the production CLIProxy path: safe
    Anthropic-Beta override plus the OAuth billing fingerprint. Without the
    block, premium Claude models 429 through CLIProxy on a valid token (the
    documented standalone-script gotcha), so /provider test reported a
    healthy route as failed (live 2026-08-10)."""
    from nymeria.vendor.react_agent.cliproxy import (
        CLIPROXY_ANTHROPIC_BETA_HEADER,
        CLIPROXY_BILLING_SYSTEM_BLOCK,
    )

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
            "llm_model": "claude-opus-5",
            "api_key": "cpx-secret-key",
            "llm_base_url": "http://localhost:8318",
        },
    )

    assert response.status_code == 200
    call = FakeAsyncClient.calls[0]
    assert call["headers"]["Anthropic-Beta"] == CLIPROXY_ANTHROPIC_BETA_HEADER
    assert call["json"]["system"] == [dict(CLIPROXY_BILLING_SYSTEM_BLOCK)]


def test_llm_provider_test_direct_anthropic_keeps_sdk_default_headers(
    tmp_path: Path,
    monkeypatch,
):
    """Direct api.anthropic.com probes stay on default headers: no beta
    override, no billing block (the CLIProxy treatment is proxy-scoped)."""
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
            "llm_model": "claude-opus-5",
            "api_key": "sk-ant-real",
        },
    )

    assert response.status_code == 200
    call = FakeAsyncClient.calls[0]
    assert "Anthropic-Beta" not in call["headers"]
    assert "system" not in call["json"]
    assert "User-Agent" not in call["headers"]


def test_llm_provider_test_custom_non_cliproxy_base_gets_no_cliproxy_treatment(
    tmp_path: Path,
    monkeypatch,
):
    """The CLIProxy scoping guard: a custom base that is NOT CLIProxy-shaped
    keeps the cloak UA (any-custom-base contract) but must NOT gain the
    Anthropic-Beta override or the billing block (review finding: the
    guard was previously untested; deleting it passed the suite)."""
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
            "llm_model": "claude-opus-5",
            "api_key": "sk-ant-real",
            "llm_base_url": "https://gateway.example.com",
        },
    )

    assert response.status_code == 200
    call = FakeAsyncClient.calls[0]
    assert call["headers"]["User-Agent"] == "claude-cli/2.1.113"
    assert "Anthropic-Beta" not in call["headers"]
    assert "system" not in call["json"]
