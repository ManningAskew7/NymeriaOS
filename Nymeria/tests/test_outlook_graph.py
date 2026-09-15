"""Tests for the shared Outlook Graph layer (``nymeria/tools/outlook_graph.py``).

Covers the contracts the whole ``outlook_*`` family leans on: refresh requests
the credential's stored scopes (B1), the ``mailbox`` rewrite onto
``/users/{upn}`` (B2), the scope gate's reconnect message (B3), ``$batch``
chunking and per-item outcomes (B4), the multi-target cap and conversation
expansion (B5, B6), folder resolution by alias, path, and id (B7), and the
expired-token error naming the account (B8). Compatibility re-exports (B27).
"""

import time
from types import SimpleNamespace

import pytest

from nymeria.config.oauth_providers import OUTLOOK_SCOPES
from nymeria.tools import outlook_graph as og
from nymeria.tools.auth_cache_utils import OAuthAccountSelectionError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stub_policy_client(monkeypatch, handler):
    """Route every policy-client call through ``handler(method, url, **kw)``.

    The seam is the FACTORY (``og._http_client``), which is how the tools build
    their client; patching httpx directly would silently stop meaning anything.
    """
    calls = []

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kw):
            calls.append((method, url, kw))
            return handler(method, url, **kw)

        def post(self, url, **kw):
            calls.append(("POST", url, kw))
            return handler("POST", url, **kw)

        def get(self, url, **kw):
            calls.append(("GET", url, kw))
            return handler("GET", url, **kw)

    monkeypatch.setattr(og, "_http_client", lambda **kwargs: _Client())
    return calls


def _resp(status=200, payload=None, text=None):
    body = payload if payload is not None else {}
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        text=text if text is not None else ("x" if body else ""),
        headers={},
    )


def _bind_cache(monkeypatch, accounts, thread_bound=False):
    cache = {"accounts": dict(accounts)}
    persisted = []
    source = SimpleNamespace(
        cache=cache,
        accounts=cache["accounts"],
        persist=lambda c: persisted.append(c),
        thread_bound=thread_bound,
    )
    monkeypatch.setattr(og.auth_utils, "resolve_oauth_cache", lambda *a, **k: source)
    # The legacy device-code completion path reads settings.data_dir; it is not
    # under test here and the stubbed settings do not carry it.
    monkeypatch.setattr(og, "try_complete_pending_auth", lambda user_id: False)
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(outlook_default_account_id=None),
    )
    return persisted


def _live_account(**extra):
    acct = {"email": "me@x.com", "access_token": "tok", "expires_at": time.time() + 3600}
    acct.update(extra)
    return acct


def _fake_graph(monkeypatch, responses, calls=None):
    """Replace ``og.graph_request`` with a recorder yielding ``responses`` in order."""
    calls = calls if calls is not None else []
    seq = iter(responses)

    def _fake(user_id, method, endpoint, account_id=None, json_data=None, params=None, **kw):
        calls.append({"method": method, "endpoint": endpoint, "params": dict(params or {}), "mailbox": kw.get("mailbox")})
        return next(seq)

    monkeypatch.setattr(og, "graph_request", _fake)
    return calls


# ---------------------------------------------------------------------------
# B1: refresh requests the stored scopes
# ---------------------------------------------------------------------------


def test_refresh_scope_string_uses_the_credentials_stored_scopes():
    acct = {"scopes": ["User.Read", "Mail.ReadWrite", "Calendars.ReadWrite", "ChannelMessage.Send"]}
    assert og._refresh_scope_string(acct) == (
        "offline_access User.Read Mail.ReadWrite Calendars.ReadWrite ChannelMessage.Send"
    )


def test_refresh_scope_string_keeps_offline_access_when_already_present():
    acct = {"scopes": ["offline_access", "Mail.ReadWrite"]}
    assert og._refresh_scope_string(acct) == "offline_access Mail.ReadWrite"


def test_refresh_scope_string_falls_back_to_registry_for_legacy_accounts():
    assert og._refresh_scope_string({}) == " ".join(OUTLOOK_SCOPES)
    assert og._refresh_scope_string({"scopes": []}) == " ".join(OUTLOOK_SCOPES)


