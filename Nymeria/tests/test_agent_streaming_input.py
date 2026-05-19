"""Unit tests for prepare_astream_input extracted from NymeriaAgent."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage

from nymeria.core.agent_streaming_input import prepare_astream_input


def _fake_agent(
    *,
    pending_summary: str | None = None,
    pending_notepad: str | None = None,
    provider: str = "openai",
    model: str = "gpt-4o",
) -> Any:
    """Build a minimal agent stub exposing only the surface the function reads."""
    compaction = SimpleNamespace(
        format_user_resume=lambda msg, summ: f"[RESUME:{summ}]\n{msg}",
        pop_pending_notepad=lambda _tid: pending_notepad,
    )
    settings = SimpleNamespace(llm_provider=provider, llm_model=model)
    llm_cfg = SimpleNamespace(provider=None, model=None)
    return SimpleNamespace(
        get_pending_summary=lambda _tid: pending_summary,
        _compaction=compaction,
        _format_notepad_section=lambda np: f"\n\n[NOTEPAD:{np}]",
        _get_llm_config_for_thread=lambda _tid: llm_cfg,
        settings=settings,
    )


def _patch_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compatible: bool = True,
    unsupported: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> None:
    """Patch the lazy-imported model_capabilities helpers at their source module."""
    import nymeria.config.model_capabilities as mc

    monkeypatch.setattr(
        mc,
        "evaluate_attachment_compatibility",
        lambda _model, _provider, _atts: {
            "compatible": compatible,
            "unsupported_modalities": list(unsupported),
            "warnings": list(warnings),
        },
    )
    monkeypatch.setattr(mc, "infer_mime_type", lambda mime, _filename: mime)
    monkeypatch.setattr(
        mc,
        "normalize_attachment_file_type",
        lambda ftype, _mime, _filename: ftype,
    )


def test_no_attachments_user_message():
    agent = _fake_agent()
    state, summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="hello",
        thread_id="t1",
        attachments=None,
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert summary is None
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert isinstance(msg, HumanMessage)
    assert msg.content == "hello"
    assert msg.additional_kwargs == {}


def test_no_attachments_self_invoke_marks_internal():
    agent = _fake_agent()
    state, _summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="wake up",
        thread_id="t1",
        attachments=None,
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=True,
    )
    assert state is not None
    [msg] = state["messages"]
    assert msg.content == "wake up"
    assert msg.additional_kwargs == {
        "internal": True,
        "internal_type": "autonomous_wakeup",
    }


def test_pending_summary_prepends_resume():
    agent = _fake_agent(pending_summary="prior context")
    state, summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="continuing",
        thread_id="t1",
        attachments=None,
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert summary == "prior context"
    assert state is not None
    [msg] = state["messages"]
    assert msg.content == "[RESUME:prior context]\ncontinuing"


def test_pending_notepad_extends_message():
    agent = _fake_agent(pending_notepad="todo: ship")
    state, _summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="check",
        thread_id="t1",
        attachments=None,
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert state is not None
    [msg] = state["messages"]
    assert msg.content == "check\n\n[NOTEPAD:todo: ship]"


def test_legacy_images_param_merged_into_attachments(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(monkeypatch)
    agent = _fake_agent()
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="see this",
        thread_id="t1",
        attachments=None,
        images=[{"data_url": "data:image/png;base64,AAA", "mime_type": "image/png"}],
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    parts = msg.content
    assert parts[0] == {"type": "text", "text": "see this"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAA"},
    }


def test_image_attachment_compatible_model(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(monkeypatch, compatible=True)
    agent = _fake_agent()
    attachments = [{
        "file_type": "image",
        "data_url": "data:image/png;base64,XYZ",
        "mime_type": "image/png",
    }]
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="describe",
        thread_id="t1",
        attachments=attachments,
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert msg.content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,XYZ"},
    }


def test_image_attachment_incompatible_blocks_by_default(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(
        monkeypatch,
        compatible=False,
        unsupported=("vision",),
        warnings=("No vision in this model",),
    )
    agent = _fake_agent(model="text-only-model")
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="look",
        thread_id="t1",
        attachments=[{
            "file_type": "image",
            "data_url": "data:image/png;base64,AAA",
            "mime_type": "image/png",
        }],
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert state is None
    assert error is not None
    assert error["type"] == "error"
    assert "text-only-model" in error["content"]
    assert "vision" in error["content"]
    assert "No vision in this model" in error["content"]


def test_image_attachment_incompatible_forced_succeeds(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(
        monkeypatch,
        compatible=False,
        unsupported=("vision",),
        warnings=("Mock warning",),
    )
    agent = _fake_agent()
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="look",
        thread_id="t1",
        attachments=[{
            "file_type": "image",
            "data_url": "data:image/png;base64,AAA",
            "mime_type": "image/png",
        }],
        images=None,
        force_unsupported_attachments=True,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert msg.content[1]["type"] == "image_url"


def test_pdf_attachment_emits_file_part(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(monkeypatch)
    agent = _fake_agent()
    pdf_b64 = base64.b64encode(b"%PDF-1.4\n...").decode("ascii")
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="read this",
        thread_id="t1",
        attachments=[{
            "file_type": "document",
            "data_url": f"data:application/pdf;base64,{pdf_b64}",
            "mime_type": "application/pdf",
        }],
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert msg.content[1] == {
        "type": "file",
        "source_type": "base64",
        "mime_type": "application/pdf",
        "data": pdf_b64,
    }


def test_text_file_attachment_decoded_into_text_part(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(monkeypatch)
    agent = _fake_agent()
    text_b64 = base64.b64encode(b"hello world").decode("ascii")
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="summarize",
        thread_id="t1",
        attachments=[{
            "file_type": "document",
            "data_url": f"data:text/plain;base64,{text_b64}",
            "mime_type": "text/plain",
        }],
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    text_part = msg.content[1]
    assert text_part["type"] == "text"
    assert "hello world" in text_part["text"]
    assert "Attached PLAIN file" in text_part["text"]
    assert "End of file" in text_part["text"]


def test_text_file_attachment_bad_base64_falls_back_to_placeholder(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_capabilities(monkeypatch)
    agent = _fake_agent()
    # "////" base64-decodes to b"\xff\xff\xff", which is not valid UTF-8 ->
    # UnicodeDecodeError fires inside the try block, exercising the fallback.
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="read",
        thread_id="t1",
        attachments=[{
            "file_type": "document",
            "data_url": "data:text/plain;base64,////",
            "mime_type": "text/plain",
        }],
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    text_part = msg.content[1]
    assert text_part["type"] == "text"
    assert "Failed to read attached text file" in text_part["text"]


def test_unsupported_attachment_returns_error_dict(monkeypatch: pytest.MonkeyPatch):
    _patch_capabilities(monkeypatch)
    agent = _fake_agent()
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="zip me",
        thread_id="t1",
        attachments=[{
            "file_type": "archive",
            "data_url": "data:application/zip;base64,AAA",
            "mime_type": "application/zip",
        }],
        images=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert state is None
    assert error is not None
    assert error["type"] == "error"
    assert "Unsupported attachment type" in error["content"]
