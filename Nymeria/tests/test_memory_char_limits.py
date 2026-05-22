from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.accounts import AccountsRepo
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import memory as memory_tools
from nymeria.tools import thread_notes


def _patch_memory_settings(monkeypatch, tmp_path, *, limit: int) -> SimpleNamespace:
    settings = SimpleNamespace(data_dir=tmp_path, memory_char_limit=limit)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(memory_tools, "_profile_manager", None)
    monkeypatch.setattr(thread_notes, "_notes_dir", None)
    return settings


def _tool_config(*, user_id: str = "user", thread_id: str = "thread") -> dict:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def test_memory_add_global_blocks_growth_over_limit(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path, limit=20)

    first = memory_tools.memory_add.func(
        scope="global",
        key="a",
        content="1234567890",
        config=_tool_config(),
    )
    second = memory_tools.memory_add.func(
        scope="global",
        key="b",
        content="1234567890",
        config=_tool_config(),
    )

    assert first.startswith("[Saved]:")
    assert second.startswith("[Error]: Global memory is full")
    manager = UserProfileManager(tmp_path)
    profile = manager.get_profile("user")
    assert [memory.key for memory in profile.memories] == ["a"]


def test_memory_edit_global_allows_reducing_existing_over_limit(monkeypatch, tmp_path):
    settings = _patch_memory_settings(monkeypatch, tmp_path, limit=100)
    config = _tool_config()
    memory_tools.memory_add.func(scope="global", key="a", content="1234567890", config=config)
    memory_tools.memory_add.func(scope="global", key="b", content="1234567890", config=config)

    settings.memory_char_limit = 10
    result = memory_tools.memory_edit.func(
        scope="global",
        key="a",
        find="1234567890",
        replace="12345",
        config=config,
    )

    assert result == "[Saved]: Updated 'a'."
    manager = UserProfileManager(tmp_path)
    assert manager.get_profile("user").get_memory("a").value == "12345"


def test_memory_add_thread_blocks_over_effective_limit(monkeypatch, tmp_path):
    _patch_memory_settings(monkeypatch, tmp_path, limit=8)

    result = memory_tools.memory_add.func(
        scope="thread",
        content="123456789",
        config=_tool_config(),
    )

    assert result.startswith("[Error]: Thread memory is full")
    assert thread_notes.read_notepad("thread") is None


def test_memory_edit_thread_allows_reducing_existing_over_limit(monkeypatch, tmp_path):
    settings = _patch_memory_settings(monkeypatch, tmp_path, limit=20)
    config = _tool_config()
    memory_tools.memory_add.func(scope="thread", content="123456789012", config=config)

    settings.memory_char_limit = 8
    result = memory_tools.memory_edit.func(
        scope="thread",
        find="9012",
        replace="9",
        config=config,
    )

    assert result.startswith("[Saved]: Text replaced")
    assert thread_notes.read_notepad("thread") == "123456789"


class _MemoryApiAgent:
    def __init__(self, data_dir):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.profile_manager = UserProfileManager(data_dir)
        self.settings = SimpleNamespace(data_dir=data_dir, memory_char_limit=20)

    def _get_memory_index(self, user_id: str):
        return None

    def sync_agent_tools(self) -> None:
        return None


def test_memory_rest_save_enforces_global_char_limit(tmp_path, api_client_builder):
    agent = _MemoryApiAgent(tmp_path)
    settings = api_client_builder.settings(tmp_path, memory_char_limit=20)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    headers = api_client_builder.auth(token)

    ok = client.post(
        "/users/owner/memories",
        headers=headers,
        json={"key": "a", "value": "1234567890"},
    )
    blocked = client.post(
        "/users/owner/memories",
        headers=headers,
        json={"key": "b", "value": "1234567890"},
    )

    assert ok.status_code == 200
    assert blocked.status_code == 400
    assert "Global memory is full" in blocked.json()["detail"]
