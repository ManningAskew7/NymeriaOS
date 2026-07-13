from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nymeria.config.settings import DEFAULT_LLM_FALLBACK_MODELS
from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_llm_config import (
    LLMConfigHost,
    activate_temporary_llm_fallback,
    get_llm_config_for_thread,
)
from nymeria.core.thread_config import ActiveLLMFallback, ThreadConfig, ThreadLLMConfig
from nymeria.core.time_utils import utc_now


_NO_THREAD_CONFIG = object()


class _Settings:
    llm_provider = "anthropic"
    llm_model = "claude-sonnet-4-6"
    llm_fallback_models = DEFAULT_LLM_FALLBACK_MODELS
    llm_temperature = 1.0
    llm_max_tokens = None
    llm_top_p = 0.9
    llm_top_k = None
    llm_frequency_penalty = 0.1
    llm_presence_penalty = 0.2
    llm_reasoning_effort = "medium"
    llm_extended_thinking = False
    llm_use_model_defaults = False
    llm_base_url = None
    llm_context_length = None
    llm_ollama_num_ctx = None
    llm_provider_route = None
    openai_api_mode = "responses"
    llm_stream_max_retries = 2
    llm_stream_retry_initial_delay = 1.0
    llm_stream_retry_max_delay = 8.0
    llm_fallback_hold_seconds = 7200
    anthropic_api_key = "anthropic-proxy-key"
    anthropic_direct_api_key = "anthropic-direct-key"
    openai_api_key = "openai-key"
    openrouter_api_key = "openrouter-key"

    def get_api_key_for_provider(self) -> str:
        return "generic-provider-key"


def _make_agent(llm_config: ThreadLLMConfig | object | None = _NO_THREAD_CONFIG, **settings_overrides):
    with patch.object(NymeriaAgent, "__init__", lambda self: None):
        agent = NymeriaAgent()

    settings = _Settings()
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    agent.settings = settings

    manager = MagicMock()
    if llm_config is _NO_THREAD_CONFIG:
        manager.get_config.return_value = None
    else:
        manager.get_config.return_value = ThreadConfig(
            thread_id="thread-1",
            llm_config=llm_config,
        )
    agent.thread_config_manager = manager
    return agent


def test_llm_config_resolution_uses_global_defaults_without_thread_config():
    agent = _make_agent()

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "anthropic"
    assert config.model == "claude-sonnet-4-6"
    assert config.temperature == 1.0
    assert config.reasoning_effort == "medium"
    assert config.base_url is None
    assert config.api_key == "anthropic-direct-key"
    assert config.openai_api_mode == "responses"
    assert len(config.fallbacks) == 1
    assert config.fallbacks[0].provider == "anthropic"
    assert config.fallbacks[0].model == "claude-haiku-4-5-20251001"


