"""#459: a config-less thread's fallback swap belongs to that thread alone.

Before #459, a thread with no config file (no memories, TODOs or tool
preferences on its user either) was served ONE per-user graph cached under
the sentinel thread id ``""`` and built with no thread id: its baked
``LLMConfig`` carried no activation or consent callback and no prompt-cache
key, and a swap's candidate index and pending note lived on that shared
object. So a swap recorded no hold, asked no consent, and moved EVERY
config-less thread of that user onto the fallback while ``done`` and
``[COST]`` named the primary. Since #459 every thread builds and caches its
own graph under ``(user, thread_id)``, and the turn start (``chat()`` and
``astream()``, never the graph lookup) resets the looked-up graph's swap
state, so a swap whose hold did not persist never outlives its turn (D3).

Harness: the REAL ``NymeriaAgent.astream``/``chat`` (the stub-agent pattern
from ``test_agent_turn_loops.py``), the REAL graph lookup and cache
(``get_graph_for_user_impl``), the REAL resolver and ``build_agent_config``,
the REAL stream-processor recovery (site 3), the REAL activation and consent
callbacks, a real ``ThreadLockManager`` and a real ``ThreadConfigManager`` on
``tmp_path``. Only the compile is faked (``_build_*_graph_with_prompt``): the
fake graph carries the ``LLMConfig`` the real ``build_agent_config`` baked
(``nymeria_llm_config``, exactly what ``create_graph`` stashes) and runs
model calls the way the agent node does (consume a stamped swap note, then
call the ACTIVE candidate), recording which model each call reached: that
log is the wire.

Behaviors (plan it42, section 3) and where they are pinned: B1 isolation,
B2 the hold, B3 consent, B4 the reported model, B5 a reclaimed thread's next
outage, B6 configured threads unchanged, B7 concurrency, B9 idle expiry,
B10 a swap with no persisted hold ends with its turn (both turn seams), plus
the D3 placement trap (a mid-turn graph lookup keeps the swapped model). B8
(cache keys, the baked prompt-cache key and callbacks) is pinned here at the
turn level and in ``test_graph_build_unification.py`` at the lookup level.

Edges named and skipped: the refusal swap and the sync in-node transport
site run the same activation callback as site 3 and are pinned by
``test_fallback_consent.py``; the tool-reload rebuild after a swap with no
persisted hold (it invalidates and rebuilds mid-turn, so the re-drive starts
on the primary again) is pre-existing and filed as an it42 follow-up; a
restart (graphs are in-memory by design).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, cast

import pytest
from langchain_core.messages import AIMessage

import nymeria.core.fallback_approvals as fallback_approvals
from nymeria.core import fallback_reclaim as reclaim
from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_llm_config import get_llm_config_for_thread
from nymeria.core.pending_prompt_queue import (
    InMemoryPendingPromptQueue,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.core.time_utils import utc_now
from nymeria.core.turn_runner import HolderTurn
from nymeria.core.user_profile import UserProfile
from nymeria.vendor.react_agent import providers as providers_module
from nymeria.vendor.react_agent.config import CheckpointerConfig
from nymeria.vendor.react_agent.nodes import (
    llm_activate_next_fallback,
    llm_active_candidate_descriptor,
)
from test_agent_streaming import _RetryableStreamError
from test_agent_turn_loops import _stream_agent
from test_fallback_reclaim import _Clock, _Probe, _TurnSettings

USER = "owner"
THREAD_A = "configless-a"
THREAD_B = "configless-b"
THREAD_C = "configured-c"
PRIMARY = "claude-sonnet-4-6"  # _Settings' global model
FALLBACK = "claude-haiku-4-5-20251001"  # the default chain's first fallback
PROVIDER = "anthropic"
_CHECKPOINTER = CheckpointerConfig(backend="memory")


class _RigSettings(_TurnSettings):
    # No same-model retries: the first retryable failure moves down the chain.
    llm_stream_max_retries = 0
    llm_stream_retry_initial_delay = 0.0
    llm_stream_retry_max_delay = 0.0
    tool_timeout = 300
    tool_output_max_chars = 100000
    log_level = "INFO"


class _Graph:
    """Compiled-graph stand-in at the build seam (see the module docstring)."""

    def __init__(self, rig: "_Rig", llm_config: Any) -> None:
        self.rig = rig
        self.nymeria_llm_config = llm_config

    async def astream_events(self, input_state, config=None, version=None):
        assert config is not None
        thread_id = config["configurable"]["thread_id"]
        for _ in range(self.rig.calls_per_run.get(thread_id, 1)):
            number = await self.rig.before_call(thread_id)
            run_id = f"{thread_id}-{number}"
            # The agent node starts (LangGraph emits this before the node
            # body runs): a cancelled turn stops HERE, before the node
            # consumes a stamped note or calls the model.
            yield {"event": "on_chain_start", "name": "agent", "run_id": run_id}
            model = self.rig.model_call(thread_id, self.nymeria_llm_config)
            yield {"event": "on_chat_model_start", "run_id": run_id}
            await self.rig.after_call(thread_id, number)
            if self.rig.fails(thread_id, number, model):
                exc = _RetryableStreamError(f"server_error from {model}")
                exc.nymeria_stream_chunks_before_error = 1
                raise exc
            yield {
                "event": "on_chat_model_end",
                "run_id": run_id,
                "data": {"output": AIMessage(content=f"ok from {model}")},
            }
        self.rig.after_run(thread_id)

    def invoke(self, input_state, config=None):
        """The sync agent node (``chat()``): an in-node swap activates the
        next candidate on THIS config (the vendored helper the node's swap
        uses), then calls it."""
        assert config is not None
        thread_id = config["configurable"]["thread_id"]
        model = self.rig.model_call(thread_id, self.nymeria_llm_config)
        if model in self.rig.down:
            exc = _RetryableStreamError(f"server_error from {model}")
            if llm_activate_next_fallback(self.nymeria_llm_config, exc=exc) is None:
                raise exc
            model = self.rig.model_call(thread_id, self.nymeria_llm_config)
        return {"messages": [AIMessage(content=f"ok from {model}")]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})

    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})

    async def aupdate_state(self, config, values):
        return None

    def update_state(self, config, values):
        return None


Hook = Callable[[], Awaitable[None]]


class _Rig:
    """One agent, one data dir, user U with no memories, personality
    overrides, TODOs or tool preferences (so before #459 every thread
    without a config file took the shared-graph path)."""

    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        agent: Any = _stream_agent(THREAD_A)
        agent._thread_locks = ThreadLockManager()
        agent._chat_turn_local = threading.local()
        agent._fire_done_observe_sync = lambda **kwargs: None
        agent._compaction = SimpleNamespace(
            note_turn_end=lambda *args: None,
            check_and_compact_sync=lambda *args, **kwargs: None,
        )
        agent._analyze_turn_safety = lambda messages, max_iterations, **kwargs: (
            SimpleNamespace(should_stop=False)
        )
        agent._memory_seeded_threads = {THREAD_A, THREAD_B, THREAD_C}
        agent.thread_config_manager = ThreadConfigManager(tmp_path)
        agent.settings = _RigSettings()
        for key, value in settings.items():
            setattr(agent.settings, key, value)
        # The real resolver and run config (the stub agent fakes both).
        del agent._get_llm_config_for_thread
        del agent._graph_run_config
        # A real profile: the prompt build reads its #164 status rule.
        agent.profile_manager = SimpleNamespace(
            get_profile=lambda user_id: UserProfile(
                user_id=user_id, enabled_global_skills=[]
            )
        )
        agent.todo_manager = SimpleNamespace(
            get_todos=lambda user_id: SimpleNamespace(
                get_active_todos_for_thread=lambda thread_id: [],
                get_active_todos=lambda: [],
            )
        )
        agent.accounts_repo = SimpleNamespace(
            get_thread_owner=lambda thread_id: USER,
            get_user_by_id=lambda user_id: None,
        )
        agent.skill_manager = None
        # The REAL graph cache and lookup; only the compile is faked.
        agent._user_graphs = {}
        agent._async_user_graphs = {}
        agent._graph_cache_lock = threading.Lock()
        agent._GRAPH_CACHE_MAX = 50
        agent._base_system_prompt = "BASE"
        agent._sync_external_resource_edits = lambda: None
        agent._build_async_graph_with_prompt = self._build_async
        agent._build_graph_with_prompt = self._build_sync
        self.invalidated: list[str] = []

        def invalidate(thread_id: str) -> None:
            self.invalidated.append(thread_id)
            NymeriaAgent.invalidate_thread_config_cache(agent, thread_id)

        agent.invalidate_thread_config_cache = invalidate
        self.agent = agent
        # One event loop for every turn, like the API process: the async
        # graph cache is keyed by the running loop, so a loop per turn would
        # rebuild every turn and hide what the cache carries across turns.
        self.loop = asyncio.new_event_loop()
        self.down: set[str] = set()
        self.wire: list[tuple[str, str]] = []
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.builds: list[tuple[str, str]] = []
        self.graphs: list[_Graph] = []
        self.calls_per_run: dict[str, int] = {}
        self.before: dict[tuple[str, int], Hook] = {}
        self.after: dict[tuple[str, int], Hook] = {}
        self.on_run_end: dict[str, Callable[[], None]] = {}
        self._calls: dict[str, int] = {}
        # Which streamed calls fail: by default every call that reaches a
        # model in ``down`` (the provider's outage).
        self.fails: Callable[[str, int, str], bool] = (
            lambda thread_id, number, model: model in self.down
        )

    # -- the fake compile ------------------------------------------------------

    def _build(self, prompt: str, user_id: str, thread_id: str, checkpointer) -> _Graph:
        agent = self.agent
        tc = agent.thread_config_manager.get_config(thread_id) if thread_id else None
        config = agent._build_agent_config(
            prompt, checkpointer, thread_id, tc, acting_user_id=user_id
        )
        self.builds.append((user_id, thread_id))
        graph = _Graph(self, config.llm)
        self.graphs.append(graph)
        return graph

    def _build_async(self, prompt: str, user_id: str = "default", thread_id: str = ""):
        return self._build(prompt, user_id, thread_id, _CHECKPOINTER)

    def _build_sync(self, prompt: str, user_id: str = "default", thread_id: str = ""):
        return self._build(prompt, user_id, thread_id, _CHECKPOINTER)

    # -- the fake agent node ---------------------------------------------------

    async def before_call(self, thread_id: str) -> int:
        number = self._calls.get(thread_id, 0) + 1
        self._calls[thread_id] = number
        hook = self.before.get((thread_id, number))
        if hook is not None:
            await hook()
        return number

    async def after_call(self, thread_id: str, number: int) -> None:
        hook = self.after.get((thread_id, number))
        if hook is not None:
            await hook()

    def after_run(self, thread_id: str) -> None:
        hook = self.on_run_end.pop(thread_id, None)
        if hook is not None:
            hook()

    def model_call(self, thread_id: str, llm_config: Any) -> str:
        pending = getattr(llm_config, "pending_fallback_note", None)
        if pending:
            llm_config.pending_fallback_note = None
            self.notes.append((thread_id, pending))
        model = str(llm_active_candidate_descriptor(llm_config).get("model") or "")
        self.wire.append((thread_id, model))
        return model

    # -- turns -----------------------------------------------------------------

    def models(self, thread_id: str) -> list[str]:
        return [model for tid, model in self.wire if tid == thread_id]

    async def turn(self, thread_id: str, message: str = "hello") -> list[dict[str, Any]]:
        chunks = [
            chunk
            async for chunk in self.agent.astream(
                message, thread_id=thread_id, user_id=USER, source="user"
            )
        ]
        from nymeria.core.embedding_jobs import wait_for_pending_embedding_jobs

        await wait_for_pending_embedding_jobs()
        return chunks

    def run(self, coro: Awaitable[Any]) -> Any:
        set_pending_queue(InMemoryPendingPromptQueue())
        try:
            return self.loop.run_until_complete(coro)
        finally:
            reset_pending_queue_for_tests()

    def astream(self, thread_id: str, message: str = "hello") -> list[dict[str, Any]]:
        return self.run(self.turn(thread_id, message))

    def graph(self, thread_id: str) -> _Graph:
        """The cached async graph serving ``thread_id``'s turns."""
        (graph,) = [
            graph
            for key, (_hash, graph) in self.agent._async_user_graphs.items()
            if key[2] == thread_id
        ]
        return graph

    def chat(self, thread_id: str, message: str = "hello") -> str:
        set_pending_queue(InMemoryPendingPromptQueue())
        try:
            return self.agent.chat(
                message, thread_id=thread_id, user_id=USER, source="user"
            )
        finally:
            reset_pending_queue_for_tests()

    # -- reads -----------------------------------------------------------------

    def saved(self, thread_id: str) -> ThreadConfig | None:
        return self.agent.thread_config_manager.get_config(thread_id)

    def configure(self, thread_id: str, **fields: Any) -> None:
        assert self.agent.thread_config_manager.save_config(
            ThreadConfig(thread_id=thread_id, **fields)
        )

    def resolved_model(self, thread_id: str) -> str:
        """What ``done.model`` reports (``HolderTurn.done_event``); ``[COST]``
        resolves the same accessor (``agent_context_stats``)."""
        holder = SimpleNamespace(agent=self.agent, spec=SimpleNamespace(thread_id=thread_id))
        done = HolderTurn.done_event(cast(HolderTurn, holder))
        assert done["type"] == "done"
        return str(done["model"])


