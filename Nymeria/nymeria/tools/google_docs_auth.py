"""Google Docs / Drive / Sheets OAuth 2.0 authentication tools.

Per-user OAuth flow, callback handling, token cache I/O, refresh validation,
and tool generation live in ``auth_cache_utils``. This module wires the
Docs/Drive/Sheets-specific provider name, cache file, scopes, and user-facing
tool names.

Token cache is per-user at ``data/auth_tokens/<user_id>/google_docs.json``
and tokens are auto-refreshed on expiry (60-second buffer) by ``google_docs.py``.

Optional tools, enable per-thread via thread config.
"""

from pathlib import Path

from dotenv import load_dotenv

from . import auth_cache_utils as auth_utils

# Load .env so GOOGLE_OAUTH_CREDENTIALS is available via os.environ
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)

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


def load_token_cache(user_id: str) -> dict:
    """Load this user's Google Docs/Drive/Sheets token cache."""
    return auth_utils.load_token_cache(user_id, _CACHE_FILENAME)


def save_token_cache(user_id: str, cache: dict) -> None:
    """Persist this user's Google Docs/Drive/Sheets token cache."""
    auth_utils.save_token_cache(user_id, _CACHE_FILENAME, cache)


_TOOLS = auth_utils.create_google_oauth_tools(
    auth_utils.GoogleOAuthToolSpec(
        provider=PROVIDER,
        cache_filename=_CACHE_FILENAME,
        scopes=GOOGLE_SCOPES,
        service_display_name="Google Docs",
        setup_api_name="Google Docs API",
        usable_tools_label="Google Docs tools",
        start_tool_name="google_docs_auth_start",
        complete_tool_name="google_docs_auth_complete",
        clear_tool_name="google_docs_auth_clear",
        list_tool_name="google_docs_list_accounts",
        no_accounts_message=(
            "[Info]: No Google accounts authenticated for Docs. "
            "Use google_docs_auth_start to add an account."
        ),
        no_usable_accounts_message=(
            "[Info]: No usable Google accounts authenticated for Docs. "
            "Use google_docs_auth_start to add an account."
        ),
        list_heading="[Success]: Authenticated Google accounts (Docs):\n",
    )
)

google_docs_auth_start = _TOOLS[0]
google_docs_auth_complete = _TOOLS[1]
google_docs_auth_clear = _TOOLS[2]
google_docs_list_accounts = _TOOLS[3]

GOOGLE_DOCS_AUTH_TOOLS = [
    google_docs_auth_start,
    google_docs_auth_complete,
    google_docs_auth_clear,
    google_docs_list_accounts,
]
