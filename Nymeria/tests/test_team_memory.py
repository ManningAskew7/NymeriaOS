"""Team memory scope tests (backlog #100 phase 3).

Exercises memory_add/memory_edit/memory_read with scope="team" through
``.invoke(input, config=...)`` (real InjectedToolArg path) against a fake
agent carrying a REAL TeamManager and ThreadConfigManager, mirroring
``test_team_manage_tool.py``. Caps ride the global memory settings knobs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.team_manager import TeamManager
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.tools import memory as memory_tools


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


def _cfg(thread_id="t-main", user_id="u1"):
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


@pytest.fixture
def agent(tmp_path: Path, monkeypatch):
    """Teamed t-main + callable teammate t-a, unteamed t-free; settings patched."""
    fake = _Agent(tmp_path, owned=["t-main", "t-a", "t-free"])
    team = fake.team_manager.create_team("u1", name="Ops", description="Ops crew")
    fake.team_manager.default_team_id = team.id  # test-side convenience handle
    fake.thread_config_manager.save_config(
        ThreadConfig(thread_id="t-main", callable_team_id=team.id)
    )
    fake.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="t-a",
            callable=True,
            callable_name="HelperA",
            callable_team_id=team.id,
        )
    )
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: fake)
    settings = SimpleNamespace(data_dir=tmp_path, memory_char_limit=8000)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    memory_tools._profile_manager = None
    fake.settings = settings
    return fake


def _add(content, key=None, thread_id="t-main"):
    args = {"scope": "team", "content": content}
    if key is not None:
        args["key"] = key
    return memory_tools.memory_add.invoke(args, config=_cfg(thread_id=thread_id))


def _edit(find, replace="", key=None, thread_id="t-main"):
    args = {"scope": "team", "find": find, "replace": replace}
    if key is not None:
        args["key"] = key
    return memory_tools.memory_edit.invoke(args, config=_cfg(thread_id=thread_id))


def _read(key=None, query=None, thread_id="t-main"):
    args = {"scope": "team"}
    if key is not None:
        args["key"] = key
    if query is not None:
        args["query"] = query
    return memory_tools.memory_read.invoke(args, config=_cfg(thread_id=thread_id))


def test_add_read_roundtrip_renders_identity_header(agent):
    out = _add("Staging is https://stage.example.com", key="api_endpoint")
    assert out.startswith("[Saved]: Team memory 'api_endpoint'")

    shown = _read()
    assert "[Team]: Ops" in shown
    assert "Description: Ops crew" in shown
    # Roster: callable name for teammates, acting thread marked.
    assert "HelperA" in shown
    assert "t-main (this thread)" in shown
    assert "Team memory (1 entries):" in shown
    assert "- api_endpoint: Staging is https://stage.example.com" in shown

    assert _read(key="api_endpoint") == (
        "api_endpoint: Staging is https://stage.example.com"
    )
    assert "[Info]: No team memory with key 'nope'." == _read(key="nope")

    filtered = _read(query="staging")
    assert filtered.startswith("Team memories (1 shown):")
    assert "[Info]: No team memories match 'zzz'." == _read(query="zzz")


def test_empty_team_read_names_the_next_step(agent):
    shown = _read()
    assert "[Team]: Ops" in shown
    assert "none yet" in shown and "memory_add(scope='team'" in shown


def test_unteamed_thread_gets_clear_error(agent):
    for out in (
        _add("x", key="k", thread_id="t-free"),
        _edit("a", "b", key="k", thread_id="t-free"),
        _read(thread_id="t-free"),
    ):
        assert out.startswith("[Error]")
        assert "not in a callable team" in out


def test_add_requires_key_and_empty_content_is_noop(agent):
    assert "requires a key" in _add("x")
    out = _add("", key="k")
    assert out.startswith("[Info]")
    assert "memory_edit(scope='team'" in out
    assert agent.team_manager.get_team("u1", agent.team_manager.default_team_id).memories == []


def test_edit_find_replace_and_delete_on_empty(agent):
    _add("deadline Friday", key="plan")
    assert "[Error]: No team memory with key 'other'." == _edit(
        "a", "b", key="other"
    )
    assert "Could not find 'zzz'" in _edit("zzz", "b", key="plan")

    out = _edit("Friday", "Monday", key="plan")
    assert out == "[Saved]: Updated team memory 'plan'."
    assert _read(key="plan") == "plan: deadline Monday"

    # Empty find + non-empty replace sets the whole value.
    assert "[Saved]" in _edit("", "rewritten", key="plan")
    assert _read(key="plan") == "plan: rewritten"

    # Whole-value delete removes the key.
    assert _edit("", "", key="plan") == "[Deleted]: Removed team memory 'plan'."
    assert "No team memory with key 'plan'" in _read(key="plan")


def test_caps_ride_the_global_memory_settings(agent):
    agent.settings.memory_max_entries = 1
    assert "[Saved]" in _add("v1", key="k1")
    over = _add("v2", key="k2")
    assert "Memory limit reached (1 memories)" in over
    # Upserting the existing key still passes the entry cap.
    assert "[Saved]" in _add("v1b", key="k1")

    del agent.settings.memory_max_entries
    agent.settings.memory_value_max_chars = 5
    _add("abcdefghij", key="k3")
    assert _read(key="k3") == "k3: abcde"

    del agent.settings.memory_value_max_chars
    agent.settings.memory_char_limit = 30
    over = _add("x" * 100, key="k4")
    assert over.startswith("[Error]: Team memory is full")


def test_memory_clear_all_leaves_team_memory_alone(agent):
    _add("shared", key="team_fact")
    memory_tools.memory_add.invoke(
        {"scope": "global", "content": "solo", "key": "user_fact"}, config=_cfg()
    )
    out = memory_tools.memory_clear_all.invoke({}, config=_cfg())
    assert out.startswith("[Cleared]")
    team = agent.team_manager.get_team("u1", agent.team_manager.default_team_id)
    assert [m.key for m in team.memories] == ["team_fact"]


def test_rag_hooks_fire_with_per_team_keys(agent, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        memory_tools,
        "rag_index_team_memory",
        lambda user_id, team_id, key, value: calls.append(
            ("index", user_id, team_id, key, value)
        ),
    )
    monkeypatch.setattr(
        memory_tools,
        "rag_remove_team_memory",
        lambda user_id, team_id, key: calls.append(("remove", user_id, team_id, key)),
    )
    team_id = agent.team_manager.default_team_id
    _add("v", key="k")
    _edit("v", "w", key="k")
    _edit("", "", key="k")
    assert calls == [
        ("index", "u1", team_id, "k", "v"),
        ("index", "u1", team_id, "k", "w"),
        ("remove", "u1", team_id, "k"),
    ]


def test_dangling_team_id_read_is_synthetic_and_write_adopts(agent):
    """Raw-edited membership: read renders without a store write, write adopts."""
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="t-free",
            callable_team_id="team-raw",
            callable_team_name="Raw Crew",
        )
    )
    before = len(agent.team_manager.get_store_cached("u1").teams)
    shown = _read(thread_id="t-free")
    assert "[Team]: Raw Crew" in shown
    assert len(agent.team_manager.get_store_cached("u1").teams) == before

    assert "[Saved]" in _add("v", key="k", thread_id="t-free")
    adopted = agent.team_manager.get_team("u1", "team-raw")
    assert adopted is not None and adopted.name == "Raw Crew"
    assert adopted.get_memory("k").value == "v"


def test_team_key_length_cap(agent):
    assert "200 characters or fewer" in _add("v", key="k" * 201)
