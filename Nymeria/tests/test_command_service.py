from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandHttpClient,
    CommandService,
    _CommandBackendUser,
)
from nymeria.core.todo_manager import TodoManager


class FakeCommandApi:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.memories = [{"key": "a", "value": "12345"}]
        self.env_set_keys: set[str] = set()
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
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "llm_base_url": None,
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

    async def list_todos(self, user_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_todos", (user_id,), {}))
        return [
            {"status": "pending"},
            {"status": "in_progress"},
        ]

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
    tools_core = next(cmd for cmd in admin_desktop if cmd.id == "tools.core")
    compact = next(cmd for cmd in admin_desktop if cmd.id == "compact")

    assert tools_core.name == "tools core"
    assert tools_core.path == ["tools", "core"]
    assert "/tools_core" in tools_core.aliases
    assert tools_core.scope == "global"
    assert tools_core.agent_allowed is True
    assert tools_core.requires_thread is False
    assert tools_core.execution_kind == "command"

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
    assert "config set" not in non_admin
    assert "config show" in non_admin

    agent = _command_names(service, actor="agent", surface="agent", is_admin=True)
    assert "compact" not in agent
    assert "tools core" in agent


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
            aliases=("tools core",),
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
            "/tools_core",
            api=api,
        )
    )

    assert result.success is True
    assert result.command == "tools core"
    assert result.level == "success"
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
    global_set = run(service.execute(ctx, "/memory limit global 12000", api=api))
    thread_set = run(service.execute(ctx, "/memory limit thread 6000", api=api))
    thread_clear = run(service.execute(ctx, "/memory limit thread inherit", api=api))

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
    global_on = run(service.execute(ctx, "/sequential-tools global on", api=api))

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
    # The global subcommand must not require a thread; thread-scoped subcommands do.
    service = CommandService()
    api = FakeCommandApi()
    ctx = CommandContext(
        user_id="alice",
        thread_id=None,
        actor="user",
        surface="cli",
        is_admin=True,
    )

    global_off = run(service.execute(ctx, "/sequential-tools global off", api=api))
    assert global_off.success is True
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "sequential_tool_execution": False},
    ) in api.calls

    needs_thread = run(service.execute(ctx, "/sequential-tools on", api=api))
    assert needs_thread.success is False


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


def test_think_rejects_unknown_tokens() -> None:
    api = FakeCommandApi()

    result = _run_think(api, "/think sideways")

    assert result.success is False
    assert "Usage:" in result.markdown
    assert not [call for call in api.calls if call[0].startswith("update_")]


# ── /settings (delegating alias of the /config family) ─────────────────────


def _run_command(api: FakeCommandApi, command: str, *, is_admin: bool = True):
    return run(
        CommandService().execute(
            CommandContext(
                user_id="alice",
                thread_id="thread-1",
                actor="user",
                surface="cli",
                is_admin=is_admin,
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
    assert "Usage: /settings" in unknown.markdown


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


def test_provider_list_groups_by_tier_without_secrets() -> None:
    api = FakeCommandApi()
    api.env_set_keys = {"anthropic_api_key"}

    result = _run_command(api, "/provider list")

    assert result.success is True
    assert "[NATIVE]" in result.markdown
    assert "anthropic" in result.markdown
    assert "authenticated" in result.markdown
    assert "secret" not in result.markdown.lower()


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
    assert "Unknown provider" in unknown.markdown

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
    assert (
        "update_settings",
        (),
        {"user_id": "alice", "llm_provider": "anthropic"},
    ) in api.calls


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

    group = run(
        service.execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/tools",
            api=api,
        )
    )
    assert group.success is False
    assert "`/tools` requires a subcommand" in group.markdown
    assert "core" in group.markdown

    unknown_subcommand = run(
        service.execute(
            CommandContext(user_id="alice", actor="user", surface="desktop"),
            "/tools nope",
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
            "/help",
            api=FakeCommandApi(),
        )
    )

    assert result.success is True
    assert result.markdown.count("| `/tools core` |") == 1
    assert "| `/tools` |" not in result.markdown
    assert "| `/env get` |" not in result.markdown
    assert "| `/config show` |" in result.markdown


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
    tools_core = next(item for item in data if item["id"] == "tools.core")
    assert tools_core["path"] == ["tools", "core"]
    assert tools_core["execution_kind"] == "command"
    assert tools_core["requires_thread"] is False
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
            "command": "/tools_core",
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
        "command": "tools core",
        "level": "success",
        "data": None,
    }
    assert api.closed is False


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
    assert len(service._commands) == 130
    assert sum(cmd.executable for cmd in service._commands.values()) == 113

    help_cmd = by_name["help"]
    assert help_cmd.category == "General"
    assert help_cmd.aliases == (("h",),)

    config_set = by_name["config set"]
    assert (config_set.requires_admin, config_set.mutates_state) == (True, True)
    assert config_set.danger_level == "dangerous"

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
    assert by_name["branch"].aliases == (("fork",),)

    assert by_name["mcp remove"].aliases == (
        ("mcp_remove",),
        ("mcp_rm",),
        ("mcp_delete",),
    )
    assert by_name["tools core"].aliases == (("tools_core",), ("tools", "list_core"))

    # Lifecycle-hook commands: mutating subcommands are agent-gated (the agent
    # authors via the hook_config tool) and hidden from chat surfaces, while the
    # bare `/hook` read command stays available everywhere.
    assert by_name["hook"].agent_allowed is True
    assert by_name["hook"].aliases == (("hooks",),)
    hook_create = by_name["hook create"]
    assert (hook_create.agent_allowed, hook_create.mutates_state) == (False, True)
    assert "telegram" not in hook_create.surfaces
    assert by_name["hook delete"].danger_level == "dangerous"

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


class _ModelCatalogCommandApi(FakeCommandApi):
    async def list_available_models(self) -> list[dict[str, Any]]:
        self.calls.append(("list_available_models", (), {}))
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
    assert "Usage: /usage" in result.markdown


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
    assert "Usage: /usage session" in extra.markdown


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
