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


def test_wordpress_create_record_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("WORDPRESS_URL", "https://wp.example")
    monkeypatch.setenv("WORDPRESS_USERNAME", "alice")
    monkeypatch.setenv("WORDPRESS_PASSWORD", "app-pass")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": 10, "title": {"raw": "Hello"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.wordpress_create_record.func(
            resource="posts",
            fields_json='{"title":"Hello","status":"draft"}',
        )
    )

    expected_auth = base64.b64encode(b"alice:app-pass").decode()
    assert result["id"] == 10
    assert captured["method"] == "POST"
    assert captured["url"] == "https://wp.example/wp-json/wp/v2/posts"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["json_body"] == {"title": "Hello", "status": "draft"}


def test_strapi_create_entry_uses_env_token_and_v4_payload(monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("STRAPI_URL", "https://strapi.example")
    monkeypatch.setenv("STRAPI_API_TOKEN", "strapi-token")
    monkeypatch.setenv("STRAPI_API_VERSION", "v4")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"data": {"id": 1, "attributes": {"title": "Hello"}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.strapi_create_entry.func(
            collection="articles",
            fields_json='{"title":"Hello"}',
        )
    )

    assert result["data"]["id"] == 1
    assert captured["method"] == "POST"
    assert captured["url"] == "https://strapi.example/api/articles"
    assert captured["headers"]["Authorization"] == "Bearer strapi-token"
    assert captured["json_body"] == {"data": {"title": "Hello"}}


def test_contentful_list_records_uses_vault_delivery_token(tmp_path, monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Contentful",
        provider="contentful",
        kind="api_key",
        allowed_targets=["native_tool:contentful_list_records"],
        secret_fields={
            "spaceId": "space-1",
            "ContentDeliveryaccessToken": "delivery-token",
            "base_url": "https://cdn.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"items": [{"sys": {"id": "entry-1"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.contentful_list_records.func(
            resource="entries",
            environment="master",
            content_type="blogPost",
            limit=3,
            query_json='{"order":"-sys.createdAt"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["items"] == [{"sys": {"id": "entry-1"}}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://cdn.example/spaces/space-1/environments/master/entries"
    assert captured["headers"]["Authorization"] == "Bearer delivery-token"
    assert captured["params"]["limit"] == 3
    assert captured["params"]["content_type"] == "blogPost"
    assert captured["params"]["order"] == "-sys.createdAt"


def test_ghost_create_post_uses_env_admin_key(monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("GHOST_URL", "https://ghost.example")
    monkeypatch.setenv("GHOST_ADMIN_API_KEY", "keyid:" + ("a" * 64))
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"posts": [{"id": "post-1", "title": "Hello"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.ghost_create_post.func(
            fields_json='{"title":"Hello","status":"draft"}',
        )
    )

    assert result == [{"id": "post-1", "title": "Hello"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://ghost.example/ghost/api/admin/posts/"
    assert captured["headers"]["Authorization"].startswith("Ghost ")
    assert captured["headers"]["Accept-Version"] == "v5.0"
    assert captured["json_body"] == {"posts": [{"title": "Hello", "status": "draft"}]}


def test_storyblok_publish_story_uses_vault_management_token(tmp_path, monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Storyblok",
        provider="storyblok",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "spaceId": "space-1",
            "accessToken": "storyblok-token",
            "management_base_url": "https://mapi.example/v1",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"story": {"id": 123, "published": True}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.storyblok_publish_story.func(
            story_id="123",
            release_id="rel-1",
            language="en",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == 123
    assert captured["method"] == "GET"
    assert captured["url"] == "https://mapi.example/v1/spaces/space-1/stories/123/publish"
    assert captured["headers"]["Authorization"] == "storyblok-token"
    assert captured["params"]["release_id"] == "rel-1"
    assert captured["params"]["lang"] == "en"


def test_webflow_update_item_uses_vault_token_and_live_path(tmp_path, monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Webflow",
        provider="webflow",
        kind="api_key",
        allowed_targets=["native_tool:webflow_update_collection_item"],
        secret_fields={
            "accessToken": "webflow-token",
            "base_url": "https://webflow.example/v2",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "item-1", "fieldData": json_body["fieldData"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.webflow_update_collection_item.func(
            collection_id="collection-1",
            item_id="item-1",
            field_data_json='{"name":"Homepage"}',
            live=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "item-1"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://webflow.example/v2/collections/collection-1/items/item-1/live"
    assert captured["json_body"] == {"fieldData": {"name": "Homepage"}}
    assert captured["headers"]["Authorization"] == "Bearer webflow-token"


def test_content_management_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import content_management_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("WORDPRESS_URL", "https://wp.example")
    monkeypatch.setenv("STRAPI_URL", "https://strapi.example")
    monkeypatch.setenv("CONTENTFUL_SPACE_ID", "space-1")
    monkeypatch.setenv("GHOST_URL", "https://ghost.example")
    monkeypatch.setenv("STORYBLOK_SPACE_ID", "space-1")

    wordpress_result = tools.wordpress_list_records.func(resource="posts")
    strapi_result = tools.strapi_list_entries.func(collection="articles")
    contentful_result = tools.contentful_list_records.func(resource="entries")
    ghost_result = tools.ghost_list_posts.func()
    storyblok_result = tools.storyblok_publish_story.func(story_id="123")
    webflow_result = tools.webflow_list_sites.func()

    assert 'provider "wordpress"' in wordpress_result
    assert "WORDPRESS_USERNAME + WORDPRESS_PASSWORD" in wordpress_result
    assert 'provider "strapi"' in strapi_result
    assert "STRAPI_API_TOKEN or STRAPI_EMAIL + STRAPI_PASSWORD" in strapi_result
    assert 'provider "contentful"' in contentful_result
    assert "CONTENTFUL_DELIVERY_TOKEN" in contentful_result
    assert 'provider "ghost"' in ghost_result
    assert "GHOST_CONTENT_API_KEY" in ghost_result
    assert 'provider "storyblok"' in storyblok_result
    assert "STORYBLOK_MANAGEMENT_TOKEN" in storyblok_result
    assert 'provider "webflow"' in webflow_result
    assert "WEBFLOW_ACCESS_TOKEN" in webflow_result


def test_content_management_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "wordpress_list_records",
        "wordpress_get_record",
        "strapi_list_entries",
        "strapi_get_entry",
        "contentful_list_records",
        "contentful_get_record",
        "ghost_list_posts",
        "ghost_get_post",
        "storyblok_list_stories",
        "storyblok_get_story",
        "webflow_list_sites",
        "webflow_list_site_collections",
        "webflow_get_collection",
        "webflow_list_collection_items",
        "webflow_get_collection_item",
    ]
    moderate_names = [
        "wordpress_create_record",
        "wordpress_update_record",
        "wordpress_delete_record",
        "strapi_create_entry",
        "strapi_update_entry",
        "strapi_delete_entry",
        "ghost_create_post",
        "ghost_update_post",
        "ghost_delete_post",
        "storyblok_publish_story",
        "storyblok_unpublish_story",
        "storyblok_delete_story",
        "webflow_create_collection_item",
        "webflow_update_collection_item",
        "webflow_delete_collection_item",
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


def test_content_management_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        contentful_list_records,
        ghost_create_post,
        storyblok_publish_story,
        strapi_create_entry,
        webflow_create_collection_item,
        wordpress_create_record,
    )

    assert "config" not in wordpress_create_record.args_schema.model_json_schema()["properties"]
    assert "config" not in strapi_create_entry.args_schema.model_json_schema()["properties"]
    assert "config" not in contentful_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in ghost_create_post.args_schema.model_json_schema()["properties"]
    assert "config" not in storyblok_publish_story.args_schema.model_json_schema()["properties"]
    assert "config" not in webflow_create_collection_item.args_schema.model_json_schema()["properties"]
