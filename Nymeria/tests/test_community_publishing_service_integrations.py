import json

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings
    from nymeria.tools import community_publishing_service_integrations as tools

    get_settings.cache_clear()
    tools._REDDIT_TOKEN_CACHE.clear()
    yield
    get_settings.cache_clear()
    tools._REDDIT_TOKEN_CACHE.clear()


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_reddit_search_uses_public_json_without_credentials(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"children": [{"data": {"id": "abc"}}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.reddit_search_posts.func(query="python", subreddit="learnpython", limit=3))

    assert result["data"]["children"][0]["data"]["id"] == "abc"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://www.reddit.com/r/learnpython/search.json"
    assert captured["params"]["q"] == "python"
    assert captured["params"]["restrict_sr"] is True
    assert captured["params"]["limit"] == 3


def test_reddit_create_comment_uses_vault_access_token(tmp_path, monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Reddit",
        provider="reddit",
        kind="oauth2",
        allowed_targets=["native_tool:*"],
        secret_fields={"accessToken": "reddit-token", "baseUrl": "https://oauth.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"json": {"data": {"things": [{"kind": "t1"}]}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.reddit_create_comment.func(
            parent_fullname="t3_abc",
            text="hello",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["json"]["data"]["things"][0]["kind"] == "t1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://oauth.example/api/comment"
    assert captured["headers"]["Authorization"] == "Bearer reddit-token"
    assert captured["form_data"]["thing_id"] == "t3_abc"
    assert captured["form_data"]["text"] == "hello"


def test_reddit_listing_can_use_client_credentials(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "reddit-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "reddit-secret")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if url == "https://www.reddit.com/api/v1/access_token":
            return {"access_token": "app-token", "expires_in": 3600}
        return {"data": {"children": []}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.reddit_list_subreddit_posts.func(subreddit="python", listing="new", limit=2))

    assert result["data"]["children"] == []
    assert calls[0]["method"] == "POST"
    assert calls[0]["form_data"] == {"grant_type": "client_credentials"}
    assert calls[1]["url"] == "https://oauth.reddit.com/r/python/new"
    assert calls[1]["headers"]["Authorization"] == "Bearer app-token"
    assert calls[1]["params"]["limit"] == 2


def test_discourse_search_uses_env_base_without_auth(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("DISCOURSE_BASE_URL", "https://forum.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"topics": [{"id": 1, "title": "API"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.discourse_search.func(query="api", page=2))

    assert result["topics"][0]["title"] == "API"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://forum.example/search.json"
    assert captured["params"] == {"q": "api", "page": 2}
    assert "Api-Key" not in captured["headers"]


def test_discourse_create_topic_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Discourse",
        provider="discourse",
        kind="api_key",
        allowed_targets=["native_tool:discourse_create_topic"],
        secret_fields={"apiKey": "discourse-key", "apiUsername": "system", "baseUrl": "https://forum.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"topic_id": 10, "post_number": 1}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.discourse_create_topic.func(
            title="Launch",
            raw="hello",
            category_id=3,
            tags="api, launch",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["topic_id"] == 10
    assert captured["method"] == "POST"
    assert captured["url"] == "https://forum.example/posts.json"
    assert captured["headers"]["Api-Key"] == "discourse-key"
    assert captured["headers"]["Api-Username"] == "system"
    assert captured["json_body"]["category"] == 3
    assert captured["json_body"]["tags"] == ["api", "launch"]


def test_medium_list_publications_resolves_user_from_me(tmp_path, monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Medium",
        provider="medium",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"accessToken": "medium-token", "apiUrl": "https://medium.example/v1"},
        created_by_user_id="alice",
    )
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if url == "https://medium.example/v1/me":
            return {"data": {"id": "user-1"}}
        return {"data": [{"id": "pub-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.medium_list_publications.func(config={"configurable": {"user_id": "alice"}})
    )

    assert result["data"][0]["id"] == "pub-1"
    assert calls[0]["url"] == "https://medium.example/v1/me"
    assert calls[1]["url"] == "https://medium.example/v1/users/user-1/publications"
    assert calls[1]["headers"]["Authorization"] == "Bearer medium-token"


def test_medium_create_publication_post_sends_body(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MEDIUM_ACCESS_TOKEN", "medium-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"id": "post-1", "publishStatus": "draft"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.medium_create_publication_post.func(
            publication_id="pub-1",
            title="Hello",
            content="# Hello",
            content_format="markdown",
            tags="api, automation, nymeria, ignored",
            publish_status="draft",
            canonical_url="https://example.com/hello",
        )
    )

    assert result["data"]["id"] == "post-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.medium.com/v1/publications/pub-1/posts"
    assert captured["headers"]["Authorization"] == "Bearer medium-token"
    assert captured["json_body"]["tags"] == ["api", "automation", "nymeria"]
    assert captured["json_body"]["canonicalUrl"] == "https://example.com/hello"


def test_community_publishing_registration_and_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.community_publishing_service_integrations import COMMUNITY_PUBLISHING_SERVICE_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    names = {tool.name for tool in COMMUNITY_PUBLISHING_SERVICE_TOOLS}
    assert {
        "reddit_search_posts",
        "reddit_create_post",
        "discourse_search",
        "discourse_create_topic",
        "medium_get_me",
        "medium_create_post",
    } <= names
    assert names <= set(OPTIONAL_TOOLS)
    assert get_tool_metadata("reddit_search_posts").category == ToolCategory.INTEGRATIONS
    assert get_tool_metadata("reddit_search_posts").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("discourse_search").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("medium_get_me").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("reddit_create_post").security_level == SecurityLevel.MODERATE
    assert get_tool_metadata("medium_create_post").security_level == SecurityLevel.MODERATE


def test_community_publishing_tool_schemas_hide_config():
    from nymeria.tools import community_publishing_service_integrations as tools

    for tool in tools.COMMUNITY_PUBLISHING_SERVICE_TOOLS:
        schema = tool.args_schema.model_json_schema()
        assert "config" not in schema.get("properties", {})
