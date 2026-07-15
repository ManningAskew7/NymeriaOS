"""Regression tests for model metadata fallback behavior."""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import cast

import pytest

from nymeria.config import model_capabilities as capabilities
from nymeria.config import pricing_table as _pricing_table
from nymeria.config.model_capabilities import ModelInfo


def _set_model_cache(monkeypatch, models: dict[str, ModelInfo]) -> None:
    monkeypatch.setattr(capabilities, "_model_cache", models)
    monkeypatch.setattr(capabilities, "_cache_timestamp", time.time())
    monkeypatch.setattr(capabilities, "_cache_populated", True)
    monkeypatch.setattr(capabilities, "_cache_failure_timestamp", 0)


def _clear_model_cache(monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "_model_cache", {})
    monkeypatch.setattr(capabilities, "_cache_timestamp", 0)
    monkeypatch.setattr(capabilities, "_cache_populated", False)
    monkeypatch.setattr(capabilities, "_cache_failure_timestamp", 0)


def test_bare_openai_id_uses_openrouter_metadata(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {
            "openai/gpt-future": ModelInfo(
                id="openai/gpt-future",
                context_length=777777,
                max_completion_tokens=12345,
            )
        },
    )

    assert capabilities.get_context_limit("gpt-future") == 777777
    assert capabilities.get_model_info("gpt-future").id == "openai/gpt-future"


def test_bare_openai_snapshot_id_uses_base_model_metadata(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {
            "openai/gpt-future": ModelInfo(
                id="openai/gpt-future",
                context_length=777777,
            )
        },
    )

    assert capabilities.get_context_limit("gpt-future-2026-04-23") == 777777


def test_gpt_55_static_fallback_uses_long_context_when_metadata_missing(monkeypatch):
    _set_model_cache(monkeypatch, {})

    assert capabilities.get_context_limit("gpt-5.5") == 1050000
    assert capabilities.get_context_limit("gpt-5.5-2026-04-23") == 1050000


def test_static_fallback_recognizes_modern_multimodal_models(monkeypatch):
    _set_model_cache(monkeypatch, {})

    vision_models = [
        "gpt-5.5",
        "gpt-5.5-2026-04-23",
        "openai/gpt-5.4-mini",
        "claude-sonnet-4-20250514",
        "claude-opus-4-7",
        "anthropic/claude-sonnet-4.6",
        "google/gemini-2.5-flash",
        "google/gemini-3-pro-preview",
    ]
    document_models = [
        "gpt-5.5",
        "openai/gpt-5.4-mini",
        "claude-sonnet-4-20250514",
        "claude-opus-4-7",
        "anthropic/claude-sonnet-4.6",
        "google/gemini-2.5-flash",
        "google/gemini-3-pro-preview",
    ]

    for model in vision_models:
        assert capabilities.supports_vision(model), model

    for model in document_models:
        assert capabilities.supports_documents(model), model


def test_static_fallback_does_not_match_distinct_hyphenated_models(monkeypatch):
    _set_model_cache(monkeypatch, {})

    assert not capabilities._fallback_check("gpt-4o-mini", {"gpt-4o"})
    assert not capabilities._fallback_check("gpt-5-mini", {"openai/gpt-5"})
    assert capabilities._fallback_check("gpt-5-2025-08-07", {"openai/gpt-5"})
    assert capabilities._fallback_check("claude-sonnet-4-20250514", {"claude-sonnet-4"})


def test_failed_openrouter_fetch_uses_short_negative_ttl(monkeypatch):
    _clear_model_cache(monkeypatch)
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(capabilities, "_fetch_openrouter_models", fake_fetch)

    assert capabilities._ensure_cache() == {}
    assert capabilities._ensure_cache() == {}

    assert calls == 1


def test_expired_openrouter_failure_ttl_allows_retry(monkeypatch):
    _clear_model_cache(monkeypatch)
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(capabilities, "_fetch_openrouter_models", fake_fetch)
    monkeypatch.setattr(
        capabilities,
        "_cache_failure_timestamp",
        time.time() - capabilities._CACHE_FAILURE_TTL_SECONDS - 1,
    )

    capabilities._ensure_cache()

    assert calls == 1


def test_get_attachment_limits_matches_claude_family(monkeypatch):
    _set_model_cache(monkeypatch, {})

    limits = capabilities.get_attachment_limits("claude-opus-4-7")
    assert limits["max_images_per_request"] == 100
    assert limits["max_image_bytes"] == 5 * 1024 * 1024
    assert limits["max_pdf_pages"] == 100
    assert limits["max_total_bytes"] == 32 * 1024 * 1024

    legacy_limits = capabilities.get_attachment_limits("claude-3-opus")
    assert legacy_limits["max_images_per_request"] == 100


def test_get_attachment_limits_matches_openai_family(monkeypatch):
    _set_model_cache(monkeypatch, {})

    limits = capabilities.get_attachment_limits("gpt-5.5")
    assert limits["max_images_per_request"] == 1500
    assert limits["max_total_bytes"] == 512 * 1024 * 1024
    # OpenAI does not publish a per-PDF page cap.
    assert limits["max_pdf_pages"] is None


def test_get_attachment_limits_matches_gemini_family(monkeypatch):
    _set_model_cache(monkeypatch, {})

    limits = capabilities.get_attachment_limits("gemini-2.5-pro")
    assert limits["max_images_per_request"] == 3000
    assert limits["max_pdf_pages"] == 1000


def test_get_attachment_limits_falls_back_to_defaults_for_unknown(monkeypatch):
    _set_model_cache(monkeypatch, {})

    limits = capabilities.get_attachment_limits("totally-unknown-model-x")
    assert limits["max_images_per_request"] == 16
    assert limits["max_pdf_pages"] == 100


def test_get_attachment_limits_uses_live_modelinfo_overrides(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {
            "anthropic/claude-mystery": ModelInfo(
                id="anthropic/claude-mystery",
                max_images_per_request=7,
                max_image_bytes=1234,
                max_pdf_pages=42,
                max_total_attachment_bytes=999,
            )
        },
    )

    limits = capabilities.get_attachment_limits("anthropic/claude-mystery")
    assert limits["max_images_per_request"] == 7
    assert limits["max_image_bytes"] == 1234
    assert limits["max_pdf_pages"] == 42
    assert limits["max_total_bytes"] == 999


def test_evaluate_attachment_compatibility_anthropic_uses_live_modalities(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {
            "claude-opus-4-7": ModelInfo(
                id="claude-opus-4-7",
                input_modalities={"text", "image", "file"},
            )
        },
    )

    report = capabilities.evaluate_attachment_compatibility(
        "claude-opus-4-7",
        "anthropic",
        [
            {"file_type": "image", "mime_type": "image/png", "file_name": "a.png", "data_url": "data:,"},
            {"file_type": "document", "mime_type": "application/pdf", "file_name": "b.pdf", "data_url": "data:,"},
        ],
    )

    assert report["compatible"] is True
    assert report["model_input_modalities"] == ["file", "image", "text"]


def test_evaluate_attachment_compatibility_anthropic_flags_unreported_file(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {
            "claude-image-only": ModelInfo(
                id="claude-image-only",
                input_modalities={"text", "image"},
            )
        },
    )

    report = capabilities.evaluate_attachment_compatibility(
        "claude-image-only",
        "anthropic",
        [
            {"file_type": "document", "mime_type": "application/pdf", "file_name": "b.pdf", "data_url": "data:,"},
        ],
    )

    assert report["compatible"] is False
    assert "file" in report["unsupported_modalities"]


# ---------------------------------------------------------------------------
# F8 characterization suite: full-branch coverage of
# evaluate_attachment_compatibility. These lock the public contract (the
# `compatible` bool, the three sorted modality lists, and the warning strings +
# order) so the decomposition into _classify_attachment / _modality_supported
# stays behavior-preserving. They pass against the pre- and post-refactor code.
# ---------------------------------------------------------------------------

