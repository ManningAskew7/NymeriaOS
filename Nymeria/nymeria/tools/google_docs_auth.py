"""Google Docs / Drive / Sheets OAuth 2.0 authentication tools.

Per-user OAuth flow: each ``(user_id, "google_docs")`` pair gets its own
in-memory flow state and ephemeral callback server, so concurrent users
don't overwrite each other's pending auth. The heavy lifting lives in
``auth_cache_utils`` — this file only wires the Docs/Drive/Sheets specifics
(scopes, cache filename) and exposes the agent tools.

Token cache is per-user at ``data/auth_tokens/<user_id>/google_docs.json``
and tokens are auto-refreshed on expiry (60-second buffer) by ``google_docs.py``.

Optional tools — enable per-thread via thread config.
"""

import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Annotated, Optional

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from . import auth_cache_utils as auth_utils
from .auth_cache_utils import (
    OAUTH_TIMEOUT_SECONDS,
    OAuthFlow,
    clear_flow,
    exchange_code_for_tokens,
    get_flow,
    get_google_credentials_path,
    register_flow,
    save_google_account,
    start_callback_server,
)
from .utils import get_user_id

# Load .env so GOOGLE_OAUTH_CREDENTIALS is available via os.environ
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)

PROVIDER = "google_docs"
_CACHE_FILENAME = "google_docs.json"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


# ---------------------------------------------------------------------------
# Backwards-compatible token cache wrappers (used by google_docs.py)
# ---------------------------------------------------------------------------

def load_token_cache(user_id: str) -> dict:
    return auth_utils.load_token_cache(user_id, _CACHE_FILENAME)


def save_token_cache(user_id: str, cache: dict) -> None:
    auth_utils.save_token_cache(user_id, _CACHE_FILENAME, cache)


def _persist_or_delete_cache(user_id: str, cache: dict) -> None:
    if cache:
        save_token_cache(user_id, cache)
    else:
        auth_utils.delete_token_cache(user_id, _CACHE_FILENAME)


