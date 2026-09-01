"""Tests for file_write's attach=True path.

Spec: tmp/file-attach-binary-plan.md (expected behavior 8). No dedicated
coverage of this existed before (found during the file_read attach work),
so this pins the pre-existing in-workspace/outside-workspace tag/note
behavior, which was refactored to share `_attach_result_suffix` with
file_read but is not meant to change.
"""

from __future__ import annotations

import pytest

from nymeria.tools.filesystem import file_write


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    yield


def test_attach_true_inside_workspace_writes_and_tags(tmp_path):
    path = tmp_path / "report.txt"

    result = file_write.func(str(path), content="hello", attach=True)

    assert result.startswith("[Success]: Wrote 5 characters")
    assert f"[attach:{path.resolve()}]" in result
    assert path.read_text(encoding="utf-8") == "hello"


def test_attach_false_inside_workspace_has_no_tag(tmp_path):
    path = tmp_path / "report.txt"

    result = file_write.func(str(path), content="hello", attach=False)

    assert result.startswith("[Success]: Wrote 5 characters")
    assert "[attach:" not in result


def test_attach_true_outside_workspace_skips_with_info_note(tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside-workspace")
    path = outside_dir / "report.txt"

    result = file_write.func(str(path), content="hello", attach=True)

    assert result.startswith("[Success]: Wrote 5 characters")
    assert "[attach:" not in result
    assert "[Info]: Attachment skipped." in result
    assert str(outside_dir) in result
    assert path.read_text(encoding="utf-8") == "hello"  # write still happened


def test_attach_true_overwrites_existing_content(tmp_path):
    # Documents the exact footgun from the bug report: attach=True on
    # file_write ALWAYS overwrites file_path with `content` first. This is
    # the behavior the file_write docstring now warns about; file_read's
    # attach=True is the safe alternative for an existing file (see
    # test_file_read_attach.py).
    path = tmp_path / "existing.bin"
    path.write_bytes(b"\x00original binary payload that would be lost\xff")

    result = file_write.func(str(path), content="x", attach=True)

    assert result.startswith("[Success]: Wrote 1 characters")
    assert path.read_bytes() == b"x"
    assert f"[attach:{path.resolve()}]" in result
