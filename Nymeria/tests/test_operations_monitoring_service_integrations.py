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


def test_netlify_list_sites_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Netlify",
        provider="netlify",
        kind="api_key",
        allowed_targets=["native_tool:netlify_list_sites"],
        secret_fields={"access_token": "netlify-token", "base_url": "https://netlify.example/api/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return [{"id": "site-1"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.netlify_list_sites.func(
            limit=3,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"id": "site-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://netlify.example/api/v1/sites"
    assert captured["params"] == {"filter": "all", "per_page": 3}
    assert captured["headers"]["Authorization"] == "Bearer netlify-token"


def test_uptimerobot_list_monitors_uses_env_key(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("UPTIMEROBOT_API_KEY", "ur-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"stat": "ok", "monitors": [{"id": 123}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.uptimerobot_list_monitors.func(
            limit=5,
            monitor_ids="123,456",
            statuses="2,9",
            types="1,3",
            include_logs=True,
            include_response_times=True,
        )
    )

    assert result == [{"id": 123}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.uptimerobot.com/v2/getMonitors"
    assert captured["form_data"]["api_key"] == "ur-key"
    assert captured["form_data"]["format"] == "json"
    assert captured["form_data"]["limit"] == 5
    assert captured["form_data"]["monitors"] == "123-456"
    assert captured["form_data"]["statuses"] == "2-9"
    assert captured["form_data"]["types"] == "1-3"
    assert captured["form_data"]["logs"] == 1
    assert captured["form_data"]["response_times"] == 1


def test_pagerduty_create_incident_uses_env_token_and_from_email(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "pd-token")
    monkeypatch.setenv("PAGERDUTY_FROM_EMAIL", "alice@example.com")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"incident": {"id": "inc-1", "title": "API down"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pagerduty_create_incident.func(
            title="API down",
            service_id="svc-1",
            urgency="high",
            details="500s from edge",
            incident_key="api-down",
        )
    )

    assert result["id"] == "inc-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.pagerduty.com/incidents"
    assert captured["headers"]["Authorization"] == "Token token=pd-token"
    assert captured["headers"]["From"] == "alice@example.com"
    incident = captured["json_body"]["incident"]
    assert incident["title"] == "API down"
    assert incident["service"] == {"id": "svc-1", "type": "service_reference"}
    assert incident["urgency"] == "high"
    assert incident["incident_key"] == "api-down"
    assert incident["body"]["details"] == "500s from edge"


def test_sentry_update_issue_uses_env_token(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sentry-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "issue-1", "status": "resolved"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.sentry_update_issue.func(
            organization_slug="acme",
            issue_id="issue-1",
            status="resolved",
            assigned_to="me",
            fields_json='{"isBookmarked": true}',
        )
    )

    assert result["status"] == "resolved"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://sentry.io/api/0/organizations/acme/issues/issue-1/"
    assert captured["headers"]["Authorization"] == "Bearer sentry-token"
    assert captured["json_body"] == {"isBookmarked": True, "status": "resolved", "assignedTo": "me"}


def test_cloudflare_create_dns_record_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Cloudflare",
        provider="cloudflare",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_token": "cf-token",
            "base_url": "https://cloudflare.example/client/v4",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"success": True, "result": {"id": "record-1"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.cloudflare_create_dns_record.func(
            zone_id="zone-1",
            record_type="a",
            name="api.example.com",
            content="203.0.113.10",
            ttl=120,
            proxied=True,
            fields_json='{"comment":"created by test"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "record-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://cloudflare.example/client/v4/zones/zone-1/dns_records"
    assert captured["headers"]["Authorization"] == "Bearer cf-token"
    assert captured["json_body"] == {
        "type": "A",
        "name": "api.example.com",
        "content": "203.0.113.10",
        "ttl": 120,
        "proxied": True,
        "comment": "created by test",
    }


def test_operations_monitoring_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    netlify_result = tools.netlify_list_sites.func()
    uptimerobot_result = tools.uptimerobot_get_account.func()
    pagerduty_result = tools.pagerduty_list_incidents.func()
    sentry_result = tools.sentry_list_organizations.func()
    cloudflare_result = tools.cloudflare_list_zones.func()

    assert 'provider "netlify"' in netlify_result
    assert "NETLIFY_ACCESS_TOKEN" in netlify_result
    assert 'provider "uptimerobot"' in uptimerobot_result
    assert "UPTIMEROBOT_API_KEY" in uptimerobot_result
    assert 'provider "pagerduty"' in pagerduty_result
    assert "PAGERDUTY_API_TOKEN" in pagerduty_result
    assert 'provider "sentry"' in sentry_result
    assert "SENTRY_AUTH_TOKEN" in sentry_result
    assert 'provider "cloudflare"' in cloudflare_result
    assert "CLOUDFLARE_API_TOKEN" in cloudflare_result


def test_operations_monitoring_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "netlify_list_sites",
        "netlify_get_site",
        "netlify_list_deploys",
        "netlify_get_deploy",
        "uptimerobot_get_account",
        "uptimerobot_list_monitors",
        "uptimerobot_get_monitor",
        "pagerduty_list_incidents",
        "pagerduty_get_incident",
        "pagerduty_list_services",
        "pagerduty_get_user",
        "sentry_list_organizations",
        "sentry_list_projects",
        "sentry_list_project_issues",
        "sentry_get_issue",
        "sentry_list_project_events",
        "sentry_get_event",
        "cloudflare_list_zones",
        "cloudflare_list_dns_records",
        "cloudflare_list_origin_certificates",
        "cloudflare_get_origin_certificate",
    ]
    moderate_names = [
        "netlify_cancel_deploy",
        "netlify_delete_site",
        "uptimerobot_create_monitor",
        "uptimerobot_update_monitor",
        "uptimerobot_delete_monitor",
        "uptimerobot_reset_monitor",
        "pagerduty_create_incident",
        "pagerduty_update_incident",
        "pagerduty_add_incident_note",
        "sentry_update_issue",
        "cloudflare_create_dns_record",
        "cloudflare_update_dns_record",
        "cloudflare_delete_dns_record",
        "cloudflare_upload_origin_certificate",
        "cloudflare_delete_origin_certificate",
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


def test_operations_monitoring_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        cloudflare_create_dns_record,
        netlify_list_sites,
        pagerduty_create_incident,
        sentry_update_issue,
        uptimerobot_list_monitors,
    )

    assert "config" not in netlify_list_sites.args_schema.model_json_schema()["properties"]
    assert "config" not in uptimerobot_list_monitors.args_schema.model_json_schema()["properties"]
    assert "config" not in pagerduty_create_incident.args_schema.model_json_schema()["properties"]
    assert "config" not in sentry_update_issue.args_schema.model_json_schema()["properties"]
    assert "config" not in cloudflare_create_dns_record.args_schema.model_json_schema()["properties"]
