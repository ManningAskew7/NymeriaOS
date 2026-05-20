from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nymeria.api.routers.commands import create_commands_router
from nymeria.core.accounts import AuthenticatedUser
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandHttpClient,
    CommandService,
)


def run(coro):
    return asyncio.run(coro)


class FakeCommandApi:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

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
                    "category": "core",
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
        return {"enabled_tools": [], "disabled_tools": []}

    async def get_env_var(
        self,
        key: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("get_env_var", (key,), {"user_id": user_id}))
        return {"name": key, "value": "secret-value"}


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

    async def fake_prune_now(thread_id: str, user_id: str) -> dict[str, Any]:
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
