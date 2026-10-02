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

Review fixes pinned here too: the hold's recorded origin decides the
policy (legacy holds follow the mode), an offer is spent only on a turn that
can show it (and is never probed for elsewhere), single flight without joins,
verdict freshness by probe SEND time, a hung probe discarded, a changed
configured route probed afresh, ``/resume`` evicting an expired hold, an
exhausted output budget reading healthy, and our own faults never reading as
a provider verdict.

Edges named and skipped: a restart resetting route state (in-memory by
design); the agent reaching the reclaim (there is no tool or command to
reach it with; ``/fallback revert`` agent-blocking is pinned in
test_command_service).
"""

from __future__ import annotations

import ast
import asyncio
import json
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core import bot_reactions
from nymeria.core import fallback_reclaim as reclaim
from nymeria.core.agent_history import format_conversation_history
from nymeria.core.agent_llm_config import (
    clear_active_llm_fallback,
    get_llm_config_for_thread,
    release_fallback_for_config_write,
)
from nymeria.core.event_bus import agent_stream_chunk_to_autonomous_event_data
from nymeria.core.fallback_approvals import reclaim_action, reclaim_offer_renders
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
OTHER_MODEL = "claude-opus-4-1"
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
    origin: str | None = None,
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
        hold_origin=origin,
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

    def astream(
        self,
        message: str = "hello",
        *,
        offer_surface: bool = False,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """One streaming turn. ``offer_surface`` is the client declaration
        (``ChatRequest.supports_reclaim_offers``, as the CLI sends it); a
        bare turn is a desktop or mobile one (no declaration, no bot
        origin)."""

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
                        message,
                        thread_id=THREAD,
                        user_id="owner",
                        source=source,
                        _reclaim_offer_surface=offer_surface,
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


@contextmanager
def _origin(platform: str, thread_id: str = THREAD):
    """A chat-bot turn: the chat route stamps the turn-origin registry from
    the request's ``platform_origin`` before the turn starts."""
    bot_reactions.set_turn_origin(
        thread_id, platform=platform, channel_id="c1", message_id="m1"
    )
    try:
        yield
    finally:
        bot_reactions.clear_turn_origin(thread_id)


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


def _resume(turn: _Turn) -> list[dict[str, Any]]:
    """A ``/resume`` re-drive: no prompt, the graph re-entered as is."""

    async def resume() -> list[dict[str, Any]]:
        set_pending_queue(InMemoryPendingPromptQueue())
        try:

            def lookup(*args: Any, **kwargs: Any) -> _FakeAsyncGraph:
                turn._record_lookup()
                return _FakeAsyncGraph(lambda: iter(()))

            turn.agent._get_async_graph_for_user = lookup
            return [
                chunk
                async for chunk in turn.agent.astream(
                    "",
                    thread_id=THREAD,
                    user_id="owner",
                    _resume_halted_turn=True,
                    _reclaim_offer_surface=True,
                )
            ]
        finally:
            reset_pending_queue_for_tests()

    return asyncio.run(resume())


def test_a_resume_turn_never_applies_or_probes_a_live_hold(tmp_path, probe, inline):
    """A resume adds no prompt, so an end note would have nowhere to ride:
    a LIVE hold stays exactly as it was (no reclaim applied, no probe)."""
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # healthy verdict recorded

    events = _resume(turn)

    assert _reclaims(events) == []
    assert turn.hold() is not None
    assert turn.at_lookup[-1]["model"] == (HELD_PROVIDER, HELD_MODEL)
    assert probe.count == 1


