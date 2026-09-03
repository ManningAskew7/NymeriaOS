"""Regression tests for the extracted classic Tools API router."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
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


def test_reset_default_tools_restores_fresh_install_set(
    tmp_path: Path,
    api_client_builder,
):
    """DELETE /tools/defaults writes exactly what a brand-new profile is
    seeded with: capability-expansion names stripped (before 2026-08-30 the
    raw seed leaked them, producing a set no fresh install ever had) and the
    keyless web defaults included.
    """
    from nymeria.tools import (
        CAPABILITY_EXPANSION_TOOL_NAMES,
        fresh_default_thread_tool_names,
    )

    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    profile = agent.profile_manager.get_profile("owner")
    cap_expansion = sorted(CAPABILITY_EXPANSION_TOOL_NAMES)[0]
    profile.tool_preferences.default_thread_tools = ["bash_execute", cap_expansion]
    agent.profile_manager.save_profile(profile)

    response = client.delete(
        "/tools/defaults", headers=api_client_builder.auth(token)
    )

    expected = fresh_default_thread_tool_names()
    assert response.status_code == 200
    assert response.json()["default_tools"] == sorted(expected)
    saved = agent.profile_manager.get_profile(
        "owner"
    ).tool_preferences.default_thread_tools
    assert saved == expected
    assert cap_expansion not in saved
    assert "web_search_ddgs" in saved and "fetch_url_nymeria" in saved


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
        ToolSearchIndex(embedding_api_key=None),
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
        ToolSearchIndex(embedding_api_key=None),
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


def test_command_backend_set_default_tools_enforces_the_same_role_gates(
    tmp_path: Path,
    api_client_builder,
):
    """#321 two-shape parity on the WRITE, and the gate the agent must not dodge.

    ``PUT /tools/defaults`` and ``CommandBackendClient.set_default_tools`` share
    one writer (``apply_default_tools_update``), so a non-admin is refused an
    admin-only tool on BOTH shapes. The in-process shape is the one the agent
    reaches through ``/tools enable <name> global``, and it derives admin-ness
    from its own authenticated user rather than from the command executor, whose
    ``is_admin`` is ``None`` for an agent.
    """
    from nymeria.core.command_service import CommandBackendClient, _CommandBackendUser

    client, agent = _client(tmp_path, api_client_builder)
    _create_user(agent, "owner")
    _create_user(agent, "admin", role="admin")
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]

    def backend(user_id: str, role: str) -> CommandBackendClient:
        return CommandBackendClient(
            agent,
            user=_CommandBackendUser(id=user_id, role=role),
            settings_fn=lambda: api_client_builder.settings(tmp_path),
        )

    with pytest.raises(ValueError, match="Admin-only tools"):
        asyncio.run(
            backend("owner", "user").set_default_tools(
                "owner", [SEED_TOOLS[0].name, admin_only]
            )
        )
    assert (
        agent.profile_manager.get_profile("owner").tool_preferences.default_thread_tools
        != [SEED_TOOLS[0].name, admin_only]
    )

    developer_only = sorted(DEVELOPER_ONLY_TOOL_NAMES)[0]
    with pytest.raises(ValueError, match="Developer-only diagnostic tools"):
        asyncio.run(
            backend("owner", "user").set_default_tools(
                "owner", [SEED_TOOLS[0].name, developer_only]
            )
        )

    with pytest.raises(ValueError, match="Unknown tools"):
        asyncio.run(backend("owner", "user").set_default_tools("owner", ["no_such_tool"]))

    # A non-admin cannot reach another account's profile at all: the access
    # check runs before the writer, so nothing is validated on their behalf.
    with pytest.raises(Exception):
        asyncio.run(
            backend("owner", "user").set_default_tools("admin", [SEED_TOOLS[0].name])
        )

    allowed = asyncio.run(
        backend("admin", "admin").set_default_tools(
            "admin", [SEED_TOOLS[0].name, admin_only]
        )
    )
    assert allowed["default_tools"] == sorted([SEED_TOOLS[0].name, admin_only])
    assert agent.profile_manager.get_profile(
        "admin"
    ).tool_preferences.default_thread_tools == [SEED_TOOLS[0].name, admin_only]
    # The write has to reach live graphs, not just the profile on disk.
    assert agent.default_graph_rebuilds == 1


def test_command_backend_set_default_tools_judges_the_target_users_role(
    tmp_path: Path,
    api_client_builder,
):
    """An admin writing someone else's defaults is gated by THEIR role.

    The HTTP route reaches the same conclusion through act-as, where `is_admin`
    is the target's role, so judging by the caller here would let an admin park
    an admin-only tool in a non-admin's profile through one shape and not the
    other. Sharing the writer is pointless if the two shapes feed it different
    verdicts.
    """
    from nymeria.core.command_service import CommandBackendClient, _CommandBackendUser

    _client_, agent = _client(tmp_path, api_client_builder)
    _create_user(agent, "owner")
    _create_user(agent, "admin", role="admin")
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]

    admin_client = CommandBackendClient(
        agent,
        user=_CommandBackendUser(id="admin", role="admin"),
        settings_fn=lambda: api_client_builder.settings(tmp_path),
    )

    with pytest.raises(ValueError, match="Admin-only tools"):
        asyncio.run(
            admin_client.set_default_tools("owner", [SEED_TOOLS[0].name, admin_only])
        )
    assert (
        agent.profile_manager.get_profile("owner").tool_preferences.default_thread_tools
        != [SEED_TOOLS[0].name, admin_only]
    )


def test_default_tools_validates_what_the_write_adds_not_the_whole_list(
    tmp_path: Path,
    api_client_builder,
):
    """#325: one stale entry must not refuse every later write.

    The write is a whole-list replace, so a caller changing one tool resubmits
    the account's entire set. Validating all of it meant an uninstalled tool
    still in the profile answered every write with an error about a tool the
    user never touched, including the removal that would have cleared it.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")

    profile = agent.profile_manager.get_profile("owner")
    profile.tool_preferences.default_thread_tools = [SEED_TOOLS[0].name, "ghost_tool"]
    agent.profile_manager.save_profile(profile)

    # Adding a real tool alongside the stale one now succeeds.
    added = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, "ghost_tool", "todo"]},
    )
    assert added.status_code == 200, added.json()

    # And the stale name can be dropped, which is the way out.
    cleared = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, "nym_todo"]},
    )
    assert cleared.status_code == 200, cleared.json()
    assert "ghost_tool" not in (
        agent.profile_manager.get_profile("owner").tool_preferences.default_thread_tools
    )


