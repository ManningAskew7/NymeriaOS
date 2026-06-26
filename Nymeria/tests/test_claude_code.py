"""Tests for Claude Code executable discovery (slice 18 F9).

Covers the Windows VS Code extension lookup, which replaced a hardcoded,
version-pinned path with a highest-version glob. The version ordering must be
numeric (``2.1.113`` outranks ``2.1.29``), not lexicographic.
"""

import importlib

# Import the submodule explicitly: `nymeria.tools` re-exports the `claude_code`
# tool object under that name, which would shadow `from nymeria.tools import
# claude_code`.
claude_module = importlib.import_module("nymeria.tools.claude_code")


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
        claude_module._extension_version_key("anthropic.claude-code-2.1.113-win32-x64")
        == (2, 1, 113)
    )


def test_extension_version_key_orders_numerically_not_lexically():
    # The whole point of F9: a string sort puts "2.1.29" above "2.1.113".
    newer = claude_module._extension_version_key("anthropic.claude-code-2.1.113-win32-x64")
    older = claude_module._extension_version_key("anthropic.claude-code-2.1.29-win32-x64")
    assert newer > older


def test_extension_version_key_unparseable_sorts_lowest():
    assert claude_module._extension_version_key("not-a-claude-code-dir") == ()
    # An empty tuple sorts below any real version.
    assert () < claude_module._extension_version_key(
        "anthropic.claude-code-0.0.1-win32-x64"
    )


def test_extension_version_key_non_numeric_segment_defaults_zero():
    assert (
        claude_module._extension_version_key("anthropic.claude-code-2.1.x-win32-x64")
        == (2, 1, 0)
    )


# --- _find_claude_code_executable --------------------------------------------


def test_path_claude_wins_first(monkeypatch):
    """`claude` on PATH short-circuits before any platform-specific lookup."""
    monkeypatch.setattr(claude_module.shutil, "which", lambda name: "/usr/bin/claude")
    assert claude_module._find_claude_code_executable() == "/usr/bin/claude"


def test_windows_picks_highest_version(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_module.sys, "platform", "win32")
    monkeypatch.setattr(claude_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_module.Path, "home", staticmethod(lambda: tmp_path))

    _make_extension(tmp_path, "2.1.29")
    newest = _make_extension(tmp_path, "2.1.113")
    _make_extension(tmp_path, "2.0.5")

    assert claude_module._find_claude_code_executable() == str(newest)


def test_windows_falls_back_to_claude_exe_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_module.sys, "platform", "win32")
    monkeypatch.setattr(claude_module.Path, "home", staticmethod(lambda: tmp_path))

    def fake_which(name):
        return r"C:\tools\claude.exe" if name == "claude.exe" else None

    monkeypatch.setattr(claude_module.shutil, "which", fake_which)
    # No extension dirs created, so the glob misses and the PATH fallback wins.
    assert claude_module._find_claude_code_executable() == r"C:\tools\claude.exe"


def test_windows_returns_none_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_module.sys, "platform", "win32")
    monkeypatch.setattr(claude_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_module.Path, "home", staticmethod(lambda: tmp_path))
    assert claude_module._find_claude_code_executable() is None


def test_non_windows_no_path_claude_returns_none(monkeypatch):
    monkeypatch.setattr(claude_module.sys, "platform", "linux")
    monkeypatch.setattr(claude_module.shutil, "which", lambda name: None)
    assert claude_module._find_claude_code_executable() is None
