"""Regression tests for model metadata fallback behavior."""

from __future__ import annotations

import time
import threading

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
