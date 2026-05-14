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


def test_coingecko_price_builds_simple_price_request(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured.update({"url": url, "params": params, "headers": headers})
        return {"bitcoin": {"usd": 100}}

    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = tools.coingecko_price.func(
        ids="bitcoin, ethereum",
        vs_currencies="usd,aud",
        include_market_cap=True,
    )

    assert json.loads(result) == {"bitcoin": {"usd": 100}}
    assert captured["url"].endswith("/simple/price")
    assert captured["params"]["ids"] == "bitcoin,ethereum"
    assert captured["params"]["vs_currencies"] == "usd,aud"
    assert captured["params"]["include_market_cap"] == "true"


def test_hackernews_get_item_can_drop_comments(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    monkeypatch.setattr(
        tools,
        "_get_json",
        lambda url, params=None, headers=None: {"id": 123, "title": "Story", "children": [{"id": 1}]},
    )

    result = json.loads(tools.hackernews_get_item.func(item_id=123, include_comments=False))

    assert result == {"id": 123, "title": "Story"}


def test_npm_package_info_uses_configured_registry(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured["url"] = url
        return {"name": "@scope/pkg", "version": "1.0.0"}

    monkeypatch.setenv("NPM_REGISTRY_URL", "https://npm.example")
    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(tools.npm_package_info.func("@scope/pkg", "latest"))

    assert result["name"] == "@scope/pkg"
    assert captured["url"] == "https://npm.example/%40scope%2Fpkg/latest"


def test_npm_package_info_uses_vault_registry_and_token(tmp_path, monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Private npm",
        provider="npm",
        kind="api_key",
        allowed_targets=["native_tool:npm_package_info"],
        secret_fields={
            "registry_url": "https://private-npm.example",
            "token": "npm-token",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return {"name": "pkg"}

    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(
        tools.npm_package_info.func(
            "pkg",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"name": "pkg"}
    assert captured["url"] == "https://private-npm.example/pkg/latest"
    assert captured["headers"] == {"Authorization": "Bearer npm-token"}


def test_open_thesaurus_synonyms_maps_options(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured.update({"url": url, "params": params, "headers": headers})
        return {"synsets": [{"terms": [{"term": "Auto"}]}]}

    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(
        tools.open_thesaurus_synonyms.func(
            text="wagen",
            baseform=True,
            substring=True,
            substring_max_results=300,
        )
    )

    assert result == [{"terms": [{"term": "Auto"}]}]
    assert captured["url"].endswith("/synonyme/search")
    assert captured["params"]["q"] == "wagen"
    assert captured["params"]["format"] == "application/json"
    assert captured["params"]["baseform"] == "true"
    assert captured["params"]["substring"] == "true"
    assert captured["params"]["substringMaxResults"] == 250
    assert captured["headers"] == {"User-Agent": "Nymeria"}


def test_rss_feed_read_parses_feed(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    xml = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Example Feed</title>
        <link>https://example.com</link>
        <item>
          <title>Post</title>
          <link>https://example.com/post</link>
          <description>Hello</description>
        </item>
      </channel>
    </rss>"""

    monkeypatch.setattr(tools, "_get_text", lambda url, verify=True: xml)

    result = json.loads(tools.rss_feed_read.func("https://example.com/feed.xml", max_items=1))

    assert result["feed"]["title"] == "Example Feed"
    assert result["items"] == [
        {
            "title": "Post",
            "link": "https://example.com/post",
            "published": None,
            "author": None,
            "summary": "Hello",
        }
    ]


def test_nasa_apod_requires_key_and_sends_key(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    assert "NASA_API_KEY" in tools.nasa_apod.func()

    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured.update({"url": url, "params": params})
        return {"title": "APOD"}

    monkeypatch.setenv("NASA_API_KEY", "nasa-key")
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(tools.nasa_apod.func(date="2026-05-14"))

    assert result == {"title": "APOD"}
    assert captured["url"].endswith("/planetary/apod")
    assert captured["params"]["api_key"] == "nasa-key"
    assert captured["params"]["date"] == "2026-05-14"


def test_nasa_apod_uses_vault_credential(tmp_path, monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="NASA",
        provider="nasa",
        kind="api_key",
        allowed_targets=["native_tool:nasa_apod"],
        secret_fields={"api_key": "vault-nasa-key"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured["params"] = params
        return {"title": "Vault APOD"}

    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(
        tools.nasa_apod.func(
            date="2026-05-14",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"title": "Vault APOD"}
    assert captured["params"]["api_key"] == "vault-nasa-key"


def test_openweathermap_current_uses_city_location(monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured.update({"url": url, "params": params})
        return {"name": "Berlin"}

    monkeypatch.setenv("OPENWEATHERMAP_API_KEY", "weather-key")
    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(tools.openweathermap_current.func(city="Berlin,de", units="metric", language="de"))

    assert result == {"name": "Berlin"}
    assert captured["url"].endswith("/weather")
    assert captured["params"]["APPID"] == "weather-key"
    assert captured["params"]["q"] == "Berlin,de"
    assert captured["params"]["units"] == "metric"
    assert captured["params"]["lang"] == "de"


def test_openweathermap_current_uses_vault_credential(tmp_path, monkeypatch):
    from nymeria.tools import public_info_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="OpenWeatherMap",
        provider="openweathermap",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"api_key": "vault-weather-key"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_get_json(url, params=None, headers=None):
        captured["params"] = params
        return {"name": "Sydney"}

    monkeypatch.setattr(tools, "_get_json", fake_get_json)

    result = json.loads(
        tools.openweathermap_current.func(
            city="Sydney,au",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"name": "Sydney"}
    assert captured["params"]["APPID"] == "vault-weather-key"


def test_quickchart_create_url_returns_encoded_chart():
    from nymeria.tools.public_info_integrations import quickchart_create_url

    result = json.loads(
        quickchart_create_url.func(
            chart_type="bar",
            labels_json='["A", "B"]',
            data_json="[1, 2]",
            label="Sales",
            width=640,
            height=360,
        )
    )

    assert result["chart"]["type"] == "bar"
    assert result["chart"]["data"]["labels"] == ["A", "B"]
    assert result["chart"]["data"]["datasets"][0]["data"] == [1, 2]
    assert result["url"].startswith("https://quickchart.io/chart?")
    assert "width=640" in result["url"]
    assert "height=360" in result["url"]


def test_public_info_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "coingecko_price",
        "coingecko_coin_markets",
        "hackernews_search",
        "hackernews_get_item",
        "hackernews_get_user",
        "npm_package_info",
        "npm_package_search",
        "open_thesaurus_synonyms",
        "nasa_apod",
        "openweathermap_current",
        "openweathermap_forecast",
        "quickchart_create_url",
    }
    for name in safe_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE
        assert metadata.default_enabled is False

    rss_metadata = get_tool_metadata("rss_feed_read")
    assert "rss_feed_read" in OPTIONAL_TOOLS
    assert rss_metadata is not None
    assert rss_metadata.category == ToolCategory.INTEGRATIONS
    assert rss_metadata.security_level == SecurityLevel.MODERATE


def test_native_credential_lookup_prefers_bound_target(tmp_path, monkeypatch):
    from nymeria.tools.native_credentials import get_native_credential_value

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Unscoped NASA",
        provider="nasa",
        kind="api_key",
        secret_fields={"api_key": "unscoped"},
        created_by_user_id="alice",
    )
    scoped = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Scoped NASA",
        provider="nasa",
        kind="api_key",
        allowed_targets=["native_tool:nasa_apod"],
        secret_fields={"api_key": "scoped"},
        created_by_user_id="alice",
    )
    repo.bind_credential(
        scoped.id,
        target_type="native_tool",
        target_id="nasa_apod",
        actor_user_id="alice",
    )

    result = get_native_credential_value(
        provider="nasa",
        field_names=("api_key",),
        tool_name="nasa_apod",
        config={"configurable": {"user_id": "alice"}},
    )

    assert result is not None
    assert result.value == "scoped"
    assert result.credential_id == scoped.id
