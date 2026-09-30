"""Public MCP: any host name for an authenticated caller, and the caller's own
token on the backend calls (#430, #427).

These drive the REAL app `create_mcp_asgi_app` / `run_http` build (the MCP
SDK's streamable-HTTP transport behind `MCPAuthMiddleware`) with a live
session manager, so a Host refusal from either layer shows up as it would
on the wire. Only the `/me` resolution and the backend API are faked.

- #430: the SDK's DNS-rebinding guard switched itself on at import and
  answered 421 to every Host but a port-carrying localhost, so Caddy's
  `https://<host>/mcp`, a Tailscale name, or a Cloudflare tunnel never
  reached the server. The bearer gate is what refuses a rebinding page;
  the unauthenticated override alone keeps a loopback-only Host/Origin check.
- #427: tool calls rode the admin service token plus Act-As, so a non-admin
  resolved as an admin relay (`via_act_as`), which the API reads as "a bot
  vouched for this shared channel". The caller's own token goes instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import httpx
import pytest

import nymeria.mcp_auth as mcp_auth
import nymeria.mcp_backend_client as backend_client
from nymeria import mcp_server

SERVICE = "nym_service-token"
USER_TOKEN = "nym_user-token"
ADMIN_TOKEN = "nym_admin-token"
IDENTITIES = {
    USER_TOKEN: {"user_id": "bob", "role": "user"},
    ADMIN_TOKEN: {"user_id": "manning", "role": "admin"},
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


@pytest.fixture(autouse=True)
def _fresh_mcp(monkeypatch):
    """A fresh session manager per test (the SDK allows one run per manager),
    no real /me, and the module state restored afterwards."""
    server = mcp_server.mcp
    saved = (
        server._session_manager,
        server.settings.streamable_http_path,
        server.settings.host,
        server.settings.port,
        mcp_server._backend_url_override,
        mcp_server._service_token_override,
        mcp_server._client,
        mcp_server._http_gated,
    )
    server._session_manager = None
    monkeypatch.delenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("NYMERIA_MCP_BACKEND_WAIT_SECONDS", "0")
    mcp_auth._parse_backend_wait.cache_clear()

    async def fake_resolve(_base_url, token, **_kwargs):
        return IDENTITIES.get(token)

    monkeypatch.setattr(mcp_auth, "_resolve_token", fake_resolve)
    yield
    (
        server._session_manager,
        server.settings.streamable_http_path,
        server.settings.host,
        server.settings.port,
        mcp_server._backend_url_override,
        mcp_server._service_token_override,
        mcp_server._client,
        mcp_server._http_gated,
    ) = saved
    mcp_auth._parse_backend_wait.cache_clear()


def _embedded_app():
    """The slim shape: the app mounted at /mcp inside the API process."""
    return mcp_server.create_mcp_asgi_app(api_url="http://api.test", service_token=SERVICE), "/"


def _standalone_app(monkeypatch):
    """The Docker shape: what `run_http` hands uvicorn."""
    import uvicorn

    served: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kw: served.setdefault("app", app))
    mcp_server.run_http(host="0.0.0.0", port=8001, api_url="http://api.test")
    # run_http re-configures the backend and clears any token override; set it
    # after, or the client would read a real token file from the checkout.
    monkeypatch.setattr(mcp_server, "_service_token_override", SERVICE)
    return served["app"], "/mcp"


def _post(app, path: str, host: str, body: dict, *, token: Optional[str] = None,
          origin: Optional[str] = None) -> list[httpx.Response]:
    # Host set as a header, not through base_url, so a value httpx would
    # parse (userinfo, a bad port) reaches the server byte for byte.
    headers = {**MCP_HEADERS, "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin

    async def run() -> list[httpx.Response]:
        async with mcp_server.mcp.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
            ) as client:
                return [await client.post(path, headers=headers, json=body)]

    return asyncio.run(run())


def _initialized(response: httpx.Response) -> bool:
    return response.status_code == 200 and '"protocolVersion"' in response.text


# -- #430: an authenticated caller reaches the server under any name ---------


PUBLIC_NAMES = [
    "nymeria.example.com",  # Caddy on 443/80: no port in Host
    "nymeria.tail1234.ts.net",  # Tailscale serve
    "localhost",  # portless loopback, which the SDK's guard refused too
    "127.0.0.1:8010",  # the slim loopback, served before and after
]


@pytest.mark.parametrize("host", PUBLIC_NAMES)
def test_an_authenticated_caller_reaches_the_embedded_server_under_any_name(host):
    app, path = _embedded_app()
    [response] = _post(app, path, host, INITIALIZE, token=USER_TOKEN)
    assert _initialized(response), (response.status_code, response.text)


@pytest.mark.parametrize("host", ["nymeria.example.com", "localhost"])
def test_an_authenticated_caller_reaches_the_standalone_server_under_any_name(
    monkeypatch, host
):
    app, path = _standalone_app(monkeypatch)
    [response] = _post(app, path, host, INITIALIZE, token=USER_TOKEN)
    assert _initialized(response), (response.status_code, response.text)


@pytest.mark.parametrize("token", [None, "nym_unknown"])
def test_a_public_name_still_needs_a_valid_bearer(token):
    app, path = _embedded_app()
    [response] = _post(app, path, "nymeria.example.com", INITIALIZE, token=token)
    assert response.status_code == 401
    assert "protocolVersion" not in response.text


# -- #430: the unauthenticated override answers on loopback only -------------


LOOPBACK_HOSTS = [
    "127.0.0.1:8001", "localhost", "localhost:8001", "[::1]:8001", "[::1]", "LOCALHOST:8001",
]
FOREIGN_HOSTS = [
    "nymeria.example.com",
    "localhost.evil.example",
    "evil.example@localhost",
    "localhost:notaport",
    "127.0.0.1.nip.io:8001",
    "local\thost:8001",  # urlsplit would drop the tab and read localhost
    "",
]


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
def test_the_unauthenticated_override_serves_loopback_hosts(monkeypatch, host):
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    app, path = _embedded_app()
    [response] = _post(app, path, host, INITIALIZE)
    assert _initialized(response), (response.status_code, response.text)


@pytest.mark.parametrize("host", FOREIGN_HOSTS)
def test_the_unauthenticated_override_refuses_any_other_host(monkeypatch, host):
    """A DNS-rebinding page arrives under its own name: refused, not served."""
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    app, path = _embedded_app()
    [response] = _post(app, path, host, INITIALIZE)
    assert response.status_code == 421
    assert response.json()["type"] == "misdirected"


def test_the_unauthenticated_override_refuses_a_missing_host(monkeypatch):
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    app, _path = _embedded_app()
    scope_headers = [(b"content-type", b"application/json")]
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": scope_headers,
             "query_string": b"", "root_path": ""}
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 421


@pytest.mark.parametrize(
    "header",
    [
        ("X-Forwarded-For", "203.0.113.9"),
        ("Forwarded", "for=203.0.113.9"),
        ("X-Forwarded-Host", "nymeria.example.com"),
        ("X-Real-IP", "203.0.113.9"),
    ],
)
def test_the_unauthenticated_override_refuses_a_proxied_request(monkeypatch, header):
    """Caddy's no-domain `:80` site passes any Host through, so a remote
    client could name `localhost`; the proxy's forwarding header gives it away."""
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    app, path = _standalone_app(monkeypatch)
    name, value = header

    async def run() -> httpx.Response:
        async with mcp_server.mcp.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
            ) as client:
                return await client.post(
                    path, json=INITIALIZE,
                    headers={**MCP_HEADERS, "Host": "localhost", name: value},
                )

    response = asyncio.run(run())
    assert response.status_code == 421
    assert "protocolVersion" not in response.text


