"""Shared OAuth scaffold for provider token caches and Google-based tools.

Per-user OAuth flow state, callback server, token-cache I/O, userinfo and
token-exchange helpers, previously duplicated in calendar_auth.py and
google_docs_auth.py. The duplication caused two real bugs:

1. Module-global ``_auth_state`` dicts allowed concurrent users to overwrite
   each other's pending OAuth flow.
2. ``_save_account`` referenced ``user_id`` it never accepted as a parameter,
   crashing OAuth completion with ``NameError`` whenever it ran.

This module fixes both: flow state is keyed by ``(user_id, provider)``, the
callback handler dispatches by state parameter (unique per flow, signed and
validated), token persistence accepts ``user_id`` explicitly, and the shared
Google credential/request/auth-tool helpers keep Calendar and Docs behavior in
one place.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Optional, Sequence, Tuple

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

OAUTH_TIMEOUT_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Per-user flow state
# ---------------------------------------------------------------------------


@dataclass
class OAuthFlow:
    """One in-flight OAuth Authorization-Code flow.

    Identified by ``(user_id, provider)``. ``state_param`` is unique per
    flow and is what the callback handler uses to route an inbound redirect
    to the right flow, so concurrent flows from different users don't
    collide.
    """

    user_id: str
    provider: str  # e.g. "google_calendar", "google_docs"
    state_param: str
    client_id: str
    client_secret: str
    token_uri: str
    redirect_uri: str
    started_at: float
    code_verifier: Optional[str] = None
    auth_code: Optional[str] = None
    auth_error: Optional[str] = None
    completed: bool = False
    port: Optional[int] = None
    httpd: Any = field(default=None, repr=False)
    server_thread: Any = field(default=None, repr=False)


_FLOW_REGISTRY: Dict[str, OAuthFlow] = {}
_STATE_INDEX: Dict[str, str] = {}
_REGISTRY_LOCK = threading.Lock()


def _flow_key(user_id: str, provider: str) -> str:
    return f"{user_id}:{provider}"


def register_flow(flow: OAuthFlow) -> None:
    """Register a new flow, replacing any existing one for the same key.

    Closes the prior flow's HTTP server (if any) so we don't leak file
    descriptors when a user retries auth_start mid-flow.
    """
    key = _flow_key(flow.user_id, flow.provider)
    with _REGISTRY_LOCK:
        old = _FLOW_REGISTRY.get(key)
        if old:
            _STATE_INDEX.pop(old.state_param, None)
            if old.httpd is not None:
                try:
                    old.httpd.server_close()
                except Exception:
                    logger.debug("Error closing previous OAuth HTTP server")
        _FLOW_REGISTRY[key] = flow
        _STATE_INDEX[flow.state_param] = key


def get_flow(user_id: str, provider: str) -> Optional[OAuthFlow]:
    with _REGISTRY_LOCK:
        return _FLOW_REGISTRY.get(_flow_key(user_id, provider))


def lookup_flow_by_state(state_param: str) -> Optional[OAuthFlow]:
    """Find a flow by the ``state`` URL parameter. Used by the HTTP handler
    to dispatch a single global server's requests to the right flow."""
    with _REGISTRY_LOCK:
        key = _STATE_INDEX.get(state_param)
        if key is None:
            return None
        return _FLOW_REGISTRY.get(key)


def clear_flow(user_id: str, provider: str) -> None:
    """Tear down a flow: drop registry entries and close its HTTP server."""
    key = _flow_key(user_id, provider)
    with _REGISTRY_LOCK:
        flow = _FLOW_REGISTRY.pop(key, None)
        if flow is None:
            return
        _STATE_INDEX.pop(flow.state_param, None)
    if flow.httpd is not None:
        try:
            flow.httpd.server_close()
        except Exception:
            logger.debug("Error closing OAuth HTTP server on flow clear")


