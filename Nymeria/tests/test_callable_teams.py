from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from nymeria.core.accounts import AccountsRepo
from nymeria.core.agent import NymeriaAgent
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.triggers import api as api_module
from nymeria.vendor.react_agent.nodes import SafeToolNode


@dataclass
class FakeSettings:
    data_dir: Path
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    cors_origins_list: list[str] | None = None

    def __post_init__(self):
        if self.cors_origins_list is None:
            self.cors_origins_list = ["*"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nymeria.db"


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        return []


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeAgent, str]:
    settings = FakeSettings(tmp_path)
    agent = FakeAgent(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    app = api_module.create_api_app(agent)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner")
    token = agent.accounts_repo.issue_token("owner")
    return TestClient(app), agent, token


def test_team_scoped_callable_filter_keeps_unteamed_legacy_visibility(tmp_path: Path):
    manager = ThreadConfigManager(tmp_path)
    manager.save_config(ThreadConfig(thread_id="caller", callable_team_id="team-a", callable_team_name="Ops"))
    manager.save_config(ThreadConfig(thread_id="agent-a", callable=True, callable_name="AgentA", callable_team_id="team-a", callable_team_name="Ops"))
    manager.save_config(ThreadConfig(thread_id="agent-b", callable=True, callable_name="AgentB", callable_team_id="team-b", callable_team_name="Sales"))
    manager.save_config(ThreadConfig(thread_id="agent-free", callable=True, callable_name="AgentFree"))

    agent = NymeriaAgent.__new__(NymeriaAgent)
    agent.thread_config_manager = manager
    agent.accounts_repo = SimpleNamespace(
        list_threads_for_user=lambda _user_id: ["caller", "free-caller", "agent-a", "agent-b", "agent-free"]
    )

    scoped = agent._get_team_scoped_callable_threads(user_id="owner", caller_thread_id="caller")
    assert [tc.callable_name for tc in scoped] == ["AgentA"]
    assert agent.is_callable_visible_to_thread("caller", "agent-a") is True
    assert agent.is_callable_visible_to_thread("caller", "agent-b") is False

    unscoped = agent._get_team_scoped_callable_threads(user_id="owner", caller_thread_id="free-caller")
    assert {tc.callable_name for tc in unscoped} == {"AgentA", "AgentB", "AgentFree"}
    assert agent.is_callable_visible_to_thread("free-caller", "agent-b") is True


def test_callable_timeout_abort_uses_current_user_scope(tmp_path: Path):
    manager = ThreadConfigManager(tmp_path)
    manager.save_config(ThreadConfig(thread_id="owner-parent"))
    manager.save_config(ThreadConfig(thread_id="bob-parent"))
    manager.save_config(ThreadConfig(thread_id="owner-helper", callable=True, callable_name="Helper"))
    manager.save_config(ThreadConfig(thread_id="bob-helper", callable=True, callable_name="Helper"))

    ownership = {
        "owner-parent": "owner",
        "owner-helper": "owner",
        "bob-parent": "bob",
        "bob-helper": "bob",
    }

    agent = NymeriaAgent.__new__(NymeriaAgent)
    agent.thread_config_manager = manager
    agent.accounts_repo = SimpleNamespace(
        list_threads_for_user=lambda user_id: [
            thread_id for thread_id, owner in ownership.items() if owner == user_id
        ],
        get_thread_owner=lambda thread_id: ownership.get(thread_id),
    )
    agent._callable_tool_thread_map = {"Helper": "bob-helper"}
    aborted: list[str] = []
    agent.abort_with_cascade = aborted.append

    agent._on_tool_timeout(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "Helper", "args": {"task": "work"}, "id": "call-1"}],
                )
            ]
        },
        config={"configurable": {"thread_id": "owner-parent", "user_id": "owner"}},
    )

    assert aborted == ["owner-helper"]


