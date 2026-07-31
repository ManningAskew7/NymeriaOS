"""Tests for the shared tool execution envelope (``core/tool_execution.py``).

The property under test throughout: a tool call is subject to the same gate and
the same lifecycle hooks no matter which spelling reached it. Before the
envelope, only a bound call fired hooks, so ``tool_invoke(name="bash_execute")``
walked past a ``require_approval`` or ``block_if_matches`` hook on
``bash_execute`` in one call with nothing disabled.

Each test here is written to fail if the mechanism it covers is removed, not
merely to describe current behaviour.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from langchain_core.tools import tool

import nymeria.tools.tool_invoke  # noqa: F401 - ensure submodule import
from nymeria.core import tool_execution
from nymeria.core.hooks import HookEvent, PostToolOutcome, PreToolOutcome
from nymeria.core.tool_execution import (
    BY_NAME_EXCLUDED_TOOL_NAMES,
    TRANSPORT_TOOL_NAMES,
    ToolDenied,
    by_name_gate_reason,
    by_name_tool_config,
)
from nymeria.tools.tool_search import PROTECTED_MANAGEMENT_TOOL_NAMES
from nymeria.vendor.react_agent.nodes import SafeToolNode

# The packages re-export ``tool_invoke`` and ``dispatch`` as attributes, which
# shadow the submodules of the same name; reach the modules themselves.
ti = sys.modules["nymeria.tools.tool_invoke"]
dmod = importlib.import_module("nymeria.core.hooks.dispatch")

# No module-level asyncio mark: this file mixes async dispatch tests with sync
# gate and config-builder tests, and a blanket mark warns on every sync one.


# --- fakes ---------------------------------------------------------------


class _FakeTC:
    def __init__(self, disabled=None):
        self.disabled_tools = list(disabled or [])


class _FakeTCM:
    def __init__(self, tc):
        self._tc = tc

    def get_config(self, thread_id):
        return self._tc


class _FakeAgent:
    def __init__(self, tools=(), disabled=None):
        self._tools = list(tools)
        self.thread_config_manager = _FakeTCM(_FakeTC(disabled))

    def _compute_tool_superset(self, user_id, thread_id):
        return list(self._tools), {t.name for t in self._tools}

    def _select_tools_for_graph(self, user_id, thread_id):
        return list(self._tools), self.thread_config_manager.get_config(thread_id)


@tool
def env_stub_add(a: int, b: int) -> str:
    """Add two numbers (envelope test stub)."""
    return str(a + b)


def _wire(monkeypatch, agent, *, role="user"):
    monkeypatch.setattr(ti, "current_agent", lambda: agent)
    monkeypatch.setattr(ti, "caller_role", lambda user_id: role)
    monkeypatch.setattr(ti, "get_thread_id", lambda config: "t")
    monkeypatch.setattr(ti, "get_user_id", lambda config: "tester")


async def _invoke(name, arguments=None, config=None):
    return await ti._tool_invoke_impl(
        name, arguments, tool_call_id="env_test_1", config=config if config is not None else {}
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    dmod.reset()
    yield
    dmod.reset()


# --- the bypass this module exists to close ------------------------------


@pytest.mark.asyncio
async def test_deferred_call_fires_pre_hook_under_the_target_name(monkeypatch):
    """A hook matching the TARGET sees the deferred spelling of the call.

    The regression this closes: a hook scoped to ``bash_execute`` never saw
    ``tool_invoke(name="bash_execute")``, because the only fire point was the
    graph tool node and the node only knew the name ``tool_invoke``.
    """
    seen: list = []

    def _spy(ctx):
        seen.append((ctx.tool_name, dict(ctx.tool_args or {})))
        return None

    dmod.register(HookEvent.PRE_TOOL_USE, _spy, name="spy")
    agent = _FakeAgent([env_stub_add])
    _wire(monkeypatch, agent)

    result = await _invoke("env_stub_add", {"a": 1, "b": 2})

    assert result == "3"
    assert seen == [("env_stub_add", {"a": 1, "b": 2})]


@pytest.mark.asyncio
async def test_deferred_call_denied_by_hook_returns_structured_error(monkeypatch):
    """A PRE veto refuses the deferred call and the turn continues.

    Structured error text, not a raised exception: the model has to be able to
    read the refusal and do something else.
    """
    dmod.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: PreToolOutcome(decision="deny", reason="not on my watch"),
        matcher="env_stub_add",
        name="guard",
    )
    agent = _FakeAgent([env_stub_add])
    _wire(monkeypatch, agent)

    result = await _invoke("env_stub_add", {"a": 1, "b": 2})

    assert "blocked" in result
    assert "not on my watch" in result
    assert "3" not in result


@pytest.mark.asyncio
async def test_deferred_call_hook_modify_reaches_the_tool(monkeypatch):
    """A PRE ``modify`` rewrites the args the target actually runs with.

    Guards the closure bug this shape invites: computing the effective call and
    then invoking with the original args captured in the enclosing scope.
    """
    dmod.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: PreToolOutcome(decision="modify", updated_args={"b": 40}),
        matcher="env_stub_add",
        name="rewriter",
    )
    agent = _FakeAgent([env_stub_add])
    _wire(monkeypatch, agent)

    assert await _invoke("env_stub_add", {"a": 2, "b": 5}) == "42"


@pytest.mark.asyncio
async def test_deferred_call_post_hook_rewrites_the_returned_text(monkeypatch):
    """A POST rewrite reaches a by-name result, which is a bare string.

    The envelope is polymorphic over ToolMessage and str precisely so this is
    not silently inert on the deferred path.
    """
    dmod.register(
        HookEvent.POST_TOOL_USE,
        lambda ctx: PostToolOutcome(updated_result_text="REDACTED"),
        name="redactor",
    )
    agent = _FakeAgent([env_stub_add])
    _wire(monkeypatch, agent)

    assert await _invoke("env_stub_add", {"a": 1, "b": 2}) == "REDACTED"


@pytest.mark.asyncio
async def test_post_hook_sees_the_by_name_result_text(monkeypatch):
    """POST context carries the target's own result, not a repr of a wrapper."""
    seen: list = []

    def _spy(ctx):
        seen.append((ctx.tool_name, ctx.tool_result_text))
        return None

    dmod.register(HookEvent.POST_TOOL_USE, _spy, name="spy")
    agent = _FakeAgent([env_stub_add])
    _wire(monkeypatch, agent)

    await _invoke("env_stub_add", {"a": 3, "b": 4})

    assert seen == [("env_stub_add", "7")]


