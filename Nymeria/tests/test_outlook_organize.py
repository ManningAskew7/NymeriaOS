"""Tests for the Outlook organisation tools (``nymeria/tools/outlook_organize.py``).

Plan behaviours B13 (master categories with colour presets), B15 (follow-up
flags with mailbox-zoned dates), B16 (inbox rules with folder resolution and
JSON refusals), B17 (automatic replies), B18 (Focused/Other overrides), B19
(List-Unsubscribe: mailto, one-click through the egress policy, link-only, no
header), plus the folder tree and folder CRUD, the scope gate wiring, and the
docstring hygiene tripwire. Graph is faked at the ``graph_request`` /
``graph_batch`` seams in BOTH namespaces (``outlook_graph`` for the shared
helpers the tools call, ``outlook_organize`` for the tools' own calls) so every
assert is on the exact endpoint, payload, and result string.

Edges deliberately not covered here: Graph-side 4xx bodies for duplicate
category names and rule quotas (the error text is Graph's, passed through
``graph_request`` unchanged and covered in ``test_outlook_graph.py``).
"""

import ast
import pathlib
import time
from types import SimpleNamespace

import pytest

from nymeria.tools import outlook_graph as og
from nymeria.tools import outlook_organize as oo
from nymeria.tools.registry import get_tool_group

_CFG = {"configurable": {"user_id": "u1", "thread_id": "t1"}}
_FID = "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OAAuAAAAAAAiQ8W967B7TKBjgx9rVEURAQAiIsqMbYjsT5e-T7KzowPTAAAAAAEMAAA="
_SCOPE_MSG = (
    "Outlook account 'me@x.com' was connected without the permission(s) needed for x: "
    "MailboxSettings.ReadWrite. Reconnect it with request_credential(provider=\"outlook\", kind=\"oauth\")."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_gate(monkeypatch):
    """Let every tool through the scope gate, recording what it was asked for."""
    asked = []

    def _fake(user_id, needed, account_id=None, *, mailbox=None, thread_id=None, purpose="this operation"):
        asked.append({"needed": list(needed), "mailbox": mailbox, "purpose": purpose, "account_id": account_id})
        return None

    monkeypatch.setattr(oo, "require_scopes", _fake)
    return asked


def _closed_gate(monkeypatch, message=_SCOPE_MSG):
    monkeypatch.setattr(oo, "require_scopes", lambda *a, **k: message)


def _fake_graph(monkeypatch, handler):
    """Route ``graph_request`` (both namespaces) through ``handler(call)``; record calls."""
    calls = []

    def _fake(user_id, method, endpoint, account_id=None, json_data=None, params=None, **kw):
        call = {
            "method": method, "endpoint": endpoint, "json": json_data, "params": dict(params or {}),
            "mailbox": kw.get("mailbox"), "account_id": account_id,
        }
        calls.append(call)
        return handler(call)

    monkeypatch.setattr(og, "graph_request", _fake)
    monkeypatch.setattr(oo, "graph_request", _fake)
    return calls


def _sequence(*responses):
    seq = iter(responses)
    return lambda call: next(seq)


def _record_batch(monkeypatch, handler=None):
    """Capture ``graph_batch`` calls (both namespaces).

    ``handler(request)`` returns a body (ok), ``"FAIL"`` (404), or ``None`` (ok, empty body).
    """
    batches = []

    def _fake(user_id, requests, account_id=None, *, mailbox=None, thread_id=None):
        batches.append({"requests": requests, "mailbox": mailbox, "account_id": account_id})
        out = []
        for r in requests:
            rid = str(r["id"])
            custom = handler(r) if handler else None
            if custom is None:
                out.append({"id": rid, "status": 200, "ok": True, "body": {}, "error": None})
            elif custom == "FAIL":
                out.append({"id": rid, "status": 404, "ok": False, "body": None, "error": "API Error (404): gone"})
            else:
                out.append({"id": rid, "status": 200, "ok": True, "body": custom, "error": None})
        return out

    monkeypatch.setattr(og, "graph_batch", _fake)
    monkeypatch.setattr(oo, "graph_batch", _fake)
    return batches


def _child_url(fid):
    return f"/me/mailFolders/{fid}/childFolders?$top=250&$select={oo._FOLDER_SELECT}"


def _folder(fid, name, children=0, unread=0, total=0):
    return {"id": fid, "displayName": name, "childFolderCount": children, "unreadItemCount": unread, "totalItemCount": total}


# ---------------------------------------------------------------------------
# Folder tree
# ---------------------------------------------------------------------------


def test_list_folders_root_tags_well_known_names_and_expands_one_level_in_one_batch(monkeypatch):
    asked = _open_gate(monkeypatch)
    top = [_folder("INBOX", "Inbox", 1, 12, 340), _folder("SENT", "Sent Items", 0, 0, 1200), _folder("CLI", "Clients", 2, 3, 45)]
    calls = _fake_graph(monkeypatch, _sequence((True, {"value": top})))

    def handler(req):
        rid = req["id"]
        if rid == "wk:inbox":
            return {"id": "INBOX"}
        if rid == "wk:sentitems":
            return {"id": "SENT"}
        if rid.startswith("wk:"):
            return "FAIL"  # a mailbox without, say, an archive folder
        if req["url"].startswith("/me/mailFolders/INBOX/"):
            return {"value": [_folder("RCPT", "Receipts", 0, 1, 9)]}
        if req["url"].startswith("/me/mailFolders/CLI/"):
            return {"value": [_folder("ACME", "Acme", 3, 0, 20), _folder("GLOBEX", "Globex", 0, 2, 5)]}
        return None

    batches = _record_batch(monkeypatch, handler)
    out = oo.outlook_list_folders.invoke({"mailbox": "s@x.com"}, config=_CFG)

    assert calls == [{
        "method": "GET", "endpoint": "/me/mailFolders", "json": None,
        "params": {"$top": 250, "$select": oo._FOLDER_SELECT}, "mailbox": "s@x.com", "account_id": None,
    }]
    assert len(batches) == 1 and batches[0]["mailbox"] == "s@x.com"
    reqs = batches[0]["requests"]
    assert [r["id"] for r in reqs[:10]] == [f"wk:{n}" for n in oo._WELL_KNOWN_FOLDERS]
    assert reqs[0] == {"id": "wk:inbox", "method": "GET", "url": "/me/mailFolders/inbox?$select=id"}
    # Only folders with children are expanded (childFolderCount saves the call for Sent Items).
    assert [r["url"] for r in reqs[10:]] == [_child_url("INBOX"), _child_url("CLI")]
    assert out == "\n".join([
        "[Success]: Folder tree under the mailbox root (depth 2):",
        "Inbox [inbox] (12 unread, 340 total) id=INBOX",
        "  Receipts (1 unread, 9 total) id=RCPT",
        "Sent Items [sentitems] (0 unread, 1200 total) id=SENT",
        "Clients (3 unread, 45 total) id=CLI",
        "  Acme (0 unread, 20 total) id=ACME [+3 subfolder(s) not shown]",
        "  Globex (2 unread, 5 total) id=GLOBEX",
    ])
    assert asked == [{"needed": [], "mailbox": "s@x.com", "purpose": "listing folders", "account_id": None}]


def test_list_folders_depth_three_issues_one_batch_per_level_and_clamps_depth(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence((True, {"value": [_folder("CLI", "Clients", 1)]})))

    def handler(req):
        if req["url"].startswith("/me/mailFolders/CLI/"):
            return {"value": [_folder("ACME", "Acme", 1)]}
        if req["url"].startswith("/me/mailFolders/ACME/"):
            return {"value": [_folder("INV", "Invoices", 0, 4, 4)]}
        return "FAIL"

    batches = _record_batch(monkeypatch, handler)
    out = oo.outlook_list_folders.invoke({"depth": 3}, config=_CFG)
    assert len(batches) == 2
    assert batches[1]["requests"] == [{"id": "child:0", "method": "GET", "url": _child_url("ACME")}]
    assert out.splitlines()[1:] == [
        "Clients (0 unread, 0 total) id=CLI",
        "  Acme (0 unread, 0 total) id=ACME",
        "    Invoices (4 unread, 4 total) id=INV",
    ]

    _fake_graph(monkeypatch, _sequence((True, {"value": [_folder("CLI", "Clients", 1)]})))
    batches = _record_batch(monkeypatch, lambda r: "FAIL")
    out = oo.outlook_list_folders.invoke({"depth": 0}, config=_CFG)
    assert out.splitlines()[0] == "[Success]: Folder tree under the mailbox root (depth 1):"
    assert all(r["id"].startswith("wk:") for r in batches[0]["requests"])  # no expansion at depth 1
    assert "[+1 subfolder(s) not shown]" in out

    _fake_graph(monkeypatch, _sequence((True, {"value": [_folder("A", "A")]})))
    _record_batch(monkeypatch, lambda r: "FAIL")
    assert oo.outlook_list_folders.invoke({"depth": 9}, config=_CFG).startswith("[Success]: Folder tree under the mailbox root (depth 4):")


def test_list_folders_under_a_parent_path_walks_the_resolver_and_skips_alias_lookup(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"value": [_folder("CLI", "Clients", 1)]}),
        (True, {"value": [_folder("ACME", "Acme", 1)]}),
        (True, {"value": [_folder("INV", "Invoices", 0, 1, 2)]}),
    ))
    batches = _record_batch(monkeypatch)
    out = oo.outlook_list_folders.invoke({"parent": "Clients/Acme"}, config=_CFG)
    assert [c["endpoint"] for c in calls] == [
        "/me/mailFolders", "/me/mailFolders/CLI/childFolders", "/me/mailFolders/ACME/childFolders",
    ]
    assert batches == []  # nothing to expand, and no well-known lookup below the root
    assert out == "[Success]: Folder tree under 'Clients/Acme' (depth 2):\nInvoices (1 unread, 2 total) id=INV"


