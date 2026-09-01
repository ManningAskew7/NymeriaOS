"""Tests for file_read's attach=True and attach_only=True paths.

Spec: tmp/file-attach-binary-plan.md (expected behaviors 2-7 for attach,
9-11 for attach_only). Behavior 1 (attach=False is unchanged, plus the new
hint sentence) lives in
test_file_read_image.py::test_non_image_binary_uses_text_path, next to the
pre-existing pin it extends. Behavior 8 (file_write's attach path) lives in
test_file_write_attach.py.

Backstory: a generated PDF handed to file_write(attach=True, content="x")
truncated a 45,355-byte file to 1 byte, because file_write's attach can only
deliver what it just wrote. file_read never writes, so attach=True there is
the safe path for an EXISTING file (text, image, or arbitrary binary); the
byte-identity assertions below are the direct regression pin for that
incident. attach_only=True goes one step further: it never reads the file's
content into the tool return at all (no decode, no image handling), so
delivering an existing file costs no context tokens beyond a filename and a
byte count.
"""

from __future__ import annotations

import pytest
from PIL import Image

from nymeria.core import generated_image_context
from nymeria.tools import filesystem
from nymeria.tools.filesystem import file_read
from nymeria.vendor.react_agent.config import LLMConfig

_VISION_CFG = LLMConfig(provider="anthropic", model="claude-sonnet-4")
_CFG = {"configurable": {"thread_id": "t1", "user_id": "u1"}}


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    yield


def _binary_bytes() -> bytes:
    # Not valid text in any common encoding, and not a recognized image
    # magic: forces the UnicodeDecodeError path used by the actual bug
    # report (a PDF), without depending on a real PDF library.
    return b"%PDF-1.4\n\x00\x01\x02\x80\x81\xfe\xffbinary content here\n" * 50


