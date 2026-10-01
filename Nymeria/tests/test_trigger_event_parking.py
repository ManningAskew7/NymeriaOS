"""#265: a trigger event whose ACTION fails is kept and retried, not lost.

Every poll source consumes an event before its action runs (an Outlook
check tags the mail read on the mailbox, RSS/Slack/Teams/HTTP advance their
cursor), so an event whose action then failed was gone for good: nothing
would ever detect it again. The manager now parks the events of a failed
fire on the trigger (``failed_events``) in the same store write that records
the failure, and the next poll fires them again, oldest first. Crash
semantics are unchanged (a process dying between consume and fire still
loses that batch); what changes is that an OBSERVED failure keeps them.

Harness: a real ``TriggerManager`` on ``tmp_path`` with a scripted source,
and the agent turn replaced at ``_stream_live`` (the seam every
``agent_prompt`` fire goes through) by a recorder that fails or succeeds on
cue. Non-agent actions are exercised through ``notify``, with the sender
replaced the same way.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import timedelta
import re
from collections import Counter
from pathlib import Path

import pytest

from nymeria.core import activity_log as activity_log_module
from nymeria.core import notification_dispatch as dispatch_module
from nymeria.core.activity_log import ActivityLog
from nymeria.core.notifications import NOTIFICATION_SUMMARY_MAX_CHARS
from nymeria.core.pending_prompt_queue import (
    create_pending_queue,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.trigger_manager import (
    MAX_FAILED_EVENTS,
    RETRY_BATCH_MAX,
    ParkedTriggerEvent,
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
    describe_resume,
)
from nymeria.core.time_utils import utc_now
from nymeria.tools import triggers as trigger_tools
from nymeria.triggers.trigger_api import TriggerResponse

USER = "owner"
TEMPLATE = "Mail: {subject}"


class _Settings:
    data_dir: Path
    trigger_failure_alert_after = 2
    trigger_failure_pause_after = 5
    trigger_failure_alert_cooldown_minutes = 0
    notification_level = "none"


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    current = _Settings()
    current.data_dir = tmp_path
    import nymeria.config as config_pkg

    monkeypatch.setattr(config_pkg, "get_settings", lambda: current)
    # The fire paths log activity; keep it in this test's directory.
    monkeypatch.setattr(activity_log_module, "_activity_log", ActivityLog(tmp_path))
    return current


@pytest.fixture
def alerts(monkeypatch, settings):
    sent: list[str] = []
    monkeypatch.setattr(
        dispatch_module,
        "send_owner_alert",
        lambda message, _settings, **kw: sent.append(message),
    )
    return sent


class _ScriptedSource:
    """Returns the next scripted batch on each poll (then nothing)."""

    def __init__(self, batches: list[list[dict]]):
        self.batches = [list(b) for b in batches]
        self.checks = 0

    def check(self, source_config, state, user_id="", thread_id=None):
        self.checks += 1
        return self.batches.pop(0) if self.batches else []


@pytest.fixture
def source(monkeypatch):
    from nymeria.triggers import sources as sources_module

    holder: dict[str, _ScriptedSource] = {}
    monkeypatch.setattr(sources_module, "get_source", lambda name: holder.get("s"))

    def wire(batches: list[list[dict]]) -> _ScriptedSource:
        holder["s"] = _ScriptedSource(batches)
        return holder["s"]

    return wire


class _Turns:
    """Stand-in for ``TriggerManager._stream_live``: one scripted outcome per
    agent turn (an error string fails it, None succeeds), prompts recorded."""

    def __init__(self, outcomes: list[str | None]):
        self.outcomes = list(outcomes)
        self.prompts: list[str] = []
        self.succeeded: list[str] = []
        self.attachments: list[object] = []

    def __call__(self, agent, prompt, thread_id, user_id, task_id, **kw):
        self.prompts.append(prompt)
        self.attachments.append(kw.get("attachments"))
        error = self.outcomes.pop(0) if self.outcomes else None
        if error is not None:
            raise RuntimeError(error)
        self.succeeded.append(prompt)
        return (["done"], [], False, False)


def _add(manager: TriggerManager, action_type: str = "agent_prompt", **fields):
    config = (
        {"prompt_template": TEMPLATE}
        if action_type == "agent_prompt"
        else {"message_template": TEMPLATE}
    )
    trigger = TriggerDefinition(
        id="trig-1",
        name="Mail sweep",
        source_type="static",
        source_config={},
        action=TriggerAction(type=action_type, config=config),  # type: ignore[arg-type]
        thread_id="trig-thread",
        enabled=True,
        **fields,
    )
    with manager.atomic_update(USER) as store:
        store.triggers.append(trigger)
    return trigger


def _poll_and_fire(manager: TriggerManager) -> int:
    """One ticker cycle: poll, then fire whatever the poll returned."""
    fired = manager.check_triggers(USER)
    for trigger, events in fired:
        manager.fire_action_batch(trigger, events, object(), USER)  # type: ignore[arg-type]
    return len(fired)


def _stored(tmp_path: Path) -> TriggerDefinition:
    """Read the trigger back through a FRESH manager: the parked list must be
    on disk, not only on an in-memory copy."""
    trigger = TriggerManager(tmp_path).get_trigger(USER, "trig-1")
    assert trigger is not None
    return trigger


def _parked_subjects(tmp_path: Path) -> list[str]:
    return [p.event.get("subject") for p in _stored(tmp_path).failed_events]


def _mail(*subjects: str) -> list[dict]:
    return [{"subject": s} for s in subjects]


# ------------------------------------------------------------------
# R1 R3: a failed batch is kept on disk and retried first, in one batch
# ------------------------------------------------------------------


def test_failed_batch_is_kept_and_retried_ahead_of_new_events(
    tmp_path, source, monkeypatch
):
    source([_mail("a", "b"), _mail("c")])
    manager = TriggerManager(tmp_path)
    _add(manager)
    turns = _Turns(["relay 502", None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)

    stored = _stored(tmp_path)
    assert [p.event for p in stored.failed_events] == _mail("a", "b")
    assert all(p.attempts == 1 for p in stored.failed_events)
    assert all("relay 502" in p.last_error for p in stored.failed_events)
    # The parked copy is the event as the source produced it, nothing added.
    raw = json.loads((tmp_path / "triggers" / f"{USER}.json").read_text())
    assert raw["triggers"][0]["failed_events"][0]["event"] == {"subject": "a"}

    _poll_and_fire(manager)

    assert len(turns.prompts) == 2  # the retry rode the next poll's ONE batch
    retry_prompt = turns.prompts[1]
    positions = [retry_prompt.index(f"Mail: {s}") for s in ("a", "b", "c")]
    assert positions == sorted(positions)  # parked events first, oldest first
    assert "--- Item 1 (retry: an earlier attempt failed) ---\nMail: a" in retry_prompt
    assert "--- Item 2 (retry: an earlier attempt failed) ---\nMail: b" in retry_prompt
    assert "--- Item 3 ---\nMail: c" in retry_prompt  # the new one is not a retry
    assert _stored(tmp_path).failed_events == []
    assert _stored(tmp_path).fire_count == 3  # a retried event counts once


def test_a_retry_that_fails_again_counts_the_attempt(
    tmp_path, source, monkeypatch
):
    source([_mail("a", "b")])
    manager = TriggerManager(tmp_path)
    _add(manager)
    monkeypatch.setattr(manager, "_stream_live", _Turns(["first error", "second error"]))

    _poll_and_fire(manager)
    first_failed = [p.first_failed_at for p in _stored(tmp_path).failed_events]
    _poll_and_fire(manager)

    stored = _stored(tmp_path)
    assert [p.event for p in stored.failed_events] == _mail("a", "b")
    assert [p.attempts for p in stored.failed_events] == [2, 2]
    assert all(p.last_error == "second error" for p in stored.failed_events)
    # The age of the failure is when it FIRST failed, not the latest retry.
    assert [p.first_failed_at for p in stored.failed_events] == first_failed


# ------------------------------------------------------------------
# R2: single-event and non-agent fires keep their event too
# ------------------------------------------------------------------


def test_failed_single_event_prompt_is_retried(tmp_path, source, monkeypatch):
    source([_mail("x")])
    manager = TriggerManager(tmp_path)
    _add(manager)
    turns = _Turns(["turn crashed", "crashed again", None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)
    assert _parked_subjects(tmp_path) == ["x"]
    _poll_and_fire(manager)
    assert [p.attempts for p in _stored(tmp_path).failed_events] == [2]
    _poll_and_fire(manager)

    assert turns.succeeded == [turns.prompts[2]]
    assert turns.prompts[0] == "Mail: x"  # a first delivery carries no note
    assert turns.prompts[1].startswith(
        "[Retry: delivering this trigger event failed before (1 attempt(s))."
    )
    assert turns.prompts[2].startswith(
        "[Retry: delivering this trigger event failed before (2 attempt(s))."
    )
    assert turns.prompts[2].endswith("Mail: x")
    assert "crashed" not in turns.prompts[2]  # the error text stays out
    assert _stored(tmp_path).failed_events == []


def test_failed_non_agent_action_keeps_its_event_without_bookkeeping(
    tmp_path, source, monkeypatch
):
    source([_mail("x")])
    manager = TriggerManager(tmp_path)
    _add(manager, action_type="notify")
    calls: list[dict] = []
    outcomes = ["push relay down", None]

    def fake_notify(config, template_vars, user_id, trigger):
        calls.append(dict(template_vars))
        error = outcomes.pop(0)
        if error:
            raise RuntimeError(error)

    monkeypatch.setattr(manager, "_fire_notify", fake_notify)

    _poll_and_fire(manager)
    assert _parked_subjects(tmp_path) == ["x"]
    _poll_and_fire(manager)

    assert [c["subject"] for c in calls] == ["x", "x"]
    # R7: a retry reaches a non-agent action as the same event, nothing added.
    assert calls[1].keys() == calls[0].keys()
    assert _stored(tmp_path).failed_events == []


# ------------------------------------------------------------------
# R4: a flapping action loses nothing
# ------------------------------------------------------------------


def test_flapping_action_delivers_every_event_exactly_once(
    tmp_path, source, alerts, monkeypatch
):
    """Pre-#265 a success every other fire reset the #264 streak, so the
    trigger never paused and every event of every failed fire was lost."""
    subjects = [f"e{i}" for i in range(1, 7)]
    source([_mail(s) for s in subjects])
    manager = TriggerManager(tmp_path)
    _add(manager)
    turns = _Turns(["flap", None] * 3)
    monkeypatch.setattr(manager, "_stream_live", turns)

    for _ in subjects:
        _poll_and_fire(manager)

    delivered = Counter(re.findall(r"Mail: (e\d+)", "\n".join(turns.succeeded)))
    assert delivered == {subject: 1 for subject in subjects}
    assert _stored(tmp_path).failed_events == []
    assert _stored(tmp_path).auto_paused_at is None


# ------------------------------------------------------------------
# R5 R6 R10: the pause keeps what it stopped, resume retries it
# ------------------------------------------------------------------


def test_pause_mid_batch_keeps_the_unattempted_rest(
    tmp_path, source, alerts, monkeypatch
):
    source([_mail("a", "b", "c")])
    manager = TriggerManager(tmp_path)
    _add(manager, action_type="notify", action_failures=4)
    calls: list[str] = []

    def failing_notify(config, template_vars, user_id, trigger):
        calls.append(template_vars["subject"])
        raise RuntimeError("push relay down")

    monkeypatch.setattr(manager, "_fire_notify", failing_notify)

    _poll_and_fire(manager)

    assert calls == ["a"]  # the fifth failure paused it; b and c never ran
    stored = _stored(tmp_path)
    assert stored.auto_paused_at is not None
    assert [(p.event["subject"], p.attempts) for p in stored.failed_events] == [
        ("a", 1),
        ("b", 0),
        ("c", 0),
    ]


def test_paused_trigger_keeps_parked_events_and_resume_retries_them(
    tmp_path, source, alerts, monkeypatch
):
    feed = source([_mail(f"e{i}") for i in range(1, 6)] + [_mail("e6")])
    manager = TriggerManager(tmp_path)
    _add(manager)
    turns = _Turns(["kaput"] * 5)
    monkeypatch.setattr(manager, "_stream_live", turns)

    for _ in range(5):
        _poll_and_fire(manager)

    stored = _stored(tmp_path)
    assert stored.auto_paused_at is not None
    assert _parked_subjects(tmp_path) == ["e1", "e2", "e3", "e4", "e5"]
    # R10: the pause alert says the events were kept, with the count.
    assert "[TRIGGER PAUSED]" in alerts[-1]
    assert "The 5 event(s) its failed runs could not deliver are kept" in alerts[-1]
    assert "discard them" in alerts[-1]  # R12: the way out of a poison event

    # Paused: no poll, no fire, parked events untouched.
    checks = feed.checks
    assert _poll_and_fire(manager) == 0
    assert feed.checks == checks
    assert _parked_subjects(tmp_path) == ["e1", "e2", "e3", "e4", "e5"]

    summary = manager.resume_trigger(USER, "trig-1")
    assert summary is not None and summary["parked_events"] == 5
    assert "5 event(s) from its failed runs will be retried" in describe_resume(
        "trig-1", summary
    )

    _poll_and_fire(manager)  # turns are out of failures: this one succeeds

    last = turns.succeeded[-1]
    positions = [last.index(f"Mail: e{i}") for i in range(1, 7)]
    assert positions == sorted(positions)
    assert _stored(tmp_path).failed_events == []


def test_pause_alert_with_parked_events_keeps_the_resume_command_in_app(
    tmp_path, source, alerts, monkeypatch
):
    """#406 behavior 17: a 200-character name, a long error and the parked
    events sentences together still leave the verdict and the resume command
    in the in-app row (the message cut at the cap); the full message keeps
    every sentence for external destinations."""
    source([_mail(f"e{i}") for i in range(1, 6)])
    manager = TriggerManager(tmp_path)
    _add(manager)
    long_name = ("Sweep the shared accounts mailbox for supplier invoices " * 4)[:200]
    with manager.atomic_update(USER) as store:
        store.triggers[0].name = long_name
    monkeypatch.setattr(manager, "_stream_live", _Turns(["relay refused: " + "x" * 300] * 5))

    for _ in range(5):
        _poll_and_fire(manager)

    message = alerts[-1]
    row = message[:NOTIFICATION_SUMMARY_MAX_CHARS]
    assert row.startswith("[TRIGGER PAUSED]")
    assert "stopped after 5 consecutive failed actions" in row
    assert "Fix the cause, then /triggers resume trig-1." in row
    assert "The 5 event(s) its failed runs could not deliver are kept" in message
    assert "discard them" in message
    assert f'Full name: "{long_name}".' in message
    assert f"Last error: {_stored(tmp_path).last_error}." in message


def test_trigger_views_show_the_parked_count(tmp_path, monkeypatch):
    manager = TriggerManager(tmp_path)
    monkeypatch.setattr(trigger_tools, "_get_trigger_manager", lambda: manager)
    _add(
        manager,
        failed_events=[ParkedTriggerEvent(event={"subject": "a"}), ParkedTriggerEvent(event={"subject": "b"})],
    )
    config = {"configurable": {"user_id": USER, "thread_id": "trig-thread"}}

    listing = trigger_tools._trigger_list(config=config)
    detail = trigger_tools._trigger_inspect(
        trigger_id="trig-1", action="detail", limit=10, config=config
    )

    assert "Failed, awaiting retry: 2 event(s)" in listing
    assert "Failed events awaiting retry: 2" in detail
    assert TriggerResponse.from_definition(_stored(tmp_path)).failed_events == 2


def test_discard_failed_drops_parked_events_and_nothing_else(tmp_path, monkeypatch):
    """R11: the escape hatch for an event that is itself what fails."""
    manager = TriggerManager(tmp_path)
    monkeypatch.setattr(trigger_tools, "_get_trigger_manager", lambda: manager)
    _add(
        manager,
        action_failures=5,
        auto_paused_at=utc_now(),
        failed_events=[ParkedTriggerEvent(event={"subject": s}, attempts=3) for s in "abc"],
    )
    config = {"configurable": {"user_id": USER, "thread_id": "trig-thread"}}

    detail = trigger_tools._trigger_inspect(
        trigger_id="trig-1", action="detail", limit=10, config=config
    )
    # R12: the detail view shows WHAT is parked, so the choice is informed.
    assert "- 3 failed attempt(s): subject: a" in detail
    assert 'trigger_config(action="discard_failed")' in detail

    out = trigger_tools.trigger_config.func(  # type: ignore[union-attr]
        action="discard_failed", trigger_id="trig-1", config=config
    )

    # The reply names what went, so the transcript keeps a record of it.
    assert out.splitlines() == [
        "[Success]: Discarded 3 failed event(s) from trigger trig-1. "
        "They will not be retried.",
        "  - subject: a",
        "  - subject: b",
        "  - subject: c",
    ]
    stored = _stored(tmp_path)
    assert stored.failed_events == []
    assert stored.auto_paused_at is not None  # discard is not resume
    assert stored.action_failures == 5
    again = trigger_tools.trigger_config.func(  # type: ignore[union-attr]
        action="discard_failed", trigger_id="trig-1", config=config
    )
    assert "no failed events waiting" in again
    missing = trigger_tools.trigger_config.func(  # type: ignore[union-attr]
        action="discard_failed", trigger_id="nope", config=config
    )
    assert missing.startswith("[Error]")


def test_detail_view_caps_the_parked_listing(tmp_path, monkeypatch):
    manager = TriggerManager(tmp_path)
    monkeypatch.setattr(trigger_tools, "_get_trigger_manager", lambda: manager)
    _add(
        manager,
        failed_events=[ParkedTriggerEvent(event={"subject": f"e{i}"}) for i in range(8)],
    )
    config = {"configurable": {"user_id": USER, "thread_id": "trig-thread"}}

    detail = trigger_tools._trigger_inspect(
        trigger_id="trig-1", action="detail", limit=10, config=config
    )

    assert "subject: e4" in detail and "subject: e5" not in detail
    assert "... and 3 more" in detail


def test_detail_names_an_outlook_event_by_subject_and_sender(tmp_path, monkeypatch):
    """The listing exists so a person can choose what to discard: an Outlook
    event leads with its opaque email_id, which names nothing."""
    manager = TriggerManager(tmp_path)
    monkeypatch.setattr(trigger_tools, "_get_trigger_manager", lambda: manager)
    event = {
        "email_id": "AAMkAGI2" + "x" * 120,
        "subject": "Invoice 42",
        "from_name": "Acme Billing",
        "from_address": "billing@acme.test",
        "body_preview": "Please find\nattached",
    }
    _add(manager, failed_events=[ParkedTriggerEvent(event=event, attempts=2)])
    config = {"configurable": {"user_id": USER, "thread_id": "trig-thread"}}

    detail = trigger_tools._trigger_inspect(
        trigger_id="trig-1", action="detail", limit=10, config=config
    )

    assert (
        "- 2 failed attempt(s): subject: Invoice 42; from_name: Acme Billing; "
        "from_address: billing@acme.test" in detail
    )
    assert "AAMkAGI2" not in detail


# ------------------------------------------------------------------
# R7: the retry bookkeeping never leaks
# ------------------------------------------------------------------


def test_a_forged_retry_key_in_an_event_is_plain_data(tmp_path, source, monkeypatch):
    """Events are source data (a webhook body is whatever its caller sent), so
    no key in one can pass it off as a retry or feed the bookkeeping."""
    forged = {"subject": "x", "_trigger_retry": {"attempts": 99, "first_failed_at": "x"}}
    source([[forged]])
    manager = TriggerManager(tmp_path)
    _add(manager)
    turns = _Turns(["error quoting SECRET-BODY-TEXT", None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)
    parked = _stored(tmp_path).failed_events
    assert [(p.event, p.attempts) for p in parked] == [(forged, 1)]
    _poll_and_fire(manager)

    assert turns.prompts[0] == "Mail: x"  # a first try, whatever the key says
    assert turns.prompts[1].startswith(
        "[Retry: delivering this trigger event failed before (1 attempt(s))."
    )
    # The retry says it IS one, never what the error said: an action's
    # error can quote external content.
    assert "SECRET-BODY-TEXT" not in turns.prompts[1]


def test_an_event_held_back_by_a_pause_is_delivered_as_a_first_try(
    tmp_path, source, monkeypatch
):
    """Held back before it ran (attempts 0), so it is not labelled a retry."""
    source([[]])
    manager = TriggerManager(tmp_path)
    _add(manager, failed_events=[ParkedTriggerEvent(event={"subject": "held"})])
    turns = _Turns([None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)

    assert turns.prompts == ["Mail: held"]
    assert _stored(tmp_path).failed_events == []


def test_attachments_are_not_kept_while_an_event_waits(tmp_path, source, monkeypatch):
    """An Outlook event carries each attachment base64-inline (up to 10 MB),
    and the trigger store is read and rewritten on every poll."""
    payload = "QUJD" * 50_000
    event = {
        "subject": "x",
        "attachments": [
            {"file_name": "invoice.pdf", "data_url": f"data:application/pdf;base64,{payload}"},
            {"file_name": "logo.png", "data_url": f"data:image/png;base64,{payload}"},
        ],
    }
    source([[event]])
    manager = TriggerManager(tmp_path)
    _add(manager)
    turns = _Turns(["turn crashed", None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)

    assert turns.attachments[0] is not None  # the first try had them
    raw = (tmp_path / "triggers" / f"{USER}.json").read_text()
    assert payload not in raw
    parked = _stored(tmp_path).failed_events
    assert parked[0].event == {"subject": "x"}
    assert parked[0].dropped_attachments == ["invoice.pdf", "logo.png"]

    _poll_and_fire(manager)

    assert turns.attachments[1] is None
    assert (
        "[Its attachments were not kept while it waited (invoice.pdf, logo.png); "
        "fetch them from the source if you need them.]" in turns.prompts[1]
    )


# ------------------------------------------------------------------
# R8: the cap
# ------------------------------------------------------------------


def test_parked_list_is_capped_oldest_dropped(
    tmp_path, source, monkeypatch, caplog
):
    total = MAX_FAILED_EVENTS + 50
    source([_mail(*(f"e{i}" for i in range(total)))])
    manager = TriggerManager(tmp_path)
    _add(manager)
    monkeypatch.setattr(manager, "_stream_live", _Turns(["kaput"]))

    with caplog.at_level(logging.WARNING, logger="nymeria.core.trigger_manager"):
        _poll_and_fire(manager)

    parked = _parked_subjects(tmp_path)
    assert len(parked) == MAX_FAILED_EVENTS
    assert parked[0] == "e50" and parked[-1] == f"e{total - 1}"  # newest kept
    assert any("dropped its 50 oldest" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------
# Busy thread: a retry is not lost to the busy-defer paths
# ------------------------------------------------------------------


class _BusyLocks:
    def is_thread_busy(self, thread_id):
        return True

    def get_lock_info(self, thread_id):
        return {"held_seconds": 3}


class _BusyAgent:
    _thread_locks = _BusyLocks()


@pytest.fixture
def isolated_queue():
    backend = create_pending_queue(None)
    set_pending_queue(backend)
    try:
        yield backend
    finally:
        reset_pending_queue_for_tests()


def test_busy_thread_keeps_retries_parked_and_queues_only_new_events(
    tmp_path, source, isolated_queue
):
    """The pending-prompt queue hands an event to a turn whose failure the
    trigger never sees, so a retry waits for an idle thread instead."""
    source([_mail("new")])
    manager = TriggerManager(tmp_path)
    _add(
        manager,
        failed_events=[
            ParkedTriggerEvent(event={"subject": "old"}, attempts=2, last_error="boom")
        ],
    )

    manager.check_triggers(USER, agent=_BusyAgent())  # type: ignore[arg-type]

    assert [p.message for p in isolated_queue.drain("trig-thread")] == ["Mail: new"]
    stored = _stored(tmp_path)
    assert [(p.event, p.attempts, p.last_error) for p in stored.failed_events] == [
        ({"subject": "old"}, 2, "boom")  # unchanged: it never ran
    ]


def test_busy_defer_fallback_keeps_retries_out_of_the_pending_cap(
    tmp_path, source, monkeypatch
):
    """The legacy defer keeps only the newest 50 pending events, and retried
    events go FIRST, so without their own list they are what the cap drops."""
    source([_mail(*(f"n{i}" for i in range(60)))])
    manager = TriggerManager(tmp_path)
    _add(manager, failed_events=[ParkedTriggerEvent(event={"subject": "old"}, attempts=1, last_error="boom")])
    monkeypatch.setattr(manager, "_enqueue_busy_trigger", lambda **kw: False)

    manager.check_triggers(USER, agent=_BusyAgent())  # type: ignore[arg-type]

    stored = _stored(tmp_path)
    assert [(p.event, p.attempts, p.last_error) for p in stored.failed_events] == [
        ({"subject": "old"}, 1, "boom")  # not re-counted: it never ran
    ]
    assert len(stored.pending_events) == 50
    assert stored.pending_events[0] == {"subject": "n10"}  # only fresh events


# ------------------------------------------------------------------
# Retry batches are bounded and keep their age order
# ------------------------------------------------------------------


def _aged(count: int, prefix: str = "e") -> list[ParkedTriggerEvent]:
    start = utc_now() - timedelta(hours=1)
    return [
        ParkedTriggerEvent(
            event={"subject": f"{prefix}{i}"},
            attempts=1,
            first_failed_at=start + timedelta(seconds=i),
        )
        for i in range(count)
    ]


def test_parked_events_are_handed_back_a_bounded_batch_at_a_time(
    tmp_path, source, monkeypatch
):
    """A resume after an outage must not send every parked event in ONE turn."""
    source([_mail("new")])
    manager = TriggerManager(tmp_path)
    _add(manager, failed_events=_aged(25))
    turns = _Turns([None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)

    fired = re.findall(r"Mail: (\w+)", turns.prompts[0])
    assert fired == [f"e{i}" for i in range(RETRY_BATCH_MAX)] + ["new"]
    assert _parked_subjects(tmp_path) == [f"e{i}" for i in range(RETRY_BATCH_MAX, 25)]


def test_a_failed_retry_batch_keeps_its_place_at_the_front(
    tmp_path, source, monkeypatch
):
    source([_mail("new")])
    manager = TriggerManager(tmp_path)
    _add(manager, failed_events=_aged(12))
    monkeypatch.setattr(manager, "_stream_live", _Turns(["still down"]))

    _poll_and_fire(manager)

    parked = _stored(tmp_path).failed_events
    # Oldest first: the failed batch, then the two it was taken ahead of,
    # then this poll's new event.
    assert [p.event["subject"] for p in parked] == [f"e{i}" for i in range(12)] + ["new"]
    assert [p.attempts for p in parked] == [2] * 10 + [1, 1, 1]


# ------------------------------------------------------------------
# Parked events wait while the trigger does not poll
# ------------------------------------------------------------------


def _source_fails(manager, source):
    feed = source([])

    def broken(*args, **kwargs):
        raise RuntimeError("feed down")

    feed.check = broken  # type: ignore[method-assign]


def _cooling_down(manager, source):
    source([])
    with manager.atomic_update(USER) as store:
        store.triggers[0].cooldown_seconds = 3600
        store.triggers[0].last_fired = utc_now()


def _disabled(manager, source):
    source([])
    with manager.atomic_update(USER) as store:
        store.triggers[0].enabled = False


@pytest.mark.parametrize(
    "hold", [_source_fails, _cooling_down, _disabled], ids=lambda f: f.__name__
)
def test_parked_events_wait_while_the_trigger_does_not_poll(
    tmp_path, source, monkeypatch, hold
):
    manager = TriggerManager(tmp_path)
    _add(manager, failed_events=_aged(3))
    hold(manager, source)
    turns = _Turns([None])
    monkeypatch.setattr(manager, "_stream_live", turns)

    _poll_and_fire(manager)

    assert turns.prompts == []
    assert _parked_subjects(tmp_path) == ["e0", "e1", "e2"]


def test_a_store_written_before_parking_existed_loads(tmp_path):
    manager = TriggerManager(tmp_path)
    _add(manager)
    path = tmp_path / "triggers" / f"{USER}.json"
    raw = json.loads(path.read_text())
    del raw["triggers"][0]["failed_events"]
    path.write_text(json.dumps(raw))

    assert TriggerManager(tmp_path).get_trigger(USER, "trig-1").failed_events == []  # type: ignore[union-attr]


# ------------------------------------------------------------------
# What counts as a failed delivery
# ------------------------------------------------------------------


class _NotifyStub:
    def __init__(self, result: str):
        self.result = result
        self.calls = 0

    def invoke(self, args, config=None):
        self.calls += 1
        return self.result


@pytest.mark.parametrize(
    ("result", "fails"),
    [
        ("[Error]: profile 'default' delivered nothing - telegram: 502", True),
        ("[Success]: delivered to telegram", False),
        ("[Logged]: notification recorded in the in-app feed; no destinations", False),
    ],
)
def test_a_notify_that_delivered_nothing_is_a_failed_action(
    tmp_path, source, monkeypatch, result, fails
):
    """The notify tool reports a total delivery failure as a string, never a
    raise, so it used to read as a success: never counted, never kept."""
    # The package re-exports the tool under the module's own name, so the
    # attribute route would hand back the tool, not the module.
    notify_module = importlib.import_module("nymeria.tools.notify")
    stub = _NotifyStub(result)
    monkeypatch.setattr(notify_module, "notify", stub)
    source([_mail("x")])
    manager = TriggerManager(tmp_path)
    _add(manager, action_type="notify")

    _poll_and_fire(manager)

    stored = _stored(tmp_path)
    assert stub.calls == 1
    assert _parked_subjects(tmp_path) == (["x"] if fails else [])
    assert stored.action_failures == (1 if fails else 0)


def test_a_completion_publish_that_raises_does_not_park_delivered_events(
    tmp_path, source, monkeypatch
):
    import nymeria.core.event_bus as event_bus_module

    def broken_publish(*args, **kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus_module, "publish_autonomous_event", broken_publish)
    source([_mail("a", "b")])
    manager = TriggerManager(tmp_path)
    _add(manager)
    monkeypatch.setattr(manager, "_stream_live", _Turns([None]))

    _poll_and_fire(manager)

    stored = _stored(tmp_path)
    assert stored.failed_events == []
    assert stored.action_failures == 0
