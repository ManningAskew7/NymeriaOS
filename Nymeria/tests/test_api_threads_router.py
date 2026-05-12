from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, ThreadLLMConfig
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import ALL_TOOLS
from nymeria.triggers import api as api_module


class FakeThreadLocks:
    def __init__(self):
        self.lock_info = None

    def get_lock_info(self, thread_id: str):
        return self.lock_info


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self._thread_locks = FakeThreadLocks()
        self.history_calls: list[dict[str, Any]] = []
        self.invalidated: list[str] = []
        self.synced_tools = 0

    def get_conversation_history(
        self,
        thread_id: str,
        *,
        include_internal: bool,
        show_autonomous_prompts: bool,
        show_prompt_metadata: bool,
    ):
        self.history_calls.append(
            {
                "thread_id": thread_id,
                "include_internal": include_internal,
                "show_autonomous_prompts": show_autonomous_prompts,
                "show_prompt_metadata": show_prompt_metadata,
            }
        )
        return [{"role": "user", "content": "hello"}]

    def get_context_stats(self, thread_id: str):
        return {"thread_id": thread_id, "tokens": 42}

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    agent.synced_tools = 0
    return client, agent


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def test_thread_history_visibility_flags_and_context_processing_state(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    thread_id = "thread-history"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            show_autonomous_prompts=True,
            show_prompt_metadata=True,
        )
    )
    agent._thread_locks.lock_info = {"owner": "test"}

    history = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
    )
    internal_history = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
        params={"include_internal": "true"},
    )
    context = client.get(
        f"/threads/{thread_id}/context",
        headers=api_client_builder.auth(token),
    )

    assert history.status_code == 200
    assert history.json() == {
        "thread_id": thread_id,
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert internal_history.status_code == 200
    assert agent.history_calls == [
        {
            "thread_id": thread_id,
            "include_internal": False,
            "show_autonomous_prompts": True,
            "show_prompt_metadata": True,
        },
        {
            "thread_id": thread_id,
            "include_internal": True,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
        },
    ]
    assert context.status_code == 200
    assert context.json() == {
        "thread_id": thread_id,
        "tokens": 42,
        "processing": True,
    }


def test_thread_status_returns_revision_and_processing_state(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    settings = api_client_builder.settings(tmp_path)
    token = _create_user(agent, "owner")
    thread_id = "thread-status"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent._thread_locks.lock_info = {"owner": "test"}

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
            "checkpoint BLOB, metadata BLOB)"
        )
        conn.executemany(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (thread_id, "", "0001", b"old checkpoint", b"{}"),
                (thread_id, "", "0003", b"new checkpoint", b"{}"),
                (thread_id, "nested", "9999", b"nested checkpoint", b"{}"),
                ("other-thread", "", "9999", b"other checkpoint", b"{}"),
            ],
        )
        conn.commit()

    response = client.get(
        f"/threads/{thread_id}/status",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": thread_id,
        "revision": "0003",
        "processing": True,
    }


def test_thread_status_enforces_thread_access(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    other_token = _create_user(agent, "other")
    thread_id = "thread-owned"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    owner = client.get(
        f"/threads/{thread_id}/status",
        headers=api_client_builder.auth(owner_token),
    )
    other = client.get(
        f"/threads/{thread_id}/status",
        headers=api_client_builder.auth(other_token),
    )

    assert owner.status_code == 200
    assert owner.json() == {
        "thread_id": thread_id,
        "revision": None,
        "processing": False,
    }
    assert other.status_code == 404


def test_thread_status_falls_back_to_graph_state_for_memory_backend(
    tmp_path: Path,
    api_client_builder,
):
    class FakeState:
        config = {"configurable": {"checkpoint_id": "memory-revision"}}

    class FakeGraph:
        def get_state(self, config):
            assert config == {"configurable": {"thread_id": "thread-memory"}}
            return FakeState()

    settings = api_client_builder.settings(tmp_path, database_backend="memory")
    agent = FakeAgent(tmp_path)
    agent._default_graph = FakeGraph()
    client = api_client_builder.client(agent, settings)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("thread-memory", "owner")

    response = client.get(
        "/threads/thread-memory/status",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": "thread-memory",
        "revision": "memory-revision",
        "processing": False,
    }


def test_thread_metadata_rename_callable_validates_conflicts_and_publishes_event(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    events: list[dict[str, Any]] = []

    def capture_sync_event(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(api_module, "publish_sync_event", capture_sync_event)
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    target = "callable-target"
    existing = "callable-existing"
    agent.accounts_repo.claim_thread(target, "owner")
    agent.accounts_repo.claim_thread(existing, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=target, callable=True, callable_name="Helper")
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=existing, callable=True, callable_name="ExistingHelper")
    )

    invalid = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": "bad name"},
    )
    duplicate = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": "ExistingHelper"},
    )
    core_conflict = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": ALL_TOOLS[0].name},
    )
    renamed = client.patch(
        f"/threads/{target}/metadata",
        headers={
            **api_client_builder.auth(token),
            "X-Nymeria-Client-Id": "desktop-1",
        },
        json={"title": "RenamedHelper", "pinned": True},
    )

    assert invalid.status_code == 400
    assert "Invalid callable name" in invalid.json()["detail"]
    assert duplicate.status_code == 409
    assert "already used by thread callable-existing" in duplicate.json()["detail"]
    assert core_conflict.status_code == 400
    assert "conflicts with a core tool name" in core_conflict.json()["detail"]
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["title"] == "RenamedHelper"
    assert body["title_source"] == "callable"
    assert body["pinned"] is True

    saved = agent.thread_config_manager.get_config(target)
    assert saved is not None
    assert saved.callable_name == "RenamedHelper"
    assert agent.invalidated == [target]
    assert agent.synced_tools == 1
    assert events == [
        {
            "event_type": "thread_updated",
            "thread_id": target,
            "user_id": "owner",
            "data": {
                "title": "RenamedHelper",
                "title_source": "callable",
                "pinned": True,
            },
            "origin_client_id": "desktop-1",
        }
    ]


