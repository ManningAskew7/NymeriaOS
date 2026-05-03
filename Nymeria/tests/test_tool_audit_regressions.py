"""Regression tests for the 2026-04-27 tool audit findings."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import nymeria.tools as tools_package
from nymeria.tools import (
    ALL_TOOLS,
    DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
    OPTIONAL_TOOLS,
    filter_developer_only_tools,
)
from nymeria.tools import auth_cache_utils
from nymeria.tools import calendar
from nymeria.tools import google_sheets
from nymeria.tools import outlook_attachments
from nymeria.tools import outlook_email
from nymeria.tools import _prv_a_products
from nymeria.tools.metadata import get_all_tool_metadata
from nymeria.tools import triggers as trigger_tools
from nymeria.tools.tool_search import _search, tool_search
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.user_profile import ToolPreferences, migrate_tool_names


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
    config_args = trigger_tools.trigger_config.args
    info_args = trigger_tools.trigger_info.args

    assert [tool.name for tool in trigger_tools.TRIGGER_TOOLS] == [
        "trigger_config",
        "trigger_info",
    ]
    assert config_args["source_config"]["anyOf"][0]["type"] == "object"
    assert config_args["action_config"]["anyOf"][0]["type"] == "object"
    assert config_args["conditions"]["anyOf"][0]["type"] == "array"
    assert "current_thread_only" in info_args


def test_trigger_dispatch_tools_route_to_existing_implementations(monkeypatch):
    calls = []

    def fake_create(**kwargs):
        calls.append(("create", kwargs))
        return "created"

    def fake_update(**kwargs):
        calls.append(("update", kwargs))
        return "updated"

    def fake_delete(**kwargs):
        calls.append(("delete", kwargs))
        return "deleted"

    monkeypatch.setattr(trigger_tools, "_trigger_create", fake_create)
    monkeypatch.setattr(trigger_tools, "_trigger_update", fake_update)
    monkeypatch.setattr(trigger_tools, "_trigger_delete", fake_delete)

    cfg = _config()
    assert trigger_tools.trigger_config.func(
        action="create",
        name="Deploy alert",
        source_type="webhook",
        action_type="notify",
        action_config={"message_template": "Deploy {status}"},
        config=cfg,
    ) == "created"
    assert trigger_tools.trigger_config.func(
        action="update",
        trigger_id="abc12345",
        enabled=False,
        config=cfg,
    ) == "updated"
    assert trigger_tools.trigger_config.func(
        action="delete",
        trigger_id="abc12345",
        config=cfg,
    ) == "deleted"

    assert [call[0] for call in calls] == ["create", "update", "delete"]
    assert calls[0][1]["config"] == cfg
    assert calls[1][1]["enabled"] is False
    assert calls[2][1]["trigger_id"] == "abc12345"


def test_trigger_info_tool_routes_read_actions(monkeypatch):
    calls = []

    def fake_list(**kwargs):
        calls.append(("list", kwargs))
        return "listed"

    def fake_sources():
        calls.append(("sources", {}))
        return "sources"

    def fake_inspect(**kwargs):
        calls.append(("inspect", kwargs))
        return kwargs["action"]

    monkeypatch.setattr(trigger_tools, "_trigger_list", fake_list)
    monkeypatch.setattr(trigger_tools, "_trigger_sources_info", fake_sources)
    monkeypatch.setattr(trigger_tools, "_trigger_inspect", fake_inspect)

    cfg = _config()
    assert trigger_tools.trigger_info.func(
        action="list",
        enabled_only=True,
        current_thread_only=True,
        config=cfg,
    ) == "listed"
    assert trigger_tools.trigger_info.func(action="sources", config=cfg) == "sources"
    assert trigger_tools.trigger_info.func(
        action="history",
        trigger_id="abc12345",
        limit=3,
        config=cfg,
    ) == "history"

    assert [call[0] for call in calls] == ["list", "sources", "inspect"]
    assert calls[0][1]["enabled_only"] is True
    assert calls[2][1]["trigger_id"] == "abc12345"
    assert calls[2][1]["limit"] == 3


def test_legacy_tool_names_migrate_to_consolidated_tools():
    assert migrate_tool_names([
        "todo",
        "todo_delete",
        "todo_list",
        "trigger_create",
        "trigger_update",
        "trigger_delete",
        "trigger_list",
        "trigger_inspect",
        "trigger_sources_info",
    ]) == [
        "nym_todo",
        "nym_todo_delete",
        "nym_todo_list",
        "trigger_config",
        "trigger_info",
    ]

    prefs = ToolPreferences(default_thread_tools=[
        "trigger_create",
        "trigger_update",
        "trigger_list",
    ])
    assert prefs.default_thread_tools == ["trigger_config", "trigger_info"]

    now = datetime.now(timezone.utc)
    tc = ThreadConfig(
        thread_id="thread-1",
        enabled_tools=["trigger_create", "trigger_update", "trigger_list"],
        disabled_tools=["trigger_delete"],
        temporary_tools={
            "trigger_inspect": {
                "enabled_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }
        },
    )
    assert tc.enabled_tools == ["trigger_config", "trigger_info"]
    assert tc.disabled_tools == ["trigger_config"]
    assert list(tc.temporary_tools.keys()) == ["trigger_info"]


def test_legacy_tool_symbols_are_not_public_exports():
    legacy_trigger_symbols = [
        "trigger_create",
        "trigger_update",
        "trigger_delete",
        "trigger_list",
        "trigger_inspect",
        "trigger_sources_info",
        "trigger_test",
    ]
    legacy_todo_symbols = ["todo", "todo_delete", "todo_list"]

    for name in legacy_trigger_symbols:
        assert not hasattr(trigger_tools, name)
    for name in legacy_todo_symbols:
        assert name not in tools_package.__all__

    registered_names = {tool.name for tool in ALL_TOOLS} | set(OPTIONAL_TOOLS)
    assert not (registered_names & set(legacy_trigger_symbols + legacy_todo_symbols))


def test_trigger_consolidation_and_tool_search_schema_budget():
    trigger_schema_chars = sum(
        len(tool.name)
        + len(tool.description or "")
        + len(json.dumps(tool.args, sort_keys=True))
        for tool in trigger_tools.TRIGGER_TOOLS
    )

    assert trigger_schema_chars < 4000
    assert len(tool_search.description or "") < 1200


def test_builtin_tools_have_metadata():
    tools = list(ALL_TOOLS) + list(OPTIONAL_TOOLS.values())
    missing = sorted({tool.name for tool in tools if get_all_tool_metadata(tool.name) is None})

    assert missing == []


def test_hello_test_is_developer_only_in_tool_discovery():
    assert "hello_test" in DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES

    user_results = _search("hello", "", "thread-a", user_role="user")
    admin_results = _search("hello", "", "thread-a", user_role="admin")
    user_core_results = _search("", "core", "thread-a", user_role="user")
    admin_core_results = _search("", "core", "thread-a", user_role="admin")

    assert "hello_test" not in user_results
    assert "hello_test" in admin_results
    assert "hello_test" not in user_core_results
    assert "hello_test" in admin_core_results

    allowed, blocked = filter_developer_only_tools(
        ["hello_test", "sticky_note"],
        "user",
    )
    assert allowed == {"sticky_note"}
    assert blocked == {"hello_test"}
