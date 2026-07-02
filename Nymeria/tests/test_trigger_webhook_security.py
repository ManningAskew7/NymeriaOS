from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from nymeria.core.accounts import AccountsRepo
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.triggers import trigger_api as trigger_api_module
from nymeria.triggers.sources.webhook_source import WebhookSource
from nymeria.triggers.webhook_security import (
    reject_stale_messages,
    require_configured_secret,
    verify_meta_signature,
)


def _meta_signature(raw_body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _StampedMessage:
    """Minimal inbound-message stand-in carrying just a `.timestamp`."""

    def __init__(self, timestamp: object) -> None:
        self.timestamp = timestamp


def _stamped(timestamp: object) -> _StampedMessage:
    return _StampedMessage(timestamp)


def test_require_configured_secret_returns_stripped_value() -> None:
    assert require_configured_secret("  s3cret  ", "WHATSAPP_APP_SECRET") == "s3cret"
    assert require_configured_secret("token", "WEBEX_WEBHOOK_SECRET") == "token"


def test_require_configured_secret_raises_503_when_unset() -> None:
    for missing in (None, "", "   "):
        with pytest.raises(HTTPException) as exc_info:
            require_configured_secret(missing, "WHATSAPP_APP_SECRET")
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "WHATSAPP_APP_SECRET is required"


def test_reject_stale_messages_passes_when_all_fresh() -> None:
    now = time.time()
    # No raise expected: both epoch-seconds timestamps are inside the window.
    reject_stale_messages(
        {"ignored": True},
        lambda _payload: [_stamped(now), _stamped(now - 30)],
        unit="seconds",
    )


def test_reject_stale_messages_no_op_when_no_messages() -> None:
    # An empty extraction must never raise (e.g. status/delivery callbacks).
    reject_stale_messages({}, lambda _payload: [], unit="seconds")


def test_reject_stale_messages_raises_403_on_any_stale_message() -> None:
    now = time.time()
    with pytest.raises(HTTPException) as exc_info:
        reject_stale_messages(
            {},
            lambda _payload: [_stamped(now), _stamped(now - 3600)],
            unit="seconds",
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Stale webhook event"


def test_reject_stale_messages_respects_timestamp_unit() -> None:
    now_ms = time.time() * 1000.0
    # The same millisecond value is fresh as milliseconds but reads as a
    # far-future (stale) instant when interpreted as seconds.
    reject_stale_messages({}, lambda _payload: [_stamped(now_ms)], unit="milliseconds")
    with pytest.raises(HTTPException) as exc_info:
        reject_stale_messages({}, lambda _payload: [_stamped(now_ms)], unit="seconds")
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Stale webhook event"


def test_verify_meta_signature_accepts_matching_hmac() -> None:
    body = b'{"object":"page"}'
    secret = "app-secret"
    assert verify_meta_signature(body, _meta_signature(body, secret), secret) is True


def test_verify_meta_signature_rejects_missing_secret_or_header() -> None:
    body = b"payload"
    # No app secret configured -> never trust the request.
    assert verify_meta_signature(body, "sha256=anything", None) is False
    assert verify_meta_signature(body, "sha256=anything", "") is False
    # Missing or non-sha256 header is rejected without raising.
    assert verify_meta_signature(body, None, "secret") is False
    assert verify_meta_signature(body, "sha1=deadbeef", "secret") is False


def test_verify_meta_signature_rejects_tampered_body_or_secret() -> None:
    body = b'{"object":"page"}'
    secret = "app-secret"
    good = _meta_signature(body, secret)
    # A signature computed for a different body or a different secret fails.
    assert verify_meta_signature(b'{"object":"instagram"}', good, secret) is False
    assert verify_meta_signature(body, _meta_signature(body, "other-secret"), secret) is False
    assert verify_meta_signature(body, "sha256=bad", secret) is False


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
    name: str = "Webhook",
    source_config: dict | None = None,
    action: TriggerAction | None = None,
) -> None:
    manager = TriggerManager(data_dir)
    trigger = TriggerDefinition(
        id=trigger_id,
        name=name,
        source_type="webhook",
        source_config=source_config or {},
        action=action
        or TriggerAction(
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


def test_public_webhook_fire_derives_owner_from_matching_secret(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    client, _token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(
        tmp_path,
        user_id="owner",
        trigger_id="sharedid",
        name="Owner webhook",
        source_config={"secret": "owner-secret"},
    )
    _insert_webhook_trigger(
        tmp_path,
        user_id="attacker",
        trigger_id="sharedid",
        name="Attacker webhook",
        source_config={"secret": "attacker-secret"},
    )

    response = client.post(
        "/triggers/fire/sharedid?user_id=attacker&secret=owner-secret",
        json={"message": "hello"},
    )

    manager = TriggerManager(tmp_path)
    assert response.status_code == 200
    owner_trigger = manager.get_trigger("owner", "sharedid")
    attacker_trigger = manager.get_trigger("attacker", "sharedid")
    assert owner_trigger is not None
    assert attacker_trigger is not None
    assert owner_trigger.fire_count == 1
    assert attacker_trigger.fire_count == 0


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


def test_webhook_fire_routes_non_agent_actions_through_fire_action(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    """Phase 4 routing fix: a notify/create_todo/run_workflow webhook fire
    goes through ``fire_action``'s type dispatch, not the /chat relay."""
    import threading

    client, _token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(
        tmp_path,
        source_config={"secret": "shared"},
        action=TriggerAction(
            type="run_workflow", config={"workflow_id": "wf_demo"}
        ),
    )

    fired = threading.Event()
    fire_calls: list[tuple[str, str, str]] = []

    def fake_fire_action(self, trigger, event, executor, user_id):
        fire_calls.append((trigger.id, trigger.action.type, user_id))
        fired.set()

    monkeypatch.setattr(TriggerManager, "fire_action", fake_fire_action)
    dispatch_calls: list[dict] = []
    monkeypatch.setattr(
        trigger_api_module,
        "_dispatch_trigger_fire",
        lambda **kw: dispatch_calls.append(kw),
    )

    response = client.post(
        "/triggers/fire/webhook1?user_id=owner&secret=shared",
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "fired"
    assert response.json()["action_type"] == "run_workflow"
    assert fired.wait(5), "fire_action was never dispatched"
    assert fire_calls == [("webhook1", "run_workflow", "owner")]
    assert dispatch_calls == []


def test_webhook_fire_agent_prompt_still_relays_to_chat(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    import threading

    client, _token = _client(tmp_path, api_client_builder, monkeypatch)
    _insert_webhook_trigger(tmp_path, source_config={"secret": "shared"})

    dispatched = threading.Event()
    dispatch_calls: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatch_calls.append(kwargs)
        dispatched.set()

    monkeypatch.setattr(trigger_api_module, "_dispatch_trigger_fire", fake_dispatch)
    fire_calls: list = []
    monkeypatch.setattr(
        TriggerManager,
        "fire_action",
        lambda self, *a, **kw: fire_calls.append(a),
    )

    response = client.post(
        "/triggers/fire/webhook1?user_id=owner&secret=shared",
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert dispatched.wait(5), "_dispatch_trigger_fire was never dispatched"
    assert dispatch_calls and dispatch_calls[0]["action_type"] == "agent_prompt"
    assert fire_calls == []
