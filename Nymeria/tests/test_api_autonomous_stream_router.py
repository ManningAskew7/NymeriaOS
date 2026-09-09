from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from fastapi import HTTPException, Request

from nymeria.api.routers import autonomous_stream as autonomous_module
from nymeria.api.routers.autonomous_stream import (
    _event_to_sse_payload,
    _generate_autonomous_sse_events,
    _resolve_stream_auth,
)
from nymeria.api.sse import SSE_KEEPALIVE_FRAME
from nymeria.core import browser_targets, chrome_subscribers
from nymeria.core.accounts import AccountsRepo
from nymeria.core.event_bus import AutonomousEvent, EventBus, set_event_bus
from nymeria.core.user_profile import UserProfile


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _create_user(
    agent: FakeAgent, user_id: str, *, role: Literal["admin", "user"] = "user"
) -> str:
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
        request=cast(Request, FakeRequest()),
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


# -- browser_command is served to the extension only -----------------------
#
# The envelope carries the whole command, an upload's base64 file bytes
# included. Every other subscriber class parsed a ~14MB frame and dropped it,
# so these pin per-class delivery: the extension gets it, nobody else does,
# and nothing else on the stream changes.


def _upload_command() -> AutonomousEvent:
    return AutonomousEvent(
        event_type="browser_command",
        thread_id="thread-1",
        user_id="alice",
        data={
            "command_id": "cmd-1",
            "command_type": "act",
            "args": {"tab_id": 3, "action": "upload", "file_base64": "QUJD" * 10},
        },
    )


def _marker() -> AutonomousEvent:
    """A plain event queued behind the command, so a dropped command shows up
    as the marker arriving first rather than as a generator that hangs."""
    return AutonomousEvent(
        event_type="response",
        thread_id="thread-1",
        user_id="alice",
        data={"content": "marker"},
    )


def _first_frame_for(client_id: str | None, *, firehose: bool = False) -> str:
    queue: Queue = Queue()
    queue.put_nowait(_upload_command())
    queue.put_nowait(_marker())
    frame, _bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=EventBus(),
            user_id="*" if firehose else "alice",
            firehose=firehose,
            client_id=client_id,
        )
    )
    return frame


def test_browser_command_reaches_the_extension():
    frame = _first_frame_for("nymeria-browser-abc123")

    assert '"type": "browser_command"' in frame
    assert "file_base64" in frame


@pytest.mark.parametrize(
    "client_id",
    [
        pytest.param("6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8", id="desktop_or_mobile_uuid"),
        pytest.param("cli-9f8e7d6c5b4a", id="cli"),
        pytest.param(None, id="legacy_client_without_an_id"),
        pytest.param("nymeria-browser", id="prefix_lookalike"),
    ],
)
def test_browser_command_does_not_reach_other_subscriber_kinds(client_id):
    frame = _first_frame_for(client_id)

    # The marker queued BEHIND the command is what arrives first.
    assert '"content": "marker"' in frame
    assert "browser_command" not in frame
    assert "file_base64" not in frame


def test_browser_command_does_not_reach_the_admin_firehose():
    # The bots' Act-As: * stream sees every user's events and can act on none
    # of these, so it was carrying other users' upload bytes across the
    # container network. Narrowing it is deliberate: a firehose subscriber is
    # not an extension even when it claims an extension client_id.
    frame = _first_frame_for("nymeria-browser-abc123", firehose=True)

    assert '"content": "marker"' in frame
    assert "browser_command" not in frame


# -- browser_login_started/ended are owner-only, never on the firehose -----
#
# #293: session METADATA (login URL, session_id, thread_id, counters --
# never frame bytes or keystrokes, those ride the chrome-only
# browser_login_input) rode the firehose unfiltered because the two types
# were never CHROME_ONLY. They are also NOT extension-only, unlike
# browser_command: desktop's own non-firehose stream is the real, intended
# recipient (it is what raises/retracts the live-login viewer), so these
# tests pin BOTH halves: the owning user's own stream still gets the event,
# and only a firehose subscriber loses it.


