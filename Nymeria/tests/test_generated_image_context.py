from __future__ import annotations

import base64
import io
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image

from nymeria.core import generated_image_context
from nymeria.core.generated_image_context import (
    NATIVE_IMAGE_ARTIFACT_KEY,
    window_images_for_llm,
)
from nymeria.vendor.react_agent.config import LLMConfig

_ANTHROPIC = LLMConfig(provider="anthropic", model="claude-sonnet-4")


def _tool_message(path: str, *, enabled: bool = True, source: str | None = None) -> ToolMessage:
    metadata = {
        "path": path,
        "mime_type": "image/png",
        "native_context_enabled": enabled,
    }
    if source is not None:
        metadata["source"] = source
    return ToolMessage(
        content=f"Generated image\n[attach:{path}]",
        tool_call_id="call-1",
        artifact={NATIVE_IMAGE_ARTIFACT_KEY: metadata},
    )


def _png(path, size) -> None:
    Image.new("RGB", size, "white").save(path, format="PNG")


def _blocks(message) -> list[dict]:
    assert isinstance(message.content, list)
    return message.content


def _image_block(message) -> dict:
    return next(b for b in _blocks(message) if b["type"] == "image_url")


def _texts(message) -> list[str]:
    return [b["text"] for b in _blocks(message) if b["type"] == "text"]


def _delivered_size(message) -> tuple[int, int]:
    url = _image_block(message)["image_url"]["url"]
    raw = base64.b64decode(url.partition(",")[2])
    with Image.open(io.BytesIO(raw)) as img:
        return img.size


@pytest.fixture
def vision_workspace(tmp_path, monkeypatch):
    """Vision-capable model with ``tmp_path`` as the confined workspace."""
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    return tmp_path


def test_hydrates_generated_image_tool_message_for_vision_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(generated_image_context, "supports_vision", lambda _model: True)
    image_path = tmp_path / "generated.png"
    _png(image_path, (32, 32))

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

    # Still refused, and now it says so rather than leaving the tool's own
    # "[attach:...]" standing over an image the model never received.
    assert not any(b["type"] == "image_url" for b in _blocks(hydrated[0]))
    note = "".join(_texts(hydrated[0]))
    assert "outside the workspace" in note
    assert "2000px" not in note and "chrome_screenshot" not in note


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
    _png(outside, (32, 32))

    messages = [_file_read_tool_message(str(outside))]
    hydrated = window_images_for_llm(
        messages,
        LLMConfig(provider="anthropic", model="claude-sonnet-4"),
    )

    assert hydrated is not messages
    assert hydrated[0].content[1]["type"] == "image_url"


# --------------------------------------------------------------------------- #
# Pixel ceiling: a tall image is small in bytes and still fails the request
# --------------------------------------------------------------------------- #

def test_tall_image_is_downscaled_and_disclosed(vision_workspace):
    # The measured turn-killer: 1368x2088 sails through every byte cap and the
    # provider rejects the whole request over its dimensions.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    assert image_path.stat().st_size < 5 * 1024 * 1024

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    width, height = _delivered_size(hydrated[0])
    assert max(width, height) == 2000
    assert abs(width / height - 1368 / 2088) < 0.01  # aspect preserved
    note = next(t for t in _texts(hydrated[0]) if t.startswith("[Image downscaled"))
    assert f"from 1368x2088 to {width}x{height}" in note
    assert "2000px" in note


def test_tall_image_already_in_history_is_fitted_on_every_replay(vision_workspace):
    # Why this is worse than one dead turn: the oversized image STAYS in the
    # transcript, so every later turn re-sends it and dies the same way before
    # the agent runs anything. Fitting happens on replay (the checkpoint keeps
    # only the artifact path, hydrated from disk each call), so a thread that
    # already hit the 400 comes back to life without editing its history.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    history = [
        HumanMessage(content="take a screenshot of that page"),
        AIMessage(content="Captured it."),
        _tool_message(str(image_path), source="browser_screenshot"),
        HumanMessage(content="thanks, now what does the header say?"),
    ]

    replayed = window_images_for_llm(history, _ANTHROPIC)

    assert max(_delivered_size(replayed[2])) == 2000
    # Only the image-bearing message is rewritten; the rest of the transcript
    # is passed through untouched.
    assert [replayed[i] is history[i] for i in (0, 1, 3)] == [True, True, True]


