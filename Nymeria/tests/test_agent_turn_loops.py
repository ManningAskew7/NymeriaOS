"""Tests for the shared chat()/astream() turn-loop helpers.

Unit tests drive the helpers directly against duck-typed stub agents (the
helpers reach every collaborator via ``agent.<attr>``, so a SimpleNamespace
suffices). The integration tests at the bottom drive the REAL
``NymeriaAgent.astream`` with the stub-agent pattern from
``test_tool_reload_state.py`` to lock two behaviors at the call-site level:
the holder_kind fix (a first-pass tool reload must not corrupt the turn
source seen by DONE hooks) and the cross-user drain rejection.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage

from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_turn_loops import (
    build_queued_prompt_messages,
    run_subturn_compact_loop,
    run_subturn_compact_loop_sync,
    run_tool_reload_loop,
    run_tool_reload_loop_sync,
)
from nymeria.core.pending_prompt_queue import (
    InMemoryPendingPromptQueue,
    _SENTINEL_PROMPT_ABSORBED,
    make_pending_prompt,
    notify_batch_absorbed,
    notify_batch_error,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.token_tracker import TokenTracker


class _RecMailbox:
    """Duck-typed FanoutMailbox recorder."""

    def __init__(self):
        self.events = []
        self.closed = False

    def put(self, evt):
        self.events.append(evt)

    def close(self):
        self.closed = True


class _FakeSyncGraph:
    def __init__(self, messages=None, on_invoke=None):
        self.messages = messages if messages is not None else []
        self.invocations = []
        self.on_invoke = on_invoke

    def invoke(self, input_state, config=None):
        self.invocations.append((input_state, config))
        if self.on_invoke is not None:
            self.on_invoke()
        return {"messages": self.messages}


class _FakeStreamProcessor:
    def __init__(self, events=None):
        self.events = events if events is not None else [{"type": "response", "content": "ok"}]
        self.drives = []

    async def drive(self, graph_obj, input_state):
        self.drives.append((graph_obj, input_state))
        for evt in self.events:
            yield evt


def _reload_agent(*, sync=True, reload_again=False) -> Any:
    """Stub agent for the reload-loop helpers."""
    agent = SimpleNamespace()
    agent.MAX_TOOL_RELOADS_PER_TURN = 3
    agent._pending_tool_reload = {}
    agent._turn_reload_count = {}
    agent.invalidated = []
    agent.invalidate_thread_config_cache = agent.invalidated.append
    agent._create_tool_reload_resume_message = lambda info: {"resume": info["new_tools"]}

    def _rearm():
        if reload_again:
            agent._pending_tool_reload["t1"] = {"new_tools": ["again"]}

    if sync:
        agent.built_graphs = []

        def _get_graph(user_id, thread_id=None):
            g = _FakeSyncGraph(messages=[f"m{len(agent.built_graphs)}"], on_invoke=_rearm)
            agent.built_graphs.append(g)
            return g

        agent._get_graph_for_user = _get_graph
    else:
        agent.built_graphs = []

        def _get_async_graph(user_id, thread_id=None):
            g = SimpleNamespace(name=f"g{len(agent.built_graphs)}")
            agent.built_graphs.append(g)
            _rearm()
            return g

        agent._get_async_graph_for_user = _get_async_graph
    return agent


# ---------------------------------------------------------------------------
# run_tool_reload_loop_sync
# ---------------------------------------------------------------------------


def test_sync_reload_loop_noop_when_nothing_pending():
    agent = _reload_agent()
    graph = object()
    out_graph, messages = run_tool_reload_loop_sync(
        agent, graph=graph, thread_id="t1", user_id="u1",
        config={}, context_label="tool reload",
    )
    assert out_graph is graph
    assert messages is None
    assert agent._turn_reload_count == {}


def test_sync_reload_loop_single_reload_rebuilds_and_resumes():
    agent = _reload_agent()
    agent._pending_tool_reload["t1"] = {"new_tools": ["hello"]}
    out_graph, messages = run_tool_reload_loop_sync(
        agent, graph=object(), thread_id="t1", user_id="u1",
        config={"c": 1}, context_label="tool reload",
    )
    assert len(agent.built_graphs) == 1
    assert out_graph is agent.built_graphs[0]
    assert messages == ["m0"]
    assert agent._turn_reload_count["t1"] == 1
    assert agent.invalidated == ["t1"]
    # The resume message rides an invoke on the fresh graph with the config.
    state, config = agent.built_graphs[0].invocations[0]
    assert state == {"messages": [{"resume": ["hello"]}]}
    assert config == {"c": 1}


def test_sync_reload_loop_respects_cap():
    agent = _reload_agent(reload_again=True)
    agent._pending_tool_reload["t1"] = {"new_tools": ["first"]}
    out_graph, messages = run_tool_reload_loop_sync(
        agent, graph=object(), thread_id="t1", user_id="u1",
        config={}, context_label="tool reload",
    )
    assert agent._turn_reload_count["t1"] == 3
    assert len(agent.built_graphs) == 3
    # The re-armed 4th request is left pending (the call site pops residuals).
    assert "t1" in agent._pending_tool_reload
    assert out_graph is agent.built_graphs[-1]
    assert messages == ["m2"]


# ---------------------------------------------------------------------------
# run_tool_reload_loop (async)
# ---------------------------------------------------------------------------


def _collect_reload_events(agent, *, pending_batch, abort=False):
    async def _run():
        abort_event = threading.Event()
        if abort:
            abort_event.set()
        sink = []
        sp: Any = _FakeStreamProcessor()
        events = []
        async for evt in run_tool_reload_loop(
            agent,
            stream_processor=sp,
            thread_id="t1",
            user_id="u1",
            abort_event=abort_event,
            pending_batch=pending_batch,
            context_label="tool reload",
            graph_sink=sink,
        ):
            events.append(evt)
        return events, sink, sp

    return asyncio.run(_run())


def test_async_reload_loop_yields_reload_event_then_stream():
    agent = _reload_agent(sync=False)
    agent._pending_tool_reload["t1"] = {
        "new_tools": ["hello"],
        "ttl": "1h",
        "ttl_seconds": 3600,
        "source": "skill_kit",
        "skill_name": "kit",
        "reason": "why",
    }
    events, sink, sp = _collect_reload_events(agent, pending_batch=None)
    assert [e["type"] for e in events] == ["tool_reload", "response"]
    assert events[0]["ttl_seconds"] == 3600
    assert events[0]["source"] == "skill_kit"
    assert events[0]["skill_name"] == "kit"
    assert sink == [agent.built_graphs[0]]
    # Resume drive targeted the fresh graph with the resume message.
    graph_obj, state = sp.drives[0]
    assert graph_obj is agent.built_graphs[0]
    assert state == {"messages": [{"resume": ["hello"]}]}


def test_async_reload_loop_fans_out_to_batch_mailboxes():
    agent = _reload_agent(sync=False)
    agent._pending_tool_reload["t1"] = {"new_tools": ["hello"]}
    mailbox: Any = _RecMailbox()
    prompt = make_pending_prompt(
        message="hi", source="user", source_id=None, source_label="u",
        user_id="u1", is_autonomous=False, fanout_mailbox=mailbox,
    )
    events, sink, sp = _collect_reload_events(agent, pending_batch=[prompt])
    # The mailbox mirrors the reload event AND the resumed stream events.
    assert [e["type"] for e in mailbox.events] == ["tool_reload", "response"]
    assert [e["type"] for e in events] == ["tool_reload", "response"]


def test_async_reload_loop_breaks_on_abort():
    agent = _reload_agent(sync=False)
    agent._pending_tool_reload["t1"] = {"new_tools": ["hello"]}
    events, sink, sp = _collect_reload_events(agent, pending_batch=None, abort=True)
    assert events == []
    assert sink == []
    assert "t1" in agent._pending_tool_reload  # untouched


def test_async_reload_loop_does_not_touch_caller_source_binding():
    """Regression: the first-pass loop used to rebind the caller's `source`."""
    agent = _reload_agent(sync=False)
    agent._pending_tool_reload["t1"] = {"new_tools": ["hello"], "source": "tool_search"}
    source = "ticker"  # caller-scope turn source
    events, _, _ = _collect_reload_events(agent, pending_batch=None)
    assert events[0]["source"] == "tool_search"
    assert source == "ticker"