def test_expired_token_refresh_posts_stored_scopes_and_persists(monkeypatch):
    persisted = _bind_cache(monkeypatch, {
        "A": {
            "email": "a@x.com", "access_token": "old", "refresh_token": "r1",
            "expires_at": time.time() - 5,
            "scopes": ["Mail.ReadWrite", "Calendars.ReadWrite"],
        },
    })
    posted = {}

    def handler(method, url, **kw):
        posted.update(kw.get("data") or {})
        return _resp(200, {"access_token": "new", "refresh_token": "r2", "expires_in": 3600,
                           "scope": "Mail.ReadWrite Calendars.ReadWrite"})

    _stub_policy_client(monkeypatch, handler)

    token, account = og.acquire_access_token("u1")

    assert token == "new"
    assert posted["scope"] == "offline_access Mail.ReadWrite Calendars.ReadWrite"
    assert posted["grant_type"] == "refresh_token"
    assert persisted and persisted[-1]["accounts"]["A"]["refresh_token"] == "r2"
    # The granted list from the response is written back so the next refresh
    # asks for exactly what Entra confirmed.
    assert persisted[-1]["accounts"]["A"]["scopes"] == ["Mail.ReadWrite", "Calendars.ReadWrite"]


# ---------------------------------------------------------------------------
# B2: mailbox rewrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint,mailbox,expected", [
    ("/me/messages/x", "sales@x.com", "/users/sales@x.com/messages/x"),
    ("/me", "sales@x.com", "/users/sales@x.com"),
    ("/me/messages/x", None, "/me/messages/x"),
    ("/me/messages/x", "  ", "/me/messages/x"),
    ("/teams/t/channels", "sales@x.com", "/teams/t/channels"),
])
def test_apply_mailbox_rewrites_only_me_rooted_paths(endpoint, mailbox, expected):
    assert og._apply_mailbox(endpoint, mailbox) == expected


def test_graph_request_addresses_the_shared_mailbox(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    calls = _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(200, {"value": []}))

    ok, _ = og.graph_request("u1", "GET", "/me/mailFolders/inbox/messages", mailbox="sales@_prv_a.com.au")

    assert ok
    assert calls[0][1] == f"{og.GRAPH_BASE}/users/sales@_prv_a.com.au/mailFolders/inbox/messages"


def test_graph_request_defaults_to_the_signed_in_mailbox(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    calls = _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(200, {"value": []}))
    og.graph_request("u1", "GET", "/me/messages")
    assert calls[0][1] == f"{og.GRAPH_BASE}/me/messages"


# ---------------------------------------------------------------------------
# B3: scope gate
# ---------------------------------------------------------------------------


def test_require_scopes_names_missing_scope_and_the_reconnect(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account(scopes=["Mail.ReadWrite", "Mail.Send"])})
    msg = og.require_scopes("u1", [og.SCOPE_MAILBOX_SETTINGS], purpose="category management")
    assert msg is not None
    assert "MailboxSettings.ReadWrite" in msg
    assert 'request_credential(provider="outlook", kind="oauth")' in msg
    assert "me@x.com" in msg
    assert "category management" in msg


def test_require_scopes_passes_when_granted_including_url_form(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account(
        scopes=["https://graph.microsoft.com/MailboxSettings.ReadWrite", "Mail.ReadWrite"],
    )})
    assert og.require_scopes("u1", [og.SCOPE_MAILBOX_SETTINGS]) is None


def test_require_scopes_lets_legacy_accounts_through(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})  # no scopes list at all
    assert og.require_scopes("u1", [og.SCOPE_MAILBOX_SETTINGS]) is None


def test_require_scopes_adds_the_shared_scope_for_a_mailbox(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account(scopes=["Mail.ReadWrite"])})
    msg = og.require_scopes("u1", [], mailbox="sales@x.com")
    assert msg is not None and "Mail.ReadWrite.Shared" in msg
    assert og.require_scopes("u1", [], mailbox=None) is None


def test_require_scopes_reports_selection_and_missing_account(monkeypatch):
    _bind_cache(monkeypatch, {})
    assert "No authenticated Outlook account" in (og.require_scopes("u1", ["x"]) or "")
    _bind_cache(monkeypatch, {"A": _live_account(), "B": _live_account()})
    msg = og.require_scopes("u1", ["x"])
    assert msg is not None and "Several Outlook accounts" in msg


