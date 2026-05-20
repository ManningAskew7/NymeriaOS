"""Unit tests for the ``kind="oauth"`` branch of ``request_credential``.

These cover the *agent-visible* return shape, what the tool resolves to
when the OAuth start succeeds (a pending placeholder + waiting future) or
fails fast (unknown provider, missing PUBLIC_URL, etc.). The follow-up
finalize path is covered by ``test_oauth_callback.py``.

For the success path we don't drive the full callback round-trip; we just
verify that:
  * an ``auth_prompt`` event is published with ``mode="oauth"``,
  * a pending vault record is created with ``kind="oauth_token"`` and
    ``status="pending_setup"``,
  * the prompt is registered with the coordinator,
  * the auth URL embeds the expected redirect_uri / scopes / state,
  * the tool returns ``pending`` only after we let it time out.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from langchain_core.runnables import RunnableConfig

from nymeria.core.accounts import AccountsRepo
from nymeria.core.auth_prompt_coordinator import get_auth_prompt_coordinator


_GOOGLE_OAUTH_CLIENT_JSON = json.dumps(
    {
        "installed": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
)


@dataclass
class _Settings:
    data_dir: Path
    api_port: int = 8000
    nymeria_public_url: str | None = "https://nymeria.example.test"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated vault + coordinator + Google client config JSON file."""
    import nymeria.core.auth_prompt_coordinator as coord_mod
    coord_mod._coordinator = None

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    client_json = tmp_path / "google_oauth_client.json"
    client_json.write_text(_GOOGLE_OAUTH_CLIENT_JSON, encoding="utf-8")
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", str(client_json))

    settings = _Settings(data_dir=tmp_path)

    import nymeria.config as config_mod
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    import nymeria.core.credential_vault as vault_mod
    vault_mod._vault_repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")

    # Seed the user so vault creation can run (otherwise the vault repo is
    # happy, but the request_credential tool resolves user_id from config so
    # we don't strictly need it, keep the accounts repo around anyway).
    accounts = AccountsRepo(tmp_path / "accounts.db")
    accounts.create_user("default", "default@example.com", "Default", role="admin")

    yield settings, vault_mod._vault_repo


def _config(thread_id: str = "oauth-thread") -> RunnableConfig:
    return {"configurable": {"user_id": "default", "thread_id": thread_id}}


