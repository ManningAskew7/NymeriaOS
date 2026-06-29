"""Tests for thread configuration and callable-team routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, ThreadLLMConfig
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import SEED_TOOLS


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.invalidated: list[str] = []
        self.synced_tools = 0

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder) -> tuple[TestClient, FakeAgent, str]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    agent.synced_tools = 0
    return client, agent, token


def test_thread_notepad_round_trip(tmp_path: Path, api_client_builder, monkeypatch):
    from nymeria.tools import thread_notes

    # Redirect the notepad store to a temp dir (thread_notes resolves its dir via
    # the global get_settings(), not the injected test settings).
    monkeypatch.setattr(thread_notes, "_notes_dir", tmp_path / "thread_notes")

    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-notepad"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    # Empty to start.
    empty = client.get(f"/threads/{thread_id}/notepad", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["content"] == ""
    assert empty.json()["char_count"] == 0
    assert empty.json()["char_limit"] > 0

    # Write content.
    note = "Remember: ship on Friday."
    put = client.put(
        f"/threads/{thread_id}/notepad",
        headers=headers,
        json={"content": note},
    )
    assert put.status_code == 200
    assert put.json()["content"] == note
    assert put.json()["char_count"] == len(note)

    # Read it back.
    got = client.get(f"/threads/{thread_id}/notepad", headers=headers)
    assert got.json()["content"] == note

    # Blank content clears the notepad.
    cleared = client.put(
        f"/threads/{thread_id}/notepad",
        headers=headers,
        json={"content": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["content"] == ""
    assert client.get(f"/threads/{thread_id}/notepad", headers=headers).json()["content"] == ""


def test_thread_notepad_rejects_over_limit(tmp_path: Path, api_client_builder, monkeypatch):
    from nymeria.tools import thread_notes
    from nymeria.core import memory_limits

    monkeypatch.setattr(thread_notes, "_notes_dir", tmp_path / "thread_notes")
    # Force a tiny effective limit so the write is rejected.
    monkeypatch.setattr(
        memory_limits, "get_effective_thread_memory_char_limit", lambda *a, **k: 10
    )
    monkeypatch.setattr(
        thread_notes, "get_effective_thread_memory_char_limit", lambda *a, **k: 10
    )

    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-notepad-limit"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    resp = client.put(
        f"/threads/{thread_id}/notepad",
        headers=headers,
        json={"content": "x" * 200},
    )
    assert resp.status_code == 400
    assert "limit" in resp.json()["detail"].lower()


def test_thread_config_update_syncs_callable_metadata_and_partial_llm(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-config"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={
            "instructions": "Use the ops checklist.",
            "llm_config": {
                "provider": "openai",
                "model": "gpt-5.5",
                "temperature": 0.0,
                "max_tokens": 1,
            },
            "callable": True,
            "callable_name": "OpsHelper",
            "callable_description": "Help with ops.",
            "callable_max_iterations": 7,
            "enabled_skills": ["skill-a"],
            "disabled_skills": ["skill-b"],
            "inject_todos_in_prompt": True,
            "memory_char_limit": 6000,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert body["instructions"] == "Use the ops checklist."
    assert body["llm_config"]["provider"] == "openai"
    assert body["llm_config"]["model"] == "gpt-5.5"
    assert body["llm_config"]["temperature"] == 0.0
    assert body["llm_config"]["max_tokens"] == 1
    assert body["callable"] is True
    assert body["callable_name"] == "OpsHelper"
    assert body["callable_description"] == "Help with ops."
    assert body["callable_max_iterations"] == 7
    assert body["enabled_skills"] == ["skill-a"]
    assert body["disabled_skills"] == ["skill-b"]
    assert body["inject_todos_in_prompt"] is True
    assert body["memory_char_limit"] == 6000
    assert body["has_customizations"] is True

    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "openai"
    assert saved.llm_config.model == "gpt-5.5"
    assert saved.memory_char_limit == 6000
    meta = agent.thread_metadata_manager.get_thread("owner", thread_id)
    assert meta is not None
    assert meta.title == "OpsHelper"
    assert agent.invalidated == [thread_id]
    assert agent.synced_tools == 1

    clear_model = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"llm_config": {"model": None}},
    )

    assert clear_model.status_code == 200
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "openai"
    assert saved.llm_config.model is None

    clear_memory_limit = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"clear_memory_char_limit": True},
    )

    assert clear_memory_limit.status_code == 200
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.memory_char_limit is None


def test_thread_config_dreaming_partial_merge_and_clear(
    tmp_path: Path,
    api_client_builder,
):
    # Locks the dreaming call site of the shared `_merge_optional_submodel`
    # helper: build-from-None drops explicit None, merge-onto-existing applies
    # explicit None while preserving untouched fields, and clear nulls the whole
    # submodel.
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-dreaming"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    # Build from None: set fields take effect; unmentioned fields keep model
    # defaults (min_idle_minutes -> None, enabled -> request value).
    created = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={
            "dreaming": {
                "enabled": True,
                "min_interval_hours": 12,
                "model": "dream-model",
            }
        },
    )
    assert created.status_code == 200
    assert created.json()["dreaming"]["model"] == "dream-model"
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.dreaming is not None
    assert saved.dreaming.enabled is True
    assert saved.dreaming.min_interval_hours == 12
    assert saved.dreaming.model == "dream-model"
    assert saved.dreaming.min_idle_minutes is None
    # A dreaming-only update touches no callable/tool fields, so no tool sync.
    assert agent.synced_tools == 0

    # Merge onto existing: an explicit None nulls just that field; the other
    # fields set above survive (proves the setattr-loop branch, not a rebuild).
    merged = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"dreaming": {"model": None}},
    )
    assert merged.status_code == 200
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.dreaming is not None
    assert saved.dreaming.model is None
    assert saved.dreaming.enabled is True
    assert saved.dreaming.min_interval_hours == 12

    # clear_dreaming wins over a same-request dreaming payload and nulls it all.
    cleared = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"clear_dreaming": True, "dreaming": {"enabled": True}},
    )
    assert cleared.status_code == 200
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.dreaming is None

    # Build-from-None must DROP an explicit null, not force-set it. A fresh
    # thread hits the construct branch; DreamingConfig.enabled is a plain bool,
    # so force-setting enabled=None would raise (422). Dropping it yields the
    # model default (False), proving the construct-branch None filter.
    fresh_id = "thread-dreaming-fresh"
    agent.accounts_repo.claim_thread(fresh_id, "owner")
    built = client.patch(
        f"/threads/{fresh_id}/config",
        headers=headers,
        json={"dreaming": {"enabled": None, "min_interval_hours": 6}},
    )
    assert built.status_code == 200
    saved = agent.thread_config_manager.get_config(fresh_id)
    assert saved is not None
    assert saved.dreaming is not None
    assert saved.dreaming.enabled is False
    assert saved.dreaming.min_interval_hours == 6


def test_thread_config_reasoning_effort_validation_and_legacy_coercion(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-effort"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    accepted = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"llm_config": {"reasoning_effort": "xhigh"}},
    )
    assert accepted.status_code == 200
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.llm_config is not None
    assert saved.llm_config.reasoning_effort == "xhigh"

    rejected = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"llm_config": {"reasoning_effort": "ultra"}},
    )
    assert rejected.status_code == 422

    # Legacy garbage persisted on disk must not break the config load: it is
    # coerced to None (inherit) by the before-mode validator.
    config_path = tmp_path / "thread_configs" / f"{thread_id}.json"
    assert config_path.exists(), "expected a persisted thread config file"
    raw = config_path.read_text(encoding="utf-8")
    config_path.write_text(raw.replace('"xhigh"', '"turbo"'), encoding="utf-8")

    reloaded = agent.thread_config_manager.get_config(thread_id)
    assert reloaded is not None
    assert reloaded.llm_config is not None
    assert reloaded.llm_config.reasoning_effort is None


def test_thread_config_rejects_invalid_core_and_duplicate_callable_names(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    agent.accounts_repo.claim_thread("existing", "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="existing", callable=True, callable_name="Helper")
    )
    agent.accounts_repo.claim_thread("target", "owner")

    missing_name = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable": True},
    )
    invalid_name = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable_name": "bad name"},
    )
    core_name = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable_name": SEED_TOOLS[0].name},
    )
    duplicate = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable_name": "Helper"},
    )

    assert missing_name.status_code == 400
    assert "callable_name is required" in missing_name.json()["detail"]
    assert invalid_name.status_code == 400
    assert "Invalid callable name" in invalid_name.json()["detail"]
    assert core_name.status_code == 400
    assert "conflicts with a core tool name" in core_name.json()["detail"]
    assert duplicate.status_code == 409
    assert "already used by thread existing" in duplicate.json()["detail"]


def test_thread_config_delete_removes_callable_config_and_syncs_tools(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "callable-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            callable=True,
            callable_name="Helper",
            llm_config=ThreadLLMConfig(provider="openai"),
        )
    )

    response = client.delete(
        f"/threads/{thread_id}/config",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "thread_id": thread_id}
    assert agent.thread_config_manager.get_config(thread_id) is None
    assert agent.invalidated == [thread_id]
    assert agent.synced_tools == 1


def test_unconfigured_thread_config_matches_saved_config_shape(
    tmp_path: Path,
    api_client_builder,
):
    """An unconfigured thread must return the same payload shape as a saved one.

    The default payload was a hand-maintained dict that had drifted from the
    ThreadConfig model, dropping temporary_tools/enabled_skills/disabled_skills/
    inject_profile_in_prompt; a client read undefined for those on a fresh
    thread and a real value after the first save. The default is now derived
    from the model so the two branches can never diverge.
    """
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)

    fresh_id = "fresh-thread"
    agent.accounts_repo.claim_thread(fresh_id, "owner")

    default = client.get(f"/threads/{fresh_id}/config", headers=headers)
    assert default.status_code == 200
    default_body = default.json()

    # The four fields the old hand-built dict dropped are present with their
    # model defaults.
    assert default_body["temporary_tools"] == {}
    assert default_body["enabled_skills"] == []
    assert default_body["disabled_skills"] == []
    assert default_body["inject_profile_in_prompt"] is False

    # A fresh thread is uncustomized, and no config is persisted yet so the
    # timestamps stay null (the "no saved config" signal).
    assert default_body["has_customizations"] is False
    assert default_body["created_at"] is None
    assert default_body["updated_at"] is None

    # Saving a config and reading it back must yield the identical key set.
    saved_id = "saved-thread"
    agent.accounts_repo.claim_thread(saved_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=saved_id, instructions="hi")
    )
    saved = client.get(f"/threads/{saved_id}/config", headers=headers)
    assert saved.status_code == 200
    assert set(default_body) == set(saved.json())


def test_thread_config_image_window_size_round_trip(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "img-window-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    # Default (unset) is None and inherits the model max.
    default = client.get(f"/threads/{thread_id}/config", headers=headers)
    assert default.status_code == 200
    assert default.json()["image_window_size"] is None

    set_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"image_window_size": 5},
    )
    assert set_resp.status_code == 200
    body = set_resp.json()
    assert body["image_window_size"] == 5
    assert body["has_customizations"] is True
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.image_window_size == 5

    clear_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"clear_image_window_size": True},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["image_window_size"] is None
    cleared = agent.thread_config_manager.get_config(thread_id)
    assert cleared is not None
    assert cleared.image_window_size is None


def test_thread_config_sequential_tool_execution_round_trip(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "seq-tools-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    # Default (unset) is None -> inherits the global setting.
    default = client.get(f"/threads/{thread_id}/config", headers=headers)
    assert default.status_code == 200
    assert default.json()["sequential_tool_execution"] is None

    # Tri-state: explicit True (force sequential) and explicit False (force
    # concurrent) are both distinct overrides from "inherit".
    on_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"sequential_tool_execution": True},
    )
    assert on_resp.status_code == 200
    assert on_resp.json()["sequential_tool_execution"] is True
    assert on_resp.json()["has_customizations"] is True
    assert agent.thread_config_manager.get_config(thread_id).sequential_tool_execution is True

    off_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"sequential_tool_execution": False},
    )
    assert off_resp.status_code == 200
    assert off_resp.json()["sequential_tool_execution"] is False
    assert agent.thread_config_manager.get_config(thread_id).sequential_tool_execution is False

    clear_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"clear_sequential_tool_execution": True},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["sequential_tool_execution"] is None
    assert agent.thread_config_manager.get_config(thread_id).sequential_tool_execution is None


def test_thread_config_claude_code_overrides_round_trip(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "cc-override-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    # Default (unset) is None for both fields.
    default = client.get(f"/threads/{thread_id}/config", headers=headers)
    assert default.status_code == 200
    assert default.json()["claude_code_model"] is None
    assert default.json()["claude_code_mode"] is None

    set_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"claude_code_model": "claude-opus-4-8", "claude_code_mode": "plan"},
    )
    assert set_resp.status_code == 200
    body = set_resp.json()
    assert body["claude_code_model"] == "claude-opus-4-8"
    assert body["claude_code_mode"] == "plan"
    assert body["has_customizations"] is True
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.claude_code_model == "claude-opus-4-8"
    assert saved.claude_code_mode == "plan"

    clear_resp = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"clear_claude_code_model": True, "clear_claude_code_mode": True},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["claude_code_model"] is None
    assert clear_resp.json()["claude_code_mode"] is None
    cleared = agent.thread_config_manager.get_config(thread_id)
    assert cleared is not None
    assert cleared.claude_code_model is None
    assert cleared.claude_code_mode is None


def test_thread_config_claude_code_mode_rejects_invalid(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "cc-mode-invalid-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    rejected = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"claude_code_mode": "explode-everything"},
    )
    assert rejected.status_code == 422
