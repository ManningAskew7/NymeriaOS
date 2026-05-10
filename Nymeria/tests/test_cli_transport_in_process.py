from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nymeria.triggers.cli.events import DoneEvent, ResponseEvent
from nymeria.triggers.cli.transport.in_process import InProcessAgentClient


@dataclass(slots=True)
class FakeSettings:
    database_backend: str = "memory"
    db_path: Path | None = None
    postgres_uri: str = ""


@dataclass(slots=True)
class FakeThreadMetadata:
    thread_id: str
    title: str = "New Chat"
    pinned: bool = False
    platform: str = "desktop"
    platform_meta: dict[str, str] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    title_source: str = "default"

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FakeMetadataStore:
    user_id: str
    threads: dict[str, FakeThreadMetadata] = field(default_factory=dict)


class FakeThreadMetadataManager:
    def __init__(self) -> None:
        self.stores: dict[str, FakeMetadataStore] = {}
        self.deleted: list[tuple[str, str]] = []

    def get_store(self, user_id: str = "default") -> FakeMetadataStore:
        return self.stores.setdefault(user_id, FakeMetadataStore(user_id=user_id))

    def upsert_thread(
        self,
        user_id: str,
        thread_id: str,
        **fields: Any,
    ) -> FakeThreadMetadata:
        store = self.get_store(user_id)
        meta = store.threads.get(thread_id)
        if meta is None:
            meta = FakeThreadMetadata(thread_id=thread_id)
            store.threads[thread_id] = meta
        for key, value in fields.items():
            if value is not None:
                setattr(meta, key, value)
        return meta

    def delete_thread(self, user_id: str, thread_id: str) -> bool:
        self.deleted.append((user_id, thread_id))
        return self.get_store(user_id).threads.pop(thread_id, None) is not None


@dataclass(slots=True)
class FakeThreadConfig:
    callable: bool = False
    callable_name: str = ""


class FakeThreadConfigManager:
    def __init__(self) -> None:
        self.configs: dict[str, FakeThreadConfig] = {}
        self.deleted: list[str] = []

    def get_config(self, thread_id: str) -> FakeThreadConfig | None:
        return self.configs.get(thread_id)

    def delete_config(self, thread_id: str) -> None:
        self.deleted.append(thread_id)
        self.configs.pop(thread_id, None)


class FakeAgent:
    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        settings: FakeSettings | None = None,
    ) -> None:
        self.events = events or [
            {"type": "response", "content": "Hello"},
            {"type": "done", "tool_call_count": 0},
        ]
        self.settings = settings or FakeSettings()
        self.thread_metadata_manager = FakeThreadMetadataManager()
        self.thread_config_manager = FakeThreadConfigManager()
        self.astream_calls: list[dict[str, Any]] = []
        self.abort_calls: list[str] = []
        self.history_calls: list[dict[str, Any]] = []
        self.context_calls: list[str] = []
        self.invalidated: list[str] = []
        self.sync_agent_tools_count = 0

    async def astream(self, **kwargs: Any):
        self.astream_calls.append(copy.deepcopy(kwargs))
        for event in self.events:
            yield copy.deepcopy(event)

    def abort_with_cascade(self, thread_id: str) -> None:
        self.abort_calls.append(thread_id)

    def get_conversation_history(
        self,
        thread_id: str,
        *,
        include_internal: bool = False,
        show_autonomous_prompts: bool = False,
        show_prompt_metadata: bool = False,
    ) -> list[dict[str, Any]]:
        self.history_calls.append(
            {
                "thread_id": thread_id,
                "include_internal": include_internal,
                "show_autonomous_prompts": show_autonomous_prompts,
                "show_prompt_metadata": show_prompt_metadata,
            }
        )
        return [{"role": "assistant", "content": "history"}]

    def get_context_stats(self, thread_id: str) -> dict[str, Any]:
        self.context_calls.append(thread_id)
        return {
            "thread_id": thread_id,
            "model": "test-model",
            "total_tokens": 12,
        }

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)

    def sync_agent_tools(self) -> None:
        self.sync_agent_tools_count += 1


def run(coro):
    return asyncio.run(coro)


def test_stream_chat_normalizes_local_astream_events() -> None:
    agent = FakeAgent(
        events=[
            {"type": "response", "content": "Hello"},
            {"type": "done", "tool_call_count": 0},
        ]
    )
    client = InProcessAgentClient(agent)

    async def collect():
        return [
            event
            async for event in client.stream_chat(
                "hello",
                "thread-a",
                "alice",
                attachments=[{"file_name": "note.txt"}],
                force_unsupported_attachments=True,
                is_self_invoke=True,
                trigger_override="manual-test",
            )
        ]

    events = run(collect())

    assert events == [
        ResponseEvent(thread_id="thread-a", content="Hello"),
        DoneEvent(thread_id="thread-a", tool_call_count=0),
    ]
    assert agent.astream_calls == [
        {
            "message": "hello",
            "thread_id": "thread-a",
            "user_id": "alice",
            "attachments": [{"file_name": "note.txt"}],
            "force_unsupported_attachments": True,
            "_is_self_invoke": True,
            "_trigger_override": "manual-test",
        }
    ]


