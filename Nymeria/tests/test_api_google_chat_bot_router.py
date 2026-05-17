from __future__ import annotations

from pathlib import Path


class FakeAgent:
    def __init__(self) -> None:
        self.synced_tools = 0

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1


def test_google_chat_webhook_rejects_invalid_json(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        google_chat_service_account_json='{"type":"service_account"}',
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/google-chat/webhook",
        content=b"{bad json",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON payload"


def test_google_chat_webhook_requires_credentials(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(tmp_path)
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/google-chat/webhook",
        json={"type": "MESSAGE"},
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON or GOOGLE_CHAT_SERVICE_ACCOUNT_FILE is required"
    )


def test_google_chat_webhook_rejects_missing_bearer_token(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        google_chat_service_account_json='{"type":"service_account"}',
        google_chat_validate_auth=True,
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/google-chat/webhook",
        json={"type": "MESSAGE"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing Google Chat bearer token"


def test_google_chat_webhook_ignores_disabled_auth_toggle(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        google_chat_service_account_json='{"type":"service_account"}',
        google_chat_validate_auth=False,
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/google-chat/webhook",
        json={"type": "ADDED_TO_SPACE"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing Google Chat bearer token"
