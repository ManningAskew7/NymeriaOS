from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path


class FakeAgent:
    def __init__(self) -> None:
        self.synced_tools = 0

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1


def _sign(raw_body: bytes, secret: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("ascii")


def test_line_webhook_rejects_invalid_json(
    tmp_path: Path,
    api_client_builder,
):
    raw = b"{bad json"
    settings = api_client_builder.settings(
        tmp_path,
        line_channel_access_token="token",
        line_channel_secret="secret",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/line/webhook",
        content=raw,
        headers={"x-line-signature": _sign(raw, "secret")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON payload"


def test_line_webhook_requires_channel_access_token(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(tmp_path)
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/line/webhook",
        json={"destination": "Ubot", "events": []},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "LINE_CHANNEL_ACCESS_TOKEN is required"


def test_line_webhook_rejects_missing_signature(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        line_channel_access_token="token",
        line_channel_secret="secret",
        line_validate_signature=True,
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/line/webhook",
        json={"destination": "Ubot", "events": []},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing LINE signature"


def test_line_webhook_rejects_invalid_signature(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        line_channel_access_token="token",
        line_channel_secret="secret",
        line_validate_signature=True,
    )
    client = api_client_builder.client(FakeAgent(), settings)
    raw = json.dumps({"destination": "Ubot", "events": []}).encode("utf-8")

    response = client.post(
        "/integrations/line/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-line-signature": "not-valid",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid LINE signature"


def test_line_webhook_accepts_empty_events_with_valid_signature(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        line_channel_access_token="token",
        line_channel_secret="secret",
        line_validate_signature=True,
    )
    client = api_client_builder.client(FakeAgent(), settings)
    raw = json.dumps({"destination": "Ubot", "events": []}).encode("utf-8")

    response = client.post(
        "/integrations/line/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-line-signature": _sign(raw, "secret"),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_line_webhook_ignores_disabled_signature_toggle(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        line_channel_access_token="token",
        line_channel_secret="secret",
        line_validate_signature=False,
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/line/webhook",
        json={"destination": "Ubot", "events": []},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing LINE signature"
