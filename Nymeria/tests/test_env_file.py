"""Unit tests for the shared dotenv merge-writer (`nymeria/config/env_file.py`).

This is the one primitive that both the offline `nymeria init` finalize and the
online `PATCH /settings` handler write through, so its formatting, merge, and
atomic-0600 guarantees are covered here once rather than in each caller.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import dotenv_values

from nymeria.config.env_file import (
    format_env_value,
    merge_env_lines,
    parse_env_value,
    write_env_file,
)


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


def test_parse_env_value_inverts_format():
    # parse(format(x)) == x for the cases format produces: plain, base64, and the
    # quoted/escaped special-char path. This is the property _sync_updated_env_vars
    # relies on so a hot-reloaded value never carries literal quotes.
    for value in (
        "gpt-5.5",
        "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=",
        "model with space",
        'he said "hi"',
        "a\\b",
        "trailing space ",
        '{"k": "v", "n": 1}',
    ):
        assert parse_env_value(format_env_value(value)) == value


def test_parse_env_value_leaves_unquoted_and_empty():
    assert parse_env_value("gpt-5.5") == "gpt-5.5"
    assert parse_env_value("") == ""


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


def test_merge_env_lines_collapses_duplicate_keys_to_one_line(caplog):
    # #301. Every reader of these files takes the LAST occurrence of a key
    # (python-dotenv, the pydantic dotenv source, run.py's boot load, the
    # restart re-merge), so preserving a later duplicate leaves the writer and
    # every reader disagreeing about which line is live: the write reports
    # success, the process serves the new value, and the next restart silently
    # reverts. Exactly one line survives, at the first occurrence, and unrelated
    # lines between the duplicates are untouched.
    existing = ["DUP=secret-one", "# a comment between them", "KEEP=me", "DUP=secret-two"]
    with caplog.at_level(logging.WARNING, logger="nymeria.config.env_file"):
        out = merge_env_lines(existing, [("DUP", "x")])
    assert out == ["DUP=x", "# a comment between them", "KEEP=me"]
    # Removing a line from a file the user owns is announced rather than silent,
    # and named by KEY only: the line removed may hold a secret.
    assert "DUP" in caplog.text
    assert "secret-one" not in caplog.text
    assert "secret-two" not in caplog.text


def test_merge_env_lines_collapses_however_many_duplicates_there_are(caplog):
    with caplog.at_level(logging.WARNING, logger="nymeria.config.env_file"):
        assert merge_env_lines(["K=1", "K=2", "K=3"], [("K", "final")]) == ["K=final"]
    # One warning per key, carrying the count, not one per removed line.
    assert len(caplog.records) == 1
    assert "2" in caplog.text


def test_merge_env_lines_without_duplicates_says_nothing(caplog):
    # The collapse is the exceptional path: an ordinary write must not log.
    with caplog.at_level(logging.WARNING, logger="nymeria.config.env_file"):
        out = merge_env_lines(["K=old", "OTHER=1"], [("K", "new")])
    assert out == ["K=new", "OTHER=1"]
    assert caplog.records == []


def test_merge_env_lines_drop_removes_retired_keys():
    # A drop key's existing line is removed instead of preserved; comments,
    # blanks, and other keys are untouched.
    existing = ["# keep", "A=1", "RETIRED=x:y", "B=2"]
    out = merge_env_lines(existing, [("A", "9")], drop=["RETIRED"])
    assert out == ["# keep", "A=9", "B=2"]
    # Dropping a key that is not in the file is a no-op.
    assert merge_env_lines(["X=1"], [], drop=["RETIRED"]) == ["X=1"]


def test_merge_env_lines_produced_wins_over_drop():
    # A key in both produced and drop is written, so callers can pass a static
    # drop list and let produced membership decide retire-vs-update.
    out = merge_env_lines(["K=old"], [("K", "new")], drop=["K"])
    assert out == ["K=new"]
    # Drop also removes duplicate occurrences of a retired key.
    out = merge_env_lines(["GONE=1", "GONE=2"], [], drop=["GONE"])
    assert out == []


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


def test_write_env_file_merge_reads_back_as_written_over_a_duplicated_key(
    tmp_path: Path,
):
    # The property the whole of #301 exists for, asserted through the real
    # reader rather than the line list: after a merge write, a dotenv parse of
    # the file returns what the writer intended, whatever shape the file was in.
    # Before the collapse this file read back as "old", so `PATCH /settings`
    # reported success, served "new" until the next restart, and then reverted.
    path = tmp_path / ".env"
    path.write_text("LLM_MODEL=old\n# note\nLLM_MODEL=old\n", encoding="utf-8")

    write_env_file(path, [("LLM_MODEL", "new")], merge=True)

    assert dotenv_values(path) == {"LLM_MODEL": "new"}
    assert path.read_text(encoding="utf-8") == "LLM_MODEL=new\n# note\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_file_merge_collapses_a_duplicated_vault_key(tmp_path: Path):
    # The highest-consequence instance of #301, and the one the item missed:
    # `snapshot restore --write-env` writes the credential-vault Fernet key
    # through this same writer (`core/snapshot.py`). Landing it on a dead
    # duplicate line meant the restore reported success and the restored vault
    # could not be decrypted, because every reader still saw the OLD key.
    fernet = "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u="
    path = tmp_path / "config.env"
    path.write_text(
        "NYMERIA_SECRETS_KEY=stale-key-one\n"
        "# left over from a hand edit\n"
        "NYMERIA_SECRETS_KEY=stale-key-two\n",
        encoding="utf-8",
    )

    write_env_file(
        path, [("NYMERIA_SECRETS_KEY", format_env_value(fernet))], merge=True
    )

    assert dotenv_values(path) == {"NYMERIA_SECRETS_KEY": fernet}
    # The base64url key must also survive unquoted, the other property this
    # file already pins for the Fernet case.
    assert f"NYMERIA_SECRETS_KEY={fernet}" in path.read_text(encoding="utf-8")
