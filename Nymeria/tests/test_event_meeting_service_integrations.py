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


def test_demio_register_event_uses_vault_credentials_and_extra_fields(tmp_path, monkeypatch):
    from nymeria.tools import event_meeting_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Demio",
        provider="demio",
        kind="api_key",
        allowed_targets=["native_tool:demio_register_event"],
        secret_fields={
            "api_key": "demio-key",
            "api_secret": "demio-secret",
            "base_url": "https://demio.example/api/v1",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"registration_url": "https://join.example"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.demio_register_event.func(
            event_id="evt-1",
            name="Alice",
            email="alice@example.com",
            fields_json='{"company": "Nymeria", "42": "VIP"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"registration_url": "https://join.example"}
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://demio.example/api/v1/event/register"
    assert captured["headers"]["Api-Key"] == "demio-key"
    assert captured["headers"]["Api-Secret"] == "demio-secret"
    assert captured["json_body"] == {
        "id": "evt-1",
        "name": "Alice",
        "email": "alice@example.com",
        "company": "Nymeria",
        "42": "VIP",
    }


def test_zoom_create_meeting_uses_env_bearer_token_and_json_body(monkeypatch):
    from nymeria.tools import event_meeting_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ZOOM_ACCESS_TOKEN", "zoom-token")
    monkeypatch.setenv("ZOOM_BASE_URL", "https://zoom.example/v2")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"id": 123, "topic": json_body["topic"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.zoom_create_meeting.func(
            topic="Planning",
            fields_json='{"duration": 30, "settings": {"join_before_host": true}}',
        )
    )

    assert result == {"id": 123, "topic": "Planning"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://zoom.example/v2/users/me/meetings"
    assert captured["headers"]["Authorization"] == "Bearer zoom-token"
    assert captured["json_body"] == {
        "duration": 30,
        "settings": {"join_before_host": True},
        "topic": "Planning",
    }


def test_gotowebinar_list_webinars_uses_account_key_and_unwraps_embedded(monkeypatch):
    from nymeria.tools import event_meeting_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("GOTOWEBINAR_ACCESS_TOKEN", "goto-token")
    monkeypatch.setenv("GOTOWEBINAR_ACCOUNT_KEY", "acct-1")
    monkeypatch.setenv("GOTOWEBINAR_BASE_URL", "https://goto.example/rest/v2")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
            }
        )
        return {"_embedded": {"webinars": [{"webinarKey": "web-1"}, {"webinarKey": "web-2"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.gotowebinar_list_webinars.func(
            from_time="2026-01-01T00:00:00Z",
            to_time="2026-02-01T00:00:00Z",
            limit=1,
        )
    )

    assert result == [{"webinarKey": "web-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://goto.example/rest/v2/accounts/acct-1/webinars"
    assert captured["headers"]["Authorization"] == "Bearer goto-token"
    assert captured["params"] == {
        "fromTime": "2026-01-01T00:00:00Z",
        "toTime": "2026-02-01T00:00:00Z",
        "limit": 1,
    }


def test_gotowebinar_create_registrant_uses_vault_credentials(tmp_path, monkeypatch):
    from nymeria.tools import event_meeting_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="GoToWebinar",
        provider="gotowebinar",
        kind="oauth",
        allowed_targets=["native_tool:gotowebinar_create_registrant"],
        secret_fields={
            "access_token": "goto-token",
            "organizer_key": "org-1",
            "base_url": "https://goto.example/rest/v2",
        },
        created_by_user_id="alice",
    )
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
        return {"registrantKey": "reg-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.gotowebinar_create_registrant.func(
            webinar_key="web-1",
            first_name="Alice",
            last_name="Example",
            email="alice@example.com",
            fields_json='{"organization": "Nymeria"}',
            resend_confirmation=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"registrantKey": "reg-1"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://goto.example/rest/v2/organizers/org-1/webinars/web-1/registrants"
    assert captured["params"] == {"resendConfirmation": True}
    assert captured["headers"]["Authorization"] == "Bearer goto-token"
    assert captured["json_body"] == {
        "organization": "Nymeria",
        "firstName": "Alice",
        "lastName": "Example",
        "email": "alice@example.com",
        "responses": [],
    }


def test_event_meeting_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "demio_list_events",
        "demio_get_event",
        "demio_get_session_participants",
        "zoom_list_meetings",
        "zoom_get_meeting",
        "gotowebinar_list_webinars",
        "gotowebinar_get_webinar",
        "gotowebinar_list_sessions",
        "gotowebinar_get_session",
        "gotowebinar_list_registrants",
        "gotowebinar_get_registrant",
    }
    moderate_names = {
        "demio_register_event",
        "zoom_create_meeting",
        "zoom_update_meeting",
        "zoom_delete_meeting",
        "gotowebinar_create_webinar",
        "gotowebinar_update_webinar",
        "gotowebinar_create_registrant",
        "gotowebinar_delete_registrant",
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


def test_event_meeting_tool_schemas_hide_runtime_config():
    from nymeria.tools.event_meeting_service_integrations import (
        demio_register_event,
        gotowebinar_create_registrant,
        zoom_create_meeting,
    )

    assert "config" not in demio_register_event.args_schema.model_json_schema()["properties"]
    assert "config" not in zoom_create_meeting.args_schema.model_json_schema()["properties"]
    assert "config" not in gotowebinar_create_registrant.args_schema.model_json_schema()["properties"]
