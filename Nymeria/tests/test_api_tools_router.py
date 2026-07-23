"""Regression tests for the extracted classic Tools API router."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.tool_search_index import ToolSearchIndex
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import (
    ADMIN_ONLY_TOOL_NAMES,
    SEED_TOOLS,
    DEVELOPER_ONLY_TOOL_NAMES,
)
from nymeria.vendor.react_agent.tool_registry import ToolRegistry


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.profile_manager = UserProfileManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = ToolRegistry().register_all(SEED_TOOLS)
        self._callable_tool_thread_map: dict[str, str] = {}
        self.synced_tools = 0
        self.default_graph_rebuilds = 0

    def sync_agent_tools(self):
        self.synced_tools += 1

    def _rebuild_default_graphs(self):
        self.default_graph_rebuilds += 1

    def _get_team_scoped_callable_threads(
        self,
        *,
        user_id: str,
        caller_thread_id: str,
    ):
        owned = set(self.accounts_repo.list_threads_for_user(user_id))
        caller_tc = self.thread_config_manager.get_config(caller_thread_id)
        callables = self.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned,
        )
        # Mirrors the real bubble semantics (backlog #97): caller and target
        # teams must match, with "no team" itself a bubble.
        caller_team = (caller_tc.callable_team_id if caller_tc else None) or None
        return [
            tc
            for tc in callables
            if (tc.callable_team_id or None) == caller_team
        ]


def _client(tmp_path: Path, api_client_builder) -> tuple[TestClient, FakeAgent]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    return api_client_builder.client(agent, settings), agent


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def _save_thread(agent: FakeAgent, user_id: str, config: ThreadConfig) -> None:
    agent.thread_config_manager.save_config(config)
    agent.accounts_repo.claim_thread(config.thread_id, user_id)


def _register_registry_callable(agent: FakeAgent, name: str) -> None:
    def helper(task: str) -> str:
        return task

    agent.tool_registry.register_function(
        helper,
        name=name,
        description="Registry callable from another user's thread",
    )
    agent._callable_tool_thread_map[name] = "other-helper"


def test_list_tools_hides_registry_callable_collision_and_readds_owned_callable(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    _create_user(agent, "other")
    _register_registry_callable(agent, "Helper")
    _save_thread(
        agent,
        "owner",
        ThreadConfig(
            thread_id="owner-helper",
            callable=True,
            callable_name="Helper",
            callable_description="Owner helper",
        ),
    )
    _save_thread(
        agent,
        "other",
        ThreadConfig(
            thread_id="other-helper",
            callable=True,
            callable_name="Helper",
            callable_description="Other helper",
        ),
    )

    response = client.get(
        "/tools",
        headers=api_client_builder.auth(owner_token),
    )

    assert response.status_code == 200
    helpers = [tool for tool in response.json()["tools"] if tool["name"] == "Helper"]
    assert helpers == [
        {"name": "Helper", "description": "Owner helper", "enabled": True}
    ]


def test_thread_callable_tools_filters_to_runtime_visible_callables(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    _save_thread(
        agent,
        "owner",
        ThreadConfig(
            thread_id="caller",
            callable=True,
            callable_name="Caller",
            disabled_tools=["Blocked"],
            callable_team_id="team-a",
            callable_team_name="Ops",
        ),
    )
    for config in (
        ThreadConfig(
            thread_id="allowed",
            callable=True,
            callable_name="Allowed",
            callable_description="Allowed helper",
            callable_team_id="team-a",
            callable_team_name="Ops",
        ),
        ThreadConfig(
            thread_id="blocked",
            callable=True,
            callable_name="Blocked",
            callable_team_id="team-a",
            callable_team_name="Ops",
        ),
        ThreadConfig(
            thread_id="same-name",
            callable=True,
            callable_name="Caller",
            callable_team_id="team-a",
            callable_team_name="Ops",
        ),
        ThreadConfig(
            thread_id="core-collision",
            callable=True,
            callable_name=SEED_TOOLS[0].name,
            callable_team_id="team-a",
            callable_team_name="Ops",
        ),
        ThreadConfig(
            thread_id="other-team",
            callable=True,
            callable_name="OtherTeam",
            callable_team_id="team-b",
            callable_team_name="Sales",
        ),
    ):
        _save_thread(agent, "owner", config)

    response = client.get(
        "/threads/caller/callable-tools",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": "caller",
        "callable_thread_count": 1,
        "callable_threads": [
            {
                "thread_id": "allowed",
                "name": "Allowed",
                "description": "Allowed helper",
                "team_id": "team-a",
                "team_name": "Ops",
            }
        ],
    }


def test_default_tools_role_gates_and_rebuilds_default_graphs(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    user_token = _create_user(agent, "owner")
    admin_token = _create_user(agent, "admin", role="admin")
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]
    developer_only = sorted(DEVELOPER_ONLY_TOOL_NAMES)[0]

    user_admin_only = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(user_token),
        json={"tool_names": [SEED_TOOLS[0].name, admin_only]},
    )
    user_developer_only = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(user_token),
        json={"tool_names": [SEED_TOOLS[0].name, developer_only]},
    )
    admin_allowed = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(admin_token),
        json={"tool_names": [SEED_TOOLS[0].name, admin_only, "todo"]},
    )

    assert user_admin_only.status_code == 403
    assert "Admin-only tools" in user_admin_only.json()["detail"]
    assert user_developer_only.status_code == 403
    assert "Developer-only diagnostic tools" in user_developer_only.json()["detail"]
    assert admin_allowed.status_code == 200
    assert admin_allowed.json() == {
        "status": "ok",
        "default_tools": sorted([SEED_TOOLS[0].name, admin_only, "nym_todo"]),
        "count": 3,
    }
    assert agent.profile_manager.get_profile(
        "admin"
    ).tool_preferences.default_thread_tools == [
        SEED_TOOLS[0].name,
        admin_only,
        "nym_todo",
    ]
    assert agent.default_graph_rebuilds == 1


def test_default_tools_accepts_split_auth_manager_legacy_name(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")

    response = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": ["auth_manager", "auth_manage"]},
    )

    expected = ["auth_bindings", "auth_cleanup", "auth_inspect"]
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "default_tools": expected,
        "count": 3,
    }
    assert agent.profile_manager.get_profile(
        "owner"
    ).tool_preferences.default_thread_tools == [
        "auth_inspect",
        "auth_cleanup",
        "auth_bindings",
    ]


def test_command_backend_get_default_tools_matches_route_payload(
    tmp_path: Path,
    api_client_builder,
):
    # TurnExecutor two-shape parity: GET /tools/defaults (HTTP) and the
    # in-process CommandBackendClient.get_default_tools now share one serializer
    # (serialize_default_tools), so their payloads are byte-identical. Seed a
    # custom default-tools set and an owned callable thread so the comparison
    # exercises real default_tools + callable_thread_count, not the empty shape.
    from nymeria.core.command_service import CommandBackendClient, _CommandBackendUser

    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner", role="admin")
    client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, "todo"]},
    )
    _save_thread(
        agent,
        "owner",
        ThreadConfig(
            thread_id="owner-helper",
            callable=True,
            callable_name="Helper",
            callable_description="Owner helper",
        ),
    )

    backend = CommandBackendClient(
        agent,
        user=_CommandBackendUser(id="owner", role="admin"),
        settings_fn=lambda: api_client_builder.settings(tmp_path),
    )

    route_json = client.get(
        "/tools/defaults", headers=api_client_builder.auth(token)
    ).json()
    backend_dict = asyncio.run(backend.get_default_tools("owner"))

    assert backend_dict == route_json
    # Sanity: the compared payload is non-trivial.
    assert backend_dict["callable_thread_count"] == 1
    assert SEED_TOOLS[0].name in backend_dict["default_tools"]


def test_user_tool_search_endpoint_returns_ranked_hints(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    from nymeria.core import tool_search_index as search_index_module

    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    monkeypatch.setattr(
        search_index_module,
        "_DEFAULT_INDEX",
        ToolSearchIndex(openai_api_key=None),
    )

    response = client.get(
        "/users/owner/tools/search",
        params={"query": "browser", "top_k": 5},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "browser"
    assert payload["mode"] in {"bm25", "fuzzy", "substring"}
    assert payload["results"]
    first = payload["results"][0]
    assert {
        "name",
        "description",
        "category",
        "security_level",
        "tool_type",
        "is_default",
        "status",
        "score",
        "enable_hint",
    }.issubset(first)
    assert first["enable_hint"].startswith("/tools enable") or first[
        "enable_hint"
    ].startswith("Already enabled")


# ---------------------------------------------------------------------------
# Credential (auth) axis on the tool read models (dev-todo #7).
# ---------------------------------------------------------------------------


def _seed_vault(tmp_path: Path, monkeypatch, *, connected_user: str | None = None):
    """Bind a tmp CredentialVaultRepo onto get_credential_vault_repo.

    Settings-independent (unlike the get_settings-based fixture in
    test_credential_registry.py) so it works inside the HTTP client context too.
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


