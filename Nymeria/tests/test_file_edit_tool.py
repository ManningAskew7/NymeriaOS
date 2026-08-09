from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS
from nymeria.tools import filesystem
from nymeria.tools import execution_environment
from nymeria.tools.file_edit import file_edit
from nymeria.tools.filesystem import file_write
from nymeria.tools.metadata import SecurityLevel, get_all_tool_metadata


@pytest.fixture(autouse=True)
def workspace_root(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))


def _call(path: Path, edits: list[dict], **kwargs):
    return json.loads(file_edit.func(str(path), edits, **kwargs))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_file_edit_replaces_unique_exact_text(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("alpha beta gamma\n", encoding="utf-8")

    result = _call(
        path,
        [{"operation": "replace", "old_text": "beta", "new_text": "BETA"}],
    )

    assert result["ok"] is True
    assert result["edits_applied"] == 1
    assert result["changed"] is True
    assert "+alpha BETA gamma" in result["diff"]
    assert path.read_text(encoding="utf-8") == "alpha BETA gamma\n"


def test_file_edit_requires_unique_match_without_occurrence(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("same same\n", encoding="utf-8")

    result = _call(
        path,
        [{"operation": "replace", "old_text": "same", "new_text": "changed"}],
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "match_count"
    assert result["error"]["details"]["match_count"] == 2
    assert path.read_text(encoding="utf-8") == "same same\n"


def test_file_edit_can_target_explicit_occurrence(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("x x x\n", encoding="utf-8")

    result = _call(
        path,
        [
            {
                "operation": "replace",
                "old_text": "x",
                "new_text": "Y",
                "occurrence": 2,
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_text(encoding="utf-8") == "x Y x\n"


def test_file_edit_delete_and_insert_operations(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("one two three\n", encoding="utf-8")

    result = _call(
        path,
        [
            {"operation": "delete", "old_text": "two "},
            {"operation": "insert_before", "old_text": "three", "new_text": "TWO "},
            {"operation": "insert_after", "old_text": "three", "new_text": " four"},
        ],
    )

    assert result["ok"] is True
    assert path.read_text(encoding="utf-8") == "one TWO three four\n"


def test_file_edit_replace_range_requires_matching_context(tmp_path):
    path = tmp_path / "example.txt"
    original = "line 1\nline 2\nline 3\n"
    # newline="\n" pins LF bytes on Windows too: this test pushes raw line
    # context through the tool, and the platform-default translation would
    # write CRLF and break the context match (found by the first Windows
    # smoke run). The tool itself is byte-honest by design.
    path.write_text(original, encoding="utf-8", newline="\n")

    mismatch = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 2,
                "end_line": 3,
                "old_text": "line two\nline 3\n",
                "new_text": "updated\n",
            }
        ],
    )
    assert mismatch["ok"] is False
    assert mismatch["error"]["type"] == "range_context_mismatch"
    assert path.read_text(encoding="utf-8") == original

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 2,
                "end_line": 3,
                "old_text": "line 2\nline 3\n",
                "new_text": "updated\n",
            }
        ],
    )
    assert result["ok"] is True
    assert path.read_text(encoding="utf-8") == "line 1\nupdated\n"


def test_file_edit_dry_run_returns_diff_without_writing(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("alpha\n", encoding="utf-8")

    result = _call(
        path,
        [{"operation": "replace", "old_text": "alpha", "new_text": "beta"}],
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["bytes_written"] == 0
    assert "+beta" in result["diff"]
    assert path.read_text(encoding="utf-8") == "alpha\n"


def test_file_edit_expected_sha256_guards_against_stale_content(tmp_path):
    path = tmp_path / "example.txt"
    # newline="\n" pins LF bytes: the sha comparison below hashes the actual
    # file bytes, which Windows newline translation would otherwise change.
    path.write_text("current\n", encoding="utf-8", newline="\n")

    result = _call(
        path,
        [{"operation": "replace", "old_text": "current", "new_text": "next"}],
        expected_sha256=_sha256(b"stale\n"),
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "hash_mismatch"
    assert result["original_sha256"] == _sha256(b"current\n")
    assert path.read_text(encoding="utf-8") == "current\n"


def test_file_edit_is_all_or_nothing(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("alpha beta\n", encoding="utf-8")

    result = _call(
        path,
        [
            {"operation": "replace", "old_text": "alpha", "new_text": "ALPHA"},
            {"operation": "replace", "old_text": "missing", "new_text": "MISSING"},
        ],
    )

    assert result["ok"] is False
    assert result["error"]["edit_index"] == 1
    assert path.read_text(encoding="utf-8") == "alpha beta\n"


def test_file_edit_preserves_crlf_for_inserted_text(tmp_path):
    path = tmp_path / "example.txt"
    path.write_bytes(b"alpha\r\nomega\r\n")

    result = _call(
        path,
        [
            {
                "operation": "insert_after",
                "old_text": "alpha\r\n",
                "new_text": "beta\n",
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_bytes() == b"alpha\r\nbeta\r\nomega\r\n"


def test_file_edit_rejects_protected_nymeria_paths(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(project_root))
    protected_path = project_root / "nymeria" / "core" / "agent.py"

    result = _call(
        protected_path,
        [{"operation": "replace", "old_text": "Agent", "new_text": "Agent"}],
        dry_run=True,
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path"


def test_file_write_rejects_protected_nymeria_paths(monkeypatch):
    # file_write and file_edit must enforce the same protected-dir policy via the
    # shared filesystem.protected_path_error helper.
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(project_root))
    protected_path = project_root / "nymeria" / "core" / "agent.py"

    result = file_write.func(str(protected_path), "should not be written")

    assert result.startswith("[Error]:")
    assert "Cannot modify protected system file" in result
    assert "nymeria/core/agent.py" in result


def test_file_edit_rejects_paths_outside_workspace_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        filesystem,
        "get_settings",
        lambda: type("Settings", (), {"nymeria_confine_file_to_workspace": True})(),
    )
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    result = _call(
        outside,
        [{"operation": "replace", "old_text": "outside", "new_text": "changed"}],
        dry_run=True,
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "path_outside_workspace"
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_file_write_is_confined_to_workspace_when_enabled(tmp_path, monkeypatch):
    project_root = tmp_path.parent / f"{tmp_path.name}-project"
    project_root.mkdir()
    monkeypatch.setattr(
        filesystem,
        "get_settings",
        lambda: type("Settings", (), {"nymeria_confine_file_to_workspace": True})(),
    )
    monkeypatch.setattr(
        execution_environment,
        "get_settings",
        lambda: type("Settings", (), {"project_root": project_root})(),
    )
    ok = file_write.func(str(tmp_path / "reports" / "result.txt"), "hello")
    relative_denied = file_write.func("reports/relative-result.txt", "nope")
    denied = file_write.func(str(tmp_path.parent / "outside-write.txt"), "nope")

    assert ok.startswith("[Success]")
    assert (tmp_path / "reports" / "result.txt").read_text(encoding="utf-8") == "hello"
    assert relative_denied.startswith("[Error]: Path outside workspace")
    assert denied.startswith("[Error]: Path outside workspace")


def test_file_edit_is_seed_default_and_has_metadata():
    assert "file_edit" in {tool.name for tool in SEED_TOOLS}
    assert "file_edit" not in CATALOG_TOOLS

    meta = get_all_tool_metadata("file_edit")
    assert meta is not None
    assert meta.security_level == SecurityLevel.MODERATE
    assert meta.default_enabled is True


# --- Line-convention alignment with file_read windows (plan addendum 15-17,
#     tmp/file-read-offset-plan.md) ---


def test_replace_range_accepts_lf_old_text_on_crlf_file(tmp_path):
    path = tmp_path / "dos.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\n")

    # old_text as lifted from a file_read window (LF-normalized).
    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 2,
                "end_line": 3,
                "old_text": "two\nthree\n",
                "new_text": "TWO\nTHREE\n",
            }
        ],
    )

    assert result["ok"] is True
    # Splice is byte-honest and new_text lands in the file's newline style.
    assert path.read_bytes() == b"one\r\nTWO\r\nTHREE\r\nfour\r\n"


def test_replace_range_still_accepts_byte_exact_crlf_old_text(tmp_path):
    path = tmp_path / "dos.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 2,
                "end_line": 2,
                "old_text": "two\r\n",
                "new_text": "TWO\n",
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_replace_range_content_mismatch_still_rejected_on_crlf(tmp_path):
    path = tmp_path / "dos.txt"
    original = b"one\r\ntwo\r\nthree\r\n"
    path.write_bytes(original)

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 2,
                "end_line": 2,
                "old_text": "not two\n",
                "new_text": "X\n",
            }
        ],
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "range_context_mismatch"
    assert path.read_bytes() == original


def test_replace_range_line_numbers_split_on_lf_only(tmp_path):
    # A form feed is NOT a line break for grep -n, editors, or file_read's
    # windows; splitlines() treats it as one. Line 2 must be "b\x0cc".
    path = tmp_path / "feed.txt"
    path.write_bytes(b"a\nb\x0cc\nd\n")

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 2,
                "end_line": 2,
                "old_text": "b\x0cc\n",
                "new_text": "B\n",
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_bytes() == b"a\nB\nd\n"


def test_replace_range_edits_last_line_of_file_without_trailing_newline(tmp_path):
    path = tmp_path / "tail.txt"
    path.write_bytes(b"one\ntwo\nthree")

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 3,
                "end_line": 3,
                "old_text": "three",
                "new_text": "THREE",
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_bytes() == b"one\ntwo\nTHREE"


def test_replace_range_first_line_edit_preserves_untailed_last_line(tmp_path):
    path = tmp_path / "tail.txt"
    path.write_bytes(b"one\ntwo\nthree")

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 1,
                "end_line": 1,
                "old_text": "one\n",
                "new_text": "ONE\n",
            }
        ],
    )

    assert result["ok"] is True
    # The tail line, which has no trailing newline, must survive intact.
    assert path.read_bytes() == b"ONE\ntwo\nthree"


