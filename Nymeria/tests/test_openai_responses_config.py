"""Tests for OpenAI Responses API mode provider wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import warnings

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from nymeria.vendor.react_agent import providers
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
    assert type(llm).__name__ == "NymeriaChatAnthropic"


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


def test_anthropic_async_http_client_is_instance_local_and_preserves_cliproxy_headers(
    monkeypatch,
):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    config = _anthropic_config(base_url="http://cli-proxy-api-latest:8317")
    first = create_llm(config)
    second = create_llm(config)

    first_async_client = first._async_client
    second_async_client = second._async_client

    try:
        assert first_async_client is not second_async_client
        assert first_async_client._client is not second_async_client._client
        assert (
            first_async_client.default_headers["User-Agent"]
            == "claude-cli/2.1.113"
        )
        assert str(first_async_client.base_url).rstrip("/") == (
            "http://cli-proxy-api-latest:8317"
        )
    finally:
        asyncio.run(first_async_client.close())
        asyncio.run(second_async_client.close())


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
