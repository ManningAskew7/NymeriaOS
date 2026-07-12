"""Phase 1 tests for the dreaming (self-reflection) subsystem.

Covers:
- DreamingConfig serialization on ThreadConfig
- thread_instructions_set tool gating and write-to-parent semantics
- invoke_dream's synchronous setup: shadow thread creation, parent bookkeeping,
  and metadata registration. The daemon dispatch is suppressed via a monkey
  patch so we can assert on the setup side effects without a real agent runtime.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.dreaming import (
    DEFAULT_DREAM_ENABLED_CORE_TOOLS,
    DEFAULT_DREAM_DISABLED_CORE_TOOLS,
    DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS,
    DreamInvocationError,
    invoke_dream,
)
from nymeria.core.agent_graph import compute_tool_superset, select_tools_for_graph
from nymeria.core.thread_config import (
    DreamingConfig,
    ThreadConfig,
    ThreadConfigManager,
    ThreadLLMConfig,
)
from nymeria.core.time_utils import utc_now
from nymeria.core.todo_manager import TodoManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import memory as memory_tools
from nymeria.tools import thread_notes, todo as todo_tools
from nymeria.tools.dream_tools import thread_instructions_set


# ---------------------------------------------------------------------------
# Test fixtures: a stub agent that satisfies invoke_dream's interface.
# ---------------------------------------------------------------------------


class _StubAgent:
    """Minimal stand-in for NymeriaAgent providing only what invoke_dream uses."""

    def __init__(self, data_dir: Path):
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.invalidated: list[str] = []
        self.claims: list[tuple[str, str]] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)


@pytest.fixture
def stub_agent(tmp_path: Path) -> _StubAgent:
    return _StubAgent(tmp_path)


# ---------------------------------------------------------------------------
# DreamingConfig + ThreadConfig serialization
# ---------------------------------------------------------------------------


def test_dreaming_config_default_is_off():
    """A bare DreamingConfig is off, with gates left None to inherit globals."""
    cfg = DreamingConfig()
    assert cfg.enabled is False
    # Thresholds default to None: blank means "inherit the global dreaming
    # default", resolved at dream time (see resolve_dreaming_thresholds).
    assert cfg.min_interval_hours is None
    assert cfg.min_idle_minutes is None
    assert cfg.min_turns_since_last is None
    assert cfg.model is None
    assert cfg.last_dream_at is None
    assert cfg.last_dream_thread_id is None


def test_thread_config_round_trips_dreaming_and_shadow(tmp_path: Path):
    """ThreadConfig with dreaming + shadow_parent_id serializes and reloads."""
    mgr = ThreadConfigManager(tmp_path)
    tc = ThreadConfig(
        thread_id="t1",
        dreaming=DreamingConfig(enabled=True, min_interval_hours=12),
        shadow_parent_id=None,
    )
    assert mgr.save_config(tc)
    loaded = mgr.get_config("t1")
    assert loaded is not None
    assert loaded.dreaming is not None
    assert loaded.dreaming.enabled is True
    assert loaded.dreaming.min_interval_hours == 12
    assert loaded.shadow_parent_id is None


def test_has_customizations_picks_up_dreaming_and_shadow():
    """Both new fields should flip ThreadConfig.has_customizations()."""
    bare = ThreadConfig(thread_id="t1")
    assert bare.has_customizations() is False

    with_dream = ThreadConfig(
        thread_id="t1", dreaming=DreamingConfig(enabled=True)
    )
    assert with_dream.has_customizations() is True

    with_shadow = ThreadConfig(thread_id="t1", shadow_parent_id="parent-1")
    assert with_shadow.has_customizations() is True


def test_dreaming_config_off_with_only_default_gates_is_not_customization():
    """A DreamingConfig with everything at defaults shouldn't dirty the config."""
    tc = ThreadConfig(thread_id="t1", dreaming=DreamingConfig())
    assert tc.has_customizations() is False


# ---------------------------------------------------------------------------
# thread_instructions_set tool
# ---------------------------------------------------------------------------


def _runnable_config(thread_id: str) -> dict[str, Any]:
    """Build the RunnableConfig shape LangChain tools receive."""
    return {"configurable": {"thread_id": thread_id, "user_id": "u1"}}