def test_the_unauthenticated_override_on_a_public_name_refuses_even_a_valid_bearer(monkeypatch):
    """The override never publishes the server, whatever the caller sends."""
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    app, path = _standalone_app(monkeypatch)
    [response] = _post(app, path, "nymeria.example.com", INITIALIZE, token=ADMIN_TOKEN)
    assert response.status_code == 421


@pytest.mark.parametrize(
    "origin, served",
    [
        ("http://localhost:6274", True),  # a local inspector
        ("http://127.0.0.1", True),
        ("https://localhost", True),
        ("http://[::1]:3000", True),
        ("https://evil.example", False),
        ("http://localhost.evil.example", False),
        ("http://localhost/path", False),  # not an origin
        ("null", False),
    ],
)
def test_the_unauthenticated_override_refuses_cross_site_origins(monkeypatch, origin, served):
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    app, path = _embedded_app()
    [response] = _post(app, path, "127.0.0.1:8001", INITIALIZE, origin=origin)
    if served:
        assert _initialized(response), (response.status_code, response.text)
    else:
        assert response.status_code == 403
        assert response.json()["type"] == "forbidden"


# -- #427: the backend sees the caller's own token ---------------------------


def _record_backend(monkeypatch, status: int = 200) -> list[httpx.Request]:
    """Answer every backend API call and record it. This patches
    `httpx.AsyncClient` process-wide for the test (the backend client shares
    the module); the test's own ASGI client names its transport, so it is
    left alone."""
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"detail": "Invalid API key"})
        if request.url.path == "/chat":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"type": "done", "thread_id": "discord_1_2"}\n\n',
            )
        return httpx.Response(200, json={"thread_id": "t", "processing": False})

    def client(*args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(answer)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(backend_client.httpx, "AsyncClient", client)
    return seen


def _call_tool(app, path: str, host: str, token: Optional[str], name: str, arguments: dict):
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    [response] = _post(app, path, host, body, token=token)
    assert response.status_code == 200, response.text
    return response


def _auth(request: httpx.Request) -> tuple[str, Optional[str]]:
    return request.headers["authorization"], request.headers.get("x-nymeria-act-as")


def test_a_non_admin_tool_call_carries_the_callers_own_token(monkeypatch):
    """The shared-channel arm of the API admits `via_act_as`; a non-admin's
    own token never resolves that way (`test_api_act_as_resolution.py`, #350),
    so `discord_<g>_<c>` is refused for them exactly as for a direct REST
    caller (`test_command_thread_gate.py`, shared-channel relay vs direct)."""
    seen = _record_backend(monkeypatch)
    app, path = _embedded_app()
    _call_tool(
        app, path, "nymeria.example.com", USER_TOKEN,
        "nymeria_turn_status", {"thread_id": "discord_1_2", "user_id": "manning"},
    )
    assert [r.url.path for r in seen] == ["/threads/discord_1_2/status"]
    authorization, act_as = _auth(seen[0])
    assert authorization == f"Bearer {USER_TOKEN}"
    # Pinned to self (the user_id argument is ignored); the API treats a
    # non-admin naming its own id as no Act-As at all (#350).
    assert act_as == "bob"
    assert SERVICE not in json.dumps(dict(seen[0].headers))


def test_an_admin_keeps_act_as_on_its_own_token(monkeypatch):
    seen = _record_backend(monkeypatch)
    app, path = _standalone_app(monkeypatch)
    _call_tool(
        app, path, "localhost:8001", ADMIN_TOKEN,
        "nymeria_turn_status", {"thread_id": "t1", "user_id": "claude-test"},
    )
    assert _auth(seen[0]) == (f"Bearer {ADMIN_TOKEN}", "claude-test")


def test_an_admin_without_user_id_acts_as_itself_on_its_own_token(monkeypatch):
    seen = _record_backend(monkeypatch)
    app, path = _embedded_app()
    _call_tool(app, path, "nymeria.example.com", ADMIN_TOKEN, "nymeria_turn_status", {"thread_id": "t1"})
    assert _auth(seen[0]) == (f"Bearer {ADMIN_TOKEN}", "manning")


def test_without_an_inbound_caller_the_service_token_is_used(monkeypatch):
    """STDIO and the unauthenticated override have no caller to forward."""
    monkeypatch.setenv("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "1")
    seen = _record_backend(monkeypatch)
    app, path = _embedded_app()
    _call_tool(
        app, path, "127.0.0.1:8001", None,
        "nymeria_turn_status", {"thread_id": "t1", "user_id": "alice"},
    )
    assert _auth(seen[0]) == (f"Bearer {SERVICE}", "alice")


def test_concurrent_callers_each_forward_their_own_token(monkeypatch):
    seen = _record_backend(monkeypatch)
    app, path = _embedded_app()

    async def slow_resolve(_base_url, token, **_kwargs):
        await asyncio.sleep(0.01)  # interleave the two requests
        return IDENTITIES.get(token)

    monkeypatch.setattr(mcp_auth, "_resolve_token", slow_resolve)

    def body(thread_id: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "nymeria_turn_status", "arguments": {"thread_id": thread_id}},
        }

    async def run() -> None:
        async with mcp_server.mcp.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://nymeria.example.com"
            ) as client:
                responses = await asyncio.gather(*(
                    client.post(
                        path,
                        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
                        json=body(thread_id),
                    )
                    for token, thread_id in [(USER_TOKEN, "bobs"), (ADMIN_TOKEN, "admins")] * 3
                ))
        assert all(r.status_code == 200 for r in responses)

    asyncio.run(run())
    by_thread = {r.url.path.split("/")[2]: set() for r in seen}
    for request in seen:
        by_thread[request.url.path.split("/")[2]].add(request.headers["authorization"])
    assert by_thread == {"bobs": {f"Bearer {USER_TOKEN}"}, "admins": {f"Bearer {ADMIN_TOKEN}"}}


