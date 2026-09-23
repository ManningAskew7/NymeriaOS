"""Backlog #22: the queued-prompt halt boundary leaves a well-formed history.

A prompt that arrives mid-turn is absorbed at the ONLY halt point,
``route_after_tools``, which runs after the tools node finished a batch. So
the history tail at halt is always a complete ``AIMessage(tool_calls)`` plus
every one of its ``ToolMessage`` results, and the queued prompt is APPENDED
after that as a fresh ``HumanMessage``; no truncated assistant text and no
dangling tool call can reach the model through this path. This drives the
real compiled graph (scripted model, real tools node, real router, memory
checkpointer) and the real absorb helpers, so a refactor that moved the halt
or turned the append into an insert goes red here.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from nymeria.core.agent_callable_lifecycle import (
    DANGLING_MARKER_REPEATED,
    patch_dangling_tool_calls,
)
from nymeria.core.agent_turn_loops import build_queued_prompt_messages
from nymeria.core.pending_prompt_queue import (
    create_pending_queue,
    get_pending_queue,
    make_pending_prompt,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.vendor.react_agent.config import AgentConfig, CheckpointerConfig, LLMConfig
from nymeria.vendor.react_agent.graph import create_graph

THREAD_ID = "halt-boundary-thread"
QUEUED_TEXT = "follow up that arrived mid-batch"

# What the scripted model was handed on each call, in order.
_MODEL_INPUTS: list[list[Any]] = []
# Whether the echo tool lets a prompt arrive during its batch (the control
# test turns it off; a module flag, since the tool is a module-level object).
_ARRIVAL = {"enabled": True}


@pytest.fixture
def isolated_queue():
    backend = create_pending_queue(None)
    set_pending_queue(backend)
    _MODEL_INPUTS.clear()
    _ARRIVAL["enabled"] = True
    try:
        yield backend
    finally:
        reset_pending_queue_for_tests()
        _MODEL_INPUTS.clear()
        _ARRIVAL["enabled"] = True


class _TwoCallsThenDone(BaseChatModel):
    """Call 1 asks for two tool calls; every later call answers ``done``."""

    @property
    def _llm_type(self) -> str:
        return "halt-boundary-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _MODEL_INPUTS.append(list(messages))
        if len(_MODEL_INPUTS) == 1:
            message = AIMessage(
                content="looking twice",
                tool_calls=[
                    {"name": "echo", "args": {"text": "first"}, "id": "call_1"},
                    {"name": "echo", "args": {"text": "second"}, "id": "call_2"},
                ],
            )
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool
def echo(text: str) -> str:
    """Echo the text; the first call also lets a prompt arrive mid-batch."""
    backend = get_pending_queue()
    if _ARRIVAL["enabled"] and backend.size(THREAD_ID) == 0:
        backend.enqueue(
            THREAD_ID,
            make_pending_prompt(
                message=QUEUED_TEXT,
                source="user",
                source_id=None,
                source_label="user-1",
                user_id="user-1",
                is_autonomous=False,
                fanout_mailbox=None,
                consumer_loop=None,
            ),
        )
    return f"echo:{text}"


def _graph():
    return create_graph(
        config=AgentConfig(
            llm=LLMConfig(provider="custom", custom_llm=_TwoCallsThenDone()),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt="test system",
        ),
        tools=[echo],
    )


def _tool_call_ids(message: AIMessage) -> list[str]:
    return [call["id"] for call in message.tool_calls]


def _assert_batch_complete(messages: list[Any], ai_index: int) -> int:
    """Every tool call on ``messages[ai_index]`` has its ToolMessage right
    after it, in order, with nothing else in between. Returns the index of
    the last ToolMessage of the batch."""
    ai = messages[ai_index]
    assert isinstance(ai, AIMessage) and ai.tool_calls
    ids = _tool_call_ids(ai)
    results = messages[ai_index + 1 : ai_index + 1 + len(ids)]
    assert [type(m) for m in results] == [ToolMessage] * len(ids)
    assert [m.tool_call_id for m in results] == ids
    return ai_index + len(ids)


def test_halt_lands_after_the_complete_batch_and_the_queued_prompt_is_appended(
    isolated_queue,
):
    graph = _graph()
    config = {"configurable": {"thread_id": THREAD_ID, "user_id": "user-1"}}

    # 1. A prompt arrives while the batch executes: the graph halts right
    #    after the tools node, before the model is asked to continue.
    result = graph.invoke({"messages": [HumanMessage(content="hi")]}, config=config)
    messages = result["messages"]

    assert len(_MODEL_INPUTS) == 1, "the halt must land before the next model call"
    assert isolated_queue.consume_halt_observation(THREAD_ID) == 1
    ai_index = next(i for i, m in enumerate(messages) if isinstance(m, AIMessage))
    last_tool_index = _assert_batch_complete(messages, ai_index)
    assert last_tool_index == len(messages) - 1, (
        "at halt the tail is the batch itself: AIMessage(tool_calls) plus every "
        "ToolMessage, and nothing after"
    )
    assert messages[ai_index].content == "looking twice"  # never truncated

    # 2. The pre-inject patch the drain loop runs finds nothing dangling.
    assert patch_dangling_tool_calls(None, graph, config, marker=DANGLING_MARKER_REPEATED) == 0

    # 3. Absorb exactly the way the drain loops do: build, then update_state.
    pending_batch = isolated_queue.drain(THREAD_ID)
    assert [p.message for p in pending_batch] == [QUEUED_TEXT]
    injected = build_queued_prompt_messages(pending_batch)
    assert len(injected) == 1 and isinstance(injected[0], HumanMessage)
    graph.update_state(config, {"messages": injected})

    after = graph.get_state(config).values["messages"]
    assert after[: len(messages)] == messages, "injection never rewrites the batch"
    assert after[len(messages) :] == injected, "the queued prompt is appended, not inserted"
    assert QUEUED_TEXT in after[-1].content
    assert after[-1].additional_kwargs["queued_batch"]["position"] == 1

    # 4. The re-drive hands the model exactly that sequence and completes.
    final = graph.invoke({"messages": []}, config=config)
    assert len(_MODEL_INPUTS) == 2
    redrive_input = _MODEL_INPUTS[1]
    redrive_ai_index = next(
        i for i, m in enumerate(redrive_input) if isinstance(m, AIMessage)
    )
    redrive_last_tool = _assert_batch_complete(redrive_input, redrive_ai_index)
    assert isinstance(redrive_input[redrive_last_tool + 1], HumanMessage)
    assert QUEUED_TEXT in redrive_input[redrive_last_tool + 1].content
    assert redrive_input[-1] is redrive_input[redrive_last_tool + 1]
    assert isinstance(final["messages"][-1], AIMessage)
    assert final["messages"][-1].content == "done"
    assert not final["messages"][-1].tool_calls


def test_no_queued_prompt_means_no_halt(isolated_queue):
    """Control: with nothing queued the same batch continues straight into the
    next model call, so the halt above is the queue's doing, not the graph's."""
    _ARRIVAL["enabled"] = False
    graph = _graph()
    config = {"configurable": {"thread_id": THREAD_ID, "user_id": "user-1"}}

    result = graph.invoke({"messages": [HumanMessage(content="hi")]}, config=config)

    assert len(_MODEL_INPUTS) == 2
    assert result["messages"][-1].content == "done"
    assert isolated_queue.consume_halt_observation(THREAD_ID) == 0
