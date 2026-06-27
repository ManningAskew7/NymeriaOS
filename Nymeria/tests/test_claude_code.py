"""Tests for Claude Code executable discovery.

Covers the Windows VS Code extension lookup, which replaced a hardcoded,
version-pinned path with a highest-version glob. The version ordering must be
numeric (``2.1.113`` outranks ``2.1.29``), not lexicographic.

The discovery logic now lives in ``nymeria.tools.claude_code_bridge`` (shared by
the tool and the host runner); ``resolve_claude_executable`` replaced the old
``_find_claude_code_executable``.
"""

import importlib

bridge = importlib.import_module("nymeria.tools.claude_code_bridge")


def _make_extension(home, version):
    """Create a synthetic claude-code VS Code extension exe; return its path."""
    exe = (
        home
        / ".vscode"
        / "extensions"
        / f"anthropic.claude-code-{version}-win32-x64"
        / "resources"
        / "native-binary"
        / "claude.exe"
    )
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("")
    return exe


# --- _extension_version_key --------------------------------------------------


def test_extension_version_key_parses_numeric_tuple():
    assert (
        bridge._extension_version_key("anthropic.claude-code-2.1.113-win32-x64")
        == (2, 1, 113)
    )


def test_extension_version_key_orders_numerically_not_lexically():
    newer = bridge._extension_version_key("anthropic.claude-code-2.1.113-win32-x64")
    older = bridge._extension_version_key("anthropic.claude-code-2.1.29-win32-x64")
    assert newer > older


def test_extension_version_key_unparseable_sorts_lowest():
    assert bridge._extension_version_key("not-a-claude-code-dir") == ()
    assert () < bridge._extension_version_key("anthropic.claude-code-0.0.1-win32-x64")


def test_extension_version_key_non_numeric_segment_defaults_zero():
    assert (
        bridge._extension_version_key("anthropic.claude-code-2.1.x-win32-x64")
        == (2, 1, 0)
    )


# --- resolve_claude_executable -----------------------------------------------


def test_path_claude_wins_first(monkeypatch):
    """`claude` on PATH short-circuits before any platform-specific lookup."""
    monkeypatch.setattr(bridge.shutil, "which", lambda name: "/usr/bin/claude")
    assert bridge.resolve_claude_executable() == "/usr/bin/claude"


def test_windows_picks_highest_version(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    monkeypatch.setattr(bridge.Path, "home", staticmethod(lambda: tmp_path))

    _make_extension(tmp_path, "2.1.29")
    newest = _make_extension(tmp_path, "2.1.113")
    _make_extension(tmp_path, "2.0.5")

    assert bridge.resolve_claude_executable() == str(newest)


def test_windows_falls_back_to_claude_exe_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.Path, "home", staticmethod(lambda: tmp_path))

    def fake_which(name):
        return r"C:\tools\claude.exe" if name == "claude.exe" else None

    monkeypatch.setattr(bridge.shutil, "which", fake_which)
    assert bridge.resolve_claude_executable() == r"C:\tools\claude.exe"


def test_windows_returns_none_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    monkeypatch.setattr(bridge.Path, "home", staticmethod(lambda: tmp_path))
    assert bridge.resolve_claude_executable() is None


def test_non_windows_no_path_claude_returns_none(monkeypatch):
    monkeypatch.setattr(bridge.sys, "platform", "linux")
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    assert bridge.resolve_claude_executable() is None