# ---------------------------------------------------------------------------
# run_subturn_compact_loop_sync / run_subturn_compact_loop
# ---------------------------------------------------------------------------


def _fake_sp() -> Any:
    return _FakeStreamProcessor()


def _compact_agent(*, results) -> Any:
    agent = SimpleNamespace()
    agent._subturn_compact_requested = {"t1"}
    agent._compactions_this_turn = {}
    seq = list(results)
    agent._compaction = SimpleNamespace(
        _do_compact_sync=lambda tid, uid: seq.pop(0) if seq else None
    )

    async def _do_auto(tid, uid):
        return seq.pop(0) if seq else None

    agent._do_auto_compact = _do_auto
    agent.built_graphs = []

    def _get_async_graph(user_id, thread_id=None):
        g = SimpleNamespace(name=f"g{len(agent.built_graphs)}")
        agent.built_graphs.append(g)
        return g

    agent._get_async_graph_for_user = _get_async_graph
    return agent


def test_sync_subturn_loop_compacts_and_reinvokes():
    agent = _compact_agent(results=[{"success": True}])
    graph = _FakeSyncGraph(messages=["after-compact"])
    messages = run_subturn_compact_loop_sync(
        agent, graph=graph, thread_id="t1", user_id="u1", config={"c": 1}
    )
    assert messages == ["after-compact"]
    assert graph.invocations == [({"messages": []}, {"c": 1})]
    assert agent._compactions_this_turn["t1"] == 1
    assert "t1" not in agent._subturn_compact_requested