def test_thread_instructions_set_refuses_outside_dream(stub_agent, tmp_path):
    """Tool errors when the caller is not a shadow thread."""
    # Save a regular (non-shadow) config for the calling thread.
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="caller-1")
    )

    with patch(
        "nymeria.tools.dream_tools.get_current_agent",
        create=True,
        return_value=stub_agent,
    ), patch(
        "nymeria.core.agent.get_current_agent", return_value=stub_agent
    ):
        result = thread_instructions_set.invoke(
            {
                "new_text": "Be brief.",
                "change_summary": "tightening style",
            },
            config=_runnable_config("caller-1"),
        )
    assert isinstance(result, str)
    assert result.startswith("[Error]")
    assert "shadow_parent_id" in result


def test_thread_instructions_set_requires_change_summary(stub_agent):
    """Empty change_summary is rejected outright."""
    result = thread_instructions_set.invoke(
        {"new_text": "Be brief.", "change_summary": ""},
        config=_runnable_config("any"),
    )
    assert result.startswith("[Error]")
    assert "change_summary" in result


def test_thread_instructions_set_writes_to_parent(stub_agent):
    """When called from a shadow thread, the tool writes to the parent."""
    parent_id = "parent-1"
    shadow_id = "dream-parent-1-x"

    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=parent_id, instructions="OLD instructions")
    )
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=shadow_id, shadow_parent_id=parent_id)
    )

    with patch("nymeria.core.agent.get_current_agent", return_value=stub_agent):
        result = thread_instructions_set.invoke(
            {
                "new_text": "NEW instructions, user prefers Australian spelling.",
                "change_summary": "captured Australian-spelling preference",
            },
            config=_runnable_config(shadow_id),
        )

    assert result.startswith("[Saved]"), result
    reloaded = stub_agent.thread_config_manager.get_config(parent_id)
    assert reloaded is not None
    assert reloaded.instructions == "NEW instructions, user prefers Australian spelling."
    assert parent_id in stub_agent.invalidated


def test_thread_instructions_set_clears_when_empty(stub_agent):
    """Empty new_text clears the parent's instructions field."""
    parent_id = "parent-2"
    shadow_id = "dream-parent-2-x"
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=parent_id, instructions="EXISTING text")
    )
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=shadow_id, shadow_parent_id=parent_id)
    )

    with patch("nymeria.core.agent.get_current_agent", return_value=stub_agent):
        result = thread_instructions_set.invoke(
            {"new_text": "", "change_summary": "wiping stale instructions"},
            config=_runnable_config(shadow_id),
        )

    assert result.startswith("[Saved]"), result
    reloaded = stub_agent.thread_config_manager.get_config(parent_id)
    assert reloaded is not None
    assert reloaded.instructions is None


def test_dream_shadow_memory_tools_target_parent(
    stub_agent, monkeypatch, tmp_path
):
    """Thread-scoped memory calls from a dream shadow write the parent notepad."""
    settings = SimpleNamespace(
        data_dir=tmp_path,
        memory_char_limit=8000,
        activity_retention_hours=24,
        user_timezone="UTC",
    )
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(thread_notes, "_notes_dir", None)

    parent_id = "parent-memory"
    shadow_id = "dream-parent-memory-x"
    stub_agent.thread_config_manager.save_config(ThreadConfig(thread_id=parent_id))
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=shadow_id, shadow_parent_id=parent_id)
    )

    with patch("nymeria.core.agent.get_current_agent", return_value=stub_agent):
        write_result = memory_tools.memory_add.func(
            scope="thread",
            content="Parent notepad from dream.",
            config=_runnable_config(shadow_id),
        )
        read_result = memory_tools.memory_read.func(
            scope="thread",
            config=_runnable_config(shadow_id),
        )

    assert write_result.startswith("[Saved]"), write_result
    assert read_result == "Parent notepad from dream."
    assert thread_notes.read_notepad(parent_id) == "Parent notepad from dream."
    assert thread_notes.read_notepad(shadow_id) is None


