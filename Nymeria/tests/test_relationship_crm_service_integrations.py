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


def test_copper_list_records_uses_env_headers(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setenv("COPPER_API_KEY", "copper-key")
    monkeypatch.setenv("COPPER_EMAIL", "ada@example.com")
    monkeypatch.setenv("COPPER_BASE_URL", "https://copper.example/developer_api/v1")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return [{"id": 1, "name": "Analytical Engines"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.copper_list_records.func(resource="companies", filter_json='{"name": "Analytical"}', limit=10))

    assert result[0]["name"] == "Analytical Engines"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://copper.example/developer_api/v1/companies/search"
    assert captured["json_body"] == {"name": "Analytical", "page_size": 10}
    assert captured["headers"]["X-PW-AccessToken"] == "copper-key"
    assert captured["headers"]["X-PW-UserEmail"] == "ada@example.com"


def test_agilecrm_create_company_uses_vault_basic_auth(tmp_path, monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Agile CRM",
        provider="agilecrm",
        kind="api_key",
        allowed_targets=["native_tool:agilecrm_create_record"],
        secret_fields={"email": "ada@example.com", "apiKey": "agile-key", "subdomain": "acme"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": 42, "type": kwargs["json_body"]["type"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.agilecrm_create_record.func(
            resource="company",
            fields_json='{"properties": [{"type": "SYSTEM", "name": "name", "value": "Acme"}]}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_basic = base64.b64encode(b"ada@example.com:agile-key").decode("ascii")
    assert result["type"] == "COMPANY"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://acme.agilecrm.com/dev/api/contacts"
    assert captured["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert captured["json_body"]["type"] == "COMPANY"


def test_monica_list_records_appends_api_to_base_url(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setenv("MONICA_ACCESS_TOKEN", "monica-token")
    monkeypatch.setenv("MONICA_BASE_URL", "https://monica.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": [{"id": 7, "first_name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.monica_list_records.func(resource="contacts", limit=5, page=2))

    assert result["data"][0]["first_name"] == "Ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://monica.example/api/contacts"
    assert captured["params"] == {"limit": 5, "page": 2}
    assert captured["headers"]["Authorization"] == "Bearer monica-token"


def test_relationship_crm_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    copper = tools.copper_get_record.func(resource="companies", record_id="1")
    agile = tools.agilecrm_get_record.func(resource="contact", record_id="1")
    monica = tools.monica_get_record.func(resource="contact", record_id="1")

    assert "No Copper credential found" in copper
    assert "COPPER_API_KEY and COPPER_EMAIL" in copper
    assert "native_tool:copper_get_record" in copper
    assert "No Agile CRM credential found" in agile
    assert "AGILECRM_EMAIL, AGILECRM_API_KEY, and AGILECRM_SUBDOMAIN" in agile
    assert "No Monica CRM credential found" in monica
    assert "MONICA_ACCESS_TOKEN" in monica


def test_relationship_crm_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "copper_list_records",
        "copper_get_record",
        "agilecrm_list_records",
        "agilecrm_get_record",
        "monica_list_records",
        "monica_get_record",
    ]
    moderate_names = [
        "copper_create_record",
        "copper_update_record",
        "copper_delete_record",
        "agilecrm_create_record",
        "agilecrm_update_record",
        "agilecrm_delete_record",
        "monica_create_record",
        "monica_update_record",
        "monica_delete_record",
    ]

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


def test_relationship_crm_tool_schemas_hide_runtime_config():
    from nymeria.tools.relationship_crm_service_integrations import (
        agilecrm_create_record,
        copper_create_record,
        monica_update_record,
    )

    assert "config" not in copper_create_record.args
    assert "config" not in agilecrm_create_record.args
    assert "config" not in monica_update_record.args