def test_stop_is_idempotent_until_next_stream() -> None:
    agent = FakeAgent()
    client = InProcessAgentClient(agent)

    async def stop_sequence():
        first = await client.stop("thread-a", user_id="alice")
        duplicate = await client.stop("thread-a", user_id="alice")
        streamed = [event async for event in client.stream_chat("next", "thread-a")]
        after_new_stream = await client.stop("thread-a", user_id="alice")
        return first, duplicate, streamed, after_new_stream

    first, duplicate, streamed, after_new_stream = run(stop_sequence())

    assert first["status"] == "stopping"
    assert duplicate["status"] == "already_stopping"
    assert after_new_stream["status"] == "stopping"
    assert streamed[-1] == DoneEvent(thread_id="thread-a", tool_call_count=0)
    assert agent.abort_calls == ["thread-a", "thread-a"]


def test_history_and_context_stats_use_local_agent_methods() -> None:
    agent = FakeAgent()
    client = InProcessAgentClient(agent)

    async def query():
        history = await client.get_history(
            "thread-a",
            user_id="alice",
            include_internal=True,
            show_prompt_metadata=True,
        )
        stats = await client.get_context_stats("thread-a", user_id="alice")
        return history, stats

    history, stats = run(query())

    assert history == {
        "thread_id": "thread-a",
        "messages": [{"role": "assistant", "content": "history"}],
    }
    assert agent.history_calls == [
        {
            "thread_id": "thread-a",
            "include_internal": True,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": True,
        }
    ]
    assert stats["thread_id"] == "thread-a"
    assert agent.context_calls == ["thread-a"]


def test_list_threads_merges_checkpoints_and_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        conn.executemany(
            "INSERT INTO checkpoints (thread_id) VALUES (?)",
            [("checkpoint-only",), ("meta-thread",)],
        )
        conn.commit()

    agent = FakeAgent(
        settings=FakeSettings(database_backend="sqlite", db_path=db_path)
    )
    store = agent.thread_metadata_manager.get_store("alice")
    store.threads["meta-thread"] = FakeThreadMetadata(
        thread_id="meta-thread",
        title="Metadata thread",
        title_source="user",
    )
    store.threads["callable-thread"] = FakeThreadMetadata(
        thread_id="callable-thread",
        title="Old title",
    )
    agent.thread_config_manager.configs["callable-thread"] = FakeThreadConfig(
        callable=True,
        callable_name="Helper",
    )
    client = InProcessAgentClient(agent)

    threads = run(client.list_threads("alice"))
    by_id = {thread["thread_id"]: thread for thread in threads}

    assert set(by_id) == {"checkpoint-only", "meta-thread", "callable-thread"}
    assert by_id["checkpoint-only"]["title"] == "New Chat"
    assert by_id["meta-thread"]["title"] == "Metadata thread"
    assert by_id["callable-thread"]["title"] == "Helper"
    assert by_id["callable-thread"]["callable"] is True


def test_thread_metadata_operations_stay_behind_client_boundary() -> None:
    agent = FakeAgent()
    agent.thread_config_manager.configs["thread-a"] = FakeThreadConfig(
        callable=True,
        callable_name="OldHelper",
    )
    client = InProcessAgentClient(agent)

    async def mutate():
        created = await client.create_thread(
            "alice",
            thread_id="thread-a",
            title="Draft",
        )
        updated = await client.update_thread_metadata(
            "thread-a",
            "alice",
            title="Renamed",
            pinned=True,
        )
        deleted = await client.delete_thread("thread-a", "alice")
        return created, updated, deleted

    created, updated, deleted = run(mutate())

    assert created["title"] == "OldHelper"
    assert created["title_source"] == "callable"
    assert updated["title"] == "OldHelper"
    assert updated["pinned"] is True
    assert deleted["metadata_deleted"] is True
    assert deleted["config_deleted"] is True
    assert deleted["callable_deleted"] is True
    assert deleted["checkpoints"] == {"checkpoint_rows_remaining": 0}
    assert agent.thread_metadata_manager.deleted == [("alice", "thread-a")]
    assert agent.thread_config_manager.deleted == ["thread-a"]
    assert agent.invalidated == ["thread-a"]
    assert agent.sync_agent_tools_count == 1
