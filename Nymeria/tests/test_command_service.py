from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cli_fixtures import run
from nymeria.api.routers import todos as todos_router
from nymeria.api.routers.commands import create_commands_router
from nymeria.core.accounts import AuthenticatedUser
from nymeria.core.command_forms import (
    CommandOutput,
    command_data,
    command_error,
    command_info,
    command_success,
    command_warning,
    form_option,
    form_payload,
    form_tab,
    radio_field,
    render_outcome,
)
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandHttpClient,
    CommandService,
    _CommandBackendUser,
    _CommandExecutor,
)
from nymeria.core.todo_manager import TodoManager


class FakeCommandApi:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.todos: list[dict[str, Any]] = [
            {"status": "pending"},
            {"status": "in_progress"},
        ]
        self.memories = [{"key": "a", "value": "12345"}]
        self.env_set_keys: set[str] = set()
        # Settable so a test can model an APPLIED CLIProxy route, which on
        # the wire is an ordinary provider plus a proxy base URL.
        self.llm_base_url: str | None = None
        self.llm_provider = "openai"
        self.provider_test_result: dict[str, Any] = {"ok": True, "message": ""}
        self.thread_config = {"enabled_tools": [], "disabled_tools": [], "memory_char_limit": None}
        self.threads: list[dict[str, Any]] = [
            {
                "thread_id": "thread-1",
                "title": "Current",
                "pinned": False,
                "platform": "cli",
                "updated_at": "2026-05-10T12:00:00Z",
            },
            {
                "thread_id": "thread-2",
                "title": "Next",
                "pinned": True,
                "platform": "cli",
                "updated_at": "2026-05-10T13:00:00Z",
            },
        ]
        self.thread_teams: list[dict[str, Any]] = []

    async def close(self) -> None:
        self.closed = True

    async def cliproxy_auth_files(
        self, provider: str | None = None, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        # The default fake models an unconfigured install: the real facade
        # answers the wire-shaped 400 the /cliproxy routes return.
        self.calls.append(("cliproxy_auth_files", (provider,), {}))
        request = httpx.Request("GET", "http://test/cliproxy/auth-files")
        response = httpx.Response(
            400,
            json={"detail": "CLIProxy management is not configured"},
            request=request,
        )
        raise httpx.HTTPStatusError(
            "CLIProxy management is not configured",
            request=request,
            response=response,
        )

    async def list_threads(self, user_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_threads", (user_id,), {}))
        return [dict(thread) for thread in self.threads]

    async def list_thread_teams(self, user_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_thread_teams", (user_id,), {}))
        return [dict(team) for team in self.thread_teams]

    async def create_thread(
        self,
        user_id: str | None = None,
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("create_thread", (user_id,), {"thread_id": thread_id, "title": title}))
        created = {
            "thread_id": thread_id or "thread-new",
            "title": title or "New Chat",
            "pinned": False,
            "platform": "cli",
        }
        self.threads.append(dict(created))
        return created

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "update_thread_metadata",
                (thread_id,),
                {"user_id": user_id, "title": title, "pinned": pinned},
            )
        )
        for item in self.threads:
            if item["thread_id"] == thread_id:
                if title is not None:
                    item["title"] = title
                if pinned is not None:
                    item["pinned"] = pinned
                return dict(item)
        return {"thread_id": thread_id, "title": title, "pinned": pinned}

    async def delete_thread(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("delete_thread", (thread_id,), {"user_id": user_id}))
        self.threads = [item for item in self.threads if item["thread_id"] != thread_id]
        return {"ok": True, "thread_id": thread_id}

    async def branch_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        from_message_index: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "branch_thread",
                (thread_id,),
                {"user_id": user_id, "title": title, "from_message_index": from_message_index},
            )
        )
        return {
            "thread_id": "branch-1",
            "title": title or "Branch of Current",
            "from_message_index": from_message_index,
        }

    async def compact_thread(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("compact_thread", (thread_id,), {"user_id": user_id}))
        return {"messages_removed": 3, "messages_before": 8, "messages_after": 5}

    async def get_default_tools(self, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("get_default_tools", (user_id,), {}))
        return {
            "default_tools": ["bash_execute"],
            "available_tools": [
                {
                    "name": "bash_execute",
                    "description": "Execute shell commands\nwith safeguards",
                    "category": "general",
                },
                {
                    "name": "browser",
                    "description": "Use a browser",
                    "category": "web",
                },
            ],
        }

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("get_thread_config", (thread_id,), {"user_id": user_id}))
        return dict(self.thread_config)

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_settings", (), {"user_id": user_id}))
        return {
            "llm_provider": self.llm_provider,
            "llm_model": "gpt-test",
            "llm_base_url": self.llm_base_url,
            "llm_extended_thinking": False,
            "llm_reasoning_effort": None,
            "context_management": "auto_compact",
            "compact_threshold": 0.8,
            "memory_char_limit": 8000,
        }

    async def get_context_stats(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(("get_context_stats", (thread_id,), {}))
        return {
            "model": "gpt-test",
            "total_tokens": 1200,
            "context_limit": 8000,
            "usage_percentage": 15,
            "compaction_count": 0,
            "context_management": "auto_compact",
            "cumulative_tokens": 2400,
        }

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_history", (thread_id,), {"user_id": user_id, "include_internal": include_internal})
        )
        return {
            "thread_id": thread_id,
            "messages": [
                {"role": "human", "content": "make a report"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "name": "file_write",
                            "artifacts": [
                                {"path": "/workspace/report.txt", "name": "report.txt", "size_bytes": 2048},
                            ],
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "artifacts": [{"path": "/workspace/data.csv", "sizeBytes": 1500000}],
                },
            ],
        }

    async def get_tool_categories(self) -> dict[str, Any]:
        self.calls.append(("get_tool_categories", (), {}))
        return {"categories": {"general": ["bash_execute"], "web": ["browser"]}}

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "list_todos",
                (user_id,),
                {"filter_status": filter_status, "thread_id": thread_id},
            )
        )
        # Honor the filter the way both real clients do (active-only when
        # unset), so a handler that forgets filter_status="all" cannot pass
        # a done-visibility test (#143 review catch).
        items = [dict(item) for item in self.todos]
        if filter_status == "all":
            return items
        if filter_status:
            return [i for i in items if i.get("status") == filter_status]
        return [i for i in items if i.get("status") != "done"]

    async def add_todo(
        self,
        user_id: str,
        task: str,
        scheduled_for: str | None = "1d",
        notes: str | None = None,
        recurrence: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "add_todo",
                (user_id,),
                {
                    "task": task,
                    "scheduled_for": scheduled_for,
                    "notes": notes,
                    "recurrence": recurrence,
                    "thread_id": thread_id,
                },
            )
        )
        return {
            "id": "todo-new-12345",
            "task": task,
            "status": "pending",
            "scheduled_for": scheduled_for,
            "recurrence": recurrence,
            "thread_id": thread_id,
        }

    async def update_todo(self, user_id: str, todo_id: str, **patch: Any) -> dict[str, Any]:
        self.calls.append(("update_todo", (user_id, todo_id), dict(patch)))
        base = next((dict(t) for t in self.todos if t.get("id") == todo_id), {"id": todo_id})
        base.update({k: v for k, v in patch.items() if not k.startswith("clear_")})
        return base

    async def complete_todo(self, user_id: str, todo_id: str) -> dict[str, Any]:
        self.calls.append(("complete_todo", (user_id, todo_id), {}))
        base = next((dict(t) for t in self.todos if t.get("id") == todo_id), {"id": todo_id})
        base["status"] = "done"
        return base

    async def delete_todo(self, user_id: str, todo_id: str) -> dict[str, Any]:
        self.calls.append(("delete_todo", (user_id, todo_id), {}))
        return {"status": "ok", "deleted_id": todo_id}

    async def get_env_var(
        self,
        key: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("get_env_var", (key,), {"user_id": user_id}))
        return {"name": key, "value": "secret-value"}

    async def get_env_vars(self, *, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_env_vars", (), {"user_id": user_id}))
        entries = [
            {"name": name, "is_set": name in self.env_set_keys}
            for name in (
                "openai_api_key",
                "anthropic_api_key",
                "anthropic_direct_api_key",
                "openrouter_api_key",
            )
        ]
        return {"entries": entries}

    async def test_llm_provider_config(
        self,
        request: dict[str, Any],
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("test_llm_provider_config", (request,), {"user_id": user_id})
        )
        return dict(self.provider_test_result)

    async def list_memories(self, user_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_memories", (user_id,), {}))
        return list(self.memories)

    async def save_memory(self, user_id: str, key: str, value: str) -> dict[str, Any]:
        self.calls.append(("save_memory", (user_id, key, value), {}))
        self.memories.append({"key": key, "value": value})
        return {"key": key, "value": value}

    async def forget_memory(self, user_id: str, key: str) -> dict[str, Any]:
        self.calls.append(("forget_memory", (user_id, key), {}))
        self.memories = [m for m in self.memories if m.get("key") != key]
        return {"key": key}

    async def search_memories(self, user_id: str, query: str) -> list[dict[str, Any]]:
        self.calls.append(("search_memories", (user_id, query), {}))
        return [m for m in self.memories if query in m.get("key", "") or query in m.get("value", "")]

    async def update_settings(self, *, user_id: str | None = None, **kwargs) -> dict[str, Any]:
        self.calls.append(("update_settings", (), {"user_id": user_id, **kwargs}))
        return {"updated": list(kwargs), "restart_required": False}

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        self.calls.append(("update_thread_config", (thread_id,), {"user_id": user_id, **kwargs}))
        self.thread_config.update(kwargs)
        if kwargs.get("clear_memory_char_limit"):
            self.thread_config["memory_char_limit"] = None
        return dict(self.thread_config)


class _FakeAccountsRepo:
    def __init__(self, default_role: str = "user") -> None:
        self.claims: list[tuple[str, str]] = []
        self.default_role = default_role

    def claim_thread(self, thread_id: str, user_id: str) -> str:
        self.claims.append((thread_id, user_id))
        return user_id

    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id,
            email=f"{user_id}@example.test",
            display_name=user_id,
            role=self.default_role,
        )


class _FakeThreadLocks:
    def __init__(self) -> None:
        self.lock_info: dict[str, Any] | None = None

    def get_lock_info(self, thread_id: str):
        return self.lock_info


class _FakeThreadMetadataManager:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def delete_thread(self, user_id: str, thread_id: str) -> None:
        self.deleted.append((user_id, thread_id))


class _FakeAsyncGraph:
    async def aget_state(self, config: dict) -> SimpleNamespace:
        return SimpleNamespace(values={"messages": []})


class _GatherBaseFailure(BaseException):
    pass


class _PartiallyFailingCommandApi(FakeCommandApi):
    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        raise _GatherBaseFailure("settings unavailable")


class _FailingContextCommandApi(FakeCommandApi):
    async def get_context_stats(self, thread_id: str) -> dict[str, Any]:
        raise _GatherBaseFailure("context unavailable")

    async def get_tool_categories(self) -> dict[str, Any]:
        raise _GatherBaseFailure("categories unavailable")


class _FakeAgent:
    def __init__(self) -> None:
        self.accounts_repo = _FakeAccountsRepo()
        self._thread_locks = _FakeThreadLocks()
        self.aborted: list[str] = []
        self.thread_metadata_manager = _FakeThreadMetadataManager()
        self._default_async_graph = _FakeAsyncGraph()
        self.flushed: list[tuple[str, str]] = []

    def get_context_stats(self, thread_id: str) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "total_tokens": 1200,
            "context_limit": 8000,
            "usage_percentage": 15,
            "compaction_count": 0,
            "context_management": "auto_compact",
        }

    def abort_with_cascade(self, thread_id: str, *, restore_queue: bool = False):
        self.aborted.append(thread_id)
        restored = getattr(self, "restored_to_return", [])
        return list(restored) if restore_queue else []

    def _flush_memories_before_trim(self, user_id: str, thread_id: str, messages: list) -> None:
        self.flushed.append((user_id, thread_id))


def _command_names(service: CommandService, **kwargs: Any) -> set[str]:
    return {cmd.name for cmd in service.list_commands(**kwargs)}


def test_registry_exposes_full_path_metadata_and_visibility_filters() -> None:
    service = CommandService()

    admin_desktop = service.list_commands(actor="user", surface="desktop", is_admin=True)
    tools_list = next(cmd for cmd in admin_desktop if cmd.id == "tools.list")
    compact = next(cmd for cmd in admin_desktop if cmd.id == "compact")

    assert tools_list.name == "tools list"
    assert tools_list.path == ["tools", "list"]
    assert "/tools_enabled" in tools_list.aliases
    # `/tools_core` is an INJECTED alias (#133): it carries its filter
    # token, so it is truthfully listed with the rest.
    assert "/tools_core" in tools_list.aliases
    assert tools_list.scope == "global"
    assert tools_list.agent_allowed is True
    assert tools_list.requires_thread is False
    assert tools_list.execution_kind == "command"

    assert compact.execution_kind == "chat_stream"
    assert compact.agent_allowed is False
    assert compact.requires_thread is True

    non_admin = _command_names(
        service,
        actor="user",
        surface="desktop",
        is_admin=False,
    )
    assert "env get" not in non_admin
    assert "settings set" not in non_admin
    assert "settings" in non_admin

    agent = _command_names(service, actor="agent", surface="agent", is_admin=True)
    assert "compact" not in agent
    assert "tools list" in agent


def test_displayed_names_are_hyphenated_and_paths_stay_stored_form() -> None:
    """Rule 4 (#131 wave B): one displayed spelling, and it is the usage line's.

    `GET /commands` serializes `CommandInfo` verbatim, so `name` is what every
    palette prints and what the desktop composer inserts. It must agree with
    the generated `usage`, which has rendered hyphens since #129, while `path`
    keeps the stored underscore form the dispatcher matches on.
    """
    service = CommandService()
    by_id = {cmd.id: cmd for cmd in service.list_commands(actor="user", is_admin=True)}

    for command_id, name, path in (
        ("sequential_tools", "sequential-tools", ["sequential_tools"]),
        ("background.set_url", "background set-url", ["background", "set_url"]),
        (
            "provider.reasoning_passback",
            "provider reasoning-passback",
            ["provider", "reasoning_passback"],
        ),
    ):
        info = by_id[command_id]
        assert info.name == name
        assert info.path == path
        assert info.usage.startswith(f"/{name}"), info.usage

    # No payload name may carry an underscore at all.
    underscored = [cmd.name for cmd in by_id.values() if "_" in cmd.name]
    assert underscored == []

    # The derived subcommand list is display copy too: it is what the wire
    # payload carries and what a usage error prints after "Subcommands:".
    assert "set-url" in by_id["background"].subcommands
    assert "set_url" not in by_id["background"].subcommands

    # Aliases keep their stored spelling: the flat `family_verb` forms ARE the
    # chat platforms' command names, and Telegram rejects a hyphen in one.
    assert "/background_set_url" not in by_id["background.set_url"].aliases
    assert "/provider_reasoning_passback" in by_id["provider.reasoning_passback"].aliases
    assert "/tools_enabled" in by_id["tools.list"].aliases


def test_help_renders_the_hyphenated_spelling_everywhere() -> None:
    """The card title, the subcommand table, and the family list must agree."""
    service = CommandService()
    ctx = CommandContext(user_id="alice", actor="user", surface="cli", is_admin=True)

    card = run(service.execute(ctx, "/help background", api=FakeCommandApi()))
    assert card.success is True, card.markdown
    assert "## /background" in card.markdown
    # The subcommand row label and its generated usage speak one spelling.
    assert "| set-url | `/background set-url <base-url>` |" in card.markdown
    assert "set_url" not in card.markdown

    leaf = run(service.execute(ctx, "/help background set-url", api=FakeCommandApi()))
    assert leaf.success is True, leaf.markdown
    assert "## /background set-url" in leaf.markdown
    assert "set_url" not in leaf.markdown

    # The underscore spelling still RESOLVES; it just is not what is shown.
    typed_underscore = run(
        service.execute(ctx, "/help background set_url", api=FakeCommandApi())
    )
    assert typed_underscore.markdown == leaf.markdown

    index = run(service.execute(ctx, "/help", api=FakeCommandApi()))
    assert "`/sequential-tools`" in index.markdown
    assert "`/sequential_tools`" not in index.markdown

    listing = run(service.execute(ctx, "/help all", api=FakeCommandApi()))
    assert "`/sequential-tools`" in listing.markdown
    assert "`/background set-url`" in listing.markdown
    assert "_url" not in listing.markdown


def test_registry_rejects_duplicate_ids_paths_and_alias_conflicts() -> None:
    service = CommandService()

    with pytest.raises(ValueError, match="Duplicate command id"):
        service.register(
            "new command",
            id="help",
            description="Duplicate id",
            category="Tests",
        )

    with pytest.raises(ValueError, match="Duplicate command path"):
        service.register(
            "help",
            id="help.again",
            description="Duplicate path",
            category="Tests",
        )

    with pytest.raises(ValueError, match="conflicts with command path"):
        service.register(
            "test alias",
            description="Alias conflict",
            category="Tests",
            aliases=("tools enable",),
        )


def test_alias_resolution_and_command_path_execution() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="telegram",
                is_admin=True,
            ),
            "/tools_list core",
            api=api,
        )
    )

    assert result.success is True
    assert result.command == "tools list"
    # A readout is level "info" since #132 (the success collapse is gone).
    assert result.level == "info"
    assert "### Core Tools" in result.markdown
    assert "bash_execute" in result.markdown


def test_status_degrades_when_parallel_fetch_returns_base_exception() -> None:
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/status",
            api=_PartiallyFailingCommandApi(),
        )
    )

    assert result.success is True
    assert "### Nymeria Status" in result.markdown
    assert "1 pending / 1 in progress" in result.markdown


def test_context_degrades_when_parallel_fetch_returns_base_exception() -> None:
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/context",
            api=_FailingContextCommandApi(),
        )
    )

    assert result.success is True
    assert "### Context Breakdown" in result.markdown
    assert "gpt-test | openai" in result.markdown
    assert "0 / 0 tokens" in result.markdown


def test_memory_limit_command_shows_usage_and_updates_limits() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    shown = run(service.execute(ctx, "/memory limit", api=api))
    global_set = run(service.execute(ctx, "/memory limit 12000 global", api=api))
    thread_set = run(service.execute(ctx, "/memory limit 6000 thread", api=api))
    thread_clear = run(service.execute(ctx, "/memory limit inherit thread", api=api))

    assert shown.success is True
    assert "global: 8 / 8000 chars" in shown.markdown
    assert global_set.success is True
    assert ("update_settings", (), {"user_id": "alice", "memory_char_limit": 12000}) in api.calls
    assert thread_set.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "memory_char_limit": 6000},
    ) in api.calls
    assert thread_clear.success is True
    assert api.thread_config["memory_char_limit"] is None


def test_sequential_tools_command_shows_status_and_updates() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    shown = run(service.execute(ctx, "/sequential-tools", api=api))
    thread_on = run(service.execute(ctx, "/sequential-tools on", api=api))
    thread_off = run(service.execute(ctx, "/sequential-tools off", api=api))
    thread_inherit = run(service.execute(ctx, "/sequential-tools inherit", api=api))
    global_on = run(service.execute(ctx, "/sequential-tools on global", api=api))

    # Status: global default off, thread inherits (no override set).
    assert shown.success is True
    assert "global: off" in shown.markdown
    assert "inherits global" in shown.markdown

    # Thread override on / off go to update_thread_config as a flat boolean.
    assert thread_on.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "sequential_tool_execution": True},
    ) in api.calls
    assert thread_off.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "sequential_tool_execution": False},
    ) in api.calls

    # inherit clears the override.
    assert thread_inherit.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "clear_sequential_tool_execution": True},
    ) in api.calls

    # global writes the server setting.
    assert global_on.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "sequential_tool_execution": True},
    ) in api.calls


def test_sequential_tools_command_global_works_without_thread() -> None:
    # The global scope must not require a thread; the default (thread) scope
    # does, and never silently falls back to writing the global default.
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id=None,
        actor="user",
        surface="cli",
        is_admin=True,
    )

    global_off = run(service.execute(ctx, "/sequential-tools off global", api=api))
    assert global_off.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "sequential_tool_execution": False},
    ) in api.calls

    needs_thread = run(service.execute(ctx, "/sequential-tools on", api=api))
    assert needs_thread.success is False
    assert "requires an active thread" in needs_thread.markdown
    # Exactly one global write happened: the default scope did not become
    # "global" just because there was no thread to write.
    assert [call for call in api.calls if call[0] == "update_settings"] == [
        ("update_settings", (), {"user_id": "alice", "sequential_tool_execution": False})
    ]


def test_sequential_tools_command_status_shows_override_and_default_alias() -> None:
    # Covers the status (override) display branch and the `default` alias of `inherit`.
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    # An explicit thread override renders distinctly from "inherits global".
    run(service.execute(ctx, "/sequential-tools on", api=api))
    status = run(service.execute(ctx, "/sequential-tools", api=api))
    assert status.success is True
    assert "thread: on (override)" in status.markdown

    # `default` is an alias of `inherit` and clears the override.
    cleared = run(service.execute(ctx, "/sequential-tools default", api=api))
    assert cleared.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "clear_sequential_tool_execution": True},
    ) in api.calls


def test_sequential_tools_in_process_whitelist_persists_tristate() -> None:
    """Slash commands persist via ``CommandBackendClient.update_thread_config``,
    NOT the HTTP PATCH route, so its separate kwarg whitelist must round-trip the
    tri-state. Locks on->True, off->explicit False (not omitted), clear->None, and
    the clear-flag-wins precedence the HTTP route also enforces.
    """
    from nymeria.core.thread_config import ThreadConfig

    class _Manager:
        def __init__(self) -> None:
            self.configs: dict[str, ThreadConfig] = {}

        def get_config(self, thread_id: str):
            return self.configs.get(thread_id)

        def save_config(self, tc: ThreadConfig) -> bool:
            self.configs[tc.thread_id] = tc
            return True

    manager = _Manager()
    agent = SimpleNamespace(
        thread_config_manager=manager,
        accounts_repo=SimpleNamespace(claim_thread=lambda tid, uid: uid),
        invalidate_thread_config_cache=lambda tid: None,
    )
    user = _CommandBackendUser(id="alice", role="admin")
    client = CommandBackendClient(agent, user=user)

    run(client.update_thread_config("t1", user_id="alice", sequential_tool_execution=True))
    assert manager.configs["t1"].sequential_tool_execution is True

    run(client.update_thread_config("t1", user_id="alice", sequential_tool_execution=False))
    assert manager.configs["t1"].sequential_tool_execution is False

    run(client.update_thread_config("t1", user_id="alice", clear_sequential_tool_execution=True))
    assert manager.configs["t1"].sequential_tool_execution is None

    # The clear flag wins even when a boolean is also supplied (HTTP-route parity).
    run(
        client.update_thread_config(
            "t1",
            user_id="alice",
            sequential_tool_execution=True,
            clear_sequential_tool_execution=True,
        )
    )
    assert manager.configs["t1"].sequential_tool_execution is None


def test_fast_command_switches_thread_and_sets_global() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    toggled = run(service.execute(ctx, "/fast", api=api))
    did_set = run(service.execute(ctx, "/fast set gpt-mini", api=api))

    assert toggled.success is True
    # LLM_FAST_MODEL is unset, so "fast" resolves to openai's provider default.
    assert (
        "update_thread_config",
        ("thread-1",),
        {
            "user_id": "alice",
            "llm_config": {"provider": "openai", "model": "gpt-4o-mini"},
        },
    ) in api.calls
    assert did_set.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_fast_model": "gpt-mini"},
    ) in api.calls


class _SmartTierCommandApi(FakeCommandApi):
    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        data = await super().get_settings(user_id=user_id)
        data["llm_smart_model"] = "anthropic:claude-opus-4-8"
        return data


def test_smart_command_switches_to_cross_provider_tier() -> None:
    service = CommandService()
    api = _SmartTierCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    result = run(service.execute(ctx, "/smart on", api=api))

    assert result.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {
            "user_id": "alice",
            "llm_config": {"provider": "anthropic", "model": "claude-opus-4-8"},
        },
    ) in api.calls


def test_fast_set_rejects_tier_alias() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    result = run(service.execute(ctx, "/fast set smart", api=api))

    assert result.success is False
    assert not any(name == "update_settings" for name, _args, _kw in api.calls)


def test_background_command_shows_set_and_clears() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    # Show: unset background resolves to the main model, no settings write.
    shown = run(service.execute(ctx, "/background", api=api))
    assert shown.success is True
    assert "gpt-test" in shown.markdown
    assert not any(name == "update_settings" for name, _a, _kw in api.calls)

    # Set the model.
    did_set = run(service.execute(ctx, "/background set llama-3.3-70b", api=api))
    assert did_set.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_background_model": "llama-3.3-70b"},
    ) in api.calls

    # Set a base URL override.
    did_url = run(service.execute(ctx, "/background set-url http://localhost:1234/v1", api=api))
    assert did_url.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_background_base_url": "http://localhost:1234/v1"},
    ) in api.calls

    # Clear writes empty strings for both keys.
    did_clear = run(service.execute(ctx, "/background clear", api=api))
    assert did_clear.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_background_model": "", "llm_background_base_url": ""},
    ) in api.calls

    # Global-only: never touches the thread config.
    assert not any(name == "update_thread_config" for name, _a, _kw in api.calls)


def test_background_set_rejects_tier_alias() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    result = run(service.execute(ctx, "/background set smart", api=api))

    assert result.success is False
    assert not any(name == "update_settings" for name, _a, _kw in api.calls)


def test_fallback_add_updates_global_chain() -> None:
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    added = run(service.execute(ctx, "/fallback add openai:gpt-4o-mini", api=api))

    assert added.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_fallback_models": "openai:gpt-4o-mini"},
    ) in api.calls


class _OffEffortCommandApi(FakeCommandApi):
    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        data = await super().get_settings(user_id=user_id)
        data["llm_reasoning_effort"] = "off"
        return data


def test_think_on_global_clears_persisted_off_effort() -> None:
    """/think on global after /think off must clear effort="off" (null)."""
    api = _OffEffortCommandApi()

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/think on global",
            api=api,
        )
    )

    assert result.success is True
    assert "effort reset to default" in result.markdown
    updates = [call for call in api.calls if call[0] == "update_settings"]
    assert updates == [
        (
            "update_settings",
            (),
            {
                "user_id": "alice",
                "llm_extended_thinking": True,
                "llm_reasoning_effort": None,
            },
        )
    ]


def test_think_on_in_thread_with_global_off_guides_instead_of_no_op() -> None:
    """A thread-scoped on cannot neutralize a globally persisted "off"
    (the choke point resolves a thread "" back to the global value), so the
    handler must guide instead of writing a no-op."""
    api = _OffEffortCommandApi()

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/think on",
            api=api,
        )
    )

    assert result.success is False
    assert "/think on global" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]
    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_think_on_defaults_to_thread_scope_when_thread_active() -> None:
    api = FakeCommandApi()

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/think on",
            api=api,
        )
    )

    assert result.success is True
    assert "this thread" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]
    thread_updates = [
        call for call in api.calls if call[0] == "update_thread_config"
    ]
    assert thread_updates == [
        (
            "update_thread_config",
            ("thread-1",),
            {"user_id": "alice", "llm_config": {"extended_thinking": True}},
        )
    ]


