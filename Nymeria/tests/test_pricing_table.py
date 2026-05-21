"""Tests for the LiteLLM pricing-table loader."""

from __future__ import annotations

import httpx
import pytest

from nymeria.config import pricing_table


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """Reset the module-level cache before each test so order does not matter."""
    monkeypatch.setattr(pricing_table, "_litellm_cache", {}, raising=False)
    monkeypatch.setattr(pricing_table, "_last_fetch_ts", 0.0, raising=False)
    monkeypatch.setattr(pricing_table, "_last_failure_ts", 0.0, raising=False)
    monkeypatch.setattr(pricing_table, "_loaded_from_bundle", False, raising=False)
    pricing_table._ensure_bundle_loaded()
    yield


class TestBundledSnapshot:
    def test_bundle_loads_at_import(self):
        assert pricing_table.cache_size() > 0

    def test_bundle_has_known_models(self):
        assert pricing_table.get_rates("anthropic", "claude-opus-4-7") is not None
        assert pricing_table.get_rates("openai", "gpt-4o-2024-08-06") is not None


class TestGetRates:
    def test_anthropic_cache_aware_rates(self):
        rates = pricing_table.get_rates("anthropic", "claude-opus-4-7")
        assert rates is not None
        assert rates.input_per_token > 0
        assert rates.output_per_token > 0
        assert rates.cache_read_per_token is not None
        assert rates.cache_write_5m_per_token is not None
        assert rates.supports_prompt_caching is True

    def test_openai_rates_have_cache_read(self):
        rates = pricing_table.get_rates("openai", "gpt-4o")
        assert rates is not None
        assert rates.cache_read_per_token is not None

    def test_dated_model_suffix_strip(self):
        """``gpt-4o-2024-08-06`` should resolve even if only ``gpt-4o`` is keyed."""
        rates = pricing_table.get_rates("openai", "gpt-4o-2024-08-06")
        assert rates is not None
        assert rates.input_per_token > 0

    def test_provider_prefix_lookup(self):
        rates = pricing_table.get_rates("openrouter", "anthropic/claude-sonnet-4-5")
        assert rates is not None

    def test_unknown_model_returns_none(self):
        assert pricing_table.get_rates("openai", "not-a-real-model-xyz") is None

    def test_empty_model_returns_none(self):
        assert pricing_table.get_rates("openai", "") is None

    def test_get_rates_uses_bundle_without_initial_network(self, monkeypatch):
        calls = []

        def fail(url, timeout):
            calls.append(url)
            raise AssertionError("unexpected network refresh")

        monkeypatch.setattr(httpx, "get", fail)
        assert pricing_table.get_rates("openai", "gpt-4o") is not None
        assert calls == []


class TestRefresh:
    def test_refresh_succeeds_with_fake_payload(self, monkeypatch):
        payload = {
            "sample_spec": {},
            "fake-model-x": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000004,
                "cache_read_input_token_cost": 0.0000001,
                "supports_prompt_caching": True,
            },
        }

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        monkeypatch.setattr(httpx, "get", lambda url, timeout: FakeResponse())
        size = pricing_table.refresh_litellm_pricing(force=True)
        assert size == 1
        rates = pricing_table.get_rates("openai", "fake-model-x")
        assert rates is not None
        assert rates.input_per_token == pytest.approx(0.000001)
        assert rates.cache_read_per_token == pytest.approx(0.0000001)

    def test_refresh_failure_keeps_bundle(self, monkeypatch):
        size_before = pricing_table.cache_size()

        def fail(url, timeout):
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "get", fail)
        size_after = pricing_table.refresh_litellm_pricing(force=True)
        assert size_after == size_before
        assert pricing_table.cache_size() == size_before

    def test_refresh_ttl_skipped_when_fresh(self, monkeypatch):
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"sample_spec": {}}

        def fake_get(url, timeout):
            calls.append(url)
            return FakeResponse()

        monkeypatch.setattr(httpx, "get", fake_get)
        pricing_table.refresh_litellm_pricing(force=True)
        pricing_table.refresh_litellm_pricing(force=False)
        # Only the forced call should have hit the network.
        assert len(calls) == 1

    def test_refresh_failure_backoff_blocks_retry(self, monkeypatch):
        calls = []

        def fail(url, timeout):
            calls.append(url)
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "get", fail)
        pricing_table.refresh_litellm_pricing(force=True)
        # Lazy refresh should NOT retry within the backoff window.
        pricing_table._maybe_refresh_lazy()
        assert len(calls) == 1


class TestCandidateKeys:
    def test_unprefixed_model(self):
        keys = pricing_table._candidate_keys("openai", "gpt-4o")
        assert "gpt-4o" in keys
        assert "openai/gpt-4o" in keys

    def test_dated_model_strip(self):
        keys = pricing_table._candidate_keys("openai", "gpt-4o-2024-08-06")
        assert "gpt-4o" in keys
        assert "gpt-4o-2024-08-06" in keys

    def test_provider_prefix_already_in_model(self):
        keys = pricing_table._candidate_keys("openrouter", "anthropic/claude-sonnet-4")
        assert "claude-sonnet-4" in keys
        assert "anthropic/claude-sonnet-4" in keys

    def test_provider_prefixed_dated_model_strip(self):
        keys = pricing_table._candidate_keys("openrouter", "openai/gpt-4o-2024-08-06")
        assert "gpt-4o" in keys
        assert "openrouter/gpt-4o" in keys