def test_thread_branch_clones_checkpoints_config_and_metadata(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    events: list[dict[str, Any]] = []

    def capture_sync_event(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(api_module, "publish_sync_event", capture_sync_event)
    client, agent = _client(tmp_path, api_client_builder)
    settings = api_client_builder.settings(tmp_path)
    token = _create_user(agent, "owner")
    source = "source-thread"
    agent.accounts_repo.claim_thread(source, "owner")
    agent.thread_metadata_manager.upsert_thread(
        "owner",
        source,
        title="Source Thread",
        title_source="user",
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=source,
            instructions="Keep answers short.",
            enabled_tools=["web_search"],
            disabled_tools=["bash_execute"],
            llm_config=ThreadLLMConfig(model="claude-test"),
        )
    )

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
            "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, "
            "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
        )
        conn.execute(
            "CREATE TABLE writes ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
            "task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB)"
        )
        conn.executemany(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (source, "", "0001", None, "json", b"old", b"{}"),
                (source, "", "0002", "0001", "json", b"new", b"{}"),
                ("other-thread", "", "9999", None, "json", b"other", b"{}"),
            ],
        )
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, "", "0002", "task", 0, "messages", "json", b"write"),
        )
        conn.commit()

    response = client.post(
        f"/threads/{source}/branch",
        headers={
            **api_client_builder.auth(token),
            "X-Nymeria-Client-Id": "cli-1",
        },
        json={"title": "Experimental Path"},
    )

    assert response.status_code == 200
    body = response.json()
    branch_id = body["thread_id"]
    assert branch_id.startswith("branch-")
    assert body["source_thread_id"] == source
    assert body["title"] == "Experimental Path"
    assert body["config_cloned"] is True
    assert body["checkpoints"]["checkpoints_copied"] == 2
    assert body["checkpoints"]["writes_copied"] == 1
    assert agent.accounts_repo.get_thread_owner(branch_id) == "owner"

    cloned_config = agent.thread_config_manager.get_config(branch_id)
    assert cloned_config is not None
    assert cloned_config.thread_id == branch_id
    assert cloned_config.instructions == "Keep answers short."
    assert cloned_config.enabled_tools == ["web_search"]
    assert cloned_config.disabled_tools == ["bash_execute"]
    assert cloned_config.llm_config is not None
    assert cloned_config.llm_config.model == "claude-test"

    with sqlite3.connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT checkpoint_id, parent_checkpoint_id, checkpoint "
            "FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id",
            (branch_id,),
        ).fetchall()
        write_rows = conn.execute(
            "SELECT checkpoint_id, value FROM writes WHERE thread_id = ?",
            (branch_id,),
        ).fetchall()
    assert rows == [("0001", None, b"old"), ("0002", "0001", b"new")]
    assert write_rows == [("0002", b"write")]
    assert events == [
        {
            "event_type": "thread_created",
            "thread_id": branch_id,
            "user_id": "owner",
            "data": {
                "title": "Experimental Path",
                "title_source": "user",
                "platform": "desktop",
                "source_thread_id": source,
            },
            "origin_client_id": "cli-1",
        }
    ]


def test_thread_claim_rejects_shared_and_hides_other_user_owner(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    _create_user(agent, "other")
    admin_token = _create_user(agent, "admin", role="admin")
    agent.accounts_repo.claim_thread("other-thread", "other")

    fresh = client.post(
        "/threads/fresh-thread/claim",
        headers=api_client_builder.auth(owner_token),
    )
    shared = client.post(
        "/threads/telegram_-123/claim",
        headers=api_client_builder.auth(owner_token),
    )
    hidden = client.post(
        "/threads/other-thread/claim",
        headers=api_client_builder.auth(owner_token),
    )
    admin = client.post(
        "/threads/other-thread/claim",
        headers=api_client_builder.auth(admin_token),
    )

    assert fresh.status_code == 200
    assert fresh.json() == {"thread_id": "fresh-thread", "owner": "owner"}
    assert shared.status_code == 400
    assert shared.json()["detail"] == "Shared-channel threads cannot be claimed"
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "Not found"
    assert admin.status_code == 200
    assert admin.json() == {"thread_id": "other-thread", "owner": "other"}
