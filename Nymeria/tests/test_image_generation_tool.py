from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from nymeria.core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY
from nymeria.tools import image_generation


def test_openai_adapter_decodes_b64_response(monkeypatch):
    captured = {}

    class FakeImages:
        def generate(self, **params):
            captured.update(params)
            encoded = base64.b64encode(b"openai-image").decode("ascii")
            return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

    class FakeOpenAI:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key
            self.images = FakeImages()

    import openai

    monkeypatch.setattr(image_generation, "_openai_key", lambda: "test-openai-key")
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

    raw, mime_type, model = image_generation._generate_openai(
        "draw a precise icon",
        {
            "openai_model": "gpt-image-2",
            "openai_output_format": "webp",
            "openai_size": "auto",
            "openai_quality": "high",
            "openai_moderation": "auto",
        },
    )

    assert raw == b"openai-image"
    assert mime_type == "image/webp"
    assert model == "gpt-image-2"
    assert captured["api_key"] == "test-openai-key"
    assert captured["prompt"] == "draw a precise icon"
    assert captured["response_format"] == "b64_json"


def test_openai_image_url_fetch_blocks_private_network(monkeypatch):
    import httpx

    def fail_request(*args, **kwargs):
        raise AssertionError("blocked image URL should not issue an HTTP request")

    monkeypatch.setattr(httpx.Client, "request", fail_request)

    with pytest.raises(RuntimeError, match="egress policy"):
        image_generation._read_openai_image_bytes(
            SimpleNamespace(url="http://127.0.0.1:8000/image.png")
        )


def test_gemini_adapter_reads_inline_image_data(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, **params):
            captured.update(params)
            return SimpleNamespace(
                parts=[
                    SimpleNamespace(text="done"),
                    SimpleNamespace(
                        inline_data=SimpleNamespace(
                            data=base64.b64encode(b"gemini-image").decode("ascii"),
                            mime_type="image/png",
                        )
                    ),
                ]
            )

    class FakeClient:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key
            self.models = FakeModels()

    from google import genai

    monkeypatch.setattr(image_generation, "_gemini_key", lambda: "test-gemini-key")
    monkeypatch.setattr(genai, "Client", FakeClient)

    raw, mime_type, model = image_generation._generate_gemini(
        "draw a banana",
        {
            "gemini_model": "gemini-3-pro-image-preview",
            "gemini_aspect_ratio": "16:9",
            "gemini_image_size": "2K",
        },
    )

    assert raw == b"gemini-image"
    assert mime_type == "image/png"
    assert model == "gemini-3-pro-image-preview"
    assert captured["api_key"] == "test-gemini-key"
    assert captured["model"] == "gemini-3-pro-image-preview"
    assert captured["contents"] == ["draw a banana"]


def test_finalize_image_writes_workspace_artifact_and_native_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))

    content, artifact = image_generation.finalize_image(
        raw=b"fake-image",
        mime_type="image/png",
        provider="openai",
        model="gpt-image-2",
        prompt="A clean product icon",
        output_name="product icon",
        config={"configurable": {"user_id": "owner@example.com"}},
    )

    metadata = artifact[NATIVE_IMAGE_ARTIFACT_KEY]
    assert "Generated image via openai (gpt-image-2)" in content
    assert "[attach:" in content
    assert metadata["native_context_enabled"] is True
    assert metadata["prompt"] == "A clean product icon"
    path = tmp_path / "image-generation" / "owner-example.com"
    files = list(path.glob("*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"fake-image"
    assert metadata["path"] == str(files[0])


def test_download_image_bytes_decodes_data_uri():
    import base64 as _b64

    encoded = _b64.b64encode(b"data-uri-image").decode("ascii")
    raw, mime_type = image_generation.download_image_bytes(f"data:image/webp;base64,{encoded}")

    assert raw == b"data-uri-image"
    assert mime_type == "image/webp"
