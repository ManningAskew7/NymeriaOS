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


def test_messenger_webhook_verification_challenge(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        messenger_webhook_verify_token="verify-me",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    accepted = client.get(
        "/integrations/messenger/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-value",
        },
    )
    rejected = client.get(
        "/integrations/messenger/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "challenge-value",
        },
    )

    assert accepted.status_code == 200
    assert accepted.text == "challenge-value"
    assert rejected.status_code == 403


def test_messenger_webhook_rejects_bad_signature(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        messenger_app_secret="secret",
        messenger_page_access_token="token",
        messenger_page_id="page-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/messenger/webhook",
        content=b'{"entry":[]}',
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid webhook signature"


def test_messenger_webhook_requires_app_secret(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        messenger_page_access_token="token",
        messenger_page_id="page-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post("/integrations/messenger/webhook", content=b'{"entry":[]}')

    assert response.status_code == 503
    assert response.json()["detail"] == "MESSENGER_APP_SECRET is required"


def test_messenger_webhook_accepts_signed_empty_payload(
    tmp_path: Path,
    api_client_builder,
):
    body = b'{"entry":[]}'
    secret = "secret"
    settings = api_client_builder.settings(
        tmp_path,
        messenger_app_secret=secret,
        messenger_page_access_token="token",
        messenger_page_id="page-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/messenger/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_messenger_webhook_requires_page_access_token(
    tmp_path: Path,
    api_client_builder,
):
    body = b'{"entry":[]}'
    secret = "secret"
    settings = api_client_builder.settings(tmp_path, messenger_app_secret=secret)
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/messenger/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "MESSENGER_PAGE_ACCESS_TOKEN is required"


def test_messenger_webhook_rejects_stale_message_timestamp(
    tmp_path: Path,
    api_client_builder,
):
    secret = "secret"
    payload = {
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "recipient": {"id": "page-1"},
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
        messenger_app_secret=secret,
        messenger_page_access_token="token",
        messenger_page_id="page-1",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/messenger/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret)},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Stale webhook event"
