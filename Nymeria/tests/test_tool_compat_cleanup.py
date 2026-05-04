"""Regression tests for removed tool compatibility shims."""

from pathlib import Path


def test_legacy_tool_shim_files_are_absent():
    tools_dir = Path(__file__).resolve().parents[1] / "nymeria" / "tools"
    removed = [
        "_prv_a_products.py",
        "_prv_a_acme.py",
        "_prv_a_acme.py",
        "_prv_a_supplier.py",
        "_prv_a_utils.py",
        "_prv_a_vendor.py",
        "subagent.py",
    ]

    assert [name for name in removed if (tools_dir / name).exists()] == []


def test_tool_compatibility_aliases_are_absent():
    import nymeria.tools as tools
    from nymeria.tools import runtime_admin

    assert not hasattr(tools, "SUBAGENT_TOOLS")
    assert not hasattr(runtime_admin, "SUBAGENT_TOOLS")
    assert not hasattr(tools, "get_all_tools_with_agents")
