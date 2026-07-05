"""Tests for server-authoritative tool timing (backlog #35).

Covers the SafeToolNode execution-bracket stamp (additional_kwargs["tool_timing"]),
the opt-in [Duration: ...] suffix on the model-visible result text
(TOOL_TIMING_IN_RESULTS), and the history-reload surfacing of persisted timing.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.runtime import DEFAULT_RUNTIME
from langgraph._internal._constants import CONFIG_KEY_RUNTIME

from nymeria.core.agent_history import format_conversation_history
from nymeria.core.hooks import HookEvent, PostToolOutcome, PreToolOutcome
from nymeria.vendor.react_agent.nodes import SafeToolNode, format_tool_duration_ms

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


@tool
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def _config(*, timing_in_results: bool = False, thread_id: str = "t-timing"):
    configurable = {
        CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
        "thread_id": thread_id,
        "user_id": "u-timing",
    }
    if timing_in_results:
        configurable["tool_timing_in_results"] = True
    return {"configurable": configurable}


def _msg(tool_calls):
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def _call(text="hi", cid="c1"):
    return {"name": "echo", "args": {"text": text}, "id": cid}


def _first_msg(result):
    return result["messages"][0]


def _assert_valid_timing(msg: ToolMessage) -> dict:
    timing = msg.additional_kwargs.get("tool_timing")
    assert isinstance(timing, dict)
    assert isinstance(timing["duration_ms"], int)
    assert timing["duration_ms"] >= 0
    # started_at must be a parseable ISO-8601 UTC timestamp.
    parsed = datetime.fromisoformat(timing["started_at"])
    assert parsed.tzinfo is not None
    return timing


# --------------------------------------------------------------------------- #
# format_tool_duration_ms
# --------------------------------------------------------------------------- #


def test_format_tool_duration_ms_matches_client_badge_format():
    assert format_tool_duration_ms(0) == "0ms"
    assert format_tool_duration_ms(850) == "850ms"
    assert format_tool_duration_ms(3400) == "3.4s"
    assert format_tool_duration_ms(9999) == "10.0s"
    assert format_tool_duration_ms(42000) == "42s"
    assert format_tool_duration_ms(300000) == "300s"


# --------------------------------------------------------------------------- #
# SafeToolNode stamp: no-hooks fast path
# --------------------------------------------------------------------------- #


def test_no_hooks_result_carries_tool_timing_stamp():
    dmod.reset()
    try:
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("world")]), _config())
        msg = _first_msg(out)
        assert msg.content == "echo:world"  # content untouched by default
        _assert_valid_timing(msg)
    finally:
        dmod.reset()


def test_no_hooks_async_result_carries_tool_timing_stamp():
    dmod.reset()
    try:
        node = SafeToolNode([echo])
        out = asyncio.run(node.ainvoke(_msg([_call("world")]), _config()))
        msg = _first_msg(out)
        assert msg.content == "echo:world"
        _assert_valid_timing(msg)
    finally:
        dmod.reset()


def test_timing_in_results_flag_appends_duration_suffix():
    dmod.reset()
    try:
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config(timing_in_results=True))
        msg = _first_msg(out)
        timing = _assert_valid_timing(msg)
        expected_suffix = f"\n\n[Duration: {format_tool_duration_ms(timing['duration_ms'])}]"
        assert msg.content == f"echo:hi{expected_suffix}"
    finally:
        dmod.reset()


def test_timing_suffix_absent_when_flag_off():
    dmod.reset()
    try:
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config())
        assert "[Duration:" not in _first_msg(out).content
    finally:
        dmod.reset()


# --------------------------------------------------------------------------- #
# SafeToolNode stamp: hooks path
# --------------------------------------------------------------------------- #


def test_hooks_path_stamps_timing_and_appends_after_hook_note():
    dmod.reset()
    try:
        dmod.register(
            HookEvent.POST_TOOL_USE,
            lambda c: PostToolOutcome(additional_context="[note]"),
        )
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config(timing_in_results=True))
        msg = _first_msg(out)
        timing = _assert_valid_timing(msg)
        suffix = f"[Duration: {format_tool_duration_ms(timing['duration_ms'])}]"
        assert msg.content == f"echo:hi\n\n[note]\n\n{suffix}"
    finally:
        dmod.reset()


def test_hooks_path_stamps_timing_without_flag():
    dmod.reset()
    try:
        dmod.register(
            HookEvent.POST_TOOL_USE,
            lambda c: PostToolOutcome(additional_context="[note]"),
        )
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config())
        msg = _first_msg(out)
        assert msg.content == "echo:hi\n\n[note]"
        _assert_valid_timing(msg)
    finally:
        dmod.reset()


def test_pre_hook_denied_call_carries_no_timing_stamp():
    # A denied call never executed, so it gets no tool_timing (by design; the
    # same holds for the timeout/cancelled/skipped synthetic messages built
    # outside _run_one).
    dmod.reset()
    try:
        dmod.register(
            HookEvent.PRE_TOOL_USE,
            lambda c: PreToolOutcome(decision="deny", reason="nope"),
        )
        node = SafeToolNode([echo])
        out = node.invoke(_msg([_call("hi")]), _config(timing_in_results=True))
        msg = _first_msg(out)
        assert msg.status == "error"
        assert "tool_timing" not in msg.additional_kwargs
        assert "[Duration:" not in msg.content
    finally:
        dmod.reset()


# --------------------------------------------------------------------------- #
# History reload: persisted stamp surfaces on the tool_call step
# --------------------------------------------------------------------------- #


def test_history_surfaces_persisted_tool_timing_on_step():
    messages = [
        HumanMessage(content="run it", id="user-1"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "hi"}}],
            id="assistant-1",
        ),
        ToolMessage(
            content="echo:hi",
            tool_call_id="call-1",
            additional_kwargs={
                "tool_timing": {
                    "started_at": "2026-07-05T01:02:03+00:00",
                    "duration_ms": 2500,
                }
            },
        ),
        AIMessage(content="Done", id="assistant-2"),
    ]

    history = format_conversation_history(messages, thread_id="thread-a")
    steps = history[1]["steps"]
    tool_step = next(s for s in steps if s["type"] == "tool_call")
    assert tool_step["duration_ms"] == 2500
    assert tool_step["startTime"] == "2026-07-05T01:02:03+00:00"
    assert tool_step["endTime"] == "2026-07-05T01:02:05.500000+00:00"


def test_history_ignores_malformed_tool_timing():
    messages = [
        HumanMessage(content="run it", id="user-1"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "hi"}}],
            id="assistant-1",
        ),
        ToolMessage(
            content="echo:hi",
            tool_call_id="call-1",
            additional_kwargs={"tool_timing": {"started_at": "not-a-date"}},
        ),
        AIMessage(content="Done", id="assistant-2"),
    ]

    history = format_conversation_history(messages, thread_id="thread-a")
    steps = history[1]["steps"]
    tool_step = next(s for s in steps if s["type"] == "tool_call")
    assert "duration_ms" not in tool_step
    assert "endTime" not in tool_step
    # startTime passes through as-is; clients treat unparseable values as absent.
    assert tool_step.get("startTime") == "not-a-date"


def test_history_step_without_timing_stamp_is_unchanged():
    messages = [
        HumanMessage(content="run it", id="user-1"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "hi"}}],
            id="assistant-1",
        ),
        ToolMessage(content="echo:hi", tool_call_id="call-1"),
        AIMessage(content="Done", id="assistant-2"),
    ]

    history = format_conversation_history(messages, thread_id="thread-a")
    steps = history[1]["steps"]
    tool_step = next(s for s in steps if s["type"] == "tool_call")
    assert "startTime" not in tool_step
    assert "endTime" not in tool_step
    assert "duration_ms" not in tool_step