def test_dream_shadow_todo_tools_target_parent(
    stub_agent, monkeypatch, tmp_path
):
    """TODO tools called from a dream shadow create/list items on the parent."""
    settings = SimpleNamespace(
        data_dir=tmp_path,
        memory_char_limit=8000,
        activity_retention_hours=24,
        user_timezone="UTC",
    )
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(todo_tools, "_todo_manager", TodoManager(tmp_path))

    parent_id = "parent-todo"
    shadow_id = "dream-parent-todo-x"
    stub_agent.thread_config_manager.save_config(ThreadConfig(thread_id=parent_id))
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=shadow_id, shadow_parent_id=parent_id)
    )

    with patch("nymeria.core.agent.get_current_agent", return_value=stub_agent):
        add_result = todo_tools.nym_todo.func(
            task="Review dream follow-up",
            scheduled_for="2h",
            config=_runnable_config(shadow_id),
        )
        list_result = todo_tools.nym_todo_list.func(
            config=_runnable_config(shadow_id),
        )

    assert add_result.startswith("[Added]"), add_result
    assert "Review dream follow-up" in list_result
    todos = todo_tools._get_todo_manager().get_todos("u1").items
    assert len(todos) == 1
    assert todos[0].thread_id == parent_id


def test_dream_graph_uses_strict_tool_allowlist(stub_agent):
    """Dream shadows ignore default/core leakage and only bind dream policy tools."""
    parent_id = "parent-tools"
    shadow_id = "dream-parent-tools-x"
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=shadow_id,
            shadow_parent_id=parent_id,
            enabled_tools=[
                "thread_instructions_set",
                "tool_create",
                "web_search_perplexity",
                "browser_navigate",
            ],
            disabled_tools=["nym_todo_delete"],
        )
    )

    tools, tc = select_tools_for_graph(stub_agent, "u1", shadow_id)
    names = {tool.name for tool in tools}
    superset_tools, superset_names = compute_tool_superset(stub_agent, "u1", shadow_id)

    assert tc is not None and tc.shadow_parent_id == parent_id
    assert set(DEFAULT_DREAM_ENABLED_CORE_TOOLS) - {"nym_todo_delete"} <= names
    assert "thread_instructions_set" in names
    assert "tool_create" in names
    assert "web_search_perplexity" not in names
    assert "browser_navigate" not in names
    assert "bash_execute" not in names
    assert "nym_todo_delete" not in names
    assert {tool.name for tool in superset_tools} == names
    assert superset_names == names


# ---------------------------------------------------------------------------
# invoke_dream synchronous setup
# ---------------------------------------------------------------------------


@pytest.fixture
def no_daemon_dispatch(monkeypatch):
    """Suppress the background daemon thread so tests can assert on setup only."""

    class _NoopThread:
        def __init__(self, *args, **kwargs):
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(
        "nymeria.core.dreaming.invoke.threading.Thread", _NoopThread
    )
    return _NoopThread


def test_invoke_dream_creates_shadow_thread_with_whitelist(
    stub_agent, no_daemon_dispatch
):
    """Happy path: invoke_dream sets up a shadow thread with the expected config."""
    parent_id = "parent-happy"
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=parent_id,
            instructions="Be terse.",
            llm_config=ThreadLLMConfig(model="claude-sonnet-4-6"),
            dreaming=DreamingConfig(enabled=True),
        )
    )

    shadow_id, summary = invoke_dream(
        stub_agent,
        parent_thread_id=parent_id,
        user_id="u1",
    )

    assert shadow_id.startswith("dream-")
    assert summary["parent_thread_id"] == parent_id
    assert summary["enabled_optional_tools"] == list(
        DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS
    )
    assert summary["disabled_core_tools"] == list(
        DEFAULT_DREAM_DISABLED_CORE_TOOLS
    )

    shadow_tc = stub_agent.thread_config_manager.get_config(shadow_id)
    assert shadow_tc is not None
    assert shadow_tc.shadow_parent_id == parent_id
    assert shadow_tc.system_prompt is not None
    assert "dream" in shadow_tc.system_prompt.lower()
    assert shadow_tc.llm_config is not None
    # The shadow should inherit the parent's model when no override is given.
    assert shadow_tc.llm_config.model == "claude-sonnet-4-6"
    assert "thread_instructions_set" in shadow_tc.enabled_tools
    assert "bash_execute" in shadow_tc.disabled_tools

    # Metadata registered with dream platform + temporary lifetime.
    meta = stub_agent.thread_metadata_manager.get_thread("u1", shadow_id)
    assert meta is not None
    assert meta.platform == "dream"
    assert meta.platform_meta is not None
    assert meta.platform_meta.get("shadow_parent") == parent_id
    assert meta.platform_meta.get("lifetime") == "temporary"

    # Parent bookkeeping was updated.
    parent_after = stub_agent.thread_config_manager.get_config(parent_id)
    assert parent_after is not None
    assert parent_after.dreaming is not None
    assert parent_after.dreaming.last_dream_at is not None
    assert parent_after.dreaming.last_dream_thread_id == shadow_id


