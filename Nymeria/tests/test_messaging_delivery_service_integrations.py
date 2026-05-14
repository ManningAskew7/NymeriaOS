import base64
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


def test_twilio_send_message_uses_env_basic_auth_and_form(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "form_data": form_data,
                "headers": headers,
            }
        )
        return {"sid": "SM123", "status": "queued"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.twilio_send_message.func(
            from_number="+15550000001",
            to_number="+15550000002",
            body="Hello",
            status_callback="https://example.com/status",
        )
    )

    expected_auth = base64.b64encode(b"AC123:twilio-token").decode()
    assert result["sid"] == "SM123"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["form_data"]["From"] == "+15550000001"
    assert captured["form_data"]["To"] == "+15550000002"
    assert captured["form_data"]["Body"] == "Hello"
    assert captured["form_data"]["StatusCallback"] == "https://example.com/status"


def test_twilio_list_messages_uses_vault_api_key_sid(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Twilio",
        provider="twilio",
        kind="api_key",
        allowed_targets=["native_tool:twilio_list_messages"],
        secret_fields={
            "account_sid": "AC123",
            "api_key_sid": "SK123",
            "api_key_secret": "twilio-secret",
            "base_url": "https://twilio.example/2010-04-01",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"messages": [{"sid": "SM1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.twilio_list_messages.func(
            to_number="+15550000002",
            page_size=5,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_auth = base64.b64encode(b"SK123:twilio-secret").decode()
    assert result == [{"sid": "SM1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://twilio.example/2010-04-01/Accounts/AC123/Messages.json"
    assert captured["params"]["To"] == "+15550000002"
    assert captured["params"]["PageSize"] == 5
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_sendgrid_send_email_uses_env_key(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SENDGRID_API_KEY", "sendgrid-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok", "status_code": 202}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.sendgrid_send_email.func(
            from_email="sender@example.com",
            to_emails="alice@example.com,bob@example.com",
            subject="Hello",
            text="Plain text",
            html="<p>HTML</p>",
            from_name="Sender",
            dynamic_template_data_json='{"name":"Alice"}',
        )
    )

    assert result["status_code"] == 202
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert captured["headers"]["Authorization"] == "Bearer sendgrid-key"
    assert captured["json_body"]["from"] == {"email": "sender@example.com", "name": "Sender"}
    assert captured["json_body"]["personalizations"][0]["to"] == [
        {"email": "alice@example.com"},
        {"email": "bob@example.com"},
    ]
    assert captured["json_body"]["personalizations"][0]["dynamic_template_data"] == {"name": "Alice"}
    assert captured["json_body"]["content"] == [
        {"type": "text/plain", "value": "Plain text"},
        {"type": "text/html", "value": "<p>HTML</p>"},
    ]


def test_sendgrid_upsert_contacts_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="SendGrid",
        provider="sendgrid",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "sendgrid-key",
            "base_url": "https://sendgrid.example/v3",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"job_id": "job-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.sendgrid_upsert_contacts.func(
            contacts_json='[{"email":"alice@example.com","first_name":"Alice"}]',
            list_ids="list-1,list-2",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["job_id"] == "job-1"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://sendgrid.example/v3/marketing/contacts"
    assert captured["headers"]["Authorization"] == "Bearer sendgrid-key"
    assert captured["json_body"]["contacts"] == [{"email": "alice@example.com", "first_name": "Alice"}]
    assert captured["json_body"]["list_ids"] == ["list-1", "list-2"]


def test_mailgun_send_email_uses_env_key_and_domain(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAILGUN_API_KEY", "mailgun-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"id": "<message-id>", "message": "Queued"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailgun_send_email.func(
            from_email="Sender <sender@example.com>",
            to_emails="alice@example.com",
            subject="Hello",
            text="Plain text",
            cc="cc@example.com",
            extra_fields_json='{"o:tag":"welcome"}',
        )
    )

    expected_auth = base64.b64encode(b"api:mailgun-key").decode()
    assert result["message"] == "Queued"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mailgun.net/v3/mg.example.com/messages"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["form_data"]["from"] == "Sender <sender@example.com>"
    assert captured["form_data"]["to"] == "alice@example.com"
    assert captured["form_data"]["cc"] == "cc@example.com"
    assert captured["form_data"]["o:tag"] == "welcome"


def test_mailgun_list_events_uses_vault(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Mailgun",
        provider="mailgun",
        kind="api_key",
        allowed_targets=["native_tool:mailgun_list_events"],
        secret_fields={
            "api_key": "mailgun-key",
            "domain": "mg.example.com",
            "base_url": "https://mailgun.example/v3",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"items": [{"event": "delivered"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailgun_list_events.func(
            event="delivered",
            ascending=True,
            limit=10,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_auth = base64.b64encode(b"api:mailgun-key").decode()
    assert result == [{"event": "delivered"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://mailgun.example/v3/mg.example.com/events"
    assert captured["params"]["event"] == "delivered"
    assert captured["params"]["ascending"] == "yes"
    assert captured["params"]["limit"] == 10
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_messaging_delivery_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    values = {}
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: values.get(name))

    twilio_account = tools.twilio_list_messages.func()
    values["twilio_account_sid"] = "AC123"
    twilio_token = tools.twilio_get_message.func("SM123")
    sendgrid = tools.sendgrid_list_lists.func()
    mailgun_domain = tools.mailgun_get_domain.func()
    values["mailgun_domain"] = "mg.example.com"
    mailgun_key = tools.mailgun_get_domain.func()

    assert "No Twilio account SID found" in twilio_account
    assert "TWILIO_ACCOUNT_SID" in twilio_account
    assert "No Twilio credential found" in twilio_token
    assert "TWILIO_AUTH_TOKEN" in twilio_token
    assert "native_tool:twilio_get_message" in twilio_token
    assert "No SendGrid credential found" in sendgrid
    assert "SENDGRID_API_KEY" in sendgrid
    assert "native_tool:sendgrid_list_lists" in sendgrid
    assert "No Mailgun domain found" in mailgun_domain
    assert "MAILGUN_DOMAIN" in mailgun_domain
    assert "No Mailgun credential found" in mailgun_key
    assert "MAILGUN_API_KEY" in mailgun_key
    assert "native_tool:mailgun_get_domain" in mailgun_key


def test_messaging_delivery_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "twilio_list_messages",
        "twilio_get_message",
        "sendgrid_list_contacts",
        "sendgrid_get_contact",
        "sendgrid_list_lists",
        "mailgun_list_events",
        "mailgun_get_domain",
    }
    moderate_names = {
        "twilio_send_message",
        "twilio_make_call",
        "sendgrid_send_email",
        "sendgrid_upsert_contacts",
        "mailgun_send_email",
    }

    for name in safe_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_messaging_delivery_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.messaging_delivery_service_integrations import (
        mailgun_send_email,
        sendgrid_send_email,
        twilio_send_message,
    )

    assert "config" not in twilio_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in sendgrid_send_email.args_schema.model_json_schema()["properties"]
    assert "config" not in mailgun_send_email.args_schema.model_json_schema()["properties"]