def test_sync_subturn_loop_breaks_on_failure_and_discards():
    agent = _compact_agent(results=[{"success": False, "reason": "small"}])
    graph = _FakeSyncGraph()
    messages = run_subturn_compact_loop_sync(
        agent, graph=graph, thread_id="t1", user_id="u1", config={}
    )
    assert messages is None
    assert graph.invocations == []
    assert agent._compactions_this_turn == {}
    assert "t1" not in agent._subturn_compact_requested


def test_async_subturn_loop_yields_events_and_redrives():
    agent = _compact_agent(
        results=[{"success": True, "messages_removed": 4, "summary": "s"}]
    )

    async def _run():
        sink = []
        sp: Any = _FakeStreamProcessor()
        events = []
        async for evt in run_subturn_compact_loop(
            agent,
            stream_processor=sp,
            thread_id="t1",
            user_id="u1",
            abort_event=threading.Event(),
            graph_sink=sink,
        ):
            events.append(evt)
        return events, sink, sp

    events, sink, sp = asyncio.run(_run())
    assert [e["type"] for e in events] == ["compacting", "compacted", "response"]
    assert events[1] == {
        "type": "compacted",
        "messages_removed": 4,
        "auto_resumed": True,
        "summary": "s",
        "subturn": True,
    }
    assert sink == [agent.built_graphs[0]]
    assert sp.drives[0] == (agent.built_graphs[0], {"messages": []})
    assert "t1" not in agent._subturn_compact_requested


def test_async_subturn_loop_skips_when_aborted():
    agent = _compact_agent(results=[{"success": True}])

    async def _run():
        abort_event = threading.Event()
        abort_event.set()
        events = []
        async for evt in run_subturn_compact_loop(
            agent,
            stream_processor=_fake_sp(),
            thread_id="t1",
            user_id="u1",
            abort_event=abort_event,
            graph_sink=[],
        ):
            events.append(evt)
        return events

    assert asyncio.run(_run()) == []
    # Post-loop discard still clears the flag.
    assert "t1" not in agent._subturn_compact_requested


# ---------------------------------------------------------------------------
# Batch primitives
# ---------------------------------------------------------------------------


def _prompt(**overrides):
    kwargs = dict(
        message="do it", source="user", source_id=None, source_label="u",
        user_id="u1", is_autonomous=False, fanout_mailbox=None,
    )
    kwargs.update(overrides)
    return make_pending_prompt(**kwargs)


def test_build_queued_prompt_messages_visibility_flags():
    user_p = _prompt()
    auto_p = _prompt(source="ticker", is_autonomous=True, message="wake")
    msgs = build_queued_prompt_messages([user_p, auto_p])
    assert len(msgs) == 2
    assert msgs[0].content.startswith("[Time: ")
    assert msgs[0].content.endswith("\n\ndo it")
    assert "internal" not in msgs[0].additional_kwargs
    assert msgs[1].additional_kwargs["internal"] is True
    assert msgs[1].additional_kwargs["internal_type"] == "autonomous_wakeup"
    assert msgs[1].content.endswith("\n\nwake")