def test_a_resume_turn_evicts_an_expired_hold_and_the_note_waits(tmp_path, probe, inline):
    """An EXPIRED hold is evicted at a resume too (red before the review
    fix: the resumed turn ran the held model): the resumed graph builds on
    the primary, and the end note stays latched for the next prompted turn
    (a resume has no prompt to carry it)."""
    turn = _Turn(tmp_path)
    turn.seed(
        active_llm_fallback=_hold(
            age=timedelta(minutes=65), remaining=timedelta(minutes=-5)
        )
    )

    _resume(turn)

    seen = turn.at_lookup[0]
    assert seen["hold"] is None
    assert seen["model"] == (PRIMARY_PROVIDER, PRIMARY_MODEL)
    assert seen["note"]["reason"] == "expired"
    assert probe.count == 0
    # The next prompted turn carries the note, once.
    turn.astream()
    assert "[System info]: The fallback hold on this thread expired" in turn.prompt()
    turn.astream()
    assert "[System info]" not in turn.prompt()


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
    # This thread's healthy verdict is never applied here (no later settle on
    # it). OTHER threads keep exercising the route; their holds form AFTER
    # that verdict, so it proves nothing for them and they probe when due.
    turn.seed(active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    probe.outcome = ConnectError("down")
    turn.settle()  # t=0, failure 1 -> due at 20 min
    clock.advance(minutes=20)
    probe.outcome = "ok"
    turn.settle()  # t=20, healthy -> due at 30 min, failures reset
    assert probe.count == 2
    probe.outcome = ConnectError("down again")
    other = "reclaim-other"
    turn.seed(
        other,
        active_llm_fallback=_hold(
            activated_at=clock.now + timedelta(minutes=1), remaining=timedelta(hours=30)
        ),
    )
    clock.advance(minutes=11)
    turn.settle(other)  # t=31: due since t=30, other's hold is 10 min old
    assert probe.count == 3
    # One failure after a reset backs off 2x the interval again, not 4x:
    # due at t=51.
    turn.seed(
        "reclaim-third",
        active_llm_fallback=_hold(
            activated_at=clock.now - timedelta(minutes=9), remaining=timedelta(hours=30)
        ),
    )
    clock.advance(minutes=19)
    turn.settle("reclaim-third")  # t=50
    assert probe.count == 3
    clock.advance(minutes=2)
    turn.settle("reclaim-third")  # t=52
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


def test_concurrent_checks_on_one_route_share_one_probe(tmp_path, probe, spawned):
    """Single flight per route, no joins: a thread whose route already has a
    probe in flight starts none and does not wait on it; it reads the
    route's verdict at its next turn start."""
    turn = _Turn(tmp_path)
    threads = [f"reclaim-n{i}" for i in range(4)]
    for thread_id in threads:
        turn.seed(thread_id, active_llm_fallback=_hold())
    probe.gate = threading.Event()

    turn.settle(threads[0])
    assert probe.started.wait(5)
    for thread_id in threads[1:]:
        assert turn.settle(thread_id) is None
    for check in spawned[1:]:
        check.join(2)
        assert not check.is_alive(), "a check waited on another thread's probe"
    assert probe.count == 1
    # Still in flight: a second turn start on a waiting thread sends nothing.
    assert turn.settle(threads[1]) is None
    probe.gate.set()
    spawned[0].join(5)

    assert probe.count == 1
    # Every thread reads the one verdict: each ends at its next turn start.
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


def test_a_probe_sent_before_the_hold_formed_is_never_reused(
    tmp_path, probe, spawned, clock
):
    """Freshness is judged by when the probe was SENT, not when it finished:
    a probe in flight while another thread's hold formed predates that
    hold's failure even though its verdict lands after it (red before the
    review fix, which judged by completion time)."""
    turn = _Turn(tmp_path)
    turn.seed("reclaim-a", active_llm_fallback=_hold(activated_at=clock.now - timedelta(minutes=30)))
    probe.gate = threading.Event()
    turn.settle("reclaim-a")  # probe sent at t=0
    assert probe.started.wait(5)
    turn.seed("reclaim-b", active_llm_fallback=_hold(activated_at=clock.now + timedelta(seconds=1)))
    clock.advance(seconds=5)
    probe.gate.set()  # healthy, finished at t=5: after b formed
    for check in spawned:
        check.join(5)

    clock.advance(seconds=597)  # b is 601 s old; the route is due at 605 s
    assert turn.settle("reclaim-b") is None
    for check in spawned:
        check.join(5)
    assert turn.settle("reclaim-b") is None
    assert turn.hold("reclaim-b") is not None
    assert probe.count == 1
    status = _status("reclaim-b", turn.held("reclaim-b"), turn.agent.settings)
    assert (status["state"], status["last_verdict"]) == ("scheduled", None)

    clock.advance(seconds=5)  # due: b gets its own probe
    turn.settle("reclaim-b")
    for check in spawned:
        check.join(5)
    assert probe.count == 2
    assert turn.reclaimed("reclaim-b")["outcome"] == "ended"


def test_a_hung_probe_is_discarded_and_never_leaves_status_checking(
    tmp_path, probe, spawned, clock
):
    """A probe whose factory ignores the request timeout hangs: past the
    timeout plus a margin it is discarded, the status stops saying
    "checking", and a later turn start probes again (red before the review
    fix: the owning thread stayed "checking" until restart)."""
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    probe.gate = threading.Event()
    try:
        turn.settle()
        assert probe.started.wait(5)
        assert _status(THREAD, turn.held(), turn.agent.settings)["state"] == "checking"
        clock.advance(seconds=reclaim.PROBE_TIMEOUT_SECONDS)
        assert turn.settle() is None  # still within the margin: no second probe
        assert probe.count == 1

        clock.advance(seconds=reclaim._INFLIGHT_STALE_SECONDS - reclaim.PROBE_TIMEOUT_SECONDS + 1)
        assert _status(THREAD, turn.held(), turn.agent.settings)["state"] == "scheduled"
        turn.settle()
        for _ in range(100):
            if probe.count == 2:
                break
            threading.Event().wait(0.02)
        assert probe.count == 2
    finally:
        probe.gate.set()
        for check in spawned:
            check.join(5)
    # Both probes finish healthy; the route's verdict applies as usual.
    assert turn.reclaimed()["outcome"] == "ended"


class _Scripted:
    """``create_llm`` fake answering the n-th probe with ``steps[n]``: an
    optional gate it blocks on, then its outcome ("ok" or an exception)."""

    def __init__(self, steps: list[tuple[threading.Event | None, Any]]) -> None:
        self.steps = steps
        self.started = [threading.Event() for _ in steps]
        self.calls = 0
        self._lock = threading.Lock()

    def create_llm(self, cfg: Any) -> Any:
        with self._lock:
            index = self.calls
            self.calls += 1
        gate, outcome = self.steps[index]

        def invoke(messages: Any) -> AIMessage:
            self.started[index].set()
            if gate is not None:
                assert gate.wait(10), "probe gate never released"
            if isinstance(outcome, BaseException):
                raise outcome
            return AIMessage(content="ok")

        return SimpleNamespace(invoke=invoke)


def _start(target: Any) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_a_discarded_probe_answering_late_never_overwrites_the_newer_verdict(
    tmp_path, monkeypatch, clock
):
    """A hung probe that answers healthy only AFTER a newer probe (sent
    later) found the primary down is the older evidence: the newer verdict
    stands and no turn start reclaims on the late answer."""
    gate = threading.Event()
    scripted = _Scripted([(gate, "ok"), (None, _StatusError(503))])
    monkeypatch.setattr(providers_module, "create_llm", scripted.create_llm)
    pending: list[Any] = []
    monkeypatch.setattr(reclaim, "_spawn", pending.append)
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    try:
        turn.settle()
        hung = _start(pending.pop())
        assert scripted.started[0].wait(5)
        clock.advance(seconds=reclaim._INFLIGHT_STALE_SECONDS + 1)
        turn.settle()
        _start(pending.pop()).join(5)  # the newer probe: 503
        assert scripted.calls == 2
    finally:
        gate.set()
    hung.join(5)
    assert not hung.is_alive()

    assert _status(THREAD, turn.held(), turn.agent.settings)["last_verdict"] == (
        "provider_server_error"
    )
    assert turn.settle() is None
    assert turn.hold() is not None
    assert pending == []  # the route backs off: nothing new to check


def test_a_discarded_check_finishing_late_leaves_the_newer_check_in_charge(
    tmp_path, monkeypatch, clock
):
    """A hung check that finishes after the thread started a newer one never
    clears the newer one's flag: the status keeps saying "checking" and no
    turn start applies or starts anything around it until the newer check
    is done."""
    gate = threading.Event()
    scripted = _Scripted([(gate, "ok")])
    monkeypatch.setattr(providers_module, "create_llm", scripted.create_llm)
    pending: list[Any] = []
    monkeypatch.setattr(reclaim, "_spawn", pending.append)
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    try:
        turn.settle()
        hung = _start(pending.pop())
        assert scripted.started[0].wait(5)
        clock.advance(seconds=reclaim._INFLIGHT_STALE_SECONDS + 1)
        turn.settle()
        newer = pending.pop()  # registered, not yet running
    finally:
        gate.set()
    hung.join(5)
    assert not hung.is_alive()

    assert _status(THREAD, turn.held(), turn.agent.settings)["state"] == "checking"
    assert turn.settle() is None
    assert pending == []

    _start(newer).join(5)
    # The late answer was fresh evidence: the newer check had nothing to
    # probe, and the next turn start acts on it.
    assert scripted.calls == 1
    assert turn.reclaimed()["outcome"] == "ended"


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


def test_a_changed_configured_route_is_probed_afresh_never_reclaimed_on_the_old_verdict(
    tmp_path, probe, inline
):
    """A healthy verdict is about the route that was probed. When the
    configured route moves before the next turn (an operator's global
    ``/model``, which never touches a hold), that verdict proves nothing
    about the new primary: no reclaim onto a never-probed model, and a fresh
    probe of the new route instead (the configured-route fingerprint)."""
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # the configured primary answers
    assert [c.model for c in probe.configs] == [PRIMARY_MODEL]

    def status() -> dict[str, Any]:
        tc = turn.agent.thread_config_manager.get_config(THREAD)
        return _status(THREAD, turn.hold(), turn.agent.settings, tc.llm_config)

    assert status()["state"] == "recovered"

    turn.agent.settings.llm_model = OTHER_MODEL
    # /fallback status agrees: the healthy verdict was about the old route.
    assert (status()["state"], status()["last_verdict"]) == ("scheduled", None)
    events = turn.astream()

    assert _reclaims(events) == []
    assert turn.hold() is not None
    assert turn.at_lookup[-1]["model"] == (HELD_PROVIDER, HELD_MODEL)
    assert [c.model for c in probe.configs] == [PRIMARY_MODEL, OTHER_MODEL]

    # The new route's own verdict applies at the next turn start.
    events = turn.astream()
    assert [(e["outcome"], e["to_model"]) for e in _reclaims(events)] == [
        ("ended", OTHER_MODEL)
    ]
    assert turn.at_lookup[-1]["model"] == (PRIMARY_PROVIDER, OTHER_MODEL)


# -- 7: holds a human chose (and legacy ask-mode holds) are offered once ------------


@pytest.mark.parametrize(
    "mode, permanent, origin",
    [
        ("ask", False, None),  # legacy hold: follows the ask mode
        ("auto", True, None),  # permanent: only a human picks one
        ("ask", True, None),
        ("auto", False, "user"),  # approved at an ask prompt, mode since moved
        ("auto", True, "user"),
    ],
)
def test_holds_a_human_chose_get_one_offer_and_keep_the_hold(
    tmp_path, probe, inline, mode, permanent, origin
):
    turn = _Turn(tmp_path)
    seeded = _hold(remaining=None if permanent else timedelta(minutes=60), origin=origin)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode=mode),
        active_llm_fallback=seeded,
    )
    turn.astream(offer_surface=True)  # probe

    events = turn.astream(offer_surface=True)

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
    events = turn.astream(offer_surface=True)
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


