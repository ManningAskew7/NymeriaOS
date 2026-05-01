"""Regression tests for provider-token streaming through the ReAct graph."""

from __future__ import annotations

import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool

from nymeria.vendor.react_agent.config import (
    AgentConfig,
    CheckpointerConfig,
    LLMConfig,
)
from nymeria.vendor.react_agent.graph import create_graph


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


async def _async_only_tool(value: str) -> str:
    return f"tool-ok:{value}"


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