def _att(file_type, mime_type, file_name="f"):
    return {
        "file_type": file_type,
        "mime_type": mime_type,
        "file_name": file_name,
        "data_url": "data:,",
    }


def test_eval_empty_and_none_attachments_are_compatible(monkeypatch):
    _clear_model_cache(monkeypatch)
    for attachments in (None, []):
        report = capabilities.evaluate_attachment_compatibility("m", "openai", attachments)
        assert report == {
            "compatible": True,
            "model_input_modalities": [],
            "required_modalities": [],
            "unsupported_modalities": [],
            "warnings": [],
        }


def test_eval_else_provider_never_consults_live_modalities(monkeypatch):
    # A non-openrouter/anthropic provider must NOT report live modalities, even
    # when the model is cached with them.
    _set_model_cache(
        monkeypatch,
        {"m": ModelInfo(id="m", input_modalities={"text", "image", "file"})},
    )
    report = capabilities.evaluate_attachment_compatibility("m", "openai", [_att("image", "image/png")])
    assert report["model_input_modalities"] == []
    assert report["compatible"] is True


def test_eval_else_provider_image_static_fallback(monkeypatch):
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: False)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: True)
    report = capabilities.evaluate_attachment_compatibility("m", "openai", [_att("image", "image/png")])
    assert report["compatible"] is False
    assert report["required_modalities"] == ["image"]
    assert report["unsupported_modalities"] == ["image"]

    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    ok = capabilities.evaluate_attachment_compatibility("m", "openai", [_att("image", "image/png")])
    assert ok["compatible"] is True
    assert ok["unsupported_modalities"] == []


def test_eval_pdf_required_modality_is_file(monkeypatch):
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: True)
    report = capabilities.evaluate_attachment_compatibility(
        "m", "openai", [_att("document", "application/pdf", "b.pdf")]
    )
    assert report["required_modalities"] == ["file"]
    assert report["compatible"] is True


def test_eval_else_provider_pdf_static_fallback(monkeypatch):
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: False)
    report = capabilities.evaluate_attachment_compatibility(
        "m", "openai", [_att("document", "application/pdf", "b.pdf")]
    )
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["file"]


def test_eval_text_document_types_supported(monkeypatch):
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: True)
    for mime, name in (("text/plain", "a.txt"), ("text/markdown", "a.md"), ("text/csv", "a.csv")):
        report = capabilities.evaluate_attachment_compatibility("m", "openai", [_att("document", mime, name)])
        assert report["required_modalities"] == ["text"], mime
        assert report["compatible"] is True, mime


def test_eval_unsupported_document_mime_warns_exact_string(monkeypatch):
    _clear_model_cache(monkeypatch)
    report = capabilities.evaluate_attachment_compatibility(
        "m", "openai", [_att("document", "application/msword", "a.doc")]
    )
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["document"]
    assert report["warnings"] == [
        "Unsupported document type 'application/msword'. "
        "Supported documents are PDF, TXT, MD, and CSV."
    ]


def test_eval_unsupported_attachment_type_warns_exact_string(monkeypatch):
    _clear_model_cache(monkeypatch)
    report = capabilities.evaluate_attachment_compatibility(
        "m", "openai", [_att("video", "video/mp4", "a.mp4")]
    )
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["document"]
    assert report["warnings"] == [
        "Unsupported attachment type. Supported types are image and document."
    ]


def test_eval_openrouter_image_unsupported_when_not_reported(monkeypatch):
    _set_model_cache(monkeypatch, {"m": ModelInfo(id="m", input_modalities={"text"})})
    report = capabilities.evaluate_attachment_compatibility("m", "openrouter", [_att("image", "image/png")])
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["image"]
    assert report["model_input_modalities"] == ["text"]


def test_eval_openrouter_pdf_warns_but_stays_compatible(monkeypatch):
    # OpenRouter can pre-parse PDFs: warn when file input is unreported, but never
    # mark the attachment incompatible.
    _set_model_cache(monkeypatch, {"m": ModelInfo(id="m", input_modalities={"text", "image"})})
    report = capabilities.evaluate_attachment_compatibility(
        "m", "openrouter", [_att("document", "application/pdf", "b.pdf")]
    )
    assert report["compatible"] is True
    assert "file" not in report["unsupported_modalities"]
    assert report["warnings"] == [
        "This model does not report native file input. "
        "OpenRouter may parse PDFs before sending text to the model."
    ]


def test_eval_openrouter_pdf_no_warning_when_no_modalities_reported(monkeypatch):
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: True)
    report = capabilities.evaluate_attachment_compatibility(
        "m", "openrouter", [_att("document", "application/pdf", "b.pdf")]
    )
    assert report["warnings"] == []
    assert report["unsupported_modalities"] == []
    assert report["compatible"] is True


def test_eval_anthropic_pdf_static_fallback_when_no_modalities(monkeypatch):
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: False)
    report = capabilities.evaluate_attachment_compatibility(
        "m", "anthropic", [_att("document", "application/pdf", "b.pdf")]
    )
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["file"]
    assert report["model_input_modalities"] == []


def test_eval_text_unsupported_when_reported_modalities_lack_text(monkeypatch):
    _set_model_cache(monkeypatch, {"m": ModelInfo(id="m", input_modalities={"image"})})
    report = capabilities.evaluate_attachment_compatibility(
        "m", "anthropic", [_att("document", "text/plain", "a.txt")]
    )
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["text"]


def test_eval_mixed_attachments_all_supported_anthropic(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {"m": ModelInfo(id="m", input_modalities={"text", "image", "file"})},
    )
    report = capabilities.evaluate_attachment_compatibility(
        "m",
        "anthropic",
        [
            _att("image", "image/png"),
            _att("document", "application/pdf", "b.pdf"),
            _att("document", "text/plain", "a.txt"),
        ],
    )
    assert report["compatible"] is True
    assert report["required_modalities"] == ["file", "image", "text"]
    assert report["model_input_modalities"] == ["file", "image", "text"]
    assert report["warnings"] == []


def test_eval_anthropic_image_unsupported_when_reported_modalities_lack_image(monkeypatch):
    # The image check is now provider-agnostic; assert the anthropic arm too so a
    # future re-divergence from the openrouter path is caught.
    _set_model_cache(monkeypatch, {"m": ModelInfo(id="m", input_modalities={"text", "file"})})
    report = capabilities.evaluate_attachment_compatibility("m", "anthropic", [_att("image", "image/png")])
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["image"]


def test_eval_multiple_unsupported_modalities_are_sorted(monkeypatch):
    _set_model_cache(monkeypatch, {"m": ModelInfo(id="m", input_modalities={"text"})})
    report = capabilities.evaluate_attachment_compatibility(
        "m",
        "anthropic",
        [
            _att("image", "image/png"),
            _att("document", "application/pdf", "b.pdf"),
            _att("document", "application/msword", "d.doc"),
        ],
    )
    assert report["compatible"] is False
    assert report["unsupported_modalities"] == ["document", "file", "image"]


def test_eval_provider_normalized_before_match(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {"m": ModelInfo(id="m", input_modalities={"text", "image", "file"})},
    )
    report = capabilities.evaluate_attachment_compatibility("m", "  AnThRoPic  ", [_att("image", "image/png")])
    # Mixed-case/whitespace provider is normalized, so live modalities are consulted.
    assert report["model_input_modalities"] == ["file", "image", "text"]
    assert report["compatible"] is True


def test_eval_falsy_provider_uses_static_fallback(monkeypatch):
    # Both "" and None are normalized via `(provider or "")` and must take the
    # non-live (static-fallback) path. None is off the `str` contract but the
    # function defends against it, so the test does too.
    _clear_model_cache(monkeypatch)
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    for provider in ("", cast(str, None)):
        report = capabilities.evaluate_attachment_compatibility("m", provider, [_att("image", "image/png")])
        assert report["model_input_modalities"] == []
        assert report["compatible"] is True