def test_invoke_dream_applies_model_override(stub_agent, no_daemon_dispatch):
    """An explicit model override should win over the parent's llm_config.model."""
    parent_id = "parent-override"
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=parent_id,
            llm_config=ThreadLLMConfig(model="claude-sonnet-4-6"),
            dreaming=DreamingConfig(enabled=True),
        )
    )

    shadow_id, summary = invoke_dream(
        stub_agent,
        parent_thread_id=parent_id,
        user_id="u1",
        model_override="claude-haiku-4-5-20251001",
    )

    shadow_tc = stub_agent.thread_config_manager.get_config(shadow_id)
    assert shadow_tc is not None
    assert shadow_tc.llm_config is not None
    assert shadow_tc.llm_config.model == "claude-haiku-4-5-20251001"
    assert summary["model"] == "claude-haiku-4-5-20251001"


def test_invoke_dream_rejects_shadow_as_parent(stub_agent, no_daemon_dispatch):
    """You cannot dream from inside a shadow thread (would chain reflections)."""
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="shadow-1", shadow_parent_id="real-parent")
    )
    with pytest.raises(DreamInvocationError) as exc:
        invoke_dream(
            stub_agent,
            parent_thread_id="shadow-1",
            user_id="u1",
        )
    assert "shadow" in str(exc.value).lower()


def test_invoke_dream_rejects_missing_inputs(stub_agent, no_daemon_dispatch):
    """Empty parent_thread_id or user_id are guard-railed."""
    with pytest.raises(DreamInvocationError):
        invoke_dream(stub_agent, parent_thread_id="", user_id="u1")
    with pytest.raises(DreamInvocationError):
        invoke_dream(stub_agent, parent_thread_id="t1", user_id="")


def test_idle_sweeper_deletes_temporary_dream_threads(stub_agent, monkeypatch):
    """Temporary cleanup must handle dream-* IDs, not only spawned-* IDs."""
    import importlib

    spawn_thread_mod = importlib.import_module("nymeria.tools.spawn_thread")
    shadow_id = "dream-parent-cleanup-x"
    called: list[tuple[str, str]] = []

    stub_agent.thread_metadata_manager.upsert_thread(
        "u1",
        shadow_id,
        title="Dream cleanup",
        platform="dream",
        platform_meta={
            spawn_thread_mod.PLATFORM_META_LIFETIME: "temporary",
            spawn_thread_mod.PLATFORM_META_IDLE_TIMEOUT: "1",
            spawn_thread_mod.PLATFORM_META_LAST_ACTIVE: (
                utc_now() - timedelta(hours=2)
            ).isoformat(),
        },
    )

    def fake_delete(agent, target_thread_id: str, user_id: str) -> bool:
        called.append((target_thread_id, user_id))
        return True

    monkeypatch.setattr(spawn_thread_mod, "_delete_temporary_thread", fake_delete)

    deleted = spawn_thread_mod.sweep_idle_spawned_threads(stub_agent)

    assert deleted == 1
    assert called == [(shadow_id, "u1")]


# ---------------------------------------------------------------------------
# _run_dream_cycle daemon body: SSE event emission + summary handling.
#
# invoke_dream's daemon dispatch is suppressed in the setup tests above. These
# exercise the background body directly with a canned stream so we cover the
# event contract frontends rely on (task_started/task_completed on the shadow,
# dream_completed on the parent) without a real LLM runtime.
# ---------------------------------------------------------------------------


class _EventSink:
    """Capture the three event-bus publishers the dream cycle uses."""

    def __init__(self):
        self.autonomous: list[dict[str, Any]] = []
        self.sync: list[dict[str, Any]] = []
        self.stream_chunks: list[dict[str, Any]] = []

    def install(self, monkeypatch):
        import nymeria.core.event_bus as bus

        def _auto(event_type, thread_id, user_id, task_id, data):
            self.autonomous.append(
                {
                    "event_type": event_type,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "task_id": task_id,
                    "data": data,
                }
            )

        def _sync(event_type, thread_id, user_id, data, origin_client_id=""):
            self.sync.append(
                {
                    "event_type": event_type,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "data": data,
                }
            )

        def _chunk(chunk, *, thread_id, user_id, task_id):
            self.stream_chunks.append(
                {"chunk": chunk, "thread_id": thread_id, "task_id": task_id}
            )
            return True

        monkeypatch.setattr(bus, "publish_autonomous_event", _auto)
        monkeypatch.setattr(bus, "publish_sync_event", _sync)
        monkeypatch.setattr(bus, "publish_agent_stream_chunk", _chunk)
        return self