def test_rotated_photo_discloses_an_orientation_consistent_pair(vision_workspace, monkeypatch):
    # EXIF orientation 6 is a quarter turn. The delivered side is measured after
    # the transpose, so measuring the source as STORED would disclose a
    # transposed pair ("from 3000x1000 to 666x2000"), which reads as a bug.
    monkeypatch.setattr(
        generated_image_context, "get_model_max_image_dimension", lambda _model: 2000
    )
    image_path = vision_workspace / "photo.jpg"
    img = Image.new("RGB", (3000, 1000), "white")
    exif = img.getexif()
    exif[0x0112] = 6
    img.save(image_path, format="JPEG", exif=exif)

    hydrated = window_images_for_llm(
        [_tool_message(str(image_path), source="file_read")], _ANTHROPIC
    )

    note = next(t for t in _texts(hydrated[0]) if t.startswith("[Image downscaled"))
    delivered = _delivered_size(hydrated[0])
    assert delivered[1] > delivered[0]  # the transpose really happened
    assert f"from 1000x3000 to {delivered[0]}x{delivered[1]}" in note


def test_a_byte_driven_shrink_is_not_blamed_on_the_pixel_limit():
    # _encode_to_fit shrinks PAST the ceiling when the byte budget still does not
    # fit, so a delivered edge short of the ceiling was driven by bytes. Asserted
    # on the note builder directly: reaching this through a real image needs a
    # re-encode that grows a file already under the cap, which is not a shape a
    # readable fixture can pin.
    payload = generated_image_context._ImagePayload(
        "data:image/jpeg;base64,QQ==", True, (4000, 4000), (900, 900)
    )

    note = generated_image_context._downscale_note(payload, None, 2000, 5 * 1024 * 1024)

    assert "from 4000x4000 to 900x900" in note
    assert "5 MB image budget" in note
    fractional = generated_image_context._downscale_note(payload, None, 2000, 3_670_016)
    assert "3.5 MB image budget" in fractional  # not the floor, "3 MB"
    assert "2000px" not in note  # the pixels are not why it ended up this small


def test_screenshot_downscale_names_the_region_recovery_route(vision_workspace):
    image_path = vision_workspace / "shot.png"
    _png(image_path, (1368, 2088))

    from_screenshot = window_images_for_llm(
        [_tool_message(str(image_path), source="browser_screenshot")], _ANTHROPIC
    )
    from_image_gen = window_images_for_llm(
        [_tool_message(str(image_path), source="image_gen")], _ANTHROPIC
    )

    shot_note = next(t for t in _texts(from_screenshot[0]) if t.startswith("[Image downscaled"))
    assert "chrome_screenshot" in shot_note and "region_ref" in shot_note
    # The route is only offered where it exists: a generated image has none.
    gen_note = next(t for t in _texts(from_image_gen[0]) if t.startswith("[Image downscaled"))
    assert "chrome_screenshot" not in gen_note


def test_image_inside_the_ceiling_is_sent_untouched_and_silently(vision_workspace):
    image_path = vision_workspace / "normal.png"
    _png(image_path, (1368, 900))
    original = image_path.read_bytes()

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    blocks = _blocks(hydrated[0])
    assert len(blocks) == 2  # the tool text and the image, nothing else
    assert not any("downscaled" in text.lower() for text in _texts(hydrated[0]))
    url = _image_block(hydrated[0])["image_url"]["url"]
    assert base64.b64decode(url.partition(",")[2]) == original  # byte-identical


