"""Regression tests for high-risk tool containment."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


def test_bash_execute_is_optional_admin_only():
    from nymeria.tools import ALL_TOOLS, ADMIN_ONLY_OPTIONAL_TOOL_NAMES, OPTIONAL_TOOLS

    core_tool_names = {tool.name for tool in ALL_TOOLS}

    assert "bash_execute" not in core_tool_names
    assert "bash_execute" in OPTIONAL_TOOLS
    assert "bash_execute" in ADMIN_ONLY_OPTIONAL_TOOL_NAMES


def test_self_file_write_is_disabled_without_escape_hatch(monkeypatch):
    from nymeria.core import self_agent

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_self_edit=False),
    )

    result = self_agent.self_file_write.func("nymeria/tools/backdoor.py", "print('no')")

    assert "Self-modification writes are disabled" in result


def test_claude_code_rejects_workdir_outside_workspace(tmp_path, monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr(claude_module, "CLAUDE_CODE_EXE", sys.executable)

    result = claude_module.claude_code.func("inspect", working_dir=str(outside))

    assert "Working directory outside workspace" in result


def test_claude_code_bash_requires_escape_hatch(tmp_path, monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(workspace))
    monkeypatch.delenv("NYMERIA_ALLOW_CLAUDE_CODE_BASH", raising=False)
    monkeypatch.setattr(claude_module, "CLAUDE_CODE_EXE", sys.executable)

    result = claude_module.claude_code.func(
        "inspect",
        working_dir=str(workspace),
        allow_bash=True,
    )

    assert "Claude Code Bash access is disabled" in result