def _fake_stream(chunks, *, response_parts=None, iteration_limit_hit=False, raises=None):
    """Build a stand-in for stream_bridge.stream_and_collect.

    Replays ``chunks`` through ``on_chunk`` (so task_started fires on the first
    non-queued chunk), then returns a StreamCollection — or raises if asked.
    """
    from nymeria.core.stream_bridge import StreamCollection

    def _impl(agent, *, astream_kwargs, on_chunk=None, error_message_factory=None):
        collection = StreamCollection()
        collection.iteration_limit_hit = iteration_limit_hit
        if raises is not None:
            # Replay any chunks queued before the failure, then blow up.
            for c in chunks:
                if on_chunk is not None:
                    on_chunk(c, collection)
            raise raises
        for c in chunks:
            if on_chunk is not None:
                on_chunk(c, collection)
        if response_parts:
            collection.response_parts = list(response_parts)
        return collection

    return _impl


def _run_cycle(monkeypatch, stub_agent, fake_stream):
    from nymeria.core.dreaming import invoke as invoke_mod
    import nymeria.core.stream_bridge as bridge

    monkeypatch.setattr(bridge, "stream_and_collect", fake_stream)
    invoke_mod._run_dream_cycle(
        agent=stub_agent,
        shadow_thread_id="dream-parent-1-x",
        parent_thread_id="parent-1",
        user_id="u1",
        initial_prompt="[Dream cycle starting]",
        title="Dream: parent-1",
    )


def test_run_dream_cycle_emits_started_completed_and_dream_completed(
    stub_agent, monkeypatch
):
    """Happy path: started + completed on shadow, dream_completed on parent."""
    sink = _EventSink().install(monkeypatch)
    fake = _fake_stream(
        chunks=[
            {"type": "queued"},  # must NOT trigger task_started
            {"type": "thinking", "content": "looking at memory"},
            {"type": "response", "content": "Pruned 2 stale facts."},
        ],
        response_parts=["Pruned 2 stale facts."],
    )
    _run_cycle(monkeypatch, stub_agent, fake)

    started = [e for e in sink.autonomous if e["event_type"] == "task_started"]
    completed = [e for e in sink.autonomous if e["event_type"] == "task_completed"]
    assert len(started) == 1, sink.autonomous
    # task_started must not fire on the "queued" chunk.
    assert started[0]["data"]["trigger"] == "dream"
    assert started[0]["data"]["parent_thread_id"] == "parent-1"
    assert started[0]["thread_id"] == "dream-parent-1-x"

    assert len(completed) == 1
    assert completed[0]["data"].get("error") is not True
    assert completed[0]["data"]["content"] == "Pruned 2 stale facts."
    assert completed[0]["thread_id"] == "dream-parent-1-x"

    dream_done = [e for e in sink.sync if e["event_type"] == "dream_completed"]
    assert len(dream_done) == 1
    assert dream_done[0]["thread_id"] == "parent-1"  # keyed to the PARENT
    assert dream_done[0]["data"]["shadow_thread_id"] == "dream-parent-1-x"
    assert dream_done[0]["data"]["summary"] == "Pruned 2 stale facts."


def test_run_dream_cycle_marks_iteration_limit(stub_agent, monkeypatch):
    """Hitting the iteration limit with no summary yields the sentinel text."""
    sink = _EventSink().install(monkeypatch)
    fake = _fake_stream(
        chunks=[{"type": "thinking", "content": "..."}],
        response_parts=None,
        iteration_limit_hit=True,
    )
    _run_cycle(monkeypatch, stub_agent, fake)

    completed = [e for e in sink.autonomous if e["event_type"] == "task_completed"]
    assert len(completed) == 1
    assert "iteration limit" in completed[0]["data"]["content"].lower()