def test_eval_warning_order_loop_warnings_precede_openrouter_pdf_note(monkeypatch):
    # An unsupported attachment (loop warning) plus an OpenRouter PDF (post-loop
    # warning): the loop warning must come first.
    _set_model_cache(monkeypatch, {"m": ModelInfo(id="m", input_modalities={"text", "image"})})
    report = capabilities.evaluate_attachment_compatibility(
        "m",
        "openrouter",
        [
            _att("document", "application/msword", "a.doc"),
            _att("document", "application/pdf", "b.pdf"),
        ],
    )
    assert report["warnings"] == [
        "Unsupported document type 'application/msword'. "
        "Supported documents are PDF, TXT, MD, and CSV.",
        "This model does not report native file input. "
        "OpenRouter may parse PDFs before sending text to the model.",
    ]


def test_modality_supported_prefers_reported_modalities(monkeypatch):
    # When modalities are reported, the static fallbacks must not be consulted.
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: False)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: False)
    assert capabilities._modality_supported("m", "image", {"image"}) is True
    assert capabilities._modality_supported("m", "file", {"image"}) is False
    assert capabilities._modality_supported("m", "text", {"text"}) is True
    assert capabilities._modality_supported("m", "text", {"image"}) is False


def test_modality_supported_static_fallback_when_unreported(monkeypatch):
    monkeypatch.setattr(capabilities, "supports_vision", lambda mid: True)
    monkeypatch.setattr(capabilities, "supports_documents", lambda mid: False)
    assert capabilities._modality_supported("m", "image", set()) is True
    assert capabilities._modality_supported("m", "file", set()) is False
    # text has no static fallback: unreported text is treated as supported.
    assert capabilities._modality_supported("m", "text", set()) is True


def test_classify_attachment_outcomes():
    assert capabilities._classify_attachment(
        {"file_type": "image", "mime_type": "image/png", "file_name": "a.png"}
    ) == ("image", False, False, None)

    assert capabilities._classify_attachment(
        {"file_type": "document", "mime_type": "application/pdf", "file_name": "b.pdf"}
    ) == ("file", True, False, None)

    assert capabilities._classify_attachment(
        {"file_type": "document", "mime_type": "text/plain", "file_name": "c.txt"}
    ) == ("text", False, False, None)

    other = capabilities._classify_attachment(
        {"file_type": "document", "mime_type": "application/msword", "file_name": "d.doc"}
    )
    assert other.required_modality is None
    assert other.is_pdf is False
    assert other.unsupported is True
    assert other.warning == (
        "Unsupported document type 'application/msword'. "
        "Supported documents are PDF, TXT, MD, and CSV."
    )

    unknown = capabilities._classify_attachment(
        {"file_type": "video", "mime_type": "video/mp4", "file_name": "e.mp4"}
    )
    assert unknown.required_modality is None
    assert unknown.unsupported is True
    assert unknown.warning == "Unsupported attachment type. Supported types are image and document."


def test_refresh_anthropic_models_parses_capability_block(monkeypatch):
    _set_model_cache(monkeypatch, {})
    monkeypatch.setattr(
        capabilities,
        "_live_model_cache",
        {},
    )

    # NB: the output ceiling is `max_tokens` on Anthropic's model object (the
    # same name as the request parameter it bounds). `max_output_tokens` does
    # not exist in the API; this fixture used to carry that phantom name, which
    # is part of how the real parser went two and a half weeks reading a field
    # that was always absent. See test_refresh_anthropic_models_reads_max_tokens.
    sample_response = {
        "data": [
            {
                "id": "claude-opus-4-7",
                "display_name": "Claude Opus 4.7",
                "max_input_tokens": 200000,
                "max_tokens": 8192,
                "capabilities": {
                    "image_input": {"supported": True},
                    "pdf_input": {"supported": True},
                },
            },
            {
                "id": "claude-haiku-3-5",
                "display_name": "Claude Haiku 3.5",
                "max_input_tokens": 200000,
                "capabilities": {
                    "image_input": {"supported": True},
                    "pdf_input": {"supported": False},
                },
            },
        ]
    }

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        assert url.endswith("/v1/models")
        assert headers["x-api-key"] == "sk-test"
        return _FakeResponse(sample_response)

    monkeypatch.setattr(capabilities.httpx, "get", fake_get)

    count = capabilities.refresh_anthropic_models(
        base_url="https://api.anthropic.com",
        api_key="sk-test",
    )

    assert count == 2
    opus = capabilities._lookup_model("claude-opus-4-7")
    assert opus is not None
    assert "image" in opus.input_modalities
    assert "file" in opus.input_modalities
    assert opus.context_length == 200000
    assert opus.max_completion_tokens == 8192
    haiku = capabilities._lookup_model("claude-haiku-3-5")
    assert haiku is not None
    assert "image" in haiku.input_modalities
    assert "file" not in haiku.input_modalities
    # Absent on the wire stays None rather than becoming a bogus 0.
    assert haiku.max_completion_tokens is None


def _fake_models_response(monkeypatch, payload):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    monkeypatch.setattr(
        capabilities.httpx, "get", lambda url, headers=None, timeout=None: _FakeResponse()
    )


def test_refresh_anthropic_models_reads_max_tokens_not_max_output_tokens(monkeypatch):
    # Anthropic's model object names the output ceiling `max_tokens`. There is
    # no `max_output_tokens` field in the API, so reading that name resolved to
    # None for every model and this tier silently learned nothing (the
    # fallthrough to the catalog tier hid it) until a model absent from the
    # catalog (claude-sonnet-5) fell all the way through to langchain's 4096.
    _set_model_cache(monkeypatch, {})
    monkeypatch.setattr(capabilities, "_live_model_cache", {})
    _fake_models_response(
        monkeypatch,
        {
            "data": [
                {
                    "id": "claude-sonnet-5",
                    "display_name": "Claude Sonnet 5",
                    "max_input_tokens": 1000000,
                    "max_tokens": 128000,
                }
            ]
        },
    )

    assert capabilities.refresh_anthropic_models(
        base_url="https://api.anthropic.com", api_key="sk-test"
    ) == 1
    info = capabilities._lookup_model("claude-sonnet-5")
    assert info is not None
    assert info.max_completion_tokens == 128000


def test_refresh_anthropic_models_accepts_max_output_tokens_alias(monkeypatch):
    # Retained only for Anthropic-compatible gateways that invent the
    # friendlier name; the real API never sends it.
    _set_model_cache(monkeypatch, {})
    monkeypatch.setattr(capabilities, "_live_model_cache", {})
    _fake_models_response(
        monkeypatch,
        {"data": [{"id": "gateway-model", "max_output_tokens": 4242}]},
    )

    capabilities.refresh_anthropic_models(
        base_url="https://gateway.example", api_key="sk-test"
    )
    info = capabilities._lookup_model("gateway-model")
    assert info is not None
    assert info.max_completion_tokens == 4242


def _clear_probe_cache(monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "_probe_cache", {})
    monkeypatch.setattr(capabilities, "_probe_cache_ts", {})


def _fake_probe_post(monkeypatch, status_code, payload, calls=None):
    class _FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

        def json(self):
            if not isinstance(payload, dict):
                raise ValueError("not json")
            return payload

    def fake_post(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append(json)
        return _FakeResponse()

    monkeypatch.setattr(capabilities.httpx, "post", fake_post)


def test_probe_max_output_tokens_parses_ceiling_from_rejection(monkeypatch):
    # The provider is the only source that knows a model it has just shipped.
    # An over-limit request is refused BEFORE inference, and the refusal names
    # the real ceiling. This is the only tier that works through a gateway
    # serving no capability metadata at all (CLIProxy).
    _clear_probe_cache(monkeypatch)
    calls: list = []
    _fake_probe_post(
        monkeypatch,
        400,
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "max_tokens: 99999999 > 128000, which is the maximum "
                    "allowed number of output tokens for claude-sonnet-5"
                ),
            },
        },
        calls=calls,
    )

    assert capabilities.probe_max_output_tokens(
        "claude-sonnet-5", base_url="http://proxy:8317", api_key="k"
    ) == 128000
    # The probe must deliberately overshoot so the request is ALWAYS refused:
    # it learns the ceiling from the rejection, so a value a real model could
    # accept returns a completion instead of an answer, and the probe silently
    # stops working. Assert the PROPERTY, not equality with the constant:
    # comparing to `_PROBE_REQUEST_MAX_TOKENS` is a tautology that holds for any
    # value the constant might be changed to, including 100 (verified: setting
    # it to 100 broke the probe and this assertion still passed).
    assert calls[0]["max_tokens"] >= 1_000_000, (
        "the probe must overshoot any plausible real ceiling, or it will be "
        f"answered instead of refused (sent {calls[0]['max_tokens']})"
    )


