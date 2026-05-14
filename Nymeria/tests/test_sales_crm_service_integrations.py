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


def test_pipedrive_missing_credentials_return_setup_hint(monkeypatch):
    from nymeria.tools import sales_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    result = tools.pipedrive_get_record.func("deals", "1")

    assert "No Pipedrive credential found" in result
    assert "PIPEDRIVE_API_TOKEN" in result
    assert "native_tool:pipedrive_get_record" in result


def test_sales_crm_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "pipedrive_list_records",
        "pipedrive_search_records",
        "pipedrive_get_record",
        "pipedrive_list_users",
    }
    moderate_names = {
        "pipedrive_create_record",
        "pipedrive_update_record",
        "pipedrive_delete_record",
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


def test_sales_crm_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.sales_crm_service_integrations import (
        pipedrive_create_record,
        pipedrive_list_records,
        pipedrive_update_record,
    )

    assert "config" not in pipedrive_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in pipedrive_create_record.args_schema.model_json_schema()["properties"]
    assert "config" not in pipedrive_update_record.args_schema.model_json_schema()["properties"]