def test_serialize_default_tools_stamps_auth_axis_incl_mcp(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    import nymeria.tools.productivity_service_integrations  # noqa: F401
    from nymeria.api.routers.tools import serialize_default_tools
    from nymeria.tools.credential_registry import spec_for_tool
    from nymeria.tools.metadata import (
        register_mcp_server_tool_metadata,
        unregister_mcp_server_tool_metadata,
    )

    agent = FakeAgent(tmp_path)
    _create_user(agent, "owner")
    _seed_vault(tmp_path, monkeypatch, connected_user="owner")
    register_mcp_server_tool_metadata("mcp__testserver__ping", "Ping the test server")
    try:
        payload = serialize_default_tools(agent, user_id="owner", role="user")
    finally:
        unregister_mcp_server_tool_metadata("mcp__testserver__ping")

    tools = {item["name"]: item for item in payload["available_tools"]}
    # Every serialized tool carries the axis keys (stamp runs after the MCP loop).
    for item in payload["available_tools"]:
        assert "auth_status" in item
        assert "auth_provider" in item

    # Provider-mapped tool gets real values.
    assert tools["todoist_list_tasks"]["auth_provider"] == "todoist"
    assert tools["todoist_list_tasks"]["auth_status"] == "connected"

    # The MCP entry is present and carries the keys (no provider spec -> None).
    assert "mcp__testserver__ping" in tools
    assert tools["mcp__testserver__ping"]["auth_status"] is None
    assert tools["mcp__testserver__ping"]["auth_provider"] is None

    # A spec-less builtin gets None for both.
    plain_name = next(
        item["name"]
        for item in payload["available_tools"]
        if spec_for_tool(item["name"]) is None
    )
    assert tools[plain_name]["auth_status"] is None
    assert tools[plain_name]["auth_provider"] is None


def test_tool_search_response_model_preserves_auth_fields(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    # Regression: response_model=ToolSearchResponse used to silently drop
    # auth_status/auth_provider. A provider-mapped result must round-trip them.
    import nymeria.tools.productivity_service_integrations  # noqa: F401
    from nymeria.core import tool_search_index as search_index_module

    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    _seed_vault(tmp_path, monkeypatch, connected_user="owner")
    monkeypatch.setattr(
        search_index_module,
        "_DEFAULT_INDEX",
        ToolSearchIndex(openai_api_key=None),
    )

    response = client.get(
        "/users/owner/tools/search",
        params={"query": "todoist", "top_k": 15},
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    results = response.json()["results"]
    todoist = next(r for r in results if r["name"].startswith("todoist_"))
    assert todoist["auth_provider"] == "todoist"
    assert todoist["auth_status"] == "connected"
    # A non-integration result serializes the keys with a null value.
    plain = next(
        (r for r in results if r["auth_provider"] is None),
        None,
    )
    if plain is not None:
        assert plain["auth_status"] is None