def test_probe_max_output_tokens_caches_and_does_not_refetch(monkeypatch):
    _clear_probe_cache(monkeypatch)
    calls: list = []
    _fake_probe_post(
        monkeypatch,
        400,
        {"error": {"message": "max_tokens: 99999999 > 64000, which is the maximum allowed number of output tokens for m"}},
        calls=calls,
    )

    for _ in range(3):
        assert capabilities.probe_max_output_tokens(
            "m", base_url="http://proxy:8317", api_key="k"
        ) == 64000
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status_code,payload",
    [
        (200, {"content": [{"type": "text", "text": "hi"}]}),   # accepted, no ceiling
        (500, {"error": {"message": "upstream exploded"}}),      # server error
        (401, {"error": {"message": "invalid api key"}}),        # auth failure
        (400, {"error": {"message": "some reworded validation error"}}),
        (400, "not json at all"),
    ],
)
def test_probe_max_output_tokens_fails_soft(monkeypatch, status_code, payload):
    # Only a 4xx carrying the documented message is trusted. Everything else
    # resolves to None so the caller falls through to the next tier, rather
    # than to a confidently wrong number.
    _clear_probe_cache(monkeypatch)
    _fake_probe_post(monkeypatch, status_code, payload)
    assert capabilities.probe_max_output_tokens(
        "m", base_url="http://proxy:8317", api_key="k"
    ) is None


def test_probe_max_output_tokens_survives_network_failure(monkeypatch):
    _clear_probe_cache(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(capabilities.httpx, "post", boom)
    assert capabilities.probe_max_output_tokens(
        "m", base_url="http://proxy:8317", api_key="k"
    ) is None


def test_probe_max_output_tokens_noop_without_credentials(monkeypatch):
    _clear_probe_cache(monkeypatch)
    calls: list = []
    # Counted, never raised: probe_max_output_tokens wraps the request in
    # `except Exception`, and AssertionError is an Exception, so a raising fake
    # is swallowed and the test would pass even with the gate deleted.
    monkeypatch.setattr(
        capabilities.httpx, "post", lambda *a, **k: calls.append(a) or _unreachable()
    )
    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="") is None
    assert capabilities.probe_max_output_tokens("m", base_url="", api_key="k") is None
    assert capabilities.probe_max_output_tokens("", base_url="http://p", api_key="k") is None
    assert calls == []


def _unreachable():  # pragma: no cover - only runs if a gate regresses
    raise RuntimeError("unreachable")


def test_probe_trusts_only_a_4xx_even_when_the_message_matches(monkeypatch):
    """Isolates the status gate.

    Every other fails-soft case is rejected by the regex, so none of them prove
    the status check does anything. This one carries a perfectly parseable
    ceiling on a 500: a server error is not a validation verdict, and treating
    it as one would cache a number nobody asserted.
    """
    _clear_probe_cache(monkeypatch)
    _fake_probe_post(
        monkeypatch,
        500,
        {
            "error": {
                "message": (
                    "max_tokens: 99999999 > 128000, which is the maximum "
                    "allowed number of output tokens for m"
                )
            }
        },
    )
    assert (
        capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k")
        is None
    )


def test_probe_caches_negatives_so_a_blind_gateway_is_asked_once(monkeypatch):
    """The negative cache is what bounds live construction-time I/O.

    The probe runs from _create_anthropic_llm, so an endpoint that does not
    speak this dialect would otherwise cost a fresh 8s-timeout round trip on
    EVERY LLM construction, in production and in CI alike.
    """
    _clear_probe_cache(monkeypatch)
    calls: list = []
    _fake_probe_post(monkeypatch, 200, {"content": []}, calls=calls)

    for _ in range(3):
        assert (
            capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k")
            is None
        )
    assert len(calls) == 1


def test_probe_negative_cache_expires_sooner_than_a_positive_one(monkeypatch):
    """A blind gateway may start answering; a known ceiling will not change.

    Hence the split TTL. Both halves are pinned here because nothing else
    advances the clock, and a TTL that silently became infinite (or zero) would
    look identical to a working cache in every other test.
    """
    _clear_probe_cache(monkeypatch)
    calls: list = []
    _fake_probe_post(monkeypatch, 200, {"content": []}, calls=calls)
    now = [1_000_000.0]
    monkeypatch.setattr(capabilities.time, "time", lambda: now[0])

    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k") is None
    assert len(calls) == 1

    now[0] += capabilities._PROBE_FAILURE_TTL_SECONDS - 1
    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k") is None
    assert len(calls) == 1  # still inside the negative TTL

    now[0] += 2
    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k") is None
    assert len(calls) == 2  # negative TTL expired: re-probed


def test_probe_positive_cache_survives_the_negative_ttl(monkeypatch):
    _clear_probe_cache(monkeypatch)
    calls: list = []
    _fake_probe_post(
        monkeypatch,
        400,
        {"error": {"message": "max_tokens: 99999999 > 64000, which is the maximum allowed number of output tokens for m"}},
        calls=calls,
    )
    now = [1_000_000.0]
    monkeypatch.setattr(capabilities.time, "time", lambda: now[0])

    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k") == 64000
    now[0] += capabilities._PROBE_FAILURE_TTL_SECONDS + 1
    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k") == 64000
    assert len(calls) == 1  # a real ceiling is held for the long TTL

    now[0] += capabilities._PROBE_TTL_SECONDS
    assert capabilities.probe_max_output_tokens("m", base_url="http://p", api_key="k") == 64000
    assert len(calls) == 2  # positive TTL expired: re-probed


def test_refresh_anthropic_models_falls_back_when_capabilities_absent(monkeypatch):
    # CLIProxy (and some Anthropic-compatible gateways) return /v1/models with
    # no capabilities block. Without a fallback this marks every Claude model
    # text-only in the authoritative live cache, silently disabling all image
    # input. Known vision models must still resolve as image/file capable, and
    # an unknown model resolves OPTIMISTICALLY (vision + document) so a brand-new
    # multimodal slug is not marked vision-blind. A non-visual model TYPE stays
    # text-only.
    _set_model_cache(monkeypatch, {})
    monkeypatch.setattr(capabilities, "_live_model_cache", {})

    sample_response = {
        "data": [
            {
                "id": "claude-opus-4-6",
                "display_name": "Claude Opus 4.6",
                "max_input_tokens": 200000,
            },
            {
                "id": "some-unknown-model-xyz",
                "display_name": "Unknown",
                "max_input_tokens": 8000,
            },
            {
                "id": "text-embedding-3-large",
                "display_name": "Embed",
                "max_input_tokens": 8000,
            },
        ]
    }

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return sample_response

    monkeypatch.setattr(capabilities.httpx, "get", lambda *a, **k: _FakeResponse())

    count = capabilities.refresh_anthropic_models(
        base_url="http://cli-proxy-api:8317",
        api_key="sk-test",
    )

    assert count == 3
    opus = capabilities._lookup_model("claude-opus-4-6")
    assert opus is not None
    assert "image" in opus.input_modalities  # static fallback applied
    assert "file" in opus.input_modalities
    assert capabilities.supports_vision("claude-opus-4-6") is True
    # Unknown slug -> optimistic vision + document in the authoritative cache.
    unknown = capabilities._lookup_model("some-unknown-model-xyz")
    assert unknown is not None
    assert unknown.input_modalities == {"text", "image", "file"}
    # Non-visual model TYPE stays text-only despite the optimistic default.
    embed = capabilities._lookup_model("text-embedding-3-large")
    assert embed is not None
    assert embed.input_modalities == {"text"}


