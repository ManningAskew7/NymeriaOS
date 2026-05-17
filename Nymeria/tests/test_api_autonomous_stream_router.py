from __future__ import annotations

import asyncio
import json
from pathlib import Path
from queue import Queue

import pytest
from fastapi import HTTPException

from nymeria.api.routers.autonomous_stream import (
    _event_to_sse_payload,
    _generate_autonomous_sse_events,
    _resolve_stream_auth,
)
from nymeria.core.accounts import AccountsRepo
from nymeria.core.event_bus import AutonomousEvent, EventBus


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def test_autonomous_stream_auth_uses_account_user_over_query_user(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    token = _create_user(agent, "alice")

    user_id, firehose = _resolve_stream_auth(
        get_agent_fn=lambda: agent,
        requested_user_id="bob",
        presented_token=token,
        x_nymeria_act_as=None,
    )

    assert user_id == "alice"
    assert firehose is False


def test_autonomous_stream_auth_allows_admin_firehose(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    token = _create_user(agent, "service", role="admin")

    user_id, firehose = _resolve_stream_auth(
        get_agent_fn=lambda: agent,
        requested_user_id="default",
        presented_token=token,
        x_nymeria_act_as="*",
    )

    assert user_id == "*"
    assert firehose is True


def test_autonomous_stream_auth_rejects_non_admin_act_as(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    token = _create_user(agent, "alice")

    with pytest.raises(HTTPException) as exc:
        _resolve_stream_auth(
            get_agent_fn=lambda: agent,
            requested_user_id="alice",
            presented_token=token,
            x_nymeria_act_as="bob",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Act-As requires admin"


def test_autonomous_stream_route_rejects_missing_token(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    client = api_client_builder.client(FakeAgent(tmp_path), settings)

    response = client.get("/autonomous/stream")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API key"


def test_autonomous_event_payload_strips_internal_and_reserved_fields():
    serialized = _event_to_sse_payload(
        AutonomousEvent(
            event_type="thread_updated",
            thread_id="thread-real",
            user_id="alice",
            task_id="task-real",
            data={
                "_origin_client_id": "client-1",
                "type": "wrong",
                "thread_id": "wrong-thread",
                "task_id": "wrong-task",
                "timestamp": "wrong-time",
                "title": "Updated",
            },
        )
    )

    body = json.loads(serialized)

    assert body["type"] == "thread_updated"
    assert body["thread_id"] == "thread-real"
    assert body["task_id"] == "task-real"
    assert body["title"] == "Updated"
    assert "_origin_client_id" not in body
    assert body["timestamp"] != "wrong-time"


async def _next_sse_data(
    *,
    queue: Queue,
    event_bus: EventBus,
    user_id: str,
    firehose: bool = False,
    client_id: str | None = None,
) -> tuple[str, EventBus]:
    subscriber_id = "test-subscriber"
    event_bus.subscribe(subscriber_id)
    generator = _generate_autonomous_sse_events(
        request=FakeRequest(),
        event_bus=event_bus,
        queue=queue,
        subscriber_id=subscriber_id,
        user_id=user_id,
        firehose=firehose,
        client_id=client_id,
    )

    try:
        return await generator.__anext__(), event_bus
    finally:
        await generator.aclose()


def test_autonomous_sse_filters_by_user_and_unsubscribes_on_close():
    queue: Queue = Queue()
    event_bus = EventBus()
    queue.put_nowait(
        AutonomousEvent(
            event_type="response",
            thread_id="thread-bob",
            user_id="bob",
            data={"content": "hidden"},
        )
    )
    queue.put_nowait(
        AutonomousEvent(
            event_type="response",
            thread_id="thread-alice",
            user_id="alice",
            data={"content": "visible"},
        )
    )

    frame, bus = asyncio.run(
        _next_sse_data(queue=queue, event_bus=event_bus, user_id="alice")
    )

    assert '"content": "visible"' in frame
    assert "hidden" not in frame
    assert bus.get_subscriber_count() == 0


def test_autonomous_sse_default_user_is_not_implicit_firehose():
    queue: Queue = Queue()
    event_bus = EventBus()
    queue.put_nowait(
        AutonomousEvent(
            event_type="response",
            thread_id="thread-bob",
            user_id="bob",
            data={"content": "hidden"},
        )
    )
    queue.put_nowait(
        AutonomousEvent(
            event_type="response",
            thread_id="thread-default",
            user_id="default",
            data={"content": "visible"},
        )
    )

    frame, bus = asyncio.run(
        _next_sse_data(queue=queue, event_bus=event_bus, user_id="default")
    )

    assert '"content": "visible"' in frame
    assert "hidden" not in frame
    assert bus.get_subscriber_count() == 0


def test_autonomous_sse_firehose_yields_other_user_events():
    queue: Queue = Queue()
    event_bus = EventBus()
    queue.put_nowait(
        AutonomousEvent(
            event_type="response",
            thread_id="thread-bob",
            user_id="bob",
            data={"content": "from bob"},
        )
    )

    frame, bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=event_bus,
            user_id="*",
            firehose=True,
        )
    )

    assert '"content": "from bob"' in frame
    assert bus.get_subscriber_count() == 0


def test_autonomous_sse_filters_origin_client_events():
    queue: Queue = Queue()
    event_bus = EventBus()
    queue.put_nowait(
        AutonomousEvent(
            event_type="thread_updated",
            thread_id="thread-1",
            user_id="alice",
            data={"title": "self echo", "_origin_client_id": "client-a"},
        )
    )
    queue.put_nowait(
        AutonomousEvent(
            event_type="thread_updated",
            thread_id="thread-1",
            user_id="alice",
            data={"title": "other client", "_origin_client_id": "client-b"},
        )
    )

    frame, bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=event_bus,
            user_id="alice",
            client_id="client-a",
        )
    )

    assert "other client" in frame
    assert "self echo" not in frame
    assert "_origin_client_id" not in frame
    assert bus.get_subscriber_count() == 0
