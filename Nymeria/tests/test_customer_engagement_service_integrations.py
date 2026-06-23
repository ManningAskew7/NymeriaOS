import base64
import hashlib
import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_hubspot_search_crm_objects_uses_env_token_and_body(monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "hubspot-token")
    monkeypatch.setenv("HUBSPOT_BASE_URL", "https://hubspot.example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"results": [{"id": "1", "properties": {"email": "alice@example.com"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.hubspot_search_crm_objects.func(
            object_type="contact",
            query="alice@example.com",
            filter_groups_json='[{"filters":[{"propertyName":"email","operator":"EQ","value":"alice@example.com"}]}]',
            properties="email, firstname",
            sorts_json='[{"propertyName":"createdate","direction":"DESCENDING"}]',
            limit=5,
            after="cursor-1",
        )
    )

    assert result[0]["id"] == "1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://hubspot.example/crm/v3/objects/contacts/search"
    assert captured["headers"]["Authorization"] == "Bearer hubspot-token"
    assert captured["json_body"]["query"] == "alice@example.com"
    assert captured["json_body"]["properties"] == ["email", "firstname"]
    assert captured["json_body"]["filterGroups"][0]["filters"][0]["propertyName"] == "email"
    assert captured["json_body"]["sorts"][0]["direction"] == "DESCENDING"
    assert captured["json_body"]["limit"] == 5
    assert captured["json_body"]["after"] == "cursor-1"


def test_hubspot_create_crm_object_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="HubSpot",
        provider="hubspot",
        kind="api_key",
        allowed_targets=["native_tool:hubspot_create_crm_object"],
        secret_fields={
            "private_app_token": "hubspot-token",
            "base_url": "https://hubspot.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "2", "properties": json_body["properties"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.hubspot_create_crm_object.func(
            object_type="companies",
            properties_json='{"name":"Acme","domain":"example.com"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "2"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://hubspot.example/crm/v3/objects/companies"
    assert captured["headers"]["Authorization"] == "Bearer hubspot-token"
    assert captured["json_body"]["properties"]["domain"] == "example.com"


def test_zendesk_search_uses_env_basic_auth_and_params(monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ZENDESK_EMAIL", "alice@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "zendesk-token")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"results": [{"id": 100, "result_type": "ticket"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.zendesk_search.func(
            query="type:ticket status:open",
            sort_by="updated_at",
            sort_order="desc",
            include="users,organizations",
            page=2,
        )
    )

    expected = base64.b64encode(b"alice@example.com/token:zendesk-token").decode()
    assert result == [{"id": 100, "result_type": "ticket"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://example.zendesk.com/api/v2/search.json"
    assert captured["headers"]["Authorization"] == f"Basic {expected}"
    assert captured["params"]["query"] == "type:ticket status:open"
    assert captured["params"]["sort_by"] == "updated_at"
    assert captured["params"]["include"] == "users,organizations"
    assert captured["params"]["page"] == 2


def test_zendesk_create_ticket_uses_vault_bearer_and_payload(tmp_path, monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Zendesk",
        provider="zendesk",
        kind="oauth_token",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "access_token": "zendesk-token",
            "base_url": "https://support.example.com/api/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"ticket": {"id": 101, **json_body["ticket"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.zendesk_create_ticket.func(
            subject="Need help",
            comment_body="The thing is broken.",
            requester_email="customer@example.com",
            priority="high",
            tags="vip, broken",
            custom_fields_json='[{"id":123,"value":"yes"}]',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["ticket"]["id"] == 101
    assert captured["method"] == "POST"
    assert captured["url"] == "https://support.example.com/api/v2/tickets.json"
    assert captured["headers"]["Authorization"] == "Bearer zendesk-token"
    assert captured["json_body"]["ticket"]["requester"]["email"] == "customer@example.com"
    assert captured["json_body"]["ticket"]["tags"] == ["vip", "broken"]
    assert captured["json_body"]["ticket"]["custom_fields"][0]["id"] == 123


def test_mailchimp_list_members_uses_env_api_key_and_derived_base(monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAILCHIMP_API_KEY", "abc123-us21")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"members": [{"id": "member-1", "email_address": "alice@example.com"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailchimp_list_members.func(
            list_id="list-1",
            status="subscribed",
            count=5,
            offset=10,
        )
    )

    assert result == [{"id": "member-1", "email_address": "alice@example.com"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://us21.api.mailchimp.com/3.0/lists/list-1/members"
    assert captured["headers"]["Authorization"] == "apikey abc123-us21"
    assert captured["params"]["status"] == "subscribed"
    assert captured["params"]["count"] == 5
    assert captured["params"]["offset"] == 10


def test_mailchimp_add_or_update_member_uses_vault_key_and_hash(tmp_path, monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Mailchimp",
        provider="mailchimp",
        kind="api_key",
        allowed_targets=["native_tool:mailchimp_add_or_update_member"],
        secret_fields={
            "api_key": "abc123-us21",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "member-1", "email_address": json_body["email_address"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailchimp_add_or_update_member.func(
            list_id="list-1",
            email="Alice@Example.com",
            status_if_new="subscribed",
            merge_fields_json='{"FNAME":"Alice"}',
            tags="vip, customer",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_hash = hashlib.md5(b"alice@example.com").hexdigest()
    assert result["email_address"] == "Alice@Example.com"
    assert captured["method"] == "PUT"
    assert captured["url"] == f"https://us21.api.mailchimp.com/3.0/lists/list-1/members/{expected_hash}"
    assert captured["headers"]["Authorization"] == "apikey abc123-us21"
    assert captured["json_body"]["merge_fields"] == {"FNAME": "Alice"}
    assert captured["json_body"]["tags"] == ["vip", "customer"]


def test_customer_engagement_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import customer_engagement_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    hubspot = tools.hubspot_get_crm_object.func("contacts", "1")
    zendesk_base = tools.zendesk_get_ticket.func("1")
    mailchimp_base = tools.mailchimp_list_audiences.func()

    assert "No HubSpot credential found" in hubspot
    assert "HUBSPOT_ACCESS_TOKEN" in hubspot
    assert "native_tool:hubspot_get_crm_object" in hubspot
    assert "No Zendesk base URL found" in zendesk_base
    assert "ZENDESK_SUBDOMAIN" in zendesk_base
    assert "No Mailchimp API root found" in mailchimp_base
    assert "MAILCHIMP_API_KEY" in mailchimp_base


def test_customer_engagement_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "hubspot_list_crm_objects",
        "hubspot_search_crm_objects",
        "hubspot_get_crm_object",
        "zendesk_search",
        "zendesk_get_ticket",
        "zendesk_list_tickets",
        "zendesk_get_user",
        "zendesk_search_users",
        "mailchimp_list_audiences",
        "mailchimp_list_members",
        "mailchimp_get_member",
        "mailchimp_list_campaigns",
    }
    moderate_names = {
        "hubspot_create_crm_object",
        "hubspot_update_crm_object",
        "hubspot_archive_crm_object",
        "zendesk_create_ticket",
        "zendesk_update_ticket",
        "mailchimp_add_or_update_member",
        "mailchimp_update_member_tags",
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


def test_customer_engagement_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.customer_engagement_service_integrations import (
        hubspot_search_crm_objects,
        mailchimp_add_or_update_member,
        zendesk_create_ticket,
    )

    assert "config" not in hubspot_search_crm_objects.args_schema.model_json_schema()["properties"]
    assert "config" not in zendesk_create_ticket.args_schema.model_json_schema()["properties"]
    assert "config" not in mailchimp_add_or_update_member.args_schema.model_json_schema()["properties"]