@pytest.mark.parametrize("shape", ["embedded", "standalone"])
def test_a_gated_server_refuses_a_backend_call_that_lost_its_caller(monkeypatch, shape):
    """Behind the HTTP gate every backend call has a caller; one without
    (a lost request context) fails instead of running as the service account."""
    seen = _record_backend(monkeypatch)
    if shape == "embedded":
        _embedded_app()
    else:
        _standalone_app(monkeypatch)

    async def lost_context_call():
        return await mcp_server._get_client().get("/threads/discord_1_2/status", act_as="alice")

    with pytest.raises(backend_client.NymeriaAPIError) as refused:
        asyncio.run(lost_context_call())
    assert refused.value.status_code == 500
    assert seen == []


def test_stdio_mode_still_uses_the_service_token(monkeypatch):
    """A local STDIO launch has no HTTP gate and no inbound caller."""
    seen = _record_backend(monkeypatch)
    _embedded_app()  # a gated build earlier in the same process
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: None)
    mcp_server.run_stdio(api_url="http://api.test")
    monkeypatch.setattr(mcp_server, "_service_token_override", SERVICE)

    asyncio.run(mcp_server._get_client().get("/threads/t1/status", act_as="alice"))
    assert _auth(seen[0]) == (f"Bearer {SERVICE}", "alice")