def test_refresh_anthropic_models_noop_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = {"count": 0}

    def fake_get(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("HTTP should not be called without an API key")

    monkeypatch.setattr(capabilities.httpx, "get", fake_get)
    assert capabilities.refresh_anthropic_models() == 0
    assert called["count"] == 0


def test_concurrent_cache_misses_share_one_openrouter_fetch(monkeypatch):
    _clear_model_cache(monkeypatch)
    entered_fetch = threading.Event()
    release_fetch = threading.Event()
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        entered_fetch.set()
        assert release_fetch.wait(timeout=2)
        return {
            "provider/test-model": ModelInfo(
                id="provider/test-model",
                context_length=64000,
            )
        }

    monkeypatch.setattr(capabilities, "_fetch_openrouter_models", fake_fetch)

    results = []

    def worker():
        results.append(capabilities._ensure_cache())

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert entered_fetch.wait(timeout=2)
    second.start()
    release_fetch.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == 1
    assert len(results) == 2
    assert all("provider/test-model" in result for result in results)


# ---------------------------------------------------------------------------
# register_model_metadata merge semantics (F3: dataclasses.replace refactor)
# ---------------------------------------------------------------------------


def _fresh_caches(monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "_model_cache", {})
    monkeypatch.setattr(capabilities, "_live_model_cache", {})


def test_register_new_model_records_id_name_and_limits(monkeypatch):
    _fresh_caches(monkeypatch)

    capabilities.register_model_metadata(
        model_id="acme/widget-1",
        name="Widget One",
        context_length=64000,
        max_completion_tokens=4096,
    )

    info = capabilities._model_cache["acme/widget-1"]
    assert info.id == "acme/widget-1"
    assert info.name == "Widget One"
    assert info.context_length == 64000
    assert info.max_completion_tokens == 4096
    # The same object is mirrored into the live cache.
    assert capabilities._live_model_cache["acme/widget-1"] is info


def test_register_empty_name_falls_back_to_model_id(monkeypatch):
    _fresh_caches(monkeypatch)

    capabilities.register_model_metadata(model_id="acme/widget-2", context_length=8000)

    assert capabilities._model_cache["acme/widget-2"].name == "acme/widget-2"


def test_register_preserves_existing_fields_when_not_provided(monkeypatch):
    # The silent-data-loss guard: a partial update must not wipe prior metadata.
    _fresh_caches(monkeypatch)
    monkeypatch.setattr(
        capabilities,
        "_model_cache",
        {
            "acme/widget-3": ModelInfo(
                id="acme/widget-3",
                name="Widget Three",
                context_length=200000,
                input_modalities={"text", "image"},
                tokenizer="GPT",
            )
        },
    )

    capabilities.register_model_metadata(
        model_id="acme/widget-3",
        max_completion_tokens=8192,
    )

    info = capabilities._model_cache["acme/widget-3"]
    assert info.max_completion_tokens == 8192          # newly merged
    assert info.context_length == 200000               # preserved
    assert info.name == "Widget Three"                 # preserved
    assert info.input_modalities == {"text", "image"}  # preserved
    assert info.tokenizer == "GPT"                      # preserved


def test_register_zero_numeric_is_treated_as_absent(monkeypatch):
    _fresh_caches(monkeypatch)
    monkeypatch.setattr(
        capabilities,
        "_model_cache",
        {"acme/widget-4": ModelInfo(id="acme/widget-4", context_length=128000)},
    )

    capabilities.register_model_metadata(model_id="acme/widget-4", context_length=0)

    # 0 means "no provider number reported"; the prior value must survive.
    assert capabilities._model_cache["acme/widget-4"].context_length == 128000


def test_register_does_not_alias_caller_or_prior_set(monkeypatch):
    _fresh_caches(monkeypatch)

    caller_set = {"text"}
    capabilities.register_model_metadata(
        model_id="acme/widget-5",
        input_modalities=caller_set,
    )
    first = capabilities._model_cache["acme/widget-5"]
    # Mutating the caller's set after the call must not leak into the cache.
    caller_set.add("image")
    assert first.input_modalities == {"text"}

    # A follow-up partial update keeps the modalities, as an independent copy.
    capabilities.register_model_metadata(model_id="acme/widget-5", tokenizer="GPT")
    second = capabilities._model_cache["acme/widget-5"]
    assert second.input_modalities == {"text"}
    assert second.input_modalities is not first.input_modalities


def test_register_ignores_blank_model_id(monkeypatch):
    _fresh_caches(monkeypatch)
    capabilities.register_model_metadata(model_id="")
    assert capabilities._model_cache == {}


def test_get_context_limit_prefers_longest_static_match(monkeypatch):
    # F5: the precomputed length-desc list must keep longest-match-first so the
    # more specific "claude-opus-4-8" (1M) wins over "claude-opus-4" (200k).
    _clear_model_cache(monkeypatch)
    assert capabilities.get_context_limit("my-claude-opus-4-8-preview") == 1000000
    assert capabilities.get_context_limit("my-claude-opus-4-preview") == 200000


# ============================================================================
# F7: vision/document capability-set equivalence + invariants
# ============================================================================
#
# The six per-provider sets plus the two unions are derived from one canonical
# per-provider capability table (vision/document flags) via a separator-aware
# id-form expander. These tests lock that derivation to the exact membership it
# replaced and guard the structural invariants the expander relies on.

# Golden snapshot captured from the pre-F7 literal sets (sorted lists). If a new
# model is added to the capability table, this snapshot must be updated in the
# same change: that deliberate step is the anti-drift guard the F7 finding wants.
_F7_GOLDEN = json.loads(
    (Path(__file__).parent / "_capability_models_f7_golden.json").read_text()
)

_CAPABILITY_SET_NAMES = (
    "ANTHROPIC_VISION_CAPABLE_MODELS",
    "ANTHROPIC_DOCUMENT_CAPABLE_MODELS",
    "OPENAI_VISION_CAPABLE_MODELS",
    "OPENAI_DOCUMENT_CAPABLE_MODELS",
    "GEMINI_VISION_CAPABLE_MODELS",
    "GEMINI_DOCUMENT_CAPABLE_MODELS",
    "VISION_CAPABLE_MODELS",
    "DOCUMENT_CAPABLE_MODELS",
)


def test_f7_golden_snapshot_covers_every_capability_set():
    # Tripwire: the snapshot file enumerates exactly the eight derived sets, so a
    # renamed/added/removed set cannot silently skip the equivalence check below.
    assert set(_F7_GOLDEN) == set(_CAPABILITY_SET_NAMES)


@pytest.mark.parametrize("name", _CAPABILITY_SET_NAMES)
def test_f7_capability_set_matches_golden(name):
    # The derived set must equal the exact pre-refactor membership, byte-for-byte.
    assert sorted(getattr(capabilities, name)) == _F7_GOLDEN[name]


@pytest.mark.parametrize(
    "vision, document",
    [
        ("ANTHROPIC_VISION_CAPABLE_MODELS", "ANTHROPIC_DOCUMENT_CAPABLE_MODELS"),
        ("OPENAI_VISION_CAPABLE_MODELS", "OPENAI_DOCUMENT_CAPABLE_MODELS"),
        ("GEMINI_VISION_CAPABLE_MODELS", "GEMINI_DOCUMENT_CAPABLE_MODELS"),
        ("VISION_CAPABLE_MODELS", "DOCUMENT_CAPABLE_MODELS"),
    ],
)
def test_f7_document_is_subset_of_vision(vision, document):
    # The expander assumes document-capable implies vision-capable for every
    # provider; a future model that breaks this must be added consciously.
    assert getattr(capabilities, document) <= getattr(capabilities, vision)


def test_f7_anthropic_models_carry_both_id_forms():
    # The fallback matcher is dot/hyphen separator-sensitive, so every Anthropic
    # model (except the provider-form-only legacy entry) must appear as BOTH the
    # "anthropic/<dotted>" form and the hyphenated bare form. Stated as one exact
    # set equality: the bare forms are precisely the hyphenated derivations of the
    # provider forms minus the provider-form-only entry. This catches a missing
    # bare, an orphan bare, and a stray bare for the fast entry, all at once.
    for model_set in (
        capabilities.ANTHROPIC_VISION_CAPABLE_MODELS,
        capabilities.ANTHROPIC_DOCUMENT_CAPABLE_MODELS,
    ):
        provider_forms = {e for e in model_set if e.startswith("anthropic/")}
        bare_forms = {e for e in model_set if not e.startswith("anthropic/")}
        expected_bare = {
            pf.split("/", 1)[1].replace(".", "-")
            for pf in provider_forms
            if pf != "anthropic/claude-opus-4.6-fast"
        }
        assert bare_forms == expected_bare


def test_f7_fast_irregularity_is_preserved():
    # claude-opus-4.6-fast is vision-only and provider-form-only today (a likely
    # drift flagged in 24-config F7). Lock both asymmetries so a future "fix" is a
    # deliberate edit, not an accident of the refactor.
    assert "anthropic/claude-opus-4.6-fast" in capabilities.ANTHROPIC_VISION_CAPABLE_MODELS
    assert "anthropic/claude-opus-4.6-fast" not in capabilities.ANTHROPIC_DOCUMENT_CAPABLE_MODELS
    assert "claude-opus-4-6-fast" not in capabilities.ANTHROPIC_VISION_CAPABLE_MODELS
    assert "claude-opus-4-6-fast" not in capabilities.ANTHROPIC_DOCUMENT_CAPABLE_MODELS


@pytest.mark.parametrize(
    "set_name, prefix",
    [
        ("OPENAI_VISION_CAPABLE_MODELS", "openai/"),
        ("OPENAI_DOCUMENT_CAPABLE_MODELS", "openai/"),
        ("GEMINI_VISION_CAPABLE_MODELS", "google/"),
        ("GEMINI_DOCUMENT_CAPABLE_MODELS", "google/"),
    ],
)
def test_f7_openai_and_gemini_are_provider_form_only(set_name, prefix):
    # OpenAI/Gemini ids carry only the "provider/<dotted>" form; the matcher
    # re-derives the bare/auto-prefixed variants, so no bare form is stored.
    for entry in getattr(capabilities, set_name):
        assert entry.startswith(prefix), entry


@pytest.mark.parametrize(
    "vision_count, document_count, set_pair",
    [
        (33, 32, ("ANTHROPIC_VISION_CAPABLE_MODELS", "ANTHROPIC_DOCUMENT_CAPABLE_MODELS")),
        (31, 24, ("OPENAI_VISION_CAPABLE_MODELS", "OPENAI_DOCUMENT_CAPABLE_MODELS")),
        (18, 15, ("GEMINI_VISION_CAPABLE_MODELS", "GEMINI_DOCUMENT_CAPABLE_MODELS")),
        (84, 71, ("VISION_CAPABLE_MODELS", "DOCUMENT_CAPABLE_MODELS")),
    ],
)
def test_f7_capability_set_counts(vision_count, document_count, set_pair):
    # Count tripwires on the expanded sets: an accidental add/drop fails loudly.
    vision_name, document_name = set_pair
    assert len(getattr(capabilities, vision_name)) == vision_count
    assert len(getattr(capabilities, document_name)) == document_count


def test_f7_behavioral_spot_checks(monkeypatch):
    # End-to-end through supports_vision/supports_documents with the static
    # fallback (empty live cache), one model per capability category.
    _clear_model_cache(monkeypatch)

    # vision+document model, all three id-forms.
    for model in ("claude-opus-4-8", "claude-opus-4.8", "anthropic/claude-opus-4.8"):
        assert capabilities.supports_vision(model), model
        assert capabilities.supports_documents(model), model

    # OpenAI vision-only (codex): vision yes, document no.
    assert capabilities.supports_vision("gpt-5.2-codex")
    assert not capabilities.supports_documents("gpt-5.2-codex")

    # Gemini vision-only image model: vision yes, document no.
    assert capabilities.supports_vision("google/gemini-2.5-flash-image")
    assert not capabilities.supports_documents("google/gemini-2.5-flash-image")

    # meta-llama vision extra: vision yes, document no.
    assert capabilities.supports_vision("meta-llama/llama-3.2-90b-vision-instruct")
    assert not capabilities.supports_documents("meta-llama/llama-3.2-90b-vision-instruct")

    # Unlisted normal model: now OPTIMISTICALLY vision + document (a
    # capability-blind gateway reports no modalities, so the default must not be
    # blind). A non-visual model TYPE still resolves to neither.
    assert capabilities.supports_vision("acme/not-a-real-model")
    assert capabilities.supports_documents("acme/not-a-real-model")
    assert not capabilities.supports_vision("acme/fake-embed-9")
    assert not capabilities.supports_documents("acme/fake-embed-9")


# ============================================================================
# Bundled-catalog tier (backlog #61): live caches > catalog > static tables
# for context limits; curated tables > catalog for vision/document flags.
# ============================================================================


def _set_catalog(monkeypatch, entries: dict) -> None:
    """Point the LiteLLM catalog cache at synthetic entries for one test."""
    monkeypatch.setattr(_pricing_table, "_litellm_cache", entries)
    monkeypatch.setattr(_pricing_table, "_loaded_from_bundle", True)


def _offline(monkeypatch) -> None:
    """Empty-but-populated live caches: no OpenRouter fetch, no live data."""
    _set_model_cache(monkeypatch, {})
    monkeypatch.setattr(capabilities, "_live_model_cache", {})
    monkeypatch.setattr(capabilities, "_input_unsupported_marks", {})


def test_context_limit_resolves_from_catalog_when_live_metadata_missing(monkeypatch):
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"future-model-q": {"max_input_tokens": 424242}})

    assert capabilities.get_context_limit("future-model-q") == 424242
    # CLIProxy-style effort suffixes are stripped before the catalog lookup.
    assert capabilities.get_context_limit("future-model-q(xhigh)") == 424242


