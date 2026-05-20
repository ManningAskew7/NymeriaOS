"""Tests for OpenAI Responses API mode provider wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import warnings

import anthropic
import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
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
    _normalize_openai_base_url,
    _normalize_openrouter_responses_payload,
    _should_disable_streaming_for_local_base_url,
    _wrap_cliproxy_context_management_event,
    create_llm,
    create_llm_with_tools,
)


def _openai_config(**overrides) -> LLMConfig:
    values = {
        "provider": "openai",
        "model": "gpt-5.5",
        "api_key": "test-key",
        "base_url": "http://example.test/v1",
        "temperature": None,
    }
    values.update(overrides)
    return LLMConfig(**values)


def _openrouter_config(**overrides) -> LLMConfig:
    values = {
        "provider": "openrouter",
        "model": "qwen/qwen3.6-flash",
        "api_key": "test-key",
        "base_url": "https://openrouter.ai/api/v1",
        "temperature": None,
    }
    values.update(overrides)
    return LLMConfig(**values)


def _anthropic_config(**overrides) -> LLMConfig:
    values = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "api_key": "test-key",
        "temperature": None,
    }
    values.update(overrides)
    return LLMConfig(**values)


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
