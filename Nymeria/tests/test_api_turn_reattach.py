"""GET /threads/{id}/turn/stream re-attach route (re-attachable turns, slice 2).

Covers replay of a finished turn (byte-identical payloads after the
``turn_attach`` preamble), the live replay-then-tail handoff, the
turn_not_found / turn_replay_gap contracts, thread-access enforcement, and
the ``turn`` block on GET /threads/{id}/status.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.turn_stream_buffer import (
    STATE_DONE,
    TurnReplayGapError,
    get_turn_stream_registry,
    reset_turn_stream_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_turn_stream_registry()
    yield
    reset_turn_stream_registry()


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

    def sync_agent_tools(self):
        pass

    def invalidate_thread_config_cache(self, thread_id: str):
        pass


def _client(tmp_path: Path, api_client_builder) -> tuple[Any, FakeAgent]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    return client, agent


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def _seed_finished_turn(
    thread_id: str,
    user_id: str = "owner",
    user_message_id: str | None = None,
):
    buffer = get_turn_stream_registry().begin_turn(
        thread_id, user_id, user_message_id=user_message_id
    )
    buffer.append({"type": "turn_started", "turn_id": buffer.turn_id})
    buffer.append({"type": "response", "content": "Hello "})
    buffer.append({"type": "response", "content": "world"})
    buffer.append({"type": "done", "thread_id": thread_id})
    buffer.finish(STATE_DONE)
    return buffer


def _sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_reattach_replays_finished_turn_byte_identical(
    tmp_path: Path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")
    buffer = _seed_finished_turn("t1")

    with client.stream(
        "GET",
        "/threads/t1/turn/stream",
        headers=api_client_builder.auth(token),
    ) as response:
        assert response.status_code == 200
        body = response.read().decode()

    events = _sse_events(body)
    assert events[0]["type"] == "turn_attach"
    assert events[0]["turn_id"] == buffer.turn_id
    assert events[0]["state"] == "done"
    assert events[0]["last_seq"] == 4
    assert events[0]["truncated"] is False

    replayed_lines = [
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data: ")
    ][1:]
    assert replayed_lines == [payload for _, payload in buffer._entries]


def test_reattach_from_seq_skips_prefix(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")
    _seed_finished_turn("t1")

    with client.stream(
        "GET",
        "/threads/t1/turn/stream?from_seq=3",
        headers=api_client_builder.auth(token),
    ) as response:
        body = response.read().decode()

    types = [e["type"] for e in _sse_events(body)]
    assert types == ["turn_attach", "done"]


def test_reattach_missing_and_mismatched_turn_are_not_found(
    tmp_path: Path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")

    missing = client.get(
        "/threads/t1/turn/stream",
        headers=api_client_builder.auth(token),
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "turn_not_found"

    _seed_finished_turn("t1")
    mismatched = client.get(
        "/threads/t1/turn/stream?turn_id=some-other-turn",
        headers=api_client_builder.auth(token),
    )
    assert mismatched.status_code == 404
    assert mismatched.json()["detail"]["code"] == "turn_not_found"


def test_reattach_replay_gap_is_410(tmp_path: Path, api_client_builder, monkeypatch):
    monkeypatch.setattr(
        "nymeria.core.turn_stream_buffer.MAX_EVENTS_PER_TURN", 2
    )
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")
    _seed_finished_turn("t1")  # 4 events through a 2-event cap: truncated

    response = client.get(
        "/threads/t1/turn/stream",
        headers=api_client_builder.auth(token),
    )
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "turn_replay_gap"


def test_mid_stream_gap_ends_with_turn_replay_gap_frame(
    tmp_path: Path, api_client_builder
):
    """An eviction gap that opens AFTER attach ends the stream honestly.

    The attach-time check can only 410 pre-existing gaps; when the buffer
    overflows past a slow reader mid-stream, the route must emit a
    ``turn_replay_gap`` frame (clients reconcile from history) rather than
    silently skipping the evicted span.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")
    buffer = get_turn_stream_registry().begin_turn("t1", "owner")

    async def _gappy_stream(from_seq: int = 0):
        yield json.dumps({"type": "response", "content": "x", "seq": 1})
        raise TurnReplayGapError("evicted past the reader")

    buffer.stream_payloads = _gappy_stream  # type: ignore[method-assign]

    with client.stream(
        "GET",
        "/threads/t1/turn/stream",
        headers=api_client_builder.auth(token),
    ) as response:
        assert response.status_code == 200
        body = response.read().decode()

    events = _sse_events(body)
    assert [e["type"] for e in events] == [
        "turn_attach",
        "response",
        "turn_replay_gap",
    ]
    assert events[-1]["thread_id"] == "t1"
    assert events[-1]["turn_id"] == buffer.turn_id


