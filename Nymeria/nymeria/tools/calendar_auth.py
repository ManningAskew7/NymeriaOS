"""Google Calendar OAuth 2.0 authentication tools.

Per-user OAuth flow, callback handling, token cache I/O, refresh validation,
and tool generation live in ``auth_cache_utils``. This module wires the
Google Calendar-specific provider name, cache file, scopes, and user-facing
tool names.

Token cache is per-user at ``data/auth_tokens/<user_id>/google_calendar.json``
and tokens are auto-refreshed on expiry (60-second buffer) by ``calendar.py``.

Optional tools, enable per-thread via thread config.
"""

from pathlib import Path

from dotenv import load_dotenv

from . import auth_cache_utils as auth_utils

# Load .env so GOOGLE_OAUTH_CREDENTIALS is available via os.environ
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)

PROVIDER = "google_calendar"
_CACHE_FILENAME = "google_calendar.json"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def load_token_cache(user_id: str) -> dict:
    """Load this user's Google Calendar token cache."""
    return auth_utils.load_token_cache(user_id, _CACHE_FILENAME)


def save_token_cache(user_id: str, cache: dict) -> None:
    """Persist this user's Google Calendar token cache."""
    auth_utils.save_token_cache(user_id, _CACHE_FILENAME, cache)


_TOOLS = auth_utils.create_google_oauth_tools(
    auth_utils.GoogleOAuthToolSpec(
        provider=PROVIDER,
        cache_filename=_CACHE_FILENAME,
        scopes=GOOGLE_SCOPES,
        service_display_name="Google Calendar",
        setup_api_name="Google Calendar API",
        usable_tools_label="calendar tools",
        start_tool_name="calendar_auth_start",
        complete_tool_name="calendar_auth_complete",
        clear_tool_name="calendar_auth_clear",
        list_tool_name="calendar_list_authenticated_accounts",
        no_accounts_message=(
            "[Info]: No Google accounts authenticated. "
            "Use calendar_auth_start to add an account."
        ),
        no_usable_accounts_message=(
            "[Info]: No usable Google Calendar accounts authenticated. "
            "Use calendar_auth_start to add an account."
        ),
        list_heading="[Success]: Authenticated Google accounts:\n",
    )
)

calendar_auth_start = _TOOLS[0]
calendar_auth_complete = _TOOLS[1]
calendar_auth_clear = _TOOLS[2]
calendar_list_authenticated_accounts = _TOOLS[3]

CALENDAR_AUTH_TOOLS = [
    calendar_auth_start,
    calendar_auth_complete,
    calendar_auth_clear,
    calendar_list_authenticated_accounts,
]