def test_callable_timeout_abort_uses_callable_thread_owner_scope(tmp_path: Path):
    manager = ThreadConfigManager(tmp_path)
    manager.save_config(ThreadConfig(thread_id="owner-worker", callable=True, callable_name="Worker"))
    manager.save_config(ThreadConfig(thread_id="owner-helper", callable=True, callable_name="Helper"))
    manager.save_config(ThreadConfig(thread_id="bob-helper", callable=True, callable_name="Helper"))

    ownership = {
        "owner-worker": "owner",
        "owner-helper": "owner",
        "bob-helper": "bob",
    }

    agent = NymeriaAgent.__new__(NymeriaAgent)
    agent.thread_config_manager = manager
    agent.accounts_repo = SimpleNamespace(
        list_threads_for_user=lambda user_id: [
            thread_id for thread_id, owner in ownership.items() if owner == user_id
        ],
        get_thread_owner=lambda thread_id: ownership.get(thread_id),
    )
    agent._callable_tool_thread_map = {"Helper": "bob-helper"}
    aborted: list[str] = []
    agent.abort_with_cascade = aborted.append

    agent._on_tool_timeout(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "Helper", "args": {"task": "work"}, "id": "call-1"}],
                )
            ]
        },
        config={"configurable": {"thread_id": "owner-worker", "user_id": "admin"}},
    )

    assert aborted == ["owner-helper"]


def test_safe_tool_node_timeout_hook_receives_runnable_config():
    seen = {}

    def on_timeout(input_dict, config=None):
        seen["input"] = input_dict
        seen["config"] = config

    node = SafeToolNode([], on_timeout=on_timeout)
    input_dict = {"messages": []}
    config = {"configurable": {"thread_id": "thread-a", "user_id": "owner"}}

    node._notify_timeout(input_dict, config)

    assert seen == {"input": input_dict, "config": config}


def test_safe_tool_node_timeout_hook_keeps_legacy_one_arg_callback():
    seen = {}

    def on_timeout(input_dict):
        seen["input"] = input_dict

    node = SafeToolNode([], on_timeout=on_timeout)
    input_dict = {"messages": []}

    node._notify_timeout(input_dict, {"configurable": {"thread_id": "thread-a"}})

    assert seen == {"input": input_dict}


def test_thread_team_api_moves_membership_and_clears_on_delete(tmp_path: Path, monkeypatch):
    client, agent, token = _client(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}
    for thread_id in ("thread-a", "thread-b", "thread-c"):
        agent.accounts_repo.claim_thread(thread_id, "owner")

    created = client.post(
        "/thread-teams",
        headers=headers,
        json={"name": "Ops", "thread_ids": ["thread-a", "thread-b"]},
    )
    assert created.status_code == 200
    team = created.json()
    assert team["name"] == "Ops"
    assert set(team["thread_ids"]) == {"thread-a", "thread-b"}

    cfg_a = agent.thread_config_manager.get_config("thread-a")
    assert cfg_a is not None
    assert cfg_a.callable_team_id == team["id"]
    assert cfg_a.callable_team_name == "Ops"

    renamed = client.patch(
        f"/thread-teams/{team['id']}",
        headers=headers,
        json={"name": "Ops Team", "thread_ids": ["thread-b", "thread-c"]},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Ops Team"
    assert set(renamed.json()["thread_ids"]) == {"thread-b", "thread-c"}

    assert agent.thread_config_manager.get_config("thread-a").callable_team_id is None
    assert agent.thread_config_manager.get_config("thread-b").callable_team_name == "Ops Team"
    assert agent.thread_config_manager.get_config("thread-c").callable_team_id == team["id"]

    listed = client.get("/thread-teams", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    deleted = client.delete(f"/thread-teams/{team['id']}", headers=headers)
    assert deleted.status_code == 200
    assert agent.thread_config_manager.get_config("thread-b").callable_team_id is None
    assert agent.thread_config_manager.get_config("thread-c").callable_team_id is None
    assert "" in agent.invalidated
