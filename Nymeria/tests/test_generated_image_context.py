from __future__ import annotations

from langchain_core.messages import ToolMessage

from nymeria.core import generated_image_context
from nymeria.core.generated_image_context import (
    NATIVE_IMAGE_ARTIFACT_KEY,
    window_images_for_llm,
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
    hydrated = window_images_for_llm(
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
    skipped_for_mode = window_images_for_llm(
        messages,
        LLMConfig(provider="openai", model="gpt-5.5", openai_api_mode="chat_completions"),
    )
    skipped_for_disabled = window_images_for_llm(
        [_tool_message(str(image_path), enabled=False)],
        LLMConfig(provider="openai", model="gpt-5.5", openai_api_mode="responses"),
    )

    assert skipped_for_mode is messages
    assert isinstance(skipped_for_disabled[0].content, str)


def test_image_gate_resolves_effective_mode_from_provider_default(monkeypatch):
    # The gate must resolve a null openai_api_mode via the provider default, the
    # same way the LLM factories do. OpenRouter's default is chat_completions
    # (which cannot carry tool/history images), so a null mode reports
    # chat_completions_route; OpenAI's default is responses, so it stays
    # supported. Explicit modes win over the default on both.
    from nymeria.core.generated_image_context import explain_image_context_support

    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)

    def gate(provider, mode):
        return explain_image_context_support(
            LLMConfig(provider=provider, model="m", openai_api_mode=mode)
        )

    assert gate("openrouter", None) == (False, "chat_completions_route")
    assert gate("openrouter", "responses") == (True, "supported")
    assert gate("openai", None) == (True, "supported")
    assert gate("openai", "chat_completions") == (False, "chat_completions_route")


def test_hydration_rejects_paths_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png-bytes")

    messages = [_tool_message(str(outside))]
    hydrated = window_images_for_llm(
        messages,
        LLMConfig(provider="anthropic", model="claude-sonnet-4"),
    )

    assert hydrated is messages


def _file_read_tool_message(path: str) -> ToolMessage:
    return ToolMessage(
        content=f"Loaded image\n{path}",
        tool_call_id="call-2",
        artifact={
            NATIVE_IMAGE_ARTIFACT_KEY: {
                "path": path,
                "mime_type": "image/png",
                "native_context_enabled": True,
                "source": "file_read",
            }
        },
    )


def test_file_read_image_outside_workspace_is_hydrated(tmp_path, monkeypatch):
    # file_read reads arbitrary paths, so its images are exempt from workspace
    # confinement (unlike generated images).
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png-bytes")

    messages = [_file_read_tool_message(str(outside))]
    hydrated = window_images_for_llm(
        messages,
        LLMConfig(provider="anthropic", model="claude-sonnet-4"),
    )

    assert hydrated is not messages
    assert hydrated[0].content[1]["type"] == "image_url"


def test_hydration_respects_model_image_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    monkeypatch.setattr(
        generated_image_context,
        "get_attachment_limits",
        lambda _model: {"max_image_bytes": 4},
    )
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"too-many-bytes-for-the-cap")

    messages = [_tool_message(str(image_path))]
    hydrated = window_images_for_llm(
        messages,
        LLMConfig(provider="anthropic", model="claude-sonnet-4"),
    )

    assert hydrated is messages  # image exceeds the 4-byte model cap, dropped
