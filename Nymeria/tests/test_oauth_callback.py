"""Unit tests for the OAuth auth-code callback handler.

``handle_auth_code_callback`` is the function the static
``/connect/credentials/oauth/callback`` endpoint delegates to. Once a
provider redirects the user's browser back, this handler:

1. Verifies the ``state`` parameter against the hash stashed on the
   pending prompt (defence against CSRF / replayed callbacks).
2. Exchanges the authorization code for tokens at the provider's token
   endpoint.
3. Promotes the placeholder vault credential to ``active`` via
   ``finalize_oauth_credential``.
4. Resolves the coordinator future so the prompt done-callback can run.
5. Renders a success/failure HTML page (the caller does the rendering;
   we just return the title/message/status).

These tests stub the provider HTTP exchanges so they run offline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from nymeria.config.oauth_providers import OAUTH_PROVIDERS
from nymeria.core.auth_prompt_coordinator import (
    get_auth_prompt_coordinator,
    hash_prompt_token,
    new_prompt_id,
    new_prompt_token,
)
from nymeria.core.oauth_callback_handler import handle_auth_code_callback
from nymeria.core.oauth_start import _pack_state


@dataclass
class _Settings:
    data_dir: Path
    api_port: int = 8000
    nymeria_public_url: str | None = "https://nymeria.example.test"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh coordinator + isolated vault + secret key for each test."""
    import nymeria.core.auth_prompt_coordinator as coord_mod
    coord_mod._coordinator = None

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

    settings = _Settings(data_dir=tmp_path)
    import nymeria.config as config_mod
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    # Seed the users table before the vault repo so the credentials FK works.
    from nymeria.core.accounts import AccountsRepo
    accounts = AccountsRepo(tmp_path / "accounts.db")
    accounts.create_user("default", "default@example.com", "Default", role="admin")
    import nymeria.core.credential_vault as vault_mod
    vault_mod._vault_repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")
    yield settings, vault_mod._vault_repo


async def _register_pending_prompt(
    *,
    provider: str = "google_calendar",
    prompt_id: str | None = None,
):
    """Create a pending OAuth prompt the way ``request_credential`` would,
    minus the SSE event. Returns ``(prompt, raw_nonce, packed_state)``."""
    coord = get_auth_prompt_coordinator()
    pid = prompt_id or new_prompt_id()

    from nymeria.core.credential_vault import get_credential_vault_repo

    repo = get_credential_vault_repo()
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="default",
        name="Google Calendar (pending)",
        provider=provider,
        kind="oauth_token",
        account_label=None,
        status="pending_setup",
        metadata={"setup_session": True, "prompt_id": pid, "oauth_pending": True},
        created_by_user_id="default",
    )

    coord.register(
        prompt_id=pid,
        credential_id=record.id,
        user_id="default",
        thread_id="oauth-thread",
        provider=provider,
    )

    descriptor = OAUTH_PROVIDERS[provider]
    nonce = new_prompt_token()
    state_hash = hash_prompt_token(nonce)
    state_token = _pack_state(pid, nonce)

    prompt = coord.get(pid)
    assert prompt is not None
    prompt.metadata = {
        "_oauth_state": {
            "provider_id": descriptor.provider_id,
            "flow": "auth_code",
            "redirect_uri": "https://nymeria.example.test/connect/credentials/oauth/callback",
            "client_id": "test-client-id",
            "client_secret": "test-secret",
            "token_uri": descriptor.token_uri,
            "code_verifier": "abc123",
            "scopes": list(descriptor.scopes),
            "state_token_hash": state_hash,
        }
    }
    return prompt, nonce, state_token


# ---------------------------------------------------------------------------
# State / parameter validation
# ---------------------------------------------------------------------------


def test_missing_state_returns_400(env):
    result = asyncio.run(handle_auth_code_callback(code="x", state=None))
    assert result.ok is False
    assert result.status_code == 400
    assert "state" in result.message.lower()


