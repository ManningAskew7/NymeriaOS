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


def test_affinity_create_person_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setenv("AFFINITY_API_KEY", "affinity-key")
    monkeypatch.setenv("AFFINITY_BASE_URL", "https://affinity.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": 33, "emails": kwargs["json_body"]["emails"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.affinity_create_person.func(
            first_name="Ada",
            last_name="Lovelace",
            emails_csv="ada@example.com, ada@engine.example",
            organization_ids_csv="10,11",
        )
    )

    expected_basic = base64.b64encode(b":affinity-key").decode("ascii")
    assert result["id"] == 33
    assert captured["method"] == "POST"
    assert captured["url"] == "https://affinity.example/persons"
    assert captured["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert captured["json_body"]["emails"] == ["ada@example.com", "ada@engine.example"]
    assert captured["json_body"]["organization_ids"] == [10, 11]


def test_affinity_list_entries_builds_paged_endpoint(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setenv("AFFINITY_API_KEY", "affinity-key")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"list_entries": [{"id": 7}], "page_token": "next"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.affinity_list_entries.func(list_id="123", limit=25, page_token="abc"))

    assert result["list_entries"][0]["id"] == 7
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.affinity.co/lists/123/list-entries"
    assert captured["params"] == {"page_size": 25, "page_token": "abc"}


def test_keap_create_record_uses_vault_bearer_and_snake_case(tmp_path, monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Keap",
        provider="keap",
        kind="oauth_token",
        allowed_targets=["native_tool:keap_create_record"],
        secret_fields={"accessToken": "keap-token"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": 9, "body": kwargs["json_body"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.keap_create_record.func(
            resource="contact",
            fields_json='{"givenName": "Ada", "emailAddresses": [{"email": "ada@example.com"}]}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == 9
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://api.infusionsoft.com/crm/rest/v1/contacts"
    assert captured["headers"]["Authorization"] == "Bearer keap-token"
    assert captured["json_body"]["given_name"] == "Ada"
    assert captured["json_body"]["email_addresses"][0]["email"] == "ada@example.com"


def test_keap_apply_tags_uses_env_token(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setenv("KEAP_ACCESS_TOKEN", "keap-token")
    monkeypatch.setenv("KEAP_BASE_URL", "https://keap.example/rest/v1")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"applied": kwargs["json_body"]["tagIds"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.keap_apply_tags.func(contact_id="42", tag_ids_csv="1, 2,3"))

    assert result["applied"] == [1, 2, 3]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://keap.example/rest/v1/contacts/42/tags"
    assert captured["headers"]["Authorization"] == "Bearer keap-token"


def test_relationship_crm_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import relationship_crm_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    copper = tools.copper_get_record.func(resource="companies", record_id="1")
    agile = tools.agilecrm_get_record.func(resource="contact", record_id="1")
    monica = tools.monica_get_record.func(resource="contact", record_id="1")
    affinity = tools.affinity_get_record.func(resource="person", record_id="1")
    keap = tools.keap_get_record.func(resource="contact", record_id="1")

    assert "No Copper credential found" in copper
    assert "COPPER_API_KEY and COPPER_EMAIL" in copper
    assert "native_tool:copper_get_record" in copper
    assert "No Agile CRM credential found" in agile
    assert "AGILECRM_EMAIL, AGILECRM_API_KEY, and AGILECRM_SUBDOMAIN" in agile
    assert "No Monica CRM credential found" in monica
    assert "MONICA_ACCESS_TOKEN" in monica
    assert "No Affinity credential found" in affinity
    assert "AFFINITY_API_KEY" in affinity
    assert "No Keap credential found" in keap
    assert "KEAP_ACCESS_TOKEN" in keap


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
        "affinity_list_records",
        "affinity_get_record",
        "affinity_list_entries",
        "keap_list_records",
        "keap_get_record",
        "keap_list_contact_tags",
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
        "affinity_create_person",
        "affinity_create_organization",
        "affinity_update_record",
        "affinity_delete_record",
        "affinity_create_list_entry",
        "affinity_delete_list_entry",
        "keap_create_record",
        "keap_update_note",
        "keap_delete_record",
        "keap_apply_tags",
        "keap_remove_tags",
        "keap_send_email",
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
        affinity_create_person,
        copper_create_record,
        keap_apply_tags,
        monica_update_record,
    )

    assert "config" not in copper_create_record.args
    assert "config" not in agilecrm_create_record.args
    assert "config" not in monica_update_record.args
    assert "config" not in affinity_create_person.args
    assert "config" not in keap_apply_tags.args
