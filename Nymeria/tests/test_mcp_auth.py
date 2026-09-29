"""Tests for MCP inbound auth and Act-As pinning (C-4)."""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import nymeria.mcp_auth as mcp_auth


# -- effective_act_as -------------------------------------------------------


def test_effective_act_as_no_identity_is_passthrough():
    # STDIO / local mode: no resolved identity, caller value preserved.
    assert mcp_auth.effective_act_as("alice") == "alice"
    assert mcp_auth.effective_act_as(None) is None


def test_effective_act_as_admin_keeps_act_as():
    token = mcp_auth.set_identity({"user_id": "admin", "role": "admin"})
    try:
        assert mcp_auth.effective_act_as("victim") == "victim"
        assert mcp_auth.effective_act_as("default") == "admin"
        assert mcp_auth.effective_act_as(None) == "admin"
    finally:
        mcp_auth.reset_identity(token)


def test_effective_act_as_non_admin_is_pinned_to_self():
    token = mcp_auth.set_identity({"user_id": "bob", "role": "user"})
    try:
        # A non-admin cannot Act-As anyone else, regardless of the argument.
        assert mcp_auth.effective_act_as("victim") == "bob"
        assert mcp_auth.effective_act_as("default") == "bob"
        assert mcp_auth.effective_act_as(None) == "bob"
    finally:
        mcp_auth.reset_identity(token)


# -- MCPAuthMiddleware ------------------------------------------------------


async def _inner(request):
    return JSONResponse({"identity": mcp_auth.current_identity()})


def _client() -> TestClient:
    inner = Starlette(routes=[Route("/x", _inner, methods=["GET", "POST"])])
    app = mcp_auth.MCPAuthMiddleware(inner, resolve_base_url="http://backend.test")
    return TestClient(app)


def test_middleware_rejects_missing_bearer(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    resp = _client().post("/x")
    assert resp.status_code == 401


def test_middleware_rejects_invalid_bearer(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)

    async def fake_resolve(base_url, token):
        return None

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    resp = _client().post("/x", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_middleware_accepts_valid_bearer_and_sets_identity(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)

    async def fake_resolve(base_url, token):
        return {"user_id": "bob", "role": "user"}

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    resp = _client().post("/x", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert resp.json()["identity"] == {"user_id": "bob", "role": "user"}


def test_middleware_allow_unauthenticated_escape_hatch(monkeypatch):
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "true")
    resp = _client().post("/x")
    assert resp.status_code == 200
    assert resp.json()["identity"] is None


def test_middleware_skips_auth_for_options(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    # OPTIONS must not be challenged with 401 (no auth header on preflight).
    resp = _client().options("/x")
    assert resp.status_code != 401


# -- #374: a backend that cannot answer is not a bad credential --------------
# The real resolver runs through the middleware; only the HTTP transport is
# faked, so status mapping, caching, and the response shape are all covered.


class _Backend:
    """A scripted /me: each request takes the current ``reply`` (a status, a
    (status, json) pair, or an exception to raise)."""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert request.url.path == "/me"
        if isinstance(self.reply, Exception):
            raise self.reply
        status, payload = self.reply if isinstance(self.reply, tuple) else (self.reply, None)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setattr(mcp_auth, "_RESOLVE_CACHE", {})
    scripted = _Backend(200)
    monkeypatch.setattr(mcp_auth, "_HTTP_TRANSPORT", httpx.MockTransport(scripted))
    return scripted


def _call(client: TestClient):
    return client.post("/x", headers={"Authorization": "Bearer tok"})


_OK = (200, {"id": "bob", "role": "user"})


def _assert_retry_later(resp) -> None:
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    # No Bearer challenge: that is what tells a client to (re)authenticate.
    assert "www-authenticate" not in resp.headers
    assert resp.json()["type"] == "unavailable"


@pytest.mark.parametrize(
    "outage",
    [
        httpx.ConnectError("connection refused"),  # the API container still starting
        httpx.ReadTimeout("slow"),
        502,
        503,
    ],
)
def test_an_unreachable_backend_answers_retry_later_and_is_not_cached(backend, outage):
    # A1, A2: during a compose restart; the first request after the backend
    # returns must succeed at once, not wait out a negative cache.
    client = _client()
    backend.reply = outage
    _assert_retry_later(_call(client))
    backend.reply = _OK
    resp = _call(client)
    assert resp.status_code == 200
    assert resp.json()["identity"] == {"user_id": "bob", "role": "user"}


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_still_a_401_challenge_and_is_cached(backend, status):
    # A3: fail-closed and unchanged for a real bad credential.
    client = _client()
    backend.reply = status
    for _ in range(2):
        resp = _call(client)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
    assert backend.calls == 1  # the second answer came from the negative cache


def test_a_valid_token_passes_and_is_cached(backend):
    # A4
    client = _client()
    backend.reply = _OK
    assert _call(client).json()["identity"] == {"user_id": "bob", "role": "user"}
    assert _call(client).status_code == 200
    assert backend.calls == 1


@pytest.mark.parametrize(
    "reply",
    [
        404,  # the base URL points at something that is not Nymeria
        (200, {"detail": "no id here"}),
        (200, "<html>not json</html>"),
        (200, ["not", "an", "object"]),
    ],
)
def test_a_misconfigured_or_broken_backend_is_not_a_bad_credential(backend, reply):
    # A5: never "your token is wrong", never access, never cached; and not
    # "retry shortly" either, since a wrong API URL does not heal by waiting.
    client = _client()
    backend.reply = reply
    for _ in range(2):
        resp = _call(client)
        assert resp.status_code == 502
        assert "www-authenticate" not in resp.headers
        assert resp.json()["type"] == "unexpected"
    assert backend.calls == 2


def test_a_rate_limited_resolution_is_a_rejection_but_is_not_cached(backend):
    # The API answers /me 429 only from its auth-FAILURE limiter, which every
    # MCP resolution shares: a junk-token spray must not turn a genuinely
    # revoked token into "retry shortly", nor fill the negative cache.
    client = _client()
    backend.reply = 429
    for _ in range(2):
        resp = _call(client)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
    assert backend.calls == 2


@pytest.mark.parametrize("bearer", [b"Bearer nym_\xe9", b"Bearer two words", b"Bearer "])
def test_a_malformed_bearer_is_refused_without_asking_the_backend(backend, bearer):
    # Not token-shaped: it cannot be a Nymeria token, and forwarding a
    # non-ASCII one failed inside the HTTP client as a false "unavailable".
    resp = _client().post("/x", headers={"Authorization": bearer})
    assert resp.status_code == 401
    assert backend.calls == 0


def test_a_recently_resolved_token_rides_out_an_outage(backend):
    # The 60 s positive cache: restarting only the API interrupts nobody.
    client = _client()
    backend.reply = _OK
    assert _call(client).status_code == 200
    backend.reply = httpx.ConnectError("api restarting")
    resp = _call(client)
    assert resp.status_code == 200
    assert resp.json()["identity"] == {"user_id": "bob", "role": "user"}
