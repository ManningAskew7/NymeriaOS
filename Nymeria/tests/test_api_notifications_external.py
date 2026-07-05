"""Tests for POST /notifications/external and its api_client transport.

The endpoint is the vault-safe delivery path for thin services that hold
only the service token (never NYMERIA_SECRETS_KEY): they send off-frontend
alerts through the API, which owns the key and does the actual channel
dispatch. (The watchdog sweep now dispatches in-process from the ticker;
this endpoint remains the generic thin-client surface.) These tests pin the
auth contract (Act-As scoping) and the request/response shapes on both
sides of the wire.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.api.routers import notifications_config as notifications_config_module
from nymeria.triggers.api_client import NymeriaAPIClient


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _patch_dispatch(monkeypatch, result=None):
    """Replace the channel dispatch with a recorder; returns the call log."""
    calls: list[dict[str, Any]] = []

    def _fake_dispatch(message, settings, *, user_id="default", thread_id=""):
        calls.append(
            {"message": message, "user_id": user_id, "thread_id": thread_id}
        )
        return list(result or [])

    monkeypatch.setattr(
        notifications_config_module,
        "send_external_notifications",
        _fake_dispatch,
    )
    return calls


def _client(tmp_path, api_client_builder, *, role="user"):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="owner", role=role,
    )
    return client, agent, token


# ── Endpoint auth + behavior ──────────────────────────────────────────────


def test_external_send_requires_auth(tmp_path, api_client_builder, monkeypatch):
    _patch_dispatch(monkeypatch)
    client, _agent, _token = _client(tmp_path, api_client_builder)
    resp = client.post("/notifications/external", json={"message": "hi"})
    assert resp.status_code == 401


def test_external_send_dispatches_for_authenticated_user(
    tmp_path, api_client_builder, monkeypatch
):
    calls = _patch_dispatch(monkeypatch, result=["tg-main"])
    client, _agent, token = _client(tmp_path, api_client_builder)

    resp = client.post(
        "/notifications/external",
        json={"message": "stale todos", "thread_id": "th-1"},
        headers=api_client_builder.auth(token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"delivered_to": ["tg-main"]}
    assert calls == [
        {"message": "stale todos", "user_id": "owner", "thread_id": "th-1"}
    ]


def test_external_send_rejects_non_admin_act_as(
    tmp_path, api_client_builder, monkeypatch
):
    calls = _patch_dispatch(monkeypatch)
    client, agent, token = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("victim", "victim@example.com", "Victim")

    resp = client.post(
        "/notifications/external",
        json={"message": "hi"},
        headers=api_client_builder.auth(token, **{"X-Nymeria-Act-As": "victim"}),
    )
    assert resp.status_code == 403
    assert calls == []


def test_external_send_admin_act_as_targets_that_user(
    tmp_path, api_client_builder, monkeypatch
):
    # The service token is admin-role, so this is the thin-client path:
    # admin credentials plus X-Nymeria-Act-As select the notified user.
    calls = _patch_dispatch(monkeypatch, result=["dest-a"])
    client, agent, token = _client(tmp_path, api_client_builder, role="admin")
    agent.accounts_repo.create_user("target", "target@example.com", "Target")

    resp = client.post(
        "/notifications/external",
        json={"message": "hi", "thread_id": "th-9"},
        headers=api_client_builder.auth(token, **{"X-Nymeria-Act-As": "target"}),
    )
    assert resp.status_code == 200
    assert resp.json() == {"delivered_to": ["dest-a"]}
    assert calls == [{"message": "hi", "user_id": "target", "thread_id": "th-9"}]


def test_external_send_rejects_empty_message(
    tmp_path, api_client_builder, monkeypatch
):
    calls = _patch_dispatch(monkeypatch)
    client, _agent, token = _client(tmp_path, api_client_builder)

    resp = client.post(
        "/notifications/external",
        json={"message": ""},
        headers=api_client_builder.auth(token),
    )
    assert resp.status_code == 422
    assert calls == []


# ── NymeriaAPIClient.send_external_notification ───────────────────────────


class _FakeResponse:
    def __init__(self, payload: Any):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _RecordingHTTPClient:
    def __init__(self, payload: Any):
        self._payload = payload
        self.requests: list[dict[str, Any]] = []

    async def request(self, method, url, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers"),
            }
        )
        return _FakeResponse(self._payload)


def _drive_client(payload: Any) -> tuple[list[str], _RecordingHTTPClient]:
    client = NymeriaAPIClient("http://api.test", "tok")
    recording = _RecordingHTTPClient(payload)
    client._client_for_loop = lambda: recording  # type: ignore[method-assign]

    async def _run() -> list[str]:
        return await client.send_external_notification(
            "user-7", "alert body", thread_id="th-2",
        )

    return asyncio.run(_run()), recording


def test_client_method_posts_with_act_as_and_parses_delivered():
    delivered, recording = _drive_client({"delivered_to": ["tg-main", "slack"]})
    assert delivered == ["tg-main", "slack"]

    req = recording.requests[0]
    assert req["method"] == "POST"
    assert req["url"].endswith("/notifications/external")
    assert req["json"] == {"message": "alert body", "thread_id": "th-2"}
    assert req["headers"]["X-Nymeria-Act-As"] == "user-7"


def test_client_method_tolerates_malformed_response():
    delivered, _recording = _drive_client({"unexpected": True})
    assert delivered == []
