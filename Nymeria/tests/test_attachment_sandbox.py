"""Unit tests for the per-thread attachment sandbox module."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from nymeria.core import attachment_sandbox


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the workspace root so tests don't write into /workspace."""
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    return tmp_path


def _data_url(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_resolve_unique_path_appends_friendly_suffix(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(b"x")
    resolved = attachment_sandbox._resolve_unique_path(tmp_path, "a.png")
    assert resolved.name == "a_2.png"


def test_resolve_unique_path_caps_friendly_probe_then_random(tmp_path: Path):
    # Occupy the base name plus every friendly _2.._20 slot.
    (tmp_path / "a.png").write_bytes(b"x")
    for i in range(2, attachment_sandbox._MAX_FRIENDLY_SUFFIX + 1):
        (tmp_path / f"a_{i}.png").write_bytes(b"x")
    resolved = attachment_sandbox._resolve_unique_path(tmp_path, "a.png")
    # Must not run past the cap to a_21; falls back to a random suffix.
    assert resolved.name != f"a_{attachment_sandbox._MAX_FRIENDLY_SUFFIX + 1}.png"
    assert resolved.name.startswith("a_")
    assert resolved.suffix == ".png"
    assert not resolved.exists()


def test_get_thread_attachment_dir_creates_subtree(_isolated_workspace: Path):
    path = attachment_sandbox.get_thread_attachment_dir("thread-a")
    assert path.exists()
    assert path.is_dir()
    assert path.name == "attachments"
    assert path.parent.name == "thread-a"


def test_write_attachment_plain_text_round_trip(_isolated_workspace: Path):
    record = attachment_sandbox.write_attachment(
        "t1",
        {
            "file_type": "document",
            "data_url": _data_url(b"hello world\nline 2", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "notes.txt",
        },
    )

    sandbox_path = Path(record.sandbox_path)
    assert sandbox_path.exists()
    assert sandbox_path.read_bytes() == b"hello world\nline 2"
    assert record.byte_size == 18
    assert record.original_name == "notes.txt"
    assert record.mime_type == "text/plain"
    assert record.extracted_text_path is not None
    assert Path(record.extracted_text_path).read_text() == "hello world\nline 2"
    meta_path = sandbox_path.with_suffix(sandbox_path.suffix + ".meta.json")
    meta = json.loads(meta_path.read_text())
    assert meta["sha256"] == record.sha256
    assert meta["original_name"] == "notes.txt"


def test_write_attachment_pdf_extracts_text(_isolated_workspace: Path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    pdf_path = _isolated_workspace / "src.pdf"
    with pdf_path.open("wb") as fh:
        writer.write(fh)

    pdf_bytes = pdf_path.read_bytes()
    record = attachment_sandbox.write_attachment(
        "t-pdf",
        {
            "file_type": "document",
            "data_url": _data_url(pdf_bytes, "application/pdf"),
            "mime_type": "application/pdf",
            "file_name": "blank.pdf",
        },
    )

    assert record.pages == 1
    assert record.extracted_text_path is not None
    text_path = Path(record.extracted_text_path)
    assert text_path.exists()
    # Blank page text is empty per pypdf; the extractor still writes a sibling.
    assert "Page 1" in text_path.read_text()
    _ = pypdf  # silence unused-import warning


def test_write_attachment_dedup_collision(_isolated_workspace: Path):
    file_data = {
        "file_type": "document",
        "data_url": _data_url(b"first", "text/plain"),
        "mime_type": "text/plain",
        "file_name": "dup.txt",
    }
    r1 = attachment_sandbox.write_attachment("t1", file_data)
    file_data2 = {**file_data, "data_url": _data_url(b"second", "text/plain")}
    r2 = attachment_sandbox.write_attachment("t1", file_data2)

    assert r1.sandbox_path != r2.sandbox_path
    assert Path(r1.sandbox_path).read_bytes() == b"first"
    assert Path(r2.sandbox_path).read_bytes() == b"second"
    # Second write should land at <stem>_2.<suffix> per dedup rule.
    assert Path(r2.sandbox_path).name == "dup_2.txt"


def test_write_attachment_sanitizes_filename(_isolated_workspace: Path):
    record = attachment_sandbox.write_attachment(
        "t1",
        {
            "file_type": "document",
            "data_url": _data_url(b"x", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "../../etc/passwd",
        },
    )

    sandbox_path = Path(record.sandbox_path)
    assert sandbox_path.parent.name == "attachments"
    assert ".." not in sandbox_path.name
    assert sandbox_path.is_relative_to(_isolated_workspace)


def test_write_attachment_rejects_oversize(_isolated_workspace: Path):
    huge = b"x" * 100
    with pytest.raises(ValueError, match="exceeds sandbox ceiling"):
        attachment_sandbox.write_attachment(
            "t1",
            {
                "file_type": "document",
                "data_url": _data_url(huge, "text/plain"),
                "mime_type": "text/plain",
                "file_name": "big.txt",
            },
            max_bytes=10,
        )


def test_write_attachment_rejects_empty_payload(_isolated_workspace: Path):
    with pytest.raises(ValueError, match="Empty attachment payload"):
        attachment_sandbox.write_attachment(
            "t1",
            {
                "file_type": "document",
                "data_url": "",
                "mime_type": "text/plain",
                "file_name": "empty.txt",
            },
        )


def test_cleanup_thread_attachments_removes_subtree(_isolated_workspace: Path):
    attachment_sandbox.write_attachment(
        "t-cleanup",
        {
            "file_type": "document",
            "data_url": _data_url(b"hello", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "a.txt",
        },
    )
    base = _isolated_workspace / "threads" / "t-cleanup"
    assert base.exists()
    # Original + .txt sibling + .meta.json = 3 files.
    removed = attachment_sandbox.cleanup_thread_attachments("t-cleanup")
    assert removed == 3
    assert not base.exists()


def test_cleanup_thread_attachments_idempotent(_isolated_workspace: Path):
    # Never-written thread returns 0 without raising.
    assert attachment_sandbox.cleanup_thread_attachments("never-existed") == 0


def test_build_attachment_preamble_includes_paths(_isolated_workspace: Path):
    r = attachment_sandbox.write_attachment(
        "t-preamble",
        {
            "file_type": "document",
            "data_url": _data_url(b"hello", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "readme.txt",
        },
    )
    preamble = attachment_sandbox.build_attachment_preamble([r])

    assert "readme.txt" in preamble
    assert "file_read" in preamble
    assert r.sandbox_path in preamble
    assert r.extracted_text_path in preamble


def test_find_attachment_by_id_round_trips(_isolated_workspace: Path):
    record = attachment_sandbox.write_attachment(
        "t-find",
        {
            "file_type": "document",
            "data_url": _data_url(b"abc", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "x.txt",
        },
    )
    found = attachment_sandbox.find_attachment_by_id("t-find", record.id)
    assert found is not None
    assert found.id == record.id
    assert found.original_name == "x.txt"
    assert found.sandbox_path == record.sandbox_path
    assert found.byte_size == 3


def test_find_attachment_by_id_returns_none_for_unknown(_isolated_workspace: Path):
    attachment_sandbox.write_attachment(
        "t-find",
        {
            "file_type": "document",
            "data_url": _data_url(b"abc", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "x.txt",
        },
    )
    assert attachment_sandbox.find_attachment_by_id("t-find", "does-not-exist") is None
    # And no dir at all also returns None (idempotent).
    assert attachment_sandbox.find_attachment_by_id("never-existed", "any") is None


def test_to_history_dict_excludes_bytes(_isolated_workspace: Path):
    r = attachment_sandbox.write_attachment(
        "t-history",
        {
            "file_type": "document",
            "data_url": _data_url(b"hi", "text/plain"),
            "mime_type": "text/plain",
            "file_name": "x.txt",
        },
    )
    payload = r.to_history_dict()

    assert payload["type"] == "document"
    assert payload["name"] == "x.txt"
    assert payload["size"] == 2
    assert payload["sandbox_path"] == r.sandbox_path
    assert "data_url" not in payload


# --------------------------------------------------------------------------- #
# Persistent user-attached images
# --------------------------------------------------------------------------- #

def test_prompt_attached_images_dir_layout(_isolated_workspace: Path):
    path = attachment_sandbox.prompt_attached_images_dir("alice")
    assert path.exists()
    assert path == _isolated_workspace / "images" / "prompt-attached" / "alice"


def test_persist_prompt_attached_image_writes_with_original_stem(_isolated_workspace: Path):
    saved = attachment_sandbox.persist_prompt_attached_image(
        "alice",
        data_url=_data_url(b"png-bytes-1", "image/png"),
        file_name="receipt.png",
        mime_type="image/png",
    )
    assert saved is not None
    assert saved.parent == _isolated_workspace / "images" / "prompt-attached" / "alice"
    assert saved.name.startswith("receipt-")
    assert saved.suffix == ".png"
    assert saved.read_bytes() == b"png-bytes-1"


def test_persist_prompt_attached_image_dedups_identical_bytes(_isolated_workspace: Path):
    first = attachment_sandbox.persist_prompt_attached_image(
        "bob", data_url=_data_url(b"same", "image/png"), file_name="a.png"
    )
    second = attachment_sandbox.persist_prompt_attached_image(
        "bob", data_url=_data_url(b"same", "image/png"), file_name="a.png"
    )
    different = attachment_sandbox.persist_prompt_attached_image(
        "bob", data_url=_data_url(b"other", "image/png"), file_name="a.png"
    )
    assert first == second  # same content hash -> same path
    assert different != first


def test_persist_prompt_attached_image_bad_data_url_returns_none(_isolated_workspace: Path):
    assert attachment_sandbox.persist_prompt_attached_image(
        "alice", data_url="not-a-data-url", file_name="x.png"
    ) is None


def test_build_attached_image_note_single_and_multiple():
    one = attachment_sandbox.build_attached_image_note(["/ws/images/prompt-attached/u/a-1.png"])
    assert "do NOT re-read" in one
    assert "/ws/images/prompt-attached/u/a-1.png" in one
    assert "workspace/images/" in one

    many = attachment_sandbox.build_attached_image_note(["/ws/a.png", "/ws/b.png"])
    assert "/ws/a.png" in many and "/ws/b.png" in many

    assert attachment_sandbox.build_attached_image_note([]) == ""
