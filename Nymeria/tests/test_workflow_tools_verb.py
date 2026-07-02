"""Tests for the ``nym.tools.*`` dispatch primitive: gates, lookup, invoke.

Unit-level around ``dispatch_tool_by_name`` using the module's monkeypatch
seams (``_caller_role``, ``_find_tool``, ``_current_agent``), plus one
end-to-end run through a real child subprocess with a stub tool.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.tools import InjectedToolCallId, tool

from nymeria.core.workflows import WorkflowBudget, execute_workflow
from nymeria.core.workflows.registry import VerbError
from nymeria.core.workflows import verbs_tools

pytestmark = pytest.mark.asyncio


@tool
def wf_stub_add(a: int, b: int) -> str:
    """Add two numbers (workflow test stub)."""
    return str(a + b)


@tool
def wf_stub_whoami(config_probe: str = "") -> str:
    """Echo back (workflow test stub)."""
    return f"probe:{config_probe}"


async def _dispatch(tool_name: str, args: dict, *, role: str = "user", found=None):
    return await verbs_tools.dispatch_tool_by_name(
        agent=object(),
        user_id="tester",
        thread_id="t",
        tool_name=tool_name,
        args=args,
        tool_call_id="wf_test_1",
    )


async def test_denylist_blocks_management_tools(monkeypatch):
    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "admin")
    with pytest.raises(VerbError) as excinfo:
        await _dispatch("tool_create", {})
    assert "protected management tool" in str(excinfo.value)


async def test_admin_only_tool_blocked_for_user_role(monkeypatch):
    from nymeria.tools import ADMIN_ONLY_TOOL_NAMES

    admin_tool = sorted(ADMIN_ONLY_TOOL_NAMES)[0]
    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    with pytest.raises(VerbError) as excinfo:
        await _dispatch(admin_tool, {})
    assert "admin-only" in str(excinfo.value)


async def test_unknown_tool_reports_not_found(monkeypatch):
    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    monkeypatch.setattr(
        verbs_tools, "_find_tool", lambda agent, u, t, name: None
    )
    with pytest.raises(VerbError) as excinfo:
        await _dispatch("no_such_tool", {})
    assert "not enabled" in str(excinfo.value)


async def test_dispatch_invokes_tool_with_caller_config(monkeypatch):
    seen = {}

    @tool
    def wf_stub_ctx(x: int) -> str:
        """Stub that proves invocation happened."""
        seen["x"] = x
        return f"got {x}"

    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    monkeypatch.setattr(
        verbs_tools, "_find_tool", lambda agent, u, t, name: wf_stub_ctx
    )
    result = await _dispatch("wf_stub_ctx", {"x": 7})
    assert result == "got 7"
    assert seen == {"x": 7}


@tool
def wf_stub_cmd(
    x: int,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Stub that requires an injected tool_call_id."""
    _CMD_CAPTURE["tcid"] = tool_call_id
    return f"id={tool_call_id}"


_CMD_CAPTURE: dict = {}


async def test_injected_tool_call_id_supplied_preemptively(monkeypatch):
    captured = _CMD_CAPTURE

    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    monkeypatch.setattr(
        verbs_tools, "_find_tool", lambda agent, u, t, name: wf_stub_cmd
    )
    result = await verbs_tools.dispatch_tool_by_name(
        agent=object(),
        user_id="tester",
        thread_id="t",
        tool_name="wf_stub_cmd",
        args={"x": 1},
        tool_call_id="wf_synth_42",
    )
    assert result == "id=wf_synth_42"
    assert captured["tcid"] == "wf_synth_42"


async def test_tool_without_injected_id_gets_no_extra_arg(monkeypatch):
    seen = {}

    @tool
    def wf_stub_plain(x: int) -> str:
        """Stub that takes no tool_call_id."""
        seen["keys"] = "ok"
        return str(x)

    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    monkeypatch.setattr(
        verbs_tools, "_find_tool", lambda agent, u, t, name: wf_stub_plain
    )
    # A plain tool must not receive an unexpected tool_call_id kwarg.
    result = await verbs_tools.dispatch_tool_by_name(
        agent=object(),
        user_id="tester",
        thread_id="t",
        tool_name="wf_stub_plain",
        args={"x": 9},
        tool_call_id="wf_synth_1",
    )
    assert result == "9"
    assert seen == {"keys": "ok"}


async def test_tool_failure_normalizes_to_verb_error(monkeypatch):
    @tool
    def wf_stub_fail(x: int) -> str:
        """Stub that always fails."""
        raise RuntimeError("downstream API 500")

    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    monkeypatch.setattr(
        verbs_tools, "_find_tool", lambda agent, u, t, name: wf_stub_fail
    )
    with pytest.raises(VerbError) as excinfo:
        await _dispatch("wf_stub_fail", {"x": 1})
    assert "downstream API 500" in str(excinfo.value)


async def test_end_to_end_child_calls_stub_tool(monkeypatch):
    monkeypatch.setattr(verbs_tools, "_caller_role", lambda user_id: "user")
    monkeypatch.setattr(verbs_tools, "_current_agent", lambda: object())
    monkeypatch.setattr(
        verbs_tools, "_find_tool", lambda agent, u, t, name: wf_stub_add
    )
    source = (
        "def run():\n"
        "    return {'sum': nym.tools.wf_stub_add(a=2, b=3)}\n"
    )
    result = await execute_workflow(
        source=source,
        user_id="tester",
        thread_id="t",
        budget=WorkflowBudget(wall_clock_seconds=30.0),
        persist_record=False,
    )
    assert result.envelope.status == "ok"
    assert result.envelope.output == {"sum": "5"}
    assert result.trace.steps[0].verb == "tools.wf_stub_add"


async def test_end_to_end_no_agent_runtime_is_verb_error(monkeypatch):
    monkeypatch.setattr(verbs_tools, "_current_agent", lambda: None)
    source = "def run():\n    nym.tools.anything(x=1)\n"
    result = await execute_workflow(
        source=source,
        user_id="tester",
        thread_id="t",
        budget=WorkflowBudget(wall_clock_seconds=30.0),
        persist_record=False,
    )
    assert result.envelope.status == "error"
    assert result.envelope.error is not None
    assert result.envelope.error.kind == "verb_error"
    assert "no agent runtime" in result.envelope.error.message
