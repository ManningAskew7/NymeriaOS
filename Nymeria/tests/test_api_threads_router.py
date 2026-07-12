from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, ThreadLLMConfig
from nymeria.core.thread_metadata import ThreadMetadataManager
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
        include_hidden_anchors: bool = False,
    ):
        self.history_calls.append(
            {
                "thread_id": thread_id,
                "include_internal": include_internal,
                "show_autonomous_prompts": show_autonomous_prompts,
                "show_prompt_metadata": show_prompt_metadata,
                "include_hidden_anchors": include_hidden_anchors,
            }
        )
        return [{"role": "user", "content": "hello"}]

    def get_context_stats(self, thread_id: str):
        return {"thread_id": thread_id, "tokens": 42}

    def get_raw_checkpoint(self, thread_id: str):
        # Mirrors agent_context.get_raw_checkpoint's shape: messages are dumped
        # verbatim (a standalone ToolMessage is kept, not embedded like /history).
        return {
            "thread_id": thread_id,
            "checkpoint_id": "0003",
            "checkpoint_ns": "",
            "next": [],
            "config": {
                "configurable": {"thread_id": thread_id, "checkpoint_id": "0003"}
            },
            "metadata": {"step": 2},
            "created_at": "2026-01-01T00:00:00+00:00",
            "parent_config": None,
            "message_count": 2,
            "values": {
                "messages": [
                    {
                        "type": "ai",
                        "content": "",
                        "tool_calls": [
                            {"name": "web_search", "args": {"q": "x"}, "id": "call_1"}
                        ],
                    },
                    {"type": "tool", "content": "result", "tool_call_id": "call_1"},
                ]
            },
        }

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
            "include_hidden_anchors": False,
        },
        {
            "thread_id": thread_id,
            "include_internal": True,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
            "include_hidden_anchors": False,
        },
    ]
    assert context.status_code == 200
    assert context.json() == {
        "thread_id": thread_id,
        "tokens": 42,
        "processing": True,
    }