def test_an_automatic_hold_ends_even_on_an_ask_mode_thread(tmp_path, probe, inline):
    """The recorded origin decides (review fix): nobody chose an automatic
    hold (an unanswered prompt, a turn that could not park), so it ends
    when the primary answers, even on an ask-mode thread. Red before the
    fix, which keyed on the mode alone and offered it."""
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(origin="automatic"),
    )
    turn.astream()  # probe (an end needs no surface that can show an offer)

    events = turn.astream()

    assert [e["outcome"] for e in _reclaims(events)] == ["ended"]
    assert turn.hold() is None
    assert turn.prompt().endswith(RECOVERED_NOTE)


def test_a_turn_that_cannot_show_an_offer_neither_probes_for_it_nor_spends_it(
    tmp_path, probe, inline
):
    """A desktop or mobile turn (no declaration, no bot origin) cannot show
    the offer until #468: it neither probes for one (a verdict nobody can
    see only goes stale) nor stamps it. Red before the review fix: the
    second turn spent the one offer invisibly."""
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(),
    )
    for _ in range(2):
        assert _reclaims(turn.astream()) == []
    assert probe.count == 0
    assert turn.held().reclaim_offered_at is None


@pytest.mark.parametrize("platform", ["telegram", "discord", "slack", "whatsapp", "teams"])
def test_a_pending_offer_waits_for_a_bot_turn_that_can_show_it(
    tmp_path, probe, inline, platform
):
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(),
    )
    with _origin(platform):
        turn.astream()  # a bot turn probes
    assert probe.count == 1
    # A desktop turn and an autonomous turn on the bot's thread: nothing spent.
    assert _reclaims(turn.astream()) == []
    with _origin(platform):
        assert _reclaims(turn.astream(source="trigger")) == []
    assert turn.held().reclaim_offered_at is None
    status = _status(THREAD, turn.held(), turn.agent.settings, turn.config().llm_config)
    assert (status["state"], status["action"]) == ("offer_pending", "offer")

    with _origin(platform):
        events = turn.astream()

    assert [e["outcome"] for e in _reclaims(events)] == ["offered"]
    assert turn.held().reclaim_offered_at is not None
    assert probe.count == 1


