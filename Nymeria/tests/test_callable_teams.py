from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from nymeria.core.accounts import AccountsRepo
from nymeria.core.agent import NymeriaAgent
from nymeria.core.team_manager import TeamManager
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.vendor.react_agent.nodes import SafeToolNode


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.team_manager = TeamManager(
            data_dir,
            thread_config_manager=self.thread_config_manager,
            accounts_repo=self.accounts_repo,
        )
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        return []


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent, str]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    return client, agent, token


def _isolation_agent(tmp_path: Path) -> NymeriaAgent:
    """Agent with a teamed caller, two teamed callables, and one unteamed."""
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
    return agent


def test_team_scoped_callable_filter_isolates_both_directions(tmp_path: Path):
    """Backlog #97: teams are bubbles; unteamed threads see only unteamed."""
    agent = _isolation_agent(tmp_path)

    scoped = agent._get_team_scoped_callable_threads(user_id="owner", caller_thread_id="caller")
    assert [tc.callable_name for tc in scoped] == ["AgentA"]

    # An unteamed caller (no saved config) sees only unteamed callables.
    unscoped = agent._get_team_scoped_callable_threads(user_id="owner", caller_thread_id="free-caller")
    assert {tc.callable_name for tc in unscoped} == {"AgentFree"}


def test_runtime_visibility_guard_isolates_both_directions(tmp_path: Path):
    agent = _isolation_agent(tmp_path)

    # Teamed caller: same team only.
    assert agent.is_callable_visible_to_thread("caller", "agent-a") is True
    assert agent.is_callable_visible_to_thread("caller", "agent-b") is False
    assert agent.is_callable_visible_to_thread("caller", "agent-free") is False

    # Unteamed caller: unteamed targets only.
    assert agent.is_callable_visible_to_thread("free-caller", "agent-free") is True
    assert agent.is_callable_visible_to_thread("free-caller", "agent-a") is False
    assert agent.is_callable_visible_to_thread("free-caller", "agent-b") is False

    # No caller thread context: team scoping does not apply.
    assert agent.is_callable_visible_to_thread("", "agent-a") is True


def test_branch_clone_carries_callable_team(tmp_path: Path):
    """Branched spawns inherit the parent's team via the full config clone."""
    from nymeria.core.thread_branch import _clone_thread_config

    manager = ThreadConfigManager(tmp_path)
    manager.save_config(
        ThreadConfig(
            thread_id="source",
            callable=True,
            callable_name="Source",
            callable_team_id="team-a",
            callable_team_name="Ops",
        )
    )
    agent = SimpleNamespace(
        thread_config_manager=manager,
        accounts_repo=SimpleNamespace(
            list_threads_for_user=lambda _user_id: ["source"]
        ),
        invalidate_thread_config_cache=lambda thread_id: None,
        sync_agent_tools=lambda: None,
    )

    cloned, callable_name = _clone_thread_config(
        agent,
        user_id="owner",
        source_thread_id="source",
        target_thread_id="branch-1",
        title="Branch",
    )
    assert cloned is True
    assert callable_name
    branch_tc = manager.get_config("branch-1")
    assert branch_tc is not None
    assert branch_tc.callable_team_id == "team-a"
    # The deprecated name is never re-persisted (backlog #100): the clone
    # carries the membership id only; display names resolve from the store.
    assert branch_tc.callable_team_name is None


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