def test_thread_history_show_autonomous_prompts_query_param_overrides_thread_config(
    tmp_path: Path,
    api_client_builder,
):
    """
    The desktop/mobile clients keep a global `show_autonomous_prompts` toggle
    in localStorage. They pass its effective value as a query parameter so
    history filtering honors the global setting without mutating each
    thread's config. When the param is supplied it takes precedence over
    the per-thread flag; when omitted, the per-thread flag still drives
    filtering (regression check for older clients and MCP/CLI callers).
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    thread_id = "thread-history-override"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            show_autonomous_prompts=False,
            show_prompt_metadata=False,
        )
    )

    no_param = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
    )
    param_true = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
        params={"show_autonomous_prompts": "true"},
    )

    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            show_autonomous_prompts=True,
            show_prompt_metadata=False,
        )
    )

    param_false = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
        params={"show_autonomous_prompts": "false"},
    )
    internal_with_param = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
        params={"include_internal": "true", "show_autonomous_prompts": "true"},
    )

    assert no_param.status_code == 200
    assert param_true.status_code == 200
    assert param_false.status_code == 200
    assert internal_with_param.status_code == 200

    assert agent.history_calls == [
        {
            "thread_id": thread_id,
            "include_internal": False,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
            "include_hidden_anchors": False,
        },
        {
            "thread_id": thread_id,
            "include_internal": False,
            "show_autonomous_prompts": True,
            "show_prompt_metadata": False,
            "include_hidden_anchors": False,
        },
        {
            "thread_id": thread_id,
            "include_internal": False,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
            "include_hidden_anchors": False,
        },
        {
            "thread_id": thread_id,
            "include_internal": True,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
            "include_hidden_anchors": False,
        },
    ]


def test_thread_checkpoint_returns_raw_state_for_owner(
    tmp_path: Path,
    api_client_builder,
):
    """The checkpoint endpoint returns the raw, unprojected state so tool calls
    and standalone tool results from prior turns can be verified verbatim (the
    /history endpoint would consolidate turns and embed/drop tool results)."""
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    thread_id = "thread-checkpoint"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.get(
        f"/threads/{thread_id}/checkpoint",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert body["checkpoint_id"] == "0003"
    messages = body["values"]["messages"]
    assert messages[0]["tool_calls"][0]["id"] == "call_1"
    assert messages[1]["tool_call_id"] == "call_1"


def test_thread_checkpoint_enforces_thread_access(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    other_token = _create_user(agent, "other")
    thread_id = "thread-owned-checkpoint"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    owner = client.get(
        f"/threads/{thread_id}/checkpoint",
        headers=api_client_builder.auth(owner_token),
    )
    other = client.get(
        f"/threads/{thread_id}/checkpoint",
        headers=api_client_builder.auth(other_token),
    )

    assert owner.status_code == 200
    assert other.status_code == 404


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
        "turn": None,
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
        "turn": None,
    }
    assert other.status_code == 404


def test_read_only_get_does_not_claim_ownerless_thread(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    """Read-only thread GETs must not TOFU-claim an ownerless thread.

    Opening a new "New Chat" tab fires read-only GETs (skills, callable-tools,
    config, context, ...). If those claim ownership on first touch, an unused
    tab becomes a permanent empty owner row that reappears in the sidebar
    after a sync. Ownership must instead be established by the first write.
    """
    monkeypatch.setattr(api_module, "publish_sync_event", lambda **kwargs: None)
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    thread_id = "fresh-tab"

    # Precondition: nobody owns the freshly generated thread id.
    assert agent.accounts_repo.get_thread_owner(thread_id) is None

    # A read-only GET succeeds but must leave the thread ownerless.
    read = client.get(
        f"/threads/{thread_id}/context",
        headers=api_client_builder.auth(token),
    )
    assert read.status_code == 200
    assert agent.accounts_repo.get_thread_owner(thread_id) is None

    # The first write (rename) claims it on first touch.
    write = client.patch(
        f"/threads/{thread_id}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": "Renamed"},
    )
    assert write.status_code == 200
    assert agent.accounts_repo.get_thread_owner(thread_id) == "owner"


def test_read_only_get_still_hides_thread_owned_by_other_user(
    tmp_path: Path,
    api_client_builder,
):
    """The non-claiming read branch preserves cross-user isolation: a thread
    owned by someone else is still 404, and ownership is never reassigned."""
    client, agent = _client(tmp_path, api_client_builder)
    other_token = _create_user(agent, "intruder")
    _create_user(agent, "owner")
    thread_id = "owned-elsewhere"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    resp = client.get(
        f"/threads/{thread_id}/context",
        headers=api_client_builder.auth(other_token),
    )
    assert resp.status_code == 404
    assert agent.accounts_repo.get_thread_owner(thread_id) == "owner"


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
        "turn": None,
    }


def test_thread_metadata_title_update_on_callable_thread_allows_spaces_and_preserves_callable_name(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    """
    Title is a display string and is independent of callable_name.

    Pre-fix, the metadata handler re-derived callable_name from the new
    title, which (a) rejected any title containing spaces/punctuation with
    HTTP 400, and (b) silently rewrote the LLM tool binding when the caller
    only intended to relabel the thread. The fix decouples the two: titles
    accept any free-form string and callable_name is unchanged. Callers that
    want to rename the binding must call PATCH /threads/{id}/config.
    """
    events: list[dict[str, Any]] = []

    def capture_sync_event(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(api_module, "publish_sync_event", capture_sync_event)
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    target = "callable-target"
    agent.accounts_repo.claim_thread(target, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=target, callable=True, callable_name="Helper")
    )

    free_form_title = client.patch(
        f"/threads/{target}/metadata",
        headers={
            **api_client_builder.auth(token),
            "X-Nymeria-Client-Id": "desktop-1",
        },
        json={"title": "CT-01 child (callable: helper)", "pinned": True},
    )

    assert free_form_title.status_code == 200
    body = free_form_title.json()
    assert body["title"] == "CT-01 child (callable: helper)"
    assert body["title_source"] == "user"
    assert body["pinned"] is True

    saved = agent.thread_config_manager.get_config(target)
    assert saved is not None
    assert saved.callable_name == "Helper", (
        "callable_name must not be re-derived from a title update"
    )
    assert agent.invalidated == [], "title-only update should not invalidate config cache"
    assert agent.synced_tools == 0, "title-only update should not touch the tool registry"
    assert events == [
        {
            "event_type": "thread_updated",
            "thread_id": target,
            "user_id": "owner",
            "data": {
                "title": "CT-01 child (callable: helper)",
                "title_source": "user",
                "pinned": True,
            },
            "origin_client_id": "desktop-1",
        }
    ]


def test_thread_metadata_title_does_not_collide_with_other_callables(
    tmp_path: Path,
    api_client_builder,
):
    """
    Display titles are not unique within a user. Two callable threads can both
    be titled 'Helper Workshop' even if one is bound as callable_name 'helper'
    and the other as 'workshop' — uniqueness only matters for callable_name,
    which is checked at PATCH /threads/{id}/config.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    target = "callable-target"
    other = "callable-other"
    agent.accounts_repo.claim_thread(target, "owner")
    agent.accounts_repo.claim_thread(other, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=target, callable=True, callable_name="helper")
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=other, callable=True, callable_name="workshop")
    )

    response = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": "workshop"},
    )

    # Title 'workshop' would collide with the other thread's callable_name
    # under the pre-fix behaviour (HTTP 409). Post-fix, titles are free-form.
    assert response.status_code == 200
    assert response.json()["title"] == "workshop"
    # The callable binding is unchanged.
    saved = agent.thread_config_manager.get_config(target)
    assert saved is not None
    assert saved.callable_name == "helper"


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


def test_thread_claim_can_seed_cli_metadata(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")

    response = client.post(
        "/threads/cli-thread/claim",
        headers=api_client_builder.auth(token),
        json={"title": "CLI Draft", "platform": "cli"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == "cli-thread"
    assert body["owner"] == "owner"
    assert body["title"] == "CLI Draft"
    assert body["title_source"] == "user"
    assert body["platform"] == "cli"

    listed = client.get("/threads", headers=api_client_builder.auth(token))
    assert listed.status_code == 200
    [thread] = listed.json()["threads"]
    assert thread["thread_id"] == "cli-thread"
    assert thread["platform"] == "cli"
