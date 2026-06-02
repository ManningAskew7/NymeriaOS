"""Phase 2 tests for the dreaming scheduler.

Covers:
- evaluate_dream_eligibility: the pure cheapest-first gate matrix (no I/O).
- sweep_dreamable_threads: per-user enumeration, activity-log-driven idle/turn
  gates, skipping disabled/shadow/processing threads, and honoring the
  per-thread dream model. invoke_dream is monkeypatched so we assert on the
  scheduler's decisions, not a real dream turn.
- invoke_dream's single-flight guard (claim/release).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from nymeria.core.accounts import AccountsRepo
from nymeria.core.activity_log import ActivityEntry, ActivityLog, ActivityType
from nymeria.core.dreaming import (
    DreamInvocationError,
    evaluate_dream_eligibility,
    invoke_dream,
    sweep_dreamable_threads,
)
from nymeria.core.thread_config import (
    DreamingConfig,
    ThreadConfig,
    ThreadConfigManager,
    ThreadLLMConfig,
)
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.time_utils import utc_now


# ---------------------------------------------------------------------------
# evaluate_dream_eligibility — pure gate matrix
# ---------------------------------------------------------------------------


def _tc(**dreaming_kwargs) -> ThreadConfig:
    return ThreadConfig(
        thread_id="t1", dreaming=DreamingConfig(enabled=True, **dreaming_kwargs)
    )


def test_gate_disabled_when_no_dreaming():
    tc = ThreadConfig(thread_id="t1")
    d = evaluate_dream_eligibility(
        tc, now=utc_now(), last_activity_at=utc_now(), user_turns_since_dream=99
    )
    assert not d.eligible and "disabled" in d.reason


def test_gate_disabled_when_dreaming_off():
    tc = ThreadConfig(thread_id="t1", dreaming=DreamingConfig(enabled=False))
    d = evaluate_dream_eligibility(
        tc, now=utc_now(), last_activity_at=utc_now(), user_turns_since_dream=99
    )
    assert not d.eligible and "disabled" in d.reason


def test_gate_skips_shadow_threads():
    tc = _tc()
    tc.shadow_parent_id = "parent-1"
    now = utc_now()
    d = evaluate_dream_eligibility(
        tc,
        now=now,
        last_activity_at=now - timedelta(hours=1),
        user_turns_since_dream=99,
    )
    assert not d.eligible and "shadow" in d.reason


def test_gate_interval_blocks_recent_dream():
    now = utc_now()
    tc = _tc(min_interval_hours=6, min_turns_since_last=1, min_idle_minutes=5)
    tc.dreaming.last_dream_at = now - timedelta(hours=2)  # < 6h
    d = evaluate_dream_eligibility(
        tc,
        now=now,
        last_activity_at=now - timedelta(hours=1),
        user_turns_since_dream=99,
    )
    assert not d.eligible and "interval" in d.reason


def test_gate_turns_block_when_too_few():
    now = utc_now()
    tc = _tc(min_turns_since_last=10, min_idle_minutes=5)
    d = evaluate_dream_eligibility(
        tc,
        now=now,
        last_activity_at=now - timedelta(hours=1),
        user_turns_since_dream=3,
    )
    assert not d.eligible and "turns" in d.reason


def test_gate_idle_blocks_active_thread():
    now = utc_now()
    tc = _tc(min_turns_since_last=1, min_idle_minutes=30)
    d = evaluate_dream_eligibility(
        tc,
        now=now,
        last_activity_at=now - timedelta(minutes=5),  # only 5m idle
        user_turns_since_dream=5,
    )
    assert not d.eligible and "idle" in d.reason


def test_gate_no_activity_is_not_eligible():
    now = utc_now()
    tc = _tc(min_turns_since_last=1, min_idle_minutes=30)
    d = evaluate_dream_eligibility(
        tc, now=now, last_activity_at=None, user_turns_since_dream=5
    )
    assert not d.eligible and "no recorded activity" in d.reason


def test_gate_all_pass():
    now = utc_now()
    tc = _tc(min_interval_hours=6, min_turns_since_last=2, min_idle_minutes=30)
    tc.dreaming.last_dream_at = now - timedelta(hours=8)  # > 6h
    d = evaluate_dream_eligibility(
        tc,
        now=now,
        last_activity_at=now - timedelta(minutes=45),  # > 30m idle
        user_turns_since_dream=4,  # > 2 turns
    )
    assert d.eligible, d.reason


def test_gate_first_dream_passes_interval_when_never_dreamed():
    now = utc_now()
    tc = _tc(min_interval_hours=6, min_turns_since_last=1, min_idle_minutes=30)
    assert tc.dreaming.last_dream_at is None
    d = evaluate_dream_eligibility(
        tc,
        now=now,
        last_activity_at=now - timedelta(minutes=45),
        user_turns_since_dream=2,
    )
    assert d.eligible, d.reason


# ---------------------------------------------------------------------------
# sweep_dreamable_threads — enumeration + activity-driven gates
# ---------------------------------------------------------------------------


class _Locks:
    def __init__(self, busy: Optional[set] = None):
        self._busy = busy or set()

    def get_lock_info(self, thread_id: str):
        return "busy" if thread_id in self._busy else None


class _StubAgent:
    """Minimal NymeriaAgent surface for the sweep + invoke_dream."""

    def __init__(self, data_dir: Path, busy: Optional[set] = None):
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self._thread_locks = _Locks(busy)
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)


@pytest.fixture
def stub_agent(tmp_path: Path) -> _StubAgent:
    return _StubAgent(tmp_path)


@pytest.fixture
def activity(tmp_path: Path, monkeypatch) -> ActivityLog:
    """Install a tmp-backed activity log as the process global."""
    alog = ActivityLog(tmp_path)
    monkeypatch.setattr("nymeria.core.activity_log._activity_log", alog)
    return alog


@pytest.fixture
def capture_dreams(monkeypatch) -> List[Tuple[str, str, Optional[str]]]:
    """Replace invoke_dream so the sweep records intent instead of dreaming."""
    calls: List[Tuple[str, str, Optional[str]]] = []

    def _fake(agent, *, parent_thread_id, user_id, model_override=None, **_):
        calls.append((parent_thread_id, user_id, model_override))
        return f"dream-{parent_thread_id}-x", {}

    monkeypatch.setattr("nymeria.core.dreaming.invoke.invoke_dream", _fake)
    return calls


def _seed_user_activity(
    alog: ActivityLog,
    user_id: str,
    per_thread: dict,
) -> None:
    """Write USER_MESSAGE entries for a user.

    ``per_thread`` maps thread_id -> list of "minutes ago" ints. Stored
    oldest-first to match the activity log's on-disk ordering.
    """
    now = utc_now()
    entries: list[ActivityEntry] = []
    for thread_id, ages in per_thread.items():
        for age in ages:
            entries.append(
                ActivityEntry(
                    type=ActivityType.USER_MESSAGE,
                    message="hi",
                    user_id=user_id,
                    thread_id=thread_id,
                    timestamp=now - timedelta(minutes=age),
                )
            )
    entries.sort(key=lambda e: e.timestamp)  # oldest-first
    alog._save_entries(user_id, entries)


def _register(agent, user_id, thread_id, tc, platform="desktop"):
    agent.thread_config_manager.save_config(tc)
    agent.thread_metadata_manager.upsert_thread(
        user_id, thread_id, title=thread_id, platform=platform
    )


def test_sweep_fires_only_for_eligible_threads(stub_agent, activity, capture_dreams):
    user = "u1"
    # Eligible: idle 45m, 3 user turns, low gates.
    _register(
        stub_agent,
        user,
        "t-eligible",
        ThreadConfig(
            thread_id="t-eligible",
            dreaming=DreamingConfig(
                enabled=True, min_idle_minutes=30, min_turns_since_last=2
            ),
        ),
    )
    # Too active: last message 5m ago.
    _register(
        stub_agent,
        user,
        "t-active",
        ThreadConfig(
            thread_id="t-active",
            dreaming=DreamingConfig(
                enabled=True, min_idle_minutes=30, min_turns_since_last=1
            ),
        ),
    )
    # Too few turns: idle but only 1 message, needs 5.
    _register(
        stub_agent,
        user,
        "t-fewturns",
        ThreadConfig(
            thread_id="t-fewturns",
            dreaming=DreamingConfig(
                enabled=True, min_idle_minutes=30, min_turns_since_last=5
            ),
        ),
    )
    # Disabled.
    _register(
        stub_agent,
        user,
        "t-off",
        ThreadConfig(thread_id="t-off", dreaming=DreamingConfig(enabled=False)),
    )

    _seed_user_activity(
        activity,
        user,
        {
            "t-eligible": [45, 60, 90],
            "t-active": [5, 60, 90],
            "t-fewturns": [45],
            "t-off": [45, 60, 90],
        },
    )

    fired = sweep_dreamable_threads(stub_agent)

    assert fired == 1
    assert [c[0] for c in capture_dreams] == ["t-eligible"]
    assert capture_dreams[0][1] == user


def test_sweep_counts_turns_since_last_dream(stub_agent, activity, capture_dreams):
    """USER_MESSAGEs before last_dream_at don't count toward the turn gate."""
    user = "u1"
    now = utc_now()
    tc = ThreadConfig(
        thread_id="t-dreamed",
        dreaming=DreamingConfig(
            enabled=True,
            min_idle_minutes=30,
            min_turns_since_last=2,
            min_interval_hours=6,
        ),
    )
    # Dreamed 10h ago (interval passes) — but all activity predates it.
    tc.dreaming.last_dream_at = now - timedelta(hours=10)
    _register(stub_agent, user, "t-dreamed", tc)
    # 3 messages, all ~12h ago (before the last dream) → 0 turns since dream.
    _seed_user_activity(activity, user, {"t-dreamed": [12 * 60, 13 * 60, 14 * 60]})

    assert sweep_dreamable_threads(stub_agent) == 0
    assert capture_dreams == []


