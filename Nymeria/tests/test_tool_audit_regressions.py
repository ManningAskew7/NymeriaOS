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
from nymeria.tools import calendar_auth
from nymeria.tools import google_docs
from nymeria.tools import google_docs_auth
from nymeria.tools import google_sheets
from nymeria.tools import gmail_auth
from nymeria.tools import outlook_auth
from nymeria.tools import outlook_attachments
from nymeria.tools import outlook_email
from nymeria.plugins._prv_a import products as _prv_a_products
from nymeria.tools.metadata import (
    TOOL_METADATA,
    ToolCategory,
    _description_from_tool,
    get_all_tool_metadata,
    refresh_builtin_tool_metadata,
)
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


def test_google_docs_operation_tools_pass_user_id_to_request(monkeypatch):
    calls = []

    def fake_request(user_id, operation, account_id=None):
        calls.append((user_id, account_id, operation))
        return True, {"body": {"content": [{"endIndex": 100}]}}

    monkeypatch.setattr(google_docs, "_docs_request", fake_request)

    cfg = _config("docs-user")
    results = [
        google_docs.google_docs_append_text.func(
            "doc-1",
            "hello",
            account_id="docs-account",
            config=cfg,
        ),
        google_docs.google_docs_insert_text.func(
            "doc-1",
            "hello",
            2,
            account_id="docs-account",
            config=cfg,
        ),
        google_docs.google_docs_delete_range.func(
            "doc-1",
            2,
            4,
            account_id="docs-account",
            config=cfg,
        ),
        google_docs.google_docs_apply_text_style.func(
            "doc-1",
            2,
            4,
            bold=True,
            account_id="docs-account",
            config=cfg,
        ),
        google_docs.google_docs_update_paragraph_style.func(
            "doc-1",
            2,
            4,
            heading_level=1,
            account_id="docs-account",
            config=cfg,
        ),
        google_docs.google_docs_insert_table.func(
            "doc-1",
            2,
            3,
            5,
            account_id="docs-account",
            config=cfg,
        ),
        google_docs.google_docs_insert_page_break.func(
            "doc-1",
            6,
            account_id="docs-account",
            config=cfg,
        ),
    ]

    assert all(result.startswith("[Success]:") for result in results)
    assert [(c[0], c[1]) for c in calls] == [("docs-user", "docs-account")] * 8
    assert all(callable(c[2]) for c in calls)


def test_google_docs_write_segments_pass_user_id_to_docs_request(monkeypatch):
    calls = []

    def fake_request(user_id, operation, account_id=None):
        calls.append((user_id, account_id, operation))
        return True, {"ok": True}

    monkeypatch.setattr(google_docs, "_docs_request", fake_request)

    success, message = google_docs._execute_write_segments(
        "audit-user",
        "doc-1",
        [google_docs.Block(kind="paragraph", text="Hello")],
        1,
        account_id="docs-account",
    )

    assert success is True
    assert message == "ok"
    assert [(c[0], c[1]) for c in calls] == [("audit-user", "docs-account")]


def test_google_docs_write_passes_user_id_to_segment_executor(monkeypatch):
    seen = {}

    def fake_request(user_id, operation, account_id=None):
        seen["read"] = {"user_id": user_id, "account_id": account_id}
        return True, {"body": {"content": [{"endIndex": 8}]}}

    def fake_execute(
        user_id,
        document_id,
        blocks,
        start_index,
        account_id=None,
        prefix_requests=None,
    ):
        seen["write"] = {
            "user_id": user_id,
            "document_id": document_id,
            "blocks": blocks,
            "start_index": start_index,
            "account_id": account_id,
            "prefix_requests": prefix_requests,
        }
        return True, "ok"

    monkeypatch.setattr(google_docs, "_docs_request", fake_request)
    monkeypatch.setattr(google_docs, "_execute_write_segments", fake_execute)

    result = google_docs.google_docs_write.func(
        "doc-1",
        "Hello",
        account_id="docs-account",
        config=_config("audit-user"),
    )

    assert result.startswith("[Success]: Wrote")
    assert seen["read"] == {"user_id": "audit-user", "account_id": "docs-account"}
    assert seen["write"]["user_id"] == "audit-user"
    assert seen["write"]["document_id"] == "doc-1"
    assert seen["write"]["start_index"] == 7
    assert seen["write"]["account_id"] == "docs-account"
    assert seen["write"]["prefix_requests"] is None
    assert [b.text for b in seen["write"]["blocks"]] == ["Hello"]


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