# ---------------------------------------------------------------------------
# B4: $batch chunking and per-item results
# ---------------------------------------------------------------------------


def test_graph_batch_chunks_by_twenty_and_keeps_input_order(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    sizes = []

    def handler(method, url, **kw):
        reqs = kw["json"]["requests"]
        sizes.append(len(reqs))
        responses = []
        for r in reqs:
            # Fail every id divisible by 7 to prove per-item outcomes survive.
            if int(r["id"]) % 7 == 0 and r["id"] != "0":
                responses.append({"id": r["id"], "status": 404,
                                  "body": {"error": {"code": "ErrorItemNotFound", "message": "gone"}}})
            else:
                responses.append({"id": r["id"], "status": 200, "body": {"ok": r["id"]}})
        return _resp(200, {"responses": list(reversed(responses))})

    calls = _stub_policy_client(monkeypatch, handler)
    requests = [{"id": str(i), "method": "PATCH", "url": f"/me/messages/m{i}", "body": {"isRead": True}} for i in range(45)]

    results = og.graph_batch("u1", requests)

    assert sizes == [20, 20, 5]
    assert all(c[1] == f"{og.GRAPH_BASE}/$batch" for c in calls)
    assert [r["id"] for r in results] == [str(i) for i in range(45)]
    assert results[3]["ok"] and results[3]["body"] == {"ok": "3"}
    assert not results[7]["ok"] and results[7]["status"] == 404
    assert "ErrorItemNotFound: gone" in results[7]["error"]
    # Bodies ride with a JSON content-type header and the mailbox rewrite applies per item.
    sent = calls[0][2]["json"]["requests"][0]
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["url"] == "/me/messages/m0"


def test_graph_batch_rewrites_item_urls_for_a_shared_mailbox(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    calls = _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(200, {"responses": [{"id": "0", "status": 204}]}))
    og.graph_batch("u1", [{"id": "0", "method": "DELETE", "url": "/me/messages/m0"}], mailbox="s@x.com")
    assert calls[0][2]["json"]["requests"][0]["url"] == "/users/s@x.com/messages/m0"


def test_graph_batch_whole_chunk_failure_yields_one_failure_per_request(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(429, {"error": {"code": "TooManyRequests", "message": "slow down"}}, text="x"))
    results = og.graph_batch("u1", [{"id": "0", "method": "GET", "url": "/me/messages/a"},
                                    {"id": "1", "method": "GET", "url": "/me/messages/b"}])
    assert len(results) == 2
    assert all(not r["ok"] and "TooManyRequests" in r["error"] for r in results)


def test_graph_batch_missing_response_is_reported_not_dropped(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(200, {"responses": [{"id": "0", "status": 200, "body": {}}]}))
    results = og.graph_batch("u1", [{"id": "0", "method": "GET", "url": "/me/messages/a"},
                                    {"id": "1", "method": "GET", "url": "/me/messages/b"}])
    assert results[0]["ok"]
    assert not results[1]["ok"] and "No response" in results[1]["error"]


def test_graph_batch_without_any_account_fails_every_item_without_a_request(monkeypatch):
    _bind_cache(monkeypatch, {})
    calls = _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(200, {}))
    results = og.graph_batch("u1", [{"id": "0", "method": "GET", "url": "/me/messages/a"}])
    assert calls == []
    assert not results[0]["ok"] and "No authenticated Outlook account" in results[0]["error"]


# ---------------------------------------------------------------------------
# B5 / B6: multi-target contract
# ---------------------------------------------------------------------------


def test_resolve_targets_refuses_above_the_cap_before_any_request(monkeypatch):
    calls = _fake_graph(monkeypatch, [])
    ids = ",".join(f"m{i}" for i in range(51))
    ok, err = og.resolve_targets("u1", email_ids=ids)
    assert not ok
    assert "51 messages" in err and "50" in err
    assert calls == []


