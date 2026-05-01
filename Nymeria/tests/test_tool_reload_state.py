"""Regression tests for per-turn tool reload bookkeeping."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from nymeria.core.agent import NymeriaAgent
from nymeria.core.event_bus import agent_stream_chunk_to_autonomous_event_data


def _bare_agent() -> NymeriaAgent:
    agent = object.__new__(NymeriaAgent)
    agent._pending_tool_reload = {}
    agent._turn_reload_count = {}
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


def test_sync_stream_cleanup_drops_unconsumed_pending_reload():
    agent = _bare_agent()
    agent._pending_tool_reload = {
        "thread-a": {"new_tools": ["bash_execute"]},
    }
    agent._turn_reload_count = {"thread-a": 1}

    agent._clear_tool_reload_state_after_stream("thread-a")

    assert agent._pending_tool_reload == {}
    assert "thread-a" not in agent._turn_reload_count


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


class _FakeGraph:
    def __init__(self, events):
        self.events = events
        self.stream_inputs = []
        self.invoke_called = False

    def stream(self, input_state, config=None, stream_mode=None):
        self.stream_inputs.append((input_state, config, stream_mode))
        yield from self.events()

    def invoke(self, input_state, config=None):
        self.invoke_called = True
        raise AssertionError("post-reload stream() must not use invoke()")

    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})


def test_stream_reload_resume_streams_post_reload_tool_events():
    agent = _bare_agent()
    agent._thread_locks = _FakeLockManager()
    agent._pending_notepads = {}
    agent.settings = SimpleNamespace(lock_timeout=1, context_management="none")
    agent._token_tracker = SimpleNamespace(record_usage=lambda *args, **kwargs: None)
    agent._get_time_context = lambda **kwargs: "[time]"
    agent._check_and_compact_sync = lambda *args, **kwargs: None
    agent.get_pending_summary = lambda thread_id: None
    agent._graph_run_config = lambda thread_id, user_id: {"configurable": {"thread_id": thread_id, "user_id": user_id}}
    agent._clean_tool_result_for_display = lambda result: result
    agent._tool_result_extra_events = lambda *args, **kwargs: []
    agent._index_conversation_turn = lambda **kwargs: None
    agent.invalidate_thread_config_cache = lambda thread_id: None
    agent._extract_tokens_from_response = lambda messages: (0, 0)
    agent._max_iterations_for_thread = lambda thread_id: 20
    agent._analyze_turn_safety = lambda messages, max_iterations: SimpleNamespace(should_stop=False)

    def initial_events():
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "id": "call-1",
                            "name": "Skill",
                            "args": {"name": "hello-kit"},
                            "type": "tool_call",
                        }],
                    )
                ]
            }
        }
        yield {
            "tools": {
                "messages": [
                    ToolMessage(
                        content="Skill Kit reload queued",
                        tool_call_id="call-1",
                        name="Skill",
                    )
                ]
            }
        }
        agent._pending_tool_reload["thread-a"] = {
            "new_tools": ["hello_test"],
            "ttl": "2h",
            "ttl_seconds": 7200,
            "source": "skill_kit",
            "skill_name": "hello-kit",
        }

    def reload_events():
        yield {
            "agent": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "id": "call-2",
                            "name": "hello_test",
                            "args": {"subject": "world"},
                            "type": "tool_call",
                        }],
                    )
                ]
            }
        }
        yield {
            "tools": {
                "messages": [
                    ToolMessage(
                        content="hello world",
                        tool_call_id="call-2",
                        name="hello_test",
                    )
                ]
            }
        }
        yield {"agent": {"messages": [AIMessage(content="Done")]}}

    initial_graph = _FakeGraph(initial_events)
    reload_graph = _FakeGraph(reload_events)
    graphs = [initial_graph, reload_graph]
    agent._get_graph_for_user = lambda *args, **kwargs: graphs.pop(0)

    events = list(agent.stream(
        "Use the hello kit",
        thread_id="thread-a",
        user_id="user-a",
        _is_self_invoke=True,
    ))

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
    assert reload_graph.stream_inputs[0][2] == "updates"
    assert reload_graph.invoke_called is False
