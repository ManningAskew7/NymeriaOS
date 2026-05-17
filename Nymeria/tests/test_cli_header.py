from __future__ import annotations

import asyncio
import copy
import io
from types import SimpleNamespace
from typing import Any

from cli_fixtures import FakeTerminalCapabilities
from rich.cells import cell_len
from rich.console import Console

from nymeria.triggers.cli.app import CLIApp
from nymeria.triggers.cli.commands import CommandResult
from nymeria.triggers.cli.header import (
    CLIHeaderSnapshot,
    HeaderHealthSnapshot,
    build_header_snapshot,
    concise_connection_label,
    thinking_mode,
)
from nymeria.triggers.cli.rendering.welcome import render_welcome
from nymeria.triggers.cli.transport.disconnected import DisconnectedAgentClient


def run(coro):
    return asyncio.run(coro)


class HeaderFakeClient:
    connection_label = "api http://api.test"
    base_url = "http://api.test"

    def __init__(self) -> None:
        self.settings = {
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "llm_base_url": None,
            "llm_extended_thinking": True,
            "openai_api_mode": "responses",
            "llm_reasoning_effort": "high",
        }
        self.thread_config = {
            "thread_id": "thread-123456",
            "instructions": "Use the project tone.",
            "system_prompt": "Custom system prompt.",
            "enabled_tools": ["web_search"],
            "disabled_tools": ["filesystem"],
            "enabled_skills": ["skill-one"],
            "disabled_skills": [],
            "llm_config": {},
            "callable": True,
            "callable_name": "Analyst",
            "callable_team_name": "Ops",
            "inject_todos_in_prompt": True,
            "inject_profile_in_prompt": True,
        }
        self.context_stats = {
            "model": "claude-sonnet-4-6",
            "total_tokens": 41000,
            "context_limit": 200000,
            "usage_percentage": 20.5,
            "compaction_count": 2,
        }

    async def health(self) -> bool:
        return True

    async def get_settings(self, user_id: str = "default") -> dict[str, Any]:
        return copy.deepcopy(self.settings)

    async def list_threads(self, user_id: str = "default") -> list[dict[str, Any]]:
        return [
            {
                "thread_id": "thread-123456",
                "title": "Procurement Review",
                "platform": "desktop",
                "pinned": True,
            }
        ]

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return copy.deepcopy({**self.thread_config, "thread_id": thread_id})

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return copy.deepcopy(self.context_stats)

    async def get_default_tools(self, user_id: str = "default") -> dict[str, Any]:
        return {
            "default_tools": ["filesystem", "mcp__fetch__fetch"],
            "available_tools": [
                {"name": "filesystem", "category": "filesystem", "is_default": True},
                {"name": "web_search", "category": "web", "is_default": False},
                {
                    "name": "mcp__fetch__fetch",
                    "category": "mcp_server",
                    "is_default": True,
                },
            ],
        }

    async def get_thread_active_skills(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> dict[str, Any]:
        return {
            "skills": [
                {"name": "skill-one", "is_skill_kit": False},
                {"name": "kit-one", "is_skill_kit": True},
            ]
        }

    async def get_thread_callable_tools(
        self,
        thread_id: str,
        user_id: str = "default",
    ) -> dict[str, Any]:
        return {
            "callable_thread_count": 2,
            "callable_threads": [{"name": "Helper"}, {"name": "Researcher"}],
        }

    async def list_triggers(
        self,
        user_id: str = "default",
        *,
        enabled_only: bool = False,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "one",
                "name": "Morning brief",
                "enabled": True,
                "thread_id": "thread-123456",
            },
            {
                "id": "two",
                "name": "Disabled",
                "enabled": False,
                "thread_id": "thread-123456",
            },
            {
                "id": "three",
                "name": "Other thread",
                "enabled": True,
                "thread_id": "other",
            },
        ]

    async def list_todos(
        self,
        user_id: str = "default",
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "todo-1",
                "task": "Review purchase order",
                "status": "pending",
                "thread_id": "thread-123456",
            },
            {
                "id": "todo-2",
                "task": "Email supplier",
                "status": "in_progress",
                "thread_id": "thread-123456",
            },
            {
                "id": "todo-3",
                "task": "File notes",
                "status": "pending",
                "thread_id": "thread-123456",
            },
            {
                "id": "todo-4",
                "task": "Done item",
                "status": "done",
                "thread_id": "thread-123456",
            },
            {
                "id": "todo-5",
                "task": "Other item",
                "status": "pending",
                "thread_id": "other",
            },
        ]


def make_state() -> SimpleNamespace:
    return SimpleNamespace(
        thread_id="thread-123456",
        user_id="alice",
        agent=None,
        settings=SimpleNamespace(
            llm_provider="openai",
            llm_model="gpt-5.5",
            llm_base_url=None,
            llm_extended_thinking=False,
            openai_api_mode="responses",
            llm_reasoning_effort=None,
        ),
    )


