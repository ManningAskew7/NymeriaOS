"""Regression tests for high-risk tool containment."""

from __future__ import annotations

import importlib
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


def _fake_claude_settings(tmp_path, **overrides):
    """A SimpleNamespace standing in for Settings for the claude_code tool."""
    root = (tmp_path / "proj").resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = dict(
        project_root=root,
        data_dir=tmp_path / "data",
        tool_timeout=300,
        nymeria_claude_code_url=None,
        nymeria_claude_code_token=None,
        nymeria_claude_code_roots=None,
        nymeria_claude_code_model=None,
        nymeria_claude_code_fallback_model=None,
        nymeria_claude_code_max_turns=None,
        nymeria_claude_code_max_budget_usd=None,
        nymeria_claude_code_disallowed_tools=None,
        nymeria_claude_code_bare=False,
        nymeria_claude_code_default_mode="dontAsk",
        nymeria_claude_code_block_seconds=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_claude_code_rejects_workdir_outside_allowlist(tmp_path, monkeypatch):
    """claude_code is now sandboxed: a working_dir outside the allowed roots is
    rejected before Claude Code is ever invoked."""
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    settings = _fake_claude_settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)

    outside = (tmp_path / "outside").resolve()
    outside.mkdir()

    def explode(*a, **k):
        raise AssertionError("Claude Code must not run for an out-of-allowlist dir")

    monkeypatch.setattr(claude_module, "run_local_blocking", explode)

    result = claude_module.claude_code.func("inspect", working_dir=str(outside))
    assert "[Error]" in result
    assert "outside the allowed roots" in result


def test_claude_code_runs_within_allowlist(tmp_path, monkeypatch):
    """A working_dir inside an allowed root reaches the runner with that cwd."""
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    bridge = importlib.import_module("nymeria.tools.claude_code_bridge")
    settings = _fake_claude_settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )

    captured = {}

    def fake_run_local(request, config, timeout, env=None):
        captured["cwd"] = request.cwd
        captured["mode"] = request.permission_mode
        return bridge.ClaudeCodeResult(ok=True, result_text="done")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)

    subdir = settings.project_root / "pkg"
    subdir.mkdir()
    result = claude_module.claude_code.func("inspect", working_dir="pkg")
    assert "done" in result
    assert captured["cwd"] == str(subdir)
    assert captured["mode"] == "dontAsk"


# --- self_agent path-containment (F6 refactor) --------------------------------
#
# These assert the `.resolve()`-before-`relative_to` ordering that defends the
# self-modification sandbox against symlink escapes. project_root uses
# tmp_path.resolve() so the test's expectations match the post-.resolve()
# reality inside the tools (on hosts where the tmp dir itself is a symlink).


def test_self_file_write_rejects_symlink_escape(tmp_path, monkeypatch):
    from nymeria.core import self_agent

    project_root = (tmp_path / "proj").resolve()
    tools_dir = project_root / "nymeria" / "tools"
    tools_dir.mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()

    # A symlink that lives inside the writable tools dir but points outside the
    # project. .resolve() canonicalizes it before the containment check, so a
    # write "through" it lands outside the allowlist and must be rejected.
    escape = tools_dir / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_self_edit=True, project_root=project_root),
    )

    result = self_agent.self_file_write.func(str(escape / "evil.py"), "x = 1")

    assert "Access denied" in result
    assert not (outside / "evil.py").exists()


def test_self_file_delete_rejects_symlink_escape(tmp_path, monkeypatch):
    from nymeria.core import self_agent

    project_root = (tmp_path / "proj").resolve()
    tools_dir = project_root / "nymeria" / "tools"
    tools_dir.mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    victim = outside / "victim.py"
    victim.write_text("keep me")

    escape = tools_dir / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_self_edit=True, project_root=project_root),
    )

    result = self_agent.self_file_delete.func(str(escape / "victim.py"))

    assert "Access denied" in result
    assert victim.exists()  # file outside the allowlist was not deleted


def test_self_file_read_rejects_symlink_escape(tmp_path, monkeypatch):
    from nymeria.core import self_agent

    project_root = (tmp_path / "proj").resolve()
    project_root.mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("top secret")

    # Symlink inside the project that points outside it. read is ungated, so
    # only project_root is needed on the fake settings.
    escape = project_root / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(project_root=project_root),
    )

    result = self_agent.self_file_read.func(str(escape / "secret.txt"))

    assert "Access denied" in result
    assert "top secret" not in result


def test_self_file_write_allows_path_inside_allowlist(tmp_path, monkeypatch):
    """Positive guard: the refactored allowlist still accepts a legitimate
    relative path under a writable dir and writes it through the resolved path.
    Uses agents/ to skip the tools/-only @tool-decorator requirement.
    """
    from nymeria.core import self_agent

    project_root = (tmp_path / "proj").resolve()
    (project_root / "nymeria" / "agents").mkdir(parents=True)
    backups_dir = (tmp_path / "backups").resolve()

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(
            nymeria_allow_self_edit=True,
            project_root=project_root,
            backups_dir=backups_dir,
        ),
    )

    result = self_agent.self_file_write.func("nymeria/agents/demo_helper.py", "x = 1\n")

    assert "Access denied" not in result
    assert "[Success]" in result
    assert (project_root / "nymeria" / "agents" / "demo_helper.py").exists()


