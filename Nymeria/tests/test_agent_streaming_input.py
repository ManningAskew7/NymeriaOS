"""Unit tests for prepare_astream_input extracted from NymeriaAgent."""

from __future__ import annotations

import base64
import io
import logging
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
    """Build a minimal agent stub exposing only the surface the function reads.

    ``thread_config_manager`` is stubbed to "no config for this thread" so the
    pending-fallback-note lookup returns quietly instead of raising into its
    best-effort handler and logging a warning on every single turn built here.
    """
    settings = SimpleNamespace(llm_provider=provider, llm_model=model)
    llm_cfg = SimpleNamespace(provider=None, model=None)
    return SimpleNamespace(
        _get_llm_config_for_thread=lambda _tid: llm_cfg,
        settings=settings,
        thread_config_manager=SimpleNamespace(get_config=lambda _tid: None),
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


def _b64_image(fmt: str, size: tuple[int, int] = (4, 4)) -> str:
    """Real encoded image bytes, base64'd, so the sniffer sees true magic bytes."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


_PNG_URL = f"data:image/png;base64,{_b64_image('PNG')}"


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
            {"data_url": _PNG_URL, "mime_type": "image/png"}
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
        "data_url": _PNG_URL,
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
        "image_url": {"url": _PNG_URL},
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
            "data_url": _PNG_URL,
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
            "data_url": _PNG_URL,
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
            "data_url": _PNG_URL,
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


def _image_blocks(msg: HumanMessage) -> list[dict[str, Any]]:
    return [part for part in msg.content if isinstance(part, dict) and part.get("type") == "image_url"]


def _clauses(msg: HumanMessage) -> list[str]:
    """The disclosure text blocks the ingress gate appends after the images."""
    return [
        part["text"] for part in msg.content[1:]
        if isinstance(part, dict) and part.get("type") == "text"
    ]


def _decoded(data_url: str):
    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(data_url.partition(",")[2])))


def _decoded_format(data_url: str) -> str:
    with _decoded(data_url) as img:
        return img.format or ""


def _relabel_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """WARNINGs from the module under test only.

    Counting every logger's records made these assertions order-dependent:
    the first test to touch ``nymeria.tools`` pays that package's import and
    anything it logs would be attributed to the correction.
    """
    return [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING and rec.name == "nymeria.core.agent_streaming_input"
    ]


def test_gif_bytes_declared_png_are_relabeled_before_checkpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A mislabeled GIF is relabeled from its bytes, not from its header.

    Microsoft Graph hands the email trigger a ``contentType`` it never
    verifies, so a signature GIF labeled ``image/png`` reaches this function
    verbatim. Anthropic 400s the WHOLE request on the mismatch ("the image
    appears to be a image/gif image"), and history replays verbatim, so the
    thread is bricked permanently: one production thread died for 16 days
    that way. The bytes have to win here, before the block becomes state.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    payload = _b64_image("GIF")
    image_att = {
        "file_type": "image",
        "data_url": f"data:image/png;base64,{payload}",
        "mime_type": "image/png",
        "file_name": "signature.png",
        # Rails set upstream by _sandbox_pending_attachments: a correction
        # must carry them through, not rebuild the attachment from scratch.
        "workspace_path": "/workspace/images/signature-abc123.png",
        "width": 903,
        "height": 302,
    }

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context="new email",
            thread_id="t1",
            image_attachments=[cast(Any, image_att)],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=True,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    [block] = _image_blocks(msg)
    # The declared media type now matches the bytes...
    assert block["image_url"]["url"].startswith("data:image/gif;base64,")
    # ...and only the label changed: the payload is byte-identical.
    assert block["image_url"]["url"].split(",", 1)[1] == payload

    metadata = msg.additional_kwargs["attachments"]
    assert metadata[0]["mime_type"] == "image/gif"
    assert metadata[0]["data_url"].startswith("data:image/gif;base64,")
    # Upstream rails survive the correction.
    assert metadata[0]["workspace_path"] == "/workspace/images/signature-abc123.png"
    assert (metadata[0]["width"], metadata[0]["height"]) == (903, 302)

    warnings = _relabel_warnings(caplog)
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "image/png" in text and "image/gif" in text and "signature.png" in text


def test_lying_mime_type_field_loses_to_the_bytes_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The bytes win on BOTH declarations, not just the data-URL header.

    The two can diverge: the desktop's fileProcessing.ts derives the header
    and the ``mime_type`` field separately, so an honest header can arrive
    beside a lying field. The field is what history rendering and the
    attachment pills read, so leaving it wrong leaves a wrong answer in the
    thread even when the wire payload is right.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    payload = _b64_image("GIF")
    data_url = f"data:image/gif;base64,{payload}"

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context="look",
            thread_id="t1",
            image_attachments=[{
                "file_type": "image",
                "data_url": data_url,
                "mime_type": "image/png",
                "file_name": "banner.gif",
            }],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=False,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    [block] = _image_blocks(msg)
    # The header was already honest, so it is unchanged...
    assert block["image_url"]["url"] == data_url
    # ...and the field that contradicted the bytes is corrected.
    assert msg.additional_kwargs["attachments"][0]["mime_type"] == "image/gif"
    assert len(_relabel_warnings(caplog)) == 1


def test_bmp_bytes_declared_png_are_converted_not_relabeled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Only a provider-supported sniff may relabel a block.

    ``sniff_image_mime`` also recognizes bmp and tiff, and Anthropic accepts
    neither: relabeling to one would swap a permanent wedge for a permanent
    wedge. The label correction leaves it alone; the ingress gate one step
    later CONVERTS the bytes to a native format instead, and says so.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    data_url = f"data:image/png;base64,{_b64_image('BMP')}"

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context="describe",
            thread_id="t1",
            image_attachments=[{
                "file_type": "image",
                "data_url": data_url,
                "mime_type": "image/png",
                "file_name": "scan.png",
            }],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=False,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    [block] = _image_blocks(msg)
    assert block["image_url"]["url"] != data_url
    fmt = _decoded_format(block["image_url"]["url"])
    assert fmt in ("PNG", "JPEG")  # whichever the shared fit helper picks, it is native
    delivered_mime = f"image/{fmt.lower()}"
    assert block["image_url"]["url"].startswith(f"data:{delivered_mime};base64,")
    assert msg.additional_kwargs["attachments"][0]["mime_type"] == delivered_mime
    [clause] = _clauses(msg)
    assert "'scan.png' converted" in clause and delivered_mime in clause
    assert _relabel_warnings(caplog) == []


def test_uppercase_base64_header_is_still_sniffed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A ``;BASE64`` header must not walk past the sniff.

    Data-URL parameter names are case-insensitive, so a producer spelling it
    in caps would otherwise get the pre-fix behavior back in full.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    payload = _b64_image("GIF")

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context="look",
            thread_id="t1",
            image_attachments=[{
                "file_type": "image",
                "data_url": f"data:image/png;BASE64,{payload}",
                "mime_type": "image/png",
                "file_name": "shouty.png",
            }],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=False,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    [block] = _image_blocks(msg)
    # Relabeled, and the header's own spelling of the parameter survives.
    assert block["image_url"]["url"] == f"data:image/gif;BASE64,{payload}"
    assert msg.additional_kwargs["attachments"][0]["mime_type"] == "image/gif"
    assert len(_relabel_warnings(caplog)) == 1


def test_only_the_lying_attachment_in_a_batch_is_corrected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """One email carries several images; each is judged on its own bytes.

    The honest one comes FIRST deliberately: a correction loop that returns
    (rather than continues) on its first pass-through would leave the liar
    downstream of it untouched, which is the poisoned-signature case.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    honest_url = f"data:image/png;base64,{_b64_image('PNG')}"
    gif_payload = _b64_image("GIF")

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context="new email",
            thread_id="t1",
            image_attachments=[
                {
                    "file_type": "image",
                    "data_url": honest_url,
                    "mime_type": "image/png",
                    "file_name": "photo.png",
                },
                {
                    "file_type": "image",
                    "data_url": f"data:image/png;base64,{gif_payload}",
                    "mime_type": "image/png",
                    "file_name": "signature.png",
                },
            ],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=True,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    honest_block, lying_block = _image_blocks(msg)
    assert honest_block["image_url"]["url"] == honest_url
    assert lying_block["image_url"]["url"] == f"data:image/gif;base64,{gif_payload}"

    metadata = msg.additional_kwargs["attachments"]
    assert [entry["mime_type"] for entry in metadata] == ["image/png", "image/gif"]

    warnings = _relabel_warnings(caplog)
    assert len(warnings) == 1
    assert "signature.png" in warnings[0].getMessage()


def test_correctly_declared_png_passes_through_byte_identical(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """An honest declaration is never rewritten and never warns."""
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    data_url = f"data:image/png;base64,{_b64_image('PNG')}"
    image_att = {
        "file_type": "image",
        "data_url": data_url,
        "mime_type": "image/png",
        "file_name": "snap.png",
    }

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
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
    [block] = _image_blocks(msg)
    assert block["image_url"]["url"] == data_url
    assert msg.additional_kwargs["attachments"][0]["mime_type"] == "image/png"
    assert _relabel_warnings(caplog) == []


def test_unidentifiable_bytes_are_dropped_with_a_clause(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Bytes nothing can decode never enter history; the turn still runs.

    Measured (backlog #181, probe P50): 77 bytes of PNG header plus garbage
    passed every byte and count cap, reached the provider, and wedged the
    thread permanently because history replays verbatim. Now the block and
    its metadata entry are dropped before the checkpoint, one clause names
    the file, and the text goes out. The filename deliberately CONTRADICTS
    the header: nothing here may trust a name anyone can choose.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    garbage = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00\x01not a png\xff\xfe" * 4).decode("ascii")
    data_url = f"data:image/png;base64,{garbage}"

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context="what is this",
            thread_id="t1",
            image_attachments=[{
                "file_type": "image",
                "data_url": data_url,
                "mime_type": "image/png",
                "file_name": "mystery.gif",
                "workspace_path": "/ws/images/mystery-abcd1234.png",
            }],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=False,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert _image_blocks(msg) == []
    assert msg.content[0] == {"type": "text", "text": "what is this"}
    [clause] = _clauses(msg)
    assert clause.startswith("[Attached image 'mystery.gif' not shown:")
    assert "saved at /ws/images/mystery-abcd1234.png" in clause
    assert msg.additional_kwargs["attachments"] == []
    [warning] = _relabel_warnings(caplog)
    assert "dropped inbound image 'mystery.gif'" in warning.getMessage()


@pytest.mark.parametrize(
    "payload, why",
    [
        ("", "zero bytes, the empty data URL"),
        ("QQ", "shorter than one base64 quantum"),
        ("QUJD", "three bytes of plain text"),
        ("!!!!not base64 at all!!!!", "not decodable as base64"),
    ],
)
def test_undecodable_payloads_are_dropped_never_faulting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    payload: str,
    why: str,
):
    """Payload shapes nothing can read are dropped, and never fault the turn.

    The empty data URL is the cheapest thread brick measured (backlog #181,
    probe P50: two messages, one empty file). Each shape is pinned as
    dropped AND as non-raising: the gate runs on every image turn, and a
    decode error here would fail the whole turn over a payload.
    """
    _patch_image_compatibility(monkeypatch)
    agent = _fake_agent()
    data_url = f"data:image/png;base64,{payload}"

    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        state, _summary, error = prepare_astream_input(
            cast(Any, agent),
            message_with_context=f"payload is {why}",
            thread_id="t1",
            image_attachments=[{
                "file_type": "image",
                "data_url": data_url,
                "mime_type": "image/png",
                "file_name": "blob.png",
            }],
            sandbox_records=None,
            force_unsupported_attachments=False,
            is_self_invoke=False,
        )

    assert error is None
    assert state is not None
    [msg] = state["messages"]
    assert _image_blocks(msg) == []
    [clause] = _clauses(msg)
    assert "'blob.png' not shown" in clause and "Nothing was saved" in clause
    assert msg.additional_kwargs["attachments"] == []
    assert len(_relabel_warnings(caplog)) == 1


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


# --------------------------------------------------------------------------- #
# Ingress gate: fit or drop what the provider would certainly reject (#181)
# --------------------------------------------------------------------------- #

def _run_images(agent: Any, atts: list[dict[str, Any]], text: str = "look") -> HumanMessage:
    state, _summary, error = prepare_astream_input(
        cast(Any, agent),
        message_with_context=text,
        thread_id="t1",
        image_attachments=atts,
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert error is None and state is not None
    [msg] = state["messages"]
    return msg


def test_oversized_upload_is_downscaled_before_checkpoint(monkeypatch: pytest.MonkeyPatch):
    """The 9000x40 class: tiny in bytes, over the ceiling, permanent 400.

    Fitted under the ceiling BEFORE it becomes history, disclosed on the
    message with the sizes and the full-size path, and the metadata carries
    the DELIVERED dimensions (what token accounting sizes) while
    ``workspace_path`` still names the untouched original on disk.
    """
    _patch_image_compatibility(monkeypatch)
    upload = f"data:image/png;base64,{_b64_image('PNG', (3000, 40))}"
    msg = _run_images(_fake_agent(), [{
        "file_type": "image",
        "data_url": upload,
        "mime_type": "image/png",
        "file_name": "banner.png",
        "workspace_path": "/ws/images/banner-1234abcd.png",
        "width": 3000,
        "height": 40,
    }])

    [block] = _image_blocks(msg)
    with _decoded(block["image_url"]["url"]) as fitted:
        assert max(fitted.size) <= 2000
        delivered = fitted.size
    [clause] = _clauses(msg)
    assert clause.startswith(
        f"[Attached image 'banner.png' downscaled from 3000x40 to {delivered[0]}x{delivered[1]} "
        "to fit this model's 2000px image limit."
    )
    assert "saved at /ws/images/banner-1234abcd.png" in clause
    [meta] = msg.additional_kwargs["attachments"]
    assert (meta["width"], meta["height"]) == delivered
    assert meta["workspace_path"] == "/ws/images/banner-1234abcd.png"
    assert meta["data_url"] == block["image_url"]["url"]


def test_truncated_pixel_data_behind_a_valid_header_is_dropped(monkeypatch: pytest.MonkeyPatch):
    """A header the byte/count caps and a header probe would all pass.

    Only realizing the pixels finds the truncation, and that is what the
    gate pays for, once, because this is the one call that persists the bytes.
    """
    _patch_image_compatibility(monkeypatch)
    whole = base64.b64decode(_b64_image("PNG", (64, 64)))
    truncated = base64.b64encode(whole[: len(whole) // 2]).decode("ascii")
    msg = _run_images(_fake_agent(), [{
        "file_type": "image",
        "data_url": f"data:image/png;base64,{truncated}",
        "mime_type": "image/png",
        "file_name": "cut.png",
    }])

    assert _image_blocks(msg) == []
    [clause] = _clauses(msg)
    assert "'cut.png' not shown: image is corrupt or unreadable" in clause


def test_in_budget_upload_is_persisted_byte_identical_and_silently(monkeypatch: pytest.MonkeyPatch):
    _patch_image_compatibility(monkeypatch)
    upload = f"data:image/jpeg;base64,{_b64_image('JPEG', (640, 480))}"
    msg = _run_images(_fake_agent(), [{
        "file_type": "image",
        "data_url": upload,
        "mime_type": "image/jpeg",
        "file_name": "photo.jpg",
    }])

    [block] = _image_blocks(msg)
    assert block["image_url"]["url"] == upload
    assert _clauses(msg) == []


def test_a_dropped_image_keeps_block_and_metadata_pairing_exact(monkeypatch: pytest.MonkeyPatch):
    """[good, bad, good]: the k-th block must still pair with the k-th entry.

    Both the image window's eviction placeholder and history reload pair
    positionally, so a dropped image must leave BOTH lists, not just one.
    """
    _patch_image_compatibility(monkeypatch)
    first = f"data:image/png;base64,{_b64_image('PNG', (8, 8))}"
    third = f"data:image/gif;base64,{_b64_image('GIF', (6, 6))}"
    msg = _run_images(_fake_agent(), [
        {"file_type": "image", "data_url": first, "mime_type": "image/png", "file_name": "a.png"},
        {"file_type": "image", "data_url": "data:image/png;base64,", "mime_type": "image/png", "file_name": "empty.png"},
        {"file_type": "image", "data_url": third, "mime_type": "image/gif", "file_name": "c.gif"},
    ])

    blocks = _image_blocks(msg)
    assert [b["image_url"]["url"] for b in blocks] == [first, third]
    assert [m["name"] for m in msg.additional_kwargs["attachments"]] == ["a.png", "c.gif"]
    [clause] = _clauses(msg)
    assert "'empty.png' not shown" in clause
    # Clauses come AFTER every image block, so the model reads them beside the
    # images and clients render them at the foot of the user's text.
    kinds = [p["type"] for p in msg.content if isinstance(p, dict)]
    assert kinds == ["text", "image_url", "image_url", "text"]


def test_upload_over_the_byte_cap_is_reencoded_under_it(monkeypatch: pytest.MonkeyPatch):
    """The per-image byte cap is a real limit on some models; fit it, say why."""
    import nymeria.config.model_capabilities as mc

    _patch_image_compatibility(monkeypatch)
    real_limits = mc.get_attachment_limits
    monkeypatch.setattr(
        mc, "get_attachment_limits",
        lambda model: {**real_limits(model), "max_image_bytes": 40_000},
    )
    from PIL import Image
    import os

    noisy = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    raw = buf.getvalue()
    assert len(raw) > 40_000
    upload = f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"

    msg = _run_images(_fake_agent(), [{
        "file_type": "image",
        "data_url": upload,
        "mime_type": "image/png",
        "file_name": "noise.png",
    }])

    [block] = _image_blocks(msg)
    fitted = base64.b64decode(block["image_url"]["url"].partition(",")[2])
    assert len(fitted) <= 40_000
    [clause] = _clauses(msg)
    assert "'noise.png' re-encoded to fit this model's" in clause
    assert "image budget" in clause


def test_a_gate_fault_drops_the_image_rather_than_persisting_it_unchecked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Fail CLOSED: an image the gate could not check is exactly what it exists to refuse."""
    import nymeria.tools.image_read as image_read

    _patch_image_compatibility(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("pillow exploded")

    monkeypatch.setattr(image_read, "prepare_image_for_native_context", boom)
    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_streaming_input"):
        msg = _run_images(_fake_agent(), [{
            "file_type": "image",
            "data_url": _PNG_URL,
            "mime_type": "image/png",
            "file_name": "fine.png",
            "workspace_path": "/ws/images/fine-1234abcd.png",
        }])

    assert _image_blocks(msg) == []
    [clause] = _clauses(msg)
    assert "'fine.png' not shown: it could not be checked before sending" in clause
    assert "saved at /ws/images/fine-1234abcd.png" in clause
    assert any("gate faulted" in w.getMessage() for w in _relabel_warnings(caplog))


def test_a_remote_image_url_is_not_the_gates_to_judge(monkeypatch: pytest.MonkeyPatch):
    _patch_image_compatibility(monkeypatch)
    url = "https://example.com/photo.jpg"
    msg = _run_images(_fake_agent(), [{
        "file_type": "image", "data_url": url, "mime_type": "image/jpeg", "file_name": "photo.jpg",
    }])

    [block] = _image_blocks(msg)
    assert block["image_url"]["url"] == url
    assert _clauses(msg) == []


def test_two_oversized_uploads_are_both_fitted_with_clauses_in_order(monkeypatch: pytest.MonkeyPatch):
    _patch_image_compatibility(monkeypatch)
    msg = _run_images(_fake_agent(), [
        {"file_type": "image", "data_url": f"data:image/png;base64,{_b64_image('PNG', (2500, 10))}",
         "mime_type": "image/png", "file_name": "one.png"},
        {"file_type": "image", "data_url": f"data:image/png;base64,{_b64_image('PNG', (10, 2500))}",
         "mime_type": "image/png", "file_name": "two.png"},
    ])

    blocks = _image_blocks(msg)
    assert len(blocks) == 2
    for block in blocks:
        with _decoded(block["image_url"]["url"]) as fitted:
            assert max(fitted.size) <= 2000
    clauses = _clauses(msg)
    assert clauses[0].startswith("[Attached image 'one.png' downscaled from 2500x10")
    assert clauses[1].startswith("[Attached image 'two.png' downscaled from 10x2500")
    assert [m["name"] for m in msg.additional_kwargs["attachments"]] == ["one.png", "two.png"]


def test_a_dropped_image_with_no_text_leaves_no_empty_text_block(monkeypatch: pytest.MonkeyPatch):
    """An empty text block is itself a provider 400; the clause carries the turn."""
    _patch_image_compatibility(monkeypatch)
    msg = _run_images(_fake_agent(), [{
        "file_type": "image", "data_url": "data:image/png;base64,",
        "mime_type": "image/png", "file_name": "empty.png",
    }], text="")

    assert [p["type"] for p in msg.content if isinstance(p, dict)] == ["text"]
    assert msg.content[0]["text"].startswith("[Attached image 'empty.png' not shown")


def test_the_label_correction_never_trusts_the_filename():
    """``sniff_image_mime`` falls back to the extension when the magic bytes
    match nothing; the correction must never hand it a filename, or a payload
    anyone can name ``.gif`` would be relabeled off that name. (Formerly
    pinned through the whole input builder; the gate now drops such bytes,
    so the guard is pinned here, on the correction itself.)"""
    from nymeria.core.agent_streaming_input import _correct_declared_image_mimes

    garbage = base64.b64encode(b"\x00\x01not an image at all\xff\xfe" * 4).decode("ascii")
    att = {
        "file_type": "image",
        "data_url": f"data:image/png;base64,{garbage}",
        "mime_type": "image/png",
        "file_name": "mystery.gif",
    }
    [out] = _correct_declared_image_mimes([att], "t1")
    assert out["mime_type"] == "image/png"
    assert out["data_url"] == att["data_url"]