def test_sweep_skips_processing_threads(tmp_path, activity, capture_dreams):
    user = "u1"
    agent = _StubAgent(tmp_path, busy={"t-busy"})
    _register(
        agent,
        user,
        "t-busy",
        ThreadConfig(
            thread_id="t-busy",
            dreaming=DreamingConfig(
                enabled=True, min_idle_minutes=30, min_turns_since_last=1
            ),
        ),
    )
    _seed_user_activity(activity, user, {"t-busy": [45, 60]})

    # Gate-eligible, but a turn holds the lock → skipped.
    assert sweep_dreamable_threads(agent) == 0
    assert capture_dreams == []


def test_sweep_skips_shadow_platform_threads(stub_agent, activity, capture_dreams):
    user = "u1"
    tc = ThreadConfig(
        thread_id="dream-shadow-x",
        shadow_parent_id="real-parent",
        dreaming=DreamingConfig(enabled=True, min_idle_minutes=5, min_turns_since_last=1),
    )
    _register(stub_agent, user, "dream-shadow-x", tc, platform="dream")
    _seed_user_activity(activity, user, {"dream-shadow-x": [45, 60]})

    assert sweep_dreamable_threads(stub_agent) == 0
    assert capture_dreams == []


def test_sweep_passes_thread_dream_model(stub_agent, activity, capture_dreams):
    user = "u1"
    _register(
        stub_agent,
        user,
        "t-model",
        ThreadConfig(
            thread_id="t-model",
            dreaming=DreamingConfig(
                enabled=True,
                min_idle_minutes=30,
                min_turns_since_last=1,
                model="claude-haiku-4-5-20251001",
            ),
        ),
    )
    _seed_user_activity(activity, user, {"t-model": [45, 60]})

    assert sweep_dreamable_threads(stub_agent) == 1
    assert capture_dreams[0][2] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# invoke_dream single-flight guard
