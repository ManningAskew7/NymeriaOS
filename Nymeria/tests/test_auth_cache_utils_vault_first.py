"""Tests for the vault-first OAuth cache resolver in ``auth_cache_utils``.

Covers the bridge between vault ``kind="oauth_token"`` credentials minted by
the ``request_credential`` flow and the legacy file/legacy_cache reader path
that ``get_google_credentials`` and ``outlook_email.get_access_token`` rely
on. The resolver merges both sources and routes refreshes back to the
correct store.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture
def vault_setup(tmp_path, monkeypatch):
    """Spin up an isolated vault + settings sandbox for one test."""
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

    # Point settings.data_dir at the tmp path so legacy file caches and the
    # vault DB share a sandbox.
    from nymeria.config import settings as settings_module
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NYMERIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")

    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")

    # Force the vault repo singleton to point at our test DB.
    from nymeria.core import credential_vault as vault_module
    monkeypatch.setattr(vault_module, "_repo_instance", None, raising=False)
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(vault_module, "get_credential_vault_repo", lambda db_path=None: repo)

    yield repo, tmp_path

    settings_module.get_settings.cache_clear()


def _mint_vault_oauth_token(
    repo: CredentialVaultRepo,
    *,
    user_id: str,
    provider: str,
    account_id: str,
    email: str,
    access_token: str = "vault-access-tok",
    refresh_token: str = "vault-refresh-tok",
    expires_at: str | None = None,
    scopes: list[str] | None = None,
    client_id: str | None = "vault-client-id",
    token_uri: str | None = None,
):
    """Create an active ``oauth_token`` credential mirroring finalize_oauth_credential."""
    metadata = {
        "provider_id": provider,
        "account_id": account_id,
        "email": email,
        "name": "Vault User",
        "scopes": scopes or ["scope-a"],
        "expires_at": expires_at
        or datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(timespec="seconds"),
        "token_uri": token_uri
        or (
            "https://oauth2.googleapis.com/token"
            if provider.startswith("google")
            else "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        ),
        "source": "oauth_callback",
    }
    if client_id:
        metadata["client_id"] = client_id
    return repo.create_credential(
        owner_type="user",
        owner_user_id=user_id,
        name=email,
        provider=provider,
        kind="oauth_token",
        account_label=email,
        metadata=metadata,
        scopes=scopes or ["scope-a"],
        allowed_targets=["native_tool:*"],
        expires_at=metadata["expires_at"],
        secret_fields={"access_token": access_token, "refresh_token": refresh_token},
        created_by_user_id=user_id,
    )


def test_resolve_oauth_cache_returns_vault_account_when_only_vault_has_entries(vault_setup):
    repo, _ = vault_setup
    _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="google_calendar",
        account_id="alice_at_example_com",
        email="alice@example.com",
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")
    accounts = source.cache.get("accounts", {})

    assert source.has_vault_accounts is True
    assert source.has_legacy_accounts is False
    assert "alice_at_example_com" in accounts
    acct = accounts["alice_at_example_com"]
    assert acct["email"] == "alice@example.com"
    assert acct["access_token"] == "vault-access-tok"
    assert acct["refresh_token"] == "vault-refresh-tok"
    assert acct["_vault_credential_id"].startswith("cred_")
    # client_id from vault metadata
    assert acct["client_id"] == "vault-client-id"


def test_resolve_oauth_cache_returns_legacy_account_when_only_legacy_has_entries(vault_setup):
    _, _ = vault_setup
    from nymeria.tools.auth_cache_utils import resolve_oauth_cache, save_token_cache

    save_token_cache(
        "alice",
        "google_calendar.json",
        {
            "accounts": {
                "legacy_acct": {
                    "email": "legacy@example.com",
                    "access_token": "legacy-tok",
                    "refresh_token": "legacy-refresh",
                    "expires_at": time.time() + 3600,
                    "scopes": ["scope-a"],
                }
            }
        },
    )

    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")
    accounts = source.cache.get("accounts", {})

    assert source.has_vault_accounts is False
    assert source.has_legacy_accounts is True
    assert "legacy_acct" in accounts
    assert accounts["legacy_acct"]["email"] == "legacy@example.com"
    assert "_vault_credential_id" not in accounts["legacy_acct"]


def test_resolve_oauth_cache_vault_wins_on_account_id_collision(vault_setup):
    repo, _ = vault_setup
    _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="outlook",
        account_id="alice_at_example_com",
        email="alice@example.com",
        access_token="VAULT-TOK",
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache, save_token_cache
    save_token_cache(
        "alice",
        "microsoft.json",
        {
            "accounts": {
                "alice_at_example_com": {
                    "email": "alice@example.com",
                    "access_token": "LEGACY-TOK",
                    "refresh_token": "legacy-refresh",
                    "expires_at": time.time() + 3600,
                }
            }
        },
    )

    source = resolve_oauth_cache("alice", "outlook", cache_filename="microsoft.json")
    accounts = source.cache.get("accounts", {})

    assert source.has_vault_accounts is True
    assert source.has_legacy_accounts is True
    acct = accounts["alice_at_example_com"]
    assert acct["access_token"] == "VAULT-TOK", "vault must override legacy on collision"
    assert acct["_vault_credential_id"].startswith("cred_")


def test_resolve_oauth_cache_keeps_legacy_accounts_not_present_in_vault(vault_setup):
    repo, _ = vault_setup
    _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="google_calendar",
        account_id="vault_acct",
        email="vault@example.com",
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache, save_token_cache
    save_token_cache(
        "alice",
        "google_calendar.json",
        {
            "accounts": {
                "legacy_only_acct": {
                    "email": "legacy@example.com",
                    "access_token": "legacy-tok",
                    "refresh_token": "legacy-refresh",
                    "expires_at": time.time() + 3600,
                }
            }
        },
    )

    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")
    accounts = source.cache.get("accounts", {})

    assert set(accounts.keys()) == {"vault_acct", "legacy_only_acct"}
    assert accounts["vault_acct"]["_vault_credential_id"].startswith("cred_")
    assert "_vault_credential_id" not in accounts["legacy_only_acct"]


def test_resolve_oauth_cache_ignores_other_kinds_and_providers(vault_setup):
    repo, _ = vault_setup
    # Wrong kind: must be ignored
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="API Key",
        provider="google_calendar",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"value": "should-not-appear"},
    )
    # Wrong provider: must be ignored
    _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="outlook",
        account_id="other_acct",
        email="other@example.com",
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache
    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")

    assert source.cache.get("accounts", {}) == {}
    assert source.has_vault_accounts is False


def test_resolve_oauth_cache_ignores_disabled_credentials(vault_setup):
    repo, _ = vault_setup
    cred = _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="google_calendar",
        account_id="alice_acct",
        email="alice@example.com",
    )
    # Disable the credential, resolver must not surface it
    repo.disable_credential(cred.id, actor_user_id="alice")

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache
    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")

    assert source.cache.get("accounts", {}) == {}


def test_persist_routes_refreshed_vault_account_back_to_vault(vault_setup):
    repo, _ = vault_setup
    cred = _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="google_calendar",
        account_id="alice_acct",
        email="alice@example.com",
        access_token="OLD-TOK",
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache
    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")
    account = source.cache["accounts"]["alice_acct"]

    # Simulate a successful refresh
    account["access_token"] = "REFRESHED-TOK"
    account["expires_at"] = time.time() + 7200
    source.persist(source.cache)

    new_token = repo.get_secret_field(
        cred.id,
        "access_token",
        actor="alice",
        target_type="native_tool",
        target_id="google_calendar",
    )
    assert new_token == "REFRESHED-TOK"


def test_persist_routes_refreshed_legacy_account_back_to_legacy(vault_setup):
    _, _ = vault_setup
    from nymeria.tools.auth_cache_utils import (
        load_token_cache,
        resolve_oauth_cache,
        save_token_cache,
    )

    save_token_cache(
        "alice",
        "microsoft.json",
        {
            "accounts": {
                "alice_acct": {
                    "email": "alice@example.com",
                    "access_token": "OLD",
                    "refresh_token": "old-refresh",
                    "expires_at": time.time() + 3600,
                }
            }
        },
    )

    source = resolve_oauth_cache("alice", "outlook", cache_filename="microsoft.json")
    account = source.cache["accounts"]["alice_acct"]
    account["access_token"] = "REFRESHED-LEGACY"
    source.persist(source.cache)

    reloaded = load_token_cache("alice", "microsoft.json")
    assert reloaded["accounts"]["alice_acct"]["access_token"] == "REFRESHED-LEGACY"


def test_persist_separates_vault_and_legacy_when_both_present(vault_setup):
    repo, _ = vault_setup
    cred = _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="outlook",
        account_id="vault_acct",
        email="vault@example.com",
        access_token="VAULT-OLD",
    )

    from nymeria.tools.auth_cache_utils import (
        load_token_cache,
        resolve_oauth_cache,
        save_token_cache,
    )

    save_token_cache(
        "alice",
        "microsoft.json",
        {
            "accounts": {
                "legacy_acct": {
                    "email": "legacy@example.com",
                    "access_token": "LEGACY-OLD",
                    "refresh_token": "legacy-refresh",
                    "expires_at": time.time() + 3600,
                }
            }
        },
    )

    source = resolve_oauth_cache("alice", "outlook", cache_filename="microsoft.json")
    source.cache["accounts"]["vault_acct"]["access_token"] = "VAULT-NEW"
    source.cache["accounts"]["legacy_acct"]["access_token"] = "LEGACY-NEW"
    source.persist(source.cache)

    # Vault row got the vault-tagged update
    new_vault_token = repo.get_secret_field(
        cred.id,
        "access_token",
        actor="alice",
        target_type="native_tool",
        target_id="outlook",
    )
    assert new_vault_token == "VAULT-NEW"

    # Legacy file got only the legacy-tagged update, not the vault one
    legacy_reloaded = load_token_cache("alice", "microsoft.json")
    assert legacy_reloaded["accounts"] == {
        "legacy_acct": {
            "email": "legacy@example.com",
            "access_token": "LEGACY-NEW",
            "refresh_token": "legacy-refresh",
            "expires_at": legacy_reloaded["accounts"]["legacy_acct"]["expires_at"],
        }
    }
    # Vault credential id sentinel must not leak into the legacy file
    assert "vault_acct" not in legacy_reloaded["accounts"]


def test_iso_to_epoch_seconds_handles_zulu_and_offset():
    from nymeria.tools.auth_cache_utils import _epoch_seconds_to_iso, _iso_to_epoch_seconds

    epoch = 1716220000.0
    iso = _epoch_seconds_to_iso(epoch)
    assert iso.endswith("+00:00")
    assert abs(_iso_to_epoch_seconds(iso) - epoch) < 1.0
    # Zulu-suffix variant
    assert abs(_iso_to_epoch_seconds(iso.replace("+00:00", "Z")) - epoch) < 1.0
    # Pass-through for numeric input
    assert _iso_to_epoch_seconds(epoch) == epoch
    # Fallbacks
    assert _iso_to_epoch_seconds(None) == 0.0
    assert _iso_to_epoch_seconds("not-an-iso") == 0.0


def test_resolve_provider_client_id_falls_back_to_outlook_default(monkeypatch):
    from nymeria.tools.auth_cache_utils import _resolve_provider_client_id

    monkeypatch.delenv("MICROSOFT_MCP_CLIENT_ID", raising=False)
    assert _resolve_provider_client_id("outlook", None) == "8ad36cab-9646-40ee-97f5-0ddd7cd6e5c8"
    assert _resolve_provider_client_id("outlook", "explicit-id") == "explicit-id"
    monkeypatch.setenv("MICROSOFT_MCP_CLIENT_ID", "env-id")
    assert _resolve_provider_client_id("outlook", None) == "env-id"
    # Unknown provider returns None when no override is given
    assert _resolve_provider_client_id("unknown_provider", None) is None


# --- the OAuth token endpoint is not attacker-supplied (E10-02) -------------
#
# `token_uri` decides where the grant proof is POSTed (the operator's OAuth
# `client_secret` on the Google path), so it is a destination, and neither source
# of an account dict is trustworthy for one. Vault METADATA is written by
# `POST /credentials`, which is gated by `verify_api_key` alone and takes
# arbitrary provider/secret_fields/metadata, so any authenticated caller can
# plant a row over REST with no agent, no admin and no file write. The LEGACY
# half is no safer: it now lives in the vault too (kind `legacy_token_cache`),
# and `PATCH /credentials/{id}` lets its owner rewrite `cache_json` wholesale
# over the same REST surface.
#
# The endpoint now comes from `config/oauth_providers.py` instead. Two of these
# assert EGRESS, meaning the address the credential object would actually be
# refreshed against, because a green "no leak" proves nothing if the fixture
# never built a credential in the first place. The first asserts the merged
# account dict instead: no consumer reads that key any more, so what it pins is
# that the vault loader does not launder caller metadata into a field the GUI
# and any future reader would take at face value.

_ATTACKER_TOKEN_URI = "https://attacker.example/token"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def test_planted_vault_metadata_cannot_move_the_token_endpoint(vault_setup):
    """The metadata column is caller-writable, so its `token_uri` is ignored."""
    repo, _ = vault_setup
    _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="google_calendar",
        account_id="alice_at_example_com",
        email="alice@example.com",
        token_uri=_ATTACKER_TOKEN_URI,
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")
    account = source.cache["accounts"]["alice_at_example_com"]

    assert account["token_uri"] == _GOOGLE_TOKEN_URI


def test_a_refresh_write_back_scrubs_a_planted_endpoint_from_the_record(vault_setup):
    """The stored record stops advertising an address that is not in use.

    Ignoring the planted value at the read sites leaves it sitting in the
    credential row, and `GET /credentials` returns metadata verbatim, so the
    GUI would keep presenting it as this account's token endpoint. Rewriting it
    from the registry on every write-back makes the planted artifact inert
    rather than merely unread.
    """
    repo, _ = vault_setup
    cred = _mint_vault_oauth_token(
        repo,
        user_id="alice",
        provider="google_calendar",
        account_id="alice_acct",
        email="alice@example.com",
        token_uri=_ATTACKER_TOKEN_URI,
    )
    assert repo.get_credential(cred.id).metadata["token_uri"] == _ATTACKER_TOKEN_URI, (
        "the fixture never planted anything, so this would prove nothing"
    )

    from nymeria.tools.auth_cache_utils import resolve_oauth_cache

    source = resolve_oauth_cache("alice", "google_calendar", cache_filename="google_calendar.json")
    account = source.cache["accounts"]["alice_acct"]
    account["access_token"] = "REFRESHED-TOK"
    account["expires_at"] = time.time() + 7200
    source.persist(source.cache)

    assert repo.get_credential(cred.id).metadata["token_uri"] == _GOOGLE_TOKEN_URI


def test_a_planted_account_dict_cannot_move_the_refresh_endpoint(monkeypatch):
    """The consumption point, which is what covers the LEGACY file too.

    Gating only the vault loader would leave the file half of the merged cache
    open, so `refresh_google_account` ignores whatever `token_uri` the account
    dict carries regardless of which store it came from.
    """
    seen: dict = {}

    class _FakeCredentials:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def refresh(self, _request):
            raise RuntimeError("stop here; the constructor is what is under test")

    import google.oauth2.credentials as google_creds

    monkeypatch.setattr(google_creds, "Credentials", _FakeCredentials)

    from nymeria.tools.auth_cache_utils import refresh_google_account

    refresh_google_account(
        {
            "access_token": "tok",
            "refresh_token": "refresh-tok",
            "client_id": "cid",
            "client_secret": "OPERATOR-GOOGLE-SECRET",
            "token_uri": _ATTACKER_TOKEN_URI,
            "scopes": ["scope-a"],
        },
        ["scope-a"],
        provider="google_calendar",
    )

    assert seen, "the credential object was never built, so this proved nothing"
    assert seen["token_uri"] == _GOOGLE_TOKEN_URI
    assert _ATTACKER_TOKEN_URI not in seen.values()


def test_a_planted_legacy_cache_cannot_move_the_endpoint_the_api_refreshes_against(
    vault_setup, monkeypatch
):
    """The third consumption point, and the only one the other two do not cover.

    ``get_google_credentials`` hands its ``Credentials`` object to
    ``googleapiclient.discovery.build``, which refreshes on its own schedule
    (any 401, or immediately when ``access_token`` is absent), so the address
    baked into the object is a live egress even though this call never posts
    anything itself. The account here comes from the LEGACY half of the merged
    cache, whose ``cache_json`` its owner rewrites over
    ``PATCH /credentials/{id}``; the vault half is already covered above, and
    gating only that half would have left this open.
    """
    _, _ = vault_setup
    seen: dict = {}

    class _FakeCredentials:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    import google.oauth2.credentials as google_creds

    monkeypatch.setattr(google_creds, "Credentials", _FakeCredentials)

    from nymeria.tools.auth_cache_utils import get_google_credentials, save_token_cache

    save_token_cache(
        "alice",
        "google_calendar.json",
        {
            "accounts": {
                "legacy_acct": {
                    "email": "legacy@example.com",
                    "access_token": "legacy-tok",
                    "refresh_token": "legacy-refresh",
                    "client_id": "cid",
                    "client_secret": "OPERATOR-GOOGLE-SECRET",
                    "token_uri": _ATTACKER_TOKEN_URI,
                    # Future, so the eager refresh is skipped and the planted
                    # address survives all the way into the returned object.
                    "expires_at": time.time() + 3600,
                    "scopes": ["scope-a"],
                }
            }
        },
    )

    creds = get_google_credentials(
        "alice", "google_calendar", ["scope-a"], cache_filename="google_calendar.json"
    )

    assert creds is not None, "no credential was built, so this proved nothing"
    assert seen["token_uri"] == _GOOGLE_TOKEN_URI
    assert _ATTACKER_TOKEN_URI not in seen.values()
