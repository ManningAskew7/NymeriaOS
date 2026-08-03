"""Tests for the /mcp backend commands added in Phase 2a.2."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
import nymeria.core.agent as agent_module
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
)


class _FakeServer(SimpleNamespace):
    pass


def _make_server(
    server_id: str,
    *,
    name: str = "",
    enabled: bool = True,
    install_status: str = "ready",
    discovered_tools: list[Any] | None = None,
    last_error: str = "",
    install_logs: list[str] | None = None,
    install_plan: dict[str, Any] | None = None,
) -> _FakeServer:
    return _FakeServer(
        id=server_id,
        name=name or server_id,
        enabled=enabled,
        install_status=install_status,
        discovered_tools=discovered_tools or [],
        last_error=last_error,
        install_logs=install_logs or [],
        install_plan=install_plan,
    )


class _FakeMCPRegistry:
    def __init__(self, servers: list[_FakeServer] | None = None) -> None:
        self.servers: dict[str, _FakeServer] = {s.id: s for s in (servers or [])}
        self.discover_calls: list[str] = []
        self.test_calls: list[str] = []
        self.deleted: list[str] = []
        self.saved: list[_FakeServer] = []
        self.discover_result: list[Any] | None = None
        self.discover_error: Exception | None = None
        self.test_result: dict[str, Any] = {"status": "ok", "tools_count": 2}

    def get_all_servers(self) -> list[_FakeServer]:
        return list(self.servers.values())

    def get_server(self, server_id: str):
        return self.servers.get(server_id)

    def delete_server(self, server_id: str) -> bool:
        if server_id in self.servers:
            self.deleted.append(server_id)
            del self.servers[server_id]
            return True
        return False

    def save_server(self, defn) -> None:
        self.saved.append(defn)
        self.servers[defn.id] = defn

    def discover_tools(self, server_id: str):
        self.discover_calls.append(server_id)
        if self.discover_error is not None:
            raise self.discover_error
        return self.discover_result if self.discover_result is not None else []

    def test_connection(self, server_id: str) -> dict[str, Any]:
        self.test_calls.append(server_id)
        return dict(self.test_result)


class _FakeAccountsRepo:
    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id,
            email=f"{user_id}@example.test",
            display_name=user_id,
            role="admin",
        )


class _FakeAgentWithMCPReload(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(accounts_repo=_FakeAccountsRepo())
        self.reloads = 0

    def reload_mcp_server_tools(self) -> None:
        self.reloads += 1


@pytest.fixture
def patched_registry(monkeypatch: pytest.MonkeyPatch):
    """Patch ``get_mcp_server_registry`` so each test owns its own fake."""

    def install(registry: _FakeMCPRegistry) -> _FakeMCPRegistry:
        monkeypatch.setattr(
            "nymeria.core.mcp_servers.get_mcp_server_registry",
            lambda: registry,
        )
        return registry

    return install


@pytest.fixture(autouse=True)
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    """Patch ``get_current_agent`` so handlers can reload tools cleanly."""

    def install(agent) -> Any:
        monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)
        return agent

    install(_FakeAgentWithMCPReload())
    return install


def _ctx(is_admin: bool = True, thread_id: str | None = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id or "",
        actor="user",
        surface="cli",
        is_admin=is_admin,
    )


def test_mcp_list_renders_a_markdown_table(patched_registry, patched_agent) -> None:
    registry = patched_registry(_FakeMCPRegistry([
        _make_server("alpha", install_status="ready", discovered_tools=[{}, {}, {}]),
        _make_server("beta", enabled=False, install_status="failed"),
    ]))
    patched_agent(_FakeAgentWithMCPReload())

    result = run(CommandService().execute(_ctx(), "/mcp list"))

    assert result.success is True, result.markdown
    assert "alpha" in result.markdown
    assert "beta" in result.markdown
    assert "| ID | State | Tools | Name |" in result.markdown
    assert registry.saved == []


def test_mcp_list_reports_empty_when_no_servers(patched_registry) -> None:
    patched_registry(_FakeMCPRegistry([]))

    result = run(CommandService().execute(_ctx(), "/mcp list"))
    assert result.success is True
    assert "No MCP servers configured" in result.markdown


def test_mcp_status_shows_server_details_when_id_given(
    patched_registry,
) -> None:
    patched_registry(_FakeMCPRegistry([
        _make_server(
            "alpha",
            name="Alpha Server",
            enabled=True,
            install_status="ready",
            discovered_tools=[{}, {}],
            install_logs=["installed deps", "discovered 2 tools"],
        ),
    ]))

    result = run(CommandService().execute(_ctx(), "/mcp status alpha"))

    assert result.success is True, result.markdown
    assert "alpha" in result.markdown
    assert "Alpha Server" in result.markdown
    assert "Recent logs" in result.markdown


def test_mcp_status_reports_unknown_id(patched_registry) -> None:
    patched_registry(_FakeMCPRegistry([]))

    result = run(CommandService().execute(_ctx(), "/mcp status missing"))
    assert result.success is False
    assert "not found" in result.markdown


def test_mcp_logs_returns_last_n_lines(patched_registry) -> None:
    patched_registry(_FakeMCPRegistry([
        _make_server(
            "alpha",
            install_logs=[f"line {i}" for i in range(50)],
        ),
    ]))

    result = run(CommandService().execute(_ctx(), "/mcp logs alpha 5"))
    assert result.success is True
    assert "line 49" in result.markdown
    assert "line 45" in result.markdown
    assert "line 30" not in result.markdown


def test_mcp_discover_calls_registry_and_reloads_agent(
    patched_registry, patched_agent
) -> None:
    registry = patched_registry(_FakeMCPRegistry([_make_server("alpha")]))
    registry.discover_result = [object(), object(), object()]
    agent = patched_agent(_FakeAgentWithMCPReload())

    result = run(CommandService().execute(_ctx(), "/mcp discover alpha"))

    assert result.success is True, result.markdown
    assert registry.discover_calls == ["alpha"]
    assert agent.reloads == 1
    assert "Discovered 3 tools" in result.markdown


def test_mcp_discover_records_failure_on_registry_server(
    patched_registry, patched_agent
) -> None:
    server = _make_server("alpha")
    registry = patched_registry(_FakeMCPRegistry([server]))
    registry.discover_error = RuntimeError("boom")
    patched_agent(_FakeAgentWithMCPReload())

    result = run(CommandService().execute(_ctx(), "/mcp discover alpha"))

    assert result.success is False
    assert "boom" in result.markdown
    assert server.install_status == "failed"
    assert server.enabled is False
    assert registry.saved[-1] is server


def test_mcp_test_reports_success_with_tool_count(patched_registry) -> None:
    registry = patched_registry(_FakeMCPRegistry([_make_server("alpha")]))
    registry.test_result = {"status": "ok", "tools_count": 5}

    result = run(CommandService().execute(_ctx(), "/mcp test alpha"))

    assert result.success is True, result.markdown
    assert "5 tools" in result.markdown
    assert registry.test_calls == ["alpha"]


def test_mcp_test_reports_error_when_status_is_failure(patched_registry) -> None:
    registry = patched_registry(_FakeMCPRegistry([_make_server("alpha")]))
    registry.test_result = {"status": "failed", "error": "auth denied"}

    result = run(CommandService().execute(_ctx(), "/mcp test alpha"))

    assert result.success is False
    assert "auth denied" in result.markdown


def test_mcp_delete_removes_and_reloads_agent(
    patched_registry, patched_agent
) -> None:
    registry = patched_registry(_FakeMCPRegistry([_make_server("alpha")]))
    agent = patched_agent(_FakeAgentWithMCPReload())

    result = run(CommandService().execute(_ctx(), "/mcp delete alpha"))

    assert result.success is True, result.markdown
    assert registry.deleted == ["alpha"]
    assert agent.reloads == 1


def test_mcp_delete_reports_unknown_id(patched_registry, patched_agent) -> None:
    patched_registry(_FakeMCPRegistry([]))
    patched_agent(_FakeAgentWithMCPReload())

    result = run(CommandService().execute(_ctx(), "/mcp delete missing"))
    assert result.success is False
    assert "not found" in result.markdown


def test_mcp_retry_runs_discovery_when_plan_present(
    monkeypatch: pytest.MonkeyPatch, patched_registry, patched_agent
) -> None:
    server = _make_server(
        "alpha",
        install_status="failed",
        install_plan={
            "source_type": "url",
            "runtime_type": "python",
            "risk_level": "medium",
            "confirmation_required": False,
            "parsed_summary": "",
        },
    )
    registry = patched_registry(_FakeMCPRegistry([server]))
    registry.discover_result = [object(), object()]
    agent = patched_agent(_FakeAgentWithMCPReload())

    result = run(CommandService().execute(_ctx(), "/mcp retry alpha"))

    assert result.success is True, result.markdown
    assert "discovered 2 tool(s)" in result.markdown
    assert "runtime: python" in result.markdown
    assert agent.reloads == 1
    assert server.install_status == "ready"
    assert server.last_error == ""


def test_mcp_retry_rejects_server_without_plan(patched_registry) -> None:
    patched_registry(_FakeMCPRegistry([_make_server("alpha", install_plan=None)]))

    result = run(CommandService().execute(_ctx(), "/mcp retry alpha"))
    assert result.success is False
    assert "no install plan" in result.markdown


# ── #129 wave 2a: declared params ────────────────────────────────────────────


def test_mcp_logs_rejects_a_non_integer_limit(patched_registry) -> None:
    # The old parser swallowed a junk limit and silently used 20; the declared
    # int positional says so instead of showing the wrong number of lines.
    registry = patched_registry(_FakeMCPRegistry([
        _make_server("alpha", install_logs=[f"line {i}" for i in range(50)]),
    ]))

    result = run(CommandService().execute(_ctx(), "/mcp logs alpha five"))

    assert result.success is False
    assert "limit must be an integer, got `five`" in result.markdown
    assert registry.saved == []
    assert "line 49" not in result.markdown


def test_mcp_logs_defaults_the_limit_to_twenty(patched_registry) -> None:
    patched_registry(_FakeMCPRegistry([
        _make_server("alpha", install_logs=[f"line {i}" for i in range(50)]),
    ]))

    result = run(CommandService().execute(_ctx(), "/mcp logs alpha"))

    assert result.success is True, result.markdown
    assert "(last 20 of 50)" in result.markdown
    assert "line 30" in result.markdown
    assert "line 29" not in result.markdown


def test_mcp_list_rejects_arguments(patched_registry) -> None:
    registry = patched_registry(_FakeMCPRegistry([_make_server("alpha")]))

    result = run(CommandService().execute(_ctx(), "/mcp list alpha"))

    assert result.success is False
    assert "Unexpected argument `alpha`" in result.markdown
    assert "| ID | State | Tools | Name |" not in result.markdown
    assert registry.saved == []


def test_mcp_delete_rejects_a_second_server_id(patched_registry) -> None:
    registry = patched_registry(
        _FakeMCPRegistry([_make_server("alpha"), _make_server("beta")])
    )

    result = run(CommandService().execute(_ctx(), "/mcp delete alpha beta"))

    assert result.success is False
    assert "Unexpected argument `beta`" in result.markdown
    assert registry.deleted == []


def test_mcp_discover_rejects_an_unknown_option(patched_registry) -> None:
    registry = patched_registry(_FakeMCPRegistry([_make_server("alpha")]))

    result = run(CommandService().execute(_ctx(), "/mcp discover alpha --force"))

    assert result.success is False
    assert "Unknown option `--force`" in result.markdown
    assert registry.discover_calls == []


def test_mcp_root_lists_and_guides_a_typo(patched_registry) -> None:
    patched_registry(_FakeMCPRegistry([_make_server("alpha")]))

    listed = run(CommandService().execute(_ctx(), "/mcp"))
    assert listed.success is True, listed.markdown
    assert "| ID | State | Tools | Name |" in listed.markdown

    # The root is a strict zero-arg command, so a mistyped verb is rejected by
    # the binder and the dispatcher layers the family guidance on top.
    typo = run(CommandService().execute(_ctx(), "/mcp lgos"))
    assert typo.success is False
    assert "Unexpected argument `lgos`" in typo.markdown
    assert "Did you mean `/mcp logs`?" in typo.markdown
    assert "Valid subcommands: delete, discover, list, logs, retry, status, test." in typo.markdown
    assert "Usage: `/mcp`." in typo.markdown
    assert "See `/help mcp`." in typo.markdown
