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

    def test_dotted_id_gains_hyphenated_variant_after_exact_forms(self):
        """OpenRouter-style dotted ids must reach LiteLLM's hyphenated keys."""
        keys = pricing_table._candidate_keys("", "anthropic/claude-sonnet-4.6")
        assert "claude-sonnet-4-6" in keys
        # Variants are fallbacks: every exact form stays ahead of them.
        assert keys.index("claude-sonnet-4.6") < keys.index("claude-sonnet-4-6")

    def test_hyphenated_id_gains_dotted_variant(self):
        keys = pricing_table._candidate_keys("anthropic", "claude-opus-4-8")
        assert "claude-opus-4.8" in keys

    def test_word_separators_are_never_swapped(self):
        """Only separators BETWEEN digits swap; word hyphens stay untouched."""
        keys = pricing_table._candidate_keys("openai", "gpt-4o-mini")
        assert keys == ["gpt-4o-mini", "openai/gpt-4o-mini"]


class TestCatalogCapabilities:
    def test_real_bundle_context_and_flags(self):
        # Structural pins only (types, positivity, stable flags): exact token
        # counts live upstream and would rot this test on a bundle refresh.
        caps = pricing_table.get_catalog_capabilities("anthropic", "claude-opus-4-8")
        assert caps is not None
        assert isinstance(caps.max_input_tokens, int) and caps.max_input_tokens > 0
        assert isinstance(caps.max_output_tokens, int) and caps.max_output_tokens > 0
        assert caps.supports_vision is True
        assert caps.supports_pdf_input is True

    def test_dotted_id_resolves_via_separator_variant(self):
        # The dotted OpenRouter-style spelling must land on the same entry as
        # the hyphenated LiteLLM key.
        dotted = pricing_table.get_catalog_capabilities("", "anthropic/claude-sonnet-4.6")
        hyphenated = pricing_table.get_catalog_capabilities("anthropic", "claude-sonnet-4-6")
        assert dotted is not None
        assert dotted == hyphenated

    def test_sparse_prefixed_entry_merges_with_bare_sibling(self, monkeypatch):
        # Provider-prefixed entries are often sparser than their bare
        # siblings; fields must merge along the candidate chain instead of
        # the first field-bearing entry masking the richer one.
        monkeypatch.setattr(
            pricing_table,
            "_litellm_cache",
            {
                "azure/o3-like": {"max_input_tokens": 200000},
                "o3-like": {"supports_vision": True, "supports_pdf_input": True},
            },
        )
        caps = pricing_table.get_catalog_capabilities("", "azure/o3-like")
        assert caps is not None
        assert caps.max_input_tokens == 200000
        assert caps.supports_vision is True
        assert caps.supports_pdf_input is True

    def test_non_finite_numbers_are_treated_as_absent(self, monkeypatch):
        # json.loads accepts NaN/Infinity literals; the refresh payload is
        # untrusted upstream data and must not raise.
        monkeypatch.setattr(
            pricing_table,
            "_litellm_cache",
            {
                "weird-inf": {"max_input_tokens": float("inf")},
                "weird-nan": {"max_input_tokens": float("nan")},
            },
        )
        assert pricing_table.get_catalog_capabilities("", "weird-inf") is None
        assert pricing_table.get_catalog_capabilities("", "weird-nan") is None

    def test_absent_flags_are_none_not_false(self, monkeypatch):
        monkeypatch.setattr(
            pricing_table,
            "_litellm_cache",
            {"model-x": {"max_input_tokens": 5000, "input_cost_per_token": 0.000001}},
        )
        caps = pricing_table.get_catalog_capabilities("", "model-x")
        assert caps is not None
        assert caps.max_input_tokens == 5000
        assert caps.supports_vision is None
        assert caps.supports_pdf_input is None
        assert caps.max_output_tokens is None

    def test_entry_without_capability_fields_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            pricing_table,
            "_litellm_cache",
            {"model-y": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}},
        )
        assert pricing_table.get_catalog_capabilities("", "model-y") is None

    def test_junk_field_types_are_treated_as_absent(self, monkeypatch):
        # sample_spec-style entries carry description strings in these fields.
        monkeypatch.setattr(
            pricing_table,
            "_litellm_cache",
            {"model-z": {"max_input_tokens": "lots", "supports_vision": "yes"}},
        )
        assert pricing_table.get_catalog_capabilities("", "model-z") is None

    def test_unknown_and_empty_model_return_none(self):
        assert pricing_table.get_catalog_capabilities("openai", "not-a-real-model-xyz") is None
        assert pricing_table.get_catalog_capabilities("openai", "") is None

    def test_lookup_never_triggers_network_refresh(self, monkeypatch):
        calls = []

        def fail(url, timeout):
            calls.append(url)
            raise AssertionError("unexpected network refresh")

        monkeypatch.setattr(httpx, "get", fail)
        # Force staleness: even then, capability reads must stay offline.
        monkeypatch.setattr(pricing_table, "_last_fetch_ts", 0.0)
        assert pricing_table.get_catalog_capabilities("openai", "gpt-4o") is not None
        assert calls == []