# ---------------------------------------------------------------------------


@pytest.fixture
def no_daemon_dispatch(monkeypatch):
    """Suppress the background daemon so the in-flight slot stays claimed."""

    class _NoopThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr("nymeria.core.dreaming.invoke.threading.Thread", _NoopThread)
    return _NoopThread


def test_invoke_dream_single_flight_guard(stub_agent, no_daemon_dispatch):
    from nymeria.core.dreaming.invoke import _release_dream_slot

    parent = "parent-single-flight"
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=parent,
            llm_config=ThreadLLMConfig(model="claude-sonnet-4-6"),
            dreaming=DreamingConfig(enabled=True),
        )
    )

    try:
        shadow1, _ = invoke_dream(stub_agent, parent_thread_id=parent, user_id="u1")

        # Second invocation while the first is "in flight" is refused.
        with pytest.raises(DreamInvocationError, match="already in progress"):
            invoke_dream(stub_agent, parent_thread_id=parent, user_id="u1")

        # Releasing the slot (as _run_dream_cycle's finally would) re-opens it.
        _release_dream_slot(parent)
        shadow2, _ = invoke_dream(stub_agent, parent_thread_id=parent, user_id="u1")
        assert shadow2 != shadow1
    finally:
        # Don't leak the process-local slot into other tests.
        _release_dream_slot(parent)


