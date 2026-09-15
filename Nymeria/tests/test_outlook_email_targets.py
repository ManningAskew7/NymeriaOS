"""Multi-target and mailbox behaviour of the ``outlook_*`` mutation tools.

``outlook_move_email``, ``outlook_set_category``, ``outlook_mark_email`` and
``outlook_delete_email`` share one contract (``email_id | email_ids |
conversation_id``, ``$batch`` execution, per-message outcomes). These lock the
Graph payloads each tool emits, the legacy single-id shapes (B28), the
category merge semantics, and the master-list note.
"""

from nymeria.tools import outlook_email as oe
from nymeria.tools import outlook_graph as og

_CFG = {"configurable": {"user_id": "u1", "thread_id": "t1"}}


def _record_batch(monkeypatch, outcomes=None):
    """Capture every ``graph_batch`` call (both module namespaces) and answer ok."""
    batches = []

    def _fake(user_id, requests, account_id=None, *, mailbox=None, thread_id=None):
        batches.append({"requests": requests, "mailbox": mailbox, "account_id": account_id})
        out = []
        for r in requests:
            rid = str(r["id"])
            body = (outcomes or {}).get(rid)
            if body is None:
                out.append({"id": rid, "status": 200, "ok": True, "body": {}, "error": None})
            elif body == "FAIL":
                out.append({"id": rid, "status": 404, "ok": False, "body": None, "error": "API Error (404): gone"})
            else:
                out.append({"id": rid, "status": 200, "ok": True, "body": body, "error": None})
        return out

    monkeypatch.setattr(og, "graph_batch", _fake)
    monkeypatch.setattr(oe, "graph_batch", _fake)
    return batches


def _fake_graph(monkeypatch, responses):
    calls = []
    seq = iter(responses)

    def _fake(user_id, method, endpoint, account_id=None, json_data=None, params=None, **kw):
        calls.append({"method": method, "endpoint": endpoint, "params": dict(params or {}), "mailbox": kw.get("mailbox")})
        return next(seq)

    monkeypatch.setattr(og, "graph_request", _fake)
    monkeypatch.setattr(oe, "graph_request", _fake)
    return calls


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


def test_move_single_alias_emits_the_legacy_move_payload(monkeypatch):
    batches = _record_batch(monkeypatch)
    out = oe.outlook_move_email.invoke({"email_id": "m1", "folder": "archive"}, config=_CFG)
    assert out == "[Success]: 1 message moved to archive."
    assert batches[0]["requests"] == [
        {"id": "0", "method": "POST", "url": "/me/messages/m1/move", "body": {"destinationId": "archive"}},
    ]


def test_move_trash_and_spam_spellings_resolve(monkeypatch):
    batches = _record_batch(monkeypatch)
    oe.outlook_move_email.invoke({"email_id": "m1", "folder": "trash"}, config=_CFG)
    oe.outlook_move_email.invoke({"email_id": "m1", "folder": "spam"}, config=_CFG)
    assert batches[0]["requests"][0]["body"] == {"destinationId": "deleteditems"}
    assert batches[1]["requests"][0]["body"] == {"destinationId": "junkemail"}


