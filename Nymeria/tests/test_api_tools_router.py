"""Regression tests for the extracted classic Tools API router."""

from __future__ import annotations

from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import (
    ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
    ALL_TOOLS,
    DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
    LOCAL_SYSTEM_ACCESS_TOOL_NAMES,
)
from nymeria.vendor.react_agent.tool_registry import ToolRegistry


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.profile_manager = UserProfileManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = ToolRegistry().register_all(ALL_TOOLS)
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
        if not caller_tc or not caller_tc.callable_team_id:
            return callables
        return [
            tc
            for tc in callables
            if tc.callable_team_id == caller_tc.callable_team_id
        ]


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent]:
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
            callable_name=ALL_TOOLS[0].name,
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
    admin_only = next(
        name
        for name in sorted(ADMIN_ONLY_OPTIONAL_TOOL_NAMES)
        if name not in LOCAL_SYSTEM_ACCESS_TOOL_NAMES
    )
    local_system_tool = sorted(LOCAL_SYSTEM_ACCESS_TOOL_NAMES)[0]
    developer_only = sorted(DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES)[0]

    admin_local_default = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(admin_token),
        json={"tool_names": [ALL_TOOLS[0].name, local_system_tool]},
    )
    user_admin_only = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(user_token),
        json={"tool_names": [ALL_TOOLS[0].name, admin_only]},
    )
    user_developer_only = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(user_token),
        json={"tool_names": [ALL_TOOLS[0].name, developer_only]},
    )
    admin_allowed = client.put(
        "/tools/defaults",
        headers=api_client_builder.auth(admin_token),
        json={"tool_names": [ALL_TOOLS[0].name, admin_only, "todo"]},
    )

    assert admin_local_default.status_code == 400
    assert "must be enabled per-thread" in admin_local_default.json()["detail"]
    assert user_admin_only.status_code == 403
    assert "Admin-only tools" in user_admin_only.json()["detail"]
    assert user_developer_only.status_code == 403
    assert "Developer-only diagnostic tools" in user_developer_only.json()["detail"]
    assert admin_allowed.status_code == 200
    assert admin_allowed.json() == {
        "status": "ok",
        "default_tools": sorted([ALL_TOOLS[0].name, admin_only, "nym_todo"]),
        "count": 3,
    }
    assert agent.profile_manager.get_profile(
        "admin"
    ).tool_preferences.default_thread_tools == [
        ALL_TOOLS[0].name,
        admin_only,
        "nym_todo",
    ]
    assert agent.default_graph_rebuilds == 1