def test_list_folders_reports_missing_parent_empty_tree_and_graph_errors(monkeypatch):
    _open_gate(monkeypatch)
    batches = _record_batch(monkeypatch)
    _fake_graph(monkeypatch, _sequence((True, {"value": []})))
    out = oo.outlook_list_folders.invoke({"parent": "Nope"}, config=_CFG)
    assert out.startswith("[Error]: No folder named 'Nope' under the mailbox root.")
    _fake_graph(monkeypatch, _sequence((True, {"value": []})))
    assert oo.outlook_list_folders.invoke({}, config=_CFG) == "[Info]: No folders under the mailbox root."
    _fake_graph(monkeypatch, _sequence((False, "API Error (401): nope")))
    assert oo.outlook_list_folders.invoke({}, config=_CFG) == "[Error]: API Error (401): nope"
    assert batches == []


def test_list_folders_subfolder_listing_failure_is_reported_inline(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence((True, {"value": [_folder("CLI", "Clients", 2)]})))
    _record_batch(monkeypatch, lambda r: "FAIL")
    out = oo.outlook_list_folders.invoke({}, config=_CFG)
    assert out.splitlines()[1:] == [
        "Clients (0 unread, 0 total) id=CLI",
        "  (could not list subfolders: API Error (404): gone)",
    ]


# ---------------------------------------------------------------------------
# Folder CRUD
# ---------------------------------------------------------------------------


def test_manage_folder_create_at_root_and_under_a_named_parent(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"id": "NEW1"}),
        (True, {"value": [_folder("CLI", "Clients")]}),
        (True, {"id": "NEW2"}),
    ))
    out = oo.outlook_manage_folder.invoke({"action": "create", "name": " Newsletters "}, config=_CFG)
    assert out == "[Success]: Folder 'Newsletters' created under the mailbox root. id=NEW1"
    assert calls[0]["method"] == "POST" and calls[0]["endpoint"] == "/me/mailFolders"
    assert calls[0]["json"] == {"displayName": "Newsletters"}

    out = oo.outlook_manage_folder.invoke({"action": "create", "name": "Acme", "parent": "Clients", "mailbox": "s@x.com"}, config=_CFG)
    assert out == "[Success]: Folder 'Acme' created under 'Clients'. id=NEW2"
    assert calls[2]["endpoint"] == "/me/mailFolders/CLI/childFolders"
    assert calls[2]["json"] == {"displayName": "Acme"} and calls[2]["mailbox"] == "s@x.com"


def test_manage_folder_rename_and_soft_delete(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"value": [{"id": _FID, "displayName": "Old"}]}), (True, {}),
        (True, {}),
    ))
    out = oo.outlook_manage_folder.invoke({"action": "rename", "folder": "Old", "name": "New"}, config=_CFG)
    assert out == f"[Success]: Folder 'Old' renamed to 'New'. id={_FID}"
    assert calls[1]["method"] == "PATCH" and calls[1]["endpoint"] == f"/me/mailFolders/{_FID}"
    assert calls[1]["json"] == {"displayName": "New"}

    out = oo.outlook_manage_folder.invoke({"action": "delete", "folder": _FID}, config=_CFG)
    assert out == f"[Success]: Folder '{_FID}' and its contents deleted."
    assert calls[2]["method"] == "DELETE" and calls[2]["endpoint"] == f"/me/mailFolders/{_FID}"
    assert len(calls) == 3  # an id needs no resolver round trip


def test_manage_folder_refuses_well_known_folders_and_bad_input_before_any_request(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence())
    inv = oo.outlook_manage_folder.invoke
    assert inv({"action": "delete", "folder": "Inbox"}, config=_CFG) == (
        "[Error]: 'Inbox' is a well-known folder; Outlook does not allow it to be deleted."
    )
    assert inv({"action": "rename", "folder": "sent", "name": "Out"}, config=_CFG) == (
        "[Error]: 'sent' is a well-known folder; Outlook does not allow it to be renamed."
    )
    assert inv({"action": "archive", "folder": "x"}, config=_CFG) == "[Error]: action must be 'create', 'rename', or 'delete'."
    assert inv({"action": "create"}, config=_CFG) == "[Error]: name is required to create a folder."
    assert inv({"action": "delete"}, config=_CFG) == "[Error]: folder is required to delete a folder."
    assert inv({"action": "rename", "folder": "Old"}, config=_CFG) == (
        "[Error]: name (the new display name) is required to rename a folder."
    )
    assert calls == []


def test_manage_folder_unresolvable_parent_refuses_without_creating(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"value": []})))
    out = oo.outlook_manage_folder.invoke({"action": "create", "name": "X", "parent": "Nope"}, config=_CFG)
    assert out.startswith("[Error]: No folder named 'Nope'")
    assert [c["method"] for c in calls] == ["GET"]


# ---------------------------------------------------------------------------
# Master categories (B13)
# ---------------------------------------------------------------------------


def test_create_category_maps_red_to_preset0(monkeypatch):
    asked = _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"id": "CAT1"})))
    out = oo.outlook_manage_categories.invoke({"action": "create", "name": "To Respond", "color": "red"}, config=_CFG)
    assert out == "[Success]: Category 'To Respond' created with colour red (preset0). id=CAT1"
    assert calls == [{
        "method": "POST", "endpoint": "/me/outlook/masterCategories", "params": {},
        "json": {"displayName": "To Respond", "color": "preset0"}, "mailbox": None, "account_id": None,
    }]
    assert asked[0]["needed"] == ["MailboxSettings.ReadWrite"] and asked[0]["purpose"] == "category management"


@pytest.mark.parametrize("given,preset", [
    ("Preset3", "preset3"), ("yellow", "preset3"), ("dark grey", "preset13"), ("DarkSteel", "preset11"),
    ("dark-red", "preset15"), ("Dark Cranberry", "preset24"), ("none", "none"), ("grey", "preset12"),
    ("GRAY", "preset12"), ("purple", "preset8"), ("dark_blue", "preset22"), ("black", "preset14"),
    ("hotpink", None), ("preset25", None), ("", None),
])
def test_colour_names_and_presets_resolve(given, preset):
    assert oo._colour_to_preset(given) == preset


def test_unknown_colour_lists_the_accepted_names_without_a_request(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence())
    out = oo.outlook_manage_categories.invoke({"action": "create", "name": "X", "color": "hotpink"}, config=_CFG)
    assert out.startswith(
        "[Error]: Unknown colour 'hotpink'. Accepted: none, preset0 to preset24, or a colour name: red, orange, brown, "
    )
    assert "dark cranberry (grey is accepted for gray)." in out
    assert calls == []


