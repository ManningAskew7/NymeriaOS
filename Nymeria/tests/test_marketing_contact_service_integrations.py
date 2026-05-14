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


def test_activecampaign_list_contacts_uses_env_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("ACTIVECAMPAIGN_API_KEY", "active-key")
    monkeypatch.setenv("ACTIVECAMPAIGN_BASE_URL", "https://acme.api-us1.com")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"contacts": [{"id": "1", "email": "ada@example.com"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.activecampaign_list_contacts.func(search="ada", limit=5))

    assert result["contacts"][0]["email"] == "ada@example.com"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://acme.api-us1.com/api/3/contacts"
    assert captured["params"]["search"] == "ada"
    assert captured["params"]["limit"] == 5
    assert captured["headers"]["Api-Token"] == "active-key"


def test_convertkit_subscribe_uses_vault_secret(tmp_path, monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="ConvertKit",
        provider="convertkit",
        kind="api_key",
        allowed_targets=["native_tool:convertkit_add_subscriber_to_form"],
        secret_fields={"apiSecret": "kit-secret", "baseUrl": "https://kit.example/v3"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"subscription": {"subscriber": {"email_address": kwargs["json_body"]["email"]}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.convertkit_add_subscriber_to_form.func(
            form_id="form-1",
            email="ada@example.com",
            first_name="Ada",
            fields_json='{"role": "engineer"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["subscription"]["subscriber"]["email_address"] == "ada@example.com"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://kit.example/v3/forms/form-1/subscribe"
    assert captured["json_body"]["api_secret"] == "kit-secret"
    assert captured["json_body"]["fields"] == {"role": "engineer"}


def test_getresponse_missing_key_returns_setup_hint(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    result = tools.getresponse_list_campaigns.func()

    assert "No GetResponse credential found" in result
    assert "GETRESPONSE_API_KEY" in result
    assert "native_tool:getresponse_list_campaigns" in result


def test_mailerlite_create_subscriber_uses_classic_header(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("MAILERLITE_API_KEY", "lite-key")
    monkeypatch.setenv("MAILERLITE_CLASSIC_API", "true")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"email": kwargs["json_body"]["email"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailerlite_create_subscriber.func(
            email="ada@example.com",
            name="Ada",
            fields_json='{"company": "Analytical Engines"}',
            groups="g1,g2",
        )
    )

    assert result["data"]["email"] == "ada@example.com"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mailerlite.com/api/v2/subscribers"
    assert captured["headers"]["X-MailerLite-ApiKey"] == "lite-key"
    assert captured["json_body"]["groups"] == ["g1", "g2"]


def test_customerio_uses_app_and_tracking_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("CUSTOMERIO_APP_API_KEY", "app-key")
    monkeypatch.setenv("CUSTOMERIO_TRACKING_SITE_ID", "site-1")
    monkeypatch.setenv("CUSTOMERIO_TRACKING_API_KEY", "track-key")
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append({"method": method, "url": url, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    assert json.loads(tools.customerio_list_campaigns.func())["ok"] is True
    assert json.loads(tools.customerio_track_event.func(customer_id="cust-1", event_name="signed_up"))["ok"] is True

    assert captured[0]["method"] == "GET"
    assert captured[0]["url"] == "https://api.customer.io/v1/campaigns"
    assert captured[0]["headers"]["Authorization"] == "Bearer app-key"
    assert captured[1]["method"] == "POST"
    assert captured[1]["url"] == "https://track.customer.io/api/v1/customers/cust-1/events"
    expected_basic = base64.b64encode(b"site-1:track-key").decode()
    assert captured[1]["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert captured[1]["json_body"]["name"] == "signed_up"


def test_iterable_track_event_uses_bulk_endpoint(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("ITERABLE_API_KEY", "iter-key")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"code": "Success"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.iterable_track_event.func(
            event_name="signed_up",
            email="ada@example.com",
            data_fields_json='{"plan": "pro"}',
            campaign_id=42,
        )
    )

    assert result["code"] == "Success"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.iterable.com/api/events/trackBulk"
    assert captured["headers"]["Api_Key"] == "iter-key"
    assert captured["json_body"]["events"][0]["email"] == "ada@example.com"
    assert captured["json_body"]["events"][0]["dataFields"] == {"plan": "pro"}
    assert captured["json_body"]["events"][0]["campaignId"] == 42


def test_posthog_capture_event_includes_project_key(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("POSTHOG_API_KEY", "ph-key")
    monkeypatch.setenv("POSTHOG_BASE_URL", "https://posthog.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.posthog_capture_event.func(
            event_name="signed_up",
            distinct_id="user-1",
            properties_json='{"plan": "pro"}',
        )
    )

    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://posthog.example/capture"
    assert captured["json_body"]["api_key"] == "ph-key"
    assert captured["json_body"]["properties"]["distinct_id"] == "user-1"
    assert captured["json_body"]["properties"]["plan"] == "pro"


def test_segment_identify_uses_basic_write_key(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("SEGMENT_WRITE_KEY", "seg-write")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"success": True}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.segment_identify.func(
            user_id="user-1",
            traits_json='{"email": "ada@example.com"}',
        )
    )

    assert result["success"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.segment.io/v1/identify"
    expected_basic = base64.b64encode(b"seg-write:").decode()
    assert captured["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert captured["json_body"]["userId"] == "user-1"
    assert captured["json_body"]["traits"]["email"] == "ada@example.com"


def test_new_marketing_missing_keys_return_setup_hints(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    assert "CUSTOMERIO_APP_API_KEY" in tools.customerio_list_campaigns.func()
    assert "CUSTOMERIO_TRACKING_SITE_ID" in tools.customerio_track_event.func("cust-1", "signed_up")
    assert "ITERABLE_API_KEY" in tools.iterable_list_lists.func()
    assert "POSTHOG_API_KEY" in tools.posthog_capture_event.func("signed_up", "user-1")
    assert "SEGMENT_WRITE_KEY" in tools.segment_identify.func(user_id="user-1")


def test_marketing_contact_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "customerio_list_campaigns",
        "customerio_get_campaign",
        "iterable_list_lists",
        "iterable_get_user",
        "activecampaign_list_contacts",
        "activecampaign_get_contact",
        "activecampaign_list_lists",
        "activecampaign_list_tags",
        "convertkit_get_account",
        "convertkit_list_forms",
        "convertkit_list_tags",
        "convertkit_list_subscribers",
        "getresponse_list_campaigns",
        "getresponse_list_contacts",
        "getresponse_get_contact",
        "mailerlite_list_subscribers",
        "mailerlite_get_subscriber",
        "mailerlite_list_groups",
    ]
    moderate_names = [
        "activecampaign_sync_contact",
        "activecampaign_update_contact",
        "activecampaign_add_contact_to_list",
        "activecampaign_add_contact_tag",
        "convertkit_add_subscriber_to_form",
        "convertkit_add_subscriber_to_tag",
        "getresponse_create_contact",
        "getresponse_update_contact",
        "getresponse_delete_contact",
        "mailerlite_create_subscriber",
        "mailerlite_update_subscriber",
        "customerio_upsert_customer",
        "customerio_track_event",
        "customerio_track_anonymous_event",
        "customerio_update_segment",
        "iterable_upsert_user",
        "iterable_track_event",
        "iterable_update_list_subscribers",
        "posthog_capture_event",
        "posthog_identify",
        "posthog_create_alias",
        "posthog_track_page_or_screen",
        "segment_identify",
        "segment_track",
        "segment_group",
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


def test_marketing_contact_tool_schemas_hide_runtime_config():
    from nymeria.tools.marketing_contact_service_integrations import (
        activecampaign_sync_contact,
        convertkit_add_subscriber_to_form,
        customerio_track_event,
        mailerlite_create_subscriber,
        posthog_capture_event,
        segment_track,
    )

    assert "config" not in activecampaign_sync_contact.args
    assert "config" not in convertkit_add_subscriber_to_form.args
    assert "config" not in customerio_track_event.args
    assert "config" not in mailerlite_create_subscriber.args
    assert "config" not in posthog_capture_event.args
    assert "config" not in segment_track.args
