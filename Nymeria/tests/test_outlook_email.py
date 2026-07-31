"""Unit tests for Outlook email helper functions.

Covers the small extractions from optimization slice 16: the KQL suffix
builder (F5), the hoisted recipient parser (F7), the shared inline-image skip
predicate (F9), the symmetric body-truncation marker (F14), and the shared
account-selection helper plus no-op deletion (F13 steps 1+3).
"""

import time
from types import SimpleNamespace

from nymeria.tools import outlook_email as oe



def _stub_policy_client(monkeypatch, module, **handlers):
    """Point ``module._http_client`` at a stub exposing the given methods.

    These paths build a client from the policy factory and call it inside a
    ``with`` block, so the seam is the FACTORY, not ``httpx``. Patching httpx
    directly used to work and silently stopped meaning anything the moment the
    call went through the factory, which is why this helper exists rather than
    a per-test lambda: one place to change if the shape moves again.
    """
    captured_kwargs = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    for name, fn in handlers.items():
        setattr(_Client, name, staticmethod(fn))

    def _factory(**kwargs):
        captured_kwargs.update(kwargs)
        return _Client()

    monkeypatch.setattr(module, "_http_client", _factory)
    return captured_kwargs

def test_build_kql_suffix_combines_structured_filters():
    suffix = oe._build_kql_suffix(
        sender="a@x.com", recipient="b@y.com", subject="Hello", has_attachments=True
    )
    assert suffix == "from:a@x.com to:b@y.com subject:Hello hasattachment:true"


def test_build_kql_suffix_empty_when_no_filters():
    assert oe._build_kql_suffix() == ""


def test_build_kql_suffix_partial_filters():
    assert oe._build_kql_suffix(sender="a@x.com") == "from:a@x.com"
    assert oe._build_kql_suffix(has_attachments=True) == "hasattachment:true"


def test_parse_recipients_splits_and_wraps():
    assert oe._parse_recipients("a@x.com, b@y.com") == [
        {"emailAddress": {"address": "a@x.com"}},
        {"emailAddress": {"address": "b@y.com"}},
    ]


def test_parse_recipients_ignores_blanks_and_empty():
    assert oe._parse_recipients(" , a@x.com ,, ") == [{"emailAddress": {"address": "a@x.com"}}]
    assert oe._parse_recipients("") == []


def test_is_inline_signature_image_predicate():
    # Small inline image -> skipped.
    assert oe._is_inline_signature_image(is_inline=True, mime="image/png", size=1000) is True
    # Above the byte threshold -> kept.
    assert oe._is_inline_signature_image(is_inline=True, mime="image/png", size=60000) is False
    # Not inline -> kept.
    assert oe._is_inline_signature_image(is_inline=False, mime="image/png", size=1000) is False
    # Not an image -> kept.
    assert oe._is_inline_signature_image(is_inline=True, mime="application/pdf", size=1000) is False


def test_truncate_body_returns_short_text_unchanged():
    short = "hello world"
    assert oe._truncate_body(short) is short


def test_truncate_body_marks_when_trimmed():
    long = "x" * (oe._BODY_PREVIEW_CHARS + 500)
    out = oe._truncate_body(long)
    assert out.startswith("x" * oe._BODY_PREVIEW_CHARS)
    assert out.endswith("...[truncated 500 chars]")


def _email(content_type, content):
    return {
        "subject": "Subject",
        "from": {"emailAddress": {"address": "a@x.com", "name": "Alice"}},
        "receivedDateTime": "2026-06-21T10:00:00Z",
        "toRecipients": [{"emailAddress": {"address": "b@y.com"}}],
        "body": {"contentType": content_type, "content": content},
    }


def test_format_single_email_truncates_long_plain_text():
    # Plain-text bodies were previously not truncated at all (F14).
    long_body = "y" * (oe._BODY_PREVIEW_CHARS + 100)
    out = oe._format_single_email(_email("text", long_body))
    assert "...[truncated 100 chars]" in out


def test_format_single_email_caps_html_after_conversion():
    long_html = "<p>" + ("z" * (oe._BODY_PREVIEW_CHARS + 50)) + "</p>"
    out = oe._format_single_email(_email("html", long_html))
    assert "...[truncated" in out


def test_format_single_email_keeps_short_body_intact():
    out = oe._format_single_email(_email("text", "just a short note"))
    assert "just a short note" in out
    assert "[truncated" not in out


