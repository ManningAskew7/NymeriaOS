"""Tests for the bots' dropped-turn chat recovery (backlog #88).

``consume_chat_stream_with_recovery`` is the bot-side port of the CLI
transport's re-attach loop: a connection drop after ``turn_started`` resumes
the turn from the last seen ``seq`` instead of raising into the bots' legacy
sync fallback (which re-POSTs the prompt and runs the turn a second time).
Failures BEFORE the turn's identity re-raise so that fallback, and its
platform branches (429 shed), keep working for the only case where a re-POST
is not a duplicate.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest

from nymeria.triggers import sse_consumer as sse_consumer_mod
from nymeria.triggers.sse_consumer import (
    TURN_LOST_MESSAGE,
    consume_chat_stream_with_recovery,
)


class _Handler:
    """Recording SSEEventHandler."""

    def __init__(self) -> None:
        self.chunks: List[str] = []
        self.errors: List[str] = []
        self.done: List[int] = []
        self.stream_ends: List[int] = []
        self.flushes = 0

    async def flush_text(self, final: bool = False) -> None:
        self.flushes += 1

    async def on_thinking(self) -> None: ...
    async def on_response_chunk(self, content: str) -> None:
        self.chunks.append(content)

    async def on_compacting(self, message: str) -> None: ...
    async def on_compacted(self, summary, messages_removed, title) -> None: ...
    async def on_tool_call(self, name, args, call_id, count) -> None: ...
    async def on_tool_result(self, call_id, result, attachments) -> None: ...
    async def on_tool_reload(self, tools, ttl) -> None: ...
    async def on_workspace_artifact(self, path: str) -> None: ...
    async def on_error(self, content: str) -> None:
        self.errors.append(content)

    async def on_iteration_limit(self, content: str) -> None: ...
    async def on_turn_rewound(self, content: str) -> None: ...
    async def on_done(self, tool_call_count: int) -> None:
        self.done.append(tool_call_count)

    async def on_stream_end(self, tool_call_count: int) -> None:
        self.stream_ends.append(tool_call_count)


class _PoisonHandler(_Handler):
    """Raises once while rendering a marked chunk (a bot-side render bug)."""

    async def on_response_chunk(self, content: str) -> None:
        if "POISON" in content and "POISON" not in "".join(self.chunks):
            self.chunks.append(content)
            raise RuntimeError("render exploded")
        await super().on_response_chunk(content)


class _RecoveryAPI:
    """Fake chat API: a primary stream plus reattach queues and statuses.

    ``reattach_queues`` mirrors the server's exclusive ``from_seq`` replay
    (events at or below the cursor are skipped); each call consumes one
    queue. A queue entry of ``{"_raise": exc}`` raises mid-stream.
    """

    def __init__(
        self,
        primary: List[Any],
        *,
        statuses: Optional[List[Optional[Dict[str, Any]]]] = None,
        reattach_queues: Optional[List[List[Any]]] = None,
    ) -> None:
        self._primary = list(primary)
        self._statuses = list(statuses or [])
        self._queues = [list(q) for q in (reattach_queues or [])]
        self.reattach_calls: List[tuple] = []
        self.status_calls = 0
        self.chat_calls = 0
        self.chat_stream_kwargs: List[Dict[str, Any]] = []

    async def chat_stream(self, message: str, thread_id: str, user_id: str, **kwargs):
        self.chat_calls += 1
        self.chat_stream_kwargs.append(kwargs)
        for item in self._primary:
            if isinstance(item, dict) and "_raise" in item:
                raise item["_raise"]
            yield item

    async def get_thread_status(self, thread_id: str, user_id: Optional[str] = None):
        self.status_calls += 1
        if not self._statuses:
            return {"processing": False, "turn": None}
        if len(self._statuses) == 1:
            return self._statuses[0]
        return self._statuses.pop(0)

    async def reattach_turn_stream(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        turn_id: Optional[str] = None,
        from_seq: int = 0,
    ):
        self.reattach_calls.append((turn_id, from_seq))
        events = self._queues.pop(0) if self._queues else []
        for item in events:
            if isinstance(item, dict) and "_raise" in item:
                raise item["_raise"]
            seq = item.get("seq")
            if isinstance(seq, int) and seq <= from_seq:
                continue
            yield item


_STATUS_T1 = {"processing": True, "turn": {"turn_id": "t1"}}


def _run(api: Any, handler: Any) -> str:
    return asyncio.run(
        consume_chat_stream_with_recovery(
            api, handler, message="hi", thread_id="th", user_id="u1",
        )
    )


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sse_consumer_mod, "CHAT_RECOVERY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(sse_consumer_mod, "CHAT_RECOVERY_MAX_DELAY_SECONDS", 0.0)


def test_drop_after_turn_started_reattaches_and_delivers_tail_once():
    """#88 behavior 13: the tail arrives exactly once, no re-POST."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "response", "content": "Hello ", "seq": 2},
            {"_raise": httpx.ReadError("dropped")},
        ],
        statuses=[_STATUS_T1],
        reattach_queues=[
            [
                {"type": "turn_attach", "turn_id": "t1"},
                {"type": "response", "content": "Hello ", "seq": 2},
                {"type": "response", "content": "world", "seq": 3},
                {"type": "done", "seq": 4},
            ]
        ],
    )
    handler = _Handler()
    assert _run(api, handler) == "recovered"
    assert handler.chunks == ["Hello ", "world"]  # seq 2 not replayed twice
    assert handler.done == [0]
    assert handler.stream_ends == [0]
    assert handler.errors == []
    assert api.reattach_calls == [("t1", 2)]