def test_notify_batch_absorbed_sentinel_close_and_wake():
    mailbox = _RecMailbox()
    with_mb = _prompt(fanout_mailbox=mailbox)
    without_mb = _prompt()
    notify_batch_absorbed([with_mb, without_mb])
    assert mailbox.events == [{"type": _SENTINEL_PROMPT_ABSORBED}]
    assert mailbox.closed is True
    assert with_mb.notify_event.is_set()
    assert without_mb.notify_event.is_set()
    assert with_mb.abandoned is False


def test_notify_batch_error_without_abandoned():
    mailbox = _RecMailbox()
    p = _prompt(fanout_mailbox=mailbox)
    notify_batch_error([p], code="inject_failed", content="boom")
    assert mailbox.events == [
        {"type": "error", "code": "inject_failed", "content": "boom"}
    ]
    assert mailbox.closed is True
    assert p.notify_event.is_set()
    assert p.abandoned is False


def test_notify_batch_error_with_abandoned():
    p = _prompt()
    notify_batch_error(
        [p], code="cross_user_queue_unsupported", content="busy", abandoned=True
    )
    assert p.abandoned is True
    assert p.notify_event.is_set()


# ---------------------------------------------------------------------------
# Integration: real astream() call sites (stub-agent pattern from
# test_tool_reload_state.py)
# ---------------------------------------------------------------------------


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
        self.state_updates = []

    async def astream_events(self, input_state, config=None, version=None):
        self.stream_inputs.append((input_state, config, version))
        async for event in self.events():
            yield event

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})

    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})

    async def aupdate_state(self, config, values):
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
    return agent


def test_astream_reload_preserves_holder_kind_for_done_hooks():
    """Regression for the `source` shadow: after a first-pass tool reload the
    DONE observe/continuation hooks must still see the real turn source, not
    the reload's source."""
    thread_id = "turn-loops-holder-kind"
    agent = _stream_agent(thread_id)

    recorded = {}

    async def _record_done(**kwargs):
        recorded.update(kwargs)

    continuation_kinds = []

    async def _no_continuation(**kwargs):
        continuation_kinds.append(kwargs.get("holder_kind"))
        return None

    agent._fire_done_observe = _record_done
    agent._maybe_done_continuation = _no_continuation

    async def initial_events():
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="first")},
        }
        agent._pending_tool_reload[thread_id] = {
            "new_tools": ["hello_test"],
            "ttl": "2h",
            "ttl_seconds": 7200,
            "source": "skill_kit",
            "skill_name": "hello-kit",
        }

    async def reload_events():
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="done")},
        }

    graphs = [_FakeAsyncGraph(initial_events), _FakeAsyncGraph(reload_events)]
    agent._get_async_graph_for_user = lambda *args, **kwargs: graphs.pop(0)

    set_pending_queue(InMemoryPendingPromptQueue())
    try:

        async def collect():
            events = []
            async for event in agent.astream(
                "hi", thread_id=thread_id, user_id="user-a", _is_self_invoke=True
            ):
                events.append(event)
            return events

        events = asyncio.run(collect())
    finally:
        reset_pending_queue_for_tests()

    assert [e["type"] for e in events] == ["response", "tool_reload", "response"]
    assert events[1]["source"] == "skill_kit"
    # _is_self_invoke=True resolves the turn source to "ticker"; before the
    # fix these read "skill_kit" (the reload source).
    assert recorded["holder_kind"] == "ticker"
    assert continuation_kinds == ["ticker"]
    assert recorded["completed_normally"] is True


