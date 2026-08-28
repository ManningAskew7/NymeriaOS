"""Tests for file_read's windowed reads (offset + max_lines).

Spec: tmp/file-read-offset-plan.md (expected behaviors 1-14 and 17-18; 15-16
live in test_file_edit_tool.py). Known deliberately-unpinned edge: a file
ending in a bare \\r with no newline (the display strips the \\r, so the
read-to-edit round trip stays open there; edge of edges, fails safe).
"""

from __future__ import annotations

import json

import pytest

from nymeria.tools import llm_extract
from nymeria.tools.file_edit import file_edit
from nymeria.tools.filesystem import file_read


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    yield


def _twelve_line_file(tmp_path):
    path = tmp_path / "sample.txt"
    # newline="\n" pins LF bytes on Windows too; the replace_range round-trip
    # pushes raw line context through file_edit, which is byte-honest.
    path.write_text(
        "".join(f"line {i}\n" for i in range(1, 13)),
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_offset_with_max_lines_returns_numbered_window_and_marker(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, artifact = file_read.func(str(path), max_lines=3, offset=5)

    assert artifact == {}
    lines = content.splitlines()
    assert lines[:3] == [
        f"{5:>6}\tline 5",
        f"{6:>6}\tline 6",
        f"{7:>6}\tline 7",
    ]
    assert lines[3] == "[Showing lines 5-7 of 12]"
    assert len(lines) == 4
    # No out-of-window content leaks.
    assert "line 4" not in content
    assert "line 8" not in content


def test_offset_without_max_lines_reads_to_eof(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, _ = file_read.func(str(path), offset=5)

    lines = content.splitlines()
    assert lines[0] == f"{5:>6}\tline 5"
    assert lines[-2] == f"{12:>6}\tline 12"
    assert lines[-1] == "[Showing lines 5-12 of 12]"
    assert len(lines) == 9  # lines 5..12 plus the marker


def test_offset_past_eof_errors_with_total_line_count(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, _ = file_read.func(str(path), offset=20)

    assert content.startswith("[Error]:")
    assert "offset 20" in content
    assert "12 lines" in content
    assert "\t" not in content  # no numbered content alongside the error


def test_offset_below_one_errors(tmp_path):
    path = _twelve_line_file(tmp_path)

    for bad in (0, -3):
        content, _ = file_read.func(str(path), offset=bad)
        assert content.startswith("[Error]:")
        assert "1-based" in content


def test_no_offset_output_is_unchanged_raw(tmp_path):
    path = _twelve_line_file(tmp_path)

    whole, _ = file_read.func(str(path))
    assert whole == path.read_text(encoding="utf-8")
    assert "Showing lines" not in whole

    truncated, _ = file_read.func(str(path), max_lines=2)
    assert truncated == "line 1\nline 2\n\n[Truncated after 2 lines]"
    assert "\t" not in truncated  # no line-number prefixes


def test_extraction_receives_raw_unnumbered_window(tmp_path, monkeypatch):
    path = _twelve_line_file(tmp_path)
    calls = {}

    def fake_extraction(content, prompt):
        calls["content"] = content
        return "EXTRACTED", "fake-model", False

    monkeypatch.setattr(llm_extract, "run_extraction", fake_extraction)

    content, _ = file_read.func(
        str(path), max_lines=3, offset=5, extraction_prompt="anything"
    )

    assert content == "EXTRACTED\n\n[Extracted by fake-model]"
    assert calls["content"] == "line 5\nline 6\nline 7\n"  # raw window only


def test_windowed_numbers_agree_with_file_edit_replace_range(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, _ = file_read.func(str(path), max_lines=2, offset=6)
    shown = [
        line.split("\t", 1) for line in content.splitlines() if "\t" in line
    ]
    start_line = int(shown[0][0])
    end_line = int(shown[-1][0])
    old_text = "".join(f"{text}\n" for _, text in shown)

    result = json.loads(
        file_edit.func(
            file_path=str(path),
            edits=[
                {
                    "operation": "replace_range",
                    "old_text": old_text,
                    "new_text": "REPLACED\n",
                    "start_line": start_line,
                    "end_line": end_line,
                }
            ],
        )
    )

    assert result["ok"] is True
    new_lines = path.read_text(encoding="utf-8").splitlines()
    assert new_lines[4] == "line 5"  # untouched neighbor before
    assert new_lines[5] == "REPLACED"  # lines 6-7 replaced in place
    assert new_lines[6] == "line 8"  # untouched neighbor after


def test_explicit_offset_one_is_navigation_mode(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, _ = file_read.func(str(path), offset=1)

    lines = content.splitlines()
    assert lines[0] == f"{1:>6}\tline 1"
    assert lines[-1] == "[Showing lines 1-12 of 12]"


# --- Edge sweep (plan addendum behaviors 9-14, 18) ---


def test_offset_at_exact_last_line_and_one_past(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, _ = file_read.func(str(path), offset=12)
    lines = content.splitlines()
    assert lines[0] == f"{12:>6}\tline 12"
    assert lines[-1] == "[Showing lines 12-12 of 12]"

    content, _ = file_read.func(str(path), offset=13)
    assert content.startswith("[Error]:")
    assert "12 lines" in content


def test_window_clipped_by_eof_reports_clipped_range(tmp_path):
    path = _twelve_line_file(tmp_path)

    content, _ = file_read.func(str(path), max_lines=100, offset=10)

    lines = content.splitlines()
    assert lines[-1] == "[Showing lines 10-12 of 12]"
    assert len(lines) == 4  # lines 10, 11, 12 plus the marker


def test_no_trailing_newline_marker_on_own_line(tmp_path):
    path = tmp_path / "tail.txt"
    path.write_text("a\nb\nc", encoding="utf-8", newline="\n")

    content, _ = file_read.func(str(path), offset=3)
    assert content.splitlines() == [
        f"{3:>6}\tc",
        "[Showing lines 3-3 of 3]",
    ]


def test_no_trailing_newline_extraction_window_has_no_phantom_newline(
    tmp_path, monkeypatch
):
    path = tmp_path / "tail.txt"
    path.write_text("a\nb\nc", encoding="utf-8", newline="\n")
    calls = {}

    def fake_extraction(content, prompt):
        calls["content"] = content
        return "X", "m", False

    monkeypatch.setattr(llm_extract, "run_extraction", fake_extraction)
    file_read.func(str(path), offset=2, extraction_prompt="x")
    assert calls["content"] == "b\nc"


def test_empty_file_offset_errors_with_zero_lines(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    content, _ = file_read.func(str(path), offset=1)
    assert content.startswith("[Error]:")
    assert "(0 lines)" in content


def test_negative_max_lines_errors_on_both_paths(tmp_path):
    path = _twelve_line_file(tmp_path)

    for kwargs in ({"max_lines": -1}, {"max_lines": -3, "offset": 5}):
        content, _ = file_read.func(str(path), **kwargs)
        assert content.startswith("[Error]:")
        assert "non-negative" in content


def test_max_lines_zero_means_no_limit(tmp_path):
    path = _twelve_line_file(tmp_path)

    legacy, _ = file_read.func(str(path), max_lines=0)
    assert legacy == path.read_text(encoding="utf-8")  # pre-existing meaning

    windowed, _ = file_read.func(str(path), max_lines=0, offset=11)
    assert windowed.splitlines()[-1] == "[Showing lines 11-12 of 12]"


def test_undecodable_byte_after_window_does_not_fail_windowed_read(tmp_path):
    path = tmp_path / "mixed.log"
    path.write_bytes(b"line 1\nline 2\nline 3\n\xff\xfe garbage\n")

    content, _ = file_read.func(str(path), max_lines=2, offset=1)
    lines = content.splitlines()
    assert lines[0] == f"{1:>6}\tline 1"
    assert lines[-1] == "[Showing lines 1-2 of 4]"  # bad line still counted

    # The same bad byte INSIDE the window is an honest decode error.
    content, _ = file_read.func(str(path), offset=4)
    assert content.startswith("[Error]:")
    assert "decode" in content.lower()


def test_crlf_file_windowed_read_displays_without_cr(tmp_path):
    path = tmp_path / "dos.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    content, _ = file_read.func(str(path), max_lines=2, offset=2)
    assert content.splitlines() == [
        f"{2:>6}\ttwo",
        f"{3:>6}\tthree",
        "[Showing lines 2-3 of 3]",
    ]
    assert "\r" not in content


def test_form_feed_is_line_content_not_a_line_break(tmp_path):
    # grep -n, editors, and file_edit.replace_range all treat \x0c as
    # content; splitlines() would call this a 4-line file.
    path = tmp_path / "feed.txt"
    path.write_bytes(b"a\nb\x0cc\nd\n")

    content, _ = file_read.func(str(path), offset=2)
    # Exact-string pin: splitlines() in the assert would itself split on
    # the form feed and mask the behavior under test.
    assert content == (
        f"{2:>6}\tb\x0cc\n{3:>6}\td\n[Showing lines 2-3 of 3]"
    )


def test_bare_cr_is_line_content_in_windowed_mode(tmp_path):
    # \n-only convention: an old-Mac style bare \r does not break a line in
    # windowed mode (agreeing with grep -n; the whole-file text-mode read
    # still applies universal newlines, a documented mode difference).
    path = tmp_path / "cr.txt"
    path.write_bytes(b"a\rb\rc\n")

    content, _ = file_read.func(str(path), offset=1)
    # Exact-string pin (splitlines() would split on the bare \r itself).
    assert content == f"{1:>6}\ta\rb\rc\n[Showing lines 1-1 of 1]"


def test_utf16_windowed_read_uses_decode_fallback(tmp_path):
    path = tmp_path / "wide.txt"
    path.write_bytes("alpha\nbeta\ngamma\n".encode("utf-16"))

    content, _ = file_read.func(str(path), encoding="utf-16", offset=2)
    lines = content.splitlines()
    assert lines[0] == f"{2:>6}\tbeta"
    assert lines[-1] == "[Showing lines 2-3 of 3]"


def test_offset_ignored_for_images(tmp_path, monkeypatch):
    from PIL import Image

    from nymeria.tools import filesystem

    path = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), "red").save(path, format="PNG")
    # Pin the deterministic no-active-agent note path, as the image tests do.
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _c: None)

    content, _ = file_read.func(str(path), offset=999)
    assert not content.startswith("[Error]:")  # image note, not an offset error
    assert "Showing lines" not in content
