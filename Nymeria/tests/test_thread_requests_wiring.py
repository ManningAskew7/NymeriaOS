"""``NymeriaAgent.astream`` reminds a callee that ends its turn owing a reply.

Behaviours 23 and 24 of ``tmp/request-reply-plan.md`` (backlog #357): a turn
on a thread that owes a reply to another thread's request and ends without
``reply_to_thread`` is re-driven ONCE with an ``[Unreplied request]`` prompt
(a ``source="request_reminder"`` pending prompt at the drain-loop seam, so
the same turn continues under the same lock; "system" is the expiry notice's
reserved source and is dropped at absorb when no notice is due); a request already reminded about does
not re-drive again; a turn that replied gets no reminder; and nothing the
turn said reaches the caller implicitly. Stub-agent pattern from
``test_agent_turn_loops.py``: a scripted fake graph, one script per drive.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from nymeria.core import completion_delivery as cd
from nymeria.core import thread_requests as tr
from nymeria.core.agent import NymeriaAgent
from nymeria.core.pending_prompt_queue import (
    InMemoryPendingPromptQueue,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.token_tracker import TokenTracker

THREAD = "wiring-callee"
CALLER = "wiring-caller"


class _FakeLockManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.abort_event = threading.Event()

    def get_lock(self, thread_id):
        return self.lock

    def get_lock_info(self, thread_id):
        return None

    def set_lock_info(self, thread_id, holder, task_id=None):
        return None

    def clear_lock_info(self, thread_id):
        return None

    def get_abort_event(self, thread_id):
        return self.abort_event

    def is_thread_busy(self, thread_id):
        return False


class _FakeAsyncGraph:
    """Plays one scripted run per ``astream_events`` call (first drive, then
    each drained-batch re-drive). A callable inside a script is a side effect
    (a tool's work, such as a reply) executed mid-stream."""

    def __init__(self, *runs):
        self.runs = [list(r) for r in runs]
        self.stream_inputs = []
        self.state_updates = []

    async def astream_events(self, input_state, config=None, version=None):
        self.stream_inputs.append((input_state, config, version))
        for event in self.runs.pop(0):
            if callable(event):
                event()
                continue
            yield event

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})

    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})

    async def aupdate_state(self, config, values):
        # A drained batch is injected into the checkpoint, then the graph is
        # re-driven with an empty input; the injected text lives here.
        self.state_updates.append((config, values))


def _stream_agent(thread_id) -> Any:
    agent: Any = object.__new__(NymeriaAgent)
    agent._pending_tool_reload = {}
    agent._turn_reload_count = {}
    agent._subturn_compact_requested = set()
    agent._compactions_this_turn = {}
    agent._compacting_threads = set()
    agent._memory_seeded_threads = {thread_id}
    agent._thread_locks = _FakeLockManager()
    agent.accounts_repo = SimpleNamespace(get_thread_owner=lambda tid: "owner")
    # A re-drive builds its input through the expiry-notice and fallback-note
    # consumers, which read the thread config.
    agent.thread_config_manager = SimpleNamespace(get_config=lambda thread_id: None)
    agent.scheduler = SimpleNamespace(cancel=lambda *args, **kwargs: None)
    agent.settings = SimpleNamespace(lock_timeout=1, context_management="none")
    agent._token_tracker = TokenTracker()
    agent._get_time_context = lambda **kwargs: "[time]"
    agent._patch_dangling_tool_calls = lambda *args, **kwargs: 0
    agent.get_pending_summary = lambda thread_id: None
    agent._graph_run_config = lambda thread_id, user_id, **kwargs: {
        "configurable": {"thread_id": thread_id, "user_id": user_id}
    }
    agent._clean_tool_result_for_display = lambda result: result
    agent._tool_result_extra_events = lambda *args, **kwargs: []
    agent._index_conversation_turn = lambda **kwargs: None
    agent.invalidate_thread_config_cache = lambda thread_id: None
    agent._extract_tokens_from_response = lambda messages: (0, 0)
    agent._max_iterations_for_thread = lambda thread_id: 20
    agent._analyze_turn_safety = lambda messages, max_iterations: SimpleNamespace(
        should_stop=False
    )
    agent._get_llm_config_for_thread = lambda thread_id: None
    agent._record_turn_usage = lambda *args, **kwargs: (0, 0, False)
    agent._hook_registry_for_turn = lambda *args, **kwargs: None

    async def _no_done(**kwargs):
        return None

    agent._fire_done_observe = _no_done
    agent._maybe_done_continuation = _no_done
    # The reminder seam itself is the real method: it is what is under test.
    return agent


def _model_step(text: str, tool_calls=None):
    return [
        {"event": "on_chat_model_start", "data": {}},
        {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content=text, tool_calls=tool_calls or [])},
        },
    ]


_REPLY_CALL = {
    "name": "reply_to_thread",
    "args": {"request_id": "req-x", "content": "here"},
    "id": "tc-reply",
    "type": "tool_call",
}