def test_create_category_without_colour_sends_none(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"id": "C"})))
    out = oo.outlook_manage_categories.invoke({"action": "create", "name": "Plain"}, config=_CFG)
    assert calls[0]["json"] == {"displayName": "Plain", "color": "none"}
    assert out == "[Success]: Category 'Plain' created with colour none (no colour). id=C"


def test_update_category_resolves_the_id_by_name_and_patches_colour_only(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"value": [{"id": "CAT1", "displayName": "To Respond", "color": "preset0"}]}),
        (True, {"id": "CAT1", "displayName": "To Respond", "color": "preset4"}),
    ))
    out = oo.outlook_manage_categories.invoke({"action": "update", "name": "to respond", "color": "green"}, config=_CFG)
    assert calls[0]["method"] == "GET" and calls[0]["endpoint"] == "/me/outlook/masterCategories"
    assert calls[1]["method"] == "PATCH" and calls[1]["endpoint"] == "/me/outlook/masterCategories/CAT1"
    assert calls[1]["json"] == {"color": "preset4"}
    assert out == (
        "[Success]: Category 'To Respond' colour changed to green (preset4). "
        "(Graph cannot rename a category; delete and recreate to change the name.)"
    )


def test_update_category_by_guid_skips_the_list_read_and_requires_a_colour(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {})))
    guid = "5a9a6aa8-b65f-4357-b1f9-60c6bf6330d8"
    oo.outlook_manage_categories.invoke({"action": "update", "name": guid, "color": "preset7"}, config=_CFG)
    assert [c["method"] for c in calls] == ["PATCH"]
    assert calls[0]["endpoint"] == f"/me/outlook/masterCategories/{guid}"
    assert oo.outlook_manage_categories.invoke({"action": "update", "name": "X"}, config=_CFG) == (
        "[Error]: color is required to update a category (names cannot be changed in Graph)."
    )
    assert len(calls) == 1


def test_delete_category_by_name_and_missing_name(monkeypatch):
    _open_gate(monkeypatch)
    master = {"value": [{"id": "CAT9", "displayName": "FYI", "color": "preset7"}]}
    calls = _fake_graph(monkeypatch, _sequence((True, master), (True, {}), (True, master)))
    out = oo.outlook_manage_categories.invoke({"action": "delete", "name": "fyi", "color": "ignored"}, config=_CFG)
    assert out == (
        "[Success]: Category 'FYI' deleted from the master list. Messages already tagged keep the label text "
        "but lose its colour."
    )
    assert calls[1]["method"] == "DELETE" and calls[1]["endpoint"] == "/me/outlook/masterCategories/CAT9"
    out = oo.outlook_manage_categories.invoke({"action": "delete", "name": "Nope"}, config=_CFG)
    assert out == "[Error]: No category named 'Nope' in the master list. outlook_list_categories shows what exists."
    assert len(calls) == 3


def test_list_categories_renders_colours_and_empty_list(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"value": [
            {"id": "CAT1", "displayName": "To Respond", "color": "preset0"},
            {"id": "CAT2", "displayName": "FYI", "color": "preset22"},
        ]}),
        (True, {"value": []}),
    ))
    out = oo.outlook_list_categories.invoke({"mailbox": "s@x.com"}, config=_CFG)
    assert out == "\n".join([
        "[Success]: 2 categories in the master list:",
        "  - To Respond: red (preset0) id=CAT1",
        "  - FYI: dark blue (preset22) id=CAT2",
    ])
    assert calls[0]["endpoint"] == "/me/outlook/masterCategories" and calls[0]["mailbox"] == "s@x.com"
    assert oo.outlook_list_categories.invoke({}, config=_CFG) == (
        '[Info]: The master category list is empty. outlook_manage_categories(action="create") adds one.'
    )


# ---------------------------------------------------------------------------
# Scope gate wiring (B3 as seen from these tools)
# ---------------------------------------------------------------------------


_SETTINGS_TOOLS = [
    (oo.outlook_list_categories, {}),
    (oo.outlook_manage_categories, {"action": "create", "name": "X", "color": "red"}),
    (oo.outlook_list_rules, {}),
    (oo.outlook_manage_rule, {"action": "delete", "name": "X"}),
    (oo.outlook_get_mailbox_settings, {}),
    (oo.outlook_set_auto_reply, {"status": "disabled"}),
]
_MAIL_ONLY_TOOLS = [
    (oo.outlook_list_folders, {}),
    (oo.outlook_manage_folder, {"action": "create", "name": "X"}),
    (oo.outlook_flag_email, {"email_id": "m1"}),
    (oo.outlook_focused_overrides, {"action": "list"}),
    (oo.outlook_unsubscribe, {"email_id": "m1"}),
]


@pytest.mark.parametrize("tool,args", _SETTINGS_TOOLS + _MAIL_ONLY_TOOLS, ids=lambda v: getattr(v, "name", ""))
def test_a_closed_gate_stops_every_tool_before_any_graph_request(monkeypatch, tool, args):
    _closed_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence())
    batches = _record_batch(monkeypatch)
    assert tool.invoke({**args, "mailbox": "s@x.com"}, config=_CFG) == f"[Error]: {_SCOPE_MSG}"
    assert calls == [] and batches == []


def test_settings_tools_ask_for_the_mailbox_settings_scope_and_mail_tools_do_not(monkeypatch):
    asked = _open_gate(monkeypatch)
    _fake_graph(monkeypatch, lambda call: (True, {"value": [], "timeZone": "UTC"}))
    _record_batch(monkeypatch)
    for tool, args in _SETTINGS_TOOLS:
        tool.invoke({**args, "mailbox": "s@x.com"}, config=_CFG)
        assert asked[-1]["needed"] == [og.SCOPE_MAILBOX_SETTINGS], tool.name
        assert asked[-1]["mailbox"] == "s@x.com", tool.name
    for tool, args in _MAIL_ONLY_TOOLS:
        tool.invoke({**args, "mailbox": "s@x.com"}, config=_CFG)
        assert asked[-1]["needed"] == [], tool.name
        assert asked[-1]["mailbox"] == "s@x.com", tool.name


def test_the_real_gate_names_the_missing_scope_for_a_settings_tool(monkeypatch):
    """Unpatched ``require_scopes`` over a credential that lacks the scope."""
    cache = {"accounts": {"A": {
        "email": "me@x.com", "access_token": "tok", "expires_at": time.time() + 3600,
        "scopes": ["Mail.ReadWrite", "Mail.Send"],
    }}}
    source = SimpleNamespace(cache=cache, accounts=cache["accounts"], persist=lambda c: None, thread_bound=False)
    monkeypatch.setattr(og.auth_utils, "resolve_oauth_cache", lambda *a, **k: source)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: SimpleNamespace(outlook_default_account_id=None))
    calls = _fake_graph(monkeypatch, _sequence())
    out = oo.outlook_list_rules.invoke({}, config=_CFG)
    assert out.startswith("[Error]: Outlook account 'me@x.com' was connected without the permission(s) needed for listing inbox rules: MailboxSettings.ReadWrite.")
    assert 'request_credential(provider="outlook", kind="oauth")' in out
    assert calls == []


# ---------------------------------------------------------------------------
# Follow-up flags (B15)
# ---------------------------------------------------------------------------


def test_flag_with_due_reads_the_mailbox_zone_once_and_patches_start_and_due(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"timeZone": "AUS Eastern Standard Time"})))
    batches = _record_batch(monkeypatch)
    out = oo.outlook_flag_email.invoke({"email_ids": "m1,m2", "due": "2026-09-20", "mailbox": "s@x.com"}, config=_CFG)
    assert calls == [{
        "method": "GET", "endpoint": "/me/mailboxSettings", "json": None, "params": {"$select": "timeZone"},
        "mailbox": "s@x.com", "account_id": None,
    }]
    when = {"dateTime": "2026-09-20T00:00:00", "timeZone": "AUS Eastern Standard Time"}
    flag = {"flagStatus": "flagged", "startDateTime": when, "dueDateTime": when}
    assert batches[0]["requests"] == [
        {"id": "0", "method": "PATCH", "url": "/me/messages/m1", "body": {"flag": flag}},
        {"id": "1", "method": "PATCH", "url": "/me/messages/m2", "body": {"flag": flag}},
    ]
    assert batches[0]["mailbox"] == "s@x.com"
    assert out == "[Success]: 2 messages flagged for follow-up (due 2026-09-20)."


