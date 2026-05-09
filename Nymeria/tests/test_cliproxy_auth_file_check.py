"""Regression tests for CLIProxy auth-file smoke checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "check_cliproxy_cloak.py"
SPEC = importlib.util.spec_from_file_location("check_cliproxy_cloak", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
check_cliproxy_cloak = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_cliproxy_cloak)


def write_auth(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_active_claude_auth_requires_tool_prefix_disabled(tmp_path: Path):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    bad = auth_dir / "claude-bad.json"
    write_auth(
        bad,
        {
            "type": "claude",
            "email": "bad@example.com",
            "access_token": "redacted",
        },
    )

    checked, bad_files = check_cliproxy_cloak.check_tool_prefix_disabled([auth_dir])

    assert checked == [bad]
    assert bad_files == [bad]


def test_truthy_tool_prefix_values_match_cliproxy_parser(tmp_path: Path):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    good_bool = auth_dir / "claude-bool.json"
    good_string = auth_dir / "claude-string.json"
    good_alias = auth_dir / "claude-alias.json"
    write_auth(good_bool, {"type": "claude", "tool_prefix_disabled": True})
    write_auth(good_string, {"type": "claude", "tool_prefix_disabled": "true"})
    write_auth(good_alias, {"type": "claude", "tool-prefix-disabled": 1})

    checked, bad_files = check_cliproxy_cloak.check_tool_prefix_disabled([auth_dir])

    assert checked == [good_alias, good_bool, good_string]
    assert bad_files == []


def test_disabled_and_non_claude_auth_files_are_ignored(tmp_path: Path):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    disabled = auth_dir / "claude-disabled.json"
    codex = auth_dir / "codex.json"
    write_auth(disabled, {"type": "claude", "disabled": True})
    write_auth(codex, {"type": "codex"})

    checked, bad_files = check_cliproxy_cloak.check_tool_prefix_disabled([auth_dir])

    assert checked == []
    assert bad_files == []


def test_print_check_fails_loudly_for_bad_active_auth(
    tmp_path: Path,
    capsys,
):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    write_auth(auth_dir / "claude-bad.json", {"type": "claude"})

    code = check_cliproxy_cloak.print_tool_prefix_check([auth_dir])

    captured = capsys.readouterr()
    assert code == 1
    assert "FAIL" in captured.out
    assert "tool_prefix_disabled=true" in captured.out
    assert "claude-bad.json" in captured.out


def test_cloak_state_report_flags_forced_config_cloak(tmp_path: Path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
claude-api-key:
  - api-key: sk-ant-test
    cloak:
      mode: always
""",
        encoding="utf-8",
    )

    code = check_cliproxy_cloak.print_cloak_state_report([], [config_path])

    captured = capsys.readouterr()
    assert code == 1
    assert "cloak.mode=always" in captured.out
    assert "override Nymeria identity" in captured.out


def test_cloak_state_report_warns_for_unhonored_auth_file_attributes(
    tmp_path: Path,
    capsys,
):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    write_auth(
        auth_dir / "claude.json",
        {
            "type": "claude",
            "tool_prefix_disabled": True,
            "attributes": {"cloak_mode": "never"},
        },
    )

    code = check_cliproxy_cloak.print_cloak_state_report([auth_dir], [])

    captured = capsys.readouterr()
    assert code == 0
    assert "attributes.'cloak_mode'" in captured.out
    assert "does not lift nested auth-file attributes" in captured.out


def test_cloak_state_report_fails_auth_file_forced_cloak(tmp_path: Path):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_path = auth_dir / "claude.json"
    write_auth(
        auth_path,
        {
            "type": "claude",
            "tool_prefix_disabled": True,
            "cloak_mode": "always",
        },
    )

    info, warnings, failures = check_cliproxy_cloak.collect_cloak_state_findings(
        [auth_dir],
        [],
    )

    assert any("tool_prefix_disabled=true" in line for line in info)
    assert any("top-level 'cloak_mode'" in line for line in warnings)
    assert failures == [
        f"{auth_path} declares cloak_mode=always; if a future binary honors "
        "that for file-backed OAuth, it would force Claude Code identity"
    ]