@pytest.fixture
def make_rig(tmp_path):
    rigs: list[_Rig] = []

    def make(**settings: Any) -> _Rig:
        rig = _Rig(tmp_path, **settings)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.loop.close()


def _of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == kind]


def _started(events: list[dict[str, Any]]) -> list[str]:
    return [str(event["model"]) for event in _of(events, "llm_call_started")]


@pytest.fixture(autouse=True)
def _fresh_reclaim_state():
    reclaim.reset_reclaim_state_for_tests()
    yield
    reclaim.reset_reclaim_state_for_tests()


# -- B1 and B4: the swap is the swapping thread's, and named as such ------------


def test_a_swap_on_one_configless_thread_never_moves_another(make_rig):
    rig = make_rig()
    rig.down.add(PRIMARY)
    swap_turn = rig.astream(THREAD_A)
    assert rig.models(THREAD_A) == [PRIMARY, FALLBACK]
    assert len(_of(swap_turn, "provider_fallback")) == 1

    rig.down.clear()
    events = rig.astream(THREAD_B)

    assert rig.models(THREAD_B) == [PRIMARY]
    assert _started(events) == [PRIMARY]
    assert rig.saved(THREAD_B) is None
    # Nor did A's swap note reach B's conversation.
    assert [tid for tid, _note in rig.notes] == [THREAD_A]
    # B8 at the turn level: each thread's turns ran on a graph built for it,
    # whose baked config carries its own prompt-cache key and both callbacks.
    assert rig.builds == [(USER, THREAD_A), (USER, THREAD_B)]
    swap_graph, b_graph = rig.graphs
    assert rig.graph(THREAD_B) is b_graph
    for graph, thread_id in ((swap_graph, THREAD_A), (b_graph, THREAD_B)):
        llm = graph.nymeria_llm_config
        assert llm.prompt_cache_key == f"nym-{thread_id}"
        assert llm.fallback_activation_callback is not None
        assert llm.fallback_decision_callback is not None
    assert b_graph.nymeria_llm_config.active_fallback_candidate_index == 0


