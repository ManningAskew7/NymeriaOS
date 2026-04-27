"""Shared OAuth scaffold for Google-based tools (calendar, docs, drive, sheets).

Per-user OAuth flow state, callback server, token-cache I/O, userinfo and
token-exchange helpers, previously duplicated in calendar_auth.py and
google_docs_auth.py. The duplication caused two real bugs:

1. Module-global ``_auth_state`` dicts allowed concurrent users to overwrite
   each other's pending OAuth flow.
2. ``_save_account`` referenced ``user_id`` it never accepted as a parameter,
   crashing OAuth completion with ``NameError`` whenever it ran.

This module fixes both: flow state is keyed by ``(user_id, provider)``, the
callback handler dispatches by state parameter (unique per flow, signed and
validated), and token persistence accepts ``user_id`` explicitly.
"""

from __future__ import annotations

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
from typing import Any, Dict, Optional, Tuple

import httpx

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
                    pass
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
            pass


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
                pass
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
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_token_cache(user_id: str, cache_filename: str) -> dict:
    path = cache_path(user_id, cache_filename)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_token_cache(user_id: str, cache_filename: str, cache: dict) -> None:
    path = cache_path(user_id, cache_filename)
    path.write_text(json.dumps(cache, indent=2))


def delete_token_cache(user_id: str, cache_filename: str) -> bool:
    """Delete a user's token cache file if it exists.

    Returns ``True`` when a file was removed, otherwise ``False``. The parent
    directory is still created by ``cache_path``; leaving an empty per-user auth
    directory is harmless and keeps this helper simple.
    """
    path = cache_path(user_id, cache_filename)
    if not path.exists():
        return False
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
        pass
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

    creds = Credentials(
        token=account.get("access_token"),
        refresh_token=refresh_token,
        token_uri=account.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=account.get("client_id"),
        client_secret=account.get("client_secret"),
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


def exchange_code_for_tokens(
    auth_code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_uri: str = "https://oauth2.googleapis.com/token",
) -> Tuple[bool, Any]:
    """Exchange an authorization code for ``(access_token, refresh_token)``.

    Returns ``(True, token_data_dict)`` on success or ``(False, error_string)``.
    """
    try:
        response = httpx.post(
            token_uri,
            data={
                "code": auth_code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
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
        "client_secret": client_secret,
        "scopes": list(scopes),
        "expires_at": time.time() + expires_in,
    }
    save_token_cache(user_id, cache_filename, cache)

    return account_id, email, name
