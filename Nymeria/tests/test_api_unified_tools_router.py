"""Regression tests for the extracted Unified Tools API router."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nymeria.api.routers import unified_tools as unified_tools_router
from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.time_utils import utc_now
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import (
    ADMIN_ONLY_TOOL_NAMES,
    SEED_TOOLS,
    DEVELOPER_ONLY_TOOL_NAMES,
)
from nymeria.tools.definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    ToolParameter,
)


class FakeCustomToolLoader:
    def __init__(self):
        self.definitions = {}

    def get_all_definitions(self):
        return list(self.definitions.values())

    def get_definition(self, tool_id: str):
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
        self.profile_manager = UserProfileManager(data_dir)
        self.reload_count = 0
        self.synced_tools = 0
        self.default_graph_rebuilds = 0

    def sync_agent_tools(self):
        self.synced_tools += 1

    def reload_custom_tools(self):
        # The routers deliberately use the narrow custom-tool reload; the
        # full-module reload_tools is reserved for runtime_admin (#277).
        self.reload_count += 1

    def _rebuild_default_graphs(self):
        self.default_graph_rebuilds += 1


def _client(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
    *,
    loader: FakeCustomToolLoader | None = None,
) -> tuple[Any, FakeAgent, FakeCustomToolLoader]:
    loader = loader or FakeCustomToolLoader()
    monkeypatch.setattr(unified_tools_router, "get_custom_tool_loader", lambda: loader)
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    return client, agent, loader


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def _custom_definition(tool_id: str = "price_lookup") -> CustomToolDefinition:
    return CustomToolDefinition(
        id=tool_id,
        name="Price Lookup",
        description="Look up a price",
        parameters={
            "symbol": ToolParameter(
                type="string",
                description="Ticker symbol",
                required=True,
            )
        },
        implementation_type="http",
        http_config=HTTPToolConfig(
            method="GET",
            url="https://api.example.com/prices/${symbol}",
            headers={"X-Test": "1"},
            response_format="json",
        ),
        enabled=True,
        tags=["finance"],
    )


def test_unified_tools_list_is_user_scoped_and_hides_custom_tools_from_non_admin(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, loader = _client(tmp_path, api_client_builder, monkeypatch)
    loader.save_definition(_custom_definition())
    user_token = _create_user(agent, "owner")
    admin_token = _create_user(agent, "admin", role="admin")

    user_response = client.get(
        "/users/owner/tools/unified",
        headers=api_client_builder.auth(user_token),
    )
    admin_response = client.get(
        "/users/admin/tools/unified",
        headers=api_client_builder.auth(admin_token),
    )
    cross_user_response = client.get(
        "/users/admin/tools/unified",
        headers=api_client_builder.auth(user_token),
    )

    assert user_response.status_code == 200
    assert admin_response.status_code == 200
    assert cross_user_response.status_code == 404

    user_payload = user_response.json()
    user_tools = {tool["id"]: tool for tool in user_payload["tools"]}
    assert "price_lookup" not in user_tools
    assert user_payload["custom_count"] == 0
    assert user_tools[SEED_TOOLS[0].name]["tool_type"] == "builtin"

    admin_payload = admin_response.json()
    admin_tools = {tool["id"]: tool for tool in admin_payload["tools"]}
    assert admin_tools["price_lookup"]["tool_type"] == "custom"
    assert admin_tools["price_lookup"]["parameters"]["symbol"]["required"] is True
    assert admin_tools["price_lookup"]["http_config"]["url"] == (
        "https://api.example.com/prices/${symbol}"
    )
    assert admin_payload["custom_count"] == 1


def test_unified_tool_enable_preserves_role_gates_and_rebuilds_defaults(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, _loader = _client(tmp_path, api_client_builder, monkeypatch)
    user_token = _create_user(agent, "owner")
    admin_token = _create_user(agent, "admin", role="admin")
    core_tool = SEED_TOOLS[0].name
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]
    developer_only = sorted(DEVELOPER_ONLY_TOOL_NAMES)[0]

    disable_response = client.put(
        f"/users/owner/tools/unified/{core_tool}/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": False},
    )
    user_admin_only_response = client.put(
        f"/users/owner/tools/unified/{admin_only}/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": True},
    )
    user_developer_only_response = client.put(
        f"/users/owner/tools/unified/{developer_only}/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": True},
    )
    admin_allowed_response = client.put(
        f"/users/admin/tools/unified/{admin_only}/enable",
        headers=api_client_builder.auth(admin_token),
        json={"enabled": True},
    )

    assert disable_response.status_code == 200
    assert disable_response.json() == {
        "status": "ok",
        "tool_id": core_tool,
        "enabled": False,
        "tool_type": "builtin",
    }
    owner_defaults = agent.profile_manager.get_profile(
        "owner"
    ).tool_preferences.default_thread_tools
    assert core_tool not in owner_defaults

    assert user_admin_only_response.status_code == 403
    assert user_admin_only_response.json()["detail"] == (
        f"Tool '{admin_only}' is admin-only"
    )
    assert user_developer_only_response.status_code == 403
    assert user_developer_only_response.json()["detail"] == (
        f"Tool '{developer_only}' is developer-only"
    )
    assert admin_allowed_response.status_code == 200
    assert admin_allowed_response.json()["tool_id"] == admin_only
    assert agent.default_graph_rebuilds == 2


def test_unified_description_and_config_mutations_are_user_scoped(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, _loader = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "owner")
    _create_user(agent, "other")
    tool_id = SEED_TOOLS[0].name

    set_description = client.put(
        f"/users/owner/tools/unified/{tool_id}/description",
        headers=api_client_builder.auth(token),
        json={"description": "Short custom description"},
    )
    set_config = client.put(
        f"/users/owner/tools/unified/{tool_id}/config",
        headers=api_client_builder.auth(token),
        json={"config": {"mode": "compact"}},
    )
    clear_config = client.put(
        f"/users/owner/tools/unified/{tool_id}/config",
        headers=api_client_builder.auth(token),
        json={"config": {}},
    )
    cross_user = client.put(
        f"/users/other/tools/unified/{tool_id}/description",
        headers=api_client_builder.auth(token),
        json={"description": "Should not land"},
    )

    assert set_description.status_code == 200
    assert set_description.json() == {
        "status": "ok",
        "tool_id": tool_id,
        "action": "set",
        "description": "Short custom description",
    }
    assert set_config.status_code == 200
    assert set_config.json()["config"] == {"mode": "compact"}
    assert clear_config.status_code == 200
    assert clear_config.json() == {
        "status": "ok",
        "tool_id": tool_id,
        "action": "cleared",
        "config": {},
    }
    assert cross_user.status_code == 404

    prefs = agent.profile_manager.get_profile("owner").tool_preferences
    assert prefs.get_custom_description(tool_id) == "Short custom description"
    assert prefs.get_tool_config(tool_id) == {}


def test_unified_custom_tool_crud_is_admin_only_and_preserves_reload_side_effects(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, agent, loader = _client(tmp_path, api_client_builder, monkeypatch)
    user_token = _create_user(agent, "owner")
    admin_token = _create_user(agent, "admin", role="admin")

    user_create = client.post(
        "/tools/unified",
        headers=api_client_builder.auth(user_token),
        json={
            "id": "price_lookup",
            "name": "Price Lookup",
            "description": "Look up a price",
            "implementation_type": "http",
            "parameters": {},
            "http_config": {
                "method": "GET",
                "url": "https://api.example.com/prices/${symbol}",
            },
        },
    )
    admin_create = client.post(
        "/tools/unified",
        headers=api_client_builder.auth(admin_token),
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
            "tags": ["finance"],
        },
    )
    update_response = client.put(
        "/tools/unified/price_lookup",
        headers=api_client_builder.auth(admin_token),
        json={"description": "Look up a public market price", "enabled": False},
    )

    assert user_create.status_code == 403
    assert admin_create.status_code == 200
    created = admin_create.json()
    assert created["id"] == "price_lookup"
    assert created["tool_type"] == "custom"
    assert created["parameters"]["symbol"]["required"] is True
    assert created["http_config"]["headers"] == {"X-Test": "1"}

    assert update_response.status_code == 200
    assert update_response.json()["description"] == "Look up a public market price"
    assert loader.get_definition("price_lookup").enabled is False
    assert agent.reload_count == 2

    delete_response = client.delete(
        "/tools/unified/price_lookup",
        headers=api_client_builder.auth(admin_token),
    )

    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "ok", "deleted": "price_lookup"}
    assert loader.get_definition("price_lookup") is None
    # Delete rides the agent-level narrow reload (unregister + graph
    # rebuild), not the loader-only module function that left the deleted
    # tool bound until restart (#277 review finding).
    assert agent.reload_count == 3


def _seed_vault(tmp_path: Path, monkeypatch, *, connected_user: str | None = None):
    """Bind a tmp CredentialVaultRepo onto get_credential_vault_repo.

    The owning user must already exist in tmp_path/accounts.db (FK requirement).
    """
    from cryptography.fernet import Fernet

    import nymeria.core.credential_vault as vault_mod

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")
    monkeypatch.setattr(vault_mod, "get_credential_vault_repo", lambda *a, **k: repo)
    if connected_user is not None:
        repo.create_credential(
            owner_type="user",
            owner_user_id=connected_user,
            name="todoist key",
            provider="todoist",
            kind="api_key",
            secret_fields={"api_key": "sk-x"},
        )
    return repo


def test_unified_tools_carry_credential_auth_axis(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    import nymeria.tools.productivity_service_integrations  # noqa: F401
    from nymeria.tools.credential_registry import spec_for_tool

    client, agent, _loader = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "owner")
    _seed_vault(tmp_path, monkeypatch, connected_user="owner")

    response = client.get(
        "/users/owner/tools/unified",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    tools = {tool["id"]: tool for tool in response.json()["tools"]}

    # Provider-mapped builtin tool returns real (non-None) auth fields.
    assert tools["todoist_list_tasks"]["auth_provider"] == "todoist"
    assert tools["todoist_list_tasks"]["auth_status"] == "connected"

    # A spec-less builtin returns None for both.
    plain_id = next(
        tool_id
        for tool_id in tools
        if spec_for_tool(tool_id) is None
    )
    assert tools[plain_id]["auth_status"] is None
    assert tools[plain_id]["auth_provider"] is None


def test_unified_tool_response_schema_accepts_auth_fields():
    # Schema-level guard: UnifiedToolResponse round-trips the optional axis.
    from nymeria.api.schemas.unified_tools import UnifiedToolResponse

    resp = UnifiedToolResponse(
        id="todoist_list_tasks",
        name="todoist_list_tasks",
        description="d",
        default_description="d",
        category="integrations",
        security_level="moderate",
        enabled=True,
        enabled_reason="default_thread_tools",
        tool_type="builtin",
        auth_status="needs_setup",
        auth_provider="todoist",
    )
    dumped = resp.model_dump()
    assert dumped["auth_status"] == "needs_setup"
    assert dumped["auth_provider"] == "todoist"


def test_unified_mcp_tools_carry_server_provenance_and_setup_axis(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    from nymeria.tools.metadata import (
        register_mcp_server_tool_metadata,
        unregister_mcp_server_tool_metadata,
    )

    client, agent, _loader = _client(tmp_path, api_client_builder, monkeypatch)
    token = _create_user(agent, "owner")

    ready = "mcp__notion__search_docs"
    unconfigured = "mcp__linear__list_issues"
    register_mcp_server_tool_metadata(
        ready,
        "Search Notion docs",
        live=True,
        enabled=True,
        server_id="notion",
        server_name="Notion",
        install_status="ready",
    )
    register_mcp_server_tool_metadata(
        unconfigured,
        "List Linear issues",
        live=False,
        enabled=True,
        server_id="linear",
        server_name="Linear",
        install_status="needs_config",
    )
    try:
        response = client.get(
            "/users/owner/tools/unified",
            headers=api_client_builder.auth(token),
        )
        assert response.status_code == 200
        tools = {tool["id"]: tool for tool in response.json()["tools"]}

        assert tools[ready]["tool_type"] == "mcp_server"
        assert tools[ready]["server_id"] == "notion"
        assert tools[ready]["server_name"] == "Notion"
        assert tools[ready]["display_name"] == "Notion / search_docs"
        # The name the model calls stays the clean internal identifier.
        assert tools[ready]["name"] == ready
        assert tools[ready]["auth_status"] == "connected"
        # No credential provider for MCP; the axis rides install status alone.
        assert tools[ready]["auth_provider"] is None

        assert tools[unconfigured]["server_name"] == "Linear"
        assert tools[unconfigured]["auth_status"] == "needs_setup"
    finally:
        unregister_mcp_server_tool_metadata(ready)
        unregister_mcp_server_tool_metadata(unconfigured)


def test_unified_disable_of_a_contract_tool_warns_about_the_lost_capability(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    """Disabling reply_to_thread or wait_for_reply is allowed (modularity) but
    the response says what stops working; disabling any other tool carries no
    warning (backlog #357)."""
    client, agent, _loader = _client(tmp_path, api_client_builder, monkeypatch)
    user_token = _create_user(agent, "owner")

    reply_off = client.put(
        "/users/owner/tools/unified/reply_to_thread/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": False},
    )
    wait_off = client.put(
        "/users/owner/tools/unified/wait_for_reply/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": False},
    )
    plain_off = client.put(
        f"/users/owner/tools/unified/{SEED_TOOLS[0].name}/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": False},
    )
    reply_on = client.put(
        "/users/owner/tools/unified/reply_to_thread/enable",
        headers=api_client_builder.auth(user_token),
        json={"enabled": True},
    )

    assert reply_off.status_code == 200
    assert "cannot answer requests other threads make" in reply_off.json()["warning"]
    assert wait_off.status_code == 200
    assert "cannot wait inline" in wait_off.json()["warning"]
    assert plain_off.status_code == 200 and "warning" not in plain_off.json()
    assert reply_on.status_code == 200 and "warning" not in reply_on.json()
    defaults = agent.profile_manager.get_profile("owner").tool_preferences.default_thread_tools
    assert "reply_to_thread" in defaults and "wait_for_reply" not in defaults
