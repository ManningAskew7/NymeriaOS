from __future__ import annotations

from unittest.mock import MagicMock, patch

from nymeria.config.settings import DEFAULT_LLM_FALLBACK_MODELS
from nymeria.core.agent import NymeriaAgent
from nymeria.core.thread_config import ThreadConfig, ThreadLLMConfig


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
