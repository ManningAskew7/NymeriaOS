"""Tests for the per-provider image-token estimator and dimension helper."""

from __future__ import annotations

import io

import pytest

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


@pytest.mark.parametrize(
    "model_id,expected_hires",
    [
        # The 4.7 boundary, pinned on both sides.
        ("claude-opus-4-6", False),
        ("claude-sonnet-4-6", False),
        ("claude-opus-4-7", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4.8", True),
        # The generations a hardcoded name list missed. claude-opus-5 is a
        # shipped default model, and it was being estimated at 1568 instead of
        # 4784, a 3x under-count feeding image-aware compaction sizing.
        ("claude-opus-5", True),
        ("claude-sonnet-5", True),
        ("claude-fable-5", True),
        ("claude-mythos-5", True),
        # Below the boundary.
        ("claude-haiku-4-5-20251001", False),
        ("claude-opus-4-20250514", False),
        # Patch geometry belongs to the model, so a gateway-hosted copy keeps
        # it. This is the one axis where the provider prefix must NOT gate.
        ("bedrock/anthropic.claude-opus-4-8", True),
        ("openrouter/anthropic/claude-opus-4.7", True),
    ],
)
def test_anthropic_hires_boundary_is_ordinal_not_a_name_list(model_id, expected_hires):
    tokens = estimate_image_tokens(model_id, 8000, 8000)
    assert tokens == (4784 if expected_hires else 1568), model_id


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
