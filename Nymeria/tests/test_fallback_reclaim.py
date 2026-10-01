"""#439: a fallback hold ends (or is offered back) once the primary recovers.

Turn-level tests drive the REAL ``NymeriaAgent.astream``/``chat`` with the
stub-agent pattern from ``test_agent_turn_loops.py``, a real
``ThreadLockManager`` (so the turn's own lock marks the thread busy, exactly
the condition that hid an expired hold from the idle-only eviction) and a
real ``ThreadConfigManager`` on ``tmp_path``. The graph lookup is the faked
seam: it records what the REAL resolver returns at that moment, which is the
model the turn would build against. The probe is faked at the ``create_llm``
factory seam (the request itself is the only thing not exercised), and the
clock is the module's ``_now`` seam.

Behaviors (plan it36, section 6) and where they are pinned: eligibility and
exclusions (refusal, invalid_request, offered, too young, interval 0), the
backoff ladder (doubling, cap, 429/401/403 to the cap, success resets, the
flap rule), single-flight and verdict reuse across threads, credential
separation, the end/offer policy (auto ends a timed hold; ask mode and
permanent holds get one offer), hold-on-hold, probe_invalid, non-blocking
turns, revert-wins, no mid-turn reclaim, the probe's CLIProxy shape, the
expired-hold eviction (D5, proven red on the unfixed code first), status.

Edges named and skipped: a provider factory that ignores request_timeout
(the stale in-flight replacement is a 120s wall-clock path; covered by
reading, not by a test); a restart resetting route state (in-memory by
design); the agent reaching the reclaim (there is no tool or command to
reach it with; ``/fallback revert`` agent-blocking is pinned in
test_command_service).
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import json
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core import fallback_reclaim as reclaim
from nymeria.core.agent_history import format_conversation_history
from nymeria.core.agent_llm_config import (
    clear_active_llm_fallback,
    get_llm_config_for_thread,
    release_fallback_for_config_write,
)
from nymeria.core.event_bus import agent_stream_chunk_to_autonomous_event_data
from nymeria.core.fallback_approvals import reclaim_action
from nymeria.core.pending_prompt_queue import (
    InMemoryPendingPromptQueue,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.thread_config import (
    ActiveLLMFallback,
    ThreadConfig,
    ThreadConfigManager,
    ThreadLLMConfig,
)
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.core.time_utils import utc_now
from nymeria.vendor.react_agent import providers as providers_module
from nymeria.vendor.react_agent.cliproxy import CLIPROXY_BILLING_SYSTEM_BLOCK
from test_agent_turn_loops import _FakeAsyncGraph, _stream_agent
from test_llm_config_resolution import _Settings

THREAD = "reclaim-thread"
PRIMARY_PROVIDER = "anthropic"
PRIMARY_MODEL = "claude-sonnet-4-6"  # _Settings' global model
HELD_PROVIDER = "anthropic"
HELD_MODEL = "claude-haiku-4-5-20251001"
RECOVERED_NOTE = (
    "[System info]: The fallback hold on this thread ended because the primary "
    "model is answering again; the thread is back on its primary model "
    f"({PRIMARY_MODEL}). {HELD_MODEL} handled the conversation since the switch."
)
LONG = timedelta(hours=12)


class _TurnSettings(_Settings):
    lock_timeout = 1
    context_management = "none"
    llm_fallback_switch_mode = "auto"
    llm_refusal_swap_mode = "ask"
    llm_fallback_prompt_timeout_seconds = 180
    llm_fallback_reclaim_interval_seconds = 600


def _hold(
    *,
    age: timedelta = timedelta(minutes=30),
    remaining: timedelta | None = timedelta(minutes=60),
    reason: str | None = "provider_server_error",
    source_model: str = PRIMARY_MODEL,
    offered: bool = False,
    activated_at: Any = None,
) -> ActiveLLMFallback:
    now = utc_now()
    activated = activated_at if activated_at is not None else now - age
    return ActiveLLMFallback(
        provider=HELD_PROVIDER,
        model=HELD_MODEL,
        source_provider=PRIMARY_PROVIDER,
        source_model=source_model,
        hold_seconds=0 if remaining is None else 5400,
        activated_at=activated,
        expires_at=None if remaining is None else activated + age + remaining,
        reason=reason,
        http_status=502,
        reclaim_offered_at=(now - timedelta(minutes=1)) if offered else None,
    )


class _StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


class ConnectError(Exception):
    """Named like httpx's transport error (the classifier keys on names)."""


class _Probe:
    """Fake factory at the ``create_llm`` seam: records every probe config
    and message list, answers with the scripted outcome (``ok``, an
    exception, or per-credential outcomes), optionally blocking on a gate."""

    def __init__(self) -> None:
        self.configs: list[Any] = []
        self.messages: list[Any] = []
        self.outcome: Any = "ok"
        self.by_key: dict[str, Any] = {}
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        self._lock = threading.Lock()

    def create_llm(self, cfg: Any) -> Any:
        with self._lock:
            self.configs.append(cfg)
        return SimpleNamespace(invoke=lambda messages: self._invoke(cfg, messages))

    def _invoke(self, cfg: Any, messages: Any) -> AIMessage:
        with self._lock:
            self.messages.append(messages)
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10), "probe gate never released"
        outcome = self.by_key.get(cfg.api_key, self.outcome)
        if isinstance(outcome, BaseException):
            raise outcome
        return AIMessage(content="ok")

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.configs)


class _Clock:
    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self):
        return self.now

    def advance(self, **delta: Any) -> None:
        self.now += timedelta(**delta)


