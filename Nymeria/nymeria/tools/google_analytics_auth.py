"""Google Analytics OAuth 2.0 authentication tools."""

from dotenv import load_dotenv

from nymeria.config.settings import get_env_file_paths

from . import auth_cache_utils as auth_utils

for _ENV_PATH in get_env_file_paths():
    try:
        load_dotenv(_ENV_PATH)
    except UnicodeDecodeError:
        pass  # Ignore unreadable env files; other configured env paths may still load.

PROVIDER = "google_analytics"
_CACHE_FILENAME = "google_analytics.json"

GOOGLE_ANALYTICS_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def load_token_cache(user_id: str) -> dict:
    """Load this user's Google Analytics token cache."""
    return auth_utils.load_token_cache(user_id, _CACHE_FILENAME)


def save_token_cache(user_id: str, cache: dict) -> None:
    """Persist this user's Google Analytics token cache."""
    auth_utils.save_token_cache(user_id, _CACHE_FILENAME, cache)


_TOOLS = auth_utils.create_google_oauth_tools(
    auth_utils.GoogleOAuthToolSpec(
        provider=PROVIDER,
        cache_filename=_CACHE_FILENAME,
        scopes=GOOGLE_ANALYTICS_SCOPES,
        service_display_name="Google Analytics",
        setup_api_name="Google Analytics API",
        usable_tools_label="Google Analytics tools",
        start_tool_name="google_analytics_auth_start",
        complete_tool_name="google_analytics_auth_complete",
        clear_tool_name="google_analytics_auth_clear",
        list_tool_name="google_analytics_list_accounts",
        no_accounts_message=(
            "[Info]: No Google accounts authenticated for Analytics. "
            "Use google_analytics_auth_start to add an account."
        ),
        no_usable_accounts_message=(
            "[Info]: No usable Google accounts authenticated for Analytics. "
            "Use google_analytics_auth_start to add an account."
        ),
        list_heading="[Success]: Authenticated Google accounts (Analytics):\n",
    )
)

google_analytics_auth_start = _TOOLS[0]
google_analytics_auth_complete = _TOOLS[1]
google_analytics_auth_clear = _TOOLS[2]
google_analytics_list_accounts = _TOOLS[3]

GOOGLE_ANALYTICS_AUTH_TOOLS = [
    google_analytics_auth_start,
    google_analytics_auth_complete,
    google_analytics_auth_clear,
    google_analytics_list_accounts,
]