def test_context_limit_exact_static_match_beats_catalog(monkeypatch):
    # The curated table stores TOTAL context windows; the catalog's
    # max_input_tokens is an input-only budget for some providers. An exact
    # curated match must win over a divergent catalog value.
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"gpt-5.5": {"max_input_tokens": 999000}})

    assert capabilities.get_context_limit("gpt-5.5") == 1050000


def test_context_limit_catalog_beats_substring_fallback(monkeypatch):
    # "custom-gpt-5.5-variant" has no exact curated entry; the substring
    # fallback would fuzzily map it onto gpt-5.5 (1050000). Exact catalog
    # data for the actual id must win over that guess.
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"custom-gpt-5.5-variant": {"max_input_tokens": 55555}})

    assert capabilities.get_context_limit("custom-gpt-5.5-variant") == 55555


def test_context_limit_live_metadata_beats_catalog(monkeypatch):
    _set_model_cache(
        monkeypatch,
        {"openai/gpt-future": ModelInfo(id="openai/gpt-future", context_length=777777)},
    )
    monkeypatch.setattr(capabilities, "_live_model_cache", {})
    _set_catalog(monkeypatch, {"gpt-future": {"max_input_tokens": 111111}})

    assert capabilities.get_context_limit("gpt-future") == 777777


def test_context_limit_static_table_still_covers_catalog_misses(monkeypatch):
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {})

    assert capabilities.get_context_limit("claude-opus-4-8") == 1000000
    assert (
        capabilities.get_context_limit("totally-unknown-model")
        == capabilities.DEFAULT_CONTEXT_LIMITS["_default"]
    )


