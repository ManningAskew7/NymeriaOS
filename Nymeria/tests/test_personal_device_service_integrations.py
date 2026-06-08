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


def test_oura_activity_uses_env_token(monkeypatch):
    from nymeria.tools import personal_device_service_integrations as tools

    monkeypatch.setenv("OURA_ACCESS_TOKEN", "oura-token")
    monkeypatch.setenv("OURA_BASE_URL", "https://oura.example/v2")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": [{"id": "a"}, {"id": "b"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.oura_get_daily_activity.func(start_date="2026-05-01", end_date="2026-05-02", limit=1)
    )

    assert result["data"] == [{"id": "a"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://oura.example/v2/usercollection/daily_activity"
    assert captured["params"] == {"start_date": "2026-05-01", "end_date": "2026-05-02"}
    assert captured["headers"]["Authorization"] == "Bearer oura-token"


def test_strava_create_activity_uses_vault_access_token(tmp_path, monkeypatch):
    from nymeria.tools import personal_device_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Strava",
        provider="strava",
        kind="oauth2",
        allowed_targets=["native_tool:strava_create_activity"],
        secret_fields={"accessToken": "strava-token", "baseUrl": "https://strava.example/api/v3"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": 10, "name": kwargs["form_data"]["name"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.strava_create_activity.func(
            name="Run",
            sport_type="Run",
            start_date_local="2026-05-14T09:00:00",
            elapsed_time_seconds=1800,
            distance_meters=5000,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == 10
    assert captured["method"] == "POST"
    assert captured["url"] == "https://strava.example/api/v3/activities"
    assert captured["headers"]["Authorization"] == "Bearer strava-token"
    assert captured["form_data"]["sport_type"] == "Run"
    assert captured["form_data"]["elapsed_time"] == 1800


def test_homeassistant_call_service_uses_env_base_and_token(monkeypatch):
    from nymeria.tools import personal_device_service_integrations as tools

    monkeypatch.setenv("HOMEASSISTANT_BASE_URL", "http://ha.example:8123")
    monkeypatch.setenv("HOMEASSISTANT_ACCESS_TOKEN", "ha-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return [{"entity_id": "light.kitchen"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.homeassistant_call_service.func(
            domain="light",
            service="turn_on",
            data_json='{"entity_id": "light.kitchen"}',
        )
    )

    assert result[0]["entity_id"] == "light.kitchen"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://ha.example:8123/api/services/light/turn_on"
    assert captured["headers"]["Authorization"] == "Bearer ha-token"
    assert captured["json_body"] == {"entity_id": "light.kitchen"}


def test_homeassistant_missing_credentials_returns_setup_hint(monkeypatch):
    from nymeria.tools import personal_device_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    result = tools.homeassistant_get_state.func("sensor.temp")

    assert "No Home Assistant credential found" in result
    assert "HOMEASSISTANT_BASE_URL and HOMEASSISTANT_ACCESS_TOKEN" in result
    assert "native_tool:homeassistant_get_state" in result


def test_philips_hue_update_light_uses_vault_token_and_username(tmp_path, monkeypatch):
    from nymeria.tools import personal_device_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Philips Hue",
        provider="philips_hue",
        kind="oauth2",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "accessToken": "hue-token",
            "username": "bridge-user",
            "baseUrl": "https://hue.example/route",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return [{"success": {"/lights/1/state/on": True}}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.philips_hue_update_light_state.func(
            light_id="1",
            on=True,
            brightness=200,
            fields_json='{"transitiontime": 4}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result[0]["success"]["/lights/1/state/on"] is True
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://hue.example/route/api/bridge-user/lights/1/state"
    assert captured["headers"]["Authorization"] == "Bearer hue-token"
    assert captured["json_body"] == {"on": True, "bri": 200, "transitiontime": 4}


def test_personal_device_registration_and_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import get_tool_metadata
    from nymeria.tools.personal_device_service_integrations import PERSONAL_DEVICE_SERVICE_TOOLS

    names = {tool.name for tool in PERSONAL_DEVICE_SERVICE_TOOLS}
    assert len(names) == 25
    assert "oura_get_profile" in CATALOG_TOOLS
    assert "strava_create_activity" in CATALOG_TOOLS
    assert "homeassistant_call_service" in CATALOG_TOOLS
    assert "philips_hue_update_light_state" in CATALOG_TOOLS
    assert get_tool_metadata("oura_get_profile").security_level.value == "safe"
    assert get_tool_metadata("homeassistant_call_service").security_level.value == "moderate"
    assert get_tool_metadata("philips_hue_update_light_state").security_level.value == "moderate"


def test_personal_device_tool_schemas_hide_config():
    from nymeria.tools.personal_device_service_integrations import (
        homeassistant_get_state,
        oura_get_daily_sleep,
        philips_hue_update_light_state,
        strava_create_activity,
    )

    assert "config" not in oura_get_daily_sleep.args
    assert "config" not in strava_create_activity.args
    assert "config" not in homeassistant_get_state.args
    assert "config" not in philips_hue_update_light_state.args