# --- transport suppression: seen once, under the real name ---------------


@pytest.mark.parametrize("transport_name", sorted(TRANSPORT_TOOL_NAMES))
def test_the_node_skips_its_fire_for_every_transport_tool(transport_name):
    """The node does not fire for a transport tool, whatever hooks are active.

    Without this, a matcher-less (fire-on-everything) hook fires twice for one
    action, and a ``require_approval`` prompts the human twice for one call.
    Parametrised over the set so adding a member cannot skip the check.
    """
    dmod.register(HookEvent.PRE_TOOL_USE, lambda ctx: None, name="spy")
    registry = dmod.default_registry
    assert dmod.tool_hooks_active(registry) is True

    assert SafeToolNode._skip_hook_envelope({"name": transport_name}, registry, dmod) is True
    # A non-transport tool with hooks active still takes the envelope.
    assert SafeToolNode._skip_hook_envelope({"name": "env_stub_add"}, registry, dmod) is False


def test_every_transport_tool_is_excluded_from_by_name_dispatch():
    """Transport tools cannot be reached BY name either.

    The two sets answer different questions but a transport tool must be in
    both: suppressing its outer fire while letting a workflow dispatch it by
    name would give an unfired, unbounded recursion.
    """
    # Non-emptiness first: an emptied TRANSPORT_TOOL_NAMES would satisfy the
    # subset relation vacuously while silently disarming node suppression.
    assert TRANSPORT_TOOL_NAMES
    assert TRANSPORT_TOOL_NAMES <= BY_NAME_EXCLUDED_TOOL_NAMES


# --- the gate ------------------------------------------------------------


