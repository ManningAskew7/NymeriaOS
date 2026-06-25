"""Tests for nymeria.triggers.attachment_helpers.

Focused on finalize_attachments (slice 21 F12): the per-message file-count cap
shared by the Telegram and Discord bots. The module had no prior direct
coverage, so a couple of build_attachment characterization cases (which also
exercise classify/infer_mime_type indirectly) are included alongside.
"""

from __future__ import annotations

from typing import Any, Dict, List

from nymeria.triggers import attachment_helpers
from nymeria.triggers.attachment_helpers import MAX_FILES_PER_MESSAGE, finalize_attachments


def _att(name: str) -> Dict[str, Any]:
    return {"file_type": "image", "data_url": "data:,", "mime_type": "image/png", "file_name": name}


def test_finalize_under_cap_passthrough():
    atts: List[Dict[str, Any]] = [_att("a"), _att("b")]
    errors: List[str] = []
    out_atts, out_errors = finalize_attachments(atts, errors)
    assert out_atts == atts
    assert out_atts is atts  # no truncation -> same object returned
    assert out_errors == []
    assert out_errors is errors


def test_finalize_at_cap_no_truncation():
    atts = [_att(str(i)) for i in range(MAX_FILES_PER_MESSAGE)]
    errors: List[str] = []
    out_atts, out_errors = finalize_attachments(atts, errors)
    assert len(out_atts) == MAX_FILES_PER_MESSAGE
    assert out_errors == []


def test_finalize_over_cap_truncates_and_appends_notice():
    atts = [_att(str(i)) for i in range(MAX_FILES_PER_MESSAGE + 2)]
    errors: List[str] = []
    out_atts, out_errors = finalize_attachments(atts, errors)
    # Keeps the first MAX_FILES_PER_MESSAGE, drops the tail.
    assert len(out_atts) == MAX_FILES_PER_MESSAGE
    assert out_atts == atts[:MAX_FILES_PER_MESSAGE]
    # Exactly one notice with the canonical wording.
    assert out_errors == [
        f"Skipped 2 extra file(s). Max is {MAX_FILES_PER_MESSAGE} per message."
    ]


def test_finalize_preserves_existing_errors_and_appends_after():
    atts = [_att(str(i)) for i in range(MAX_FILES_PER_MESSAGE + 1)]
    errors = ["earlier problem"]
    _out_atts, out_errors = finalize_attachments(atts, errors)
    assert out_errors == [
        "earlier problem",
        f"Skipped 1 extra file(s). Max is {MAX_FILES_PER_MESSAGE} per message.",
    ]
    # Mutates the same errors list in place.
    assert out_errors is errors


def test_finalize_empty_is_noop():
    out_atts, out_errors = finalize_attachments([], [])
    assert out_atts == []
    assert out_errors == []


def test_build_attachment_rejects_unsupported_type():
    att, err = attachment_helpers.build_attachment(b"data", "application/zip", "a.zip")
    assert att is None
    assert err is not None
    assert "Unsupported file type" in err


def test_build_attachment_accepts_png():
    att, err = attachment_helpers.build_attachment(b"\x89PNG", "image/png", "a.png")
    assert err is None
    assert att is not None
    assert att["file_type"] == "image"
    assert att["mime_type"] == "image/png"
    assert att["data_url"].startswith("data:image/png;base64,")
