"""Tests for MCP inbound auth and Act-As pinning (C-4)."""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import nymeria.mcp_auth as mcp_auth


@pytest.fixture(autouse=True)
def _no_real_hold(monkeypatch):
    """Nothing here waits in real time: the #424 hold is off unless a test
    opts in through ``held`` (a fake clock), and the parsed value is fresh."""
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "0")
    mcp_auth._parse_backend_wait.cache_clear()
    monkeypatch.setattr(mcp_auth, "_held_requests", 0)
    yield
    mcp_auth._parse_backend_wait.cache_clear()


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

    async def fake_resolve(base_url, token, **_kwargs):
        return None

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    resp = _client().post("/x", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_middleware_accepts_valid_bearer_and_sets_identity(monkeypatch):
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)

    async def fake_resolve(base_url, token, **_kwargs):
        return {"user_id": "bob", "role": "user"}

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    resp = _client().post("/x", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert resp.json()["identity"] == {"user_id": "bob", "role": "user"}


def test_middleware_allow_unauthenticated_escape_hatch(monkeypatch):
    # Loopback only since #430 (tests/test_mcp_public_access.py has the rest).
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "true")
    client = _client()
    client.base_url = httpx.URL("http://127.0.0.1:8001")
    resp = client.post("/x")
    assert resp.status_code == 200
    assert resp.json()["identity"] is None
    assert client.post("/x", headers={"Host": "testserver"}).status_code == 421


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
    # These pin the answer SHAPES; the #424 hold before a 503 has its own
    # tests below, so here it is off and an outage answers at once.
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "0")
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


# -- #424: ride out an API restart instead of refusing through it ------------

NYM = "nym_held-token"