def test_attach_true_text_file_reads_normally_and_appends_tag(tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("hello world\n", encoding="utf-8")

    content, artifact = file_read.func(str(path), attach=True)

    assert artifact == {}
    assert content.startswith("hello world")
    assert f"[attach:{path.resolve()}]" in content


def test_attach_true_binary_file_succeeds_and_leaves_bytes_untouched(tmp_path):
    path = tmp_path / "doc.pdf"
    original_bytes = _binary_bytes()
    path.write_bytes(original_bytes)

    content, artifact = file_read.func(str(path), attach=True)

    assert not content.startswith("[Error]")
    assert content.startswith("[Success]")
    assert "doc.pdf" in content
    assert f"[attach:{path.resolve()}]" in content
    assert artifact == {}
    # The core regression: file_read must never write, so the bytes on disk
    # are exactly what they were before the call.
    assert path.read_bytes() == original_bytes


def test_attach_false_binary_file_still_errors_unchanged_shape(tmp_path):
    # Companion to the attach=True test above: confirms attach=True is what
    # changes the outcome, not some unrelated change to binary handling.
    path = tmp_path / "doc.pdf"
    path.write_bytes(_binary_bytes())

    content, artifact = file_read.func(str(path))

    assert content.startswith("[Error]")
    assert "[attach:" not in content
    assert artifact == {}


def test_attach_true_oversized_text_file_succeeds_without_reading_it(tmp_path):
    path = tmp_path / "huge.txt"
    # Just over file_read's 10 MB text cap; plain decodable ASCII so the only
    # thing that can be under test is the size branch, not the decode one.
    path.write_bytes(b"a" * (10 * 1024 * 1024 + 1))

    content, artifact = file_read.func(str(path), attach=True)

    assert not content.startswith("[Error]")
    assert content.startswith("[Success]")
    assert f"[attach:{path.resolve()}]" in content
    assert artifact == {}


def test_attach_false_oversized_text_file_still_errors_unchanged_shape(tmp_path):
    path = tmp_path / "huge.txt"
    path.write_bytes(b"a" * (10 * 1024 * 1024 + 1))

    content, _ = file_read.func(str(path))

    assert content.startswith("[Error]: File too large")
    assert "[attach:" not in content


def test_attach_true_outside_workspace_skips_with_info_note(tmp_path, tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside-workspace")
    path = outside_dir / "notes.txt"
    path.write_text("secret notes\n", encoding="utf-8")

    content, artifact = file_read.func(str(path), attach=True)

    assert content.startswith("secret notes")  # original content unchanged
    assert "[attach:" not in content
    assert "[Info]: Attachment skipped." in content
    assert str(tmp_path) in content  # names the ALLOWED (workspace) dir
    assert artifact == {}


def test_attach_true_binary_outside_workspace_keeps_original_error(tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside-workspace")
    path = outside_dir / "doc.pdf"
    path.write_bytes(_binary_bytes())

    content, _ = file_read.func(str(path), attach=True)

    # Nothing to attach outside the workspace, so the decode failure stands,
    # with a note explaining why attach did not help.
    assert content.startswith("[Error]: Cannot decode")
    assert "[attach:" not in content
    assert "[Info]: Attachment skipped." in content


def test_attach_true_with_offset_appends_tag_after_window(tmp_path):
    path = tmp_path / "lines.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 6)), encoding="utf-8")

    content, _ = file_read.func(str(path), offset=2, max_lines=2, attach=True)

    assert "[Showing lines 2-3 of 5]" in content
    assert content.endswith(f"[attach:{path.resolve()}]")


def test_attach_true_with_extraction_appends_tag_after_attribution(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("alpha beta\n", encoding="utf-8")
    monkeypatch.setattr(
        "nymeria.tools.llm_extract.run_extraction",
        lambda content, prompt: ("EXTRACTED", "fake-model", False),
    )

    content, _ = file_read.func(str(path), extraction_prompt="anything", attach=True)

    assert content.startswith("EXTRACTED\n\n[Extracted by fake-model]")
    assert content.endswith(f"[attach:{path.resolve()}]")


def test_attach_true_offset_past_eof_still_attaches_alongside_the_error(tmp_path):
    # The file itself is fine and in the workspace; only the requested
    # window is out of range, so attach should still deliver it (the caller
    # asked for attach unconditionally, not "attach only if offset succeeds").
    path = tmp_path / "lines.txt"
    path.write_text("line 1\nline 2\n", encoding="utf-8")

    content, _ = file_read.func(str(path), offset=99, attach=True)

    assert content.startswith("[Error]: offset 99")
    assert content.endswith(f"[attach:{path.resolve()}]")


def test_attach_true_extraction_failure_still_attaches_alongside_the_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "notes.txt"
    path.write_text("alpha beta\n", encoding="utf-8")
    monkeypatch.setattr(
        "nymeria.tools.llm_extract.run_extraction",
        lambda content, prompt: ("[Error]: extraction backend unavailable", "", False),
    )

    content, _ = file_read.func(str(path), extraction_prompt="anything", attach=True)

    assert content.startswith("[Error]: extraction backend unavailable")
    assert content.endswith(f"[attach:{path.resolve()}]")


def _png(path, size=(8, 8), color="red"):
    Image.new("RGB", size, color).save(path, format="PNG")


def test_attach_true_image_appends_tag_alongside_vision_note(tmp_path, monkeypatch):
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: True)
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _c: _VISION_CFG)
    path = tmp_path / "pic.png"
    _png(path)

    content, artifact = file_read.func(str(path), config=_CFG, attach=True)

    assert content.startswith("Loaded image 'pic.png'")
    assert content.endswith(f"[attach:{path.resolve()}]")
    assert artifact != {}  # native-vision artifact is still attached too


def test_attach_true_unreadable_image_still_delivers_the_raw_file(tmp_path, monkeypatch):
    # A genuinely corrupt image (Pillow identifies it, then fails to decode
    # it) still errors on its vision note, but the raw bytes are still a
    # legitimate thing to hand back to the user: attach is about delivery,
    # not about whether the model could make sense of the contents. Forcing
    # the Pillow-internal failure via a mock (rather than hand-crafting
    # corrupt PNG bytes) follows the same approach as
    # test_generated_image_context.py.
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: True)
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _c: _VISION_CFG)
    monkeypatch.setattr(
        filesystem,
        "prepare_image_for_native_context",
        lambda *_a, **_kw: (None, None, "image is corrupt or unreadable (fake)"),
    )
    path = tmp_path / "broken.png"
    _png(path)  # a real PNG; the mock above is what forces the error branch

    content, artifact = file_read.func(str(path), config=_CFG, attach=True)

    assert content.startswith("[Error]: Cannot read image")
    assert content.endswith(f"[attach:{path.resolve()}]")
    assert artifact == {}