def test_self_file_write_rejects_dotdot_traversal(tmp_path, monkeypatch):
    """`..` segments must be collapsed by .resolve() before the containment
    check, so a relative path that climbs out of the writable dirs is rejected.
    """
    from nymeria.core import self_agent

    project_root = (tmp_path / "proj").resolve()
    (project_root / "nymeria" / "tools").mkdir(parents=True)

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_self_edit=True, project_root=project_root),
    )

    result = self_agent.self_file_write.func(
        "nymeria/tools/../../../etc/passwd_clone.py", "x = 1"
    )

    assert "Access denied" in result


def test_self_file_write_rejects_inside_project_but_not_writable(tmp_path, monkeypatch):
    """A path inside the project but outside the writable allowlist
    (tools/agents/triggers-sources) is denied for write, even though read/list
    would allow it. Locks the _resolve_in_writable_dir vs _resolve_in_project
    distinction.
    """
    from nymeria.core import self_agent

    project_root = (tmp_path / "proj").resolve()
    (project_root / "nymeria" / "core").mkdir(parents=True)

    monkeypatch.setattr(
        self_agent,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_self_edit=True, project_root=project_root),
    )

    result = self_agent.self_file_write.func("nymeria/core/agent.py", "x = 1")

    assert "Access denied" in result


def _rollback_settings(monkeypatch, project_root, backups_dir, *, allow_self_edit):
    """Point both halves of the rollback tool at a temp tree.

    Two patches because the tool reads settings itself (for the backup dir) and
    the policy helpers it calls read them through ``self_agent``.
    """
    from nymeria.core import self_agent

    settings = SimpleNamespace(
        nymeria_allow_self_edit=allow_self_edit,
        project_root=project_root,
        backups_dir=backups_dir,
    )
    monkeypatch.setattr(self_agent, "get_settings", lambda: settings)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)


def _seed_backup(project_root, backups_dir, relative, planted):
    """Create a backup holding ``planted`` for ``relative``, as a write would.

    Uses the real ``BackupManager``, so the stash layout under test is the one
    the tool will look in.
    """
    from nymeria.core.backup import BackupManager

    target = project_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(planted, encoding="utf-8")
    BackupManager(backups_dir, project_root).create_backup(target)
    target.write_text("current", encoding="utf-8")
    return target


def test_self_modify_rollback_honours_the_kill_switch(tmp_path, monkeypatch):
    """C12-01: the switch says it stops source mutation; rollback IS one.

    ``NYMERIA_ALLOW_SELF_EDIT=false`` is an operator asserting that these tools
    may not write source. Before this, the one tool named "rollback" still did.
    """
    from nymeria.tools.runtime_admin import self_modify_rollback

    project_root = (tmp_path / "proj").resolve()
    backups_dir = (tmp_path / "backups").resolve()
    _rollback_settings(monkeypatch, project_root, backups_dir, allow_self_edit=True)
    target = _seed_backup(project_root, backups_dir, "nymeria/tools/demo.py", "planted")

    _rollback_settings(monkeypatch, project_root, backups_dir, allow_self_edit=False)
    result = self_modify_rollback.func("nymeria/tools/demo.py")

    assert result.startswith("[Error]")
    assert "NYMERIA_ALLOW_SELF_EDIT" in result
    assert target.read_text(encoding="utf-8") == "current"


def test_self_modify_rollback_rejects_targets_outside_the_writable_allowlist(
    tmp_path, monkeypatch
):
    """C12-01's laundering route: a restore is a write to a named destination.

    The backup stash is reachable with an ordinary ``file_write`` (``backups``
    is not a secret store and sits several components deep), so without a screen
    on the DESTINATION this tool copied caller-supplied bytes over any path,
    including the three sets every other write path refuses: protected source
    dirs, the credential stores, and the global prompt overrides.
    """
    from nymeria.tools.runtime_admin import self_modify_rollback

    project_root = (tmp_path / "proj").resolve()
    backups_dir = (project_root / "data" / "backups").resolve()
    _rollback_settings(monkeypatch, project_root, backups_dir, allow_self_edit=True)

    for relative in (
        "nymeria/core/agent.py",                  # protected source dir
        "data/auth_tokens/u1/google.json",        # credential store
        "data/system_prompt.md",                  # global prompt override
        "../outside.py",                          # outside the project entirely
    ):
        target = _seed_backup(project_root, backups_dir, relative, "planted")
        result = self_modify_rollback.func(relative)
        assert result.startswith("[Error]"), f"{relative} was restored"
        assert "nymeria/tools/" in result, f"{relative}: no remedy named"
        assert target.read_text(encoding="utf-8") == "current", f"{relative} was written"


def test_self_modify_rollback_still_restores_inside_the_allowlist(tmp_path, monkeypatch):
    """The other half: every backup that CAN exist is inside the allowlist.

    ``create_backup`` has exactly two callers, ``self_file_write`` and
    ``self_file_delete``, and both resolve through the same helper first, so the
    screen costs the tool nothing it was ever able to do.
    """
    from nymeria.tools.runtime_admin import self_modify_rollback

    project_root = (tmp_path / "proj").resolve()
    backups_dir = (tmp_path / "backups").resolve()
    _rollback_settings(monkeypatch, project_root, backups_dir, allow_self_edit=True)
    target = _seed_backup(project_root, backups_dir, "nymeria/tools/demo.py", "good")

    result = self_modify_rollback.func("nymeria/tools/demo.py")

    assert result.startswith("[Success]"), result
    assert target.read_text(encoding="utf-8") == "good"
