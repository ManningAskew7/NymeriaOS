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


def test_bamboohr_get_employee_uses_env_auth(monkeypatch):
    from nymeria.tools import time_hr_service_integrations as tools

    monkeypatch.setenv("BAMBOOHR_API_KEY", "bamboo-key")
    monkeypatch.setenv("BAMBOOHR_SUBDOMAIN", "acme")
    monkeypatch.setenv("BAMBOOHR_BASE_URL", "https://bamboo.example/gateway")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "42", "displayName": "Ada Lovelace"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.bamboohr_get_employee.func("42", fields="displayName,workEmail"))

    assert result["displayName"] == "Ada Lovelace"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://bamboo.example/gateway/acme/v1/employees/42"
    assert captured["params"] == {"fields": "displayName,workEmail"}
    expected = base64.b64encode(b"bamboo-key:x").decode("ascii")
    assert captured["headers"]["Authorization"] == f"Basic {expected}"


def test_bamboohr_create_employee_uses_vault_credentials(tmp_path, monkeypatch):
    from nymeria.tools import time_hr_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="BambooHR",
        provider="bamboohr",
        kind="api_key",
        allowed_targets=["native_tool:bamboohr_create_employee"],
        secret_fields={
            "apiKey": "bamboo-token",
            "subdomain": "acme",
            "baseUrl": "https://bamboo.example/gateway",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "99"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.bamboohr_create_employee.func(
            first_name="Grace",
            last_name="Hopper",
            fields_json='{"workEmail": "grace@example.com"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "99"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://bamboo.example/gateway/acme/v1/employees"
    assert captured["json_body"]["firstName"] == "Grace"
    assert captured["json_body"]["workEmail"] == "grace@example.com"


def test_beeminder_create_datapoint_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import time_hr_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Beeminder",
        provider="beeminder",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"authToken": "bee-token", "baseUrl": "https://bee.example/api/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "dp1", "value": kwargs["json_body"]["value"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.beeminder_create_datapoint.func(
            goal_slug="steps",
            value=10,
            comment="walk",
            request_id="req-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "dp1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://bee.example/api/v1/users/me/goals/steps/datapoints.json"
    assert captured["params"]["auth_token"] == "bee-token"
    assert captured["json_body"] == {"value": 10, "comment": "walk", "requestid": "req-1"}


def test_clockify_create_time_entry_uses_env_key(monkeypatch):
    from nymeria.tools import time_hr_service_integrations as tools

    monkeypatch.setenv("CLOCKIFY_API_KEY", "clock-key")
    monkeypatch.setenv("CLOCKIFY_BASE_URL", "https://clock.example/api/v1")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "entry-1", "description": kwargs["json_body"]["description"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.clockify_create_time_entry.func(
            workspace_id="workspace-1",
            start="2026-05-14T09:00:00Z",
            end="2026-05-14T10:00:00Z",
            project_id="project-1",
            description="Deep work",
            tag_ids="tag-1, tag-2",
            billable=True,
        )
    )

    assert result["id"] == "entry-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://clock.example/api/v1/workspaces/workspace-1/time-entries"
    assert captured["headers"]["X-Api-Key"] == "clock-key"
    assert captured["json_body"]["tagIds"] == ["tag-1", "tag-2"]
    assert captured["json_body"]["billable"] is True


def test_clockify_missing_key_returns_setup_hint(monkeypatch):
    from nymeria.tools import time_hr_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    result = tools.clockify_list_workspaces.func()

    assert "No Clockify credential found" in result
    assert "CLOCKIFY_API_KEY" in result
    assert "native_tool:clockify_list_workspaces" in result


def test_harvest_list_projects_uses_vault_token_and_account(tmp_path, monkeypatch):
    from nymeria.tools import time_hr_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Harvest",
        provider="harvest",
        kind="api_key",
        allowed_targets=["native_tool:harvest_list_projects"],
        secret_fields={
            "accessToken": "harvest-token",
            "accountId": "123",
            "apiUrl": "https://harvest.example/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"projects": [{"id": 1, "name": "Client work"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.harvest_list_projects.func(
            client_id="45",
            active=True,
            limit=5,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["projects"][0]["name"] == "Client work"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://harvest.example/v2/projects"
    assert captured["params"]["client_id"] == "45"
    assert captured["params"]["is_active"] is True
    assert captured["params"]["per_page"] == 5
    assert captured["headers"]["Authorization"] == "Bearer harvest-token"
    assert captured["headers"]["Harvest-Account-Id"] == "123"


def test_time_hr_registration_and_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import get_tool_metadata
    from nymeria.tools.time_hr_service_integrations import TIME_HR_SERVICE_TOOLS

    names = {tool.name for tool in TIME_HR_SERVICE_TOOLS}
    assert len(names) == 30
    assert "bamboohr_list_employees" in CATALOG_TOOLS
    assert "beeminder_create_datapoint" in CATALOG_TOOLS
    assert "clockify_create_time_entry" in CATALOG_TOOLS
    assert "harvest_list_projects" in CATALOG_TOOLS
    assert get_tool_metadata("bamboohr_list_employees").security_level.value == "safe"
    assert get_tool_metadata("clockify_create_time_entry").security_level.value == "moderate"
    assert get_tool_metadata("harvest_stop_time_entry").security_level.value == "moderate"


def test_time_hr_tool_schemas_hide_config():
    from nymeria.tools.time_hr_service_integrations import (
        bamboohr_get_employee,
        clockify_create_time_entry,
        harvest_create_time_entry,
    )

    assert "config" not in bamboohr_get_employee.args
    assert "config" not in clockify_create_time_entry.args
    assert "config" not in harvest_create_time_entry.args
