"""Regression tests for provider-token streaming through the ReAct graph."""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from nymeria.vendor.react_agent.config import (
    AgentConfig,
    CheckpointerConfig,
    LLMFallbackConfig,
    LLMConfig,
)
from nymeria.vendor.react_agent.graph import create_graph
from nymeria.vendor.react_agent import nodes as nodes_module
from nymeria.vendor.react_agent.nodes import create_tools_node


class _StreamingFakeModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "streaming-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="sync fallback"))]
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for part in ("he", "llo"):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))
            if run_manager:
                await run_manager.on_llm_new_token(part, chunk=chunk)
            yield chunk


class _TransientStreamError(RuntimeError):
    status_code = 500


class _ResponseBackedStreamError(RuntimeError):
    def __init__(self, message: str, response: httpx.Response):
        super().__init__(message)
        self.response = response


class _RetryableBeforeChunkModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "retryable-before-chunk-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="sync"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls <= 2:
            raise _TransientStreamError("server_error: temporary upstream failure")
        chunk = ChatGenerationChunk(message=AIMessageChunk(content="ok"))
        if run_manager:
            await run_manager.on_llm_new_token("ok", chunk=chunk)
        yield chunk


class _FailAfterChunkModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fail-after-chunk-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="sync"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        chunk = ChatGenerationChunk(message=AIMessageChunk(content="partial"))
        if run_manager:
            await run_manager.on_llm_new_token("partial", chunk=chunk)
        yield chunk
        raise _TransientStreamError("server_error after content")


class _NonRetryableBeforeChunkModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "non-retryable-before-chunk-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="sync"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if False:
            yield ChatGenerationChunk(message=AIMessageChunk(content=""))
        raise ValueError("context_length_exceeded: prompt is too long")


class _AlwaysFailBeforeChunkModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "always-fail-before-chunk-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        raise _TransientStreamError("server_error: primary failed")

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if False:
            yield ChatGenerationChunk(message=AIMessageChunk(content=""))
        raise _TransientStreamError("server_error: primary failed")


class _FallbackStreamingModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fallback-streaming-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="fallback"))]
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        chunk = ChatGenerationChunk(message=AIMessageChunk(content="fallback"))
        if run_manager:
            await run_manager.on_llm_new_token("fallback", chunk=chunk)
        yield chunk


async def _async_only_tool(value: str) -> str:
    return f"tool-ok:{value}"


class _MiniGraphState(TypedDict):
    messages: Annotated[list, add_messages]


class _AsyncToolCallingModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "async-tool-calling-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            message = AIMessage(
                content="using tool",
                tool_calls=[
                    {
                        "name": "async_only_tool",
                        "args": {"value": "x"},
                        "id": "call_1",
                    }
                ],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_async_graph_emits_chat_model_stream_chunks():
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(provider="custom", custom_llm=_StreamingFakeModel()),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect():
        stream_chunks = []
        end_outputs = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "stream-test"}},
            version="v2",
        ):
            if event.get("event") == "on_chat_model_stream":
                content = getattr(event["data"]["chunk"], "content", "")
                if content:
                    stream_chunks.append(content)
            elif event.get("event") == "on_chat_model_end":
                output = event["data"].get("output")
                end_outputs.append(getattr(output, "content", None))
        return stream_chunks, end_outputs

    stream_chunks, end_outputs = asyncio.run(collect())

    assert stream_chunks == ["he", "llo"]
    assert end_outputs[-1] == "hello"


def test_async_graph_retries_retryable_stream_failure_before_chunks():
    model = _RetryableBeforeChunkModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                custom_llm=model,
                stream_max_retries=2,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect():
        stream_chunks = []
        end_outputs = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "retry-before-chunk-test"}},
            version="v2",
        ):
            if event.get("event") == "on_chat_model_stream":
                content = getattr(event["data"]["chunk"], "content", "")
                if content:
                    stream_chunks.append(content)
            elif event.get("event") == "on_chat_model_end":
                output = event["data"].get("output")
                end_outputs.append(getattr(output, "content", None))
        return stream_chunks, end_outputs

    stream_chunks, end_outputs = asyncio.run(collect())

    assert model.calls == 3
    assert stream_chunks == ["ok"]
    assert end_outputs[-1] == "ok"


