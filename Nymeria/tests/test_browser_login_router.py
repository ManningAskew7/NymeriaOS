"""Integration tests for the /browser-login endpoints.

Covers the plan's expected behaviors for the three wires of the human
login handoff: frames up from the extension (B1), frames down to the
viewer (B1), operator input toward the tab (B2), and the confidentiality
properties that make the handoff worth having, namely that a login
session is strictly its owner's (no admin exemption) and that keystrokes
never fan out to other subscribers (B7).
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.browser_login_sessions import (
    STATE_ACTIVE,
    STATE_ENDED,
    get_browser_login_registry,
    new_login_session_id,
    reset_for_tests,
)
from nymeria.core.chrome_subscribers import CHROME_ONLY_EVENT_TYPES
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.triggers import api as api_module

_SECRET_PIXELS = "PASSWORD-ON-SCREEN-JPEG-BYTES"


@dataclass
class _Settings:
    data_dir: Path
    nymeria_api_key: str | None = None
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    context_management: str = "none"
    sliding_window_cycles: int = 20
    todo_auto_archive_days: int = 7
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
    cors_origins_list: list[str] | None = None
    nymeria_public_url: str | None = "https://nymeria.example.test"
    # Read by the app's lifespan startup, which this test's client runs
    # (it enters TestClient so sessions share the app's event loop).
    default_executor_max_workers: int = 32

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = ["http://localhost"]


class _ThreadMetadataManager:
    def get_store(self, user_id: str):
        _ = user_id
        return type("_Store", (), {"threads": {}})()


class _Agent:
    def __init__(self, data_dir: Path) -> None:
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.credential_vault = CredentialVaultRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = _ThreadMetadataManager()
        self.thread_config_manager = None
        self._graph_cache_lock = threading.Lock()
        self._user_graphs: dict = {}
        self._async_user_graphs: dict = {}
        self._base_system_prompt = "base"
        self._default_graph = None
        self._default_async_graph = None

    def _build_graph_with_prompt(self, prompt: str):
        return object()

    def _build_async_graph_with_prompt(self, prompt: str):
        return object()

    def sync_agent_tools(self) -> None:
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    reset_for_tests()
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = _Settings(data_dir=tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    import nymeria.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    import nymeria.core.credential_vault as vault_mod

    vault_mod._vault_repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")
    api_module._reset_auth_failure_rate_limiter_for_tests()

    agent = _Agent(tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice", role="user")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Bob", role="user")
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    # Entered as a context manager on purpose: that gives the client ONE
    # portal for its lifetime, and `client.portal` is the app's own event
    # loop. Creating sessions on it reproduces production, where the login
    # tool, the frame POSTs and the viewer's SSE all run on the single API
    # loop; a test that spread them over three loops would be exercising an
    # arrangement the product never has.
    with TestClient(api_module.create_api_app(agent)) as client:
        yield (
            client,
            agent.accounts_repo.issue_token("alice"),
            agent.accounts_repo.issue_token("bob"),
            agent.accounts_repo.issue_token("admin"),
        )
    api_module._reset_auth_failure_rate_limiter_for_tests()
    reset_for_tests()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _start_session(client, *, user_id: str = "alice", tab_id: int = 7):
    async def start():
        return get_browser_login_registry().start(
            session_id=new_login_session_id(),
            user_id=user_id,
            thread_id=f"thread-{user_id}",
            tab_id=tab_id,
            url="https://accounts.google.com/signin",
        )

    return client.portal.call(start)


def _frames(count: int = 1, data: str = _SECRET_PIXELS) -> dict:
    return {
        "frames": [
            {"data": f"{data}-{index}", "metadata": {"deviceWidth": 1280}}
            for index in range(count)
        ]
    }


def _key_event(key: str = "a") -> dict:
    return {"events": [{"type": "key", "key": key}]}


async def _await(future):
    return await future


def _agent_result(client, future):
    """The outcome the waiting agent tool would receive, read from the app loop."""
    return client.portal.call(_await, future)


# ---------- frames up from the extension ----------


def test_posting_frames_buffers_them_and_acks(env) -> None:
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)

    resp = client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(2),
    )

    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2, "session_active": True, "last_seq": 2}
    assert session.frames_received == 2


def test_frames_for_an_ended_session_get_the_stop_cue(env) -> None:
    """The extension's only way to learn about an ending it never heard: a
    session that ended on the backend's clock answers session_active false,
    and the extension stops capturing."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    get_browser_login_registry().finish(session.session_id, reason="expired")

    resp = client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(1),
    )

    assert resp.status_code == 200
    assert resp.json()["session_active"] is False
    assert resp.json()["accepted"] == 0
    assert session.frames_received == 0


