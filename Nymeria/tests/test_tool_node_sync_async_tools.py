"""Coroutine-only tools on SafeToolNode's SYNC path (#390).

``POST /chat/sync`` and the bots' sync fallback drive the graph with
``invoke``; nine tool modules are ``async def`` only, and LangChain's
``StructuredTool._run`` refuses them ("does not support sync invocation").
The node bridges them onto a private event loop instead. Same harness as
the timeout tests: ``DEFAULT_RUNTIME`` through ``config``.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph._internal._constants import CONFIG_KEY_RUNTIME
from langgraph.runtime import DEFAULT_RUNTIME

from nymeria.vendor.react_agent.nodes import SafeToolNode


def _config(**configurable):
    return {
        "configurable": {
            CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
            "thread_id": "t-async-sync",
            "user_id": "u-async-sync",
            **configurable,
        }
    }


def _msg(tool_calls):
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def _by_id(result):
    return {m.tool_call_id: m for m in result["messages"]}


@tool
async def echo_async(text: str) -> str:
    """Coroutine-only tool: echoes after a loop hop."""
    await asyncio.sleep(0.01)
    return f"async:{text}"


@tool
def echo_sync(text: str) -> str:
    """Plain sync tool."""
    return f"sync:{text}"


def test_sync_invoke_runs_a_coroutine_only_tool_and_its_sync_sibling():
    node = SafeToolNode([echo_async, echo_sync], tool_timeout=5)
    result = node.invoke(
        _msg([
            {"name": "echo_async", "args": {"text": "a"}, "id": "c1"},
            {"name": "echo_sync", "args": {"text": "b"}, "id": "c2"},
        ]),
        _config(),
    )
    by_id = _by_id(result)
    assert by_id["c1"].content == "async:a"
    assert by_id["c1"].status != "error"
    assert by_id["c2"].content == "sync:b"


def test_ordered_sync_path_runs_a_coroutine_only_tool():
    node = SafeToolNode([echo_async, echo_sync], tool_timeout=5)
    result = node.invoke(
        _msg([
            {"name": "echo_sync", "args": {"text": "first"}, "id": "c1"},
            {"name": "echo_async", "args": {"text": "second"}, "id": "c2"},
        ]),
        _config(sequential_tools=True),
    )
    assert [m.content for m in result["messages"]] == ["sync:first", "async:second"]


def test_sync_invoke_still_bounds_a_hung_coroutine_only_tool():
    @tool
    async def hangs_async(x: str = "") -> str:
        """Sleeps past the per-call timeout."""
        await asyncio.sleep(5.0)
        return "should-not-finish"

    node = SafeToolNode([hangs_async, echo_sync], tool_timeout=1)
    started = time.monotonic()
    result = node.invoke(
        _msg([
            {"name": "hangs_async", "args": {}, "id": "h"},
            {"name": "echo_sync", "args": {"text": "ok"}, "id": "s"},
        ]),
        _config(),
    )
    elapsed = time.monotonic() - started
    by_id = _by_id(result)
    assert "[Error]" in by_id["h"].content and "timed out" in by_id["h"].content
    assert by_id["s"].content == "sync:ok"
    assert elapsed < 3.0, f"waited past the per-call timeout: {elapsed:.1f}s"


def test_sync_invoke_reports_a_coroutine_only_tools_exception_as_an_error_message():
    @tool
    async def explodes(x: str = "") -> str:
        """Raises inside the coroutine."""
        raise RuntimeError("kaboom in the coroutine")

    node = SafeToolNode([explodes], tool_timeout=5)
    result = node.invoke(_msg([{"name": "explodes", "args": {}, "id": "e"}]), _config())
    msg = _by_id(result)["e"]
    assert msg.status == "error"
    assert "kaboom in the coroutine" in msg.content
    assert "sync invocation" not in msg.content


def test_hook_sandwich_on_the_sync_path_runs_a_coroutine_only_tool():
    """The hooked dispatch path calls the node's executor directly rather
    than the parent's runner; the bridge must sit under both."""
    import importlib

    from nymeria.core.hooks import HookEvent, PostToolOutcome

    dmod = importlib.import_module("nymeria.core.hooks.dispatch")
    dmod.reset()
    try:
        dmod.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(additional_context="[hooked]"))
        node = SafeToolNode([echo_async], tool_timeout=5)
        result = node.invoke(_msg([{"name": "echo_async", "args": {"text": "h"}, "id": "c1"}]), _config())
        assert _by_id(result)["c1"].content == "async:h\n\n[hooked]"
    finally:
        dmod.reset()


