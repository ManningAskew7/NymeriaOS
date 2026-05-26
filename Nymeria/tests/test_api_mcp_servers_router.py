"""MCP Servers API route extraction regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nymeria.api.routers import mcp_servers as mcp_servers_router
from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools.definitions.mcp_schema import MCPDiscoveredTool, MCPServerDefinition


class FakeMCPServerRegistry:
    def __init__(self):
        self.servers: dict[str, MCPServerDefinition] = {}
        self.discovered = [
            MCPDiscoveredTool(
                name="search",
                description="Search through the MCP server",
                input_schema={"type": "object"},
            )
        ]
        self.discover_calls: list[str] = []

    def get_all_servers(self):
        return list(self.servers.values())

    def get_server(self, server_id: str):
        return self.servers.get(server_id)

    def save_server(self, defn: MCPServerDefinition):
        self.servers[defn.id] = defn
        return Path(f"/tmp/{defn.id}.json")

    def delete_server(self, server_id: str) -> bool:
        return self.servers.pop(server_id, None) is not None

    def discover_tools(self, server_id: str):
        self.discover_calls.append(server_id)
        self.servers[server_id].discovered_tools = list(self.discovered)
        return list(self.discovered)

    def test_connection(self, server_id: str):
        return {
            "status": "ok",
            "server_id": server_id,
            "tools_count": len(self.discovered),
            "tool_names": [tool.name for tool in self.discovered],
        }


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.profile_manager = UserProfileManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.synced_tools = 0
        self.mcp_reload_count = 0
        self.invalidated_thread_ids: list[str] = []
        self.default_graph_rebuilds = 0

    def sync_agent_tools(self):
        self.synced_tools += 1

    def reload_mcp_server_tools(self):
        self.mcp_reload_count += 1

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated_thread_ids.append(thread_id)

    def _rebuild_default_graphs(self):
        self.default_graph_rebuilds += 1


def _client(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
    *,
    registry: FakeMCPServerRegistry | None = None,
) -> tuple[Any, FakeAgent, FakeMCPServerRegistry]:
    registry = registry or FakeMCPServerRegistry()
    monkeypatch.setattr(
        mcp_servers_router,
        "get_mcp_server_registry",
        lambda: registry,
    )
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    return api_client_builder.client(agent, settings), agent, registry


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def test_mcp_servers_are_admin_only(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, _registry = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "owner", role="user")

    response = client.get(
        "/mcp-servers",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 403


def test_mcp_server_create_act_as_attaches_discovered_tools_to_target_thread(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, registry = _client(tmp_path, api_client_builder, monkeypatch)
    admin_token = _create_user(agent, "admin", role="admin")
    _create_user(agent, "alice")
    headers = api_client_builder.auth(admin_token, **{"X-Nymeria-Act-As": "alice"})

    response = client.post(
        "/mcp-servers?thread_id=thread-1",
        headers=headers,
        json={
            "id": "example",
            "name": "Example MCP",
            "description": "Example server",
            "server_command": "npx",
            "server_args": ["-y", "@example/mcp"],
            "env_vars": {"MODE": "test"},
            "working_directory": "/tmp",
            "enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == "thread-1"
    assert body["tool_names"] == ["mcp__example__search"]
    assert body["tool_display_names"] == ["Example MCP / search"]
    assert registry.discover_calls == ["example"]
    assert agent.mcp_reload_count == 1
    assert agent.invalidated_thread_ids == ["thread-1"]
    assert agent.accounts_repo.get_thread_owner("thread-1") == "alice"
    config = agent.thread_config_manager.get_config("thread-1")
    assert config is not None
    assert config.enabled_tools == ["mcp__example__search"]


def test_mcp_install_preview_parses_without_discovery(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, registry = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "admin", role="admin")

    from nymeria.core import mcp_runtime

    settings = api_client_builder.settings(tmp_path)
    monkeypatch.setattr(mcp_runtime, "get_settings", lambda: settings)

    response = client.post(
        "/mcp-servers/install/preview",
        headers=api_client_builder.auth(token),
        json={"source": "npx -y @example/mcp-server"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["preview_token"]) == 32
    assert body["plan"]["source_type"] == "stdio"
    assert body["plan"]["runtime_type"] == "npx"
    assert registry.discover_calls == []


def test_mcp_install_requires_confirmation_for_risky_sources(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, registry = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "admin", role="admin")

    from nymeria.core import mcp_runtime

    settings = api_client_builder.settings(tmp_path)
    monkeypatch.setattr(mcp_runtime, "get_settings", lambda: settings)

    preview = client.post(
        "/mcp-servers/install/preview",
        headers=api_client_builder.auth(token),
        json={"source": "https://github.com/example/example-mcp-server"},
    )
    assert preview.status_code == 200

    response = client.post(
        "/mcp-servers/install",
        headers=api_client_builder.auth(token),
        json={"preview_token": preview.json()["preview_token"]},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "needs admin confirmation" in detail["message"]
    assert detail["preview"]["confirmation_required"] is True
    assert detail["server"]["install_status"] == "draft"
    assert detail["server"]["enabled"] is False
    assert list(registry.servers) == []


def test_mcp_install_confirmed_package_stays_draft_when_unsandboxed_disabled(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, registry = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "admin", role="admin")

    from nymeria.core import mcp_runtime

    settings = api_client_builder.settings(tmp_path)
    monkeypatch.setattr(mcp_runtime, "get_settings", lambda: settings)

    preview = client.post(
        "/mcp-servers/install/preview",
        headers=api_client_builder.auth(token),
        json={"source": "npx -y @example/mcp-server"},
    )
    assert preview.status_code == 200

    response = client.post(
        "/mcp-servers/install",
        headers=api_client_builder.auth(token),
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmed": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "draft"
    assert "Managed MCP installs" in body["discovery_error"]
    assert body["server"]["install_status"] == "failed"
    assert registry.discover_calls == []
