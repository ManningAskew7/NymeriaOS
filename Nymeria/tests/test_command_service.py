from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cli_fixtures import run
from nymeria.api.routers.commands import create_commands_router
from nymeria.core.accounts import AuthenticatedUser
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandHttpClient,
    CommandService,
)


class FakeCommandApi:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.memories = [{"key": "a", "value": "12345"}]
        self.thread_config = {"enabled_tools": [], "disabled_tools": [], "memory_char_limit": None}

    async def close(self) -> None:
        self.closed = True

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

    def abort_with_cascade(self, thread_id: str) -> None:
        self.aborted.append(thread_id)

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


def test_think_on_clears_persisted_off_effort() -> None:
    """/think on after /think off must clear effort="off" (explicit null)."""
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
            "/think on",
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
    assert "claude-fable-5 supports: low, medium, high, xhigh, max" in result.markdown


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
