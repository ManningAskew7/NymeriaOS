"""Tests for spawn_thread semantic resolution, TTL knob, and core opt-out.

Covers the four ergonomic params added on top of the original spawn_thread:
  - tool_queries / tool_query_top_k
  - ttl_hours
  - include_core_tools
And confirms the existing prompt + instructions paths still work.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from nymeria.core import thread_requests as tr
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import SEED_TOOLS
from nymeria.tools.spawn_thread import (
    DEFAULT_IDLE_TIMEOUT_HOURS,
    PLATFORM_META_IDLE_TIMEOUT,
    PLATFORM_META_LAST_ACTIVE,
    PLATFORM_META_LIFETIME,
    _build_spawn_preamble,
    _invoke_spawned,
    _reconcile_lifetime,
    _resolve_semantic_tools,
    _resolve_spawn_tools,
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
# kit= (phase 5: activate a skill or Skill Kit on the new thread)
# ---------------------------------------------------------------------------


def _kit_skill(*, is_kit: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        name="research-kit",
        is_skill_kit=is_kit,
        required_tools=["web_search"],
        tool_ttl="2h",
    )


class TestKitBinding:
    def test_missing_skill_manager_errors(self, stub_agent):
        # The stub agent has no skill_manager attribute at all.
        result = spawn_thread.invoke(
            {"title": "child", "kit": "research-kit"},
            config=_runnable_config(),
        )
        assert result.startswith("[Error]")
        assert "Skill manager unavailable" in result

    def test_unknown_kit_fails_fast_without_creating_thread(self, stub_agent):
        stub_agent.skill_manager = SimpleNamespace(
            get=lambda name, user_id=None: None
        )
        result = spawn_thread.invoke(
            {"title": "child", "kit": "no-such-kit"},
            config=_runnable_config(),
        )
        assert result.startswith("[Error]")
        assert "not found" in result
        assert stub_agent.thread_config_manager.list_configured_threads() == []

    def test_kit_activation_success_adds_preamble_line(self, stub_agent):
        stub_agent.skill_manager = SimpleNamespace(
            get=lambda name, user_id=None: _kit_skill()
        )
        calls: dict[str, Any] = {}

        def fake_activate(*, agent, thread_id, user_id, skill_name, reason):
            calls.update(thread_id=thread_id, user_id=user_id, skill_name=skill_name)
            return True, "[Success]: kit activated"

        with patch(
            "nymeria.core.command_service.activate_skill_kit",
            side_effect=fake_activate,
        ):
            result = spawn_thread.invoke(
                {"title": "researcher", "kit": "research-kit"},
                config=_runnable_config(),
            )
        assert not result.startswith("[Error]"), result
        new_id = _extract_thread_id(result)
        assert calls == {
            "thread_id": new_id,
            "user_id": "u1",
            "skill_name": "research-kit",
        }
        assert "Kit: research-kit (tools: web_search, TTL 2h)" in result

    def test_plain_skill_gets_skill_line(self, stub_agent):
        stub_agent.skill_manager = SimpleNamespace(
            get=lambda name, user_id=None: _kit_skill(is_kit=False)
        )
        with patch(
            "nymeria.core.command_service.activate_skill_kit",
            return_value=(True, "[Success]: Skill activated."),
        ):
            result = spawn_thread.invoke(
                {"title": "guided", "kit": "research-kit"},
                config=_runnable_config(),
            )
        assert not result.startswith("[Error]"), result
        assert "Skill: research-kit enabled" in result

    def test_bind_failure_rolls_back_config(self, stub_agent):
        stub_agent.skill_manager = SimpleNamespace(
            get=lambda name, user_id=None: _kit_skill()
        )
        seen: dict[str, Any] = {}

        def fake_activate(*, agent, thread_id, user_id, skill_name, reason):
            seen["thread_id"] = thread_id
            return False, "[Error]: admin-blocked required tool"

        with patch(
            "nymeria.core.command_service.activate_skill_kit",
            side_effect=fake_activate,
        ):
            result = spawn_thread.invoke(
                {"title": "child", "kit": "research-kit"},
                config=_runnable_config(),
            )
        assert result.startswith("[Error]")
        assert "admin-blocked required tool" in result
        # The half-created thread was rolled back: no config, no metadata.
        tid = seen["thread_id"]
        assert stub_agent.thread_config_manager.get_config(tid) is None
        assert stub_agent.thread_metadata_manager.get_thread("u1", tid) is None


# ---------------------------------------------------------------------------
# _invoke_spawned internals (the autonomous-turn SSE scaffold, slice 18 F3)
# ---------------------------------------------------------------------------


class TestInvokeSpawnedInternals:
    """_invoke_spawned publishes the child's turn over the shared
    AutonomousTurnEmitter (slice 18 F3) and, under a parent, dispatches a
    REQUEST (backlog #357): the child's prompt carries the [Request Metadata]
    block, the parent gets a [Requested] receipt at once (or the reply inline
    after ``wait_seconds``), the child's plain text is never the reply, and
    the abort-cascade edge exists only while the parent waits."""

    @pytest.fixture(autouse=True)
    def _fresh_ledger(self):
        tr.reset_for_tests()
        yield
        tr.reset_for_tests()

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
            _thread_locks=ThreadLockManager(),
            accounts_repo=SimpleNamespace(get_thread_owner=lambda tid: "u1"),
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

    @staticmethod
    def _wait_until(predicate, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        assert predicate(), "condition not met in time"

    def test_under_a_parent_the_child_gets_a_request_and_the_parent_a_receipt(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        captured_kwargs: dict[str, Any] = {}
        registered: list[tuple[str, str]] = []
        unregistered: list[tuple[str, str]] = []
        finished = threading.Event()
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
            finished.set()
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

        # The parent gets the receipt at once; the child's turn runs on a worker.
        assert result.startswith("[Requested]: request_id=req-")
        assert "target=ResponderBot" in result and "target_thread_id=child-1" in result
        req = tr.requests_awaited_by("parent-1")[0]
        assert req.id in result and req.target_thread_id == "child-1"
        assert req.task == "do the thing" and req.caller_name == "Parent Title"

        assert finished.wait(5)
        message = captured_kwargs["message"]
        assert message.startswith("[Request Metadata]\n")
        assert f"request_id: {req.id}" in message
        assert "source_thread_id: parent-1" in message
        assert "source_thread_name: Parent Title" in message
        assert "reply_to_thread" in message
        assert message.endswith("\n\ndo the thing")
        assert captured_kwargs["_trigger_override"] == 'SpawnedBy("parent-1", "Parent Title")'
        assert captured_kwargs["_is_self_invoke"] is True
        assert captured_kwargs["source"] == "callable"
        assert captured_kwargs["source_label"] == "ResponderBot"
        assert captured_kwargs["source_id"] == req.task_id

        self._wait_until(lambda: len(events) == 2)
        assert [e[0] for e in events] == ["task_started", "task_completed"]
        assert events[0][1] == "child-1" and events[0][2] == "u1"
        # both bookend events carry the same spawned-* task id, the request's task id
        assert events[0][3] == events[1][3] == req.task_id
        assert events[0][3].startswith("spawned-")
        assert events[0][4] == {
            "prompt": "do the thing",
            "callable_name": "ResponderBot",
            "trigger": "spawn_thread",
        }
        assert events[1][4] == {"content": "child reply", "callable_name": "ResponderBot"}
        assert chunks == [{"type": "response", "content": "child reply"}]
        # Nobody waited, so no abort-cascade edge; and the child's plain text
        # is NOT its reply: the request stays open until reply_to_thread.
        assert registered == [] and unregistered == []
        assert req.state == tr.STATE_OPEN

    def test_wait_seconds_returns_the_reply_inline_and_holds_the_edge_only_while_waiting(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        registered: list[tuple[str, str]] = []
        unregistered: list[tuple[str, str]] = []
        self._patch_publishers(monkeypatch, events, chunks)

        class FakeStreamResult:
            iteration_limit_hit = False

            def response_text(self, *, fallback_to_thinking: bool = True) -> str:
                return "ok, sent"

        def fake_stream_and_collect(
            agent_arg, *, astream_kwargs, on_chunk, error_message_factory
        ):
            # The child's reply_to_thread call, as its turn would make it.
            req = tr.requests_owed_by("child-1")[0]
            tr.reply(
                request_id=req.id, content="child reply", final=True,
                replier_thread_id="child-1", agent=agent_arg,
            )
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
            wait_seconds=5,
        )

        assert result.startswith("[Requested]: request_id=req-")
        assert "[Reply from ResponderBot]" in result
        assert result.rstrip().endswith("child reply")
        req = tr.get_request(result.split("request_id=")[1].split()[0])
        assert req is not None and req.state == tr.STATE_REPLIED
        assert req.delivered_via == "inline"
        assert registered == [("parent-1", "child-1")]
        assert unregistered == [("parent-1", "child-1")]

    def test_a_workflow_spawn_under_a_parent_stays_synchronous(self, monkeypatch):
        """``nym.thread`` spawns fresh threads with the workflow's thread as the
        parent but reads the child's text itself (``schema=`` extracts from
        it): inside ``synchronous_spawn()`` the child runs as a plain turn and
        no request is opened, while lineage and the trigger label are kept."""
        from nymeria.tools.spawn_thread import synchronous_spawn

        events: list[tuple] = []
        chunks: list[dict] = []
        captured_kwargs: dict[str, Any] = {}
        self._patch_publishers(monkeypatch, events, chunks)

        class FakeStreamResult:
            iteration_limit_hit = False

            def response_text(self, *, fallback_to_thinking: bool = True) -> str:
                return '{"answer": 42}'

        def fake_stream_and_collect(
            agent_arg, *, astream_kwargs, on_chunk, error_message_factory
        ):
            captured_kwargs.update(astream_kwargs)
            return FakeStreamResult()

        monkeypatch.setattr(
            "nymeria.core.stream_bridge.stream_and_collect",
            fake_stream_and_collect,
        )
        agent = self._spawn_agent(register=lambda p, c: None, unregister=lambda p, c: None)

        with synchronous_spawn():
            result = _invoke_spawned(
                agent,
                child_thread_id="child-1",
                parent_thread_id="parent-1",
                title="Worker",
                task="compute",
                user_id="u1",
            )

        assert result == '{"answer": 42}'
        assert captured_kwargs["message"] == "compute"
        assert captured_kwargs["_trigger_override"] == 'SpawnedBy("parent-1", "Parent Title")'
        assert tr.open_requests() == []
        assert events[-1][4] == {"content": '{"answer": 42}', "callable_name": "Worker"}

    def test_a_child_whose_first_turn_dies_fails_the_request(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        self._patch_publishers(monkeypatch, events, chunks)
        delivered: list = []
        monkeypatch.setattr(
            "nymeria.core.completion_delivery.fire_autonomous_turn",
            lambda agent, d: delivered.append(d),
        )

        def boom(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("nymeria.core.stream_bridge.stream_and_collect", boom)
        agent = self._spawn_agent(register=lambda p, c: None, unregister=lambda p, c: None)

        result = _invoke_spawned(
            agent,
            child_thread_id="child-1",
            parent_thread_id="parent-1",
            title="Bot",
            task="t",
            user_id="u1",
        )

        assert result.startswith("[Requested]")
        self._wait_until(lambda: len(delivered) == 1)
        assert delivered[0].thread_id == "parent-1"
        assert delivered[0].prompt_text.startswith("[NoReply]") and "kaboom" in delivered[0].prompt_text
        req = tr.get_request(result.split("request_id=")[1].split()[0])
        assert req is not None and req.state == tr.STATE_FAILED

    def test_parentless_error_path_returns_error_and_skips_task_started(self, monkeypatch):
        events: list[tuple] = []
        chunks: list[dict] = []
        registered: list[tuple[str, str]] = []
        unregistered: list[tuple[str, str]] = []
        self._patch_publishers(monkeypatch, events, chunks)

        def boom(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("nymeria.core.stream_bridge.stream_and_collect", boom)
        agent = self._spawn_agent(
            register=lambda p, c: registered.append((p, c)),
            unregister=lambda p, c: unregistered.append((p, c)),
        )

        result = _invoke_spawned(
            agent,
            child_thread_id="child-1",
            parent_thread_id=None,
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
        # No parent, no request, no wait: no cascade edge either way.
        assert registered == [] and unregistered == []
        assert tr.open_requests() == []

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


# ---------------------------------------------------------------------------
# F6 decomposition: direct unit tests for the extracted helpers
# ---------------------------------------------------------------------------


class TestReconcileLifetime:
    """_reconcile_lifetime(ttl_hours, lifetime, idle_timeout_hours)."""

    def test_permanent_when_all_none(self):
        assert _reconcile_lifetime(None, None, None) == (None, [], None)

    def test_positive_int_passes_through(self):
        assert _reconcile_lifetime(6, None, None) == (6, [], None)

    def test_string_int_is_coerced(self):
        # The @tool coerces args to int, but the helper's int() is defensive.
        weird: Any = "8"
        assert _reconcile_lifetime(weird, None, None) == (8, [], None)

    def test_zero_rejected(self):
        ttl, warnings, error = _reconcile_lifetime(0, None, None)
        assert ttl is None
        assert error == "[Error]: ttl_hours must be >= 1 (or None for a permanent thread)."

    def test_negative_rejected(self):
        ttl, _w, error = _reconcile_lifetime(-1, None, None)
        assert ttl is None
        assert error is not None and error.startswith("[Error]: ttl_hours must be >= 1")

    def test_non_integer_rejected(self):
        weird: Any = "abc"
        ttl, _w, error = _reconcile_lifetime(weird, None, None)
        assert ttl is None
        assert error == "[Error]: ttl_hours must be an integer; got 'abc'."

    def test_unknown_lifetime_rejected(self):
        ttl, _w, error = _reconcile_lifetime(None, "ephemeral", None)
        assert ttl is None
        assert error is not None and error.startswith("[Error]: Unknown lifetime 'ephemeral'")

    def test_legacy_temporary_without_idle_uses_default(self):
        assert _reconcile_lifetime(None, "temporary", None) == (
            DEFAULT_IDLE_TIMEOUT_HOURS,
            [],
            None,
        )

    def test_legacy_temporary_with_idle_uses_idle(self):
        assert _reconcile_lifetime(None, "temporary", 3) == (3, [], None)

    def test_ttl_wins_over_legacy_lifetime_with_warning(self):
        ttl, warnings, error = _reconcile_lifetime(5, "temporary", 9)
        assert (ttl, error) == (5, None)
        assert warnings == [
            "legacy lifetime/idle_timeout_hours ignored because ttl_hours is set"
        ]

    def test_permanent_lifetime_with_idle_warns(self):
        ttl, warnings, error = _reconcile_lifetime(None, "permanent", 4)
        assert (ttl, error) == (None, None)
        assert warnings == [
            "idle_timeout_hours ignored when lifetime='permanent'; use ttl_hours for temporary cleanup"
        ]

    def test_idle_without_lifetime_warns(self):
        ttl, warnings, error = _reconcile_lifetime(None, None, 4)
        assert (ttl, error) == (None, None)
        assert warnings == [
            "idle_timeout_hours ignored without lifetime='temporary'; use ttl_hours for temporary cleanup"
        ]


class TestResolveSpawnToolsUnit:
    """_resolve_spawn_tools(agent, ...) -> (enabled, disabled, records, warnings, error)."""

    def _call(self, agent, **overrides):
        kwargs = dict(
            user_id="u1",
            parent_thread_id=None,
            tool_queries=None,
            tool_query_top_k=8,
            tool_categories=None,
            optional_tools=None,
            disabled_tools=None,
            include_core_tools=True,
            pre_warnings=[],
        )
        kwargs.update(overrides)
        return _resolve_spawn_tools(agent, **kwargs)

    def test_admin_only_tool_blocked_for_user(self, stub_agent):
        enabled, disabled, _records, _warnings, error = self._call(
            stub_agent, optional_tools=["claude_code"]
        )
        assert error is not None
        assert error.startswith(
            "[Error]: Admin-only tools cannot be enabled on a spawned thread"
        )
        assert "claude_code" in error

    def test_developer_only_tool_blocked_for_user(self, stub_agent):
        _enabled, _disabled, _records, _warnings, error = self._call(
            stub_agent, optional_tools=["hello_test"]
        )
        assert error is not None
        assert error.startswith(
            "[Error]: Developer-only diagnostic tools cannot be enabled on a "
            "spawned thread"
        )

    def test_unknown_optional_tool_warns(self, stub_agent):
        enabled, _disabled, _records, warnings, error = self._call(
            stub_agent, optional_tools=["zzz_not_a_tool"]
        )
        assert error is None
        assert "zzz_not_a_tool" not in enabled
        assert any("unknown tool(s): zzz_not_a_tool" in w for w in warnings)

    def test_unknown_disabled_tool_warns(self, stub_agent):
        _enabled, disabled, _records, warnings, error = self._call(
            stub_agent, disabled_tools=["zzz_not_a_tool"]
        )
        assert error is None
        assert "zzz_not_a_tool" not in disabled
        assert any("unknown tool(s) to disable: zzz_not_a_tool" in w for w in warnings)

    def test_include_core_tools_false_funnels_seed_into_disabled(self, stub_agent):
        _enabled, disabled, _records, _warnings, error = self._call(
            stub_agent, include_core_tools=False
        )
        assert error is None
        seed_names = {t.name for t in SEED_TOOLS}
        assert seed_names.issubset(set(disabled))

    def test_unknown_category_warns(self, stub_agent):
        _enabled, _disabled, _records, warnings, error = self._call(
            stub_agent, tool_categories=["definitely-not-a-category"]
        )
        assert error is None
        assert any(
            "unknown categor(ies): definitely-not-a-category" in w for w in warnings
        )

    def test_valid_category_expands_into_enabled(self, stub_agent):
        from nymeria.tools import (
            ADMIN_ONLY_TOOL_NAMES,
            CATALOG_TOOLS,
            DEVELOPER_ONLY_TOOL_NAMES,
        )
        from nymeria.tools.metadata import (
            get_all_categories,
            get_category_tools_summary,
        )

        summary = get_category_tools_summary()
        restricted = set(ADMIN_ONLY_TOOL_NAMES) | set(DEVELOPER_ONLY_TOOL_NAMES)
        # Pick a category whose CATALOG (bindable) tools are all enable-able by
        # a normal user, so the admin/developer gate does not fire.
        target = None
        expected: set[str] = set()
        for cat in get_all_categories():
            cands = {n for n in summary.get(cat, []) if n in CATALOG_TOOLS}
            if cands and not (cands & restricted):
                target = cat
                expected = cands
                break
        assert target is not None, "no unrestricted category with catalog tools"
        enabled, _disabled, _records, _warnings, error = self._call(
            stub_agent, tool_categories=[target]
        )
        assert error is None
        assert expected.issubset(enabled)

    def test_pre_warnings_are_threaded_through(self, stub_agent):
        _enabled, _disabled, _records, warnings, error = self._call(
            stub_agent, pre_warnings=["carried-over"]
        )
        assert error is None
        assert warnings[0] == "carried-over"


class TestBuildSpawnPreamble:
    """_build_spawn_preamble(...) line order and content."""

    def test_minimal_preamble(self):
        out = _build_spawn_preamble(
            new_thread_id="spawned-x",
            mode_norm="fresh",
            parent_thread_id=None,
            ttl_hours_resolved=None,
            make_callable=False,
            callable_name=None,
            tool_resolution_records=[],
            include_core_tools=True,
            enabled_tools=[],
            disabled_tools=[],
            warnings=[],
        )
        assert out == (
            "[Spawned]: thread_id=spawned-x\n"
            'To delete later: spawn_thread(action="delete", '
            'delete_thread_id="spawned-x")'
        )

    def test_full_preamble_line_order(self):
        records = [{"name": "browser_navigate", "score": 0.88, "query": "research"}]
        out = _build_spawn_preamble(
            new_thread_id="spawned-y",
            mode_norm="branched",
            parent_thread_id="parent-1",
            ttl_hours_resolved=6,
            make_callable=True,
            callable_name="spawned_y",
            tool_resolution_records=records,
            include_core_tools=True,
            enabled_tools=["browser_navigate"],
            disabled_tools=["bash_execute"],
            warnings=["heads up"],
        )
        lines = out.split("\n")
        assert lines[0] == "[Spawned]: thread_id=spawned-y"
        assert lines[1] == "Mode: branched from parent-1 (inherits checkpoint history)."
        assert lines[2] == "Lifetime: temporary (auto-deletes after 6h of inactivity)."
        assert lines[3] == 'Callable as: spawned_y(task="..."). Any unteamed thread can invoke this.'
        assert lines[4] == '[Resolved tools]: browser_navigate (0.88 <- "research")'
        assert lines[5] == "Enabled optional tools: browser_navigate"
        assert lines[6] == "Disabled core tools: bash_execute"
        assert lines[7] == "[Warning]: heads up"
        assert lines[8] == (
            'To delete later: spawn_thread(action="delete", '
            'delete_thread_id="spawned-y")'
        )
        # The "Core tools: disabled" line is mutually exclusive with the
        # "Disabled core tools" line (it only renders when include_core_tools
        # is False), so it must NOT appear here.
        assert "Core tools: disabled" not in out

    def test_core_tools_disabled_line_when_opted_out(self):
        out = _build_spawn_preamble(
            new_thread_id="spawned-z",
            mode_norm="fresh",
            parent_thread_id=None,
            ttl_hours_resolved=None,
            make_callable=False,
            callable_name=None,
            tool_resolution_records=[],
            include_core_tools=False,
            enabled_tools=["browser_navigate"],
            disabled_tools=["bash_execute"],
            warnings=[],
        )
        assert "Core tools: disabled (include_core_tools=False" in out
        # When include_core_tools is False the "Disabled core tools" line is
        # suppressed (the funnel already lists every core tool as disabled).
        assert "Disabled core tools:" not in out

    def test_resolved_tools_truncates_after_six(self):
        records = [
            {"name": f"tool_{i}", "score": 0.5, "query": "q"} for i in range(8)
        ]
        out = _build_spawn_preamble(
            new_thread_id="spawned-w",
            mode_norm="fresh",
            parent_thread_id=None,
            ttl_hours_resolved=None,
            make_callable=False,
            callable_name=None,
            tool_resolution_records=records,
            include_core_tools=True,
            enabled_tools=[],
            disabled_tools=[],
            warnings=[],
        )
        resolved_line = next(
            line for line in out.split("\n") if line.startswith("[Resolved tools]:")
        )
        assert "tool_0" in resolved_line
        assert "tool_5" in resolved_line
        assert "tool_6" not in resolved_line
        assert "+2 more" in resolved_line

    def test_team_line_renders_and_scopes_callable_wording(self):
        out = _build_spawn_preamble(
            new_thread_id="spawned-t",
            mode_norm="fresh",
            parent_thread_id=None,
            ttl_hours_resolved=None,
            make_callable=True,
            callable_name="spawned_t",
            tool_resolution_records=[],
            include_core_tools=True,
            enabled_tools=[],
            disabled_tools=[],
            warnings=[],
            team_line="Team: Ops (inherited from the spawning thread).",
            child_teamed=True,
        )
        lines = out.split("\n")
        assert lines[1] == "Team: Ops (inherited from the spawning thread)."
        assert lines[2] == (
            'Callable as: spawned_t(task="..."). Same-team threads can invoke this.'
        )

    def test_opted_out_team_line_keeps_unteamed_callable_wording(self):
        # team="none" from a teamed parent renders a team line but the child
        # is unteamed: the invoke-scope hint must key on membership, not on
        # the line's presence.
        out = _build_spawn_preamble(
            new_thread_id="spawned-t",
            mode_norm="fresh",
            parent_thread_id=None,
            ttl_hours_resolved=None,
            make_callable=True,
            callable_name="spawned_t",
            tool_resolution_records=[],
            include_core_tools=True,
            enabled_tools=[],
            disabled_tools=[],
            warnings=[],
            team_line="Team: none (opted out of the spawning thread's team 'Ops').",
            child_teamed=False,
        )
        lines = out.split("\n")
        assert lines[1] == (
            "Team: none (opted out of the spawning thread's team 'Ops')."
        )
        assert lines[2] == (
            'Callable as: spawned_t(task="..."). Any unteamed thread can invoke this.'
        )


# ---------------------------------------------------------------------------
# callable-team inheritance (backlog #97)
# ---------------------------------------------------------------------------


class TestTeamInheritance:
    """Fresh spawns join the spawning thread's callable-team bubble."""

    def test_fresh_spawn_inherits_parent_team(self, stub_agent):
        from nymeria.core.thread_config import ThreadConfig

        stub_agent.thread_config_manager.save_config(
            ThreadConfig(
                thread_id="parent-1",
                callable_team_id="team-a",
                callable_team_name="Ops",
            )
        )
        result = spawn_thread.invoke(
            {"title": "teamed child"}, config=_runnable_config()
        )
        child_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(child_id)
        assert tc is not None
        assert tc.callable_team_id == "team-a"
        # Deprecated (backlog #100): only the membership id is written; the
        # receipt's display name resolved from the surviving legacy config
        # name (the stub agent has no team store).
        assert tc.callable_team_name is None
        assert "Team: Ops (inherited from the spawning thread)." in result
        assert "Same-team threads can invoke this." in result

    def test_fresh_spawn_receipt_prefers_team_store_name(self, stub_agent):
        from types import SimpleNamespace

        from nymeria.core.thread_config import ThreadConfig

        stub_agent.thread_config_manager.save_config(
            ThreadConfig(
                thread_id="parent-1",
                callable_team_id="team-a",
                callable_team_name="Stale Legacy",
            )
        )
        stub_agent.team_manager = SimpleNamespace(
            resolve_team_name=lambda user_id, team_id: "Ops (store)"
        )
        result = spawn_thread.invoke(
            {"title": "teamed child"}, config=_runnable_config()
        )
        assert "Team: Ops (store) (inherited from the spawning thread)." in result

    def test_fresh_spawn_from_unteamed_parent_stays_unteamed(self, stub_agent):
        result = spawn_thread.invoke(
            {"title": "plain child"}, config=_runnable_config()
        )
        child_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(child_id)
        assert tc is not None
        assert tc.callable_team_id is None
        assert tc.callable_team_name is None
        assert "Team:" not in result
        assert "Any unteamed thread can invoke this." in result


