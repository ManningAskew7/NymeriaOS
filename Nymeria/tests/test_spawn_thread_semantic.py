"""Tests for spawn_thread semantic resolution, TTL knob, and core opt-out.

Covers the four ergonomic params added on top of the original spawn_thread:
  - tool_queries / tool_query_top_k
  - ttl_hours
  - include_core_tools
And confirms the existing prompt + instructions paths still work.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import SEED_TOOLS
from nymeria.tools.spawn_thread import (
    PLATFORM_META_IDLE_TIMEOUT,
    PLATFORM_META_LAST_ACTIVE,
    PLATFORM_META_LIFETIME,
    _invoke_spawned,
    _resolve_semantic_tools,
    spawn_thread,
)


class _StubAccountsRepo:
    def __init__(self) -> None:
        self.claimed: list[tuple[str, str]] = []

    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(role="user")

    def claim_thread(self, thread_id: str, user_id: str) -> None:
        self.claimed.append((thread_id, user_id))


class _StubAgent:
    """Minimal stand-in for NymeriaAgent providing only what spawn_thread uses."""

    def __init__(self, data_dir: Path) -> None:
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.accounts_repo = _StubAccountsRepo()
        self.tool_registry = None
        self.invalidated: list[str] = []
        self.synced: int = 0

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)

    def sync_agent_tools(self) -> None:
        self.synced += 1


@pytest.fixture
def stub_agent(tmp_path: Path) -> _StubAgent:
    return _StubAgent(tmp_path)


@pytest.fixture(autouse=True)
def _patch_current_agent(stub_agent):
    with patch(
        "nymeria.core.agent.get_current_agent", return_value=stub_agent
    ), patch(
        "nymeria.core.event_bus.publish_sync_event", lambda *a, **kw: None
    ):
        import importlib

        spawn_thread_module = importlib.import_module("nymeria.tools.spawn_thread")
        with spawn_thread_module._spawn_rate_lock:
            spawn_thread_module._spawn_counts.clear()
        yield


def _runnable_config(parent_thread_id: str | None = "parent-1") -> dict[str, Any]:
    cfg: dict[str, Any] = {"configurable": {"user_id": "u1"}}
    if parent_thread_id is not None:
        cfg["configurable"]["thread_id"] = parent_thread_id
    return cfg


def _extract_thread_id(preamble: str) -> str:
    import re

    m = re.search(r"thread_id=(spawned-[A-Za-z0-9-]+)", preamble)
    assert m, f"no thread_id in preamble: {preamble}"
    return m.group(1)


# ---------------------------------------------------------------------------
# ttl_hours
# ---------------------------------------------------------------------------


class TestTtlHours:
    def test_none_is_permanent(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "permanent child"},
            config=_runnable_config(),
        )
        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        meta = stub_agent.thread_metadata_manager.get_thread("u1", new_id)
        assert meta is not None
        assert (
            not meta.platform_meta
            or PLATFORM_META_LIFETIME not in meta.platform_meta
        )
        assert "Lifetime: temporary" not in result

    def test_positive_marks_temporary(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "temp child", "ttl_hours": 6},
            config=_runnable_config(),
        )
        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        meta = stub_agent.thread_metadata_manager.get_thread("u1", new_id)
        assert meta is not None
        assert meta.platform_meta is not None
        assert meta.platform_meta.get(PLATFORM_META_LIFETIME) == "temporary"
        assert meta.platform_meta.get(PLATFORM_META_IDLE_TIMEOUT) == "6"
        assert meta.platform_meta.get(PLATFORM_META_LAST_ACTIVE)
        assert "auto-deletes after 6h" in result

    def test_zero_rejected(self):
        result = spawn_thread.invoke(
            {"title": "bad ttl", "ttl_hours": 0},
            config=_runnable_config(),
        )
        assert result.startswith("[Error]")
        assert "ttl_hours" in result

    def test_negative_rejected(self):
        result = spawn_thread.invoke(
            {"title": "bad ttl", "ttl_hours": -1},
            config=_runnable_config(),
        )
        assert result.startswith("[Error]")
        assert "ttl_hours" in result

    def test_legacy_lifetime_temporary_alias_still_marks_temporary(self, stub_agent):
        result = spawn_thread.invoke(
            {
                "title": "legacy temp child",
                "lifetime": "temporary",
                "idle_timeout_hours": 3,
            },
            config=_runnable_config(),
        )
        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        meta = stub_agent.thread_metadata_manager.get_thread("u1", new_id)
        assert meta is not None
        assert meta.platform_meta is not None
        assert meta.platform_meta.get(PLATFORM_META_LIFETIME) == "temporary"
        assert meta.platform_meta.get(PLATFORM_META_IDLE_TIMEOUT) == "3"
        assert "auto-deletes after 3h" in result

    def test_legacy_lifetime_rejects_unknown_value(self):
        result = spawn_thread.invoke(
            {"title": "bad legacy", "lifetime": "ephemeral"},
            config=_runnable_config(),
        )
        assert result.startswith("[Error]")
        assert "Unknown lifetime" in result


# ---------------------------------------------------------------------------
# tool_queries / semantic resolution
# ---------------------------------------------------------------------------


def _fake_search_response(results: list[dict[str, Any]]):
    return SimpleNamespace(
        results=[
            SimpleNamespace(name=r["name"], score=r["score"]) for r in results
        ]
    )


class TestToolQueries:
    def test_resolves_via_search_index(self, stub_agent):
        # browser_navigate is an optional tool name; bash_execute is core (should be
        # filtered out); zzz_unknown is unknown (should be filtered out).
        responses = {
            "research": _fake_search_response(
                [
                    {"name": "browser_navigate", "score": 0.92},
                    {"name": "bash_execute", "score": 0.88},
                    {"name": "zzz_unknown", "score": 0.5},
                ]
            ),
        }

        def fake_search(query, **_kw):
            return responses.get(query, _fake_search_response([]))

        with patch(
            "nymeria.core.tool_search_index.search_tools",
            side_effect=fake_search,
        ):
            result = spawn_thread.invoke(
                {
                    "title": "researcher",
                    "tool_queries": ["research"],
                    "tool_query_top_k": 5,
                },
                config=_runnable_config(),
            )

        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(new_id)
        assert tc is not None
        assert "browser_navigate" in tc.enabled_tools
        assert "bash_execute" not in tc.enabled_tools
        assert "zzz_unknown" not in tc.enabled_tools

    def test_preamble_includes_resolved_line(self, stub_agent):
        def fake_search(query, **_kw):
            return _fake_search_response(
                [{"name": "browser_navigate", "score": 0.88}]
            )

        with patch(
            "nymeria.core.tool_search_index.search_tools",
            side_effect=fake_search,
        ):
            result = spawn_thread.invoke(
                {"title": "r", "tool_queries": ["research"]},
                config=_runnable_config(),
            )

        assert "[Resolved tools]:" in result
        assert "browser_navigate" in result
        assert "0.88" in result
        assert '"research"' in result

    def test_unmatched_query_emits_warning(self, stub_agent):
        with patch(
            "nymeria.core.tool_search_index.search_tools",
            return_value=_fake_search_response([]),
        ):
            result = spawn_thread.invoke(
                {"title": "r", "tool_queries": ["totally-unknown-intent"]},
                config=_runnable_config(),
            )

        assert "[Warning]" in result
        assert "tool_queries with no matches" in result
        assert "totally-unknown-intent" in result

    def test_search_failure_does_not_break_spawn(self, stub_agent):
        with patch(
            "nymeria.core.tool_search_index.search_tools",
            side_effect=RuntimeError("index unavailable"),
        ):
            result = spawn_thread.invoke(
                {"title": "r", "tool_queries": ["research"]},
                config=_runnable_config(),
            )

        assert not result.startswith("[Error]"), result
        # No resolved tools line when no matches survived.
        assert "[Resolved tools]:" not in result


class TestResolveSemanticToolsUnit:
    def test_dedupes_keeping_highest_score(self, stub_agent):
        def fake_search(query, **_kw):
            if query == "a":
                return _fake_search_response([{"name": "browser_navigate", "score": 0.5}])
            if query == "b":
                return _fake_search_response([{"name": "browser_navigate", "score": 0.9}])
            return _fake_search_response([])

        with patch(
            "nymeria.core.tool_search_index.search_tools",
            side_effect=fake_search,
        ):
            names, records = _resolve_semantic_tools(
                ["a", "b"],
                top_k=3,
                agent=stub_agent,
                user_id="u1",
                user_role="user",
                parent_thread_id=None,
            )

        assert names == {"browser_navigate"}
        assert len(records) == 1
        assert records[0]["score"] == 0.9
        assert records[0]["query"] == "b"

    def test_ignores_blank_queries(self, stub_agent):
        called: list[str] = []

        def fake_search(query, **_kw):
            called.append(query)
            return _fake_search_response([])

        with patch(
            "nymeria.core.tool_search_index.search_tools",
            side_effect=fake_search,
        ):
            _resolve_semantic_tools(
                ["", "   ", "real"],
                top_k=3,
                agent=stub_agent,
                user_id="u1",
                user_role="user",
                parent_thread_id=None,
            )

        assert called == ["real"]


# ---------------------------------------------------------------------------
# include_core_tools
# ---------------------------------------------------------------------------


class TestIncludeCoreTools:
    def test_false_disables_every_core_tool(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "focused", "include_core_tools": False},
            config=_runnable_config(),
        )
        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(new_id)
        assert tc is not None
        for tool in SEED_TOOLS:
            assert tool.name in tc.disabled_tools, (
                f"core tool {tool.name} not disabled"
            )
        assert "Core tools: disabled" in result
        # The Disabled-core-tools dump line is suppressed when core is fully off
        # (we already say "Core tools: disabled"); don't double-print.
        assert "Disabled core tools:" not in result

    def test_true_preserves_default_behavior(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "open", "include_core_tools": True},
            config=_runnable_config(),
        )
        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(new_id)
        assert tc is not None
        assert tc.disabled_tools == []
        assert "Core tools: disabled" not in result


# ---------------------------------------------------------------------------
# disabled_tools validation
# ---------------------------------------------------------------------------


class TestDisabledToolsValidation:
    def test_valid_optional_name_disables_without_unknown_core_warning(self, stub_agent):
        # Regression: a valid catalog (optional) tool named in disabled_tools must
        # land in disabled_tools and NOT be mislabeled an "unknown core tool".
        result = spawn_thread.invoke(
            {"title": "t", "disabled_tools": ["web_search_tavily"]},
            config=_runnable_config(),
        )
        assert not result.startswith("[Error]"), result
        assert "unknown core tool" not in result
        new_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(new_id)
        assert "web_search_tavily" in tc.disabled_tools

    def test_genuinely_unknown_name_warns_as_unknown_tool(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "t", "disabled_tools": ["not_a_real_tool_xyz"]},
            config=_runnable_config(),
        )
        assert "unknown tool(s) to disable" in result
        assert "not_a_real_tool_xyz" in result


# ---------------------------------------------------------------------------
# instructions + prompt regression
# ---------------------------------------------------------------------------


class TestExistingArgsStillWork:
    def test_instructions_persisted(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "haiku-bot", "instructions": "Reply only in haiku."},
            config=_runnable_config(),
        )
        new_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(new_id)
        assert tc is not None
        assert tc.instructions == "Reply only in haiku."

    def test_prompt_dispatches_via_invoke_spawned(self, stub_agent):
        captured: dict[str, Any] = {}

        def fake_invoke(**kw):
            captured.update(kw)
            return "child says: hello back"

        with patch(
            "nymeria.tools.spawn_thread._invoke_spawned",
            side_effect=fake_invoke,
        ):
            result = spawn_thread.invoke(
                {"title": "responder", "prompt": "Hello there"},
                config=_runnable_config(),
            )

        assert captured.get("task") == "Hello there"
        assert "child says: hello back" in result


# ---------------------------------------------------------------------------
# _invoke_spawned internals (the autonomous-turn SSE scaffold, slice 18 F3)
# ---------------------------------------------------------------------------


class TestInvokeSpawnedInternals:
    """Lock the SSE-publishing behavior of _invoke_spawned through the refactor
    onto the shared AutonomousTurnEmitter (slice 18 F3)."""

    @staticmethod
    def _spawn_agent(register, unregister, parent_title="Parent Title"):
        meta = (
            SimpleNamespace(title=parent_title) if parent_title is not None else None
        )
        return SimpleNamespace(
            register_callable_invocation=register,
            unregister_callable_invocation=unregister,
            thread_metadata_manager=SimpleNamespace(
                get_thread=lambda uid, tid: meta
            ),
        )

    @staticmethod
    def _patch_publishers(monkeypatch, events, chunks):
        def fake_publish_autonomous_event(
            event_type, thread_id, user_id, task_id, data
        ):
            events.append((event_type, thread_id, user_id, task_id, data))

        def fake_publish_agent_stream_chunk(chunk, *, thread_id, user_id, task_id):
            chunks.append(chunk)
            return True

        monkeypatch.setattr(
            "nymeria.core.event_bus.publish_autonomous_event",
            fake_publish_autonomous_event,
        )
        monkeypatch.setattr(
            "nymeria.core.event_bus.publish_agent_stream_chunk",
            fake_publish_agent_stream_chunk,
        )

    def test_publishes_bookends_trigger_and_registers(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        captured_kwargs: dict[str, Any] = {}
        registered: list[tuple[str, str]] = []
        unregistered: list[tuple[str, str]] = []
        self._patch_publishers(monkeypatch, events, chunks)

        class FakeStreamResult:
            iteration_limit_hit = False

            def response_text(self, *, fallback_to_thinking: bool = True) -> str:
                return "child reply"

        def fake_stream_and_collect(
            agent_arg, *, astream_kwargs, on_chunk, error_message_factory
        ):
            captured_kwargs.update(astream_kwargs)
            on_chunk({"type": "response", "content": "child reply"}, SimpleNamespace())
            return FakeStreamResult()

        monkeypatch.setattr(
            "nymeria.core.stream_bridge.stream_and_collect",
            fake_stream_and_collect,
        )
        agent = self._spawn_agent(
            register=lambda p, c: registered.append((p, c)),
            unregister=lambda p, c: unregistered.append((p, c)),
        )

        result = _invoke_spawned(
            agent,
            child_thread_id="child-1",
            parent_thread_id="parent-1",
            title="ResponderBot",
            task="do the thing",
            user_id="u1",
        )

        assert result == "child reply"
        assert [e[0] for e in events] == ["task_started", "task_completed"]
        assert events[0][1] == "child-1" and events[0][2] == "u1"
        # both bookend events carry the same spawned-* task id
        assert events[0][3] == events[1][3]
        assert events[0][3].startswith("spawned-")
        assert events[0][4] == {
            "prompt": "do the thing",
            "callable_name": "ResponderBot",
            "trigger": "spawn_thread",
        }
        assert events[1][4] == {"content": "child reply", "callable_name": "ResponderBot"}
        assert chunks == [{"type": "response", "content": "child reply"}]
        assert captured_kwargs["_trigger_override"] == 'SpawnedBy("parent-1", "Parent Title")'
        assert captured_kwargs["_is_self_invoke"] is True
        assert captured_kwargs["message"] == "do the thing"
        assert registered == [("parent-1", "child-1")]
        assert unregistered == [("parent-1", "child-1")]

    def test_error_path_returns_error_and_skips_task_started(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        unregistered: list[tuple[str, str]] = []
        self._patch_publishers(monkeypatch, events, chunks)

        def boom(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("nymeria.core.stream_bridge.stream_and_collect", boom)
        agent = self._spawn_agent(
            register=lambda p, c: None,
            unregister=lambda p, c: unregistered.append((p, c)),
        )

        result = _invoke_spawned(
            agent,
            child_thread_id="child-1",
            parent_thread_id="parent-1",
            title="Bot",
            task="t",
            user_id="u1",
        )

        assert result == "[Error]: Initial message failed: kaboom"
        # spawn does not force task_started: an error before any chunk yields only
        # the error task_completed event.
        assert [e[0] for e in events] == ["task_completed"]
        err = events[0][4]
        assert err == {
            "error": True,
            "error_message": "kaboom",
            "content": "Task failed: kaboom",
            "callable_name": "Bot",
        }
        assert unregistered == [("parent-1", "child-1")]  # finally still runs

    def test_iteration_limit_appends_note_to_response_and_completed(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        self._patch_publishers(monkeypatch, events, chunks)

        class FakeStreamResult:
            iteration_limit_hit = True

            def response_text(self, *, fallback_to_thinking: bool = True) -> str:
                return "partial answer"

        def fake_stream_and_collect(
            agent_arg, *, astream_kwargs, on_chunk, error_message_factory
        ):
            on_chunk({"type": "response", "content": "partial answer"}, SimpleNamespace())
            return FakeStreamResult()

        monkeypatch.setattr(
            "nymeria.core.stream_bridge.stream_and_collect",
            fake_stream_and_collect,
        )
        agent = self._spawn_agent(register=lambda p, c: None, unregister=lambda p, c: None)

        result = _invoke_spawned(
            agent,
            child_thread_id="child-1",
            parent_thread_id=None,
            title="Bot",
            task="t",
            user_id="u1",
        )

        assert "partial answer" in result
        assert result.endswith("result may be incomplete.]")
        # the completed event carries the same iteration-limit-adjusted text
        assert events[-1][0] == "task_completed"
        assert events[-1][4]["content"] == result