def test_a_frame_racing_the_end_gets_the_stop_cue(env) -> None:
    """The narrow window the ack's state read exists for: the session ended
    between the lookup and the append. Every frame is refused and the ack
    still tells the extension to stop, rather than reporting a live session
    that is already over."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    session.mark_ended("completed")

    resp = client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(2),
    )

    assert resp.json() == {"accepted": 0, "session_active": False, "last_seq": 0}
    assert session.frames_received == 0


def test_another_users_frames_never_reach_the_session(env) -> None:
    """Nobody may push pictures into someone else's viewer."""
    client, _alice, bob, _admin = env
    session, _future = _start_session(client, user_id="alice")

    resp = client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(bob),
        json=_frames(2),
    )

    assert resp.json()["session_active"] is False
    assert session.frames_received == 0


def test_an_admin_may_not_push_frames_into_another_users_session(env) -> None:
    """The sibling browser routes let an admin act for a user; these do not.
    A login session carries a live picture of somebody's password field."""
    client, _alice, _bob, admin = env
    session, _future = _start_session(client, user_id="alice")

    resp = client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(admin),
        json=_frames(1),
    )

    assert resp.json()["session_active"] is False
    assert session.frames_received == 0


def test_an_oversized_frame_batch_is_rejected(env) -> None:
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)

    resp = client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(9),
    )

    assert resp.status_code == 422
    assert session.frames_received == 0


# ---------- frames down to the viewer ----------


def _sse_events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_the_viewer_stream_opens_with_attach_then_carries_frames_then_ends(
    env,
) -> None:
    """The viewer's whole feed in one read: the attach preamble it renders
    its banner and countdown from, the frames, and the terminal event that
    tells it the human is done."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(2),
    )

    async def end_shortly() -> None:
        await asyncio.sleep(0.05)
        get_browser_login_registry().finish(session.session_id, reason="completed")

    # Ends the session from the app loop while the read below is tailing, so
    # this exercises the live path (pulse wakes the reader) and not a replay
    # of an already-finished buffer.
    client.portal.start_task_soon(end_shortly)

    with client.stream(
        "GET",
        f"/browser-login/{session.session_id}/stream",
        headers=_auth(alice),
    ) as response:
        assert response.status_code == 200
        body = response.read().decode()

    events = _sse_events(body)
    assert [event["type"] for event in events] == [
        "login_attach",
        "login_frame",
        "login_frame",
        "login_end",
    ]
    assert events[0]["session_id"] == session.session_id
    assert events[0]["seconds_remaining"] > 0
    assert [event["seq"] for event in events[1:3]] == [1, 2]
    assert events[1]["data"].startswith(_SECRET_PIXELS)
    assert events[1]["metadata"] == {"deviceWidth": 1280}
    assert events[-1]["state"] == STATE_ENDED
    assert events[-1]["end_reason"] == "completed"


def test_the_viewer_stream_resumes_from_a_cursor(env) -> None:
    """A viewer that reconnects asks for what it has not seen, and gets the
    tail rather than the whole buffer again."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(3),
    )

    async def end_shortly() -> None:
        await asyncio.sleep(0.05)
        get_browser_login_registry().finish(session.session_id, reason="completed")

    client.portal.start_task_soon(end_shortly)

    with client.stream(
        "GET",
        f"/browser-login/{session.session_id}/stream?from_seq=2",
        headers=_auth(alice),
    ) as response:
        body = response.read().decode()

    events = _sse_events(body)
    frames = [event for event in events if event["type"] == "login_frame"]
    assert [frame["seq"] for frame in frames] == [3]


def _end_shortly(client, session_id: str) -> None:
    """Schedule the session's end on the app loop.

    Used by the refusal tests below so that a stream wrongly GRANTED still
    terminates: without it, a regression that hands out someone else's feed
    would hang the suite instead of failing it, and a hang is the one test
    outcome nobody reads as a bug in the code under test.
    """

    async def end() -> None:
        await asyncio.sleep(0.05)
        get_browser_login_registry().finish(session_id, reason="completed")

    client.portal.start_task_soon(end)