@pytest.fixture(autouse=True)
def _fresh():
    tr.reset_for_tests()
    set_pending_queue(InMemoryPendingPromptQueue())
    yield
    tr.reset_for_tests()
    reset_pending_queue_for_tests()


def _capture_deliveries(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(cd, "fire_autonomous_turn", lambda agent, d: seen.append(d))
    return seen


def _owed() -> tr.ThreadRequest:
    req = tr.open_request(
        caller_thread_id=CALLER,
        caller_user_id="owner",
        caller_name="Coordinator",
        target_thread_id=THREAD,
        callable_name="Helper",
        task="find the staff directory",
        task_id="t-1",
    )
    # The request's prompt reached the callee's turn (the worker's stream
    # started): only then may a turn end remind about it.
    req.progress.observe({"type": "response", "content": "on it"})
    return req


def _run(agent, graph, **astream_kwargs):
    agent._get_async_graph_for_user = lambda *args, **kwargs: graph

    async def collect():
        events = []
        async for event in agent.astream(**astream_kwargs):
            events.append(event)
        return events

    return asyncio.run(collect())


def _turn_kwargs(**overrides):
    base: dict = dict(
        message="[Request Metadata] ... find the staff directory",
        thread_id=THREAD,
        user_id="owner",
        _is_self_invoke=True,
        source="callable",
        source_id="t-1",
        source_label="Helper",
    )
    base.update(overrides)
    return base


def _injected_text(graph) -> str:
    """The text of the last batch injected into state for a re-drive."""
    injected = [u for _, u in graph.state_updates if u.get("messages")]
    assert injected, "no re-drive injected anything"
    content = injected[-1]["messages"][-1].content
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


def test_a_turn_that_ends_owing_a_reply_is_redriven_once_with_the_reminder(monkeypatch):
    seen = _capture_deliveries(monkeypatch)
    req = _owed()
    agent = _stream_agent(THREAD)
    graph = _FakeAsyncGraph(
        # The callee does the work and ends its turn WITHOUT reply_to_thread.
        _model_step("Found it: the directory is at /nas/HR/staff.xlsx."),
        # The re-drive: it replies and ends again; no third drive exists, so a
        # second reminder would fail the test by running the script dry.
        _model_step("Replying now.", [_REPLY_CALL]) + _model_step("Sent."),
    )

    events = _run(agent, graph, **_turn_kwargs())

    assert len(graph.stream_inputs) == 2
    assert graph.stream_inputs[1][0] == {"messages": []}  # re-entry on the injected state
    reminder = _injected_text(graph)
    assert "[Unreplied request]" in reminder
    assert req.id in reminder and "Coordinator" in reminder
    assert "Nothing from this turn was delivered" in reminder
    injected = [e for e in events if e.get("type") == "prompt_injected"]
    assert len(injected) == 1 and injected[0]["sources"] == [tr.REMINDER_SOURCE]
    assert req.reminded_at is not None
    assert req.state == tr.STATE_OPEN
    time.sleep(0.2)
    assert seen == []  # the callee's plain text never reached the caller


def test_a_request_already_reminded_about_does_not_redrive_again(monkeypatch):
    seen = _capture_deliveries(monkeypatch)
    req = _owed()
    req.reminded_at = time.time()
    agent = _stream_agent(THREAD)
    graph = _FakeAsyncGraph(_model_step("still not replying"))

    events = _run(agent, graph, **_turn_kwargs())

    assert len(graph.stream_inputs) == 1 and graph.state_updates == []
    assert not any(e.get("type") == "prompt_injected" for e in events)
    assert req.state == tr.STATE_OPEN and seen == []


def test_a_turn_that_replied_gets_no_reminder_and_the_caller_gets_the_reply(monkeypatch):
    seen = _capture_deliveries(monkeypatch)
    req = _owed()
    agent = _stream_agent(THREAD)

    def the_reply_tool_runs():
        tr.reply(
            request_id=req.id, content="/nas/HR/staff.xlsx", final=True,
            replier_thread_id=THREAD, agent=agent,
        )

    graph = _FakeAsyncGraph(
        _model_step("Replying.", [_REPLY_CALL]) + [the_reply_tool_runs] + _model_step("Done.")
    )

    events = _run(agent, graph, **_turn_kwargs())

    assert len(graph.stream_inputs) == 1
    assert not any(e.get("type") == "prompt_injected" for e in events)
    assert req.state == tr.STATE_REPLIED
    deadline = time.time() + 3
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    assert len(seen) == 1
    assert seen[0].thread_id == CALLER and "/nas/HR/staff.xlsx" in seen[0].prompt_text


def test_a_thread_owing_nothing_is_never_redriven(monkeypatch):
    _capture_deliveries(monkeypatch)
    agent = _stream_agent(THREAD)
    graph = _FakeAsyncGraph(_model_step("just chatting"))

    events = _run(agent, graph, **_turn_kwargs(source="user", source_id=None, _is_self_invoke=False))

    assert len(graph.stream_inputs) == 1
    assert not any(e.get("type") == "prompt_injected" for e in events)