def test_astream_drain_rejects_cross_user_prompts_and_absorbs_own():
    thread_id = "turn-loops-cross-user"
    agent = _stream_agent(thread_id)

    async def _no_continuation(**kwargs):
        return None

    async def _record_done(**kwargs):
        return None

    agent._maybe_done_continuation = _no_continuation
    agent._fire_done_observe = _record_done

    async def model_events():
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="answer")},
        }

    graph = _FakeAsyncGraph(model_events)
    agent._get_async_graph_for_user = lambda *args, **kwargs: graph

    backend = InMemoryPendingPromptQueue()
    set_pending_queue(backend)

    cross_mailbox: Any = _RecMailbox()
    own_mailbox: Any = _RecMailbox()
    cross_prompt = _prompt(
        user_id="user-b", message="mine too", fanout_mailbox=cross_mailbox
    )
    own_prompt = _prompt(
        user_id="user-a", message="follow up", fanout_mailbox=own_mailbox
    )
    backend.enqueue(thread_id, cross_prompt)
    backend.enqueue(thread_id, own_prompt)

    try:

        async def collect():
            events = []
            async for event in agent.astream(
                "hi", thread_id=thread_id, user_id="user-a", _is_self_invoke=True
            ):
                events.append(event)
            return events

        events = asyncio.run(collect())
    finally:
        reset_pending_queue_for_tests()

    # Cross-user prompt: error + close + abandoned + woken, never injected.
    assert cross_mailbox.events == [
        {
            "type": "error",
            "code": "cross_user_queue_unsupported",
            "content": (
                "This thread is busy with another user's turn. "
                "Retry once the current turn finishes."
            ),
        }
    ]
    assert cross_mailbox.closed is True
    assert cross_prompt.abandoned is True
    assert cross_prompt.notify_event.is_set()

    # Same-user prompt: injected, mirrored stream, absorbed sentinel.
    own_types = [e["type"] for e in own_mailbox.events]
    assert own_types[0] == "prompt_injected"
    assert own_types[-1] == _SENTINEL_PROMPT_ABSORBED
    assert "response" in own_types
    assert own_mailbox.closed is True
    assert own_prompt.abandoned is False
    assert own_prompt.notify_event.is_set()

    # The injected HumanMessage batch contains only the same-user prompt.
    assert len(graph.state_updates) == 1
    _, values = graph.state_updates[0]
    injected = values["messages"]
    assert len(injected) == 1
    assert injected[0].content.endswith("follow up")

    # The holder's own stream saw the inject + halt bookkeeping events.
    holder_types = [e["type"] for e in events]
    assert "prompt_injected" in holder_types
    assert holder_types.count("response") == 2  # first pass + re-drive


# ---------------------------------------------------------------------------
# Integration: /resume of a graceful iteration-cap halt (backlog #27) and the
# drain loop's patch-before-inject guard, driven through the REAL astream.
# ---------------------------------------------------------------------------


def _graceful_halt_tail(count: int) -> list:
    from langchain_core.messages import HumanMessage, ToolMessage

    messages: list = [HumanMessage(content="go")]
    for index in range(count):
        call_id = f"call-{index}"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"id": call_id, "name": "lookup", "args": {"i": index}}],
            )
        )
        messages.append(ToolMessage(content=f"result {index}", tool_call_id=call_id))
    return messages


class _StatefulAsyncGraph(_FakeAsyncGraph):
    """_FakeAsyncGraph whose checkpoint state is settable per test."""

    def __init__(self, events, messages):
        super().__init__(events)
        self.messages = messages

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self.messages})

    def get_state(self, config):
        return SimpleNamespace(values={"messages": self.messages})


def _resume_agent(thread_id: str) -> Any:
    agent = _stream_agent(thread_id)
    # The real astream passes tool_call_offset; the plain _stream_agent stub
    # predates the kwarg.
    agent._analyze_turn_safety = (
        lambda messages, max_iterations, **kwargs: SimpleNamespace(should_stop=False)
    )

    async def _no_continuation(**kwargs):
        return None

    async def _record_done(**kwargs):
        return None

    agent._maybe_done_continuation = _no_continuation
    agent._fire_done_observe = _record_done
    return agent


