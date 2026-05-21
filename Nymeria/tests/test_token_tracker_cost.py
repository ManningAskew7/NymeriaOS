"""Tests for the cost-tracking fields on ThreadTokenUsage / TokenTracker."""

from __future__ import annotations

from nymeria.core.token_tracker import TokenTracker


class TestRecordCost:
    def test_cumulative_accumulation(self):
        t = TokenTracker()
        t.record_usage("thread-1", 100, 50, cost_usd=0.005)
        t.record_usage("thread-1", 200, 100, cost_usd=0.01)
        u = t.get_usage("thread-1")
        assert u.total_cost_usd == 0.015
        assert u.last_cost_usd == 0.01

    def test_cost_unavailable_does_not_accumulate(self):
        t = TokenTracker()
        t.record_usage("thread-2", 100, 50, cost_unavailable=True)
        u = t.get_usage("thread-2")
        assert u.cost_unavailable is True
        assert u.total_cost_usd == 0.0
        assert u.last_cost_usd is None
        # Even if a number is provided, the N/A flag wins.
        t.record_usage("thread-2", 100, 50, cost_usd=0.5, cost_unavailable=True)
        assert t.get_usage("thread-2").total_cost_usd == 0.0

    def test_none_cost_does_not_set_na_flag(self):
        t = TokenTracker()
        t.record_usage("thread-3", 100, 50, cost_usd=None)
        u = t.get_usage("thread-3")
        assert u.cost_unavailable is False
        assert u.total_cost_usd == 0.0
        assert u.last_cost_usd is None

    def test_tokens_still_recorded_when_cost_missing(self):
        t = TokenTracker()
        t.record_usage("thread-4", 100, 50, cost_usd=None)
        u = t.get_usage("thread-4")
        assert u.total_input_tokens == 100
        assert u.total_output_tokens == 50

    def test_high_water_index_default(self):
        t = TokenTracker()
        u = t.get_usage("thread-5")
        assert u.last_recorded_message_index == 0

    def test_subsequent_paid_call_clears_na_flag(self):
        t = TokenTracker()
        t.record_usage("thread-6", 100, 50, cost_unavailable=True)
        t.record_usage("thread-6", 100, 50, cost_usd=0.01)
        u = t.get_usage("thread-6")
        assert u.total_cost_usd == 0.01
        assert u.cost_unavailable is False
        assert u.last_cost_usd == 0.01

    def test_missing_rates_clear_last_turn_cost(self):
        t = TokenTracker()
        t.record_usage("thread-7", 100, 50, cost_usd=0.01)
        t.record_usage("thread-7", 100, 50, cost_usd=None)
        u = t.get_usage("thread-7")
        assert u.total_cost_usd == 0.01
        assert u.last_cost_usd is None