def test_gate_refuses_protected_management_tools():
    agent = _FakeAgent()
    for name in sorted(PROTECTED_MANAGEMENT_TOOL_NAMES):
        reason = by_name_gate_reason(agent, name, "u", "t", "admin")
        assert reason and "protected management tool" in reason, name


def test_gate_refuses_admin_only_tools_for_a_non_admin():
    from nymeria.tools import ADMIN_ONLY_TOOL_NAMES

    agent = _FakeAgent()
    candidates = sorted(ADMIN_ONLY_TOOL_NAMES - PROTECTED_MANAGEMENT_TOOL_NAMES)
    assert candidates, "no admin-only tool outside the protected set to test with"
    name = candidates[0]
    assert by_name_gate_reason(agent, name, "u", "t", "user")
    assert by_name_gate_reason(agent, name, "u", "t", "admin") is None


def test_gate_refuses_thread_disabled_tools():
    agent = _FakeAgent(disabled=["env_stub_add"])
    reason = by_name_gate_reason(agent, "env_stub_add", "u", "t", "admin")
    assert reason and "disabled on this thread" in reason


def test_gate_allowlist_arm_is_inert_by_default():
    """The seam ships returning None, so no thread is narrowed today."""
    agent = _FakeAgent()
    assert tool_execution.tool_allowlist(agent, "u", "t") is None
    assert by_name_gate_reason(agent, "env_stub_add", "u", "t", "admin") is None


def test_gate_allowlist_arm_refuses_a_tool_outside_the_set(monkeypatch):
    """The positive arm is real code, not a comment promising one.

    ``disabled_tools`` is a denylist, so "this thread may use only X" is
    inexpressible without this. The future per-tool permission model and the
    security profiles both hang off this seam.
    """
    agent = _FakeAgent()
    monkeypatch.setattr(
        tool_execution, "tool_allowlist", lambda a, u, t: frozenset({"other_tool"})
    )
    reason = by_name_gate_reason(agent, "env_stub_add", "u", "t", "admin")
    assert reason and "not in this thread's allowed tool set" in reason
    assert by_name_gate_reason(agent, "other_tool", "u", "t", "admin") is None


def test_gate_allowlist_distinguishes_empty_from_absent(monkeypatch):
    """``frozenset()`` permits nothing; ``None`` means no allowlist at all."""
    agent = _FakeAgent()
    monkeypatch.setattr(tool_execution, "tool_allowlist", lambda a, u, t: frozenset())
    assert by_name_gate_reason(agent, "env_stub_add", "u", "t", "admin")


@pytest.mark.asyncio
async def test_deferred_path_enforces_the_allowlist(monkeypatch):
    """The arm is wired into the live deferred path, not just callable directly."""
    agent = _FakeAgent([env_stub_add])
    _wire(monkeypatch, agent)
    monkeypatch.setattr(
        tool_execution, "tool_allowlist", lambda a, u, t: frozenset({"something_else"})
    )
    result = await _invoke("env_stub_add", {"a": 1, "b": 2})
    assert "not in this thread's allowed tool set" in result


# --- config inheritance --------------------------------------------------


def test_by_name_config_inherits_the_turn_hook_registry():
    """The bug fix behind the module: the turn config must not be discarded.

    langchain's ``ensure_config`` replaces ``configurable`` wholesale rather
    than merging, so building a fresh three-key dict deleted the turn's hook
    registry. That discard, not a missing lookup, is why by-name calls were
    invisible to hooks.
    """
    sentinel = object()
    base = {
        "configurable": {
            "user_id": "someone-else",
            "thread_id": "other-thread",
            "hook_registry": sentinel,
            "hook_holder_kind": "trigger",
            "hook_is_autonomous": True,
        }
    }
    built = by_name_tool_config(user_id="u", thread_id="t", base=base)["configurable"]

    assert built["hook_registry"] is sentinel
    assert built["hook_holder_kind"] == "trigger"
    assert built["hook_is_autonomous"] is True
    # Identity is the CALLER's, never inherited: a by-name call must not be able
    # to borrow whoever the base config belonged to.
    assert built["user_id"] == "u"
    assert built["thread_id"] == "t"


