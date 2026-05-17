from __future__ import annotations

import hashlib
import hmac
from pathlib import Path


class FakeAgent:
    def __init__(self) -> None:
        self.synced_tools = 0

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1


def test_webex_webhook_rejects_bad_signature(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        webex_webhook_secret="secret",
        webex_access_token="token",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/webex/webhook",
        content=b'{"resource":"messages"}',
        headers={"X-Spark-Signature": "bad"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid webhook signature"


def test_webex_webhook_requires_access_token(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(tmp_path)
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/webex/webhook",
        json={"resource": "messages"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "WEBEX_ACCESS_TOKEN is required"


def test_webex_webhook_accepts_signed_non_message_payload(
    tmp_path: Path,
    api_client_builder,
):
    body = b'{"resource":"rooms","event":"created","data":{"id":"room-1"}}'
    secret = "secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    settings = api_client_builder.settings(
        tmp_path,
        webex_webhook_secret=secret,
        webex_access_token="token",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/webex/webhook",
        content=body,
        headers={"X-Spark-Signature": digest},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
