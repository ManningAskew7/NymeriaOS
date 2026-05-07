from __future__ import annotations

from langchain_core.messages import ToolMessage

from nymeria.core import generated_image_context
from nymeria.core.generated_image_context import (
    NATIVE_IMAGE_ARTIFACT_KEY,
    hydrate_generated_images_for_llm,
)
from nymeria.vendor.react_agent.config import LLMConfig


def _tool_message(path: str, *, enabled: bool = True) -> ToolMessage:
    return ToolMessage(
        content=f"Generated image\n[attach:{path}]",
        tool_call_id="call-1",
        artifact={
            NATIVE_IMAGE_ARTIFACT_KEY: {
                "path": path,
                "mime_type": "image/png",
                "native_context_enabled": enabled,
            }
        },
    )


def test_hydrates_generated_image_tool_message_for_vision_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"png-bytes")

    original = _tool_message(str(image_path))
    messages = [original]
    hydrated = hydrate_generated_images_for_llm(
        messages,
        LLMConfig(provider="anthropic", model="claude-sonnet-4"),
    )

    assert hydrated is not messages
    assert hydrated[0] is not original
    assert isinstance(hydrated[0].content, list)
    assert hydrated[0].content[0]["type"] == "text"
    assert hydrated[0].content[1]["type"] == "image_url"
    assert hydrated[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert original.content == f"Generated image\n[attach:{image_path}]"


def test_hydration_skips_chat_completions_and_disabled_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"png-bytes")

    messages = [_tool_message(str(image_path))]
    skipped_for_mode = hydrate_generated_images_for_llm(
        messages,
        LLMConfig(provider="openai", model="gpt-5.5", openai_api_mode="chat_completions"),
    )
    skipped_for_disabled = hydrate_generated_images_for_llm(
        [_tool_message(str(image_path), enabled=False)],
        LLMConfig(provider="openai", model="gpt-5.5", openai_api_mode="responses"),
    )

    assert skipped_for_mode is messages
    assert isinstance(skipped_for_disabled[0].content, str)


def test_hydration_rejects_paths_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png-bytes")

    messages = [_tool_message(str(outside))]
    hydrated = hydrate_generated_images_for_llm(
        messages,
        LLMConfig(provider="anthropic", model="claude-sonnet-4"),
    )

    assert hydrated is messages
