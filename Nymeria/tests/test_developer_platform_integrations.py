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


def test_github_search_repositories_builds_request(monkeypatch):
    from nymeria.tools import developer_platform_integrations as tools

    captured = {}

    def fake_request(method, url, params=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {
            "total_count": 1,
            "incomplete_results": False,
            "items": [
                {
                    "full_name": "octocat/Hello-World",
                    "owner": {"login": "octocat"},
                    "description": "Example",
                    "html_url": "https://github.com/octocat/Hello-World",
                    "stargazers_count": 42,
                }
            ],
        }

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.github_search_repositories.func(
            query="tetris language:python",
            sort="stars",
            order="desc",
            limit=5,
        )
    )

    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.github.com/search/repositories"
    assert captured["params"]["q"] == "tetris language:python"
    assert captured["params"]["per_page"] == 5
    assert captured["headers"]["Accept"] == "application/vnd.github+json"
    assert result["total_count"] == 1
    assert result["items"][0]["full_name"] == "octocat/Hello-World"


def test_github_list_issues_filters_pull_requests(monkeypatch):
    from nymeria.tools import developer_platform_integrations as tools

    monkeypatch.setattr(
        tools,
        "_request_json",
        lambda method, url, params=None, headers=None: [
            {
                "number": 1,
                "title": "Issue",
                "state": "open",
                "user": {"login": "alice"},
                "labels": [{"name": "bug"}],
            },
            {
                "number": 2,
                "title": "PR",
                "state": "open",
                "user": {"login": "bob"},
                "pull_request": {"url": "https://api.github.com/pr/2"},
            },
        ],
    )

    result = json.loads(
        tools.github_list_issues.func(
            owner="octocat",
            repo="Hello-World",
            include_pull_requests=False,
        )
    )

    assert result == [
        {
            "number": 1,
            "title": "Issue",
            "state": "open",
            "html_url": None,
            "author": "alice",
            "labels": ["bug"],
            "comments": None,
            "created_at": None,
            "updated_at": None,
            "closed_at": None,
            "is_pull_request": False,
        }
    ]


def test_github_uses_vault_token_and_base_url(tmp_path, monkeypatch):
    from nymeria.tools import developer_platform_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="GitHub Enterprise",
        provider="github",
        kind="api_key",
        allowed_targets=["native_tool:github_get_repository"],
        secret_fields={
            "access_token": "gh-token",
            "base_url": "https://github.example/api/v3",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return {"full_name": "team/repo", "owner": {"login": "team"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.github_get_repository.func(
            owner="team",
            repo="repo",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["full_name"] == "team/repo"
    assert captured["url"] == "https://github.example/api/v3/repos/team/repo"
    assert captured["headers"]["Authorization"] == "Bearer gh-token"


def test_gitlab_project_encodes_path_and_appends_api_base(monkeypatch):
    from nymeria.tools import developer_platform_integrations as tools

    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example")
    captured = {}

    def fake_request(method, url, params=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return {
            "id": 1,
            "path_with_namespace": "group/project",
            "name_with_namespace": "Group / Project",
        }

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.gitlab_get_project.func("group/project"))

    assert result["path_with_namespace"] == "group/project"
    assert captured["url"] == "https://gitlab.example/api/v4/projects/group%2Fproject"


def test_gitlab_uses_vault_token_and_base_url(tmp_path, monkeypatch):
    from nymeria.tools import developer_platform_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="GitLab",
        provider="gitlab",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "private_token": "gl-token",
            "server": "https://gitlab.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return [{"iid": 3, "title": "Issue", "state": "opened", "author": {"username": "alice"}}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.gitlab_list_project_issues.func(
            project="group/project",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result[0]["iid"] == 3
    assert captured["url"] == "https://gitlab.example/api/v4/projects/group%2Fproject/issues"
    assert captured["headers"]["Private-Token"] == "gl-token"


def test_graphql_execute_query_uses_vault_connection(tmp_path, monkeypatch):
    from nymeria.tools import developer_platform_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="GraphQL",
        provider="graphql",
        kind="api_key",
        allowed_targets=["native_tool:graphql_execute_query"],
        secret_fields={
            "endpoint": "https://api.example.com/graphql",
            "bearer_token": "graphql-token",
            "headers_json": '{"X-Workspace": "team-a"}',
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"data": {"viewer": {"login": "alice"}}}

    monkeypatch.setattr(tools, "_request_json_body", fake_request)

    result = json.loads(
        tools.graphql_execute_query.func(
            query="query Viewer($id: ID!) { viewer(id: $id) { login } }",
            variables_json='{"id": "u_1"}',
            operation_name="Viewer",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"data": {"viewer": {"login": "alice"}}}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.example.com/graphql"
    assert captured["json_body"] == {
        "query": "query Viewer($id: ID!) { viewer(id: $id) { login } }",
        "variables": {"id": "u_1"},
        "operationName": "Viewer",
    }
    assert captured["headers"]["Authorization"] == "Bearer graphql-token"
    assert captured["headers"]["X-Workspace"] == "team-a"


def test_developer_platform_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "github_get_repository",
        "github_search_repositories",
        "github_list_issues",
        "github_get_issue",
        "github_list_pull_requests",
        "github_list_releases",
        "github_get_release",
        "gitlab_get_project",
        "gitlab_search_projects",
        "gitlab_list_project_issues",
        "gitlab_get_project_issue",
        "gitlab_list_project_releases",
        "gitlab_get_project_release",
        "gitlab_list_user_projects",
    }
    moderate_names = {"graphql_execute_query"}

    for name in safe_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE
        assert metadata.default_enabled is False

    for name in moderate_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE
        assert metadata.default_enabled is False


def test_developer_platform_tool_schemas_hide_runtime_config():
    from nymeria.tools.developer_platform_integrations import (
        graphql_execute_query,
        github_get_repository,
        gitlab_get_project,
    )

    assert "config" not in graphql_execute_query.args_schema.model_json_schema()["properties"]
    assert "config" not in github_get_repository.args_schema.model_json_schema()["properties"]
    assert "config" not in gitlab_get_project.args_schema.model_json_schema()["properties"]