def test_second_drop_advances_cursor_and_recovers_again():
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"_raise": httpx.ReadError("dropped")},
        ],
        statuses=[_STATUS_T1],
        reattach_queues=[
            [
                {"type": "response", "content": "part one ", "seq": 2},
                {"_raise": httpx.ReadError("dropped again")},
            ],
            [
                {"type": "response", "content": "part two", "seq": 3},
                {"type": "done", "seq": 4},
            ],
        ],
    )
    handler = _Handler()
    assert _run(api, handler) == "recovered"
    assert handler.chunks == ["part one ", "part two"]
    assert api.reattach_calls == [("t1", 1), ("t1", 2)]


def test_turn_gone_yields_honest_lost_message_not_silence():
    """#88 behavior 14: unrecoverable turn ends with the honest notice."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "response", "content": "partial", "seq": 2},
            {"_raise": httpx.ReadError("dropped")},
        ],
        statuses=[{"processing": False, "turn": None}],
    )
    handler = _Handler()
    assert _run(api, handler) == "lost"
    assert handler.errors == [TURN_LOST_MESSAGE]
    assert handler.stream_ends == [0]
    assert api.reattach_calls == []


def test_failure_before_turn_started_reraises_for_legacy_fallback():
    """#88 behavior 15: a pre-turn failure re-raises so the bots' sync
    fallback (and its 429-shed branch) keeps working; the handler is left
    unfinished for the fallback to own."""
    api = _RecoveryAPI([{"_raise": httpx.ConnectError("refused")}])
    handler = _Handler()
    with pytest.raises(httpx.ConnectError):
        _run(api, handler)
    assert handler.stream_ends == []
    assert api.status_calls == 0


def test_prompt_queued_stream_never_recovers():
    """#88 behavior 16: a queued ack shape never held a turn."""
    api = _RecoveryAPI(
        [
            {"type": "prompt_queued", "seq": 1},
        ],
    )
    handler = _Handler()
    assert _run(api, handler) == "queued"
    assert api.status_calls == 0
    assert handler.stream_ends == [0]