def test_the_reported_model_is_the_one_that_ran_after_a_configless_swap(make_rig):
    rig = make_rig()
    rig.down.add(PRIMARY)

    events = rig.astream(THREAD_A)

    # The re-driven call names the fallback, and so do done.model and
    # [COST] (the resolver now folds A's persisted hold into its primary).
    assert _started(events) == [PRIMARY, FALLBACK]
    assert rig.resolved_model(THREAD_A) == FALLBACK
    assert get_llm_config_for_thread(rig.agent, THREAD_A, USER).model == FALLBACK

    # The next turn runs the held fallback as its primary and says so.
    rig.down.clear()
    events = rig.astream(THREAD_A)
    assert rig.models(THREAD_A)[-1] == FALLBACK
    assert _started(events) == [FALLBACK]
    assert rig.resolved_model(THREAD_A) == FALLBACK


# -- B2 and B6: the hold is byte for byte a configured thread's -----------------


@pytest.mark.parametrize("kind", ["configless", "configured"])
def test_a_swap_records_the_hold_on_every_thread_shape(make_rig, kind):
    rig = make_rig()
    thread_id = THREAD_A if kind == "configless" else THREAD_C
    if kind == "configured":
        rig.configure(thread_id, instructions="be brief")
    rig.down.add(PRIMARY)
    before = utc_now()

    events = rig.astream(thread_id)

    tc = rig.saved(thread_id)
    assert tc is not None and tc.active_llm_fallback is not None, "no hold persisted"
    hold = tc.active_llm_fallback
    assert (hold.provider, hold.model) == (PROVIDER, FALLBACK)
    assert (hold.source_provider, hold.source_model) == (PROVIDER, PRIMARY)
    assert hold.hold_seconds == 7200  # the global default
    assert hold.hold_origin == "automatic"
    assert (hold.reason, hold.http_status) == ("provider_server_error", 500)
    assert hold.activated_at >= before
    assert hold.expires_at == hold.activated_at + timedelta(seconds=7200)
    (swap,) = _of(events, "provider_fallback")
    assert swap["to_model"] == FALLBACK
    assert swap["hold_seconds"] == 7200
    assert swap["expires_at"] == hold.expires_at.isoformat()
    # The cached graph was dropped, so the next turn builds against the hold.
    assert rig.invalidated == [thread_id]
    assert (USER, thread_id) in rig.builds

    rig.down.clear()
    rig.astream(thread_id)
    assert rig.models(thread_id) == [PRIMARY, FALLBACK, FALLBACK]
    assert rig.builds.count((USER, thread_id)) == 2


