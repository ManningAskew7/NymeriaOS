"""Tests for ThreadTokenUsage / TokenTracker.

Covers the 2026-07-04 repair-pass semantics: per-turn consumption vs context
occupancy, turn_recorded honesty, cumulative survival across compaction, the
post-compaction high-water index clamp, and the cost fields.
"""

from __future__ import annotations

from nymeria.core.token_tracker import TokenTracker


class TestRecordTurn:
    def test_turn_and_cumulative_accumulation(self):
        t = TokenTracker()
        t.record_turn("thread-1", turn_input_tokens=100, turn_output_tokens=50,
                      context_tokens=100, cost_usd=0.005)
        t.record_turn("thread-1", turn_input_tokens=200, turn_output_tokens=100,
                      context_tokens=250, cost_usd=0.01)
        u = t.get_usage("thread-1")
        assert u.turn_input_tokens == 200
        assert u.turn_output_tokens == 100
        assert u.turn_recorded is True
        assert u.total_input_tokens == 300
        assert u.total_output_tokens == 150
        assert u.context_tokens == 250
        assert u.total_cost_usd == 0.015
        assert u.last_cost_usd == 0.01

    def test_empty_turn_keeps_occupancy_and_marks_unrecorded(self):
        """A metadata-less turn must not zero the context bar (defect #1) and
        must not leave the previous turn's values looking current (defect #6)."""
        t = TokenTracker()
        t.record_turn("thread-1", turn_input_tokens=100, turn_output_tokens=50,
                      context_tokens=100)
        t.record_turn("thread-1", turn_input_tokens=0, turn_output_tokens=0,
                      context_tokens=None, cost_unavailable=True)
        u = t.get_usage("thread-1")
        assert u.context_tokens == 100  # occupancy kept, not zeroed
        assert u.turn_input_tokens == 0
        assert u.turn_output_tokens == 0
        assert u.turn_recorded is False
        assert u.total_input_tokens == 100  # cumulative unchanged

    def test_zero_context_tokens_keeps_previous_estimate(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=10, turn_output_tokens=5,
                      context_tokens=500)
        t.record_turn("t", turn_input_tokens=20, turn_output_tokens=8,
                      context_tokens=0)
        assert t.get_usage("t").context_tokens == 500

    def test_cost_unavailable_does_not_accumulate(self):
        t = TokenTracker()
        t.record_turn("thread-2", turn_input_tokens=100, turn_output_tokens=50,
                      cost_unavailable=True)
        u = t.get_usage("thread-2")
        assert u.cost_unavailable is True
        assert u.total_cost_usd == 0.0
        assert u.last_cost_usd is None
        # Even if a number is provided, the N/A flag wins.
        t.record_turn("thread-2", turn_input_tokens=100, turn_output_tokens=50,
                      cost_usd=0.5, cost_unavailable=True)
        assert t.get_usage("thread-2").total_cost_usd == 0.0

    def test_none_cost_does_not_set_na_flag(self):
        t = TokenTracker()
        t.record_turn("thread-3", turn_input_tokens=100, turn_output_tokens=50,
                      cost_usd=None)
        u = t.get_usage("thread-3")
        assert u.cost_unavailable is False
        assert u.total_cost_usd == 0.0
        assert u.last_cost_usd is None

    def test_tokens_still_recorded_when_cost_missing(self):
        t = TokenTracker()
        t.record_turn("thread-4", turn_input_tokens=100, turn_output_tokens=50,
                      cost_usd=None)
        u = t.get_usage("thread-4")
        assert u.total_input_tokens == 100
        assert u.total_output_tokens == 50

    def test_high_water_index_default(self):
        t = TokenTracker()
        u = t.get_usage("thread-5")
        assert u.last_recorded_message_index == 0

    def test_subsequent_paid_call_clears_na_flag(self):
        t = TokenTracker()
        t.record_turn("thread-6", turn_input_tokens=100, turn_output_tokens=50,
                      cost_unavailable=True)
        t.record_turn("thread-6", turn_input_tokens=100, turn_output_tokens=50,
                      cost_usd=0.01)
        u = t.get_usage("thread-6")
        assert u.total_cost_usd == 0.01
        assert u.cost_unavailable is False
        assert u.last_cost_usd == 0.01

    def test_missing_rates_clear_last_turn_cost(self):
        t = TokenTracker()
        t.record_turn("thread-7", turn_input_tokens=100, turn_output_tokens=50,
                      cost_usd=0.01)
        t.record_turn("thread-7", turn_input_tokens=100, turn_output_tokens=50,
                      cost_usd=None)
        u = t.get_usage("thread-7")
        assert u.total_cost_usd == 0.01
        assert u.last_cost_usd is None


