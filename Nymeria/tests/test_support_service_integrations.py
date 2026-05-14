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


def test_freshdesk_create_ticket_uses_env_key_and_domain(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("FRESHDESK_API_KEY", "freshdesk-key")
    monkeypatch.setenv("FRESHDESK_DOMAIN", "example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"id": 10, **json_body}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.freshdesk_create_ticket.func(
            subject="Need help",
            description="The thing is broken.",
            email="customer@example.com",
            priority=3,
            status=2,
            tags="vip, broken",
            custom_fields_json='{"cf_region":"us"}',
        )
    )

    expected_auth = base64.b64encode(b"freshdesk-key:X").decode()
    assert result["id"] == 10
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.freshdesk.com/api/v2/tickets"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["json_body"]["email"] == "customer@example.com"
    assert captured["json_body"]["tags"] == ["vip", "broken"]
    assert captured["json_body"]["custom_fields"] == {"cf_region": "us"}


def test_freshdesk_search_tickets_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Freshdesk",
        provider="freshdesk",
        kind="api_key",
        allowed_targets=["native_tool:freshdesk_search_tickets"],
        secret_fields={
            "api_key": "freshdesk-key",
            "base_url": "https://support.example.com/api/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"results": [{"id": 11, "subject": "Open"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.freshdesk_search_tickets.func(
            query="status:2",
            page=2,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result[0]["id"] == 11
    assert captured["method"] == "GET"
    assert captured["url"] == "https://support.example.com/api/v2/search/tickets"
    assert captured["params"]["query"] == "status:2"
    assert captured["params"]["page"] == 2
    assert captured["headers"]["Authorization"].startswith("Basic ")


def test_helpscout_create_conversation_uses_env_token(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("HELPSCOUT_ACCESS_TOKEN", "helpscout-token")
    monkeypatch.setenv("HELPSCOUT_BASE_URL", "https://helpscout.example/v2")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": 100, **json_body}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.helpscout_create_conversation.func(
            mailbox_id="123",
            subject="Question",
            customer_email="customer@example.com",
            thread_text="Can you help?",
            customer_first_name="Alice",
            tags="vip,billing",
        )
    )

    assert result["id"] == 100
    assert captured["method"] == "POST"
    assert captured["url"] == "https://helpscout.example/v2/conversations"
    assert captured["headers"]["Authorization"] == "Bearer helpscout-token"
    assert captured["json_body"]["mailboxId"] == 123
    assert captured["json_body"]["customer"]["email"] == "customer@example.com"
    assert captured["json_body"]["threads"][0]["type"] == "customer"
    assert captured["json_body"]["tags"] == ["vip", "billing"]


def test_helpscout_list_conversations_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Help Scout",
        provider="helpscout",
        kind="oauth_token",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "access_token": "helpscout-token",
            "base_url": "https://helpscout.example/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"_embedded": {"conversations": [{"id": 123}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.helpscout_list_conversations.func(
            mailbox_id="44",
            status="closed",
            embed="threads",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"id": 123}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://helpscout.example/v2/conversations"
    assert captured["params"]["mailbox"] == "44"
    assert captured["params"]["status"] == "closed"
    assert captured["params"]["embed"] == "threads"
    assert captured["headers"]["Authorization"] == "Bearer helpscout-token"


def test_intercom_search_contacts_uses_env_token_and_version(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("INTERCOM_ACCESS_TOKEN", "intercom-token")
    monkeypatch.setenv("INTERCOM_BASE_URL", "https://intercom.example")
    monkeypatch.setenv("INTERCOM_VERSION", "2.11")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"data": [{"id": "abc", "email": "alice@example.com"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.intercom_search_contacts.func(
            query_json='{"field":"email","operator":"=","value":"alice@example.com"}',
            pagination_json='{"per_page":10}',
        )
    )

    assert result == [{"id": "abc", "email": "alice@example.com"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://intercom.example/contacts/search"
    assert captured["headers"]["Authorization"] == "Bearer intercom-token"
    assert captured["headers"]["Intercom-Version"] == "2.11"
    assert captured["json_body"]["query"]["field"] == "email"
    assert captured["json_body"]["pagination"]["per_page"] == 10


def test_intercom_reply_conversation_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Intercom",
        provider="intercom",
        kind="oauth_token",
        allowed_targets=["native_tool:intercom_reply_conversation"],
        secret_fields={
            "access_token": "intercom-token",
            "base_url": "https://intercom.example",
            "intercom_version": "2.10",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"type": "conversation", "id": "conv-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.intercom_reply_conversation.func(
            conversation_id="conv-1",
            admin_id="42",
            body="We are checking this now.",
            message_type="note",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "conv-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://intercom.example/conversations/conv-1/reply"
    assert captured["headers"]["Authorization"] == "Bearer intercom-token"
    assert captured["headers"]["Intercom-Version"] == "2.10"
    assert captured["json_body"]["type"] == "admin"
    assert captured["json_body"]["admin_id"] == "42"
    assert captured["json_body"]["message_type"] == "note"


def test_support_service_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    freshdesk_base = tools.freshdesk_get_ticket.func("1")
    helpscout = tools.helpscout_get_customer.func("1")
    intercom = tools.intercom_get_contact.func("1")

    assert "No Freshdesk base URL found" in freshdesk_base
    assert "FRESHDESK_DOMAIN" in freshdesk_base
    assert "No Help Scout credential found" in helpscout
    assert "HELPSCOUT_ACCESS_TOKEN" in helpscout
    assert "native_tool:helpscout_get_customer" in helpscout
    assert "No Intercom credential found" in intercom
    assert "INTERCOM_ACCESS_TOKEN" in intercom
    assert "native_tool:intercom_get_contact" in intercom


def test_support_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "freshdesk_list_tickets",
        "freshdesk_search_tickets",
        "freshdesk_get_ticket",
        "freshdesk_list_contacts",
        "freshdesk_get_contact",
        "helpscout_list_mailboxes",
        "helpscout_get_mailbox",
        "helpscout_list_conversations",
        "helpscout_get_conversation",
        "helpscout_list_customers",
        "helpscout_get_customer",
        "intercom_list_contacts",
        "intercom_search_contacts",
        "intercom_get_contact",
        "intercom_list_conversations",
        "intercom_get_conversation",
    }
    moderate_names = {
        "freshdesk_create_ticket",
        "freshdesk_update_ticket",
        "freshdesk_delete_ticket",
        "freshdesk_create_contact",
        "freshdesk_update_contact",
        "helpscout_create_conversation",
        "helpscout_create_thread",
        "helpscout_create_customer",
        "helpscout_update_customer",
        "intercom_create_contact",
        "intercom_update_contact",
        "intercom_archive_contact",
        "intercom_reply_conversation",
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


def test_support_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.support_service_integrations import (
        freshdesk_create_ticket,
        helpscout_create_conversation,
        intercom_reply_conversation,
    )

    assert "config" not in freshdesk_create_ticket.args_schema.model_json_schema()["properties"]
    assert "config" not in helpscout_create_conversation.args_schema.model_json_schema()["properties"]
    assert "config" not in intercom_reply_conversation.args_schema.model_json_schema()["properties"]
