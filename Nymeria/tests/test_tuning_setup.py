"""Wizard agent-tuning wiring: catalog, env mapping, hydrate, flags, review.

Mirrors the voice-setup tests: picks and typed values must write the right env
lines, blank fields must write nothing (settings defaults stay in charge),
hydrate must round-trip on-disk lines back into the wizard's raw input strings,
and invalid hand-edited values must be skipped, never crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.setup import tuning_catalog
from nymeria.setup.runner import _build_state, build_parser
from nymeria.setup.state import WizardState
from nymeria.setup.tuning_catalog import (
    CONTEXT_FIELDS,
    LIMIT_FIELDS,
    parse_field,
    tuning_drop_env,
    tuning_env_for_state,
    tuning_summary_lines,
)


# ── parsing ──────────────────────────────────────────────────────────────────


def test_blank_field_parses_to_nothing():
    field = CONTEXT_FIELDS["compact_tokens"]
    assert parse_field(field, "") == (None, None)
    assert parse_field(field, "   ") == (None, None)


def test_int_field_bounds_and_formats():
    field = CONTEXT_FIELDS["compact_tokens"]
    assert parse_field(field, "200000") == ("200000", None)
    assert parse_field(field, "200,000") == ("200000", None)
    assert parse_field(field, "200_000") == ("200000", None)
    assert parse_field(field, "999")[1] is not None  # below minimum
    assert parse_field(field, "3000000")[1] is not None  # above maximum
    assert parse_field(field, "abc")[1] is not None
    assert parse_field(field, "1500.5")[1] is not None  # whole numbers only


def test_percent_field_converts_to_fraction():
    field = CONTEXT_FIELDS["compact_percent"]
    assert parse_field(field, "80") == ("0.8", None)
    assert parse_field(field, "45%") == ("0.45", None)
    assert parse_field(field, "4")[1] is not None
    assert parse_field(field, "99")[1] is not None


def test_timezone_field_validates_iana_names():
    field = next(f for f in LIMIT_FIELDS if f.key == "user_timezone")
    assert parse_field(field, "Australia/Sydney") == ("Australia/Sydney", None)
    assert parse_field(field, "UTC") == ("UTC", None)
    assert parse_field(field, "Mars/Olympus")[1] is not None


def test_float_field_accepts_decimals():
    field = next(f for f in tuning_catalog.SAMPLING_FIELDS if f.key == "llm_temperature")
    assert parse_field(field, "0.7") == ("0.7", None)
    assert parse_field(field, "2.5")[1] is not None


def test_non_finite_input_is_rejected_not_crashed():
    """inf/nan must produce a validation error, never a crash or an env value
    (a written LLM_TEMPERATURE=nan would brick Settings() on next load)."""
    int_field = CONTEXT_FIELDS["compact_tokens"]
    float_field = next(
        f for f in tuning_catalog.SAMPLING_FIELDS if f.key == "llm_temperature"
    )
    for raw in ("inf", "-inf", "nan", "Infinity"):
        for field in (int_field, float_field):
            value, error = parse_field(field, raw)
            assert value is None, (field.key, raw)
            assert error is not None, (field.key, raw)


# ── env production ───────────────────────────────────────────────────────────


def test_untouched_state_writes_nothing():
    assert tuning_env_for_state(WizardState()) == {}
    assert tuning_summary_lines(WizardState()) == []


def test_tokens_strategy_writes_mode_and_trigger():
    state = WizardState(extras={
        "context_strategy": "compact_tokens",
        "compact_threshold_tokens": "200000",
    })
    assert tuning_env_for_state(state) == {
        "CONTEXT_MANAGEMENT": "auto_compact",
        "COMPACT_THRESHOLD_MODE": "tokens",
        "COMPACT_THRESHOLD_TOKENS": "200000",
    }


def test_blank_trigger_leaves_default_in_charge():
    state = WizardState(extras={"context_strategy": "compact_tokens"})
    env = tuning_env_for_state(state)
    assert env == {
        "CONTEXT_MANAGEMENT": "auto_compact",
        "COMPACT_THRESHOLD_MODE": "tokens",
    }


def test_percent_strategy_writes_fraction():
    state = WizardState(extras={
        "context_strategy": "compact_percent",
        "compact_threshold_percent": "45",
    })
    env = tuning_env_for_state(state)
    assert env["COMPACT_THRESHOLD_MODE"] == "percentage"
    assert env["COMPACT_THRESHOLD"] == "0.45"


def test_only_the_matching_trigger_is_written():
    """A leftover number for an abandoned strategy never lands in the file."""
    state = WizardState(extras={
        "context_strategy": "sliding_window",
        "sliding_window_cycles": "8",
        "compact_threshold_tokens": "200000",  # typed before switching away
    })
    env = tuning_env_for_state(state)
    assert env["CONTEXT_MANAGEMENT"] == "sliding_window"
    assert env["SLIDING_WINDOW_CYCLES"] == "8"
    assert "COMPACT_THRESHOLD_TOKENS" not in env
    assert "COMPACT_THRESHOLD_MODE" not in env


def test_none_strategy_writes_only_the_mode():
    state = WizardState(extras={"context_strategy": "none"})
    assert tuning_env_for_state(state) == {"CONTEXT_MANAGEMENT": "none"}


def test_limit_fields_write_independently_of_context():
    state = WizardState(extras={
        "memory_max_entries": "250",
        "user_timezone": "Australia/Sydney",
    })
    env = tuning_env_for_state(state)
    assert env == {
        "MEMORY_MAX_ENTRIES": "250",
        "USER_TIMEZONE": "Australia/Sydney",
    }


def test_effort_gates_sampling_fields():
    """Sampling overrides ride the llm_tuning step; without an effort pick the
    step was never completed, so nothing from it is written."""
    state = WizardState(extras={"llm_temperature": "0.7"})
    assert tuning_env_for_state(state) == {}
    state.extras["llm_effort"] = "medium"
    env = tuning_env_for_state(state)
    assert env["LLM_REASONING_EFFORT"] == "medium"
    assert env["LLM_TEMPERATURE"] == "0.7"


def test_invalid_extras_are_skipped_not_crashed():
    state = WizardState(extras={
        "context_strategy": "compact_tokens",
        "compact_threshold_tokens": "garbage",
        "llm_effort": "ultra",  # not a valid effort
    })
    env = tuning_env_for_state(state)
    assert "COMPACT_THRESHOLD_TOKENS" not in env
    assert "LLM_REASONING_EFFORT" not in env


def test_resolve_extra_env_includes_tuning():
    from nymeria.onboarding import HostingOption
    from nymeria.setup.finalize import _resolve_extra_env

    state = WizardState(
        hosting=HostingOption.LOCAL,
        extras={
            "context_strategy": "compact_tokens",
            "compact_threshold_tokens": "200000",
            "llm_effort": "medium",
        },
    )
    extra = _resolve_extra_env(state)
    assert extra["CONTEXT_MANAGEMENT"] == "auto_compact"
    assert extra["COMPACT_THRESHOLD_TOKENS"] == "200000"
    assert extra["LLM_REASONING_EFFORT"] == "medium"


# ── hydrate round-trip ───────────────────────────────────────────────────────


def _write_config(tmp_path: Path, lines: list[str]) -> Path:
    config = tmp_path / "config.env"
    config.write_text("\n".join(lines) + "\n")
    return config


def test_hydrate_round_trips_tuning(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "LLM_MODEL=gpt-5.5",
        "CONTEXT_MANAGEMENT=auto_compact",
        "COMPACT_THRESHOLD_MODE=percentage",
        "COMPACT_THRESHOLD=0.45",
        "LLM_REASONING_EFFORT=high",
        "MEMORY_MAX_ENTRIES=250",
        "USER_TIMEZONE=Australia/Sydney",
        "LLM_TOP_K=40",
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    assert state.extras["context_strategy"] == "compact_percent"
    assert state.extras["compact_threshold_percent"] == "45"
    assert state.extras["llm_effort"] == "high"
    assert state.extras["memory_max_entries"] == "250"
    assert state.extras["user_timezone"] == "Australia/Sydney"
    assert state.extras["llm_top_k"] == "40"
    # An untouched reconfigure re-produces exactly what is on disk.
    env = tuning_env_for_state(state)
    assert env["COMPACT_THRESHOLD"] == "0.45"
    assert env["COMPACT_THRESHOLD_MODE"] == "percentage"
    assert env["LLM_REASONING_EFFORT"] == "high"


def test_hydrate_ignores_hand_edited_garbage(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "CONTEXT_MANAGEMENT=garbage",
        "COMPACT_THRESHOLD=abc",
        "LLM_REASONING_EFFORT=ultra",
        "TOOL_TIMEOUT=5",  # below the 30s floor
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    for key in ("context_strategy", "compact_threshold_percent", "llm_effort",
                "tool_timeout"):
        assert key not in state.extras


def test_hydrate_respects_explicit_flag(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "CONTEXT_MANAGEMENT=sliding_window",
    ])
    state = WizardState(root=tmp_path, extras={"context_strategy": "compact_tokens"})
    assert hydrate_state_from_disk(state)
    assert state.extras["context_strategy"] == "compact_tokens"


def test_hydrate_skips_inexact_percent_fractions(tmp_path):
    """A hand-set COMPACT_THRESHOLD=0.333 must not be seeded as '33' (an
    untouched reconfigure would silently rewrite the line as 0.33)."""
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "CONTEXT_MANAGEMENT=auto_compact",
        "COMPACT_THRESHOLD_MODE=percentage",
        "COMPACT_THRESHOLD=0.333",
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    assert "compact_threshold_percent" not in state.extras
    # The exact case still seeds (covered above with 0.45).


def test_cleared_hydrated_field_is_dropped(tmp_path):
    """Clearing a prefilled field on reconfigure retires the env line, so
    blank really means 'back to the default shown'."""
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "MEMORY_MAX_ENTRIES=250",
        "TOOL_TIMEOUT=600",
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    assert set(state.extras["tuning_on_disk_keys"]) == {
        "memory_max_entries", "tool_timeout",
    }
    # Untouched reconfigure: both values re-produced, nothing dropped.
    assert tuning_env_for_state(state)["MEMORY_MAX_ENTRIES"] == "250"
    assert tuning_drop_env(state) == ()
    # The user clears one field (collect pops the key): its line is retired,
    # the untouched one survives.
    state.extras.pop("memory_max_entries")
    assert tuning_drop_env(state) == ("MEMORY_MAX_ENTRIES",)
    assert "MEMORY_MAX_ENTRIES" not in tuning_env_for_state(state)
    assert tuning_env_for_state(state)["TOOL_TIMEOUT"] == "600"


def test_unvisited_steps_drop_nothing():
    """No on-disk markers (fresh install or flags-only run) -> no drops."""
    state = WizardState(extras={"context_strategy_unset_marker": True})
    assert tuning_drop_env(state) == ()


def test_strategy_pick_retires_other_triggers():
    """Picking a strategy retires the other strategies' trigger lines (a
    stale COMPACT_THRESHOLD still affects overflow recovery and displays)."""
    state = WizardState(extras={
        "context_strategy": "compact_tokens",
        "compact_threshold_tokens": "200000",
    })
    drops = tuning_drop_env(state)
    assert set(drops) == {"COMPACT_THRESHOLD", "SLIDING_WINDOW_CYCLES"}
    # The active strategy's own line is never in the drop list.
    assert "COMPACT_THRESHOLD_TOKENS" not in drops


def test_hydrate_defaults_threshold_mode_to_tokens(tmp_path):
    """auto_compact with no explicit mode line means the tokens default."""
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "CONTEXT_MANAGEMENT=auto_compact",
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    assert state.extras["context_strategy"] == "compact_tokens"


# ── CLI flags ────────────────────────────────────────────────────────────────


def test_cli_tuning_flags_land_in_state():
    parser = build_parser()
    args = parser.parse_args([
        "--context", "compact_tokens",
        "--timezone", "Australia/Sydney",
        "--reasoning-effort", "medium",
    ])
    state = _build_state(args)
    assert state.extras["context_strategy"] == "compact_tokens"
    assert state.extras["user_timezone"] == "Australia/Sydney"
    assert state.extras["llm_effort"] == "medium"


def test_cli_rejects_unknown_context_strategy():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--context", "vibes"])


def test_cli_rejects_unknown_effort():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--reasoning-effort", "ultra"])


def test_cli_rejects_bad_timezone():
    parser = build_parser()
    args = parser.parse_args(["--timezone", "Mars/Olympus"])
    with pytest.raises(SystemExit, match="timezone"):
        _build_state(args)


# ── catalog/settings agreement ───────────────────────────────────────────────


def test_effort_values_match_settings_literal():
    """The wizard's effort values must be exactly the settings Literal values
    minus nothing: finalize writes them raw into LLM_REASONING_EFFORT."""
    from typing import get_args, get_origin, Union

    from nymeria.config.settings import Settings

    annotation = Settings.model_fields["llm_reasoning_effort"].annotation
    # Optional[Literal[...]] -> unwrap the Optional.
    if get_origin(annotation) is Union:
        annotation = next(
            arg for arg in get_args(annotation) if arg is not type(None)
        )
    literal_values = set(get_args(annotation))
    assert tuning_catalog.EFFORT_VALUES == literal_values


def test_context_values_cover_settings_literal():
    from typing import get_args

    from nymeria.config.settings import Settings

    management = set(
        get_args(Settings.model_fields["context_management"].annotation)
    )
    produced = {
        env["CONTEXT_MANAGEMENT"]
        for env in (
            tuning_env_for_state(WizardState(extras={"context_strategy": v}))
            for v in tuning_catalog.CONTEXT_VALUES
        )
    }
    assert produced == management


def test_review_summary_lines():
    state = WizardState(extras={
        "context_strategy": "compact_tokens",
        "compact_threshold_tokens": "200000",
        "memory_max_entries": "250",
        "llm_effort": "max",
        "llm_max_tokens": "32000",
    })
    lines = tuning_summary_lines(state)
    assert any("COMPACT_THRESHOLD_TOKENS=200000" in line for line in lines)
    assert any("MEMORY_MAX_ENTRIES=250" in line for line in lines)
    assert any("Reasoning effort: Max" in line and "LLM_MAX_TOKENS=32000" in line
               for line in lines)