# ---------------------------------------------------------------------------
# Dream fork-seeding: clone parent checkpoint + soft-prune into the shadow
# ---------------------------------------------------------------------------


class _StubSyncGraph:
    def __init__(self, messages):
        self._messages = list(messages)
        self.updates: list = []

    def get_state(self, config):
        return SimpleNamespace(values={"messages": list(self._messages)})

    def update_state(self, config, payload):
        self.updates.append(payload)


def test_seed_shadow_clones_and_soft_prunes(monkeypatch):
    import nymeria.config as cfg
    import nymeria.core.thread_branch as tb
    from nymeria.core.dreaming import invoke as invoke_mod

    clone_calls: list = []

    def fake_clone(settings, src, dst, **kw):
        clone_calls.append((src, dst))
        return {"checkpoints": 1}

    monkeypatch.setattr(tb, "clone_thread_checkpoints", fake_clone)
    monkeypatch.setattr(cfg, "get_settings", lambda: SimpleNamespace(data_dir="/tmp"))

    big = ToolMessage(content="W" * 1200, tool_call_id="tc1", name="web", id="t1")
    graph = _StubSyncGraph([big, HumanMessage(content="hi", id="h1")])
    agent = SimpleNamespace(_default_graph=graph)

    invoke_mod._seed_shadow_from_parent(agent, "parent-1", "dream-shadow-x")

    assert clone_calls == [("parent-1", "dream-shadow-x")]
    assert len(graph.updates) == 1
    reps = graph.updates[0]["messages"]
    assert len(reps) == 1 and reps[0].id == "t1"  # only the big tool result rewritten
    assert "truncated" in reps[0].content
    assert reps[0].additional_kwargs["prune_mode"] == "soft"


def test_seed_shadow_swallows_clone_failure(monkeypatch):
    """A clone failure must not raise — the dream falls back to memory-only."""
    import nymeria.config as cfg
    import nymeria.core.thread_branch as tb
    from nymeria.core.dreaming import invoke as invoke_mod

    def boom(*a, **k):
        raise RuntimeError("checkpoint db down")

    monkeypatch.setattr(tb, "clone_thread_checkpoints", boom)
    monkeypatch.setattr(cfg, "get_settings", lambda: SimpleNamespace(data_dir="/tmp"))

    graph = _StubSyncGraph([])
    agent = SimpleNamespace(_default_graph=graph)

    # Must not raise.
    invoke_mod._seed_shadow_from_parent(agent, "p", "s")
    assert graph.updates == []


# ---------------------------------------------------------------------------
# Memory-seed suppression on shadow threads
# ---------------------------------------------------------------------------


def test_skip_memory_seed_true_for_shadow(stub_agent):
    from nymeria.core.agent import NymeriaAgent

    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="dream-x", shadow_parent_id="parent-1")
    )
    assert NymeriaAgent._thread_skip_memory_seed(stub_agent, "dream-x") is True


def test_skip_memory_seed_false_for_normal(stub_agent):
    from nymeria.core.agent import NymeriaAgent

    stub_agent.thread_config_manager.save_config(ThreadConfig(thread_id="normal-1"))
    assert NymeriaAgent._thread_skip_memory_seed(stub_agent, "normal-1") is False


def test_skip_memory_seed_false_for_unknown_thread(stub_agent):
    from nymeria.core.agent import NymeriaAgent

    assert NymeriaAgent._thread_skip_memory_seed(stub_agent, "never-seen") is False
