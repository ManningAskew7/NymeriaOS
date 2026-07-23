"""team_manage tool tests (backlog #100 phase 2).

Exercises the tool through ``.invoke(input, config=...)`` so the
InjectedToolArg config path is real, against a fake agent carrying a REAL
TeamManager and ThreadConfigManager (the shared services under test are the
same ones behind the REST routes and the nym verb).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.team_manager import TeamManager
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.tools.teams import team_manage


class _Repo:
    def __init__(self, owned: list[str]):
        self._owned = list(owned)

    def list_threads_for_user(self, user_id: str):
        return list(self._owned)

    def get_thread_owner(self, thread_id: str):
        return "u1" if thread_id in self._owned else None

    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(role="user")


class _Agent:
    def __init__(self, data_dir: Path, owned: list[str]):
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.accounts_repo = _Repo(owned)
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


def _cfg(user_id="u1", thread_id="t-main"):
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


@pytest.fixture
def agent(tmp_path: Path, monkeypatch):
    fake = _Agent(tmp_path, owned=["t-main", "t-a", "t-b"])
    fake.thread_config_manager.save_config(
        ThreadConfig(thread_id="t-a", callable=True, callable_name="HelperA")
    )
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: fake)
    return fake


@pytest.fixture
def published(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        "nymeria.core.team_manager.publish_teams_changed",
        lambda user_id, *, team_id="", reason="": events.append(
            {"user_id": user_id, "team_id": team_id, "reason": reason}
        ),
    )
    return events


def _run(args, user_id="u1"):
    return team_manage.invoke(args, config=_cfg(user_id=user_id))


def test_create_list_show_roundtrip(agent, published):
    out = _run({"action": "create", "name": "Ops", "description": "Ops crew"})
    assert out.startswith("[Success]: Created team 'Ops'")

    listed = _run({"action": "list"})
    assert "Ops" in listed and "0 member(s)" in listed

    shown = _run({"action": "show", "team": "Ops"})
    assert "[Team]: Ops" in shown
    assert "Description: Ops crew" in shown
    assert "(none; empty teams are legal)" in shown
    assert "Team memory keys: 0" in shown

    assert published and published[-1]["reason"] == "created"
    # Entity creation rebuilds nothing (no members changed).
    assert agent.invalidated == []


def test_create_collision_and_missing_name(agent):
    _run({"action": "create", "name": "Ops"})
    assert "[Error]" in _run({"action": "create", "name": "ops"})
    assert "[Error]" in _run({"action": "create"})


def test_rename_is_store_only_and_publishes(agent, published):
    _run({"action": "create", "name": "Ops"})
    agent.invalidated.clear()
    out = _run({"action": "rename", "team": "Ops", "name": "Operations"})
    assert out.startswith("[Success]: Renamed team to 'Operations'")
    # O(1) rename: no graph invalidation, no member config touched.
    assert agent.invalidated == []
    assert published[-1]["reason"] == "renamed"
    assert agent.team_manager.team_names("u1") == {
        published[-1]["team_id"]: "Operations"
    }


def test_describe_set_and_clear(agent):
    _run({"action": "create", "name": "Ops"})
    assert "[Success]" in _run(
        {"action": "describe", "team": "Ops", "description": "The crew"}
    )
    shown = _run({"action": "show", "team": "Ops"})
    assert "Description: The crew" in shown
    assert "Cleared description" in _run(
        {"action": "describe", "team": "Ops", "description": ""}
    )


def test_add_thread_by_callable_name_fans_out(agent, published):
    _run({"action": "create", "name": "Ops"})
    agent.invalidated.clear()
    out = _run({"action": "add_thread", "team": "Ops", "thread": "helpera"})
    assert out.startswith("[Success]: Thread t-a joined team 'Ops'")

    tc = agent.thread_config_manager.get_config("t-a")
    assert tc.callable_team_id and tc.callable_team_name is None
    # Fan-out: every owned thread plus the "" sentinel.
    assert set(agent.invalidated) == {"t-main", "t-a", "t-b", ""}
    assert published[-1]["reason"] == "membership"

    # Idempotent re-add is informational, not a mutation.
    agent.invalidated.clear()
    assert "[Info]" in _run({"action": "add_thread", "team": "Ops", "thread": "t-a"})
    assert agent.invalidated == []


def test_remove_thread_and_non_member_info(agent):
    _run({"action": "create", "name": "Ops"})
    _run({"action": "add_thread", "team": "Ops", "thread": "t-a"})
    assert "[Info]" in _run(
        {"action": "remove_thread", "team": "Ops", "thread": "t-b"}
    )
    out = _run({"action": "remove_thread", "team": "Ops", "thread": "t-a"})
    assert "left team 'Ops'" in out
    tc = agent.thread_config_manager.get_config("t-a")
    assert (tc.callable_team_id or None) is None


def test_delete_unteams_members(agent, published):
    _run({"action": "create", "name": "Ops"})
    _run({"action": "add_thread", "team": "Ops", "thread": "t-a"})
    _run({"action": "add_thread", "team": "Ops", "thread": "t-b"})
    out = _run({"action": "delete", "team": "Ops"})
    assert "[Success]: Deleted team 'Ops'" in out
    assert "2 member thread(s) are now unteamed" in out
    assert agent.team_manager.get_store_cached("u1").teams == []
    for thread_id in ("t-a", "t-b"):
        tc = agent.thread_config_manager.get_config(thread_id)
        assert (tc.callable_team_id or None) is None
    assert published[-1]["reason"] == "deleted"


def test_unknown_team_thread_and_action_errors(agent):
    assert "[Error]: Unknown team" in _run({"action": "show", "team": "Nope"})
    _run({"action": "create", "name": "Ops"})
    assert "[Error]" in _run(
        {"action": "add_thread", "team": "Ops", "thread": "not-owned"}
    )
    assert "[Error]: action must be one of" in _run({"action": "bogus"})
    assert "requires team=" in _run({"action": "show"})
    assert "requires thread=" in _run({"action": "add_thread", "team": "Ops"})


def test_team_ref_accepts_id_and_name(agent):
    _run({"action": "create", "name": "Ops"})
    team_id = next(iter(agent.team_manager.team_names("u1")))
    assert "[Team]: Ops" in _run({"action": "show", "team": team_id})
    assert "[Team]: Ops" in _run({"action": "show", "team": "ops"})


def test_add_thread_move_between_teams_banks_legacy_name(agent):
    """A tool-path move between teams banks the OLD team's legacy name.

    The thread carries a raw-edit legacy team (id + deprecated name, store
    migrated empty); add_thread into a new team must fold that name into the
    store before the config clear (the shared set_thread_team applier).
    """
    assert agent.team_manager.get_store_cached("u1").teams == []
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="t-a",
            callable=True,
            callable_name="HelperA",
            callable_team_id="team-old",
            callable_team_name="Old Ops",
        )
    )
    _run({"action": "create", "name": "New Team"})
    out = _run({"action": "add_thread", "team": "New Team", "thread": "t-a"})
    assert out.startswith("[Success]")
    tc = agent.thread_config_manager.get_config("t-a")
    assert tc.callable_team_id != "team-old" and tc.callable_team_name is None
    assert agent.team_manager.resolve_team_name("u1", "team-old") == "Old Ops"


def test_show_does_not_adopt_dangling_id(agent):
    """show is read-only: a dangling membership id renders without a store write."""
    assert agent.team_manager.get_store_cached("u1").teams == []
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="t-a",
            callable=True,
            callable_name="HelperA",
            callable_team_id="team-raw",
            callable_team_name="Raw Crew",
        )
    )
    shown = _run({"action": "show", "team": "team-raw"})
    assert "[Team]: Raw Crew" in shown
    assert "- t-a (callable: HelperA)" in shown
    assert agent.team_manager.get_store_cached("u1").teams == []


def test_delete_partial_failure_is_honest_and_invalidates(agent, monkeypatch, published):
    """A mid-delete save failure reports the true partial state and fans out."""
    _run({"action": "create", "name": "Ops"})
    _run({"action": "add_thread", "team": "Ops", "thread": "t-a"})
    _run({"action": "add_thread", "team": "Ops", "thread": "t-b"})

    real_save = agent.thread_config_manager.save_config
    calls = {"n": 0}

    def failing_save(config):
        calls["n"] += 1
        if calls["n"] == 2:  # second member's unteam write fails
            return False
        return real_save(config)

    monkeypatch.setattr(agent.thread_config_manager, "save_config", failing_save)
    agent.invalidated.clear()
    out = _run({"action": "delete", "team": "Ops"})
    assert out.startswith("[Error]")
    assert "was NOT deleted" in out
    assert "1 member thread(s) were already unteamed (t-a)" in out
    # The persisted partial change still fanned out and nudged clients.
    assert "" in agent.invalidated
    assert published[-1]["reason"] == "membership"
    assert agent.team_manager.team_names("u1")  # entity kept for the retry