def test_astream_resume_invalid_tail_yields_error():
    thread_id = "turn-loops-resume-invalid"
    agent = _resume_agent(thread_id)
    agent._max_iterations_for_thread = lambda thread_id: 5

    async def model_events():
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="should never run")},
        }

    # A clean, completed tail: nothing to resume.
    graph = _StatefulAsyncGraph(model_events, _graceful_halt_tail(2))
    agent._get_async_graph_for_user = lambda *args, **kwargs: graph

    async def collect():
        events = []
        async for event in agent.astream(
            "", thread_id=thread_id, user_id="user-a", _resume_halted_turn=True
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert [e["type"] for e in events] == ["error"]
    assert events[0]["code"] == "resume_invalid"
    # The graph was never driven.
    assert graph.stream_inputs == []


def test_astream_resume_redrives_with_anchored_window():
    thread_id = "turn-loops-resume-ok"
    agent = _resume_agent(thread_id)
    agent._max_iterations_for_thread = lambda thread_id: 5

    async def model_events():
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="continued")},
        }

    # A graceful cap halt: 6 executed exchanges (> 5), ToolMessage-terminal.
    graph = _StatefulAsyncGraph(model_events, _graceful_halt_tail(6))
    agent._get_async_graph_for_user = lambda *args, **kwargs: graph

    async def collect():
        events = []
        async for event in agent.astream(
            "", thread_id=thread_id, user_id="user-a", _resume_halted_turn=True
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    types = [e["type"] for e in events]

    # The resume announces itself, then streams the continuation.
    assert types[0] == "turn_resumed"
    assert events[0]["tool_call_offset"] == 6
    assert "response" in types

    # The re-drive is the compaction shape: empty input, nothing injected
    # into history, and the safety window anchored via the config stamp.
    (input_state, config, _version) = graph.stream_inputs[0]
    assert input_state == {"messages": []}
    assert config["configurable"]["turn_safety_tool_call_offset"] == 6
    assert graph.state_updates == []


def test_astream_resume_second_halt_emits_resumable_iteration_limit():
    """End-to-end pin: a resumed turn whose continuation crosses the cap
    AGAIN runs the REAL post-drive analyzer (no stub) and emits an
    iteration_limit event with resumable=True and the window-anchored count
    (the pre-resume calls must not re-fire or inflate the report)."""
    thread_id = "turn-loops-resume-second-halt"
    agent = _resume_agent(thread_id)
    # Drop the helper's analyzer stub so the class facade (the real
    # analyzer + resumable predicate + event builder) runs.
    del agent._analyze_turn_safety
    agent._max_iterations_for_thread = lambda thread_id: 5

    # A graceful cap halt to resume from: 6 executed exchanges (> 5).
    tail = _graceful_halt_tail(6)

    async def model_events():
        # The continuation executes 6 more tool batches before halting
        # gracefully again: grow the checkpoint tail in place so the
        # post-drive aget_state sees the re-crossed window (12 total).
        tail.extend(_graceful_halt_tail(6)[1:])
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="continued")},
        }

    graph = _StatefulAsyncGraph(model_events, tail)
    agent._get_async_graph_for_user = lambda *args, **kwargs: graph

    async def collect():
        events = []
        async for event in agent.astream(
            "", thread_id=thread_id, user_id="user-a", _resume_halted_turn=True
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert events[0]["type"] == "turn_resumed"
    assert events[0]["tool_call_offset"] == 6

    limits = [e for e in events if e["type"] == "iteration_limit"]
    assert len(limits) == 1
    limit = limits[0]
    assert limit["reason"] == "max_iterations"
    assert limit["scope"] == "main_agent"
    # Window-anchored: 12 executed calls minus the resume offset of 6.
    assert limit["tool_call_count"] == 6
    assert limit["max_iterations"] == 5
    assert limit["resumable"] is True
    assert "/resume" in limit["content"]


def test_astream_drain_patches_dangling_before_inject():
    from nymeria.core.agent_callable_lifecycle import DANGLING_MARKER_REPEATED

    thread_id = "turn-loops-drain-patch"
    agent = _resume_agent(thread_id)

    order: list[Any] = []

    def _record_patch(graph, config, marker=None):
        order.append(("patch", marker))
        return 0

    agent._patch_dangling_tool_calls = _record_patch

    async def model_events():
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="answer")},
        }

    class _OrderedGraph(_FakeAsyncGraph):
        async def aupdate_state(self, config, values):
            order.append(("inject",))
            await super().aupdate_state(config, values)

    graph = _OrderedGraph(model_events)
    agent._get_async_graph_for_user = lambda *args, **kwargs: graph

    backend = InMemoryPendingPromptQueue()
    set_pending_queue(backend)
    backend.enqueue(thread_id, _prompt(user_id="user-a", message="follow up"))

    try:

        async def collect():
            events = []
            async for event in agent.astream(
                "hi", thread_id=thread_id, user_id="user-a"
            ):
                events.append(event)
            return events

        asyncio.run(collect())
    finally:
        reset_pending_queue_for_tests()

    # The drain loop patches a (potentially dangling) tail with the
    # repeated-halt marker BEFORE injecting the queued HumanMessage, so a
    # repeated-tool halt plus a queued prompt can never hand the provider a
    # tool_use without its tool_result.
    inject_at = order.index(("inject",))
    assert ("patch", DANGLING_MARKER_REPEATED) in order[:inject_at]
