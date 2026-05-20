"""Shared OAuth scaffold for the vault-first credential flow.

Tokens are minted by ``request_credential(provider=..., kind="oauth")`` and
stored in the credential vault (kind ``oauth_token``). Legacy per-provider
file caches at ``data/auth_tokens/<user>/<provider>.json`` are still honoured
during the migration window: :func:`resolve_oauth_cache` merges vault rows
with the legacy file (vault wins on account_id collision) and returns a
single dict-shaped view plus a persist callback that routes writes back to
whichever store the account originated from.

The Google credential/request helpers below all read through that resolver,
so once a user re-auths through the new flow their tools keep working without
any per-tool code changes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token cache I/O
# ---------------------------------------------------------------------------


def safe_user_id(user_id: str) -> str:
    safe = "".join(c for c in user_id if c.isalnum() or c in "-_")
    return safe or "default"


def cache_path(user_id: str, cache_filename: str) -> Path:
    """Per-user token cache path under ``data/auth_tokens/<user_id>/``."""
    from ..config import get_settings

    settings = get_settings()
    path = settings.data_dir / "auth_tokens" / safe_user_id(user_id) / cache_filename
    _ensure_private_dir(path.parent)
    return path


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        logger.debug("Failed to chmod private auth-cache directory %s", path)


def _write_private_json(path: Path, data: dict) -> None:
    _ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            logger.debug("Failed to chmod private auth-cache file %s", path)


def load_token_cache(user_id: str, cache_filename: str) -> dict:
    try:
        from ..core.credential_vault import get_credential_vault_repo

        cached = get_credential_vault_repo().load_legacy_cache(user_id, cache_filename)
        if isinstance(cached, dict):
            return cached
    except Exception:
        # Keep legacy reads available when the vault key is not configured,
        # when a migration has not run yet, or during focused unit tests that
        # monkeypatch file-cache helpers.
        logger.debug("Credential vault token-cache load unavailable", exc_info=True)

    path = cache_path(user_id, cache_filename)
    if path.exists():
        try:
            try:
                path.chmod(0o600)
            except OSError:
                logger.debug("Failed to chmod existing token cache file %s", path)
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read token cache file", exc_info=True)
    return {}


def save_token_cache(user_id: str, cache_filename: str, cache: dict) -> None:
    try:
        from ..core.credential_vault import get_credential_vault_repo

        get_credential_vault_repo().upsert_legacy_cache(user_id, cache_filename, cache)
        path = cache_path(user_id, cache_filename)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                logger.debug("Failed to remove migrated token cache file %s", path)
        return
    except Exception:
        logger.debug("Credential vault token-cache save unavailable; using file cache", exc_info=True)

    path = cache_path(user_id, cache_filename)
    _write_private_json(path, cache)


def delete_token_cache(user_id: str, cache_filename: str) -> bool:
    """Delete a user's token cache file if it exists.

    Returns ``True`` when a file was removed, otherwise ``False``. The parent
    directory is still created by ``cache_path``; leaving an empty per-user auth
    directory is harmless and keeps this helper simple.
    """
    removed = False
    try:
        from ..core.credential_vault import get_credential_vault_repo

        removed = get_credential_vault_repo().delete_legacy_cache(user_id, cache_filename)
    except Exception:
        logger.debug("Credential vault token-cache delete unavailable", exc_info=True)

    path = cache_path(user_id, cache_filename)
    if not path.exists():
        return removed
    path.unlink()
    return True


# ---------------------------------------------------------------------------
# Vault-first OAuth cache resolver
# ---------------------------------------------------------------------------
#
# Two storage shapes exist in production:
#   1. Legacy: per-user JSON files under ``data/auth_tokens/<user>/<provider>.json``,
#      mirrored (since the credential vault landed) into a ``legacy_cache`` table
#      keyed by ``(user_id, cache_filename)``. ``load_token_cache`` /
#      ``save_token_cache`` already read/write this path transparently.
#   2. New: ``credentials`` rows of ``kind="oauth_token"`` populated by the
#      ``request_credential`` flow. Tokens land in ``credential_secret_fields``
#      under ``access_token`` / ``refresh_token`` and metadata stores
#      ``account_id``/``email``/``scopes``/``expires_at``/``token_uri``.
#
# The resolver below merges both into the legacy in-memory shape so existing
# consumers (``get_google_credentials``, ``outlook_email.get_access_token``)
# need only small changes. Vault accounts win on ``account_id`` collision,
# and refreshed tokens persist back to whichever store the account came from.


@dataclass
class OAuthCacheSource:
    """Unified read/write handle over vault + legacy OAuth account storage.

    ``cache`` has the legacy shape ``{"accounts": {<account_id>: {...}}}``.
    Vault-backed accounts carry a ``_vault_credential_id`` sentinel used by
    ``persist`` to route writes back to the correct credential row. Legacy
    accounts have no sentinel and route to ``save_token_cache``.
    """

    cache: dict
    persist: Callable[[dict], None]
    has_vault_accounts: bool
    has_legacy_accounts: bool


_VAULT_CRED_ID_KEY = "_vault_credential_id"


def _iso_to_epoch_seconds(value: Any) -> float:
    """Parse an ISO 8601 timestamp into a float epoch. ``0.0`` on failure."""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        # ``fromisoformat`` accepts ``+00:00`` and ``Z`` (Python 3.11+).
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _epoch_seconds_to_iso(value: Any) -> str:
    """Format a float epoch as ISO 8601 UTC. Empty string on falsy input."""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def _resolve_provider_client_id(
    provider: str,
    account_metadata_client_id: Optional[str],
) -> Optional[str]:
    """Pick a ``client_id`` for refresh. Falls back per provider family.

    Existing vault rows minted before the ``client_id`` metadata fix do not
    carry it, so the resolver consults the same env/file sources the legacy
    code path always used.
    """
    if account_metadata_client_id:
        return account_metadata_client_id
    if provider.startswith("google"):
        cfg = _load_google_oauth_client_config()
        cid = cfg.get("client_id") if isinstance(cfg, dict) else None
        return str(cid) if cid else None
    if provider == "outlook":
        # Mirrors outlook_auth.get_client_id() so refresh works in dev setups
        # without MICROSOFT_MCP_CLIENT_ID.
        return os.environ.get("MICROSOFT_MCP_CLIENT_ID") or "8ad36cab-9646-40ee-97f5-0ddd7cd6e5c8"
    return None


def _load_vault_oauth_cache(user_id: str, provider: str) -> dict:
    """Read active vault ``oauth_token`` credentials for ``(user_id, provider)``.

    Returns a legacy-shape ``{"accounts": {...}}`` dict, possibly empty.
    Decryption failures are logged and the affected account is skipped so a
    single bad row doesn't poison the whole read.
    """
    try:
        from ..core.credential_vault import (
            CredentialAccessDenied,
            CredentialSecretUnavailable,
            get_credential_vault_repo,
        )
    except Exception:
        logger.debug("Credential vault import failed", exc_info=True)
        return {}

    try:
        repo = get_credential_vault_repo()
        all_creds = repo.list_credentials(owner_user_id=user_id, include_disabled=False)
    except Exception:
        logger.debug("Vault list_credentials failed for user=%s", user_id, exc_info=True)
        return {}

    accounts: dict[str, dict] = {}
    for cred in all_creds:
        if cred.kind != "oauth_token" or cred.provider != provider or cred.status != "active":
            continue
        meta = cred.metadata or {}
        # Vault credentials are gated by ``allowed_targets`` (e.g. ``native_tool:*``).
        # The resolver is the canonical native-tool reader; identify as such so
        # the access check matches the policy stored on the row.
        secret_kwargs = {
            "actor_user_id": cred.owner_user_id,
            "target_type": "native_tool",
            "target_id": provider,
        }
        try:
            access_token = repo.get_secret_field(cred.id, "access_token", **secret_kwargs)
        except (CredentialSecretUnavailable, CredentialAccessDenied):
            logger.warning(
                "Vault oauth_token %s missing access_token; skipping", cred.id
            )
            continue
        except Exception:
            logger.warning(
                "Vault oauth_token %s decrypt failed; skipping", cred.id, exc_info=True
            )
            continue

        refresh_token = ""
        try:
            refresh_token = repo.get_secret_field(cred.id, "refresh_token", **secret_kwargs)
        except (CredentialSecretUnavailable, CredentialAccessDenied):
            pass  # refresh_token is optional; some providers don't issue one
        except Exception:
            logger.debug(
                "Vault oauth_token %s refresh_token unavailable", cred.id, exc_info=True
            )

        account_id = str(meta.get("account_id") or cred.account_label or cred.id)
        scopes = list(meta.get("scopes") or cred.scopes or [])
        expires_at_epoch = _iso_to_epoch_seconds(meta.get("expires_at") or cred.expires_at)
        client_id = _resolve_provider_client_id(provider, meta.get("client_id"))

        accounts[account_id] = {
            "email": meta.get("email") or cred.account_label or "unknown",
            "name": meta.get("name") or "Unknown User",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at_epoch,
            "scopes": scopes,
            "client_id": client_id,
            "token_uri": meta.get("token_uri")
            or ("https://oauth2.googleapis.com/token" if provider.startswith("google") else ""),
            _VAULT_CRED_ID_KEY: cred.id,
        }

    return {"accounts": accounts} if accounts else {}


def _persist_vault_oauth_account(user_id: str, account_id: str, account: dict) -> None:
    """Update a single vault ``oauth_token`` row from a refreshed account dict.

    Silently skips accounts without a ``_vault_credential_id`` sentinel (i.e.
    legacy-origin accounts persisted alongside vault ones).
    """
    cred_id = account.get(_VAULT_CRED_ID_KEY)
    if not cred_id:
        return
    try:
        from ..core.credential_vault import get_credential_vault_repo
    except Exception:
        logger.debug("Credential vault import failed during persist", exc_info=True)
        return
    try:
        repo = get_credential_vault_repo()
        existing = repo.get_credential(cred_id)
    except Exception:
        logger.warning("Vault get_credential failed for %s", cred_id, exc_info=True)
        return
    if existing is None:
        logger.debug("Vault credential %s vanished before refresh write-back", cred_id)
        return

    metadata = dict(existing.metadata or {})
    expires_iso = _epoch_seconds_to_iso(account.get("expires_at"))
    if expires_iso:
        metadata["expires_at"] = expires_iso
    if account.get("scopes"):
        metadata["scopes"] = list(account["scopes"])
    if account.get("client_id"):
        metadata["client_id"] = str(account["client_id"])

    secret_fields = {"access_token": str(account.get("access_token") or "")}
    if account.get("refresh_token"):
        secret_fields["refresh_token"] = str(account["refresh_token"])

    try:
        repo.upsert_credential(
            credential_id=existing.id,
            owner_type=existing.owner_type,
            owner_user_id=existing.owner_user_id,
            name=existing.name,
            provider=existing.provider,
            kind="oauth_token",
            account_label=existing.account_label,
            metadata=metadata,
            scopes=list(metadata.get("scopes") or existing.scopes or []),
            allowed_targets=existing.allowed_targets or ["native_tool:*"],
            expires_at=expires_iso or existing.expires_at,
            status="active",
            secret_fields=secret_fields,
            actor_user_id=existing.owner_user_id,
        )
    except Exception:
        logger.warning(
            "Vault upsert_credential write-back failed for %s", cred_id, exc_info=True
        )


def _persist_oauth_cache(
    user_id: str,
    cache_filename: str,
    cache: dict,
) -> None:
    """Route each account in ``cache`` back to its origin store.

    Vault-tagged accounts go to ``_persist_vault_oauth_account``; everything
    else falls through to ``save_token_cache`` (which already handles the
    legacy_cache + file fallback). Vault accounts are stripped from the
    legacy payload so we don't double-write or leak the sentinel.
    """
    accounts = cache.get("accounts") or {}
    legacy_accounts: dict[str, dict] = {}
    for account_id, account in accounts.items():
        if _VAULT_CRED_ID_KEY in account:
            _persist_vault_oauth_account(user_id, account_id, account)
        else:
            legacy_accounts[account_id] = account

    # Always persist the legacy half so non-vault accounts (and other top-level
    # keys like ``pending_auth``) survive a refresh. Strip the vault accounts
    # so we don't store ciphertext-derived state in the legacy cache.
    legacy_cache = dict(cache)
    legacy_cache["accounts"] = legacy_accounts
    if not legacy_accounts:
        legacy_cache.pop("accounts", None)
    save_token_cache(user_id, cache_filename, legacy_cache)


def resolve_oauth_cache(
    user_id: str,
    provider: str,
    *,
    cache_filename: Optional[str] = None,
) -> OAuthCacheSource:
    """Return an ``OAuthCacheSource`` merging vault + legacy account storage.

    Read order:
        1. Vault ``oauth_token`` rows for ``(user_id, provider)``.
        2. Legacy file/legacy_cache under ``cache_filename`` (defaults to
           ``<provider>.json``).

    Vault accounts win on ``account_id`` collision. ``persist`` routes each
    account back to its origin: vault rows via ``upsert_credential``, legacy
    rows via ``save_token_cache``.
    """
    filename = _google_cache_filename(provider, cache_filename)
    vault_cache = _load_vault_oauth_cache(user_id, provider)
    legacy_cache = load_token_cache(user_id, filename)

    merged: dict[str, Any] = dict(legacy_cache or {})
    legacy_accounts = dict((legacy_cache or {}).get("accounts") or {})
    vault_accounts = dict(vault_cache.get("accounts") or {})

    # Vault overrides legacy on collision.
    merged_accounts: dict[str, dict] = {}
    merged_accounts.update(legacy_accounts)
    merged_accounts.update(vault_accounts)
    merged["accounts"] = merged_accounts

    def _persist(updated_cache: dict) -> None:
        _persist_oauth_cache(user_id, filename, updated_cache)

    return OAuthCacheSource(
        cache=merged,
        persist=_persist,
        has_vault_accounts=bool(vault_accounts),
        has_legacy_accounts=bool(legacy_accounts),
    )


# ---------------------------------------------------------------------------
# Google-specific helpers (userinfo, token exchange, account save)
# ---------------------------------------------------------------------------


def get_google_credentials_path() -> Optional[str]:
    path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
    if path:
        path = os.path.expanduser(path)
        if os.path.exists(path):
            return path
    return None


def _load_google_oauth_client_config() -> dict[str, Any]:
    creds_path = get_google_credentials_path()
    if not creds_path:
        return {}
    try:
        with open(creds_path, encoding="utf-8") as f:
            client_config = json.load(f)
    except Exception:
        logger.debug("Failed to load Google OAuth credentials file", exc_info=True)
        return {}
    creds_data = client_config.get("installed") or client_config.get("web")
    return creds_data if isinstance(creds_data, dict) else {}


def _resolve_google_client_secret(client_id: Optional[str]) -> Optional[str]:
    if not client_id:
        return None
    creds_data = _load_google_oauth_client_config()
    if creds_data.get("client_id") == client_id:
        secret = creds_data.get("client_secret")
        return str(secret) if secret else None
    return None


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def fetch_google_user_info(access_token: str) -> Tuple[str, str]:
    """Return ``(email, display_name)``. Falls back to placeholders on error."""
    try:
        response = httpx.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.status_code == 200:
            info = response.json()
            return info.get("email", "unknown"), info.get("name", "Unknown User")
    except Exception:
        logger.debug("Failed to fetch Google user info")
    return "unknown", "Unknown User"


def refresh_google_account(account: dict, scopes: list) -> Tuple[str, str]:
    """Refresh a stored Google OAuth account.

    Returns ``("refreshed", "")`` on success, ``("invalid", reason)`` when
    Google rejects the refresh token, or ``("unavailable", reason)`` when the
    local environment cannot validate it safely.
    """
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        return "unavailable", f"google-auth packages are not installed: {e}"

    refresh_token = account.get("refresh_token")
    if not refresh_token:
        return "invalid", "no refresh token stored"

    client_secret = account.get("client_secret") or _resolve_google_client_secret(
        account.get("client_id")
    )
    if not client_secret:
        return "unavailable", "Google OAuth client secret is unavailable"

    creds = Credentials(
        token=account.get("access_token"),
        refresh_token=refresh_token,
        token_uri=account.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=account.get("client_id"),
        client_secret=client_secret,
        scopes=account.get("scopes", list(scopes)),
    )

    try:
        creds.refresh(GoogleAuthRequest())
    except RefreshError as e:
        reason = str(e)
        lowered = reason.lower()
        invalid_markers = (
            "invalid_grant",
            "invalid_client",
            "unauthorized_client",
            "expired or revoked",
            "revoked",
            "deleted",
        )
        if any(marker in lowered for marker in invalid_markers):
            return "invalid", reason
        return "unavailable", reason
    except Exception as e:
        return "unavailable", str(e)

    account["access_token"] = creds.token
    account["expires_at"] = (
        creds.expiry.timestamp() if creds.expiry else time.time() + 3600
    )
    if creds.refresh_token:
        account["refresh_token"] = creds.refresh_token
    account.pop("client_secret", None)
    return "refreshed", ""


def validate_google_accounts_for_display(
    accounts: dict,
    scopes: list,
) -> Tuple[list[dict], bool]:
    """Validate cached Google accounts before presenting them as usable.

    Mutates ``accounts`` in place when a token refresh succeeds or Google
    confirms a refresh token is invalid. Returns ``(rows, changed)`` where
    each row has account_id/account/status/usable/reason keys.
    """
    required_scopes = set(scopes)
    rows: list[dict] = []
    changed = False

    for account_id, account in list(accounts.items()):
        email = account.get("email", "unknown")
        saved_scopes = set(account.get("scopes", []))
        missing_scopes = required_scopes - saved_scopes
        if missing_scopes:
            rows.append({
                "account_id": account_id,
                "account": account,
                "status": "missing required scopes - re-authentication required",
                "usable": False,
                "reason": f"missing scopes: {', '.join(sorted(missing_scopes))}",
            })
            continue

        expires_at = account.get("expires_at", 0)
        expired = time.time() > expires_at - 60
        has_refresh = bool(account.get("refresh_token"))

        if not expired:
            rows.append({
                "account_id": account_id,
                "account": account,
                "status": "active",
                "usable": True,
                "reason": "",
            })
            continue

        if not has_refresh:
            rows.append({
                "account_id": account_id,
                "account": account,
                "status": "expired - re-authentication required",
                "usable": False,
                "reason": "no refresh token stored",
            })
            continue

        refresh_status, reason = refresh_google_account(account, scopes)
        if refresh_status == "refreshed":
            accounts[account_id] = account
            changed = True
            rows.append({
                "account_id": account_id,
                "account": account,
                "status": "active (refreshed)",
                "usable": True,
                "reason": "",
            })
            continue

        if refresh_status == "invalid":
            logger.info("Clearing invalid Google token for %s: %s", email, reason)
            del accounts[account_id]
            changed = True
            continue

        rows.append({
            "account_id": account_id,
            "account": account,
            "status": f"expired - refresh could not be verified: {reason}",
            "usable": False,
            "reason": reason,
        })

    return rows, changed


def _google_cache_filename(provider: str, cache_filename: Optional[str] = None) -> str:
    return cache_filename or f"{provider}.json"


def _google_scopes_are_satisfied(account: dict, scopes: Sequence[str]) -> bool:
    saved_scopes = set(account.get("scopes", []))
    return saved_scopes >= set(scopes)


def get_google_credentials(
    user_id: str,
    provider: str,
    scopes: Sequence[str],
    account_id: Optional[str] = None,
    *,
    cache_filename: Optional[str] = None,
    provider_display_name: Optional[str] = None,
):
    """Return valid Google OAuth credentials for a saved provider account.

    ``provider`` is the cache namespace, for example ``google_calendar`` or
    ``google_docs``. Vault-stored credentials (kind=oauth_token) win on
    account_id collision over the legacy file/legacy_cache path. Refreshes
    write back to whichever store the account originated from.
    """
    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.error(
            "google-auth packages not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )
        return None

    source = resolve_oauth_cache(user_id, provider, cache_filename=cache_filename)
    accounts = source.cache.get("accounts", {})
    if not accounts:
        return None

    if account_id:
        aid = account_id
        account = accounts.get(account_id)
    else:
        aid, account = next(iter(accounts.items()), (None, None))

    if not aid or not account:
        return None

    if not _google_scopes_are_satisfied(account, scopes):
        logger.info(
            "%s token for %s is missing required scopes; re-authentication required.",
            provider_display_name or provider,
            account.get("email", "unknown"),
        )
        return None

    expires_at = account.get("expires_at", 0)
    if time.time() >= expires_at - 60:
        status, reason = refresh_google_account(account, list(scopes))
        if status != "refreshed":
            display_name = provider_display_name or provider
            if status == "invalid":
                logger.warning(
                    "%s refresh token revoked or expired for %s: %s",
                    display_name,
                    account.get("email", "unknown"),
                    reason,
                )
            else:
                logger.error("%s token refresh failed: %s", display_name, reason)
            return None
        accounts[aid] = account
        source.cache["accounts"] = accounts
        source.persist(source.cache)

    return Credentials(
        token=account.get("access_token"),
        refresh_token=account.get("refresh_token"),
        token_uri=account.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=account.get("client_id"),
        client_secret=account.get("client_secret") or _resolve_google_client_secret(
            account.get("client_id")
        ),
        scopes=account.get("scopes", list(scopes)),
    )


def google_api_request(
    user_id: str,
    provider: str,
    scopes: Sequence[str],
    operation: Callable[[Any], Any],
    *,
    service_name: str,
    service_version: str,
    account_id: Optional[str] = None,
    auth_tool_name: Optional[str] = None,
    api_label: str,
    cache_filename: Optional[str] = None,
    build_kwargs: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    """Execute a Google API operation with shared auth and error handling.

    ``auth_tool_name`` is accepted for backwards-compatibility but no longer
    used: the legacy per-provider ``*_auth_start`` tools were removed in
    favour of ``request_credential(provider=..., kind="oauth")``.
    """
    del auth_tool_name  # legacy arg; tolerated for callers that still pass it.
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        return False, (
            "google-api-python-client not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = get_google_credentials(
        user_id,
        provider,
        scopes,
        account_id=account_id,
        cache_filename=cache_filename,
        provider_display_name=api_label,
    )
    if not creds:
        return False, (
            f"No authenticated Google account. Call "
            f"request_credential(provider=\"{provider}\", kind=\"oauth\") to connect."
        )

    try:
        kwargs = build_kwargs or {}
        service = build(service_name, service_version, credentials=creds, **kwargs)
        result = operation(service)
        return True, result
    except HttpError as e:
        try:
            error_details = json.loads(e.content.decode()) if e.content else {}
            msg = error_details.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        status = getattr(getattr(e, "resp", None), "status", "unknown")
        return False, f"{api_label} API error ({status}): {msg}"
    except Exception as e:
        logger.error("%s request failed: %s", api_label, e, exc_info=True)
        return False, f"Request failed: {str(e)}"


def exchange_code_for_tokens(
    auth_code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_uri: str = "https://oauth2.googleapis.com/token",
    code_verifier: Optional[str] = None,
) -> Tuple[bool, Any]:
    """Exchange an authorization code for ``(access_token, refresh_token)``.

    Returns ``(True, token_data_dict)`` on success or ``(False, error_string)``.
    """
    try:
        data = {
            "code": auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        response = httpx.post(token_uri, data=data, timeout=30)
        if response.status_code != 200:
            return False, f"Token exchange failed ({response.status_code}): {response.text}"
        return True, response.json()
    except Exception as e:
        return False, f"Token exchange request failed: {str(e)}"


def save_google_account(
    user_id: str,
    cache_filename: str,
    token_data: dict,
    client_id: str,
    client_secret: str,
    token_uri: str,
    scopes: list,
) -> Tuple[str, str, str]:
    """Persist a fetched token bundle into the per-user cache.

    Returns ``(account_id, email, display_name)``. Replaces the previously
    duplicated and broken ``_save_account()`` which referenced ``user_id``
    without accepting it as a parameter.
    """
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)

    email, name = fetch_google_user_info(access_token)
    account_id = email.lower().replace("@", "_at_").replace(".", "_")

    cache = load_token_cache(user_id, cache_filename)
    cache["accounts"] = cache.get("accounts", {})
    cache["accounts"][account_id] = {
        "email": email,
        "name": name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_uri": token_uri,
        "client_id": client_id,
        "scopes": list(scopes),
        "expires_at": time.time() + expires_in,
    }
    save_token_cache(user_id, cache_filename, cache)

    return account_id, email, name
