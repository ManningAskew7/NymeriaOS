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


def test_clearbit_enrich_company_uses_env_api_key(monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("CLEARBIT_API_KEY", "clearbit-key")
    monkeypatch.setenv("CLEARBIT_COMPANY_BASE_URL", "https://clearbit.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"name": "Example Inc"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.clearbit_enrich_company.func(domain="example.com", company_name="Example")
    )

    assert result["name"] == "Example Inc"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://clearbit.example/v2/companies/find"
    assert captured["headers"]["Authorization"] == "Bearer clearbit-key"
    assert captured["params"]["domain"] == "example.com"
    assert captured["params"]["company_name"] == "Example"


def test_uplead_enrich_person_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Uplead",
        provider="upleadApi",
        kind="api_key",
        allowed_targets=["native_tool:uplead_enrich_person"],
        secret_fields={"apiKey": "uplead-key", "base_url": "https://uplead.example/v2"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"email": "ada@example.com"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.uplead_enrich_person.func(
            first_name="Ada",
            last_name="Lovelace",
            domain="example.com",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["email"] == "ada@example.com"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://uplead.example/v2/person-search"
    assert captured["headers"]["Authorization"] == "uplead-key"
    assert captured["params"]["first_name"] == "Ada"
    assert captured["params"]["last_name"] == "Lovelace"
    assert captured["params"]["domain"] == "example.com"


def test_dropcontact_submit_uses_env_key(monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("DROPCONTACT_API_KEY", "dropcontact-key")
    monkeypatch.setenv("DROPCONTACT_BASE_URL", "https://dropcontact.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"request_id": "req-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.dropcontact_submit_enrichment.func(
            email="ada@example.com",
            first_name="Ada",
            last_name="Lovelace",
            company="Example",
            french_company_enrich=True,
            language="fr",
        )
    )

    assert result["request_id"] == "req-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://dropcontact.example/batch"
    assert captured["headers"]["X-Access-Token"] == "dropcontact-key"
    assert captured["json_body"]["data"][0]["email"] == "ada@example.com"
    assert captured["json_body"]["siren"] is True
    assert captured["json_body"]["language"] == "fr"


def test_humantic_get_profile_uses_query_api_key(monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("HUMANTIC_API_KEY", "humantic-key")
    monkeypatch.setenv("HUMANTIC_BASE_URL", "https://humantic.example/v1")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"results": {"profile": "ready"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.humantic_get_profile.func(user_id="ada@example.com", persona="sales,hiring"))

    assert result["profile"] == "ready"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://humantic.example/v1/user-profile"
    assert captured["params"]["userid"] == "ada@example.com"
    assert captured["params"]["persona"] == "sales,hiring"
    assert captured["params"]["apikey"] == "humantic-key"


def test_lonescale_add_people_item_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="LoneScale",
        provider="lonescale",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"api_key": "lonescale-key", "baseUrl": "https://lonescale.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "item-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.lonescale_add_people_item.func(
            list_id="list-1",
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            company_name="Example",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "item-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://lonescale.example/lists/list-1/item"
    assert captured["headers"]["X-API-KEY"] == "lonescale-key"
    assert captured["headers"]["Authorization"] == "lonescale-key"
    assert captured["json_body"]["first_name"] == "Ada"
    assert captured["json_body"]["company_name"] == "Example"


def test_uproc_process_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("UPROC_EMAIL", "ada@example.com")
    monkeypatch.setenv("UPROC_API_KEY", "uproc-key")
    monkeypatch.setenv("UPROC_BASE_URL", "https://uproc.example/api/v2")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "queued"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.uproc_process.func(
            processor="email/verify",
            params_json='{"email": "ada@example.com"}',
            callback_url="https://hooks.example/uproc",
        )
    )

    auth_header = captured["headers"]["Authorization"]
    auth_value = base64.b64decode(auth_header.removeprefix("Basic ")).decode()
    assert result["status"] == "queued"
    assert auth_value == "ada@example.com:uproc-key"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://uproc.example/api/v2/process"
    assert captured["json_body"]["processor"] == "email/verify"
    assert captured["json_body"]["params"] == {"email": "ada@example.com"}
    assert captured["json_body"]["callback"] == {"data": "https://hooks.example/uproc"}


def test_lead_enrichment_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import lead_enrichment_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    clearbit = tools.clearbit_enrich_person.func(email="ada@example.com")
    uproc = tools.uproc_get_profile.func()

    assert "No Clearbit credential found" in clearbit
    assert "CLEARBIT_API_KEY" in clearbit
    assert "native_tool:clearbit_enrich_person" in clearbit
    assert "No uProc credential found" in uproc
    assert "UPROC_EMAIL" in uproc
    assert "native_tool:uproc_get_profile" in uproc


def test_lead_enrichment_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    tool_names = {
        "clearbit_enrich_company",
        "clearbit_autocomplete_company",
        "clearbit_enrich_person",
        "uplead_enrich_company",
        "uplead_enrich_person",
        "dropcontact_submit_enrichment",
        "dropcontact_fetch_request",
        "humantic_create_profile",
        "humantic_get_profile",
        "humantic_update_profile_text",
        "lonescale_create_list",
        "lonescale_add_people_item",
        "lonescale_add_company_item",
        "uproc_get_profile",
        "uproc_process",
    }

    for name in tool_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_lead_enrichment_tool_schemas_hide_runtime_config():
    from nymeria.tools.lead_enrichment_service_integrations import (
        clearbit_enrich_person,
        dropcontact_submit_enrichment,
        uproc_process,
    )

    assert "config" not in clearbit_enrich_person.args_schema.model_json_schema()["properties"]
    assert "config" not in dropcontact_submit_enrichment.args_schema.model_json_schema()["properties"]
    assert "config" not in uproc_process.args_schema.model_json_schema()["properties"]
