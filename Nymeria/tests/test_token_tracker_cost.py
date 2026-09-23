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


def test_compaction_timestamp_is_aware_utc_not_a_naive_local_clock():
    """``last_compaction_at`` used ``datetime.now()``, a naive LOCAL time, while
    every other timestamp in the system is aware UTC.

    It reaches clients verbatim as ``context_stats.last_compaction``
    (``agent_context_stats`` calls ``.isoformat()`` on it), so the wire carried
    a bare ISO string whose zone was the SERVER's, unmarked. Measured live
    2026-08-26 on a Sydney deployment: a compaction whose turn ran at 11:34 UTC
    reported ``2026-08-26T21:34:33.986808``. A consumer reading that as UTC,
    which is the only reasonable default for an unmarked ISO string on an API,
    is off by the server's offset, and arithmetic against an aware datetime
    raises outright.
    """
    from datetime import timezone

    from nymeria.core.token_tracker import TokenTracker

    tracker = TokenTracker()
    tracker.reset_after_compact("thread-1", remaining_tokens=100)

    stamped = tracker._row("thread-1").last_compaction_at
    assert stamped is not None
    assert stamped.tzinfo is not None, "naive timestamp: the server's zone leaks to clients"
    assert stamped.utcoffset() == timezone.utc.utcoffset(None)
    # Serializes with an explicit offset, so a client cannot misread it.
    assert stamped.isoformat().endswith("+00:00")


class TestOccupancyEstimates:
    """#260: ``last_input_tokens`` has ONE meaning, the last model call's
    prompt-side tokens including the fixed overhead (system prompt, tool
    schemas, memories). The estimator writers adjust that figure; they never
    replace it with a messages-only estimate."""

    def test_adjust_subtracts_the_prune_delta_and_keeps_the_overhead(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=1, turn_output_tokens=1,
                      context_tokens=77_136, context_model="m")
        # The prune removed an estimated 57,000 message tokens; 3,000 remain.
        got = t.adjust_context_estimate(
            "t", removed_tokens=57_000, remaining_estimate=3_000, context_model="m"
        )
        assert got == 20_136
        u = t.get_usage("t")
        assert u.context_tokens == 20_136
        assert u.context_model == "m"

    def test_adjust_never_drops_below_the_remaining_messages(self):
        t = TokenTracker()
        t.set_context_estimate("t", 10_000, context_model="m")
        got = t.adjust_context_estimate(
            "t", removed_tokens=9_500, remaining_estimate=3_000, context_model="m"
        )
        assert got == 3_000
        assert t.get_usage("t").context_tokens == 3_000

    def test_adjust_with_no_prior_figure_uses_the_remaining_estimate(self):
        t = TokenTracker()
        got = t.adjust_context_estimate(
            "t", removed_tokens=500, remaining_estimate=3_000, context_model="m"
        )
        assert got == 3_000
        u = t.get_usage("t")
        assert u.context_tokens == 3_000
        assert u.context_model == "m"  # stamped when nothing was stamped
        # A zeroed row with a foreign stamp: the figure written is entirely
        # in the new model's units, so the stamp follows it (a kept stamp
        # would make the next poll re-scale a number already in those units).
        t.reset_after_compact("t2", 0, context_model="claude-old")
        t.adjust_context_estimate(
            "t2", removed_tokens=500, remaining_estimate=3_000, context_model="gpt-new"
        )
        assert t.get_usage("t2").context_model == "gpt-new"

    def test_adjust_keeps_a_different_existing_stamp(self):
        # The measured figure was taken under another model; the delta is an
        # approximation in the prune model's tokens, and the stamp must stay
        # so the model-switch rebase still runs later.
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=1, turn_output_tokens=1,
                      context_tokens=50_000, context_model="claude-old")
        t.adjust_context_estimate(
            "t", removed_tokens=10_000, remaining_estimate=1_000, context_model="gpt-new"
        )
        u = t.get_usage("t")
        assert u.context_tokens == 40_000
        assert u.context_model == "claude-old"

    def test_rebase_scales_by_the_estimator_ratio(self):
        t = TokenTracker()
        t.record_turn("t", turn_input_tokens=1, turn_output_tokens=1,
                      context_tokens=150_000, context_model="claude-old")
        got = t.rebase_context_estimate(
            "t", old_estimate=100_000, new_estimate=40_000, context_model="gpt-new"
        )
        assert got == 60_000
        u = t.get_usage("t")
        assert u.context_tokens == 60_000
        assert u.context_model == "gpt-new"

    def test_rebase_falls_back_to_the_new_estimate_without_a_usable_ratio(self):
        t = TokenTracker()
        # No prior figure at all.
        assert t.rebase_context_estimate(
            "t", old_estimate=100, new_estimate=40_000, context_model="gpt-new"
        ) == 40_000
        # A zero old estimate cannot scale anything, and neither can one that
        # truncates to zero (the guard and the division must agree).
        t.set_context_estimate("t2", 150_000, context_model="claude-old")
        assert t.rebase_context_estimate(
            "t2", old_estimate=0, new_estimate=40_000, context_model="gpt-new"
        ) == 40_000
        assert t.get_usage("t2").context_model == "gpt-new"
        t.set_context_estimate("t3", 150_000, context_model="claude-old")
        assert t.rebase_context_estimate(
            "t3", old_estimate=0.5, new_estimate=40_000, context_model="gpt-new"  # type: ignore[arg-type]
        ) == 40_000
