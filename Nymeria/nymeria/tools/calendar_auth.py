"""Google Calendar OAuth 2.0 authentication tools.

Provides agent-guided OAuth flow using the Authorization Code grant with
a localhost redirect. A temporary HTTP server captures the callback
automatically in local/WSL2 environments. For Docker or remote setups,
the user can manually paste the redirect URL as a fallback.

Token cache is stored at ~/.google_calendar_token_cache.json and tokens
are auto-refreshed on expiry (60-second buffer).

Optional tools — enable per-thread via thread config.
"""

import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Annotated, Optional

import httpx
from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_user_id

# Load .env so GOOGLE_OAUTH_CREDENTIALS is available via os.environ
# (Pydantic Settings only loads its own defined fields)
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)

# Per-user Google Calendar token cache: data/auth_tokens/<user_id>/google_calendar.json
_CACHE_FILENAME = "google_calendar.json"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# How long the background HTTP server waits for the redirect
OAUTH_TIMEOUT_SECONDS = 300  # 5 minutes

# Auth state: shared between the background server thread and the main thread
_auth_state: dict = {
    "server_thread": None,
    "httpd": None,
    "port": None,
    "auth_code": None,
    "auth_error": None,
    "completed": False,
    "started_at": None,
    "state_param": None,
    "client_id": None,
    "client_secret": None,
    "token_uri": None,
    "redirect_uri": None,
}
_auth_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Token cache helpers
# ---------------------------------------------------------------------------

def get_credentials_path() -> Optional[str]:
    """Get path to Google OAuth credentials JSON from environment."""
    path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
    if path:
        path = os.path.expanduser(path)
        if os.path.exists(path):
            return path
    return None


def _safe_user_id(user_id: str) -> str:
    safe = "".join(c for c in user_id if c.isalnum() or c in "-_")
    return safe or "default"


def _cache_path(user_id: str) -> Path:
    """Per-user token cache path."""
    from ..config import get_settings
    settings = get_settings()
    path = settings.data_dir / "auth_tokens" / _safe_user_id(user_id) / _CACHE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_token_cache(user_id: str) -> dict:
    """Load a user's Google Calendar token cache."""
    path = _cache_path(user_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_token_cache(user_id: str, cache: dict) -> None:
    """Persist a user's Google Calendar token cache."""
    path = _cache_path(user_id)
    path.write_text(json.dumps(cache, indent=2))


# ---------------------------------------------------------------------------
# Background OAuth HTTP server
# ---------------------------------------------------------------------------

class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the single OAuth redirect callback from Google."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        with _auth_state_lock:
            # CSRF check
            returned_state = params.get("state", [None])[0]
            if returned_state != _auth_state["state_param"]:
                _auth_state["auth_error"] = "State mismatch — possible CSRF."
                self._send_page(
                    400,
                    "Authentication Failed",
                    "Invalid state parameter. Please try authenticating again.",
                )
                _auth_state["completed"] = True
                return

            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]

            if error:
                _auth_state["auth_error"] = error
                self._send_page(
                    400,
                    "Authentication Denied",
                    f"Google returned an error: {error}",
                )
            elif code:
                _auth_state["auth_code"] = code
                self._send_page(
                    200,
                    "Authentication Successful",
                    "You can close this tab and return to Nymeria.",
                )
            else:
                _auth_state["auth_error"] = "No code or error in callback."
                self._send_page(
                    400,
                    "Authentication Failed",
                    "Unexpected callback — no authorization code received.",
                )

            _auth_state["completed"] = True

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


def _start_oauth_server() -> int:
    """
    Start the background OAuth redirect server on an ephemeral port.

    Returns the port number.
    """
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    port = httpd.server_address[1]

    with _auth_state_lock:
        _auth_state["httpd"] = httpd
        _auth_state["port"] = port
        _auth_state["auth_code"] = None
        _auth_state["auth_error"] = None
        _auth_state["completed"] = False
        _auth_state["started_at"] = time.time()
        _auth_state["state_param"] = secrets.token_urlsafe(16)

    def _serve():
        httpd.timeout = OAUTH_TIMEOUT_SECONDS
        httpd.handle_request()  # Blocks for one request (the redirect)
        httpd.server_close()
        # If timed out (no request arrived) mark as completed
        with _auth_state_lock:
            if not _auth_state["completed"]:
                _auth_state["auth_error"] = "timeout"
                _auth_state["completed"] = True

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    with _auth_state_lock:
        _auth_state["server_thread"] = t

    return port


