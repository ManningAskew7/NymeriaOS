"""Regression tests for Nymeria's vendored ReactAgent package exports."""

import importlib.util
from pathlib import Path

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
        "agent_node",
        "tools_node",
        "should_continue",
        "get_graph_with_memory",
        "get_preset",
        "PROVIDER_PRESETS",
        "MODELS_TOOL_COMPATIBLE",
        "MODELS_WITH_TOOL_ISSUES",
        "check_model_compatibility",
    }

    assert removed_names.isdisjoint(react_agent.__all__)

    for name in removed_names - {"graph"}:
        assert not hasattr(react_agent, name)

    # Python sets package.graph to the imported submodule; the removed surface
    # was the old default graph object exported from __all__.
    assert react_agent.graph.__name__ == "nymeria.vendor.react_agent.graph"

    assert importlib.util.find_spec("nymeria.vendor.react_agent.tools") is None


def test_vendored_react_agent_does_not_load_dotenv_directly():
    vendor_root = Path(__file__).resolve().parents[1] / "nymeria" / "vendor" / "react_agent"

    for relative_path in ("config.py", "nodes.py"):
        source = (vendor_root / relative_path).read_text(encoding="utf-8")
        assert "load_dotenv" not in source
