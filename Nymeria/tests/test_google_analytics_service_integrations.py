import json


def test_google_analytics_run_report_builds_ga4_request(monkeypatch):
    from nymeria.tools import google_analytics_service_integrations as tools

    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return True, {"rows": [{"metricValues": [{"value": "42"}]}]}

    monkeypatch.setattr(tools, "_analytics_request", fake_request)

    result = json.loads(
        tools.google_analytics_run_report.func(
            property_id="properties/1234",
            metrics="activeUsers,eventCount",
            dimensions="date,sessionSource",
            start_date="2026-05-01",
            end_date="2026-05-15",
            limit=25,
            order_bys_json='[{"metric":{"metricName":"activeUsers"},"desc":true}]',
            filters_json='{"dimensionFilter":{"filter":{"fieldName":"country","stringFilter":{"value":"US"}}}}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["rows"][0]["metricValues"][0]["value"] == "42"
    assert captured["user_id"] == "alice"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://analyticsdata.googleapis.com/v1beta/properties/1234:runReport"
    body = captured["json_body"]
    assert body["dateRanges"] == [{"startDate": "2026-05-01", "endDate": "2026-05-15"}]
    assert body["metrics"] == [{"name": "activeUsers"}, {"name": "eventCount"}]
    assert body["dimensions"] == [{"name": "date"}, {"name": "sessionSource"}]
    assert body["limit"] == "25"
    assert body["orderBys"][0]["metric"]["metricName"] == "activeUsers"
    assert body["dimensionFilter"]["filter"]["fieldName"] == "country"


def test_google_analytics_list_account_summaries_uses_admin_api(monkeypatch):
    from nymeria.tools import google_analytics_service_integrations as tools

    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return True, {"accountSummaries": [{"name": "accountSummaries/1", "displayName": "Main"}]}

    monkeypatch.setattr(tools, "_analytics_request", fake_request)

    result = json.loads(
        tools.google_analytics_list_account_summaries.func(
            page_size=3,
            page_token="next",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"name": "accountSummaries/1", "displayName": "Main"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
    assert captured["params"] == {"pageSize": 3, "pageToken": "next"}


def test_google_analytics_missing_auth_returns_setup_hint(monkeypatch):
    from nymeria.tools import google_analytics_service_integrations as tools

    monkeypatch.setattr(tools.auth_utils, "get_google_credentials", lambda *args, **kwargs: None)

    success, message = tools._analytics_request(
        user_id="alice",
        method="GET",
        url="https://analyticsadmin.googleapis.com/v1beta/accountSummaries",
        account_id=None,
    )

    assert success is False
    assert "No authenticated Google Analytics account" in message
    assert "request_credential(provider=\"google_analytics\"" in message


def test_google_analytics_tools_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "google_analytics_list_account_summaries",
        "google_analytics_get_metadata",
        "google_analytics_run_report",
        "google_analytics_run_realtime_report",
    ]
    moderate_names: list[str] = []

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.GOOGLE_DOCS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.GOOGLE_DOCS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_google_analytics_tool_schemas_hide_runtime_config():
    from nymeria.tools import google_analytics_run_report

    assert "config" not in google_analytics_run_report.args_schema.model_json_schema()["properties"]
