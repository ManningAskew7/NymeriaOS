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
    callable_team_id: str = ""
    callable_team_name: str = ""


@dataclass(slots=True)
class FakeTodoStatus:
    value: str


@dataclass(slots=True)
class FakeTodo:
    id: str
    task: str
    status: Any
    thread_id: str | None = None
    created_at: str = "2026-05-10T12:00:00Z"

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "status": getattr(self.status, "value", self.status),
            "thread_id": self.thread_id,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class FakeTodoList:
    items: list[FakeTodo] = field(default_factory=list)

    def get_active_todos(self) -> list[FakeTodo]:
        return [
            item
            for item in self.items
            if getattr(item.status, "value", item.status) != "done"
        ]


class FakeTodoManager:
    def __init__(self) -> None:
        self.lists: dict[str, FakeTodoList] = {}

    def get_todos(self, user_id: str = "default") -> FakeTodoList:
        return self.lists.setdefault(user_id, FakeTodoList())


@dataclass(slots=True)
class FakeTrigger:
    id: str
    name: str
    enabled: bool = True
    thread_id: str = ""

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return asdict(self)


class FakeTriggerManager:
    def __init__(self) -> None:
        self.triggers: dict[str, list[FakeTrigger]] = {}

    def get_triggers(self, user_id: str = "default") -> list[FakeTrigger]:
        return self.triggers.setdefault(user_id, [])


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
        self.todo_manager = FakeTodoManager()
        self.trigger_manager = FakeTriggerManager()
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


def test_local_transport_lists_teams_todos_and_triggers() -> None:
    agent = FakeAgent()
    store = agent.thread_metadata_manager.get_store("alice")
    store.threads["team-thread"] = FakeThreadMetadata(thread_id="team-thread")
    store.threads["plain-thread"] = FakeThreadMetadata(thread_id="plain-thread")
    agent.thread_config_manager.configs["team-thread"] = FakeThreadConfig(
        callable=True,
        callable_name="Helper",
        callable_team_id="ops",
        callable_team_name="Ops",
    )
    agent.todo_manager.get_todos("alice").items.extend(
        [
            FakeTodo("todo-1", "Active", "pending", "team-thread"),
            FakeTodo("todo-2", "Done", "done", "team-thread"),
            FakeTodo("todo-3", "Other", "pending", "plain-thread"),
        ]
    )
    agent.trigger_manager.get_triggers("alice").extend(
        [
            FakeTrigger("trigger-1", "Enabled", True, "team-thread"),
            FakeTrigger("trigger-2", "Disabled", False, "team-thread"),
            FakeTrigger("trigger-3", "Other", True, "plain-thread"),
        ]
    )
    client = InProcessAgentClient(agent)

    async def query():
        teams = await client.list_thread_teams("alice")
        todos = await client.list_todos("alice", thread_id="team-thread")
        triggers = await client.list_triggers(
            "alice",
            enabled_only=True,
            thread_id="team-thread",
        )
        return teams, todos, triggers

    teams, todos, triggers = run(query())

    assert teams == [{"id": "ops", "name": "Ops", "thread_ids": ["team-thread"]}]
    assert [todo["id"] for todo in todos] == ["todo-1"]
    assert [trigger["id"] for trigger in triggers] == ["trigger-1"]


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
    assert created["platform"] == "cli"
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


def test_stream_autonomous_subscribes_to_event_bus_and_filters_user() -> None:
    from collections.abc import AsyncGenerator
    from typing import cast

    import nymeria.core.event_bus as event_bus_module
    from nymeria.core.event_bus import AutonomousEvent, EventBus

    agent = FakeAgent()
    client = InProcessAgentClient(agent, default_user_id="alice")
    bus = EventBus()
    previous_bus = event_bus_module._event_bus
    event_bus_module.set_event_bus(bus)

    async def scenario():
        # The method is typed AsyncIterator; the concrete object is an async
        # generator, so cast to reach aclose() for the unsubscribe-on-close check.
        gen = cast(
            "AsyncGenerator[Any, None]",
            client.stream_autonomous("alice"),
        )
        first = asyncio.ensure_future(gen.__anext__())
        # Let the worker thread subscribe and block on the bus queue before
        # any event is published (a pre-subscription publish would be dropped).
        await asyncio.sleep(0.1)
        assert bus.get_subscriber_count() == 1
        # A mismatched user is filtered out; only the matching user is
        # delivered, so the first yielded event must be alice's task_completed.
        bus.publish(
            AutonomousEvent(event_type="task_started", thread_id="other", user_id="bob")
        )
        bus.publish(
            AutonomousEvent(
                event_type="task_completed",
                thread_id="t-1",
                user_id="alice",
                task_id="task-9",
            )
        )
        event = await asyncio.wait_for(first, timeout=2.0)
        await gen.aclose()
        return event, bus.get_subscriber_count()

    try:
        event, subscribers_after_close = run(scenario())
    finally:
        event_bus_module._event_bus = previous_bus

    assert event.type == "task_completed"
    assert event.thread_id == "t-1"
    # The generator must unsubscribe on close so reconnects do not leak queues.
    assert subscribers_after_close == 0


def test_supports_autonomous_stream_uses_capability_flag() -> None:
    from nymeria.triggers.cli.autonomous import supports_autonomous_stream
    from nymeria.triggers.cli.transport.api import APIAgentClient
    from nymeria.triggers.cli.transport.disconnected import DisconnectedAgentClient

    in_process = InProcessAgentClient(FakeAgent(), default_user_id="alice")
    assert supports_autonomous_stream(in_process) is True

    # __new__ skips the NymeriaAPIClient dependency; only the class-level
    # capability flag and bound stream_autonomous method matter for gating.
    api_client = APIAgentClient.__new__(APIAgentClient)
    assert supports_autonomous_stream(api_client) is True

    assert supports_autonomous_stream(DisconnectedAgentClient()) is False

    class NoStreamClient:
        connection_label = "api http://localhost:8000"

    assert supports_autonomous_stream(NoStreamClient()) is False


def test_autonomous_event_to_payload_matches_router_serialization() -> None:
    import json

    from nymeria.api.routers.autonomous_stream import _event_to_sse_payload
    from nymeria.core.event_bus import AutonomousEvent, autonomous_event_to_payload

    event = AutonomousEvent(
        event_type="task_completed",
        thread_id="t-1",
        user_id="alice",
        task_id="task-9",
        data={
            "content": "done",
            "summary": {"ok": True},
            "_origin_client_id": "client-x",  # stripped: underscore-prefixed
            "type": "ignored",  # stripped: reserved top-level key
            "thread_id": "ignored",  # stripped: reserved top-level key
            "task_id": "ignored",  # stripped: reserved top-level key
            "timestamp": "ignored",  # stripped: reserved top-level key
        },
    )

    payload = autonomous_event_to_payload(event)
    assert payload == {
        "type": "task_completed",
        "thread_id": "t-1",
        "task_id": "task-9",
        "timestamp": event.timestamp.isoformat(),
        "content": "done",
        "summary": {"ok": True},
    }
    # The API SSE router must serialize exactly this dict, so the in-process
    # transport and the SSE wire never drift.
    assert _event_to_sse_payload(event) == json.dumps(payload)
