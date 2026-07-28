"""CLIProxyManagementClient behavior against a scripted httpx.MockTransport.

Covers the error taxonomy (unreachable / auth / unsupported), the OAuth
session shapes, the no-retry-on-401 guarantee (the proxy IP-bans after 5
consecutive auth failures, so a bad key must fail fast and the probe must
short-circuit), and the tool_prefix_disabled fixup paths (fields PATCH on
current binaries, download -> inject -> re-upload on older ones).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from nymeria.cliproxy import management_client
from nymeria.cliproxy.catalog import CLIPROXY_PROVIDERS, get_cliproxy_provider
from nymeria.cliproxy.management_client import (
    CLIProxyAuthError,
    CLIProxyConflict,
    CLIProxyManagementClient,
    CLIProxyManagementError,
    CLIProxyNotFound,
    CLIProxyUnreachable,
    CLIProxyUnsupported,
    confirm_login_landed,
)

BASE = "http://proxy.test:8317"


@pytest.fixture(autouse=True)
def _clear_oauth_session_ledger():
    management_client._oauth_session_ledger.clear()
    yield
    management_client._oauth_session_ledger.clear()


class Recorder:
    """MockTransport handler that records requests and scripts responses."""

    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def make_client(respond) -> tuple[CLIProxyManagementClient, Recorder]:
    recorder = Recorder(respond)
    client = CLIProxyManagementClient(
        BASE, "cpm-test-secret", transport=httpx.MockTransport(recorder)
    )
    return client, recorder


@pytest.mark.asyncio
async def test_start_oauth_returns_url_and_state_and_sends_bearer():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/management/anthropic-auth-url"
        assert request.headers["Authorization"] == "Bearer cpm-test-secret"
        return httpx.Response(
            200, json={"status": "ok", "url": "https://claude.ai/x", "state": "s1"}
        )

    client, _ = make_client(respond)
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    result = await client.start_oauth(spec)
    assert result == {"url": "https://claude.ai/x", "state": "s1"}


@pytest.mark.asyncio
async def test_start_oauth_maps_404_to_unsupported():
    client, _ = make_client(lambda request: httpx.Response(404, text="404 page not found"))
    spec = get_cliproxy_provider("grok")
    assert spec is not None
    with pytest.raises(CLIProxyUnsupported):
        await client.start_oauth(spec)


@pytest.mark.asyncio
async def test_probe_marks_404_unsupported_and_caches():
    supported_endpoints = {"anthropic", "codex"}

    def respond(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/")[-1].removesuffix("-auth-url")
        if endpoint in supported_endpoints:
            return httpx.Response(200, json={"url": "u", "state": "s"})
        return httpx.Response(404, text="404 page not found")

    client, recorder = make_client(respond)
    probed = await client.probe_providers()
    assert probed["claude"] is True
    assert probed["codex"] is True
    assert probed["grok"] is False
    assert probed["kimi"] is False
    first_count = len(recorder.requests)
    assert first_count == len(CLIPROXY_PROVIDERS)

    # Cached: a second call makes no further requests.
    again = await client.probe_providers()
    assert again == probed
    assert len(recorder.requests) == first_count


@pytest.mark.asyncio
async def test_auth_error_short_circuits_probe_and_is_not_retried():
    client, recorder = make_client(
        lambda request: httpx.Response(401, json={"error": "unauthorized"})
    )
    with pytest.raises(CLIProxyAuthError):
        await client.probe_providers()
    # One request only: never burn the proxy's 5-failure ban budget.
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_connect_error_maps_to_unreachable():
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, _ = make_client(respond)
    with pytest.raises(CLIProxyUnreachable):
        await client.auth_status("s1")


@pytest.mark.asyncio
async def test_oauth_callback_sends_normalizer_provider_and_redirect_url():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/management/oauth-callback"
        body = json.loads(request.content)
        assert body == {
            "provider": "xai",
            "redirect_url": "http://127.0.0.1:56121/callback?code=c&state=s",
        }
        return httpx.Response(200, json={"status": "ok"})

    client, recorder = make_client(respond)
    spec = get_cliproxy_provider("grok")
    assert spec is not None
    await client.oauth_callback(
        spec, redirect_url="http://127.0.0.1:56121/callback?code=c&state=s"
    )
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_oauth_callback_rejected_for_device_flow():
    client, recorder = make_client(lambda request: httpx.Response(200, json={}))
    spec = get_cliproxy_provider("kimi")
    assert spec is not None
    with pytest.raises(CLIProxyConflict):
        await client.oauth_callback(spec, redirect_url="http://x/callback")
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_auth_status_unwraps_status_field():
    client, _ = make_client(
        lambda request: httpx.Response(200, json={"status": "wait"})
    )
    assert await client.auth_status("s1") == "wait"


@pytest.mark.asyncio
async def test_list_auth_files_accepts_both_response_shapes():
    files = [{"name": "claude-a.json", "provider": "claude"}]
    client, _ = make_client(lambda request: httpx.Response(200, json={"files": files}))
    assert await client.list_auth_files() == files
    client, _ = make_client(lambda request: httpx.Response(200, json=files))
    assert await client.list_auth_files() == files


@pytest.mark.asyncio
async def test_ensure_tool_prefix_disabled_prefers_fields_patch():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/v0/management/auth-files/fields"
        body = json.loads(request.content)
        assert body == {"name": "claude-a.json", "tool_prefix_disabled": True}
        return httpx.Response(200, json={"status": "ok"})

    client, recorder = make_client(respond)
    await client.ensure_tool_prefix_disabled("claude-a.json")
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_ensure_tool_prefix_disabled_falls_back_to_reupload():
    auth_json = {"type": "claude", "access_token": "redacted"}

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v0/management/auth-files/fields":
            return httpx.Response(404, json={"error": "not found"})
        if path == "/v0/management/auth-files/download":
            return httpx.Response(200, content=json.dumps(auth_json).encode())
        if path == "/v0/management/auth-files" and request.method == "POST":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client, recorder = make_client(respond)
    await client.ensure_tool_prefix_disabled("claude-a.json")

    upload = recorder.requests[-1]
    assert upload.method == "POST"
    assert b'"tool_prefix_disabled": true' in upload.content


@pytest.mark.asyncio
async def test_config_knobs_skip_routes_the_binary_does_not_serve():
    def respond(request: httpx.Request) -> httpx.Response:
        # Live v7 binaries wrap each knob in {<last path segment>: value}.
        if request.url.path.endswith("/request-retry"):
            return httpx.Response(200, json={"request-retry": 3})
        if request.url.path.endswith("/api-keys"):
            return httpx.Response(200, json={"api-keys": ["cpx-a"]})
        if request.url.path.endswith("/strategy"):
            return httpx.Response(200, json={"strategy": "round-robin"})
        return httpx.Response(404, text="404 page not found")

    client, _ = make_client(respond)
    knobs = await client.get_config_knobs()
    assert knobs["request-retry"] == 3
    assert knobs["api-keys"] == ["cpx-a"]
    assert knobs["routing/strategy"] == "round-robin"
    assert "oauth-model-alias" not in knobs


@pytest.mark.asyncio
async def test_set_config_knob_wraps_by_kind_and_rejects_unknown():
    bodies: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        bodies[request.url.path] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    client, _ = make_client(respond)
    await client.set_config_knob("request-retry", 5)
    await client.set_config_knob("api-keys", ["cpx-a", "cpx-b"])
    await client.set_config_knob("oauth-excluded-models", {"claude": ["m1"]})
    assert bodies["/v0/management/request-retry"] == {"value": 5}
    assert bodies["/v0/management/api-keys"] == {"items": ["cpx-a", "cpx-b"]}
    assert bodies["/v0/management/oauth-excluded-models"] == {"claude": ["m1"]}
    with pytest.raises(ValueError):
        await client.set_config_knob("debug", True)


@pytest.mark.asyncio
async def test_generic_http_error_carries_proxy_message():
    client, _ = make_client(
        lambda request: httpx.Response(500, json={"error": "disk full"})
    )
    with pytest.raises(CLIProxyManagementError, match="disk full"):
        await client.auth_status("s1")


@pytest.mark.asyncio
async def test_not_found_on_named_resource_maps_to_not_found():
    client, _ = make_client(
        lambda request: httpx.Response(404, json={"error": "auth file not found"})
    )
    with pytest.raises(CLIProxyNotFound):
        await client.delete_auth_file("missing.json")

@pytest.mark.asyncio
async def test_resolve_or_mint_gatekeeper_reuses_first_configured_key():
    from nymeria.cliproxy.management_client import resolve_or_mint_gatekeeper

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v0/management/api-keys"
        return httpx.Response(200, json={"api-keys": ["cpx-first", "cpx-second"]})

    client, recorder = make_client(respond)
    assert await resolve_or_mint_gatekeeper(client) == "cpx-first"
    assert len(recorder.requests) == 1  # read only, no write


@pytest.mark.asyncio
async def test_resolve_or_mint_gatekeeper_mints_when_proxy_has_none():
    from nymeria.cliproxy.management_client import resolve_or_mint_gatekeeper

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"api-keys": []})
        assert request.method == "PUT"
        assert request.url.path == "/v0/management/api-keys"
        return httpx.Response(200, json={"status": "ok"})

    client, recorder = make_client(respond)
    minted = await resolve_or_mint_gatekeeper(client)
    assert minted.startswith("cpx-nymeria-")
    put = recorder.requests[-1]
    assert json.loads(put.content) == {"items": [minted]}


# ── OAuth session ledger + the stale-session guard ──────────────────────────
#
# The relogin trap: the proxy answers ok for a session it no longer knows
# (expired, ~10 min TTL), and confirm-on-ok then blesses a PRE-EXISTING auth
# file. The ledger is stamped inside start_oauth/oauth_callback so every
# surface (wizard, headless, REST routes, command facade) shares the one
# predicate in confirm_login_landed. Advisory + fail-open by design.

CLAUDE_AUTH_FILE = {
    "name": "claude-alice.json",
    "provider": "claude",
    "account": "alice@example.com",
}


def _confirm_responder(status: str = "ok"):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0/management/get-auth-status":
            return httpx.Response(200, json={"status": status})
        if request.url.path == "/v0/management/auth-files":
            return httpx.Response(200, json=[dict(CLAUDE_AUTH_FILE)])
        if request.url.path.endswith("-auth-url"):
            return httpx.Response(200, json={"url": "https://x", "state": "s1"})
        if request.url.path == "/v0/management/oauth-callback":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected path {request.url.path}")

    return respond


def _backdate_session(state: str, age_seconds: float) -> None:
    started, delivered = management_client._oauth_session_ledger[state]
    management_client._oauth_session_ledger[state] = (
        time.monotonic() - age_seconds,
        delivered,
    )


@pytest.mark.asyncio
async def test_confirm_ok_trusted_within_the_session_window():
    client, _ = make_client(_confirm_responder())
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    await client.start_oauth(spec)
    status, detail = await confirm_login_landed(client, "s1", spec)
    assert status == "ok"
    assert detail == "alice@example.com"


@pytest.mark.asyncio
async def test_confirm_ok_refused_for_old_pasteless_session():
    client, _ = make_client(_confirm_responder())
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    await client.start_oauth(spec)
    _backdate_session("s1", management_client.SESSION_OK_GUARD_SECONDS + 60)

    status, detail = await confirm_login_landed(client, "s1", spec)
    assert status == "error"
    assert "stale-session" in detail
    assert "Restart the login" in detail


@pytest.mark.asyncio
async def test_confirm_ok_trusted_after_delivered_callback_even_when_old():
    """A callback the proxy ACCEPTED proves the session was alive, so an
    old ok stays trustworthy (a delivery to a dead session errors)."""
    client, _ = make_client(_confirm_responder())
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    await client.start_oauth(spec)
    await client.oauth_callback(
        spec, redirect_url="http://127.0.0.1:54545/callback?code=c&state=s1"
    )
    _backdate_session("s1", management_client.SESSION_OK_GUARD_SECONDS + 60)

    status, detail = await confirm_login_landed(client, "s1", spec)
    assert status == "ok"
    assert detail == "alice@example.com"


@pytest.mark.asyncio
async def test_confirm_ok_fails_open_for_a_session_this_process_never_saw():
    """No stamp = no guard (e.g. an API restart mid-login): the guard is
    advisory, so an unknown state falls through to the auth-file confirm
    instead of refusing a legitimate long poll."""
    client, _ = make_client(_confirm_responder())
    spec = get_cliproxy_provider("claude")
    assert spec is not None

    status, detail = await confirm_login_landed(client, "unknown-state", spec)
    assert status == "ok"
    assert detail == "alice@example.com"


@pytest.mark.asyncio
async def test_oauth_callback_with_explicit_state_marks_delivery():
    client, _ = make_client(_confirm_responder())
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    await client.start_oauth(spec)
    await client.oauth_callback(spec, code="c", state="s1")
    assert management_client._oauth_session_ledger["s1"][1] is True


@pytest.mark.asyncio
async def test_ledger_prunes_expired_sessions_on_stamp():
    client, _ = make_client(_confirm_responder())
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    management_client._oauth_session_ledger["old"] = (
        time.monotonic() - management_client._LEDGER_TTL_SECONDS - 1,
        False,
    )
    await client.start_oauth(spec)
    assert "old" not in management_client._oauth_session_ledger
    assert "s1" in management_client._oauth_session_ledger