def _login_lifecycle_event(event_type: str, *, user_id: str = "alice") -> AutonomousEvent:
    return AutonomousEvent(
        event_type=event_type,
        thread_id="thread-1",
        user_id=user_id,
        data={
            "session_id": "blogin_abc123",
            "tab_id": 7,
            "url": "https://accounts.google.com/signin",
            "origin": "agent",
        },
    )


def _first_frame_for_login_event(event_type: str, *, firehose: bool = False) -> str:
    queue: Queue = Queue()
    queue.put_nowait(_login_lifecycle_event(event_type))
    queue.put_nowait(_marker())
    frame, _bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=EventBus(),
            user_id="*" if firehose else "alice",
            firehose=firehose,
        )
    )
    return frame


@pytest.mark.parametrize("event_type", ["browser_login_started", "browser_login_ended"])
def test_browser_login_lifecycle_reaches_the_owning_users_own_stream(event_type):
    # Guards the naive-fix trap: literally reusing CHROME_ONLY_EVENT_TYPES
    # would require is_extension_stream, which desktop's client_id never
    # satisfies, silencing desktop's own live-viewer raise/retract. This
    # must stay green on both the unfixed AND the correctly-fixed code.
    frame = _first_frame_for_login_event(event_type)

    assert f'"type": "{event_type}"' in frame


@pytest.mark.parametrize("event_type", ["browser_login_started", "browser_login_ended"])
def test_browser_login_lifecycle_does_not_reach_the_admin_firehose(event_type):
    frame = _first_frame_for_login_event(event_type, firehose=True)

    # The marker queued BEHIND the login event is what arrives first.
    assert '"content": "marker"' in frame
    assert event_type not in frame


def _session_release_frame_for(client_id: str | None) -> str:
    queue: Queue = Queue()
    queue.put_nowait(
        AutonomousEvent(
            event_type="browser_session_release",
            thread_id="thread-1",
            user_id="alice",
            data={},
        )
    )
    queue.put_nowait(_marker())
    frame, _bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=EventBus(),
            user_id="alice",
            firehose=False,
            client_id=client_id,
        )
    )
    return frame


def test_session_release_reaches_the_extension_and_nobody_else():
    """The turn-end release (#191) is chrome-only: nothing but the extension
    holds a debugger session, so serving it anywhere else is one event per
    browser-driving turn of pure noise on every other subscriber."""
    frame = _session_release_frame_for("nymeria-browser-abc123")
    assert '"type": "browser_session_release"' in frame

    for client_id in ("6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8", "cli-9f8e7d6c5b4a", None):
        frame = _session_release_frame_for(client_id)
        assert '"content": "marker"' in frame
        assert "browser_session_release" not in frame


def test_non_browser_events_are_untouched_by_the_kind_filter():
    queue: Queue = Queue()
    queue.put_nowait(
        AutonomousEvent(
            event_type="browser_command_result",
            thread_id="thread-1",
            user_id="alice",
            data={"command_id": "cmd-1", "ok": True},
        )
    )
    frame, _bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=EventBus(),
            user_id="alice",
            client_id="6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
        )
    )

    # Results are how a watcher learns what the browser did; only the
    # byte-bearing command envelope is extension-only.
    assert '"type": "browser_command_result"' in frame


# -- M-1: stream auth failures route through the shared rate limiter --------


def test_resolve_stream_auth_invokes_failure_handler_on_bad_token():
    class _Repo:
        def verify_token(self, _token):
            return None

    class _Agent:
        accounts_repo = _Repo()

    calls = []

    def handler(request, failure):
        calls.append((request, failure))
        raise HTTPException(status_code=429, detail="rate limited")

    with pytest.raises(HTTPException) as exc_info:
        _resolve_stream_auth(
            get_agent_fn=lambda: _Agent(),
            requested_user_id="default",
            presented_token="bad-token",
            x_nymeria_act_as=None,
            request=cast(Request, object()),
            auth_failure_handler=handler,
        )

    assert exc_info.value.status_code == 429
    assert len(calls) == 1