def test_async_graph_does_not_retry_stream_failure_after_chunk():
    model = _FailAfterChunkModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                custom_llm=model,
                stream_max_retries=2,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect_until_error():
        stream_chunks = []
        with pytest.raises(_TransientStreamError):
            async for event in graph.astream_events(
                {"messages": [HumanMessage(content="hi")]},
                config={"configurable": {"thread_id": "fail-after-chunk-test"}},
                version="v2",
            ):
                if event.get("event") == "on_chat_model_stream":
                    content = getattr(event["data"]["chunk"], "content", "")
                    if content:
                        stream_chunks.append(content)
        return stream_chunks

    stream_chunks = asyncio.run(collect_until_error())

    assert model.calls == 1
    assert stream_chunks == ["partial"]


def test_async_graph_does_not_retry_non_retryable_stream_error():
    model = _NonRetryableBeforeChunkModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                custom_llm=model,
                stream_max_retries=2,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect_until_error():
        with pytest.raises(ValueError):
            async for _ in graph.astream_events(
                {"messages": [HumanMessage(content="hi")]},
                config={"configurable": {"thread_id": "non-retryable-test"}},
                version="v2",
            ):
                pass

    asyncio.run(collect_until_error())

    assert model.calls == 1


def test_llm_error_classification_ignores_unread_stream_response_body():
    response = httpx.Response(
        500,
        stream=httpx.ByteStream(b"context_length_exceeded"),
    )
    exc = _ResponseBackedStreamError("Internal Server Error", response)

    assert nodes_module.is_context_overflow_error(exc) is False
    assert nodes_module._is_retryable_llm_error(exc) is True


def _anthropic_stream_error(status: int, error_type: str, message: str = "boom"):
    """A real ``anthropic.APIStatusError`` as the SDK raises it mid-stream.

    Anthropic reports a failure that happens after the stream opened as an
    SSE ``error`` event on the HTTP 200 the stream started with, and the SDK
    maps by response status, so a 200 falls to the bare ``APIStatusError``
    with ``status_code == 200`` (backlog #315). The response needs a request
    attached or the constructor raises.
    """
    import anthropic

    body = {"type": "error", "error": {"type": error_type, "message": message}}
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json=body,
    )
    return anthropic.Anthropic(api_key="test")._make_status_error(
        f"{body['error']}", body=body, response=response
    )


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        # The #315 shape: overloaded mid-stream, reported on the 200.
        (200, "overloaded_error", True),
        # A 2xx is no evidence either way; the marker precedence still holds.
        (200, "invalid_request_error", False),
        # HTTP-level shapes are unchanged by the fix.
        (529, "overloaded_error", True),
        (400, "invalid_request_error", False),
    ],
)
def test_llm_error_classification_reads_markers_behind_a_2xx_status(
    status, error_type, retryable
):
    exc = _anthropic_stream_error(status, error_type)
    assert exc.status_code == status
    assert nodes_module._is_retryable_llm_error(exc) is retryable


class _OverloadedOn200BeforeChunkModel(BaseChatModel):
    """First call dies the way a live overloaded stream does; second streams."""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "overloaded-on-200-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="sync"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise _anthropic_stream_error(200, "overloaded_error", "Overloaded")
        chunk = ChatGenerationChunk(message=AIMessageChunk(content="ok"))
        if run_manager:
            await run_manager.on_llm_new_token("ok", chunk=chunk)
        yield chunk


def test_async_graph_retries_overloaded_error_streamed_on_a_200():
    """A mid-stream overloaded_error on a 200 is retried, not raised (#315)."""
    model = _OverloadedOn200BeforeChunkModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                custom_llm=model,
                stream_max_retries=1,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect():
        end_outputs = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "overloaded-on-200-test"}},
            version="v2",
        ):
            if event.get("event") == "on_chat_model_end":
                output = event["data"].get("output")
                end_outputs.append(getattr(output, "content", None))
        return end_outputs

    end_outputs = asyncio.run(collect())

    assert model.calls == 2
    assert end_outputs[-1] == "ok"