def test_resolve_targets_precedence_and_dedup():
    ok, ids = og.resolve_targets("u1", email_id="single", email_ids="a, b,\nb, c")
    assert ok and ids == ["a", "b", "c"]
    ok, ids = og.resolve_targets("u1", email_id="  single ")
    assert ok and ids == ["single"]
    ok, err = og.resolve_targets("u1")
    assert not ok and "email_id" in err and "conversation_id" in err


def test_resolve_targets_conversation_skips_deleted_items(monkeypatch):
    calls = _fake_graph(monkeypatch, [
        (True, {"value": [
            {"id": "m1", "parentFolderId": "INBOX"},
            {"id": "m2", "parentFolderId": "DELETED"},
            {"id": "m3", "parentFolderId": "SENT"},
        ]}),
        (True, {"id": "DELETED"}),
    ])
    ok, ids = og.resolve_targets("u1", conversation_id="conv'1", mailbox="s@x.com")
    assert ok and ids == ["m1", "m3"]
    assert calls[0]["params"]["$filter"] == "conversationId eq 'conv''1'"
    assert calls[0]["mailbox"] == "s@x.com"
    assert calls[1]["endpoint"] == "/me/mailFolders/deleteditems"


def test_resolve_targets_conversation_can_include_deleted(monkeypatch):
    _fake_graph(monkeypatch, [
        (True, {"value": [{"id": "m1", "parentFolderId": "INBOX"}, {"id": "m2", "parentFolderId": "DELETED"}]}),
    ])
    ok, ids = og.resolve_targets("u1", conversation_id="c", include_deleted=True)
    assert ok and ids == ["m1", "m2"]


def test_resolve_targets_conversation_with_no_messages_is_an_error(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": []})])
    ok, err = og.resolve_targets("u1", conversation_id="c")
    assert not ok and "No messages found" in err


def test_summarize_batch_renders_success_partial_and_total_failure():
    ids = ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "b"]
    ok_all = [{"id": "0", "ok": True, "error": None}, {"id": "1", "ok": True, "error": None}]
    assert og.summarize_batch(ok_all, ids, "moved to archive") == "[Success]: 2 messages moved to archive."
    assert og.summarize_batch(ok_all[:1], ids[:1], "moved to archive") == "[Success]: 1 message moved to archive."
    partial = [{"id": "0", "ok": True, "error": None}, {"id": "1", "ok": False, "error": "API Error (404): gone"}]
    out = og.summarize_batch(partial, ids, "moved to archive")
    assert out.startswith("[Partial]: 1/2 messages moved to archive; 1 failed:")
    assert "b...: API Error (404): gone" in out
    dead = [{"id": "0", "ok": False, "error": "e0"}, {"id": "1", "ok": False, "error": "e1"}]
    assert og.summarize_batch(dead, ids, "moved to archive").startswith("[Error]: 0/2 messages moved to archive:")


# ---------------------------------------------------------------------------
# B7: folder resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling,expected", [
    ("trash", "deleteditems"), ("Spam", "junkemail"), ("sent", "sentitems"),
    ("Sent Items", "sentitems"), ("deleted", "deleteditems"), ("ARCHIVE", "archive"),
    ("inbox", "inbox"), ("junk", "junkemail"),
])
def test_resolve_folder_accepts_every_alias_spelling_without_a_request(monkeypatch, spelling, expected):
    calls = _fake_graph(monkeypatch, [])
    assert og.resolve_folder("u1", spelling) == (True, expected)
    assert calls == []


def test_resolve_folder_passes_a_graph_id_through(monkeypatch):
    calls = _fake_graph(monkeypatch, [])
    fid = "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OAAuAAAAAAAiQ8W967B7TKBjgx9rVEURAQAiIsqMbYjsT5e-T7KzowPTAAAAAAEMAAA="
    assert og.resolve_folder("u1", fid) == (True, fid)
    assert calls == []


def test_resolve_folder_walks_a_slash_path(monkeypatch):
    calls = _fake_graph(monkeypatch, [
        (True, {"value": [{"id": "CLIENTS", "displayName": "Clients"}, {"id": "OTHER", "displayName": "Other"}]}),
        (True, {"value": [{"id": "ACME", "displayName": "ACME"}]}),
    ])
    assert og.resolve_folder("u1", "clients/Acme") == (True, "ACME")
    assert calls[0]["endpoint"] == "/me/mailFolders"
    assert calls[1]["endpoint"] == "/me/mailFolders/CLIENTS/childFolders"