def test_think_on_without_persisted_off_leaves_effort_untouched() -> None:
    api = FakeCommandApi()

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/think on global",
            api=api,
        )
    )

    assert result.success is True
    updates = [call for call in api.calls if call[0] == "update_settings"]
    assert updates == [
        (
            "update_settings",
            (),
            {"user_id": "alice", "llm_extended_thinking": True},
        )
    ]


class _ModeledCommandApi(FakeCommandApi):
    """FakeCommandApi reporting a global provider/model for clamp notes."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__()
        self.provider = provider
        self.model = model

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        data = await super().get_settings(user_id=user_id)
        data["llm_provider"] = self.provider
        data["llm_model"] = self.model
        return data


def _run_think(api: FakeCommandApi, command: str):
    return run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=True,
                supports_forms=True,
            ),
            command,
            api=api,
        )
    )


def test_think_over_ask_reports_clamped_level() -> None:
    api = _ModeledCommandApi("openai", "gpt-5.1")

    result = _run_think(api, "/think xhigh")

    assert result.success is True
    assert "effort: xhigh" in result.markdown
    assert "gpt-5.1 runs at high" in result.markdown


def test_think_supported_level_has_no_clamp_note() -> None:
    api = _ModeledCommandApi("anthropic", "claude-opus-4-8")

    result = _run_think(api, "/think max")

    assert result.success is True
    assert "runs at" not in result.markdown


def test_think_off_on_undisableable_model_reports_floor() -> None:
    api = _ModeledCommandApi("anthropic", "claude-fable-5")

    result = _run_think(api, "/think off")

    assert result.success is True
    assert "cannot disable thinking" in result.markdown
    assert "runs at low" in result.markdown


def test_think_show_lists_supported_levels() -> None:
    api = _ModeledCommandApi("anthropic", "claude-fable-5")

    result = _run_think(api, "/think")

    assert result.success is True
    assert "low, medium, high, xhigh, max (claude-fable-5)" in result.markdown
    # Scope-aware show: global state plus the active thread's override row.
    assert "Global" in result.markdown
    assert "Effective" in result.markdown


def test_think_level_in_thread_writes_thread_config_and_reasoning_hint() -> None:
    api = FakeCommandApi()

    result = _run_think(api, "/think high")

    assert result.success is True
    assert "this thread" in result.markdown
    thread_updates = [
        call for call in api.calls if call[0] == "update_thread_config"
    ]
    assert thread_updates == [
        (
            "update_thread_config",
            ("thread-1",),
            {
                "user_id": "alice",
                "llm_config": {"extended_thinking": True, "reasoning_effort": "high"},
            },
        )
    ]
    assert result.data == {"state": {"reasoning": {"enabled": True, "effort": "high"}}}


def test_think_off_in_thread_persists_off_and_hint_disables() -> None:
    api = FakeCommandApi()

    result = _run_think(api, "/think off")

    assert result.success is True
    thread_updates = [
        call for call in api.calls if call[0] == "update_thread_config"
    ]
    assert thread_updates == [
        (
            "update_thread_config",
            ("thread-1",),
            {
                "user_id": "alice",
                "llm_config": {"extended_thinking": False, "reasoning_effort": "off"},
            },
        )
    ]
    assert result.data is not None
    assert result.data["state"]["reasoning"]["enabled"] is False


def test_reasoning_and_thinking_are_catalog_aliases_of_think() -> None:
    api = FakeCommandApi()

    shown = _run_think(api, "/reasoning")
    assert shown.success is True
    assert "Thinking:" in shown.markdown

    set_result = _run_think(api, "/thinking medium global")
    assert set_result.success is True
    updates = [call for call in api.calls if call[0] == "update_settings"]
    assert updates == [
        (
            "update_settings",
            (),
            {
                "user_id": "alice",
                "llm_extended_thinking": True,
                "llm_reasoning_effort": "medium",
            },
        )
    ]


def test_think_bare_attaches_the_scope_form() -> None:
    """Bare /think keeps its markdown and declares the level picker: one tab
    per writable scope, each with its own tab-level submit template."""
    api = FakeCommandApi()
    api.thread_config["llm_config"] = {"reasoning_effort": "medium"}

    result = _run_think(api, "/think")

    assert result.success is True
    assert "Thinking:" in result.markdown  # fallback intact
    form = (result.data or {}).get("form")
    assert form is not None
    assert form["title"] == "Thinking"
    assert form["submit"] == {"command": "think {thread_level} thread"}
    labels = [tab["label"] for tab in form["tabs"]]
    assert labels == ["This thread", "Global"]
    assert form["tabs"][0]["submit"] == {"command": "think {thread_level} thread"}
    assert form["tabs"][1]["submit"] == {"command": "think {global_level} global"}
    # Distinct field keys per tab: the CLI renderer keys its cursor by field
    # key alone, so a shared key would park the cursor on the OTHER tab's
    # current value (last tab wins in init_state) and Enter would apply the
    # wrong level.
    assert form["tabs"][0]["fields"][0]["key"] == "thread_level"
    assert form["tabs"][1]["fields"][0]["key"] == "global_level"

    def current_of(tab: dict[str, Any]) -> str:
        options = tab["fields"][0]["options"]
        assert [o["id"] for o in options] == [
            "off", "on", "low", "medium", "high", "xhigh", "max",
        ]
        return next(o["id"] for o in options if o["current"])

    # Thread tab parks on the thread's effective value (the override);
    # global tab on the global state (thinking disabled, no effort).
    assert current_of(form["tabs"][0]) == "medium"
    assert current_of(form["tabs"][1]) == "off"


def test_think_form_meta_carries_clamp_notes_for_the_active_model() -> None:
    api = _ModeledCommandApi("openai", "gpt-5.1")

    result = _run_think(api, "/think")

    form = (result.data or {}).get("form")
    assert form is not None
    options = form["tabs"][0]["fields"][0]["options"]
    xhigh = next(o for o in options if o["id"] == "xhigh")
    assert xhigh["meta"] == "runs at high"


def test_think_bare_without_thread_offers_only_the_global_tab() -> None:
    api = FakeCommandApi()

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="",
                actor="user",
                surface="cli",
                is_admin=True,
                supports_forms=True,
            ),
            "/think",
            api=api,
        )
    )

    assert result.success is True
    form = (result.data or {}).get("form")
    assert form is not None
    assert [tab["label"] for tab in form["tabs"]] == ["Global"]
    assert form["submit"] == {"command": "think {global_level} global"}


def test_think_show_reports_on_for_an_effort_only_config() -> None:
    """A persisted level enables thinking at every provider factory even
    with the extended_thinking flag off (they gate on either signal), so the
    status line and the form's current flag agree on that."""

    class _EffortOnlyApi(FakeCommandApi):
        async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
            data = await super().get_settings(user_id=user_id)
            data["llm_reasoning_effort"] = "high"
            return data

    result = _run_think(_EffortOnlyApi(), "/think")

    assert result.success is True
    assert "Thinking: on" in result.markdown
    form = (result.data or {}).get("form")
    assert form is not None
    options = form["tabs"][1]["fields"][0]["options"]
    assert next(o["id"] for o in options if o["current"]) == "high"


def test_think_rejects_unknown_tokens() -> None:
    api = FakeCommandApi()

    result = _run_think(api, "/think sideways")

    assert result.success is False
    assert "Usage:" in result.markdown
    assert not [call for call in api.calls if call[0].startswith("update_")]


# ── /settings (delegating alias of the /config family) ─────────────────────


def _run_command(
    api: FakeCommandApi,
    command: str,
    *,
    is_admin: bool = True,
    surface: str = "cli",
):
    return run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface=surface,
                is_admin=is_admin,
                supports_forms=True,
            ),
            command,
            api=api,
        )
    )


def test_settings_bare_and_view_delegate_to_config_show() -> None:
    api = FakeCommandApi()

    bare = _run_command(api, "/settings")
    view = _run_command(api, "/settings view")

    for result in (bare, view):
        assert result.success is True
        assert "provider: openai" in result.markdown
        assert "model: gpt-test" in result.markdown


def test_settings_get_and_set_delegate_to_config_handlers() -> None:
    api = FakeCommandApi()

    got = _run_command(api, "/settings get llm_model")
    assert got.success is True
    assert "llm_model = gpt-test" in got.markdown

    set_result = _run_command(api, "/settings set log_level DEBUG")
    assert set_result.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "log_level": "DEBUG"},
    ) in api.calls

    unknown = _run_command(api, "/settings frobnicate")
    assert unknown.success is False
    assert "Unexpected argument `frobnicate`" in unknown.markdown
    assert "Valid subcommands: get, set." in unknown.markdown
    assert "Usage: `/settings`" in unknown.markdown


# ── /provider family ────────────────────────────────────────────────────────


def test_provider_show_reports_active_provider_and_credential_status() -> None:
    api = FakeCommandApi()
    api.env_set_keys = {"openai_api_key"}

    result = _run_command(api, "/provider")

    assert result.success is True
    assert "OpenAI" in result.markdown
    assert "authenticated (server)" in result.markdown
    assert "gpt-test" in result.markdown


def test_provider_show_degrades_when_env_listing_is_admin_gated() -> None:
    class _EnvDeniedApi(FakeCommandApi):
        async def get_env_vars(self, *, user_id: str | None = None) -> dict[str, Any]:
            raise RuntimeError("403 admin required")

    result = _run_command(_EnvDeniedApi(), "/provider")

    assert result.success is True
    assert "unknown (unavailable (admin only))" in result.markdown


def test_provider_root_attaches_the_providers_cliproxy_form() -> None:
    """Bare /provider keeps its markdown and declares the two-layer entry
    point: a Providers tab (registry-wide, tier-grouped) submitting into
    the per-provider ACTION step (/provider <name>), and a CLIProxy tab
    listing the subscription-OAuth catalog behind /provider cliproxy."""
    api = FakeCommandApi()
    api.env_set_keys = {"openai_api_key"}

    result = _run_command(api, "/provider")

    assert result.success is True
    assert "Active provider" in result.markdown  # fallback intact
    form = (result.data or {}).get("form")
    assert form is not None
    assert form["version"] == 1
    assert form["title"] == "Provider"
    assert form["submit"] == {"command": "provider {provider}"}
    labels = [tab["label"] for tab in form["tabs"]]
    assert labels == ["Providers", "CLIProxy"]
    assert form["tabs"][0]["submit"] == {"command": "provider {provider}"}
    assert form["tabs"][1]["submit"] == {"command": "provider cliproxy {target}"}

    fields = form["tabs"][0]["fields"]
    assert [field["kind"] for field in fields] == ["search", "radio"]
    options = fields[1]["options"]
    by_id = {option["id"]: option for option in options}
    # Registry-wide, not the /provider set trio.
    assert "groq" in by_id
    assert by_id["openai"]["current"] is True
    # Credential status rides meta for the managed trio only.
    assert "authenticated" in by_id["openai"]["meta"]
    assert "authenticated" not in by_id["groq"]["meta"]
    # notes_for_user is folded into meta (the renderer shows meta OR
    # description, and meta is never empty here); description stays empty.
    from nymeria.config.llm_providers import get_llm_provider_spec

    groq_spec = get_llm_provider_spec("groq")
    assert groq_spec is not None and groq_spec.notes_for_user
    assert groq_spec.notes_for_user in by_id["groq"]["meta"]
    assert all(not option["description"] for option in options)
    # Grouped by tier: every native option precedes the first gateway one.
    tiers = ["[NATIVE]" if "[NATIVE]" in o["meta"] else "" for o in options]
    assert "[NATIVE]" not in tiers[tiers.index("") :]
    # The CLIProxy tab mirrors the subscription catalog.
    from nymeria.cliproxy.catalog import list_cliproxy_providers

    cliproxy_ids = [o["id"] for o in form["tabs"][1]["fields"][0]["options"]]
    assert cliproxy_ids == [spec.id for spec in list_cliproxy_providers()]
    assert "claude" in cliproxy_ids
    # No secret material anywhere in the payload.
    assert "sk-" not in str(form)


def test_provider_cliproxy_reports_target_route_and_management_status() -> None:
    api = FakeCommandApi()

    overview = _run_command(api, "/provider cliproxy")
    assert overview.success is True
    assert "claude" in overview.markdown
    assert "not configured" in overview.markdown

    detail = _run_command(api, "/provider cliproxy claude")
    assert detail.success is True
    assert "Claude (Max/Pro subscription)" in detail.markdown
    assert "anthropic" in detail.markdown  # route shape
    assert "cliproxy_management_url" in detail.markdown  # enable guidance

    unknown = _run_command(api, "/provider cliproxy nope")
    assert unknown.success is False
    assert "Unknown CLIProxy target" in unknown.markdown


def test_provider_root_gives_non_admins_the_picker_without_cliproxy() -> None:
    """The picker now submits into the ungated action step, so non-admins
    browse too (their action step offers the thread scope); only the
    CLIProxy tab, whose submit target refuses them, is dropped. The agent
    actor gets the same treatment (provider cliproxy is agent_allowed
    False)."""
    api = FakeCommandApi()

    result = _run_command(api, "/provider", is_admin=False)

    assert result.success is True
    assert "Active provider" in result.markdown
    form = (result.data or {}).get("form")
    assert form is not None
    labels = [tab["label"] for tab in form["tabs"]]
    assert labels == ["Providers"]
    assert form["tabs"][0]["submit"] == {"command": "provider {provider}"}

    agent_result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="agent",
                surface="agent",
                is_admin=None,
                # The flag controls DELIVERY, the actor controls CONTENT:
                # an agent-driven form client still gets the agent-shaped tabs.
                supports_forms=True,
            ),
            "/provider",
            api=api,
        )
    )
    agent_form = (agent_result.data or {}).get("form")
    assert agent_form is not None
    assert [tab["label"] for tab in agent_form["tabs"]] == ["Providers"]


def test_provider_action_step_offers_use_setup_cliproxy_and_test() -> None:
    """/provider <name> is the per-provider action step: status markdown
    plus tabs of next moves, each submitting a REAL registered command so
    the dispatch gate stays the only authorization layer."""
    api = FakeCommandApi()

    result = _run_command(api, "/provider anthropic")

    assert result.success is True
    assert "Provider: Anthropic" in result.markdown
    assert "missing key" in result.markdown  # credential row, trio provider
    # The markdown fallback names every typed equivalent.
    assert "/provider switch anthropic [global|thread]" in result.markdown
    assert "/provider setup anthropic" in result.markdown
    assert "/provider cliproxy claude" in result.markdown
    assert "/provider test anthropic" in result.markdown

    form = (result.data or {}).get("form")
    assert form is not None
    assert form["title"] == "Provider: Anthropic"
    labels = [tab["label"] for tab in form["tabs"]]
    # anthropic has a CLIProxy OAuth target (claude), so all four tabs.
    assert labels == ["Use", "Set up", "CLIProxy", "Test"]
    submits = {tab["label"]: tab["submit"]["command"] for tab in form["tabs"]}
    assert submits == {
        "Use": "provider switch anthropic {scope}",
        "Set up": "provider setup anthropic",
        "CLIProxy": "provider cliproxy {target}",
        "Test": "provider test anthropic",
    }
    by_label = {tab["label"]: tab for tab in form["tabs"]}
    scope_ids = [
        option["id"] for option in by_label["Use"]["fields"][0]["options"]
    ]
    assert scope_ids == ["thread", "global"]
    # Set up and Test are fieldless ACTION tabs (#139): their placeholder-
    # free templates dispatch as-is on Enter, and the description explains
    # the action (this retired the live-token one-option-radio workaround).
    assert by_label["Set up"]["fields"] == []
    assert "Chained setup" in by_label["Set up"]["description"]
    assert by_label["Test"]["fields"] == []
    assert "connectivity test" in by_label["Test"]["description"]
    assert "claude" in [
        o["id"] for o in by_label["CLIProxy"]["fields"][0]["options"]
    ]
    # Distinct field keys across the fielded tabs (the /think cursor trap).
    keys = [
        tab["fields"][0]["key"] for tab in form["tabs"] if tab["fields"]
    ]
    assert len(keys) == len(set(keys))
    assert "sk-" not in str(form)


def test_provider_action_step_without_cliproxy_target_or_thread() -> None:
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id=None,
                actor="user",
                surface="cli",
                is_admin=True,
                supports_forms=True,
            ),
            "/provider groq",
            api=api,
        )
    )

    assert result.success is True
    assert "GROQ_API_KEY" in result.markdown  # untracked provider env hint
    form = (result.data or {}).get("form")
    assert form is not None
    labels = [tab["label"] for tab in form["tabs"]]
    # groq has no OAuth target; no thread context, so scope offers global only.
    assert labels == ["Use", "Set up", "Test"]
    by_label = {tab["label"]: tab for tab in form["tabs"]}
    scope_ids = [
        option["id"] for option in by_label["Use"]["fields"][0]["options"]
    ]
    assert scope_ids == ["global"]


def test_provider_action_step_marks_current_scopes() -> None:
    api = FakeCommandApi()
    api.thread_config["llm_config"] = {"provider": "anthropic"}

    result = _run_command(api, "/provider anthropic")

    assert "this thread's override" in result.markdown
    form = (result.data or {}).get("form")
    scope = {
        option["id"]: option
        for option in form["tabs"][0]["fields"][0]["options"]
    }
    assert scope["thread"]["current"] is True
    assert scope["global"]["current"] is False

    active = _run_command(api, "/provider openai")
    assert "the global default" in active.markdown
    active_form = (active.data or {}).get("form")
    active_scope = {
        option["id"]: option
        for option in active_form["tabs"][0]["fields"][0]["options"]
    }
    assert active_scope["global"]["current"] is True


def test_provider_action_step_non_admin_gets_thread_scope_only() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider anthropic", is_admin=False)

    assert result.success is True
    # The markdown offers only what the caller can run.
    assert "/provider switch anthropic" in result.markdown
    assert "/provider setup" not in result.markdown
    assert "/provider test" not in result.markdown
    form = (result.data or {}).get("form")
    assert form is not None
    labels = [tab["label"] for tab in form["tabs"]]
    assert labels == ["Use"]
    scope_ids = [
        option["id"] for option in form["tabs"][0]["fields"][0]["options"]
    ]
    assert scope_ids == ["thread"]


def test_provider_action_step_agent_actor_omits_the_secret_flows() -> None:
    # provider setup / cliproxy are agent_allowed=False at dispatch; the
    # action step mirrors that cosmetically so the agent is never handed a
    # submit it cannot run. Switch and test stay (agent context is
    # is_admin=None, and the gates block only on a definite False).
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="agent",
                surface="agent",
                is_admin=None,
                supports_forms=True,
            ),
            "/provider anthropic",
            api=api,
        )
    )

    assert result.success is True
    assert "/provider setup" not in result.markdown
    assert "/provider cliproxy" not in result.markdown
    assert "/provider test anthropic" in result.markdown
    form = (result.data or {}).get("form")
    assert form is not None
    labels = [tab["label"] for tab in form["tabs"]]
    assert labels == ["Use", "Test"]


def test_provider_action_step_unknown_provider() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider bogus")

    assert result.success is False
    assert "Unknown provider" in result.markdown
    assert "/provider list" in result.markdown


def test_provider_action_step_suggests_mistyped_subcommands() -> None:
    # One token is valid grammar now, so "/provider lst" reaches the
    # action step; the guidance layer's suggestion must survive instead
    # of misdiagnosing the typo as an unknown provider.
    api = FakeCommandApi()

    result = _run_command(api, "/provider lst")

    assert result.success is False
    assert "Usage:" in result.markdown
    assert "Did you mean `/provider list`?" in result.markdown


def test_provider_action_step_resolves_stored_aliases() -> None:
    # llm_provider and the thread override may hold a registry ALIAS
    # (claude -> anthropic); a casefold-only compare read the provider as
    # idle, dropped the "In use as" row, and parked the scope radio on
    # "thread" instead of the current "global" (review finding F1).
    class AliasedApi(FakeCommandApi):
        async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
            settings = await super().get_settings(user_id=user_id)
            settings["llm_provider"] = "claude"
            return settings

    api = AliasedApi()

    result = _run_command(api, "/provider anthropic")

    assert "the global default" in result.markdown
    form = (result.data or {}).get("form")
    scope = {
        option["id"]: option
        for option in form["tabs"][0]["fields"][0]["options"]
    }
    assert scope["global"]["current"] is True

    # Same for a thread override stored as an alias.
    plain = FakeCommandApi()
    plain.thread_config["llm_config"] = {"provider": "claude"}
    threaded = _run_command(plain, "/provider anthropic")
    assert "this thread's override" in threaded.markdown
    thread_scope = {
        option["id"]: option
        for option in (threaded.data or {})["form"]["tabs"][0]["fields"][0]["options"]
    }
    assert thread_scope["thread"]["current"] is True

    # The bare card shares the fix: an aliased active provider keeps its
    # real credential status instead of "not managed by /provider".
    bare_api = AliasedApi()
    bare_api.env_set_keys = {"anthropic_api_key"}
    bare = _run_command(bare_api, "/provider")
    assert "authenticated" in bare.markdown
    assert "not managed by /provider" not in bare.markdown


def test_provider_action_step_blocked_surface_drops_secret_flows() -> None:
    # provider setup / cliproxy carry blocked_surfaces for chat platforms
    # (typed keys persist in platform history); the action step's offers
    # derive from the registry flags, so a telegram admin is not invited
    # to run commands execute() will refuse.
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="telegram",
                is_admin=True,
                supports_forms=True,
            ),
            "/provider anthropic",
            api=api,
        )
    )

    assert result.success is True
    assert "/provider setup" not in result.markdown
    assert "/provider cliproxy" not in result.markdown
    assert "/provider test anthropic" in result.markdown
    form = (result.data or {}).get("form")
    assert [tab["label"] for tab in form["tabs"]] == ["Use", "Test"]


def test_provider_action_step_act_line_matches_available_scopes() -> None:
    # Non-admin without a thread: neither switch scope applies, no form,
    # and the markdown must not advertise a switch the gate refuses.
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id=None,
                actor="user",
                surface="cli",
                is_admin=False,
            ),
            "/provider groq",
            api=api,
        )
    )

    assert result.success is True
    assert not (result.data or {}).get("form")
    assert "Act:" not in result.markdown
    assert "/provider switch" not in result.markdown

    # Non-admin WITH a thread: the act line names the one usable scope.
    threaded = _run_command(api, "/provider groq", is_admin=False)
    assert "/provider switch groq thread" in threaded.markdown
    assert "[global|thread]" not in threaded.markdown


def test_provider_list_groups_by_tier_without_secrets() -> None:
    api = FakeCommandApi()
    api.env_set_keys = {"anthropic_api_key"}

    result = _run_command(api, "/provider list")

    assert result.success is True
    assert "[NATIVE]" in result.markdown
    assert "anthropic" in result.markdown
    assert "authenticated" in result.markdown
    assert "secret" not in result.markdown.lower()


def test_provider_list_default_view_withholds_the_unverified_tier() -> None:
    """The registry's 100+ unverified specs are opt-in, not the default.

    The full table was long enough to be cut mid-row by the output
    budget, which reads as a complete answer. The default view keeps the
    curated tiers and says how many rows it withheld.
    """
    api = FakeCommandApi()

    default = _run_command(api, "/provider list")

    assert default.success is True
    assert "[NATIVE]" in default.markdown
    assert "[GATEWAY]" in default.markdown
    # `deepinfra` is unverified and inactive: withheld from the default.
    assert "deepinfra" not in default.markdown
    assert "/provider list --all" in default.markdown
    assert "more unverified providers" in default.markdown


def test_provider_list_all_and_tier_filters_reach_the_full_registry() -> None:
    api = FakeCommandApi()

    everything = _run_command(api, "/provider list --all")
    assert everything.success is True
    assert "deepinfra" in everything.markdown
    assert "[NATIVE]" in everything.markdown
    # Nothing was withheld, so the footer must not claim otherwise.
    assert "more unverified providers" not in everything.markdown

    unverified = _run_command(api, "/provider list --tier unverified")
    assert unverified.success is True
    assert "deepinfra" in unverified.markdown
    assert "[NATIVE]" not in unverified.markdown

    native = _run_command(api, "/provider list --tier native")
    assert native.success is True
    assert "[NATIVE]" in native.markdown
    assert "[GATEWAY]" not in native.markdown
    assert "deepinfra" not in native.markdown
    # An explicit --tier states the caller's scope, so the default view's
    # "N more unverified" footer would just be counting what they excluded.
    assert "more unverified providers" not in native.markdown

    bogus = _run_command(api, "/provider list --tier nonsense")
    assert bogus.success is False
    assert "nonsense" in bogus.markdown


def test_provider_list_always_shows_the_active_provider_whatever_its_tier() -> None:
    """The one row that can never be withheld is the one in use."""
    api = FakeCommandApi()
    api.llm_provider = "deepinfra"

    default = _run_command(api, "/provider list")

    assert default.success is True
    assert "deepinfra" in default.markdown


def test_provider_list_carries_the_cliproxy_catalog() -> None:
    """CLIProxy targets are a separate catalog and used to be invisible.

    They appeared only inside form payloads, so every non-form surface
    (and every user who typed the command rather than clicking) was told
    the registry list was the whole story.
    """
    api = FakeCommandApi()

    result = _run_command(api, "/provider list")

    assert result.success is True
    assert "[CLIPROXY]" in result.markdown
    # Inside the CLIProxy SECTION, not merely somewhere in the markdown:
    # claude, kimi and grok are also registry-provider text.
    section = result.markdown.split("[CLIPROXY]", 1)[1].split("[NATIVE]", 1)[0]
    for target in ("claude", "codex", "gemini-cli", "antigravity", "kimi", "grok"):
        assert target in section, target
    assert "/provider cliproxy" in result.markdown

    only = _run_command(api, "/provider list --tier cliproxy")
    assert "[CLIPROXY]" in only.markdown
    assert "[NATIVE]" not in only.markdown


def test_provider_list_cliproxy_tier_explains_itself_when_it_is_withheld() -> None:
    """A filtered view whose only rows are hidden must not answer blank.

    /provider cliproxy is admin-only and chat-blocked, so its section is
    dropped for those callers. `--tier cliproxy` then had nothing left to
    render and returned a bare table header, which reads as "there are
    none" rather than "you cannot see these".
    """
    api = FakeCommandApi()

    non_admin = _run_command(api, "/provider list --tier cliproxy", is_admin=False)
    assert non_admin.success is False
    assert "not available to you" in non_admin.markdown
    assert "/provider list" in non_admin.markdown

    chat = _run_command(api, "/provider list --tier cliproxy", surface="discord")
    assert chat.success is False
    assert "not available to you" in chat.markdown


def test_provider_list_hides_cliproxy_from_callers_it_would_refuse() -> None:
    """Cosmetic gating only: the refusal itself stays at the dispatch gate.

    /provider cliproxy is admin-only and blocked on chat platforms, so
    advertising it to a non-admin or in Discord would be an offer the
    gate refuses.
    """
    api = FakeCommandApi()

    non_admin = _run_command(api, "/provider list", is_admin=False)
    assert non_admin.success is True
    assert "[CLIPROXY]" not in non_admin.markdown
    assert "[NATIVE]" in non_admin.markdown

    chat = _run_command(api, "/provider list", surface="discord")
    assert chat.success is True
    assert "[CLIPROXY]" not in chat.markdown


