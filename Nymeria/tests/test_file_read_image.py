"""Integration tests for file_read's image-viewing path."""

from __future__ import annotations

import os

import pytest
from PIL import Image

from nymeria.core import generated_image_context
from nymeria.core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY
from nymeria.tools import filesystem
from nymeria.tools.filesystem import file_read
from nymeria.vendor.react_agent.config import LLMConfig

_VISION_CFG = LLMConfig(provider="anthropic", model="claude-sonnet-4")
_CHAT_COMPLETIONS_CFG = LLMConfig(
    provider="openai", model="gpt-5.5", openai_api_mode="chat_completions"
)


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: True)
    yield


def _supported(monkeypatch, cfg=_VISION_CFG):
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _config: cfg)


def _png(path, size=(32, 32), color="red"):
    Image.new("RGB", size, color).save(path, format="PNG")


_CFG = {"configurable": {"thread_id": "t1", "user_id": "u1"}}


def test_text_read_returns_tuple(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("alpha beta\n", encoding="utf-8")

    content, artifact = file_read.func(str(path))
    assert content.strip() == "alpha beta"
    assert artifact == {}


def test_image_fast_path(tmp_path, monkeypatch):
    _supported(monkeypatch)
    path = tmp_path / "pic.png"
    _png(path)

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.startswith("Loaded image 'pic.png'")
    meta = artifact[NATIVE_IMAGE_ARTIFACT_KEY]
    assert meta["source"] == "file_read"
    assert meta["native_context_enabled"] is True
    assert meta["path"] == str(path)  # fast path points at the original


def test_oversized_image_written_to_sandbox(tmp_path, monkeypatch):
    _supported(monkeypatch)
    monkeypatch.setattr(
        "nymeria.config.model_capabilities.get_attachment_limits",
        lambda _model: {"max_image_bytes": 40_000},
    )
    path = tmp_path / "big.png"
    Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3)).save(path, "PNG")

    content, artifact = file_read.func(str(path), config=_CFG)
    meta = artifact[NATIVE_IMAGE_ARTIFACT_KEY]
    assert meta["source"] == "file_read"
    assert "fetched" in meta["path"]  # persisted copy in the per-thread sandbox
    assert meta["path"] != str(path)
    assert meta["original_path"] == str(path)
    assert os.path.getsize(meta["path"]) <= 40_000
    assert "Downscaled" in content


def test_tall_image_is_downscaled_and_the_note_names_both_sizes(tmp_path, monkeypatch):
    # file_read had the same hole as the replay path: a tall image is small in
    # bytes, so only the pixel ceiling catches it.
    _supported(monkeypatch)
    path = tmp_path / "tall.png"
    _png(path, size=(1368, 2088), color="white")

    content, artifact = file_read.func(str(path), config=_CFG)

    delivered = Image.open(artifact[NATIVE_IMAGE_ARTIFACT_KEY]["path"])
    assert max(delivered.size) == 2000
    assert f"Downscaled from 1368x2088 to {delivered.width}x{delivered.height}" in content


def test_file_read_ceiling_comes_from_the_model_not_the_module_default(tmp_path, monkeypatch):
    # The module fallback and the table's answer happen to coincide today, so
    # only a non-default model limit proves file_read actually resolves one.
    _supported(monkeypatch)
    monkeypatch.setattr(
        "nymeria.core.image_limits.get_model_max_image_dimension", lambda _model: 512
    )
    path = tmp_path / "tall.png"
    _png(path, size=(1368, 2088), color="white")

    content, artifact = file_read.func(str(path), config=_CFG)

    assert max(Image.open(artifact[NATIVE_IMAGE_ARTIFACT_KEY]["path"]).size) == 512
    assert "to 335x512" in content


def test_conversion_only_does_not_claim_a_downscale(tmp_path, monkeypatch):
    # A bmp is converted, not resized. Naming sizes turned the old vague copy
    # into a self-contradiction ("Downscaled from 64x64 to 64x64").
    _supported(monkeypatch)
    path = tmp_path / "tiny.bmp"
    Image.new("RGB", (64, 64), "green").save(path, format="BMP")

    content, _artifact = file_read.func(str(path), config=_CFG)

    assert "Downscaled" not in content
    assert "Converted to image/" in content


def test_non_vision_model_warns(tmp_path, monkeypatch):
    _supported(monkeypatch)
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _m: False)
    path = tmp_path / "pic.png"
    _png(path)

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.startswith("[Note]")
    assert "does not support image input" in content
    assert artifact == {}


def test_chat_completions_route_warns(tmp_path, monkeypatch):
    _supported(monkeypatch, cfg=_CHAT_COMPLETIONS_CFG)
    path = tmp_path / "pic.png"
    _png(path)

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.startswith("[Note]")
    assert "chat/completions" in content
    assert artifact == {}


def test_no_active_agent_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _config: None)
    path = tmp_path / "pic.png"
    _png(path)

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.startswith("[Note]")
    assert artifact == {}


def test_png_extension_on_text_file_falls_back_to_text(tmp_path, monkeypatch):
    _supported(monkeypatch)
    # Image extension but plain-text content -> sniff false-positive, Pillow
    # fails to open, file_read reads it as text.
    path = tmp_path / "actually_text.png"
    path.write_text("just some text\n", encoding="utf-8")

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.strip() == "just some text"
    assert artifact == {}


def test_corrupt_image_errors(tmp_path, monkeypatch):
    _supported(monkeypatch)
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00not-a-real-png")

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.startswith("[Error]")
    assert artifact == {}


def test_non_image_binary_uses_text_path(tmp_path, monkeypatch):
    _supported(monkeypatch)
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4\n\x00\x01\x02\x80\x81binary")

    content, artifact = file_read.func(str(path), config=_CFG)
    assert content.startswith("[Error]")  # not decodable as text, never an image
    assert artifact == {}
    # The error names the real fix (attach=True) instead of only blaming the
    # encoding; test_file_read_attach.py covers attach=True actually working.
    assert "attach=True" in content
