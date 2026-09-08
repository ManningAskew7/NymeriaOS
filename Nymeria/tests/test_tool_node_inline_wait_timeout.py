"""SafeToolNode's per-call kill honours a tool's declared inline wait.

A callable-thread call with ``wait_seconds``, ``wait_for_reply``, and
``spawn_thread(prompt=, wait_seconds=)`` wait a caller-settable time for
ANOTHER thread's reply and return before that wait lapses. The node must not
kill them at ``tool_timeout``: a tool whose ``metadata["inline_wait_timeout"]``
callable returns a kill timeout for the call's arguments gets that timeout;
every other tool keeps ``tool_timeout``. Written from behaviour 16 of
``tmp/request-reply-plan.md``.

Same harness as the other tool-node tests: ``DEFAULT_RUNTIME`` injected
through ``config[configurable][__pregel_runtime]``.
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


def _config(thread_id: str = "t-wait", *, sequential: bool = False):
    configurable: dict = {
        CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
        "thread_id": thread_id,
        "user_id": "u-wait",
    }
    if sequential:
        configurable["sequential_tools"] = True
    return {"configurable": configurable}


def _msg(tool_calls):
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def _by_id(result):
    return {m.tool_call_id: m for m in result["messages"]}


def _sync_tools():
    @tool
    def waits(x: str = "") -> str:
        """Blocks past tool_timeout, as a call waiting on another thread does."""
        time.sleep(2.5)
        return "answered"

    @tool
    def plain(x: str = "") -> str:
        """Blocks past tool_timeout with no declared inline wait. It finishes
        BEFORE the waiting sibling on purpose: a node that judged calls in
        index order would find it already done when it got round to it and
        return its text; a per-call deadline kills it at tool_timeout."""
        time.sleep(2.0)
        return "should-not-finish"

    waits.metadata = {"inline_wait_timeout": lambda args: 6.0}
    return waits, plain


@pytest.mark.parametrize("sequential", [False, True])
def test_declared_inline_wait_outlives_tool_timeout_while_a_plain_tool_is_killed(sequential):
    waits, plain = _sync_tools()
    node = SafeToolNode([waits, plain], tool_timeout=1)
    calls = [
        {"name": "waits", "args": {}, "id": "a"},
        {"name": "plain", "args": {}, "id": "b"},
    ]

    result = node.invoke(_msg(calls), _config(sequential=sequential))

    by_id = _by_id(result)
    assert by_id["a"].content == "answered"
    assert "[Error]" in by_id["b"].content and "timed out after 1 seconds" in by_id["b"].content
    assert by_id["b"].status == "error"


def test_async_path_honours_the_declared_inline_wait():
    @tool
    async def waits(x: str = "") -> str:
        """Blocks past tool_timeout."""
        await asyncio.sleep(2.5)
        return "answered"

    @tool
    async def plain(x: str = "") -> str:
        """Blocks the same time, undeclared."""
        await asyncio.sleep(2.5)
        return "should-not-finish"

    waits.metadata = {"inline_wait_timeout": lambda args: 6.0}
    node = SafeToolNode([waits, plain], tool_timeout=1)
    calls = [
        {"name": "waits", "args": {}, "id": "a"},
        {"name": "plain", "args": {}, "id": "b"},
    ]

    result = asyncio.run(node.ainvoke(_msg(calls), _config()))

    by_id = _by_id(result)
    assert by_id["a"].content == "answered"
    assert "timed out after 1 seconds" in by_id["b"].content


def test_kill_timeout_is_derived_from_the_calls_own_arguments():
    seen_args: list[dict] = []

    @tool
    def waits(timeout_seconds: int = 0) -> str:
        """Records nothing; the derivation is what is under test."""
        return "ok"

    def derive(args):
        seen_args.append(dict(args))
        return float(args.get("timeout_seconds") or 0) + 60.0

    waits.metadata = {"inline_wait_timeout": derive}

    @tool
    def plain(x: str = "") -> str:
        """No declaration."""
        return "ok"

    node = SafeToolNode([waits, plain], tool_timeout=300)

    assert node._call_timeout({"name": "waits", "args": {"timeout_seconds": 7000}, "id": "a"}) == 7060.0
    assert seen_args == [{"timeout_seconds": 7000}]
    # A declared wait under tool_timeout never lowers the kill.
    assert node._call_timeout({"name": "waits", "args": {"timeout_seconds": 10}, "id": "a"}) == 300.0
    assert node._call_timeout({"name": "plain", "args": {}, "id": "b"}) == 300.0
    assert node._call_timeout({"name": "unknown-tool", "args": {}, "id": "c"}) == 300.0


def test_declaration_returning_none_or_raising_falls_back_to_tool_timeout():
    @tool
    def none_tool(x: str = "") -> str:
        """Declares, but this call is not a wait."""
        return "ok"

    @tool
    def raising_tool(x: str = "") -> str:
        """Its derivation is broken."""
        return "ok"

    none_tool.metadata = {"inline_wait_timeout": lambda args: None}

    def boom(args):
        raise ValueError("bad args")

    raising_tool.metadata = {"inline_wait_timeout": boom}
    node = SafeToolNode([none_tool, raising_tool], tool_timeout=45)

    assert node._call_timeout({"name": "none_tool", "args": {}, "id": "a"}) == 45.0
    assert node._call_timeout({"name": "raising_tool", "args": {}, "id": "b"}) == 45.0


def test_timeout_message_names_the_effective_seconds():
    @tool
    def waits(x: str = "") -> str:
        """Outlives even its declared wait."""
        time.sleep(4.0)
        return "never"

    waits.metadata = {"inline_wait_timeout": lambda args: 2.0}
    node = SafeToolNode([waits], tool_timeout=1)

    result = node.invoke(_msg([{"name": "waits", "args": {}, "id": "a"}]), _config())

    content = _by_id(result)["a"].content
    assert "timed out after 2 seconds" in content


def test_the_real_tools_declare_their_waits():
    """The production tools carry the declaration the node reads."""
    from nymeria.agents.tool_factory import create_callable_thread_tool
    from nymeria.core.thread_config import ThreadConfig
    from nymeria.tools.spawn_thread import spawn_thread
    from nymeria.tools.thread_requests import wait_for_reply

    callable_tool = create_callable_thread_tool(
        ThreadConfig(thread_id="t", callable=True, callable_name="Helper")
    )
    node = SafeToolNode([callable_tool, wait_for_reply, spawn_thread], tool_timeout=300)

    # A waiting call is killed at its wait plus the margin, never at tool_timeout.
    assert node._call_timeout({"name": "Helper", "args": {"task": "x", "wait_seconds": 500}, "id": "a"}) == 560.0
    assert node._call_timeout({"name": "wait_for_reply", "args": {"request_id": "r", "timeout_seconds": 400}, "id": "b"}) == 460.0
    assert node._call_timeout({"name": "spawn_thread", "args": {"title": "t", "prompt": "p", "wait_seconds": 500}, "id": "c"}) == 560.0
    # Calls that do not wait keep the plain tool_timeout.
    assert node._call_timeout({"name": "Helper", "args": {"task": "x"}, "id": "d"}) == 300.0
    assert node._call_timeout({"name": "wait_for_reply", "args": {"request_id": "r"}, "id": "e"}) == 300.0
    assert node._call_timeout({"name": "spawn_thread", "args": {"title": "t"}, "id": "f"}) == 300.0
