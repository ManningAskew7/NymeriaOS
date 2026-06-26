from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path


class FakeAgent:
    def __init__(self) -> None:
        self.synced_tools = 0

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1


def _sign(raw_body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_instagram_webhook_verification_challenge(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        instagram_webhook_verify_token="verify-me",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    accepted = client.get(
        "/integrations/instagram/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-value",
        },
    )
    rejected = client.get(
        "/integrations/instagram/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "challenge-value",
        },
    )

    assert accepted.status_code == 200
    assert accepted.text == "challenge-value"
    assert rejected.status_code == 403


def test_instagram_webhook_rejects_bad_signature(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        instagram_app_secret="secret",
        instagram_access_token="token",
        instagram_ig_user_id="ig-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/instagram/webhook",
        content=b'{"entry":[]}',
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid webhook signature"


def test_instagram_webhook_requires_app_secret(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        instagram_access_token="token",
        instagram_ig_user_id="ig-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post("/integrations/instagram/webhook", content=b'{"entry":[]}')

    assert response.status_code == 503
    assert response.json()["detail"] == "INSTAGRAM_APP_SECRET is required"


def test_instagram_webhook_accepts_signed_empty_payload(
    tmp_path: Path,
    api_client_builder,
):
    body = b'{"entry":[]}'
    secret = "secret"
    settings = api_client_builder.settings(
        tmp_path,
        instagram_app_secret=secret,
        instagram_access_token="token",
        instagram_ig_user_id="ig-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/instagram/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_instagram_webhook_requires_ig_access_token(
    tmp_path: Path,
    api_client_builder,
):
    body = b'{"entry":[]}'
    secret = "secret"
    settings = api_client_builder.settings(tmp_path, instagram_app_secret=secret)
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/instagram/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "INSTAGRAM_ACCESS_TOKEN is required"


def test_instagram_webhook_rejects_stale_message_timestamp(
    tmp_path: Path,
    api_client_builder,
):
    secret = "secret"
    payload = {
        "entry": [
            {
                "id": "ig-1",
                "messaging": [
                    {
                        "sender": {"id": "ig-user-1"},
                        "recipient": {"id": "ig-1"},
                        "timestamp": int((time.time() - 3600) * 1000),
                        "message": {"mid": "m_1", "text": "hello"},
                    }
                ],
            }
        ]
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    settings = api_client_builder.settings(
        tmp_path,
        instagram_app_secret=secret,
        instagram_access_token="token",
        instagram_ig_user_id="ig-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/instagram/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Stale webhook event"


def test_instagram_router_seen_cache_is_shared_event_cache() -> None:
    # Slice 22 F2: the module-level webhook dedupe singleton is the shared
    # bot_helpers.SeenEventCache (was a per-bot _SeenMessageCache copy), so the
    # webhook routers share one TTL-cache implementation instead of per-bot copies.
    from nymeria.api.routers.instagram_bot import _INSTAGRAM_SEEN_CACHE
    from nymeria.triggers.bot_helpers import SeenEventCache

    assert isinstance(_INSTAGRAM_SEEN_CACHE, SeenEventCache)
