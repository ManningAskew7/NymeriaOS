"""Tests for OpenAI Responses API mode provider wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import warnings

import anthropic
import httpx
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from nymeria.vendor.react_agent import providers
from nymeria.vendor.react_agent.cliproxy import (
    CLIPROXY_ANTHROPIC_BETA_HEADER,
    CLIPROXY_CLAUDE_USER_AGENT,
    CLIPROXY_REDACT_THINKING_BETA,
)
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import (
    ChatOpenAIWithReasoning,
    _convert_responses_chunk_to_generation_chunk_compat,
    _convert_openrouter_responses_chunk_to_generation_chunk,
    _process_responses_stream_chunk,
    _responses_chunk_indicates_reasoning,
    _flat_reasoning_content_replay_mode,
    _looks_like_deepseek_base_url,
    _normalize_groq_responses_payload,
    _normalize_openai_base_url,
    _normalize_openrouter_responses_payload,
    _normalize_sambanova_responses_payload,
    _should_disable_streaming_for_local_base_url,
    _supports_openrouter_style_reasoning_replay,
    _wrap_cliproxy_context_management_event,
    create_llm,
    create_llm_with_tools,
)

from _provider_test_helpers import llm_config  # type: ignore[import-not-found]


def _openai_config(**overrides) -> LLMConfig:
    return llm_config(
        {"provider": "openai", "model": "gpt-5.5", "base_url": "http://example.test/v1"},
        **overrides,
    )


def _openrouter_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "openrouter",
            "model": "qwen/qwen3.6-flash",
            "base_url": "https://openrouter.ai/api/v1",
        },
        **overrides,
    )


def _groq_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "base_url": "https://api.groq.com/openai/v1",
        },
        **overrides,
    )


def _sambanova_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "sambanova",
            "model": "gpt-oss-120b",
            "base_url": "https://api.sambanova.ai/v1",
        },
        **overrides,
    )


def _vercel_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "vercel",
            "model": "anthropic/claude-sonnet-4.5",
            "base_url": "https://ai-gateway.vercel.sh/v1",
        },
        **overrides,
    )


def _aihubmix_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "aihubmix",
            "model": "claude-sonnet-4-5",
            "base_url": "https://aihubmix.com/v1",
        },
        **overrides,
    )


def _nvidia_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "nvidia",
            "model": "deepseek-ai/deepseek-v4-pro",
            "base_url": "https://integrate.api.nvidia.com/v1",
        },
        **overrides,
    )


def _deepseek_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "deepseek",
            "model": "deepseek-reasoner",
            "base_url": "https://api.deepseek.com",
        },
        **overrides,
    )


def _fireworks_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "fireworks-ai",
            "model": "accounts/fireworks/models/qwen3-235b-a22b",
            "base_url": "https://api.fireworks.ai/inference/v1",
        },
        **overrides,
    )


def _moonshot_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "moonshotai",
            "model": "kimi-k2-thinking",
            "base_url": "https://api.moonshot.ai/v1",
        },
        **overrides,
    )


def _alibaba_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "alibaba",
            "model": "qwen3.5-plus",
            "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        },
        **overrides,
    )


def _baseten_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "baseten",
            "model": "deepseek-ai/DeepSeek-V4-Pro",
            "base_url": "https://inference.baseten.co/v1",
        },
        **overrides,
    )


def _litellm_config(**overrides) -> LLMConfig:
    return llm_config(
        {"provider": "litellm", "model": "deepseek-reasoner", "base_url": "http://localhost:4000"},
        **overrides,
    )


def _together_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "togetherai",
            "model": "deepseek-ai/DeepSeek-V3.1",
            "base_url": "https://api.together.ai/v1",
        },
        **overrides,
    )


def _novita_config(**overrides) -> LLMConfig:
    return llm_config(
        {
            "provider": "novita-ai",
            "model": "deepseek/deepseek-r1",
            "base_url": "https://api.novita.ai/openai",
        },
        **overrides,
    )


def _anthropic_config(**overrides) -> LLMConfig:
    return llm_config({"provider": "anthropic", "model": "claude-sonnet-4-20250514"}, **overrides)


def _run_in_new_event_loop(async_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(async_fn())
    finally:
        loop.run_until_complete(providers.close_provider_async_http_pools_for_loop(loop))
        loop.close()


class _ToolOrderingFakeModel(BaseChatModel):
    seen_tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "tool-ordering-fake"

    def bind_tools(self, tools, **kwargs):
        self.seen_tool_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))]
        )


def test_chat_openai_with_reasoning_is_importable_stable_class():
    assert providers._get_chat_openai_with_reasoning() is ChatOpenAIWithReasoning
    assert (
        providers._get_chat_openai_with_reasoning()
        is providers._get_chat_openai_with_reasoning()
    )

    openai_llm = create_llm(_openai_config())
    openrouter_llm = create_llm(_openrouter_config())

    assert type(openai_llm) is ChatOpenAIWithReasoning
    assert type(openrouter_llm) is ChatOpenAIWithReasoning


def test_openai_compatible_llms_omit_sampling_params_for_provider_defaults(
    monkeypatch,
):
    captured: list[dict] = []

    class CaptureModel:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(providers, "ChatOpenAIWithReasoning", CaptureModel)
    monkeypatch.setattr(
        providers,
        "_attach_loop_local_openai_async_http_client",
        lambda kwargs, **_options: None,
    )

    providers._create_openai_llm(
        _openai_config(
            temperature=None,
            top_p=None,
            frequency_penalty=None,
            presence_penalty=None,
        )
    )
    providers._create_openrouter_llm(
        _openrouter_config(
            temperature=None,
            top_p=None,
            frequency_penalty=None,
            presence_penalty=None,
        )
    )

    for kwargs in captured:
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert "frequency_penalty" not in kwargs
        assert "presence_penalty" not in kwargs


def test_create_llm_with_tools_sorts_tools_deterministically():
    @tool
    def zz_tool(value: str) -> str:
        """Echo a value."""
        return value

    @tool
    def longer_tool(value: str) -> str:
        """Echo a value."""
        return value

    @tool
    def aa_tool(value: str) -> str:
        """Echo a value."""
        return value

    model = _ToolOrderingFakeModel()

    create_llm_with_tools(
        LLMConfig(provider="custom", custom_llm=model),
        [zz_tool, longer_tool, aa_tool],
    )

    assert model.seen_tool_names == ["longer_tool", "aa_tool", "zz_tool"]


def test_openai_responses_mode_replays_checkpoint_items_payload():
    llm = create_llm(
        _openai_config(
            openai_api_mode="responses",
            extended_thinking=True,
            reasoning_effort="high",
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
        AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "id": "rs_123",
                    "summary": [
                        {"type": "summary_text", "text": "Prior thought"}
                    ],
                },
                {"type": "text", "text": "Hello", "id": "msg_123"},
            ],
            response_metadata={"id": "resp_123"},
        ),
        HumanMessage(content="What did I say?"),
    ])

    assert "input" in payload
    assert "messages" not in payload
    assert "previous_response_id" not in payload
    assert payload["input"][0] == {
        "content": "You are Nymeria.",
        "role": "system",
        "type": "message",
    }
    assert payload["input"][1] == {
        "content": "Hi",
        "role": "user",
        "type": "message",
    }
    assert payload["input"][2] == {
        "type": "reasoning",
        "id": "rs_123",
        "summary": [{"type": "summary_text", "text": "Prior thought"}],
    }
    assert payload["input"][-1] == {
        "content": "What did I say?",
        "role": "user",
        "type": "message",
    }
    assert payload["reasoning"] == {"summary": "auto", "effort": "high"}
    assert payload["store"] is False


def test_openai_default_mode_uses_responses_payload():
    llm = create_llm(_openai_config())

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "input" in payload
    assert "messages" not in payload
    assert payload["store"] is False


def test_openai_client_retries_disabled_for_central_retry_policy():
    llm = create_llm(_openai_config())

    assert llm.root_client.max_retries == 0
    assert llm.root_async_client.max_retries == 0


def test_openrouter_default_mode_uses_responses_payload():
    llm = create_llm(
        _openrouter_config(
            extended_thinking=True,
            reasoning_effort="high",
            max_tokens=1234,
        )
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "input" in payload
    assert "messages" not in payload
    assert "previous_response_id" not in payload
    assert payload["store"] is False
    assert payload["max_output_tokens"] == 1234
    assert payload["reasoning"] == {"summary": "auto", "effort": "high"}


def test_openrouter_client_retries_disabled_for_central_retry_policy():
    llm = create_llm(_openrouter_config())

    assert llm.root_client.max_retries == 0
    assert llm.root_async_client.max_retries == 0


def test_direct_openai_loop_local_client_preserves_stream_usage_default(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    async def touch_client():
        llm = create_llm(_openai_config(base_url=None))
        return llm.stream_usage, llm.root_async_client.max_retries

    stream_usage, max_retries = _run_in_new_event_loop(touch_client)

    assert stream_usage is True
    assert max_retries == 0


def test_openai_loop_local_client_honors_openai_api_base_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE", "http://env-openai.test/v1")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    async def touch_client():
        llm = create_llm(_openai_config(base_url=None))
        return llm.root_async_client._client.base_url, llm.stream_usage

    base_url, stream_usage = _run_in_new_event_loop(touch_client)

    assert str(base_url).rstrip("/") == "http://env-openai.test/v1"
    assert stream_usage is None


def test_openai_async_http_client_is_reused_within_one_event_loop():
    async def touch_two_clients():
        first = create_llm(_openai_config())
        second = create_llm(_openai_config())
        return first.root_async_client._client, second.root_async_client._client

    first_http_client, second_http_client = _run_in_new_event_loop(touch_two_clients)

    assert first_http_client is second_http_client


def test_openai_async_http_client_is_loop_local_across_event_loops():
    async def touch_client():
        llm = create_llm(_openai_config())
        return llm.root_async_client._client

    first_http_client = _run_in_new_event_loop(touch_client)
    second_http_client = _run_in_new_event_loop(touch_client)

    assert first_http_client is not second_http_client


def test_openrouter_async_http_client_is_loop_local_across_event_loops():
    async def touch_client():
        llm = create_llm(_openrouter_config())
        return llm.root_async_client._client

    first_http_client = _run_in_new_event_loop(touch_client)
    second_http_client = _run_in_new_event_loop(touch_client)

    assert first_http_client is not second_http_client


def test_openai_async_http_pool_close_is_loop_scoped():
    loop_one = asyncio.new_event_loop()
    loop_two = asyncio.new_event_loop()

    async def touch_client():
        llm = create_llm(_openai_config())
        return llm.root_async_client._client

    try:
        first_http_client = loop_one.run_until_complete(touch_client())
        second_http_client = loop_two.run_until_complete(touch_client())

        assert first_http_client is not second_http_client
        assert not first_http_client.is_closed
        assert not second_http_client.is_closed

        loop_one.run_until_complete(
            providers.close_openai_async_http_pools_for_loop(loop_one)
        )

        assert first_http_client.is_closed
        assert not second_http_client.is_closed
    finally:
        loop_two.run_until_complete(
            providers.close_openai_async_http_pools_for_loop(loop_two)
        )
        loop_one.close()
        loop_two.close()


def test_anthropic_client_retries_disabled_for_central_retry_policy():
    llm = create_llm(_anthropic_config())

    assert llm.max_retries == 0


def test_anthropic_non_cliproxy_base_url_does_not_use_context_management_adapter(
    monkeypatch,
):
    def fail_if_checked(_chat_model_cls):
        raise AssertionError("adapter guard should only run for CLIProxy URLs")

    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        fail_if_checked,
    )

    llm = create_llm(_anthropic_config(base_url="https://api.anthropic.com"))

    assert isinstance(llm, ChatAnthropic)
    assert type(llm) is not ChatAnthropic
    assert "NymeriaChatAnthropic" in type(llm).__name__
    assert "CLIProxy" not in type(llm).__name__
    default_headers = llm._client_params.get("default_headers") or {}
    assert default_headers.get("User-Agent") != CLIPROXY_CLAUDE_USER_AGENT
    assert "Anthropic-Beta" not in default_headers


def test_anthropic_direct_tool_payload_adds_cache_breakpoint():
    @tool
    def alpha_tool(value: str) -> str:
        """Echo a value."""
        return value

    llm = create_llm(_anthropic_config(base_url="https://api.anthropic.com"))
    bound = llm.bind_tools([alpha_tool])

    payload = bound.bound._get_request_payload(
        [HumanMessage(content="hi")],
        **bound.kwargs,
    )

    assert payload["tools"][-1]["name"] == "alpha_tool"
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_tool_cache_breakpoint_respects_existing_annotations():
    payload = {
        "tools": [
            {
                "name": "existing",
                "cache_control": {"type": "ephemeral"},
            },
            {"name": "plain"},
        ]
    }

    providers._inject_tool_cache_control(payload)

    assert "cache_control" not in payload["tools"][1]


def test_anthropic_cliproxy_base_url_uses_context_management_adapter(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    llm = create_llm(
        _anthropic_config(base_url="http://cli-proxy-api-latest:8317")
    )

    assert isinstance(llm, ChatAnthropic)
    assert type(llm) is not ChatAnthropic
    assert type(llm).__name__ == "CLIProxyCompatibleChatAnthropic"


def test_anthropic_async_client_is_reused_within_one_event_loop(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    llm = create_llm(
        _anthropic_config(base_url="http://cli-proxy-api-latest:8317")
    )

    async def touch_client_twice():
        return llm._async_client, llm._async_client

    first_async_client, second_async_client = _run_in_new_event_loop(
        touch_client_twice
    )

    assert first_async_client is second_async_client


def test_anthropic_async_client_is_loop_local_across_event_loops(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    llm = create_llm(
        _anthropic_config(base_url="http://cli-proxy-api-latest:8317")
    )

    async def touch_client():
        return llm._async_client

    first_async_client = _run_in_new_event_loop(touch_client)
    second_async_client = _run_in_new_event_loop(touch_client)

    assert first_async_client is not second_async_client
    assert first_async_client._client is not second_async_client._client


def test_anthropic_async_client_preserves_cliproxy_http_settings(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )
    monkeypatch.setenv("ANTHROPIC_PROXY", "http://proxy.example:8080")

    captured_http_kwargs = []
    original_http_client_cls = anthropic.DefaultAsyncHttpxClient

    class RecordingDefaultAsyncHttpxClient(original_http_client_cls):
        def __init__(self, **kwargs):
            captured_http_kwargs.append(dict(kwargs))
            super().__init__(**kwargs)

    monkeypatch.setattr(
        anthropic,
        "DefaultAsyncHttpxClient",
        RecordingDefaultAsyncHttpxClient,
    )

    llm = create_llm(
        _anthropic_config(
            base_url="http://cli-proxy-api-latest:8317",
            request_timeout=123,
        )
    )

    async def touch_client():
        return llm._async_client

    async_client = _run_in_new_event_loop(touch_client)

    assert async_client.max_retries == 0
    assert async_client.timeout == 123
    assert async_client.default_headers["User-Agent"] == CLIPROXY_CLAUDE_USER_AGENT
    assert (
        async_client.default_headers["Anthropic-Beta"]
        == CLIPROXY_ANTHROPIC_BETA_HEADER
    )
    assert (
        CLIPROXY_REDACT_THINKING_BETA
        not in async_client.default_headers["Anthropic-Beta"]
    )
    assert str(async_client.base_url).rstrip("/") == (
        "http://cli-proxy-api-latest:8317"
    )
    assert captured_http_kwargs == [
        {
            "base_url": "http://cli-proxy-api-latest:8317",
            "timeout": 123,
            "proxy": "http://proxy.example:8080",
        }
    ]


def test_anthropic_async_http_client_uses_sdk_default_client_defaults(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    llm = create_llm(
        _anthropic_config(base_url="http://cli-proxy-api-latest:8317")
    )

    async def touch_http_client():
        async_client = llm._async_client
        return async_client._client, async_client.timeout

    http_client, wrapper_timeout = _run_in_new_event_loop(touch_http_client)

    assert isinstance(http_client, anthropic.DefaultAsyncHttpxClient)
    assert type(http_client) is not httpx.AsyncClient
    assert http_client.follow_redirects is True
    assert http_client.timeout.read == 600
    assert wrapper_timeout.read == 600


def test_cliproxy_context_management_dict_is_wrapped_for_langchain():
    event = SimpleNamespace(context_management={"strategy": "clear"})

    wrapped = _wrap_cliproxy_context_management_event(event)

    assert wrapped is event
    assert wrapped.context_management.model_dump() == {"strategy": "clear"}


def test_cliproxy_context_management_adapter_detects_affected_langchain(
    monkeypatch,
):
    class AffectedChatAnthropic:
        def _make_message_chunk_from_anthropic_event(self, event):
            context_management = getattr(event, "context_management", None)
            return context_management.model_dump()

    monkeypatch.setattr(
        providers,
        "_get_langchain_anthropic_version",
        lambda: "1.4.2",
    )

    use_adapter, reason = providers._should_use_cliproxy_context_management_adapter(
        AffectedChatAnthropic
    )

    assert use_adapter is True
    assert "context_management.model_dump" in reason


def test_cliproxy_context_management_adapter_skips_fixed_langchain(monkeypatch):
    class FixedChatAnthropic:
        def _make_message_chunk_from_anthropic_event(self, event):
            context_management = getattr(event, "context_management", None)
            if isinstance(context_management, dict):
                return context_management
            return None

    monkeypatch.setattr(
        providers,
        "_get_langchain_anthropic_version",
        lambda: "1.4.2",
    )

    use_adapter, reason = providers._should_use_cliproxy_context_management_adapter(
        FixedChatAnthropic
    )

    assert use_adapter is False
    assert "no longer calls" in reason


def test_cliproxy_context_management_adapter_skips_unverified_major(monkeypatch):
    class FutureChatAnthropic:
        def _make_message_chunk_from_anthropic_event(self, event):
            return event

    monkeypatch.setattr(
        providers,
        "_get_langchain_anthropic_version",
        lambda: "2.0.0",
    )

    use_adapter, reason = providers._should_use_cliproxy_context_management_adapter(
        FutureChatAnthropic
    )

    assert use_adapter is False
    assert "outside the verified patch range" in reason


def test_openrouter_chat_completions_mode_stays_on_messages_payload():
    llm = create_llm(
        _openrouter_config(openai_api_mode="chat_completions")
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "messages" in payload
    assert "input" not in payload


def test_openai_chat_completions_mode_stays_on_messages_payload():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _openai_config(
                openai_api_mode="chat_completions",
                reasoning_effort="high",
            )
        )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "messages" in payload
    assert "input" not in payload
    assert payload["reasoning_effort"] == "high"


def test_openai_chat_completions_does_not_replay_reasoning_metadata():
    llm = create_llm(
        _openai_config(openai_api_mode="chat_completions")
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Hi"),
        AIMessage(
            content="Hello",
            additional_kwargs={"reasoning_content": "Private thought"},
        ),
        HumanMessage(content="Again"),
    ])

    assistant = payload["messages"][1]
    assert "reasoning" not in assistant
    assert "reasoning_details" not in assistant


def test_openrouter_streaming_chunk_preserves_reasoning_details_metadata():
    llm = create_llm(
        _openrouter_config(
            openai_api_mode="chat_completions",
            extended_thinking=True,
            reasoning_effort="low",
        )
    )

    chunk = {
        "choices": [
            {
                "delta": {
                    "role": "assistant",
                    "reasoning_details": [
                        {
                            "type": "reasoning.text",
                            "text": "Need context",
                            "format": "unknown",
                            "index": 0,
                        }
                    ],
                }
            }
        ]
    }

    generation_chunk = llm._convert_chunk_to_generation_chunk(
        chunk,
        AIMessageChunk,
        {},
    )

    extras = generation_chunk.message.additional_kwargs
    assert extras["reasoning_content"] == "Need context"
    assert extras["reasoning_details"] == [
        {
            "type": "reasoning.text",
            "text": "Need context",
            "format": "unknown",
        }
    ]


def test_openrouter_replays_reasoning_details_in_chat_payload():
    llm = create_llm(
        _openrouter_config(
            openai_api_mode="chat_completions",
            extended_thinking=True,
            reasoning_effort="low",
        )
    )
    details = [
        {
            "type": "reasoning.text",
            "text": "Prior thought",
            "format": "unknown",
            "index": 0,
        }
    ]

    payload = llm._get_request_payload([
        HumanMessage(content="Hi"),
        AIMessage(
            content="Hello",
            additional_kwargs={
                "reasoning_content": "Prior thought",
                "reasoning_details": details,
            },
        ),
        HumanMessage(content="Again"),
    ])

    assistant = payload["messages"][1]
    assert assistant["reasoning_details"] == details
    assert "reasoning" not in assistant


def test_openrouter_replays_reasoning_string_when_details_absent():
    llm = create_llm(
        _openrouter_config(
            openai_api_mode="chat_completions",
            extended_thinking=True,
            reasoning_effort="low",
        )
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Hi"),
        AIMessage(
            content="Hello",
            additional_kwargs={"reasoning_content": "Prior thought"},
        ),
        HumanMessage(content="Again"),
    ])

    assistant = payload["messages"][1]
    assert assistant["reasoning"] == "Prior thought"


def test_openrouter_responses_payload_normalization_adds_required_ids():
    payload = {
        "previous_response_id": "resp_old",
        "input": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "todo_add",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "{}",
            },
        ],
    }

    normalized = _normalize_openrouter_responses_payload(payload)
    first_ids = [item["id"] for item in normalized["input"]]
    normalized_again = _normalize_openrouter_responses_payload(normalized)

    assert "previous_response_id" not in normalized
    assert normalized["input"][0]["status"] == "completed"
    assert normalized["input"][0]["id"].startswith("msg_")
    assert normalized["input"][1]["id"].startswith("fc_")
    assert normalized["input"][2]["id"].startswith("fc_output_")
    assert [item["id"] for item in normalized_again["input"]] == first_ids


def test_openrouter_responses_payload_strips_inline_thinking_from_replay():
    payload = {
        "input": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "private chain</think>\n\nVisible answer",
                    },
                    {
                        "type": "output_text",
                        "text": "Keep <think>hidden</think> visible",
                    },
                ],
            },
        ],
    }

    normalized = _normalize_openrouter_responses_payload(payload)

    content = normalized["input"][0]["content"]
    assert content == [
        {"type": "output_text", "text": "Visible answer"},
        {"type": "output_text", "text": "Keep visible"},
    ]


def test_openrouter_responses_reasoning_delta_becomes_content_block():
    _, _, _, generation_chunk = (
        _convert_openrouter_responses_chunk_to_generation_chunk(
            {
                "type": "response.reasoning.delta",
                "delta": "Need context",
                "output_index": 0,
            },
            -1,
            -1,
            -1,
        )
    )

    assert generation_chunk is not None
    assert generation_chunk.message.content == [
        {
            "type": "reasoning",
            "summary": [
                {"index": 0, "type": "summary_text", "text": "Need context"}
            ],
            "index": 0,
        }
    ]


def test_openrouter_responses_reasoning_text_delta_becomes_content_block():
    _, _, _, generation_chunk = (
        _convert_openrouter_responses_chunk_to_generation_chunk(
            {
                "type": "response.reasoning_text.delta",
                "delta": "Claude thought",
                "output_index": 0,
                "content_index": 0,
            },
            -1,
            -1,
            -1,
        )
    )

    assert generation_chunk is not None
    assert generation_chunk.message.content == [
        {
            "type": "reasoning",
            "summary": [
                {"index": 0, "type": "summary_text", "text": "Claude thought"}
            ],
            "index": 0,
        }
    ]


def test_responses_converter_missing_private_symbol_streams_text(monkeypatch):
    from langchain_openai.chat_models import base as lc_openai_base

    monkeypatch.delattr(
        lc_openai_base,
        "_convert_responses_chunk_to_generation_chunk",
        raising=False,
    )

    _, _, _, generation_chunk = _convert_responses_chunk_to_generation_chunk_compat(
        SimpleNamespace(
            type="response.output_text.delta",
            delta="Hello",
            output_index=0,
            content_index=0,
        ),
        -1,
        -1,
        -1,
        metadata={"headers": {"x-request-id": "req_123"}},
        output_version="responses/v1",
    )

    assert generation_chunk is not None
    assert generation_chunk.message.content == [
        {"type": "text", "text": "Hello", "index": 0}
    ]
    assert generation_chunk.message.response_metadata["model_provider"] == "openai"
    assert generation_chunk.message.response_metadata["headers"] == {
        "x-request-id": "req_123"
    }


def test_responses_converter_missing_private_symbol_streams_reasoning(monkeypatch):
    from langchain_openai.chat_models import base as lc_openai_base

    monkeypatch.delattr(
        lc_openai_base,
        "_convert_responses_chunk_to_generation_chunk",
        raising=False,
    )

    _, _, _, generation_chunk = _convert_responses_chunk_to_generation_chunk_compat(
        SimpleNamespace(
            type="response.reasoning_summary_text.delta",
            delta="Need context",
            output_index=0,
            summary_index=0,
            item_id="rs_123",
        ),
        -1,
        -1,
        -1,
        output_version="responses/v1",
    )

    assert generation_chunk is not None
    assert generation_chunk.message.content == [
        {
            "type": "reasoning",
            "summary": [
                {"index": 0, "type": "summary_text", "text": "Need context"}
            ],
            "index": 0,
            "id": "rs_123",
        }
    ]


def test_local_cliproxy_sidecar_does_not_disable_streaming():
    llm = create_llm(
        _openai_config(
            base_url="http://localhost:8318",
        )
    )

    assert "streaming" not in llm.model_fields_set
    assert str(llm.openai_api_base).rstrip("/") == "http://localhost:8318/v1"


def test_openai_cliproxy_base_url_normalizes_to_v1():
    assert (
        _normalize_openai_base_url("http://cli-proxy-api:8317")
        == "http://cli-proxy-api:8317/v1"
    )
    assert (
        _normalize_openai_base_url("http://cli-proxy-api-latest:8317/v1")
        == "http://cli-proxy-api-latest:8317/v1"
    )
    assert (
        _normalize_openai_base_url("localhost:8318")
        == "localhost:8318/v1"
    )
    assert (
        _normalize_openai_base_url("http://localhost:8080")
        == "http://localhost:8080"
    )


def test_local_llm_base_url_still_disables_streaming():
    llm = create_llm(
        _openai_config(
            model="local-llm",
            base_url="http://localhost:8080/v1",
        )
    )

    assert "streaming" in llm.model_fields_set
    assert llm.streaming is False


def test_local_llm_private_urls_disable_streaming():
    assert _should_disable_streaming_for_local_base_url("http://192.168.1.20:8080/v1")
    assert _should_disable_streaming_for_local_base_url("http://100.77.243.5:11434/v1")


def test_openai_local_llm_allows_missing_key_and_adds_ollama_options(monkeypatch):
    captured: list[dict] = []

    class CaptureModel:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(providers, "ChatOpenAIWithReasoning", CaptureModel)
    monkeypatch.setattr(
        providers,
        "_attach_loop_local_openai_async_http_client",
        lambda kwargs, **_options: None,
    )

    providers._create_openai_llm(
        _openai_config(
            api_key=None,
            model="qwen3:8b",
            base_url="http://localhost:11434/v1",
            ollama_num_ctx=32768,
            extended_thinking=False,
            reasoning_effort=None,
        )
    )

    kwargs = captured[0]
    assert kwargs["api_key"] == "not-needed"
    assert kwargs["streaming"] is False
    assert kwargs["extra_body"] == {
        "options": {"num_ctx": 32768},
        "think": False,
    }


def test_streaming_heuristic_handles_schemeless_urls():
    assert not _should_disable_streaming_for_local_base_url("localhost:8318/v1")
    assert _should_disable_streaming_for_local_base_url("localhost:8080/v1")


def test_streaming_heuristic_matches_cliproxy_hostname_not_path():
    assert not _should_disable_streaming_for_local_base_url(
        "http://cli-proxy-api-latest:8317/v1"
    )
    assert _should_disable_streaming_for_local_base_url(
        "http://localhost:8080/cliproxy-alike/v1"
    )


def test_groq_defaults_to_responses_and_strips_store():
    llm = create_llm(_groq_config(extended_thinking=True, reasoning_effort="low"))

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "input" in payload
    assert "messages" not in payload
    # Groq's Responses API is stateless-only; the state fields are dropped.
    assert "store" not in payload
    assert "previous_response_id" not in payload
    # Groq accepts the nested reasoning object as-is (effort honored).
    assert payload["reasoning"] == {"summary": "auto", "effort": "low"}


def test_sambanova_defaults_to_responses_and_flattens_reasoning():
    llm = create_llm(
        _sambanova_config(extended_thinking=True, reasoning_effort="high")
    )

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "input" in payload
    assert "messages" not in payload
    assert "store" not in payload
    # SambaNova takes a top-level reasoning_effort scalar, not the nested object.
    assert "reasoning" not in payload
    assert payload["reasoning_effort"] == "high"


def test_groq_responses_normalizer_drops_state_fields_only():
    payload = {
        "input": [],
        "store": False,
        "previous_response_id": "resp_old",
        "reasoning": {"summary": "auto", "effort": "low"},
    }

    normalized = _normalize_groq_responses_payload(payload)

    assert "store" not in normalized
    assert "previous_response_id" not in normalized
    # Reasoning object is left intact for Groq.
    assert normalized["reasoning"] == {"summary": "auto", "effort": "low"}


def test_sambanova_responses_normalizer_flattens_effort():
    payload = {
        "input": [],
        "store": False,
        "reasoning": {"summary": "auto", "effort": "medium"},
    }

    normalized = _normalize_sambanova_responses_payload(payload)

    assert "store" not in normalized
    assert "reasoning" not in normalized
    assert normalized["reasoning_effort"] == "medium"


def test_sambanova_responses_normalizer_without_reasoning_is_noop_on_effort():
    payload = {"input": [], "store": False}

    normalized = _normalize_sambanova_responses_payload(payload)

    assert "store" not in normalized
    assert "reasoning_effort" not in normalized


def test_groq_and_sambanova_advertise_responses_support():
    from nymeria.config.llm_providers import get_llm_provider_spec

    for provider_id in ("groq", "sambanova"):
        spec = get_llm_provider_spec(provider_id)
        assert spec is not None, provider_id
        assert spec.supports_responses is True, provider_id
        assert spec.default_api_mode == "responses", provider_id


def test_openrouter_style_reasoning_replay_predicate():
    # Provider-id dispatch (primary path).
    assert _supports_openrouter_style_reasoning_replay("openrouter")
    assert _supports_openrouter_style_reasoning_replay("vercel")
    assert _supports_openrouter_style_reasoning_replay("aihubmix")
    assert not _supports_openrouter_style_reasoning_replay("openai")
    assert not _supports_openrouter_style_reasoning_replay("groq")
    # Base-URL fallback (wrapper built without an id).
    assert _supports_openrouter_style_reasoning_replay(None, "https://openrouter.ai/api/v1")
    assert _supports_openrouter_style_reasoning_replay(
        None, "https://ai-gateway.vercel.sh/v1"
    )
    assert _supports_openrouter_style_reasoning_replay(None, "https://aihubmix.com/v1")
    # Not OpenRouter-shaped: stock OpenAI, Groq, and Vercel's v0 (a different host).
    assert not _supports_openrouter_style_reasoning_replay(
        None, "https://api.openai.com/v1"
    )
    assert not _supports_openrouter_style_reasoning_replay(
        None, "https://api.groq.com/openai/v1"
    )
    assert not _supports_openrouter_style_reasoning_replay(None, "https://api.v0.dev/v1")


def test_vercel_defaults_to_responses_payload():
    llm = create_llm(_vercel_config())

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    # Vercel uses the stock OpenAI Responses shape (no provider normalizer).
    assert "input" in payload
    assert "messages" not in payload
    assert payload["store"] is False


def test_nvidia_defaults_to_responses_payload():
    llm = create_llm(_nvidia_config())

    payload = llm._get_request_payload([
        SystemMessage(content="You are Nymeria."),
        HumanMessage(content="Hi"),
    ])

    assert "input" in payload
    assert "messages" not in payload
    assert payload["store"] is False


def test_vercel_chat_completions_replays_reasoning_details():
    llm = create_llm(
        _vercel_config(
            openai_api_mode="chat_completions",
            extended_thinking=True,
            reasoning_effort="low",
        )
    )
    details = [
        {
            "type": "reasoning.text",
            "text": "Prior thought",
            "format": "unknown",
            "index": 0,
        }
    ]

    payload = llm._get_request_payload([
        HumanMessage(content="Hi"),
        AIMessage(
            content="Hello",
            additional_kwargs={
                "reasoning_content": "Prior thought",
                "reasoning_details": details,
            },
        ),
        HumanMessage(content="Again"),
    ])

    assistant = payload["messages"][1]
    assert assistant["reasoning_details"] == details


def test_aihubmix_chat_completions_replays_reasoning_details():
    llm = create_llm(_aihubmix_config(openai_api_mode="chat_completions"))
    details = [
        {
            "type": "reasoning.text",
            "text": "Prior thought",
            "format": "unknown",
            "index": 0,
        }
    ]

    payload = llm._get_request_payload([
        HumanMessage(content="Hi"),
        AIMessage(
            content="Hello",
            additional_kwargs={
                "reasoning_content": "Prior thought",
                "reasoning_details": details,
            },
        ),
        HumanMessage(content="Again"),
    ])

    assistant = payload["messages"][1]
    assert assistant["reasoning_details"] == details


def test_deepseek_flat_reasoning_replay_mode():
    assert _looks_like_deepseek_base_url("https://api.deepseek.com")
    # Provider-id dispatch (primary path).
    assert _flat_reasoning_content_replay_mode("deepseek") == "tool_calls_only"
    assert _flat_reasoning_content_replay_mode("openai") is None
    # Base-URL fallback for the providers wired before id-threading.
    assert _flat_reasoning_content_replay_mode(None, "https://api.deepseek.com") == (
        "tool_calls_only"
    )
    # Not a flat-replay provider: no replay.
    assert _flat_reasoning_content_replay_mode(None, "https://api.openai.com/v1") is None
    assert _flat_reasoning_content_replay_mode(None, "https://openrouter.ai/api/v1") is None


def test_alibaba_qwen_and_baseten_use_tool_calls_only_replay():
    # The Alibaba/Qwen family spans five hosts (one with no `dashscope`
    # substring) and Baseten is one host serving many models; both dispatch on
    # the provider id, not a base-URL guess.
    for provider_id in (
        "alibaba",
        "alibaba-cn",
        "alibaba-coding-plan",
        "alibaba-coding-plan-cn",
        "qwen-oauth",
        "baseten",
    ):
        assert _flat_reasoning_content_replay_mode(provider_id) == "tool_calls_only", (
            provider_id
        )


def test_deepseek_replays_reasoning_content_on_tool_call_turn():
    llm = create_llm(_deepseek_config())

    ai_with_tools = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "Let me look up the weather."},
        tool_calls=[
            {
                "name": "get_weather",
                "args": {"city": "Paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Weather in Paris?"),
        ai_with_tools,
        ToolMessage(content="Sunny, 20C", tool_call_id="call_1"),
        HumanMessage(content="And London?"),
    ])

    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "Let me look up the weather."


def test_deepseek_strips_reasoning_content_on_non_tool_turn():
    llm = create_llm(_deepseek_config())

    ai_no_tools = AIMessage(
        content="It is sunny.",
        additional_kwargs={"reasoning_content": "Private chain of thought."},
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Weather in Paris?"),
        ai_no_tools,
        HumanMessage(content="Thanks"),
    ])

    assistant = payload["messages"][1]
    # deepseek-reasoner 400s if reasoning_content is present on a non-tool turn.
    assert "reasoning_content" not in assistant


def test_deepseek_has_no_responses_support():
    from nymeria.config.llm_providers import get_llm_provider_spec

    spec = get_llm_provider_spec("deepseek")
    assert spec is not None
    assert spec.supports_responses is False


def test_fireworks_and_moonshot_use_flat_replay_all():
    # Provider-id dispatch (primary path).
    assert _flat_reasoning_content_replay_mode("fireworks-ai") == "all"
    assert _flat_reasoning_content_replay_mode("moonshotai") == "all"
    assert _flat_reasoning_content_replay_mode("moonshotai-cn") == "all"
    # Base-URL fallback.
    assert _flat_reasoning_content_replay_mode(
        None, "https://api.fireworks.ai/inference/v1"
    ) == "all"
    assert _flat_reasoning_content_replay_mode(None, "https://api.moonshot.ai/v1") == "all"
    assert _flat_reasoning_content_replay_mode(None, "https://api.moonshot.cn/v1") == "all"


def test_fireworks_sets_reasoning_history_when_reasoning_enabled():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Parameters .* should be specified explicitly",
            category=UserWarning,
        )
        llm = create_llm(
            _fireworks_config(extended_thinking=True, reasoning_effort="high")
        )

    payload = llm._get_request_payload([HumanMessage(content="Hi")])
    assert payload.get("reasoning_history") == "preserved"
    assert payload.get("reasoning_effort") == "high"


def test_fireworks_no_reasoning_toggle_without_reasoning():
    llm = create_llm(_fireworks_config())

    payload = llm._get_request_payload([HumanMessage(content="Hi")])
    assert "reasoning_history" not in payload


def test_fireworks_replays_reasoning_content_unconditionally():
    llm = create_llm(_fireworks_config(extended_thinking=True))

    ai_no_tools = AIMessage(
        content="Answer",
        additional_kwargs={"reasoning_content": "prior thought"},
    )
    payload = llm._get_request_payload([
        HumanMessage(content="Q1"),
        ai_no_tools,
        HumanMessage(content="Q2"),
    ])

    assistant = payload["messages"][1]
    # "all" mode re-attaches even on a non-tool assistant turn.
    assert assistant["reasoning_content"] == "prior thought"


def test_moonshot_sets_thinking_keep_all_when_reasoning_enabled():
    llm = create_llm(_moonshot_config(extended_thinking=True))

    assert llm.extra_body == {"thinking": {"type": "enabled", "keep": "all"}}


def test_alibaba_replays_reasoning_content_on_tool_call_turn():
    llm = create_llm(_alibaba_config())

    ai_with_tools = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "Check the forecast."},
        tool_calls=[
            {
                "name": "get_weather",
                "args": {"city": "Paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Weather in Paris?"),
        ai_with_tools,
        ToolMessage(content="Sunny, 20C", tool_call_id="call_1"),
        HumanMessage(content="And London?"),
    ])

    assistant = payload["messages"][1]
    # Qwen3.5 leaks </think> into content if reasoning_content is dropped on a
    # tool-call turn (alibabacloud.com/help/en/model-studio/deep-thinking).
    assert assistant["reasoning_content"] == "Check the forecast."


def test_alibaba_strips_reasoning_content_on_non_tool_turn():
    llm = create_llm(_alibaba_config())

    ai_no_tools = AIMessage(
        content="It is sunny.",
        additional_kwargs={"reasoning_content": "Private chain of thought."},
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Weather in Paris?"),
        ai_no_tools,
        HumanMessage(content="Thanks"),
    ])

    # DashScope multi-round guidance: keep only `content`, drop reasoning.
    assert "reasoning_content" not in payload["messages"][1]


def test_alibaba_sets_enable_thinking_when_reasoning_enabled():
    llm = create_llm(_alibaba_config(extended_thinking=True))

    assert llm.extra_body == {"enable_thinking": True}


def test_alibaba_no_enable_thinking_without_reasoning():
    llm = create_llm(_alibaba_config())

    # The toggle is opt-in; plain chat must not force thinking on.
    assert not (llm.extra_body or {}).get("enable_thinking")


def test_baseten_replays_empty_reasoning_content_on_tool_turn_without_trace():
    """Baseten thinking-by-default models 400 if a tool-call turn omits
    reasoning_content; an empty string satisfies the constraint when no trace
    was captured (baseten.co/library/deepseek-v3-2)."""
    llm = create_llm(_baseten_config())

    ai_tool_no_trace = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Q"),
        ai_tool_no_trace,
        ToolMessage(content="R", tool_call_id="call_1"),
        HumanMessage(content="Q2"),
    ])

    assert payload["messages"][1]["reasoning_content"] == ""


def test_baseten_strips_reasoning_content_on_non_tool_turn():
    llm = create_llm(_baseten_config())

    ai_no_tools = AIMessage(
        content="Answer",
        additional_kwargs={"reasoning_content": "trace"},
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Q"),
        ai_no_tools,
        HumanMessage(content="Q2"),
    ])

    assert "reasoning_content" not in payload["messages"][1]


def test_baseten_applies_no_enable_toggle():
    # Enablement on Baseten is per-model (reasoning_effort vs chat_template_args),
    # so the client sets no blanket toggle: only the passback replay is wired.
    llm = create_llm(_baseten_config(extended_thinking=True))

    assert not (llm.extra_body or {}).get("enable_thinking")


def test_gateways_use_tool_calls_only_replay():
    # Multi-model gateways (LiteLLM, Together, Novita) normalize every backend
    # to a flat reasoning_content string. tool_calls_only is the never-400
    # default: it satisfies the DeepSeek/Qwen-backed models they proxy without
    # erroring the "all"-style backends (which only lose the plain-turn echo).
    for provider_id in ("litellm", "togetherai", "novita-ai"):
        assert _flat_reasoning_content_replay_mode(provider_id) == "tool_calls_only", (
            provider_id
        )


def test_together_captures_reasoning_field_as_reasoning_content():
    # Together returns reasoning under `reasoning`, not `reasoning_content`;
    # capture normalizes it so replay is uniform with DeepSeek/Novita.
    llm = create_llm(_together_config())

    chunk = {
        "choices": [
            {"delta": {"role": "assistant", "reasoning": "Compare 9.9 and 9.11."}}
        ]
    }

    generation_chunk = llm._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})

    assert (
        generation_chunk.message.additional_kwargs["reasoning_content"]
        == "Compare 9.9 and 9.11."
    )


def test_litellm_replays_reasoning_content_on_tool_call_turn():
    llm = create_llm(_litellm_config())

    ai_with_tools = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "Routing to the weather tool."},
        tool_calls=[
            {
                "name": "get_weather",
                "args": {"city": "Paris"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Weather in Paris?"),
        ai_with_tools,
        ToolMessage(content="Sunny, 20C", tool_call_id="call_1"),
        HumanMessage(content="And London?"),
    ])

    assistant = payload["messages"][1]
    assert assistant["reasoning_content"] == "Routing to the weather tool."


def test_novita_strips_reasoning_content_on_non_tool_turn():
    llm = create_llm(_novita_config())

    ai_no_tools = AIMessage(
        content="It is sunny.",
        additional_kwargs={"reasoning_content": "Private chain of thought."},
    )

    payload = llm._get_request_payload([
        HumanMessage(content="Weather in Paris?"),
        ai_no_tools,
        HumanMessage(content="Thanks"),
    ])

    # DeepSeek-backed models behind the gateway 400 if reasoning rides a plain turn.
    assert "reasoning_content" not in payload["messages"][1]


def test_gateways_send_their_documented_enable_toggles():
    # Each gateway's documented enable form goes on the wire when reasoning is
    # requested: LiteLLM takes the unified reasoning_effort, Together takes
    # reasoning.enabled, Novita takes enable_thinking.
    litellm_llm = create_llm(_litellm_config(extended_thinking=True))
    together_llm = create_llm(_together_config(extended_thinking=True))
    novita_llm = create_llm(_novita_config(extended_thinking=True))

    # langchain-openai promotes reasoning_effort from model_kwargs to the
    # explicit field.
    assert litellm_llm.reasoning_effort == "medium"
    assert (together_llm.extra_body or {}).get("reasoning") == {"enabled": True}
    assert (novita_llm.extra_body or {}).get("enable_thinking") is True


def test_nymeria_provider_threaded_and_absent_from_request_payload():
    llm = create_llm(_alibaba_config())

    assert llm.nymeria_provider == "alibaba"

    payload = llm._get_request_payload([HumanMessage(content="Hi")])
    # Internal bookkeeping must never reach the wire payload.
    assert "nymeria_provider" not in payload


def test_nvidia_and_vercel_advertise_responses_support():
    from nymeria.config.llm_providers import get_llm_provider_spec

    for provider_id in ("nvidia", "vercel"):
        spec = get_llm_provider_spec(provider_id)
        assert spec is not None, provider_id
        assert spec.supports_responses is True, provider_id
        assert spec.default_api_mode == "responses", provider_id

    # AIHubMix has no /responses; its passback is the chat reasoning_details path.
    aihubmix = get_llm_provider_spec("aihubmix")
    assert aihubmix is not None
    assert aihubmix.supports_responses is False


def test_notes_for_user_flows_through_catalog_response():
    """The catalog response surfaces tier and notes_for_user for unverified providers.

    Regression guard for the dossier-driven warning text in DeepSeek and xAI
    specs. If the registry value drifts or the schema/router projection drops
    the field, this test catches the break before users see stale picker chips.
    """
    from nymeria.api.schemas.settings import LLMProviderSpecResponse
    from nymeria.config.llm_providers import list_llm_provider_specs

    deepseek_spec = None
    for spec in list_llm_provider_specs():
        if spec.id == "deepseek":
            deepseek_spec = spec
            break
    assert deepseek_spec is not None
    assert deepseek_spec.tier == "unverified"
    assert deepseek_spec.notes_for_user, "deepseek should carry user-visible warning"

    response = LLMProviderSpecResponse(
        id=deepseek_spec.id,
        label=deepseek_spec.label,
        api_format=deepseek_spec.api_format,
        default_base_url=deepseek_spec.default_base_url,
        api_key_env_vars=list(deepseek_spec.api_key_env_vars),
        base_url_env_vars=list(deepseek_spec.base_url_env_vars),
        default_model=deepseek_spec.default_model,
        default_api_mode=deepseek_spec.default_api_mode,
        supports_chat_completions=deepseek_spec.supports_chat_completions,
        supports_responses=deepseek_spec.supports_responses,
        requires_api_key=deepseek_spec.requires_api_key,
        requires_base_url=deepseek_spec.requires_base_url,
        docs_url=deepseek_spec.docs_url,
        notes=deepseek_spec.notes,
        aliases=list(deepseek_spec.aliases),
        tier=deepseek_spec.tier,
        notes_for_user=deepseek_spec.notes_for_user,
        supported_routes=list(deepseek_spec.supported_routes),
        default_route=deepseek_spec.default_route,
        openai_compat_base_url=deepseek_spec.openai_compat_base_url,
        verified=deepseek_spec.verified,
    )

    assert response.tier == "unverified"
    assert response.notes_for_user == deepseek_spec.notes_for_user
    assert response.verified is False
    # JSON serialization includes the new fields.
    payload = response.model_dump()
    assert payload["tier"] == "unverified"
    assert payload["notes_for_user"] == deepseek_spec.notes_for_user
    assert payload["supported_routes"] == list(deepseek_spec.supported_routes)
    assert payload["default_route"] == deepseek_spec.default_route


def test_anthropic_native_for_claude_flag_scoped_to_signature_dropping_gateways():
    """LiteLLM's compat path drops Claude's signed thinking, so it opts into the
    picker's 'switch to Anthropic' hint. Gateways that round-trip the signature
    via reasoning_details (OpenRouter, Vercel, AIHubMix) must NOT set it, or the
    UI would warn on signature-safe providers."""
    from nymeria.api.schemas.settings import LLMProviderSpecResponse
    from nymeria.config.llm_providers import get_llm_provider_spec

    litellm = get_llm_provider_spec("litellm")
    assert litellm is not None
    assert litellm.anthropic_native_for_claude is True

    for provider_id in ("openrouter", "vercel", "aihubmix"):
        spec = get_llm_provider_spec(provider_id)
        assert spec is not None, provider_id
        assert spec.anthropic_native_for_claude is False, provider_id

    # The flag must survive the schema projection the catalog router uses.
    response = LLMProviderSpecResponse(
        id=litellm.id,
        label=litellm.label,
        api_format=litellm.api_format,
        anthropic_native_for_claude=litellm.anthropic_native_for_claude,
    )
    assert response.anthropic_native_for_claude is True
    # Defaults to False for providers that never set it.
    assert (
        LLMProviderSpecResponse(
            id="x", label="x", api_format="openai_chat"
        ).anthropic_native_for_claude
        is False
    )


# Bucket-A gateways: serve Claude, drop its signature on the OpenAI-compat path,
# expose a native /v1/messages endpoint. id -> confirmed Anthropic SDK base URL.
_ANTHROPIC_MESSAGES_GATEWAYS = {
    "litellm": None,  # proxy root serves both surfaces; reuse configured base
    "opencode": "https://opencode.ai/zen",
    "zenmux": "https://zenmux.ai/api/anthropic",
    "requesty": "https://router.requesty.ai",
    "fastrouter": "https://api.fastrouter.ai",
    "poe": "https://api.poe.com",
}


def test_anthropic_messages_route_advertised_by_bucket_a_gateways():
    from nymeria.config.llm_providers import get_llm_provider_spec

    for provider_id, base in _ANTHROPIC_MESSAGES_GATEWAYS.items():
        spec = get_llm_provider_spec(provider_id)
        assert spec is not None, provider_id
        assert "anthropic_messages" in spec.supported_routes, provider_id
        # Default stays openai_compat so existing behavior is unchanged; the
        # Anthropic route is an explicit per-thread opt-in.
        assert spec.default_route == "openai_compat", provider_id
        assert spec.anthropic_native_for_claude is True, provider_id
        assert spec.anthropic_messages_base_url == base, provider_id

    # Signature-safe gateways must NOT advertise the route (they round-trip
    # Claude reasoning via reasoning_details on the compat path already).
    for provider_id in ("openrouter", "vercel", "aihubmix"):
        spec = get_llm_provider_spec(provider_id)
        assert spec is not None, provider_id
        assert "anthropic_messages" not in spec.supported_routes, provider_id


def test_anthropic_messages_base_url_resolves_per_gateway():
    from nymeria.config.llm_providers import resolve_provider_base_url

    # The Anthropic endpoint differs from the OpenAI base for most gateways.
    assert (
        resolve_provider_base_url("opencode", provider_route="anthropic_messages")
        == "https://opencode.ai/zen"
    )
    assert (
        resolve_provider_base_url("zenmux", provider_route="anthropic_messages")
        == "https://zenmux.ai/api/anthropic"
    )
    # FastRouter's Anthropic endpoint is on a different host than its OpenAI base.
    assert (
        resolve_provider_base_url("fastrouter", provider_route="anthropic_messages")
        == "https://api.fastrouter.ai"
    )
    # The openai_compat route still resolves the OpenAI base.
    assert (
        resolve_provider_base_url("opencode", provider_route="openai_compat")
        == "https://opencode.ai/zen/v1"
    )


def test_anthropic_messages_route_dispatches_to_langchain_anthropic():
    from langchain_anthropic import ChatAnthropic

    # Hosted gateway: base URL resolves to the confirmed Anthropic endpoint root.
    llm = create_llm(
        LLMConfig(
            provider="opencode",
            model="claude-sonnet-4-5",
            api_key="test-key",
            base_url=None,
            temperature=None,
            provider_route="anthropic_messages",
        )
    )
    assert isinstance(llm, ChatAnthropic)
    assert not isinstance(llm, ChatOpenAIWithReasoning)
    assert "opencode.ai/zen" in str(llm.anthropic_api_url)

    # LiteLLM has requires_api_key=False: the no-key path must not raise and the
    # proxy root (configured base) is reused for the Anthropic surface.
    litellm = create_llm(
        LLMConfig(
            provider="litellm",
            model="claude-opus-4-1",
            api_key=None,
            base_url="http://localhost:4000",
            temperature=None,
            provider_route="anthropic_messages",
        )
    )
    assert isinstance(litellm, ChatAnthropic)
    assert "localhost:4000" in str(litellm.anthropic_api_url)


def test_gateway_openai_compat_route_still_uses_openai_adapter():
    # Default route is unchanged: same gateway without the route override stays
    # on the OpenAI-compatible reasoning adapter.
    llm = create_llm(
        LLMConfig(
            provider="opencode",
            model="claude-sonnet-4-5",
            api_key="test-key",
            base_url=None,
            temperature=None,
        )
    )
    assert isinstance(llm, ChatOpenAIWithReasoning)


# --- Responses stream per-chunk dispatch (slice 25 F3) --------------------
#
# The sync `_stream_responses` and async `_astream_responses` shells share one
# pure per-chunk transform (`_process_responses_stream_chunk`) plus a pure
# reasoning predicate (`_responses_chunk_indicates_reasoning`). These lock the
# helpers and the previously-untested sync/async shell equivalence.


def test_process_responses_stream_chunk_uses_openrouter_fallback():
    result = _process_responses_stream_chunk(
        {
            "type": "response.reasoning.delta",
            "delta": "Need context",
            "output_index": 0,
        },
        -1,
        -1,
        -1,
        is_openrouter=True,
        schema=None,
        metadata={},
        has_reasoning=False,
        output_version="responses/v1",
    )

    _, _, _, generation_chunk = result
    assert generation_chunk is not None
    assert generation_chunk.message.content == [
        {
            "type": "reasoning",
            "summary": [
                {"index": 0, "type": "summary_text", "text": "Need context"}
            ],
            "index": 0,
        }
    ]


def test_process_responses_stream_chunk_uses_compat_converter():
    result = _process_responses_stream_chunk(
        SimpleNamespace(
            type="response.output_text.delta",
            delta="Hello",
            output_index=0,
            content_index=0,
        ),
        -1,
        -1,
        -1,
        is_openrouter=False,
        schema=None,
        metadata={},
        has_reasoning=False,
        output_version="responses/v1",
    )

    _, _, _, generation_chunk = result
    assert generation_chunk is not None
    assert generation_chunk.text == "Hello"


def test_process_responses_stream_chunk_swallows_converter_error_for_openrouter(
    monkeypatch,
):
    # A non-reasoning event skips the OpenRouter fallback branch and reaches the
    # compat converter; for an OpenRouter base URL a converter error is
    # swallowed (returns no chunk, indices unchanged).
    def _boom(*args, **kwargs):
        raise KeyError("bad chunk")

    monkeypatch.setattr(
        providers, "_convert_responses_chunk_to_generation_chunk_compat", _boom
    )

    result = _process_responses_stream_chunk(
        SimpleNamespace(type="response.output_text.delta", delta="x"),
        3,
        4,
        5,
        is_openrouter=True,
        schema=None,
        metadata={},
        has_reasoning=False,
        output_version=None,
    )

    assert result == (3, 4, 5, None)


def test_process_responses_stream_chunk_reraises_converter_error_when_not_openrouter(
    monkeypatch,
):
    def _boom(*args, **kwargs):
        raise KeyError("bad chunk")

    monkeypatch.setattr(
        providers, "_convert_responses_chunk_to_generation_chunk_compat", _boom
    )

    with pytest.raises(KeyError):
        _process_responses_stream_chunk(
            SimpleNamespace(type="response.output_text.delta", delta="x"),
            -1,
            -1,
            -1,
            is_openrouter=False,
            schema=None,
            metadata={},
            has_reasoning=False,
            output_version=None,
        )


def test_responses_chunk_indicates_reasoning_via_additional_kwargs():
    generation_chunk = SimpleNamespace(
        message=SimpleNamespace(
            additional_kwargs={"reasoning": "thinking"}, content="hi"
        )
    )
    assert _responses_chunk_indicates_reasoning(generation_chunk) is True


def test_responses_chunk_indicates_reasoning_via_content_block():
    generation_chunk = SimpleNamespace(
        message=SimpleNamespace(
            additional_kwargs={},
            content=[{"type": "reasoning", "summary": []}],
        )
    )
    assert _responses_chunk_indicates_reasoning(generation_chunk) is True


def test_responses_chunk_indicates_reasoning_false_for_plain_text():
    list_content = SimpleNamespace(
        message=SimpleNamespace(
            additional_kwargs={}, content=[{"type": "text", "text": "hi"}]
        )
    )
    str_content = SimpleNamespace(
        message=SimpleNamespace(additional_kwargs={}, content="hi")
    )
    assert _responses_chunk_indicates_reasoning(list_content) is False
    assert _responses_chunk_indicates_reasoning(str_content) is False


def _fake_text_delta_chunk(text, output_index=0, content_index=0):
    return SimpleNamespace(
        type="response.output_text.delta",
        delta=text,
        output_index=output_index,
        content_index=content_index,
    )


class _SyncRecordingRunManager:
    def __init__(self):
        self.tokens: list[str] = []

    def on_llm_new_token(self, token, chunk=None):
        self.tokens.append(token)


class _AsyncRecordingRunManager:
    def __init__(self):
        self.tokens: list[str] = []

    async def on_llm_new_token(self, token, chunk=None):
        self.tokens.append(token)


class _FakeSyncResponseStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return iter(self._chunks)

    def __exit__(self, *exc):
        return False


class _FakeAsyncResponseStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()

    async def __aexit__(self, *exc):
        return False


def test_stream_responses_sync_and_async_yield_identical_sequences(monkeypatch):
    # End-to-end equivalence of the two shells over identical fake chunks. The
    # shells had no prior direct coverage; this guards against the sync/async
    # paths drifting after the shared-helper extraction.
    chunks = [_fake_text_delta_chunk("Hel"), _fake_text_delta_chunk("lo")]

    monkeypatch.setattr(
        ChatOpenAIWithReasoning,
        "_ensure_sync_client_available",
        lambda self: None,
    )
    monkeypatch.setattr(
        ChatOpenAIWithReasoning,
        "_get_request_payload",
        lambda self, messages, stop=None, **kwargs: {},
    )

    sync_llm = create_llm(_openai_config())
    assert isinstance(sync_llm, ChatOpenAIWithReasoning)
    object.__setattr__(
        sync_llm,
        "root_client",
        SimpleNamespace(
            responses=SimpleNamespace(
                create=lambda **kwargs: _FakeSyncResponseStream(chunks)
            )
        ),
    )
    sync_rm = _SyncRecordingRunManager()
    sync_chunks = list(sync_llm._stream_responses([], run_manager=sync_rm))

    async_llm = create_llm(_openai_config())
    assert isinstance(async_llm, ChatOpenAIWithReasoning)

    async def _async_create(**kwargs):
        return _FakeAsyncResponseStream(chunks)

    object.__setattr__(
        async_llm,
        "root_async_client",
        SimpleNamespace(responses=SimpleNamespace(create=_async_create)),
    )
    async_rm = _AsyncRecordingRunManager()

    async def _collect():
        out = []
        async for chunk in async_llm._astream_responses([], run_manager=async_rm):
            out.append(chunk)
        return out

    async_chunks = _run_in_new_event_loop(_collect)

    assert [chunk.text for chunk in sync_chunks] == ["Hel", "lo"]
    assert [chunk.text for chunk in async_chunks] == ["Hel", "lo"]
    assert [chunk.message.content for chunk in sync_chunks] == [
        chunk.message.content for chunk in async_chunks
    ]
    assert sync_rm.tokens == async_rm.tokens == ["Hel", "lo"]