def test_header_snapshot_builds_full_dashboard_data() -> None:
    snapshot = run(build_header_snapshot(make_state(), HeaderFakeClient()))

    assert snapshot.thread_title == "Analyst"
    assert snapshot.short_thread_id == "thread-1"
    assert snapshot.platform == "desktop"
    assert snapshot.pinned is True
    assert snapshot.callable is True
    assert snapshot.callable_team == "Ops"
    assert snapshot.provider == "Claude API"
    assert snapshot.api_type == "messages/v1"
    assert snapshot.model == "claude-sonnet-4-6"
    assert snapshot.thinking_mode == "adaptive (high)"
    assert snapshot.context_tokens == 41000
    assert snapshot.context_limit == 200000
    assert snapshot.context_percent == 20.5
    assert snapshot.compaction_count == 2
    assert snapshot.tool_count == 1
    assert snapshot.mcp_tool_count == 1
    assert snapshot.callable_tool_count == 2
    assert snapshot.skill_count == 1
    assert snapshot.skill_kit_count == 1
    assert snapshot.todo_count == 3
    assert snapshot.todo_labels == ("Review purchase order", "Email supplier")
    assert snapshot.trigger_count == 1
    assert snapshot.trigger_labels == ("Morning brief",)
    assert "instructions" in snapshot.flags
    assert "system prompt" in snapshot.flags
    assert "TODOs" in snapshot.flags
    assert "profile" in snapshot.flags
    assert "callable Analyst" in snapshot.flags
    assert "team Ops" in snapshot.flags
    assert snapshot.backend_url == "http://api.test"
    assert snapshot.connection_label.startswith("api ok")


