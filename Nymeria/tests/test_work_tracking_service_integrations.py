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


def test_asana_list_tasks_uses_project_endpoint_and_env_token(monkeypatch):
    from nymeria.tools import work_tracking_service_integrations as tools

    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "asana-token")
    monkeypatch.setenv("ASANA_BASE_URL", "https://asana.example/api/1.0")
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
        return {"data": [{"gid": "task-1", "name": "Ship"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.asana_list_tasks.func(
            project_gid="project-1",
            workspace_gid="workspace-1",
            limit=3,
            opt_fields="gid,name",
        )
    )

    assert result == [{"gid": "task-1", "name": "Ship"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://asana.example/api/1.0/projects/project-1/tasks"
    assert captured["params"]["workspace"] == "workspace-1"
    assert captured["params"]["limit"] == 3
    assert captured["params"]["opt_fields"] == "gid,name"
    assert captured["headers"]["Authorization"] == "Bearer asana-token"


def test_asana_create_task_uses_vault_token_and_data_payload(tmp_path, monkeypatch):
    from nymeria.tools import work_tracking_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Asana",
        provider="asana",
        kind="api_key",
        allowed_targets=["native_tool:asana_create_task"],
        secret_fields={
            "access_token": "asana-token",
            "base_url": "https://asana.example/api/1.0",
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
        return {"data": {"gid": "task-1", "name": json_body["data"]["name"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.asana_create_task.func(
            name="Ship tools",
            workspace_gid="workspace-1",
            notes="Native integration batch",
            projects="project-1, project-2",
            assignee="me",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["gid"] == "task-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://asana.example/api/1.0/tasks"
    assert captured["headers"]["Authorization"] == "Bearer asana-token"
    assert captured["json_body"]["data"]["name"] == "Ship tools"
    assert captured["json_body"]["data"]["projects"] == ["project-1", "project-2"]
    assert captured["json_body"]["data"]["assignee"] == "me"


def test_asana_missing_token_returns_setup_hint(monkeypatch):
    from nymeria.tools import work_tracking_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    result = tools.asana_get_task.func("task-1")

    assert "No Asana credential found" in result
    assert "ASANA_ACCESS_TOKEN" in result
    assert "native_tool:asana_get_task" in result


def test_linear_list_issues_uses_env_key_and_graphql_filter(monkeypatch):
    from nymeria.tools import work_tracking_service_integrations as tools

    monkeypatch.setenv("LINEAR_API_KEY", "linear-key")
    monkeypatch.setenv("LINEAR_API_URL", "https://linear.example/graphql")
    captured = {}

    def fake_graphql(api_url, headers, query, variables=None):
        captured.update(
            {
                "api_url": api_url,
                "headers": headers,
                "query": query,
                "variables": variables,
            }
        )
        return {"issues": {"nodes": [{"id": "issue-1", "title": "Roadmap"}]}}

    monkeypatch.setattr(tools, "_linear_graphql", fake_graphql)

    result = json.loads(
        tools.linear_list_issues.func(
            team_id="team-1",
            assignee_id="user-1",
            state_id="state-1",
            limit=4,
        )
    )

    assert result == [{"id": "issue-1", "title": "Roadmap"}]
    assert captured["api_url"] == "https://linear.example/graphql"
    assert captured["headers"]["Authorization"] == "linear-key"
    assert captured["variables"]["first"] == 4
    assert captured["variables"]["filter"]["team"]["id"]["eq"] == "team-1"
    assert captured["variables"]["filter"]["assignee"]["id"]["eq"] == "user-1"
    assert captured["variables"]["filter"]["state"]["id"]["eq"] == "state-1"


def test_linear_create_issue_uses_vault_key_and_input(tmp_path, monkeypatch):
    from nymeria.tools import work_tracking_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Linear",
        provider="linear",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "linear-key",
            "api_url": "https://linear.example/graphql",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_graphql(api_url, headers, query, variables=None):
        captured.update(
            {
                "api_url": api_url,
                "headers": headers,
                "query": query,
                "variables": variables,
            }
        )
        return {"issueCreate": {"success": True, "issue": {"id": "issue-1", "title": "Follow up"}}}

    monkeypatch.setattr(tools, "_linear_graphql", fake_graphql)

    result = json.loads(
        tools.linear_create_issue.func(
            team_id="team-1",
            title="Follow up",
            description="Check status",
            assignee_id="user-1",
            priority=2,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["success"] is True
    assert captured["api_url"] == "https://linear.example/graphql"
    assert captured["headers"]["Authorization"] == "linear-key"
    assert captured["variables"]["input"]["teamId"] == "team-1"
    assert captured["variables"]["input"]["title"] == "Follow up"
    assert captured["variables"]["input"]["assigneeId"] == "user-1"
    assert captured["variables"]["input"]["priority"] == 2


def test_work_tracking_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "asana_get_user",
        "asana_list_users",
        "asana_list_projects",
        "asana_get_project",
        "asana_list_tasks",
        "asana_search_tasks",
        "asana_get_task",
        "linear_list_teams",
        "linear_list_users",
        "linear_list_workflow_states",
        "linear_list_issues",
        "linear_get_issue",
    }
    moderate_names = {
        "asana_create_project",
        "asana_update_project",
        "asana_create_task",
        "asana_create_subtask",
        "asana_update_task",
        "asana_add_task_comment",
        "linear_create_issue",
        "linear_update_issue",
        "linear_add_issue_comment",
        "linear_add_issue_link",
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


def test_work_tracking_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.work_tracking_service_integrations import (
        asana_list_tasks,
        linear_create_issue,
    )

    assert "config" not in asana_list_tasks.args_schema.model_json_schema()["properties"]
    assert "config" not in linear_create_issue.args_schema.model_json_schema()["properties"]