class _Clock:
    """A fake monotonic clock that the hold's sleeps advance; no real waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


@pytest.fixture
def held(backend, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(mcp_auth, "_monotonic", clock.monotonic)
    monkeypatch.setattr(mcp_auth, "_sleep", clock.sleep)
    monkeypatch.delenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", raising=False)
    return clock


class _Restarting:
    """/me refuses ``down_for`` connections, then answers as Nymeria."""

    def __init__(self, down_for: int, reply=_OK) -> None:
        self.down_for = down_for
        self.reply = reply
        self.calls = 0
        self.timeouts: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.timeouts.append(request.extensions["timeout"]["read"])
        if self.calls <= self.down_for:
            raise httpx.ConnectError("connection refused")
        status, payload = self.reply
        return httpx.Response(status, json=payload)


def _script(monkeypatch, handler) -> None:
    monkeypatch.setattr(mcp_auth, "_HTTP_TRANSPORT", httpx.MockTransport(handler))


def _held_call(client: TestClient, method: str = "POST", token: str = NYM):
    return client.request(method, "/x", headers={"Authorization": f"Bearer {token}"})


def test_a_client_connecting_during_an_api_restart_is_held_then_served(held, monkeypatch, caplog):
    api = _Restarting(down_for=6)
    _script(monkeypatch, api)
    with caplog.at_level("DEBUG", logger="nymeria.mcp_auth"):
        resp = _held_call(_client())
    assert resp.status_code == 200
    assert resp.json()["identity"] == {"user_id": "bob", "role": "user"}
    assert api.calls == 7
    # The documented backoff: quick first re-checks, then every 2 s.
    assert held.sleeps == [0.25, 0.5, 1.0, 2.0, 2.0, 2.0]
    # One warning for the outage (the first attempt); the re-checks are debug.
    outage = [r for r in caplog.records if "backend unavailable" in r.getMessage()]
    assert [r.levelname for r in outage] == ["WARNING"] + ["DEBUG"] * 5
    assert any(r.levelname == "INFO" and "held, not refused" in r.getMessage() for r in caplog.records)


def test_a_backend_down_past_the_hold_answers_503_at_the_deadline(held, monkeypatch):
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "5")
    api = _Restarting(down_for=10_000)
    _script(monkeypatch, api)
    _assert_retry_later(_held_call(_client()))
    assert sum(held.sleeps) == pytest.approx(5.0)
    assert api.calls > 2
    # Every attempt's HTTP timeout fits the hold, the first one included, so
    # a blackholed API cannot stretch it by a whole 10 s client timeout.
    assert all(t <= 5.0 for t in api.timeouts)


def test_a_zero_hold_answers_503_after_one_attempt(held, monkeypatch):
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "0")
    api = _Restarting(down_for=10_000)
    _script(monkeypatch, api)
    _assert_retry_later(_held_call(_client()))
    assert api.calls == 1
    assert api.timeouts == [mcp_auth._RESOLVE_TIMEOUT]
    assert held.sleeps == []


@pytest.mark.parametrize(
    ("reply", "status"),
    [((401, {"detail": "no"}), 401), ((404, {"detail": "no"}), 502)],
)
def test_a_rejection_or_a_foreign_answer_is_never_held(held, monkeypatch, reply, status):
    api = _Restarting(down_for=0, reply=reply)
    _script(monkeypatch, api)
    assert _held_call(_client()).status_code == status
    assert api.calls == 1
    assert held.sleeps == []


def test_a_token_rejected_once_the_backend_is_back_ends_the_hold(held, monkeypatch):
    """A revoked token held through a restart gets its 401 the moment the API
    answers, not more polling."""
    api = _Restarting(down_for=2, reply=(401, {"detail": "revoked"}))
    _script(monkeypatch, api)
    resp = _held_call(_client())
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert api.calls == 3
    assert held.sleeps == [0.25, 0.5]  # no waiting after the answer


@pytest.mark.parametrize(("method", "token"), [("POST", "junk-token"), ("DELETE", NYM)])
def test_junk_tokens_and_deletes_are_not_held(held, monkeypatch, method, token):
    """A bearer that cannot be a Nymeria token buys no hold, and a DELETE
    (always 405 on this stateless server) is not worth delaying."""
    api = _Restarting(down_for=10_000)
    _script(monkeypatch, api)
    _assert_retry_later(_held_call(_client(), method=method, token=token))
    assert api.calls == 1
    assert held.sleeps == []


def test_holds_are_capped(held, monkeypatch):
    api = _Restarting(down_for=10_000)
    _script(monkeypatch, api)
    monkeypatch.setattr(mcp_auth, "_held_requests", mcp_auth._MAX_HELD_REQUESTS)
    _assert_retry_later(_held_call(_client()))
    assert api.calls == 1
    assert held.sleeps == []
    assert mcp_auth._held_requests == mcp_auth._MAX_HELD_REQUESTS


def test_the_hold_count_is_released(held, monkeypatch):
    _script(monkeypatch, _Restarting(down_for=3))
    assert _held_call(_client()).status_code == 200
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "2")
    mcp_auth._parse_backend_wait.cache_clear()
    _script(monkeypatch, _Restarting(down_for=10_000))
    _assert_retry_later(_held_call(_client(), token=NYM + "-2"))
    assert mcp_auth._held_requests == 0


def test_a_held_request_takes_a_token_another_request_resolved(held, monkeypatch):
    api = _Restarting(down_for=10_000)
    _script(monkeypatch, api)

    def another_request_resolved(sleeps_so_far: int) -> None:
        if sleeps_so_far == 2:
            token_hash = mcp_auth.hashlib.sha256(NYM.encode()).hexdigest()
            mcp_auth._cache_put(token_hash, {"user_id": "bob", "role": "user"})

    held.on_sleep = another_request_resolved
    resp = _held_call(_client())
    assert resp.status_code == 200
    assert api.calls == 2  # the first attempt and one re-check; then the cache answered


def test_every_resolution_reuses_one_tls_context(backend, monkeypatch):
    """Building a client without one loads the CA bundle on the event loop
    (~35-100 ms per attempt), which concurrent holds multiplied."""
    seen = []
    real = httpx.AsyncClient

    def recording(*args, **kwargs):
        seen.append(kwargs.get("verify"))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording)
    backend.reply = 503
    client = _client()
    _call(client)
    _call(client)
    assert len(seen) == 2
    assert isinstance(seen[0], mcp_auth.ssl.SSLContext)
    assert seen[0] is seen[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", mcp_auth._BACKEND_WAIT_DEFAULT),
        ("abc", mcp_auth._BACKEND_WAIT_DEFAULT),
        ("nan", mcp_auth._BACKEND_WAIT_DEFAULT),
        ("-3", 0.0),
        ("7.5", 7.5),
        ("9999", mcp_auth._BACKEND_WAIT_MAX),
    ],
)
def test_the_hold_setting_is_clamped_and_defaults_on_nonsense(monkeypatch, raw, expected):
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", raw)
    assert mcp_auth._backend_wait_seconds() == expected


def test_a_bad_hold_setting_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "twenty")
    with caplog.at_level("WARNING", logger="nymeria.mcp_auth"):
        for _ in range(3):
            mcp_auth._backend_wait_seconds()
    assert len([r for r in caplog.records if "not a number" in r.getMessage()]) == 1


def test_the_default_hold_stays_inside_a_30_s_client_connect_budget():
    assert 17 < mcp_auth._BACKEND_WAIT_DEFAULT < 30
