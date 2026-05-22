"""Custom Tools API route extraction regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nymeria.api.routers import custom_tools as custom_tools_router
from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.time_utils import utc_now


class FakeCustomToolLoader:
    def __init__(self):
        self.definitions = {}
        self.lookup_ids = []

    def get_all_definitions(self):
        return list(self.definitions.values())

    def get_definition(self, tool_id: str):
        self.lookup_ids.append(tool_id)
        return self.definitions.get(tool_id)

    def save_definition(self, definition):
        definition.updated_at = utc_now()
        self.definitions[definition.id] = definition
        return Path(f"/tmp/{definition.id}.json")

    def delete_definition(self, tool_id: str) -> bool:
        return self.definitions.pop(tool_id, None) is not None


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.reload_count = 0
        self.synced_tools = 0

    def reload_tools(self):
        self.reload_count += 1

    def sync_agent_tools(self):
        self.synced_tools += 1


def _authenticated_client(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
    *,
    loader: FakeCustomToolLoader,
    role: str = "admin",
) -> tuple[Any, FakeAgent, str]:
    monkeypatch.setattr(custom_tools_router, "get_custom_tool_loader", lambda: loader)
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    agent.accounts_repo.create_user(
        "caller",
        "caller@example.com",
        "Caller",
        role=role,
    )
    token = agent.accounts_repo.issue_token("caller")
    return client, agent, token


def test_custom_tool_export_route_is_not_shadowed_by_tool_id(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    loader = FakeCustomToolLoader()
    client, _agent, token = _authenticated_client(
        tmp_path,
        api_client_builder,
        monkeypatch,
        loader=loader,
    )

    response = client.get(
        "/tools/custom/export",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert "export" not in loader.lookup_ids


def test_custom_tools_are_admin_only(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    loader = FakeCustomToolLoader()
    client, _agent, token = _authenticated_client(
        tmp_path,
        api_client_builder,
        monkeypatch,
        loader=loader,
        role="user",
    )

    response = client.get(
        "/tools/custom",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 403


def test_custom_tool_crud_reloads_agent_and_preserves_response_shape(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    loader = FakeCustomToolLoader()
    client, agent, token = _authenticated_client(
        tmp_path,
        api_client_builder,
        monkeypatch,
        loader=loader,
    )
    headers = api_client_builder.auth(token)

    create_response = client.post(
        "/tools/custom",
        headers=headers,
        json={
            "id": "price_lookup",
            "name": "Price Lookup",
            "description": "Look up a price",
            "implementation_type": "http",
            "parameters": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol",
                    "required": True,
                }
            },
            "http_config": {
                "method": "GET",
                "url": "https://api.example.com/prices/${symbol}",
                "headers": {"X-Test": "1"},
                "response_format": "json",
            },
            "enabled": True,
            "tags": ["finance"],
        },
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["id"] == "price_lookup"
    assert created["parameters"]["symbol"]["required"] is True
    assert created["http_config"]["url"] == "https://api.example.com/prices/${symbol}"
    assert created["mcp_config"] is None
    assert agent.reload_count == 1

    update_response = client.put(
        "/tools/custom/price_lookup",
        headers=headers,
        json={"description": "Look up a public market price", "enabled": False},
    )

    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["description"] == "Look up a public market price"
    assert updated["enabled"] is False
    assert agent.reload_count == 2

    delete_response = client.delete("/tools/custom/price_lookup", headers=headers)

    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "ok", "deleted_id": "price_lookup"}
    assert agent.reload_count == 3


def test_custom_tool_crud_supports_python_config(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    loader = FakeCustomToolLoader()
    client, agent, token = _authenticated_client(
        tmp_path,
        api_client_builder,
        monkeypatch,
        loader=loader,
    )
    headers = api_client_builder.auth(token)

    create_response = client.post(
        "/tools/custom",
        headers=headers,
        json={
            "id": "python_echo",
            "name": "Python Echo",
            "description": "Echo text through Python",
            "implementation_type": "python",
            "parameters": {
                "value": {
                    "type": "string",
                    "description": "Value to echo",
                    "required": True,
                }
            },
            "python_config": {
                "source_code": "def run(value: str) -> str:\n    return value\n",
                "entrypoint": "run",
            },
            "enabled": True,
            "tags": ["python"],
        },
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["implementation_type"] == "python"
    assert created["python_config"]["entrypoint"] == "run"
    assert created["http_config"] is None
    assert created["mcp_config"] is None
    assert agent.reload_count == 1


def test_custom_tool_import_export_round_trip_for_mcp_config(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    loader = FakeCustomToolLoader()
    client, agent, token = _authenticated_client(
        tmp_path,
        api_client_builder,
        monkeypatch,
        loader=loader,
    )
    headers = api_client_builder.auth(token)

    import_response = client.post(
        "/tools/custom/import",
        headers=headers,
        json={
            "tools": [
                {
                    "id": "read_file_mcp",
                    "name": "Read File MCP",
                    "description": "Read a file through MCP",
                    "parameters": {},
                    "implementation_type": "mcp",
                    "mcp_config": {
                        "server_command": "npx",
                        "server_args": ["-y", "@modelcontextprotocol/server-filesystem"],
                        "tool_name": "read_file",
                        "env_vars": {"MODE": "test"},
                        "working_directory": "/tmp",
                    },
                    "enabled": True,
                    "tags": ["mcp"],
                }
            ]
        },
    )

    assert import_response.status_code == 200
    assert import_response.json() == {"status": "ok", "imported": 1, "errors": []}
    assert agent.reload_count == 1

    export_response = client.get("/tools/custom/export", headers=headers)

    assert export_response.status_code == 200
    exported = export_response.json()
    assert exported["total"] == 1
    tool = exported["tools"][0]
    assert tool["id"] == "read_file_mcp"
    assert tool["implementation_type"] == "mcp"
    assert tool["mcp_config"]["server_command"] == "npx"
    assert tool["mcp_config"]["tool_name"] == "read_file"