def test_attach_and_status_carry_user_message_id(
    tmp_path: Path, api_client_builder
):
    """The turn's initiating-message anchor rides both read surfaces.

    Live-attach viewers use it to trim hydrated history back to the turn
    start before replaying, so it must appear on the ``turn_attach``
    preamble and the status ``turn`` block alike.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")
    _seed_finished_turn("t1", user_message_id="msg-anchor-1")

    with client.stream(
        "GET",
        "/threads/t1/turn/stream",
        headers=api_client_builder.auth(token),
    ) as response:
        body = response.read().decode()
    attach = _sse_events(body)[0]
    assert attach["type"] == "turn_attach"
    assert attach["user_message_id"] == "msg-anchor-1"

    status = client.get(
        "/threads/t1/status",
        headers=api_client_builder.auth(token),
    )
    assert status.json()["turn"]["user_message_id"] == "msg-anchor-1"


def test_reattach_does_not_claim_ownerless_thread(
    tmp_path: Path, api_client_builder
):
    """The attach GET is a read: it must not TOFU-claim an ownerless thread.

    Sibling read GETs (status, history) pass claim=False; before this pin the
    attach route used the claiming default, so merely watching a thread could
    transfer first-touch ownership.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "viewer")
    _seed_finished_turn("t-unowned", user_id="viewer")

    with client.stream(
        "GET",
        "/threads/t-unowned/turn/stream",
        headers=api_client_builder.auth(token),
    ) as response:
        assert response.status_code == 200
        response.read()

    assert agent.accounts_repo.get_thread_owner("t-unowned") is None


def test_reattach_enforces_thread_access(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    _create_user(agent, "owner")
    other_token = _create_user(agent, "other")
    agent.accounts_repo.claim_thread("t1", "owner")
    _seed_finished_turn("t1")

    response = client.get(
        "/threads/t1/turn/stream",
        headers=api_client_builder.auth(other_token),
    )
    assert response.status_code == 404


def test_thread_status_reports_attachable_turn(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("t1", "owner")
    buffer = _seed_finished_turn("t1")

    response = client.get(
        "/threads/t1/status",
        headers=api_client_builder.auth(token),
    )
    assert response.status_code == 200
    assert response.json()["turn"] == {
        "turn_id": buffer.turn_id,
        "state": "done",
        "last_seq": 4,
        "truncated": False,
        "user_message_id": None,
    }


@pytest.mark.asyncio
async def test_reattach_tails_live_turn_until_finish():
    """Replay-then-live handoff at the buffer layer, as the route consumes it.

    Driven through the route's generator contract (stream_payloads) rather
    than a live HTTP socket: append after the reader started, then finish;
    the reader must see the tail and terminate.
    """
    registry = get_turn_stream_registry()
    buffer = registry.begin_turn("t-live", "owner")
    buffer.append({"type": "turn_started", "turn_id": buffer.turn_id})
    buffer.append({"type": "response", "content": "partial"})

    received: list[dict[str, Any]] = []

    async def _reader() -> None:
        async for payload in buffer.stream_payloads(from_seq=0):
            received.append(json.loads(payload))

    reader = asyncio.create_task(_reader())
    await asyncio.sleep(0.05)
    assert [e["type"] for e in received] == ["turn_started", "response"]

    buffer.append({"type": "response", "content": " rest"})
    buffer.append({"type": "done"})
    buffer.finish(STATE_DONE)
    await asyncio.wait_for(reader, timeout=2)
    assert [e["type"] for e in received] == [
        "turn_started",
        "response",
        "response",
        "done",
    ]