# ---------------------------------------------------------------------------
# outlook_search_emails / outlook_list_emails dispatch + render (slice 16 F11)
#
# The multi-mode search dispatcher and the shared message-render loop had no
# prior direct coverage. These lock the per-mode behavior (thread / kql /
# filters-only / single / batch / error) and the exact render spacing before
# the F11 extraction, then guard it after.
# ---------------------------------------------------------------------------

_CFG = {"configurable": {"user_id": "u1", "thread_id": "t1"}}


def _stub_summary(monkeypatch):
    """Make format_email_summary deterministic: ``S:<id>`` per message."""
    monkeypatch.setattr(oe, "format_email_summary", lambda m: f"S:{m.get('id', '')}")


def _bind_graph(monkeypatch, responses, calls):
    """Replace graph_request with a recorder yielding ``responses`` in order."""
    seq = iter(responses)

    def _fake(user_id, method, endpoint, account_id=None, params=None):
        calls.append({
            "user_id": user_id,
            "method": method,
            "endpoint": endpoint,
            "account_id": account_id,
            "params": dict(params or {}),
        })
        return next(seq)

    monkeypatch.setattr(oe, "graph_request", _fake)


def test_search_thread_mode_sorts_chronologically_and_renders(monkeypatch):
    _stub_summary(monkeypatch)
    calls = []
    # Returned out of order; the thread branch sorts by receivedDateTime asc.
    messages = [
        {"id": "b", "receivedDateTime": "2026-01-02T00:00:00Z"},
        {"id": "a", "receivedDateTime": "2026-01-01T00:00:00Z"},
    ]
    _bind_graph(monkeypatch, [(True, {"value": messages})], calls)

    out = oe.outlook_search_emails.invoke({"thread_id": "THREAD-XYZ"}, config=_CFG)

    assert out == (
        "[Success]: 2 email(s) in thread (chronological):\n\nS:a\n\nS:b\n"
    )
    # Thread mode queries /me/messages with a conversationId filter, no $search,
    # and the per-call limit cap (min(limit, 25)) reaches $top.
    assert calls[0]["endpoint"] == "/me/messages"
    assert calls[0]["params"]["$filter"] == "conversationId eq 'THREAD-XYZ'"
    assert calls[0]["params"]["$top"] == 10
    assert "$search" not in calls[0]["params"]


def test_search_thread_mode_empty(monkeypatch):
    _stub_summary(monkeypatch)
    _bind_graph(monkeypatch, [(True, {"value": []})], [])
    out = oe.outlook_search_emails.invoke({"thread_id": "abcdefghijklmnopqrstuvwxyz"}, config=_CFG)
    # Thread id is truncated to 20 chars in the info message.
    assert out == "[Info]: No emails found for thread 'abcdefghijklmnopqrst...'."


def test_search_thread_mode_error(monkeypatch):
    _stub_summary(monkeypatch)
    _bind_graph(monkeypatch, [(False, "graph boom")], [])
    out = oe.outlook_search_emails.invoke({"thread_id": "T1"}, config=_CFG)
    assert out == "[Error]: graph boom"


def test_search_single_query_renders_match_header(monkeypatch):
    _stub_summary(monkeypatch)
    calls = []
    _bind_graph(monkeypatch, [(True, {"value": [{"id": "m1"}, {"id": "m2"}]})], calls)

    out = oe.outlook_search_emails.invoke({"query": "foo"}, config=_CFG)

    assert out == "[Success]: Found 2 email(s) matching 'foo':\n\nS:m1\n\nS:m2\n"
    assert calls[0]["params"]["$search"] == '"foo"'


def test_search_filters_only_uses_kql_suffix_as_query(monkeypatch):
    _stub_summary(monkeypatch)
    calls = []
    _bind_graph(monkeypatch, [(True, {"value": [{"id": "m1"}]})], calls)

    # No query/queries, but a sender filter => kql_suffix becomes the query.
    out = oe.outlook_search_emails.invoke({"sender": "acme"}, config=_CFG)

    assert out == "[Success]: Found 1 email(s) matching 'from:acme':\n\nS:m1\n"
    assert calls[0]["params"]["$search"] == '"from:acme"'


def test_search_kql_mode_passes_raw_query(monkeypatch):
    _stub_summary(monkeypatch)
    calls = []
    _bind_graph(monkeypatch, [(True, {"value": [{"id": "m1"}]})], calls)

    out = oe.outlook_search_emails.invoke({"kql": "from:x OR from:y"}, config=_CFG)

    assert out == "[Success]: Found 1 email(s) matching 'from:x OR from:y':\n\nS:m1\n"
    assert calls[0]["params"]["$search"] == '"from:x OR from:y"'