@pytest.fixture(autouse=True)
def _fresh_reclaim_state():
    reclaim.reset_reclaim_state_for_tests()
    yield
    reclaim.reset_reclaim_state_for_tests()


@pytest.fixture
def probe(monkeypatch) -> _Probe:
    fake = _Probe()
    monkeypatch.setattr(providers_module, "create_llm", fake.create_llm)
    return fake


@pytest.fixture
def inline(monkeypatch) -> None:
    """Run each background check to completion inside the settle call."""
    monkeypatch.setattr(reclaim, "_spawn", lambda target: target())


@pytest.fixture
def spawned(monkeypatch) -> list[threading.Thread]:
    """Run each background check on a real thread the test can join."""
    threads: list[threading.Thread] = []

    def spawn(target: Any) -> None:
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        thread.start()

    monkeypatch.setattr(reclaim, "_spawn", spawn)
    return threads


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(reclaim, "_now", fake)
    return fake


class _Turn:
    """One agent, one data dir, the real lock and config managers."""

    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        agent: Any = _stream_agent(THREAD)
        agent._thread_locks = ThreadLockManager()
        agent._chat_turn_local = threading.local()
        agent._fire_done_observe_sync = lambda **kwargs: None
        agent._compaction = SimpleNamespace(note_turn_end=lambda *args: None)
        agent.thread_config_manager = ThreadConfigManager(tmp_path)
        agent.settings = _TurnSettings()
        for key, value in settings.items():
            setattr(agent.settings, key, value)
        self.invalidated: list[str] = []
        agent.invalidate_thread_config_cache = self.invalidated.append
        self.agent = agent
        # What the turn's graph lookup saw: the persisted hold and the model
        # the REAL resolver hands the graph build.
        self.at_lookup: list[dict[str, Any]] = []
        self.graphs: list[_FakeAsyncGraph] = []

    def seed(self, thread_id: str = THREAD, **fields: Any) -> None:
        assert self.agent.thread_config_manager.save_config(
            ThreadConfig(thread_id=thread_id, **fields)
        )

    def saved(self, thread_id: str = THREAD) -> ThreadConfig | None:
        return self.agent.thread_config_manager.get_config(thread_id)

    def hold(self, thread_id: str = THREAD) -> ActiveLLMFallback | None:
        tc = self.saved(thread_id)
        return tc.active_llm_fallback if tc is not None else None

    def held(self, thread_id: str = THREAD) -> ActiveLLMFallback:
        hold = self.hold(thread_id)
        assert hold is not None, "no hold persisted"
        return hold

    def config(self, thread_id: str = THREAD) -> ThreadConfig:
        tc = self.saved(thread_id)
        assert tc is not None, "no thread config persisted"
        return tc

    def reclaimed(self, thread_id: str = THREAD) -> dict[str, Any]:
        event = self.settle(thread_id)
        assert event is not None, "no reclaim event"
        return event

    def settle(self, thread_id: str = THREAD, *, offers: bool = True):
        return reclaim.settle_hold_at_turn_start(
            self.agent, thread_id, "owner", offers=offers
        )

    def _record_lookup(self) -> None:
        tc = self.saved()
        config = get_llm_config_for_thread(self.agent, THREAD)
        self.at_lookup.append(
            {
                "hold": tc.active_llm_fallback if tc is not None else None,
                "note": tc.pending_fallback_note if tc is not None else None,
                "model": (config.provider, config.model),
            }
        )

    def astream(self, message: str = "hello") -> list[dict[str, Any]]:
        async def events():
            yield {
                "event": "on_chat_model_end",
                "data": {"output": AIMessage(content="ok")},
            }

        def lookup(*args: Any, **kwargs: Any) -> _FakeAsyncGraph:
            self._record_lookup()
            graph = _FakeAsyncGraph(events)
            self.graphs.append(graph)
            return graph

        self.agent._get_async_graph_for_user = lookup
        set_pending_queue(InMemoryPendingPromptQueue())
        try:

            async def collect() -> list[dict[str, Any]]:
                chunks = [
                    chunk
                    async for chunk in self.agent.astream(
                        message, thread_id=THREAD, user_id="owner"
                    )
                ]
                from nymeria.core.embedding_jobs import (
                    wait_for_pending_embedding_jobs,
                )

                await wait_for_pending_embedding_jobs()
                return chunks

            return asyncio.run(collect())
        finally:
            reset_pending_queue_for_tests()

    def chat(self, message: str = "hello") -> None:
        """Drive the sync turn up to its graph lookup, then stop it there
        (everything past the lookup is the unchanged turn body)."""

        class _StopAtLookup(Exception):
            pass

        def lookup(*args: Any, **kwargs: Any) -> Any:
            self._record_lookup()
            raise _StopAtLookup()

        self.agent._get_graph_for_user = lookup
        set_pending_queue(InMemoryPendingPromptQueue())
        try:
            with pytest.raises(_StopAtLookup):
                self.agent.chat(message, thread_id=THREAD, user_id="owner")
        finally:
            reset_pending_queue_for_tests()

    def prompt_message(self) -> HumanMessage:
        """The HumanMessage the graph was driven with (checkpointed as-is)."""
        input_state = self.graphs[-1].stream_inputs[0][0]
        return input_state["messages"][-1]

    def prompt(self) -> str:
        return str(self.prompt_message().content)


def _reclaims(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "fallback_hold_reclaimed"]


# -- D5: an expired hold no longer drives one more turn ------------------------