def test_resolve_folder_path_may_start_with_a_well_known_alias(monkeypatch):
    calls = _fake_graph(monkeypatch, [(True, {"value": [{"id": "X", "displayName": "Receipts"}]})])
    assert og.resolve_folder("u1", "inbox/Receipts") == (True, "X")
    assert calls[0]["endpoint"] == "/me/mailFolders/inbox/childFolders"


def test_resolve_folder_refuses_ambiguous_names_listing_both_ids(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": [
        {"id": "ID1", "displayName": "Clients"}, {"id": "ID2", "displayName": "clients"},
    ]})])
    ok, err = og.resolve_folder("u1", "Clients")
    assert not ok and "ambiguous" in err and "ID1" in err and "ID2" in err


def test_resolve_folder_missing_name_points_at_the_folder_tools(monkeypatch):
    _fake_graph(monkeypatch, [(True, {"value": [{"id": "A", "displayName": "Alpha"}]})])
    ok, err = og.resolve_folder("u1", "Beta")
    assert not ok
    assert "No folder named 'Beta'" in err
    assert "outlook_list_folders" in err and "outlook_manage_folder" in err


def test_resolve_folder_surfaces_graph_errors_and_empty_input(monkeypatch):
    _fake_graph(monkeypatch, [(False, "API Error (401): nope")])
    assert og.resolve_folder("u1", "Clients") == (False, "API Error (401): nope")
    assert og.resolve_folder("u1", "  ") == (False, "Folder name is required.")


# ---------------------------------------------------------------------------
# B8: token errors name the account
# ---------------------------------------------------------------------------


def test_rejected_refresh_names_the_account_and_status(monkeypatch):
    _bind_cache(monkeypatch, {"A": {
        "email": "sales@_prv_a.com.au", "access_token": "old", "refresh_token": "dead",
        "expires_at": time.time() - 5,
    }})
    _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(
        400, {"error": "invalid_grant", "error_description": "AADSTS70000: refresh token expired"}, text="x",
    ))
    with pytest.raises(og.OutlookTokenError) as exc:
        og.acquire_access_token("u1")
    msg = str(exc.value)
    assert "sales@_prv_a.com.au" in msg and "400" in msg and "AADSTS70000" in msg
    assert "request_credential" in msg

    ok, err = og.graph_request("u1", "GET", "/me/messages")
    assert not ok and "sales@_prv_a.com.au" in err
    assert "No authenticated" not in err


def test_missing_refresh_token_is_named_not_hidden(monkeypatch):
    _bind_cache(monkeypatch, {"A": {"email": "a@x.com", "access_token": "old", "expires_at": time.time() - 5}})
    with pytest.raises(og.OutlookTokenError) as exc:
        og.acquire_access_token("u1")
    assert "no refresh token" in str(exc.value) and "a@x.com" in str(exc.value)


def test_get_access_token_stays_tolerant_for_autonomous_callers(monkeypatch):
    # Poll sources and channels treat None as "skip"; selection errors still raise.
    _bind_cache(monkeypatch, {"A": {"email": "a@x.com", "access_token": "old", "expires_at": time.time() - 5}})
    assert og.get_access_token("u1") is None
    _bind_cache(monkeypatch, {"A": _live_account(), "B": _live_account()})
    with pytest.raises(OAuthAccountSelectionError):
        og.get_access_token("u1")


def test_graph_request_error_text_carries_the_graph_error_code(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(
        403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}, text="x",
    ))
    ok, err = og.graph_request("u1", "GET", "/me/outlook/masterCategories")
    assert not ok and err == "API Error (403): ErrorAccessDenied: Access is denied."


