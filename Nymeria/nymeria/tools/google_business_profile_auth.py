"""Google Business Profile OAuth 2.0 authentication tools."""

from dotenv import load_dotenv

from nymeria.config.settings import get_env_file_paths

from . import auth_cache_utils as auth_utils

for _ENV_PATH in get_env_file_paths():
    try:
        load_dotenv(_ENV_PATH)
    except UnicodeDecodeError:
        pass  # Ignore unreadable env files; other configured env paths may still load.

PROVIDER = "google_business_profile"
_CACHE_FILENAME = "google_business_profile.json"

GOOGLE_BUSINESS_PROFILE_SCOPES = [
    "https://www.googleapis.com/auth/business.manage",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def load_token_cache(user_id: str) -> dict:
    """Load this user's Google Business Profile token cache."""
    return auth_utils.load_token_cache(user_id, _CACHE_FILENAME)


def save_token_cache(user_id: str, cache: dict) -> None:
    """Persist this user's Google Business Profile token cache."""
    auth_utils.save_token_cache(user_id, _CACHE_FILENAME, cache)


_TOOLS = auth_utils.create_google_oauth_tools(
    auth_utils.GoogleOAuthToolSpec(
        provider=PROVIDER,
        cache_filename=_CACHE_FILENAME,
        scopes=GOOGLE_BUSINESS_PROFILE_SCOPES,
        service_display_name="Google Business Profile",
        setup_api_name="Google Business Profile API",
        usable_tools_label="Google Business Profile tools",
        start_tool_name="google_business_profile_auth_start",
        complete_tool_name="google_business_profile_auth_complete",
        clear_tool_name="google_business_profile_auth_clear",
        list_tool_name="google_business_profile_list_accounts",
        no_accounts_message=(
            "[Info]: No Google accounts authenticated for Business Profile. "
            "Use google_business_profile_auth_start to add an account."
        ),
        no_usable_accounts_message=(
            "[Info]: No usable Google accounts authenticated for Business Profile. "
            "Use google_business_profile_auth_start to add an account."
        ),
        list_heading="[Success]: Authenticated Google accounts (Business Profile):\n",
    )
)

google_business_profile_auth_start = _TOOLS[0]
google_business_profile_auth_complete = _TOOLS[1]
google_business_profile_auth_clear = _TOOLS[2]
google_business_profile_list_accounts = _TOOLS[3]

GOOGLE_BUSINESS_PROFILE_AUTH_TOOLS = [
    google_business_profile_auth_start,
    google_business_profile_auth_complete,
    google_business_profile_auth_clear,
    google_business_profile_list_accounts,
]