# -- B3: consent parks a config-less turn ---------------------------------------


@pytest.fixture
def parked(monkeypatch) -> dict[str, Any]:
    """The park itself (``_await_parked_decision``) answered as the human
    would; the real gate decides whether to park, and the real activation
    applies the answer. Its translation is pinned in test_fallback_consent."""
    seen: dict[str, Any] = {"calls": [], "answer": None}

    async def answer(**kwargs: Any) -> dict[str, Any]:
        seen["calls"].append(kwargs)
        return seen["answer"]

    monkeypatch.setattr(fallback_approvals, "_await_parked_decision", answer)
    return seen


def test_ask_mode_parks_a_configless_turn_and_an_approval_holds_its_choice(
    make_rig, parked
):
    rig = make_rig(llm_fallback_switch_mode="ask")
    parked["answer"] = {"action": "swap", "hold_origin": "user", "hold_seconds": 60}
    rig.down.add(PRIMARY)

    events = rig.astream(THREAD_A)

    (call,) = parked["calls"]
    assert (call["thread_id"], call["user_id"], call["kind"]) == (
        THREAD_A,
        USER,
        "transport",
    )
    assert call["payload"]["to_model"] == FALLBACK
    hold = rig.saved(THREAD_A).active_llm_fallback
    assert hold is not None
    assert (hold.model, hold.hold_seconds, hold.hold_origin) == (FALLBACK, 60, "user")
    assert _of(events, "provider_fallback")[0]["hold_seconds"] == 60
    assert rig.models(THREAD_A) == [PRIMARY, FALLBACK]

    # B is untouched: its own graph, the primary, no config.
    rig.down.clear()
    rig.astream(THREAD_B)
    assert rig.models(THREAD_B) == [PRIMARY]
    assert rig.saved(THREAD_B) is None


