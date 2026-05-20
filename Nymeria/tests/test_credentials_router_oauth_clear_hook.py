"""Regression test for the credential-delete OAuth post_clear_hook wiring.

Phase 8 added :func:`_fire_oauth_post_clear_hook` in
``nymeria/api/routers/credentials.py`` so that disconnecting a Gmail
credential (hard delete or disable) also tears down the exported MCP
google-auth credentials file. Without this test, the hook could silently
become dead code again on a future refactor.
"""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.api.routers.credentials import _fire_oauth_post_clear_hook
from nymeria.config.oauth_providers import OAUTH_PROVIDERS


def _fake_record(*, kind: str, provider: str, account_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="cred-abc",
        kind=kind,
        provider=provider,
        owner_user_id="alice",
        metadata={"account_id": account_id} if account_id else {},
    )


def test_fires_for_oauth_token_with_post_clear_hook(monkeypatch):
    calls = []
    descriptor = OAUTH_PROVIDERS["google_gmail"].__class__(
        provider_id="google_gmail",
        display_name="Google Gmail",
        auth_uri="x",
        token_uri="x",
        userinfo_uri="x",
        scopes=(),
        supported_flows=frozenset({"auth_code"}),
        default_flow="auth_code",
        post_clear_hook=lambda user_id, account_id: calls.append((user_id, account_id)) or None,
    )
    monkeypatch.setitem(OAUTH_PROVIDERS, "google_gmail", descriptor)

    _fire_oauth_post_clear_hook(
        _fake_record(kind="oauth_token", provider="google_gmail", account_id="me_at_example_com")
    )

    assert calls == [("alice", "me_at_example_com")]


def test_skips_non_oauth_token_kinds(monkeypatch):
    calls = []
    descriptor = OAUTH_PROVIDERS["google_gmail"].__class__(
        provider_id="google_gmail",
        display_name="Google Gmail",
        auth_uri="x",
        token_uri="x",
        userinfo_uri="x",
        scopes=(),
        supported_flows=frozenset({"auth_code"}),
        default_flow="auth_code",
        post_clear_hook=lambda user_id, account_id: calls.append((user_id, account_id)),
    )
    monkeypatch.setitem(OAUTH_PROVIDERS, "google_gmail", descriptor)

    _fire_oauth_post_clear_hook(
        _fake_record(kind="api_key", provider="google_gmail", account_id="me_at_example_com")
    )

    assert calls == []


def test_skips_provider_without_hook():
    # google_calendar's descriptor has no post_clear_hook by default. Calling
    # the helper must be a no-op, not a crash.
    _fire_oauth_post_clear_hook(
        _fake_record(kind="oauth_token", provider="google_calendar", account_id="acct")
    )


def test_swallows_hook_exception(monkeypatch):
    """A flaky post-clear hook must not surface to the delete endpoint."""
    def boom(_user_id, _account_id):
        raise RuntimeError("boom")

    descriptor = OAUTH_PROVIDERS["google_gmail"].__class__(
        provider_id="google_gmail",
        display_name="Google Gmail",
        auth_uri="x",
        token_uri="x",
        userinfo_uri="x",
        scopes=(),
        supported_flows=frozenset({"auth_code"}),
        default_flow="auth_code",
        post_clear_hook=boom,
    )
    monkeypatch.setitem(OAUTH_PROVIDERS, "google_gmail", descriptor)

    # Must not raise.
    _fire_oauth_post_clear_hook(
        _fake_record(kind="oauth_token", provider="google_gmail", account_id="acct")
    )


def test_gmail_descriptor_clears_exported_mcp_credentials(tmp_path, monkeypatch):
    """End-to-end: the real google_gmail post_clear_hook deletes the exported file."""
    from nymeria.config import settings as settings_module
    from nymeria.core import mcp_auth_bridge

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    target = mcp_auth_bridge.gmail_mcp_credentials_path("alice")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text("{}", encoding="utf-8")
    assert target.exists()

    _fire_oauth_post_clear_hook(
        _fake_record(kind="oauth_token", provider="google_gmail", account_id="acct")
    )

    assert not target.exists()
