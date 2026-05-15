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


def test_jira_search_issues_uses_env_basic_auth_and_search_endpoint(monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("JIRA_EMAIL", "alice@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example")
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
        return {"issues": [{"key": "NYM-1", "fields": {"summary": "Ship"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.jira_search_issues.func(
            jql="project = NYM ORDER BY updated DESC",
            fields="summary,status",
            limit=3,
        )
    )

    expected_auth = base64.b64encode(b"alice@example.com:jira-token").decode()
    assert result == [{"key": "NYM-1", "fields": {"summary": "Ship"}}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://jira.example/rest/api/3/search/jql"
    assert captured["json_body"]["jql"] == "project = NYM ORDER BY updated DESC"
    assert captured["json_body"]["fields"] == ["summary", "status"]
    assert captured["json_body"]["maxResults"] == 3
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_jira_create_issue_uses_vault_bearer_and_adf_description(tmp_path, monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Jira",
        provider="jira",
        kind="oauth_token",
        allowed_targets=["native_tool:jira_create_issue"],
        secret_fields={
            "access_token": "jira-bearer",
            "base_url": "https://jira.example",
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
        return {"key": "NYM-2", "fields": json_body["fields"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.jira_create_issue.func(
            summary="Ship project management tools",
            project_key="NYM",
            issue_type_name="Task",
            description="Create native tools.",
            labels="integrations, backend",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    fields = captured["json_body"]["fields"]
    assert result["key"] == "NYM-2"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://jira.example/rest/api/3/issue"
    assert captured["headers"]["Authorization"] == "Bearer jira-bearer"
    assert fields["project"] == {"key": "NYM"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["labels"] == ["integrations", "backend"]
    assert fields["description"]["type"] == "doc"
    assert fields["description"]["content"][0]["content"][0]["text"] == "Create native tools."


def test_jira_missing_base_or_auth_returns_setup_hint(monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    result = tools.jira_get_myself.func()

    assert "No Jira base URL found" in result
    assert "JIRA_BASE_URL" in result

    monkeypatch.setattr(
        tools,
        "_settings_value",
        lambda name: "https://jira.example" if name == "jira_base_url" else None,
    )

    result = tools.jira_get_myself.func()

    assert "No Jira credential found" in result
    assert "JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_BASE_URL" in result
    assert "native_tool:jira_get_myself" in result


def test_clickup_list_tasks_uses_env_token_and_filters(monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("CLICKUP_ACCESS_TOKEN", "clickup-token")
    monkeypatch.setenv("CLICKUP_BASE_URL", "https://clickup.example/api/v2")
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
        return {"tasks": [{"id": "task-1", "name": "Roadmap"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.clickup_list_tasks.func(
            list_id="list-1",
            include_closed=True,
            subtasks=True,
            statuses="open,review",
            assignees="101, 202, not-an-id",
            tags="backend, integrations",
            page=2,
        )
    )

    assert result == [{"id": "task-1", "name": "Roadmap"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://clickup.example/api/v2/list/list-1/task"
    assert captured["headers"]["Authorization"] == "clickup-token"
    assert captured["params"]["include_closed"] is True
    assert captured["params"]["subtasks"] is True
    assert captured["params"]["statuses[]"] == ["open", "review"]
    assert captured["params"]["assignees[]"] == [101, 202]
    assert captured["params"]["tags[]"] == ["backend", "integrations"]
    assert captured["params"]["page"] == 2


def test_clickup_create_task_uses_vault_token_and_epoch_body(tmp_path, monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="ClickUp",
        provider="clickup",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "token": "clickup-token",
            "base_url": "https://clickup.example/api/v2",
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
        return {"id": "task-1", "name": json_body["name"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.clickup_create_task.func(
            list_id="list-1",
            name="Follow up",
            markdown_content="Check **status**",
            assignees="101, 202",
            tags="backend, integrations",
            priority=2,
            due_date="2026-05-14T00:00:00Z",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "task-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://clickup.example/api/v2/list/list-1/task"
    assert captured["headers"]["Authorization"] == "clickup-token"
    assert captured["json_body"]["name"] == "Follow up"
    assert captured["json_body"]["markdown_content"] == "Check **status**"
    assert "description" not in captured["json_body"]
    assert captured["json_body"]["assignees"] == [101, 202]
    assert captured["json_body"]["tags"] == ["backend", "integrations"]
    assert captured["json_body"]["priority"] == 2
    assert isinstance(captured["json_body"]["due_date"], int)


def test_monday_create_item_uses_env_token_and_graphql_json(monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MONDAY_API_TOKEN", "monday-token")
    monkeypatch.setenv("MONDAY_API_URL", "https://monday.example/v2")
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
        return {"data": {"create_item": {"id": "item-1", "name": "Follow up"}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.monday_create_item.func(
            board_id="board-1",
            group_id="topics",
            item_name="Follow up",
            column_values_json='{"status": {"label": "Working on it"}}',
        )
    )

    assert result == {"id": "item-1", "name": "Follow up"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://monday.example/v2"
    assert captured["headers"]["Authorization"] == "Bearer monday-token"
    assert captured["headers"]["API-Version"] == "2023-10"
    assert "create_item" in captured["json_body"]["query"]
    assert captured["json_body"]["variables"]["boardId"] == "board-1"
    assert captured["json_body"]["variables"]["columnValues"] == '{"status": {"label": "Working on it"}}'


def test_taiga_update_record_logs_in_and_includes_version(monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TAIGA_BASE_URL", "https://taiga.example")
    monkeypatch.setenv("TAIGA_USERNAME", "alice")
    monkeypatch.setenv("TAIGA_PASSWORD", "taiga-pass")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        if url.endswith("/auth"):
            return {"auth_token": "taiga-token"}
        if method == "GET":
            return {"id": 42, "version": 7}
        return {"id": 42, "subject": json_body["subject"], "version": json_body["version"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.taiga_update_record.func(
            resource="task",
            record_id="42",
            fields_json='{"subject": "Ship docs"}',
        )
    )

    assert result == {"id": 42, "subject": "Ship docs", "version": 7}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://taiga.example/api/v1/auth"
    assert calls[0]["json_body"] == {"type": "normal", "username": "alice", "password": "taiga-pass"}
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == "https://taiga.example/api/v1/tasks/42"
    assert calls[2]["method"] == "PATCH"
    assert calls[2]["url"] == "https://taiga.example/api/v1/tasks/42"
    assert calls[2]["headers"]["Authorization"] == "Bearer taiga-token"
    assert calls[2]["json_body"] == {"subject": "Ship docs", "version": 7}


def test_wekan_create_card_uses_vault_token_and_instance_url(tmp_path, monkeypatch):
    from nymeria.tools import project_management_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Wekan",
        provider="wekan",
        kind="api_key",
        allowed_targets=["native_tool:wekan_create_card"],
        secret_fields={
            "token": "wekan-token",
            "url": "https://wekan.example",
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
        return {"_id": "card-1", "title": json_body["title"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.wekan_create_card.func(
            board_id="board-1",
            list_id="list-1",
            title="Native PM batch",
            author_id="alice",
            fields_json='{"description": "Use saved connection"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"_id": "card-1", "title": "Native PM batch"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://wekan.example/api/boards/board-1/lists/list-1/cards"
    assert captured["headers"]["Authorization"] == "Bearer wekan-token"
    assert captured["json_body"] == {
        "description": "Use saved connection",
        "title": "Native PM batch",
        "authorId": "alice",
    }


def test_project_management_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "jira_get_myself",
        "jira_list_projects",
        "jira_search_issues",
        "jira_get_issue",
        "jira_list_issue_transitions",
        "jira_list_users",
        "jira_list_issue_comments",
        "clickup_list_teams",
        "clickup_list_spaces",
        "clickup_list_folders",
        "clickup_list_lists",
        "clickup_get_task",
        "clickup_list_tasks",
        "clickup_list_task_comments",
        "monday_get_me",
        "monday_list_boards",
        "monday_get_board",
        "monday_list_board_columns",
        "monday_list_board_groups",
        "monday_list_items",
        "monday_get_item",
        "taiga_list_projects",
        "taiga_list_records",
        "taiga_get_record",
        "wekan_get_current_user",
        "wekan_list_users",
        "wekan_list_user_boards",
        "wekan_get_board",
        "wekan_list_lists",
        "wekan_list_cards",
        "wekan_get_card",
        "wekan_list_card_comments",
    }
    moderate_names = {
        "jira_create_issue",
        "jira_update_issue",
        "jira_add_issue_comment",
        "clickup_create_task",
        "clickup_update_task",
        "clickup_add_task_comment",
        "monday_create_board",
        "monday_archive_board",
        "monday_create_board_column",
        "monday_create_board_group",
        "monday_create_item",
        "monday_update_item_columns",
        "monday_add_item_update",
        "monday_move_item",
        "monday_delete_item",
        "taiga_create_record",
        "taiga_update_record",
        "taiga_delete_record",
        "wekan_create_board",
        "wekan_delete_board",
        "wekan_create_list",
        "wekan_delete_list",
        "wekan_create_card",
        "wekan_update_card",
        "wekan_delete_card",
        "wekan_add_card_comment",
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


def test_project_management_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.project_management_service_integrations import (
        clickup_create_task,
        jira_search_issues,
        monday_create_item,
        taiga_list_records,
        wekan_create_card,
    )

    assert "config" not in jira_search_issues.args_schema.model_json_schema()["properties"]
    assert "config" not in clickup_create_task.args_schema.model_json_schema()["properties"]
    assert "config" not in monday_create_item.args_schema.model_json_schema()["properties"]
    assert "config" not in taiga_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in wekan_create_card.args_schema.model_json_schema()["properties"]