def test_search_batch_mode_groups_with_delimiters(monkeypatch):
    _stub_summary(monkeypatch)
    calls = []
    _bind_graph(
        monkeypatch,
        [(True, {"value": [{"id": "a1"}]}), (True, {"value": [{"id": "b1"}]})],
        calls,
    )

    out = oe.outlook_search_emails.invoke({"queries": "alpha | beta"}, config=_CFG)

    assert out == (
        "=== Search 1/2: alpha ===\n"
        "[Success]: Found 1 email(s) matching 'alpha':\n\nS:a1\n"
        "\n\n"
        "=== Search 2/2: beta ===\n"
        "[Success]: Found 1 email(s) matching 'beta':\n\nS:b1\n"
    )
    assert [c["params"]["$search"] for c in calls] == ['"alpha"', '"beta"']


def test_search_no_criteria_error(monkeypatch):
    _stub_summary(monkeypatch)
    _bind_graph(monkeypatch, [], [])  # no graph call expected
    out = oe.outlook_search_emails.invoke({}, config=_CFG)
    assert out == "[Error]: Provide a query, filters, or both."


def test_list_emails_render_header_and_spacing(monkeypatch):
    _stub_summary(monkeypatch)
    calls = []
    _bind_graph(monkeypatch, [(True, {"value": [{"id": "m1"}, {"id": "m2"}]})], calls)

    out = oe.outlook_list_emails.invoke({"folder": "inbox"}, config=_CFG)

    assert out == "[Success]: Found 2 email(s) in inbox:\n\nS:m1\n\nS:m2\n"
    assert calls[0]["endpoint"] == "/me/mailFolders/inbox/messages"


def test_search_retries_dropping_filter_on_failure(monkeypatch):
    # _search_single_query (left untouched by F11) retries with the date $filter
    # dropped when a combined $filter + $search request fails. Lock that path so
    # the extraction's "don't touch the retry logic" promise stays guarded.
    _stub_summary(monkeypatch)
    calls = []
    _bind_graph(
        monkeypatch,
        [(False, "filter rejected"), (True, {"value": [{"id": "m1"}]})],
        calls,
    )

    out = oe.outlook_search_emails.invoke({"query": "foo", "days_back": 7}, config=_CFG)

    assert out == "[Success]: Found 1 email(s) matching 'foo':\n\nS:m1\n"
    # First attempt carries both $search and the date $filter; the retry drops
    # the filter (and its $orderby) but keeps the search.
    assert "$filter" in calls[0]["params"] and calls[0]["params"]["$search"] == '"foo"'
    assert "$filter" not in calls[1]["params"]
    assert "$orderby" not in calls[1]["params"]
    assert calls[1]["params"]["$search"] == '"foo"'


def test_render_message_list_spacing_contract(monkeypatch):
    # The shared render helper is the single source of truth for spacing; the
    # header carries its own trailing newline and each message is followed by a
    # blank line.
    _stub_summary(monkeypatch)
    assert oe._render_message_list("HEAD:\n", []) == "HEAD:\n"
    assert oe._render_message_list("HEAD:\n", [{"id": "x"}]) == "HEAD:\n\nS:x\n"
    assert (
        oe._render_message_list("HEAD:\n", [{"id": "x"}, {"id": "y"}])
        == "HEAD:\n\nS:x\n\nS:y\n"
    )


# ---------------------------------------------------------------------------
# Account selection (slice 16 F13 steps 1+3)
#
# _select_account is the single "explicit > configured-default > first" helper
# shared by get_account and get_access_token. These lock the priority order and
# the (account_id, account) pair contract, the get_account/get_access_token
# wiring that routes through it, and the no-op .replace deletion (step 3).
# ---------------------------------------------------------------------------


def _patch_default(monkeypatch, default_id):
    """Pin outlook_default_account_id for the configured-default branch."""
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(outlook_default_account_id=default_id),
    )


def test_select_account_explicit_present_returns_pair():
    accounts = {"A": {"id": "A"}, "B": {"id": "B"}}
    assert oe._select_account(accounts, "B") == ("B", {"id": "B"})


def test_select_account_explicit_absent_returns_id_with_none():
    # An explicit id never falls back to default/first; the account is None.
    accounts = {"A": {"id": "A"}}
    assert oe._select_account(accounts, "Z") == ("Z", None)


