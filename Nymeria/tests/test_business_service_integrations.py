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


def test_bitly_create_uses_vault_token_and_body(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Bitly",
        provider="bitly",
        kind="api_key",
        allowed_targets=["native_tool:bitly_create_bitlink"],
        secret_fields={
            "access_token": "bitly-token",
            "base_url": "https://bitly.example/v4",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"id": "bit.ly/example", "long_url": json_body["long_url"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.bitly_create_bitlink.func(
            long_url="https://example.com",
            title="Example",
            tags="docs, test",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "bit.ly/example"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://bitly.example/v4/bitlinks"
    assert captured["headers"]["Authorization"] == "Bearer bitly-token"
    assert captured["json_body"]["long_url"] == "https://example.com"
    assert captured["json_body"]["tags"] == ["docs", "test"]


def test_bitly_get_returns_setup_hint_without_token(monkeypatch):
    from nymeria.tools.business_service_integrations import bitly_get_bitlink

    monkeypatch.delenv("BITLY_TOKEN", raising=False)

    result = bitly_get_bitlink.func("bit.ly/example")

    assert "No Bitly credential found" in result
    assert "BITLY_TOKEN" in result


def test_brandfetch_colors_uses_api_key(monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return {"colors": [{"hex": "#000000", "type": "dark"}]}

    monkeypatch.setenv("BRANDFETCH_API_KEY", "brand-key")
    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.brandfetch_get_brand_colors.func("openai.com"))

    assert result == [{"hex": "#000000", "type": "dark"}]
    assert captured["url"] == "https://api.brandfetch.io/v2/brands/openai.com"
    assert captured["headers"]["Authorization"] == "Bearer brand-key"


def test_marketstack_eod_builds_latest_request(monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"url": url, "params": params})
        return {"data": [{"symbol": "AAPL"}]}

    monkeypatch.setenv("MARKETSTACK_API_KEY", "market-key")
    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.marketstack_get_eod.func(symbols="AAPL, MSFT", latest=True, limit=2))

    assert result == {"data": [{"symbol": "AAPL"}]}
    assert captured["url"] == "https://api.marketstack.com/v1/eod/latest"
    assert captured["params"]["access_key"] == "market-key"
    assert captured["params"]["symbols"] == "AAPL,MSFT"
    assert captured["params"]["limit"] == 2


def test_deepl_translate_uses_vault_free_plan(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="DeepL Free",
        provider="deepl",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "deepl-key",
            "api_plan": "free",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "data": data, "headers": headers})
        return {"translations": [{"text": "Hallo", "detected_source_language": "EN"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.deepl_translate_text.func(
            text="Hello",
            target_lang="DE",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"text": "Hallo", "detected_source_language": "EN"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api-free.deepl.com/v2/translate"
    assert captured["headers"]["Authorization"] == "DeepL-Auth-Key deepl-key"
    assert captured["data"]["target_lang"] == "DE"


def test_business_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "bitly_get_bitlink",
        "brandfetch_get_brand",
        "brandfetch_get_brand_logos",
        "brandfetch_get_brand_colors",
        "marketstack_get_eod",
        "marketstack_get_ticker",
        "marketstack_get_exchange",
        "deepl_list_languages",
    }
    moderate_names = {"bitly_create_bitlink", "bitly_update_bitlink", "deepl_translate_text"}

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


def test_business_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.business_service_integrations import (
        bitly_get_bitlink,
        deepl_translate_text,
    )

    assert "config" not in bitly_get_bitlink.args_schema.model_json_schema()["properties"]
    assert "config" not in deepl_translate_text.args_schema.model_json_schema()["properties"]
