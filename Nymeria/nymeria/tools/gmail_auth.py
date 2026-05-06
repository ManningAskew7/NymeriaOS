"""Google Gmail OAuth 2.0 authentication tools.

This provider exists mainly for Gmail MCP servers that need a google-auth
compatible credentials file. Tokens are stored in Nymeria's normal per-user
auth cache and exported to the MCP credential path after successful auth.
"""

from pathlib import Path

from dotenv import load_dotenv

from . import auth_cache_utils as auth_utils

# Load .env so GOOGLE_OAUTH_CREDENTIALS is available via os.environ
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)

PROVIDER = "google_gmail"
_CACHE_FILENAME = "google_gmail.json"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def load_token_cache(user_id: str) -> dict:
    """Load this user's Google Gmail token cache."""
    return auth_utils.load_token_cache(user_id, _CACHE_FILENAME)


def save_token_cache(user_id: str, cache: dict) -> None:
    """Persist this user's Google Gmail token cache."""
    auth_utils.save_token_cache(user_id, _CACHE_FILENAME, cache)


def _export_gmail_mcp_credentials(user_id: str, account_id: str) -> str:
    from ..core.mcp_auth_bridge import export_google_account_for_gmail_mcp

    export_path = export_google_account_for_gmail_mcp(
        user_id,
        account_id=account_id,
        cache_filename=_CACHE_FILENAME,
    )
    if export_path is None:
        return (
            "[Warning]: Gmail auth was saved, but no MCP credential file was "
            "exported. Re-run gmail_list_accounts and check token scope status."
        )
    return f"[Info]: Gmail MCP credentials exported to `{export_path}`."


def _clear_gmail_mcp_credentials(user_id: str, _account_id: str | None = None) -> str | None:
    from ..core.mcp_auth_bridge import gmail_mcp_credentials_path

    path = gmail_mcp_credentials_path(user_id)
    if not path.exists():
        return None
    path.unlink()
    return f"[Info]: Removed exported Gmail MCP credentials at `{path}`."


_TOOLS = auth_utils.create_google_oauth_tools(
    auth_utils.GoogleOAuthToolSpec(
        provider=PROVIDER,
        cache_filename=_CACHE_FILENAME,
        scopes=GOOGLE_SCOPES,
        service_display_name="Google Gmail",
        setup_api_name="Gmail API",
        usable_tools_label="Gmail MCP auth bridge",
        start_tool_name="gmail_auth_start",
        complete_tool_name="gmail_auth_complete",
        clear_tool_name="gmail_auth_clear",
        list_tool_name="gmail_list_accounts",
        no_accounts_message=(
            "[Info]: No Google accounts authenticated for Gmail. "
            "Use gmail_auth_start to add an account."
        ),
        no_usable_accounts_message=(
            "[Info]: No usable Google accounts authenticated for Gmail. "
            "Use gmail_auth_start to add an account."
        ),
        list_heading="[Success]: Authenticated Google accounts (Gmail):\n",
        post_save_hook=_export_gmail_mcp_credentials,
        post_clear_hook=_clear_gmail_mcp_credentials,
    )
)

gmail_auth_start = _TOOLS[0]
gmail_auth_complete = _TOOLS[1]
gmail_auth_clear = _TOOLS[2]
gmail_list_accounts = _TOOLS[3]

GMAIL_AUTH_TOOLS = [
    gmail_auth_start,
    gmail_auth_complete,
    gmail_auth_clear,
    gmail_list_accounts,
]