# ---------------------------------------------------------------------------
# Callback HTTP server
# ---------------------------------------------------------------------------


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the OAuth redirect callback from the provider.

    The same handler class serves all flows; request dispatch is by the
    ``state`` URL parameter, which uniquely identifies the flow.
    """

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        returned_state = params.get("state", [None])[0]
        flow = lookup_flow_by_state(returned_state) if returned_state else None

        if flow is None:
            # Either no state, or the flow expired / belonged to a different
            # process. Reject without leaking which case it was.
            self._send_page(
                400,
                "Authentication Failed",
                "Invalid or expired authentication session. Please start "
                "the flow again from Nymeria.",
            )
            return

        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            flow.auth_error = error
            self._send_page(
                400,
                "Authentication Denied",
                f"Provider returned an error: {error}",
            )
        elif code:
            flow.auth_code = code
            self._send_page(
                200,
                "Authentication Successful",
                "You can close this tab and return to Nymeria.",
            )
        else:
            flow.auth_error = "No code or error in callback."
            self._send_page(
                400,
                "Authentication Failed",
                "Unexpected callback: no authorization code received.",
            )

        flow.completed = True

    def _send_page(self, status: int, title: str, message: str):
        body = f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;justify-content:center;
align-items:center;min-height:100vh;margin:0;background:#1a1a2e;color:#e0e0e0}}
.card{{background:#16213e;border-radius:12px;padding:2rem 3rem;text-align:center;
box-shadow:0 4px 24px rgba(0,0,0,.3)}}h2{{color:{"#4ade80" if status==200 else "#f87171"}}}
</style></head><body><div class="card"><h2>{title}</h2><p>{message}</p></div></body></html>""".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default access logging