def test_astream_evicts_an_expired_hold_before_the_graph_builds(tmp_path):
    turn = _Turn(tmp_path)
    turn.seed(
        active_llm_fallback=_hold(
            age=timedelta(minutes=65), remaining=timedelta(minutes=-5)
        )
    )

    turn.astream()

    seen = turn.at_lookup[0]
    assert seen["hold"] is None
    assert seen["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    # The end note rides THIS turn's prompt (consumed at prompt build).
    assert turn.prompt().endswith(
        "[System info]: The fallback hold on this thread expired; the thread is "
        f"back on its primary model ({PRIMARY_MODEL}). {HELD_MODEL} handled the "
        "conversation since the switch."
    )
    saved = turn.saved()
    assert saved is None or saved.pending_fallback_note is None


def test_chat_evicts_an_expired_hold_before_the_graph_builds(tmp_path):
    turn = _Turn(tmp_path)
    turn.seed(
        active_llm_fallback=_hold(
            age=timedelta(minutes=65), remaining=timedelta(minutes=-5)
        )
    )

    turn.chat()

    seen = turn.at_lookup[0]
    assert seen["hold"] is None
    assert seen["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert seen["note"]["reason"] == "expired"
    assert seen["note"]["to_model"] == PRIMARY_MODEL


def test_expired_eviction_runs_even_with_reclaim_off(tmp_path, probe):
    turn = _Turn(tmp_path, llm_fallback_reclaim_interval_seconds=0)
    turn.seed(
        active_llm_fallback=_hold(
            age=timedelta(minutes=65), remaining=timedelta(minutes=-5)
        )
    )

    turn.astream()

    assert turn.at_lookup[0]["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert probe.count == 0


def test_a_live_hold_still_drives_the_turn(tmp_path):
    turn = _Turn(tmp_path, llm_fallback_reclaim_interval_seconds=0)
    turn.seed(active_llm_fallback=_hold())

    turn.astream()

    seen = turn.at_lookup[0]
    assert seen["hold"] is not None
    assert seen["model"] == (HELD_PROVIDER, HELD_MODEL)
    assert "[System info]" not in turn.prompt()


# -- 1 and 2: probe the configured primary, end an auto hold at the next turn --


def test_an_eligible_hold_probes_the_primary_once_and_this_turn_stays_held(
    tmp_path, probe, inline
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())

    events = turn.astream()

    assert probe.count == 1
    sent = probe.configs[0]
    assert (sent.provider, sent.model) == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert [m.content for m in probe.messages[0]] == ["Reply with ok."]
    # Non-blocking (M3): the verdict applies at the NEXT turn start.
    assert turn.at_lookup[0]["model"] == (HELD_PROVIDER, HELD_MODEL)
    assert _reclaims(events) == []
    assert turn.hold() is not None


def test_a_healthy_primary_ends_an_auto_hold_at_the_next_turn_start(
    tmp_path, probe, inline
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # probes; the verdict is recorded

    events = turn.astream()

    # The event streams first, before any graph work.
    assert events[0] == {
        "type": "fallback_hold_reclaimed",
        "thread_id": THREAD,
        "reason": "recovered",
        "from_provider": HELD_PROVIDER,
        "from_model": HELD_MODEL,
        "to_provider": PRIMARY_PROVIDER,
        "to_model": PRIMARY_MODEL,
        "expires_at": events[0]["expires_at"],
        "permanent": False,
        "outcome": "ended",
    }
    assert len(_reclaims(events)) == 1
    seen = turn.at_lookup[-1]
    assert seen["hold"] is None
    assert seen["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert THREAD in turn.invalidated  # the cached graph is dropped
    assert turn.prompt().endswith(RECOVERED_NOTE)
    assert probe.count == 1  # the verdict is reused, not re-probed
    # History renders the persisted note as a typed notice.
    history = format_conversation_history(
        [turn.prompt_message(), AIMessage(content="ok", id="a1")], thread_id=THREAD
    )
    notice = next(e for e in history if e.get("kind") == "fallback_notice")
    assert notice["content"] == (
        "Fallback hold ended (primary recovered); this thread is back on "
        f"{PRIMARY_MODEL}."
    )

    # The next turn is ordinary: no event, no probe, no repeated note.
    events = turn.astream()
    assert _reclaims(events) == []
    assert probe.count == 1
    assert "[System info]" not in turn.prompt()


def test_a_sync_turn_probes_and_a_later_sync_turn_ends_the_hold(
    tmp_path, probe, inline
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())

    turn.chat()
    assert probe.count == 1
    assert turn.at_lookup[0]["model"] == (HELD_PROVIDER, HELD_MODEL)

    turn.chat()
    seen = turn.at_lookup[1]
    assert seen["hold"] is None
    assert seen["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert seen["note"]["reason"] == "recovered"
    assert seen["note"]["text"] == RECOVERED_NOTE


def test_a_resume_turn_never_settles_the_hold(tmp_path, probe, inline):
    """A resume adds no prompt, so an end note would have nowhere to ride:
    the settle skips it entirely (the hold stays exactly as it was)."""
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # healthy verdict recorded

    async def resume() -> list[dict[str, Any]]:
        set_pending_queue(InMemoryPendingPromptQueue())
        try:
            turn.agent._get_async_graph_for_user = lambda *a, **k: _FakeAsyncGraph(
                lambda: iter(())
            )
            return [
                chunk
                async for chunk in turn.agent.astream(
                    "", thread_id=THREAD, user_id="owner", _resume_halted_turn=True
                )
            ]
        finally:
            reset_pending_queue_for_tests()

    events = asyncio.run(resume())

    assert _reclaims(events) == []
    assert turn.hold() is not None


# -- 3 and 4: an unhealthy or undue probe changes nothing -------------------------


def test_a_failed_probe_leaves_the_turn_unchanged_and_backs_off(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    seeded = _hold(remaining=LONG)
    turn.seed(active_llm_fallback=seeded)
    probe.outcome = _StatusError(503)

    first = turn.astream()
    second = turn.astream()

    assert probe.count == 1  # backing off: no second probe
    for events in (first, second):
        assert events == [{"type": "response", "content": "ok"}]
    assert [seen["model"] for seen in turn.at_lookup] == [(HELD_PROVIDER, HELD_MODEL)] * 2
    assert "[System info]" not in turn.prompt()
    # No new or re-armed hold: the persisted record is byte-identical.
    assert turn.held().model_dump() == seeded.model_dump()

    clock.advance(seconds=2 * 600 - 1)
    turn.astream()
    assert probe.count == 1
    clock.advance(seconds=2)
    turn.astream()
    assert probe.count == 2


def _probe_times(turn: _Turn, clock: _Clock, probe: _Probe, *, minutes: int) -> list[int]:
    """Settle once a minute for ``minutes``; the minutes at which a probe
    was sent, relative to the start."""
    sent: list[int] = []
    for minute in range(minutes):
        before = probe.count
        turn.settle()
        if probe.count > before:
            sent.append(minute)
        clock.advance(minutes=1)
    return sent


def test_the_backoff_doubles_from_the_interval_and_caps_at_an_hour(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    probe.outcome = ConnectError("connection refused")

    # 600s interval: 20 min, 40 min, then the 60 min cap, and it stays there.
    assert _probe_times(turn, clock, probe, minutes=241) == [0, 20, 60, 120, 180, 240]


@pytest.mark.parametrize("status", [429, 401, 403])
def test_quota_and_auth_failures_jump_straight_to_the_cap(
    tmp_path, probe, inline, clock, status
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    probe.outcome = _StatusError(status)

    assert _probe_times(turn, clock, probe, minutes=121) == [0, 60, 120]


def test_a_healthy_probe_resets_the_ladder(tmp_path, probe, inline, clock):
    turn = _Turn(tmp_path)
    # Ask mode keeps the hold through a healthy verdict (an offer), so the
    # route keeps being exercised by OTHER threads after it.
    turn.seed(active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    probe.outcome = ConnectError("down")
    turn.settle()  # t=0, failure 1 -> due at 20 min
    clock.advance(minutes=20)
    probe.outcome = "ok"
    turn.settle()  # t=20, healthy -> due at 30 min, failures reset
    assert probe.count == 2
    probe.outcome = ConnectError("down again")
    other = "reclaim-other"
    turn.seed(other, active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    clock.advance(minutes=10)
    turn.settle(other)  # t=30: due (interval after the healthy probe)
    assert probe.count == 3
    # One failure after a reset backs off 2x the interval again, not 4x.
    clock.advance(minutes=19)
    turn.seed("reclaim-third", active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    turn.settle("reclaim-third")
    assert probe.count == 3
    clock.advance(minutes=2)
    turn.settle("reclaim-third")
    assert probe.count == 4


@pytest.mark.parametrize(
    "hold, interval, label",
    [
        (_hold(age=timedelta(minutes=5)), 600, "younger than the interval"),
        (_hold(), 0, "interval 0 is off"),
        (_hold(offered=True), 600, "already offered"),
        (_hold(reason="refusal"), 600, "refusal hold"),
        (_hold(reason="invalid_request"), 600, "invalid_request hold"),
    ],
)
def test_ineligible_holds_are_never_probed(tmp_path, probe, inline, hold, interval, label):
    turn = _Turn(tmp_path, llm_fallback_reclaim_interval_seconds=interval)
    turn.seed(active_llm_fallback=hold)

    events = turn.astream()
    turn.astream()

    assert probe.count == 0, label
    assert _reclaims(events) == [], label
    assert turn.hold() is not None, label


@pytest.mark.parametrize("reason", [None, "auth_error", "rate_limited", "timeout"])
def test_transport_health_reasons_and_legacy_holds_are_probed(
    tmp_path, probe, inline, reason
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(reason=reason))

    turn.astream()

    assert probe.count == 1


# -- 5 and 6: one probe per route, shared verdicts, credentials kept apart ---------


def test_concurrent_checks_on_one_route_share_one_probe(
    tmp_path, probe, spawned, monkeypatch
):
    joined = threading.Semaphore(0)

    class _WatchedFuture(concurrent.futures.Future):
        def result(self, timeout=None):
            joined.release()
            return super().result(timeout)

    monkeypatch.setattr(reclaim, "_FUTURE_FACTORY", _WatchedFuture)
    turn = _Turn(tmp_path)
    threads = [f"reclaim-n{i}" for i in range(4)]
    for thread_id in threads:
        turn.seed(thread_id, active_llm_fallback=_hold())
    probe.gate = threading.Event()

    turn.settle(threads[0])
    assert probe.started.wait(5)
    for thread_id in threads[1:]:
        turn.settle(thread_id)
    for _ in threads[1:]:
        assert joined.acquire(timeout=5), "a check probed instead of joining"
    probe.gate.set()
    for thread in spawned:
        thread.join(5)

    assert probe.count == 1
    # Every thread got the one verdict: each ends at its next turn start.
    for thread_id in threads:
        event = turn.settle(thread_id)
        assert event is not None and event["outcome"] == "ended", thread_id
        assert turn.hold(thread_id) is None


def test_a_fresh_route_verdict_is_reused_but_never_for_a_newer_hold(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    turn.seed("reclaim-a", active_llm_fallback=_hold())
    turn.settle("reclaim-a")
    assert probe.count == 1

    # A hold that predates the verdict reuses it: no request. (Not applied
    # here: an applied reclaim would make the hold below a flap.)
    turn.seed("reclaim-b", active_llm_fallback=_hold(age=timedelta(minutes=40)))
    turn.settle("reclaim-b")
    assert probe.count == 1
    status = _status("reclaim-b", turn.hold("reclaim-b"), _TurnSettings())
    assert status["state"] == "recovered"

    # A hold that formed AFTER the verdict proves the route failed since: the
    # old verdict is not reused, and it waits for the route to be due.
    clock.advance(minutes=1)
    turn.seed(
        "reclaim-c",
        active_llm_fallback=_hold(activated_at=clock.now - timedelta(seconds=30)),
    )
    clock.advance(minutes=8)  # the hold is now 8.5 min old: still too young
    assert turn.settle("reclaim-c") is None
    clock.advance(minutes=2)  # 10.5 min old and the route is due (10 min)
    turn.settle("reclaim-c")
    assert probe.count == 2


def test_a_healthy_route_verdict_older_than_the_hold_is_never_reused(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    turn.seed(
        "reclaim-a",
        active_llm_fallback=_hold(activated_at=clock.now - timedelta(minutes=30)),
    )
    turn.settle("reclaim-a")  # healthy now; the route is next due in 10 minutes
    assert probe.count == 1
    # The operator shortens the interval, and a hold forms on the same route
    # AFTER that verdict: it is old enough to check before the route is due.
    turn.agent.settings.llm_fallback_reclaim_interval_seconds = 60
    clock.advance(seconds=30)
    turn.seed("reclaim-c", active_llm_fallback=_hold(activated_at=clock.now))
    clock.advance(minutes=2)

    assert turn.settle("reclaim-c") is None
    assert probe.count == 1  # the route is not due: no request

    # The older healthy verdict proves nothing about a failure after it, so
    # nothing was recorded for this hold and nothing applies at the next turn.
    status = _status("reclaim-c", turn.held("reclaim-c"), turn.agent.settings)
    assert status["state"] == "scheduled"
    assert status["last_verdict"] is None
    assert turn.settle("reclaim-c") is None
    assert turn.hold("reclaim-c") is not None


def test_different_credentials_are_different_routes(tmp_path, probe, inline):
    turn = _Turn(tmp_path)
    for thread_id, key in (("reclaim-k1", "sk-user-one"), ("reclaim-k2", "sk-user-two")):
        turn.seed(
            thread_id,
            llm_config=ThreadLLMConfig(api_key=key),
            active_llm_fallback=_hold(),
        )
    probe.by_key = {"sk-user-one": "ok", "sk-user-two": _StatusError(503)}

    turn.settle("reclaim-k1")
    turn.settle("reclaim-k2")

    assert sorted(c.api_key for c in probe.configs) == ["sk-user-one", "sk-user-two"]
    assert turn.reclaimed("reclaim-k1")["outcome"] == "ended"
    assert turn.settle("reclaim-k2") is None
    assert turn.hold("reclaim-k2") is not None


# -- 7: ask mode and permanent holds are offered once, never ended ------------------


@pytest.mark.parametrize(
    "mode, permanent",
    [("ask", False), ("auto", True), ("ask", True)],
)
def test_ask_mode_and_permanent_holds_get_one_offer_and_keep_the_hold(
    tmp_path, probe, inline, mode, permanent
):
    turn = _Turn(tmp_path)
    seeded = _hold(remaining=None if permanent else timedelta(minutes=60))
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode=mode),
        active_llm_fallback=seeded,
    )
    turn.astream()  # probe

    events = turn.astream()

    offers = _reclaims(events)
    assert len(offers) == 1
    assert offers[0]["outcome"] == "offered"
    assert offers[0]["to_model"] == PRIMARY_MODEL
    assert offers[0]["from_model"] == HELD_MODEL
    assert offers[0]["permanent"] is permanent
    if permanent:
        assert offers[0]["expires_at"] is None
    else:
        assert seeded.expires_at is not None
        assert offers[0]["expires_at"] == seeded.expires_at.isoformat()
    # The hold stays, the turn runs it, and nothing is said to the model.
    assert turn.at_lookup[-1]["model"] == (HELD_PROVIDER, HELD_MODEL)
    assert "[System info]" not in turn.prompt()
    hold = turn.hold()
    assert hold is not None and hold.reclaim_offered_at is not None
    assert hold.activated_at == seeded.activated_at

    # Never probed or offered again.
    events = turn.astream()
    assert _reclaims(events) == []
    assert probe.count == 1

    # The user's revert still ends it (reason reverted).
    tc = turn.config()
    assert tc.llm_config is not None
    release_fallback_for_config_write(
        tc, before_llm=tc.llm_config.model_copy(), settings=turn.agent.settings, revert=True
    )
    turn.agent.thread_config_manager.save_config(tc)
    turn.astream()
    assert turn.at_lookup[-1]["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert "was manually reverted" in turn.prompt()


def test_a_sync_turn_defers_the_offer_to_the_next_streaming_turn(
    tmp_path, probe, inline
):
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(),
    )
    turn.chat()  # probe
    turn.chat()  # healthy, but chat() cannot show an offer
    assert turn.held().reclaim_offered_at is None

    events = turn.astream()

    assert [e["outcome"] for e in _reclaims(events)] == ["offered"]
    assert turn.held().reclaim_offered_at is not None


def test_reclaim_action_policy():
    assert reclaim_action(switch_mode="auto", permanent=False) == "end"
    assert reclaim_action(switch_mode=None, permanent=False) == "end"
    assert reclaim_action(switch_mode="ask", permanent=False) == "offer"
    assert reclaim_action(switch_mode="auto", permanent=True) == "offer"
    assert reclaim_action(switch_mode="ask", permanent=True) == "offer"


# -- 9 and 11: hold-on-hold, probe_invalid ------------------------------------------


def test_a_hold_on_hold_probes_and_names_the_configured_primary(
    tmp_path, probe, inline
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(source_model="intermediate-fallback"))
    turn.astream()

    events = turn.astream()

    assert [c.model for c in probe.configs] == [PRIMARY_MODEL]
    assert _reclaims(events)[0]["to_model"] == PRIMARY_MODEL
    assert turn.prompt().endswith(RECOVERED_NOTE)
    assert "intermediate-fallback" not in turn.prompt()


@pytest.mark.parametrize("status", [400, 422])
def test_a_rejected_probe_stops_probing_that_hold_and_never_reclaims(
    tmp_path, probe, inline, clock, caplog, status
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    probe.outcome = _StatusError(status)

    with caplog.at_level("WARNING", logger="nymeria.core.fallback_reclaim"):
        turn.astream()
    for _ in range(3):
        clock.advance(hours=2)
        probe.outcome = "ok"  # even a later healthy primary: this hold is done
        events = turn.astream()
        assert _reclaims(events) == []

    assert probe.count == 1
    assert turn.hold() is not None
    warnings = [
        r
        for r in caplog.records
        if r.name == "nymeria.core.fallback_reclaim" and r.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "rejected the probe request itself" in warnings[0].getMessage()


# -- 12 and 13: the turn never waits; a revert wins ---------------------------------


def test_the_turn_never_waits_for_the_probe(tmp_path, probe, spawned):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    probe.gate = threading.Event()

    first = turn.astream()  # returns while the probe is still blocked
    assert probe.started.wait(5)
    assert not probe.gate.is_set()
    assert first == [{"type": "response", "content": "ok"}]

    second = turn.astream()  # still in flight: no second probe, no event
    assert _reclaims(second) == []
    assert probe.count == 1

    probe.gate.set()
    for thread in spawned:
        thread.join(5)
    third = turn.astream()
    assert [e["outcome"] for e in _reclaims(third)] == ["ended"]


def test_a_revert_after_the_verdict_wins(tmp_path, probe, inline):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # healthy verdict recorded
    tc = turn.config()
    release_fallback_for_config_write(
        tc, before_llm=None, settings=turn.agent.settings, revert=True
    )
    turn.agent.thread_config_manager.save_config(tc)

    events = turn.astream()

    assert _reclaims(events) == []
    assert "was manually reverted" in turn.prompt()
    assert "answering again" not in turn.prompt()


def test_a_revert_racing_the_settle_wins(tmp_path, probe, inline, monkeypatch):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.settle()  # healthy verdict recorded
    manager = turn.agent.thread_config_manager
    real_get = manager.get_config
    stale = real_get(THREAD).model_copy(deep=True)  # what the settle reads
    reverted = real_get(THREAD)
    release_fallback_for_config_write(
        reverted, before_llm=None, settings=turn.agent.settings, revert=True
    )
    manager.save_config(reverted)  # the revert lands right after that read
    reads = iter([stale])
    monkeypatch.setattr(
        manager, "get_config", lambda thread_id: next(reads, None) or real_get(thread_id)
    )

    # The settle saw the hold, but the pinned clear finds it gone: no reclaim
    # is reported and the revert's own end note stands.
    assert turn.settle() is None
    assert turn.hold() is None
    note = str(turn.config().pending_fallback_note)
    assert "reverted" in note
    assert "recovered" not in note


def test_a_verdict_never_applies_to_a_newer_hold(tmp_path, probe, inline):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # healthy verdict for THIS hold
    newer = _hold(age=timedelta(seconds=30))  # reverted, then failed again
    turn.seed(active_llm_fallback=newer)

    events = turn.astream()

    assert _reclaims(events) == []
    assert turn.held().activated_at == newer.activated_at
    assert probe.count == 1  # and the new hold is too young to probe


def test_an_eligible_newer_hold_is_probed_afresh_not_ended_by_the_old_verdict(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(activated_at=clock.now - timedelta(minutes=30)))
    turn.astream()  # healthy verdict for THIS hold, recorded at clock.now
    # Reverted, then the primary failed again a minute later; by the next
    # turn the new hold is old enough to check, and the old verdict is still
    # inside its cap window.
    newer = _hold(activated_at=clock.now + timedelta(minutes=1))
    turn.seed(active_llm_fallback=newer)
    clock.advance(minutes=15)

    events = turn.astream()

    # The old hold's verdict predates this hold's failure: no reclaim now,
    # one fresh probe for the new hold instead.
    assert _reclaims(events) == []
    assert turn.held().activated_at == newer.activated_at
    assert probe.count == 2
    status = _status(THREAD, turn.held(), turn.agent.settings)
    assert status["state"] == "recovered"  # applies at the NEXT turn


def test_the_pinned_clear_refuses_a_different_hold(tmp_path):
    turn = _Turn(tmp_path)
    seeded = _hold()
    turn.seed(active_llm_fallback=seeded)

    assert (
        clear_active_llm_fallback(
            turn.agent,
            THREAD,
            reason="recovered",
            expected_activated_at=seeded.activated_at - timedelta(seconds=1),
        )
        is None
    )
    assert turn.hold() is not None
    assert turn.config().pending_fallback_note is None


# -- 14 and 18: no mid-turn reclaim; only the turn start settles ---------------------


def test_resolution_and_graph_rebuilds_never_apply_a_verdict(tmp_path, probe, inline):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.settle()  # healthy verdict recorded

    # A mid-turn graph rebuild resolves config; it must not end the hold.
    config = get_llm_config_for_thread(turn.agent, THREAD)

    assert (config.provider, config.model) == (HELD_PROVIDER, HELD_MODEL)
    assert turn.hold() is not None


def _calls_in(tree: ast.AST, name: str) -> list[str]:
    """Names of the functions in ``tree`` whose bodies call ``name``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, (ast.Attribute, ast.Name))
                    and (getattr(inner.func, "attr", None) or getattr(inner.func, "id", None))
                    == name
                ):
                    found.append(node.name)
                    break
    return found


def test_only_the_two_turn_starts_settle_a_hold():
    """The seam lives in chat() and astream() (both TurnExecutor shapes end
    there, in the API process) and nowhere a mid-turn rebuild passes."""
    package = Path(reclaim.__file__).resolve().parents[1]
    agent_tree = ast.parse((package / "core" / "agent.py").read_text(encoding="utf-8"))
    assert sorted(_calls_in(agent_tree, "_settle_fallback_hold_at_turn_start")) == [
        "astream",
        "chat",
    ]
    callers = {
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "settle_hold_at_turn_start" in path.read_text(encoding="utf-8")
    }
    assert callers == {"core/agent.py", "core/fallback_reclaim.py"}


# -- 15: the probe's shape on the wire ----------------------------------------------


def test_the_probe_config_is_tiny_and_bare(tmp_path, probe, inline):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())

    turn.astream()

    sent = probe.configs[0]
    assert sent.max_tokens == 64
    assert sent.extended_thinking is False
    assert sent.reasoning_effort == "off"
    assert sent.request_timeout == 10
    assert sent.stream_max_retries == 0
    assert sent.fallbacks == []
    assert sent.fallback_activation_callback is None
    assert sent.fallback_decision_callback is None
    assert sent.prompt_cache_key is None
    assert sent.custom_llm is None


def test_an_always_thinking_claude_gets_room_to_answer():
    from nymeria.vendor.react_agent.config import LLMConfig

    cfg = reclaim.probe_config(
        LLMConfig(provider="anthropic", model="claude-opus-5-5", api_key="k")
    )

    assert cfg.max_tokens == 2048
    assert cfg.extended_thinking is False


def test_a_cliproxy_probe_carries_the_treatment_and_no_cache_control(
    tmp_path, inline, monkeypatch
):
    real_create_llm = providers_module.create_llm
    built: list[Any] = []
    payloads: list[dict[str, Any]] = []

    def spy(cfg: Any) -> Any:
        llm = real_create_llm(cfg)
        built.append(llm)

        def invoke(messages: Any) -> AIMessage:
            payloads.append(getattr(llm, "_get_request_payload")(messages))
            return AIMessage(content="ok")

        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(providers_module, "create_llm", spy)
    turn = _Turn(tmp_path, llm_base_url="http://cli-proxy-api:8317")
    turn.seed(active_llm_fallback=_hold())

    turn.astream()

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["system"][0] == CLIPROXY_BILLING_SYSTEM_BLOCK
    assert "cache_control" not in json.dumps(payload)
    assert "tools" not in payload
    assert payload["max_tokens"] == 64
    assert payload["messages"] == [{"role": "user", "content": "Reply with ok."}]
    headers = built[0].default_headers
    assert headers["User-Agent"].startswith("claude-cli")
    assert "oauth-2025-04-20" in headers["Anthropic-Beta"]
    assert built[0].max_retries == 0


# -- 16: flap damping ----------------------------------------------------------------


def test_a_hold_reforming_soon_after_a_reclaim_starts_the_route_at_the_cap(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.settle()  # t=0 probe, healthy
    assert turn.reclaimed()["outcome"] == "ended"  # reclaimed at t=0
    # The primary fails again five minutes later: a new hold forms.
    turn.seed(
        active_llm_fallback=_hold(
            activated_at=clock.now + timedelta(minutes=5), remaining=timedelta(hours=30)
        )
    )

    clock.advance(minutes=16)  # the new hold is 11 min old: eligible
    turn.settle()
    clock.advance(minutes=48)  # t=64 min: before activated + 60 min cap
    turn.settle()
    assert probe.count == 1
    clock.advance(minutes=2)  # t=66 min: past the cap
    turn.settle()
    assert probe.count == 2


# -- copy, events, status ------------------------------------------------------------


def test_the_recovered_end_note_and_history_copy():
    from nymeria.vendor.react_agent import nodes

    stamp = nodes.fallback_note_stamp(
        {"from_model": HELD_MODEL, "to_model": PRIMARY_MODEL, "reason": "recovered"},
        kind="transport",
        phase="end",
    )

    assert stamp["text"] == RECOVERED_NOTE
    human = HumanMessage(
        content=f"next\n\n{stamp['text']}",
        additional_kwargs={"fallback_note": stamp},
        id="h1",
    )
    history = format_conversation_history(
        [human, AIMessage(content="ok", id="a1")], thread_id="t1"
    )
    notice = next(entry for entry in history if entry.get("kind") == "fallback_notice")
    assert notice["content"] == (
        f"Fallback hold ended (primary recovered); this thread is back on {PRIMARY_MODEL}."
    )
    assert history[0]["content"] == "next"


def test_the_event_is_mirrored_for_autonomous_turns():
    chunk = {"type": "fallback_hold_reclaimed", "thread_id": "t", "outcome": "ended"}

    assert agent_stream_chunk_to_autonomous_event_data(chunk) == (
        "fallback_hold_reclaimed",
        {"thread_id": "t", "outcome": "ended"},
    )


@pytest.mark.parametrize(
    "exc, verdict",
    [
        (_StatusError(400), "probe_invalid"),
        (_StatusError(422), "probe_invalid"),
        (_StatusError(429), "rate_limited"),
        (_StatusError(401), "auth_error"),
        (_StatusError(403), "auth_error"),
        (_StatusError(503), "provider_server_error"),
        (_StatusError(404), "retryable_http_error"),
        (ConnectError("refused"), "transport_error"),
        (ConnectError("timed out"), "timeout"),
        (ValueError("Unknown provider: x"), "transient_provider_error"),
    ],
)
def test_probe_failures_classify_in_the_hold_taxonomy(exc, verdict):
    assert reclaim.classify_probe_failure(exc) == verdict


def _status(thread_id: str, hold: Any, settings: Any) -> dict[str, Any]:
    status = reclaim.reclaim_status(thread_id, hold, settings)
    assert status is not None
    return status


def test_reclaim_status_states(tmp_path, probe, inline, clock):
    settings = _TurnSettings()
    assert reclaim.reclaim_status(THREAD, None, settings) is None
    assert _status(THREAD, _hold(offered=True), settings)["state"] == "offered"
    assert _status(THREAD, _hold(reason="refusal"), settings)["state"] == "excluded"
    off = _TurnSettings()
    off.llm_fallback_reclaim_interval_seconds = 0
    assert _status(THREAD, _hold(), off)["state"] == "off"

    young = _hold(age=timedelta(minutes=4))
    status = _status(THREAD, young, settings)
    assert status["state"] == "scheduled"
    assert status["next_check_at"] == (young.activated_at + timedelta(seconds=600)).isoformat()

    turn = _Turn(tmp_path)
    held = _hold(remaining=timedelta(hours=30))
    turn.seed(active_llm_fallback=held)
    probe.outcome = _StatusError(429)
    turn.settle()
    status = _status(THREAD, turn.hold(), settings)
    assert status["state"] == "scheduled"
    assert status["last_verdict"] == "rate_limited"
    assert status["next_check_at"] == (clock.now + timedelta(hours=1)).isoformat()

    probe.outcome = "ok"
    clock.advance(hours=1, seconds=1)
    turn.settle()
    assert _status(THREAD, turn.hold(), settings)["state"] == "recovered"


def test_a_settle_fault_leaves_the_turn_on_its_hold(tmp_path, monkeypatch):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("config store unreadable")

    monkeypatch.setattr(reclaim, "_settle", boom)
    events = turn.astream()

    assert events == [{"type": "response", "content": "ok"}]
    assert turn.at_lookup[0]["model"] == (HELD_PROVIDER, HELD_MODEL)


# -- the API executor shape: the /chat route relays the event -------------------------


def test_the_chat_route_relays_the_event_and_mirrors_it(tmp_path, probe, inline):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nymeria.api.routers.chat import create_chat_router
    from test_api_chat_publish_gating import _PublishRecorder, _sse_events

    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # probe: healthy verdict recorded
    agent = turn.agent
    agent.thread_metadata_manager = SimpleNamespace(auto_title=lambda *a, **k: None)
    agent.accounts_repo = SimpleNamespace(get_thread_owner=lambda thread_id: None)
    agent.get_context_stats = lambda thread_id: {}

    async def events():
        yield {"event": "on_chat_model_end", "data": {"output": AIMessage(content="ok")}}

    agent._get_async_graph_for_user = lambda *a, **k: _FakeAsyncGraph(events)
    recorder = _PublishRecorder()
    router = create_chat_router(
        verify_api_key=lambda: SimpleNamespace(id="owner"),
        get_agent_fn=lambda: agent,
        get_settings_fn=lambda: SimpleNamespace(),
        require_thread_access_fn=lambda user, thread_id: None,
        publish_sync_event_fn=recorder.publish_sync_event,
        publish_agent_stream_chunk_fn=recorder.publish_agent_stream_chunk,
        publish_autonomous_event_fn=recorder.publish_autonomous_event,
        create_autonomous_notification_fn=recorder.create_autonomous_notification,
        should_notify_autonomous_fn=recorder.should_notify_autonomous,
    )
    app = FastAPI()
    app.include_router(router)
    set_pending_queue(InMemoryPendingPromptQueue())
    try:
        with TestClient(app).stream(
            "POST",
            "/chat",
            json={"message": "wake", "thread_id": THREAD, "is_self_invoke": True},
        ) as response:
            body = "".join(response.iter_text())
    finally:
        reset_pending_queue_for_tests()

    relayed = [e for e in _sse_events(body) if e.get("type") == "fallback_hold_reclaimed"]
    assert [e["outcome"] for e in relayed] == ["ended"]
    mirrored = [
        c for c in recorder.stream_chunks if c.get("type") == "fallback_hold_reclaimed"
    ]
    assert [c["outcome"] for c in mirrored] == ["ended"]
    assert turn.hold() is None