def test_ceiling_comes_from_the_model_not_a_constant(vision_workspace, monkeypatch):
    monkeypatch.setattr(
        generated_image_context, "get_model_max_image_dimension", lambda _model: 512
    )
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    assert max(_delivered_size(hydrated[0])) == 512
    assert "512px" in next(t for t in _texts(hydrated[0]) if t.startswith("[Image downscaled"))


def test_image_at_the_ceiling_exactly_is_untouched(vision_workspace):
    image_path = vision_workspace / "square.png"
    _png(image_path, (2000, 2000))
    original = image_path.read_bytes()

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    url = _image_block(hydrated[0])["image_url"]["url"]
    assert base64.b64decode(url.partition(",")[2]) == original
    assert not any("downscaled" in text.lower() for text in _texts(hydrated[0]))


def test_lossless_source_stays_lossless_when_the_byte_cap_allows(vision_workspace):
    # A screenshot's value is small text. Trimming 4 percent of its pixels is no
    # reason to hand the model a JPEG when it is 10x inside the byte cap.
    image_path = vision_workspace / "text-shot.png"
    _png(image_path, (1368, 2088))

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    assert _image_block(hydrated[0])["image_url"]["url"].startswith("data:image/png;base64,")


def test_oversized_image_that_cannot_be_reduced_is_not_sent_but_is_disclosed(
    vision_workspace, monkeypatch
):
    # Never send what the provider will reject: a failed downscale drops the one
    # image rather than falling through to the original. It does NOT go silent
    # either, because the tool's own text still says a picture is attached.
    monkeypatch.setattr(
        "nymeria.tools.image_read.prepare_image_for_native_context",
        lambda *_a, **_kw: (None, None, "image is corrupt or unreadable"),
    )
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    assert not any(b["type"] == "image_url" for b in _blocks(hydrated[0]))
    note = next(t for t in _texts(hydrated[0]) if t.startswith("[Image not shown"))
    assert "image is corrupt or unreadable" in note


def test_a_failed_fit_is_not_retried_on_every_call(vision_workspace, monkeypatch):
    # Nothing persists a failure to disk, so without memoizing it an irreducible
    # image re-runs eight LANCZOS rounds on every LLM call, forever, to produce
    # the same nothing.
    import nymeria.tools.image_read as image_read

    calls: list[int] = []

    def failing(_path, **_kwargs):
        calls.append(1)
        return None, None, "image could not be reduced"

    monkeypatch.setattr(image_read, "prepare_image_for_native_context", failing)
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]

    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-fail")
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-fail")

    assert len(calls) == 1


def test_image_whose_dimensions_cannot_be_read_is_never_sent(vision_workspace):
    # The pixel axis must fail CLOSED like the byte axis. An image we cannot
    # measure could be any size, and being wrong costs the whole request plus
    # every replay of it, so it is dropped with a note instead of gambled.
    image_path = vision_workspace / "truncated.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00truncated-before-the-pixels")

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    assert not any(b["type"] == "image_url" for b in _blocks(hydrated[0]))
    assert "dimensions could not be read" in "".join(_texts(hydrated[0]))


def test_an_over_ceiling_image_is_resized_once_and_then_reused(vision_workspace, monkeypatch):
    # The resize costs ~180ms and this transform runs over the WHOLE history on
    # every LLM call, so paying it per call would cost seconds on a thread full
    # of screenshots. The fitted copy is persisted, and the in-memory cache is
    # cleared here so only the on-disk copy can explain the saving.
    import nymeria.tools.image_read as image_read

    calls: list[int] = []
    real = image_read.prepare_image_for_native_context

    def counting(path, **kwargs):
        calls.append(kwargs["long_edge_ceiling"])
        return real(path, **kwargs)

    monkeypatch.setattr(image_read, "prepare_image_for_native_context", counting)
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]

    generated_image_context._ENCODE_CACHE.clear()
    first = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-fit")
    generated_image_context._ENCODE_CACHE.clear()
    second = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-fit")

    assert calls == [2000]
    assert _delivered_size(first[0]) == _delivered_size(second[0]) == (1310, 2000)
    fitted = list((vision_workspace / "threads" / "t-fit" / "fitted").glob("fitted_*"))
    assert len(fitted) == 1


