"""Read-side Outlook tools after the organisation pass.

``outlook_list_emails`` filters and paging (B9), ``outlook_get_email`` state
lines, curated headers and batched fetch (B10), ``outlook_get_conversation``
ordering and the "who spoke last" verdict (B11), and ``outlook_sync_changes``
delta mechanics: initial bound, cursor round-trip, page following, and the
new / changed / removed split (B12).
"""

import pytest

from nymeria.tools import outlook_email as oe
from nymeria.tools import outlook_graph as og

_CFG = {"configurable": {"user_id": "u1", "thread_id": "t1"}}


def _fake_graph(monkeypatch, responses):
    calls = []
    seq = iter(responses)

    def _fake(user_id, method, endpoint, account_id=None, json_data=None, params=None, **kw):
        calls.append({
            "method": method, "endpoint": endpoint, "params": dict(params or {}) if params else None,
            "mailbox": kw.get("mailbox"), "headers": kw.get("headers"),
        })
        return next(seq)

    monkeypatch.setattr(og, "graph_request", _fake)
    monkeypatch.setattr(oe, "graph_request", _fake)
    return calls


def _msg(mid, received, sender="a@x.com", name="", **extra):
    m = {"id": mid, "subject": f"S-{mid}", "isRead": True, "receivedDateTime": received,
         "from": {"emailAddress": {"address": sender, "name": name}}}
    m.update(extra)
    return m


# ---------------------------------------------------------------------------
# B9: list filters
# ---------------------------------------------------------------------------


def test_list_combines_every_filter_into_one_clause(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {"value": []})])
    monkeypatch.setattr(oe, "_days_back_cutoff", lambda d: "2026-09-08T00:00:00Z")
    out = oe.outlook_list_emails.invoke({
        "folder": "inbox", "unread_only": True, "sender": "ann@x.com", "days_back": 7,
        "categories": "To Respond, VIP", "flagged": True, "importance": "High",
        "focused": "other", "has_attachments": True,
    }, config=_CFG)
    assert out == "[Info]: No emails found in inbox."
    assert calls[0]["params"]["$filter"] == (
        "receivedDateTime ge 2026-09-08T00:00:00Z and isRead eq false and "
        "from/emailAddress/address eq 'ann@x.com' and "
        "categories/any(c:c eq 'To Respond') and categories/any(c:c eq 'VIP') and "
        "flag/flagStatus eq 'flagged' and importance eq 'high' and "
        "inferenceClassification eq 'other' and hasAttachments eq true"
    )
    assert calls[0]["params"]["$orderby"] == "receivedDateTime desc"
    assert "$skip" not in calls[0]["params"]


def test_list_partial_sender_matches_address_or_name_prefix(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {"value": []})])
    oe.outlook_list_emails.invoke({"sender": "o'acme"}, config=_CFG)
    assert calls[0]["params"]["$filter"] == (
        "(startswith(from/emailAddress/address,'o''acme') or startswith(from/emailAddress/name,'o''acme'))"
    )


def test_list_flagged_false_and_no_filters(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {"value": []}), (True, {"value": []})])
    oe.outlook_list_emails.invoke({"flagged": False}, config=_CFG)
    assert calls[0]["params"]["$filter"] == "flag/flagStatus eq 'notFlagged'"
    oe.outlook_list_emails.invoke({}, config=_CFG)
    assert "$filter" not in calls[1]["params"]


def test_list_validates_importance_and_focused_before_any_request(monkeypatch):
    calls = _fake_graph(monkeypatch, [])
    assert oe.outlook_list_emails.invoke({"importance": "urgent"}, config=_CFG) == (
        "[Error]: importance must be low, normal, or high."
    )
    assert oe.outlook_list_emails.invoke({"focused": "primary"}, config=_CFG) == (
        "[Error]: focused must be focused, other, or any."
    )
    assert calls == []


def test_list_pages_with_skip_and_hints_at_the_next_page(monkeypatch):
    rows = [_msg(f"m{i}", f"2026-09-1{i}T00:00:00Z") for i in range(3)]
    calls = _fake_graph(monkeypatch, [(True, {"value": rows})])
    out = oe.outlook_list_emails.invoke({"limit": 3, "page": 2}, config=_CFG)
    assert calls[0]["params"]["$skip"] == 3 and calls[0]["params"]["$top"] == 3
    assert out.startswith("[Success]: Found 3 email(s) in inbox (page 2):\n")
    assert out.endswith("(More may follow: pass page=3 for the next 3.)")


