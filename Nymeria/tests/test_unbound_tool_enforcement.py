"""Tests for SafeToolNode's unbound-tool-call enforcement (the superset gate).

Exercised at the ``_unbound_call_message`` seam (mirroring the repo's other
SafeToolNode tests that drive ``_parse_input`` / helpers directly rather than a
full graph ``.invoke``). The node is built with a dispatch SUPERSET that is
wider than the resolver's EFFECTIVE set, reproducing the real
dynamic-binding shape where a catalog tool is dispatchable but not bound.
"""

from __future__ import annotations

from langchain_core.tools import tool

from nymeria.tools.tool_search import tool_manage
from nymeria.vendor.react_agent.nodes import SafeToolNode


@tool
def bnd_echo(text: str) -> str:
    """Bound echo (enforcement test)."""
    return f"bound:{text}"


@tool
def unb_echo(text: str) -> str:
    """Unbound echo (enforcement test)."""
    return f"unbound:{text}"


def _call(name):
    return {"name": name, "args": {"text": "x"}, "id": "c1"}


_STRICT = {"configurable": {"thread_id": "t", "user_id": "u"}}
_PERMISSIVE = {
    "configurable": {"thread_id": "t", "user_id": "u", "allow_unbound_tool_calls": True}
}


def _dynamic_node(superset, effective):
    node = SafeToolNode(superset, dynamic_tool_resolver=lambda: (effective, "h1"))
    # Populate _effective_tool_names exactly as a real batch would.
    node._ensure_dynamic_tools_for_calls([])
    return node


def test_bound_tool_is_allowed():
    node = _dynamic_node([bnd_echo, unb_echo], [bnd_echo])
    assert node._unbound_call_message(_call("bnd_echo"), _STRICT) is None


def test_unbound_tool_refused_strict():
    from nymeria.tools.tool_invoke import tool_invoke

    # Both remedies are BOUND here, so both are offered (#320: a remedy is
    # named only when this thread can actually take it).
    node = _dynamic_node(
        [bnd_echo, unb_echo, tool_manage, tool_invoke],
        [bnd_echo, tool_manage, tool_invoke],
    )
    msg = node._unbound_call_message(_call("unb_echo"), _STRICT)
    assert msg is not None
    assert msg.status == "error"
    assert 'tool_invoke(name="unb_echo"' in msg.content
    assert 'tool_manage(action="enable", tools=["unb_echo"]' in msg.content


def test_unbound_tool_refused_strict_without_bound_remedies():
    # Neither tool_manage nor tool_invoke is bound: the old copy recommended
    # both anyway (a dead pointer when the tool-management kit itself lapsed,
    # backlog #320); the refusal now says what the thread can do instead.
    node = _dynamic_node([bnd_echo, unb_echo], [bnd_echo])
    msg = node._unbound_call_message(_call("unb_echo"), _STRICT)
    assert msg is not None
    assert msg.status == "error"
    assert "tool_invoke(" not in msg.content
    assert "tool_manage(" not in msg.content
    assert "ask the user to run /tools enable unb_echo" in msg.content


def test_unbound_tool_allowed_permissive_when_gates_pass():
    node = _dynamic_node([bnd_echo, unb_echo], [bnd_echo])
    assert node._unbound_call_message(_call("unb_echo"), _PERMISSIVE) is None


def test_unbound_tool_refused_permissive_when_denylisted():
    # tool_manage is a protected management tool: even permissive mode refuses it.
    node = _dynamic_node([bnd_echo, tool_manage], [bnd_echo])
    msg = node._unbound_call_message(_call("tool_manage"), _PERMISSIVE)
    assert msg is not None
    assert msg.status == "error"
    assert "protected management tool" in msg.content


def test_unknown_tool_falls_through_to_parent():
    # Not in the superset at all: leave the canonical invalid-tool error to the
    # parent ToolNode rather than shadowing it here.
    node = _dynamic_node([bnd_echo], [bnd_echo])
    assert node._unbound_call_message(_call("does_not_exist"), _STRICT) is None


def test_legacy_mode_no_enforcement():
    # No resolver: effective set is None, so enforcement is skipped entirely
    # (the dispatch table is already the exact bound set).
    node = SafeToolNode([bnd_echo])
    node._ensure_dynamic_tools_for_calls([])
    assert node._effective_tool_names is None
    assert node._unbound_call_message(_call("unb_echo"), _STRICT) is None