@pytest.mark.parametrize("status,code,message", [
    (404, "ErrorItemNotFound", "The specified object was not found in the store., Default folder Inbox not found."),
    (403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."),
])
def test_graph_request_names_the_shared_mailbox_on_403_and_404(monkeypatch, status, code, message):
    # Verified live 2026-09-15: an account without delegated access gets a bare
    # 404 "Default folder Inbox not found", which reads like a bug in the tool.
    _bind_cache(monkeypatch, {"A": _live_account()})
    _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(status, {"error": {"code": code, "message": message}}, text="x"))
    ok, err = og.graph_request("u1", "GET", "/me/mailFolders/inbox/messages", mailbox="sales@_prv_a.com.au")
    assert not ok
    assert err == (
        f"API Error ({status}): {code}: {message} (shared mailbox sales@_prv_a.com.au: the signed-in account "
        "may not have been granted access to it in Exchange, or the address is wrong)"
    )
    ok, err = og.graph_request("u1", "GET", "/me/mailFolders/inbox/messages")
    assert err == f"API Error ({status}): {code}: {message}"  # own mailbox: no hint


# ---------------------------------------------------------------------------
# B27: compatibility re-exports
# ---------------------------------------------------------------------------


def test_outlook_email_still_exports_the_names_other_modules_import():
    from nymeria.tools import outlook_email as oe

    for name in ("graph_request", "get_access_token", "get_account", "GRAPH_BASE", "TOKEN_URL",
                 "format_email_summary", "OAuthAccountSelectionError", "_resolve_folder_alias",
                 "_is_inline_signature_image"):
        assert hasattr(oe, name), name
    assert oe.graph_request is og.graph_request
    assert oe.get_access_token is og.get_access_token


def test_legacy_positional_graph_request_shape_still_works(monkeypatch):
    _bind_cache(monkeypatch, {"A": _live_account()})
    calls = _stub_policy_client(monkeypatch, lambda m, u, **kw: _resp(200, {"id": "x"}))
    from nymeria.tools.outlook_email import graph_request

    ok, body = graph_request("u1", "PATCH", "/me/messages/x", None, {"isRead": True}, {"$select": "id"})
    assert ok and body == {"id": "x"}
    assert calls[0][0] == "PATCH"
    assert calls[0][2]["json"] == {"isRead": True}
    assert calls[0][2]["params"] == {"$select": "id"}


# ---------------------------------------------------------------------------
# Review fixes: mailbox must be a plain address; the conversation cap is judged
# on what Graph returned, before the Deleted Items filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "sales@x.com/../me", "sales@x.com?$select=body", "sales@x.com#frag", "sales%40x.com",
    "sales@x.com/messages", "not-an-address", "a@b", "two words@x.com",
])
def test_graph_request_and_batch_refuse_a_malformed_mailbox_before_touching_the_token(monkeypatch, bad):
    def _boom(*a, **k):
        raise AssertionError("token acquired for a malformed mailbox")

    monkeypatch.setattr(og, "acquire_access_token", _boom)
    monkeypatch.setattr(og, "_http_client", _boom)
    ok, err = og.graph_request("u1", "GET", "/me/messages", mailbox=bad)
    assert ok is False
    assert err.startswith("mailbox must be a plain address such as sales@example.com (got ")
    results = og.graph_batch("u1", [{"id": "0", "method": "GET", "url": "/me/messages/m1"}], mailbox=bad)
    assert len(results) == 1 and results[0]["ok"] is False
    assert results[0]["error"] == err


@pytest.mark.parametrize("good", ["sales@x.com", "o'brien@x.co.uk", "first.last+tag@sub.example.com"])
def test_mailbox_error_accepts_ordinary_addresses(good):
    assert og.mailbox_error(good) is None
    assert og.mailbox_error("  " + good + " ") is None
    assert og.mailbox_error("") is None and og.mailbox_error(None) is None


def test_resolve_targets_conversation_cap_is_judged_before_the_deleted_filter(monkeypatch):
    # 51 messages come back, 40 of them in Deleted Items: the cap is still hit,
    # because a longer conversation than the page shows cannot be mutated as a
    # silent subset.
    rows = [{"id": f"m{i}", "parentFolderId": "DELETED" if i < 40 else "INBOX"} for i in range(51)]
    calls = _fake_graph(monkeypatch, [(True, {"value": rows}), (True, {"id": "DELETED"})])
    ok, err = og.resolve_targets("u1", conversation_id="big")
    assert ok is False
    assert err.startswith("Conversation 'big...' has more than 50 messages; the limit is 50 per call.")
    assert len(calls) == 1  # refused before the Deleted Items lookup