class OverviewHeaderClient:
    connection_label = "api http://api.test"
    base_url = "http://api.test"

    def __init__(self) -> None:
        self.legacy_called = False

    async def health(self) -> bool:
        return True

    async def get_thread_overview(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        assert thread_id == "thread-123456"
        assert user_id == "alice"
        return {
            "thread": {
                "thread_id": thread_id,
                "title": "Overview Header",
                "platform": "desktop",
                "pinned": True,
            },
            "context": {
                "model": "gpt-5.5",
                "total_tokens": 900,
                "context_limit": 1000,
                "usage_percentage": 90,
                "compaction_count": 3,
            },
            "config_summary": {
                "instructions_present": True,
                "system_prompt_override_present": True,
                "inject_todos_in_prompt": True,
                "inject_profile_in_prompt": True,
                "show_autonomous_prompts": True,
                "show_prompt_metadata": True,
                "telegram_autonomous_delivery": "notify_only",
                "in_app_notification_level": "all_autonomous",
            },
            "llm": {
                "provider": "openai",
                "provider_label": "OpenAI API",
                "model": "gpt-5.5",
                "api_mode": "responses",
                "api_mode_label": "responses/v1",
                "thinking_label": "high",
            },
            "callable": {
                "enabled": True,
                "name": "Analyst",
                "team_name": "Ops",
                "visible_thread_count": 4,
            },
            "tools": {
                "effective_builtin_count": 10,
                "effective_mcp_count": 2,
                "effective_callable_count": 4,
            },
            "skills": {
                "active_skill_count": 5,
                "active_skill_kit_count": 2,
            },
            "todos": {
                "active_count": 2,
                "first_labels": ["Review PO", "Email supplier"],
            },
            "triggers": {
                "enabled_count": 1,
                "first_labels": ["Morning brief"],
            },
            "user": {
                "id": "alice",
                "display_name": "Alice",
                "role": "admin",
            },
            "section_errors": {"mcp": "offline"},
        }

    async def get_settings(self, *args, **kwargs):
        self.legacy_called = True
        raise AssertionError("legacy path should not run")


def test_header_snapshot_prefers_thread_overview_payload() -> None:
    client = OverviewHeaderClient()
    snapshot = run(build_header_snapshot(make_state(), client))

    assert client.legacy_called is False
    assert snapshot.thread_title == "Overview Header"
    assert snapshot.provider == "OpenAI API"
    assert snapshot.api_type == "responses/v1"
    assert snapshot.model == "gpt-5.5"
    assert snapshot.thinking_mode == "high"
    assert snapshot.context_percent == 90
    assert snapshot.compaction_count == 3
    assert snapshot.tool_count == 10
    assert snapshot.mcp_tool_count == 2
    assert snapshot.callable_tool_count == 4
    assert snapshot.skill_count == 5
    assert snapshot.skill_kit_count == 2
    assert snapshot.todo_count == 2
    assert snapshot.trigger_count == 1
    assert "autonomous prompts" in snapshot.flags
    assert "prompt metadata" in snapshot.flags
    assert "telegram notify_only" in snapshot.flags
    assert "notifications all_autonomous" in snapshot.flags
    assert snapshot.failures == ("mcp",)


class OverviewFailingClient(HeaderFakeClient):
    async def get_thread_overview(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("overview unavailable")


def test_header_snapshot_falls_back_when_overview_fails() -> None:
    snapshot = run(build_header_snapshot(make_state(), OverviewFailingClient()))

    assert snapshot.thread_title == "Analyst"
    assert "overview" in snapshot.failures


class OverviewMissingPartialClient(HeaderFakeClient):
    async def get_thread_overview(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        return None

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("config unavailable")


def test_header_snapshot_labels_missing_overview_when_legacy_fallback_fails() -> None:
    snapshot = run(build_header_snapshot(make_state(), OverviewMissingPartialClient()))

    assert "overview unavailable" in snapshot.failures
    assert "thread_config" in snapshot.failures


class CLIProxyHeaderClient(HeaderFakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.settings["llm_base_url"] = "http://localhost:8317"


def test_header_snapshot_labels_anthropic_cliproxy_oauth_provider() -> None:
    snapshot = run(build_header_snapshot(make_state(), CLIProxyHeaderClient()))

    assert snapshot.provider == "cliproxy OAuth"
    assert snapshot.api_type == "messages/v1"


def test_header_snapshot_labels_thread_direct_anthropic_override() -> None:
    client = CLIProxyHeaderClient()
    client.thread_config["llm_config"] = {
        "provider": "anthropic",
        "base_url": "",
    }

    snapshot = run(build_header_snapshot(make_state(), client))

    assert snapshot.provider == "Claude API"
    assert snapshot.api_type == "messages/v1"


def test_header_snapshot_labels_openai_api_modes() -> None:
    client = HeaderFakeClient()
    client.settings.update(
        {
            "llm_provider": "openai",
            "llm_model": "gpt-5.5",
            "openai_api_mode": "responses",
        }
    )
    client.context_stats["model"] = "gpt-5.5"

    responses_snapshot = run(build_header_snapshot(make_state(), client))
    assert responses_snapshot.provider == "OpenAI API"
    assert responses_snapshot.api_type == "responses/v1"

    client.thread_config["llm_config"] = {"openai_api_mode": "chat_completions"}
    completions_snapshot = run(build_header_snapshot(make_state(), client))
    assert completions_snapshot.api_type == "chat completions/v1"


def test_header_snapshot_labels_openrouter_concisely() -> None:
    client = HeaderFakeClient()
    client.settings.update(
        {
            "llm_provider": "openrouter",
            "llm_model": "anthropic/claude-sonnet-4.6",
            "openai_api_mode": "responses",
        }
    )
    client.context_stats["model"] = "anthropic/claude-sonnet-4.6"

    snapshot = run(build_header_snapshot(make_state(), client))

    assert snapshot.provider == "OpenRouter"
    assert snapshot.api_type == "responses/v1"


class PartialHeaderClient(HeaderFakeClient):
    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("config unavailable")


def test_header_snapshot_degrades_when_optional_calls_fail_or_are_missing() -> None:
    snapshot = run(build_header_snapshot(make_state(), PartialHeaderClient()))
    missing_snapshot = run(
        build_header_snapshot(
            make_state(), DisconnectedAgentClient(default_user_id="alice")
        )
    )

    assert snapshot.thread_id == "thread-123456"
    assert "thread_config" in snapshot.failures
    assert snapshot.connection_label.startswith("api ok")
    assert missing_snapshot.connection_label == "disconnected"
    assert missing_snapshot.backend_url == ""


class LocalHeaderClient:
    connection_label = "local agent"


def test_header_snapshot_handles_missing_api_methods_on_local_client() -> None:
    snapshot = run(build_header_snapshot(make_state(), LocalHeaderClient()))

    assert snapshot.thread_id == "thread-123456"
    assert snapshot.user_id == "alice"
    assert snapshot.connection_label == "local"
    assert snapshot.failures == ()
    assert concise_connection_label(snapshot, LocalHeaderClient()) == "local"


def test_thinking_mode_derivation() -> None:
    assert (
        thinking_mode(
            provider="anthropic",
            model="claude-sonnet-4-6",
            extended=True,
            effort="high",
        )
        == "adaptive (high)"
    )
    assert (
        thinking_mode(
            provider="anthropic",
            model="claude-sonnet-4",
            extended=True,
            effort=None,
        )
        == "medium"
    )
    assert (
        thinking_mode(
            provider="openai", model="gpt-5.5", extended=False, effort="xhigh"
        )
        == "xhigh"
    )
    assert (
        thinking_mode(provider="openai", model="gpt-4o", extended=False, effort=None)
        == "off"
    )


def test_rich_header_render_contains_dashboard_fields_and_fits_width() -> None:
    stream = io.StringIO()
    console = Console(file=stream, width=92, force_terminal=False, color_system=None)
    state = SimpleNamespace(console=console)
    snapshot = CLIHeaderSnapshot(
        thread_id="thread-123456",
        user_id="alice",
        thread_title="Procurement Review",
        platform="desktop",
        pinned=True,
        callable=True,
        callable_name="Analyst",
        callable_team="Ops",
        provider="Claude API",
        model="claude-sonnet-4-6",
        thinking_mode="adaptive (high)",
        context_tokens=41000,
        context_limit=200000,
        context_percent=20.5,
        compaction_count=2,
        tool_count=8,
        mcp_tool_count=3,
        callable_tool_count=2,
        skill_count=4,
        skill_kit_count=1,
        todo_count=3,
        todo_labels=("Review PO", "Email"),
        trigger_count=1,
        trigger_labels=("Morning brief",),
        flags=("instructions", "system prompt", "TODOs", "profile"),
        backend_url="http://api.test",
        health=HeaderHealthSnapshot(
            status="ok", backend_url="http://api.test", latency_ms=24
        ),
        api_type="messages/v1",
    )

    render_welcome(
        state,
        snapshot,
        capabilities=FakeTerminalCapabilities(width=92),
    )
    output = stream.getvalue()

    assert "N Y M E R I A" in output
    assert "┌" in output
    assert "╭" not in output
    assert "Procurement Review" in output
    assert "Claude API (messages/v1)" in output
    assert "(messages/v1)" in output
    assert "claude-sonnet-4-6 (Adaptive High)" in output
    assert "thinking adaptive (high)" not in output
    assert "http://api.test" in output
    assert "api ok 24ms" in output
    assert "8 tools" in output
    assert "3 mcp" in output
    assert "1 kits" in output
    assert "Review PO, Email, +1 more" in output
    assert "Morning brief" in output
    assert "instructions" in output
    assert "system prompt" in output
    assert all(cell_len(line) <= 79 for line in output.splitlines())


def test_rich_header_caps_width_for_scrollback_resize_stability() -> None:
    stream = io.StringIO()
    console = Console(file=stream, width=140, force_terminal=False, color_system=None)
    state = SimpleNamespace(console=console)

    render_welcome(
        state,
        CLIHeaderSnapshot(
            thread_id="thread-123456",
            user_id="alice",
            thread_title="A Wide Startup Terminal",
            provider="OpenRouter",
            model="anthropic/claude-sonnet-4.6",
            api_type="responses/v1",
            health=HeaderHealthSnapshot(status="ok", latency_ms=18),
        ),
        capabilities=FakeTerminalCapabilities(width=140),
    )

    assert all(cell_len(line) <= 79 for line in stream.getvalue().splitlines())


def test_cli_app_refresh_hooks_mark_and_render_header(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_build_header_snapshot(state, client, *, runtime_config=None):
        calls.append(f"{state.thread_id}:{state.user_id}:{client.connection_label}")
        return CLIHeaderSnapshot(
            thread_id=state.thread_id,
            user_id=state.user_id,
            thread_title="Fixture",
            backend_url="http://api.test",
            health=HeaderHealthSnapshot(
                status="ok",
                backend_url="http://api.test",
                latency_ms=12,
            ),
        )

    monkeypatch.setattr(
        "nymeria.triggers.cli.app.build_header_snapshot",
        fake_build_header_snapshot,
    )
    app = CLIApp(agent=None, thread_id="thread-1", user_id="alice")
    app.state.console = Console(file=io.StringIO(), width=90, force_terminal=False)
    app._client = HeaderFakeClient()
    caps = SimpleNamespace(width=90, renderer="rich", color_enabled=False)

    app._refresh_header_snapshot(caps)
    app._apply_repl_command_result(CommandResult.clear(), caps)
    run(app._dispatch_repl_action({"type": "switch_thread", "thread_id": "thread-2"}))
    app._refresh_and_render_pending_header(caps)
    run(
        app._dispatch_repl_action(
            {"type": "replace_client", "client": HeaderFakeClient()}
        )
    )
    app._refresh_and_render_pending_header(caps)
    run(app._dispatch_repl_action({"type": "set_model", "model": "gpt-5.5"}))
    app._refresh_and_render_pending_header(caps)
    run(app._dispatch_repl_action({"type": "thread_config_updated"}))
    app._refresh_and_render_pending_header(caps)
    run(app._dispatch_repl_action({"type": "switch_user", "user_id": "bob"}))
    app._refresh_and_render_pending_header(caps)

    assert len(calls) == 7
    assert calls[0].startswith("thread-1:alice")
    assert any(call.startswith("thread-2:bob") for call in calls)
