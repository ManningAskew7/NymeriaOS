"""Tests for the ``tool_invoke`` deferred-execution meta-tool and schema render.

Unit-level around ``_tool_invoke_impl`` using the module's seams
(``current_agent``, ``caller_role``, ``get_thread_id``/``get_user_id``, and the
``verbs_tools`` gate/invoke primitives), plus the compact schema renderer.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.tools import InjectedToolCallId, tool

import sys

import nymeria.tools.tool_invoke  # noqa: F401 - ensure submodule import

# The package re-exports the tool object under the name ``tool_invoke``, which
# shadows the submodule attribute; reach the module itself for monkeypatching.
ti = sys.modules["nymeria.tools.tool_invoke"]

pytestmark = pytest.mark.asyncio


# --- fakes ---------------------------------------------------------------


class _FakeTC:
    def __init__(self, disabled=None):
        self.disabled_tools = list(disabled or [])


class _FakeTCM:
    def __init__(self, tc):
        self._tc = tc
        self.saves = 0

    def get_config(self, thread_id):
        return self._tc

    def save_config(self, tc):  # spy: tool_invoke must never write config
        self.saves += 1


class _FakeAgent:
    def __init__(self, tools, disabled=None):
        self._tools = list(tools)
        self.thread_config_manager = _FakeTCM(_FakeTC(disabled))

    def _compute_tool_superset(self, user_id, thread_id):
        return list(self._tools), {t.name for t in self._tools}


@tool
def ti_stub_add(a: int, b: int) -> str:
    """Add two numbers (tool_invoke test stub)."""
    return str(a + b)


_CMD_CAPTURE: dict = {}


@tool
def ti_stub_cmd(x: int, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
    """Stub requiring an injected tool_call_id (tool_invoke test)."""
    _CMD_CAPTURE["tcid"] = tool_call_id
    return f"id={tool_call_id}"


def _wire(monkeypatch, agent, *, role="user"):
    monkeypatch.setattr(ti, "current_agent", lambda: agent)
    monkeypatch.setattr(ti, "caller_role", lambda user_id: role)
    monkeypatch.setattr(ti, "get_thread_id", lambda config: "t")
    monkeypatch.setattr(ti, "get_user_id", lambda config: "tester")


async def _invoke(name, arguments=None):
    return await ti._tool_invoke_impl(
        name, arguments, tool_call_id="ti_test_1", config={}
    )


# --- happy path + config invariant --------------------------------------


async def test_dispatches_unbound_tool_and_never_writes_config(monkeypatch):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_add", {"a": 2, "b": 5})
    assert result == "7"
    assert agent.thread_config_manager.saves == 0
    assert agent.thread_config_manager._tc.disabled_tools == []


async def test_arguments_accepts_json_string(monkeypatch):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_add", '{"a": 3, "b": 4}')
    assert result == "7"


async def test_injected_tool_call_id_synthesized(monkeypatch):
    _CMD_CAPTURE.clear()
    agent = _FakeAgent([ti_stub_cmd])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_cmd", {"x": 1})
    assert result == "id=ti_test_1"
    assert _CMD_CAPTURE["tcid"] == "ti_test_1"


# --- gates ---------------------------------------------------------------


async def test_denylist_blocks_management_tool(monkeypatch):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent, role="admin")
    result = await _invoke("tool_create", {})
    assert "tool_invoke error" in result
    assert "protected management tool" in result


async def test_admin_only_blocked_for_user(monkeypatch):
    from nymeria.tools import ADMIN_ONLY_TOOL_NAMES

    admin_tool = sorted(ADMIN_ONLY_TOOL_NAMES)[0]
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent, role="user")
    result = await _invoke(admin_tool, {})
    assert "tool_invoke error" in result
    assert "admin-only" in result


async def test_disabled_tool_blocked(monkeypatch):
    agent = _FakeAgent([ti_stub_add], disabled=["ti_stub_add"])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_add", {"a": 1, "b": 1})
    assert "tool_invoke error" in result
    assert "disabled" in result


@pytest.mark.parametrize(
    "name",
    [
        "tool_invoke",
        "Skill",
        "run_tools_in_order",
        # Tool-system mutators: their whole job is to bind + reload, so they
        # belong on the binding surface, never the one-off deferred path.
        "install_skill",
        "install_mcp_server",
    ],
)
async def test_excluded_tools_refused(monkeypatch, name):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent)
    result = await _invoke(name, {})
    assert "cannot be called through the deferred path" in result


async def test_unknown_tool_reports_not_available(monkeypatch):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent)
    result = await _invoke("no_such_tool", {})
    assert "tool_invoke error" in result
    assert "no tool named" in result


async def test_empty_name_errors(monkeypatch):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent)
    result = await _invoke("", {})
    assert "name is required" in result


async def test_no_agent_runtime(monkeypatch):
    _wire(monkeypatch, None)
    result = await _invoke("ti_stub_add", {})
    assert "no agent runtime" in result


# --- validation echo -----------------------------------------------------


async def test_validation_error_echoes_schema(monkeypatch):
    agent = _FakeAgent([ti_stub_add])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_add", {"a": "not-an-int", "b": 1})
    assert "rejected the arguments" in result
    assert "Expected arguments" in result
    # the echoed schema names the real fields
    assert '"a"' in result and '"b"' in result


@tool
def ti_stub_body_valerr(a: int) -> str:
    """Stub whose ARGS are fine but whose BODY raises a pydantic ValidationError."""
    from pydantic import BaseModel

    class _Upstream(BaseModel):
        n: int

    _Upstream.model_validate({"n": "not-an-int"})  # body-side validation failure
    return "unreachable"


async def test_body_validation_error_not_misread_as_bad_args(monkeypatch):
    # A ValidationError raised INSIDE the tool (e.g. validating an upstream API
    # response) must surface as a generic tool failure, not the misleading
    # "fix your arguments + here's the input schema" echo that would loop the model.
    agent = _FakeAgent([ti_stub_body_valerr])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_body_valerr", {"a": 1})
    assert "failed:" in result
    assert "rejected the arguments" not in result
    assert "Expected arguments" not in result
    assert "Fix the arguments" not in result


@tool
def ti_stub_returns_command(x: int) -> object:
    """Stub returning a LangGraph Command (a graph-control directive)."""
    from langgraph.types import Command

    return Command(update={})


async def test_command_result_refused_with_bind_hint(monkeypatch):
    # A tool that returns a graph-control Command cannot be delivered through the
    # deferred path; refuse clearly and point at binding rather than stringifying.
    agent = _FakeAgent([ti_stub_returns_command])
    _wire(monkeypatch, agent)
    result = await _invoke("ti_stub_returns_command", {"x": 1})
    assert "graph-control directive" in result
    assert "tool_manage" in result


# --- workflow / registry-backed target -----------------------------------


async def test_unbound_registry_tool_reachable_and_gate_refusal_passes_through(monkeypatch):
    """A published workflow is a registry-backed tool reachable via tool_invoke
    without binding; its own gate refusal (unapproved revision) must pass through
    verbatim, not be swallowed or rewrapped. (The real revision gate itself is
    covered by tests/test_workflow_tool_lifecycle.py.)"""

    @tool
    async def wf_like_tool(topic: str) -> str:
        """Workflow-like registry tool that refuses on an unapproved revision."""
        return "[Workflow blocked]: revision not approved; an admin must approve it."

    agent = _FakeAgent([wf_like_tool])
    _wire(monkeypatch, agent)
    result = await _invoke("wf_like_tool", {"topic": "x"})
    assert result == "[Workflow blocked]: revision not approved; an admin must approve it."
