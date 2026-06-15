"""Unit tests for nymeria.tools.image_read (detection + Pillow preparation)."""

from __future__ import annotations

import io

from PIL import Image

from nymeria.tools.image_read import (
    _NATIVE_SUPPORTED_MIME,
    prepare_image_for_native_context,
    sniff_image_mime,
)


def _save(img: Image.Image, path, fmt: str) -> None:
    img.save(path, format=fmt)


def _png_signature() -> bytes:
    return b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# sniff_image_mime
# --------------------------------------------------------------------------- #

def test_sniff_recognizes_magic_bytes():
    assert sniff_image_mime(_png_signature(), "x.png") == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0", "x") == "image/jpeg"
    assert sniff_image_mime(b"GIF89a....", "x") == "image/gif"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "x") == "image/webp"
    assert sniff_image_mime(b"BM\x00\x00", "x") == "image/bmp"
    assert sniff_image_mime(b"II*\x00", "x") == "image/tiff"


def test_sniff_extension_fallback_and_none():
    # No magic match, but an image extension -> fallback.
    assert sniff_image_mime(b"not an image", "photo.jpeg") == "image/jpeg"
    # No magic, non-image extension -> None (read as text).
    assert sniff_image_mime(b"hello world", "notes.txt") is None
    assert sniff_image_mime(b"", "") is None


# --------------------------------------------------------------------------- #
# prepare_image_for_native_context
# --------------------------------------------------------------------------- #

def test_fast_path_returns_original(tmp_path):
    path = tmp_path / "small.png"
    _save(Image.new("RGB", (32, 32), "red"), path, "PNG")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024
    )
    assert error is None
    assert mime == "image/png"
    assert out_bytes is None  # fast path: caller uses the original file


def test_resize_when_over_byte_cap(tmp_path):
    # Random noise compresses poorly, so this PNG is large.
    import os

    noise = Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3))
    path = tmp_path / "noise.png"
    _save(noise, path, "PNG")
    assert path.stat().st_size > 50_000

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=50_000
    )
    assert error is None
    assert out_bytes is not None
    assert mime in _NATIVE_SUPPORTED_MIME
    assert len(out_bytes) <= 50_000


def test_resize_when_over_dimension_ceiling(tmp_path):
    path = tmp_path / "wide.png"
    _save(Image.new("RGB", (200, 200), "blue"), path, "PNG")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024, long_edge_ceiling=64
    )
    assert error is None
    assert out_bytes is not None
    # Re-open the resized output and confirm it was downscaled.
    reopened = Image.open(io.BytesIO(out_bytes))
    assert max(reopened.size) <= 64


def test_convertible_format_becomes_native(tmp_path):
    path = tmp_path / "pic.bmp"
    _save(Image.new("RGB", (64, 64), "green"), path, "BMP")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024
    )
    assert error is None
    assert out_bytes is not None  # BMP is converted, never fast-pathed
    assert mime in _NATIVE_SUPPORTED_MIME


def test_alpha_preserved_on_resize(tmp_path):
    img = Image.new("RGBA", (200, 200), (255, 0, 0, 128))
    path = tmp_path / "alpha.png"
    _save(img, path, "PNG")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024, long_edge_ceiling=64
    )
    assert error is None
    assert mime == "image/png"  # not flattened to JPEG
    reopened = Image.open(io.BytesIO(out_bytes))
    assert reopened.mode in ("RGBA", "LA", "P")


def test_static_gif_fast_path(tmp_path):
    path = tmp_path / "still.gif"
    _save(Image.new("P", (32, 32)), path, "GIF")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024
    )
    assert error is None
    assert mime == "image/gif"
    assert out_bytes is None  # untouched


def test_decompression_bomb_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    path = tmp_path / "bomb.png"
    _save(Image.new("RGB", (200, 200), "white"), path, "PNG")

    # long_edge_ceiling forces the resize/load path where the bomb guard fires.
    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024, long_edge_ceiling=64
    )
    assert out_bytes is None
    assert error is not None


def test_corrupt_image_returns_no_bytes(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(_png_signature() + b"\x00garbage-not-a-real-png")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024
    )
    assert out_bytes is None
    assert mime is None  # caller will fall back to a text read


def test_missing_file_returns_error(tmp_path):
    out_bytes, mime, error = prepare_image_for_native_context(
        tmp_path / "nope.png", max_image_bytes=5 * 1024 * 1024
    )
    assert out_bytes is None
    assert error is not None