def test_async_graph_uses_configured_fallback_after_primary_failure(monkeypatch):
    primary = _AlwaysFailBeforeChunkModel()
    fallback = _FallbackStreamingModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                custom_llm=primary,
                stream_max_retries=0,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
                fallbacks=[
                    LLMFallbackConfig(
                        provider="custom",
                        model="fallback-model",
                    )
                ],
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    created_models = []

    def fake_create_llm_with_tools(config, tools):
        created_models.append(config.model)
        return fallback

    monkeypatch.setattr(
        nodes_module,
        "create_llm_with_tools",
        fake_create_llm_with_tools,
    )

    async def collect():
        stream_chunks = []
        end_outputs = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "fallback-test"}},
            version="v2",
        ):
            if event.get("event") == "on_chat_model_stream":
                content = getattr(event["data"]["chunk"], "content", "")
                if content:
                    stream_chunks.append(content)
            elif event.get("event") == "on_chat_model_end":
                output = event["data"].get("output")
                end_outputs.append(getattr(output, "content", None))
        return stream_chunks, end_outputs

    stream_chunks, end_outputs = asyncio.run(collect())

    assert primary.calls == 1
    assert fallback.calls == 1
    assert created_models == ["fallback-model"]
    assert stream_chunks == ["fallback"]
    assert end_outputs[-1] == "fallback"


def test_async_graph_exhausts_primary_retries_before_fallback(monkeypatch):
    primary = _AlwaysFailBeforeChunkModel()
    fallback = _FallbackStreamingModel()
    activations = []
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                model="primary-model",
                custom_llm=primary,
                stream_max_retries=2,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
                fallback_activation_callback=lambda payload: activations.append(payload)
                or {
                    "hold_seconds": 7200,
                    "expires_at": "2026-05-22T12:00:00+00:00",
                },
                fallbacks=[
                    LLMFallbackConfig(
                        provider="custom",
                        model="fallback-model",
                    )
                ],
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    def fake_create_llm_with_tools(config, tools):
        return fallback

    monkeypatch.setattr(
        nodes_module,
        "create_llm_with_tools",
        fake_create_llm_with_tools,
    )

    async def collect():
        custom_events = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "fallback-after-retries-test"}},
            version="v2",
        ):
            if event.get("event") == "on_custom_event":
                custom_events.append((event.get("name"), event.get("data")))
        return custom_events

    custom_events = asyncio.run(collect())
    retry_events = [data for name, data in custom_events if name == "provider_retry"]
    fallback_events = [
        data for name, data in custom_events if name == "provider_fallback"
    ]

    assert primary.calls == 3
    assert fallback.calls == 1
    assert [event["attempt"] for event in retry_events] == [1, 2]
    assert retry_events[0]["provider"] == "custom"
    assert retry_events[0]["model"] == "primary-model"
    assert len(fallback_events) == 1
    assert fallback_events[0]["to_model"] == "fallback-model"
    assert fallback_events[0]["hold_seconds"] == 7200
    assert activations[0]["from_model"] == "primary-model"
    assert activations[0]["to_model"] == "fallback-model"

    custom_events = asyncio.run(collect())
    assert primary.calls == 3
    assert fallback.calls == 2
    assert [name for name, _data in custom_events if name == "provider_retry"] == []
    assert [name for name, _data in custom_events if name == "provider_fallback"] == []


def test_async_graph_invokes_async_only_tools():
    async_tool = StructuredTool.from_function(
        coroutine=_async_only_tool,
        name="async_only_tool",
        description="Async-only test tool.",
    )
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                custom_llm=_AsyncToolCallingModel(),
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[async_tool],
    )

    async def collect_tool_outputs():
        outputs = []
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="use the tool")]},
            config={"configurable": {"thread_id": "async-tool-test"}},
            version="v2",
        ):
            if event.get("event") == "on_tool_end":
                outputs.append(event["data"]["output"].content)
        return outputs

    assert asyncio.run(collect_tool_outputs()) == ["tool-ok:x"]


def test_tools_node_truncates_large_tool_output_and_preserves_call_id():
    payload = "a" * 75_000 + "b" * 75_000

    def long_tool() -> str:
        """Return a large payload."""
        return payload

    result = _invoke_single_tool_graph(
        tool=StructuredTool.from_function(long_tool, name="long_tool"),
        tool_name="long_tool",
        call_id="call-1",
        tool_output_max_chars=100_000,
    )

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-1"
    assert "Tool output truncated" in message.content
    assert "original 150000 chars" in message.content
    assert "omitted 50000 chars" in message.content
    assert message.content.startswith("a" * 100)
    assert message.content.endswith("b" * 100)


def test_tools_node_leaves_under_limit_tool_output_unchanged():
    payload = "z" * 99_999

    def short_tool() -> str:
        """Return a payload under the truncation limit."""
        return payload

    result = _invoke_single_tool_graph(
        tool=StructuredTool.from_function(short_tool, name="short_tool"),
        tool_name="short_tool",
        call_id="call-2",
        tool_output_max_chars=100_000,
    )

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-2"
    assert message.content == payload


