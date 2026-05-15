import json


def test_google_business_profile_list_locations_uses_business_information_api(monkeypatch):
    from nymeria.tools import google_business_profile_service_integrations as tools

    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return True, {"locations": [{"name": "locations/1", "title": "Main"}]}

    monkeypatch.setattr(tools, "_profile_request", fake_request)

    result = json.loads(
        tools.google_business_profile_list_locations.func(
            account_name="123",
            read_mask="name,title",
            page_size=12,
            page_token="next",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"name": "locations/1", "title": "Main"}]
    assert captured["user_id"] == "alice"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://mybusinessbusinessinformation.googleapis.com/v1/accounts/123/locations"
    assert captured["params"] == {"readMask": "name,title", "pageSize": 12, "pageToken": "next"}


def test_google_business_profile_reply_to_review_builds_reply_request(monkeypatch):
    from nymeria.tools import google_business_profile_service_integrations as tools

    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return True, {"comment": "Thanks for visiting"}

    monkeypatch.setattr(tools, "_profile_request", fake_request)

    result = json.loads(
        tools.google_business_profile_reply_to_review.func(
            review_name="reviews/rev-1",
            account_name="accounts/123",
            location_name="locations/456",
            comment="Thanks for visiting",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"comment": "Thanks for visiting"}
    assert captured["method"] == "PUT"
    assert (
        captured["url"]
        == "https://mybusiness.googleapis.com/v4/accounts/123/locations/456/reviews/rev-1/reply"
    )
    assert captured["json_body"] == {"comment": "Thanks for visiting"}


def test_google_business_profile_create_post_builds_local_post_body(monkeypatch):
    from nymeria.tools import google_business_profile_service_integrations as tools

    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return True, {"name": "accounts/123/locations/456/localPosts/789"}

    monkeypatch.setattr(tools, "_profile_request", fake_request)

    result = json.loads(
        tools.google_business_profile_create_post.func(
            account_name="accounts/123",
            location_name="456",
            summary="Spring sale",
            topic_type="offer",
            title="20% off",
            start="2026-05-15",
            end="2026-05-20",
            coupon_code="SPRING20",
            redeem_online_url="https://example.com/sale",
            fields_json='{"languageCode":"en"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["name"].endswith("/localPosts/789")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://mybusiness.googleapis.com/v4/accounts/123/locations/456/localPosts"
    body = captured["json_body"]
    assert body["topicType"] == "OFFER"
    assert body["summary"] == "Spring sale"
    assert body["event"]["title"] == "20% off"
    assert body["event"]["schedule"]["startDate"] == {"year": 2026, "month": 5, "day": 15}
    assert body["offer"]["couponCode"] == "SPRING20"
    assert body["offer"]["redeemOnlineUrl"] == "https://example.com/sale"
    assert body["languageCode"] == "en"


def test_google_business_profile_missing_auth_returns_setup_hint(monkeypatch):
    from nymeria.tools import google_business_profile_service_integrations as tools

    monkeypatch.setattr(tools.auth_utils, "get_google_credentials", lambda *args, **kwargs: None)

    success, message = tools._profile_request(
        user_id="alice",
        method="GET",
        url="https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
        account_id=None,
    )

    assert success is False
    assert "No authenticated Google Business Profile account" in message
    assert "google_business_profile_auth_start" in message


def test_google_business_profile_tools_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "google_business_profile_list_accounts",
        "google_business_profile_list_profile_accounts",
        "google_business_profile_list_locations",
        "google_business_profile_list_reviews",
        "google_business_profile_get_review",
        "google_business_profile_list_posts",
        "google_business_profile_get_post",
    ]
    moderate_names = [
        "google_business_profile_auth_start",
        "google_business_profile_auth_complete",
        "google_business_profile_auth_clear",
        "google_business_profile_reply_to_review",
        "google_business_profile_delete_review_reply",
        "google_business_profile_create_post",
        "google_business_profile_update_post",
        "google_business_profile_delete_post",
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


def test_google_business_profile_tool_schemas_hide_runtime_config():
    from nymeria.tools import google_business_profile_create_post

    assert "config" not in google_business_profile_create_post.args_schema.model_json_schema()["properties"]