def test_a_ceiling_change_refits_instead_of_reusing_the_old_copy(vision_workspace, monkeypatch):
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]

    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-fit")
    monkeypatch.setattr(
        generated_image_context, "get_model_max_image_dimension", lambda _model: 512
    )
    generated_image_context._ENCODE_CACHE.clear()
    narrower = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-fit")

    assert max(_delivered_size(narrower[0])) == 512  # not the stale 2000px copy
    fitted = list((vision_workspace / "threads" / "t-fit" / "fitted").glob("fitted_*"))
    assert len(fitted) == 2  # keyed by the limits, so both copies coexist


def test_a_rewritten_source_is_refitted_not_served_from_the_old_copy(vision_workspace):
    # The stem hashes mtime_ns and size, so overwriting the path the artifact
    # points at cannot keep serving the previous picture.
    #
    # NOT covered, deliberately: a rewrite landing on the SAME byte length
    # within one filesystem timestamp tick keys identically and would be served
    # from the old copy. Accepted rather than paid for with a content hash of
    # every image on every call (see the note on _fit_image_payload_cached);
    # generated images get unique paths, so the key rarely repeats at all.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]

    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-rewrite")
    _png(image_path, (2400, 2400))  # same path, different image
    generated_image_context._ENCODE_CACHE.clear()
    after = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-rewrite")

    assert _delivered_size(after[0]) == (2000, 2000)
    assert len(list((vision_workspace / "threads" / "t-rewrite" / "fitted").glob("*"))) == 2


def test_a_torn_fitted_copy_is_refitted_rather_than_sent(vision_workspace):
    # The fitted copy is a file in an agent-writable workspace and a crashed
    # write can tear it, so it is re-measured before it is trusted. Serving it
    # unchecked would put the brick back one branch below the check that kills it.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]
    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-torn")
    fitted = next((vision_workspace / "threads" / "t-torn" / "fitted").glob("fitted_*"))
    fitted.write_bytes(b"\x89PNG\r\n\x1a\nHALF-WRITTEN")

    generated_image_context._ENCODE_CACHE.clear()
    after = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-torn")

    assert _delivered_size(after[0]) == (1310, 2000)  # re-fitted, not the 20 bytes
    assert fitted.stat().st_size > 1000


def test_an_oversized_fitted_copy_is_refitted_rather_than_sent(vision_workspace):
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]
    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-big")
    fitted = next((vision_workspace / "threads" / "t-big" / "fitted").glob("fitted_*"))
    _png(fitted, (6000, 6000))  # tampered: over the ceiling again

    generated_image_context._ENCODE_CACHE.clear()
    after = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-big")

    assert max(_delivered_size(after[0])) == 2000


def test_a_failed_publish_leaves_no_litter_behind(vision_workspace, monkeypatch):
    # Random-suffix temp plus unlink on failure, the convention
    # core/storage_paths.write_text_atomic documents: a target-derived temp name
    # is shared by concurrent writers and the loser publishes a truncated file.
    from pathlib import Path as _Path

    def boom(self, _target):
        raise OSError("no space left on device")

    monkeypatch.setattr(_Path, "replace", boom)
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))

    generated_image_context._ENCODE_CACHE.clear()
    hydrated = window_images_for_llm(
        [_tool_message(str(image_path))], _ANTHROPIC, thread_id="t-fail-publish"
    )

    assert max(_delivered_size(hydrated[0])) == 2000  # the turn is unaffected
    assert list((vision_workspace / "threads" / "t-fail-publish" / "fitted").glob("*")) == []


def test_two_writers_of_one_fit_do_not_share_a_temp_name(vision_workspace, monkeypatch):
    # A temp name derived from the target is shared by two turns of the same
    # thread fitting the same image, and the loser publishes a truncated file.
    from pathlib import Path as _Path

    written: list[str] = []
    real_write = _Path.write_bytes

    def capture(self, data):
        written.append(self.name)
        return real_write(self, data)

    monkeypatch.setattr(_Path, "write_bytes", capture)
    generated_image_context._persist_fit("fitted_same", "t-race", b"one", "image/png")
    generated_image_context._persist_fit("fitted_same", "t-race", b"two", "image/png")

    assert len(set(written)) == 2
    assert not any(name.endswith(".part") for name in written)


