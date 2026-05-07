from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nymeria.core.accounts import AccountsRepo
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.triggers import trigger_api as trigger_api_module
from nymeria.triggers.sources.webhook_source import WebhookSource


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = SimpleNamespace(
            upsert_thread=lambda *args, **kwargs: None,
            delete_thread=lambda *args, **kwargs: None,
        )

    def sync_agent_tools(self):
        pass


def _client(tmp_path: Path, api_client_builder, monkeypatch):
    settings = api_client_builder.settings(tmp_path)
    settings.nymeria_service_token = None
    settings.api_port = 8000
    monkeypatch.setattr(trigger_api_module, "get_settings", lambda: settings)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner")
    token = agent.accounts_repo.issue_token("owner")
    return client, token


def _insert_webhook_trigger(
    data_dir: Path,
    *,
    user_id: str = "owner",
    trigger_id: str = "webhook1",
    source_config: dict | None = None,
) -> None:
    manager = TriggerManager(data_dir)
    trigger = TriggerDefinition(
        id=trigger_id,
        name="Webhook",
        source_type="webhook",
        source_config=source_config or {},
        action=TriggerAction(
            type="agent_prompt",
            config={"prompt_template": "Webhook payload: {message}"},
        ),
        thread_id=f"trigger-{trigger_id}",
        created_by="user",
        state={"trigger_id": trigger_id},
    )
    with manager.atomic_update(user_id) as store:
        store.triggers.append(trigger)


def test_webhook_source_requires_non_empty_secret():
    source = WebhookSource()

    assert source.validate_config({}) == (False, "Missing required field: secret")
    assert source.validate_config({"secret": ""}) == (
        False,
        "Field 'secret' must be a non-empty string",
    )
    assert source.validate_config({"secret": "shared"}) == (True, "ok")

    assert source.validate_secret({}, None) is False
    assert source.validate_secret({"secret": "shared"}, None) is False
    assert source.validate_secret({"secret": "shared"}, "wrong") is False
    assert source.validate_secret({"secret": "shared"}, "shared") is True


def test_public_webhook_fire_requires_matching_shared_secret(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, _token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(tmp_path, source_config={"secret": "shared"})

    missing = client.post(
        "/triggers/fire/webhook1?user_id=owner",
        json={"message": "hello"},
    )
    wrong = client.post(
        "/triggers/fire/webhook1?user_id=owner&secret=wrong",
        json={"message": "hello"},
    )
    valid = client.post(
        "/triggers/fire/webhook1?user_id=owner&secret=shared",
        json={"message": "hello"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert valid.status_code == 200
    assert valid.json()["status"] == "fired"


def test_invalid_bearer_token_does_not_fall_back_to_shared_secret(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, _token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(tmp_path, source_config={"secret": "shared"})

    response = client.post(
        "/triggers/fire/webhook1?user_id=owner&secret=shared",
        headers={"Authorization": "Bearer wrong"},
        json={"message": "hello"},
    )

    assert response.status_code == 401


def test_secretless_webhook_trigger_is_not_publicly_fireable(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, _token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(tmp_path, source_config={})

    response = client.post(
        "/triggers/fire/webhook1?user_id=owner",
        json={"message": "hello"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid or missing webhook secret"


def test_authenticated_webhook_fire_uses_token_user_without_url_secret(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(tmp_path, source_config={})

    response = client.post(
        "/triggers/fire/webhook1?user_id=attacker",
        headers=api_client_builder.auth(token),
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert response.json()["trigger_id"] == "webhook1"


def test_webhook_trigger_creation_requires_secret(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, token = _client(tmp_path, api_client_builder, monkeypatch)

    response = client.post(
        "/triggers",
        headers=api_client_builder.auth(token),
        json={
            "name": "No secret webhook",
            "source_type": "webhook",
            "source_config": {},
            "action_type": "agent_prompt",
            "action_config": {"prompt_template": "Webhook payload: {message}"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Failed to create trigger. Check source_type and config."
    )
