import base64
import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


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


def test_freshservice_create_ticket_uses_env_key_and_domain(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("FRESHSERVICE_API_KEY", "freshservice-key")
    monkeypatch.setenv("FRESHSERVICE_DOMAIN", "itdesk")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"ticket": {"id": 100, **json_body}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.freshservice_create_ticket.func(
            subject="VPN down",
            description="Cannot connect.",
            email="ada@example.com",
            priority=2,
            status=2,
            urgency=2,
            impact=1,
            category="Access",
            custom_fields_json='{"asset_id": "laptop-1"}',
        )
    )

    expected_auth = base64.b64encode(b"freshservice-key:X").decode()
    assert result["ticket"]["id"] == 100
    assert captured["method"] == "POST"
    assert captured["url"] == "https://itdesk.freshservice.com/api/v2/tickets"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["json_body"]["email"] == "ada@example.com"
    assert captured["json_body"]["custom_fields"] == {"asset_id": "laptop-1"}


def test_servicenow_table_tools_shape_requests(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SERVICENOW_BASE_URL", "https://snow.example.com")
    monkeypatch.setenv("SERVICENOW_ACCESS_TOKEN", "snow-token")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
            }
        )
        if method == "GET":
            return {"result": [{"sys_id": "abc", "short_description": "Broken"}]}
        return {"result": {"sys_id": "abc", **(json_body or {})}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    listed = json.loads(
        tools.servicenow_list_records.func(
            table="incident",
            query="active=true",
            fields="sys_id,short_description",
            limit=10,
            display_value=True,
        )
    )
    updated = json.loads(
        tools.servicenow_update_record.func(
            table="incident",
            sys_id="abc",
            fields_json='{"short_description": "Fixed"}',
        )
    )

    assert listed[0]["sys_id"] == "abc"
    assert updated["short_description"] == "Fixed"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://snow.example.com/api/now/table/incident"
    assert calls[0]["params"]["sysparm_query"] == "active=true"
    assert calls[0]["params"]["sysparm_display_value"] == "true"
    assert calls[0]["headers"]["Authorization"] == "Bearer snow-token"
    assert calls[1]["method"] == "PATCH"
    assert calls[1]["url"] == "https://snow.example.com/api/now/table/incident/abc"


def test_zammad_create_record_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Zammad",
        provider="zammad",
        kind="api_key",
        allowed_targets=["native_tool:zammad_create_record"],
        secret_fields={"token": "zammad-token", "base_url": "https://zammad.example.com"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": 7, **json_body}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.zammad_create_record.func(
            resource="ticket",
            fields_json='{"title": "Printer", "group": "Users"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == 7
    assert captured["method"] == "POST"
    assert captured["url"] == "https://zammad.example.com/api/v1/tickets"
    assert captured["headers"]["Authorization"] == "Token token=zammad-token"
    assert captured["json_body"] == {"title": "Printer", "group": "Users"}


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


def test_drift_create_contact_uses_env_token(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("DRIFT_ACCESS_TOKEN", "drift-token")
    monkeypatch.setenv("DRIFT_BASE_URL", "https://drift.example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"data": {"id": "contact-1", "attributes": json_body["attributes"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.drift_create_contact.func(
            email="alice@example.com",
            name="Alice",
            phone="+15551234567",
            attributes_json='{"company": "Example"}',
        )
    )

    assert result["id"] == "contact-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://drift.example/contacts"
    assert captured["headers"]["Authorization"] == "Bearer drift-token"
    assert captured["json_body"]["attributes"] == {
        "email": "alice@example.com",
        "name": "Alice",
        "phone": "+15551234567",
        "company": "Example",
    }


def test_drift_list_contact_attributes_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Drift",
        provider="drift",
        kind="api_key",
        allowed_targets=["native_tool:drift_list_contact_attributes"],
        secret_fields={"accessToken": "drift-token", "base_url": "https://drift.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "headers": headers})
        return {"data": {"properties": [{"name": "company"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.drift_list_contact_attributes.func(config={"configurable": {"user_id": "alice"}})
    )

    assert result == [{"name": "company"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://drift.example/contacts/attributes"
    assert captured["headers"]["Authorization"] == "Bearer drift-token"


def test_support_service_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import support_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    freshdesk_base = tools.freshdesk_get_ticket.func("1")
    freshservice_base = tools.freshservice_get_ticket.func("1")
    servicenow_base = tools.servicenow_get_record.func("incident", "abc")
    zammad_base = tools.zammad_get_record.func("ticket", "1")
    helpscout = tools.helpscout_get_customer.func("1")
    intercom = tools.intercom_get_contact.func("1")
    drift = tools.drift_get_contact.func("1")

    assert "No Freshdesk base URL found" in freshdesk_base
    assert "FRESHDESK_DOMAIN" in freshdesk_base
    assert "No Freshservice base URL found" in freshservice_base
    assert "FRESHSERVICE_DOMAIN" in freshservice_base
    assert "No ServiceNow base URL found" in servicenow_base
    assert "SERVICENOW_INSTANCE" in servicenow_base
    assert "No Zammad base URL found" in zammad_base
    assert "ZAMMAD_BASE_URL" in zammad_base
    assert "No Help Scout credential found" in helpscout
    assert "HELPSCOUT_ACCESS_TOKEN" in helpscout
    assert "native_tool:helpscout_get_customer" in helpscout
    assert "No Intercom credential found" in intercom
    assert "INTERCOM_ACCESS_TOKEN" in intercom
    assert "native_tool:intercom_get_contact" in intercom
    assert "No Drift credential found" in drift
    assert "DRIFT_ACCESS_TOKEN" in drift
    assert "native_tool:drift_get_contact" in drift


def test_support_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "freshdesk_list_tickets",
        "freshdesk_search_tickets",
        "freshdesk_get_ticket",
        "freshdesk_list_contacts",
        "freshdesk_get_contact",
        "freshservice_list_tickets",
        "freshservice_get_ticket",
        "freshservice_list_requesters",
        "freshservice_get_requester",
        "servicenow_list_records",
        "servicenow_get_record",
        "zammad_list_records",
        "zammad_get_record",
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
        "drift_get_contact",
        "drift_list_contact_attributes",
    }
    moderate_names = {
        "freshdesk_create_ticket",
        "freshdesk_update_ticket",
        "freshdesk_delete_ticket",
        "freshdesk_create_contact",
        "freshdesk_update_contact",
        "freshservice_create_ticket",
        "freshservice_update_ticket",
        "servicenow_create_record",
        "servicenow_update_record",
        "servicenow_delete_record",
        "zammad_create_record",
        "zammad_update_record",
        "helpscout_create_conversation",
        "helpscout_create_thread",
        "helpscout_create_customer",
        "helpscout_update_customer",
        "intercom_create_contact",
        "intercom_update_contact",
        "intercom_archive_contact",
        "intercom_reply_conversation",
        "drift_create_contact",
        "drift_update_contact",
        "drift_delete_contact",
    }

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


def test_support_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.support_service_integrations import (
        freshdesk_create_ticket,
        freshservice_create_ticket,
        helpscout_create_conversation,
        intercom_reply_conversation,
        drift_create_contact,
        servicenow_update_record,
        zammad_create_record,
    )

    assert "config" not in freshdesk_create_ticket.args_schema.model_json_schema()["properties"]
    assert "config" not in freshservice_create_ticket.args_schema.model_json_schema()["properties"]
    assert "config" not in helpscout_create_conversation.args_schema.model_json_schema()["properties"]
    assert "config" not in intercom_reply_conversation.args_schema.model_json_schema()["properties"]
    assert "config" not in drift_create_contact.args_schema.model_json_schema()["properties"]
    assert "config" not in servicenow_update_record.args_schema.model_json_schema()["properties"]
    assert "config" not in zammad_create_record.args_schema.model_json_schema()["properties"]
