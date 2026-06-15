"""The chat/astream attachment hook persists images and notes their path."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.agent import NymeriaAgent


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    return tmp_path


def _sandbox(message, images):
    # The method does not use ``self``; call it unbound with a dummy receiver.
    return NymeriaAgent._sandbox_pending_attachments(
        SimpleNamespace(), "thread-x", "alice", message, None, images
    )


def test_image_attachment_is_persisted_and_noted(_workspace: Path):
    images = [{"data_url": _data_url(b"png-bytes"), "mime_type": "image/png", "file_name": "shot.png"}]

    message, image_attachments, sandbox_records, error = _sandbox("hello", images)

    assert error is None
    assert len(image_attachments) == 1  # still sent inline this turn
    assert sandbox_records == []
    # The image is persisted to the per-user images dir.
    saved = list((_workspace / "images" / "prompt-attached" / "alice").glob("shot-*.png"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"png-bytes"
    # The path note is prepended to the message, ahead of the user's text.
    assert message.startswith("[Attached image saved to")
    assert "do NOT re-read" in message
    assert message.rstrip().endswith("hello")


def test_no_attachments_leaves_message_unchanged(_workspace: Path):
    message, image_attachments, sandbox_records, error = _sandbox("just text", None)
    assert message == "just text"
    assert image_attachments == []
    assert error is None