def test_select_account_uses_configured_default(monkeypatch):
    _patch_default(monkeypatch, "B")
    accounts = {"A": {"id": "A"}, "B": {"id": "B"}}
    assert oe._select_account(accounts) == ("B", {"id": "B"})


def test_select_account_default_missing_falls_to_first(monkeypatch):
    _patch_default(monkeypatch, "Z")  # configured but not in the cache
    accounts = {"A": {"id": "A"}, "B": {"id": "B"}}
    assert oe._select_account(accounts) == ("A", {"id": "A"})


def test_select_account_no_default_returns_first(monkeypatch):
    _patch_default(monkeypatch, None)
    accounts = {"A": {"id": "A"}, "B": {"id": "B"}}
    assert oe._select_account(accounts) == ("A", {"id": "A"})


def test_select_account_settings_error_falls_to_first(monkeypatch):
    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("nymeria.config.get_settings", _boom)
    accounts = {"A": {"id": "A"}, "B": {"id": "B"}}
    assert oe._select_account(accounts) == ("A", {"id": "A"})


def _bind_cache(monkeypatch, accounts):
    source = SimpleNamespace(cache={"accounts": dict(accounts)}, persist=lambda c: None)
    monkeypatch.setattr(oe.auth_utils, "resolve_oauth_cache", lambda *a, **k: source)
    return source


def test_get_account_routes_through_select_account(monkeypatch):
    _patch_default(monkeypatch, None)
    _bind_cache(monkeypatch, {"A": {"id": "A"}, "B": {"id": "B"}})
    assert oe.get_account("u1") == {"id": "A"}  # first
    assert oe.get_account("u1", "B") == {"id": "B"}  # explicit
    assert oe.get_account("u1", "Z") is None  # explicit-missing


def test_get_account_empty_cache_returns_none(monkeypatch):
    _bind_cache(monkeypatch, {})
    assert oe.get_account("u1") is None


def test_get_access_token_selects_account_for_valid_token(monkeypatch):
    # A non-expired token short-circuits before any refresh; this confirms the
    # selection (default vs explicit) feeds the token read without touching the
    # security-sensitive refresh path.
    _patch_default(monkeypatch, "B")
    future = time.time() + 3600
    accounts = {
        "A": {"id": "A", "access_token": "tokA", "expires_at": future},
        "B": {"id": "B", "access_token": "tokB", "expires_at": future},
    }
    _bind_cache(monkeypatch, accounts)
    assert oe.get_access_token("u1") == "tokB"  # configured default
    assert oe.get_access_token("u1", "A") == "tokA"  # explicit override


def test_get_access_token_refreshes_and_persists_under_selected_key(monkeypatch):
    # An expired token drives the refresh branch; this locks that the resolved
    # account KEY (aid), not just the account value, threads into the write-back
    # cache["accounts"][aid] = account and the persist callback.
    _patch_default(monkeypatch, "B")
    past = time.time() - 10
    accounts = {
        "A": {"id": "A", "access_token": "oldA", "refresh_token": "rA", "expires_at": past},
        "B": {"id": "B", "access_token": "oldB", "refresh_token": "rB", "expires_at": past},
    }
    source = _bind_cache(monkeypatch, accounts)
    persisted = []
    source.persist = lambda cache: persisted.append(cache)

    _stub_policy_client(
        monkeypatch,
        oe,
        post=lambda url, **kw: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "newB",
                "refresh_token": "rB2",
                "expires_in": 3600,
            },
        ),
    )

    assert oe.get_access_token("u1") == "newB"
    # The configured-default account "B" was refreshed and written back under
    # its own key; the unselected account "A" is untouched.
    assert persisted and persisted[-1]["accounts"]["B"]["access_token"] == "newB"
    assert persisted[-1]["accounts"]["A"]["access_token"] == "oldA"


def test_try_complete_pending_auth_posts_to_bare_token_url(monkeypatch):
    # Step 3 removed a no-op TOKEN_URL.replace("/token", "/token"); lock that the
    # device-code exchange still posts to the bare TOKEN_URL.
    monkeypatch.setattr(
        oe.auth_utils,
        "load_token_cache",
        lambda *a, **k: {
            "pending_auth": {"device_code": "dc-1", "expires_at": time.time() + 100}
        },
    )
    posted = {}

    def _fake_post(url, **kwargs):
        posted["url"] = url
        return SimpleNamespace(status_code=400)  # non-200 short-circuits to False

    _stub_policy_client(monkeypatch, oe, post=_fake_post)

    assert oe.try_complete_pending_auth("u1") is False
    assert posted["url"] == oe.TOKEN_URL