def test_list_short_page_has_no_more_hint(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": [_msg("m1", "2026-09-10T00:00:00Z")]})])
    out = oe.outlook_list_emails.invoke({"limit": 5}, config=_CFG)
    assert "More may follow" not in out


def test_list_retries_without_server_sort_and_sorts_client_side(monkeypatch):
    rows = [_msg("old", "2026-09-01T00:00:00Z"), _msg("new", "2026-09-10T00:00:00Z")]
    calls = _fake_graph(monkeypatch, [
        (False, "API Error (400): The restriction or sort order is too complex for this operation."),
        (True, {"value": rows}),
    ])
    out = oe.outlook_list_emails.invoke({"flagged": True}, config=_CFG)
    assert "$orderby" in calls[0]["params"] and "$orderby" not in calls[1]["params"]
    assert calls[1]["params"]["$filter"] == "flag/flagStatus eq 'flagged'"
    assert out.index("ID: new") < out.index("ID: old")


def test_list_does_not_retry_an_unfiltered_failure(monkeypatch):
    calls = _fake_graph(monkeypatch, [(False, "API Error (401): nope")])
    assert oe.outlook_list_emails.invoke({}, config=_CFG) == "[Error]: API Error (401): nope"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# B10: get, headers, batch
# ---------------------------------------------------------------------------

_FULL = {
    "id": "M1", "subject": "Renewal", "receivedDateTime": "2026-09-15T01:02:03Z",
    "from": {"emailAddress": {"address": "news@vendor.com", "name": "Vendor"}},
    "replyTo": [{"emailAddress": {"address": "reply@vendor.com"}}],
    "toRecipients": [{"emailAddress": {"address": "me@x.com"}}],
    "body": {"contentType": "text", "content": "Hello"},
    "categories": ["Marketing"], "flag": {"flagStatus": "flagged", "dueDateTime": {"dateTime": "2026-09-20T00:00:00.0000000"}},
    "importance": "high", "inferenceClassification": "other", "conversationId": "CONV1",
    "webLink": "https://outlook.office365.com/x",
    "internetMessageHeaders": [
        {"name": "List-Unsubscribe", "value": "<mailto:u@vendor.com>, <https://vendor.com/u>"},
        {"name": "list-unsubscribe-post", "value": "List-Unsubscribe=One-Click"},
        {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"},
        {"name": "Received", "value": "from somewhere"},
    ],
}


def test_get_renders_state_lines_and_only_curated_headers(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, dict(_FULL))])
    out = oe.outlook_get_email.invoke({"email_id": "M1", "include_headers": True}, config=_CFG)
    assert calls[0]["params"]["$select"].endswith(",internetMessageHeaders")
    assert "**Categories:** Marketing" in out
    assert "**Flag:** flagged (due 2026-09-20)" in out
    assert "**Importance:** high" in out
    assert "**Focused Inbox:** other" in out
    assert "**Reply-To:** reply@vendor.com" in out
    assert "**Thread:** CONV1" in out
    assert "**Link:** https://outlook.office365.com/x" in out
    assert "**ID:** M1" in out
    assert "  List-Unsubscribe: <mailto:u@vendor.com>, <https://vendor.com/u>" in out
    assert "  List-Unsubscribe-Post: List-Unsubscribe=One-Click" in out  # canonical casing restored
    assert "X-MS-Exchange" not in out and "Received:" not in out
    assert out.index("**Headers:**") < out.index("**Body:**")


def test_get_without_headers_neither_selects_nor_renders_them(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {k: v for k, v in _FULL.items() if k != "internetMessageHeaders"})])
    out = oe.outlook_get_email.invoke({"email_id": "M1"}, config=_CFG)
    assert "internetMessageHeaders" not in calls[0]["params"]["$select"]
    assert "**Headers:**" not in out


