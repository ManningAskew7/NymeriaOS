from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nymeria.tools import ALL_TOOLS, OPTIONAL_TOOLS
from nymeria.tools.file_edit import file_edit
from nymeria.tools.metadata import SecurityLevel, get_all_tool_metadata


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
    path.write_text(original, encoding="utf-8")

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
    path.write_text("current\n", encoding="utf-8")

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


def test_file_edit_rejects_protected_nymeria_paths():
    project_root = Path(__file__).resolve().parents[1]
    protected_path = project_root / "nymeria" / "core" / "agent.py"

    result = _call(
        protected_path,
        [{"operation": "replace", "old_text": "Agent", "new_text": "Agent"}],
        dry_run=True,
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path"


def test_file_edit_is_optional_and_has_metadata():
    assert "file_edit" not in {tool.name for tool in ALL_TOOLS}
    assert "file_edit" in OPTIONAL_TOOLS

    meta = get_all_tool_metadata("file_edit")
    assert meta is not None
    assert meta.security_level == SecurityLevel.MODERATE
    assert meta.default_enabled is False