def test_a_fitted_copy_whose_bytes_are_not_its_extension_is_refitted(vision_workspace):
    # Size is only half of what makes a payload valid. The media type is
    # declared from the file's EXTENSION, and a fitted copy lives in an
    # agent-writable directory, so a `.png` holding JPEG bytes would be sent as
    # `data:image/png` and refused by the provider for the type instead of the
    # size: the same request-killing 400, one branch below the check for it.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]
    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-lying")
    fitted = next((vision_workspace / "threads" / "t-lying" / "fitted").glob("fitted_*.png"))
    # In the ceiling, so only the type is wrong.
    Image.new("RGB", (800, 600), "white").save(fitted, format="JPEG")

    generated_image_context._ENCODE_CACHE.clear()
    after = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-lying")

    url = _image_block(after[0])["image_url"]["url"]
    raw = base64.b64decode(url.partition(",")[2])
    with Image.open(io.BytesIO(raw)) as img:
        actual_format = img.format
    assert url.startswith("data:image/png;base64,") and actual_format == "PNG"
    assert _delivered_size(after[0]) == (1310, 2000)  # re-fitted, not the planted 800x600


def test_an_unusable_fitted_copy_is_removed_rather_than_refitted_forever(
    vision_workspace, monkeypatch
):
    # The lookup takes the FIRST {stem}.* match, so a bad copy whose extension
    # differs from the one a re-fit publishes is never overwritten. Left on
    # disk it costs a full resize on every LLM call, forever, with a healthy
    # sibling sitting beside it.
    import nymeria.tools.image_read as image_read

    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]
    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-sticky")
    good = next((vision_workspace / "threads" / "t-sticky" / "fitted").glob("fitted_*.png"))
    bad = good.with_suffix(".jpg")  # sorts before .png, so it is found first
    Image.new("RGB", (6000, 6000), "white").save(bad, format="JPEG")

    fits: list[str] = []
    real = image_read.prepare_image_for_native_context

    def counting(path, **kwargs):
        fits.append(str(path))
        return real(path, **kwargs)

    monkeypatch.setattr(image_read, "prepare_image_for_native_context", counting)
    generated_image_context._ENCODE_CACHE.clear()
    first = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-sticky")
    generated_image_context._ENCODE_CACHE.clear()
    second = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-sticky")

    assert not bad.exists()  # the copy that failed its check is gone
    assert len(fits) == 1  # one recovery re-fit, then the healthy copy serves
    assert max(_delivered_size(first[0])) == max(_delivered_size(second[0])) == 2000


def test_the_trim_evicts_the_oldest_fit_first(vision_workspace, monkeypatch):
    # Oldest first is the whole point: newest-first would evict the copy that
    # was just published for the image the model is about to be sent, so the
    # thread would re-fit the same picture on every call.
    from nymeria.core.attachment_sandbox import get_thread_fitted_image_dir

    monkeypatch.setattr(generated_image_context, "_FITTED_STORE_MAX_BYTES", 25_000)
    directory = get_thread_fitted_image_dir("t-order")
    for index in range(4):
        target = directory / f"fitted_{index}.png"
        target.write_bytes(b"x" * 10_000)
        stamp = 1_700_000_000 + index
        os.utime(target, (stamp, stamp))

    generated_image_context._trim_fitted_store(directory)

    assert sorted(f.name for f in directory.glob("*")) == ["fitted_2.png", "fitted_3.png"]


