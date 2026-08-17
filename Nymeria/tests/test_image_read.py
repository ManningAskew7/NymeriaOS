"""Unit tests for nymeria.tools.image_read (detection + Pillow preparation)."""

from __future__ import annotations

import io
import os

from PIL import Image

from nymeria.tools.image_read import (
    _NATIVE_SUPPORTED_MIME,
    prepare_image_for_native_context,
    probe_image,
    read_image_dimensions,
    sniff_image_mime,
)


def _save(img: Image.Image, path, fmt: str) -> None:
    img.save(path, format=fmt)


def _save_bytes(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


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
    noise = Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3))
    path = tmp_path / "noise.png"
    _save(noise, path, "PNG")
    assert path.stat().st_size > 50_000

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=50_000
    )
    assert error is None
    assert out_bytes is not None
    # Bytes are the binding constraint here, so the lossless source format is
    # NOT kept: JPEG is the only way under the cap.
    assert mime == "image/jpeg"
    assert len(out_bytes) <= 50_000


def test_lossless_source_is_kept_when_only_the_pixel_ceiling_binds(tmp_path):
    # The same function, the other constraint: a screenshot trimmed for pixels
    # while sitting well inside the byte cap keeps its lossless encoding, because
    # small text is exactly what JPEG artifacts destroy.
    path = tmp_path / "shot.png"
    _save(Image.new("RGB", (1368, 2088), "white"), path, "PNG")

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024, long_edge_ceiling=2000
    )
    assert error is None
    assert mime == "image/png"
    assert max(Image.open(io.BytesIO(out_bytes)).size) == 2000


def test_an_over_budget_lossless_encode_is_dropped_only_when_lossy_is_smaller(tmp_path):
    # Resizing sharp content antialiases it, which is what PNG cannot compress:
    # review measured 323 KB -> 1.13 MB for a text screenshot, so the growth
    # budget exists. But a blind switch to JPEG can produce MORE bytes, so the
    # budget triggers a comparison, not a surrender. Both directions, budget
    # forced to trip.
    from nymeria.tools.image_read import _encode_to_fit

    sparse = Image.new("RGB", (2000, 1310), "white")
    for y in range(0, 1310, 40):
        for x in range(0, 2000, 7):
            sparse.putpixel((x, y), (0, 0, 0))
    noisy = Image.frombytes("RGB", (2000, 1310), os.urandom(2000 * 1310 * 3))

    kept, kept_mime = _encode_to_fit(
        sparse, False, 20 * 1024 * 1024, keep_format="PNG", keep_format_max_bytes=1
    )
    dropped, dropped_mime = _encode_to_fit(
        noisy, False, 20 * 1024 * 1024, keep_format="PNG", keep_format_max_bytes=1
    )

    assert kept_mime == "image/png"  # lossless was the SMALLER encode here
    assert dropped_mime == "image/jpeg"  # and the larger one there
    assert len(dropped) < len(_save_bytes(noisy, "PNG"))


def test_the_orientation_probe_never_decodes_a_png(tmp_path, monkeypatch):
    # Pillow's PNG getexif() calls load(). This probe runs per image per LLM
    # call, so a decode here is the difference between a header read and 35 ms
    # of pixels; the format gate is what prevents it.
    from PIL import ImageFile

    decodes: list[int] = []
    real_load = ImageFile.ImageFile.load

    def spy(self, *args, **kwargs):
        decodes.append(1)
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)
    path = tmp_path / "shot.png"
    _save(Image.new("RGB", (1368, 2088), "white"), path, "PNG")

    assert read_image_dimensions(path, apply_exif=True) == (1368, 2088)
    assert decodes == []


def test_exif_rotation_can_be_reported_as_the_image_is_seen(tmp_path):
    # Orientation 6 is a quarter turn: stored 60x30, displayed 30x60 by anything
    # that honours EXIF (providers do, and so does the resize path), so a
    # before/after disclosure has to measure both sides the same way.
    path = tmp_path / "rotated.jpg"
    img = Image.new("RGB", (60, 30), "red")
    exif = img.getexif()
    exif[0x0112] = 6
    img.save(path, format="JPEG", exif=exif)

    assert read_image_dimensions(path) == (60, 30)
    assert read_image_dimensions(path, apply_exif=True) == (30, 60)


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


