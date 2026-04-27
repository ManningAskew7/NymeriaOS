"""Tests for OpenAI Responses API mode provider wiring."""

from __future__ import annotations

import warnings

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import (
    _normalize_openai_base_url,
    _should_disable_streaming_for_local_base_url,
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
        _openrouter_config(extended_thinking=True, reasoning_effort="low")
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
        _openrouter_config(extended_thinking=True, reasoning_effort="low")
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
        _openrouter_config(extended_thinking=True, reasoning_effort="low")
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