def test_another_user_cannot_open_the_viewer_stream(env) -> None:
    client, _alice, bob, _admin = env
    session, _future = _start_session(client, user_id="alice")
    _end_shortly(client, session.session_id)

    with client.stream(
        "GET", f"/browser-login/{session.session_id}/stream", headers=_auth(bob)
    ) as resp:
        # 404, never 403: the route must not confirm the session exists.
        assert resp.status_code == 404
        assert json.loads(resp.read())["detail"]["code"] == "login_session_not_found"


def test_an_admin_cannot_watch_another_users_login(env) -> None:
    client, _alice, _bob, admin = env
    session, _future = _start_session(client, user_id="alice")
    _end_shortly(client, session.session_id)

    with client.stream(
        "GET", f"/browser-login/{session.session_id}/stream", headers=_auth(admin)
    ) as resp:
        assert resp.status_code == 404


def test_an_unknown_session_stream_is_not_found(env) -> None:
    client, alice, _bob, _admin = env
    with client.stream(
        "GET", "/browser-login/blogin_nope/stream", headers=_auth(alice)
    ) as resp:
        assert resp.status_code == 404


# ---------- operator input toward the tab ----------


def test_input_is_published_for_the_extension_to_replay(env, monkeypatch) -> None:
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    published: list = []
    import nymeria.api.routers.browser_login as router_mod

    monkeypatch.setattr(
        router_mod, "publish_autonomous_event", lambda **kw: published.append(kw)
    )

    resp = client.post(
        f"/browser-login/{session.session_id}/input",
        headers=_auth(alice),
        json=_key_event("s"),
    )

    assert resp.status_code == 200
    assert resp.json()["dispatched"] == 1
    assert len(published) == 1
    event = published[0]
    assert event["event_type"] == "browser_login_input"
    assert event["user_id"] == "alice"
    assert event["data"]["tab_id"] == 7
    assert event["data"]["session_id"] == session.session_id
    assert event["data"]["events"] == [
        {"type": "key", "key": "s", "modifiers": 0, "click_count": 1,
         "delta_x": 0.0, "delta_y": 0.0}
    ]


def test_operator_keystrokes_are_chrome_only(env) -> None:
    """A keystroke in this channel is a character of somebody's password.
    Fanning it out on the autonomous stream would copy it to the desktop,
    mobile, both CLI transports and the bots' admin firehose."""
    _ = env
    assert "browser_login_input" in CHROME_ONLY_EVENT_TYPES


def test_another_user_cannot_type_into_a_session(env, monkeypatch) -> None:
    client, _alice, bob, _admin = env
    session, _future = _start_session(client, user_id="alice")
    published: list = []
    import nymeria.api.routers.browser_login as router_mod

    monkeypatch.setattr(
        router_mod, "publish_autonomous_event", lambda **kw: published.append(kw)
    )

    resp = client.post(
        f"/browser-login/{session.session_id}/input",
        headers=_auth(bob),
        json=_key_event(),
    )

    assert resp.status_code == 404
    assert published == []