def test_a_sync_turn_never_probes_for_or_spends_an_offer(tmp_path, probe, inline):
    """chat() cannot stream an offer: on an offer hold it sends no probe
    (red before the review fix, which re-probed every cap for an offer it
    could never deliver) and never stamps one a CLI turn recorded."""
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(),
    )
    turn.chat()
    with _origin("telegram"):  # a bot's /chat/sync fallback: still no stream
        turn.chat()
    assert probe.count == 0

    turn.astream(offer_surface=True)  # the CLI probes
    assert probe.count == 1
    turn.chat()  # ready, but a sync turn cannot show it
    assert turn.held().reclaim_offered_at is None

    events = turn.astream(offer_surface=True)

    assert [e["outcome"] for e in _reclaims(events)] == ["offered"]
    assert turn.held().reclaim_offered_at is not None


def test_an_offer_racing_a_revert_is_never_stamped_or_streamed(
    tmp_path, probe, inline, monkeypatch
):
    """The offer path re-reads before stamping: a revert landing between the
    settle's read and the stamp wins (no event, nothing stamped, the
    revert's own note stands)."""
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(),
    )
    turn.settle()  # healthy verdict recorded
    manager = turn.agent.thread_config_manager
    real_get = manager.get_config
    stale = real_get(THREAD).model_copy(deep=True)  # what the settle reads
    reverted = real_get(THREAD)
    release_fallback_for_config_write(
        reverted, before_llm=None, settings=turn.agent.settings, revert=True
    )
    manager.save_config(reverted)
    reads = iter([stale])
    monkeypatch.setattr(
        manager, "get_config", lambda thread_id: next(reads, None) or real_get(thread_id)
    )

    assert turn.settle() is None
    tc = turn.config()
    assert tc.active_llm_fallback is None
    assert "reverted" in str(tc.pending_fallback_note)