def test_run_dream_cycle_publishes_error_event_on_failure(stub_agent, monkeypatch):
    """A stream failure surfaces as task_completed{error:True}, never raised."""
    sink = _EventSink().install(monkeypatch)
    fake = _fake_stream(
        chunks=[{"type": "thinking", "content": "..."}],
        raises=RuntimeError("provider exploded"),
    )
    # Must not raise out of the daemon body.
    _run_cycle(monkeypatch, stub_agent, fake)

    completed = [e for e in sink.autonomous if e["event_type"] == "task_completed"]
    assert len(completed) == 1
    assert completed[0]["data"]["error"] is True
    assert "provider exploded" in completed[0]["data"]["error_message"]
    # No dream_completed on the parent when the cycle failed.
    assert not [e for e in sink.sync if e["event_type"] == "dream_completed"]


def test_run_dream_cycle_dispatches_with_dream_source(stub_agent, monkeypatch):
    """The dream turn rides the standard source taxonomy.

    ``source="dream"`` is what makes lifecycle hooks see
    ``holder_kind="dream"`` (targetable/excludable via fire_conditions)
    instead of the dream masquerading as a "ticker" turn.
    ``_is_self_invoke`` stays as the legacy autonomous back-stop.
    """
    import nymeria.core.stream_bridge as bridge
    from nymeria.core.dreaming import invoke as invoke_mod
    from nymeria.core.stream_bridge import StreamCollection

    _EventSink().install(monkeypatch)
    seen: dict[str, Any] = {}

    def _capture(agent, *, astream_kwargs, on_chunk=None, error_message_factory=None):
        seen.update(astream_kwargs)
        return StreamCollection()

    monkeypatch.setattr(bridge, "stream_and_collect", _capture)
    invoke_mod._run_dream_cycle(
        agent=stub_agent,
        shadow_thread_id="dream-parent-1-x",
        parent_thread_id="parent-1",
        user_id="u1",
        initial_prompt="[Dream cycle starting]",
        title="Dream: parent-1",
    )

    assert seen["source"] == "dream"
    assert seen["_is_self_invoke"] is True
    assert seen["_trigger_override"] == 'Dream("parent-1")'
    assert seen["thread_id"] == "dream-parent-1-x"


# ---------------------------------------------------------------------------
# Strand 2 alignment: prompt phases <-> tool allowlist, and shadow retargeting
# of the skill/trigger management surfaces.
# ---------------------------------------------------------------------------


def test_log_user_turn_activity_gates_and_records(monkeypatch):
    """The shared chat/astream activity helper feeds the dream gates.

    Only genuine user turns record USER_MESSAGE (turns/idle gate inputs).
    Excluded: self-invoked turns, every non-"user" source (autonomous
    trigger/ticker/watchdog/dream AND programmatic callable/mcp), and
    message-less resumes. A None source counts as "user" (the default for
    direct user turns).
    """
    import nymeria.core.activity_log as al
    from nymeria.core.agent import NymeriaAgent

    calls: list[tuple] = []
    monkeypatch.setattr(
        al,
        "log_activity",
        lambda t, d, user_id=None, thread_id=None: calls.append(
            (t, d, user_id, thread_id)
        ),
    )

    NymeriaAgent._log_user_turn_activity(
        "hello\nworld", user_id="u1", thread_id="t1",
        source="user", is_self_invoke=False,
    )
    NymeriaAgent._log_user_turn_activity(
        "via default source", user_id="u1", thread_id="t1",
        source=None, is_self_invoke=False,
    )
    assert len(calls) == 2
    assert calls[0][0] == al.ActivityType.USER_MESSAGE
    assert calls[0][1] == "hello world"
    assert calls[0][2:] == ("u1", "t1")

    for source, self_invoke in (
        ("dream", True),      # dream cycle
        ("ticker", True),     # scheduled TODO
        ("callable", False),  # agent-to-agent ask: programmatic, not a user
        ("mcp", False),       # MCP client relay
        ("user", True),       # self-invoked wakeup on a user-source path
    ):
        NymeriaAgent._log_user_turn_activity(
            "x", user_id="u1", thread_id="t1",
            source=source, is_self_invoke=self_invoke,
        )
    NymeriaAgent._log_user_turn_activity(
        "x", user_id="u1", thread_id="t1",
        source="user", is_self_invoke=False, resumed=True,
    )
    assert len(calls) == 2


