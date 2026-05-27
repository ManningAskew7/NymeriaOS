"""Regression tests for per-turn tool reload bookkeeping."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command

from nymeria.core.tool_reload import (
    TOOL_RELOAD_QUEUED_KEY,
    tool_reload_command,
)
from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.event_bus import agent_stream_chunk_to_autonomous_event_data
from nymeria.vendor.react_agent.nodes import create_tools_node, route_after_tools


def _bare_agent() -> NymeriaAgent:
    agent = object.__new__(NymeriaAgent)
    agent._pending_tool_reload = {}
    agent._turn_reload_count = {}
    agent._subturn_compact_requested = set()
    agent._compactions_this_turn = {}
    return agent


def test_prepare_turn_discards_stale_pending_reload_for_same_thread():
    agent = _bare_agent()
    agent._pending_tool_reload = {
        "thread-a": {"new_tools": ["bash_execute"]},
        "thread-b": {"new_tools": ["calendar_list_events"]},
    }
    agent._turn_reload_count = {"thread-a": 2, "thread-b": 1}

    agent._prepare_tool_reload_state_for_turn("thread-a", "astream")

    assert "thread-a" not in agent._pending_tool_reload
    assert agent._pending_tool_reload == {
        "thread-b": {"new_tools": ["calendar_list_events"]}
    }
    assert agent._turn_reload_count["thread-a"] == 0
    assert agent._turn_reload_count["thread-b"] == 1


def test_tool_reload_resume_message_preserves_ttl_seconds():
    agent = _bare_agent()

    msg = agent._create_tool_reload_resume_message({
        "new_tools": ["hello_test"],
        "ttl": "2h",
        "ttl_seconds": 7200,
        "source": "skill_kit",
        "skill_name": "hello-kit",
        "reason": "required by skill",
    })

    assert msg.additional_kwargs["tool_reload_ttl_seconds"] == 7200


def test_history_tool_reload_info_includes_ttl_seconds():
    agent = _bare_agent()
    resume_msg = agent._create_tool_reload_resume_message({
        "new_tools": ["hello_test"],
        "ttl": "2h",
        "ttl_seconds": 7200,
        "source": "skill_kit",
        "skill_name": "hello-kit",
    })
    response_msg = AIMessage(content="Done")

    class _Graph:
        def get_state(self, config):
            return SimpleNamespace(values={"messages": [resume_msg, response_msg]})

        def get_state_history(self, config, limit=None):
            return []

    agent._default_graph = _Graph()

    history = agent.get_conversation_history("thread-a")

    assert history[0]["tool_reload_info"]["ttl_seconds"] == 7200


def test_autonomous_forwarding_helper_forwards_tool_reload_and_strips_only_type():
    converted = agent_stream_chunk_to_autonomous_event_data({
        "type": "tool_reload",
        "tools": ["hello_test"],
        "ttl": "2h",
        "ttl_seconds": 7200,
        "source": "skill_kit",
        "skill_name": "hello-kit",
        "reason": "required",
    })

    assert converted == (
        "tool_reload",
        {
            "tools": ["hello_test"],
            "ttl": "2h",
            "ttl_seconds": 7200,
            "source": "skill_kit",
            "skill_name": "hello-kit",
            "reason": "required",
        },
    )


def test_tool_reload_command_marks_tool_result_and_routes_to_end():
    command = tool_reload_command("Skill Kit reload queued", "call-1")
    message = command.update["messages"][0]

    assert message.additional_kwargs[TOOL_RELOAD_QUEUED_KEY] is True
    assert route_after_tools({
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "Skill",
                        "args": {"name": "hello-kit"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            message,
        ]
    }) == "end"


class _MiniGraphState(TypedDict):
    messages: Annotated[list, add_messages]


def test_reload_tool_result_ends_graph_before_agent_continues():
    calls = {"agent": 0}

    @tool
    def queue_reload() -> Command:
        """Queue a tool reload."""
        return tool_reload_command("reload queued", "call-1")

    def agent_node(state):
        calls["agent"] += 1
        if calls["agent"] == 1:
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "queue_reload",
                                "args": {},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }
        return {"messages": [AIMessage(content="continued without reload")]}

    def agent_router(state):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "end"

    graph = StateGraph(_MiniGraphState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", create_tools_node([queue_reload]))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", agent_router, {"tools": "tools", "end": END})
    graph.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "end": END},
    )

    result = graph.compile().invoke({"messages": []})

    assert calls["agent"] == 1
    assert isinstance(result["messages"][-1], ToolMessage)
    assert result["messages"][-1].additional_kwargs[TOOL_RELOAD_QUEUED_KEY] is True


class _FakeLockManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.abort_event = threading.Event()

    def get_lock(self, thread_id):
        return self.lock

    def get_lock_info(self, thread_id):
        return None

    def set_lock_info(self, thread_id, holder):
        return None

    def clear_lock_info(self, thread_id):
        return None

    def get_abort_event(self, thread_id):
        return self.abort_event


class _FakeAsyncGraph:
    def __init__(self, events):
        self.events = events
        self.stream_inputs = []

    async def astream_events(self, input_state, config=None, version=None):
        self.stream_inputs.append((input_state, config, version))
        async for event in self.events():
            yield event

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})

    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})


def test_astream_reload_resume_streams_post_reload_tool_events():
    agent = _bare_agent()
    # Memory-init seeding is out of scope here; mark the thread already seeded
    # so astream's fresh-thread seed step is a no-op for this reload test.
    agent._memory_seeded_threads = {"thread-a"}
    agent._thread_locks = _FakeLockManager()
    agent._compaction = CompactionManager(agent)
    agent.scheduler = SimpleNamespace(cancel=lambda *args, **kwargs: None)
    agent.settings = SimpleNamespace(lock_timeout=1, context_management="none")
    agent._token_tracker = SimpleNamespace(record_usage=lambda *args, **kwargs: None)
    agent._get_time_context = lambda **kwargs: "[time]"
    agent._patch_dangling_tool_calls = lambda *args, **kwargs: 0
    agent.get_pending_summary = lambda thread_id: None
    agent._graph_run_config = lambda thread_id, user_id: {"configurable": {"thread_id": thread_id, "user_id": user_id}}
    agent._clean_tool_result_for_display = lambda result: result
    agent._tool_result_extra_events = lambda *args, **kwargs: []
    agent._index_conversation_turn = lambda **kwargs: None
    agent.invalidate_thread_config_cache = lambda thread_id: None
    agent._extract_tokens_from_response = lambda messages: (0, 0)
    agent._max_iterations_for_thread = lambda thread_id: 20
    agent._analyze_turn_safety = lambda messages, max_iterations: SimpleNamespace(should_stop=False)
    agent._get_llm_config_for_thread = lambda thread_id: None

    async def initial_events():
        yield {
            "event": "on_tool_start",
            "run_id": "call-1",
            "name": "Skill",
            "data": {"input": {"name": "hello-kit"}},
        }
        yield {
            "event": "on_tool_end",
            "run_id": "call-1",
            "name": "Skill",
            "data": {
                "output": ToolMessage(
                    content="Skill Kit reload queued",
                    tool_call_id="call-1",
                    name="Skill",
                )
            },
        }
        agent._pending_tool_reload["thread-a"] = {
            "new_tools": ["hello_test"],
            "ttl": "2h",
            "ttl_seconds": 7200,
            "source": "skill_kit",
            "skill_name": "hello-kit",
        }

    async def reload_events():
        yield {
            "event": "on_tool_start",
            "run_id": "call-2",
            "name": "hello_test",
            "data": {"input": {"subject": "world"}},
        }
        yield {
            "event": "on_tool_end",
            "run_id": "call-2",
            "name": "hello_test",
            "data": {
                "output": ToolMessage(
                    content="hello world",
                    tool_call_id="call-2",
                    name="hello_test",
                )
            },
        }
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="Done")},
        }

    initial_graph = _FakeAsyncGraph(initial_events)
    reload_graph = _FakeAsyncGraph(reload_events)
    graphs = [initial_graph, reload_graph]
    agent._get_async_graph_for_user = lambda *args, **kwargs: graphs.pop(0)

    async def collect_events():
        events = []
        async for event in agent.astream(
            "Use the hello kit",
            thread_id="thread-a",
            user_id="user-a",
            _is_self_invoke=True,
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())

    event_types = [event["type"] for event in events]
    assert event_types == [
        "tool_call",
        "tool_result",
        "tool_reload",
        "tool_call",
        "tool_result",
        "response",
    ]
    assert events[2]["ttl_seconds"] == 7200
    assert events[3]["name"] == "hello_test"
    assert events[4]["result"] == "hello world"
    assert events[5]["content"] == "Done"
    assert reload_graph.stream_inputs[0][2] == "v2"