def test_ask_mode_decline_fails_the_configless_turn_and_persists_nothing(
    make_rig, parked
):
    rig = make_rig(llm_fallback_switch_mode="ask")
    parked["answer"] = {"action": "fail"}
    rig.down.add(PRIMARY)

    events = rig.astream(THREAD_A)

    assert len(parked["calls"]) == 1
    assert rig.models(THREAD_A) == [PRIMARY]
    assert _of(events, "provider_fallback") == []
    (error,) = _of(events, "error")
    assert error["content"]
    assert rig.saved(THREAD_A) is None
    assert rig.notes == []

    rig.down.clear()
    rig.astream(THREAD_B)
    assert rig.models(THREAD_B) == [PRIMARY]


# -- B5: a reclaimed thread whose config emptied still records the next hold ----


@pytest.fixture
def probe(monkeypatch) -> _Probe:
    fake = _Probe()
    monkeypatch.setattr(providers_module, "create_llm", fake.create_llm)
    return fake


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(reclaim, "_now", fake)
    monkeypatch.setattr(reclaim, "_spawn", lambda target: target())
    return fake


def test_a_reclaimed_thread_whose_config_emptied_records_its_next_hold(
    make_rig, probe, clock
):
    rig = make_rig()
    rig.configure(
        THREAD_A,
        active_llm_fallback=_auto_hold(),
    )
    clock.advance(minutes=31)

    rig.astream(THREAD_A)  # probes the primary (healthy); this turn stays held
    assert probe.count == 1
    events = rig.astream(THREAD_A)  # ends the hold, delivers the note
    assert [e["outcome"] for e in _of(events, "fallback_hold_reclaimed")] == ["ended"]
    assert rig.saved(THREAD_A) is None, "the emptied config was not deleted"
    assert rig.models(THREAD_A) == [FALLBACK, PRIMARY]

    # The thread is config-less now; a fresh outage on it is a real swap.
    rig.down.add(PRIMARY)
    events = rig.astream(THREAD_A)

    assert rig.models(THREAD_A)[-2:] == [PRIMARY, FALLBACK]
    hold = rig.saved(THREAD_A).active_llm_fallback
    assert hold is not None, "the outage after the reclaim recorded no hold"
    assert (hold.model, hold.hold_origin) == (FALLBACK, "automatic")
    assert _of(events, "provider_fallback")[0]["hold_seconds"] == 7200


