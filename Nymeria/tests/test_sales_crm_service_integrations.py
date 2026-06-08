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


def test_pipedrive_list_records_uses_env_api_token_and_v2(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "pipedrive-token")
    monkeypatch.setenv("PIPEDRIVE_BASE_URL", "https://api.pipedrive.com/api/v2")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
                "auth_params": auth_params,
            }
        )
        return {"success": True, "data": [{"id": 1, "title": "Deal"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pipedrive_list_records.func(
            resource="deals",
            limit=25,
            cursor="next",
            filter_id="7",
            owner_id="42",
        )
    )

    assert result == [{"id": 1, "title": "Deal"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.pipedrive.com/api/v2/deals"
    assert captured["params"]["limit"] == 25
    assert captured["params"]["cursor"] == "next"
    assert captured["params"]["filter_id"] == "7"
    assert captured["params"]["owner_id"] == "42"
    assert captured["auth_params"] == {"api_token": "pipedrive-token"}
    assert "Authorization" not in captured["headers"]


def test_pipedrive_search_records_uses_v1_search_with_bearer_vault(tmp_path, monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Pipedrive",
        provider="pipedrive",
        kind="oauth_token",
        allowed_targets=["native_tool:pipedrive_search_records"],
        secret_fields={
            "access_token": "pipedrive-access",
            "base_url": "https://api.pipedrive.com/api/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers, "auth_params": auth_params})
        return {"success": True, "data": {"items": [{"item": {"id": 2, "name": "Alice"}}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pipedrive_search_records.func(
            resource="persons",
            term="Alice",
            exact_match=True,
            fields="name,email",
            limit=10,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"item": {"id": 2, "name": "Alice"}}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.pipedrive.com/v1/persons/search"
    assert captured["headers"]["Authorization"] == "Bearer pipedrive-access"
    assert captured["auth_params"] == {}
    assert captured["params"]["term"] == "Alice"
    assert captured["params"]["exact_match"] == "true"
    assert captured["params"]["fields"] == "name,email"


def test_pipedrive_create_record_uses_vault_api_token(tmp_path, monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Pipedrive",
        provider="pipedrive",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_token": "pipedrive-token",
            "base_url": "https://api.pipedrive.com/api/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "auth_params": auth_params})
        return {"success": True, "data": {"id": 3, **json_body}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pipedrive_create_record.func(
            resource="deal",
            fields_json='{"title":"New deal","value":1000}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == 3
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.pipedrive.com/api/v2/deals"
    assert captured["json_body"]["title"] == "New deal"
    assert captured["auth_params"] == {"api_token": "pipedrive-token"}


def test_pipedrive_update_lead_uses_v1_put(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "pipedrive-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "auth_params": auth_params})
        return {"success": True, "data": {"id": "lead-1", **json_body}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pipedrive_update_record.func(
            resource="lead",
            record_id="lead-1",
            fields_json='{"title":"Qualified lead"}',
        )
    )

    assert result["title"] == "Qualified lead"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://api.pipedrive.com/v1/leads/lead-1"
    assert captured["auth_params"] == {"api_token": "pipedrive-token"}


def test_salesforce_query_and_create_use_env_bearer(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SALESFORCE_INSTANCE_URL", "https://example.my.salesforce.com")
    monkeypatch.setenv("SALESFORCE_ACCESS_TOKEN", "sf-token")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        if method == "GET":
            return {"records": [{"Id": "001", "Name": "Acme"}]}
        return {"id": "001", "success": True}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    query_result = json.loads(tools.salesforce_query_records.func("SELECT Id, Name FROM Account", limit=5))
    create_result = json.loads(
        tools.salesforce_create_record.func(
            object_name="Account",
            fields_json='{"Name": "Acme"}',
        )
    )

    assert query_result[0]["Name"] == "Acme"
    assert create_result["id"] == "001"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://example.my.salesforce.com/services/data/v59.0/query"
    assert calls[0]["params"]["q"] == "SELECT Id, Name FROM Account LIMIT 5"
    assert calls[0]["headers"]["Authorization"] == "Bearer sf-token"
    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "https://example.my.salesforce.com/services/data/v59.0/sobjects/Account"
    assert calls[1]["json_body"] == {"Name": "Acme"}


def test_zoho_crm_search_and_create_use_access_token(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ZOHO_CRM_ACCESS_TOKEN", "zoho-token")
    monkeypatch.setenv("ZOHO_CRM_API_DOMAIN", "https://www.zohoapis.eu")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"data": [{"id": "z1", "Company": "Acme"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    search_result = json.loads(
        tools.zoho_crm_search_records.func(
            resource="leads",
            criteria="(Company:equals:Acme)",
            per_page=25,
        )
    )
    create_result = json.loads(
        tools.zoho_crm_create_records.func(
            resource="leads",
            records_json='{"Company": "Acme"}',
        )
    )

    assert search_result[0]["id"] == "z1"
    assert create_result["data"][0]["Company"] == "Acme"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://www.zohoapis.eu/crm/v2/Leads/search"
    assert calls[0]["params"]["criteria"] == "(Company:equals:Acme)"
    assert calls[0]["params"]["per_page"] == 25
    assert calls[0]["headers"]["Authorization"] == "Zoho-oauthtoken zoho-token"
    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "https://www.zohoapis.eu/crm/v2/Leads"
    assert calls[1]["json_body"] == {"data": [{"Company": "Acme"}]}


def test_freshworks_crm_list_and_update_use_domain_and_token(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("FRESHWORKS_CRM_DOMAIN", "acme")
    monkeypatch.setenv("FRESHWORKS_CRM_API_KEY", "fresh-key")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"contacts": [{"id": 1, "first_name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    list_result = json.loads(
        tools.freshworks_crm_list_records.func(
            resource="contacts",
            view_id="10",
            page=2,
            per_page=30,
        )
    )
    update_result = json.loads(
        tools.freshworks_crm_update_record.func(
            resource="contact",
            record_id="1",
            fields_json='{"first_name": "Ada"}',
        )
    )

    assert list_result["contacts"][0]["first_name"] == "Ada"
    assert update_result["contacts"][0]["id"] == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://acme.myfreshworks.com/crm/sales/api/contacts/view/10"
    assert calls[0]["params"] == {"page": 2, "per_page": 30}
    assert calls[0]["headers"]["Authorization"] == "Token token=fresh-key"
    assert calls[1]["method"] == "PUT"
    assert calls[1]["url"] == "https://acme.myfreshworks.com/crm/sales/api/contacts/1"
    assert calls[1]["json_body"] == {"first_name": "Ada"}


def test_salesmate_search_and_create_use_session_headers(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SALESMATE_SESSION_TOKEN", "salesmate-token")
    monkeypatch.setenv("SALESMATE_LINK_NAME", "acme.salesmate.io")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None, auth_params=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"Data": {"data": [{"id": 1, "name": "Acme"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    search_result = json.loads(
        tools.salesmate_search_records.func(
            resource="companies",
            query_json='{"name": "Acme"}',
            fields="name,id",
            page_no=3,
            rows=20,
        )
    )
    create_result = json.loads(
        tools.salesmate_create_record.func(
            resource="company",
            fields_json='{"name": "Acme"}',
        )
    )

    assert search_result[0]["name"] == "Acme"
    assert create_result[0]["id"] == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://apis.salesmate.io/v2/companies/search"
    assert calls[0]["params"] == {"pageNo": 3, "rows": 20}
    assert calls[0]["json_body"] == {"fields": ["name", "id"], "query": {"name": "Acme"}}
    assert calls[0]["headers"]["sessionToken"] == "salesmate-token"
    assert calls[0]["headers"]["x-linkname"] == "acme.salesmate.io"
    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "https://apis.salesmate.io/v1/companies"
    assert calls[1]["json_body"] == {"name": "Acme"}


def test_pipedrive_missing_credentials_return_setup_hint(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    result = tools.pipedrive_get_record.func("deals", "1")
    salesforce_result = tools.salesforce_get_record.func("Account", "001")
    zoho_result = tools.zoho_crm_list_records.func()
    freshworks_result = tools.freshworks_crm_list_records.func()
    salesmate_result = tools.salesmate_list_users.func()

    assert "No Pipedrive credential found" in result
    assert "PIPEDRIVE_API_TOKEN" in result
    assert "native_tool:pipedrive_get_record" in result
    assert "SALESFORCE_INSTANCE_URL" in salesforce_result
    assert "ZOHO_CRM_ACCESS_TOKEN" in zoho_result
    assert "FRESHWORKS_CRM_DOMAIN" in freshworks_result
    assert "SALESMATE_LINK_NAME" in salesmate_result


def test_sales_crm_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "salesforce_query_records",
        "salesforce_get_record",
        "zoho_crm_list_records",
        "zoho_crm_search_records",
        "zoho_crm_get_record",
        "freshworks_crm_list_records",
        "freshworks_crm_search_records",
        "freshworks_crm_get_record",
        "salesmate_list_users",
        "salesmate_search_records",
        "salesmate_get_record",
        "pipedrive_list_records",
        "pipedrive_search_records",
        "pipedrive_get_record",
        "pipedrive_list_users",
    }
    moderate_names = {
        "salesforce_create_record",
        "salesforce_update_record",
        "salesforce_delete_record",
        "zoho_crm_create_records",
        "zoho_crm_update_record",
        "zoho_crm_delete_record",
        "freshworks_crm_create_record",
        "freshworks_crm_update_record",
        "freshworks_crm_delete_record",
        "salesmate_create_record",
        "salesmate_update_record",
        "salesmate_delete_record",
        "pipedrive_create_record",
        "pipedrive_update_record",
        "pipedrive_delete_record",
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


def test_sales_crm_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.sales_crm_service_integrations import (
        freshworks_crm_create_record,
        pipedrive_create_record,
        pipedrive_list_records,
        pipedrive_update_record,
        salesforce_query_records,
        salesmate_search_records,
        zoho_crm_update_record,
    )

    assert "config" not in salesforce_query_records.args_schema.model_json_schema()["properties"]
    assert "config" not in zoho_crm_update_record.args_schema.model_json_schema()["properties"]
    assert "config" not in freshworks_crm_create_record.args_schema.model_json_schema()["properties"]
    assert "config" not in salesmate_search_records.args_schema.model_json_schema()["properties"]
    assert "config" not in pipedrive_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in pipedrive_create_record.args_schema.model_json_schema()["properties"]
    assert "config" not in pipedrive_update_record.args_schema.model_json_schema()["properties"]