def test_context_limit_real_bundle_resolves_uncurated_model_offline(monkeypatch):
    # claude-haiku-4-5 is absent from DEFAULT_CONTEXT_LIMITS; before the
    # catalog tier it degraded to the 128k default whenever both live caches
    # were cold. The real bundled snapshot must resolve it offline. The
    # expected value comes from the catalog itself so a future bundle refresh
    # cannot rot this test.
    _offline(monkeypatch)

    assert "claude-haiku-4-5" not in capabilities.DEFAULT_CONTEXT_LIMITS
    hints = _pricing_table.get_catalog_capabilities("", "claude-haiku-4-5")
    assert hints is not None and hints.max_input_tokens
    assert capabilities.get_context_limit("claude-haiku-4-5") == hints.max_input_tokens
    assert (
        capabilities.get_context_limit("claude-haiku-4-5")
        != capabilities.DEFAULT_CONTEXT_LIMITS["_default"]
    )


def test_supports_vision_from_catalog_for_uncurated_model(monkeypatch):
    _offline(monkeypatch)
    _set_catalog(
        monkeypatch,
        {
            "sighted-model": {"supports_vision": True},
            "blind-model": {"supports_vision": False},
        },
    )

    assert capabilities.supports_vision("sighted-model") is True
    assert capabilities.supports_vision("blind-model") is False


def test_supports_documents_from_catalog_for_uncurated_model(monkeypatch):
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"filing-model": {"supports_pdf_input": True}})

    assert capabilities.supports_documents("filing-model") is True


def test_curated_vision_only_verdict_beats_catalog_pdf_flag(monkeypatch):
    # The real bundle claims supports_pdf_input for the codex family, but the
    # curated tables deliberately classify codex as vision-only. The curated
    # verdict must win in both directions for any model the tables know.
    _offline(monkeypatch)

    assert capabilities.supports_vision("gpt-5.2-codex") is True
    assert capabilities.supports_documents("gpt-5.2-codex") is False


def test_catalog_absent_vision_flag_falls_through_to_optimistic_default(monkeypatch):
    _offline(monkeypatch)
    # Catalog knows the context window but carries no vision flag; the model is
    # uncurated and not a non-visual type, so vision resolves to the OPTIMISTIC
    # default (True), not a blanket False. The context limit still comes from
    # the catalog.
    _set_catalog(monkeypatch, {"opaque-model": {"max_input_tokens": 32000}})

    assert capabilities.supports_vision("opaque-model") is True
    assert capabilities.get_context_limit("opaque-model") == 32000


def test_uncurated_modern_models_default_optimistic(monkeypatch):
    # A capability-blind gateway (CLIProxy) reports no modalities and a brand-new
    # slug is in neither the curated tables nor the offline catalog. The
    # optimistic terminal default keeps such models vision + document capable so
    # image input is not silently stripped (the claude-sonnet-5 non-vision bug).
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {})

    for model in (
        "claude-sonnet-5",
        "claude-opus-5",
        "anthropic/claude-sonnet-5",
        "gpt-5.9",
        "google/gemini-9-pro",
    ):
        assert capabilities.supports_vision(model) is True, model
        assert capabilities.supports_documents(model) is True, model


def test_non_visual_model_types_stay_non_vision(monkeypatch):
    # Isolates the marker tier: with the catalog forced EMPTY, the optimistic
    # terminal default excludes model TYPES that do not take image input. NOTE
    # the real LiteLLM catalog outranks this guard, so a catalogued image/tts
    # model (e.g. gpt-image-2) may still resolve vision=True in production via
    # its catalog verdict; the marker only decides when the catalog is silent.
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {})

    for model in (
        "text-embedding-3-large",
        "openai/text-embedding-ada-002",
        "whisper-1",
        "tts-1-hd",
        "dall-e-3",
        "gpt-image-2",
        "black-forest-labs/flux-1.1-pro",
        "some/reranker-v2",
    ):
        assert capabilities.supports_vision(model) is False, model
        assert capabilities.supports_documents(model) is False, model


def test_catalog_negative_vision_flag_still_denies_uncurated_model(monkeypatch):
    # A model the catalog positively marks text-only stays non-vision: the
    # catalog verdict wins one tier above the optimistic terminal default.
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"legacy-chat-model": {"supports_vision": False}})

    assert capabilities.supports_vision("legacy-chat-model") is False


def test_documents_follow_vision_at_optimistic_default(monkeypatch):
    # Document is a subset of vision, so the optimistic terminal default must
    # NOT grant documents to a model that resolves non-vision. A catalog model
    # with a definite supports_vision=False and no pdf flag resolves BOTH axes
    # False (regression guard: the documents fallback defers to the vision
    # verdict, not the marker guard independently).
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"text-only-chat": {"supports_vision": False}})

    assert capabilities.supports_vision("text-only-chat") is False
    assert capabilities.supports_documents("text-only-chat") is False


def test_max_output_tokens_from_catalog_with_safety_cap(monkeypatch):
    _offline(monkeypatch)
    _set_catalog(
        monkeypatch,
        {
            "chatty-model": {"max_input_tokens": 10000, "max_output_tokens": 9000},
            "modest-model": {"max_input_tokens": 100000, "max_output_tokens": 8000},
        },
    )

    # 9000 exceeds 50% of the 10000-token context; capped to 5000.
    assert capabilities.get_max_output_tokens("chatty-model") == 5000
    assert capabilities.get_max_output_tokens("modest-model") == 8000
    assert capabilities.get_max_output_tokens("absent-model") is None


def test_anthropic_gateway_fallback_uses_catalog_for_uncurated_model(monkeypatch):
    # A capabilities-less gateway response for a model the curated tables do
    # not know: the catalog supplies the modality bits for the live cache.
    _offline(monkeypatch)
    _set_catalog(
        monkeypatch,
        {"claude-newline-9": {"supports_vision": True, "supports_pdf_input": True}},
    )

    sample_response = {
        "data": [
            {
                "id": "claude-newline-9",
                "display_name": "Claude Newline 9",
                "max_input_tokens": 500000,
            }
        ]
    }

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return sample_response

    monkeypatch.setattr(capabilities.httpx, "get", lambda *a, **k: _FakeResponse())

    count = capabilities.refresh_anthropic_models(
        base_url="http://cli-proxy-api:8317",
        api_key="sk-test",
    )

    assert count == 1
    info = capabilities._lookup_model("claude-newline-9")
    assert info is not None
    assert "image" in info.input_modalities
    assert "file" in info.input_modalities


def test_mark_model_input_unsupported_drops_image_and_file(monkeypatch):
    # A model the live cache believes is fully multimodal (the optimistic
    # default, or a real capabilities response) is corrected in place when the
    # provider rejects the attachment: image/file go, text stays.
    _offline(monkeypatch)
    capabilities._live_model_cache["some-model"] = ModelInfo(
        id="some-model", input_modalities={"text", "image", "file"}
    )

    capabilities.mark_model_input_unsupported("some-model", "image", "file")

    info = capabilities.get_model_info("some-model")
    assert info is not None
    assert info.input_modalities == {"text"}
    assert capabilities.supports_vision("some-model") is False
    assert capabilities.supports_documents("some-model") is False


def test_mark_model_input_unsupported_creates_text_only_when_absent(monkeypatch):
    # An uncached model (the common CLIProxy case) gets a fresh text-only entry
    # so the optimistic default stops firing for it.
    _offline(monkeypatch)

    capabilities.mark_model_input_unsupported("brand-new-model")  # defaults to image+file

    info = capabilities.get_model_info("brand-new-model")
    assert info is not None
    assert info.input_modalities == {"text"}
    assert capabilities.supports_vision("brand-new-model") is False


