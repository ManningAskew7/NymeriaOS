import json

import pytest

from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings
    from nymeria.tools import community_publishing_service_integrations as tools

    get_settings.cache_clear()
    tools._REDDIT_TOKEN_CACHE.clear()
    yield
    get_settings.cache_clear()
    tools._REDDIT_TOKEN_CACHE.clear()


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


def test_twitter_create_post_uses_env_token(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "twitter-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"id": "tweet-1", "text": kwargs["json_body"]["text"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.twitter_create_post.func(
            text="Launch update",
            reply_to_tweet_id="https://x.com/me/status/123",
            quote_tweet_id="456",
            media_ids="m1,m2",
        )
    )

    assert result["data"]["id"] == "tweet-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.twitter.com/2/tweets"
    assert captured["headers"]["Authorization"] == "Bearer twitter-token"
    assert captured["json_body"]["text"] == "Launch update"
    assert captured["json_body"]["reply"] == {"in_reply_to_tweet_id": "123"}
    assert captured["json_body"]["quote_tweet_id"] == "456"
    assert captured["json_body"]["media"] == {"media_ids": ["m1", "m2"]}


def test_twitter_like_post_resolves_current_user(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "user-token")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if url.endswith("/users/me"):
            return {"data": {"id": "user-1"}}
        return {"data": {"liked": True}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.twitter_like_post.func(tweet_id="tweet-1"))

    assert result["data"]["liked"] is True
    assert calls[0]["url"] == "https://api.twitter.com/2/users/me"
    assert calls[1]["url"] == "https://api.twitter.com/2/users/user-1/likes"
    assert calls[1]["json_body"] == {"tweet_id": "tweet-1"}


def test_linkedin_create_post_uses_rest_headers(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "linkedin-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "ok", "id": "urn:li:share:1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.linkedin_create_post.func(
            text="New article",
            person_id="person-1",
            visibility="CONNECTIONS",
            article_url="https://example.com/article",
            article_title="Article",
        )
    )

    assert result["id"] == "urn:li:share:1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.linkedin.com/rest/posts"
    assert captured["headers"]["Authorization"] == "Bearer linkedin-token"
    assert captured["headers"]["X-Restli-Protocol-Version"] == "2.0.0"
    assert captured["json_body"]["author"] == "urn:li:person:person-1"
    assert captured["json_body"]["visibility"] == "CONNECTIONS"
    assert captured["json_body"]["content"]["article"]["source"] == "https://example.com/article"


def test_facebook_page_create_post_adds_appsecret_proof(monkeypatch):
    from nymeria.tools import community_publishing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", "facebook-token")
    monkeypatch.setenv("FACEBOOK_APP_SECRET", "secret")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "page-1_post-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.facebook_page_create_post.func(
            page_id="page-1",
            message="hello",
            link="https://example.com",
        )
    )

    assert result["id"] == "page-1_post-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://graph.facebook.com/v23.0/page-1/feed"
    assert captured["headers"]["Authorization"] == "Bearer facebook-token"
    assert captured["form_data"] == {"message": "hello", "link": "https://example.com"}
    assert len(captured["params"]["appsecret_proof"]) == 64


def test_community_publishing_registration_and_metadata():
    from nymeria.tools import CATALOG_TOOLS
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
        "twitter_search_recent",
        "twitter_create_post",
        "linkedin_get_me",
        "linkedin_create_post",
        "facebook_graph_get_node",
        "facebook_page_create_post",
    } <= names
    assert names <= set(CATALOG_TOOLS)
    assert get_tool_metadata("reddit_search_posts").category == ToolCategory.INTEGRATIONS
    assert get_tool_metadata("reddit_search_posts").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("discourse_search").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("medium_get_me").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("twitter_search_recent").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("linkedin_get_me").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("facebook_graph_get_node").security_level == SecurityLevel.SAFE
    assert get_tool_metadata("reddit_create_post").security_level == SecurityLevel.MODERATE
    assert get_tool_metadata("medium_create_post").security_level == SecurityLevel.MODERATE
    assert get_tool_metadata("twitter_create_post").security_level == SecurityLevel.MODERATE
    assert get_tool_metadata("linkedin_create_post").security_level == SecurityLevel.MODERATE
    assert get_tool_metadata("facebook_page_create_post").security_level == SecurityLevel.MODERATE


def test_community_publishing_tool_schemas_hide_config():
    from nymeria.tools import community_publishing_service_integrations as tools

    for tool in tools.COMMUNITY_PUBLISHING_SERVICE_TOOLS:
        schema = tool.args_schema.model_json_schema()
        assert "config" not in schema.get("properties", {})