def test_get_plain_message_adds_no_state_noise(monkeypatch):
    plain = {"id": "M2", "subject": "Hi", "receivedDateTime": "2026-09-15T01:02:03Z",
             "from": {"emailAddress": {"address": "a@x.com"}}, "replyTo": [{"emailAddress": {"address": "a@x.com"}}],
             "toRecipients": [], "body": {"contentType": "text", "content": "x"},
             "importance": "normal", "inferenceClassification": "focused", "flag": {"flagStatus": "notFlagged"}}
    _fake_graph(monkeypatch, [(True, plain)])
    out = oe.outlook_get_email.invoke({"email_id": "M2", "include_headers": True}, config=_CFG)
    for absent in ("**Categories:**", "**Flag:**", "**Importance:**", "**Focused Inbox:**", "**Reply-To:**"):
        assert absent not in out
    assert "**Headers:** none of the tracked headers present" in out


def test_get_batch_uses_one_batch_request_and_reports_per_item(monkeypatch):
    seen = {}

    def _batch(user_id, requests, account_id=None, *, mailbox=None, thread_id=None):
        seen["requests"] = requests
        seen["mailbox"] = mailbox
        return [
            {"id": "0", "status": 200, "ok": True, "body": dict(_FULL), "error": None},
            {"id": "1", "status": 404, "ok": False, "body": None, "error": "API Error (404): ErrorItemNotFound: gone"},
        ]

    monkeypatch.setattr(oe, "graph_batch", _batch)
    out = oe.outlook_get_email.invoke({"email_ids": "M1, M9", "mailbox": "s@x.com"}, config=_CFG)
    assert [r["url"].split("?")[0] for r in seen["requests"]] == ["/me/messages/M1", "/me/messages/M9"]
    assert "%24select=" in seen["requests"][0]["url"] and "%24expand=" in seen["requests"][0]["url"]
    assert seen["mailbox"] == "s@x.com"
    assert out.startswith("=== Email 1/2: Renewal ===\n**From:** Vendor <news@vendor.com>")
    assert "=== Email 2/2: M9 ===\n[Error]: API Error (404): ErrorItemNotFound: gone" in out


# ---------------------------------------------------------------------------
# B11: conversation
# ---------------------------------------------------------------------------


def _owner(monkeypatch, email):
    monkeypatch.setattr(oe, "get_account", lambda user_id, account_id=None, **kw: {"email": email})


def test_conversation_orders_and_marks_who_spoke_last(monkeypatch):
    _owner(monkeypatch, "Me@X.com")
    calls = _fake_graph(monkeypatch, [(True, {"value": [
        _msg("m2", "2026-09-02T00:00:00Z", sender="me@x.com", name="Me"),
        _msg("m1", "2026-09-01T00:00:00Z", sender="them@y.com", name="Them"),
    ]})])
    out = oe.outlook_get_conversation.invoke({"conversation_id": "C'1", "limit": 5}, config=_CFG)
    assert calls[0]["params"]["$filter"] == "conversationId eq 'C''1'"
    assert calls[0]["params"]["$top"] == 5
    assert not calls[0]["params"]["$select"].endswith(",body")
    lines = out.splitlines()
    assert lines[0] == (
        "[Success]: 2 message(s) in conversation (chronological). "
        "Last from Me <me@x.com> on 2026-09-02 00:00: you replied last, awaiting their reply."
    )
    assert out.index("ID: m1") < out.index("ID: m2")
    assert "[read] [2026-09-02 00:00] Me <me@x.com> (you)" in out
    assert "Them <them@y.com> (you)" not in out


def test_conversation_awaiting_your_reply_and_bodies(monkeypatch):
    _owner(monkeypatch, "me@x.com")
    calls = _fake_graph(monkeypatch, [(True, {"value": [
        _msg("m1", "2026-09-01T00:00:00Z", sender="them@y.com", body={"contentType": "html", "content": "<p>Hi<br>there</p>"}),
    ]})])
    out = oe.outlook_get_conversation.invoke({"conversation_id": "C1", "include_bodies": True}, config=_CFG)
    assert calls[0]["params"]["$select"].endswith(",body")
    assert "they spoke last, awaiting your reply" in out
    assert "   Body:\n   Hi\n   there" in out


def test_conversation_uses_the_shared_mailbox_as_owner(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {"value": [_msg("m1", "2026-09-01T00:00:00Z", sender="sales@_prv_a.com.au")]})])
    out = oe.outlook_get_conversation.invoke({"conversation_id": "C1", "mailbox": "Sales@_PRV_A.com.au"}, config=_CFG)
    assert calls[0]["mailbox"] == "Sales@_PRV_A.com.au"
    assert "you replied last" in out