def test_flag_with_distinct_start_and_due_datetimes(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence((True, {"timeZone": "UTC"})))
    batches = _record_batch(monkeypatch)
    out = oo.outlook_flag_email.invoke({"email_id": "m1", "start": "2026-09-18T09:00", "due": "2026-09-20T17:30"}, config=_CFG)
    flag = batches[0]["requests"][0]["body"]["flag"]
    assert flag["startDateTime"] == {"dateTime": "2026-09-18T09:00:00", "timeZone": "UTC"}
    assert flag["dueDateTime"] == {"dateTime": "2026-09-20T17:30:00", "timeZone": "UTC"}
    assert out == "[Success]: 1 message flagged for follow-up (due 2026-09-20)."


def test_flag_plain_clear_and_complete_send_no_dates_and_skip_the_zone_read(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence())
    batches = _record_batch(monkeypatch)
    assert oo.outlook_flag_email.invoke({"email_id": "m1"}, config=_CFG) == "[Success]: 1 message flagged for follow-up."
    assert batches[0]["requests"][0]["body"] == {"flag": {"flagStatus": "flagged"}}
    assert oo.outlook_flag_email.invoke({"email_id": "m1", "status": "clear"}, config=_CFG) == "[Success]: 1 message unflagged."
    assert batches[1]["requests"][0]["body"] == {"flag": {"flagStatus": "notFlagged"}}
    assert oo.outlook_flag_email.invoke({"email_id": "m1", "status": "complete"}, config=_CFG) == (
        "[Success]: 1 message marked follow-up complete."
    )
    assert batches[2]["requests"][0]["body"] == {"flag": {"flagStatus": "complete"}}
    assert calls == []


def test_flag_zone_read_failure_falls_back_to_utc(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence((False, "API Error (403): ErrorAccessDenied")))
    batches = _record_batch(monkeypatch)
    oo.outlook_flag_email.invoke({"email_id": "m1", "due": "2026-09-20"}, config=_CFG)
    assert batches[0]["requests"][0]["body"]["flag"]["dueDateTime"] == {"dateTime": "2026-09-20T00:00:00", "timeZone": "UTC"}


def test_flag_offset_datetime_is_converted_to_utc(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence((True, {"timeZone": "AUS Eastern Standard Time"})))
    batches = _record_batch(monkeypatch)
    oo.outlook_flag_email.invoke({"email_id": "m1", "due": "2026-09-20T09:00:00+10:00", "start": "2026-09-19T00:00:00Z"}, config=_CFG)
    flag = batches[0]["requests"][0]["body"]["flag"]
    assert flag["dueDateTime"] == {"dateTime": "2026-09-19T23:00:00", "timeZone": "UTC"}
    assert flag["startDateTime"] == {"dateTime": "2026-09-19T00:00:00", "timeZone": "UTC"}


def test_flag_validation_refuses_before_any_batch(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, lambda call: (True, {"timeZone": "UTC"}))
    batches = _record_batch(monkeypatch)
    inv = oo.outlook_flag_email.invoke
    assert inv({"email_id": "m1", "status": "done"}, config=_CFG) == "[Error]: status must be 'flagged', 'complete', or 'clear'."
    assert inv({"email_id": "m1", "status": "clear", "due": "2026-09-20"}, config=_CFG) == (
        '[Error]: due and start apply only to status="flagged".'
    )
    assert inv({"email_id": "m1", "due": "next friday"}, config=_CFG) == (
        "[Error]: due must be YYYY-MM-DD or an ISO datetime such as 2026-09-20T09:00 (got 'next friday')."
    )
    assert inv({"email_id": "m1", "due": "2026-09-20", "start": "soon"}, config=_CFG) == (
        "[Error]: start must be YYYY-MM-DD or an ISO datetime such as 2026-09-20T09:00 (got 'soon')."
    )
    assert inv({}, config=_CFG) == "[Error]: Provide an email_id, comma-separated email_ids, or a conversation_id."
    assert inv({"email_ids": ",".join(f"m{i}" for i in range(51))}, config=_CFG).startswith("[Error]: 51 messages selected")
    assert batches == []


def test_flag_conversation_expands_targets_and_reports_partial_failure(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence(
        (True, {"value": [{"id": "m1", "parentFolderId": "IN"}, {"id": "m2", "parentFolderId": "DEL"}, {"id": "m3", "parentFolderId": "IN"}]}),
        (True, {"id": "DEL"}),
    ))
    batches = _record_batch(monkeypatch, lambda r: "FAIL" if r["url"].endswith("/m3") else None)
    out = oo.outlook_flag_email.invoke({"conversation_id": "conv1"}, config=_CFG)
    assert [r["url"] for r in batches[0]["requests"]] == ["/me/messages/m1", "/me/messages/m3"]
    assert out.startswith("[Partial]: 1/2 messages flagged for follow-up; 1 failed:")
    assert "m3...: API Error (404): gone" in out


# ---------------------------------------------------------------------------
# Inbox rules (B16)
# ---------------------------------------------------------------------------


def test_create_rule_resolves_the_folder_name_and_appends_after_the_highest_sequence(monkeypatch):
    asked = _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"value": [_folder("NEWS", "Newsletters")]}),
        (True, {"value": [{"id": "r1", "sequence": 1}, {"id": "r2", "sequence": 3}]}),
        (True, {"id": "RULE9"}),
    ))
    out = oo.outlook_manage_rule.invoke({
        "action": "create", "name": "File newsletters",
        "conditions_json": '{"senderContains": ["newsletter"]}',
        "actions_json": '{"moveToFolder": "Newsletters", "markAsRead": true}',
    }, config=_CFG)
    assert [c["endpoint"] for c in calls] == [
        "/me/mailFolders", "/me/mailFolders/inbox/messageRules", "/me/mailFolders/inbox/messageRules",
    ]
    assert calls[2]["method"] == "POST"
    assert calls[2]["json"] == {
        "displayName": "File newsletters", "sequence": 4, "isEnabled": True,
        "actions": {"moveToFolder": "NEWS", "markAsRead": True},
        "conditions": {"senderContains": ["newsletter"]},
    }
    assert out == "\n".join([
        "[Success]: Rule 'File newsletters' created.",
        "[on] 'File newsletters' (sequence 4) id=RULE9",
        "    when: senderContains newsletter",
        "    do: moveToFolder Newsletters; markAsRead true",
    ])
    assert asked[0]["needed"] == ["MailboxSettings.ReadWrite"] and asked[0]["purpose"] == "inbox rule management"


def test_create_rule_with_an_unresolvable_folder_refuses_without_creating(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"value": []})))
    out = oo.outlook_manage_rule.invoke({
        "action": "create", "name": "X", "actions_json": '{"moveToFolder": "Nope"}',
    }, config=_CFG)
    assert out.startswith("[Error]: actions_json field 'moveToFolder': No folder named 'Nope' under the mailbox root.")
    assert [c["method"] for c in calls] == ["GET"]


def test_rule_json_refusals_name_the_argument_and_send_nothing(monkeypatch):
    asked = _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence())
    inv = oo.outlook_manage_rule.invoke
    out = inv({"action": "create", "name": "X", "conditions_json": "{not json", "actions_json": '{"delete": true}'}, config=_CFG)
    assert out.startswith("[Error]: conditions_json is not valid JSON: Expecting property name enclosed in double quotes")
    out = inv({"action": "create", "name": "X", "actions_json": '["delete"]'}, config=_CFG)
    assert out == "[Error]: actions_json must be a JSON object (got list)."
    out = inv({"action": "create", "name": "X", "exceptions_json": "[1", "actions_json": '{"delete": true}'}, config=_CFG)
    assert out.startswith("[Error]: exceptions_json is not valid JSON:")
    out = inv({"action": "create", "name": "X", "conditions_json": '{"fromContains": ["x"]}', "actions_json": '{"delete": true}'}, config=_CFG)
    assert out.startswith("[Error]: conditions_json has unknown field(s): fromContains. Known fields: bodyContains, bodyOrSubjectContains, categories, fromAddresses, ")
    out = inv({"action": "create", "name": "X", "actions_json": '{"moveTo": "x", "zap": 1}'}, config=_CFG)
    assert out.startswith("[Error]: actions_json has unknown field(s): moveTo, zap. Known fields: assignCategories, copyToFolder, delete, ")
    assert inv({"action": "create", "name": "X", "actions_json": '{"markAsRead": "yes"}'}, config=_CFG) == (
        "[Error]: actions_json field 'markAsRead' must be true or false."
    )
    assert inv({"action": "create", "actions_json": '{"delete": true}'}, config=_CFG) == "[Error]: name is required to create a rule."
    assert inv({"action": "create", "name": "X"}, config=_CFG) == "[Error]: actions_json is required to create a rule (at least one action)."
    assert inv({"action": "create", "name": "X", "conditions_json": '{"fromAddresses": []}', "actions_json": '{"delete": true}'}, config=_CFG) == (
        "[Error]: conditions_json field 'fromAddresses' needs at least one email address."
    )
    assert inv({"action": "toggle", "name": "X"}, config=_CFG) == (
        "[Error]: action must be 'create', 'update', 'enable', 'disable', or 'delete'."
    )
    assert calls == [] and asked == []  # refused before the gate, before Graph