def test_attach_true_image_extension_non_image_falls_back_and_still_attaches(
    tmp_path, monkeypatch
):
    # Magic bytes/extension say image, Pillow disagrees entirely (not a
    # decode failure, just "not a usable image"): file_read falls back to
    # the text path, same as test_png_extension_on_text_file_falls_back_to_text
    # in test_file_read_image.py, except this content isn't text either, so
    # attach=True is what turns the resulting UnicodeDecodeError into a
    # successful delivery instead of an [Error].
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: True)
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _c: _VISION_CFG)
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00not-a-real-png")

    content, artifact = file_read.func(str(path), config=_CFG, attach=True)

    assert content.startswith("[Success]")
    assert "not decodable as utf-8" in content
    assert content.endswith(f"[attach:{path.resolve()}]")
    assert artifact == {}


# --- attach_only=True: deliver without ever reading the content ---


def test_attach_only_delivers_without_reading_content(tmp_path):
    path = tmp_path / "report.csv"
    original_bytes = b"a,b,c\n" * 1000
    path.write_bytes(original_bytes)

    content, artifact = file_read.func(str(path), attach_only=True)

    assert content.startswith("[Success]")
    assert "report.csv" in content
    assert str(len(original_bytes)) in content
    assert f"[attach:{path.resolve()}]" in content
    assert artifact == {}
    # The actual proof this saves context: none of the file's own bytes
    # (its content, as opposed to its name/size) appear in the return.
    assert "a,b,c" not in content
    assert path.read_bytes() == original_bytes  # never touched


def test_attach_only_ignores_extraction_prompt_and_never_calls_the_llm(
    tmp_path, monkeypatch
):
    path = tmp_path / "notes.txt"
    path.write_text("alpha beta\n", encoding="utf-8")

    def boom(content, prompt):
        raise AssertionError("attach_only must never read/extract the file")

    monkeypatch.setattr("nymeria.tools.llm_extract.run_extraction", boom)

    content, _ = file_read.func(
        str(path), extraction_prompt="anything", attach_only=True
    )

    assert content.startswith("[Success]")
    assert "alpha beta" not in content


def test_attach_only_binary_file_never_attempts_a_decode(tmp_path):
    # The whole point: a binary file that would UnicodeDecodeError under a
    # normal read (or even under attach=True's read-then-catch) is delivered
    # cleanly because attach_only skips the read attempt entirely.
    path = tmp_path / "doc.pdf"
    original_bytes = _binary_bytes()
    path.write_bytes(original_bytes)

    content, artifact = file_read.func(str(path), attach_only=True)

    assert content.startswith("[Success]")
    assert "decode" not in content.lower()
    assert f"[attach:{path.resolve()}]" in content
    assert artifact == {}
    assert path.read_bytes() == original_bytes


def test_attach_only_image_never_touches_vision_path(tmp_path, monkeypatch):
    # attach_only must not even sniff for images: hitting the vision
    # machinery would defeat the "never read the file" guarantee.
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: True)

    def boom(_config):
        raise AssertionError("attach_only must not resolve an LLM config")

    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", boom)
    path = tmp_path / "pic.png"
    _png(path)

    content, artifact = file_read.func(str(path), config=_CFG, attach_only=True)

    assert content.startswith("[Success]")
    assert "Loaded image" not in content
    assert f"[attach:{path.resolve()}]" in content
    assert artifact == {}


def test_attach_only_outside_workspace_errors_with_no_content(tmp_path_factory):
    outside_dir = tmp_path_factory.mktemp("outside-workspace")
    path = outside_dir / "notes.txt"
    path.write_text("secret notes\n", encoding="utf-8")

    content, artifact = file_read.func(str(path), attach_only=True)

    assert content.startswith("[Error]: Cannot attach")
    assert "secret notes" not in content
    assert "[attach:" not in content
    assert artifact == {}


def test_attach_only_missing_file_still_errors_normally(tmp_path):
    missing = tmp_path / "nope.txt"

    content, _ = file_read.func(str(missing), attach_only=True)

    assert content.startswith("[Error]: File not found")


def test_attach_only_true_overrides_attach_true(tmp_path):
    # Passing both is not an error; attach_only simply wins (no content is
    # ever read to be shown alongside the attachment).
    path = tmp_path / "notes.txt"
    path.write_text("alpha beta\n", encoding="utf-8")

    content, _ = file_read.func(str(path), attach=True, attach_only=True)

    assert content.startswith("[Success]")
    assert "alpha beta" not in content