def test_provider_list_tags_an_active_route_that_runs_through_cliproxy() -> None:
    """An applied CLIProxy route persists as a plain provider + base URL.

    Without the tag the table shows a subscription as an ordinary
    direct-API credential, which is how a Claude Max route could sit in
    the list looking like a plain Anthropic key.
    """
    direct = FakeCommandApi()
    assert "via CLIProxy" not in _run_command(direct, "/provider list").markdown

    proxied = FakeCommandApi()
    proxied.llm_base_url = "http://cli-proxy-api:8317/v1"

    result = _run_command(proxied, "/provider list")

    assert result.success is True
    assert "via CLIProxy" in result.markdown
    # On the row that is actually active, not sprayed across the table.
    active_rows = [
        line
        for line in result.markdown.splitlines()
        if "via CLIProxy" in line and " yes " in line
    ]
    assert len(active_rows) == 1
    assert "openai" in active_rows[0]

    card = _run_command(proxied, "/provider")
    assert "via CLIProxy" in card.markdown


def test_provider_card_offers_cliproxy_only_where_it_is_usable() -> None:
    api = FakeCommandApi()

    assert "cliproxy" in _run_command(api, "/provider").markdown
    assert "cliproxy" not in _run_command(api, "/provider", is_admin=False).markdown


def test_provider_cliproxy_targets_dispatch_from_the_provider_root() -> None:
    """`/provider gemini-cli` used to be told the name did not exist.

    gemini-cli is a valid CLIProxy target; the handler fuzzy-matched the
    token against /provider's subcommands only and never consulted the
    catalog it imports four lines later.
    """
    from nymeria.cliproxy.catalog import get_cliproxy_provider

    api = FakeCommandApi()

    for target in ("gemini-cli", "codex", "antigravity"):
        result = _run_command(api, f"/provider {target}")
        assert "Unknown provider" not in result.markdown, target
        # The CLIProxy target step for THAT target, identified by its
        # own brief plus the catalog label the step renders.
        assert "Routes as" in result.markdown, target
        spec = get_cliproxy_provider(target)
        assert spec is not None
        assert spec.label in result.markdown, target


def test_provider_registry_ids_keep_winning_over_cliproxy_targets() -> None:
    """claude/kimi/grok are registry aliases first.

    Their ungated, read-only provider card must not be replaced by an
    admin-gated OAuth step; the card already offers CLIProxy as a tab.
    """
    api = FakeCommandApi()

    for token, expected in (
        ("claude", "Anthropic"),
        ("kimi", "Moonshot"),
        ("grok", "xAI"),
    ):
        result = _run_command(api, f"/provider {token}")
        assert result.success is True, token
        assert expected in result.markdown, token
        assert "Routes as" not in result.markdown, token


def test_provider_cliproxy_target_spellings_inherit_the_gates() -> None:
    """An injected alias resolves to the target's definition, so it
    cannot widen access: the same admin, agent and chat-surface refusals
    apply to `/provider codex` as to `/provider cliproxy codex`."""
    api = FakeCommandApi()

    non_admin = _run_command(api, "/provider codex", is_admin=False)
    assert non_admin.success is False
    assert "admin" in non_admin.markdown.lower()

    chat = _run_command(api, "/provider codex", surface="discord")
    assert chat.success is False
    assert "message history" in chat.markdown


def test_unknown_provider_error_points_at_both_catalogs() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider switch definitely-not-real")

    assert result.success is False
    assert "/provider list" in result.markdown
    assert "/provider cliproxy" in result.markdown

    # Not advertised to a caller the CLIProxy command would refuse.
    non_admin = _run_command(api, "/provider definitely-not-real", is_admin=False)
    assert non_admin.success is False
    assert "/provider cliproxy" not in non_admin.markdown


def test_provider_set_maps_credential_fields_to_settings() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider set openai api_key=sk-new")

    assert result.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "openai_api_key": "sk-new"},
    ) in api.calls

    unknown = _run_command(api, "/provider set bogus api_key=x")
    assert unknown.success is False
    assert "Unknown provider for /provider set" in unknown.markdown

    # A registered provider without credential settings fields is refused by
    # set (which stays trio-only), pointing at the virtual llm_api_key slot,
    # which routes the key to the provider's declared env var (a bare
    # /env set GROQ_API_KEY writes the var but configures no provider trio).
    unmanaged = _run_command(api, "/provider set groq api_key=x")
    assert unmanaged.success is False
    assert "/env set llm_api_key" in unmanaged.markdown

    bad_field = _run_command(api, "/provider set openai token=x")
    assert bad_field.success is False
    assert "Allowed fields" in bad_field.markdown
    # Only the first, valid call reached update_settings.
    assert len([call for call in api.calls if call[0] == "update_settings"]) == 1


def test_provider_set_requires_admin() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider set openai api_key=sk-new", is_admin=False)

    assert result.success is False
    assert "admin" in result.markdown.lower()
    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_provider_switch_warns_without_server_credential() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider switch anthropic")

    assert result.success is True
    assert "Switched provider to Anthropic" in result.markdown
    assert "no server credential" in result.markdown
    # Honest scope note: switch changes only llm_provider, and says so.
    assert "model (gpt-test) stays unchanged" in result.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_provider": "anthropic"},
    ) in api.calls


def test_provider_switch_accepts_any_registered_provider() -> None:
    """switch is registry-wide (not the /provider set trio); unmanaged
    providers get a generic env-var hint instead of credential status."""
    api = FakeCommandApi()

    result = _run_command(api, "/provider switch groq")

    assert result.success is True
    assert "Switched provider to Groq" in result.markdown
    assert "GROQ_API_KEY" in result.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_provider": "groq"},
    ) in api.calls

    unknown = _run_command(api, "/provider switch bogus")
    assert unknown.success is False
    assert "Unknown provider" in unknown.markdown
    assert "/provider list" in unknown.markdown


def test_provider_switch_thread_scope_writes_the_thread_override() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider switch anthropic thread")

    assert result.success is True
    assert "Provider for this thread set to Anthropic" in result.markdown
    # Same up-front credential honesty as the global scope: a thread
    # switched to an unconfigured provider fails at turn time.
    assert "no server credential" in result.markdown
    # The thread inherits the global model; say it stays put.
    assert "effective model (gpt-test) stays" in result.markdown
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "llm_config": {"provider": "anthropic"}},
    ) in api.calls
    # The global provider is untouched.
    assert not [
        call
        for call in api.calls
        if call[0] == "update_settings" and "llm_provider" in call[2]
    ]


def test_provider_switch_hands_off_to_model_pick() -> None:
    """The switch is half a route: both scopes end with an explicit model
    next step (B15), and a form-capable caller gets the picker resolved
    for the provider JUST switched to, not the settings default (B16)."""

    class _SwitchModelsApi(FakeCommandApi):
        def __init__(self) -> None:
            super().__init__()
            self.model_list_providers: list[str | None] = []

        async def list_available_models(
            self,
            provider: str | None = None,
            user_id: str | None = None,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
        ) -> list[dict[str, Any]]:
            self.model_list_providers.append(provider)
            return [
                {"id": "claude-opus-4-7", "owned_by": "anthropic"},
                {"id": "claude-haiku-4-5", "owned_by": "anthropic"},
            ]

    api = _SwitchModelsApi()
    # A CLIProxy-shaped base URL so the picker metas carry attribution
    # (the gate suppresses owned_by on direct providers).
    api.llm_base_url = "http://localhost:8318/v1"
    result = _run_command(api, "/provider switch anthropic thread")

    assert result.success is True
    assert (
        "Next: pick this thread's model: /model <name> thread"
        in result.markdown
    )
    assert api.model_list_providers == ["anthropic"]
    form = (result.data or {}).get("form")
    assert form is not None
    assert form["submit"]["command"] == "model {model} thread"
    options = form["tabs"][0]["fields"][1]["options"]
    assert [o["id"] for o in options] == [
        "claude-opus-4-7",
        "claude-haiku-4-5",
    ]
    assert options[0]["meta"] == "anthropic"

    api2 = _SwitchModelsApi()
    result2 = _run_command(api2, "/provider switch anthropic")
    assert "Next: pick the model: /model <name> global" in result2.markdown
    form2 = (result2.data or {}).get("form")
    assert form2 is not None
    assert form2["submit"]["command"] == "model {model} global"


def test_provider_switch_without_listing_keeps_text_next_step() -> None:
    """No listing endpoint: the explicit next step still lands, form-free
    (the base FakeCommandApi has no list_available_models at all)."""
    api = FakeCommandApi()

    result = _run_command(api, "/provider switch anthropic")

    assert result.success is True
    assert "Next: pick the model: /model <name> global" in result.markdown
    assert result.data is None


def test_provider_switch_thread_scope_requires_an_active_thread() -> None:
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id=None,
                actor="user",
                surface="cli",
                is_admin=True,
            ),
            "/provider switch anthropic thread",
            api=api,
        )
    )
    assert result.success is False
    assert "requires an active thread" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_provider_switch_scopes_gate_admin_per_scope() -> None:
    # Global is the admin surface; thread scope is any user's own override
    # (the /model precedent). The registry deliberately carries no
    # requires_admin so the split lives in the handler.
    api = FakeCommandApi()

    denied = _run_command(api, "/provider switch groq global", is_admin=False)
    assert denied.success is False
    assert "requires an admin user" in denied.markdown
    # The refusal offers the scope the caller CAN use.
    assert "/provider switch groq thread" in denied.markdown
    assert not [
        call
        for call in api.calls
        if call[0] == "update_settings" and "llm_provider" in call[2]
    ]

    allowed = _run_command(api, "/provider switch groq thread", is_admin=False)
    assert allowed.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "llm_config": {"provider": "groq"}},
    ) in api.calls


def test_provider_switch_agent_context_keeps_both_scopes() -> None:
    # The agent runs commands with is_admin=None (tools/slash_command.py
    # builds the context without resolving the role); the gate blocks only
    # on a definite False, so the agent keeps the global switch it has
    # always had, and gains nothing stronger from the thread scope.
    api = FakeCommandApi()
    service = CommandService()

    def _agent_run(command: str):
        return run(
            service.execute(
                CommandContext(
                    user_id="alice",
                    thread_id="thread-1",
                    actor="agent",
                    surface="agent",
                    is_admin=None,
                ),
                command,
                api=api,
            )
        )

    global_result = _agent_run("/provider switch anthropic")
    assert global_result.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_provider": "anthropic"},
    ) in api.calls

    thread_result = _agent_run("/provider switch anthropic thread")
    assert thread_result.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "llm_config": {"provider": "anthropic"}},
    ) in api.calls


def test_provider_switch_rejects_bad_scope_and_extra_args() -> None:
    api = FakeCommandApi()

    # A word in the scope slot is not a scope: it is an argument the command
    # has no room for, and the dispatcher says so before the handler runs.
    bad_scope = _run_command(api, "/provider switch anthropic sideways")
    assert bad_scope.success is False
    assert "Unexpected argument `sideways`" in bad_scope.markdown

    extra = _run_command(api, "/provider switch anthropic thread now")
    assert extra.success is False
    assert "Usage: `/provider switch <provider> [global|thread]`" in extra.markdown

    assert not [
        call
        for call in api.calls
        if call[0] in ("update_thread_config",)
        or (call[0] == "update_settings" and "llm_provider" in call[2])
    ]


def test_provider_test_uses_spec_default_model_for_inactive_provider() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider test groq")

    assert result.success is True
    test_calls = [call for call in api.calls if call[0] == "test_llm_provider_config"]
    assert len(test_calls) == 1
    request = test_calls[0][1][0]
    # Not the active provider: the spec's default model is used, and the
    # active provider's base_url/api_mode are NOT leaked into the request.
    assert request == {
        "llm_provider": "groq",
        "llm_model": "llama-3.3-70b-versatile",
    }


def test_provider_test_builds_request_without_client_secret() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider test")

    assert result.success is True
    assert "provider test succeeded" in result.markdown
    test_calls = [call for call in api.calls if call[0] == "test_llm_provider_config"]
    assert len(test_calls) == 1
    request = test_calls[0][1][0]
    # The active provider keeps its configured model; the credential is
    # resolved server-side (vault -> settings -> env), never sent by the
    # command handler.
    assert request == {
        "llm_provider": "openai",
        "llm_model": "gpt-test",
        "openai_api_mode": "responses",
    }
    assert "api_key" not in request


def test_provider_test_failure_renders_backend_message() -> None:
    api = FakeCommandApi()
    api.provider_test_result = {"ok": False, "message": "401 unauthorized"}

    result = _run_command(api, "/provider test openai")

    assert result.success is False
    assert "401 unauthorized" in result.markdown


def test_provider_test_translates_cliproxy_unknown_model() -> None:
    """The proxy's raw 502 reads as a Nymeria bug; through a CLIProxy base
    URL the failure gains the what-it-means line (dogfood 2026-08-05)."""
    api = FakeCommandApi()
    api.llm_base_url = "http://localhost:8318/v1"
    api.provider_test_result = {
        "ok": False,
        "message": (
            "Provider returned HTTP 502: unknown provider for model"
            " gemini-3.5-flash"
        ),
    }

    result = _run_command(api, "/provider test openai")

    assert result.success is False
    assert "unknown provider for model gemini-3.5-flash" in result.markdown
    assert "no logged-in subscription" in result.markdown
    assert "/provider cliproxy" in result.markdown


def test_provider_test_translates_cliproxy_upstream_not_found() -> None:
    api = FakeCommandApi()
    api.llm_base_url = "http://localhost:8318/v1"
    api.provider_test_result = {
        "ok": False,
        "message": (
            "Provider returned HTTP 404: Requested entity was not found."
        ),
    }

    result = _run_command(api, "/provider test openai")

    assert result.success is False
    assert "does not serve this model id" in result.markdown


def test_provider_test_translates_cliproxy_auth_unavailable() -> None:
    """The proxy-local 503 backoff shape gains the honest self-clearing
    copy (#148 shared helper; the shape reads as logged-out otherwise)."""
    api = FakeCommandApi()
    api.llm_base_url = "http://localhost:8318/v1"
    api.provider_test_result = {
        "ok": False,
        "message": (
            "Provider returned HTTP 503: auth_unavailable: no auth available"
            " (providers=claude, model=claude-fable-5)"
        ),
    }

    result = _run_command(api, "/provider test openai")

    assert result.success is False
    assert "error backoff" in result.markdown
    assert "re-login does not help" in result.markdown


def test_provider_test_failure_untouched_off_cliproxy() -> None:
    """A direct-API failure carrying the same phrase gains no CLIProxy
    editorial (the hint keys on the runtime's own URL predicate)."""
    api = FakeCommandApi()
    api.llm_base_url = "https://api.example.com/v1"
    api.provider_test_result = {
        "ok": False,
        "message": "unknown provider for model gpt-x",
    }

    result = _run_command(api, "/provider test openai")

    assert result.success is False
    assert "unknown provider for model gpt-x" in result.markdown
    assert "no logged-in subscription" not in result.markdown


def test_default_execution_uses_current_agent_backend_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nymeria.core.agent as agent_module

    fake_agent = _FakeAgent()
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)
    monkeypatch.setattr(
        CommandHttpClient,
        "from_service_token",
        classmethod(lambda cls: pytest.fail("HTTP loopback should not be used")),
    )

    result = run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="api",
            ),
            "/thread",
        )
    )

    assert result.success is True
    assert "thread id: thread-1" in result.markdown
    assert fake_agent.accounts_repo.claims == [("thread-1", "alice")]


def test_thread_required_admin_required_and_group_errors_are_metadata_driven() -> None:
    service = CommandService()
    api = FakeCommandApi()

    no_thread = run(
        service.execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/tools enabled",
            api=api,
        )
    )
    assert no_thread.success is False
    assert no_thread.level == "error"
    assert "requires an active thread" in no_thread.markdown

    non_admin = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="desktop",
                is_admin=False,
            ),
            "/env get perplexity_api_key",
            api=api,
        )
    )
    assert non_admin.success is False
    assert "requires an admin user" in non_admin.markdown

    # /restart is the one family the canon keeps root-less on purpose (a bare
    # /restart must never have a default action), so it is what a group root
    # with no bare command still looks like.
    group = run(
        service.execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/restart",
            api=api,
        )
    )
    assert group.success is False
    assert "`/restart` requires a subcommand" in group.markdown
    assert "api" in group.markdown

    unknown_subcommand = run(
        service.execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/restart nope",
            api=api,
        )
    )
    assert unknown_subcommand.success is False
    assert "Unknown subcommand `nope`" in unknown_subcommand.markdown

    assert api.calls == []


def test_help_is_generated_from_canonical_metadata_without_group_duplicates() -> None:
    service = CommandService()

    result = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="desktop",
                is_admin=False,
            ),
            "/help all",
            api=FakeCommandApi(),
        )
    )

    assert result.success is True
    assert result.markdown.count("| `/tools list` |") == 1
    # A family root is a real command now (style-guide rule 2), so it earns a
    # row; the admin-only verbs still do not, for a non-admin.
    assert result.markdown.count("| `/tools` |") == 1
    assert "| `/env get` |" not in result.markdown
    assert "| `/settings` |" in result.markdown


def test_compact_is_listed_but_not_executed_by_command_service() -> None:
    service = CommandService()
    compact = next(
        cmd
        for cmd in service.list_commands(actor="user", surface="desktop", is_admin=True)
        if cmd.id == "compact"
    )

    result = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="desktop",
                is_admin=True,
            ),
            "/compact",
            api=FakeCommandApi(),
        )
    )

    assert compact.execution_kind == "chat_stream"
    assert result.success is False
    assert "handled outside the command service" in result.markdown
    # Structural marker for generic bot passthroughs: they re-route
    # chat_stream commands into the chat path based on this, not on
    # matching the error string.
    assert result.data == {"execution_kind": "chat_stream"}


def test_prune_is_registered_as_executable_command() -> None:
    service = CommandService()
    prune = next(
        cmd
        for cmd in service.list_commands(actor="user", surface="desktop", is_admin=True)
        if cmd.id == "prune"
    )
    assert prune.execution_kind == "command"
    assert prune.agent_allowed is False
    assert prune.requires_thread is True
    assert prune.mutates_state is True

    agent_visible = {
        cmd.name
        for cmd in service.list_commands(actor="agent", surface="agent", is_admin=True)
    }
    assert "prune" not in agent_visible


def test_prune_dispatches_to_backend_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nymeria.core.agent as agent_module

    fake_agent = _FakeAgent()

    async def fake_prune_now(
        thread_id: str, user_id: str, *, mode: str = "full"
    ) -> dict[str, Any]:
        assert mode == "full"  # default when /prune has no mode arg
        return {
            "success": True,
            "pruned_count": 2,
            "skipped_already_pruned": 0,
            "skipped_too_short": 1,
            "chars_saved": 5000,
        }

    fake_agent.prune_now = fake_prune_now  # type: ignore[attr-defined]
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)

    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )
    result = run(CommandService().execute(ctx, "/prune"))
    assert result.success is True
    assert "Pruned 2" in result.markdown
    assert "5,000" in result.markdown


def test_stop_aborts_active_thread_and_reports_idle_when_no_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nymeria.core.agent as agent_module

    fake_agent = _FakeAgent()
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)

    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    idle = run(CommandService().execute(ctx, "/stop"))
    assert idle.success is True
    assert "idle" in idle.markdown.lower()
    assert fake_agent.aborted == []

    fake_agent._thread_locks.lock_info = {"holder": "astream", "held_seconds": 4.2}
    active = run(CommandService().execute(ctx, "/stop"))
    assert active.success is True
    assert "astream" in active.markdown
    assert "iteration boundary" in active.markdown
    assert fake_agent.aborted == ["thread-1"]


def test_stop_echoes_restored_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nymeria.core.agent as agent_module
    from nymeria.core.pending_prompt_queue import make_pending_prompt

    fake_agent = _FakeAgent()
    fake_agent._thread_locks.lock_info = {"holder": "astream", "held_seconds": 4.2}
    fake_agent.restored_to_return = [
        make_pending_prompt(
            message="follow-up question",
            source="user",
            source_id=None,
            source_label="Alice",
            user_id="alice",
            is_autonomous=False,
            fanout_mailbox=None,
            consumer_loop=None,
        )
    ]
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)

    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )
    result = run(CommandService().execute(ctx, "/stop"))
    assert result.success is True
    assert "This queued message was NOT sent:" in result.markdown
    assert "> follow-up question" in result.markdown


def test_stop_is_blocked_for_agent_actor() -> None:
    service = CommandService()
    result = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="agent",
                surface="agent",
                is_admin=True,
            ),
            "/stop",
            api=FakeCommandApi(),
        )
    )
    assert result.success is False
    assert "disabled for the agent" in result.markdown


def test_clear_clears_history_and_preserves_notepad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nymeria.core.agent as agent_module

    fake_agent = _FakeAgent()
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)

    # Monkeypatch delete_thread_checkpoints to a no-op
    import nymeria.core.checkpoint_cleanup as cleanup_mod

    deleted_checkpoints: list[str] = []

    def fake_delete(settings, thread_id):
        deleted_checkpoints.append(thread_id)

    monkeypatch.setattr(cleanup_mod, "delete_thread_checkpoints", fake_delete)

    ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )

    result = run(CommandService().execute(ctx, "/clear"))
    assert result.success is True
    assert "cleared" in result.markdown.lower()
    assert "notepad" in result.markdown.lower()
    assert fake_agent.thread_metadata_manager.deleted == [("alice", "thread-1")]
    assert deleted_checkpoints == ["thread-1"]


def test_clear_is_blocked_for_agent_actor() -> None:
    service = CommandService()
    result = run(
        service.execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="agent",
                surface="agent",
                is_admin=True,
            ),
            "/clear",
            api=FakeCommandApi(),
        )
    )
    assert result.success is False
    assert "disabled for the agent" in result.markdown


def test_restart_api_requires_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nymeria.core.agent as agent_module

    fake_agent = _FakeAgent()
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: fake_agent)

    # Non-admin should be blocked at the dispatch level
    non_admin_ctx = CommandContext(
        user_id="alice",
        thread_id=None,
        actor="user",
        surface="cli",
        is_admin=False,
    )
    result = run(CommandService().execute(non_admin_ctx, "/restart api"))
    assert result.success is False
    assert "requires an admin" in result.markdown.lower()

    # Admin should succeed — mock the restart helper to prevent actual restart
    import nymeria.api.routers.system as system_mod

    restarted: list[bool] = []

    def fake_restart(agent, settings):
        restarted.append(True)

    monkeypatch.setattr(system_mod, "restart_api_process", fake_restart)

    # Use a fake agent whose accounts_repo returns admin role
    admin_agent = _FakeAgent()
    admin_agent.accounts_repo = _FakeAccountsRepo(default_role="admin")
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: admin_agent)

    admin_ctx = CommandContext(
        user_id="alice",
        thread_id=None,
        actor="user",
        surface="cli",
        is_admin=True,
    )
    result = run(CommandService().execute(admin_ctx, "/restart api"))
    assert result.success is True
    assert "restarting" in result.markdown.lower()
    assert restarted == [True]


def test_restart_api_flat_alias_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/restart_api dispatches like /restart api (family flat-alias convention).

    Before the alias existed, the dispatcher answered /restart_api with a
    did-you-mean pointing at bare /restart, which bot-local handlers
    intercept on bot surfaces (restarting the BOT, not the API) and which
    elsewhere needs a second hop through the subcommand listing (2026-08-10
    outage, bug 3).
    """
    import nymeria.api.routers.system as system_mod
    import nymeria.core.agent as agent_module

    restarted: list[bool] = []
    monkeypatch.setattr(
        system_mod, "restart_api_process", lambda agent, settings: restarted.append(True)
    )
    admin_agent = _FakeAgent()
    admin_agent.accounts_repo = _FakeAccountsRepo(default_role="admin")
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: admin_agent)

    admin_ctx = CommandContext(
        user_id="alice",
        thread_id=None,
        actor="user",
        surface="cli",
        is_admin=True,
    )
    result = run(CommandService().execute(admin_ctx, "/restart_api"))
    assert result.success is True
    assert restarted == [True]


def test_env_set_resolves_env_var_spellings() -> None:
    """/env set speaks env-var names: exact divergent names reverse-map to
    their field (the S3 family), everything else case-folds."""
    api = FakeCommandApi()
    result = run(
        CommandService().execute(_ctx(), "/env set AWS_ACCESS_KEY_ID AKIA-x", api=api)
    )
    assert result.success is True
    sent = [c for c in api.calls if c[0] == "update_settings"][-1][2]
    assert sent.get("s3_access_key_id") == "AKIA-x"

    result = run(
        CommandService().execute(
            _ctx(), "/env set NYMERIA_PUBLIC_URL https://x.example", api=api
        )
    )
    assert result.success is True
    sent = [c for c in api.calls if c[0] == "update_settings"][-1][2]
    assert sent.get("nymeria_public_url") == "https://x.example"


class _UnappliedCommandApi(FakeCommandApi):
    """Backend answering like the applier when it filters the whole update."""

    async def update_settings(self, *, user_id=None, **kwargs):
        await super().update_settings(user_id=user_id, **kwargs)
        return {"updated": [], "restart_required": False, "warnings": []}


def test_env_set_unapplied_key_reports_error() -> None:
    """A write the applier dropped must not be reported as set.

    The applier filters an explicit None outside _CLEARABLE_NULL_SETTINGS
    ("No updates provided"); echoing "set to None" would be the silent-drop
    lie one layer up.
    """
    result = run(
        CommandService().execute(
            _ctx(), "/env set REDIS_URL none", api=_UnappliedCommandApi()
        )
    )
    assert result.success is False
    assert "was not applied" in result.markdown
    assert "does not support clearing" in result.markdown


class _WarningCommandApi(FakeCommandApi):
    """Backend echoing an applier warning alongside a successful write."""

    async def update_settings(self, *, user_id=None, **kwargs):
        result = await super().update_settings(user_id=user_id, **kwargs)
        result["warnings"] = [
            "This looks like an Outlook Safe Links wrapper, not the real site URL."
        ]
        return result


def test_env_set_renders_server_warnings_as_warning_level() -> None:
    """Applier warnings ride the typed warning level, not a success body."""
    result = run(
        CommandService().execute(
            _ctx(),
            "/env set NYMERIA_PUBLIC_URL https://wrapped.example",
            api=_WarningCommandApi(),
        )
    )
    assert result.success is True
    assert result.level == "warning"
    assert "Safe Links" in result.markdown
    assert "nymeria_public_url set to" in result.markdown


class _RejectingCommandApi(FakeCommandApi):
    """Backend raising the applier's 400 in the httpx shape both shapes use."""

    async def update_settings(self, *, user_id=None, **kwargs):
        from nymeria.core.command_service import _raise_http_status

        _raise_http_status(
            400, "Unknown setting: bogus_key. Did you mean bog_key (BOG_KEY)?"
        )


def test_env_set_unknown_key_renders_applier_error() -> None:
    """The applier's 400 detail reaches the user through the dispatcher."""
    result = run(
        CommandService().execute(
            _ctx(), "/env set BOGUS_KEY x", api=_RejectingCommandApi()
        )
    )
    assert result.success is False
    assert "Unknown setting: bogus_key" in result.markdown


def test_unknown_root_suggestion_expands_subcommand_only_family() -> None:
    """A suggested subcommand-only root expands to its full path when unique.

    Suggesting bare `/restart` costs the user another hop (and on bot
    surfaces a bot-local handler intercepts it); the hint should name the
    spelling that dispatches directly.
    """
    result = run(CommandService().execute(_ctx(), "/restartapi", api=FakeCommandApi()))
    assert result.success is False
    assert "Did you mean `/restart api`?" in result.markdown