def test_create_rule_shapes_recipients_categories_and_well_known_folders(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, {"id": "ARCH", "displayName": "Archive"}),
        (True, {"id": "R"}),
    ))
    out = oo.outlook_manage_rule.invoke({
        "action": "create", "name": "Filing", "sequence": 7, "enabled": False,
        "conditions_json": '{"fromAddresses": "Ann <ann@x.com>, bob@y.com", "hasAttachments": "true", "importance": "high"}',
        "exceptions_json": '{"subjectContains": "urgent"}',
        "actions_json": '{"copyToFolder": "archive", "assignCategories": "FYI", "forwardTo": ["Cc Bot <bot@z.com>"], "stopProcessingRules": true}',
        "allow_forwarding": True,
    }, config=_CFG)
    assert calls[0]["method"] == "GET" and calls[0]["endpoint"] == "/me/mailFolders/archive"
    assert calls[0]["params"] == {"$select": "id,displayName"}
    body = calls[1]["json"]
    assert body["conditions"] == {
        "fromAddresses": [{"emailAddress": {"address": "ann@x.com", "name": "Ann"}}, {"emailAddress": {"address": "bob@y.com"}}],
        "hasAttachments": True, "importance": "high",
    }
    assert body["exceptions"] == {"subjectContains": ["urgent"]}
    assert body["actions"] == {
        "copyToFolder": "ARCH", "assignCategories": ["FYI"],
        "forwardTo": [{"emailAddress": {"address": "bot@z.com", "name": "Cc Bot"}}], "stopProcessingRules": True,
    }
    assert body["sequence"] == 7 and body["isEnabled"] is False
    assert len(calls) == 2  # an explicit sequence needs no rules read
    assert out.splitlines()[1:] == [
        "[off] 'Filing' (sequence 7) id=R",
        "    when: fromAddresses ann@x.com, bob@y.com; hasAttachments true; importance high",
        "    except: subjectContains urgent",
        "    do: copyToFolder Archive; assignCategories FYI; forwardTo bot@z.com; stopProcessingRules true",
    ]


def test_update_enable_disable_and_delete_identify_the_rule_by_name_or_id(monkeypatch):
    _open_gate(monkeypatch)
    rules = {"value": [
        {"id": "R1", "displayName": "Old", "sequence": 1, "isEnabled": True},
        {"id": "R2", "displayName": "Other", "sequence": 2, "isEnabled": False},
    ]}
    calls = _fake_graph(monkeypatch, _sequence(
        (True, rules), (True, {}), (True, rules), (True, {}), (True, rules), (True, {}), (True, rules), (True, {}),
    ))
    inv = oo.outlook_manage_rule.invoke
    out = inv({"action": "update", "name": "old", "new_name": "Newer", "actions_json": '{"markAsRead": true}'}, config=_CFG)
    assert out == "[Success]: Rule 'Old' updated (displayName, actions)."
    assert calls[1]["method"] == "PATCH" and calls[1]["endpoint"] == "/me/mailFolders/inbox/messageRules/R1"
    assert calls[1]["json"] == {"displayName": "Newer", "actions": {"markAsRead": True}}

    assert inv({"action": "disable", "name": "Old"}, config=_CFG) == "[Success]: Rule 'Old' disabled."
    assert calls[3]["json"] == {"isEnabled": False} and calls[3]["endpoint"].endswith("/R1")
    assert inv({"action": "enable", "rule_id": "R2"}, config=_CFG) == "[Success]: Rule 'Other' enabled."
    assert calls[5]["json"] == {"isEnabled": True} and calls[5]["endpoint"].endswith("/R2")
    assert inv({"action": "delete", "name": "Old"}, config=_CFG) == "[Success]: Rule 'Old' deleted."
    assert calls[7]["method"] == "DELETE" and calls[7]["endpoint"].endswith("/R1")


def test_rule_identification_errors_and_empty_update(monkeypatch):
    _open_gate(monkeypatch)
    rules = {"value": [
        {"id": "R1", "displayName": "Dup", "sequence": 1},
        {"id": "R2", "displayName": "dup", "sequence": 2},
    ]}
    calls = _fake_graph(monkeypatch, lambda call: (True, rules))
    inv = oo.outlook_manage_rule.invoke
    assert inv({"action": "delete", "name": "Nope"}, config=_CFG) == "[Error]: No inbox rule named 'Nope'. outlook_list_rules shows them."
    out = inv({"action": "delete", "name": "DUP"}, config=_CFG)
    assert out.startswith("[Error]: Rule name 'DUP' is ambiguous: Dup (id R1); dup (id R2). Pass rule_id.")
    assert inv({"action": "delete", "rule_id": "R9"}, config=_CFG) == "[Error]: No inbox rule with id 'R9'. outlook_list_rules shows them."
    assert inv({"action": "delete"}, config=_CFG) == "[Error]: name (or rule_id) is required to identify the rule."
    assert inv({"action": "update", "name": "Dup"}, config=_CFG) == (
        "[Error]: Nothing to update. Provide new_name, conditions_json, exceptions_json, actions_json, or sequence."
    )
    assert all(c["method"] == "GET" for c in calls)  # exact-case 'Dup' resolved, yet nothing was written


def test_list_rules_renders_in_sequence_order_with_folder_names(monkeypatch):
    _open_gate(monkeypatch)
    rules = {"value": [
        {"id": "R2", "displayName": "Second", "sequence": 2, "isEnabled": False, "hasError": True,
         "conditions": {"fromAddresses": [{"emailAddress": {"address": "a@x.com"}}]},
         "actions": {"delete": True, "stopProcessingRules": True}},
        {"id": "R1", "displayName": "First", "sequence": 1, "isEnabled": True, "isReadOnly": True,
         "conditions": {"subjectContains": ["invoice", "receipt"], "hasAttachments": True},
         "exceptions": {"senderContains": ["boss"]},
         "actions": {"moveToFolder": "FIN", "assignCategories": ["Finance"]}},
    ]}
    calls = _fake_graph(monkeypatch, _sequence((True, rules)))
    batches = _record_batch(monkeypatch, lambda r: {"id": "FIN", "displayName": "Finance"} if "/FIN?" in r["url"] else "FAIL")
    out = oo.outlook_list_rules.invoke({"mailbox": "s@x.com"}, config=_CFG)
    assert calls[0]["endpoint"] == "/me/mailFolders/inbox/messageRules" and calls[0]["mailbox"] == "s@x.com"
    assert batches[0]["requests"] == [{"id": "0", "method": "GET", "url": "/me/mailFolders/FIN?$select=id,displayName"}]
    assert out == "\n".join([
        "[Success]: 2 inbox rule(s):",
        "[on] 'First' (sequence 1) [read-only] id=R1",
        "    when: subjectContains invoice, receipt; hasAttachments true",
        "    except: senderContains boss",
        "    do: moveToFolder Finance; assignCategories Finance",
        "[off] 'Second' (sequence 2) [error] id=R2",
        "    when: fromAddresses a@x.com",
        "    do: delete true; stopProcessingRules true",
    ])


