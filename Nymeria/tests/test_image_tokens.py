"""Tests for the per-provider image-token estimator and dimension helper."""

from __future__ import annotations

import io

from nymeria.config.model_capabilities import estimate_image_tokens
from nymeria.tools.image_read import read_image_dimensions


def _png_bytes(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_anthropic_patch_formula_small_image():
    # 280x280 -> ceil(280/28) * ceil(280/28) = 10 * 10 = 100 patches.
    assert estimate_image_tokens("claude-sonnet-4-6", 280, 280) == 100


def test_anthropic_caps_standard_model_at_1568():
    # A huge image is clamped to the 1568-token / 1568px ceiling on non-hires.
    assert estimate_image_tokens("claude-sonnet-4-6", 8000, 8000) == 1568


def test_anthropic_hires_model_caps_higher():
    # Opus 4.8 gets the 4784-token / 2576px ceiling.
    std = estimate_image_tokens("claude-sonnet-4-6", 8000, 8000)
    hires = estimate_image_tokens("claude-opus-4-8", 8000, 8000)
    assert hires == 4784
    assert hires > std


def test_openai_tile_formula_base_plus_tiles():
    # A small image is one tile: 85 + 170 = 255.
    assert estimate_image_tokens("gpt-5.5", 400, 400) == 255
    # A larger image costs more tiles.
    assert estimate_image_tokens("gpt-4o", 1600, 1600) > 255


def test_gemini_small_image_flat_258():
    assert estimate_image_tokens("gemini-2.5-pro", 300, 300) == 258
    # Larger image tiles into 768px tiles at 258 each.
    assert estimate_image_tokens("gemini-2.5-pro", 1000, 1000) == 258 * 4


def test_unknown_model_or_missing_dims_falls_back():
    # Unknown family with dims -> flat default.
    assert estimate_image_tokens("some-local-model", 1000, 1000) == 1300
    # Known family but no dims -> flat default (cannot compute patches/tiles).
    assert estimate_image_tokens("claude-opus-4-8", None, None) == 1300
    assert estimate_image_tokens("claude-opus-4-8", 0, 0) == 1300


def test_read_image_dimensions_from_bytes_and_path(tmp_path):
    data = _png_bytes(321, 123)
    assert read_image_dimensions(data) == (321, 123)

    path = tmp_path / "img.png"
    path.write_bytes(data)
    assert read_image_dimensions(path) == (321, 123)


def test_read_image_dimensions_non_image_returns_none(tmp_path):
    path = tmp_path / "notimage.txt"
    path.write_text("hello", encoding="utf-8")
    assert read_image_dimensions(path) is None
    assert read_image_dimensions(b"not an image") is None