def test_replace_range_forgives_missing_eof_newline_only_at_eof(tmp_path):
    path = tmp_path / "tail.txt"
    path.write_bytes(b"one\ntwo\nthree")

    # The natural reconstruction from a numbered window appends "\n"; at EOF
    # that single difference is forgiven.
    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 3,
                "end_line": 3,
                "old_text": "three\n",
                "new_text": "THREE\n",
            }
        ],
    )
    assert result["ok"] is True
    assert path.read_bytes() == b"one\ntwo\nTHREE\n"

    # The same phantom newline on a NON-final range is still a mismatch.
    path.write_bytes(b"one\ntwo\nthree")
    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 1,
                "end_line": 1,
                "old_text": "one\n\n",
                "new_text": "X\n",
            }
        ],
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "range_context_mismatch"


def test_replace_range_rejects_line_count_mismatch_on_bare_cr_content(tmp_path):
    # A bare \r is line CONTENT (not a break); old_text claiming it as a
    # break has the wrong line count and must not match (tolerance is
    # CRLF-vs-LF only).
    path = tmp_path / "cr.txt"
    original = b"a\rb\nsecond\n"
    path.write_bytes(original)

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": 1,
                "end_line": 1,
                "old_text": "a\nb\n",
                "new_text": "Z\n",
            }
        ],
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "range_context_mismatch"
    assert path.read_bytes() == original


