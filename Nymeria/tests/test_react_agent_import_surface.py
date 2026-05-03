"""Regression tests for Nymeria's vendored ReactAgent package exports."""

import importlib.util

import nymeria.vendor.react_agent as react_agent


def test_vendored_react_agent_does_not_export_demo_tool_surface():
    removed_names = {
        "TOOLS",
        "default_registry",
        "get_default_registry",
        "register_tool",
        "ReactAgent",
        "graph",
        "ExtendedAgentState",
    }

    assert removed_names.isdisjoint(react_agent.__all__)

    for name in removed_names - {"graph"}:
        assert not hasattr(react_agent, name)

    # Python sets package.graph to the imported submodule; the removed surface
    # was the old default graph object exported from __all__.
    assert react_agent.graph.__name__ == "nymeria.vendor.react_agent.graph"

    assert importlib.util.find_spec("nymeria.vendor.react_agent.tools") is None