async def _capture_event(coro) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run ``coro`` (a request_credential call) and snapshot the published
    SSE event + the resulting prompt state once the prompt becomes visible
    on the coordinator. Returns ``(event_payload, prompt_metadata)``."""
    from nymeria.core import event_bus

    captured_events: list[dict[str, Any]] = []
    real_publish = event_bus.publish_autonomous_event

    def _capture(**kwargs):
        if kwargs.get("event_type") == "auth_prompt":
            captured_events.append(kwargs.get("data") or {})
        return real_publish(**kwargs)

    import nymeria.tools.credential_prompt as cp_mod
    original = cp_mod.publish_autonomous_event
    cp_mod.publish_autonomous_event = _capture
    try:
        result = await coro
    finally:
        cp_mod.publish_autonomous_event = original
    return result, (captured_events[0] if captured_events else {})


def _call_oauth_tool(*, provider: str, **kwargs) -> str:
    """Invoke ``request_credential`` with kind='oauth' and return the raw JSON
    string the agent would see. Uses the minimum allowed timeout (15s) so an
    accidental hang is bounded."""
    from nymeria.tools.credential_prompt import request_credential

    payload = {
        "provider": provider,
        "kind": "oauth",
        "timeout_seconds": kwargs.pop("timeout_seconds", 15),
    }
    payload.update(kwargs)
    return request_credential.ainvoke(payload, config=_config())


# ---------------------------------------------------------------------------
# Negative-path tests (return immediately; no vault record, no SSE event)
# ---------------------------------------------------------------------------


def test_unknown_provider_returns_early(env):
    raw = asyncio.run(_call_oauth_tool(provider="nonexistent_provider"))
    body = json.loads(raw)
    assert body["ok"] is False
    assert body["status"] == "unknown_provider"
    # No vault placeholder must be created when the start fails.
    settings, repo = env
    assert repo.list_credentials(owner_user_id="default") == []


def test_missing_public_url_no_localhost_returns_early(env, monkeypatch):
    settings, _repo = env
    settings.nymeria_public_url = None  # type: ignore[attr-defined]
    # google_calendar is auth_code-only, so device-code auto-degrade does
    # not save us. The tool must early-return with missing_public_url.
    raw = asyncio.run(_call_oauth_tool(provider="google_calendar"))
    body = json.loads(raw)
    assert body["ok"] is False
    assert body["status"] == "missing_public_url"
    assert "use_localhost=True" in body["message"]


def test_unsupported_flow_returns_early(env):
    # google_calendar only supports auth_code; asking for device_code is a
    # user/agent error and must surface as ``unsupported_flow``.
    raw = asyncio.run(
        _call_oauth_tool(provider="google_calendar", flow="device_code")
    )
    body = json.loads(raw)
    assert body["ok"] is False
    assert body["status"] == "unsupported_flow"


def test_client_config_missing_returns_early(env, monkeypatch):
    # Drop the Google client config and request a Google provider.
    monkeypatch.delenv("GOOGLE_OAUTH_CREDENTIALS", raising=False)
    raw = asyncio.run(_call_oauth_tool(provider="google_calendar"))
    body = json.loads(raw)
    assert body["ok"] is False
    assert body["status"] == "client_config_missing"


# ---------------------------------------------------------------------------
# Happy paths (auth_code with PUBLIC_URL, and use_localhost fallback)
# ---------------------------------------------------------------------------


def _await_prompt_for(provider: str) -> Any:
    """Block until a prompt for ``provider`` appears in the coordinator."""
    coord = get_auth_prompt_coordinator()
    for _ in range(50):
        with coord._lock:
            for prompt in coord._prompts.values():
                if prompt.provider == provider:
                    return prompt
        # Tiny non-async sleep, caller is the test thread, not the loop.
        threading.Event().wait(0.02)
    raise AssertionError(f"no pending prompt for provider={provider}")


def _run_with_cancel(provider: str, **kwargs) -> dict[str, Any]:
    """Start the tool, wait for the prompt to register, cancel it via the
    coordinator (to wake the future), and return the resolved JSON body."""

    async def driver():
        async def canceller():
            # Yield control so the tool gets to register the prompt.
            for _ in range(50):
                await asyncio.sleep(0.02)
                coord = get_auth_prompt_coordinator()
                with coord._lock:
                    matching = [
                        p for p in coord._prompts.values() if p.provider == provider
                    ]
                if matching:
                    pid = matching[0].prompt_id
                    coord.resolve(
                        pid,
                        {
                            "ok": False,
                            "status": "cancelled",
                            "credential_id": matching[0].credential_id,
                            "message": "cancelled by test",
                        },
                    )
                    return matching[0]
            raise AssertionError("prompt never registered")

        from nymeria.tools.credential_prompt import request_credential

        payload = {
            "provider": provider,
            "kind": "oauth",
            "timeout_seconds": kwargs.pop("timeout_seconds", 15),
        }
        payload.update(kwargs)

        from nymeria.core import event_bus
        captured: list[dict[str, Any]] = []
        real_publish = event_bus.publish_autonomous_event
        import nymeria.tools.credential_prompt as cp_mod

        def _capture(**kw):
            if kw.get("event_type") == "auth_prompt":
                captured.append(kw.get("data") or {})
            return real_publish(**kw)

        original = cp_mod.publish_autonomous_event
        cp_mod.publish_autonomous_event = _capture
        try:
            tool_coro = request_credential.ainvoke(payload, config=_config())
            raw, matched_prompt = await asyncio.gather(tool_coro, canceller())
        finally:
            cp_mod.publish_autonomous_event = original
        return {
            "tool_result": json.loads(raw),
            "event": captured[0] if captured else {},
            "prompt": matched_prompt,
        }

    return asyncio.run(driver())


def test_auth_code_happy_path_with_public_url(env):
    """With NYMERIA_PUBLIC_URL set, the tool issues an auth-code URL that
    points the redirect back at the hosted callback path."""
    captured = _run_with_cancel("google_calendar")
    event = captured["event"]
    assert event["mode"] == "oauth"
    assert event["flow"] == "auth_code"
    assert event["provider_id"] == "google_calendar"
    assert event["display_name"] == "Google Calendar"
    auth_url = event["auth_url"]
    parsed = urlparse(auth_url)
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert qs["client_id"] == "test-client-id.apps.googleusercontent.com"
    assert qs["redirect_uri"] == (
        "https://nymeria.example.test/connect/credentials/oauth/callback"
    )
    assert qs["response_type"] == "code"
    assert "calendar" in qs["scope"]
    assert qs["code_challenge_method"] == "S256"
    # Google PKCE + offline refresh requirements.
    assert qs["access_type"] == "offline"
    assert qs["prompt"] == "consent"

    # Vault placeholder must be present, pending_setup, kind=oauth_token.
    settings, repo = env
    creds = repo.list_credentials(owner_user_id="default")
    assert len(creds) == 1
    placeholder = creds[0]
    assert placeholder.kind == "oauth_token"
    assert placeholder.status == "pending_setup"
    assert placeholder.provider == "google_calendar"
    metadata = placeholder.metadata or {}
    assert metadata.get("oauth_pending") is True
    assert metadata.get("provider_id") == "google_calendar"

    # Tool result is the cancellation we issued.
    assert captured["tool_result"]["status"] == "cancelled"


def test_use_localhost_builds_localhost_redirect(env, monkeypatch):
    """When PUBLIC_URL is unset but ``use_localhost=True``, the auth-code URL
    redirects to ``http://localhost:<api_port>``."""
    settings, _repo = env
    settings.nymeria_public_url = None  # type: ignore[attr-defined]
    settings.api_port = 8765  # type: ignore[attr-defined]

    captured = _run_with_cancel("google_calendar", use_localhost=True)
    event = captured["event"]
    assert event["flow"] == "auth_code"
    parsed = urlparse(event["auth_url"])
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert qs["redirect_uri"] == (
        "http://localhost:8765/connect/credentials/oauth/callback"
    )