def test_thread_team_api_moves_membership_and_clears_on_delete(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
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
    assert team["description"] is None
    assert set(team["thread_ids"]) == {"thread-a", "thread-b"}

    cfg_a = agent.thread_config_manager.get_config("thread-a")
    assert cfg_a is not None
    assert cfg_a.callable_team_id == team["id"]
    # Deprecated (backlog #100): the name lives in the team entity store, the
    # config carries only the membership id.
    assert cfg_a.callable_team_name is None
    assert agent.team_manager.resolve_team_name("owner", team["id"]) == "Ops"

    renamed = client.patch(
        f"/thread-teams/{team['id']}",
        headers=headers,
        json={"name": "Ops Team", "thread_ids": ["thread-b", "thread-c"]},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Ops Team"
    assert set(renamed.json()["thread_ids"]) == {"thread-b", "thread-c"}

    assert agent.thread_config_manager.get_config("thread-a").callable_team_id is None
    assert agent.thread_config_manager.get_config("thread-c").callable_team_id == team["id"]
    # The REST config response keeps serving callable_team_name, derived.
    cfg_b_response = client.get("/threads/thread-b/config", headers=headers)
    assert cfg_b_response.status_code == 200
    assert cfg_b_response.json()["callable_team_name"] == "Ops Team"

    listed = client.get("/thread-teams", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    deleted = client.delete(f"/thread-teams/{team['id']}", headers=headers)
    assert deleted.status_code == 200
    assert agent.thread_config_manager.get_config("thread-b").callable_team_id is None
    assert agent.thread_config_manager.get_config("thread-c").callable_team_id is None
    assert "" in agent.invalidated
    assert agent.team_manager.get_team("owner", team["id"]) is None


def test_team_rename_is_store_only(tmp_path: Path, api_client_builder, monkeypatch):
    """A rename or description edit is an O(1) store write (backlog #100).

    No member thread config is written and no graph is invalidated; member
    configs keep serving the new name because the REST response derives it
    from the store.
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = {"Authorization": f"Bearer {token}"}
    for thread_id in ("thread-a", "thread-b"):
        agent.accounts_repo.claim_thread(thread_id, "owner")

    created = client.post(
        "/thread-teams",
        headers=headers,
        json={"name": "Ops", "thread_ids": ["thread-a", "thread-b"]},
    )
    assert created.status_code == 200
    team_id = created.json()["id"]

    config_writes = {"count": 0}
    real_save = agent.thread_config_manager.save_config

    def counting_save(config):
        config_writes["count"] += 1
        return real_save(config)

    monkeypatch.setattr(agent.thread_config_manager, "save_config", counting_save)
    agent.invalidated.clear()

    renamed = client.patch(
        f"/thread-teams/{team_id}",
        headers=headers,
        json={"name": "Ops Team", "description": "Shared ops helpers"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Ops Team"
    assert renamed.json()["description"] == "Shared ops helpers"
    assert config_writes["count"] == 0
    assert agent.invalidated == []

    cfg_response = client.get("/threads/thread-a/config", headers=headers)
    assert cfg_response.json()["callable_team_name"] == "Ops Team"


def test_empty_team_and_membership_via_config_patch(tmp_path: Path, api_client_builder):
    """Empty teams are legal; PATCH /threads/{id}/config moves membership.

    A config PATCH with a callable_team_id keeps the entity store coherent
    (adoption), clears the deprecated callable_team_name instead of storing
    it, and the response derives the display name from the store.
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = {"Authorization": f"Bearer {token}"}
    agent.accounts_repo.claim_thread("thread-a", "owner")

    created = client.post(
        "/thread-teams",
        headers=headers,
        json={"name": "Ops", "description": "Ops helpers", "thread_ids": []},
    )
    assert created.status_code == 200
    team = created.json()
    assert team["thread_ids"] == []
    assert team["description"] == "Ops helpers"

    listed = client.get("/thread-teams", headers=headers)
    assert listed.json()["total"] == 1
    assert listed.json()["teams"][0]["thread_ids"] == []

    patched = client.patch(
        "/threads/thread-a/config",
        headers=headers,
        json={"callable_team_id": team["id"], "callable_team_name": "Ignored"},
    )
    assert patched.status_code == 200
    # Derived from the store, not from the (ignored) request field.
    assert patched.json()["callable_team_name"] == "Ops"
    cfg = agent.thread_config_manager.get_config("thread-a")
    assert cfg.callable_team_id == team["id"]
    assert cfg.callable_team_name is None
    assert agent.team_manager.members("owner", team["id"]) == ["thread-a"]

    # Unteaming everyone via thread_ids=[] keeps the team entity.
    cleared = client.patch(
        f"/thread-teams/{team['id']}",
        headers=headers,
        json={"thread_ids": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["thread_ids"] == []
    assert agent.team_manager.get_team("owner", team["id"]) is not None
    assert agent.thread_config_manager.get_config("thread-a").callable_team_id is None


def test_team_routes_publish_thread_teams_changed(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """Every team mutation publishes the thread_teams_changed sync event.

    Backlog #100 phase 2 GUI freshness: create/update/delete on the teams
    API and a membership move via the config PATCH all nudge clients through
    the shared after_team_change chokepoint.
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = {"Authorization": f"Bearer {token}"}
    agent.accounts_repo.claim_thread("thread-a", "owner")

    events: list[dict] = []
    monkeypatch.setattr(
        "nymeria.core.team_manager.publish_teams_changed",
        lambda user_id, *, team_id="", reason="": events.append(
            {"user_id": user_id, "team_id": team_id, "reason": reason}
        ),
    )

    team = client.post(
        "/thread-teams", headers=headers, json={"name": "Ops", "thread_ids": []}
    ).json()
    assert events[-1] == {"user_id": "owner", "team_id": team["id"], "reason": "created"}

    client.patch(
        f"/thread-teams/{team['id']}", headers=headers, json={"name": "Ops Team"}
    )
    assert events[-1]["reason"] == "updated"

    client.patch(
        "/threads/thread-a/config",
        headers=headers,
        json={"callable_team_id": team["id"]},
    )
    assert events[-1] == {
        "user_id": "owner",
        "team_id": team["id"],
        "reason": "membership",
    }

    # A no-op re-set of the same team publishes (and rebuilds) nothing.
    events.clear()
    agent.invalidated.clear()
    client.patch(
        "/threads/thread-a/config",
        headers=headers,
        json={"callable_team_id": team["id"]},
    )
    assert events == []
    assert "" not in agent.invalidated

    client.delete(f"/thread-teams/{team['id']}", headers=headers)
    assert events[-1]["reason"] == "deleted"


def test_membership_move_banks_previous_team_legacy_name(
    tmp_path: Path, api_client_builder
):
    """Moving a thread between teams via the teams API banks the old name.

    The shared set_thread_team applier folds the PREVIOUS team's surviving
    legacy config name into the store before clearing it, so a
    dangling-legacy team's display name survives its sole member moving to
    another team (previously only the config PATCH path banked it).
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = {"Authorization": f"Bearer {token}"}
    agent.accounts_repo.claim_thread("thread-a", "owner")

    # Migrate the store while empty, then a raw-edit legacy teamed config.
    assert agent.team_manager.get_store_cached("owner").teams == []
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            callable_team_id="team-old",
            callable_team_name="Old Ops",
        )
    )

    created = client.post(
        "/thread-teams",
        headers=headers,
        json={"name": "New Team", "thread_ids": ["thread-a"]},
    )
    assert created.status_code == 200
    cfg = agent.thread_config_manager.get_config("thread-a")
    assert cfg.callable_team_id == created.json()["id"]
    assert cfg.callable_team_name is None
    # The old team's name was banked before the config clear.
    assert agent.team_manager.resolve_team_name("owner", "team-old") == "Old Ops"


def test_unteam_patch_banks_legacy_name_before_clearing(tmp_path: Path, api_client_builder):
    """PATCH config unteaming a legacy thread banks its name into the store.

    The previous team's surviving legacy name must reach the entity store
    BEFORE the save clears it from the config, even when the store was
    migrated while this team did not exist yet (a raw-edit dangling id).
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = {"Authorization": f"Bearer {token}"}
    agent.accounts_repo.claim_thread("thread-a", "owner")

    # Migrate the store while the user has no teams (empty marker file).
    assert agent.team_manager.get_store_cached("owner").teams == []
    # Then a legacy teamed config appears (simulating a raw on-disk edit).
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            callable_team_id="team-x",
            callable_team_name="Ops",
        )
    )

    patched = client.patch(
        "/threads/thread-a/config",
        headers=headers,
        json={"callable_team_id": ""},
    )
    assert patched.status_code == 200
    cfg = agent.thread_config_manager.get_config("thread-a")
    assert cfg.callable_team_id is None
    assert cfg.callable_team_name is None
    # The name was banked into the entity store before the clear.
    assert agent.team_manager.resolve_team_name("owner", "team-x") == "Ops"


def test_legacy_config_names_migrate_into_team_store(tmp_path: Path, api_client_builder):
    """Lazy migration synthesizes entities from legacy config name pairs.

    First-seen name wins on drift (threads scanned sorted), and the REST list
    serves the migrated names without any config write.
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = {"Authorization": f"Bearer {token}"}
    for thread_id in ("thread-a", "thread-b", "thread-c"):
        agent.accounts_repo.claim_thread(thread_id, "owner")
    # Legacy on-disk state: names still stored on configs, with drift.
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", callable_team_id="team-legacy", callable_team_name="Ops")
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-b", callable_team_id="team-legacy", callable_team_name="Operations")
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-c", callable_team_id="team-other", callable_team_name="Sales")
    )

    listed = client.get("/thread-teams", headers=headers)
    assert listed.status_code == 200
    by_id = {team["id"]: team for team in listed.json()["teams"]}
    assert by_id["team-legacy"]["name"] == "Ops"  # thread-a seen first (sorted)
    assert set(by_id["team-legacy"]["thread_ids"]) == {"thread-a", "thread-b"}
    assert by_id["team-other"]["name"] == "Sales"
    # Migration wrote entities into the store without touching configs.
    assert agent.team_manager.resolve_team_name("owner", "team-legacy") == "Ops"
    assert (
        agent.thread_config_manager.get_config("thread-a").callable_team_name == "Ops"
    )
