"""Direct Microsoft Graph authentication tool.

Bypasses MCP to handle the device code authentication flow directly,
avoiding the type validation issues in the microsoft-mcp server.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Microsoft OAuth endpoints
AUTHORITY = "https://login.microsoftonline.com/common"
DEVICE_CODE_URL = f"{AUTHORITY}/oauth2/v2.0/devicecode"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"

# Scopes for Outlook/Graph API
SCOPES = [
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Contacts.Read",
    "ChannelMessage.Send",
]

# Token cache location (same as microsoft-mcp uses)
TOKEN_CACHE_PATH = Path.home() / ".microsoft_mcp_token_cache.json"


def get_client_id() -> str:
    """Get the Microsoft app client ID from environment."""
    client_id = os.environ.get("MICROSOFT_MCP_CLIENT_ID")
    if not client_id:
        # Fallback to the one configured in the tools
        client_id = "8ad36cab-9646-40ee-97f5-0ddd7cd6e5c8"
    return client_id


def load_token_cache() -> dict:
    """Load token cache from file."""
    if TOKEN_CACHE_PATH.exists():
        try:
            return json.loads(TOKEN_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def save_token_cache(cache: dict) -> None:
    """Save token cache to file."""
    TOKEN_CACHE_PATH.write_text(json.dumps(cache, indent=2))


@tool
def outlook_auth_start() -> str:
    """
    Start Microsoft account authentication using device code flow.

    Returns a device code and URL that the user must visit to complete authentication.
    After the user signs in, call outlook_auth_complete with the device_code to get tokens.

    Returns:
        Instructions with the device code and URL for the user to visit.
    """
    # Check if there's already a pending auth
    cache = load_token_cache()
    pending = cache.get("pending_auth")

    if pending:
        expires_at = pending.get("expires_at", 0)
        if time.time() < expires_at:
            # There's a valid pending auth - try to complete it first
            # (in case user already signed in)
            result = _try_complete_pending()
            if result:
                return result  # Auth completed successfully!

            # Still pending - remind user about the existing code
            remaining = int((expires_at - time.time()) / 60)
            return f"""[Info]: Authentication is already in progress!

If you've already signed in at Microsoft, call `outlook_auth_complete` now.

If not, please complete sign-in first, then call `outlook_auth_complete`.

The current authentication will expire in about {remaining} minutes.

**To start fresh with a new code**, first wait for this one to expire or delete the pending auth."""

    client_id = get_client_id()

    try:
        response = httpx.post(
            DEVICE_CODE_URL,
            data={
                "client_id": client_id,
                "scope": " ".join(SCOPES),
            },
            timeout=30,
        )

        if response.status_code != 200:
            return f"[Error]: Failed to start authentication: {response.text}"

        data = response.json()

        device_code = data.get("device_code", "")
        user_code = data.get("user_code", "")
        verification_uri = data.get("verification_uri", "https://microsoft.com/devicelogin")
        expires_in = data.get("expires_in", 900)
        interval = data.get("interval", 5)

        # Store device code info for completion
        cache = load_token_cache()
        cache["pending_auth"] = {
            "device_code": device_code,
            "user_code": user_code,  # Also store user code for reference
            "interval": interval,
            "expires_at": time.time() + expires_in,
        }
        save_token_cache(cache)

        return f"""[Success]: Authentication started!

**Instructions:**
1. Go to: {verification_uri}
2. Enter code: **{user_code}**
3. Sign in with your Microsoft account
4. After signing in, tell me "authentication complete" or call outlook_auth_complete

The code expires in {expires_in // 60} minutes."""

    except Exception as e:
        logger.error(f"Auth start failed: {e}", exc_info=True)
        return f"[Error]: Failed to start authentication: {str(e)}"


def _try_complete_pending() -> Optional[str]:
    """
    Try to complete pending auth (non-blocking single attempt).

    Returns success message if auth completed, None if still pending.
    """
    client_id = get_client_id()
    cache = load_token_cache()
    pending = cache.get("pending_auth")

    if not pending:
        return None

    device_code = pending.get("device_code")
    if not device_code:
        return None

    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)

            # Get user info
            user_info = get_user_info(access_token)
            email = user_info.get("mail") or user_info.get("userPrincipalName", "unknown")
            name = user_info.get("displayName", "Unknown User")

            # Save to cache
            account_id = email.lower().replace("@", "_at_").replace(".", "_")
            cache["accounts"] = cache.get("accounts", {})
            cache["accounts"][account_id] = {
                "email": email,
                "name": name,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
            }

            # Clear pending auth
            if "pending_auth" in cache:
                del cache["pending_auth"]

            save_token_cache(cache)

            return f"""[Success]: Authentication complete!

**Account:** {name} ({email})
**Account ID:** {account_id}

You can now use the Outlook tools."""

    except Exception as e:
        logger.debug(f"Pending auth check failed: {e}")

    return None