def _auto_hold():
    from nymeria.core.thread_config import ActiveLLMFallback

    activated = utc_now()
    return ActiveLLMFallback(
        provider=PROVIDER,
        model=FALLBACK,
        source_provider=PROVIDER,
        source_model=PRIMARY,
        hold_seconds=7200,
        activated_at=activated,
        expires_at=activated + timedelta(seconds=7200),
        reason="provider_server_error",
        http_status=500,
        hold_origin="automatic",
    )


# -- B7: concurrent turns of one user never share swap state --------------------


def test_a_concurrent_turn_never_sees_another_threads_swap(make_rig):
    rig = make_rig()
    rig.calls_per_run[THREAD_B] = 2
    b_first_done = asyncio.Event()
    a_swapped = asyncio.Event()
    b_second_done = asyncio.Event()

    async def b_after_first() -> None:
        b_first_done.set()

    async def a_waits_for_b_in_flight() -> None:
        await b_first_done.wait()

    async def a_redrive() -> None:
        # The swap is applied (index moved, note stamped) and not yet
        # consumed: let B make its next call in that window.
        a_swapped.set()
        await b_second_done.wait()

    async def b_waits_for_the_swap() -> None:
        await a_swapped.wait()

    async def b_after_second() -> None:
        b_second_done.set()

    rig.after[(THREAD_B, 1)] = b_after_first
    rig.before[(THREAD_A, 1)] = a_waits_for_b_in_flight
    rig.before[(THREAD_A, 2)] = a_redrive
    rig.before[(THREAD_B, 2)] = b_waits_for_the_swap
    rig.after[(THREAD_B, 2)] = b_after_second

    # The provider fails exactly one call: A's first. B is healthy on the
    # primary throughout, so any model B reaches other than the primary came
    # from A's swap state.
    rig.fails = lambda thread_id, number, model: (thread_id, number) == (THREAD_A, 1)

    async def both() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        b_turn = asyncio.create_task(rig.turn(THREAD_B))
        a_turn = asyncio.create_task(rig.turn(THREAD_A))
        return await b_turn, await a_turn

    b_events, a_events = rig.run(both())

    assert len(_of(a_events, "provider_fallback")) == 1
    assert rig.models(THREAD_A) == [PRIMARY, FALLBACK]
    assert rig.models(THREAD_B) == [PRIMARY, PRIMARY]
    assert _started(b_events) == [PRIMARY, PRIMARY]
    assert [tid for tid, _note in rig.notes] == [THREAD_A]
    assert rig.notes[0][1]["payload"]["to_model"] == FALLBACK


# -- B9: an expired hold on a config-less thread ends at the turn start ---------


def test_an_expired_hold_from_a_configless_swap_ends_at_the_turn_start(
    make_rig, clock
):
    rig = make_rig(llm_fallback_hold_seconds=600)
    rig.down.add(PRIMARY)
    rig.astream(THREAD_A)
    hold = rig.saved(THREAD_A).active_llm_fallback
    assert hold is not None and hold.hold_seconds == 600

    rig.down.clear()
    clock.advance(minutes=11)
    rig.astream(THREAD_A)

    assert rig.models(THREAD_A) == [PRIMARY, FALLBACK, PRIMARY]
    assert rig.saved(THREAD_A) is None
    assert rig.resolved_model(THREAD_A) == PRIMARY


# -- B10 (D3): a swap whose hold did not persist ends with its turn -------------


def _no_hold(rig: _Rig, how: str, monkeypatch) -> None:
    if how == "hold_seconds_0":
        rig.agent.settings.llm_fallback_hold_seconds = 0
    else:
        monkeypatch.setattr(
            rig.agent.thread_config_manager, "save_config", lambda tc: False
        )