def test_by_name_config_resolves_a_registry_from_the_agent_when_there_is_no_base():
    """A workflow verb has no turn config, so the registry comes from the agent.

    Without this a workflow-dispatched tool would fall back to the module-global
    default registry, which is empty in production: the hooks would look wired
    and never fire.
    """
    sentinel = object()

    class _AgentWithHooks(_FakeAgent):
        def _hook_registry_for_turn(self, thread_id, user_id):
            return sentinel

    built = by_name_tool_config(
        user_id="u", thread_id="t", agent=_AgentWithHooks()
    )["configurable"]
    assert built["hook_registry"] is sentinel


def test_by_name_config_survives_a_failing_registry_lookup():
    """A hook lookup that raises must not take the tool call down with it."""

    class _AngryAgent(_FakeAgent):
        def _hook_registry_for_turn(self, thread_id, user_id):
            raise RuntimeError("accounts repo down")

    built = by_name_tool_config(user_id="u", thread_id="t", agent=_AngryAgent())
    assert built["configurable"]["user_id"] == "u"
    assert "hook_registry" not in built["configurable"]


def test_by_name_config_does_not_inherit_arbitrary_keys():
    """Only the declared keys cross. A by-name call is not a turn."""
    base = {
        "configurable": {
            "turn_safety_max_iterations": 99,
            "allow_unbound_tool_calls": True,
            "sequential_tools": ["a"],
        }
    }
    built = by_name_tool_config(user_id="u", thread_id="t", base=base)["configurable"]
    assert set(built) == {"user_id", "thread_id", "workflow_depth"}


# --- the envelope itself -------------------------------------------------


def test_envelope_is_a_passthrough_when_no_tool_hook_is_registered():
    """The zero-hook hot path must not pay for the sandwich."""
    calls: list = []

    def _execute(call):
        calls.append(call)
        return "ok"

    out = tool_execution.run_tool_envelope(
        call={"name": "x", "args": {"a": 1}, "id": "1"}, config={}, execute=_execute
    )
    assert out == "ok"
    assert calls == [{"name": "x", "args": {"a": 1}, "id": "1"}]


def test_envelope_raises_tool_denied_on_veto():
    dmod.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: PreToolOutcome(decision="deny", reason="no"),
        name="guard",
    )
    with pytest.raises(ToolDenied) as excinfo:
        tool_execution.run_tool_envelope(
            call={"name": "x", "args": {}, "id": "1"},
            config={},
            execute=lambda call: "ran",
        )
    assert excinfo.value.reason == "no"
    assert excinfo.value.tool_name == "x"


def test_envelope_does_not_execute_a_vetoed_call():
    """The veto has to happen BEFORE the side effect, not around it."""
    ran: list = []
    dmod.register(
        HookEvent.PRE_TOOL_USE,
        lambda ctx: PreToolOutcome(decision="deny", reason="no"),
        name="guard",
    )
    with pytest.raises(ToolDenied):
        tool_execution.run_tool_envelope(
            call={"name": "x", "args": {}, "id": "1"},
            config={},
            execute=lambda call: ran.append(1),
        )
    assert ran == []


def test_post_outcome_leaves_an_unreadable_result_untouched():
    """A payload the hook cannot read passes through rather than being mangled."""
    sentinel = object()
    out = tool_execution.apply_post_tool_outcome(
        sentinel, PostToolOutcome(updated_result_text="REDACTED")
    )
    assert out is sentinel


def test_result_text_does_not_stringify_an_opaque_payload():
    """A POST hook is told "no text" rather than handed a repr to match on."""
    assert tool_execution.tool_result_text(object()) is None
    assert tool_execution.tool_result_text("plain") == "plain"


# --- self_invoke_tool: the widest by-name path, now gated ----------------


def _wire_self_invoke(monkeypatch, agent, *, role="user"):
    """Point self_invoke_tool's function-local lookups at fakes."""
    import nymeria.core.agent as agent_mod
    import nymeria.tools.utils as utils_mod

    monkeypatch.setattr(agent_mod, "get_current_agent", lambda: agent, raising=False)
    monkeypatch.setattr(utils_mod, "caller_role", lambda uid, agent=None: role)
    monkeypatch.setattr(utils_mod, "get_user_id", lambda config: "tester")
    monkeypatch.setattr(utils_mod, "get_thread_id", lambda config: "t")


