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


def test_raindrop_list_bookmarks_uses_env_token(monkeypatch):
    from nymeria.tools import bookmark_link_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("RAINDROP_ACCESS_TOKEN", "rain-token")
    monkeypatch.setenv("RAINDROP_BASE_URL", "https://rain.example/rest/v1")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"items": [{"_id": 10, "title": "Docs"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.raindrop_list_bookmarks.func(
            collection_id="0",
            search="api docs",
            tag="reference",
            page=2,
            per_page=25,
        )
    )

    assert result == [{"_id": 10, "title": "Docs"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://rain.example/rest/v1/raindrops/0"
    assert captured["params"]["search"] == "api docs"
    assert captured["params"]["tag"] == "reference"
    assert captured["params"]["page"] == 2
    assert captured["params"]["perpage"] == 25
    assert captured["headers"]["Authorization"] == "Bearer rain-token"


def test_raindrop_create_bookmark_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import bookmark_link_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Raindrop",
        provider="raindrop",
        kind="oauth",
        allowed_targets=["native_tool:raindrop_create_bookmark"],
        secret_fields={"accessToken": "rain-token", "base_url": "https://rain.example/rest/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"item": {"_id": 11, "title": kwargs["json_body"]["title"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.raindrop_create_bookmark.func(
            link="https://example.com",
            collection_id="123",
            title="Example",
            tags="docs,example",
            please_parse=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["_id"] == 11
    assert captured["method"] == "POST"
    assert captured["url"] == "https://rain.example/rest/v1/raindrop"
    assert captured["headers"]["Authorization"] == "Bearer rain-token"
    assert captured["json_body"]["collection"] == {"$id": 123}
    assert captured["json_body"]["tags"] == ["docs", "example"]
    assert captured["json_body"]["pleaseParse"] == {}


def test_yourls_shorten_uses_env_signature(monkeypatch):
    from nymeria.tools import bookmark_link_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("YOURLS_URL", "https://short.example")
    monkeypatch.setenv("YOURLS_SIGNATURE", "sig-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "success", "shorturl": "https://short.example/abc"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.yourls_shorten_url.func(
            url="https://example.com/long",
            keyword="abc",
            title="Example",
        )
    )

    assert result["shorturl"] == "https://short.example/abc"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://short.example/yourls-api.php"
    assert captured["params"]["signature"] == "sig-token"
    assert captured["params"]["action"] == "shorturl"
    assert captured["params"]["keyword"] == "abc"


def test_yourls_expand_uses_vault_username_password(tmp_path, monkeypatch):
    from nymeria.tools import bookmark_link_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="YOURLS",
        provider="yourls",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"url": "https://short.example/yourls-api.php", "username": "ada", "password": "secret"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "success", "longurl": "https://example.com/long"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.yourls_expand_url.func(
            short_url="https://short.example/abc",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["longurl"] == "https://example.com/long"
    assert captured["url"] == "https://short.example/yourls-api.php"
    assert captured["params"]["username"] == "ada"
    assert captured["params"]["password"] == "secret"
    assert captured["params"]["action"] == "expand"


def test_bookmark_link_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import bookmark_link_service_integrations as tools

    values = {"yourls_url": "https://short.example"}
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: values.get(name))

    raindrop = tools.raindrop_get_user.func()
    yourls = tools.yourls_get_db_stats.func()

    assert "No Raindrop credential found" in raindrop
    assert "RAINDROP_ACCESS_TOKEN" in raindrop
    assert "native_tool:raindrop_get_user" in raindrop
    assert "No YOURLS credential found" in yourls
    assert "YOURLS_SIGNATURE or YOURLS_USERNAME + YOURLS_PASSWORD" in yourls
    assert "native_tool:yourls_get_db_stats" in yourls


def test_bookmark_link_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "raindrop_list_bookmarks",
        "raindrop_get_bookmark",
        "raindrop_list_collections",
        "raindrop_get_collection",
        "raindrop_list_tags",
        "raindrop_get_user",
        "yourls_expand_url",
        "yourls_get_url_stats",
        "yourls_get_db_stats",
    }
    moderate_names = {
        "raindrop_create_bookmark",
        "raindrop_update_bookmark",
        "raindrop_delete_bookmark",
        "raindrop_delete_tags",
        "yourls_shorten_url",
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


def test_bookmark_link_tool_schemas_hide_runtime_config():
    from nymeria.tools.bookmark_link_service_integrations import (
        raindrop_create_bookmark,
        raindrop_list_bookmarks,
        yourls_shorten_url,
    )

    assert "config" not in raindrop_list_bookmarks.args_schema.model_json_schema()["properties"]
    assert "config" not in raindrop_create_bookmark.args_schema.model_json_schema()["properties"]
    assert "config" not in yourls_shorten_url.args_schema.model_json_schema()["properties"]
