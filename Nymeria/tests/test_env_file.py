"""Unit tests for the shared dotenv merge-writer (`nymeria/config/env_file.py`).

This is the one primitive that both the offline `nymeria init` finalize and the
online `PATCH /settings` handler write through, so its formatting, merge, and
atomic-0600 guarantees are covered here once rather than in each caller.
"""

from __future__ import annotations

from pathlib import Path

from nymeria.config.env_file import format_env_value, merge_env_lines, write_env_file


def test_format_env_value_bools_and_none():
    assert format_env_value(True) == "true"
    assert format_env_value(False) == "false"
    assert format_env_value(None) == ""
    assert format_env_value("") == ""


def test_format_env_value_plain_and_base64_unquoted():
    assert format_env_value("gpt-5.5") == "gpt-5.5"
    assert format_env_value("claude-opus-4-8") == "claude-opus-4-8"
    # Fernet keys are base64url and end with `=`; `-`, `_`, `=` are all in the
    # safe set, so the key must write unquoted (compose env_file quoting is
    # version-fragile and we never want to exercise it for a secret).
    fernet = "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u="
    assert format_env_value(fernet) == fernet


def test_format_env_value_quotes_special_chars():
    assert format_env_value("model with space") == '"model with space"'
    assert format_env_value('he said "hi"') == '"he said \\"hi\\""'
    assert format_env_value("a\\b") == '"a\\\\b"'


def test_merge_env_lines_overlays_and_preserves_order():
    existing = ["# a comment", "", "KEEP=untouched", "CHANGE=old"]
    out = merge_env_lines(existing, [("CHANGE", "new"), ("ADD", "fresh")])
    assert out == [
        "# a comment",
        "",
        "KEEP=untouched",
        "CHANGE=new",
        "ADD=fresh",
    ]


def test_merge_env_lines_tolerates_spaced_lines_and_empty_existing():
    # A leading-whitespace / `KEY = value` line still matches on the stripped key
    # and is normalized to `KEY=value`.
    assert merge_env_lines(["  CHANGE = old  "], [("CHANGE", "new")]) == ["CHANGE=new"]
    # Empty existing means every produced key is appended in order.
    assert merge_env_lines([], [("A", "1"), ("B", "2")]) == ["A=1", "B=2"]


def test_merge_env_lines_replaces_only_first_occurrence():
    out = merge_env_lines(["DUP=1", "DUP=2"], [("DUP", "x")])
    assert out == ["DUP=x", "DUP=2"]


def test_write_env_file_fresh_writes_header_and_0600(tmp_path: Path):
    path = tmp_path / "config.env"
    lines = write_env_file(
        path, [("A", "1"), ("B", "2")], merge=False, header="# header"
    )
    assert lines == ["# header", "A=1", "B=2"]
    assert path.read_text(encoding="utf-8") == "# header\nA=1\nB=2\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_file_merge_preserves_untouched(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("# keep\nKEEP=yes\nCHANGE=old\n", encoding="utf-8")
    lines = write_env_file(path, [("CHANGE", "new")], merge=True)
    text = path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert "KEEP=yes" in text
    assert "CHANGE=new" in text
    assert "CHANGE=old" not in text
    # untouched line + the single changed line, no duplicate appended
    assert lines == ["# keep", "KEEP=yes", "CHANGE=new"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_file_merge_missing_file_appends_all(tmp_path: Path):
    path = tmp_path / "config.env"
    lines = write_env_file(path, [("A", "1")], merge=True)
    assert lines == ["A=1"]
    assert path.read_text(encoding="utf-8") == "A=1\n"