def test_dispatched_stream_finalizes_locally_without_recovery():
    """#88 behavior 17: a dispatched turn buffers under the TARGET thread;
    polling this thread would end in a spurious turn_lost."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "dispatched", "dispatched_to": {"title": "Other"}, "seq": 2},
        ],
    )
    handler = _Handler()
    assert _run(api, handler) == "dispatched"
    assert api.status_calls == 0
    assert handler.stream_ends == [0]


def test_api_with_identity_but_no_reattach_degrades_to_honest_message():
    """#88 behavior 18, for a client that HAS turn identity but no
    re-attach capability: honest message, never silent truncation, never a
    re-raise into a duplicate-running fallback. NOTE: today's real
    re-attach-less clients (the Teams/WhatsApp in-process adapter) also
    never emit turn_started, so they take the re-raise path below instead;
    this fake models the guard for any future client that gains identity
    before it gains re-attach."""

    class _IdentityNoReattachAPI:
        async def chat_stream(self, message, thread_id, user_id, **kwargs):
            yield {"type": "turn_started", "turn_id": "t1", "seq": 1}
            raise httpx.ReadError("dropped")

    handler = _Handler()
    assert _run(_IdentityNoReattachAPI(), handler) == "lost"
    assert handler.errors == [TURN_LOST_MESSAGE]
    assert handler.stream_ends == [0]


def test_inprocess_adapter_shape_reraises_unchanged():
    """The real Teams/WhatsApp adapter shape: raw agent chunks, NO
    turn_started ever. A mid-stream failure must re-raise so those bots'
    legacy sync fallback (and 429-shed branch) behaves exactly as before
    the pass; recovery is deliberately inert for identity-less streams."""

    class _InProcessShapeAPI:
        async def chat_stream(self, message, thread_id, user_id, **kwargs):
            yield {"type": "response", "content": "partial"}
            raise RuntimeError("agent exploded mid-stream")

    handler = _Handler()
    with pytest.raises(RuntimeError):
        _run(_InProcessShapeAPI(), handler)
    assert handler.chunks == ["partial"]
    assert handler.stream_ends == []  # the fallback owns the ending


def test_chat_kwargs_reach_chat_stream():
    """Silently dropping a call site's kwargs (platform_origin,
    attachments, ...) must not stay green."""
    api = _RecoveryAPI([{ "type": "done", "seq": 1}])
    handler = _Handler()
    kwargs = {
        "platform_origin": {"platform": "slack"},
        "attachments": [{"name": "a.txt"}],
        "publish_autonomous_events": False,
    }
    outcome = asyncio.run(
        consume_chat_stream_with_recovery(
            api,
            handler,
            message="hi",
            thread_id="th",
            user_id="u1",
            chat_kwargs=kwargs,
        )
    )
    assert outcome == "completed"
    assert api.chat_stream_kwargs == [kwargs]


def test_recovery_attempt_budget_is_bounded():
    """Persistent reattach failure with zero progress exhausts the budget
    and ends honestly instead of looping forever."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"_raise": httpx.ReadError("dropped")},
        ],
        statuses=[_STATUS_T1],
        reattach_queues=[[{"_raise": httpx.ReadError("still down")}]] * 30,
    )
    handler = _Handler()
    assert _run(api, handler) == "lost"
    assert len(api.reattach_calls) == sse_consumer_mod.CHAT_RECOVERY_MAX_ATTEMPTS
    assert handler.errors == [TURN_LOST_MESSAGE]


class _StreamEndRaises(_Handler):
    async def on_stream_end(self, tool_call_count: int) -> None:
        await super().on_stream_end(tool_call_count)
        raise RuntimeError("finalize exploded")


def test_on_stream_end_raising_is_contained_after_recovery():
    """A raising on_stream_end after the turn's identity is known must not
    escape: the caller's except would re-POST a turn whose reply already
    landed."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"_raise": httpx.ReadError("dropped")},
        ],
        statuses=[_STATUS_T1],
        reattach_queues=[[{"type": "done", "seq": 2}]],
    )
    handler = _StreamEndRaises()
    assert _run(api, handler) == "recovered"
    assert handler.stream_ends == [0]


def test_on_stream_end_raising_is_contained_after_terminal_then_drop():
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "done", "seq": 2},
            {"_raise": httpx.ReadError("dropped during close")},
        ],
    )
    handler = _StreamEndRaises()
    assert _run(api, handler) == "completed"
    assert api.chat_calls == 1  # and nothing escaped to re-POST


def test_clean_end_without_terminal_drains_buffered_tail():
    """A clean stream end with no terminal (turn-end disconnect probe
    withheld it) drains the buffered done via the same recovery."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "response", "content": "answer", "seq": 2},
        ],
        statuses=[_STATUS_T1],
        reattach_queues=[[{"type": "done", "seq": 3}]],
    )
    handler = _Handler()
    assert _run(api, handler) == "recovered"
    assert handler.chunks == ["answer"]
    assert handler.done == [0]


def test_terminal_seen_then_drop_completes_without_recovery():
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "response", "content": "answer", "seq": 2},
            {"type": "done", "seq": 3},
            {"_raise": httpx.ReadError("dropped during close")},
        ],
    )
    handler = _Handler()
    assert _run(api, handler) == "completed"
    assert api.status_calls == 0
    assert handler.stream_ends == [0]


def test_handler_exception_mid_turn_recovers_past_the_poison_event():
    """A bot-side render bug after turn_started must not re-raise (the
    caller would re-POST); the cursor already advanced past the poison
    event, so recovery resumes after it."""
    api = _RecoveryAPI(
        [
            {"type": "turn_started", "turn_id": "t1", "seq": 1},
            {"type": "response", "content": "A", "seq": 2},
            {"type": "response", "content": "POISON", "seq": 3},
            {"type": "response", "content": "never reached", "seq": 4},
        ],
        statuses=[_STATUS_T1],
        reattach_queues=[
            [
                {"type": "response", "content": "B", "seq": 4},
                {"type": "done", "seq": 5},
            ]
        ],
    )
    handler = _PoisonHandler()
    assert _run(api, handler) == "recovered"
    assert handler.chunks == ["A", "POISON", "B"]
    assert api.reattach_calls == [("t1", 3)]
    assert api.chat_calls == 1  # never re-POSTed