def test_reclaim_action_policy():
    # Legacy holds (no recorded origin) follow the mode.
    assert reclaim_action(switch_mode="auto", permanent=False) == "end"
    assert reclaim_action(switch_mode=None, permanent=False) == "end"
    assert reclaim_action(switch_mode="ask", permanent=False) == "offer"
    # Permanent: always a human's choice.
    assert reclaim_action(switch_mode="auto", permanent=True) == "offer"
    assert reclaim_action(switch_mode="ask", permanent=True) == "offer"
    assert reclaim_action(switch_mode="auto", permanent=True, origin="automatic") == "offer"
    # The recorded origin wins over the current mode.
    assert reclaim_action(switch_mode="auto", permanent=False, origin="user") == "offer"
    assert reclaim_action(switch_mode="ask", permanent=False, origin="user") == "offer"
    assert reclaim_action(switch_mode="ask", permanent=False, origin="automatic") == "end"
    assert reclaim_action(switch_mode="auto", permanent=False, origin="automatic") == "end"


def test_offer_surfaces_are_gated_like_the_consent_park(monkeypatch):
    def offers(**kwargs: Any) -> bool:
        return reclaim_offer_renders(THREAD, **kwargs)

    human = {"is_autonomous": False, "holder_kind": "user"}
    # No origin, no declaration: a GUI turn.
    assert offers(**human) is False
    assert offers(**human, declared=True) is True
    for platform, expected in (
        ("telegram", True),
        ("discord", True),
        ("slack", True),
        ("whatsapp", True),
        ("teams", True),
        ("twitch", False),  # its handler discards text
        ("somethingnew", False),
    ):
        with _origin(platform):
            assert offers(**human) is expected, platform
            # Never on a turn no human drives, declared or not.
            assert offers(is_autonomous=True, holder_kind="user", declared=True) is False
            assert offers(is_autonomous=False, holder_kind="callable") is False
            assert offers(is_autonomous=False, holder_kind="mcp", declared=True) is False

    def boom(thread_id: str) -> Any:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(bot_reactions, "get_turn_origin", boom)
    assert offers(**human) is False  # a fault cannot spend the offer


def test_a_consented_hold_records_its_origin(tmp_path):
    """The origin rides the consent decision into the persisted hold through
    the same merge every switch site uses (``llm_fallback_hold_overrides``):
    "user" for an approval (a permanent pick included), "automatic" for an
    auto swap or an unanswered prompt."""
    from nymeria.core.agent_llm_config import activate_temporary_llm_fallback
    from nymeria.vendor.react_agent import nodes

    payload = {
        "from_provider": PRIMARY_PROVIDER,
        "from_model": PRIMARY_MODEL,
        "to_provider": HELD_PROVIDER,
        "to_model": HELD_MODEL,
        "reason": "provider_server_error",
    }
    cases = [
        ({"action": "swap", "hold_origin": "user", "hold_seconds": 600}, "user"),
        ({"action": "swap", "hold_origin": "user", "hold_permanent": True}, "user"),
        ({"action": "swap", "hold_origin": "user"}, "user"),
        ({"action": "swap"}, "automatic"),  # an ask prompt that timed out
        ({"action": "auto"}, "automatic"),
    ]
    for decision, origin in cases:
        turn = _Turn(tmp_path / origin / str(len(decision)))
        merged = nodes._payload_with_hold(payload, decision)
        activate_temporary_llm_fallback(turn.agent, THREAD, merged)
        assert turn.held().hold_origin == origin, decision


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
    assert sent.request_timeout == 30
    assert sent.stream_max_retries == 0
    assert sent.fallbacks == []
    assert sent.fallback_activation_callback is None
    assert sent.fallback_decision_callback is None
    assert sent.prompt_cache_key is None
    assert sent.custom_llm is None