def test_default_tools_still_refuses_a_newly_added_unknown_or_gated_tool(
    tmp_path: Path,
    api_client_builder,
):
    """The delta keeps every gate: only PRE-EXISTING entries are exempt.

    This is the half that makes #325's relaxation safe. A name the write brings
    IN is checked exactly as before, so nobody can put an admin-only or
    developer-only tool into a profile that did not already carry it.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]
    developer_only = sorted(DEVELOPER_ONLY_TOOL_NAMES)[0]

    unknown = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, "ghost_tool"]},
    )
    gated = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, admin_only]},
    )
    dev_gated = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, developer_only]},
    )

    assert unknown.status_code == 400
    assert "ghost_tool" in unknown.json()["detail"]
    assert gated.status_code == 403
    assert dev_gated.status_code == 403


def test_a_demoted_user_can_still_write_while_keeping_a_now_gated_tool(
    tmp_path: Path,
    api_client_builder,
):
    """The role axis of #325, which is the whole security argument of the delta.

    A user demoted out of admin still has the admin-only tool in their profile.
    Before, that entry answered every later write with a 403 naming a tool they
    never touched, so a demotion did not strip the tool, it BRICKED the command.
    Retaining it is safe only because `select_tools_for_graph` re-gates by role
    at every build, pinned by
    `test_graph_build_unification.py::test_select_tools_strips_admin_default_for_non_admin_keeps_for_admin`.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]
    developer_only = sorted(DEVELOPER_ONLY_TOOL_NAMES)[0]

    profile = agent.profile_manager.get_profile("owner")
    profile.tool_preferences.default_thread_tools = [
        SEED_TOOLS[0].name,
        admin_only,
        developer_only,
    ]
    agent.profile_manager.save_profile(profile)

    kept = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, admin_only, developer_only, "todo"]},
    )

    assert kept.status_code == 200, kept.json()
    saved = agent.profile_manager.get_profile(
        "owner"
    ).tool_preferences.default_thread_tools
    # Preserved, not silently dropped: fix (b) was considered and rejected,
    # because a reinstalled tool should light back up rather than be erased by
    # the first unrelated write.
    assert admin_only in saved
    assert developer_only in saved
    assert "nym_todo" in saved


def test_an_unrelated_add_preserves_a_stale_entry_rather_than_dropping_it(
    tmp_path: Path,
    api_client_builder,
):
    """Exempting a name from validation must not mean deleting it."""
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")

    profile = agent.profile_manager.get_profile("owner")
    profile.tool_preferences.default_thread_tools = [SEED_TOOLS[0].name, "ghost_tool"]
    agent.profile_manager.save_profile(profile)

    added = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(token),
        json={"tool_names": [SEED_TOOLS[0].name, "ghost_tool", "todo"]},
    )

    assert added.status_code == 200, added.json()
    assert "ghost_tool" in (
        agent.profile_manager.get_profile("owner").tool_preferences.default_thread_tools
    )


def test_the_delta_rule_is_identical_through_the_in_process_client(
    tmp_path: Path,
    api_client_builder,
):
    """Two-shape parity on the delta, not just on the gates.

    The shared writer exists so the HTTP route and CommandBackendClient cannot
    diverge; that is only true if the exemption behaves the same through both.
    """
    from nymeria.core.command_service import CommandBackendClient, _CommandBackendUser

    _client_, agent = _client(tmp_path, api_client_builder)
    _create_user(agent, "owner")
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]

    profile = agent.profile_manager.get_profile("owner")
    profile.tool_preferences.default_thread_tools = [
        SEED_TOOLS[0].name,
        admin_only,
        "ghost_tool",
    ]
    agent.profile_manager.save_profile(profile)

    backend = CommandBackendClient(
        agent,
        user=_CommandBackendUser(id="owner", role="user"),
        settings_fn=lambda: api_client_builder.settings(tmp_path),
    )

    result = asyncio.run(
        backend.set_default_tools(
            "owner", [SEED_TOOLS[0].name, admin_only, "ghost_tool", "todo"]
        )
    )
    assert "nym_todo" in result["default_tools"]

    # ... and a NEWLY added gated name is still refused on this shape.
    with pytest.raises(ValueError, match="Admin-only tools"):
        asyncio.run(
            backend.set_default_tools("owner", [SEED_TOOLS[0].name, "ghost_tool", sorted(ADMIN_ONLY_TOOL_NAMES)[1]])
        )