def test_llm_config_resolution_preserves_falsey_thread_overrides():
    agent = _make_agent(
        ThreadLLMConfig(
            temperature=0.0,
            max_tokens=1,
            extended_thinking=False,
            use_model_defaults=False,
            reasoning_effort="low",
            provider_route="openai_compat",
            openai_api_mode="chat_completions",
        ),
        llm_extended_thinking=True,
        llm_use_model_defaults=True,
        llm_provider="google",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "google"
    assert config.provider_route == "openai_compat"
    assert config.temperature == 0.0
    assert config.max_tokens == 1
    assert config.extended_thinking is False
    assert config.reasoning_effort == "low"
    assert config.openai_api_mode == "chat_completions"
    assert config.top_p == 0.9
    assert config.frequency_penalty == 0.1
    assert config.presence_penalty == 0.2


def test_provider_route_resolution_uses_global_then_provider_default():
    agent = _make_agent(
        ThreadLLMConfig(provider="google"),
        llm_provider="anthropic",
        llm_provider_route="openai_compat",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "google"
    assert config.provider_route == "openai_compat"

    agent = _make_agent(
        ThreadLLMConfig(provider="bedrock"),
        llm_provider="anthropic",
        llm_provider_route="openai_compat",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "bedrock"
    assert config.provider_route == "native"


def test_empty_string_thread_overrides_inherit_except_base_url_direct_api():
    agent = _make_agent(
        ThreadLLMConfig(
            provider="",
            model="",
            reasoning_effort="",
            base_url="",
            api_key="",
        ),
        llm_provider="openai",
        llm_model="gpt-5.5",
        llm_base_url="http://proxy.example/v1",
        llm_reasoning_effort="high",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "openai"
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "high"
    assert config.base_url is None
    assert config.api_key == "openai-key"


def test_llm_config_resolution_builds_global_fallback_chain():
    agent = _make_agent(
        llm_fallback_models="anthropic:claude-haiku-4-5-20251001",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert [fallback.model for fallback in config.fallbacks] == [
        "claude-haiku-4-5-20251001",
    ]
    assert [fallback.provider for fallback in config.fallbacks] == ["anthropic"]
    assert config.fallbacks[0].api_key == "anthropic-direct-key"


def test_model_defaults_clear_sampling_parameters_after_thread_resolution():
    agent = _make_agent(
        ThreadLLMConfig(use_model_defaults=True, temperature=0.0),
        llm_top_p=0.8,
        llm_frequency_penalty=0.4,
        llm_presence_penalty=0.5,
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.temperature is None
    assert config.top_p is None
    assert config.frequency_penalty is None
    assert config.presence_penalty is None


def test_anthropic_thread_provider_derives_cliproxy_subscription_base_url():
    agent = _make_agent(
        ThreadLLMConfig(provider="anthropic"),
        llm_provider="openai",
        llm_base_url="http://cli-proxy-api:8317/v1",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "anthropic"
    assert config.base_url == "http://cli-proxy-api:8317"
    assert config.api_key == "anthropic-proxy-key"


def test_context_overrides_resolve_from_thread_before_global():
    agent = _make_agent(
        ThreadLLMConfig(context_length=32_000, ollama_num_ctx=24_000),
        llm_context_length=128_000,
        llm_ollama_num_ctx=64_000,
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.context_length == 32_000
    assert config.ollama_num_ctx == 24_000


def test_local_ollama_context_probe_caps_detected_num_ctx_to_override():
    agent = _make_agent(
        ThreadLLMConfig(
            provider="ollama",
            model="qwen3:8b",
            context_length=16_000,
        )
    )

    with (
        patch("nymeria.core.agent_llm_config.detect_local_server_type", return_value="ollama"),
        patch("nymeria.core.agent_llm_config.query_local_context_length") as query_context,
        patch("nymeria.core.agent_llm_config.query_ollama_num_ctx", return_value=65_536),
    ):
        config = agent._get_llm_config_for_thread("thread-1")

    query_context.assert_not_called()
    assert config.provider == "ollama"
    assert config.context_length == 16_000
    assert config.ollama_num_ctx == 16_000


def test_active_fallback_temporarily_overrides_thread_llm_config():
    active = ActiveLLMFallback(
        provider="openai",
        model="gpt-5.5",
        source_provider="anthropic",
        source_model="claude-sonnet-4-6",
        expires_at=utc_now() + timedelta(hours=1),
        provider_route="openai_compat",
        openai_api_mode="chat_completions",
    )
    agent = _make_agent(
        ThreadLLMConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="thread-1",
        llm_config=ThreadLLMConfig(provider="anthropic", model="claude-sonnet-4-6"),
        active_llm_fallback=active,
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "openai"
    assert config.model == "gpt-5.5"
    assert config.provider_route == "native"
    assert config.openai_api_mode == "chat_completions"
    assert config.api_key == "openai-key"
    assert config.fallback_hold_seconds == 7200


def test_reasoning_effort_clamped_to_model_ladder_at_choke_point():
    # gpt-5.1 tops out at "high": an xhigh thread override is clamped.
    agent = _make_agent(
        ThreadLLMConfig(reasoning_effort="xhigh"),
        llm_provider="openai",
        llm_model="gpt-5.1",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.reasoning_effort == "high"


def test_reasoning_effort_xhigh_clamps_up_to_max_on_anthropic_46():
    agent = _make_agent(
        ThreadLLMConfig(reasoning_effort="xhigh"),
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.reasoning_effort == "max"


def test_reasoning_effort_off_clamps_to_floor_on_undisableable_model():
    agent = _make_agent(
        ThreadLLMConfig(reasoning_effort="off"),
        llm_provider="anthropic",
        llm_model="claude-fable-5",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.reasoning_effort == "low"


def test_reasoning_effort_off_passes_through_when_supported():
    agent = _make_agent(
        ThreadLLMConfig(reasoning_effort="off", extended_thinking=True),
        llm_provider="openai",
        llm_model="gpt-5.5",
    )

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.reasoning_effort == "off"
    assert config.extended_thinking is True


def test_reasoning_effort_none_is_not_clamped():
    agent = _make_agent(llm_reasoning_effort=None)

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.reasoning_effort is None


def test_expired_fallback_is_cleared_when_thread_idle():
    active = ActiveLLMFallback(
        provider="openai",
        model="gpt-5.5",
        source_provider="anthropic",
        source_model="claude-sonnet-4-6",
        expires_at=utc_now() - timedelta(seconds=1),
    )
    agent = _make_agent(
        ThreadLLMConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    tc = ThreadConfig(thread_id="thread-1", active_llm_fallback=active)
    agent.thread_config_manager.get_config.return_value = tc
    agent.thread_config_manager.delete_config.return_value = True
    agent.invalidate_thread_config_cache = MagicMock()

    config = agent._get_llm_config_for_thread("thread-1")

    assert config.provider == "anthropic"
    assert config.model == "claude-sonnet-4-6"
    agent.thread_config_manager.delete_config.assert_called_once_with("thread-1")
    agent.invalidate_thread_config_cache.assert_called_once_with("thread-1")


def test_public_accessor_matches_private_facade():
    """Slice 07 F10: the public ``get_llm_config_for_thread`` resolves the same
    config as the private facade it wraps."""
    agent = _make_agent()

    public = agent.get_llm_config_for_thread("thread-1")
    private = agent._get_llm_config_for_thread("thread-1")

    assert public.provider == private.provider
    assert public.model == private.model


def test_public_accessor_preserves_private_monkeypatch_seam():
    """The public accessor delegates to the private facade, so the many tests
    that monkeypatch ``_get_llm_config_for_thread`` still control it."""
    agent = _make_agent()
    sentinel = object()
    agent._get_llm_config_for_thread = lambda thread_id: (sentinel, thread_id)

    assert agent.get_llm_config_for_thread("thread-9") == (sentinel, "thread-9")


# --- dev-todo #76: acting-user credential-owner fallback for unclaimed threads


def _make_agent_with_accounts(owner: str | None, **settings_overrides):
    agent = _make_agent(**settings_overrides)
    accounts = MagicMock()
    accounts.get_thread_owner.return_value = owner
    agent.accounts_repo = accounts
    agent.credential_vault = MagicMock()
    return agent


def _capture_credential_owner(seen: dict, credential=None):
    def fake(provider, *, vault, owner_user_id=None, thread_id=None):
        seen["owner_user_id"] = owner_user_id
        return credential

    return fake


def test_acting_user_is_credential_owner_fallback_for_unclaimed_thread():
    """Synthetic autonomous threads (todo-<id>, trigger threads) have no
    thread_owners row; the acting user's user-owned vault credentials must
    resolve exactly as they would on an interactive thread."""
    from nymeria.core.llm_credentials import LLMProviderCredential

    agent = _make_agent_with_accounts(owner=None)
    seen: dict = {}
    credential = LLMProviderCredential(
        api_key="vault-user-key",
        base_url="https://vault.example",
        credential_id="cred-1",
    )

    with patch(
        "nymeria.core.agent_llm_config.get_llm_provider_credential",
        side_effect=_capture_credential_owner(seen, credential),
    ):
        config = agent._get_llm_config_for_thread("todo-42", "user-7")

    assert seen["owner_user_id"] == "user-7"
    assert config.api_key == "vault-user-key"
    assert config.base_url == "https://vault.example"


def test_thread_owner_row_always_wins_over_acting_user():
    agent = _make_agent_with_accounts(owner="owner-1")
    seen: dict = {}

    with patch(
        "nymeria.core.agent_llm_config.get_llm_provider_credential",
        side_effect=_capture_credential_owner(seen),
    ):
        agent._get_llm_config_for_thread("thread-1", "user-7")

    assert seen["owner_user_id"] == "owner-1"


def test_no_acting_user_keeps_prior_ownerless_resolution():
    agent = _make_agent_with_accounts(owner=None)
    seen: dict = {}

    with patch(
        "nymeria.core.agent_llm_config.get_llm_provider_credential",
        side_effect=_capture_credential_owner(seen),
    ):
        agent._get_llm_config_for_thread("todo-42")

    assert seen["owner_user_id"] is None


def test_graph_build_forwards_graph_user_as_acting_user():
    """build_agent_config passes the graph's user into the LLM-config accessor
    so every turn (interactive or autonomous) carries the owner fallback."""
    agent = _make_agent()
    agent.settings.tool_timeout = 30
    agent.settings.tool_output_max_chars = 1000
    agent.settings.log_level = "INFO"
    agent.TURN_SAME_TOOL_RESULT_LIMIT = 3
    agent._on_tool_timeout = lambda *args, **kwargs: None
    captured: dict = {}

    def fake_config(thread_id, acting_user_id=None):
        captured["thread_id"] = thread_id
        captured["acting_user_id"] = acting_user_id
        return MagicMock()

    agent._get_llm_config_for_thread = fake_config

    agent._build_agent_config(
        "prompt", {"configurable": {}}, "todo-9", None, acting_user_id="user-3"
    )

    assert captured == {"thread_id": "todo-9", "acting_user_id": "user-3"}


def test_public_accessor_forwards_acting_user_and_keeps_one_arg_seam():
    """With an acting user the public accessor forwards it; without one it
    keeps the historical one-arg call so single-parameter stubs never break."""
    agent = _make_agent()
    calls: list = []

    def stub(thread_id, acting_user_id=None):
        calls.append((thread_id, acting_user_id))
        return object()

    agent._get_llm_config_for_thread = stub

    agent.get_llm_config_for_thread("t-1", "user-5")
    agent.get_llm_config_for_thread("t-1")

    assert calls == [("t-1", "user-5"), ("t-1", None)]


# --- narrow LLMConfigHost Protocol (Workstream B) ----------------------------
#
# The tests above build a bare NymeriaAgent (patched __init__) and call the
# facade. These prove the tighter contract: the free functions run against a
# plain object annotated as ``LLMConfigHost`` (no cast, no NymeriaAgent).


class _LLMConfigHost:
    """Minimal, fully typed LLMConfigHost stub (no NymeriaAgent)."""

    def __init__(self, *, settings, thread_config_manager, thread_locks=None) -> None:
        self.settings = settings
        self.thread_config_manager = thread_config_manager
        self.accounts_repo = SimpleNamespace(get_thread_owner=lambda _tid: None)
        self.credential_vault = None
        self._thread_locks = thread_locks
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)


def _typed_host(**settings_overrides) -> _LLMConfigHost:
    settings = _Settings()
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    manager = MagicMock()
    manager.get_config.return_value = None
    manager.save_config.return_value = True
    return _LLMConfigHost(settings=settings, thread_config_manager=manager)


def test_llm_config_host_is_runtime_checkable():
    assert isinstance(_typed_host(), LLMConfigHost)


def test_get_llm_config_against_typed_host_without_agent():
    # No cast(Any, ...): declared as the narrow Protocol, so a plain
    # non-NymeriaAgent object must be accepted as the host.
    host: LLMConfigHost = _typed_host()

    config = get_llm_config_for_thread(host, "thread-x")

    assert config.provider == "anthropic"
    assert config.model == "claude-sonnet-4-6"
    assert config.base_url is None
    assert config.api_key == "anthropic-direct-key"


def test_activate_temporary_fallback_drives_typed_host_cache_invalidation():
    host = _typed_host()

    result = activate_temporary_llm_fallback(
        host,
        "thread-x",
        {
            "to_provider": "openai",
            "to_model": "gpt-5.5",
            "from_provider": "anthropic",
            "from_model": "claude-sonnet-4-6",
        },
    )

    assert result["hold_seconds"] == 7200
    assert result["expires_at"] is not None
    host.thread_config_manager.save_config.assert_called_once()
    assert host.invalidated == ["thread-x"]
