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


def test_todoist_list_tasks_uses_filter_endpoint_and_env_token(monkeypatch):
    from nymeria.tools import productivity_service_integrations as tools

    monkeypatch.setenv("TODOIST_API_KEY", "todoist-token")
    monkeypatch.setenv("TODOIST_BASE_URL", "https://todoist.example/api/v1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
                "json_body": json_body,
            }
        )
        return {"results": [{"id": "task-1", "content": "Inbox"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.todoist_list_tasks.func(
            project_id="project-1",
            filter_query="today | overdue",
            limit=3,
        )
    )

    assert result == [{"id": "task-1", "content": "Inbox"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://todoist.example/api/v1/tasks/filter"
    assert captured["params"] == {"query": "today | overdue", "limit": 3}
    assert captured["headers"]["Authorization"] == "Bearer todoist-token"


def test_todoist_create_task_uses_vault_token_and_body(tmp_path, monkeypatch):
    from nymeria.tools import productivity_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Todoist",
        provider="todoist",
        kind="api_key",
        allowed_targets=["native_tool:todoist_create_task"],
        secret_fields={
            "api_key": "todoist-token",
            "base_url": "https://todoist.example/api/v1",
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
        return {"id": "task-1", "content": json_body["content"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.todoist_create_task.func(
            content="Ship tools",
            description="Native integration batch",
            project_id="project-1",
            labels="work, ai",
            priority=2,
            due_string="tomorrow",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "task-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://todoist.example/api/v1/tasks"
    assert captured["headers"]["Authorization"] == "Bearer todoist-token"
    assert captured["json_body"]["content"] == "Ship tools"
    assert captured["json_body"]["labels"] == ["work", "ai"]
    assert captured["json_body"]["priority"] == 2


def test_todoist_missing_token_returns_setup_hint(monkeypatch):
    from nymeria.tools import productivity_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    result = tools.todoist_get_task.func("task-1")

    assert "No Todoist credential found" in result
    assert "TODOIST_API_KEY" in result
    assert "native_tool:todoist_get_task" in result


def test_trello_search_uses_env_key_and_token(monkeypatch):
    from nymeria.tools import productivity_service_integrations as tools

    monkeypatch.setenv("TRELLO_API_KEY", "trello-key")
    monkeypatch.setenv("TRELLO_API_TOKEN", "trello-token")
    monkeypatch.setenv("TRELLO_BASE_URL", "https://trello.example/1")
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
        return {"cards": [{"id": "card-1", "name": "Roadmap"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.trello_search.func(query="roadmap", model_types="cards", limit=4))

    assert result["cards"][0]["id"] == "card-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://trello.example/1/search"
    assert captured["params"]["key"] == "trello-key"
    assert captured["params"]["token"] == "trello-token"
    assert captured["params"]["modelTypes"] == "cards"
    assert captured["params"]["cards_limit"] == 4


def test_trello_create_card_uses_vault_key_token_and_body(tmp_path, monkeypatch):
    from nymeria.tools import productivity_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Trello",
        provider="trello",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "trello-key",
            "api_token": "trello-token",
            "base_url": "https://trello.example/1",
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
        return {"id": "card-1", "name": json_body["name"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.trello_create_card.func(
            list_id="list-1",
            name="Follow up",
            description="Check status",
            labels="label-1,label-2",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "card-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://trello.example/1/cards"
    assert captured["params"] == {"key": "trello-key", "token": "trello-token"}
    assert captured["json_body"]["idList"] == "list-1"
    assert captured["json_body"]["name"] == "Follow up"
    assert captured["json_body"]["idLabels"] == "label-1,label-2"


def test_productivity_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "todoist_list_tasks",
        "todoist_get_task",
        "todoist_list_projects",
        "todoist_get_project",
        "trello_search",
        "trello_get_board",
        "trello_list_board_lists",
        "trello_list_cards",
        "trello_get_card",
    }
    moderate_names = {
        "todoist_create_task",
        "todoist_update_task",
        "todoist_close_task",
        "todoist_create_project",
        "trello_create_card",
        "trello_update_card",
        "trello_add_card_comment",
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


def test_productivity_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.productivity_service_integrations import (
        todoist_list_tasks,
        trello_create_card,
    )

    assert "config" not in todoist_list_tasks.args_schema.model_json_schema()["properties"]
    assert "config" not in trello_create_card.args_schema.model_json_schema()["properties"]
