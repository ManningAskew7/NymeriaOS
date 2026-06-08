"""Regression tests for high-risk tool containment."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


def test_bash_execute_is_default_core_tool():
    from nymeria.tools import SEED_TOOLS, ADMIN_ONLY_TOOL_NAMES, CATALOG_TOOLS

    core_tool_names = {tool.name for tool in SEED_TOOLS}

    assert "bash_execute" in core_tool_names
    assert "bash_execute" not in CATALOG_TOOLS
    assert "bash_execute" not in ADMIN_ONLY_TOOL_NAMES


def test_self_file_write_is_disabled_without_escape_hatch(monkeypatch):
    from nymeria.core import self_agent

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_self_edit=False),
    )

    result = self_agent.self_file_write.func("nymeria/tools/backdoor.py", "print('no')")

    assert "Self-modification writes are disabled" in result


def test_claude_code_allows_outside_workdir_and_bash_by_default(tmp_path, monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(claude_module, "CLAUDE_CODE_EXE", sys.executable)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(claude_module.subprocess, "run", fake_run)

    result = claude_module.claude_code.func("inspect", working_dir=str(outside))

    assert result == "ok"
    assert captured["cwd"] == str(outside)
    tools_index = captured["cmd"].index("--tools") + 1
    assert "Bash" in captured["cmd"][tools_index].split(",")