def test_google_credentials_helper_loads_provider_cache_and_refreshes(monkeypatch):
    now = time.time()
    cache = {
        "accounts": {
            "acct": {
                "email": "docs@example.com",
                "access_token": "old-token",
                "refresh_token": "refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": ["scope-a"],
                "expires_at": now - 10,
            }
        }
    }
    calls = {}

    def fake_load(user_id, cache_filename):
        calls["load"] = (user_id, cache_filename)
        return cache

    def fake_save(user_id, cache_filename, saved_cache):
        calls["save"] = (user_id, cache_filename, saved_cache)

    def fake_refresh(account, scopes):
        calls["refresh"] = (account["email"], scopes)
        account["access_token"] = "new-token"
        account["expires_at"] = now + 3600
        return "refreshed", ""

    monkeypatch.setattr(auth_cache_utils, "load_token_cache", fake_load)
    monkeypatch.setattr(auth_cache_utils, "save_token_cache", fake_save)
    monkeypatch.setattr(auth_cache_utils, "refresh_google_account", fake_refresh)

    creds = auth_cache_utils.get_google_credentials(
        "docs-user",
        "google_docs",
        ["scope-a"],
        account_id="acct",
        provider_display_name="Google Docs",
    )

    assert creds.token == "new-token"
    assert calls["load"] == ("docs-user", "google_docs.json")
    assert calls["refresh"] == ("docs@example.com", ["scope-a"])
    assert calls["save"][0:2] == ("docs-user", "google_docs.json")


def test_google_api_request_helper_builds_service(monkeypatch):
    import googleapiclient.discovery

    calls = {}

    def fake_get_credentials(*args, **kwargs):
        calls["credentials"] = (args, kwargs)
        return "creds"

    def fake_build(service_name, service_version, credentials, **kwargs):
        calls["build"] = (service_name, service_version, credentials, kwargs)
        return {"service": service_name}

    monkeypatch.setattr(auth_cache_utils, "get_google_credentials", fake_get_credentials)
    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)

    success, result = auth_cache_utils.google_api_request(
        "docs-user",
        "google_docs",
        ["scope-a"],
        lambda service: {"ok": service["service"]},
        service_name="docs",
        service_version="v1",
        account_id="acct",
        auth_tool_name="google_docs_auth_start",
        api_label="Google Docs",
    )

    assert success is True
    assert result == {"ok": "docs"}
    assert calls["credentials"][0] == ("docs-user", "google_docs", ["scope-a"])
    assert calls["credentials"][1]["account_id"] == "acct"
    assert calls["build"] == ("docs", "v1", "creds", {})


def test_google_request_wrappers_delegate_to_shared_helper(monkeypatch):
    calls = []

    def fake_google_api_request(user_id, provider, scopes, operation, **kwargs):
        calls.append((user_id, provider, scopes, kwargs))
        return True, operation("service")

    monkeypatch.setattr(calendar.auth_utils, "google_api_request", fake_google_api_request)

    assert calendar._calendar_request("calendar-user", lambda s: f"calendar:{s}", account_id="cal") == (
        True,
        "calendar:service",
    )
    assert google_docs._docs_request("docs-user", lambda s: f"docs:{s}", account_id="doc") == (
        True,
        "docs:service",
    )
    assert google_docs._drive_request("drive-user", lambda s: f"drive:{s}", account_id="drive") == (
        True,
        "drive:service",
    )

    assert calls[0][1] == "google_calendar"
    assert calls[0][3]["service_name"] == "calendar"
    assert calls[1][1] == "google_docs"
    assert calls[1][3]["service_name"] == "docs"
    assert calls[2][1] == "google_docs"
    assert calls[2][3]["service_name"] == "drive"


def test_google_auth_tool_sets_are_generated_with_stable_names():
    assert [tool.name for tool in calendar_auth.CALENDAR_AUTH_TOOLS] == [
        "calendar_auth_start",
        "calendar_auth_complete",
        "calendar_auth_clear",
        "calendar_list_authenticated_accounts",
    ]
    assert [tool.name for tool in google_docs_auth.GOOGLE_DOCS_AUTH_TOOLS] == [
        "google_docs_auth_start",
        "google_docs_auth_complete",
        "google_docs_auth_clear",
        "google_docs_list_accounts",
    ]
    assert [tool.name for tool in gmail_auth.GMAIL_AUTH_TOOLS] == [
        "gmail_auth_start",
        "gmail_auth_complete",
        "gmail_auth_clear",
        "gmail_list_accounts",
    ]
    assert calendar_auth.calendar_auth_start is calendar_auth.CALENDAR_AUTH_TOOLS[0]
    assert google_docs_auth.google_docs_auth_start is google_docs_auth.GOOGLE_DOCS_AUTH_TOOLS[0]
    assert gmail_auth.gmail_auth_start is gmail_auth.GMAIL_AUTH_TOOLS[0]