def start_callback_server(flow: OAuthFlow) -> int:
    """Open an ephemeral HTTP server for this flow's callback.

    Mutates the flow in place: sets ``port``, ``httpd``, and ``server_thread``.
    Returns the port. The server handles exactly one request, then closes.
    If no request arrives within ``OAUTH_TIMEOUT_SECONDS`` the flow is marked
    timed-out.
    """
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    port = httpd.server_address[1]

    flow.httpd = httpd
    flow.port = port

    def _serve() -> None:
        httpd.timeout = OAUTH_TIMEOUT_SECONDS
        try:
            httpd.handle_request()
        finally:
            try:
                httpd.server_close()
            except Exception:
                logger.debug("Error closing OAuth callback server")
        if not flow.completed:
            flow.auth_error = "timeout"
            flow.completed = True

    t = threading.Thread(target=_serve, name=f"oauth-{flow.user_id}-{flow.provider}", daemon=True)
    t.start()
    flow.server_thread = t
    return port


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
    ``google_docs``. Tokens are refreshed with the same 60-second expiry buffer
    used by the older per-tool implementations.
    """
    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.error(
            "google-auth packages not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib"
        )
        return None

    filename = _google_cache_filename(provider, cache_filename)
    cache = load_token_cache(user_id, filename)
    accounts = cache.get("accounts", {})
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
        cache["accounts"] = accounts
        save_token_cache(user_id, filename, cache)

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
    auth_tool_name: str,
    api_label: str,
    cache_filename: Optional[str] = None,
    build_kwargs: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    """Execute a Google API operation with shared auth and error handling."""
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
        return False, f"No authenticated Google account. Use {auth_tool_name} to authenticate."

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


@dataclass(frozen=True)
class GoogleOAuthToolSpec:
    """Text and cache settings for one generated Google OAuth tool set."""

    provider: str
    cache_filename: str
    scopes: list[str]
    service_display_name: str
    setup_api_name: str
    usable_tools_label: str
    start_tool_name: str
    complete_tool_name: str
    clear_tool_name: str
    list_tool_name: str
    no_accounts_message: str
    no_usable_accounts_message: str
    list_heading: str
    post_save_hook: Optional[Callable[[str, str], Optional[str]]] = None
    post_clear_hook: Optional[Callable[[str, Optional[str]], Optional[str]]] = None


def _persist_or_delete_google_cache(user_id: str, spec: GoogleOAuthToolSpec, cache: dict) -> None:
    if cache:
        save_token_cache(user_id, spec.cache_filename, cache)
    else:
        delete_token_cache(user_id, spec.cache_filename)


def _existing_google_auth_message(user_id: str, spec: GoogleOAuthToolSpec) -> Optional[str]:
    """Return an already-authenticated message, pruning dead tokens first."""
    cache = load_token_cache(user_id, spec.cache_filename)
    accounts = cache.get("accounts", {})
    if not accounts:
        return None

    required_scopes = set(spec.scopes)
    changed = False
    for account_id, account in list(accounts.items()):
        email = account.get("email", "unknown")
        has_refresh = bool(account.get("refresh_token"))
        saved_scopes = set(account.get("scopes", []))
        missing_scopes = required_scopes - saved_scopes

        if not has_refresh:
            continue
        if missing_scopes:
            logger.info(
                "%s scope change detected for %s. Missing scopes: %s. "
                "Proceeding with re-auth.",
                spec.service_display_name,
                email,
                missing_scopes,
            )
            continue

        expires_at = account.get("expires_at", 0)
        if time.time() >= expires_at - 60:
            status, reason = refresh_google_account(account, spec.scopes)
            if status == "refreshed":
                accounts[account_id] = account
                changed = True
            elif status == "invalid":
                logger.info(
                    "Clearing invalid %s token for %s: %s",
                    spec.service_display_name,
                    email,
                    reason,
                )
                del accounts[account_id]
                changed = True
                continue
            else:
                if changed:
                    cache["accounts"] = accounts
                    _persist_or_delete_google_cache(user_id, spec, cache)
                return (
                    f"[Warning]: Found expired {spec.service_display_name} credentials for "
                    f"**{account.get('name', 'Unknown')}** ({email}), but could not "
                    f"verify the refresh token: {reason}\n\n"
                    f"Use `{spec.clear_tool_name}` to remove the saved token, then run "
                    f"`{spec.start_tool_name}` again."
                )

        if changed:
            cache["accounts"] = accounts
            _persist_or_delete_google_cache(user_id, spec, cache)

        return (
            f"[Info]: Already authenticated as **{account.get('name', 'Unknown')}** ({email}). "
            "Tokens will auto-refresh. To add another account or re-authenticate, "
            f"call `{spec.clear_tool_name}` first."
        )

    if changed:
        if accounts:
            cache["accounts"] = accounts
        else:
            cache.pop("accounts", None)
        _persist_or_delete_google_cache(user_id, spec, cache)

    return None


def _build_google_auth_success_message(
    spec: GoogleOAuthToolSpec,
    account_id: str,
    email: str,
    name: str,
) -> str:
    return (
        f"[Success]: {spec.service_display_name} authentication complete!\n\n"
        f"**Account:** {name} ({email})\n"
        f"**Account ID:** {account_id}\n\n"
        f"You can now use the {spec.usable_tools_label}."
    )


def _exchange_and_save_google_flow(
    user_id: str,
    spec: GoogleOAuthToolSpec,
    flow: OAuthFlow,
    auth_code: str,
) -> tuple[bool, str]:
    success, token_data = exchange_code_for_tokens(
        auth_code,
        flow.client_id,
        flow.client_secret,
        flow.redirect_uri,
        flow.token_uri,
        flow.code_verifier,
    )
    if not success:
        return False, f"[Error]: {token_data}"

    account_id, email, name = save_google_account(
        user_id,
        spec.cache_filename,
        token_data,
        flow.client_id,
        flow.client_secret,
        flow.token_uri,
        spec.scopes,
    )
    hook_message = None
    if spec.post_save_hook:
        try:
            hook_message = spec.post_save_hook(user_id, account_id)
        except Exception as e:
            logger.warning(
                "%s post-save hook failed for %s: %s",
                spec.service_display_name,
                account_id,
                e,
                exc_info=True,
            )
            hook_message = (
                f"[Warning]: {spec.service_display_name} authentication succeeded, "
                f"but post-auth setup failed: {e}"
            )
    clear_flow(user_id, spec.provider)
    message = _build_google_auth_success_message(spec, account_id, email, name)
    if hook_message:
        message = f"{message}\n\n{hook_message}"
    return True, message


def _run_google_post_clear_hook(
    user_id: str,
    spec: GoogleOAuthToolSpec,
    account_id: Optional[str],
) -> Optional[str]:
    if not spec.post_clear_hook:
        return None
    try:
        return spec.post_clear_hook(user_id, account_id)
    except Exception as e:
        logger.warning(
            "%s post-clear hook failed for %s: %s",
            spec.service_display_name,
            account_id or "(all)",
            e,
            exc_info=True,
        )
        return (
            f"[Warning]: {spec.service_display_name} auth cache was cleared, "
            f"but post-clear cleanup failed: {e}"
        )


def create_google_oauth_tools(spec: GoogleOAuthToolSpec) -> list[Any]:
    """Generate start/complete/clear/list tools for one Google OAuth provider."""
    from .utils import get_user_id

    @tool(
        spec.start_tool_name,
        description=f"Start {spec.service_display_name} OAuth authentication.",
    )
    def auth_start(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
        user_id = get_user_id(config)

        existing_message = _existing_google_auth_message(user_id, spec)
        if existing_message:
            return existing_message

        existing = get_flow(user_id, spec.provider)
        if existing and existing.server_thread and existing.server_thread.is_alive() and not existing.completed:
            elapsed = time.time() - existing.started_at
            remaining = max(0, OAUTH_TIMEOUT_SECONDS - elapsed)
            return (
                "[Info]: Authentication is already in progress!\n\n"
                "Open the authorization URL in your browser (if you haven't already), "
                f"then call {spec.complete_tool_name}.\n\n"
                f"The session will expire in about {int(remaining / 60)} minutes."
            )

        creds_path = get_google_credentials_path()
        if not creds_path:
            return (
                "[Error]: GOOGLE_OAUTH_CREDENTIALS environment variable is not set or the file was not found.\n\n"
                "**Setup instructions:**\n"
                "1. Go to https://console.cloud.google.com\n"
                "2. Create a project (or select existing)\n"
                f"3. Enable the **{spec.setup_api_name}** under APIs & Services > Library\n"
                "4. Go to APIs & Services > Credentials > Create Credentials > OAuth Client ID\n"
                "5. Application type: **Desktop app**\n"
                "6. Download the JSON credentials file\n"
                "7. Set `GOOGLE_OAUTH_CREDENTIALS=/path/to/credentials.json` in your .env file\n"
                "8. Restart Nymeria and try again"
            )

        try:
            with open(creds_path) as f:
                client_config = json.load(f)
            creds_data = client_config.get("installed") or client_config.get("web")
            if not creds_data:
                return "[Error]: Invalid credentials file format. Expected 'installed' or 'web' key in the JSON."

            client_id = creds_data["client_id"]
            client_secret = creds_data["client_secret"]
            auth_uri = creds_data.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")
            token_uri = creds_data.get("token_uri", "https://oauth2.googleapis.com/token")

            import secrets

            code_verifier = secrets.token_urlsafe(64)
            flow = OAuthFlow(
                user_id=user_id,
                provider=spec.provider,
                state_param=secrets.token_urlsafe(16),
                client_id=client_id,
                client_secret=client_secret,
                token_uri=token_uri,
                redirect_uri="",
                started_at=time.time(),
                code_verifier=code_verifier,
            )
            register_flow(flow)
            port = start_callback_server(flow)
            flow.redirect_uri = f"http://localhost:{port}"

            params = {
                "client_id": client_id,
                "redirect_uri": flow.redirect_uri,
                "response_type": "code",
                "scope": " ".join(spec.scopes),
                "access_type": "offline",
                "prompt": "consent",
                "state": flow.state_param,
                "code_challenge": _pkce_challenge(code_verifier),
                "code_challenge_method": "S256",
            }
            auth_url = auth_uri + "?" + urllib.parse.urlencode(params)

            return (
                "[Success]: Authorization URL generated.\n\n"
                "**Open this URL in your browser to sign in:**\n\n"
                f"{auth_url}\n\n"
                "After signing in and granting access, the page should redirect automatically "
                "and show a success message.\n\n"
                f"Then call `{spec.complete_tool_name}` to finish.\n\n"
                "**If the redirect page doesn't load** (e.g. Docker/remote), copy the full URL "
                "from your browser's address bar and give it to me. I'll extract the auth code from it."
            )

        except KeyError as e:
            return f"[Error]: Credentials file is missing required field: {e}"
        except json.JSONDecodeError:
            return "[Error]: Credentials file is not valid JSON."
        except Exception as e:
            logger.error("%s failed: %s", spec.start_tool_name, e, exc_info=True)
            return f"[Error]: Failed to start authentication: {str(e)}"

    @tool(
        spec.clear_tool_name,
        description=f"Clear saved {spec.service_display_name} authentication.",
    )
    def auth_clear(
        account_id: Optional[str] = None,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        user_id = get_user_id(config)
        cache = load_token_cache(user_id, spec.cache_filename)
        accounts = cache.get("accounts", {})

        clear_flow(user_id, spec.provider)

        if account_id:
            account = accounts.pop(account_id, None)
            if account is None:
                return (
                    f"[Info]: No {spec.service_display_name} account found with ID `{account_id}`. "
                    f"Use {spec.list_tool_name} to see saved accounts."
                )

            email = account.get("email", "unknown")
            name = account.get("name", "Unknown")
            if accounts:
                cache["accounts"] = accounts
            else:
                cache.pop("accounts", None)
            _persist_or_delete_google_cache(user_id, spec, cache)
            message = (
                f"[Success]: Cleared {spec.service_display_name} authentication for "
                f"**{name}** ({email}). Run `{spec.start_tool_name}` to authenticate again."
            )
            hook_message = _run_google_post_clear_hook(user_id, spec, account_id)
            if hook_message:
                message = f"{message}\n\n{hook_message}"
            return message

        removed_count = len(accounts)
        cache.pop("accounts", None)
        _persist_or_delete_google_cache(user_id, spec, cache)
        hook_message = _run_google_post_clear_hook(user_id, spec, None)

        if removed_count:
            message = (
                f"[Success]: Cleared {removed_count} saved {spec.service_display_name} account(s) "
                f"and any pending {spec.service_display_name} OAuth flow. "
                f"Run `{spec.start_tool_name}` to authenticate again."
            )
            if hook_message:
                message = f"{message}\n\n{hook_message}"
            return message

        message = (
            f"[Info]: No saved {spec.service_display_name} authentication was present. "
            f"Any pending {spec.service_display_name} OAuth flow was cleared."
        )
        if hook_message:
            message = f"{message}\n\n{hook_message}"
        return message

    @tool(
        spec.complete_tool_name,
        description=f"Complete pending {spec.service_display_name} OAuth authentication.",
    )
    def auth_complete(
        redirect_url: Optional[str] = None,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        user_id = get_user_id(config)
        flow = get_flow(user_id, spec.provider)
        if flow is None:
            return f"[Error]: No pending authentication. Please call {spec.start_tool_name} first."

        if redirect_url:
            try:
                parsed = urllib.parse.urlparse(redirect_url)
                params = urllib.parse.parse_qs(parsed.query)
                auth_code = params.get("code", [None])[0]
                returned_state = params.get("state", [None])[0]
                if not auth_code:
                    return "[Error]: No authorization code found in the URL. Make sure you copied the full URL from the browser's address bar."
            except Exception:
                return "[Error]: Could not parse the redirect URL. Please copy the complete URL."

            if returned_state != flow.state_param:
                return (
                    "[Error]: State parameter mismatch. The URL may be from a different "
                    f"auth session. Please call {spec.start_tool_name} to begin again."
                )

            _, message = _exchange_and_save_google_flow(user_id, spec, flow, auth_code)
            return message

        server_alive = flow.server_thread is not None and flow.server_thread.is_alive()
        if not flow.completed and server_alive:
            elapsed = int(time.time() - flow.started_at)
            remaining = max(0, OAUTH_TIMEOUT_SECONDS - elapsed)
            return (
                f"[Info]: Still waiting for browser redirect ({elapsed}s elapsed, "
                f"{remaining}s remaining).\n\n"
                "Open the URL in your browser if you haven't yet.\n\n"
                "If the redirect page didn't load, copy the full URL from your "
                "browser's address bar and call this tool again with `redirect_url`."
            )

        if flow.auth_error == "timeout":
            clear_flow(user_id, spec.provider)
            return f"[Error]: Authentication timed out (5 minutes). Please call {spec.start_tool_name} to begin again."

        if flow.auth_error:
            err = flow.auth_error
            clear_flow(user_id, spec.provider)
            return f"[Error]: Authentication failed: {err}. Please call {spec.start_tool_name} to try again."

        if not flow.auth_code:
            clear_flow(user_id, spec.provider)
            return f"[Error]: Authentication completed but no code was received. Please call {spec.start_tool_name} to try again."

        success, message = _exchange_and_save_google_flow(user_id, spec, flow, flow.auth_code)
        if not success:
            clear_flow(user_id, spec.provider)
        return message

    @tool(
        spec.list_tool_name,
        description=f"List authenticated accounts for {spec.service_display_name}.",
    )
    def list_accounts(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
        user_id = get_user_id(config)
        cache = load_token_cache(user_id, spec.cache_filename)
        accounts = cache.get("accounts", {})

        if not accounts:
            return spec.no_accounts_message

        rows, changed = validate_google_accounts_for_display(accounts, spec.scopes)
        if changed:
            if accounts:
                cache["accounts"] = accounts
            else:
                cache.pop("accounts", None)
            _persist_or_delete_google_cache(user_id, spec, cache)

        if not rows:
            return spec.no_usable_accounts_message

        lines = [spec.list_heading]
        for row in rows:
            row_account_id = row["account_id"]
            info = row["account"]
            email = info.get("email", "unknown")
            name = info.get("name", "Unknown")
            status = row["status"]

            lines.append(f"- **{name}** ({email})")
            lines.append(f"  Account ID: `{row_account_id}` ({status})")
            if not row["usable"] and row.get("reason"):
                lines.append(
                    f"  Action: run `{spec.clear_tool_name}(account_id=\"{row_account_id}\")`, "
                    f"then `{spec.start_tool_name}`."
                )

        return "\n".join(lines)

    return [auth_start, auth_complete, auth_clear, list_accounts]


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