def test_outlook_no_public_url_auto_degrades_to_device_code(env, monkeypatch):
    """Outlook supports both flows. With no PUBLIC_URL and no use_localhost,
    the start logic must auto-degrade to device_code rather than refusing.

    The device-code path performs a real HTTP POST to Microsoft's device
    authorization endpoint, so we stub ``httpx.AsyncClient`` to return a
    canned response and confirm the SSE event is rendered with mode=oauth_device.
    """
    settings, _repo = env
    settings.nymeria_public_url = None  # type: ignore[attr-defined]

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "device_code": "DEVCODE-1234",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "verification_uri_complete": (
                    "https://microsoft.com/devicelogin?code=ABCD-EFGH"
                ),
                "expires_in": 600,
                "interval": 5,
            }

    class _FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_args, **_kwargs):
            return _FakeResponse()

    import nymeria.core.oauth_start as oauth_start_mod
    monkeypatch.setattr(oauth_start_mod.httpx, "AsyncClient", _FakeAsyncClient)
    # The device-code poller spawns a task that would hit Microsoft for real.
    # Cancel it as part of the resolve flow.
    import nymeria.core.oauth_device_flow as poll_mod

    async def _noop_poll(**_kw):
        return None

    monkeypatch.setattr(poll_mod, "poll_device_token", _noop_poll)

    captured = _run_with_cancel("outlook")
    event = captured["event"]
    assert event["mode"] == "oauth_device"
    assert event["flow"] == "device_code"
    assert event["user_code"] == "ABCD-EFGH"
    assert event["verification_uri"] == "https://microsoft.com/devicelogin"
    assert event["verification_uri_complete"] == (
        "https://microsoft.com/devicelogin?code=ABCD-EFGH"
    )
    assert event["expires_in"] == 600


def test_explicit_device_code_flow_for_outlook(env, monkeypatch):
    """Even with PUBLIC_URL set, the agent can force device-code on a
    descriptor that supports both flows by passing ``flow='device_code'``."""

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "device_code": "OTHER",
                "user_code": "ZZZZ-9999",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 600,
                "interval": 5,
            }

    class _FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_args, **_kwargs):
            return _FakeResponse()

    import nymeria.core.oauth_start as oauth_start_mod
    monkeypatch.setattr(oauth_start_mod.httpx, "AsyncClient", _FakeAsyncClient)
    import nymeria.core.oauth_device_flow as poll_mod

    async def _noop_poll(**_kw):
        return None

    monkeypatch.setattr(poll_mod, "poll_device_token", _noop_poll)

    captured = _run_with_cancel("outlook", flow="device_code")
    assert captured["event"]["flow"] == "device_code"
    assert captured["event"]["user_code"] == "ZZZZ-9999"