def _existing_auth_message(user_id: str) -> Optional[str]:
    """Return an already-authenticated message, pruning dead tokens first."""
    cache = load_token_cache(user_id)
    accounts = cache.get("accounts", {})
    if not accounts:
        return None

    required_scopes = set(GOOGLE_SCOPES)
    changed = False
    for account_id, account in list(accounts.items()):
        email = account.get("email", "unknown")
        has_refresh = bool(account.get("refresh_token"))
        saved_scopes = set(account.get("scopes", []))
        scopes_match = saved_scopes >= required_scopes

        if not has_refresh:
            continue
        if not scopes_match:
            missing = required_scopes - saved_scopes
            logger.info(
                "Google Docs scope change detected for %s. Missing scopes: %s. "
                "Proceeding with re-auth.",
                email,
                missing,
            )
            continue

        expires_at = account.get("expires_at", 0)
        if time.time() >= expires_at - 60:
            status, reason = auth_utils.refresh_google_account(account, GOOGLE_SCOPES)
            if status == "refreshed":
                accounts[account_id] = account
                changed = True
            elif status == "invalid":
                logger.info(
                    "Clearing invalid Google Docs token for %s: %s",
                    email,
                    reason,
                )
                del accounts[account_id]
                changed = True
                continue
            else:
                if changed:
                    cache["accounts"] = accounts
                    _persist_or_delete_cache(user_id, cache)
                return (
                    f"[Warning]: Found expired Google Docs credentials for "
                    f"**{account.get('name', 'Unknown')}** ({email}), but could not "
                    f"verify the refresh token: {reason}\n\n"
                    "Use `google_docs_auth_clear` to remove the saved token, then run "
                    "`google_docs_auth_start` again."
                )

        if changed:
            cache["accounts"] = accounts
            _persist_or_delete_cache(user_id, cache)

        return (
            f"[Info]: Already authenticated as **{account.get('name', 'Unknown')}** ({email}). "
            "Tokens will auto-refresh. To add another account or re-authenticate, "
            "call `google_docs_auth_clear` first."
        )

    if changed:
        if accounts:
            cache["accounts"] = accounts
        else:
            cache.pop("accounts", None)
        _persist_or_delete_cache(user_id, cache)

    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def google_docs_auth_start(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    Start Google Docs authentication using the OAuth 2.0 authorization code flow.

    Generates an authorization URL for the user to open in their browser.
    After signing in and granting access, Google redirects to localhost and
    the auth code is captured automatically. Call google_docs_auth_complete to
    finish the flow.

    Requires the GOOGLE_OAUTH_CREDENTIALS environment variable to point to a
    Google OAuth Desktop App credentials JSON file from Google Cloud Console.
    """
    user_id = get_user_id(config)

    existing_message = _existing_auth_message(user_id)
    if existing_message:
        return existing_message

    existing = get_flow(user_id, PROVIDER)
    if existing and existing.server_thread and existing.server_thread.is_alive() and not existing.completed:
        elapsed = time.time() - existing.started_at
        remaining = max(0, OAUTH_TIMEOUT_SECONDS - elapsed)
        return (
            f"[Info]: Authentication is already in progress!\n\n"
            f"Open the authorization URL in your browser (if you haven't already), "
            f"then call google_docs_auth_complete.\n\n"
            f"The session will expire in about {int(remaining / 60)} minutes."
        )

    creds_path = get_google_credentials_path()
    if not creds_path:
        return (
            "[Error]: GOOGLE_OAUTH_CREDENTIALS environment variable is not set or the file was not found.\n\n"
            "**Setup instructions:**\n"
            "1. Go to https://console.cloud.google.com\n"
            "2. Create a project (or select existing)\n"
            "3. Enable the **Google Docs API** under APIs & Services > Library\n"
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
        flow = OAuthFlow(
            user_id=user_id,
            provider=PROVIDER,
            state_param=secrets.token_urlsafe(16),
            client_id=client_id,
            client_secret=client_secret,
            token_uri=token_uri,
            redirect_uri="",
            started_at=time.time(),
        )
        register_flow(flow)
        port = start_callback_server(flow)
        flow.redirect_uri = f"http://localhost:{port}"

        params = {
            "client_id": client_id,
            "redirect_uri": flow.redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": flow.state_param,
        }
        auth_url = auth_uri + "?" + urllib.parse.urlencode(params)

        return (
            f"[Success]: Authorization URL generated.\n\n"
            f"**Open this URL in your browser to sign in:**\n\n"
            f"{auth_url}\n\n"
            f"After signing in and granting access, the page should redirect automatically "
            f"and show a success message.\n\n"
            f"Then call `google_docs_auth_complete` to finish.\n\n"
            f"**If the redirect page doesn't load** (e.g. Docker/remote), copy the full URL "
            f"from your browser's address bar and give it to me — I'll extract the auth code from it."
        )

    except KeyError as e:
        return f"[Error]: Credentials file is missing required field: {e}"
    except json.JSONDecodeError:
        return "[Error]: Credentials file is not valid JSON."
    except Exception as e:
        logger.error(f"google_docs_auth_start failed: {e}", exc_info=True)
        return f"[Error]: Failed to start authentication: {str(e)}"


@tool
def google_docs_auth_clear(
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Clear saved Google Docs/Drive/Sheets authentication for the current user.

    Args:
        account_id: Optional account ID to remove. When omitted, all saved
            Google Docs/Drive/Sheets accounts and any pending OAuth flow are
            cleared for the current user.
    """
    user_id = get_user_id(config)
    cache = load_token_cache(user_id)
    accounts = cache.get("accounts", {})

    clear_flow(user_id, PROVIDER)

    if account_id:
        account = accounts.pop(account_id, None)
        if account is None:
            return (
                f"[Info]: No Google Docs account found with ID `{account_id}`. "
                "Use google_docs_list_accounts to see saved accounts."
            )

        email = account.get("email", "unknown")
        name = account.get("name", "Unknown")
        if accounts:
            cache["accounts"] = accounts
        else:
            cache.pop("accounts", None)
        _persist_or_delete_cache(user_id, cache)
        return (
            f"[Success]: Cleared Google Docs/Drive/Sheets authentication for "
            f"**{name}** ({email}). Run `google_docs_auth_start` to authenticate again."
        )

    removed_count = len(accounts)
    cache.pop("accounts", None)
    _persist_or_delete_cache(user_id, cache)

    if removed_count:
        return (
            f"[Success]: Cleared {removed_count} saved Google Docs/Drive/Sheets account(s) "
            "and any pending Google Docs OAuth flow. Run `google_docs_auth_start` to authenticate again."
        )

    return (
        "[Info]: No saved Google Docs/Drive/Sheets authentication was present. "
        "Any pending Google Docs OAuth flow was cleared."
    )


@tool
def google_docs_auth_complete(redirect_url: Optional[str] = None, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    Complete Google Docs authentication.

    Call this after opening the authorization URL from google_docs_auth_start
    in your browser and signing in. If the localhost redirect didn't work
    (Docker, remote server, etc.), pass the full URL from your browser's
    address bar as ``redirect_url``.
    """
    user_id = get_user_id(config)
    flow = get_flow(user_id, PROVIDER)
    if flow is None:
        return "[Error]: No pending authentication. Please call google_docs_auth_start first."

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
            return "[Error]: State parameter mismatch — the URL may be from a different auth session. Please call google_docs_auth_start to begin again."

        success, token_data = exchange_code_for_tokens(
            auth_code, flow.client_id, flow.client_secret, flow.redirect_uri, flow.token_uri
        )
        if not success:
            return f"[Error]: {token_data}"

        account_id, email, name = save_google_account(
            user_id, _CACHE_FILENAME, token_data,
            flow.client_id, flow.client_secret, flow.token_uri, GOOGLE_SCOPES,
        )
        clear_flow(user_id, PROVIDER)
        return (
            f"[Success]: Google Docs authentication complete!\n\n"
            f"**Account:** {name} ({email})\n"
            f"**Account ID:** {account_id}\n\n"
            f"You can now use the Google Docs tools."
        )

    server_alive = flow.server_thread is not None and flow.server_thread.is_alive()
    if not flow.completed and server_alive:
        elapsed = int(time.time() - flow.started_at)
        remaining = max(0, OAUTH_TIMEOUT_SECONDS - elapsed)
        return (
            f"[Info]: Still waiting for browser redirect ({elapsed}s elapsed, "
            f"{remaining}s remaining).\n\n"
            f"Open the URL in your browser if you haven't yet.\n\n"
            f"If the redirect page didn't load, copy the full URL from your "
            f"browser's address bar and call this tool again with `redirect_url`."
        )

    if flow.auth_error == "timeout":
        clear_flow(user_id, PROVIDER)
        return "[Error]: Authentication timed out (5 minutes). Please call google_docs_auth_start to begin again."

    if flow.auth_error:
        err = flow.auth_error
        clear_flow(user_id, PROVIDER)
        return f"[Error]: Authentication failed: {err}. Please call google_docs_auth_start to try again."

    if not flow.auth_code:
        clear_flow(user_id, PROVIDER)
        return "[Error]: Authentication completed but no code was received. Please call google_docs_auth_start to try again."

    success, token_data = exchange_code_for_tokens(
        flow.auth_code, flow.client_id, flow.client_secret, flow.redirect_uri, flow.token_uri
    )
    if not success:
        clear_flow(user_id, PROVIDER)
        return f"[Error]: {token_data}"

    account_id, email, name = save_google_account(
        user_id, _CACHE_FILENAME, token_data,
        flow.client_id, flow.client_secret, flow.token_uri, GOOGLE_SCOPES,
    )
    clear_flow(user_id, PROVIDER)
    return (
        f"[Success]: Google Docs authentication complete!\n\n"
        f"**Account:** {name} ({email})\n"
        f"**Account ID:** {account_id}\n\n"
        f"You can now use the Google Docs tools."
    )


@tool
def google_docs_list_accounts(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """
    List all authenticated Google accounts for Docs access.

    Shows account email, display name, and token status.
    """
    user_id = get_user_id(config)
    cache = load_token_cache(user_id)
    accounts = cache.get("accounts", {})

    if not accounts:
        return "[Info]: No Google accounts authenticated for Docs. Use google_docs_auth_start to add an account."

    lines = ["[Success]: Authenticated Google accounts (Docs):\n"]
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


GOOGLE_DOCS_AUTH_TOOLS = [
    google_docs_auth_start,
    google_docs_auth_complete,
    google_docs_auth_clear,
    google_docs_list_accounts,
]
