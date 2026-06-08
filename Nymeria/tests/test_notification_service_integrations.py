import json

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_pushbullet_send_push_uses_env_token(monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PUSHBULLET_ACCESS_TOKEN", "pb-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"iden": "push-1", "type": "link"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pushbullet_send_push.func(
            title="Docs",
            body="Read this",
            url="https://example.com",
            push_type="link",
            target_kind="email",
            target_value="alice@example.com",
        )
    )

    assert result["iden"] == "push-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.pushbullet.com/v2/pushes"
    assert captured["headers"]["Access-Token"] == "pb-token"
    assert captured["json_body"]["type"] == "link"
    assert captured["json_body"]["title"] == "Docs"
    assert captured["json_body"]["body"] == "Read this"
    assert captured["json_body"]["url"] == "https://example.com"
    assert captured["json_body"]["email"] == "alice@example.com"


def test_pushbullet_list_pushes_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Pushbullet",
        provider="pushbullet",
        kind="api_key",
        allowed_targets=["native_tool:pushbullet_list_pushes"],
        secret_fields={"access_token": "pb-vault", "base_url": "https://pushbullet.example/v2"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"pushes": [{"iden": "push-1"}], "cursor": "next"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pushbullet_list_pushes.func(
            limit=5,
            modified_after="1000",
            cursor="cursor-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["pushes"] == [{"iden": "push-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://pushbullet.example/v2/pushes"
    assert captured["params"]["limit"] == 5
    assert captured["params"]["modified_after"] == "1000"
    assert captured["params"]["cursor"] == "cursor-1"
    assert captured["headers"]["Access-Token"] == "pb-vault"


def test_pushcut_send_notification_uses_env_key(monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PUSHCUT_API_KEY", "pushcut-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pushcut_send_notification.func(
            notification_name="Door Alert",
            title="Door",
            text="Front door opened",
            input_text="front",
            device_names="iphone,ipad",
            fields_json='{"threadId":"door"}',
        )
    )

    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.pushcut.io/v1/notifications/Door%20Alert"
    assert captured["headers"]["API-Key"] == "pushcut-key"
    assert captured["json_body"]["title"] == "Door"
    assert captured["json_body"]["text"] == "Front door opened"
    assert captured["json_body"]["input"] == "front"
    assert captured["json_body"]["devices"] == ["iphone", "ipad"]
    assert captured["json_body"]["threadId"] == "door"


def test_gotify_send_message_uses_env_app_token(monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("GOTIFY_BASE_URL", "https://gotify.example")
    monkeypatch.setenv("GOTIFY_APP_TOKEN", "app-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": 1, "message": "hello"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.gotify_send_message.func(
            message="hello",
            title="Greeting",
            priority=8,
            extras_json='{"client::display":{"contentType":"text/markdown"}}',
        )
    )

    assert result["id"] == 1
    assert captured["method"] == "POST"
    assert captured["url"] == "https://gotify.example/message"
    assert captured["headers"]["X-Gotify-Key"] == "app-token"
    assert captured["json_body"]["message"] == "hello"
    assert captured["json_body"]["title"] == "Greeting"
    assert captured["json_body"]["priority"] == 8
    assert captured["json_body"]["extras"]["client::display"]["contentType"] == "text/markdown"


def test_gotify_list_messages_uses_vault_client_token(tmp_path, monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Gotify",
        provider="gotify",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "base_url": "https://gotify.example",
            "client_token": "client-token",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"messages": [{"id": 1}], "paging": {"limit": 2}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.gotify_list_messages.func(
            limit=2,
            since=10,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["messages"] == [{"id": 1}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://gotify.example/message"
    assert captured["params"]["limit"] == 2
    assert captured["params"]["since"] == 10
    assert captured["headers"]["X-Gotify-Key"] == "client-token"


def test_pushover_send_message_uses_env_token_and_user(monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "po-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"status": 1, "request": "req-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pushover_send_message.func(
            message="wake up",
            title="Alert",
            priority=2,
            retry_seconds=60,
            expire_seconds=600,
            device="phone",
            url="https://example.com",
            url_title="Open",
        )
    )

    assert result["status"] == 1
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.pushover.net/1/messages.json"
    assert captured["form_data"]["token"] == "po-token"
    assert captured["form_data"]["user"] == "user-key"
    assert captured["form_data"]["message"] == "wake up"
    assert captured["form_data"]["priority"] == 2
    assert captured["form_data"]["retry"] == 60
    assert captured["form_data"]["expire"] == 600
    assert captured["form_data"]["device"] == "phone"
    assert captured["form_data"]["url"] == "https://example.com"
    assert captured["form_data"]["url_title"] == "Open"


def test_signl4_send_alert_uses_vault_team_secret(tmp_path, monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="SIGNL4",
        provider="signl4",
        kind="api_key",
        allowed_targets=["native_tool:signl4_send_alert"],
        secret_fields={"team_secret": "team-secret", "base_url": "https://connect.example/webhook"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.signl4_send_alert.func(
            message="CPU high",
            title="Production",
            external_id="INC-1",
            service="API",
            location="40.0,-74.0",
            alerting_scenario="multi_ack",
            filtering=True,
            fields_json='{"Host":"api-1"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://connect.example/webhook/team-secret"
    assert captured["json_body"]["Message"] == "CPU high"
    assert captured["json_body"]["Title"] == "Production"
    assert captured["json_body"]["X-S4-ExternalID"] == "INC-1"
    assert captured["json_body"]["X-S4-Service"] == "API"
    assert captured["json_body"]["X-S4-Location"] == "40.0,-74.0"
    assert captured["json_body"]["X-S4-AlertingScenario"] == "multi_ack"
    assert captured["json_body"]["X-S4-Filtering"] == "true"
    assert captured["json_body"]["Host"] == "api-1"


def test_notification_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import notification_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("GOTIFY_BASE_URL", "https://gotify.example")
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "po-token")

    pushbullet_result = tools.pushbullet_list_pushes.func()
    pushcut_result = tools.pushcut_send_notification.func(notification_name="Door")
    gotify_result = tools.gotify_send_message.func(message="hello")
    pushover_result = tools.pushover_send_message.func(message="hello")
    signl4_result = tools.signl4_send_alert.func(message="hello")

    assert 'provider "pushbullet"' in pushbullet_result
    assert "PUSHBULLET_ACCESS_TOKEN" in pushbullet_result
    assert 'allowed target "native_tool:pushbullet_list_pushes"' in pushbullet_result
    assert 'provider "pushcut"' in pushcut_result
    assert "PUSHCUT_API_KEY" in pushcut_result
    assert 'provider "gotify"' in gotify_result
    assert "GOTIFY_APP_TOKEN" in gotify_result
    assert 'allowed target "native_tool:gotify_send_message"' in gotify_result
    assert "PUSHOVER_USER_KEY" in pushover_result
    assert 'provider "signl4"' in signl4_result
    assert "SIGNL4_TEAM_SECRET or SIGNL4_WEBHOOK_URL" in signl4_result


def test_notification_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = ["pushbullet_list_pushes", "gotify_list_messages"]
    moderate_names = [
        "pushbullet_send_push",
        "pushbullet_update_push",
        "pushbullet_delete_push",
        "pushcut_send_notification",
        "gotify_send_message",
        "gotify_delete_message",
        "pushover_send_message",
        "signl4_send_alert",
        "signl4_resolve_alert",
    ]

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_notification_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        gotify_send_message,
        pushbullet_send_push,
        pushcut_send_notification,
        signl4_send_alert,
    )

    assert "config" not in pushbullet_send_push.args_schema.model_json_schema()["properties"]
    assert "config" not in pushcut_send_notification.args_schema.model_json_schema()["properties"]
    assert "config" not in gotify_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in signl4_send_alert.args_schema.model_json_schema()["properties"]