def test_malformed_state_returns_400(env):
    # Missing the prompt_id:nonce separator.
    result = asyncio.run(handle_auth_code_callback(code="x", state="malformed"))
    assert result.ok is False
    assert result.status_code == 400
    assert "malformed" in result.message.lower()


def test_unknown_prompt_id_returns_400(env):
    fake_state = _pack_state("nonexistent-prompt", "anynonce")
    result = asyncio.run(handle_auth_code_callback(code="x", state=fake_state))
    assert result.ok is False
    assert result.status_code == 400
    assert "expired" in result.message.lower() or "used" in result.message.lower()


def test_state_hash_mismatch_returns_400(env):
    async def run():
        prompt, _real_nonce, _packed = await _register_pending_prompt()
        forged_state = _pack_state(prompt.prompt_id, "wrong-nonce")
        return await handle_auth_code_callback(code="x", state=forged_state)

    result = asyncio.run(run())
    assert result.ok is False
    assert result.status_code == 400
    assert "did not match" in result.message.lower()
    # The prompt must still be pending, a state-mismatched callback is
    # treated as a replay attempt and must not poison the future.
    coord = get_auth_prompt_coordinator()
    with coord._lock:
        assert any(p.user_id == "default" for p in coord._prompts.values())


def test_provider_error_param_resolves_as_denied(env):
    """If the provider redirects with ``error=access_denied`` we mark the
    pending prompt as denied so the agent gets a structured failure."""

    async def run():
        prompt, _, state = await _register_pending_prompt()
        return prompt, await handle_auth_code_callback(
            code=None, state=state, error="access_denied"
        )

    prompt, result = asyncio.run(run())
    assert result.ok is False
    assert result.status_code == 400
    assert "access_denied" in result.message
    # ``resolve`` removes the prompt from the coordinator dict, but the
    # ``PendingPrompt`` reference we captured still holds the future.
    assert prompt.future.done()
    payload = prompt.future.result()
    assert payload["ok"] is False
    assert payload["status"] == "denied"


def test_missing_code_resolves_with_error(env):
    """Provider didn't return ``error`` but also didn't return ``code`` , 
    treat as a flow failure, mark prompt as error, don't write the vault."""

    async def run():
        prompt, _, state = await _register_pending_prompt()
        return prompt, await handle_auth_code_callback(code=None, state=state)

    prompt, result = asyncio.run(run())
    assert result.ok is False
    assert result.status_code == 400
    assert prompt.future.done()
    payload = prompt.future.result()
    assert payload["ok"] is False
    assert payload["status"] == "error"


# ---------------------------------------------------------------------------
# Happy path, token exchange + vault write
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        # ``response.text`` is only read on error.
        self.text = ""

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Replace ``httpx.AsyncClient`` so token exchange + userinfo can be
    canned per request. Both auth_code callback and ``_fetch_userinfo`` go
    through this stub."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, *_args, **_kwargs):
        if "oauth2.googleapis.com/token" in url or "token" in url:
            return _FakeResponse(
                payload={
                    "access_token": "ya29.a0_test",
                    "refresh_token": "1//rt_test",
                    "expires_in": 3599,
                    "scope": " ".join(OAUTH_PROVIDERS["google_calendar"].scopes),
                    "token_type": "Bearer",
                }
            )
        return _FakeResponse(payload={})


def _stub_userinfo(monkeypatch, email: str = "user@example.com", name: str = "User One"):
    """Replace ``httpx.get`` (sync) used by ``_fetch_userinfo``."""
    import nymeria.core.oauth_callback_handler as handler_mod

    class _SyncResponse:
        status_code = 200

        def json(self):
            return {"email": email, "name": name, "id": "uid-12345"}

    def _fake_get(*_args, **_kwargs):
        return _SyncResponse()

    monkeypatch.setattr(handler_mod.httpx, "get", _fake_get)


