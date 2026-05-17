from __future__ import annotations

from pathlib import Path


class FakeAgent:
    def __init__(self) -> None:
        self.synced_tools = 0

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1


def test_teams_webhook_rejects_invalid_json(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        teams_bot_app_id="app-id",
        teams_bot_app_password="secret",
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/teams/webhook",
        content=b"{bad json",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON payload"


def test_teams_webhook_requires_credentials(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(tmp_path)
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/teams/webhook",
        json={"type": "message", "serviceUrl": "https://smba.trafficmanager.net/amer/"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "TEAMS_BOT_APP_ID and TEAMS_BOT_APP_PASSWORD are required"


def test_teams_webhook_rejects_missing_bot_framework_bearer_token(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        teams_bot_app_id="app-id",
        teams_bot_app_password="secret",
        teams_bot_validate_auth=True,
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/teams/webhook",
        json={"type": "message", "serviceUrl": "https://smba.trafficmanager.net/amer/"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing Bot Framework bearer token"


def test_teams_webhook_accepts_non_message_activity_when_auth_disabled(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        teams_bot_app_id="app-id",
        teams_bot_app_password="secret",
        teams_bot_validate_auth=False,
    )
    client = api_client_builder.client(FakeAgent(), settings)

    response = client.post(
        "/integrations/teams/webhook",
        json={"type": "conversationUpdate", "serviceUrl": "https://smba.trafficmanager.net/amer/"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
