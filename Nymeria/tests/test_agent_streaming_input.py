"""Unit tests for prepare_astream_input extracted from NymeriaAgent."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage

from nymeria.core.agent_streaming_input import prepare_astream_input
from nymeria.core.attachment_sandbox import AttachmentRecord


def _fake_agent(
    *,
    provider: str = "openai",
    model: str = "gpt-4o",
) -> Any:
    """Build a minimal agent stub exposing only the surface the function reads."""
    settings = SimpleNamespace(llm_provider=provider, llm_model=model)
    llm_cfg = SimpleNamespace(provider=None, model=None)
    return SimpleNamespace(
        _get_llm_config_for_thread=lambda _tid: llm_cfg,
        settings=settings,
    )


def _patch_image_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compatible: bool = True,
    unsupported: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> None:
    """Patch evaluate_attachment_compatibility (only called for image_attachments)."""
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


def _record(name: str = "doc.pdf", *, mime: str = "application/pdf") -> AttachmentRecord:
    """Build an in-memory sandbox record without touching disk."""
    return AttachmentRecord(
        id="rec-1",
        thread_id="t1",
        original_name=name,
        mime_type=mime,
        byte_size=1234,
        sandbox_path=f"/workspace/threads/t1/attachments/{name}",
        extracted_text_path=f"/workspace/threads/t1/attachments/{name}.txt",
        sha256="deadbeef",
        pages=12 if mime == "application/pdf" else None,
    )


def test_no_attachments_user_message():
    agent = _fake_agent()
    state, summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="hello",
        thread_id="t1",
        image_attachments=None,
        sandbox_records=None,
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
        image_attachments=None,
        sandbox_records=None,
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


def test_user_message_id_stamped_on_text_and_image_paths(
    monkeypatch: pytest.MonkeyPatch,
):
    """The route-minted anchor id becomes the HumanMessage's graph id.

    LangGraph's add_messages preserves caller-set ids, so the persisted
    message carries the same id the turn stream buffer exposes to
    live-attach viewers (history renders it as ``message_id``).
    """
    agent = _fake_agent()
    state, _summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="hello",
        thread_id="t1",
        image_attachments=None,
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
        user_message_id="anchor-1",
    )
    assert state is not None
    assert state["messages"][0].id == "anchor-1"

    _patch_image_compatibility(monkeypatch, compatible=True)
    state, _summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="look at this",
        thread_id="t1",
        image_attachments=[
            {"data_url": "data:image/png;base64,AAAA", "mime_type": "image/png"}
        ],
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
        user_message_id="anchor-2",
    )
    assert state is not None
    assert state["messages"][0].id == "anchor-2"


def test_user_message_id_default_leaves_id_unset():
    agent = _fake_agent()
    state, _summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="hello",
        thread_id="t1",
        image_attachments=None,
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert state is not None
    assert state["messages"][0].id is None


def test_add_messages_reducer_preserves_caller_set_id():
    """Pin the vendor behavior the live-attach anchor relies on.

    The whole anchor chain is: the chat route mints ``user_message_id``,
    ``prepare_astream_input`` stamps it as ``HumanMessage.id`` (pinned
    above), the graph's ``add_messages`` reducer persists it unchanged
    (pinned HERE), and history rendering re-exposes it as ``message_id``
    (pinned in test_agent_history.py). If LangGraph ever started
    reassigning caller-set ids, viewers could never resolve the anchor.
    """
    from langchain_core.messages import HumanMessage
    from langgraph.graph.message import add_messages

    merged = add_messages([], [HumanMessage(content="hi", id="anchor-9")])
    assert [m.id for m in merged] == ["anchor-9"]

    # And merging further messages must not rewrite the existing id.
    merged = add_messages(merged, [HumanMessage(content="again")])
    assert merged[0].id == "anchor-9"


def test_no_compaction_resume_attachment():
    """prepare_astream_input no longer glues a pending summary/notepad onto the
    user message; carried context lives in the retained resume-tail in state."""
    agent = _fake_agent()
    state, summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="continuing",
        thread_id="t1",
        image_attachments=None,
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert summary is None
    assert state is not None
    [msg] = state["messages"]
    assert msg.content == "continuing"


def test_sandbox_only_preserves_text_content(monkeypatch: pytest.MonkeyPatch):
    agent = _fake_agent()
    record = _record("report.pdf")
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="preamble already in here",
        thread_id="t1",
        image_attachments=None,
        sandbox_records=[record],
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    # No image_url blocks for a non-image-only turn: content stays plain text.
    assert msg.content == "preamble already in here"
    metadata = msg.additional_kwargs["attachments"]
    assert len(metadata) == 1
    assert metadata[0]["type"] == "document"
    assert metadata[0]["name"] == "report.pdf"
    assert metadata[0]["sandbox_path"].endswith("report.pdf")
    assert metadata[0]["pages"] == 12


def test_image_attachment_inlines_image_url(monkeypatch: pytest.MonkeyPatch):
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    image_att = {
        "file_type": "image",
        "data_url": "data:image/png;base64,XYZ",
        "mime_type": "image/png",
        "file_name": "snap.png",
    }
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="describe",
        thread_id="t1",
        image_attachments=[image_att],
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert msg.content[0] == {"type": "text", "text": "describe"}
    assert msg.content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,XYZ"},
    }
    metadata = msg.additional_kwargs["attachments"]
    assert metadata[0]["type"] == "image"
    assert metadata[0]["name"] == "snap.png"


def test_image_plus_sandbox_emits_both_in_metadata(monkeypatch: pytest.MonkeyPatch):
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="look at this",
        thread_id="t1",
        image_attachments=[{
            "file_type": "image",
            "data_url": "data:image/png;base64,AAA",
            "mime_type": "image/png",
            "file_name": "fig.png",
        }],
        sandbox_records=[_record("data.csv", mime="text/csv")],
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    # Image is inlined; sandbox doc is metadata-only.
    assert msg.content[1]["type"] == "image_url"
    metadata = msg.additional_kwargs["attachments"]
    types = {item["type"] for item in metadata}
    assert types == {"image", "document"}


def test_image_attachment_incompatible_blocks_by_default(monkeypatch: pytest.MonkeyPatch):
    _patch_image_compatibility(
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
        image_attachments=[{
            "file_type": "image",
            "data_url": "data:image/png;base64,AAA",
            "mime_type": "image/png",
        }],
        sandbox_records=None,
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
    _patch_image_compatibility(
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
        image_attachments=[{
            "file_type": "image",
            "data_url": "data:image/png;base64,AAA",
            "mime_type": "image/png",
        }],
        sandbox_records=None,
        force_unsupported_attachments=True,
        is_self_invoke=False,
    )
    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert msg.content[1]["type"] == "image_url"


def test_sandbox_record_self_invoke_marks_internal():
    agent = _fake_agent()
    state, _summary, _error = prepare_astream_input(
        cast(Any, agent),
        message_with_context="auto-wake with file",
        thread_id="t1",
        image_attachments=None,
        sandbox_records=[_record("data.csv", mime="text/csv")],
        force_unsupported_attachments=False,
        is_self_invoke=True,
    )
    assert state is not None
    [msg] = state["messages"]
    assert msg.additional_kwargs.get("internal") is True
    assert msg.additional_kwargs.get("internal_type") == "autonomous_wakeup"
    metadata = msg.additional_kwargs["attachments"]
    assert metadata[0]["name"] == "data.csv"