def test_a_trim_that_evicts_says_so_at_warning(vision_workspace, monkeypatch, caplog):
    # Past the ceiling there is no hysteresis: every call re-fits every image in
    # the window, forever, and the only symptom is a slow thread. This log line
    # is the one chance an operator has to see it coming.
    from nymeria.core.attachment_sandbox import get_thread_fitted_image_dir

    monkeypatch.setattr(generated_image_context, "_FITTED_STORE_MAX_BYTES", 15_000)
    directory = get_thread_fitted_image_dir("t-loud")
    (directory / "fitted_a.png").write_bytes(b"x" * 10_000)

    with caplog.at_level("WARNING", logger=generated_image_context.__name__):
        generated_image_context._trim_fitted_store(directory)
        assert caplog.records == []  # nothing evicted, nothing said

        (directory / "fitted_b.png").write_bytes(b"x" * 10_000)
        generated_image_context._trim_fitted_store(directory)

    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "evicted 1" in caplog.records[0].getMessage()


def test_the_trim_leaves_another_writers_inflight_temp_alone(vision_workspace, monkeypatch):
    # A .tmp belongs to a concurrent writer that has not published yet. Deleting
    # it costs that turn a re-fit for nothing, and counting bytes nobody can
    # read yet makes the trim evict published copies it did not need to.
    from nymeria.core.attachment_sandbox import get_thread_fitted_image_dir

    monkeypatch.setattr(generated_image_context, "_FITTED_STORE_MAX_BYTES", 20_000)
    directory = get_thread_fitted_image_dir("t-inflight")
    published = directory / "fitted_a.png"
    published.write_bytes(b"x" * 10_000)
    inflight = directory / "fitted_b.png.deadbeef.tmp"
    inflight.write_bytes(b"x" * 30_000)

    generated_image_context._trim_fitted_store(directory)

    assert inflight.exists()  # not this trim's to delete
    assert published.exists()  # and its bytes never counted toward the ceiling


def test_a_thread_id_the_sandbox_rejects_cannot_break_the_turn(vision_workspace):
    # ValueError, not OSError: the persist and lookup helpers catch Exception on
    # purpose, because no storage fault is worth failing an LLM call over, and a
    # narrower clause would let this one through into the outbound transform.
    from nymeria.core import attachment_sandbox

    with pytest.raises(ValueError):
        attachment_sandbox.get_thread_fitted_image_dir(".", create=False)

    assert generated_image_context._persisted_fit("fitted_x", ".") is None
    generated_image_context._persist_fit("fitted_x", ".", b"data", "image/png")


def test_looking_for_a_fitted_copy_never_creates_the_directory(vision_workspace):
    # A miss is the common case on a thread that has never needed a fit, so a
    # read that mkdir-and-chmods would run on every image of every call, and
    # would also silently repair a directory an operator had locked down.
    assert generated_image_context._persisted_fit("fitted_none", "t-cold") is None

    assert not (vision_workspace / "threads" / "t-cold").exists()