def test_list_rules_empty_no_folder_lookup_and_lookup_failure(monkeypatch):
    _open_gate(monkeypatch)
    batches = _record_batch(monkeypatch, lambda r: "FAIL")
    _fake_graph(monkeypatch, _sequence((True, {"value": []})))
    assert oo.outlook_list_rules.invoke({}, config=_CFG) == (
        '[Info]: No inbox rules are defined. outlook_manage_rule(action="create") adds one.'
    )
    _fake_graph(monkeypatch, _sequence((True, {"value": [{"id": "R", "displayName": "Any", "sequence": 1, "isEnabled": True, "actions": {"markAsRead": True}}]})))
    out = oo.outlook_list_rules.invoke({}, config=_CFG)
    assert batches == []  # no folder actions, no lookup
    assert out.splitlines()[2:] == ["    when: (always)", "    do: markAsRead true"]
    _fake_graph(monkeypatch, _sequence((True, {"value": [{"id": "R", "displayName": "Mv", "sequence": 1, "isEnabled": True, "actions": {"moveToFolder": "GONE"}}]})))
    out = oo.outlook_list_rules.invoke({}, config=_CFG)
    assert out.splitlines()[-1] == "    do: moveToFolder GONE"  # id shown when the name cannot be read


# ---------------------------------------------------------------------------
# Mailbox settings and automatic replies (B17)
# ---------------------------------------------------------------------------


def test_get_mailbox_settings_renders_every_section(monkeypatch):
    _open_gate(monkeypatch)
    settings = {
        "timeZone": "AUS Eastern Standard Time",
        "language": {"locale": "en-AU", "displayName": "English (Australia)"},
        "dateFormat": "dd/MM/yyyy", "timeFormat": "h:mm tt",
        "workingHours": {"daysOfWeek": ["monday", "tuesday"], "startTime": "08:00:00.0000000", "endTime": "17:00:00.0000000",
                         "timeZone": {"name": "AUS Eastern Standard Time"}},
        "automaticRepliesSetting": {
            "status": "scheduled", "externalAudience": "all",
            "internalReplyMessage": "<p>Away until <b>Monday</b></p>", "externalReplyMessage": "Out of office",
            "scheduledStartDateTime": {"dateTime": "2026-09-20T09:00:00.0000000", "timeZone": "AUS Eastern Standard Time"},
            "scheduledEndDateTime": {"dateTime": "2026-09-27T09:00:00.0000000", "timeZone": "AUS Eastern Standard Time"},
        },
        "archiveFolder": "ARCHIVEID", "userPurpose": "user",
    }
    calls = _fake_graph(monkeypatch, _sequence((True, settings)))
    out = oo.outlook_get_mailbox_settings.invoke({"mailbox": "s@x.com"}, config=_CFG)
    assert calls == [{"method": "GET", "endpoint": "/me/mailboxSettings", "json": None, "params": {}, "mailbox": "s@x.com", "account_id": None}]
    assert out == "\n".join([
        "[Success]: Mailbox settings:",
        "Time zone: AUS Eastern Standard Time",
        "Language: English (Australia) (en-AU)",
        "Date/time format: dd/MM/yyyy / h:mm tt",
        "Working hours: monday, tuesday, 08:00 to 17:00 (AUS Eastern Standard Time)",
        "Automatic replies: scheduled",
        "  Window: 2026-09-20T09:00 to 2026-09-27T09:00 (AUS Eastern Standard Time)",
        "  External audience: all",
        "  Internal message: Away until Monday",
        "  External message: Out of office",
        "Archive folder id: ARCHIVEID",
        "Mailbox type: user",
    ])


def test_get_mailbox_settings_minimal_shape(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence((True, {"timeZone": "UTC", "automaticRepliesSetting": {"status": "disabled"}})))
    out = oo.outlook_get_mailbox_settings.invoke({}, config=_CFG)
    assert out == "[Success]: Mailbox settings:\nTime zone: UTC\nAutomatic replies: disabled"
    _fake_graph(monkeypatch, _sequence((False, "API Error (403): ErrorAccessDenied")))
    assert oo.outlook_get_mailbox_settings.invoke({}, config=_CFG) == "[Error]: API Error (403): ErrorAccessDenied"


def test_set_auto_reply_scheduled_patches_only_the_automatic_replies_setting(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"timeZone": "AUS Eastern Standard Time"}), (True, {})))
    out = oo.outlook_set_auto_reply.invoke({
        "status": "scheduled", "internal_message": "Away", "external_message": "Out",
        "start": "2026-09-20T09:00", "end": "2026-09-27", "external_audience": "contacts", "mailbox": "s@x.com",
    }, config=_CFG)
    assert calls[0]["endpoint"] == "/me/mailboxSettings" and calls[0]["params"] == {"$select": "timeZone"}
    assert calls[1]["method"] == "PATCH" and calls[1]["endpoint"] == "/me/mailboxSettings" and calls[1]["mailbox"] == "s@x.com"
    assert calls[1]["json"] == {"automaticRepliesSetting": {
        "status": "scheduled", "internalReplyMessage": "Away", "externalAudience": "contactsOnly", "externalReplyMessage": "Out",
        "scheduledStartDateTime": {"dateTime": "2026-09-20T09:00:00", "timeZone": "AUS Eastern Standard Time"},
        "scheduledEndDateTime": {"dateTime": "2026-09-27T00:00:00", "timeZone": "AUS Eastern Standard Time"},
    }}
    assert out == (
        "[Success]: Automatic replies scheduled from 2026-09-20T09:00:00 to 2026-09-27T00:00:00 "
        "(AUS Eastern Standard Time); external audience: contactsOnly."
    )


def test_set_auto_reply_disabled_sends_only_the_status(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {})))
    out = oo.outlook_set_auto_reply.invoke({
        "status": "Disabled", "internal_message": "ignored", "external_message": "ignored",
        "start": "2026-01-01", "end": "2026-01-02", "external_audience": "all",
    }, config=_CFG)
    assert out == "[Success]: Automatic replies turned off."
    assert calls == [{"method": "PATCH", "endpoint": "/me/mailboxSettings", "params": {}, "mailbox": None, "account_id": None,
                      "json": {"automaticRepliesSetting": {"status": "disabled"}}}]


def test_set_auto_reply_always_enabled_defaults_the_external_side(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, lambda call: (True, {}))
    inv = oo.outlook_set_auto_reply.invoke
    out = inv({"status": "alwaysEnabled", "internal_message": "Away", "external_message": "Out"}, config=_CFG)
    assert calls[-1]["json"] == {"automaticRepliesSetting": {
        "status": "alwaysEnabled", "internalReplyMessage": "Away", "externalAudience": "all", "externalReplyMessage": "Out",
    }}
    assert out == "[Success]: Automatic replies enabled until turned off; external audience: all."
    out = inv({"status": "alwaysEnabled", "internal_message": "Away"}, config=_CFG)
    assert calls[-1]["json"] == {"automaticRepliesSetting": {
        "status": "alwaysEnabled", "internalReplyMessage": "Away", "externalReplyMessage": "Away",
    }}
    assert out == "[Success]: Automatic replies enabled until turned off."
    inv({"status": "alwaysEnabled", "internal_message": "Away", "external_audience": "none"}, config=_CFG)
    assert calls[-1]["json"] == {"automaticRepliesSetting": {
        "status": "alwaysEnabled", "internalReplyMessage": "Away", "externalAudience": "none",
    }}


def test_set_auto_reply_validation_sends_nothing(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, lambda call: (True, {"timeZone": "UTC"}))
    inv = oo.outlook_set_auto_reply.invoke
    assert inv({"status": "on"}, config=_CFG) == "[Error]: status must be 'disabled', 'alwaysEnabled', or 'scheduled'."
    assert inv({"status": "alwaysEnabled"}, config=_CFG) == '[Error]: internal_message is required for status="alwaysEnabled".'
    assert inv({"status": "scheduled", "internal_message": "x", "start": "2026-09-20"}, config=_CFG) == (
        '[Error]: start and end are required for status="scheduled".'
    )
    assert inv({"status": "alwaysEnabled", "internal_message": "x", "external_audience": "everyone"}, config=_CFG) == (
        "[Error]: external_audience must be 'none', 'contactsOnly', or 'all'."
    )
    assert inv({"status": "scheduled", "internal_message": "x", "start": "2026-09-27", "end": "2026-09-20"}, config=_CFG) == (
        "[Error]: end must be after start."
    )
    assert inv({"status": "scheduled", "internal_message": "x", "start": "yesterday", "end": "2026-09-20"}, config=_CFG) == (
        "[Error]: start must be YYYY-MM-DD or an ISO datetime such as 2026-09-20T09:00 (got 'yesterday')."
    )
    assert all(c["method"] == "GET" for c in calls)