def test_conversation_empty_and_missing_id(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": []})])
    assert oe.outlook_get_conversation.invoke({"conversation_id": "abcdefghijklmnopqrstuvwxyz"}, config=_CFG) == (
        "[Info]: No messages found for conversation 'abcdefghijklmnopqrst...'."
    )
    assert oe.outlook_get_conversation.invoke({"conversation_id": "  "}, config=_CFG) == "[Error]: conversation_id is required."


# ---------------------------------------------------------------------------
# B12: delta sync
# ---------------------------------------------------------------------------

_DELTA = f"{og.GRAPH_BASE}/me/mailFolders/INBOXID/messages/delta?$deltatoken=TOK1"
_NEXT = f"{og.GRAPH_BASE}/me/mailFolders/INBOXID/messages/delta?$skiptoken=SKIP1"


def test_sync_initial_call_bounds_by_since_days_and_returns_a_cursor(monkeypatch):
    monkeypatch.setattr(oe, "_days_back_cutoff", lambda d: "2026-09-08T00:00:00Z")
    calls = _fake_graph(monkeypatch, [(True, {
        "value": [_msg("m1", "2026-09-10T00:00:00Z"), {"id": "gone", "@removed": {"reason": "deleted"}}],
        "@odata.deltaLink": _DELTA,
    })])
    out = oe.outlook_sync_changes.invoke({"folder": "inbox", "since_days": 7}, config=_CFG)
    p = calls[0]["params"]
    assert calls[0]["endpoint"] == "/me/mailFolders/inbox/messages/delta"
    assert p["$filter"] == "receivedDateTime ge 2026-09-08T00:00:00Z"
    assert "lastModifiedDateTime" in p["$select"] and "categories" in p["$select"]
    assert calls[0]["headers"] == {"Prefer": "odata.maxpagesize=50"}
    assert out.startswith("[Success]: inbox: 1 new, 0 changed, 1 removed (initial sync, last 7 day(s)).")
    assert "Removed from this folder (deleted or moved elsewhere):\n  - gone" in out
    cursor_line = out.splitlines()[-1]
    assert cursor_line.startswith("Cursor (pass as cursor= next time): ")
    stamp, sep, link = cursor_line.split(": ", 1)[1].partition("|")
    assert sep == "|" and link == _DELTA and stamp.endswith("Z") and len(stamp) == 20


def test_sync_since_days_zero_has_no_filter(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {"value": [], "@odata.deltaLink": _DELTA})])
    out = oe.outlook_sync_changes.invoke({"since_days": 0}, config=_CFG)
    assert "$filter" not in calls[0]["params"]
    assert "(initial sync, whole folder)" in out


def test_sync_with_cursor_gets_the_link_verbatim_and_splits_new_from_changed(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {
        "value": [
            _msg("newer", "2026-09-15T12:00:00Z"),
            _msg("older", "2026-09-01T00:00:00Z", isRead=True, categories=["FYI"]),
            {"id": "moved", "@removed": {"reason": "deleted"}},
        ],
        "@odata.deltaLink": _DELTA.replace("TOK1", "TOK2"),
    })])
    out = oe.outlook_sync_changes.invoke({"cursor": f"2026-09-15T10:00:00Z|{_DELTA}", "folder": "ignored"}, config=_CFG)
    assert calls[0]["endpoint"] == "/me/mailFolders/INBOXID/messages/delta?$deltatoken=TOK1"
    assert calls[0]["params"] is None
    assert out.startswith("[Success]: ignored: 1 new, 1 changed, 1 removed (since 2026-09-15 10:00:00Z).")
    new_block = out.split("New:")[1].split("Changed")[0]
    assert "ID: newer" in new_block and "ID: older" not in new_block
    assert "Categories: FYI" in out.split("Changed")[1]
    assert out.splitlines()[-1].endswith(f"|{_DELTA.replace('TOK1', 'TOK2')}")


def test_sync_follows_next_links_then_stops_at_the_cap_with_a_continuation(monkeypatch):
    calls = _fake_graph(monkeypatch, [
        (True, {"value": [_msg("a", "2026-09-10T00:00:00Z")], "@odata.nextLink": _NEXT}),
        (True, {"value": [_msg("b", "2026-09-10T00:00:00Z")], "@odata.nextLink": _NEXT.replace("SKIP1", "SKIP2")}),
    ])
    out = oe.outlook_sync_changes.invoke({"max_changes": 2, "since_days": 0}, config=_CFG)
    assert calls[1]["endpoint"] == "/me/mailFolders/INBOXID/messages/delta?$skiptoken=SKIP1"
    assert calls[1]["params"] is None
    assert calls[0]["headers"] == {"Prefer": "odata.maxpagesize=2"}
    assert "[Note]: stopped at 2 items; more changes are pending." in out
    assert out.splitlines()[-1].endswith(f"|{_NEXT.replace('SKIP1', 'SKIP2')}")