@pytest.mark.parametrize(
    "model, max_tokens, thinking, effort",
    [
        # Always-on thinking (no "off" tier): adaptive at the lowest effort,
        # with room for it plus the answer (the factory's thinking warning
        # threshold). A ceiling, not a charge.
        ("claude-opus-5-5", 2048, {"type": "adaptive", "display": "summarized"}, "low"),
        # Thinking can be off: a bare 64-token answer.
        (PRIMARY_MODEL, 64, None, None),
    ],
)
def test_the_real_claude_probe_request_sizes_its_budget(
    tmp_path, inline, monkeypatch, model, max_tokens, thinking, effort
):
    """Built by the REAL factory through the turn-start check, the request
    object the SDK would send (not just the config)."""
    real_create_llm = providers_module.create_llm
    payloads: list[dict[str, Any]] = []

    def spy(cfg: Any) -> Any:
        llm = real_create_llm(cfg)

        def invoke(messages: Any) -> AIMessage:
            payloads.append(getattr(llm, "_get_request_payload")(messages))
            return AIMessage(content="ok")

        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(providers_module, "create_llm", spy)
    turn = _Turn(tmp_path, llm_model=model, llm_base_url="http://cli-proxy-api:8317")
    turn.seed(active_llm_fallback=_hold())

    turn.astream()

    (payload,) = payloads
    assert payload["model"] == model
    assert payload["max_tokens"] == max_tokens
    assert payload.get("thinking") == thinking
    assert (payload.get("output_config") or {}).get("effort") == effort
    assert payload["system"][0] == CLIPROXY_BILLING_SYSTEM_BLOCK


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


def _caused_by(exc: BaseException, cause: BaseException) -> BaseException:
    exc.__cause__ = cause
    return exc


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
        # langchain raises ValueError for a provider's error payload: a
        # provider answer, so it stays in the provider taxonomy.
        (ValueError("response error: upstream failed"), "transient_provider_error"),
        # Our own faults (no HTTP status): never a provider verdict.
        (TypeError("create() got an unexpected keyword argument 'x'"), "probe_error"),
        (AttributeError("'NoneType' object has no attribute 'content'"), "probe_error"),
        (KeyError("choices"), "probe_error"),
        # ...but a client fault while handling a provider's 503 still carries
        # that answer: the HTTP status in the chain decides.
        (_caused_by(KeyError("error"), _StatusError(503)), "provider_server_error"),
    ],
)
def test_probe_failures_classify_in_the_hold_taxonomy(exc, verdict):
    assert reclaim.classify_probe_failure(exc) == verdict


def _sdk_error(cls: Any, status: int, message: str, body: Any = None) -> Any:
    import httpx

    request = httpx.Request("POST", "http://upstream.invalid/v1/x")
    return cls(
        message,
        response=httpx.Response(status, request=request),
        body=body if body is not None else {"error": {"message": message}},
    )


class _Reworded(Exception):
    """An SDK release that rewords the error keeps its class name."""


_Reworded.__name__ = "LengthFinishReasonError"