def test_a_chat_turn_streams_with_the_callers_own_token(monkeypatch):
    """#427's real target: chat on a shared-channel thread. The stream runs in
    a background task (`_consume`), which must still carry the caller's token."""
    seen = _record_backend(monkeypatch)
    app, path = _embedded_app()
    response = _call_tool(
        app, path, "nymeria.example.com", USER_TOKEN,
        "nymeria_chat", {"message": "hi", "thread_id": "discord_1_2", "wait_seconds": 5},
    )
    chats = [r for r in seen if r.url.path == "/chat"]
    assert len(chats) == 1, response.text
    assert _auth(chats[0]) == (f"Bearer {USER_TOKEN}", "bob")


def test_a_refused_forwarded_token_is_forgotten(monkeypatch):
    """A token revoked inside the auth cache's TTL: the backend's 401 drops the
    cached resolution, so the next request re-resolves it (and a dead token is
    then challenged at the gate) instead of riding the cache into more
    per-tool 401s."""
    resolutions: list[str] = []

    async def cached_resolve(_base_url, token, **_kwargs):
        # The real cache, with only the network resolution faked.
        token_hash = mcp_auth._token_hash(token)
        hit, identity = mcp_auth._cache_get(token_hash)
        if not hit:
            resolutions.append(token)
            identity = IDENTITIES.get(token)
            mcp_auth._cache_put(token_hash, identity)
        return identity

    monkeypatch.setattr(mcp_auth, "_RESOLVE_CACHE", {})
    monkeypatch.setattr(mcp_auth, "_resolve_token", cached_resolve)
    # Another caller's live resolution must survive the eviction.
    mcp_auth._cache_put(mcp_auth._token_hash(ADMIN_TOKEN), IDENTITIES[ADMIN_TOKEN])
    seen = _record_backend(monkeypatch, status=401)
    for _attempt in range(2):
        mcp_server.mcp._session_manager = None  # one run per manager
        app, path = _embedded_app()
        _call_tool(app, path, "nymeria.example.com", USER_TOKEN,
                   "nymeria_turn_status", {"thread_id": "t1"})
    assert len(seen) == 2
    assert resolutions == [USER_TOKEN, USER_TOKEN]  # not served from the cache
    assert mcp_auth._cache_get(mcp_auth._token_hash(ADMIN_TOKEN)) == (True, IDENTITIES[ADMIN_TOKEN])
