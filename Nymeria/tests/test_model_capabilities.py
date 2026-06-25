"""Regression tests for model metadata fallback behavior."""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path

import pytest

from nymeria.config import model_capabilities as capabilities
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


def test_refresh_anthropic_models_parses_capability_block(monkeypatch):
    _set_model_cache(monkeypatch, {})
    monkeypatch.setattr(
        capabilities,
        "_live_model_cache",
        {},
    )

    sample_response = {
        "data": [
            {
                "id": "claude-opus-4-7",
                "display_name": "Claude Opus 4.7",
                "max_input_tokens": 200000,
                "max_output_tokens": 8192,
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
    haiku = capabilities._lookup_model("claude-haiku-3-5")
    assert haiku is not None
    assert "image" in haiku.input_modalities
    assert "file" not in haiku.input_modalities


def test_refresh_anthropic_models_falls_back_when_capabilities_absent(monkeypatch):
    # CLIProxy (and some Anthropic-compatible gateways) return /v1/models with
    # no capabilities block. Without a fallback this marks every Claude model
    # text-only in the authoritative live cache, silently disabling all image
    # input. Known vision models must still resolve as image/file capable.
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

    assert count == 2
    opus = capabilities._lookup_model("claude-opus-4-6")
    assert opus is not None
    assert "image" in opus.input_modalities  # static fallback applied
    assert "file" in opus.input_modalities
    assert capabilities.supports_vision("claude-opus-4-6") is True
    unknown = capabilities._lookup_model("some-unknown-model-xyz")
    assert unknown is not None
    assert unknown.input_modalities == {"text"}  # no static match -> text only


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

    # Unlisted model: neither (no live metadata, no static match).
    assert not capabilities.supports_vision("acme/not-a-real-model")
    assert not capabilities.supports_documents("acme/not-a-real-model")
