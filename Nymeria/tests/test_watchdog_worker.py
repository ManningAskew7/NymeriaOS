"""Unit tests for WatchdogWorker nudge delivery, retry, and state pruning.

The worker is a thin API client: these tests drive it against a fake
NymeriaAPIClient and pin three behaviors added in the #55 pass:

- External alerts go through client.send_external_notification (the
  POST /notifications/external transport), never in-process dispatch.
- A nudge turn that fails outright (transport error, or an in-band error
  with no response) leaves the TODOs unmarked so the next cycle retries.
- Nudge/timestamp dedupe state is pruned when TODOs disappear, so the
  long-lived process does not leak keys.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx

from nymeria.triggers.watchdog_worker import MAX_NUDGE_FAILURES, WatchdogWorker


def _settings(tmp_path):
    return SimpleNamespace(
        watchdog_interval_minutes=5,
        todo_staleness_minutes=20,
        data_dir=tmp_path,
    )


def _stale_todo(todo_id="t1", thread_id="th1", minutes_old=60) -> Dict[str, Any]:
    updated = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    return {
        "id": todo_id,
        "thread_id": thread_id,
        "status": "pending",
        "task": "do the thing",
        "updated_at": updated.isoformat(),
    }


class FakeClient:
    """Duck-typed NymeriaAPIClient covering what the worker calls."""

    def __init__(
        self,
        *,
        chunks: List[Dict[str, Any]] | None = None,
        todos: List[Dict[str, Any]] | None = None,
        notify_result: List[str] | None = None,
        notify_error: Exception | None = None,
    ):
        self.chunks = (
            chunks
            if chunks is not None
            else [{"type": "response", "content": "ok"}, {"type": "done"}]
        )
        self.todos = todos or []
        self.notify_result = notify_result if notify_result is not None else ["tg-main"]
        self.notify_error = notify_error
        self.notifications: List[Dict[str, Any]] = []
        self.chat_calls: List[Dict[str, Any]] = []

    async def chat_stream(self, **kwargs):
        self.chat_calls.append(kwargs)
        for chunk in self.chunks:
            yield chunk

    async def list_todos(self, user_id: str, **kwargs):
        return self.todos

    async def send_external_notification(
        self, user_id: str, message: str, *, thread_id: str = "",
    ) -> List[str]:
        if self.notify_error is not None:
            raise self.notify_error
        self.notifications.append(
            {"user_id": user_id, "message": message, "thread_id": thread_id}
        )
        return list(self.notify_result)


class TransportFailureClient(FakeClient):
    async def chat_stream(self, **kwargs):
        self.chat_calls.append(kwargs)
        raise httpx.ConnectError("api unreachable")
        yield  # pragma: no cover - makes this an async generator


def _worker(tmp_path, client) -> WatchdogWorker:
    return WatchdogWorker(client, _settings(tmp_path))  # type: ignore[arg-type]


# ── Nudge delivery + notification transport ───────────────────────────────


def test_successful_nudge_marks_and_notifies_via_api(tmp_path):
    client = FakeClient()
    worker = _worker(tmp_path, client)
    todo = _stale_todo()

    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert ("u1", "t1") in worker._nudged
    assert worker._nudges_sent == 1
    # External alert went through the API client (the /notifications/external
    # transport), scoped to the nudged user and thread.
    assert len(client.notifications) == 1
    sent = client.notifications[0]
    assert sent["user_id"] == "u1"
    assert sent["thread_id"] == "th1"
    assert "[Nymeria Watchdog]" in sent["message"]
    assert worker._notifications_sent == 1
    assert worker._notification_failures == 0


def test_notification_failure_is_isolated_and_counted(tmp_path):
    client = FakeClient(notify_error=RuntimeError("boom"))
    worker = _worker(tmp_path, client)
    todo = _stale_todo()

    # Must not raise: the nudge reached the agent even if the alert failed.
    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert ("u1", "t1") in worker._nudged
    assert worker._notification_failures == 1
    assert worker._notifications_sent == 0


# ── Retry semantics ────────────────────────────────────────────────────────


def test_transport_failure_leaves_todos_unmarked_for_retry(tmp_path):
    client = TransportFailureClient()
    worker = _worker(tmp_path, client)
    todo = _stale_todo()

    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert worker._nudged == {}
    assert client.notifications == []
    assert worker._nudges_sent == 0
    # The in-flight guard must release so the next cycle can retry.
    assert worker._in_flight == set()


def test_error_turn_without_response_leaves_todos_unmarked(tmp_path):
    client = FakeClient(
        chunks=[{"type": "error", "content": "llm exploded"}, {"type": "done"}]
    )
    worker = _worker(tmp_path, client)
    todo = _stale_todo()

    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert worker._nudged == {}
    assert client.notifications == []


def test_repeated_failures_hit_the_cap_and_suppress(tmp_path):
    # A deterministically failing thread must not retry (and burn an
    # errored LLM turn) every cycle forever: after MAX_NUDGE_FAILURES
    # consecutive failures the TODO is marked nudged anyway, staying
    # quiet until its updated_at advances.
    client = TransportFailureClient()
    worker = _worker(tmp_path, client)
    todo = _stale_todo()

    for _ in range(MAX_NUDGE_FAILURES - 1):
        asyncio.run(worker._nudge_thread("u1", "th1", [todo]))
        assert ("u1", "t1") not in worker._nudged  # still retrying

    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert ("u1", "t1") in worker._nudged
    assert worker._nudge_failures == {}
    assert worker._nudges_sent == 0  # no successful nudge was counted


def test_success_clears_failure_count(tmp_path):
    client = FakeClient()
    worker = _worker(tmp_path, client)
    todo = _stale_todo()
    worker._nudge_failures[("u1", "t1")] = MAX_NUDGE_FAILURES - 1

    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert ("u1", "t1") in worker._nudged
    assert worker._nudge_failures == {}


def test_error_after_response_still_marks_nudged(tmp_path):
    # The agent saw the nudge and answered; a later error must not cause a
    # duplicate nudge next cycle.
    client = FakeClient(
        chunks=[
            {"type": "response", "content": "on it"},
            {"type": "error", "content": "post-turn hiccup"},
            {"type": "done"},
        ]
    )
    worker = _worker(tmp_path, client)
    todo = _stale_todo()

    asyncio.run(worker._nudge_thread("u1", "th1", [todo]))

    assert ("u1", "t1") in worker._nudged


# ── State pruning ──────────────────────────────────────────────────────────


def test_check_user_prunes_state_for_vanished_todos(tmp_path):
    fresh = _stale_todo(todo_id="live", minutes_old=0)  # recent => not stale
    client = FakeClient(todos=[fresh])
    worker = _worker(tmp_path, client)

    now = datetime.now(timezone.utc)
    worker._nudged[("u1", "dead")] = now.timestamp()
    worker._timestamps[("u1", "dead")] = now
    # Another user's state must survive a u1 listing untouched.
    worker._nudged[("u2", "other")] = now.timestamp()

    asyncio.run(worker._check_user("u1"))

    assert ("u1", "dead") not in worker._nudged
    assert ("u1", "dead") not in worker._timestamps
    assert ("u2", "other") in worker._nudged
    # The fresh TODO is not stale, so no nudge fired.
    assert client.chat_calls == []
