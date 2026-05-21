"""Tests for cost extraction and computation from LLM responses."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.config.pricing_table import ModelRates
from nymeria.core import cost_calc


def _ai(*, usage_metadata=None, response_metadata=None, content="ok"):
    msg = AIMessage(content=content)
    if usage_metadata is not None:
        msg.usage_metadata = usage_metadata
    if response_metadata is not None:
        msg.response_metadata = response_metadata
    return msg


# Hand-tuned rates so test values are easy to verify against the spec §3 formula.
_RATES_OPENAI = ModelRates(
    input_per_token=2.5e-6,
    output_per_token=1.0e-5,
    cache_read_per_token=1.25e-6,
)
_RATES_ANTHROPIC = ModelRates(
    input_per_token=5.0e-6,
    output_per_token=2.5e-5,
    cache_read_per_token=5.0e-7,
    cache_write_5m_per_token=6.25e-6,
    cache_write_1h_per_token=1.0e-5,
)
_RATES_GEMINI = ModelRates(
    input_per_token=1.25e-6,
    output_per_token=1.0e-5,
    cache_read_per_token=1.25e-7,
    input_above_200k_per_token=2.5e-6,
    output_above_200k_per_token=2.0e-5,
)


class TestParseUsage:
    def test_anthropic_native_cache(self):
        msg = _ai(
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "input_token_details": {"cache_read": 800, "cache_creation": 100},
            },
            response_metadata={
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 800,
                }
            },
        )
        usage = cost_calc.parse_usage_from_message(msg, "anthropic")
        assert usage.prompt_tokens == 1000
        assert usage.completion_tokens == 500
        assert usage.cached_tokens == 800
        assert usage.cache_write_5m_tokens == 100

    def test_anthropic_ephemeral_breakdown(self):
        msg = _ai(
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
            },
            response_metadata={
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 100,
                    "cache_creation_input_tokens": 600,
                    "cache_read_input_tokens": 200,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 200,
                        "ephemeral_1h_input_tokens": 400,
                    },
                }
            },
        )
        usage = cost_calc.parse_usage_from_message(msg, "anthropic")
        assert usage.cached_tokens == 200
        assert usage.cache_write_5m_tokens == 200
        assert usage.cache_write_1h_tokens == 400

    def test_openai_cache_and_reasoning(self):
        msg = _ai(
            usage_metadata={
                "input_tokens": 2006,
                "output_tokens": 300,
                "total_tokens": 2306,
                "input_token_details": {"cache_read": 1920},
                "output_token_details": {"reasoning": 50},
            }
        )
        usage = cost_calc.parse_usage_from_message(msg, "openai")
        assert usage.prompt_tokens == 2006
        assert usage.completion_tokens == 300
        assert usage.cached_tokens == 1920
        assert usage.reasoning_tokens == 50

    def test_openai_responses_metadata_only(self):
        msg = _ai(
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 400},
                    "completion_tokens_details": {"reasoning_tokens": 80},
                }
            }
        )
        usage = cost_calc.parse_usage_from_message(msg, "openai")
        assert usage.prompt_tokens == 1000
        assert usage.completion_tokens == 200
        assert usage.cached_tokens == 400
        assert usage.reasoning_tokens == 80

    def test_openrouter_provider_reported_cost(self):
        msg = _ai(
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            response_metadata={"usage": {"cost": 0.0042}},
        )
        usage = cost_calc.parse_usage_from_message(msg, "openrouter")
        assert usage.provider_reported_cost_usd == pytest.approx(0.0042)

    def test_perplexity_total_cost(self):
        msg = _ai(
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            response_metadata={"usage": {"cost": {"total_cost": 0.0123}}},
        )
        usage = cost_calc.parse_usage_from_message(msg, "perplexity")
        assert usage.provider_reported_cost_usd == pytest.approx(0.0123)

    def test_deepseek_cache_hit_tokens(self):
        msg = _ai(
            usage_metadata={
                "input_tokens": 500,
                "output_tokens": 100,
                "total_tokens": 600,
            },
            response_metadata={
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "prompt_cache_hit_tokens": 300,
                    "prompt_cache_miss_tokens": 200,
                }
            },
        )
        usage = cost_calc.parse_usage_from_message(msg, "deepseek")
        assert usage.cached_tokens == 300

    def test_empty_message(self):
        msg = _ai()
        usage = cost_calc.parse_usage_from_message(msg, "openai")
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.provider_reported_cost_usd is None


class TestComputeCost:
    def test_anthropic_cache_aware_formula(self):
        # LangChain rolls cache_read AND cache_creation into prompt_tokens.
        # 1000 prompt (inclusive), 500 completion, 800 cached_read, 100 cache_write_5m
        # uncached = 1000 - 800 - 100 = 100
        # = 100*5e-6 + 800*5e-7 + 100*6.25e-6 + 500*2.5e-5
        # = 0.0005 + 0.0004 + 0.000625 + 0.0125 = 0.014025
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=1000,
            completion_tokens=500,
            cached_tokens=800,
            cache_write_5m_tokens=100,
        )
        cost = cost_calc.compute_cost_usd(usage, _RATES_ANTHROPIC)
        assert cost == pytest.approx(0.014025)

    def test_anthropic_cache_write_1h(self):
        # 1000 prompt (inclusive), 100 completion, 200 cache_write_5m, 400 cache_write_1h
        # uncached = 1000 - 200 - 400 = 400
        # = 400*5e-6 + 200*6.25e-6 + 400*1e-5 + 100*2.5e-5
        # = 0.002 + 0.00125 + 0.004 + 0.0025 = 0.00975
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=1000,
            completion_tokens=100,
            cache_write_5m_tokens=200,
            cache_write_1h_tokens=400,
        )
        cost = cost_calc.compute_cost_usd(usage, _RATES_ANTHROPIC)
        assert cost == pytest.approx(0.00975)

    def test_openai_cache_read(self):
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=2006,
            completion_tokens=300,
            cached_tokens=1920,
        )
        # uncached = 86; cost = 86*2.5e-6 + 1920*1.25e-6 + 300*1e-5
        # = 0.000215 + 0.0024 + 0.003 = 0.005615
        cost = cost_calc.compute_cost_usd(usage, _RATES_OPENAI)
        assert cost == pytest.approx(0.005615)

    def test_reasoning_tokens_not_double_billed(self):
        """Reasoning tokens are inside completion_tokens; cost reflects that."""
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=100,
            completion_tokens=500,
            reasoning_tokens=400,
        )
        cost = cost_calc.compute_cost_usd(usage, _RATES_OPENAI)
        # = 100*2.5e-6 + 500*1e-5 = 0.00025 + 0.005 = 0.00525
        assert cost == pytest.approx(0.00525)

    def test_gemini_tier_pricing_above_200k(self):
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=250_000,
            completion_tokens=1_000,
        )
        # Above-threshold rates kick in: 250000*2.5e-6 + 1000*2e-5
        cost = cost_calc.compute_cost_usd(usage, _RATES_GEMINI)
        assert cost == pytest.approx(250_000 * 2.5e-6 + 1_000 * 2.0e-5)

    def test_gemini_below_threshold(self):
        usage = cost_calc.NormalizedUsage(prompt_tokens=100_000, completion_tokens=1_000)
        cost = cost_calc.compute_cost_usd(usage, _RATES_GEMINI)
        assert cost == pytest.approx(100_000 * 1.25e-6 + 1_000 * 1.0e-5)

    def test_provider_reported_cost_preferred(self):
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=999_999,
            completion_tokens=999_999,
            provider_reported_cost_usd=0.001,
        )
        # Provider's number wins; computed total ignored.
        cost = cost_calc.compute_cost_usd(usage, _RATES_OPENAI)
        assert cost == pytest.approx(0.001)

    def test_no_rates_returns_none(self):
        usage = cost_calc.NormalizedUsage(prompt_tokens=100, completion_tokens=50)
        assert cost_calc.compute_cost_usd(usage, None) is None

    def test_no_rates_but_provider_reported(self):
        usage = cost_calc.NormalizedUsage(
            prompt_tokens=100, completion_tokens=50, provider_reported_cost_usd=0.05
        )
        assert cost_calc.compute_cost_usd(usage, None) == pytest.approx(0.05)

    def test_zero_tokens_returns_zero(self):
        usage = cost_calc.NormalizedUsage()
        assert cost_calc.compute_cost_usd(usage, _RATES_OPENAI) == pytest.approx(0.0)


class TestSumAcrossMessages:
    def test_sum_multiple_ai_messages(self):
        msgs = [
            HumanMessage(content="hi"),
            _ai(
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                }
            ),
            _ai(
                usage_metadata={
                    "input_tokens": 200,
                    "output_tokens": 80,
                    "total_tokens": 280,
                }
            ),
        ]
        summed, count = cost_calc.parse_usage_from_messages(msgs, "openai")
        assert count == 2
        assert summed.prompt_tokens == 300
        assert summed.completion_tokens == 130

    def test_sum_skips_zero_usage(self):
        msgs = [
            _ai(),  # no usage
            _ai(
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                }
            ),
        ]
        summed, count = cost_calc.parse_usage_from_messages(msgs, "openai")
        assert count == 1
        assert summed.prompt_tokens == 10

    def test_slice_new_ai_messages(self):
        msgs = ["a", "b", "c", "d"]
        new, idx = cost_calc.slice_new_ai_messages(msgs, 2)
        assert new == ["c", "d"]
        assert idx == 4

    def test_slice_new_ai_messages_empty(self):
        new, idx = cost_calc.slice_new_ai_messages(["a", "b"], 2)
        assert new == []
        assert idx == 2

    def test_slice_negative_index(self):
        new, idx = cost_calc.slice_new_ai_messages(["a", "b"], -1)
        assert new == ["a", "b"]
        assert idx == 2
