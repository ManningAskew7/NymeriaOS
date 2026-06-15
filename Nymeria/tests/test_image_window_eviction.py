"""Tests for the combined image sliding-window (generated + user images)."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, ToolMessage

from nymeria.core import generated_image_context
from nymeria.core.generated_image_context import (
    NATIVE_IMAGE_ARTIFACT_KEY,
    window_images_for_llm,
)
from nymeria.vendor.react_agent.config import LLMConfig

_DATA_URL = "data:image/png;base64,QUJD"


def _anthropic() -> LLMConfig:
    return LLMConfig(provider="anthropic", model="claude-sonnet-4-6")


def _generated_tool_message(path: str, call_id: str) -> ToolMessage:
    return ToolMessage(
        content=f"Generated image\n[attach:{path}]",
        tool_call_id=call_id,
        artifact={
            NATIVE_IMAGE_ARTIFACT_KEY: {
                "path": path,
                "mime_type": "image/png",
                "native_context_enabled": True,
            }
        },
    )


def _user_image_message(text: str, workspace_path: str) -> HumanMessage:
    msg = HumanMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": _DATA_URL}},
        ]
    )
    msg.additional_kwargs["attachments"] = [
        {
            "type": "image",
            "name": "shot.png",
            "data_url": _DATA_URL,
            "workspace_path": workspace_path,
        }
    ]
    return msg


def _force_window(monkeypatch, window: int) -> None:
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: True)
    # Byte cap for hydration (kept generous so test images are never dropped).
    monkeypatch.setattr(
        generated_image_context,
        "get_attachment_limits",
        lambda _m: {"max_images_per_request": window, "max_image_bytes": 10_000_000},
    )
    # The window count resolves through image_limits; patch it at the call site
    # (the resolver's own clamping is unit-tested in test_image_limits.py).
    monkeypatch.setattr(
        generated_image_context, "get_effective_image_window_size", lambda *a, **k: window
    )


def _is_image_block(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "image_url"


def test_window_keeps_newest_across_generated_and_user(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _force_window(monkeypatch, 2)
    g1 = tmp_path / "g1.png"
    g1.write_bytes(b"png-bytes-1")
    g2 = tmp_path / "g2.png"
    g2.write_bytes(b"png-bytes-2")

    u1 = _user_image_message("first upload", "/workspace/images/prompt-attached/u/old.png")
    tg1 = _generated_tool_message(str(g1), "call-g1")
    u2 = _user_image_message("second upload", "/workspace/images/prompt-attached/u/new.png")
    tg2 = _generated_tool_message(str(g2), "call-g2")
    messages = [u1, tg1, u2, tg2]

    out = window_images_for_llm(messages, _anthropic(), thread_id="t1")

    # Window=2 keeps the newest two slots (u2, tg2); evicts the oldest two (u1, tg1).
    # u1: image block replaced with a placeholder citing its disk path.
    assert not any(_is_image_block(b) for b in out[0].content)
    placeholder = next(b for b in out[0].content if b.get("type") == "text" and "evicted" in b["text"])
    assert "old.png" in placeholder["text"]
    # tg1 (evicted generated): not hydrated, original text result remains.
    assert isinstance(out[1].content, str)
    # u2 (kept user): image block still inline.
    assert any(_is_image_block(b) for b in out[2].content)
    # tg2 (kept generated): hydrated with a data URL.
    assert isinstance(out[3].content, list)
    assert any(_is_image_block(b) for b in out[3].content)


def test_window_large_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _force_window(monkeypatch, 100)
    g1 = tmp_path / "g1.png"
    g1.write_bytes(b"png-bytes-1")

    u1 = _user_image_message("upload", "/workspace/images/prompt-attached/u/a.png")
    tg1 = _generated_tool_message(str(g1), "call-g1")
    out = window_images_for_llm([u1, tg1], _anthropic(), thread_id="t1")

    assert any(_is_image_block(b) for b in out[0].content)  # user kept inline
    assert any(_is_image_block(b) for b in out[1].content)  # generated hydrated


def test_does_not_mutate_original_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _force_window(monkeypatch, 1)
    u1 = _user_image_message("oldest", "/workspace/images/prompt-attached/u/a.png")
    u2 = _user_image_message("newest", "/workspace/images/prompt-attached/u/b.png")
    messages = [u1, u2]

    out = window_images_for_llm(messages, _anthropic(), thread_id="t1")

    # Original u1 still carries its inline image block (outbound transform clones).
    assert any(_is_image_block(b) for b in u1.content)
    # Output evicts the oldest (u1) and keeps the newest (u2).
    assert not any(_is_image_block(b) for b in out[0].content)
    assert any(_is_image_block(b) for b in out[1].content)


def test_idempotent_on_original_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _force_window(monkeypatch, 1)
    u1 = _user_image_message("oldest", "/workspace/images/prompt-attached/u/a.png")
    u2 = _user_image_message("newest", "/workspace/images/prompt-attached/u/b.png")
    messages = [u1, u2]

    first = window_images_for_llm(messages, _anthropic(), thread_id="t1")
    second = window_images_for_llm(messages, _anthropic(), thread_id="t1")
    assert [m.content for m in first] == [m.content for m in second]


def test_per_thread_override_clamped_to_model_max(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    _force_window(monkeypatch, 100)  # model max 100
    # Per-thread override of 1 wins (below the model max). Patch the resolver at
    # the call site; the resolver itself is unit-tested in test_image_limits.py.
    monkeypatch.setattr(
        generated_image_context, "get_effective_image_window_size", lambda *a, **k: 1
    )
    u1 = _user_image_message("oldest", "/workspace/images/prompt-attached/u/a.png")
    u2 = _user_image_message("newest", "/workspace/images/prompt-attached/u/b.png")
    out = window_images_for_llm([u1, u2], _anthropic(), thread_id="t1")

    assert not any(_is_image_block(b) for b in out[0].content)  # evicted
    assert any(_is_image_block(b) for b in out[1].content)  # kept


def test_non_vision_route_strips_user_images(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    # Vision unsupported -> window 0, user images stripped to a placeholder.
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: False)
    u1 = _user_image_message("upload", "/workspace/images/prompt-attached/u/a.png")
    out = window_images_for_llm([u1], _anthropic(), thread_id="t1")

    assert not any(_is_image_block(b) for b in out[0].content)
    text = next(b["text"] for b in out[0].content if b.get("type") == "text" and "omitted" in b["text"])
    assert "cannot view images" in text


def test_no_images_returns_input_unchanged(monkeypatch):
    _force_window(monkeypatch, 2)
    messages = [HumanMessage(content="just text")]
    out = window_images_for_llm(messages, _anthropic(), thread_id="t1")
    assert out is messages