def _client(
    *,
    user: AuthenticatedUser | None = None,
    api: FakeCommandApi | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    app = FastAPI()
    resolved_user = user or AuthenticatedUser(
        id="alice",
        email="alice@example.test",
        display_name="Alice",
        role="user",
    )

    async def verify_api_key() -> AuthenticatedUser:
        return resolved_user

    if api is not None:
        monkeypatch.setattr(
            CommandBackendClient,
            "from_context",
            classmethod(lambda cls, ctx, **kwargs: api),
        )

    app.include_router(create_commands_router(verify_api_key))
    return TestClient(app)


def test_commands_api_lists_actor_surface_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch=monkeypatch)

    response = client.get(
        "/commands",
        params={"actor": "user", "surface": "desktop"},
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200
    data = response.json()
    tools_list = next(item for item in data if item["id"] == "tools.list")
    assert tools_list["path"] == ["tools", "list"]
    assert tools_list["execution_kind"] == "command"
    assert tools_list["requires_thread"] is False
    assert all(item["id"] != "env.get" for item in data)


def test_commands_api_execute_returns_markdown_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeCommandApi()
    client = _client(
        user=AuthenticatedUser(
            id="admin",
            email="admin@example.test",
            display_name="Admin",
            role="admin",
        ),
        api=api,
        monkeypatch=monkeypatch,
    )

    response = client.post(
        "/commands/execute",
        json={
            "command": "/tools_list core",
            "thread_id": "thread-1",
            "actor": "user",
            "surface": "telegram",
        },
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "markdown": "### Core Tools: 1 tools\n\nbash_execute: Execute shell commands",
        "command": "tools list",
        "level": "info",
        "data": None,
    }
    assert api.closed is False


def test_commands_api_options_endpoint_resolves_a_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _ModelCatalogCommandApi()
    client = _client(api=api, monkeypatch=monkeypatch)

    response = client.get(
        "/commands/options/models",
        params={"thread_id": "thread-1"},
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [option["id"] for option in body] == ["gpt-test", "gpt-next"]
    assert all(
        set(option) == {"id", "label", "meta", "description", "current"}
        for option in body
    )


def test_commands_api_options_endpoint_filters_and_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """q/limit narrow server-side so per-keystroke callers (Discord's 3s
    autocomplete deadline) never pull whole catalogs."""
    api = _ModelCatalogCommandApi()
    client = _client(api=api, monkeypatch=monkeypatch)
    headers = {"Authorization": "Bearer token"}

    filtered = client.get(
        "/commands/options/models", params={"q": "next"}, headers=headers
    )
    assert filtered.status_code == 200
    assert [option["id"] for option in filtered.json()] == ["gpt-next"]

    capped = client.get(
        "/commands/options/models", params={"limit": 1}, headers=headers
    )
    assert capped.status_code == 200
    assert [option["id"] for option in capped.json()] == ["gpt-test"]


def test_commands_api_options_endpoint_404_on_unknown_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(api=FakeCommandApi(), monkeypatch=monkeypatch)

    response = client.get(
        "/commands/options/nonesuch",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 404


def test_commands_api_supports_forms_flag_gates_form_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request-level capability flag reaches the dispatcher: the same
    /think call returns its picker only when the caller declares support."""
    api = FakeCommandApi()
    client = _client(api=api, monkeypatch=monkeypatch)

    base = {"command": "/think", "thread_id": "thread-1", "surface": "cli"}
    headers = {"Authorization": "Bearer token"}

    without = client.post("/commands/execute", json=base, headers=headers)
    assert without.status_code == 200
    assert without.json()["success"] is True
    assert without.json()["data"] is None

    with_flag = client.post(
        "/commands/execute",
        json={**base, "supports_forms": True},
        headers=headers,
    )
    assert with_flag.status_code == 200
    body = with_flag.json()
    assert body["success"] is True
    assert body["data"] is not None and "form" in body["data"]


# ── Slice 03 F10: write-path excepts forward HTTPException, propagate real bugs ──
#
# CommandBackendClient.add_todo/complete_todo/delete_todo wrap router helpers
# that raise only FastAPI HTTPException. Narrowed from a broad `except Exception`
# so an intended 400/409 is still rendered cleanly (re-raised as
# httpx.HTTPStatusError, which the dispatcher turns into a user message) while a
# genuine bug or storage fault propagates to the dispatcher's logger.exception
# handler instead of being masked as a misleading 400/409.


def _todo_backend(api_client_builder, tmp_path):
    settings = api_client_builder.settings(tmp_path)
    user = _CommandBackendUser(id="owner", role="admin")
    backend = CommandBackendClient(
        SimpleNamespace(), user=user, settings_fn=lambda: settings
    )
    return backend, settings


def _seed_todo(settings, user_id: str = "owner") -> str:
    todo_manager = TodoManager(settings.data_dir)
    with todo_manager.atomic_update(user_id) as todo_list:
        item = todo_list.add_item(
            "task",
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=5),
            thread_id="t1",
            created_by="user",
        )
    assert item is not None
    return item.id


def test_add_todo_forwards_httpexception_status(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, _ = _todo_backend(api_client_builder, tmp_path)

    def _bad_schedule(_value):
        raise HTTPException(status_code=400, detail="bad schedule")

    monkeypatch.setattr(todos_router, "_parse_scheduled_for", _bad_schedule)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(backend.add_todo("owner", "task", scheduled_for="nonsense"))
    assert excinfo.value.response.status_code == 400
    assert excinfo.value.response.json()["detail"] == "bad schedule"


def test_add_todo_propagates_unexpected_error(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, _ = _todo_backend(api_client_builder, tmp_path)

    def _parser_bug(_value):
        raise RuntimeError("parser blew up")

    monkeypatch.setattr(todos_router, "_parse_scheduled_for", _parser_bug)

    # Previously masked as httpx.HTTPStatusError(400); now surfaces as the real bug.
    with pytest.raises(RuntimeError, match="parser blew up"):
        run(backend.add_todo("owner", "task", scheduled_for="1d"))


def test_add_todo_out_of_range_schedule_returns_clean_400(
    api_client_builder, tmp_path
) -> None:
    # End-to-end (no monkeypatch): an out-of-range duration used to raise
    # OverflowError out of _parse_scheduled_for, which the old broad except
    # quietly turned into a 400 and the narrowed except would have leaked as a
    # logged 500. The source fix in parse_scheduled_time keeps it a clean 400.
    backend, _ = _todo_backend(api_client_builder, tmp_path)
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(backend.add_todo("owner", "task", scheduled_for="999999999999d"))
    assert excinfo.value.response.status_code == 400
    # The clean invalid-format message, not a leaked OverflowError string.
    assert "Invalid scheduled_for format" in excinfo.value.response.json()["detail"]


def test_complete_todo_forwards_httpexception_status(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    def _executing(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="currently executing")

    monkeypatch.setattr(todos_router, "_raise_if_todo_executing", _executing)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(backend.complete_todo("owner", todo_id))
    assert excinfo.value.response.status_code == 409
    assert excinfo.value.response.json()["detail"] == "currently executing"


def test_complete_todo_propagates_unexpected_error(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    def _store_fault(*_args, **_kwargs):
        raise RuntimeError("schedule db unavailable")

    monkeypatch.setattr(todos_router, "_raise_if_todo_executing", _store_fault)

    with pytest.raises(RuntimeError, match="schedule db unavailable"):
        run(backend.complete_todo("owner", todo_id))


def test_update_todo_patches_and_clears_fields_in_the_store(
    api_client_builder, tmp_path
) -> None:
    # The in-process twin of PATCH /todos/{id} (#143): same field set, same
    # validation, observable through the store it writes.
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    updated = run(
        backend.update_todo(
            "owner",
            todo_id,
            task="renamed task",
            status="in_progress",
            notes="say less",
            recurrence="daily",
        )
    )
    assert updated["task"] == "renamed task"
    assert updated["status"] == "in_progress"
    assert updated["notes"] == "say less"
    assert updated["recurrence"] == "1d"  # canonicalized

    # A thread rebind in the SAME patch as clear_schedule applies (the
    # store's clear branch used to silently drop it, so the edit verb
    # claimed a change that never happened).
    cleared = run(
        backend.update_todo(
            "owner",
            todo_id,
            clear_schedule=True,
            clear_recurrence=True,
            thread_id="other-thread",
        )
    )
    assert cleared["scheduled_for"] is None
    assert cleared["recurrence"] is None
    assert cleared["thread_id"] == "other-thread"

    # The write is durable, not just the response shape.
    stored = TodoManager(settings.data_dir).get_todos("owner").get_item(todo_id)
    assert stored is not None
    assert stored.task == "renamed task"
    assert stored.scheduled_for is None
    assert stored.recurrence is None
    assert stored.thread_id == "other-thread"


def test_update_todo_rejects_a_bad_status_with_a_clean_400(
    api_client_builder, tmp_path
) -> None:
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(backend.update_todo("owner", todo_id, status="sideways"))
    assert excinfo.value.response.status_code == 400
    assert "Invalid status" in excinfo.value.response.json()["detail"]

    # The bad patch left the stored task untouched.
    stored = TodoManager(settings.data_dir).get_todos("owner").get_item(todo_id)
    assert stored is not None and stored.task == "task"


def test_update_todo_forwards_httpexception_status(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    def _executing(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="currently executing")

    monkeypatch.setattr(todos_router, "_raise_if_todo_executing", _executing)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(backend.update_todo("owner", todo_id, task="renamed"))
    assert excinfo.value.response.status_code == 409


def test_delete_todo_forwards_httpexception_status(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    def _executing(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="currently executing")

    monkeypatch.setattr(todos_router, "_raise_if_todo_executing", _executing)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(backend.delete_todo("owner", todo_id))
    assert excinfo.value.response.status_code == 409
    assert excinfo.value.response.json()["detail"] == "currently executing"


def test_delete_todo_propagates_unexpected_error(
    api_client_builder, tmp_path, monkeypatch
) -> None:
    backend, settings = _todo_backend(api_client_builder, tmp_path)
    todo_id = _seed_todo(settings)

    def _store_fault(*_args, **_kwargs):
        raise RuntimeError("schedule db unavailable")

    monkeypatch.setattr(todos_router, "_raise_if_todo_executing", _store_fault)

    with pytest.raises(RuntimeError, match="schedule db unavailable"):
        run(backend.delete_todo("owner", todo_id))


def test_default_catalog_extracted_to_registry_defaults() -> None:
    """Slice 03 F3: the built-in catalog now lives in ``core.registry_defaults``.

    Locks the extraction's fidelity. ``CommandService()`` runs
    ``register_default_commands(self)`` then ``validate_registry()``; this
    asserts the full command count plus one entry per declaration shape (alias
    normalization, danger flags, chat-stream + note, ``agent_allowed``, a
    multi-alias and a multi-word alias). A dropped or altered declaration in
    the extracted table trips this.
    """
    from nymeria.core.registry_defaults import register_default_commands

    service = CommandService()
    by_name = {cmd.name: cmd for cmd in service._commands.values()}

    # Count tripwire: update when adding or removing a built-in command.
    # (147 after the backlog #131 rename wave: thirteen commands folded away
    # into aliases of others, and seven arrived, five of them the overview
    # roots style-guide rule 2 requires. 151 after #133 added the /alias
    # family: root overview + create + delete + list. 154 after #143 ported
    # the CLI-local todo verbs: todos edit + todos schedule + todos repeat.)
    assert len(service._commands) == 154
    assert sum(cmd.executable for cmd in service._commands.values()) == 137

    help_cmd = by_name["help"]
    assert help_cmd.category == "General"
    assert help_cmd.aliases == (("h",),)

    settings_set = by_name["settings set"]
    assert (settings_set.requires_admin, settings_set.mutates_state) == (True, True)
    assert settings_set.danger_level == "dangerous"

    skill = by_name["skill"]
    assert skill.execution_kind == "chat_stream"
    assert skill.executable is False
    assert skill.requires_thread is True
    assert skill.note == "Handled by the chat stream endpoint."

    assert by_name["compact"].agent_allowed is False

    # /quick is a chat-stream command (handled by the /chat endpoint, so
    # non-executable via the command service) and gated off for agents.
    quick = by_name["quick"]
    assert quick.execution_kind == "chat_stream"
    assert quick.executable is False
    assert quick.requires_thread is True
    assert quick.agent_allowed is False
    assert quick.note == "Handled by the chat stream endpoint."

    # /thread management subtree: read verbs stay agent-allowed, mutating and
    # navigation verbs are gated off, delete matches the "dangerous" pattern,
    # and the root carries the threads/t aliases.
    assert by_name["thread"].aliases == (("threads",), ("t",))
    assert by_name["thread list"].agent_allowed is True
    assert by_name["thread switch"].agent_allowed is False
    assert by_name["thread switch"].aliases == (("thread_switch",), ("thread", "s"))
    assert by_name["thread delete"].danger_level == "dangerous"
    assert by_name["thread delete"].mutates_state is True
    assert by_name["thread delete"].aliases == (
        ("thread_delete",),
        ("thread", "del"),
        ("thread", "rm"),
    )
    assert by_name["thread branch"].requires_thread is True
    # /branch and /fork were a duplicate root registration; backlog #131
    # folded them into whole-path aliases, kept AFTER the existing
    # single-token alias so no chat-bot menu entry is renamed.
    assert by_name["thread branch"].aliases == (
        ("thread_branch",),
        ("thread", "fork"),
        ("branch",),
        ("fork",),
    )
    assert "branch" not in by_name

    assert by_name["mcp delete"].aliases == (
        ("mcp_remove",),
        ("mcp_rm",),
        ("mcp_delete",),
        ("mcp", "remove"),
        ("mcp", "rm"),
    )
    # The four tools listing commands folded into one filter. `enabled` and
    # `category` bridge as PLAIN aliases; `core`/`optional` are INJECTED
    # aliases (#133) carrying their filter token, appended AFTER the plain
    # ones so the first single-token alias (the chat-bot menu contract)
    # stays `tools_list`.
    assert by_name["tools list"].aliases == (
        ("tools_list",),
        ("tools_enabled",),
        ("tools_category",),
        ("tools", "enabled"),
        ("tools", "category"),
        ("tools_core",),
        ("tools", "core"),
        ("tools_optional",),
        ("tools", "optional"),
    )
    for gone in ("tools core", "tools optional", "tools enabled", "tools category"):
        assert gone not in by_name, gone

    # Lifecycle-hook commands: mutating subcommands are agent-gated (the agent
    # authors via the hook_config tool) and hidden from chat surfaces, while the
    # bare `/hook` read command stays available everywhere.
    assert by_name["hook"].agent_allowed is True
    assert by_name["hook"].aliases == (("hooks",),)
    hook_create = by_name["hook create"]
    assert (hook_create.agent_allowed, hook_create.mutates_state) == (False, True)
    assert "telegram" not in hook_create.surfaces
    assert by_name["hook delete"].danger_level == "dangerous"

    # Fallback family: every verb is a registered path, so longest-prefix
    # parsing dispatches "/fallback <sub>" straight to the _cmd_fallback_<sub>
    # handlers (the parent only lists), and the human-only consent ones carry
    # agent_allowed=False, enforced pre-dispatch (the agent must not resolve or
    # revert its own consent). The chain verbs stay agent-allowed, as they were
    # when the parent handler parsed them.
    assert by_name["fallback"].agent_allowed is True
    assert by_name["fallback status"].agent_allowed is True
    for sub in ("revert", "approvals", "approve", "deny"):
        entry = by_name[f"fallback {sub}"]
        assert entry.agent_allowed is False, sub
        assert entry.category == "LLM", sub
    for sub in ("list", "add", "remove", "set", "clear"):
        entry = by_name[f"fallback {sub}"]
        assert entry.agent_allowed is True, sub
        assert entry.category == "LLM", sub
    assert by_name["fallback approve"].mutates_state is True
    assert by_name["fallback deny"].mutates_state is True
    assert by_name["fallback revert"].mutates_state is True
    assert by_name["fallback remove"].aliases == (("fallback", "rm"),)
    assert service._subcommands_for_path(("fallback",)) == [
        "add", "approvals", "approve", "clear", "deny", "list", "remove",
        "revert", "set", "status",
    ]

    # The catalog registers onto whichever service instance is passed in.
    fresh = CommandService()
    fresh._commands.clear()
    fresh._path_index.clear()
    fresh._aliases.clear()
    register_default_commands(fresh)
    fresh.validate_registry()
    # Full per-definition equality (frozen dataclass value-eq), not just id set,
    # so a kwarg drift between the two construction paths would trip.
    assert fresh._commands == service._commands


def _restricted_subcommands_behind_an_alias(
    service: CommandService,
) -> list[tuple[str, tuple[str, ...]]]:
    """Every ``(alias token, restricted subcommand path)`` pair in the registry.

    Built from the live registry rather than a fixed list so a newly aliased
    family, or a new restricted subcommand under an existing one, is covered the
    day it lands instead of the day someone remembers to extend this test.
    """
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for alias_path, command_id in service._aliases.items():
        if len(alias_path) != 1:
            continue
        parent = service._commands[command_id]
        if len(parent.path) != 1:
            continue
        for path, sub_id in service._path_index.items():
            if len(path) != 2 or path[0] != parent.path[0]:
                continue
            sub = service._commands[sub_id]
            if not sub.agent_allowed or sub.requires_admin:
                pairs.append((alias_path[0], path))
    return pairs


def test_an_alias_cannot_weaken_a_subcommands_flags() -> None:
    """An alias is a synonym, so it must resolve to the same definition.

    The bug this pins: aliases are registered against whole paths, so
    ``("hooks",)`` existed while ``("hooks", "disable")`` did not. Longest-prefix
    matching therefore resolved ``/hooks disable X`` to the PARENT ``hook``
    definition, which is ``agent_allowed=True`` because listing hooks is
    harmless, and the ``agent_allowed=False`` on ``hook.disable`` was never
    consulted. The plural spelling was a complete bypass of the flag, and the
    same held for ``/t`` and ``/threads`` over ``thread delete``.

    Asserting on the resolved definition rather than on an error string keeps
    this honest: it is the definition the dispatch gate reads.
    """
    service = CommandService()
    pairs = _restricted_subcommands_behind_an_alias(service)
    assert pairs, "registry has no aliased families left, so this proves nothing"

    for alias, path in pairs:
        canonical = service._parse_for_registry(f"/{' '.join(path)} someargument")
        via_alias = service._parse_for_registry(f"/{alias} {path[1]} someargument")
        assert via_alias.definition is not None, f"/{alias} {path[1]} resolved to nothing"
        assert via_alias.definition.id == canonical.definition.id, (
            f"/{alias} {path[1]} resolves to {via_alias.definition.id}, "
            f"not {canonical.definition.id}"
        )
        # Arguments must survive the rewrite, or the fix would break the
        # commands it is protecting.
        assert via_alias.args == canonical.args == ["someargument"]


def test_an_alias_that_stands_for_a_multi_token_path_keeps_its_arguments() -> None:
    """The other arity: one raw token expanding into two canonical ones.

    ``/hook_disable X`` is a single token standing for ``("hook", "disable")``,
    so recovering ``X`` means slicing one token off the RAW input while the
    match happened at length two. Getting that back-translation wrong silently
    eats the argument.
    """
    service = CommandService()
    parsed = service._parse_for_registry("/hook_disable abc123")
    assert parsed.definition is not None
    assert parsed.definition.id == "hook.disable"
    assert parsed.args == ["abc123"]


def test_an_unregistered_subcommand_still_reaches_the_parent_handler() -> None:
    """Alias expansion must not swallow tokens the parent still has to answer.

    An unknown verb behind a family alias has to fall through to the
    single-token path so the root's strict binder can reject it with the
    family guidance. A whole-path alias, by contrast, is a true synonym: it
    resolves to the child definition and its arguments bind to that child's
    schema.
    """
    service = CommandService()

    parsed = service._parse_for_registry("/hooks frobnicate abc")
    assert parsed.definition is not None
    assert parsed.definition.id == "hook"
    assert parsed.args == ["frobnicate", "abc"]

    # `/hook detail` is a whole-path alias of `hook show`, reachable through
    # the `/hooks` plural alias too.
    for raw in ("/hook detail abc", "/hooks detail abc"):
        aliased = service._parse_for_registry(raw)
        assert aliased.definition is not None, raw
        assert aliased.definition.id == "hook.show", raw
        assert aliased.args == ["abc"], raw


def test_an_alias_prefix_cannot_hijack_a_deeper_registered_path() -> None:
    """The /hooks disable class of bug, from the other direction (#131 F6).

    ``_expand_alias_prefix`` breaks on an exact path match, but it used to
    scan only as deep as the longest ALIAS in the registry. A two-token
    alias that is a proper prefix of a three-token path therefore expanded
    before the real path could win, dispatching the alias TARGET with the
    path's tail demoted to an argument, and the deeper command's own flags
    were never consulted. The scan ceiling now includes the longest PATH,
    so the break-on-path guard always gets its chance.

    Uses runtime registrations because the built-in catalog (correctly) has
    no alias shaped like this; the hazard is one plugin away.
    """
    service = CommandService()
    service.register(
        "zzalias target",
        description="the alias target",
        category="Tests",
        aliases=("zzfam sub",),
    )
    service.register(
        "zzfam sub leaf",
        description="the deeper real command",
        category="Tests",
    )

    deep = service._parse_for_registry("/zzfam sub leaf abc")
    assert deep.definition is not None
    assert deep.definition.id == "zzfam.sub.leaf"
    assert deep.args == ["abc"]

    # The alias keeps working for its own exact spelling.
    exact = service._parse_for_registry("/zzfam sub abc")
    assert exact.definition is not None
    assert exact.definition.id == "zzalias.target"
    assert exact.args == ["abc"]


def test_an_injected_alias_carries_its_tokens_ahead_of_the_typed_tail() -> None:
    """The #133 mechanism: an injected alias contributes ARGUMENT tokens a
    plain alias structurally cannot (args are re-sliced from the raw text,
    so a substituted path alone loses anything it wanted to say). Injected
    tokens bind first; whatever the user typed after the alias follows.
    """
    service = CommandService()
    service.register(
        "zzmodel set",
        description="synthetic target",
        category="Tests",
        injected_aliases={"zzgpt5": ("zz-provider/zz-5.5",)},
    )

    parsed = service._parse_for_registry("/zzgpt5 thread")
    assert parsed.definition is not None
    assert parsed.definition.id == "zzmodel.set"
    assert parsed.args == ["zz-provider/zz-5.5", "thread"]
    assert parsed.rest == "zz-provider/zz-5.5 thread"

    # Without a typed tail the injection alone is the argument vector.
    bare = service._parse_for_registry("/zzgpt5")
    assert bare.args == ["zz-provider/zz-5.5"]
    assert bare.rest == "zz-provider/zz-5.5"


def test_an_injection_composing_into_a_deeper_path_matches_that_command() -> None:
    """Injected tokens are part of the canonical stream, so an injection may
    legally spell out a DEEPER registered path; the deeper definition (and
    therefore its own flags) is what matches and what the gates read.
    """
    service = CommandService()
    service.register(
        "zzdeep sub",
        description="the alias owner",
        category="Tests",
        injected_aliases={"zzjump": ("leaf",)},
    )
    service.register(
        "zzdeep sub leaf",
        description="the deeper real command",
        category="Tests",
    )

    parsed = service._parse_for_registry("/zzjump extra")
    assert parsed.definition is not None
    assert parsed.definition.id == "zzdeep.sub.leaf"
    assert parsed.args == ["extra"]


def test_injected_alias_tokens_must_be_single_unquoted_words() -> None:
    """The v1 restriction that keeps the args/rest composition trivially
    correct: an injected token is one shlex-safe word. Quoted prompts and
    multi-word payloads are a recorded non-goal.
    """
    service = CommandService()
    for bad in ("two words", 'quo"ted', "quo'ted", ""):
        with pytest.raises(ValueError):
            service.register(
                "zzbadinject target",
                description="rejected",
                category="Tests",
                injected_aliases={"zzbad": (bad,)},
            )
    # Injecting nothing is a plain alias wearing the wrong declaration.
    with pytest.raises(ValueError, match="injects nothing"):
        service.register(
            "zzbadinject target",
            description="rejected",
            category="Tests",
            injected_aliases={"zzbad": ()},
        )
    # Injected aliases join the ordinary collision rules: an existing
    # command path cannot become one.
    with pytest.raises(ValueError, match="conflicts with command path"):
        service.register(
            "zzbadinject target",
            description="rejected",
            category="Tests",
            injected_aliases={"tools list": ("core",)},
        )


# ---------------------------------------------------------------------------
# User-defined aliases (backlog #133)
# ---------------------------------------------------------------------------


@pytest.fixture()
def alias_repo(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the user-alias singleton at a per-test database."""
    from nymeria.core import user_aliases as ua

    repo = ua.UserAliasesRepo(tmp_path / "accounts.db")
    monkeypatch.setattr(ua, "_repo", repo)
    return repo


def _agent_ctx() -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="agent",
        surface="agent",
        is_admin=True,
    )


def test_user_alias_expands_with_values_and_typed_tail(alias_repo) -> None:
    """The #133 headline: a personal spelling carries VALUES and still
    accepts a typed tail, and it expands only for its owner."""
    service = CommandService()
    api = FakeCommandApi()

    created = run(service.execute(_ctx(), "/alias create td todos list", api=api))
    assert created.success is True, created.markdown

    aliased = run(service.execute(_ctx(), "/td all", api=api))
    assert aliased.success is True, aliased.markdown
    assert "TODOs (all): 2 items" in aliased.markdown

    # Another user's dispatch does not see alice's table.
    bob = CommandContext(
        user_id="bob", thread_id="thread-1", actor="user",
        surface="cli", is_admin=True,
    )
    other = run(service.execute(bob, "/td all", api=api))
    assert other.success is False
    assert "Unknown command" in other.markdown


def test_user_alias_loses_to_every_catalog_spelling(alias_repo) -> None:
    """Lose-to-everything at CREATE: registered roots, built-in aliases,
    and rootless family roots (`restart` has no bare command, but a user
    alias there would hijack `/restart api`: the F6 hazard, user edition).
    """
    service = CommandService()
    api = FakeCommandApi()

    for taken in ("tools", "settings_get", "restart", "aliases"):
        result = run(
            service.execute(_ctx(), f"/alias create {taken} todos list", api=api)
        )
        assert result.success is False, taken
        assert "must not shadow the catalog" in result.markdown, taken


def test_user_alias_cannot_widen_agent_access(alias_repo) -> None:
    """The gate invariant: expansion happens before the gates, so an alias
    to an agent-blocked command is refused for the agent exactly like the
    canonical spelling (the /hooks disable class of check, user edition)."""
    service = CommandService()
    api = FakeCommandApi()

    created = run(
        service.execute(_ctx(), "/alias create hd hook disable someid", api=api)
    )
    assert created.success is True, created.markdown

    via_agent = run(service.execute(_agent_ctx(), "/hd", api=api))
    assert via_agent.success is False
    assert "not available to the agent" in via_agent.markdown


def test_agent_authored_alias_is_recorded_as_such(alias_repo) -> None:
    """Dev-sanctioned agent authoring, with provenance: the listing names
    the author, and the stamp covers that field (store tests prove the
    tamper case)."""
    service = CommandService()
    api = FakeCommandApi()

    created = run(
        service.execute(_agent_ctx(), "/alias create td todos list", api=api)
    )
    assert created.success is True, created.markdown
    assert alias_repo.list_aliases("alice")[0].author_actor == "agent"

    listing = run(service.execute(_ctx(), "/alias list", api=api))
    assert "(agent-authored)" in listing.markdown


def test_user_alias_create_refuses_bad_shapes(alias_repo) -> None:
    service = CommandService()
    api = FakeCommandApi()

    bad_name = run(service.execute(_ctx(), "/alias create a/b todos list", api=api))
    assert bad_name.success is False
    assert "one word" in bad_name.markdown

    unknown = run(service.execute(_ctx(), "/alias create zz frobnicate", api=api))
    assert unknown.success is False
    assert "does not resolve" in unknown.markdown

    quoted = run(
        service.execute(_ctx(), '/alias create zz memory save k "two words"', api=api)
    )
    assert quoted.success is False
    assert "single unquoted words" in quoted.markdown

    chat_stream = run(service.execute(_ctx(), "/alias create zz quick hello", api=api))
    assert chat_stream.success is False
    assert "chat turn" in chat_stream.markdown


def test_alias_create_accepts_a_whole_expansion_quoted_as_one_blob(alias_repo) -> None:
    """Discord's generated cog (and the CLI form rescue) shlex-quote the
    rest value they compose, so a realistic expansion arrives as ONE quoted
    blob (the #133 correctness review's H2, which made /alias create
    structurally unusable on Discord). That wrapping is not a grouped
    multi-word value: it re-splits into plain words. Grouping WITHIN the
    expansion stays refused (the v1 non-goal)."""
    service = CommandService()
    api = FakeCommandApi()

    blob = run(
        service.execute(_ctx(), "/alias create td 'todos list all'", api=api)
    )
    assert blob.success is True, blob.markdown
    listed = run(service.execute(_ctx(), "/td", api=api))
    assert "TODOs (all): 2 items" in listed.markdown

    grouped = run(
        service.execute(_ctx(), '/alias create zz memory save "two words"', api=api)
    )
    assert grouped.success is False
    assert "single unquoted words" in grouped.markdown


def test_alias_create_refuses_an_expansion_that_can_never_dispatch(alias_repo) -> None:
    """Extras and invalid values are permanent (no typed tail removes a
    token), so they are refused at CREATE instead of failing forever at
    dispatch (the review's M2). A missing required is deliberately fine: a
    prefix alias is completed by the typed tail."""
    service = CommandService()
    api = FakeCommandApi()

    extras = run(
        service.execute(
            _ctx(), "/alias create zz todos list all extraextra", api=api
        )
    )
    assert extras.success is False
    assert "would never dispatch" in extras.markdown

    prefix = run(service.execute(_ctx(), "/alias create nk memory save", api=api))
    assert prefix.success is True, prefix.markdown
    saved = run(service.execute(_ctx(), "/nk color blue", api=api))
    assert saved.success is True, saved.markdown


def test_stale_stamped_alias_is_inert_at_dispatch(alias_repo) -> None:
    """The store control wired through dispatch: an out-of-band row edit
    yields an unknown command, not the planted expansion, and the listing
    flags the row."""
    import sqlite3

    service = CommandService()
    api = FakeCommandApi()
    run(service.execute(_ctx(), "/alias create td todos list", api=api))

    with sqlite3.connect(str(alias_repo.db_path)) as conn:
        conn.execute(
            "UPDATE user_command_aliases SET tokens_json = ? WHERE name = 'td'",
            ('["env", "set", "llm_model", "evil"]',),
        )
        conn.commit()

    result = run(service.execute(_ctx(), "/td", api=api))
    assert result.success is False
    assert "Unknown command" in result.markdown

    listing = run(service.execute(_ctx(), "/alias list", api=api))
    assert "INERT" in listing.markdown


def test_list_commands_annotates_the_callers_user_aliases(alias_repo) -> None:
    """The catalog ride (#133): opted in WITH a user_id, each command
    carries THAT caller's aliases for it in `user_aliases` (display and
    client-mirror metadata; dispatch reads the store). Stale and other-user
    rows never ride, and the annotation is opt-in: the /help renderers pass
    user_id for skill visibility and must not pay an accounts.db read for
    an annotation they never show.
    """
    service = CommandService()
    api = FakeCommandApi()
    run(service.execute(_ctx(), "/alias create td todos list all", api=api))

    infos = service.list_commands(user_id="alice", include_user_aliases=True)
    by_id = {info.id: info for info in infos}
    assert by_id["todos.list"].user_aliases == ["/td"]
    assert by_id["todos"].user_aliases == []

    # user_id alone does not annotate (opt-in); another user sees nothing.
    assert all(
        info.user_aliases == [] for info in service.list_commands(user_id="alice")
    )
    assert all(
        info.user_aliases == []
        for info in service.list_commands(
            user_id="bob", include_user_aliases=True
        )
    )


def test_alias_claimed_by_a_later_builtin_goes_dormant(alias_repo) -> None:
    """Lose-to-everything at DISPATCH: the catalog can grow after an alias
    was created, and the catalog wins from that moment; the listing says
    why the spelling changed meaning."""
    service = CommandService()
    api = FakeCommandApi()
    run(service.execute(_ctx(), "/alias create zzlater todos list", api=api))

    async def _cmd_zzlater(self, bound):  # noqa: ANN001, ANN202
        return CommandOutput("builtin won")

    service.register("zzlater", description="late claim", category="Tests", params=())
    _CommandExecutor._cmd_zzlater = _cmd_zzlater  # type: ignore[attr-defined]
    try:
        result = run(service.execute(_ctx(), "/zzlater", api=api))
        assert result.success is True
        assert "builtin won" in result.markdown

        listing = run(service.execute(_ctx(), "/alias list", api=api))
        assert "dormant" in listing.markdown
    finally:
        del _CommandExecutor._cmd_zzlater  # type: ignore[attr-defined]


class _ModelCatalogCommandApi(FakeCommandApi):
    async def list_available_models(
        self, provider: str | None = None, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_available_models", (provider,), {}))
        return [
            {"id": "gpt-test", "context_length": 128000},
            {"id": "gpt-next", "context_length": 400000},
        ]


def _cli_ctx(thread_id: str | None = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id,
        actor="user",
        surface="cli",
        is_admin=True,
        supports_forms=True,
    )


def test_model_command_returns_declarative_form_payload() -> None:
    """Bare /model attaches the v1 picker form next to its markdown fallback."""
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(service.execute(_cli_ctx(), "/model", api=api))

    assert result.success is True
    assert "global: gpt-test" in result.markdown
    form = (result.data or {})["form"]
    assert form["version"] == 1
    assert form["title"] == "Select model"
    assert form["submit"]["command"] == "model {model} thread"
    (tab,) = form["tabs"]
    kinds = [field["kind"] for field in tab["fields"]]
    assert kinds == ["search", "radio"]
    options = tab["fields"][1]["options"]
    assert [option["id"] for option in options] == ["gpt-test", "gpt-next"]
    assert options[0]["current"] is True
    assert options[1]["current"] is False
    assert "ctx" in options[0]["meta"]


def test_model_command_without_thread_targets_global_scope() -> None:
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(service.execute(_cli_ctx(thread_id=None), "/model", api=api))

    form = (result.data or {})["form"]
    assert form["submit"]["command"] == "model {model} global"


def test_model_command_without_catalog_keeps_plain_output() -> None:
    """No model list available: bare /model stays a plain info string."""
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/model", api=api))

    assert result.success is True
    assert "global: gpt-test" in result.markdown
    assert result.data is None


def test_model_set_thread_scope_returns_state_hint() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/model gpt-next thread", api=api))

    assert result.success is True
    assert result.data == {"state": {"model": "gpt-next"}}
    assert ("update_thread_config", ("thread-1",), {"user_id": "alice", "llm_config": {"model": "gpt-next"}}) in api.calls

    global_result = run(service.execute(_cli_ctx(), "/model gpt-next", api=api))
    assert global_result.success is True
    assert global_result.data is None


def test_model_set_claude_on_openai_cliproxy_route_warns() -> None:
    """A claude-* id on the openai-routed proxy serves but silently loses
    the Claude OAuth treatment; the success message says so (B14)."""
    service = CommandService()
    api = FakeCommandApi()
    api.llm_base_url = "http://cli-proxy-api:8317/v1"

    result = run(
        service.execute(_cli_ctx(), "/model claude-opus-4-7 --force", api=api)
    )

    assert result.success is True
    assert "Global model set to claude-opus-4-7." in result.markdown
    assert "skips the" in result.markdown
    assert "/provider cliproxy claude" in result.markdown


def test_model_set_claude_off_cliproxy_has_no_drift_note() -> None:
    service = CommandService()
    api = FakeCommandApi()
    api.llm_base_url = "https://api.example.com/v1"

    result = run(
        service.execute(_cli_ctx(), "/model claude-opus-4-7 --force", api=api)
    )

    assert result.success is True
    assert "skips the" not in result.markdown


def test_model_set_claude_on_anthropic_cliproxy_route_has_no_drift_note() -> None:
    """The provider axis of the gate: a claude model on the ANTHROPIC
    CLIProxy route is the correctly-treated combination, so no note (a
    note there would tell a well-routed user their route is degraded)."""
    service = CommandService()
    api = FakeCommandApi()
    api.llm_provider = "anthropic"
    api.llm_base_url = "http://cli-proxy-api:8317"

    result = run(
        service.execute(_cli_ctx(), "/model claude-opus-4-7 --force", api=api)
    )

    assert result.success is True
    assert "skips the" not in result.markdown


def test_model_set_thread_scope_drift_note_follows_thread_route() -> None:
    """Thread scope resolves the EFFECTIVE route: a thread inheriting the
    global openai+CLIProxy route gets the note; a thread overridden to
    anthropic over the same globals does not."""
    service = CommandService()
    api = FakeCommandApi()
    api.llm_base_url = "http://localhost:8318/v1"

    inherited = run(
        service.execute(
            _cli_ctx(), "/model claude-opus-4-7 --force thread", api=api
        )
    )
    assert "Model for this thread set to claude-opus-4-7." in inherited.markdown
    assert "skips the" in inherited.markdown

    api.thread_config["llm_config"] = {"provider": "anthropic"}
    overridden = run(
        service.execute(
            _cli_ctx(), "/model claude-opus-4-7 --force thread", api=api
        )
    )
    assert "skips the" not in overridden.markdown


class _MixedPoolApi(FakeCommandApi):
    async def list_available_models(
        self, provider: str | None = None, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            {"id": "claude-opus-4-7", "owned_by": "anthropic"},
            {"id": "gemini-2.5-pro", "owned_by": "google", "context_length": 1048576},
            {"id": "gpt-5.5", "owned_by": "openai"},
            {"id": "gemini-3-pro-preview", "owned_by": "google"},
        ]


def test_model_list_groups_by_source_when_mixed() -> None:
    """A multi-source CLIProxy listing (the pool spans every logged-in
    subscription) groups rows under owner headings so a Claude id on a
    Gemini route reads as attribution, not as a bug. Headings and rows
    render at column 0 (the outcome renderer dedents the first body
    line, so indented blocks would come out lopsided)."""
    service = CommandService()
    api = _MixedPoolApi()
    api.llm_base_url = "http://localhost:8318/v1"

    result = run(service.execute(_cli_ctx(), "/model list", api=api))

    assert result.success is True
    lines = result.markdown.splitlines()
    assert "[anthropic]" in lines
    assert "[google]" in lines
    assert "[openai]" in lines
    # Group members sit under their heading, same column.
    google_at = lines.index("[google]")
    assert lines[google_at + 1] == "- gemini-2.5-pro | 1.0M ctx"
    assert lines[google_at + 2] == "- gemini-3-pro-preview"


def test_model_list_direct_provider_never_groups() -> None:
    """owned_by exists on direct providers too (OpenAI lists system /
    openai / openai-internal) and there it is noise: grouping keys on the
    runtime's CLIProxy URL predicate, so a direct base URL stays flat."""
    service = CommandService()
    api = _MixedPoolApi()
    api.llm_base_url = "https://api.example.com/v1"

    result = run(service.execute(_cli_ctx(), "/model list", api=api))

    assert result.success is True
    assert "- claude-opus-4-7" in result.markdown
    assert "[google]" not in result.markdown


def test_model_list_single_owner_stays_flat() -> None:
    """One displayed source, even through CLIProxy: no headings."""

    class _OneOwnerApi(FakeCommandApi):
        async def list_available_models(
            self, provider: str | None = None, user_id: str | None = None
        ) -> list[dict[str, Any]]:
            return [
                {"id": "gemini-2.5-pro", "owned_by": "google"},
                {"id": "gemini-3-pro-preview", "owned_by": "google"},
            ]

    service = CommandService()
    api = _OneOwnerApi()
    api.llm_base_url = "http://localhost:8318/v1"

    result = run(service.execute(_cli_ctx(), "/model list", api=api))

    assert result.success is True
    assert "- gemini-2.5-pro" in result.markdown
    assert "[google]" not in result.markdown


def test_model_list_unattributed_listing_stays_flat() -> None:
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(service.execute(_cli_ctx(), "/model list", api=api))

    assert result.success is True
    assert "- gpt-test" in result.markdown
    assert "[" not in result.markdown.split("\n", 1)[1]


# ── supports_forms capability gating (backlog #110) ──────────────────────────


def _no_forms_ctx() -> CommandContext:
    """A CLI-shaped caller that did NOT declare form support."""
    return CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface="cli",
        is_admin=True,
    )


def test_form_payload_stripped_without_supports_forms() -> None:
    """A caller without the capability flag gets the markdown fallback only:
    the form payload is stripped at the dispatcher choke point."""
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(service.execute(_no_forms_ctx(), "/model", api=api))

    assert result.success is True
    assert "gpt-test" in result.markdown  # fallback intact
    assert result.data is None


def _register_formstate_synthetic(
    service: CommandService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.register(
        "zzformstate",
        description="synthetic form+state command",
        category="Test",
        params=(),
    )

    async def _cmd_zzformstate(self, bound):  # noqa: ANN001, ANN202
        form = form_payload(
            "Synthetic",
            [form_tab("Pick", [radio_field("value", [form_option("a")])])],
            submit_command="zzformstate {value}",
        )
        return CommandOutput(
            "[Success]: synthetic",
            data=command_data(form=form, state={"model": "kept"}),
        )

    monkeypatch.setattr(
        _CommandExecutor, "_cmd_zzformstate", _cmd_zzformstate, raising=False
    )


def test_form_strip_keeps_state_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stripping is surgical: data.form goes, data.state survives (the CLI
    applies state hints even where forms are off, and they are small)."""
    service = CommandService()
    _register_formstate_synthetic(service, monkeypatch)

    stripped = run(service.execute(_no_forms_ctx(), "/zzformstate", api=object()))
    assert stripped.success is True
    assert stripped.data == {"state": {"model": "kept"}}

    kept = run(service.execute(_cli_ctx(), "/zzformstate", api=object()))
    assert kept.success is True
    assert kept.data is not None and "form" in kept.data
    assert kept.data["state"] == {"model": "kept"}


# ── typed result levels (#132) ───────────────────────────────────────────────

_SENTINELS = ("[Error]:", "[Success]:", "[Info]:", "[Saved]:")
_NYMERIA_PKG = Path(__file__).resolve().parents[1] / "nymeria"
# Paths relative to the nymeria/ package root: the producer surface is no
# longer core-only (#144 folded api/routers/chat.py in). The core half is
# GLOB-derived so a new command_* module (the #133 aliases mixin was missed
# by the old hand list) joins both ratchets automatically.
_COMMAND_LAYER_MODULES = tuple(
    f"core/{p.name}" for p in sorted(_NYMERIA_PKG.glob("core/command_*.py"))
) + ("api/routers/chat.py",)
# The only functions allowed to spell a sentinel: the boundary's transition
# parser (soft landing for plugin/out-of-tree handlers) and the two sites
# that legitimately PARSE a sentinel-shaped protocol (the /notepad
# tool-channel parse, and the goal-supervisor spawn's tool-channel check in
# chat.py, which today spells the colon-less "[Error]" but must not trip
# the ratchet if its comment's accurate "[Error]:" spelling ever lands).
_SENTINEL_ALLOWED = {
    ("core/command_service.py", "_render_result_markdown"),
    ("core/command_service.py", "_cmd_notepad_write"),
    ("api/routers/chat.py", "_spawn_goal_supervisor"),
}

# The outcome-artifact ratchet (#144): render_outcome in command_forms.py is
# THE producer of **Error:** / **Done.** / **Warning:**. Substring match, not
# startswith: several retired sites were f-string fragments, and a mid-string
# leak is exactly the bug class #132 fixed six of. `**Note:**` (truncation
# annotation) is a different artifact and is deliberately not matched. The
# CLI's _pop_level_signal CONSUMES the artifacts and must never join this
# module list.
_ARTIFACTS = ("**Error:**", "**Done.**", "**Warning:**")
_ARTIFACT_ALLOWED = {
    ("core/command_forms.py", "render_outcome"),
}


def _flagged_literals(module_path, is_flagged) -> list[tuple[str, str, int]]:
    """(function, literal, line) for every non-docstring flagged string."""
    import ast

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    hits: list[tuple[str, str, int]] = []

    def _docstring_node(node: Any) -> Any:
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            return None
        body = node.body
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value
        return None

    def _walk(node: Any, func: str, skip: set[int]) -> None:
        doc = _docstring_node(node)
        if doc is not None:
            skip = skip | {id(doc)}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and is_flagged(node.value)
        ):
            hits.append((func, node.value[:40], node.lineno))
        for child in ast.iter_child_nodes(node):
            _walk(child, func, skip)

    _walk(tree, "<module>", set())
    return hits


def _ratchet_violations(is_flagged, allowed: set[tuple[str, str]]) -> list[str]:
    violations: list[str] = []
    for name in _COMMAND_LAYER_MODULES:
        for func, literal, line in _flagged_literals(_NYMERIA_PKG / name, is_flagged):
            if (name, func) in allowed:
                continue
            violations.append(f"{name}:{line} in {func}: {literal!r}")
    return violations


def test_command_layer_carries_no_sentinel_literals() -> None:
    """The #132 ratchet: the prefix protocol is retired in the command
    layer. New sentinel literals mean a handler is bypassing the typed
    constructors; use command_error/command_success/command_info instead.
    (The tool layer's identical spelling is a DIFFERENT protocol and is
    deliberately out of scope here.)"""
    violations = _ratchet_violations(
        lambda value: any(value.startswith(s) for s in _SENTINELS),
        _SENTINEL_ALLOWED,
    )
    assert not violations, (
        "Sentinel literals outside the allowlist (author a typed level "
        "instead):\n" + "\n".join(violations)
    )


def test_command_layer_carries_no_handwritten_outcome_artifacts() -> None:
    """The #144 ratchet: render_outcome is THE producer of the markdown
    outcome artifacts. A new hand-spelled **Error:** / **Done.** /
    **Warning:** in the command layer or the chat_stream router means a
    site is bypassing it (level and artifact can then disagree); pass a
    level to render_outcome / _slash_sse_response instead."""
    violations = _ratchet_violations(
        lambda value: any(artifact in value for artifact in _ARTIFACTS),
        _ARTIFACT_ALLOWED,
    )
    assert not violations, (
        "Outcome-artifact literals outside render_outcome (author a level "
        "instead):\n" + "\n".join(violations)
    )


def _register_level_synthetic(
    service: CommandService, monkeypatch: pytest.MonkeyPatch, ret: Any
) -> None:
    service.register(
        "zzlevel", description="synthetic level command", category="Test", params=()
    )

    async def _cmd_zzlevel(self, bound):  # noqa: ANN001, ANN202
        return ret

    monkeypatch.setattr(
        _CommandExecutor, "_cmd_zzlevel", _cmd_zzlevel, raising=False
    )


def _run_level(monkeypatch: pytest.MonkeyPatch, ret: Any):
    service = CommandService()
    _register_level_synthetic(service, monkeypatch, ret)
    return run(service.execute(_cli_ctx(), "/zzlevel", api=object()))


def test_authored_error_fails_and_drops_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_level(
        monkeypatch, command_error("nope", data=command_data(state={"x": 1}))
    )
    assert result.success is False
    assert result.level == "error"
    assert result.markdown == "**Error:** nope"
    assert result.data is None  # failure drops data, the established rule


def test_authored_success_renders_the_done_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_level(monkeypatch, command_success("Model set."))
    assert result.success is True
    assert result.level == "success"
    assert result.markdown == "**Done.** Model set."


def test_authored_warning_keeps_data_and_renders_the_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # warning is success-with-a-caveat: data (forms, state hints) survives.
    result = _run_level(
        monkeypatch,
        command_warning("applied with caveats", data=command_data(state={"x": 1})),
    )
    assert result.success is True
    assert result.level == "warning"
    assert result.markdown == "**Warning:** applied with caveats"
    assert result.data == {"state": {"x": 1}}


def test_authored_info_gets_no_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_level(monkeypatch, command_info("Provider  anthropic"))
    assert result.success is True
    assert result.level == "info"
    assert result.markdown == "Provider  anthropic"


def test_plain_string_return_is_an_info_readout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The info level is no longer collapsed into success: a readout says
    # "info" on the wire, and the heading heuristic still promotes the
    # first plain line of a multi-line body.
    result = _run_level(monkeypatch, "Status\n\nProvider  anthropic")
    assert result.success is True
    assert result.level == "info"
    assert result.markdown.startswith("### Status")


def test_legacy_sentinel_still_wins_over_authored_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Transition rule for the in-flight sweep: a text still carrying an
    # old sentinel keeps exact legacy behavior regardless of level.
    result = _run_level(
        monkeypatch, CommandOutput("[Error]: legacy path", level="info")
    )
    assert result.success is False
    assert result.level == "error"
    assert result.markdown == "**Error:** legacy path"


def test_render_outcome_is_byte_exact_per_level() -> None:
    """The #144 producer: exact artifact bytes per level, info untouched.

    These four strings are the wire contract every surface reads; the
    dispatcher's equality tests above pin the same bytes through execute(),
    this pins the producer directly (chat.py's funnel uses it without the
    dispatcher's sentinel/heading extras)."""
    assert render_outcome("error", "nope") == "**Error:** nope"
    assert render_outcome("success", "Model set.") == "**Done.** Model set."
    assert render_outcome("warning", "partial") == "**Warning:** partial"
    body = "Status\n\nProvider  anthropic"
    # info is VERBATIM: no artifact, and none of the dispatcher's
    # heading-heuristic rewriting (that would change /goal status bytes).
    assert render_outcome("info", body) == body


def test_execution_kind_refusal_data_survives_without_the_flag() -> None:
    """The strip is form-only: the failure-path execution_kind payload (the
    bots' structural chat_stream re-route signal) must survive a form-less
    caller untouched."""
    service = CommandService()

    result = run(service.execute(_no_forms_ctx(), "/skill demo", api=object()))

    assert result.success is False
    assert (result.data or {}).get("execution_kind") == "chat_stream"


# ── missing-required rescue into a generated picker (backlog #110) ───────────


def _register_pick_synthetic(
    service: CommandService,
    monkeypatch: pytest.MonkeyPatch,
    params: tuple[Any, ...],
) -> None:
    service.register(
        "zzpick", description="synthetic picker command", category="Test",
        params=params,
    )

    async def _cmd_zzpick(self, bound):  # noqa: ANN001, ANN202
        return f"[Success]: picked {bound.get('thing')}"

    monkeypatch.setattr(_CommandExecutor, "_cmd_zzpick", _cmd_zzpick, raising=False)


def _pick_params() -> tuple[Any, ...]:
    from nymeria.core.command_params import CommandParam

    return (CommandParam("thing", required=True, choices=("alpha", "beta")),)


def test_missing_required_rescues_to_generated_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CommandService()
    _register_pick_synthetic(service, monkeypatch, _pick_params())

    result = run(service.execute(_cli_ctx(), "/zzpick", api=object()))

    assert result.success is True
    assert "Usage: `/zzpick" in result.markdown
    form = (result.data or {})["form"]
    assert form["submit"] == {"command": "zzpick {thing}"}
    options = form["tabs"][0]["fields"][0]["options"]
    assert [option["id"] for option in options] == ["alpha", "beta"]


def test_missing_required_stays_an_error_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CommandService()
    _register_pick_synthetic(service, monkeypatch, _pick_params())

    result = run(service.execute(_no_forms_ctx(), "/zzpick", api=object()))

    assert result.success is False
    assert "Missing required argument" in result.markdown
    assert result.data is None


def test_missing_required_never_rescues_for_the_agent_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CommandService()
    _register_pick_synthetic(service, monkeypatch, _pick_params())
    agent_ctx = CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="agent",
        surface="agent",
        is_admin=None,
        supports_forms=True,
    )

    result = run(service.execute(agent_ctx, "/zzpick", api=object()))

    assert result.success is False
    assert "Missing required argument" in result.markdown


def test_invalid_and_extra_arguments_never_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ABSENT arguments rescue: a wrong value or a stray extra keeps
    the strict error even for a form-capable caller."""
    service = CommandService()
    _register_pick_synthetic(service, monkeypatch, _pick_params())

    invalid = run(service.execute(_cli_ctx(), "/zzpick bogus", api=object()))
    assert invalid.success is False
    assert invalid.data is None

    extra = run(service.execute(_cli_ctx(), "/zzpick alpha stray", api=object()))
    assert extra.success is False
    assert "Unexpected argument" in extra.markdown


def test_ungeneratable_declaration_falls_through_to_the_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nymeria.core.command_params import CommandParam

    service = CommandService()
    _register_pick_synthetic(
        service,
        monkeypatch,
        (CommandParam("thing", kind="rest", required=True),),
    )

    result = run(service.execute(_cli_ctx(), "/zzpick", api=object()))

    assert result.success is False
    assert "Missing required argument" in result.markdown


# ── LLM family declared-argument adoption (backlog #129 wave 1b) ─────────────


class _FallbackChainCommandApi(FakeCommandApi):
    """A settings fake with a populated fallback chain."""

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        data = await super().get_settings(user_id=user_id)
        data["llm_fallback_models"] = "a-model,b-model"
        return data


def test_model_denies_a_model_the_provider_does_not_list() -> None:
    """The measured bug: a typo used to be written verbatim.

    Nothing rejected it, so the mistake only surfaced as a provider error on
    the next turn, in a thread whose configured model was now junk.
    """
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(service.execute(_cli_ctx(), "/model gpt-tset", api=api))

    assert result.success is False
    assert "Unknown model: gpt-tset" in result.markdown
    assert "Did you mean: gpt-test" in result.markdown
    assert "Use --force to set it anyway." in result.markdown
    assert not [
        call for call in api.calls if call[0] in ("update_settings", "update_thread_config")
    ]

    # An off-list name with no near-match still names the escape hatch.
    lonely = run(service.execute(_cli_ctx(), "/model zzzzzzzz", api=api))
    assert lonely.success is False
    assert "Did you mean" not in lonely.markdown
    assert "Use --force to set it anyway." in lonely.markdown


def test_model_force_writes_an_unlisted_model() -> None:
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(
        service.execute(_cli_ctx(), "/model my-local-build --force global", api=api)
    )

    assert result.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_model": "my-local-build"},
    ) in api.calls

    # The flag reads the same before the scope word or in place of it.
    threaded = run(
        service.execute(_cli_ctx(), "/model my-local-build --force thread", api=api)
    )
    assert threaded.success is True
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "llm_config": {"model": "my-local-build"}},
    ) in api.calls


def test_model_writes_unlisted_names_when_the_catalog_is_unavailable() -> None:
    """The guard degrades: an unlistable provider must not block a change.

    Same invocation, two backends: the one that can list models denies it,
    the one that cannot writes it.
    """
    service = CommandService()

    listless = FakeCommandApi()  # no list_available_models at all
    written = run(service.execute(_cli_ctx(), "/model my-local-build", api=listless))
    assert written.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_model": "my-local-build"},
    ) in listless.calls

    listing = _ModelCatalogCommandApi()
    denied = run(service.execute(_cli_ctx(), "/model my-local-build", api=listing))
    assert denied.success is False
    assert not [call for call in listing.calls if call[0] == "update_settings"]


def test_model_scope_word_alone_shows_that_scope() -> None:
    """``/model global`` used to set the model to the literal "global"."""
    service = CommandService()
    api = _ModelCatalogCommandApi()

    result = run(service.execute(_cli_ctx(), "/model global", api=api))

    assert result.success is True
    assert "global: gpt-test" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]
    # With a thread active the picker defaults to thread scope; naming a
    # scope retargets it instead of being swallowed as a model name.
    assert (result.data or {})["form"]["submit"]["command"] == "model {model} global"


def test_think_rejects_off_ladder_values_and_a_bare_scope() -> None:
    api = FakeCommandApi()

    off_ladder = _run_think(api, "/think sideways")
    assert off_ladder.success is False
    assert "`sideways` is not a valid mode" in off_ladder.markdown
    assert "Valid: off, on, low, medium, high, xhigh, max" in off_ladder.markdown

    bare_scope = _run_think(api, "/think global")
    assert bare_scope.success is False
    assert "Name a level to write that scope." in bare_scope.markdown

    extra = _run_think(api, "/think high now")
    assert extra.success is False
    assert "Unexpected argument `now`" in extra.markdown

    assert not [call for call in api.calls if call[0].startswith("update_")]


def test_llm_zero_argument_commands_reject_extras() -> None:
    service = CommandService()
    api = _ModelCatalogCommandApi()

    for command in (
        "/models",
        "/provider list",
        "/fallback",
        "/fallback list",
        "/fallback clear",
        "/fallback status",
    ):
        ok = run(service.execute(_cli_ctx(), command, api=api))
        assert ok.success is True, command

        rejected = run(service.execute(_cli_ctx(), f"{command} bogus", api=api))
        assert rejected.success is False, command
        assert "Unexpected argument `bogus`" in rejected.markdown, command

    # reasoning-passback needs live LLM state, so only its binding is checked.
    passback = run(
        service.execute(_cli_ctx(), "/provider reasoning-passback bogus", api=api)
    )
    assert passback.success is False
    assert "Unexpected argument `bogus`" in passback.markdown


def test_fallback_add_positions_the_model_and_validates_the_option() -> None:
    service = CommandService()

    equals_form = _FallbackChainCommandApi()
    inserted = run(
        service.execute(
            _cli_ctx(), "/fallback add c-model --position=2", api=equals_form
        )
    )
    assert inserted.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_fallback_models": "a-model,c-model,b-model"},
    ) in equals_form.calls

    spaced_form = _FallbackChainCommandApi()
    appended = run(
        service.execute(_cli_ctx(), "/fallback add c-model --position 3", api=spaced_form)
    )
    assert appended.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_fallback_models": "a-model,b-model,c-model"},
    ) in spaced_form.calls

    api = _FallbackChainCommandApi()
    not_an_int = run(
        service.execute(_cli_ctx(), "/fallback add c-model --position x", api=api)
    )
    assert not_an_int.success is False
    assert "--position must be an integer, got `x`" in not_an_int.markdown

    # Semantic validation the dispatcher cannot do stays in the handler.
    too_low = run(
        service.execute(_cli_ctx(), "/fallback add c-model --position 0", api=api)
    )
    assert too_low.success is False
    assert "Position must be 1 or greater." in too_low.markdown

    typo = run(
        service.execute(_cli_ctx(), "/fallback add c-model --postion 2", api=api)
    )
    assert typo.success is False
    assert "Unknown option `--postion`" in typo.markdown

    missing = run(service.execute(_cli_ctx(), "/fallback add", api=api))
    assert missing.success is False
    assert "Missing required argument: model-id" in missing.markdown

    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_fallback_remove_and_set_rewrite_the_chain() -> None:
    service = CommandService()

    aliased = _FallbackChainCommandApi()
    removed = run(service.execute(_cli_ctx(), "/fallback rm a-model", api=aliased))
    assert removed.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_fallback_models": "b-model"},
    ) in aliased.calls

    api = _FallbackChainCommandApi()
    absent = run(service.execute(_cli_ctx(), "/fallback remove z-model", api=api))
    assert absent.success is False
    assert "Fallback model is not configured: z-model" in absent.markdown

    # set takes the whole tail, deduping while keeping first-seen order.
    replaced = _FallbackChainCommandApi()
    chain = run(
        service.execute(_cli_ctx(), "/fallback set x-model y-model x-model", api=replaced)
    )
    assert chain.success is True
    assert "Fallback chain set: x-model -> y-model" in chain.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_fallback_models": "x-model,y-model"},
    ) in replaced.calls

    empty = run(service.execute(_cli_ctx(), "/fallback set", api=api))
    assert empty.success is False
    assert "Missing required argument: model-id" in empty.markdown


def test_provider_set_takes_repeated_pairs_and_never_echoes_a_bare_secret() -> None:
    api = FakeCommandApi()

    applied = _run_command(
        api, "/provider set anthropic api_key=sk-a direct_api_key=sk-b"
    )
    assert applied.success is True
    assert (
        "update_settings",
        (),
        {
            "user_id": "alice",
            "anthropic_api_key": "sk-a",
            "anthropic_direct_api_key": "sk-b",
        },
    ) in api.calls

    # A pasted bare secret is a usage error whose copy must not repeat it.
    leaked = _run_command(api, "/provider set anthropic sk-super-secret")
    assert leaked.success is False
    assert "sk-super-secret" not in leaked.markdown
    assert "not echoed in case it is a secret" in leaked.markdown

    missing = _run_command(api, "/provider set anthropic")
    assert missing.success is False
    assert "Missing required argument: key=value" in missing.markdown
    assert len([call for call in api.calls if call[0] == "update_settings"]) == 1


def test_provider_test_rejects_a_second_provider_token() -> None:
    api = FakeCommandApi()

    result = _run_command(api, "/provider test openai groq")

    assert result.success is False
    assert "Unexpected argument `groq`" in result.markdown
    assert "Usage: `/provider test [provider]`" in result.markdown
    assert not [call for call in api.calls if call[0] == "test_llm_provider_config"]


# ── Context-domain commands (/usage, /artifacts) migrated from the CLI ────────


class _UsageStatsCommandApi(FakeCommandApi):
    async def get_context_stats(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(("get_context_stats", (thread_id,), {}))
        return {
            "model": "gpt-thread",
            "input_tokens": 80,
            "output_tokens": 40,
            "total_tokens": 120_000,
            "context_limit": 400_000,
            "usage_percentage": 30,
            "compact_trigger_tokens": 200_000,
            "cumulative_tokens": 240_000,
            "compaction_count": 2,
        }


def test_usage_command_renders_compact_marker_from_context_stats() -> None:
    """Migrated /usage sources the 'until compact' trigger from context stats."""
    service = CommandService()
    api = _UsageStatsCommandApi()

    result = run(service.execute(_cli_ctx(), "/usage", api=api))

    assert result.success is True
    assert "Until compact" in result.markdown
    assert "of 200.0k" in result.markdown
    assert ("get_context_stats", ("thread-1",), {}) in api.calls


def test_usage_aliases_tokens_and_cost_resolve_to_usage() -> None:
    service = CommandService()

    for alias in ("/tokens", "/cost"):
        result = run(service.execute(_cli_ctx(), alias, api=_UsageStatsCommandApi()))
        assert result.success is True
        assert "Token Usage" in result.markdown


def test_usage_rejects_stray_argument() -> None:
    service = CommandService()

    result = run(service.execute(_cli_ctx(), "/usage bogus", api=_UsageStatsCommandApi()))

    assert result.success is False
    # Declared zero-arg: the binder rejects the stray word and the family-root
    # guidance layer still names the one subcommand.
    assert "Unexpected argument `bogus`" in result.markdown
    assert "Valid subcommands: session." in result.markdown
    assert "Usage: `/usage`" in result.markdown


def test_format_thread_usage_compact_cap_honors_trigger_tokens() -> None:
    from nymeria.core.command_executor_context import _format_thread_usage

    stats = {
        "model": "gpt-thread",
        "input_tokens": 80,
        "output_tokens": 40,
        "total_tokens": 120_000,
        "context_limit": 400_000,
        "usage_percentage": 30,
    }

    tokens_view = _format_thread_usage(stats, compact_trigger=200_000)
    unscaled_view = _format_thread_usage(stats, compact_trigger=400_000)

    assert "Until compact" in tokens_view
    assert "of 200.0k" in tokens_view
    assert "Until compact" not in unscaled_view


def test_usage_session_shows_cumulative_and_rejects_extra_args() -> None:
    service = CommandService()
    api = _UsageStatsCommandApi()

    ok = run(service.execute(_cli_ctx(), "/usage session", api=api))
    assert ok.success is True
    assert "Session Usage" in ok.markdown
    assert "240.0k" in ok.markdown

    extra = run(service.execute(_cli_ctx(), "/usage session extra", api=_UsageStatsCommandApi()))
    assert extra.success is False
    assert "Usage: `/usage session`" in extra.markdown


def test_artifacts_recent_lists_from_thread_history() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/artifacts recent", api=api))

    assert result.success is True
    assert "Recent Artifacts" in result.markdown
    assert "report.txt" in result.markdown
    assert "/workspace/data.csv" in result.markdown
    history_calls = [call for call in api.calls if call[0] == "get_history"]
    assert history_calls and history_calls[0][2]["include_internal"] is True


def test_artifacts_bare_root_forwards_to_recent_listing() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/artifacts", api=api))

    assert result.success is True
    assert "Recent Artifacts" in result.markdown


def test_artifacts_recent_reports_empty_history() -> None:
    class _EmptyHistoryApi(FakeCommandApi):
        async def get_history(
            self,
            thread_id: str,
            user_id: str | None = None,
            *,
            include_internal: bool = False,
        ) -> dict[str, Any]:
            return {"thread_id": thread_id, "messages": []}

    service = CommandService()

    result = run(service.execute(_cli_ctx(), "/artifacts recent", api=_EmptyHistoryApi()))

    assert result.success is True
    assert "No recent workspace artifacts" in result.markdown


# ── /thread tree migrated from the CLI ───────────────────────────────────────


def test_thread_list_renders_threads_and_reports_empty() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/thread list", api=api))
    assert result.success is True
    assert "Current" in result.markdown
    assert "Next" in result.markdown

    empty = FakeCommandApi()
    empty.threads = []
    none_result = run(service.execute(_cli_ctx(), "/thread list", api=empty))
    assert none_result.success is True
    assert "No threads found" in none_result.markdown


def test_team_list_and_show_render_teams() -> None:
    service = CommandService()
    api = FakeCommandApi()
    api.thread_teams = [
        {
            "id": "team-ops-1",
            "name": "Ops",
            "description": "Ops crew",
            "thread_ids": ["thread-1", "thread-2"],
        },
        {"id": "team-qa-2", "name": "QA", "description": None, "thread_ids": []},
    ]

    listed = run(service.execute(_cli_ctx(), "/team list", api=api))
    assert listed.success is True
    assert "Ops (team-ops-1): 2 member(s). Ops crew" in listed.markdown
    assert "QA (team-qa-2): 0 member(s)." in listed.markdown

    # Bare /team lists; /team <ref> is show shorthand (id or name, any case).
    bare = run(service.execute(_cli_ctx(), "/team", api=api))
    assert "Teams (2):" in bare.markdown

    shown = run(service.execute(_cli_ctx(), "/team show ops", api=api))
    assert shown.success is True
    assert "Team: Ops" in shown.markdown
    assert "thread-1 Current" in shown.markdown
    assert "thread-2 Next" in shown.markdown

    shorthand = run(service.execute(_cli_ctx(), "/team team-qa-2", api=api))
    assert "Team: QA" in shorthand.markdown
    assert "- (none)" in shorthand.markdown

    missing = run(service.execute(_cli_ctx(), "/team show nope", api=api))
    assert "No team matching 'nope'" in missing.markdown

    empty = FakeCommandApi()
    none_result = run(service.execute(_cli_ctx(), "/team list", api=empty))
    assert "No callable-thread teams yet" in none_result.markdown


def test_thread_switch_resolves_ref_and_returns_switch_state_hint() -> None:
    service = CommandService()
    api = FakeCommandApi()

    # Resolve by title substring (mirrors the CLI resolver).
    result = run(service.execute(_cli_ctx(), "/thread switch next", api=api))
    assert result.success is True
    assert result.data == {
        "state": {"switch_thread": {"thread_id": "thread-2", "thread_label": "Next"}}
    }

    # The `s` alias resolves to the same handler.
    aliased = run(service.execute(_cli_ctx(), "/thread s thread-2", api=api))
    assert aliased.data == {
        "state": {"switch_thread": {"thread_id": "thread-2", "thread_label": "Next"}}
    }


def test_thread_switch_missing_and_ambiguous_refs() -> None:
    service = CommandService()
    api = FakeCommandApi()
    api.threads = [
        {"thread_id": "alpha-1", "title": "Quarterly Planning"},
        {"thread_id": "alpha-2", "title": "Quarterly Review"},
    ]

    missing = run(service.execute(_cli_ctx(), "/thread switch nope", api=api))
    assert missing.success is False
    assert "No thread matching" in missing.markdown

    ambiguous = run(service.execute(_cli_ctx(), "/thread switch alpha", api=api))
    assert ambiguous.success is True
    assert "Ambiguous" in ambiguous.markdown
    assert ambiguous.data is None


def test_thread_new_creates_and_returns_switch_state_hint() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/thread new Draft title", api=api))
    assert result.success is True
    assert result.data == {
        "state": {
            "switch_thread": {"thread_id": "thread-new", "thread_label": "Draft title"}
        }
    }
    assert ("create_thread", ("alice",), {"thread_id": None, "title": "Draft title"}) in api.calls


def test_thread_rename_updates_metadata_and_returns_label_hint() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/thread rename Renamed", api=api))
    assert result.success is True
    assert result.data == {"state": {"thread_label": "Renamed"}}
    assert (
        "update_thread_metadata",
        ("thread-1",),
        {"user_id": "alice", "title": "Renamed", "pinned": None},
    ) in api.calls


def test_thread_pin_toggles_and_hints_only_for_active_thread() -> None:
    service = CommandService()
    api = FakeCommandApi()

    # Pinning a non-active thread carries no header-refresh hint.
    other = run(service.execute(_cli_ctx(), "/thread pin thread-2 off", api=api))
    assert other.success is True
    assert other.data is None
    assert (
        "update_thread_metadata",
        ("thread-2",),
        {"user_id": "alice", "title": None, "pinned": False},
    ) in api.calls

    # Toggling the active thread refreshes the header via a metadata hint.
    active = run(service.execute(_cli_ctx(), "/thread pin toggle", api=api))
    assert active.success is True
    assert active.data == {"state": {"thread_metadata_updated": True}}


def test_thread_delete_guards_active_thread_and_accepts_yes_flag() -> None:
    service = CommandService()
    api = FakeCommandApi()

    active = run(service.execute(_cli_ctx(), "/thread delete thread-1", api=api))
    assert active.success is False
    assert "active thread" in active.markdown
    assert not any(name == "delete_thread" for name, *_ in api.calls)

    deleted = run(service.execute(_cli_ctx(), "/thread delete thread-2 --yes", api=api))
    assert deleted.success is True
    assert ("delete_thread", ("thread-2",), {"user_id": "alice"}) in api.calls


def test_thread_compact_reports_result_and_context_hint() -> None:
    service = CommandService()
    api = FakeCommandApi()

    result = run(service.execute(_cli_ctx(), "/thread compact", api=api))
    assert result.success is True
    assert "Removed 3 messages (8 -> 5)" in result.markdown
    assert result.data == {"state": {"thread_context_updated": True}}
    assert ("compact_thread", ("thread-1",), {"user_id": "alice"}) in api.calls


def test_thread_branch_and_top_level_branch_share_handler() -> None:
    service = CommandService()
    api = FakeCommandApi()

    branched = run(service.execute(_cli_ctx(), "/thread branch --from 3 Side quest", api=api))
    assert branched.success is True
    assert "from message #3" in branched.markdown
    assert branched.data == {
        "state": {"switch_thread": {"thread_id": "branch-1", "thread_label": "Side quest"}}
    }
    assert (
        "branch_thread",
        ("thread-1",),
        {"user_id": "alice", "title": "Side quest", "from_message_index": 3},
    ) in api.calls

    top = run(service.execute(_cli_ctx(), "/branch Topic", api=FakeCommandApi()))
    assert top.success is True
    assert top.data == {
        "state": {"switch_thread": {"thread_id": "branch-1", "thread_label": "Topic"}}
    }


def test_thread_info_and_config_render_from_client() -> None:
    service = CommandService()
    api = FakeCommandApi()

    info = run(service.execute(_cli_ctx(), "/thread info", api=api))
    assert info.success is True
    assert "Thread ID" in info.markdown
    assert "thread-1" in info.markdown

    config = run(service.execute(_cli_ctx(), "/thread config", api=api))
    assert config.success is True
    assert "Thread Config" in config.markdown


# ── /thread family declared-argument adoption (backlog #129 wave 1) ──────────


def test_thread_root_renders_overview_but_rejects_stray_arguments() -> None:
    """The headline fix: bare ``/thread`` used to swallow anything after it.

    ``/thread bogus`` rendered the overview panel as if the word were not
    there, which is how a mistyped subcommand became a silent no-op.
    """
    service = CommandService()
    api = FakeCommandApi()

    overview = run(service.execute(_cli_ctx(), "/thread", api=api))
    assert overview.success is True
    assert "Thread Info" in overview.markdown

    stray = run(service.execute(_cli_ctx(), "/thread bogus", api=api))
    assert stray.success is False
    assert "Unexpected argument `bogus`" in stray.markdown
    assert "Usage: `/thread`" in stray.markdown
    assert not any(name == "get_context_stats" for name, *_ in api.calls[1:])

    # `/thread help` is intercepted before binding, so the card still renders.
    card = run(service.execute(_cli_ctx(), "/thread help", api=api))
    assert card.success is True
    assert "## /thread" in card.markdown


def test_thread_zero_argument_subcommands_reject_extras() -> None:
    service = CommandService()
    api = FakeCommandApi()

    for command in ("/thread list", "/thread info", "/thread config", "/team list"):
        ok = run(service.execute(_cli_ctx(), command, api=api))
        assert ok.success is True, command

        rejected = run(service.execute(_cli_ctx(), f"{command} bogus", api=api))
        assert rejected.success is False, command
        assert "Unexpected argument `bogus`" in rejected.markdown, command


def test_thread_switch_bare_rescues_form_clients_and_errors_everyone_else() -> None:
    """Bare /thread switch: a form-capable caller gets the generated thread
    picker (#110, via the threads choices_ref); a form-less caller keeps the
    strict usage error; /thread rename (free-text title) stays an error for
    everyone because typing the title IS the form."""
    service = CommandService()
    api = FakeCommandApi()

    switch = run(service.execute(_cli_ctx(), "/thread switch", api=api))
    assert switch.success is True
    form = (switch.data or {})["form"]
    assert form["submit"] == {"command": "thread switch {id_or_title}"}
    options = form["tabs"][0]["fields"][0]["options"]
    # /thread list ordering: most recently updated first (thread-2 is newer
    # in the fake's seed), with the calling thread marked current.
    assert [option["id"] for option in options] == ["thread-2", "thread-1"]
    assert [option["id"] for option in options if option["current"]] == ["thread-1"]

    plain = run(service.execute(_no_forms_ctx(), "/thread switch", api=api))
    assert plain.success is False
    assert "Missing required argument: id-or-title" in plain.markdown
    assert "Usage: `/thread switch <id-or-title>`" in plain.markdown

    rename = run(service.execute(_cli_ctx(), "/thread rename", api=api))
    assert rename.success is False
    assert "Missing required argument: title" in rename.markdown
    assert not any(name == "update_thread_metadata" for name, *_ in api.calls)


def test_thread_branch_accepts_equals_form_and_the_short_from_alias() -> None:
    """``--from=N`` and the once undocumented ``-f`` are both declared now."""
    service = CommandService()

    equals_form = FakeCommandApi()
    result = run(
        service.execute(_cli_ctx(), "/thread branch --from=3 Side quest", api=equals_form)
    )
    assert result.success is True
    assert (
        "branch_thread",
        ("thread-1",),
        {"user_id": "alice", "title": "Side quest", "from_message_index": 3},
    ) in equals_form.calls

    short_alias = FakeCommandApi()
    aliased = run(service.execute(_cli_ctx(), "/branch -f 2 Topic", api=short_alias))
    assert aliased.success is True
    assert (
        "branch_thread",
        ("thread-1",),
        {"user_id": "alice", "title": "Topic", "from_message_index": 2},
    ) in short_alias.calls


def test_thread_branch_rejects_bad_from_values_and_unknown_options() -> None:
    service = CommandService()
    api = FakeCommandApi()

    not_an_int = run(service.execute(_cli_ctx(), "/thread branch --from x Side", api=api))
    assert not_an_int.success is False
    assert "--from must be an integer, got `x`" in not_an_int.markdown

    # Semantic validation the dispatcher cannot do stays in the handler.
    too_low = run(service.execute(_cli_ctx(), "/thread branch --from 0 Side", api=api))
    assert too_low.success is False
    assert "--from must be 1 or greater" in too_low.markdown

    # Before adoption an unknown option fell through into the title, so
    # `/branch --form 3 x` silently created a branch titled "--form 3 x".
    typo = run(service.execute(_cli_ctx(), "/branch --form 3 Side quest", api=api))
    assert typo.success is False
    assert "Unknown option `--form`" in typo.markdown
    assert not any(name == "branch_thread" for name, *_ in api.calls)

    # The title is a repeatable positional, not a rest tail: --from parses on
    # EITHER side of the title (the retired hand parser's contract; a rest
    # tail silently swallowed a trailing "--from 3" into the title), and a
    # typo'd option errors anywhere, never joining the title.
    trailing = run(service.execute(_cli_ctx(), "/branch Side quest --from 2", api=api))
    assert trailing.success is True
    assert (
        "branch_thread",
        ("thread-1",),
        {"user_id": "alice", "title": "Side quest", "from_message_index": 2},
    ) in api.calls

    typo_after = run(service.execute(_cli_ctx(), "/branch Side --form 2", api=api))
    assert typo_after.success is False
    assert "Unknown option `--form`" in typo_after.markdown


def test_thread_delete_and_compact_accept_the_short_yes_alias() -> None:
    service = CommandService()
    api = FakeCommandApi()

    deleted = run(service.execute(_cli_ctx(), "/thread delete thread-2 -y", api=api))
    assert deleted.success is True
    assert ("delete_thread", ("thread-2",), {"user_id": "alice"}) in api.calls

    compacted = run(service.execute(_cli_ctx(), "/thread compact -y", api=api))
    assert compacted.success is True
    assert ("compact_thread", ("thread-1",), {"user_id": "alice"}) in api.calls

    # The confirm flag takes no value, and stray words no longer pass silently.
    valued = run(service.execute(_cli_ctx(), "/thread compact --yes=1", api=api))
    assert valued.success is False
    assert "--yes does not take a value" in valued.markdown

    extra = run(service.execute(_cli_ctx(), "/thread delete thread-2 now", api=api))
    assert extra.success is False
    assert "Unexpected argument `now`" in extra.markdown


def test_thread_pin_keeps_its_state_synonyms_and_rejects_extras() -> None:
    """``on|off|toggle`` is advertised; yes/no/true/false have always worked."""
    service = CommandService()
    api = FakeCommandApi()

    synonym = run(service.execute(_cli_ctx(), "/thread pin thread-2 no", api=api))
    assert synonym.success is True
    assert (
        "update_thread_metadata",
        ("thread-2",),
        {"user_id": "alice", "title": None, "pinned": False},
    ) in api.calls

    # A bare state word still means "the active thread".
    bare_state = run(service.execute(_cli_ctx(), "/thread pin on", api=api))
    assert bare_state.success is True
    assert (
        "update_thread_metadata",
        ("thread-1",),
        {"user_id": "alice", "title": None, "pinned": True},
    ) in api.calls

    extra = run(service.execute(_cli_ctx(), "/thread pin thread-2 off now", api=api))
    assert extra.success is False
    assert "Unexpected argument `now`" in extra.markdown

    # Without an id and without an active thread there is nothing to pin.
    orphan = run(service.execute(_cli_ctx(thread_id=None), "/thread pin", api=api))
    assert orphan.success is False
    assert "No active thread" in orphan.markdown


def test_thread_resolver_matches_ids_titles_substrings_and_ambiguity() -> None:
    from nymeria.core.command_executor_threads import resolve_thread_reference

    thread_list = [
        {"thread_id": "alpha-111", "title": "Quarterly Planning"},
        {"thread_id": "alpha-222", "title": "Quarterly Review"},
        {"thread_id": "bravo-333", "title": "Supplier Followup"},
    ]

    assert resolve_thread_reference(thread_list, "bravo-333").thread == thread_list[2]
    assert resolve_thread_reference(thread_list, "bravo").thread == thread_list[2]
    assert (
        resolve_thread_reference(thread_list, "Quarterly Planning").thread == thread_list[0]
    )
    assert resolve_thread_reference(thread_list, "supplier").thread == thread_list[2]
    assert resolve_thread_reference(thread_list, "alpha").status == "ambiguous"
    assert resolve_thread_reference(thread_list, "missing").status == "missing"


def test_thread_list_formats_backend_teams_before_ungrouped_threads() -> None:
    from nymeria.core.command_executor_threads import _format_thread_list

    thread_list = [
        {
            "thread_id": "recent",
            "title": "Recent unpinned",
            "pinned": False,
            "updated_at": "2026-05-10T12:00:00Z",
            "platform": "desktop",
        },
        {
            "thread_id": "pinned",
            "title": "Pinned ungrouped",
            "pinned": True,
            "updated_at": "2026-05-10T10:00:00Z",
            "platform": "desktop",
        },
        {
            "thread_id": "team-a",
            "title": "Team A",
            "pinned": False,
            "updated_at": "2026-05-10T09:00:00Z",
            "platform": "callable",
        },
    ]
    lines = _format_thread_list(
        thread_list,
        active_thread_id="recent",
        teams=[{"id": "ops", "name": "Ops", "thread_ids": ["team-a"]}],
    )

    team_index = lines.index("  Team: Ops")
    pinned_index = lines.index("  Pinned")
    recent_index = lines.index("  Recent")
    assert team_index < pinned_index < recent_index
    assert any("team-a" in line and "Team A" in line for line in lines)
    assert any("pinned" in line and "Pinned ungrouped" in line for line in lines)


def test_in_process_branch_thread_refuses_while_processing() -> None:
    """CommandBackendClient.branch_thread mirrors the route's mid-turn 409.

    POST /threads/{id}/branch rejects branching a processing thread (the
    branch would copy the last committed checkpoint and drop the in-flight
    turn); the in-process slash-command client must keep the same guard now
    that /thread branch and /branch run through it on every frontend.
    """

    class _Locks:
        def get_lock_info(self, thread_id: str):
            return {"holder": "turn"}

    agent = SimpleNamespace(
        _thread_locks=_Locks(),
        accounts_repo=SimpleNamespace(claim_thread=lambda tid, uid: uid),
    )
    user = _CommandBackendUser(id="alice", role="admin")
    client = CommandBackendClient(agent, user=user)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(client.branch_thread("thread-1", title="fork"))
    assert excinfo.value.response.status_code == 409
    assert "processing" in excinfo.value.response.text.lower()


def test_http_client_list_available_models_posts_ephemeral_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP facade routes credential overrides through POST, never a URL.

    A plain listing keeps the GET; an api_key or base_url override switches to
    the POST body so a just-pasted key from the /provider setup flow cannot
    land in a query string or access log.
    """

    client = CommandHttpClient("http://api.test", "token", use_act_as=True)
    calls: list[tuple[Any, ...]] = []

    async def fake_get(path, params=None, act_as=None):
        calls.append(("GET", path, params, act_as))
        return []

    async def fake_post(path, json=None, params=None, act_as=None):
        calls.append(("POST", path, json, act_as))
        return []

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "_post", fake_post)

    run(client.list_available_models("openai", "alice"))
    run(
        client.list_available_models(
            "openai",
            "alice",
            api_key="sk-ephemeral",
            base_url="http://proxy.test/v1",
        )
    )
    run(client.close())

    assert calls[0] == ("GET", "/models/available", {"provider": "openai"}, "alice")
    assert calls[1] == (
        "POST",
        "/models/available",
        {
            "provider": "openai",
            "api_key": "sk-ephemeral",
            "base_url": "http://proxy.test/v1",
        },
        "alice",
    )


def test_in_process_list_available_models_prefers_ephemeral_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process facade seeds the ephemeral key/base URL first.

    Mirrors the POST /models/available contract: an override from the
    /provider setup flow wins over vault/settings resolution and reaches the
    provider probe as the Authorization credential.
    """

    calls: list[dict[str, Any]] = []

    class _FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url: str, *, headers: dict):
            calls.append({"url": url, "headers": headers})
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                json={"data": [{"id": "m-eph", "name": "M"}]},
                request=request,
            )

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    agent = SimpleNamespace()  # no credential_vault -> vault resolution is a no-op
    settings = SimpleNamespace(llm_provider="anthropic", llm_base_url=None)
    client = CommandBackendClient(
        agent,
        user=_CommandBackendUser(id="alice", role="admin"),
        settings_fn=lambda: settings,
    )

    models = run(
        client.list_available_models(
            "lmstudio",
            api_key="sk-ephemeral",
            base_url="http://localhost:1234/v1/",
        )
    )

    assert [m["id"] for m in models] == ["m-eph"]
    assert calls[0]["url"] == "http://localhost:1234/v1/models"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ephemeral"


def test_http_client_cliproxy_facade_hits_the_admin_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP twins map onto the /cliproxy admin routes verb-for-verb."""

    client = CommandHttpClient("http://api.test", "token", use_act_as=True)
    calls: list[tuple[Any, ...]] = []

    async def fake_get(path, params=None, act_as=None):
        calls.append(("GET", path, params))
        if path == "/cliproxy/models":
            return {"models": [{"id": "m-1", "owned_by": "anthropic"}]}
        if path == "/cliproxy/oauth/status":
            return {"status": "ok", "detail": "alice@example.com"}
        return []

    async def fake_post(path, json=None, params=None, act_as=None):
        calls.append(("POST", path, json))
        if path == "/cliproxy/verify":
            return {"verdict": "ok", "detail": "claude-opus-4-7"}
        return {"status": "ok"}

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "_post", fake_post)

    run(client.cliproxy_auth_files("claude"))
    run(client.cliproxy_oauth_start("claude"))
    run(client.cliproxy_oauth_callback("claude", code="abc", state="st-1"))
    run(client.cliproxy_oauth_status("st-1", "claude"))
    models = run(client.cliproxy_models())
    run(client.cliproxy_apply_route("claude", "claude-opus-4-7"))
    verdict = run(client.cliproxy_verify_credential("claude", model="opus"))
    run(client.close())

    assert calls == [
        ("GET", "/cliproxy/auth-files", {"provider": "claude"}),
        ("POST", "/cliproxy/oauth/start", {"provider": "claude"}),
        (
            "POST",
            "/cliproxy/oauth/callback",
            {"provider": "claude", "code": "abc", "state": "st-1"},
        ),
        ("GET", "/cliproxy/oauth/status", {"state": "st-1", "provider": "claude"}),
        ("GET", "/cliproxy/models", None),
        (
            "POST",
            "/cliproxy/apply-route",
            {"provider": "claude", "model": "claude-opus-4-7", "scope": "global"},
        ),
        ("POST", "/cliproxy/verify", {"provider": "claude", "model": "opus"}),
    ]
    # The models payload is unwrapped to the bare list.
    assert [m["id"] for m in models] == ["m-1"]
    # The twins hand back the raw envelope; the executor unwraps it.
    assert verdict == {"verdict": "ok", "detail": "claude-opus-4-7"}


def test_in_process_cliproxy_auth_files_filter_accepts_v7_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process twin's provider filter is the one the slim CLI hits;
    it must keep both listed gemini spellings and drop other providers."""

    class _FakeManagement:
        async def list_auth_files(self):
            return [
                {"name": "gemini-a.json", "provider": "gemini-cli"},
                {"name": "gemini-b.json", "provider": "gemini"},
                {"name": "claude-c.json", "provider": "claude"},
            ]

    client = CommandBackendClient(
        SimpleNamespace(),
        user=_CommandBackendUser(id="alice", role="admin"),
        settings_fn=lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        client, "_cliproxy_client_or_400", lambda: _FakeManagement()
    )

    files = run(client.cliproxy_auth_files("gemini-cli"))

    assert [entry["name"] for entry in files] == [
        "gemini-a.json",
        "gemini-b.json",
    ]


def test_in_process_cliproxy_requires_admin() -> None:
    client = CommandBackendClient(
        SimpleNamespace(),
        user=_CommandBackendUser(id="bob", role="user"),
        settings_fn=lambda: SimpleNamespace(),
    )
    for call in (
        client.cliproxy_auth_files(),
        client.cliproxy_oauth_start("claude"),
        client.cliproxy_oauth_callback("claude", code="x", state="y"),
        client.cliproxy_oauth_status("st", "claude"),
        client.cliproxy_models(),
        client.cliproxy_apply_route("claude", "m"),
        client.cliproxy_verify_credential("claude"),
    ):
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            run(call)
        assert excinfo.value.response.status_code == 403


def test_in_process_cliproxy_400_when_management_unconfigured() -> None:
    """No management URL/key -> the wire-shaped 400 the routes return."""
    client = CommandBackendClient(
        SimpleNamespace(),
        user=_CommandBackendUser(id="alice", role="admin"),
        settings_fn=lambda: SimpleNamespace(
            cliproxy_management_url=None, cliproxy_management_key=None
        ),
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(client.cliproxy_oauth_start("claude"))
    assert excinfo.value.response.status_code == 400
    assert "not configured" in str(excinfo.value)


def test_in_process_cliproxy_unknown_provider_404s() -> None:
    client = CommandBackendClient(
        SimpleNamespace(),
        user=_CommandBackendUser(id="alice", role="admin"),
        settings_fn=lambda: SimpleNamespace(
            cliproxy_management_url="http://127.0.0.1:8317",
            cliproxy_management_key="secret",
        ),
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        run(client.cliproxy_oauth_start("bogus"))
    assert excinfo.value.response.status_code == 404


# ── Guidance: fuzzy suggestions, per-command help, blocked surfaces ─────────


def _ctx(surface: str = "cli", *, is_admin: bool = True) -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id="thread-1",
        actor="user",
        surface=surface,  # type: ignore[arg-type]
        is_admin=is_admin,
    )


def test_unknown_command_suggests_nearest_root() -> None:
    result = run(CommandService().execute(_ctx(), "/provder list", api=FakeCommandApi()))
    assert result.success is False
    assert "Unknown command `/provder`" in result.markdown
    assert "Did you mean `/provider`?" in result.markdown


def test_unknown_subcommand_suggests_nearest() -> None:
    # /restart is the one deliberately root-less family, so it is what reaches
    # the no-definition branch; a family WITH a root answers a bad verb from
    # its own strict binding (below).
    result = run(CommandService().execute(_ctx(), "/restart ap", api=FakeCommandApi()))
    assert result.success is False
    assert "Unknown subcommand `ap` for `/restart`" in result.markdown
    assert "Did you mean `/restart api`?" in result.markdown


def test_family_root_with_a_bare_action_suggests_nearest_too() -> None:
    result = run(CommandService().execute(_ctx(), "/memory serch cats", api=FakeCommandApi()))
    assert result.success is False
    assert "Unexpected argument `serch`" in result.markdown
    assert "Did you mean `/memory search`?" in result.markdown
    assert "Valid subcommands: delete, limit, list, save, search." in result.markdown


def test_group_root_error_points_at_help() -> None:
    result = run(CommandService().execute(_ctx(), "/restart", api=FakeCommandApi()))
    assert result.success is False
    assert "`/restart` requires a subcommand" in result.markdown
    assert "See `/help restart`." in result.markdown


def test_help_bare_is_compact_index() -> None:
    result = run(CommandService().execute(_ctx(), "/help", api=FakeCommandApi()))
    assert result.success is True
    assert len(result.markdown) < 4000
    assert "**LLM:**" in result.markdown
    assert "`/provider`" in result.markdown
    assert "`/help <command>`" in result.markdown
    assert "`/help all`" in result.markdown
    # The index lists roots only, never subcommand rows.
    assert "| Command | Usage | Description |" not in result.markdown


def test_help_all_renders_full_table_with_escaped_pipes() -> None:
    result = run(CommandService().execute(_ctx(), "/help all", api=FakeCommandApi()))
    assert result.success is True
    assert "| Command | Usage | Description |" in result.markdown
    # Pipe-alternative usage strings must not break the markdown table.
    assert "active\\|pending\\|in_progress" in result.markdown
    # The note join must not double the period ("immediately.. Handled").
    assert "immediately.. Handled" not in result.markdown
    assert "immediately. Handled" in result.markdown


def test_help_command_renders_card_with_subcommands_and_examples() -> None:
    result = run(CommandService().execute(_ctx(), "/help provider", api=FakeCommandApi()))
    assert result.success is True
    assert result.markdown.startswith("## /provider")
    assert (
        "Usage: `/provider [<provider>|setup|list|set|switch|test|cliproxy|reasoning-passback]`"
        in result.markdown
    )
    assert "| setup |" in result.markdown
    assert "Examples:" in result.markdown
    assert "`/provider switch anthropic thread`" in result.markdown


def test_help_command_card_filters_subcommands_by_surface() -> None:
    result = run(
        CommandService().execute(_ctx("telegram"), "/help provider", api=FakeCommandApi())
    )
    assert result.success is True
    # provider setup / cliproxy are blocked_surfaces-refused on chat
    # surfaces, so the card drops them there.
    assert "| setup |" not in result.markdown
    assert "| cliproxy |" not in result.markdown
    assert "| switch |" in result.markdown


def test_help_card_children_ignore_discovery_surfaces() -> None:
    # /hook subcommands are menu-filtered off chat surfaces (``surfaces`` is
    # discovery-only) but stay executable there through the generic bot
    # passthroughs, so the telegram card must still list them.
    result = run(
        CommandService().execute(_ctx("telegram"), "/help hook", api=FakeCommandApi())
    )
    assert result.success is True
    assert "| list |" in result.markdown
    assert "| disable |" in result.markdown


def test_help_card_alias_renders_single_slash() -> None:
    result = run(CommandService().execute(_ctx(), "/help think", api=FakeCommandApi()))
    assert result.success is True
    assert "`/reasoning`" in result.markdown
    assert "//reasoning" not in result.markdown


def test_help_card_admin_only_target_renders_for_non_admin() -> None:
    # Deliberate: execution acknowledges admin-only commands to non-admins
    # (the admin gate names the command in its refusal), so help does too,
    # with the Access line carrying the restriction. Menus still hide it.
    result = run(
        CommandService().execute(
            _ctx(is_admin=False), "/help provider setup", api=FakeCommandApi()
        )
    )
    assert result.success is True
    assert "admin only" in result.markdown


def test_hidden_command_has_no_help_card() -> None:
    service = CommandService()
    service.register(
        "shadow",
        description="Hidden test command",
        category="General",
        usage="/shadow",
        hidden=True,
    )
    result = run(service.execute(_ctx(), "/help shadow", api=FakeCommandApi()))
    assert result.success is False
    assert "Unknown command" in result.markdown


def test_root_help_argument_renders_card() -> None:
    result = run(CommandService().execute(_ctx(), "/provider help", api=FakeCommandApi()))
    assert result.success is True
    assert result.markdown.startswith("## /provider")


def test_group_help_argument_renders_card() -> None:
    result = run(CommandService().execute(_ctx(), "/tools help", api=FakeCommandApi()))
    assert result.success is True
    assert result.markdown.startswith("## /tools")
    assert "| enable |" in result.markdown


def test_help_unknown_target_suggests() -> None:
    result = run(CommandService().execute(_ctx(), "/help provder", api=FakeCommandApi()))
    assert result.success is False
    assert "Did you mean `/provider`?" in result.markdown


def test_blocked_surface_refuses_with_reason() -> None:
    for surface in ("slack", "telegram", "whatsapp", "teams", "discord"):
        result = run(
            CommandService().execute(
                _ctx(surface), "/provider setup openai", api=FakeCommandApi()
            )
        )
        assert result.success is False, surface
        assert f"not available on {surface}" in result.markdown
        assert "message history" in result.markdown
        assert "Available on: desktop, mobile, cli, api" in result.markdown


def test_blocked_surface_does_not_gate_cli() -> None:
    result = run(
        CommandService().execute(_ctx("cli"), "/provider setup", api=FakeCommandApi())
    )
    # Reaches the real handler (which asks for a provider), not the surface gate.
    assert "not available on" not in result.markdown


def test_root_usage_errors_render_from_registry() -> None:
    # One arg is the per-provider action step now, so the /provider usage
    # error needs two stray tokens.
    result = run(
        CommandService().execute(_ctx(), "/provider bogus extra", api=FakeCommandApi())
    )
    assert result.success is False
    assert (
        "Usage: `/provider [<provider>|setup|list|set|switch|test|cliproxy|reasoning-passback]`"
        in result.markdown
    )
    assert "See `/help provider`." in result.markdown

    # /mcp is a strict zero-arg root now, so a stray verb is a binder rejection
    # carrying the generated (argument-free) usage line.
    result = run(CommandService().execute(_ctx(), "/mcp bogus", api=FakeCommandApi()))
    assert result.success is False
    assert "Unexpected argument `bogus`" in result.markdown
    assert "Usage: `/mcp`." in result.markdown
    assert "See `/help mcp`." in result.markdown


def test_notepad_write_append_prefix_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, str] = {}

    def fake_write(thread_id: str, content: str, mode: str = "append") -> str:
        saved["content"] = content
        saved["mode"] = mode
        return "[Saved]: ok"

    import nymeria.tools.thread_notes as thread_notes

    monkeypatch.setattr(thread_notes, "write_notepad", fake_write)
    result = run(
        CommandService().execute(_ctx(), "/notepad write append: milk", api=FakeCommandApi())
    )
    assert result.success is True
    assert saved["mode"] == "append"
    assert saved["content"] == "milk"


def test_rest_extraction_survives_newlines(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chat clients send Shift+Enter newlines; the rest extractor must treat
    # any whitespace as a token boundary or the word glued to the newline is
    # silently dropped.
    saved: dict[str, str] = {}

    def fake_write(thread_id: str, content: str, mode: str = "append") -> str:
        saved["content"] = content
        return "[Saved]: ok"

    import nymeria.tools.thread_notes as thread_notes

    monkeypatch.setattr(thread_notes, "write_notepad", fake_write)
    result = run(
        CommandService().execute(
            _ctx(), "/notepad write\nmilk and bread", api=FakeCommandApi()
        )
    )
    assert result.success is True
    assert saved["content"] == "milk and bread"


# ── #129 wave 2a: memory and notepad declared params ─────────────────────────


def test_memory_save_joins_the_value_tail() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/memory save color deep blue", api=api))

    assert result.success is True, result.markdown
    assert ("save_memory", ("alice", "color", "deep blue"), {}) in api.calls


def test_memory_save_keeps_an_apostrophe_in_the_value() -> None:
    # A rest param re-joins the whitespace-split tokens, so an apostrophe in
    # free text is ordinary English rather than an unbalanced quote.
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/memory save plan Bob's plan", api=api))

    assert result.success is True, result.markdown
    assert ("save_memory", ("alice", "plan", "Bob's plan"), {}) in api.calls


def test_memory_search_joins_the_whole_query() -> None:
    api = FakeCommandApi()
    api.memories = [{"key": "a", "value": "deep blue paint"}]
    result = run(CommandService().execute(_ctx(), "/memory search deep blue", api=api))

    assert result.success is True, result.markdown
    assert ("search_memories", ("alice", "deep blue"), {}) in api.calls
    assert "deep blue paint" in result.markdown


def test_memory_forget_rejects_extra_arguments() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/memory forget color extra", api=api))

    assert result.success is False
    assert "Unexpected argument `extra`" in result.markdown
    assert not [call for call in api.calls if call[0] == "forget_memory"]


def test_memory_list_rejects_arguments() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/memory list everything", api=api))

    assert result.success is False
    assert "Unexpected argument `everything`" in result.markdown
    assert not [call for call in api.calls if call[0] == "list_memories"]


def test_memory_limit_trailing_scope_never_steals_the_value() -> None:
    """The value leads and the scope trails (#131 wave B).

    The binder pops a trailing ``global``/``thread`` word BEFORE it assigns
    positionals, which is exactly the ordering that could have eaten the
    number instead. Same digits, two scopes, two different writes; and a bare
    value writes the thread, the default scope.
    """
    api = FakeCommandApi()
    service = CommandService()

    to_global = run(service.execute(_ctx(), "/memory limit 5000 global", api=api))
    to_thread = run(service.execute(_ctx(), "/memory limit 5000 thread", api=api))
    bare = run(service.execute(_ctx(), "/memory limit 4000", api=api))

    assert to_global.success is True, to_global.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "memory_char_limit": 5000},
    ) in api.calls
    assert to_thread.success is True, to_thread.markdown
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "memory_char_limit": 5000},
    ) in api.calls
    assert bare.success is True, bare.markdown
    assert (
        "update_thread_config",
        ("thread-1",),
        {"user_id": "alice", "memory_char_limit": 4000},
    ) in api.calls


def test_memory_limit_rejects_a_word_that_is_neither_value_nor_scope() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/memory limit 5000 everywhere", api=api))

    assert result.success is False
    assert "Unexpected argument `everywhere`" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]
    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_memory_limit_scope_without_a_value_shows_generated_usage() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/memory limit global", api=api))

    assert result.success is False
    assert "Usage: `/memory limit [chars|inherit] [global|thread]`" in result.markdown
    assert "`global` takes a character count." in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_memory_limit_inherit_clears_the_thread_override_at_either_spelling() -> None:
    """`inherit` is the value; `global` names the SCOPE and no longer a value.

    Before wave B the thread arm accepted `global` as a third synonym of
    `inherit` (`/memory limit thread global`). The trailing scope token owns
    that word now, so the synonym is retired and the surviving spellings are
    the bare value and the explicit thread scope.
    """
    service = CommandService()
    for command in ("/memory limit inherit", "/memory limit inherit thread"):
        api = FakeCommandApi()
        api.thread_config["memory_char_limit"] = 6000
        result = run(service.execute(_ctx(), command, api=api))

        assert result.success is True, (command, result.markdown)
        assert "inherits the global memory character limit" in result.markdown
        assert api.thread_config["memory_char_limit"] is None

    # The retired synonym: `global` reaches the value position only in this
    # contorted spelling, and there it is a bad character count, not a
    # second way to say `inherit`.
    api = FakeCommandApi()
    api.thread_config["memory_char_limit"] = 6000
    retired = run(service.execute(_ctx(), "/memory limit global thread", api=api))
    assert retired.success is False
    assert "Limit must be an integer" in retired.markdown
    assert api.thread_config["memory_char_limit"] == 6000


def test_notepad_read_rejects_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []

    def fake_read(thread_id: str) -> str:
        reads.append(thread_id)
        return "notes"

    import nymeria.tools.thread_notes as thread_notes

    monkeypatch.setattr(thread_notes, "read_notepad", fake_read)
    result = run(CommandService().execute(_ctx(), "/notepad read all", api=FakeCommandApi()))

    assert result.success is False
    assert "Unexpected argument `all`" in result.markdown
    assert reads == []


# ── #129 wave 2b: settings, tools, tier, todo, and status declared params ────


def test_settings_set_joins_a_multi_word_value() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/settings set log_level DEBUG mode", api=api))

    assert result.success is True, result.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "log_level": "DEBUG mode"},
    ) in api.calls


def test_settings_get_without_a_key_names_the_missing_argument() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/settings get", api=api))

    assert result.success is False
    assert "Missing required argument: key" in result.markdown
    assert "Usage: `/settings get <key>`" in result.markdown
    assert not [call for call in api.calls if call[0] == "get_settings"]


def test_settings_root_rejects_extra_arguments() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/settings show everything", api=api))

    assert result.success is False
    assert "Unexpected argument `everything`" in result.markdown
    assert not [call for call in api.calls if call[0] == "get_settings"]


def test_env_set_without_a_value_writes_nothing() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/env set PERPLEXITY_API_KEY", api=api))

    assert result.success is False
    assert "Missing required argument: value" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_settings_verbs_are_registered_children_with_their_own_schemas() -> None:
    service = CommandService()
    api = FakeCommandApi()

    missing_key = run(service.execute(_ctx(), "/settings get", api=api))
    assert missing_key.success is False
    assert "Missing required argument: key" in missing_key.markdown
    assert "Usage: `/settings get <key>`" in missing_key.markdown

    written = run(service.execute(_ctx(), "/settings set log_level DEBUG level", api=api))
    assert written.success is True, written.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "log_level": "DEBUG level"},
    ) in api.calls

    extra = run(service.execute(_ctx(), "/settings show everything", api=api))
    assert extra.success is False
    assert "Unexpected argument `everything`" in extra.markdown


def test_tools_enable_requires_a_target_and_rejects_extras() -> None:
    service = CommandService()
    api = FakeCommandApi()

    missing = run(service.execute(_ctx(), "/tools enable", api=api))
    assert missing.success is False
    assert "Missing required argument: tool_or_category" in missing.markdown
    assert "Usage: `/tools enable <tool_or_category>`" in missing.markdown

    extra = run(service.execute(_ctx(), "/tools enable browser web", api=api))
    assert extra.success is False
    assert "Unexpected argument `web`" in extra.markdown
    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_tools_list_rejects_a_second_argument() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/tools list enabled all", api=api))

    assert result.success is False
    assert "Unexpected argument `all`" in result.markdown
    assert not [call for call in api.calls if call[0] == "get_default_tools"]


def test_fast_set_is_a_registered_child_and_requires_a_model_id() -> None:
    service = CommandService()
    api = FakeCommandApi()

    missing = run(service.execute(_ctx(), "/fast set", api=api))
    assert missing.success is False
    assert "Missing required argument: model-id" in missing.markdown
    assert "Usage: `/fast set <model-id>`" in missing.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]

    written = run(service.execute(_ctx(), "/smart set anthropic:claude-opus-4-8", api=api))
    assert written.success is True, written.markdown
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_smart_model": "anthropic:claude-opus-4-8"},
    ) in api.calls


def test_fast_rejects_an_unknown_mode_and_points_at_the_set_verb() -> None:
    service = CommandService()
    api = FakeCommandApi()

    # One stray word is the handler's own rejection, which names the verb.
    unknown = run(service.execute(_ctx(), "/fast summarize", api=api))
    assert unknown.success is False
    assert "Usage: `/fast [on|off|set <model-id>]`" in unknown.markdown
    assert "Subcommands: set." in unknown.markdown

    # A second word is a binder rejection, which layers the same guidance.
    extra = run(service.execute(_ctx(), "/fast on now", api=api))
    assert extra.success is False
    assert "Unexpected argument `now`" in extra.markdown
    assert "Valid subcommands: set." in extra.markdown

    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_background_verbs_bind_and_reject_extras() -> None:
    service = CommandService()
    api = FakeCommandApi()

    missing_url = run(service.execute(_ctx(), "/background set-url", api=api))
    assert missing_url.success is False
    assert "Missing required argument: base-url" in missing_url.markdown
    assert "Usage: `/background set-url <base-url>`" in missing_url.markdown

    extra = run(service.execute(_ctx(), "/background clear all", api=api))
    assert extra.success is False
    assert "Unexpected argument `all`" in extra.markdown

    unknown = run(service.execute(_ctx(), "/background bogus", api=api))
    assert unknown.success is False
    assert "Unexpected argument `bogus`" in unknown.markdown
    assert "Usage: `/background`." in unknown.markdown
    # The derived subcommand list speaks the DISPLAYED spelling (#131 wave B
    # flipped it), which is the one the generated usage above advertises.
    assert "Valid subcommands: clear, set, set-url." in unknown.markdown

    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_flipped_family_roots_declare_no_arguments() -> None:
    """The nine roots that used to carry a catch-all ``subcommand`` positional.

    Each one now binds strictly with zero arguments, so a root typo is a
    dispatcher usage error rather than a handler-side re-dispatch. The
    declaration is what makes the strict-extras guidance fire, so assert it
    directly alongside the guidance it buys.
    """
    service = CommandService()
    flipped = (
        "account", "activity", "doctor", "hook", "mcp",
        "settings", "skills", "triggers", "background",
        # The overview roots backlog #131 minted take the same strict shape.
        "env", "memory", "notepad", "todos", "tools",
    )
    for name in flipped:
        info = service.find_command(name)
        assert info is not None, name
        assert info.params == [], (name, info.params)
        assert info.usage == f"/{name}", name

    # End to end: a near-miss verb on a flipped root gets the nearest match
    # plus the full valid list, which is the whole point of the flip.
    typo = run(service.execute(_ctx(), "/mcp discovr", api=FakeCommandApi()))
    assert typo.success is False
    assert "Unexpected argument `discovr`" in typo.markdown
    assert "Did you mean `/mcp discover`?" in typo.markdown
    assert (
        "Valid subcommands: delete, discover, list, logs, retry, status, test."
        in typo.markdown
    )


def test_settings_view_alias_survives_the_root_flip() -> None:
    """``/settings view`` and ``/settings show`` are aliases of the root now.

    Backlog #131 folded the show verb into the bare root (style-guide rule 2),
    so both spellings resolve to `settings` itself and still render.
    """
    service = CommandService()
    for spelling in ("settings view", "settings show", "settings_show"):
        resolved = service.find_command(spelling)
        assert resolved is not None, spelling
        assert resolved.name == "settings", spelling

    for typed in ("/settings view", "/settings show"):
        result = _run_command(FakeCommandApi(), typed)
        assert result.success is True, result.markdown
        assert "provider: openai" in result.markdown
        assert "model: gpt-test" in result.markdown


def test_background_show_alias_survives_the_root_flip() -> None:
    """``/background show`` is a whole-path alias of the bare root now."""
    service = CommandService()
    resolved = service.find_command("background show")
    assert resolved is not None
    assert resolved.name == "background"

    api = FakeCommandApi()
    shown = run(service.execute(_ctx(), "/background show", api=api))
    assert shown.success is True, shown.markdown
    assert "Background model:" in shown.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_sequential_tools_scope_without_a_mode_shows_generated_usage() -> None:
    service = CommandService()
    api = FakeCommandApi()

    bare_global = run(service.execute(_ctx(), "/sequential-tools global", api=api))
    assert bare_global.success is False
    assert "Usage: `/sequential-tools [on|off|inherit] [global|thread]`" in bare_global.markdown
    assert "Name `on` or `off` to write that scope." in bare_global.markdown

    # `inherit` has no meaning globally: the global value IS what a thread
    # inherits, so the global scope takes only on/off.
    inherit_global = run(service.execute(_ctx(), "/sequential-tools inherit global", api=api))
    assert inherit_global.success is False
    assert "The `global` scope takes `on` or `off`." in inherit_global.markdown

    assert not [call for call in api.calls if call[0] == "update_settings"]


def test_sequential_tools_retired_the_scope_as_a_mode_value() -> None:
    """`/sequential-tools global on` is gone; the scope trails now.

    Recorded break (#131 wave B): keeping it would have meant keeping the
    scope-as-a-mode-value shape plus its second positional, which is the
    grammar the canon retired. The rejection must still teach the
    replacement, so the generated usage rides along.
    """
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/sequential-tools global on", api=api))

    assert result.success is False
    assert "Usage: `/sequential-tools [on|off|inherit] [global|thread]`" in result.markdown
    assert not [call for call in api.calls if call[0] == "update_settings"]
    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_sequential_tools_still_accepts_the_default_synonym() -> None:
    # `default` is an undocumented `inherit` synonym the handler keeps, so it
    # is inside the declared choices even though the label omits it.
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/sequential-tools default", api=api))

    assert result.success is True, result.markdown
    assert "inherits the global sequential tool execution setting" in result.markdown


def test_sequential_tools_rejects_a_second_value_word() -> None:
    """One mode word, one optional scope word, nothing else.

    Two bare positionals used to smuggle a verb grammar: ``/sequential-tools
    on off`` bound both words, the thread arm used only the first, and the
    second was silently dropped while the command reported success. Only one
    value positional is declared now, so a second word is a binder rejection
    and nothing is written.
    """
    service = CommandService()
    api = FakeCommandApi()

    for command in (
        "/sequential-tools on off",
        "/sequential-tools off on",
        "/sequential-tools inherit on",
        "/sequential-tools default off",
    ):
        result = run(service.execute(_ctx(), command, api=api))
        assert result.success is False, (command, result.markdown)
        assert "Unexpected argument" in result.markdown, command
        assert (
            "Usage: `/sequential-tools [on|off|inherit] [global|thread]`"
            in result.markdown
        ), command

    assert not [call for call in api.calls if call[0] == "update_thread_config"]
    assert not [call for call in api.calls if call[0] == "update_settings"]

    # The single-token forms and the scoped form are untouched.
    ok_thread = run(service.execute(_ctx(), "/sequential-tools on", api=api))
    assert ok_thread.success is True, ok_thread.markdown
    ok_global = run(service.execute(_ctx(), "/sequential-tools on global", api=api))
    assert ok_global.success is True, ok_global.markdown


def test_sequential_tools_mode_is_a_closed_declared_set() -> None:
    """An unknown mode is a binder rejection now, not a handler usage error."""
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/sequential-tools sideways", api=api))

    assert result.success is False
    assert "`sideways` is not a valid on|off|inherit" in result.markdown
    assert "Valid: on, off, inherit, default." in result.markdown
    assert not [call for call in api.calls if call[0] == "update_thread_config"]


def test_prune_rejects_an_unknown_mode_before_touching_the_thread() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/prune sideways", api=api))

    assert result.success is False
    assert "`sideways` is not a valid mode" in result.markdown
    assert "Valid: soft, full" in result.markdown
    assert not [call for call in api.calls if call[0] == "prune_thread"]


def test_todos_complete_requires_an_id() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/todos complete", api=api))

    assert result.success is False
    assert "Missing required argument: todo_id" in result.markdown
    assert "Usage: `/todos complete <todo_id>`" in result.markdown
    assert not [call for call in api.calls if call[0] == "list_todos"]


def test_todos_list_rejects_a_second_filter_word() -> None:
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/todos list done pending", api=api))

    assert result.success is False
    assert "Unexpected argument `pending`" in result.markdown
    assert not [call for call in api.calls if call[0] == "list_todos"]


# ── #143: the CLI-local /todo family ported into the backend catalog ─────────


def _todos_call(api: FakeCommandApi, name: str) -> tuple[str, tuple, dict]:
    matches = [call for call in api.calls if call[0] == name]
    assert len(matches) == 1, f"expected one {name} call, saw {matches}"
    return matches[0]


def test_todos_add_flag_and_pipe_grammars_bind_the_same_call() -> None:
    service = CommandService()
    flag_api = FakeCommandApi()
    flags = run(
        service.execute(
            _ctx(),
            "/todos add Check logs --schedule 2h --repeat daily --notes phone",
            api=flag_api,
        )
    )
    assert flags.success is True, flags.markdown
    assert "Created TODO todo-new" in flags.markdown
    _, _, flag_kwargs = _todos_call(flag_api, "add_todo")

    pipe_api = FakeCommandApi()
    pipe = run(
        service.execute(
            _ctx(), "/todos add Check logs | 2h | daily | phone", api=pipe_api
        )
    )
    assert pipe.success is True, pipe.markdown
    _, _, pipe_kwargs = _todos_call(pipe_api, "add_todo")

    assert flag_kwargs == pipe_kwargs == {
        "task": "Check logs",
        "scheduled_for": "2h",
        "notes": "phone",
        # validate_recurrence canonicalizes "daily" before the call.
        "recurrence": "1d",
        "thread_id": "thread-1",
    }


def test_todos_add_flags_disable_the_pipe_fallback() -> None:
    # With any option flag present the task passes VERBATIM: the pipe
    # grammar only applies to the pure legacy form, so a task containing a
    # literal `|` is never silently truncated (review catch: the
    # fire-on-any-pipe merge dropped ` b parser` on the floor here).
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            _ctx(), "/todos add Fix the a|b parser --schedule 2h", api=api
        )
    )
    assert result.success is True, result.markdown
    _, _, kwargs = _todos_call(api, "add_todo")
    assert kwargs["scheduled_for"] == "2h"
    assert kwargs["task"] == "Fix the a|b parser"


def test_todos_list_done_and_all_reach_past_the_active_default() -> None:
    # Both clients default to ACTIVE-only, so the handler must ask for the
    # full set and filter locally; before the #143 review round `done` was
    # always empty and `all` silently meant active.
    service = CommandService()
    api = FakeCommandApi()
    api.todos = [
        {"id": "abc12345", "task": "Water plants", "status": "pending"},
        {"id": "def67890", "task": "Old chore", "status": "done"},
    ]
    done = run(service.execute(_ctx(), "/todos list done", api=api))
    assert done.success is True, done.markdown
    assert "Old chore" in done.markdown
    _, _, kwargs = _todos_call(api, "list_todos")
    assert kwargs["filter_status"] == "all"

    everything = run(service.execute(_ctx(), "/todos list all", api=api))
    assert "TODOs (all): 2 items" in everything.markdown


def test_todos_list_rejects_an_unknown_filter() -> None:
    # The status set is a closed enum, so a typo is a bind error with the
    # valid list, not an empty "No bogus TODOs." readout.
    api = FakeCommandApi()
    result = run(CommandService().execute(_ctx(), "/todos list bogus", api=api))
    assert result.success is False
    assert "bogus" in result.markdown
    assert not [call for call in api.calls if call[0] == "list_todos"]


def test_todo_thread_current_without_an_active_thread_errors() -> None:
    # An explicit `--thread current` on a threadless surface must refuse
    # rather than silently widen (list) or rebind to the default thread
    # (edit); a BARE add still works there (the default is unguarded).
    service = CommandService()
    api = FakeCommandApi()
    api.todos = [{"id": "abc12345", "task": "T", "status": "pending"}]
    threadless = CommandContext(
        user_id="alice",
        thread_id=None,
        actor="user",
        surface="cli",
        is_admin=True,
    )

    listed = run(service.execute(threadless, "/todos list --thread current", api=api))
    assert listed.success is False
    assert "requires an active thread" in listed.markdown

    edited = run(
        service.execute(threadless, "/todos edit abc1 --thread current", api=api)
    )
    assert edited.success is False
    assert not [call for call in api.calls if call[0] == "update_todo"]

    added = run(service.execute(threadless, "/todos add Buy milk", api=api))
    assert added.success is True, added.markdown
    _, _, kwargs = _todos_call(api, "add_todo")
    assert kwargs["thread_id"] is None


def test_todos_edit_clear_words_match_the_sibling_verbs() -> None:
    # `--schedule none` and `--repeat off` clear, exactly like
    # `/todos schedule <id> clear`; before the review round the words were
    # forwarded verbatim and 400'd. The confirmation speaks prose words,
    # not raw patch keys.
    api = FakeCommandApi()
    api.todos = [{"id": "abc12345", "task": "T", "status": "pending"}]
    result = run(
        CommandService().execute(
            _ctx(), "/todos edit abc1 --schedule none --repeat off", api=api
        )
    )
    assert result.success is True, result.markdown
    _, _, patch = _todos_call(api, "update_todo")
    assert patch == {"clear_schedule": True, "clear_recurrence": True}
    assert "(repeat, schedule)" in result.markdown


def test_todos_add_keeps_the_1d_default_and_none_unschedules() -> None:
    service = CommandService()
    default_api = FakeCommandApi()
    assert run(
        service.execute(_ctx(), "/todos add Buy milk", api=default_api)
    ).success is True
    _, _, default_kwargs = _todos_call(default_api, "add_todo")
    assert default_kwargs["scheduled_for"] == "1d"

    none_api = FakeCommandApi()
    assert run(
        service.execute(_ctx(), "/todos add Buy milk --schedule none", api=none_api)
    ).success is True
    _, _, none_kwargs = _todos_call(none_api, "add_todo")
    assert none_kwargs["scheduled_for"] is None


def test_todos_add_rejects_a_bad_recurrence_before_the_api_call() -> None:
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            _ctx(), "/todos add Heartbeat --schedule 5m --repeat 30s", api=api
        )
    )
    assert result.success is False
    assert not [call for call in api.calls if call[0] == "add_todo"]


def test_todos_edit_builds_a_minimal_patch() -> None:
    api = FakeCommandApi()
    api.todos = [
        {"id": "abc12345", "task": "Old words", "status": "pending"},
    ]
    result = run(
        CommandService().execute(
            _ctx(), "/todos edit abc1 New words --status in_progress", api=api
        )
    )
    assert result.success is True, result.markdown
    _, args, patch = _todos_call(api, "update_todo")
    assert args == ("alice", "abc12345")
    assert patch == {"task": "New words", "status": "in_progress"}
    # Resolution listed every status so done TODOs stay addressable.
    _, _, list_kwargs = _todos_call(api, "list_todos")
    assert list_kwargs["filter_status"] == "all"


def test_todos_edit_without_changes_is_a_usage_error() -> None:
    api = FakeCommandApi()
    api.todos = [{"id": "abc12345", "task": "Old", "status": "pending"}]
    result = run(CommandService().execute(_ctx(), "/todos edit abc1", api=api))
    assert result.success is False
    assert "No TODO updates were provided." in result.markdown
    assert not [call for call in api.calls if call[0] == "update_todo"]


def test_todos_schedule_and_repeat_clear_arms_patch_the_clear_flags() -> None:
    service = CommandService()
    schedule_api = FakeCommandApi()
    schedule_api.todos = [{"id": "abc12345", "task": "T", "status": "pending"}]
    cleared = run(
        service.execute(_ctx(), "/todos schedule abc1 clear", api=schedule_api)
    )
    assert cleared.success is True, cleared.markdown
    _, _, schedule_patch = _todos_call(schedule_api, "update_todo")
    assert schedule_patch == {"clear_schedule": True}

    repeat_api = FakeCommandApi()
    repeat_api.todos = [{"id": "abc12345", "task": "T", "status": "pending"}]
    cleared = run(service.execute(_ctx(), "/todos repeat abc1 clear", api=repeat_api))
    assert cleared.success is True, cleared.markdown
    _, _, repeat_patch = _todos_call(repeat_api, "update_todo")
    assert repeat_patch == {"clear_recurrence": True}


def test_todos_repeat_rejects_a_bad_interval_before_the_api_call() -> None:
    api = FakeCommandApi()
    api.todos = [{"id": "abc12345", "task": "T", "status": "pending"}]
    result = run(CommandService().execute(_ctx(), "/todos repeat abc1 30s", api=api))
    assert result.success is False
    assert not [call for call in api.calls if call[0] == "update_todo"]


def test_todo_prefix_resolution_refuses_ambiguity() -> None:
    api = FakeCommandApi()
    api.todos = [
        {"id": "abc12345", "task": "First", "status": "pending"},
        {"id": "abc99999", "task": "Second", "status": "done"},
    ]
    ambiguous = run(CommandService().execute(_ctx(), "/todos complete abc", api=api))
    assert ambiguous.success is False
    assert "Ambiguous TODO id 'abc' matches 2" in ambiguous.markdown
    assert "First" in ambiguous.markdown and "Second" in ambiguous.markdown
    assert not [call for call in api.calls if call[0] == "complete_todo"]

    # An empty id token is refused too: `startswith("")` matches
    # everything, so a one-item store would otherwise "resolve".
    empty = run(CommandService().execute(_ctx(), '/todos delete ""', api=api))
    assert empty.success is False
    assert "id (or unique id prefix) is required" in empty.markdown
    assert not [call for call in api.calls if call[0] == "delete_todo"]

    # A unique prefix resolves, and a done TODO is addressable (all-status
    # resolution): the pre-#143 first-match pick listed active only.
    deleted = run(CommandService().execute(_ctx(), "/todos delete abc9", api=api))
    assert deleted.success is True, deleted.markdown
    _, args, _ = _todos_call(api, "delete_todo")
    assert args == ("alice", "abc99999")


def test_todos_list_thread_filter_maps_current_to_the_active_thread() -> None:
    api = FakeCommandApi()
    result = run(
        CommandService().execute(_ctx(), "/todos list --thread current", api=api)
    )
    assert result.success is True, result.markdown
    _, _, kwargs = _todos_call(api, "list_todos")
    assert kwargs["thread_id"] == "thread-1"


def test_singular_todo_spellings_alias_the_todos_family() -> None:
    service = CommandService()
    api = FakeCommandApi()
    api.todos = [{"id": "abc12345", "task": "T", "status": "pending"}]

    listed = run(service.execute(_ctx(), "/todo", api=api))
    assert listed.success is True, listed.markdown
    assert [call for call in api.calls if call[0] == "list_todos"]

    done = run(service.execute(_ctx(), "/todo done abc1", api=api))
    assert done.success is True, done.markdown
    assert [call[1] for call in api.calls if call[0] == "complete_todo"] == [
        ("alice", "abc12345")
    ]

    recur = run(service.execute(_ctx(), "/todo recurrence abc1 clear", api=api))
    assert recur.success is True, recur.markdown
    assert [call for call in api.calls if call[0] == "update_todo"]

    # The retired CLI family's --yes confirm flag stays accepted as a no-op.
    removed = run(service.execute(_ctx(), "/todo rm abc1 --yes", api=api))
    assert removed.success is True, removed.markdown
    assert [call[1] for call in api.calls if call[0] == "delete_todo"] == [
        ("alice", "abc12345")
    ]


def test_status_and_context_reject_stray_words() -> None:
    service = CommandService()
    api = FakeCommandApi()

    status = run(service.execute(_ctx(), "/status now", api=api))
    assert status.success is False
    assert "Unexpected argument `now`" in status.markdown

    context = run(service.execute(_ctx(), "/context full", api=api))
    assert context.success is False
    assert "Unexpected argument `full`" in context.markdown

    assert api.calls == []


def test_artifacts_root_rejects_arguments_and_recent_coerces_its_limit() -> None:
    service = CommandService()
    api = FakeCommandApi()

    stray = run(service.execute(_ctx(), "/artifacts everything", api=api))
    assert stray.success is False
    assert "Unexpected argument `everything`" in stray.markdown

    bad_limit = run(service.execute(_ctx(), "/artifacts recent lots", api=api))
    assert bad_limit.success is False
    assert "limit must be an integer, got `lots`" in bad_limit.markdown

    assert not [call for call in api.calls if call[0] == "get_history"]


def test_team_show_binds_a_quoted_multi_word_name() -> None:
    # The hand parser read the raw tail, so the quotes came through and never
    # matched; the rest param re-joins the split tokens instead.
    api = FakeCommandApi()
    api.thread_teams = [
        {"id": "team-ops-1", "name": "Ops Crew", "description": "", "thread_ids": []}
    ]
    result = run(CommandService().execute(_ctx(), '/team show "Ops Crew"', api=api))

    assert result.success is True, result.markdown
    assert "Team: Ops Crew" in result.markdown


# ── #131 rename wave A: the old spellings still work as aliases ──────────────


def test_every_retired_spelling_resolves_to_the_command_that_replaced_it() -> None:
    """The compatibility contract of the rename wave, in one table.

    A rename is only safe because the old whole path stays registered as an
    alias, and alias expansion substitutes the TARGET's canonical path, which
    is what lets a root become a subcommand (`/tasks`) or a subcommand become
    a root (`/settings show`). If a row here stops resolving, a user's muscle
    memory broke.
    """
    service = CommandService()
    for old, new_id in (
        ("account current", "account.show"),
        ("account_current", "account.show"),
        ("artifacts recent", "artifacts.list"),
        ("branch", "thread.branch"),
        ("fork", "thread.branch"),
        ("config", "settings"),
        ("config show", "settings"),
        ("config get", "settings.get"),
        ("config set", "settings.set"),
        ("hook log", "hook.history"),
        ("mcp remove", "mcp.delete"),
        ("memory forget", "memory.delete"),
        ("models", "model.list"),
        ("settings show", "settings"),
        ("skills inspect", "skills.show"),
        ("skills off", "skills.disable"),
        ("tasks", "todos.list"),
        ("thread info", "thread.show"),
        ("thread new", "thread.create"),
        ("tools category", "tools.list"),
        ("tools enabled", "tools.list"),
        ("tools core", "tools.list"),
        ("tools optional", "tools.list"),
    ):
        resolved = service.find_command(old)
        assert resolved is not None, old
        assert resolved.id == new_id, old


def test_retired_root_spellings_still_execute_their_replacement() -> None:
    """One end-to-end run per fold SHAPE, not per row.

    Resolution alone would not catch an alias that resolves but strands its
    arguments, which is the failure mode when a path grows or shrinks a token.
    """
    service = CommandService()
    api = FakeCommandApi()

    # Root becomes a subcommand, and its argument still lands on the filter.
    tasks = run(service.execute(_ctx(), "/tasks all", api=api))
    assert tasks.success is True, tasks.markdown
    assert "TODOs (all): 2 items" in tasks.markdown

    # Cross-family: /config carries its key and value to the settings handler.
    got = run(service.execute(_ctx(), "/config get llm_model", api=api))
    assert got.success is True, got.markdown
    assert "llm_model = gpt-test" in got.markdown

    # Subcommand becomes a root: the show verb folded into bare /settings.
    shown = run(service.execute(_ctx(), "/config show", api=api))
    assert shown.success is True, shown.markdown
    assert "provider: openai" in shown.markdown

    # Verb rename with a trailing value.
    created = run(service.execute(_ctx(), "/thread new Bridge Test", api=api))
    assert created.success is True, created.markdown
    assert (
        "create_thread",
        ("alice",),
        {"thread_id": None, "title": "Bridge Test"},
    ) in api.calls

    # The one alias that bridges a FOLDED value: the raw tail becomes the
    # filter argument, so a category still filters.
    filtered = run(service.execute(_ctx(), "/tools category web", api=api))
    assert filtered.success is True, filtered.markdown
    assert "Tools in category 'web'" in filtered.markdown
    assert "browser" in filtered.markdown


def test_injected_tools_aliases_restore_the_filtered_views() -> None:
    """`/tools core|optional` are INJECTED aliases (#133): the expansion
    carries the filter token a plain alias structurally cannot, so the old
    spellings render their OWN views again. History: pre-#131 they were
    separate commands; the #131 fold left them as wrong-view aliases (the
    review caught them rendering enabled while claiming core); the interim
    fix dropped them to an honest failure; injection restores them.
    """
    service = CommandService()
    api = FakeCommandApi()

    core = run(service.execute(_ctx(), "/tools core", api=api))
    assert core.success is True, core.markdown
    assert "Core Tools" in core.markdown

    optional = run(service.execute(_ctx(), "/tools optional", api=api))
    assert optional.success is True, optional.markdown
    assert "Optional Tools" in optional.markdown

    # The flat chat-platform twins carry the same injection.
    flat_core = run(service.execute(_ctx(), "/tools_core", api=api))
    assert "Core Tools" in flat_core.markdown

    # `enabled` stays a PLAIN alias (it is the default view), and the
    # explicit filters still reach their own views.
    bridged = run(service.execute(_ctx(), "/tools enabled", api=api))
    assert "Enabled Tools on this thread" in bridged.markdown
    core_explicit = run(service.execute(_ctx(), "/tools list core", api=api))
    assert "Core Tools" in core_explicit.markdown


# ── #157 vault-consistent posture: agent-issued sensitive settings writes ───
# These reuse the module's existing _agent_ctx helper (surface="agent"), the
# faithful shape of the real slash_command tool path.


def test_agent_sensitive_settings_write_fires_owner_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent-actor write to a sensitive key succeeds AND alerts the owner.

    Vault posture (#157): containment is loudness and reversibility, not
    blocking. The alert rides send_owner_alert (not silenceable by thread
    notification levels).
    """
    import nymeria.core.notification_dispatch as dispatch_mod

    alerts: list[dict] = []
    monkeypatch.setattr(
        dispatch_mod,
        "send_owner_alert",
        lambda message, settings, *, user_id, thread_id="", task_id=None: alerts.append(
            {"message": message, "user_id": user_id, "thread_id": thread_id}
        ),
    )
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            _agent_ctx(), "/env set NYMERIA_PUBLIC_URL https://x.example", api=api
        )
    )
    assert result.success is True
    assert len(alerts) == 1
    assert "nymeria_public_url" in alerts[0]["message"]
    assert alerts[0]["user_id"] == "alice"
    assert alerts[0]["thread_id"] == "thread-1"

    # The same write by a human actor alerts nobody.
    alerts.clear()
    result = run(
        CommandService().execute(
            _ctx(), "/env set NYMERIA_PUBLIC_URL https://x.example", api=api
        )
    )
    assert result.success is True
    assert alerts == []


def test_agent_secret_settings_alert_withholds_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alert names a secret key but never carries its value."""
    import nymeria.core.notification_dispatch as dispatch_mod

    alerts: list[str] = []
    monkeypatch.setattr(
        dispatch_mod,
        "send_owner_alert",
        lambda message, settings, *, user_id, thread_id="", task_id=None: alerts.append(
            message
        ),
    )
    result = run(
        CommandService().execute(
            _agent_ctx(),
            "/env set CLIPROXY_MANAGEMENT_KEY sk-super-secret-xyz",
            api=FakeCommandApi(),
        )
    )
    assert result.success is True
    assert len(alerts) == 1
    assert "cliproxy_management_key" in alerts[0]
    assert "sk-super-secret-xyz" not in alerts[0]


def test_agent_cannot_write_gate_disabling_settings() -> None:
    """hooks_enabled is agent-blocked: the gate is not removable by the thing
    it gates (the system-credentials carve-out analogue). Human admins are
    unaffected."""
    api = FakeCommandApi()
    result = run(
        CommandService().execute(_agent_ctx(), "/env set HOOKS_ENABLED false", api=api)
    )
    assert result.success is False
    assert "hooks_enabled" in result.markdown
    assert not [c for c in api.calls if c[0] == "update_settings"]

    human = run(
        CommandService().execute(_ctx(), "/env set HOOKS_ENABLED false", api=api)
    )
    assert human.success is True
    assert [c for c in api.calls if c[0] == "update_settings"]


def test_agent_settings_alert_failure_never_blocks_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alert dispatch is containment, not a gate: a raising dispatcher must
    not fail the command."""
    import nymeria.core.notification_dispatch as dispatch_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("push channel down")

    monkeypatch.setattr(dispatch_mod, "send_owner_alert", _boom)
    result = run(
        CommandService().execute(
            _agent_ctx(), "/env set LLM_BASE_URL https://proxy.example", api=FakeCommandApi()
        )
    )
    assert result.success is True
    assert "llm_base_url set to" in result.markdown


def test_settings_set_gate_block_and_alias_spellings() -> None:
    """The block holds on /settings set too: the invariant is per-KEY, not
    per-command-spelling."""
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            _agent_ctx(), "/settings set hooks_enabled false", api=api
        )
    )
    assert result.success is False
    assert "hooks_enabled" in result.markdown
    assert not [c for c in api.calls if c[0] == "update_settings"]


def test_agent_nonsensitive_write_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alerting on every agent settings write would destroy the signal: a
    non-listed key must not alert."""
    import nymeria.core.notification_dispatch as dispatch_mod

    alerts: list[str] = []
    monkeypatch.setattr(
        dispatch_mod,
        "send_owner_alert",
        lambda message, settings, *, user_id, thread_id="", task_id=None: alerts.append(
            message
        ),
    )
    result = run(
        CommandService().execute(
            _agent_ctx(), "/env set LLM_MODEL gpt-test", api=FakeCommandApi()
        )
    )
    assert result.success is True
    assert alerts == []


def test_agent_unapplied_sensitive_write_does_not_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No alert for a write the applier dropped: the alert reports changes,
    not attempts."""
    import nymeria.core.notification_dispatch as dispatch_mod

    alerts: list[str] = []
    monkeypatch.setattr(
        dispatch_mod,
        "send_owner_alert",
        lambda message, settings, *, user_id, thread_id="", task_id=None: alerts.append(
            message
        ),
    )
    result = run(
        CommandService().execute(
            _agent_ctx(),
            "/env set NYMERIA_PUBLIC_URL none",
            api=_UnappliedCommandApi(),
        )
    )
    assert result.success is False
    assert alerts == []


def test_background_url_commands_alert_like_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/background set-url and /background clear write an alert-listed key
    and must be exactly as loud as /env set LLM_BACKGROUND_BASE_URL: the
    control keys on which SETTING changed, never on the spelling."""
    import nymeria.core.notification_dispatch as dispatch_mod

    alerts: list[str] = []
    monkeypatch.setattr(
        dispatch_mod,
        "send_owner_alert",
        lambda message, settings, *, user_id, thread_id="", task_id=None: alerts.append(
            message
        ),
    )
    api = FakeCommandApi()
    result = run(
        CommandService().execute(
            _agent_ctx(), "/background set-url https://proxy.example/v1", api=api
        )
    )
    assert result.success is True
    assert len(alerts) == 1
    assert "llm_background_base_url" in alerts[0]

    alerts.clear()
    result = run(CommandService().execute(_agent_ctx(), "/background clear", api=api))
    assert result.success is True
    assert len(alerts) == 1
    assert "llm_background_base_url" in alerts[0]
