"""Regression tests for the 2026-04-27 tool audit findings."""

from __future__ import annotations

import time

from nymeria.tools import ALL_TOOLS, OPTIONAL_TOOLS
from nymeria.tools import auth_cache_utils
from nymeria.tools import calendar
from nymeria.tools import google_sheets
from nymeria.tools import outlook_attachments
from nymeria.tools import outlook_email
from nymeria.tools import _prv_a_products
from nymeria.tools.metadata import get_all_tool_metadata
from nymeria.tools.triggers import trigger_create, trigger_update


def _config(user_id: str = "user-1", thread_id: str = "thread-1"):
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def test_calendar_operation_tools_pass_user_id_to_request(monkeypatch):
    calls = []
    results = [
        {"items": [{"summary": "Audit event", "id": "full-event-id", "start": {"date": "2026-04-27"}}]},
        {"id": "created-id", "htmlLink": "https://calendar.example/event"},
        {"id": "updated-id"},
        {},
        {"calendars": {"primary": {"busy": []}}},
    ]

    def fake_request(user_id, operation, account_id=None):
        calls.append((user_id, account_id, operation))
        return True, results.pop(0)

    monkeypatch.setattr(calendar, "_calendar_request", fake_request)

    cfg = _config("audit-user")
    listed = calendar.calendar_list_events.func(config=cfg)
    assert "full-event-id" in listed
    calendar.calendar_create_event.func("Audit", "2026-04-27", "2026-04-28", config=cfg)
    calendar.calendar_update_event.func("evt-1", summary="Updated", config=cfg)
    calendar.calendar_respond_to_event.func("evt-1", "accepted", config=cfg)
    calendar.calendar_get_freebusy.func(
        "2026-04-27T00:00:00Z",
        "2026-04-28T00:00:00Z",
        config=cfg,
    )

    assert [c[0] for c in calls] == ["audit-user"] * 5


def test_outlook_attachments_pass_user_and_account(monkeypatch):
    seen = {}

    def fake_download(user_id, email_id, account_id=None):
        seen.update({"user_id": user_id, "email_id": email_id, "account_id": account_id})
        return [], 0

    monkeypatch.setattr(outlook_attachments, "_download_attachments", fake_download)

    result = outlook_attachments.outlook_get_attachments.func(
        "message-1",
        account_id="sales",
        config=_config("audit-user"),
    )

    assert seen == {"user_id": "audit-user", "email_id": "message-1", "account_id": "sales"}
    assert "no file attachments" in result


def test_outlook_folder_validation_and_html_cleanup():
    valid, folder_name = outlook_email._resolve_folder_alias("sent")
    assert valid is True
    assert folder_name == "sentitems"

    valid, message = outlook_email._resolve_folder_alias("notafolder")
    assert valid is False
    assert "Invalid folder" in message

    cleaned = outlook_email._html_to_text(
        "<html><head><style>.x{color:red}</style></head><body>"
        "<p>Hello&nbsp;there</p><script>alert(1)</script></body></html>"
    )
    assert "color:red" not in cleaned
    assert "alert" not in cleaned
    assert "Hello there" in cleaned


def test__prv_a_product_search_uses_service_account_search(monkeypatch):
    seen = {}

    def fake_search_sheet_data(**kwargs):
        seen.update(kwargs)
        return "[Success]: mocked _PRV_A result"

    monkeypatch.setattr(google_sheets, "search_sheet_data", fake_search_sheet_data)

    result = _prv_a_products._prv_a_product_search.func(query="1756-L81E", config=_config())

    assert result == "[Success]: mocked _PRV_A result"
    assert seen["user_id"] is None
    assert seen["use__prv_a_service_account"] is True


def test_google_account_display_validation_refreshes_and_prunes(monkeypatch):
    now = time.time()
    accounts = {
        "active": {
            "email": "active@example.com",
            "scopes": ["scope-a"],
            "expires_at": now + 3600,
            "refresh_token": "refresh",
        },
        "expired": {
            "email": "expired@example.com",
            "scopes": ["scope-a"],
            "expires_at": now - 10,
            "refresh_token": "refresh",
        },
        "invalid": {
            "email": "invalid@example.com",
            "scopes": ["scope-a"],
            "expires_at": now - 10,
            "refresh_token": "refresh",
        },
        "missing_scope": {
            "email": "missing@example.com",
            "scopes": [],
            "expires_at": now + 3600,
            "refresh_token": "refresh",
        },
    }

    def fake_refresh(account, scopes):
        if account["email"] == "expired@example.com":
            account["expires_at"] = now + 3600
            return "refreshed", ""
        return "invalid", "revoked"

    monkeypatch.setattr(auth_cache_utils, "refresh_google_account", fake_refresh)

    rows, changed = auth_cache_utils.validate_google_accounts_for_display(accounts, ["scope-a"])

    assert changed is True
    assert "invalid" not in accounts
    statuses = {row["account_id"]: row for row in rows}
    assert statuses["active"]["usable"] is True
    assert statuses["expired"]["status"] == "active (refreshed)"
    assert statuses["missing_scope"]["usable"] is False


def test_trigger_tool_schema_exposes_object_configs():
    create_args = trigger_create.args
    update_args = trigger_update.args

    assert create_args["action_config"]["type"] == "object"
    assert "action_config" in create_args
    assert update_args["source_config"]["anyOf"][0]["type"] == "object"
    assert update_args["action_config"]["anyOf"][0]["type"] == "object"


def test_builtin_tools_have_metadata():
    tools = list(ALL_TOOLS) + list(OPTIONAL_TOOLS.values())
    missing = sorted({tool.name for tool in tools if get_all_tool_metadata(tool.name) is None})

    assert missing == []