def _self_invoke(name, args_json="{}"):
    from nymeria.core.self_agent import self_invoke_tool

    return self_invoke_tool.invoke(
        {"tool_name": name, "arguments_json": args_json},
        config={"configurable": {"user_id": "tester", "thread_id": "t"}},
    )


def test_self_invoke_refuses_a_protected_management_tool(monkeypatch):
    """It applied NO gate at all before: no denylist, no role gate, no disabled.

    Narrowest tool in the system by purpose (test a tool you just wrote) and the
    widest by reach, which is the shape of every finding in this pass.
    """
    _wire_self_invoke(monkeypatch, _FakeAgent([env_stub_add]), role="admin")
    out = _self_invoke("tool_manage")
    assert "protected management tool" in out


def test_self_invoke_refuses_a_thread_disabled_tool(monkeypatch):
    _wire_self_invoke(monkeypatch, _FakeAgent([env_stub_add], disabled=["env_stub_add"]))
    out = _self_invoke("env_stub_add", '{"a": 1, "b": 2}')
    assert "disabled on this thread" in out


def test_self_invoke_still_runs_a_freshly_written_tool(monkeypatch):
    """The create -> reload -> test cycle it exists for must keep working."""
    _wire_self_invoke(monkeypatch, _FakeAgent([env_stub_add]))
    assert "[Test result]: 3" == _self_invoke("env_stub_add", '{"a": 1, "b": 2}')


def test_self_invoke_resolves_from_the_superset_not_the_global_registry(monkeypatch):
    """agent.tool_registry holds EVERY user's callable threads; the superset does not.

    Resolving from the registry made cross-account reach a matter of the runtime
    ownership gate firing. Resolving from the per-user superset makes it
    structural.
    """
    calls: list = []

    class _RegistrySpy(_FakeAgent):
        def __init__(self):
            super().__init__([])
            self.tool_registry = self

        def get_tool(self, name):  # must never be consulted for resolution
            calls.append(name)
            return env_stub_add

        def list_tools(self):
            return [{"name": "env_stub_add"}]

    _wire_self_invoke(monkeypatch, _RegistrySpy())
    out = _self_invoke("env_stub_add", '{"a": 1, "b": 2}')
    assert "not found" in out
    assert calls == []


# The other half of the transport rule. Set membership alone is not enough: a
# name added to TRANSPORT_TOOL_NAMES whose tool does NOT fire its own envelope
# is a SILENT HOOK BYPASS, which is the exact bug this module exists to fix.
# Suppression at the node and the inner fire are one mechanism; pin both.
_TRANSPORT_ENVELOPE_SITES = {
    "tool_invoke": ("nymeria.tools.tool_invoke", ("invoke_resolved_tool",)),
    "self_invoke_tool": ("nymeria.core.self_agent", ("run_tool_envelope",)),
}


def test_every_transport_tool_fires_its_own_envelope():
    """Each transport tool's module must actually call into the envelope.

    Enforced by AST rather than by reading, following the repo's gate idiom
    (`tests/test_subprocess_env_gate.py`). The mapping must also cover the set
    exactly, so adding a transport name without wiring its inner fire fails here
    rather than silently removing hook coverage for that tool's targets.
    """
    import ast
    import importlib

    assert set(_TRANSPORT_ENVELOPE_SITES) == set(TRANSPORT_TOOL_NAMES), (
        "TRANSPORT_TOOL_NAMES changed without updating _TRANSPORT_ENVELOPE_SITES. "
        "A transport tool that does not fire its own envelope is a hook bypass."
    )

    for name, (module_path, expected_calls) in _TRANSPORT_ENVELOPE_SITES.items():
        module = importlib.import_module(module_path)
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        assert called & set(expected_calls), (
            f"{name} is transport (its node fire is suppressed) but "
            f"{module_path} calls none of {expected_calls}: its targets would "
            "run with NO hooks at all."
        )