# ---------------------------------------------------------------------------
# Image strip-and-retry (Phase 2)
# ---------------------------------------------------------------------------


class _StatusError(RuntimeError):
    """RuntimeError carrying an optional HTTP status_code, like provider SDKs."""

    def __init__(self, message: str, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def test_is_image_unsupported_error_matches_capability_rejections():
    matches = [
        "Invalid content type. image_url is only supported by certain models.",
        "This model does not support image input.",
        "The model does not support vision.",
        "does not support the requested modality: image",
        "Fine-tuned model does not support image message content types",
        "PDF input is not supported for this model.",
    ]
    for msg in matches:
        assert nodes_module.is_image_unsupported_error(_StatusError(msg, 400)) is True, msg
    # 422 is also allowed; unknown status (None) still matches on a strong phrase.
    assert nodes_module.is_image_unsupported_error(_StatusError("does not support image input", 422)) is True
    assert nodes_module.is_image_unsupported_error(_StatusError("does not support image input")) is True


def test_is_image_unsupported_error_excludes_fixable_and_wrong_status():
    fixable = [
        "Could not process image",
        "Image does not match the provided media type image/jpeg",
        "A maximum of 100 PDF pages may be provided.",
        "Failed to download file from https://x/y.png",
        "text content blocks must be non-empty",
        "You uploaded an unsupported image.",
        "Invalid value: 'image'. Supported values are: 'text', 'image_url'.",
    ]
    for msg in fixable:
        assert nodes_module.is_image_unsupported_error(_StatusError(msg, 400)) is False, msg
    # Anthropic's generic capability template without a modality term must not match.
    assert (
        nodes_module.is_image_unsupported_error(
            _StatusError("Prefilling assistant messages is not supported for this model.", 400)
        )
        is False
    )
    # Right phrasing, but a non-capability status: never strip.
    for status in (401, 402, 403, 408, 429, 500, 503):
        assert (
            nodes_module.is_image_unsupported_error(_StatusError("does not support image input", status))
            is False
        ), status


def test_is_image_unsupported_error_reads_gateway_nested_text():
    # LiteLLM/OpenRouter wrap the upstream message; the flattened text still matches.
    exc = _StatusError(
        "litellm.BadRequestError: OpenAIException - Invalid content type. "
        "image_url is only supported by certain models.",
        400,
    )
    assert nodes_module.is_image_unsupported_error(exc) is True


def test_is_image_unsupported_error_template_uses_word_boundaries():
    # The generic "not supported for this model" template only counts with a
    # modality WORD, matched with word boundaries. An unrelated capability 400
    # whose text merely contains a bound tool name (`file_read`), `profile`, or
    # `documentation` must NOT be read as an image rejection (which would strip a
    # valid attachment AND mark the model non-vision).
    false_positives = [
        "tools.0.function.name 'file_read': strict function calling is not supported for this model.",
        "Updating your profile is not supported for this model.",
        "Streaming documentation lookups is not supported for this model.",
    ]
    for msg in false_positives:
        assert nodes_module.is_image_unsupported_error(_StatusError(msg, 400)) is False, msg
    # A genuine modality word still matches through the template branch.
    for msg in [
        "Image input is not supported for this model.",
        "Documents are not supported for this model.",
        "Multimodal input is not supported for this model.",
    ]:
        assert nodes_module.is_image_unsupported_error(_StatusError(msg, 400)) is True, msg


class _ImageUnsupportedError(RuntimeError):
    status_code = 400


class _ImageUnsupportedThenOkModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "image-unsupported-then-ok-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="sync"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise _ImageUnsupportedError(
                "Invalid content type. image_url is only supported by certain models."
            )
        chunk = ChatGenerationChunk(message=AIMessageChunk(content="ok"))
        if run_manager:
            await run_manager.on_llm_new_token("ok", chunk=chunk)
        yield chunk


class _ImageProcessingFailModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "image-processing-fail-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="sync"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if False:
            yield ChatGenerationChunk(message=AIMessageChunk(content=""))
        # An EXCLUDED 400 (fixable, not a capability rejection): must NOT strip-retry.
        raise _ImageUnsupportedError("Could not process image")


def _image_human_message() -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    )