@pytest.mark.parametrize("how", ["hold_seconds_0", "save_failed"])
@pytest.mark.parametrize("thread_id", [THREAD_A, THREAD_C])
def test_a_streamed_swap_with_no_persisted_hold_ends_with_its_turn(
    make_rig, monkeypatch, how, thread_id
):
    rig = make_rig()
    if thread_id == THREAD_C:
        rig.configure(THREAD_C, instructions="be brief")
    _no_hold(rig, how, monkeypatch)
    rig.down.add(PRIMARY)
    rig.astream(thread_id)
    assert rig.models(thread_id) == [PRIMARY, FALLBACK]
    assert getattr(rig.saved(thread_id), "active_llm_fallback", None) is None

    rig.down.clear()
    events = rig.astream(thread_id)

    assert rig.models(thread_id)[-1] == PRIMARY
    assert _started(events) == [PRIMARY]
    assert rig.resolved_model(thread_id) == PRIMARY
    # The same cached graph served both turns: the turn-start reset, not a
    # rebuild, put the second turn back on the primary.
    assert rig.builds == [(USER, thread_id)]


def test_a_swap_note_a_cancelled_turn_left_never_reaches_the_next_turn(make_rig):
    """The note half of the reset: a turn cancelled between the swap and the
    re-driven node leaves the swap note stamped on its cached graph. The next
    turn runs the primary, so delivering "switched to the fallback" there
    would misinform the model."""
    rig = make_rig(llm_fallback_hold_seconds=0)
    rig.down.add(PRIMARY)

    async def cancel_before_the_redrive() -> None:
        rig.agent._thread_locks.get_abort_event(THREAD_A).set()

    rig.before[(THREAD_A, 2)] = cancel_before_the_redrive
    events = rig.astream(THREAD_A)
    assert len(_of(events, "provider_fallback")) == 1
    assert [event.get("code") for event in _of(events, "error")] == ["cancelled"]
    assert rig.models(THREAD_A) == [PRIMARY]
    assert rig.notes == []  # stamped, never consumed

    rig.down.clear()
    rig.astream(THREAD_A)

    assert rig.models(THREAD_A) == [PRIMARY, PRIMARY]
    assert rig.notes == []
    assert rig.builds == [(USER, THREAD_A)]


@pytest.mark.parametrize("how", ["hold_seconds_0", "save_failed"])
def test_a_sync_swap_with_no_persisted_hold_ends_with_its_turn(
    make_rig, monkeypatch, how
):
    rig = make_rig()
    _no_hold(rig, how, monkeypatch)
    rig.down.add(PRIMARY)
    assert rig.chat(THREAD_A) == f"ok from {FALLBACK}"
    assert rig.models(THREAD_A) == [PRIMARY, FALLBACK]

    rig.down.clear()
    reply = rig.chat(THREAD_A)

    assert reply == f"ok from {PRIMARY}"
    assert rig.models(THREAD_A)[-1] == PRIMARY


def test_a_mid_turn_graph_lookup_keeps_the_swapped_model(make_rig):
    """The D3 reset runs at the turn start ONLY: a mid-turn lookup (sub-turn
    compaction here; tool reload and compaction share the lookup) is a cache
    hit on the same graph, and resetting there would put the turn back on the
    down primary mid-turn (a second failure, a second swap and note)."""
    rig = make_rig(llm_fallback_hold_seconds=0)
    rig.down.add(PRIMARY)

    async def compacted(thread_id: str, user_id: str) -> dict[str, Any]:
        return {"success": True, "messages_removed": 0, "summary": "s"}

    rig.agent._do_auto_compact = compacted
    # The re-driven run (on the fallback) asks for a sub-turn compaction,
    # whose resume looks the graph up again (one hook, the first run that
    # completes).
    rig.on_run_end[THREAD_A] = lambda: rig.agent._subturn_compact_requested.add(
        THREAD_A
    )

    events = rig.astream(THREAD_A)

    assert len(_of(events, "compacted")) == 1
    assert rig.models(THREAD_A) == [PRIMARY, FALLBACK, FALLBACK]
    assert len(_of(events, "provider_fallback")) == 1
    assert [tid for tid, _note in rig.notes] == [THREAD_A]
    # The resume reused the cached graph (no rebuild mid-turn).
    assert rig.builds == [(USER, THREAD_A)]