@tool
def outlook_auth_complete() -> str:
    """
    Complete the Microsoft authentication after the user has signed in.

    Call this after the user has visited the URL and entered the device code.
    This will poll Microsoft to check if authentication is complete and save the tokens.

    Returns:
        Success message with account info, or error if authentication failed.
    """
    client_id = get_client_id()
    cache = load_token_cache()

    pending = cache.get("pending_auth")
    if not pending:
        return "[Error]: No pending authentication. Please call outlook_auth_start first."

    device_code = pending.get("device_code")
    interval = pending.get("interval", 5)
    expires_at = pending.get("expires_at", 0)

    if time.time() > expires_at:
        del cache["pending_auth"]
        save_token_cache(cache)
        return "[Error]: Authentication expired. Please call outlook_auth_start to begin again."

    # Poll for token
    max_attempts = 60  # 5 minutes with 5 second intervals
    attempt = 0

    while attempt < max_attempts:
        try:
            response = httpx.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
                timeout=30,
            )

            data = response.json()

            if response.status_code == 200:
                # Success! Save tokens
                access_token = data.get("access_token")
                refresh_token = data.get("refresh_token")
                expires_in = data.get("expires_in", 3600)

                # Get user info
                user_info = get_user_info(access_token)
                email = user_info.get("mail") or user_info.get("userPrincipalName", "unknown")
                name = user_info.get("displayName", "Unknown User")

                # Save to cache
                account_id = email.lower().replace("@", "_at_").replace(".", "_")
                cache["accounts"] = cache.get("accounts", {})
                cache["accounts"][account_id] = {
                    "email": email,
                    "name": name,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": time.time() + expires_in,
                }

                # Clear pending auth
                if "pending_auth" in cache:
                    del cache["pending_auth"]

                save_token_cache(cache)

                return f"""[Success]: Authentication complete!

**Account:** {name} ({email})
**Account ID:** {account_id}

You can now use the Outlook tools. Use this account_id when calling other Outlook tools."""

            error = data.get("error", "")

            if error == "authorization_pending":
                # User hasn't completed auth yet, wait and retry
                attempt += 1
                time.sleep(interval)
                continue
            elif error == "slow_down":
                # Rate limited, increase interval
                interval += 5
                attempt += 1
                time.sleep(interval)
                continue
            elif error == "expired_token":
                if "pending_auth" in cache:
                    del cache["pending_auth"]
                save_token_cache(cache)
                return "[Error]: Authentication expired. Please call outlook_auth_start to begin again."
            elif error == "access_denied":
                if "pending_auth" in cache:
                    del cache["pending_auth"]
                save_token_cache(cache)
                return "[Error]: Authentication was denied. The user may have declined the consent."
            else:
                return f"[Error]: Authentication failed: {error} - {data.get('error_description', '')}"

        except Exception as e:
            logger.error(f"Auth poll failed: {e}", exc_info=True)
            attempt += 1
            time.sleep(interval)

    return "[Error]: Authentication timed out waiting for user to complete sign-in. Please try again."


def get_user_info(access_token: str) -> dict:
    """Get user profile information from Graph API."""
    try:
        response = httpx.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return {}


@tool
def outlook_list_authenticated_accounts() -> str:
    """
    List all authenticated Microsoft accounts.

    Returns:
        List of authenticated accounts with their IDs and email addresses.
    """
    cache = load_token_cache()
    accounts = cache.get("accounts", {})

    if not accounts:
        return "[Info]: No Microsoft accounts authenticated. Use outlook_auth_start to add an account."

    lines = ["[Success]: Authenticated Microsoft accounts:\n"]
    for account_id, info in accounts.items():
        email = info.get("email", "unknown")
        name = info.get("name", "Unknown")
        expires_at = info.get("expires_at", 0)
        expired = time.time() > expires_at
        status = "(token expired, will refresh)" if expired else "(active)"
        lines.append(f"- **{name}** ({email})")
        lines.append(f"  Account ID: `{account_id}` {status}")

    return "\n".join(lines)


# Export tools
AUTH_TOOLS = [
    outlook_auth_start,
    outlook_auth_complete,
    outlook_list_authenticated_accounts,
]