# ---------------------------------------------------------------------------
# Focused / Other overrides (B18)
# ---------------------------------------------------------------------------


def test_focused_override_set_posts_the_sender_and_classification(monkeypatch):
    asked = _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"id": "OV1"}), (True, {"id": "OV2"})))
    out = oo.outlook_focused_overrides.invoke({"action": "set", "sender": "a@b.com", "classify": "other", "mailbox": "s@x.com"}, config=_CFG)
    assert out == "[Success]: Mail from a@b.com will always land in Other. id=OV1"
    assert calls[0] == {
        "method": "POST", "endpoint": "/me/inferenceClassification/overrides", "params": {}, "mailbox": "s@x.com", "account_id": None,
        "json": {"classifyAs": "other", "senderEmailAddress": {"address": "a@b.com"}},
    }
    oo.outlook_focused_overrides.invoke({"action": "set", "sender": "Ann <ann@b.com>", "classify": "Focused"}, config=_CFG)
    assert calls[1]["json"] == {"classifyAs": "focused", "senderEmailAddress": {"address": "ann@b.com", "name": "Ann"}}
    assert asked[0]["needed"] == [] and asked[0]["mailbox"] == "s@x.com"


def test_focused_override_list_renders_and_empty(monkeypatch):
    _open_gate(monkeypatch)
    _fake_graph(monkeypatch, _sequence(
        (True, {"value": [
            {"id": "OV1", "classifyAs": "other", "senderEmailAddress": {"address": "a@b.com", "name": "Ann"}},
            {"id": "OV2", "classifyAs": "focused", "senderEmailAddress": {"address": "c@d.com"}},
        ]}),
        (True, {"value": []}),
    ))
    assert oo.outlook_focused_overrides.invoke({}, config=_CFG) == "\n".join([
        "[Success]: 2 sender override(s):",
        "  - a@b.com (Ann): other id=OV1",
        "  - c@d.com: focused id=OV2",
    ])
    assert oo.outlook_focused_overrides.invoke({"action": "list"}, config=_CFG) == "[Info]: No Focused/Other sender overrides are set."


def test_focused_override_remove_resolves_the_id_then_deletes(monkeypatch):
    _open_gate(monkeypatch)
    listing = {"value": [{"id": "OV1", "classifyAs": "other", "senderEmailAddress": {"address": "A@B.com"}}]}
    calls = _fake_graph(monkeypatch, _sequence((True, listing), (True, {}), (True, listing)))
    out = oo.outlook_focused_overrides.invoke({"action": "remove", "sender": "a@b.com"}, config=_CFG)
    assert out == "[Success]: Override for a@b.com removed; Outlook's own Focused/Other classification applies again."
    assert calls[0]["method"] == "GET" and calls[0]["endpoint"] == "/me/inferenceClassification/overrides"
    assert calls[1]["method"] == "DELETE" and calls[1]["endpoint"] == "/me/inferenceClassification/overrides/OV1"
    assert oo.outlook_focused_overrides.invoke({"action": "remove", "sender": "zz@b.com"}, config=_CFG) == (
        "[Info]: No override exists for zz@b.com."
    )
    assert len(calls) == 3


def test_focused_override_validation(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence())
    inv = oo.outlook_focused_overrides.invoke
    assert inv({"action": "set", "sender": "a@b.com"}, config=_CFG) == "[Error]: classify must be 'focused' or 'other'."
    assert inv({"action": "set", "classify": "other"}, config=_CFG) == "[Error]: sender is required to set an override."
    assert inv({"action": "remove"}, config=_CFG) == "[Error]: sender is required to remove an override."
    assert inv({"action": "pin"}, config=_CFG) == "[Error]: action must be 'list', 'set', or 'remove'."
    assert calls == []


# ---------------------------------------------------------------------------
# Unsubscribe (B19)
# ---------------------------------------------------------------------------


def _message(*headers):
    return {
        "subject": "Weekly deals",
        "from": {"emailAddress": {"address": "news@shop.com"}},
        "internetMessageHeaders": [{"name": n, "value": v} for n, v in headers],
    }


def _no_one_click(monkeypatch):
    """Fail loudly if the tool reaches the one-click POST when it must not."""
    def _boom(*a, **k):
        raise AssertionError("one-click POST must not be issued")
    monkeypatch.setattr(oo, "_request_with_policy", _boom)


def _record_one_click(monkeypatch, response=None, raises=None):
    posts = []

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake(client, method, url, **kw):
        posts.append((method, url, kw))
        if raises:
            raise raises
        return response or SimpleNamespace(status_code=200)

    monkeypatch.setattr(oo, "_http_client", lambda **kw: _Client())
    monkeypatch.setattr(oo, "_request_with_policy", _fake)
    return posts


def test_unsubscribe_mailto_sends_from_the_account_with_the_header_subject(monkeypatch):
    _open_gate(monkeypatch)
    _no_one_click(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, _message(("List-Unsubscribe", "<mailto:unsub@shop.com?subject=Remove%20me>"))),
        (True, {}),
    ))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1", "mailbox": "s@x.com"}, config=_CFG)
    assert calls[0] == {
        "method": "GET", "endpoint": "/me/messages/m1", "json": None,
        "params": {"$select": "internetMessageHeaders,subject,from"}, "mailbox": "s@x.com", "account_id": None,
    }
    assert calls[1]["method"] == "POST" and calls[1]["endpoint"] == "/me/sendMail" and calls[1]["mailbox"] == "s@x.com"
    assert calls[1]["json"] == {"message": {
        "subject": "Remove me",
        "body": {"contentType": "Text", "content": "unsubscribe"},
        "toRecipients": [{"emailAddress": {"address": "unsub@shop.com"}}],
    }}
    assert out == (
        "[Success]: Unsubscribe email for 'Weekly deals' from news@shop.com sent to unsub@shop.com with subject "
        "'Remove me'. Method: mailto."
    )


def test_unsubscribe_mailto_defaults_the_subject_and_accepts_bare_header_values(monkeypatch):
    _open_gate(monkeypatch)
    _no_one_click(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence(
        (True, _message(("list-unsubscribe", "mailto:unsub@shop.com, https://shop.com/u"))),
        (True, {}),
    ))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1", "method": "mailto"}, config=_CFG)
    assert calls[1]["json"]["message"]["subject"] == "unsubscribe"
    assert calls[1]["json"]["message"]["toRecipients"] == [{"emailAddress": {"address": "unsub@shop.com"}}]
    assert out.endswith("with subject 'unsubscribe'. Method: mailto.")