def test_a_dual_mode_tool_still_takes_its_sync_body():
    """The predicate is the safety boundary: MCP wrappers, custom tools and
    slash_command bind both entry points and must keep the sync one."""
    from langchain_core.tools import StructuredTool

    def sync_body(text: str) -> str:
        return f"sync-body:{text}"

    async def async_body(text: str) -> str:
        return f"async-body:{text}"

    dual = StructuredTool.from_function(
        func=sync_body, coroutine=async_body, name="dual", description="both bodies"
    )
    node = SafeToolNode([dual], tool_timeout=5)
    result = node.invoke(_msg([{"name": "dual", "args": {"text": "x"}, "id": "d"}]), _config())
    assert _by_id(result)["d"].content == "sync-body:x"


def test_work_a_coroutine_only_tool_leaves_behind_survives_the_call():
    """asyncio.run would cancel it: the credential prompt's device-code poller
    and the browser login session's future outlive their tool call."""
    import threading

    landed = threading.Event()

    @tool
    async def fire_and_forget(x: str = "") -> str:
        """Schedules background work and returns at once."""

        async def _later():
            await asyncio.sleep(0.05)
            landed.set()

        asyncio.get_running_loop().create_task(_later())
        return "dispatched"

    node = SafeToolNode([fire_and_forget], tool_timeout=5)
    result = node.invoke(_msg([{"name": "fire_and_forget", "args": {}, "id": "f"}]), _config())
    assert _by_id(result)["f"].content == "dispatched"
    assert landed.wait(2.0), "the background task was cancelled with the call"


def test_a_hung_coroutine_only_tool_is_cancelled_not_abandoned():
    """The timeout message must not leave the coroutine running on unobserved."""
    import threading

    cancelled = threading.Event()

    @tool
    async def hangs_then_notices(x: str = "") -> str:
        """Sleeps past the timeout; records its cancellation."""
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "should-not-finish"

    node = SafeToolNode([hangs_then_notices], tool_timeout=1)
    result = node.invoke(_msg([{"name": "hangs_then_notices", "args": {}, "id": "h"}]), _config())
    assert "timed out" in _by_id(result)["h"].content
    assert cancelled.wait(3.0), "the coroutine kept running after the timeout"


def test_the_real_tool_invoke_runs_on_the_sync_path_and_its_observe_hooks_fire(monkeypatch):
    """The live #390 failure: tool_invoke is TRANSPORT (hook-skip route) and
    coroutine-only. Its inner envelope schedules observe hooks as loop tasks,
    which a per-call loop would cancel unfired."""
    import importlib
    import threading

    from nymeria.core.hooks import HookEvent

    ti = importlib.import_module("nymeria.tools.tool_invoke")

    dmod = importlib.import_module("nymeria.core.hooks.dispatch")
    ti_tests = importlib.import_module("tests.test_tool_invoke")
    agent = ti_tests._FakeAgent([ti_tests.ti_stub_add])
    ti_tests._wire(monkeypatch, agent)
    fired = threading.Event()

    async def observer(ctx):
        # Yield first: a per-call loop cancels the task here, so an observer
        # that fires before its first await would pass on the broken bridge.
        await asyncio.sleep(0.05)
        fired.set()

    dmod.reset()
    try:
        dmod.register(HookEvent.POST_TOOL_USE, observer, observe=True)
        node = SafeToolNode([ti.tool_invoke], tool_timeout=10)
        result = node.invoke(
            _msg([{
                "name": "tool_invoke",
                "args": {"name": "ti_stub_add", "arguments": {"a": 2, "b": 5}},
                "id": "ti",
            }]),
            _config(),
        )
        assert _by_id(result)["ti"].content == "7"
        assert fired.wait(3.0), "the observe hook scheduled inside the tool never ran"
    finally:
        dmod.reset()


@pytest.mark.asyncio
async def test_from_a_thread_with_a_running_loop_the_refusal_stays_honest():
    """Unreachable through node.invoke (every sync dispatch runs the call on a
    pool worker), so the executor is hit directly: it must refuse rather than
    block the live loop on the bridge loop."""
    from langgraph.prebuilt.tool_node import ToolCallRequest

    node = SafeToolNode([echo_async], tool_timeout=5)
    call = {"name": "echo_async", "args": {"text": "a"}, "id": "c1", "type": "tool_call"}
    request = ToolCallRequest(tool_call=call, tool=echo_async, state={"messages": []}, runtime=None)
    msg = node._execute_tool_sync(request, "dict", _config())
    assert msg.status == "error" and "sync invocation" in msg.content