def test_outlook_auth_cache_wrappers_delegate_to_shared_cache(monkeypatch):
    calls = []

    def fake_load(user_id, cache_filename):
        calls.append(("load", user_id, cache_filename))
        return {"ok": True}

    def fake_save(user_id, cache_filename, cache):
        calls.append(("save", user_id, cache_filename, cache))

    def fake_delete(user_id, cache_filename):
        calls.append(("delete", user_id, cache_filename))
        return True

    monkeypatch.setattr(outlook_auth.auth_utils, "load_token_cache", fake_load)
    monkeypatch.setattr(outlook_auth.auth_utils, "save_token_cache", fake_save)
    monkeypatch.setattr(outlook_auth.auth_utils, "delete_token_cache", fake_delete)

    assert outlook_auth.load_token_cache("ms-user") == {"ok": True}
    outlook_auth.save_token_cache("ms-user", {"saved": True})
    assert outlook_auth.delete_token_cache("ms-user") is True

    assert calls == [
        ("load", "ms-user", "microsoft.json"),
        ("save", "ms-user", "microsoft.json", {"saved": True}),
        ("delete", "ms-user", "microsoft.json"),
    ]


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
        "mcp_search",
        "mcp_install",
        "mcp_manage",
    ]) == [
        "nym_todo",
        "nym_todo_delete",
        "nym_todo_list",
        "trigger_config",
        "trigger_info",
        "search_mcp",
        "install_mcp_server",
        "manage_mcp",
    ]

    prefs = ToolPreferences(default_thread_tools=[
        "trigger_create",
        "trigger_update",
        "trigger_list",
        "mcp_search",
        "mcp_install",
        "mcp_manage",
    ])
    assert prefs.default_thread_tools == [
        "trigger_config",
        "trigger_info",
        "search_mcp",
        "install_mcp_server",
        "manage_mcp",
    ]

    now = datetime.now(timezone.utc)
    tc = ThreadConfig(
        thread_id="thread-1",
        enabled_tools=["trigger_create", "trigger_update", "trigger_list"],
        disabled_tools=["trigger_delete"],
        temporary_tools={
            "trigger_inspect": {
                "enabled_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            },
            "mcp_manage": {
                "enabled_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            },
            "mcp_install": {
                "enabled_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }
        },
    )
    assert tc.enabled_tools == ["trigger_config", "trigger_info"]
    assert tc.disabled_tools == ["trigger_config"]
    assert list(tc.temporary_tools.keys()) == [
        "trigger_info",
        "manage_mcp",
        "install_mcp_server",
    ]


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


def test_builtin_tool_metadata_is_generated_from_registered_tools():
    tools = {tool.name: tool for tool in ALL_TOOLS}
    tools.update(OPTIONAL_TOOLS)

    refresh_builtin_tool_metadata()

    assert set(TOOL_METADATA) == set(tools)
    assert {
        name
        for name, meta in TOOL_METADATA.items()
        if meta.default_enabled
    } == {tool.name for tool in ALL_TOOLS}
    assert [
        name
        for name, tool in sorted(tools.items())
        if get_all_tool_metadata(name).description != _description_from_tool(tool)
    ] == []


def test_builtin_tool_names_avoid_claude_oauth_reserved_mcp_namespace():
    registered_names = {tool.name for tool in ALL_TOOLS} | set(OPTIONAL_TOOLS)
    rejected_by_claude_oauth = sorted(
        name
        for name in registered_names
        if (
            (name.startswith("mcp_") and not name.startswith("mcp__"))
            or name.startswith("mcp.")
            or name.startswith("mcp/")
        )
    )

    assert "manage_mcp" in registered_names
    assert "mcp_manage" not in registered_names
    assert "search_mcp" in registered_names
    assert "install_mcp_server" in registered_names
    assert "mcp_search" not in registered_names
    assert "mcp_install" not in registered_names
    assert rejected_by_claude_oauth == []


def test_new_registered_tools_receive_generated_metadata(monkeypatch):
    class FakeTool:
        name = "temporary_probe"
        description = "Probe generated metadata.\n\nArgs:\n    none: No input."

    class FakeOptionalTool:
        name = "temporary_optional_probe"
        description = "Optional generated metadata."

    with monkeypatch.context() as m:
        m.setattr(tools_package, "ALL_TOOLS", [*ALL_TOOLS, FakeTool()])
        m.setattr(
            tools_package,
            "OPTIONAL_TOOLS",
            {**OPTIONAL_TOOLS, FakeOptionalTool.name: FakeOptionalTool()},
        )
        refresh_builtin_tool_metadata()

        core_meta = get_all_tool_metadata("temporary_probe")
        optional_meta = get_all_tool_metadata("temporary_optional_probe")

        assert core_meta is not None
        assert core_meta.description == "Probe generated metadata."
        assert core_meta.default_enabled is True
        assert optional_meta is not None
        assert optional_meta.description == "Optional generated metadata."
        assert optional_meta.default_enabled is False

    refresh_builtin_tool_metadata()
    assert get_all_tool_metadata("temporary_probe") is None
    assert get_all_tool_metadata("temporary_optional_probe") is None


def test_optional_profile_tools_keep_profile_category():
    for tool_name in ("memory_clear_all", "rag_settings"):
        meta = get_all_tool_metadata(tool_name)

        assert meta is not None
        assert meta.category == ToolCategory.PROFILE
        assert meta.default_enabled is False


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