class TestTeamOverride:
    """The team= parameter (backlog #100 phase 2): inherit / none / explicit."""

    def _attach_team_store(self, stub_agent, tmp_path):
        from nymeria.core.team_manager import TeamManager

        stub_agent.team_manager = TeamManager(tmp_path)
        return stub_agent.team_manager.create_team("u1", name="Ops")

    def test_explicit_team_ref_by_name(self, stub_agent, tmp_path):
        team = self._attach_team_store(stub_agent, tmp_path)
        result = spawn_thread.invoke(
            {"title": "ops child", "team": "ops"}, config=_runnable_config()
        )
        child_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(child_id)
        assert tc.callable_team_id == team.id
        assert tc.callable_team_name is None
        assert "Team: Ops (set via team=)." in result
        assert "Same-team threads can invoke this." in result

    def test_team_none_opts_out_of_parent_team(self, stub_agent):
        from nymeria.core.thread_config import ThreadConfig

        stub_agent.thread_config_manager.save_config(
            ThreadConfig(
                thread_id="parent-1",
                callable_team_id="team-a",
                callable_team_name="Ops",
            )
        )
        result = spawn_thread.invoke(
            {"title": "solo child", "team": "none"}, config=_runnable_config()
        )
        child_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(child_id)
        assert tc.callable_team_id is None
        assert "Team: none (opted out of the spawning thread's team 'Ops')." in result
        assert "Any unteamed thread can invoke this." in result

    def test_unknown_team_ref_creates_nothing(self, stub_agent, tmp_path):
        self._attach_team_store(stub_agent, tmp_path)
        result = spawn_thread.invoke(
            {"title": "orphan", "team": "Nope"}, config=_runnable_config()
        )
        assert result.startswith("[Error]: Unknown team 'Nope'")
        assert stub_agent.accounts_repo.claimed == []

    def test_branched_spawn_team_override(self, stub_agent, tmp_path, monkeypatch):
        from nymeria.core.thread_config import ThreadConfig

        team = self._attach_team_store(stub_agent, tmp_path)
        stub_agent.thread_config_manager.save_config(
            ThreadConfig(thread_id="parent-1", callable_team_id="team-old")
        )

        def fake_branch(
            *,
            agent,
            settings,
            user_id,
            source_thread_id,
            title=None,
            from_message_index=None,
            new_thread_id=None,
        ):
            # Keyword-only, mirroring the real branch_thread signature so a
            # call-shape drift fails loudly. A real branch clones the
            # parent's config (team id included, deprecated name already
            # None per backlog #100 phase 1).
            agent.thread_config_manager.save_config(
                ThreadConfig(thread_id=new_thread_id, callable_team_id="team-old")
            )

        monkeypatch.setattr("nymeria.core.thread_branch.branch_thread", fake_branch)
        result = spawn_thread.invoke(
            {"title": "branch child", "mode": "branched", "team": "Ops"},
            config=_runnable_config(),
        )
        child_id = _extract_thread_id(result)
        tc = stub_agent.thread_config_manager.get_config(child_id)
        assert tc.callable_team_id == team.id
        assert tc.callable_team_name is None
        assert "Team: Ops (set via team=)." in result