def test_unsubscribe_one_click_posts_the_rfc_8058_body_through_the_policy_client(monkeypatch):
    _open_gate(monkeypatch)
    posts = _record_one_click(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, _message(
        ("List-Unsubscribe", "<https://shop.com/u?t=1>, <mailto:unsub@shop.com>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    ))))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1"}, config=_CFG)
    assert posts == [("POST", "https://shop.com/u?t=1", {"data": {"List-Unsubscribe": "One-Click"}})]
    assert len(calls) == 1  # auto prefers one-click over mailto: no sendMail
    assert out == (
        "[Success]: One-click unsubscribe sent for 'Weekly deals' from news@shop.com "
        "(POST https://shop.com/u?t=1, HTTP 200). Method: one_click."
    )


def test_unsubscribe_one_click_to_a_private_address_is_refused_by_the_real_policy(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, _message(
        ("List-Unsubscribe", "<https://10.0.0.5/unsub>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    ))))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1", "method": "one_click"}, config=_CFG)
    assert out.startswith(
        "[Error]: One-click unsubscribe for 'Weekly deals' was not sent: HTTP request blocked by egress policy: "
        "URL blocked by HTTP egress policy (private_network): https://10.0.0.5/unsub"
    )
    assert len(calls) == 1


def test_unsubscribe_one_click_non_success_and_transport_failure(monkeypatch):
    _open_gate(monkeypatch)
    headers = (("List-Unsubscribe", "<https://shop.com/u?t=1>"), ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"))
    _fake_graph(monkeypatch, lambda call: (True, _message(*headers)))
    _record_one_click(monkeypatch, response=SimpleNamespace(status_code=500))
    assert oo.outlook_unsubscribe.invoke({"email_id": "m1"}, config=_CFG) == (
        "[Error]: One-click unsubscribe for 'Weekly deals' was not sent: the unsubscribe endpoint answered HTTP 500."
    )
    _record_one_click(monkeypatch, response=SimpleNamespace(status_code=302))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1"}, config=_CFG)
    assert out.startswith("[Error]: One-click unsubscribe for 'Weekly deals' was not sent: the unsubscribe endpoint answered HTTP 302 (a redirect, which RFC 8058 forbids")
    assert "open the link in a browser instead" in out
    _record_one_click(monkeypatch, raises=OSError("boom"))
    assert oo.outlook_unsubscribe.invoke({"email_id": "m1"}, config=_CFG) == (
        "[Error]: One-click unsubscribe for 'Weekly deals' was not sent: request to https://shop.com/u?t=1 failed: boom."
    )


def test_unsubscribe_https_without_post_header_returns_the_link_unfetched(monkeypatch):
    _open_gate(monkeypatch)
    _no_one_click(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, _message(("List-Unsubscribe", "<https://shop.com/u>")))))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1"}, config=_CFG)
    assert out == (
        "[Info]: 'Weekly deals' from news@shop.com offers a link-based unsubscribe only (no one-click support). "
        "Open this link to complete it: https://shop.com/u"
    )
    assert len(calls) == 1


def test_unsubscribe_without_a_header_says_so(monkeypatch):
    _open_gate(monkeypatch)
    _no_one_click(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, _message(("X-Mailer", "Foo")))))
    out = oo.outlook_unsubscribe.invoke({"email_id": "m1"}, config=_CFG)
    assert out == (
        "[Info]: 'Weekly deals' from news@shop.com carries no List-Unsubscribe header, so there is nothing to "
        "unsubscribe from automatically. Look for an unsubscribe link in the body, or stop the sender with "
        "outlook_manage_rule."
    )
    assert len(calls) == 1


def test_unsubscribe_method_mismatches_and_bad_input(monkeypatch):
    _open_gate(monkeypatch)
    _no_one_click(monkeypatch)
    calls = _fake_graph(monkeypatch, lambda call: (True, _message(("List-Unsubscribe", "<https://shop.com/u>"))))
    inv = oo.outlook_unsubscribe.invoke
    assert inv({"email_id": "m1", "method": "mailto"}, config=_CFG) == (
        "[Error]: The List-Unsubscribe header of 'Weekly deals' has no mailto address. Found: https://shop.com/u. "
        'Try method="one_click" or open the link.'
    )
    assert inv({"email_id": "m1", "method": "one_click"}, config=_CFG) == (
        "[Error]: 'Weekly deals' does not support one-click unsubscribe (needs an https List-Unsubscribe URL and a "
        'List-Unsubscribe-Post header). Found: https://shop.com/u. Try method="mailto" or open the link.'
    )
    assert all(c["method"] == "GET" for c in calls)
    assert inv({"email_id": "m1", "method": "fax"}, config=_CFG) == "[Error]: method must be 'auto', 'mailto', or 'one_click'."
    assert inv({"email_id": " "}, config=_CFG) == "[Error]: email_id is required."
    _fake_graph(monkeypatch, _sequence((False, "API Error (404): ErrorItemNotFound: gone")))
    assert inv({"email_id": "m1"}, config=_CFG) == "[Error]: API Error (404): ErrorItemNotFound: gone"


# ---------------------------------------------------------------------------
# Roster, registration, description hygiene
# ---------------------------------------------------------------------------


def test_roster_and_group_registration():
    assert [t.name for t in oo.OUTLOOK_ORGANIZE_TOOLS] == [
        "outlook_list_folders", "outlook_manage_folder", "outlook_list_categories", "outlook_manage_categories",
        "outlook_flag_email", "outlook_list_rules", "outlook_manage_rule", "outlook_get_mailbox_settings",
        "outlook_set_auto_reply", "outlook_focused_overrides", "outlook_unsubscribe",
    ]
    group = get_tool_group("outlook_organize")
    assert group is not None and group.tools == tuple(oo.OUTLOOK_ORGANIZE_TOOLS)
    assert not group.admin_only and not group.developer_only


def test_every_tool_takes_account_id_and_mailbox_and_documents_the_default_mailbox():
    for t in oo.OUTLOOK_ORGANIZE_TOOLS:
        assert {"account_id", "mailbox"} <= set(t.args), t.name
        assert "signed-in account's own mailbox" in t.description, t.name
        assert "permission" in t.description, t.name


def test_no_em_dashes_or_double_hyphens_in_any_string_or_docstring():
    source = pathlib.Path(oo.__file__).read_text(encoding="utf-8")
    offenders = [
        f"line {node.lineno}: {node.value[:60]!r}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and ("—" in node.value or "--" in node.value)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Review fix: forwarding rule actions need an explicit opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["forwardTo", "redirectTo", "forwardAsAttachmentTo"])
def test_create_rule_refuses_forwarding_actions_without_the_opt_in(monkeypatch, field):
    asked = _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"id": "R"})))
    out = oo.outlook_manage_rule.invoke({
        "action": "create", "name": "Leak", "actions_json": '{"%s": ["out@evil.com"], "markAsRead": true}' % field,
    }, config=_CFG)
    assert out == (
        f"[Error]: actions_json uses {field}, which would forward or redirect future mail to another "
        "address automatically. Refused unless allow_forwarding=True is passed, and only after the user "
        "has explicitly asked for that forwarding."
    )
    assert calls == [] and asked == []  # refused before the gate and before any request


def test_update_rule_refuses_forwarding_actions_without_the_opt_in(monkeypatch):
    _open_gate(monkeypatch)
    calls = _fake_graph(monkeypatch, _sequence((True, {"value": [{"id": "R1", "displayName": "Filing"}]})))
    out = oo.outlook_manage_rule.invoke({
        "action": "update", "rule_id": "R1", "actions_json": '{"redirectTo": "out@evil.com"}',
    }, config=_CFG)
    assert out.startswith("[Error]: actions_json uses redirectTo, which would forward or redirect")
    assert calls == []


def test_one_click_redirect_is_reported_as_not_honoured(monkeypatch):
    _open_gate(monkeypatch)
    headers = (("List-Unsubscribe", "<https://shop.com/u?t=1>"), ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"))
    calls = _fake_graph(monkeypatch, lambda call: (True, _message(*headers)))
    for status in (301, 302, 307):
        _record_one_click(monkeypatch, response=SimpleNamespace(status_code=status))
        out = oo.outlook_unsubscribe.invoke({"email_id": "m1", "method": "one_click"}, config=_CFG)
        assert out == (
            "[Error]: One-click unsubscribe for 'Weekly deals' was not sent: the unsubscribe endpoint "
            f"answered HTTP {status} (a redirect, which RFC 8058 forbids for one-click), so the request "
            "was not honoured; open the link in a browser instead."
        )
    _record_one_click(monkeypatch, response=SimpleNamespace(status_code=204))
    assert oo.outlook_unsubscribe.invoke({"email_id": "m1", "method": "one_click"}, config=_CFG).endswith(
        "(POST https://shop.com/u?t=1, HTTP 204). Method: one_click."
    )
    assert all(c["method"] == "GET" for c in calls)  # never a sendMail for one_click


def test_mailto_subject_from_the_header_is_flattened_and_capped(monkeypatch):
    long_subject = "%0Aline%20two%20" + "x" * 300
    address, subject, body = oo._mailto_parts(f"mailto:leave@list.com?subject=stop{long_subject}&body=please")
    assert address == "leave@list.com"
    assert "\n" not in subject and "  " not in subject
    assert subject.startswith("stop line two x") and len(subject) == 120
    assert body == "please"


def test_flag_status_accepts_graphs_notflagged_spelling(monkeypatch):
    _open_gate(monkeypatch)
    batches = _record_batch(monkeypatch)
    out = oo.outlook_flag_email.invoke({"email_id": "m1", "status": "notFlagged"}, config=_CFG)
    assert out == "[Success]: 1 message unflagged."
    assert batches[0]["requests"][0]["body"] == {"flag": {"flagStatus": "notFlagged"}}
