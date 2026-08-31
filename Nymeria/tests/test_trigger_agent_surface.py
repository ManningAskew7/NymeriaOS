"""Agent-tool surface for triggers: does it tell the agent the truth about a
trigger the #264 policy stopped?

An auto-paused trigger keeps ``enabled=True``, so any view that renders only
that flag reports a trigger which cannot run as one that does. The agent
reads these strings and acts on them, so both the list and the detail view
have to say PAUSED.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.tools import triggers as trigger_tools

CONFIG = {"configurable": {"user_id": "owner", "thread_id": "thread-1"}}


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    mgr = TriggerManager(tmp_path)
    monkeypatch.setattr(trigger_tools, "_get_trigger_manager", lambda: mgr)
    return mgr


def _add(
    mgr: TriggerManager,
    *,
    paused: bool,
    enabled: bool = True,
    action_failures: int | None = None,
    consecutive_errors: int = 0,
    last_error: str | None = None,
    last_error_at: datetime | None = None,
) -> str:
    trigger = TriggerDefinition(
        id="trig1",
        name="Mail sweep",
        source_type="webhook",
        source_config={},
        action=TriggerAction(
            type="agent_prompt", config={"prompt_template": "Handle {message}"}
        ),
        thread_id="thread-1",
        enabled=enabled,
        health_status="failing" if paused else "healthy",
        action_failures=(5 if paused else 0)
        if action_failures is None
        else action_failures,
        consecutive_errors=consecutive_errors,
        last_error=last_error,
        last_error_at=last_error_at,
        auto_paused_at=(
            datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc) if paused else None
        ),
    )
    with mgr.atomic_update("owner") as store:
        store.triggers.append(trigger)
    return trigger.id


def test_listing_reports_a_paused_trigger_as_paused_not_on(manager):
    _add(manager, paused=True)

    out = trigger_tools._trigger_list(config=CONFIG)

    assert "PAUSED" in out
    assert "| ON |" not in out and "] ON " not in out
    assert "5 failed action" in out
    assert 'action="resume"' in out  # the agent is told how to undo it


def test_listing_still_reports_a_running_trigger_as_on(manager):
    _add(manager, paused=False)

    out = trigger_tools._trigger_list(config=CONFIG)

    assert "ON" in out
    assert "PAUSED" not in out


def test_detail_reports_a_paused_trigger_as_paused_not_enabled(manager):
    _add(manager, paused=True)

    out = trigger_tools._trigger_inspect(
        trigger_id="trig1", action="detail", limit=10, config=CONFIG
    )

    assert "Status: PAUSED" in out
    assert "Status: ENABLED" not in out
    assert "Auto-paused" in out
    assert "still enabled" in out  # names the flag that is misleading on its own


def test_detail_names_the_plane_of_each_failure_counter(manager):
    """The two counters must not read as a contradiction.

    ``action_failures`` (what auto-paused it) and ``consecutive_errors``
    (polling) count different planes, so a paused trigger legitimately shows
    "5 failed actions" beside a source counter of 0. Unlabelled, that reads
    as a bug: a live probe reported it as one.
    """
    _add(
        manager,
        paused=True,
        consecutive_errors=0,
        last_error="boom",
        last_error_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
    )

    out = trigger_tools._trigger_inspect(
        trigger_id="trig1", action="detail", limit=10, config=CONFIG
    )

    assert "Consecutive source errors: 0" in out
    assert "Consecutive errors:" not in out.replace("Consecutive source errors:", "")


def test_detail_omits_the_timestamp_when_the_error_has_none(manager):
    _add(manager, paused=True, last_error="boom", last_error_at=None)

    out = trigger_tools._trigger_inspect(
        trigger_id="trig1", action="detail", limit=10, config=CONFIG
    )

    assert "Last error: boom" in out
    assert "(?)" not in out


def test_detail_shows_action_failures_before_they_reach_the_pause_threshold(manager):
    """A trigger part-way to auto-pause has to say so, with the threshold.

    Without this the only visible failure counter is the SOURCE one, which
    stays 0, so a trigger three action failures deep looks perfectly healthy
    right up until it stops.
    """
    _add(manager, paused=False, action_failures=3)

    out = trigger_tools._trigger_inspect(
        trigger_id="trig1", action="detail", limit=10, config=CONFIG
    )

    assert "Action failures: 3" in out
    assert "auto-pauses at 5" in out
    assert "Auto-paused" not in out  # it has NOT been paused


def test_detail_stays_quiet_about_action_failures_on_a_clean_trigger(manager):
    _add(manager, paused=False)

    out = trigger_tools._trigger_inspect(
        trigger_id="trig1", action="detail", limit=10, config=CONFIG
    )

    assert "Action failures" not in out


def test_resume_action_clears_the_pause(manager):
    _add(manager, paused=True)

    out = trigger_tools._trigger_resume(trigger_id="trig1", config=CONFIG)

    assert out.startswith("[Success]")
    assert "5 failed action" in out
    stored = manager.get_trigger("owner", "trig1")
    assert stored is not None
    assert stored.auto_paused_at is None
    assert stored.action_failures == 0
    assert stored.health_status == "healthy"


def test_resume_warns_when_the_trigger_is_also_disabled(manager):
    _add(manager, paused=True, enabled=False)

    out = trigger_tools._trigger_resume(trigger_id="trig1", config=CONFIG)

    assert out.startswith("[Success]")
    assert "DISABLED" in out  # resuming did not make it run


def test_resume_reports_an_unknown_trigger(manager):
    out = trigger_tools._trigger_resume(trigger_id="nope", config=CONFIG)

    assert out.startswith("[Error]")
    assert "not found" in out


def test_trigger_config_rejects_resume_without_an_id(manager):
    out = trigger_tools.trigger_config.func(  # type: ignore[attr-defined]
        action="resume", config=CONFIG
    )

    assert out.startswith("[Error]")
    assert "trigger_id" in out