def test_sync_keeps_following_when_under_the_cap(monkeypatch):
    calls = _fake_graph(monkeypatch, [
        (True, {"value": [_msg("a", "2026-09-10T00:00:00Z")], "@odata.nextLink": _NEXT}),
        (True, {"value": [], "@odata.deltaLink": _DELTA}),
    ])
    out = oe.outlook_sync_changes.invoke({"since_days": 0}, config=_CFG)
    assert len(calls) == 2
    assert "1 new, 0 changed, 0 removed" in out and "[Note]" not in out


def test_sync_rejects_a_foreign_cursor_and_surfaces_graph_errors(monkeypatch):
    calls = _fake_graph(monkeypatch, [(False, "API Error (410): SyncStateNotFound")])
    out = oe.outlook_sync_changes.invoke({"cursor": "https://evil.example/delta?x=1"}, config=_CFG)
    assert out.startswith("[Error]: cursor not recognised")
    assert calls == []
    out = oe.outlook_sync_changes.invoke({"cursor": f"2026-09-15T10:00:00Z|{_DELTA}"}, config=_CFG)
    assert out == "[Error]: API Error (410): SyncStateNotFound"


def test_sync_shared_mailbox_and_custom_folder(monkeypatch):
    calls = _fake_graph(monkeypatch, [
        (True, {"value": [{"id": "CLI", "displayName": "Clients"}]}),
        (True, {"value": [], "@odata.deltaLink": _DELTA}),
    ])
    oe.outlook_sync_changes.invoke({"folder": "Clients", "mailbox": "s@x.com"}, config=_CFG)
    assert calls[1]["endpoint"] == "/me/mailFolders/CLI/messages/delta"
    assert calls[1]["mailbox"] == "s@x.com"


# ---------------------------------------------------------------------------
# Review fix: a cursor may only replay a folder delta endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("link", [
    f"{og.GRAPH_BASE}/me/calendarView/delta?$deltatoken=T",
    f"{og.GRAPH_BASE}/me/contacts/delta?$deltatoken=T",
    f"{og.GRAPH_BASE}/me/messages?$top=1",
    f"{og.GRAPH_BASE}/me/mailFolders/inbox/messages?$search=%22secret%22",
    f"{og.GRAPH_BASE}/me/mailFolders/inbox/messages/delta?$deltatoken=T&$select=body",
    f"{og.GRAPH_BASE}/me/mailFolders/inbox/messages/delta",
    f"{og.GRAPH_BASE}/me/mailFolders/../users/other@x.com/mailFolders/inbox/messages/delta?$deltatoken=T",
])
def test_sync_refuses_a_cursor_that_is_not_a_folder_delta_link(monkeypatch, link):
    calls = _fake_graph(monkeypatch, [])
    out = oe.outlook_sync_changes.invoke({"cursor": f"2026-09-15T10:00:00Z|{link}"}, config=_CFG)
    assert out.startswith("[Error]: cursor not recognised")
    assert calls == []


@pytest.mark.parametrize("link", [
    f"{og.GRAPH_BASE}/me/mailFolders('inbox')/messages/delta?$deltatoken=3DE7yRCv.syQd",
    f"{og.GRAPH_BASE}/me/mailFolders/AQMkAD-_x==/messages/delta?%24skiptoken=abc",
    f"{og.GRAPH_BASE}/users/sales%40x.com/mailFolders('AQMk')/messages/delta?$deltatoken=T",
])
def test_sync_accepts_graphs_own_delta_link_spellings(monkeypatch, link):
    calls = _fake_graph(monkeypatch, [(True, {"value": [], "@odata.deltaLink": link})])
    out = oe.outlook_sync_changes.invoke({"cursor": f"2026-09-15T10:00:00Z|{link}"}, config=_CFG)
    assert out.startswith("[Success]: inbox: 0 new, 0 changed, 0 removed")
    assert calls[0]["endpoint"] == link[len(og.GRAPH_BASE):]