def test_move_many_to_a_named_folder_with_copy_and_mailbox(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": [{"id": "CLI", "displayName": "Clients"}]})])
    batches = _record_batch(monkeypatch, {"1": "FAIL"})
    out = oe.outlook_move_email.invoke(
        {"email_ids": "a,b,c", "folder": "Clients", "as_copy": True, "mailbox": "s@x.com"}, config=_CFG,
    )
    reqs = batches[0]["requests"]
    assert [r["url"] for r in reqs] == ["/me/messages/a/copy", "/me/messages/b/copy", "/me/messages/c/copy"]
    assert all(r["body"] == {"destinationId": "CLI"} for r in reqs)
    assert batches[0]["mailbox"] == "s@x.com"
    assert out.startswith("[Partial]: 2/3 messages copied to Clients; 1 failed:")
    assert "b...: API Error (404): gone" in out


def test_move_requires_a_folder_and_refuses_unknown_names(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": []})])
    batches = _record_batch(monkeypatch)
    assert oe.outlook_move_email.invoke({"email_id": "m1"}, config=_CFG) == "[Error]: folder is required."
    out = oe.outlook_move_email.invoke({"email_id": "m1", "folder": "Nope"}, config=_CFG)
    assert out.startswith("[Error]: No folder named 'Nope'")
    assert batches == []


# ---------------------------------------------------------------------------
# mark / delete
# ---------------------------------------------------------------------------


def test_mark_conversation_patches_every_message(monkeypatch):
    _fake_graph(monkeypatch, [
        (True, {"value": [{"id": "m1", "parentFolderId": "IN"}, {"id": "m2", "parentFolderId": "IN"}]}),
        (True, {"id": "DEL"}),
    ])
    batches = _record_batch(monkeypatch)
    out = oe.outlook_mark_email.invoke({"conversation_id": "conv1", "is_read": False}, config=_CFG)
    assert out == "[Success]: 2 messages marked as unread."
    assert [r["url"] for r in batches[0]["requests"]] == ["/me/messages/m1", "/me/messages/m2"]
    assert all(r["method"] == "PATCH" and r["body"] == {"isRead": False} for r in batches[0]["requests"])


def test_mark_single_keeps_the_legacy_shape(monkeypatch):
    batches = _record_batch(monkeypatch)
    out = oe.outlook_mark_email.invoke({"email_id": "m1", "is_read": True}, config=_CFG)
    assert out == "[Success]: 1 message marked as read."
    assert batches[0]["requests"] == [{"id": "0", "method": "PATCH", "url": "/me/messages/m1", "body": {"isRead": True}}]


def test_delete_soft_moves_and_permanent_deletes(monkeypatch):
    batches = _record_batch(monkeypatch)
    out = oe.outlook_delete_email.invoke({"email_id": "m1"}, config=_CFG)
    assert out == "[Success]: 1 message moved to Deleted Items."
    assert batches[0]["requests"][0] == {
        "id": "0", "method": "POST", "url": "/me/messages/m1/move", "body": {"destinationId": "deleteditems"},
    }
    out = oe.outlook_delete_email.invoke({"email_ids": "a,b", "permanent": True}, config=_CFG)
    assert out == "[Success]: 2 messages permanently deleted."
    assert [r["method"] for r in batches[1]["requests"]] == ["DELETE", "DELETE"]
    assert "body" not in batches[1]["requests"][0]


def test_targets_over_the_cap_are_refused_before_any_batch(monkeypatch):
    batches = _record_batch(monkeypatch)
    ids = ",".join(f"m{i}" for i in range(51))
    out = oe.outlook_mark_email.invoke({"email_ids": ids, "is_read": True}, config=_CFG)
    assert out.startswith("[Error]: 51 messages selected")
    assert batches == []


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------


def _category_reads(monkeypatch, current_by_id, master=None):
    """GET batch answers with current categories; master-list read via graph_request."""
    def _batch(user_id, requests, account_id=None, *, mailbox=None, thread_id=None):
        out = []
        for r in requests:
            rid = str(r["id"])
            if r["method"] == "GET":
                mid = r["url"].split("/me/messages/")[1].split("?")[0]
                out.append({"id": rid, "status": 200, "ok": True, "body": {"categories": current_by_id.get(mid, [])}, "error": None})
            else:
                out.append({"id": rid, "status": 200, "ok": True, "body": {}, "error": None})
        recorded.append(requests)
        return out

    recorded = []
    monkeypatch.setattr(oe, "graph_batch", _batch)
    monkeypatch.setattr(og, "graph_batch", _batch)
    master_calls = []

    def _graph(user_id, method, endpoint, account_id=None, json_data=None, params=None, **kw):
        master_calls.append(endpoint)
        if master is None:
            return False, "API Error (403): ErrorAccessDenied"
        return True, {"value": [{"displayName": m} for m in master]}

    monkeypatch.setattr(oe, "graph_request", _graph)
    return recorded, master_calls


def test_set_category_add_merges_with_existing_labels(monkeypatch):
    recorded, _ = _category_reads(monkeypatch, {"m1": ["Keep"]}, master=["Keep", "Nymeria"])
    out = oe.outlook_set_category.invoke({"email_id": "m1", "category": "Nymeria"}, config=_CFG)
    assert out == "[Success]: 1 message tagged with 'Nymeria'."
    patches = recorded[1]
    assert patches == [{"id": "0", "method": "PATCH", "url": "/me/messages/m1", "body": {"categories": ["Keep", "Nymeria"]}}]


def test_set_category_add_existing_is_reported_as_no_change(monkeypatch):
    recorded, _ = _category_reads(monkeypatch, {"m1": ["nymeria"]}, master=["Nymeria"])
    out = oe.outlook_set_category.invoke({"email_id": "m1", "category": "Nymeria"}, config=_CFG)
    assert out == "[Info]: Email already has category 'Nymeria'."
    assert len(recorded) == 1  # the read only, no PATCH


def test_set_category_remove_and_replace(monkeypatch):
    recorded, _ = _category_reads(monkeypatch, {"m1": ["A", "B"], "m2": ["B"]}, master=["A", "B", "FYI"])
    out = oe.outlook_set_category.invoke({"email_ids": "m1,m2", "category": "B", "action": "remove"}, config=_CFG)
    assert out == "[Success]: 2 messages cleared of 'B'."
    assert [r["body"] for r in recorded[1]] == [{"categories": ["A"]}, {"categories": []}]

    out = oe.outlook_set_category.invoke({"email_ids": "m1,m2", "categories": "FYI", "action": "replace"}, config=_CFG)
    assert out == "[Success]: 2 messages set to 'FYI'."
    assert len(recorded) == 3  # replace skips the read batch
    assert all(r["body"] == {"categories": ["FYI"]} for r in recorded[2])


def test_set_category_replace_with_nothing_clears_all(monkeypatch):
    recorded, _ = _category_reads(monkeypatch, {}, master=[])
    out = oe.outlook_set_category.invoke({"email_id": "m1", "action": "replace"}, config=_CFG)
    assert out == "[Success]: 1 message set to '(none)'."
    assert recorded[0][0]["body"] == {"categories": []}


def test_set_category_warns_when_name_is_not_in_the_master_list(monkeypatch):
    _category_reads(monkeypatch, {"m1": []}, master=["Other"])
    out = oe.outlook_set_category.invoke({"email_id": "m1", "categories": "To Respond, Other"}, config=_CFG)
    assert out.startswith("[Success]: 1 message tagged with 'To Respond, Other'.")
    assert "[Note]: To Respond not in the mailbox's master category list" in out
    assert "outlook_manage_categories" in out


def test_set_category_master_list_read_failure_stays_silent(monkeypatch):
    _category_reads(monkeypatch, {"m1": []}, master=None)  # 403: older token without the scope
    out = oe.outlook_set_category.invoke({"email_id": "m1", "category": "X"}, config=_CFG)
    assert out == "[Success]: 1 message tagged with 'X'."


def test_set_category_validates_action_and_name(monkeypatch):
    recorded, _ = _category_reads(monkeypatch, {}, master=[])
    assert oe.outlook_set_category.invoke({"email_id": "m1", "category": "X", "action": "toggle"}, config=_CFG) == (
        "[Error]: action must be 'add', 'remove', or 'replace'."
    )
    assert oe.outlook_set_category.invoke({"email_id": "m1"}, config=_CFG) == "[Error]: category name is required."
    assert recorded == []


def test_set_category_partial_change_notes_the_untouched(monkeypatch):
    recorded, _ = _category_reads(monkeypatch, {"m1": ["X"], "m2": []}, master=["X"])
    out = oe.outlook_set_category.invoke({"email_ids": "m1,m2", "category": "X"}, config=_CFG)
    assert out.startswith("[Success]: 1 message tagged with 'X'.")
    assert "(1 already in the requested state, left as-is)" in out
    assert [r["url"] for r in recorded[1]] == ["/me/messages/m2"]


# ---------------------------------------------------------------------------
# list enrichment
# ---------------------------------------------------------------------------


def test_summary_row_carries_state_tags_and_categories():
    msg = {
        "id": "m1", "subject": "Quote", "isRead": False, "receivedDateTime": "2026-09-15T01:02:03Z",
        "from": {"emailAddress": {"name": "Ann", "address": "ann@x.com"}},
        "flag": {"flagStatus": "flagged", "dueDateTime": {"dateTime": "2026-09-20T00:00:00.0000000", "timeZone": "UTC"}},
        "importance": "high", "inferenceClassification": "other", "categories": ["To Respond", "VIP"],
    }
    out = oe.format_email_summary(msg)
    assert out.splitlines()[0] == "[unread] [2026-09-15 01:02] Ann <ann@x.com> [flagged due 2026-09-20] [high] [other]"
    assert "   Categories: To Respond, VIP" in out


def test_summary_row_of_a_plain_message_adds_no_tags():
    msg = {"id": "m1", "subject": "Hi", "isRead": True, "receivedDateTime": "2026-09-15T01:02:03Z",
           "from": {"emailAddress": {"address": "a@x.com"}}, "importance": "normal",
           "inferenceClassification": "focused", "flag": {"flagStatus": "notFlagged"}}
    out = oe.format_email_summary(msg)
    assert out.splitlines()[0] == "[read] [2026-09-15 01:02] a@x.com"
    assert "Categories" not in out


def test_list_emails_resolves_custom_folders(monkeypatch):
    calls = _fake_graph(monkeypatch, [
        (True, {"value": [{"id": "CLI", "displayName": "Clients"}]}),
        (True, {"value": []}),
    ])
    out = oe.outlook_list_emails.invoke({"folder": "Clients", "mailbox": "s@x.com"}, config=_CFG)
    assert out == "[Info]: No emails found in Clients."
    assert calls[1]["endpoint"] == "/me/mailFolders/CLI/messages"
    assert calls[1]["mailbox"] == "s@x.com"
    assert "categories" in calls[1]["params"]["$select"]


# ---------------------------------------------------------------------------
# Review fix: a skipped (unchanged) target must not shift the failure report
# ---------------------------------------------------------------------------


def test_set_category_reports_the_right_failure_after_a_skipped_target(monkeypatch):
    current = {"m1": ["X"], "m2": [], "m3": []}
    recorded = []

    def _batch(user_id, requests, account_id=None, *, mailbox=None, thread_id=None):
        recorded.append(requests)
        out = []
        for r in requests:
            if r["method"] == "GET":
                mid = r["url"].split("/me/messages/")[1].split("?")[0]
                out.append({"id": r["id"], "status": 200, "ok": True, "body": {"categories": current[mid]}, "error": None})
            elif r["url"].endswith("/m3"):
                out.append({"id": r["id"], "status": 404, "ok": False, "body": None, "error": "API Error (404): ErrorItemNotFound"})
            else:
                out.append({"id": r["id"], "status": 200, "ok": True, "body": {}, "error": None})
        return out

    monkeypatch.setattr(oe, "graph_batch", _batch)
    monkeypatch.setattr(og, "graph_batch", _batch)
    monkeypatch.setattr(oe, "graph_request", lambda *a, **k: (True, {"value": [{"displayName": "X"}]}))

    out = oe.outlook_set_category.invoke({"email_ids": "m1,m2,m3", "category": "X"}, config=_CFG)
    assert out.splitlines()[0] == "[Partial]: 1/2 messages tagged with 'X'; 1 failed:"
    assert "  - m3...: API Error (404): ErrorItemNotFound" in out
    assert "  - m2" not in out
    assert "(1 already in the requested state, left as-is)" in out
    patched = recorded[1]
    assert [r["url"] for r in patched] == ["/me/messages/m2", "/me/messages/m3"]
    assert [r["id"] for r in patched] == ["0", "1"]  # batch ids index the patched list
