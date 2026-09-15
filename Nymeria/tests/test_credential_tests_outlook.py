"""The ``auth_test`` probe for Outlook credentials (intake 20260908-024925Z).

Behaviours pinned here:

- The probe refreshes FIRST, with the scope string the tools use (stored
  scopes plus ``offline_access``) and the credential's own client id, then
  proves the refreshed token at ``GET /me``.
- A rejected refresh is reported as ``refresh_rejected`` with Entra's detail,
  never reaches Graph, and never echoes the refresh token.
- A credential with no refresh token is NOT ok even when ``/me`` succeeds,
  because it dies with the current access token.
- Graph rejecting the token is ``http_error`` with the token redacted.
- ``test_credential_fields(provider="outlook")`` dispatches to this probe
  (before this pass Outlook fell through to ``no_tester``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

import nymeria.core.credential_tests as credential_tests
from nymeria.core import http_policy
from nymeria.tools import outlook_graph as og

TOKEN_URL = og.TOKEN_URL
ME_URL = f"{og.GRAPH_BASE}/me"


class _Response:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.reason_phrase = "OK" if status_code == 200 else "Error"

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _wire(monkeypatch, *, token=None, me=None):
    """Fake both fixed hosts behind the policy client; record every call."""
    calls: list[dict[str, Any]] = []

    def _bare_client_forbidden(*args, **kwargs):
        raise AssertionError("bare httpx.Client constructed; probes must use policy_http_client")

    monkeypatch.setattr(httpx, "Client", _bare_client_forbidden)

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, data=None, headers=None, json=None):
            calls.append({"method": "POST", "url": url, "data": data or {}, "headers": headers or {}})
            assert url == TOKEN_URL
            assert token is not None, "unexpected token request"
            return _Response(*token)

        def get(self, url, headers=None, params=None):
            calls.append({"method": "GET", "url": url, "headers": headers or {}})
            assert url == ME_URL
            assert me is not None, "unexpected Graph request"
            return _Response(*me)

    def factory(**kwargs):
        calls.append({"method": "FACTORY", "kwargs": kwargs})
        return _Client()

    monkeypatch.setattr(http_policy, "policy_http_client", factory)
    monkeypatch.setattr(og, "_http_client", factory)
    return calls


def _run(metadata=None, fields=None):
    return asyncio.run(
        credential_tests._test_outlook("outlook", "oauth_token", metadata or {}, fields or {}, None)
    )


META = {"scopes": ["Mail.Read", "MailboxSettings.ReadWrite"], "client_id": "cid-1", "email": "a@b.com"}
FIELDS = {"access_token": "old-access", "refresh_token": "rt-secret-1"}


def test_probe_refreshes_with_stored_scopes_then_proves_the_new_token(monkeypatch):
    calls = _wire(
        monkeypatch,
        token=(200, {"access_token": "new-access", "expires_in": 3600, "scope": "Mail.Read MailboxSettings.ReadWrite"}),
        me=(200, {"mail": "a@b.com", "displayName": "A"}),
    )
    result = _run(META, FIELDS)

    assert result.ok is True
    assert result.code == "verified"
    assert result.verified is True
    assert result.message == "Outlook token works for a@b.com (refresh token verified)."
    assert result.metadata == {
        "status_code": 200,
        "email": "a@b.com",
        "refreshed": True,
        "scopes": ["Mail.Read", "MailboxSettings.ReadWrite"],
    }

    post = next(c for c in calls if c["method"] == "POST")
    assert post["data"] == {
        "client_id": "cid-1",
        "grant_type": "refresh_token",
        "refresh_token": "rt-secret-1",
        "scope": "offline_access Mail.Read MailboxSettings.ReadWrite",
    }
    get = next(c for c in calls if c["method"] == "GET")
    assert get["headers"]["Authorization"] == "Bearer new-access"
    factories = [c for c in calls if c["method"] == "FACTORY"]
    assert factories and all(
        c["kwargs"].get("timeout") == credential_tests._DEFAULT_TIMEOUT_SECONDS for c in factories
    )


def test_rejected_refresh_is_reported_and_graph_is_never_called(monkeypatch):
    calls = _wire(
        monkeypatch,
        token=(400, {"error": "invalid_grant", "error_description": "AADSTS70000: refresh token expired"}),
        me=(200, {"mail": "a@b.com"}),
    )
    result = _run(META, FIELDS)

    assert result.ok is False
    assert result.code == "refresh_rejected"
    assert result.verified is True
    assert "AADSTS70000: refresh token expired" in result.message
    assert "400" in result.message
    assert 'request_credential(provider="outlook", kind="oauth")' in result.message
    assert "rt-secret-1" not in result.message
    assert result.metadata == {"status_code": 400}
    assert [c["method"] for c in calls if c["method"] != "FACTORY"] == ["POST"]


def test_missing_refresh_token_is_not_ok_even_when_graph_accepts(monkeypatch):
    calls = _wire(monkeypatch, me=(200, {"userPrincipalName": "upn@b.com"}))
    result = _run(META, {"access_token": "old-access"})

    assert result.ok is False
    assert result.code == "no_refresh_token"
    assert result.verified is True
    assert result.message == (
        "Outlook token works for upn@b.com (no refresh token stored, so it cannot "
        "outlive the current access token)."
    )
    assert result.metadata == {"status_code": 200, "email": "upn@b.com", "refreshed": False}
    assert [c["method"] for c in calls if c["method"] != "FACTORY"] == ["GET"]
    get = calls[-1]
    assert get["headers"]["Authorization"] == "Bearer old-access"


def test_graph_rejecting_the_token_is_http_error_with_the_token_redacted(monkeypatch):
    _wire(
        monkeypatch,
        me=(401, {"error": {"code": "InvalidAuthenticationToken", "message": "Token old-access is bad"}}),
    )
    result = _run(META, {"access_token": "old-access"})

    assert result.ok is False
    assert result.code == "http_error"
    assert result.verified is True
    assert "HTTP 401" in result.message
    assert "old-access" not in result.message
    assert result.metadata == {"status_code": 401, "refreshed": False}


def test_legacy_credential_without_scope_list_refreshes_with_the_registry_set(monkeypatch):
    calls = _wire(
        monkeypatch,
        token=(200, {"access_token": "new-access"}),
        me=(200, {"mail": "a@b.com"}),
    )
    _run({"client_id": "cid-1"}, FIELDS)
    post = next(c for c in calls if c["method"] == "POST")
    scope = post["data"]["scope"].split()
    assert scope[0] == "offline_access"
    assert "MailboxSettings.ReadWrite" in scope
    assert "Mail.ReadWrite.Shared" in scope


def test_outlook_dispatches_through_test_credential_fields(monkeypatch):
    _wire(
        monkeypatch,
        token=(200, {"access_token": "new-access"}),
        me=(200, {"mail": "a@b.com"}),
    )
    result = asyncio.run(
        credential_tests.test_credential_fields(
            provider="outlook",
            kind="oauth_token",
            metadata=META,
            secret_fields=FIELDS,
        )
    )
    assert result.code == "verified"
    assert result.ok is True