def test_file_read_window_to_replace_range_round_trip_without_trailing_newline(
    tmp_path,
):
    from nymeria.tools.filesystem import file_read

    path = tmp_path / "tail.txt"
    path.write_bytes(b"alpha\nbeta\ngamma")

    shown, _ = file_read.func(str(path), offset=3)
    rows = [line.split("\t", 1) for line in shown.splitlines() if "\t" in line]
    old_text = "".join(f"{text}\n" for _, text in rows)  # natural reconstruction

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": int(rows[0][0]),
                "end_line": int(rows[-1][0]),
                "old_text": old_text,
                "new_text": "GAMMA\n",
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_bytes() == b"alpha\nbeta\nGAMMA\n"


def test_file_read_window_to_replace_range_round_trip_on_crlf(tmp_path):
    from nymeria.tools.filesystem import file_read

    path = tmp_path / "dos.txt"
    path.write_bytes(b"".join(b"line %d\r\n" % i for i in range(1, 6)))

    shown, _ = file_read.func(str(path), max_lines=2, offset=3)
    rows = [line.split("\t", 1) for line in shown.splitlines() if "\t" in line]
    old_text = "".join(f"{text}\n" for _, text in rows)

    result = _call(
        path,
        [
            {
                "operation": "replace_range",
                "start_line": int(rows[0][0]),
                "end_line": int(rows[-1][0]),
                "old_text": old_text,
                "new_text": "REPLACED\n",
            }
        ],
    )

    assert result["ok"] is True
    assert path.read_bytes() == b"line 1\r\nline 2\r\nREPLACED\r\nline 5\r\n"
