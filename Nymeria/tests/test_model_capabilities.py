"""Regression tests for model metadata fallback behavior."""

from __future__ import annotations

import time

from nymeria.config import model_capabilities as capabilities
from nymeria.config.model_capabilities import ModelInfo


def _set_model_cache(monkeypatch, models: dict[str, ModelInfo]) -> None:
    monkeypatch.setattr(capabilities, "_model_cache", models)
    monkeypatch.setattr(capabilities, "_cache_timestamp", time.time())
    monkeypatch.setattr(capabilities, "_cache_populated", True)


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