def test_resolve_stream_auth_still_401s_without_handler():
    class _Repo:
        def verify_token(self, _token):
            return None

    class _Agent:
        accounts_repo = _Repo()

    with pytest.raises(HTTPException) as exc_info:
        _resolve_stream_auth(
            get_agent_fn=lambda: _Agent(),
            requested_user_id="default",
            presented_token="bad-token",
            x_nymeria_act_as=None,
        )

    assert exc_info.value.status_code == 401


# -- F10: idle keepalive is decoupled from the 1s poll, not emitted per poll ----


class _DisconnectAfter:
    """A request that reports connected for the first ``polls`` checks."""

    def __init__(self, polls: int):
        self._polls = polls
        self._seen = 0

    async def is_disconnected(self) -> bool:
        self._seen += 1
        return self._seen > self._polls


def _collect_until_disconnect(
    *,
    queue: Queue,
    event_bus: EventBus,
    user_id: str,
    polls: int = 10,
) -> list[str]:
    """Drain the bare generator until a simulated client disconnect ends it,
    returning every emitted frame.

    The generator returns on disconnect, so the ``async for`` exhausts naturally
    and the generator's ``finally`` unsubscribes (no manual ``aclose``). Callers
    monkeypatch ``_QUEUE_POLL_INTERVAL_SECONDS`` down to keep the run sub-second.
    """
    subscriber_id = "test-subscriber"
    event_bus.subscribe(subscriber_id)

    async def drive() -> list[str]:
        generator = _generate_autonomous_sse_events(
            request=cast(Request, _DisconnectAfter(polls=polls)),
            event_bus=event_bus,
            queue=queue,
            subscriber_id=subscriber_id,
            user_id=user_id,
            firehose=False,
            client_id=None,
        )
        return [frame async for frame in generator]

    return asyncio.run(drive())


