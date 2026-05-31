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
    """A bare DreamingConfig defaults to enabled=False with sane gates."""
    cfg = DreamingConfig()
    assert cfg.enabled is False
    assert cfg.min_interval_hours == 6
    assert cfg.min_idle_minutes == 30
    assert cfg.min_turns_since_last == 10
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