def test_default_ceiling_fits_the_provider_limit_not_a_generous_guess(tmp_path):
    # The shape that killed a turn: comfortably inside the byte cap, over the
    # 2000px many-image ceiling. The old 4096 default let it through untouched.
    path = tmp_path / "tall.png"
    _save(Image.new("RGB", (1368, 2088), "white"), path, "PNG")
    assert path.stat().st_size < 5 * 1024 * 1024

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=5 * 1024 * 1024
    )
    assert error is None
    assert out_bytes is not None
    assert mime in _NATIVE_SUPPORTED_MIME
    reopened = Image.open(io.BytesIO(out_bytes))
    assert max(reopened.size) == 2000
    assert abs(reopened.width / reopened.height - 1368 / 2088) < 0.01


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


def test_an_opaque_alpha_image_is_not_pinned_to_the_png_branch(tmp_path):
    # RGBA does not mean "uses transparency": Chrome's PNG captures are RGBA
    # with every pixel opaque. Treating those as alpha images kept them on the
    # PNG-only path, where neither the JPEG ladder nor the growth budget can
    # bound what goes on the wire.
    raw = bytearray(os.urandom(400 * 400 * 4))  # detail PNG cannot compress away
    raw[3::4] = b"\xff" * (400 * 400)  # ...and a fully opaque alpha band
    path = tmp_path / "opaque_rgba.png"
    _save(Image.frombytes("RGBA", (400, 400), bytes(raw)), path, "PNG")
    assert path.stat().st_size > 150_000

    out_bytes, mime, error = prepare_image_for_native_context(
        path, max_image_bytes=150_000, long_edge_ceiling=2000
    )

    assert error is None
    assert mime == "image/jpeg"  # the byte cap is reachable, which PNG-only was not
    assert len(out_bytes) <= 150_000


def test_transparency_detection_only_ever_narrows():
    # The narrowing must not become a flattening: one transparent pixel, or one
    # nearly-opaque one, keeps the image on the lossless path, because a lost
    # alpha channel is a visible and unrecoverable change while a missed
    # optimisation only costs bytes.
    from nymeria.tools.image_read import _carries_transparency

    holed = Image.new("RGBA", (8, 8), (1, 2, 3, 255))
    holed.putpixel((0, 0), (0, 0, 0, 0))
    faint = Image.new("RGBA", (8, 8), (1, 2, 3, 255))
    faint.putpixel((7, 7), (1, 2, 3, 254))

    assert _carries_transparency(Image.new("RGBA", (8, 8), (1, 2, 3, 255))) is False
    assert _carries_transparency(holed) is True
    assert _carries_transparency(faint) is True
    # Not alpha-capable at all, and not this function's call site: answer the
    # safe way rather than claim an image has no transparency to lose.
    assert _carries_transparency(Image.new("RGB", (8, 8), "white")) is True


def test_an_irreducible_image_names_its_cap_the_way_a_person_reads_it(tmp_path, monkeypatch):
    # This string reaches the model verbatim. Integer megabytes reported a
    # 3.5 MB cap as "3 MB" and a 30,000-byte one as "0 MB", which reads as a
    # broken limit rather than as a fact about the image.
    import nymeria.tools.image_read as image_read

    monkeypatch.setattr(image_read, "_encode_to_fit", lambda *_a, **_k: (None, None))
    path = tmp_path / "big.png"
    _save(Image.new("RGB", (3000, 3000), "white"), path, "PNG")

    _, _, fractional = prepare_image_for_native_context(
        path, max_image_bytes=int(3.5 * 1024 * 1024), long_edge_ceiling=2000
    )
    _, _, tiny = prepare_image_for_native_context(
        path, max_image_bytes=30_000, long_edge_ceiling=2000
    )

    assert "3.5 MB limit" in fractional
    assert "29 KB limit" in tiny


def test_the_probe_reports_the_format_it_decoded_not_the_one_named(tmp_path):
    # A caller reading an image back out of an agent-writable directory cannot
    # treat the extension as evidence: a payload whose declared media type does
    # not match its bytes is refused by the provider like an oversized one.
    liar = tmp_path / "actually_jpeg.png"
    Image.new("RGB", (120, 80), "white").save(liar, format="JPEG")
    unknown = tmp_path / "vector.bmp"
    Image.new("RGB", (10, 10), "white").save(unknown, format="BMP")

    assert probe_image(liar) == ((120, 80), "image/jpeg")
    assert probe_image(unknown) == ((10, 10), "image/bmp")
    assert probe_image(tmp_path / "missing.png") is None
    assert read_image_dimensions(liar) == (120, 80)  # the dimensions-only view


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