def test_autonomous_sse_idle_keepalive_is_decoupled_from_poll(monkeypatch):
    # Over 10 idle polls with a keepalive cadence of every 3rd poll, the stream
    # emits exactly 3 `: keepalive` frames (at polls 3, 6, 9), not one per poll
    # and never the removed `: heartbeat`. Fewer frames than polls proves the
    # keepalive cadence is decoupled from the poll cadence.
    monkeypatch.setattr(autonomous_module, "_QUEUE_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(autonomous_module, "_KEEPALIVE_POLL_INTERVAL", 3)
    queue: Queue = Queue()
    event_bus = EventBus()

    frames = _collect_until_disconnect(
        queue=queue, event_bus=event_bus, user_id="alice", polls=10
    )

    assert frames.count(SSE_KEEPALIVE_FRAME) == 3
    assert all(frame == SSE_KEEPALIVE_FRAME for frame in frames)
    assert ": heartbeat\n\n" not in frames
    assert event_bus.get_subscriber_count() == 0


def test_autonomous_sse_event_delivered_before_any_keepalive(monkeypatch):
    # A queued event is delivered as the first frame, ahead of any keepalive, so
    # decoupling the keepalive cadence does not add latency to real events.
    monkeypatch.setattr(autonomous_module, "_QUEUE_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(autonomous_module, "_KEEPALIVE_POLL_INTERVAL", 3)
    queue: Queue = Queue()
    event_bus = EventBus()
    queue.put_nowait(
        AutonomousEvent(
            event_type="response",
            thread_id="thread-alice",
            user_id="alice",
            data={"content": "visible"},
        )
    )

    frames = _collect_until_disconnect(
        queue=queue, event_bus=event_bus, user_id="alice", polls=5
    )

    assert frames[0].startswith("data: ")
    assert '"content": "visible"' in frames[0]
    assert ": heartbeat\n\n" not in frames
    assert event_bus.get_subscriber_count() == 0


def test_autonomous_sse_keepalive_uses_shared_comment_frame():
    # The idle frame is the shared `: keepalive` SSE comment (from api/sse.py),
    # which every consumer skips, not a bespoke literal.
    assert SSE_KEEPALIVE_FRAME == ": keepalive\n\n"
    assert SSE_KEEPALIVE_FRAME.startswith(":")


def test_autonomous_generator_no_longer_self_emits_heartbeat():
    # Regression guard: the per-second `: heartbeat` self-emit is gone. Its
    # return would restore the ~25x idle frame volume this finding removed.
    import inspect

    source = inspect.getsource(_generate_autonomous_sse_events)
    assert ": heartbeat" not in source


# -- B-6: doorbell push latency beats the poll interval -------------------------


def test_autonomous_sse_push_latency_beats_the_poll_interval():
    # With the PRODUCTION 1s poll interval untouched, an event published from
    # a worker thread while the generator idles must arrive well under that
    # interval: the enqueue rings the subscriber's doorbell and the generator
    # wakes on it instead of sleeping out the poll timer. Before the doorbell
    # (measured) idle-bus delivery averaged 871ms; with it, sub-millisecond.
    # The 0.6s bound leaves CI slack while staying red for any poll-bound
    # regression, which cannot deliver before ~1.0s.
    import threading
    import time

    async def drive() -> tuple[str, float]:
        event_bus = EventBus()
        subscriber_id = "test-subscriber"
        queue = event_bus.subscribe(subscriber_id)  # binds doorbell here
        generator = _generate_autonomous_sse_events(
            request=cast(Request, FakeRequest()),
            event_bus=event_bus,
            queue=queue,
            subscriber_id=subscriber_id,
            user_id="alice",
            firehose=False,
            client_id=None,
        )

        def publish_later() -> None:
            time.sleep(0.15)  # let the generator park in its doorbell wait
            event_bus.publish(
                AutonomousEvent(
                    event_type="response",
                    thread_id="thread-alice",
                    user_id="alice",
                    data={"content": "pushed"},
                )
            )

        thread = threading.Thread(target=publish_later)
        thread.start()
        start = time.monotonic()
        try:
            frame = await asyncio.wait_for(generator.__anext__(), timeout=5.0)
        finally:
            await generator.aclose()
            thread.join()
        return frame, time.monotonic() - start

    frame, elapsed = asyncio.run(drive())
    assert frame.startswith("data: ")
    assert '"content": "pushed"' in frame
    # Poll-bound delivery cannot beat ~1.15s (the 1s timer plus the 0.15s
    # publish delay); doorbell-woken delivery lands at ~0.15s. The 0.9s
    # bound keeps xdist-on-a-loaded-box slack while staying red for any
    # poll-bound regression.
    assert elapsed < 0.9, (
        f"frame took {elapsed:.3f}s: delivery is poll-bound, not doorbell-woken"
    )


# -- single-browser routing: targeted events reach only their browser -------


def _targeted_command(target: str) -> AutonomousEvent:
    return AutonomousEvent(
        event_type="browser_command",
        thread_id="thread-1",
        user_id="alice",
        data={
            "command_id": "cmd-2",
            "command_type": "tabs",
            "args": {"action": "list"},
            "_target_client_id": target,
        },
    )


def _first_frame_for_targeted(client_id: str, target: str) -> str:
    queue: Queue = Queue()
    queue.put_nowait(_targeted_command(target))
    queue.put_nowait(_marker())
    frame, _bus = asyncio.run(
        _next_sse_data(
            queue=queue,
            event_bus=EventBus(),
            user_id="alice",
            firehose=False,
            client_id=client_id,
        )
    )
    return frame


def test_targeted_command_reaches_the_target_browser_without_the_marker_key():
    frame = _first_frame_for_targeted(
        "nymeria-browser-abc123", "nymeria-browser-abc123"
    )
    assert '"type": "browser_command"' in frame
    # The routing marker is backend-internal: stripped from the wire.
    assert "_target_client_id" not in frame


def test_targeted_command_skips_every_other_connected_browser():
    """The #282 fix at the delivery layer: a second extension on the same
    account receives NOTHING for a command routed elsewhere, so it can
    neither execute it nor race the result."""
    frame = _first_frame_for_targeted(
        "nymeria-browser-other99", "nymeria-browser-abc123"
    )
    assert '"content": "marker"' in frame
    assert "browser_command" not in frame


# -- the extension announces its kind and label on connect (E8 / E9, wire) -----
#
# `client_kind` lands on the roster row that chrome_browsers / chrome_health /
# the refusals read; `client_label` seeds the account's label for that browser
# once. Both ride the query string of the REAL route, parsed by FastAPI, so
# these go through the app rather than calling the generator: the generator is
# swapped for a one-frame stub so the otherwise endless stream completes and
# the client's GET returns.

_EXT_ID = "nymeria-browser-wiretest-0000-4000-8000-000000000001"


async def _one_frame_stream(**_kwargs):
    yield ": stub\n\n"


@pytest.fixture
def clean_chrome_registry():
    chrome_subscribers.reset_for_tests()
    set_event_bus(EventBus())
    yield
    chrome_subscribers.reset_for_tests()
    set_event_bus(EventBus())


def _connect_extension(
    api_client_builder, tmp_path, monkeypatch, *, query: str, token: str | None = None
) -> str:
    """One real extension subscribe through the app; returns the account token.

    A test that connects TWICE must pass the token back in: the accounts DB
    lives in ``tmp_path`` and outlives the FakeAgent, so re-creating the user
    trips its unique-email constraint.
    """
    monkeypatch.setattr(
        autonomous_module, "_generate_autonomous_sse_events", _one_frame_stream
    )
    agent = FakeAgent(tmp_path)
    token = token or _create_user(agent, "alice")
    client = api_client_builder.client(agent, api_client_builder.settings(tmp_path))
    response = client.get(
        f"/autonomous/stream?{query}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    return token


@pytest.mark.usefixtures("clean_chrome_registry")
@pytest.mark.parametrize(
    ("kind_param", "expected"),
    [
        pytest.param("&client_kind=server", "server", id="server"),
        pytest.param("&client_kind=desktop", "desktop", id="desktop"),
        pytest.param("", "desktop", id="absent_defaults_to_desktop"),
        pytest.param("&client_kind=headless", "desktop", id="unknown_value_stores_desktop"),
    ],
)
def test_client_kind_on_the_connect_lands_on_the_roster_row(
    api_client_builder, tmp_path, monkeypatch, kind_param, expected
):
    _connect_extension(
        api_client_builder,
        tmp_path,
        monkeypatch,
        query=f"client_id={_EXT_ID}&client_version=0.29.0{kind_param}",
    )

    (row,) = chrome_subscribers.chrome_browser_roster("alice")
    assert (row.client_id, row.kind, row.version) == (_EXT_ID, expected, "0.29.0")


@pytest.mark.usefixtures("clean_chrome_registry")
def test_client_kind_is_ignored_for_non_extension_streams(
    api_client_builder, tmp_path, monkeypatch
):
    """A desktop app claiming client_kind=server is not an extension (no
    nymeria-browser- prefix), so it registers nothing on the roster."""
    _connect_extension(
        api_client_builder,
        tmp_path,
        monkeypatch,
        query="client_id=6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8&client_kind=server",
    )

    assert chrome_subscribers.chrome_browser_roster("alice") == []
    assert not chrome_subscribers.known_server_browser("alice")


def _stub_profile_host(monkeypatch) -> UserProfile:
    profile = UserProfile(user_id="alice")

    class _Profiles:
        def get_profile(self, user_id):
            return profile

        @contextmanager
        def atomic_update(self, user_id):
            yield profile

    monkeypatch.setattr(
        browser_targets, "_agent", lambda: SimpleNamespace(profile_manager=_Profiles())
    )
    return profile


class _SeedProbe:
    """Watches the label seed a connect schedules, so these tests never sleep.

    The seed runs on a worker thread off the request path, so "check the
    profile a moment later" is a race: under `-n 2` on a loaded box a broken
    overwrite landing after the old 0.2s sleep still passed the test. This
    watches the seeder itself. ``finished`` fires when a seed has RUN, so a
    negative about its result is read after the write rather than during it;
    ``entered`` fires the instant one starts, so a test that says no seed
    should happen at all fails the moment one does, instead of hoping a sleep
    outlasts it. The assertions stay on the stored labels either way.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.finished = threading.Event()

    def install(self, monkeypatch) -> "_SeedProbe":
        real = autonomous_module.seed_browser_label

        def _watched(*args, **kwargs):
            self.entered.set()
            try:
                return real(*args, **kwargs)
            finally:
                self.finished.set()

        monkeypatch.setattr(autonomous_module, "seed_browser_label", _watched)
        return self

    def await_seed(self) -> None:
        assert self.finished.wait(10), "the scheduled label seed never ran"
        self.finished.clear()
        self.entered.clear()

    def assert_no_seed(self) -> None:
        assert not self.entered.wait(1.0), "a seed ran that should not have"


@pytest.mark.usefixtures("clean_chrome_registry")
def test_client_label_seeds_the_profile_and_a_rename_survives_a_reconnect(
    api_client_builder, tmp_path, monkeypatch
):
    """E9 on the wire: the bake's label names the browser on first connect
    when the user has not; the user's rename then wins over every later
    connect announcing the bake label."""
    profile = _stub_profile_host(monkeypatch)
    probe = _SeedProbe().install(monkeypatch)
    query = f"client_id={_EXT_ID}&client_kind=server&client_label=server%20browser"

    token = _connect_extension(api_client_builder, tmp_path, monkeypatch, query=query)
    probe.await_seed()
    assert browser_targets.browser_labels("alice") == {_EXT_ID: "server browser"}

    assert browser_targets.set_browser_label("alice", _EXT_ID, "rig") is None
    _connect_extension(
        api_client_builder, tmp_path, monkeypatch, query=query, token=token
    )
    # The reconnect's seed RAN and declined to write, which is the claim.
    probe.await_seed()
    assert profile.get_browser_preferences()["labels"] == {_EXT_ID: "rig"}


@pytest.mark.usefixtures("clean_chrome_registry")
def test_a_removed_name_is_not_restored_by_the_next_connect(
    api_client_builder, tmp_path, monkeypatch
):
    """On the wire, the un-name case: the server browser announces its baked
    label on EVERY reconnect (about once a minute), so a name the user took
    off has to stay off or they cannot un-name the rig at all."""
    profile = _stub_profile_host(monkeypatch)
    probe = _SeedProbe().install(monkeypatch)
    query = f"client_id={_EXT_ID}&client_kind=server&client_label=server%20browser"

    token = _connect_extension(api_client_builder, tmp_path, monkeypatch, query=query)
    probe.await_seed()
    assert browser_targets.set_browser_label("alice", _EXT_ID, None) is None

    _connect_extension(
        api_client_builder, tmp_path, monkeypatch, query=query, token=token
    )
    probe.await_seed()

    assert profile.get_browser_preferences()["labels"] == {}


@pytest.mark.usefixtures("clean_chrome_registry")
def test_connect_without_a_label_seeds_nothing(api_client_builder, tmp_path, monkeypatch):
    profile = _stub_profile_host(monkeypatch)
    probe = _SeedProbe().install(monkeypatch)

    _connect_extension(
        api_client_builder, tmp_path, monkeypatch, query=f"client_id={_EXT_ID}&client_kind=server"
    )

    probe.assert_no_seed()
    assert profile.get_browser_preferences()["labels"] == {}


@pytest.mark.usefixtures("clean_chrome_registry")
def test_a_malformed_client_id_becomes_neither_a_roster_row_nor_a_label(
    api_client_builder, tmp_path, monkeypatch
):
    """A labelled connect turns the client-chosen client_id into a DURABLE
    profile key, and nothing else validates it: 300 characters of anything
    were accepted on the strength of the prefix alone. A value that is not
    the shape the extension mints is not an extension stream at all."""
    profile = _stub_profile_host(monkeypatch)
    probe = _SeedProbe().install(monkeypatch)
    oversized = "nymeria-browser-" + "x" * 300

    _connect_extension(
        api_client_builder,
        tmp_path,
        monkeypatch,
        query=f"client_id={oversized}&client_kind=server&client_label=rig",
    )

    probe.assert_no_seed()
    assert chrome_subscribers.chrome_browser_roster("alice") == []
    assert profile.get_browser_preferences()["labels"] == {}