def test_dream_prompt_covers_scoped_surfaces():
    """The shipped dream prompt directs every allowlisted surface and no more.

    Scope decided 2026-07-12 (backlog #24): memory, notepad, instructions,
    TODOs, skills/kits, and trigger review. tool_create stays allowed for
    admins but deliberately unmentioned (autonomous tool authoring is parked
    backlog #58); hook management is out of dream reach entirely.
    """
    import nymeria.config as config_pkg

    text = (Path(config_pkg.__file__).parent / "dream_prompt.md").read_text()

    for mentioned in (
        "memory_add",
        "memory_edit",
        "memory_read",
        "thread_instructions_set",
        "nym_todo",
        "skill_manage",
        "skill_edit",
        "skill_write",
        "list_installed_skills",
        "trigger_info",
        "trigger_config",
    ):
        assert mentioned in text, f"dream prompt no longer directs {mentioned}"

    for absent in ("tool_create", "hook_config", "hook_info", "bash_execute"):
        assert absent not in text, f"dream prompt must not mention {absent}"


def test_default_dream_allowlist_includes_trigger_tools(stub_agent):
    """Trigger review tools ride the default optional allowlist and bind."""
    from nymeria.core.dreaming.invoke import DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS

    assert {"trigger_config", "trigger_info"} <= set(
        DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS
    )

    shadow_id = "dream-parent-trig-x"
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=shadow_id,
            shadow_parent_id="parent-trig",
            enabled_tools=sorted(DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS),
        )
    )
    tools, _tc = select_tools_for_graph(stub_agent, "u1", shadow_id)
    names = {tool.name for tool in tools}
    assert {"trigger_config", "trigger_info"} <= names


def test_dream_shadow_skill_enablement_targets_parent(stub_agent, monkeypatch):
    """skill_manage enable/disable from a shadow writes the PARENT thread config
    and never runs the same-turn reload dance (no Command, no STOP NOW)."""
    import importlib

    from langgraph.types import Command

    # The package attribute ``nymeria.tools.search_skills`` is the TOOL (the
    # package __init__ rebinds it); import the module explicitly.
    skills_mod = importlib.import_module("nymeria.tools.search_skills")

    parent_id = "parent-skills"
    shadow_id = "dream-parent-skills-x"
    stub_agent.thread_config_manager.save_config(ThreadConfig(thread_id=parent_id))
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=shadow_id, shadow_parent_id=parent_id)
    )
    stub_agent.skill_manager = SimpleNamespace(
        get=lambda name, user_id=None: SimpleNamespace(
            name=name,
            description="d",
            scope="user",
            required_tools=[],
            tool_ttl="2h",
            is_skill_kit=False,
            has_scripts=False,
            has_references=False,
        )
    )

    with patch("nymeria.core.agent.get_current_agent", return_value=stub_agent):
        result = skills_mod.skill_manage.func(
            action="enable",
            name="focus-mode",
            tool_call_id="tc-1",
            config=_runnable_config(shadow_id),
        )

    assert not isinstance(result, Command), "retargeted enable must not force-end"
    assert "STOP NOW" not in str(result)
    assert f"applied to thread {parent_id}" in str(result)

    parent_tc = stub_agent.thread_config_manager.get_config(parent_id)
    shadow_tc = stub_agent.thread_config_manager.get_config(shadow_id)
    assert "focus-mode" in parent_tc.enabled_skills
    assert "focus-mode" not in (shadow_tc.enabled_skills or [])


def test_dream_shadow_trigger_create_binds_parent(stub_agent, monkeypatch):
    """trigger_config create from a shadow binds the trigger to the PARENT."""
    from nymeria.tools import triggers as triggers_mod

    parent_id = "parent-trigger"
    shadow_id = "dream-parent-trigger-x"
    stub_agent.thread_config_manager.save_config(ThreadConfig(thread_id=parent_id))
    stub_agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=shadow_id, shadow_parent_id=parent_id)
    )

    captured: dict[str, Any] = {}

    def fake_add_trigger(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="trg-1", source_config=kwargs.get("source_config") or {},
            thread_id=kwargs.get("thread_id"),
        )

    monkeypatch.setattr(
        triggers_mod,
        "_get_trigger_manager",
        lambda: SimpleNamespace(add_trigger=fake_add_trigger),
    )

    with patch("nymeria.core.agent.get_current_agent", return_value=stub_agent):
        result = triggers_mod.trigger_config.func(
            action="create",
            name="dream-made",
            source_type="webhook",
            action_type="notify",
            action_config={"message_template": "hi"},
            source_config={"secret": "s3cret"},
            config=_runnable_config(shadow_id),
        )

    assert result.startswith("[Success]"), result
    assert captured["thread_id"] == parent_id
