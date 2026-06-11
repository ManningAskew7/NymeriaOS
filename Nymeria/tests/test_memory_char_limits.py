from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.accounts import AccountsRepo
from nymeria.core.memory_limits import (
    DEFAULT_MEMORY_MAX_ENTRIES,
    DEFAULT_MEMORY_VALUE_MAX_CHARS,
    MAX_MEMORY_MAX_ENTRIES,
    MAX_MEMORY_VALUE_MAX_CHARS,
    get_memory_max_entries,
    get_memory_value_max_chars,
    memory_entries_full_error,
    validate_profile_memory_write,
)
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


def test_validate_profile_memory_write_entry_cap_blocks_new_key_allows_upsert(tmp_path):
    manager = UserProfileManager(tmp_path)
    profile = manager.get_profile("user")
    profile.add_memory("a", "one")
    profile.add_memory("b", "two")

    blocked = validate_profile_memory_write(
        profile, key="c", value="three", limit=8000, max_entries=2
    )
    upsert = validate_profile_memory_write(
        profile, key="a", value="replacement", limit=8000, max_entries=2
    )

    assert blocked == memory_entries_full_error(2)
    assert upsert is None


def test_memory_cap_resolvers_accept_mapping_and_attribute_settings():
    assert get_memory_max_entries({"memory_max_entries": 5}) == 5
    assert get_memory_value_max_chars({"memory_value_max_chars": 2500}) == 2500

    attrs = SimpleNamespace(memory_max_entries=7, memory_value_max_chars=1500)
    assert get_memory_max_entries(attrs) == 7
    assert get_memory_value_max_chars(attrs) == 1500


def test_memory_cap_resolvers_fall_back_on_garbage_values():
    garbage = SimpleNamespace(
        memory_max_entries="not-a-number",
        memory_value_max_chars=object(),
    )
    assert get_memory_max_entries(garbage) == DEFAULT_MEMORY_MAX_ENTRIES
    assert get_memory_value_max_chars(garbage) == DEFAULT_MEMORY_VALUE_MAX_CHARS

    zeros = {"memory_max_entries": 0, "memory_value_max_chars": -3}
    assert get_memory_max_entries(zeros) == DEFAULT_MEMORY_MAX_ENTRIES
    assert get_memory_value_max_chars(zeros) == DEFAULT_MEMORY_VALUE_MAX_CHARS


def test_memory_cap_resolvers_clamp_to_ceiling():
    huge = {"memory_max_entries": 10**9, "memory_value_max_chars": 10**9}
    assert get_memory_max_entries(huge) == MAX_MEMORY_MAX_ENTRIES
    assert get_memory_value_max_chars(huge) == MAX_MEMORY_VALUE_MAX_CHARS


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


def test_memory_rest_save_accepts_long_value_when_cap_raised(
    tmp_path, api_client_builder
):
    """A >1000-char value passes the REST schema when the settings cap allows it."""
    agent = _MemoryApiAgent(tmp_path)
    agent.settings = SimpleNamespace(
        data_dir=tmp_path,
        memory_char_limit=10_000,
        memory_value_max_chars=5_000,
    )
    settings = api_client_builder.settings(tmp_path, memory_char_limit=10_000)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    headers = api_client_builder.auth(token)
    long_value = "x" * 2_500

    response = client.post(
        "/users/owner/memories",
        headers=headers,
        json={"key": "long", "value": long_value},
    )

    assert response.status_code == 200
    stored = agent.profile_manager.get_profile("owner").get_memory("long")
    assert stored is not None
    assert stored.value == long_value


def test_memory_rest_save_blocks_at_entry_cap_but_allows_upsert(
    tmp_path, api_client_builder
):
    agent = _MemoryApiAgent(tmp_path)
    agent.settings = SimpleNamespace(
        data_dir=tmp_path,
        memory_char_limit=10_000,
        memory_max_entries=1,
    )
    settings = api_client_builder.settings(tmp_path, memory_char_limit=10_000)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    headers = api_client_builder.auth(token)

    first = client.post(
        "/users/owner/memories",
        headers=headers,
        json={"key": "a", "value": "one"},
    )
    blocked = client.post(
        "/users/owner/memories",
        headers=headers,
        json={"key": "b", "value": "two"},
    )
    upsert = client.post(
        "/users/owner/memories",
        headers=headers,
        json={"key": "a", "value": "replacement"},
    )

    assert first.status_code == 200
    assert blocked.status_code == 400
    assert blocked.json()["detail"] == memory_entries_full_error(1)
    assert upsert.status_code == 200