def _budget_errors() -> list[Any]:
    import anthropic
    import openai
    from google.genai import errors as genai_errors
    from openai.types.chat import ChatCompletion

    return [
        pytest.param(
            openai.LengthFinishReasonError(
                completion=ChatCompletion.model_construct(usage=None)
            ),
            id="openai-chat-finish-length",
        ),
        pytest.param(
            _Reworded("the completion stopped early"),
            id="openai-chat-length-error-class-reworded",
        ),
        pytest.param(
            ValueError("Could not parse response content as the length limit was reached"),
            id="openai-chat-length-message-wrapped",
        ),
        pytest.param(
            ValueError("Incomplete response returned, reason: max_output_tokens"),
            id="openai-responses-incomplete-client",
        ),
        pytest.param(
            _sdk_error(
                openai.BadRequestError,
                400,
                "Incomplete response returned, reason: max_output_tokens",
            ),
            id="openai-responses-incomplete-gateway-400",
        ),
        pytest.param(
            _sdk_error(
                openai.InternalServerError,
                502,
                "upstream response not completed",
                body={
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            ),
            id="openai-responses-incomplete-relayed-body",
        ),
        pytest.param(
            _sdk_error(
                anthropic.InternalServerError,
                500,
                "upstream ended the message early",
                body={
                    "type": "error",
                    "error": {"type": "api_error", "message": "stop_reason: max_tokens"},
                },
            ),
            id="anthropic-stop-reason-max-tokens",
        ),
        pytest.param(
            genai_errors.ServerError(
                500,
                {
                    "error": {
                        "code": 500,
                        "message": "Model output ended with finish_reason MAX_TOKENS",
                        "status": "INTERNAL",
                    }
                },
            ),
            id="gemini-finish-reason-max-tokens",
        ),
    ]


def _primary_config() -> Any:
    from nymeria.vendor.react_agent.config import LLMConfig

    return LLMConfig(provider="openai", model="gpt-5.5", api_key="k")


@pytest.mark.parametrize("exc", _budget_errors())
def test_an_exhausted_output_budget_reads_healthy(probe, exc):
    """A reasoning model can spend the tiny probe budget on reasoning alone:
    the route GENERATED, so the verdict is healthy, never a failure that
    would back the route off forever (red before the review fix)."""
    probe.outcome = exc

    assert reclaim.run_probe(_primary_config()) == "healthy"


def _rejected_budgets() -> list[Any]:
    import anthropic
    import openai

    return [
        pytest.param(
            _sdk_error(
                anthropic.BadRequestError,
                400,
                "max_tokens must be greater than thinking.budget_tokens",
            ),
            id="anthropic-budget-rejected",
        ),
        pytest.param(
            _sdk_error(
                openai.BadRequestError,
                400,
                "max_tokens is too large: 9999999. This model supports at most 128000",
            ),
            id="openai-max-tokens-rejected",
        ),
    ]


@pytest.mark.parametrize("exc", _rejected_budgets())
def test_a_rejected_budget_is_not_an_exhausted_one(probe, exc):
    """The negative space: a request REJECTED over its max_tokens generated
    nothing, so it stays probe_invalid, never healthy."""
    probe.outcome = exc

    assert reclaim.run_probe(_primary_config()) == "probe_invalid"


def test_an_exhausted_budget_ends_the_hold_at_the_next_turn(tmp_path, probe, inline):
    import openai
    from openai.types.chat import ChatCompletion

    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    probe.outcome = openai.LengthFinishReasonError(
        completion=ChatCompletion.model_construct(usage=None)
    )
    turn.astream()

    events = turn.astream()

    assert [e["outcome"] for e in _reclaims(events)] == ["ended"]


@pytest.mark.parametrize(
    "exc",
    [
        TypeError("create() got an unexpected keyword argument 'x'"),
        AttributeError("'NoneType' object has no attribute 'content'"),
    ],
)
def test_our_own_fault_is_a_probe_error_logged_with_its_traceback(probe, caplog, exc):
    probe.outcome = exc

    with caplog.at_level("WARNING", logger="nymeria.core.fallback_reclaim"):
        assert reclaim.run_probe(_primary_config()) == "probe_error"

    (record,) = [r for r in caplog.records if r.name == "nymeria.core.fallback_reclaim"]
    assert record.levelname == "WARNING"
    assert "not a provider verdict" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[1] is exc


def test_a_client_that_cannot_be_built_is_a_probe_error(monkeypatch, caplog):
    sent: list[Any] = []

    def broken(cfg: Any) -> Any:
        sent.append(cfg)
        raise ValueError("Unknown provider: nope")

    monkeypatch.setattr(providers_module, "create_llm", broken)

    with caplog.at_level("WARNING", logger="nymeria.core.fallback_reclaim"):
        assert reclaim.run_probe(_primary_config()) == "probe_error"

    assert len(sent) == 1
    assert any("building the probe client failed" in r.getMessage() for r in caplog.records)


def test_a_probe_error_backs_off_to_the_cap_and_never_reclaims(
    tmp_path, probe, inline, clock
):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    probe.outcome = TypeError("create() got an unexpected keyword argument 'x'")

    assert _probe_times(turn, clock, probe, minutes=121) == [0, 60, 120]
    assert turn.hold() is not None
    status = _status(THREAD, turn.held(), turn.agent.settings)
    assert (status["state"], status["last_verdict"]) == ("scheduled", "probe_error")


def _status(
    thread_id: str, hold: Any, settings: Any, llm_config: Any = None
) -> dict[str, Any]:
    status = reclaim.reclaim_status(thread_id, hold, settings, llm_config=llm_config)
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
    status = _status(THREAD, turn.hold(), settings)
    assert (status["state"], status["action"]) == ("recovered", "end")

    expired = _hold(age=timedelta(minutes=65), remaining=timedelta(minutes=-5))
    assert _status(THREAD, expired, settings)["state"] == "expired"


def test_reclaim_status_reports_stopped_checking_and_another_threads_backoff(
    tmp_path, probe, spawned, clock
):
    turn = _Turn(tmp_path)
    settings = turn.agent.settings
    # stopped: the primary rejected the probe itself.
    turn.seed("reclaim-s", active_llm_fallback=_hold())
    probe.outcome = _StatusError(400)
    turn.settle("reclaim-s")
    for check in spawned:
        check.join(5)
    status = _status("reclaim-s", turn.held("reclaim-s"), settings)
    assert (status["state"], status["last_verdict"]) == ("stopped", "probe_invalid")

    # checking: a probe in flight on this thread's route.
    reclaim.reset_reclaim_state_for_tests()
    probe.outcome = _StatusError(503)
    probe.gate = threading.Event()
    turn.seed("reclaim-c", active_llm_fallback=_hold(remaining=timedelta(hours=30)))
    turn.settle("reclaim-c")
    assert probe.started.wait(5)
    assert _status("reclaim-c", turn.held("reclaim-c"), settings)["state"] == "checking"
    probe.gate.set()
    for check in spawned:
        check.join(5)

    # A thread on the same route reads the route's backoff, not its own
    # first-check time: the route failed at t=0 and is due at t=20 min.
    turn.seed(
        "reclaim-d",
        active_llm_fallback=_hold(
            activated_at=clock.now - timedelta(minutes=12), remaining=timedelta(hours=30)
        ),
    )
    turn.settle("reclaim-d")  # learns its route; the route is not due
    for check in spawned:
        check.join(5)
    status = _status("reclaim-d", turn.held("reclaim-d"), settings)
    assert status["state"] == "scheduled"
    assert status["next_check_at"] == (clock.now + timedelta(minutes=20)).isoformat()
    assert probe.count == 2

    # checking, too, while ANOTHER thread's probe is in flight on the route
    # (reclaim-d runs no check of its own and reads that probe's verdict).
    clock.advance(minutes=20)
    probe.gate = threading.Event()
    probe.started.clear()
    turn.settle("reclaim-c")
    assert probe.started.wait(5)
    assert _status("reclaim-d", turn.held("reclaim-d"), settings)["state"] == "checking"
    probe.gate.set()
    for check in spawned:
        check.join(5)
    assert probe.count == 3


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


def _post_chat(turn: _Turn, body: dict[str, Any]) -> tuple[list[dict[str, Any]], Any]:
    """POST /chat through the real router against the turn's agent; returns
    the relayed SSE events and the publish recorder."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nymeria.api.routers.chat import create_chat_router
    from test_api_chat_publish_gating import _PublishRecorder, _sse_events

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
        with TestClient(app).stream("POST", "/chat", json=body) as response:
            text = "".join(response.iter_text())
    finally:
        reset_pending_queue_for_tests()
    return _sse_events(text), recorder


def test_the_chat_route_relays_the_event_and_mirrors_it(tmp_path, probe, inline):
    turn = _Turn(tmp_path)
    turn.seed(active_llm_fallback=_hold())
    turn.astream()  # probe: healthy verdict recorded

    events, recorder = _post_chat(
        turn, {"message": "wake", "thread_id": THREAD, "is_self_invoke": True}
    )

    relayed = [e for e in events if e.get("type") == "fallback_hold_reclaimed"]
    assert [e["outcome"] for e in relayed] == ["ended"]
    mirrored = [
        c for c in recorder.stream_chunks if c.get("type") == "fallback_hold_reclaimed"
    ]
    assert [c["outcome"] for c in mirrored] == ["ended"]
    assert turn.hold() is None


def test_only_a_declaring_chat_client_spends_an_offer(tmp_path, probe, inline):
    """``supports_reclaim_offers`` on the /chat request is the client's
    declaration (the CLI sends it); a plain request (a desktop or mobile
    client) leaves the offer pending."""
    turn = _Turn(tmp_path)
    turn.seed(
        llm_config=ThreadLLMConfig(fallback_switch_mode="ask"),
        active_llm_fallback=_hold(),
    )
    turn.astream(offer_surface=True)  # probe: healthy verdict recorded

    plain, _recorder = _post_chat(turn, {"message": "hi", "thread_id": THREAD})
    assert [e for e in plain if e.get("type") == "fallback_hold_reclaimed"] == []
    assert turn.held().reclaim_offered_at is None

    declared, _recorder = _post_chat(
        turn, {"message": "hi", "thread_id": THREAD, "supports_reclaim_offers": True}
    )
    relayed = [e for e in declared if e.get("type") == "fallback_hold_reclaimed"]
    assert [e["outcome"] for e in relayed] == ["offered"]
    assert turn.held().reclaim_offered_at is not None