def test_mark_model_input_unsupported_preserves_other_modalities(monkeypatch):
    # Dropping only "image" leaves "file" intact (a hypothetical image-blind but
    # PDF-capable model).
    _offline(monkeypatch)
    capabilities._live_model_cache["doc-only-model"] = ModelInfo(
        id="doc-only-model", input_modalities={"text", "image", "file"}
    )

    capabilities.mark_model_input_unsupported("doc-only-model", "image")

    info = capabilities.get_model_info("doc-only-model")
    assert info is not None
    assert info.input_modalities == {"text", "file"}


def test_mark_model_input_unsupported_ignores_empty_and_text_only(monkeypatch):
    # No-op guards: empty id, and a request to drop only "text".
    _offline(monkeypatch)
    capabilities._live_model_cache["keep-model"] = ModelInfo(
        id="keep-model", input_modalities={"text", "image"}
    )

    capabilities.mark_model_input_unsupported("")
    capabilities.mark_model_input_unsupported("keep-model", "text")

    info = capabilities.get_model_info("keep-model")
    assert info is not None
    assert info.input_modalities == {"text", "image"}


def test_mark_model_input_unsupported_absent_selective_drop_keeps_others(monkeypatch):
    # Dropping only "image" on an UNCACHED model must not also strip file/doc:
    # the fresh entry seeds from the optimistic default minus only what was named.
    _offline(monkeypatch)

    capabilities.mark_model_input_unsupported("uncached-doc-model", "image")

    info = capabilities.get_model_info("uncached-doc-model")
    assert info is not None
    assert info.input_modalities == {"text", "file"}
    assert capabilities.supports_vision("uncached-doc-model") is False
    assert capabilities.supports_documents("uncached-doc-model") is True


def test_mark_model_input_unsupported_canonicalizes_key(monkeypatch):
    # The write key must match the read canonicalization (reasoning-suffix
    # stripped) so the learned verdict is actually resolvable and does not
    # re-mark forever under an unreachable key.
    _offline(monkeypatch)

    capabilities.mark_model_input_unsupported("gpt-suffix-5(xhigh)", "image", "file")

    # Both the suffixed and the bare form resolve to the same marked entry.
    assert capabilities.supports_vision("gpt-suffix-5(xhigh)") is False
    assert capabilities.supports_vision("gpt-suffix-5") is False
    assert "gpt-suffix-5" in capabilities._input_unsupported_marks


def test_mark_overrides_curated_and_catalog_vision_verdict(monkeypatch):
    # The learned live-cache entry must win the tiering over BOTH a curated
    # vision verdict and a catalog vision flag (this override is the whole point
    # of the self-correcting learning).
    _offline(monkeypatch)
    _set_catalog(monkeypatch, {"catalog-vision-model": {"supports_vision": True}})

    # Curated model (gpt-4o) and a catalog-vision model both start True.
    assert capabilities.supports_vision("gpt-4o") is True
    assert capabilities.supports_vision("catalog-vision-model") is True

    capabilities.mark_model_input_unsupported("gpt-4o", "image", "file")
    capabilities.mark_model_input_unsupported("catalog-vision-model", "image", "file")

    assert capabilities.supports_vision("gpt-4o") is False
    assert capabilities.supports_vision("catalog-vision-model") is False


def test_mark_model_input_unsupported_self_heals_after_ttl(monkeypatch):
    # A mark is a backstop, not a durable verdict: once its TTL lapses it is
    # evicted on the next lookup so a (rare) misclassified model re-derives its
    # real capability instead of staying blind for the whole process.
    _offline(monkeypatch)

    capabilities.mark_model_input_unsupported("gpt-4o", "image", "file")
    # Within the TTL the learned verdict holds.
    assert capabilities.supports_vision("gpt-4o") is False

    # Force the mark to be considered expired: the next lookup evicts it.
    monkeypatch.setattr(capabilities, "_INPUT_UNSUPPORTED_MARK_TTL_SECONDS", -1.0)
    assert capabilities.supports_vision("gpt-4o") is True
    assert "gpt-4o" not in capabilities._input_unsupported_marks


# ---------------------------------------------------------------------------
# The probe is blocking HTTP and its callers run on the API event loop.
# ---------------------------------------------------------------------------


class TestProbeNeverBlocksTheEventLoop:
    """The probe must never do HTTP on a running loop, but must still work.

    Both halves matter and they pull against each other. Blocking the loop
    stalls EVERY request in the process for up to the probe timeout; refusing to
    probe at all reinstates the 4096 cap the probe exists to discover past. The
    split (cache hit inline, miss warms on a worker thread) is what buys both,
    so both halves are pinned here.
    """

    def _rejecting_post(self, calls, loop_calls):
        import asyncio as _asyncio

        class _Rejected:
            status_code = 400
            text = ""

            @staticmethod
            def json():
                return {
                    "error": {
                        "message": (
                            "max_tokens: 99999999 > 4096000, which is the maximum "
                            "allowed number of output tokens for probe-model"
                        )
                    }
                }

        def _post(url, **kwargs):
            calls.append(url)
            try:
                _asyncio.get_running_loop()
                loop_calls.append(url)
            except RuntimeError:
                pass
            return _Rejected()

        return _post

    def test_off_loop_callers_probe_normally(self, monkeypatch):
        calls: list = []
        loop_calls: list = []
        monkeypatch.setattr(
            capabilities.httpx, "post", self._rejecting_post(calls, loop_calls)
        )
        capabilities._probe_cache.clear()
        capabilities._probe_cache_ts.clear()

        got = capabilities.probe_max_output_tokens(
            "probe-model", base_url="https://api.anthropic.com", api_key="k"
        )
        assert got == 4096000
        assert len(calls) == 1
        assert loop_calls == []

    def test_a_running_loop_gets_no_http_and_the_warm_lands_anyway(self, monkeypatch):
        """A miss on the loop: zero on-loop HTTP now, correct answer shortly.

        Asserting the background warm actually POPULATES the cache (not merely
        that the loop was spared) is the point: a version that skipped the probe
        entirely would also record zero on-loop HTTP, and would silently pin
        every Claude model to 4096 forever.
        """
        import asyncio as _asyncio

        calls: list = []
        loop_calls: list = []
        monkeypatch.setattr(
            capabilities.httpx, "post", self._rejecting_post(calls, loop_calls)
        )
        capabilities._probe_cache.clear()
        capabilities._probe_cache_ts.clear()

        async def _on_the_loop():
            first = capabilities.probe_max_output_tokens(
                "probe-model", base_url="https://api.anthropic.com", api_key="k"
            )
            for _ in range(100):
                await _asyncio.sleep(0.02)
                hit, cached = capabilities._probe_cached(
                    capabilities._probe_cache_key(
                        "probe-model", "https://api.anthropic.com"
                    )
                )
                if hit:
                    return first, cached
            return first, "warm never landed"

        first, warmed = _asyncio.run(_on_the_loop())

        assert first is None, "a cold miss on the loop must not block for an answer"
        assert loop_calls == [], f"probe HTTP ran on the event loop: {loop_calls}"
        assert warmed == 4096000, "the background warm must populate the cache"

    def test_a_warm_cache_answers_inline_even_on_the_loop(self, monkeypatch):
        """The steady state: a hit is pure memory, so the loop is never involved."""
        import asyncio as _asyncio

        calls: list = []
        loop_calls: list = []
        monkeypatch.setattr(
            capabilities.httpx, "post", self._rejecting_post(calls, loop_calls)
        )
        capabilities._probe_cache.clear()
        capabilities._probe_cache_ts.clear()
        capabilities.probe_max_output_tokens(
            "probe-model", base_url="https://api.anthropic.com", api_key="k"
        )
        assert len(calls) == 1

        async def _on_the_loop():
            return capabilities.probe_max_output_tokens(
                "probe-model", base_url="https://api.anthropic.com", api_key="k"
            )

        assert _asyncio.run(_on_the_loop()) == 4096000
        assert len(calls) == 1, "a warm hit must not re-issue HTTP"
        assert loop_calls == []