def test_pointer_coordinates_must_be_normalized(env) -> None:
    """Coordinates are fractions of the frame, so the viewer's scaling and
    the tab's real viewport cannot silently disagree. A pixel value is a
    contract violation, not a big click."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)

    resp = client.post(
        f"/browser-login/{session.session_id}/input",
        headers=_auth(alice),
        json={
            "events": [
                {"type": "mouse", "action": "click", "x": 640, "y": 320, "button": "left"}
            ]
        },
    )

    assert resp.status_code == 422


def test_an_oversized_input_batch_is_rejected(env) -> None:
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)

    resp = client.post(
        f"/browser-login/{session.session_id}/input",
        headers=_auth(alice),
        json={"events": [{"type": "key", "key": "a"}] * 33},
    )

    assert resp.status_code == 422


# ---------- ending the session ----------


def test_done_ends_the_session_wakes_the_agent_and_frees_the_tab(env) -> None:
    client, alice, _bob, _admin = env
    session, future = _start_session(client, tab_id=7)

    resp = client.post(
        f"/browser-login/{session.session_id}/end",
        headers=_auth(alice),
        json={"reason": "completed"},
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == STATE_ENDED
    assert resp.json()["end_reason"] == "completed"
    result = _agent_result(client, future)
    assert result["login_completed"] is True
    assert result["status"] == "completed"
    # And the agent may drive the tab again.
    assert get_browser_login_registry().active_for_tab("alice", 7) is None


def test_cancelling_reports_an_unsuccessful_outcome(env) -> None:
    client, alice, _bob, _admin = env
    session, future = _start_session(client)

    client.post(
        f"/browser-login/{session.session_id}/end",
        headers=_auth(alice),
        json={"reason": "cancelled"},
    )

    result = _agent_result(client, future)
    assert result["login_completed"] is False
    assert result["status"] == "cancelled"


def test_another_user_cannot_end_a_session(env) -> None:
    client, _alice, bob, _admin = env
    session, future = _start_session(client, user_id="alice")

    resp = client.post(
        f"/browser-login/{session.session_id}/end",
        headers=_auth(bob),
        json={"reason": "completed"},
    )

    assert resp.status_code == 404
    assert session.state == STATE_ACTIVE
    assert not future.done()


def test_an_unknown_end_reason_is_rejected(env) -> None:
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)

    resp = client.post(
        f"/browser-login/{session.session_id}/end",
        headers=_auth(alice),
        json={"reason": "signed_in_as_admin"},
    )

    assert resp.status_code == 422
    assert session.state == STATE_ACTIVE


# ---------- the recovery listing ----------


def test_the_listing_shows_only_the_callers_live_sessions(env) -> None:
    client, alice, bob, _admin = env
    mine, _ = _start_session(client, user_id="alice", tab_id=1)
    _start_session(client, user_id="bob", tab_id=2)

    alice_sessions = client.get("/browser-login/sessions", headers=_auth(alice)).json()
    bob_sessions = client.get("/browser-login/sessions", headers=_auth(bob)).json()

    assert [s["session_id"] for s in alice_sessions["sessions"]] == [mine.session_id]
    assert len(bob_sessions["sessions"]) == 1
    assert bob_sessions["sessions"][0]["session_id"] != mine.session_id


def test_the_listing_never_carries_frame_bytes(env) -> None:
    """The listing is the desktop's recovery path, so it is the other place
    a frame could escape into a non-viewer surface."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    client.post(
        f"/browser-login/{session.session_id}/frame",
        headers=_auth(alice),
        json=_frames(1),
    )

    body = client.get("/browser-login/sessions", headers=_auth(alice)).text

    assert _SECRET_PIXELS not in body
    assert json.loads(body)["sessions"][0]["has_frame"] is True


def test_an_ended_session_leaves_the_listing(env) -> None:
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    client.post(
        f"/browser-login/{session.session_id}/end",
        headers=_auth(alice),
        json={"reason": "completed"},
    )

    listing = client.get("/browser-login/sessions", headers=_auth(alice)).json()

    assert listing["sessions"] == []


def test_input_is_stamped_with_the_sessions_pinned_browser(env, monkeypatch) -> None:
    """A pinned session's keystrokes carry the target marker, so the stream
    filter hands them ONLY to the browser the human is signing into."""
    client, alice, _bob, _admin = env

    async def start():
        return get_browser_login_registry().start(
            session_id=new_login_session_id(),
            user_id="alice",
            thread_id="thread-alice",
            tab_id=7,
            url="https://accounts.google.com/signin",
            client_id="nymeria-browser-pinned01",
        )

    session, _future = client.portal.call(start)
    published: list = []
    import nymeria.api.routers.browser_login as router_mod

    monkeypatch.setattr(
        router_mod, "publish_autonomous_event", lambda **kw: published.append(kw)
    )
    resp = client.post(
        f"/browser-login/{session.session_id}/input",
        headers=_auth(alice),
        json=_key_event("s"),
    )

    assert resp.status_code == 200
    assert published[0]["data"]["_target_client_id"] == "nymeria-browser-pinned01"


def test_input_for_an_unpinned_session_is_not_stamped(env, monkeypatch) -> None:
    """Legacy sessions (no pinned browser) keep the pre-routing shape."""
    client, alice, _bob, _admin = env
    session, _future = _start_session(client)
    published: list = []
    import nymeria.api.routers.browser_login as router_mod

    monkeypatch.setattr(
        router_mod, "publish_autonomous_event", lambda **kw: published.append(kw)
    )
    resp = client.post(
        f"/browser-login/{session.session_id}/input",
        headers=_auth(alice),
        json=_key_event("s"),
    )

    assert resp.status_code == 200
    assert "_target_client_id" not in published[0]["data"]