def _text_page(path, size, mode="RGB") -> None:
    """A page of small text: the shape that inflates when it is resampled."""
    from PIL import ImageDraw, ImageFont

    img = Image.new(mode, size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    line = "The quick brown fox jumps over the lazy dog 0123456789 " * 8
    for y in range(2, size[1] - 12, 12):
        draw.text((2, y), line, fill=(20, 20, 20), font=font)
    img.save(path, format="PNG")


def test_a_resized_text_screenshot_does_not_arrive_bigger_than_its_own_budget(vision_workspace):
    # Image TOKENS follow dimensions, so bytes bought by keeping the source
    # lossless buy nothing but wire cost, and 100 of these ride one request.
    # Measured on this fixture: 1.7 MB delivered with the growth budget wired
    # up, 4.1 MB without it (the kept PNG), from a 0.5 MB source.
    image_path = vision_workspace / "page.png"
    _text_page(image_path, (1368, 2088))
    generated_image_context._ENCODE_CACHE.clear()

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    raw = base64.b64decode(_image_block(hydrated[0])["image_url"]["url"].partition(",")[2])
    assert max(_delivered_size(hydrated[0])) == 2000
    assert len(raw) < 2_500_000


def test_an_opaque_rgba_screenshot_is_bounded_like_any_other(vision_workspace):
    # A mode with an alpha band is not an image that USES one: Chrome's PNG
    # captures are RGBA with every pixel opaque. Treating those as alpha images
    # pinned them to the PNG-only branch, where neither the JPEG ladder nor the
    # growth budget applies. Measured on this fixture: 1.7 MB against 4.6 MB.
    image_path = vision_workspace / "page_rgba.png"
    _text_page(image_path, (1368, 2088), mode="RGBA")
    with Image.open(image_path) as probe:
        assert probe.mode == "RGBA"
    generated_image_context._ENCODE_CACHE.clear()

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    raw = base64.b64decode(_image_block(hydrated[0])["image_url"]["url"].partition(",")[2])
    assert max(_delivered_size(hydrated[0])) == 2000
    assert len(raw) < 2_500_000


def test_real_transparency_is_never_traded_for_bytes(vision_workspace):
    # The other side of that narrowing, on the workload where it bites: this is
    # the same page whose lossless encode blows the growth budget, so a wrongly
    # opaque verdict sends it as a JPEG with its alpha flattened. A lost alpha
    # channel is a visible, unrecoverable change; the bytes are only bytes.
    image_path = vision_workspace / "glass.png"
    _text_page(image_path, (1368, 2088), mode="RGBA")
    with Image.open(image_path) as img:
        img = img.convert("RGBA")
        img.putpixel((10, 10), (0, 0, 0, 0))  # one genuinely transparent pixel
        img.save(image_path, format="PNG")
    generated_image_context._ENCODE_CACHE.clear()

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    url = _image_block(hydrated[0])["image_url"]["url"]
    raw = base64.b64decode(url.partition(",")[2])
    with Image.open(io.BytesIO(raw)) as out:
        assert out.mode in ("RGBA", "LA", "PA", "P")
    assert url.startswith("data:image/png;base64,")


def test_a_source_that_vanishes_mid_fit_is_not_offered_a_region_recapture(
    vision_workspace, monkeypatch
):
    # The TOCTOU branch: the file was there when it was resolved and gone by the
    # time it was encoded. It is a missing file, not a big one, so naming a
    # pixel limit and offering to re-capture a region of it is the wrong-target
    # diagnosis the drop notes exist to stop giving.
    import nymeria.tools.image_read as image_read

    monkeypatch.setattr(
        image_read,
        "prepare_image_for_native_context",
        lambda *_a, **_k: (None, None, "could not stat image: [Errno 2] No such file or directory"),
    )
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))

    generated_image_context._ENCODE_CACHE.clear()
    hydrated = window_images_for_llm(
        [_tool_message(str(image_path), source="browser_screenshot")], _ANTHROPIC
    )

    note = "".join(_texts(hydrated[0]))
    assert "the file is no longer on disk" in note  # the same sentence a resolve-time miss gets
    assert "could not stat image" not in note  # not the raw errno string either
    assert "2000px" not in note and "chrome_screenshot" not in note


def test_the_fitted_store_is_trimmed_to_its_disk_ceiling(vision_workspace, monkeypatch):
    # Unbounded growth would be one copy per (image, limits) for the life of a
    # long thread. Trimming is safe because a missing copy is not a missing
    # image: the next replay re-fits it.
    monkeypatch.setattr(generated_image_context, "_FITTED_STORE_MAX_BYTES", 20_000)
    messages = []
    for index in range(4):
        path = vision_workspace / f"tall{index}.png"
        _png(path, (1368, 2088 + index))
        messages.append(_tool_message(str(path)))

    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm(messages, _ANTHROPIC, thread_id="t-trim")

    fitted = list((vision_workspace / "threads" / "t-trim" / "fitted").glob("*"))
    assert 0 < len(fitted) < 4
    assert sum(f.stat().st_size for f in fitted) <= 20_000