def test_happy_path_promotes_vault_credential(env, monkeypatch):
    """Valid state + code → token exchanged → vault credential goes from
    pending_setup to active with kind=oauth_token + access/refresh secrets."""
    import nymeria.core.oauth_callback_handler as handler_mod
    monkeypatch.setattr(handler_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _stub_userinfo(monkeypatch)

    async def run():
        prompt, _, state = await _register_pending_prompt()
        return prompt, await handle_auth_code_callback(code="auth-code-123", state=state)

    prompt, result = asyncio.run(run())
    assert result.ok is True
    assert result.status_code == 200
    assert "user@example.com" in result.message

    _settings, repo = env
    creds = repo.list_credentials(owner_user_id="default")
    assert len(creds) == 1
    active = creds[0]
    assert active.id == prompt.credential_id
    assert active.status == "active"
    assert active.kind == "oauth_token"
    assert active.provider == "google_calendar"
    metadata = active.metadata or {}
    assert metadata["email"] == "user@example.com"
    assert metadata["provider_id"] == "google_calendar"
    assert "expires_at" in metadata
    # The ephemeral ``_oauth_state`` blob must be cleaned up at finalize
    # time, it contains client_secret + code_verifier and shouldn't linger.
    assert "_oauth_state" not in metadata
    assert metadata.get("oauth_pending") is None
    # Secret fields are encrypted at rest; decrypt and confirm presence via
    # the owner-test accessor (no allowed_targets check on this path).
    secret_fields = repo.get_secret_fields_for_test(
        active.id, actor_user_id="default"
    )
    assert set(secret_fields) >= {"access_token", "refresh_token"}
    assert secret_fields["access_token"] == "ya29.a0_test"
    assert secret_fields["refresh_token"] == "1//rt_test"

    # The waiting future was resolved with status=active.
    payload = prompt.future.result()
    assert payload["ok"] is True
    assert payload["status"] == "active"
    assert payload["credential_id"] == active.id
    assert payload["email"] == "user@example.com"


def test_token_exchange_http_error_marks_prompt_failed(env, monkeypatch):
    """If the provider's token endpoint returns 4xx/5xx, the prompt resolves
    as ``error`` and the vault record stays in pending_setup."""

    class _ErrAsyncClient(_FakeAsyncClient):
        async def post(self, *_args, **_kwargs):
            return _FakeResponse(status_code=400, payload={"error": "invalid_grant"})

    import nymeria.core.oauth_callback_handler as handler_mod
    monkeypatch.setattr(handler_mod.httpx, "AsyncClient", _ErrAsyncClient)

    async def run():
        prompt, _, state = await _register_pending_prompt()
        return prompt, await handle_auth_code_callback(code="bad-code", state=state)

    prompt, result = asyncio.run(run())
    assert result.ok is False
    assert result.status_code == 400
    payload = prompt.future.result()
    assert payload["status"] == "error"
    _settings, repo = env
    creds = repo.list_credentials(owner_user_id="default")
    assert creds[0].status == "pending_setup"


def test_missing_oauth_state_blob_returns_500(env, monkeypatch):
    """If ``_oauth_state`` was somehow not stashed at start time (or was
    wiped), the callback must refuse rather than silently writing tokens
    against an unknown client_id."""
    import nymeria.core.oauth_callback_handler as handler_mod
    monkeypatch.setattr(handler_mod.httpx, "AsyncClient", _FakeAsyncClient)
    _stub_userinfo(monkeypatch)

    async def run():
        prompt, _, state = await _register_pending_prompt()
        # Wipe the OAuth state blob but keep the state_token_hash so we get
        # past the state-match check first.
        oauth_state = prompt.metadata["_oauth_state"]
        prompt.metadata["_oauth_state"] = {
            "state_token_hash": oauth_state["state_token_hash"]
        }
        return await handle_auth_code_callback(code="x", state=state)

    result = asyncio.run(run())
    assert result.ok is False
    assert result.status_code == 500
    assert "incomplete" in result.message.lower()