def _exchange_code_for_tokens(
    auth_code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    token_uri: str = "https://oauth2.googleapis.com/token",
) -> tuple[bool, dict | str]:
    """
    Exchange an authorization code for access and refresh tokens.

    Returns (True, token_data_dict) on success or (False, error_string) on failure.
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


def _fetch_user_info(access_token: str) -> tuple[str, str]:
    """Fetch email and display name from Google's userinfo endpoint."""
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


def _save_account(
    token_data: dict,
    client_id: str,
    client_secret: str,
    token_uri: str,
) -> tuple[str, str, str]:
    """
    Save token data to the cache file.

    Returns (account_id, email, name).
    """
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)

    email, name = _fetch_user_info(access_token)
    account_id = email.lower().replace("@", "_at_").replace(".", "_")

    cache = load_token_cache(user_id)
    cache["accounts"] = cache.get("accounts", {})
    cache["accounts"][account_id] = {
        "email": email,
        "name": name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_uri": token_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": list(GOOGLE_SCOPES),
        "expires_at": time.time() + expires_in,
    }
    save_token_cache(user_id, cache)

    return account_id, email, name


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def calendar_auth_start(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    Start Google Calendar authentication using the OAuth 2.0 authorization code flow.

    Generates an authorization URL for the user to open in their browser.
    After signing in and granting access, Google redirects to localhost and
    the auth code is captured automatically. Call calendar_auth_complete to
    finish the flow.

    If the redirect page doesn't load (e.g. Docker or remote server), the
    user can copy the full URL from their browser's address bar and pass it
    to calendar_auth_complete as the redirect_url parameter.

    Requires the GOOGLE_OAUTH_CREDENTIALS environment variable to point to a
    Google OAuth Desktop App credentials JSON file from Google Cloud Console.

    Returns:
        Authorization URL and instructions.
    """
    user_id = get_user_id(config)
    # Check for existing valid credentials
    cache = load_token_cache(user_id)
    accounts = cache.get("accounts", {})
    if accounts:
        first = next(iter(accounts.values()))
        email = first.get("email", "unknown")
        has_refresh = bool(first.get("refresh_token"))
        if has_refresh:
            return (
                f"[Info]: Already authenticated as **{first.get('name', 'Unknown')}** ({email}). "
                "Tokens will auto-refresh. To add another account or re-authenticate, "
                "delete the token cache at ~/.google_calendar_token_cache.json first."
            )

    # Check for already-running auth server
    with _auth_state_lock:
        thread = _auth_state.get("server_thread")
        if thread and thread.is_alive() and not _auth_state.get("completed"):
            elapsed = time.time() - (_auth_state.get("started_at") or 0)
            remaining = max(0, OAUTH_TIMEOUT_SECONDS - elapsed)
            return (
                f"[Info]: Authentication is already in progress!\n\n"
                f"Open the authorization URL in your browser (if you haven't already), "
                f"then call calendar_auth_complete.\n\n"
                f"The session will expire in about {int(remaining / 60)} minutes."
            )

    # Check for credentials file
    creds_path = get_credentials_path()
    if not creds_path:
        return (
            "[Error]: GOOGLE_OAUTH_CREDENTIALS environment variable is not set or the file was not found.\n\n"
            "**Setup instructions:**\n"
            "1. Go to https://console.cloud.google.com\n"
            "2. Create a project (or select existing)\n"
            "3. Enable the **Google Calendar API** under APIs & Services > Library\n"
            "4. Go to APIs & Services > Credentials > Create Credentials > OAuth Client ID\n"
            "5. Application type: **Desktop app**\n"
            "6. Download the JSON credentials file\n"
            "7. Set `GOOGLE_OAUTH_CREDENTIALS=/path/to/credentials.json` in your .env file\n"
            "8. Restart Nymeria and try again"
        )

    try:
        with open(creds_path) as f:
            client_config = json.load(f)

        # Support both "installed" and "web" credential types
        creds_data = client_config.get("installed") or client_config.get("web")
        if not creds_data:
            return "[Error]: Invalid credentials file format. Expected 'installed' or 'web' key in the JSON."

        client_id = creds_data["client_id"]
        client_secret = creds_data["client_secret"]
        auth_uri = creds_data.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")
        token_uri = creds_data.get("token_uri", "https://oauth2.googleapis.com/token")

        # Start the background server to capture the redirect
        port = _start_oauth_server()
        redirect_uri = f"http://localhost:{port}"

        with _auth_state_lock:
            state = _auth_state["state_param"]
            _auth_state["client_id"] = client_id
            _auth_state["client_secret"] = client_secret
            _auth_state["token_uri"] = token_uri
            _auth_state["redirect_uri"] = redirect_uri

        # Build the authorization URL
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        auth_url = auth_uri + "?" + urllib.parse.urlencode(params)

        return (
            f"[Success]: Authorization URL generated.\n\n"
            f"**Open this URL in your browser to sign in:**\n\n"
            f"{auth_url}\n\n"
            f"After signing in and granting access, the page should redirect automatically "
            f"and show a success message.\n\n"
            f"Then call `calendar_auth_complete` to finish.\n\n"
            f"**If the redirect page doesn't load** (e.g. Docker/remote), copy the full URL "
            f"from your browser's address bar and give it to me — I'll extract the auth code from it."
        )

    except KeyError as e:
        return f"[Error]: Credentials file is missing required field: {e}"
    except json.JSONDecodeError:
        return "[Error]: Credentials file is not valid JSON."
    except Exception as e:
        logger.error(f"calendar_auth_start failed: {e}", exc_info=True)
        return f"[Error]: Failed to start authentication: {str(e)}"


@tool
def calendar_auth_complete(redirect_url: Optional[str] = None, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    Complete Google Calendar authentication.

    Call this after opening the authorization URL from calendar_auth_start
    in your browser and signing in.

    If the localhost redirect worked, the auth code was captured automatically.
    If it didn't work (Docker, remote server, etc.), pass the full URL from
    your browser's address bar as redirect_url — the auth code will be
    extracted from the URL query parameters.

    Args:
        redirect_url: Optional — the full redirect URL from the browser's address bar
                      (e.g. "http://localhost:12345/?code=4/0AQl...&state=abc123").
                      Only needed if the automatic redirect didn't work.

    Returns:
        Success message with account info, or status if still waiting.
    """
    user_id = get_user_id(config)
    # --- Fallback path: manual redirect URL ---
    if redirect_url:
        with _auth_state_lock:
            client_id = _auth_state.get("client_id")
            client_secret = _auth_state.get("client_secret")
            token_uri = _auth_state.get("token_uri", "https://oauth2.googleapis.com/token")
            redirect_uri = _auth_state.get("redirect_uri", "http://localhost")
            expected_state = _auth_state.get("state_param")

        if not client_id:
            return "[Error]: No pending authentication. Please call calendar_auth_start first."

        # Parse the code and state from the URL
        try:
            parsed = urllib.parse.urlparse(redirect_url)
            params = urllib.parse.parse_qs(parsed.query)
            auth_code = params.get("code", [None])[0]
            returned_state = params.get("state", [None])[0]
            if not auth_code:
                return "[Error]: No authorization code found in the URL. Make sure you copied the full URL from the browser's address bar."
        except Exception:
            return "[Error]: Could not parse the redirect URL. Please copy the complete URL."

        # Validate CSRF state parameter
        if expected_state and returned_state != expected_state:
            return "[Error]: State parameter mismatch — the URL may be from a different auth session. Please call calendar_auth_start to begin again."

        success, token_data = _exchange_code_for_tokens(
            auth_code, client_id, client_secret, redirect_uri, token_uri
        )
        if not success:
            return f"[Error]: {token_data}"

        account_id, email, name = _save_account(token_data, client_id, client_secret, token_uri)

        # Fully reset auth state (including shutting down any lingering server)
        with _auth_state_lock:
            _auth_state["client_id"] = None
            _auth_state["client_secret"] = None
            _auth_state["auth_code"] = None
            _auth_state["completed"] = True
            httpd = _auth_state.get("httpd")
            if httpd:
                try:
                    httpd.server_close()
                except Exception:
                    pass
                _auth_state["httpd"] = None

        return (
            f"[Success]: Google Calendar authentication complete!\n\n"
            f"**Account:** {name} ({email})\n"
            f"**Account ID:** {account_id}\n\n"
            f"You can now use the calendar tools."
        )

    # --- Primary path: check background server ---
    with _auth_state_lock:
        completed = _auth_state.get("completed", False)
        auth_code = _auth_state.get("auth_code")
        auth_error = _auth_state.get("auth_error")
        client_id = _auth_state.get("client_id")
        client_secret = _auth_state.get("client_secret")
        token_uri = _auth_state.get("token_uri", "https://oauth2.googleapis.com/token")
        redirect_uri = _auth_state.get("redirect_uri", "http://localhost")
        server_alive = (
            _auth_state.get("server_thread") is not None
            and _auth_state["server_thread"].is_alive()
        )
        started_at = _auth_state.get("started_at", 0)

    if not client_id:
        return "[Error]: No pending authentication. Please call calendar_auth_start first."

    if not completed and server_alive:
        elapsed = int(time.time() - started_at)
        remaining = max(0, OAUTH_TIMEOUT_SECONDS - elapsed)
        return (
            f"[Info]: Still waiting for browser redirect ({elapsed}s elapsed, "
            f"{remaining}s remaining).\n\n"
            f"Open the URL in your browser if you haven't yet.\n\n"
            f"If the redirect page didn't load, copy the full URL from your "
            f"browser's address bar and call this tool again with `redirect_url`."
        )

    if auth_error == "timeout":
        with _auth_state_lock:
            _auth_state["client_id"] = None
        return "[Error]: Authentication timed out (5 minutes). Please call calendar_auth_start to begin again."

    if auth_error:
        with _auth_state_lock:
            _auth_state["client_id"] = None
        return f"[Error]: Authentication failed: {auth_error}. Please call calendar_auth_start to try again."

    if not auth_code:
        if not completed:
            return "[Error]: No pending authentication found. Please call calendar_auth_start first."
        with _auth_state_lock:
            _auth_state["client_id"] = None
        return "[Error]: Authentication completed but no code was received. Please call calendar_auth_start to try again."

    # Exchange the auth code for tokens
    success, token_data = _exchange_code_for_tokens(
        auth_code, client_id, client_secret, redirect_uri, token_uri
    )
    if not success:
        with _auth_state_lock:
            _auth_state["client_id"] = None
        return f"[Error]: {token_data}"

    account_id, email, name = _save_account(token_data, client_id, client_secret, token_uri)

    # Clear auth state
    with _auth_state_lock:
        _auth_state["auth_code"] = None
        _auth_state["client_id"] = None

    return (
        f"[Success]: Google Calendar authentication complete!\n\n"
        f"**Account:** {name} ({email})\n"
        f"**Account ID:** {account_id}\n\n"
        f"You can now use the calendar tools."
    )


@tool
def calendar_list_authenticated_accounts(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    List all authenticated Google accounts for Calendar access.

    Shows account email, display name, and token status.

    Returns:
        List of authenticated accounts with their IDs and email addresses.
    """
    user_id = get_user_id(config)
    cache = load_token_cache(user_id)
    accounts = cache.get("accounts", {})

    if not accounts:
        return "[Info]: No Google accounts authenticated. Use calendar_auth_start to add an account."

    lines = ["[Success]: Authenticated Google accounts:\n"]
    for account_id, info in accounts.items():
        email = info.get("email", "unknown")
        name = info.get("name", "Unknown")
        expires_at = info.get("expires_at", 0)
        has_refresh = bool(info.get("refresh_token"))
        expired = time.time() > expires_at

        if expired and has_refresh:
            status = "(token expired, will auto-refresh)"
        elif expired:
            status = "(expired — re-authentication required)"
        else:
            status = "(active)"

        lines.append(f"- **{name}** ({email})")
        lines.append(f"  Account ID: `{account_id}` {status}")

    return "\n".join(lines)


# Export tools
CALENDAR_AUTH_TOOLS = [
    calendar_auth_start,
    calendar_auth_complete,
    calendar_list_authenticated_accounts,
]