def test_a_storage_fault_is_disclosed_rather_than_swallowed(vision_workspace, monkeypatch):
    # The branch a real filesystem produces (permissions, EIO, a source that
    # vanished mid-fit). It used to return the message untouched, leaving the
    # tool's own "attached for you to view" standing over nothing.
    from pathlib import Path as _Path

    real_read = _Path.read_bytes

    def denied(self):
        if self.suffix == ".png":
            raise PermissionError(13, "Permission denied")
        return real_read(self)

    monkeypatch.setattr(_Path, "read_bytes", denied)
    image_path = vision_workspace / "small.png"
    _png(image_path, (64, 64))

    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    assert not any(b["type"] == "image_url" for b in _blocks(hydrated[0]))
    assert "could not be read from disk" in "".join(_texts(hydrated[0]))


def test_deleting_the_thread_takes_its_fitted_copies_with_it(vision_workspace):
    from nymeria.core.attachment_sandbox import cleanup_thread_attachments

    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    generated_image_context._ENCODE_CACHE.clear()
    window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC, thread_id="t-doomed")
    fitted_dir = vision_workspace / "threads" / "t-doomed" / "fitted"
    assert list(fitted_dir.glob("fitted_*"))

    cleanup_thread_attachments("t-doomed")

    assert not fitted_dir.exists()  # rides along with the thread, no separate sweep


def test_the_in_memory_payload_cache_is_keyed_by_the_limits_too(vision_workspace, monkeypatch):
    # The disk copy is keyed by the limits, but the in-memory cache is consulted
    # FIRST, so it needs the same key or a model switch inside one process serves
    # the previous model's picture. Deliberately no cache clear here.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))
    messages = [_tool_message(str(image_path))]

    generated_image_context._ENCODE_CACHE.clear()
    first = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-key")
    monkeypatch.setattr(
        generated_image_context, "get_model_max_image_dimension", lambda _model: 512
    )
    second = window_images_for_llm(messages, _ANTHROPIC, thread_id="t-key")

    assert max(_delivered_size(first[0])) == 2000
    assert max(_delivered_size(second[0])) == 512


def test_fitting_still_works_without_a_thread_to_persist_into(vision_workspace):
    # No thread id (a headless caller): the fit is not persisted and simply costs
    # CPU again next time, which must not turn into a failure to send the image.
    image_path = vision_workspace / "tall.png"
    _png(image_path, (1368, 2088))

    generated_image_context._ENCODE_CACHE.clear()
    hydrated = window_images_for_llm([_tool_message(str(image_path))], _ANTHROPIC)

    assert max(_delivered_size(hydrated[0])) == 2000
    assert not (vision_workspace / "threads").exists()


def test_byte_cap_still_skips_rather_than_downscales(vision_workspace, monkeypatch):
    # The two axes stay distinct, and the pixel one did not swallow the byte
    # one: this image is INSIDE the pixel ceiling and over the byte cap, so the
    # only correct outcome is the skip that shipped before the ceiling existed.
    monkeypatch.setattr(
        generated_image_context, "get_attachment_limits", lambda _model: {"max_image_bytes": 40_000}
    )
    image_path = vision_workspace / "noisy.png"
    # Random noise compresses poorly, so this PNG is comfortably over the cap.
    Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3)).save(image_path, format="PNG")
    assert image_path.stat().st_size > 40_000
    assert max(Image.open(image_path).size) <= 2000

    hydrated = window_images_for_llm(
        [_tool_message(str(image_path), source="browser_screenshot")], _ANTHROPIC
    )

    assert not any(b["type"] == "image_url" for b in _blocks(hydrated[0]))
    note = "".join(_texts(hydrated[0]))
    assert "over this model's 40000-byte image limit" in note
    # A byte problem is not a pixel problem, so no pixel limit is named. The
    # recovery route IS offered: a smaller region is a real fix for a drop about
    # size, whichever axis it was.
    assert "2000px" not in note
    assert "chrome_screenshot with region" in note


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

    # Exceeds the 4-byte model cap: dropped, and disclosed as dropped.
    assert not any(b["type"] == "image_url" for b in _blocks(hydrated[0]))
    assert "over this model's 4-byte image limit" in "".join(_texts(hydrated[0]))