def test_async_graph_strips_image_and_retries_on_capability_rejection(monkeypatch):
    # Keep the outbound image intact on attempt 1 (the provider-specific image
    # window's stripping is tested separately) so the capability rejection can
    # trigger exactly one strip-and-retry.
    monkeypatch.setattr(
        nodes_module,
        "_window_images_for_llm",
        lambda messages, llm_config, thread_id=None: messages,
    )
    from nymeria.config import model_capabilities as capabilities

    monkeypatch.setattr(capabilities, "_live_model_cache", {})
    monkeypatch.setattr(capabilities, "_model_cache", {})

    model = _ImageUnsupportedThenOkModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                model="text-only-fake",
                custom_llm=model,
                stream_max_retries=0,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect():
        events = []
        chunks = []
        async for event in graph.astream_events(
            {"messages": [_image_human_message()]},
            config={"configurable": {"thread_id": "img-strip-test"}},
            version="v2",
        ):
            if event.get("event") == "on_custom_event":
                events.append(event.get("name"))
            elif event.get("event") == "on_chat_model_stream":
                content = getattr(event["data"]["chunk"], "content", "")
                if content:
                    chunks.append(content)
        return events, chunks

    events, chunks = asyncio.run(collect())

    assert model.calls == 2  # one strip-retry
    assert "image_input_unsupported" in events
    assert chunks == ["ok"]
    # The model was learned as image/file-incapable for subsequent turns.
    info = capabilities.get_model_info("text-only-fake")
    assert info is not None
    assert "image" not in info.input_modalities
    assert "file" not in info.input_modalities


def test_async_graph_does_not_strip_on_fixable_image_400(monkeypatch):
    monkeypatch.setattr(
        nodes_module,
        "_window_images_for_llm",
        lambda messages, llm_config, thread_id=None: messages,
    )
    model = _ImageProcessingFailModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                model="text-only-fake",
                custom_llm=model,
                stream_max_retries=0,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    async def collect_until_error():
        events = []
        with pytest.raises(_ImageUnsupportedError):
            async for event in graph.astream_events(
                {"messages": [_image_human_message()]},
                config={"configurable": {"thread_id": "img-nostrip-test"}},
                version="v2",
            ):
                if event.get("event") == "on_custom_event":
                    events.append(event.get("name"))
        return events

    events = asyncio.run(collect_until_error())

    assert model.calls == 1  # no strip-retry on a fixable 400
    assert "image_input_unsupported" not in events


class _ImageUnsupportedThenOkSyncModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "image-unsupported-then-ok-sync-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise _ImageUnsupportedError("This model does not support image input.")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok-sync"))])


def test_sync_graph_strips_image_and_retries_on_capability_rejection(monkeypatch):
    # The sync agent_node (graph.invoke path) shares the one-shot strip-retry.
    monkeypatch.setattr(
        nodes_module,
        "_window_images_for_llm",
        lambda messages, llm_config, thread_id=None: messages,
    )
    from nymeria.config import model_capabilities as capabilities

    monkeypatch.setattr(capabilities, "_live_model_cache", {})
    monkeypatch.setattr(capabilities, "_model_cache", {})

    model = _ImageUnsupportedThenOkSyncModel()
    graph = create_graph(
        config=AgentConfig(
            llm=LLMConfig(
                provider="custom",
                model="text-only-sync-fake",
                custom_llm=model,
                stream_max_retries=0,
                stream_retry_initial_delay=0.0,
                stream_retry_max_delay=0.0,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[],
    )

    result = graph.invoke(
        {"messages": [_image_human_message()]},
        config={"configurable": {"thread_id": "img-strip-sync-test"}},
    )

    assert model.calls == 2  # one strip-retry
    assert result["messages"][-1].content == "ok-sync"
    info = capabilities.get_model_info("text-only-sync-fake")
    assert info is not None
    assert "image" not in info.input_modalities
    assert "file" not in info.input_modalities


def _invoke_single_tool_graph(*, tool, tool_name: str, call_id: str, tool_output_max_chars: int):
    calls = {"agent": 0}

    def agent_node(state):
        calls["agent"] += 1
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": tool_name,
                            "args": {},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def agent_router(state):
        return "tools" if calls["agent"] == 1 else "end"

    graph = StateGraph(_MiniGraphState)
    graph.add_node("agent", agent_node)
    graph.add_node(
        "tools",
        create_tools_node([tool], tool_output_max_chars=tool_output_max_chars),
    )
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", agent_router, {"tools": "tools", "end": END})
    graph.add_edge("tools", END)
    return graph.compile().invoke({"messages": []})