class TestResetAfterCompact:
    def test_occupancy_resets_but_consumption_survives(self):
        """Compaction shrinks the window; it does not un-consume tokens
        (defects #3 and #10 in the repair pass)."""
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=1000, turn_output_tokens=400,
                      context_tokens=1400, cost_usd=0.02)
        t.reset_after_compact("t", remaining_tokens=300)
        u = t.get_usage("t")
        assert u.context_tokens == 300
        assert u.total_input_tokens == 1000
        assert u.total_output_tokens == 400
        assert u.turn_input_tokens == 1000  # last turn's record survives
        assert u.turn_output_tokens == 400
        assert u.turn_recorded is True
        assert u.compaction_count == 1
        assert u.last_compaction_at is not None
        assert u.total_cost_usd == 0.02

    def test_remaining_message_count_clamps_high_water_index(self):
        """Defect #4: without the clamp, the first post-compaction turn's
        slice was empty and its cost silently dropped."""
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=10, turn_output_tokens=5,
                      context_tokens=10)
        u = t.get_usage("t")
        u.last_recorded_message_index = 120  # pre-compaction history length
        t.reset_after_compact("t", remaining_tokens=300, remaining_message_count=8)
        assert t.get_usage("t").last_recorded_message_index == 8

    def test_without_message_count_index_is_untouched(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=10, turn_output_tokens=5,
                      context_tokens=10)
        u = t.get_usage("t")
        u.last_recorded_message_index = 42
        t.reset_after_compact("t", remaining_tokens=100)
        assert t.get_usage("t").last_recorded_message_index == 42


class TestSeedRehydrated:
    def test_seeds_totals_occupancy_and_index(self):
        t = TokenTracker()
        t.seed_rehydrated("t", total_input_tokens=5000, total_output_tokens=2000,
                          context_tokens=1500, message_index=40)
        u = t.get_usage("t")
        assert u.total_input_tokens == 5000
        assert u.total_output_tokens == 2000
        assert u.context_tokens == 1500
        assert u.last_recorded_message_index == 40
        # The last turn is unknowable post-restart: clients must not
        # accumulate the zeros.
        assert u.turn_recorded is False
        assert u.turn_input_tokens == 0


class TestSetContextEstimate:
    def test_replaces_occupancy_only(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=100, turn_output_tokens=40,
                      context_tokens=900, cost_usd=0.01)
        t.set_context_estimate("t", 250)
        u = t.get_usage("t")
        assert u.context_tokens == 250
        assert u.turn_input_tokens == 100
        assert u.total_cost_usd == 0.01

    def test_negative_estimate_clamped_to_zero(self):
        t = TokenTracker()
        t.set_context_estimate("t", -5)
        assert t.get_usage("t").context_tokens == 0

class TestTurnLlmSeconds:
    def test_stored_for_recorded_turn(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=100, turn_output_tokens=50,
                      context_tokens=100, turn_llm_seconds=2.5)
        assert t.get_usage("t").turn_llm_seconds == 2.5

    def test_cleared_when_turn_unrecorded(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=100, turn_output_tokens=50,
                      context_tokens=100, turn_llm_seconds=2.5)
        t.record_turn("t", turn_input_tokens=0, turn_output_tokens=0,
                      turn_llm_seconds=1.0)
        # Tokens unknown -> a rate would divide stale tokens by new time.
        assert t.get_usage("t").turn_llm_seconds is None

    def test_none_and_nonpositive_stored_as_none(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=10, turn_output_tokens=5,
                      context_tokens=10, turn_llm_seconds=0.0)
        assert t.get_usage("t").turn_llm_seconds is None

    def test_survives_compaction_reset(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=100, turn_output_tokens=50,
                      context_tokens=100, turn_llm_seconds=2.5)
        t.reset_after_compact("t", remaining_tokens=30)
        assert t.get_usage("t").turn_llm_seconds == 2.5
