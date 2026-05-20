"""Provider descriptor registry for OAuth-based credentials.

Mirrors the shape of ``config/llm_providers.py``. Each entry describes one
OAuth provider that the unified ``request_credential(kind="oauth", ...)``
flow can authenticate against. Constants here used to be spread across the
legacy ``tools/*_auth.py`` files; they now live in one place so:

- ``credential_prompt`` can look up auth endpoints and scopes by provider id,
- the OAuth callback handler can resolve client config and post-save hooks,
- the descriptor flag set (``supported_flows``) decides whether the agent
  may auto-degrade from auth-code to device-code when no ``NYMERIA_PUBLIC_URL``
  is configured.

Two flows are supported per descriptor: ``auth_code`` (browser redirect to
the hosted-form callback URL) and ``device_code`` (RFC 8628 — user opens a
verification URL on any device and enters a short code; backend polls the
token endpoint). Providers may advertise one or both. ``auth_code`` requires
either ``NYMERIA_PUBLIC_URL`` or the agent passing ``use_localhost=True``;
``device_code`` works without either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional


OAuthFlow = Literal["auth_code", "device_code"]


_GOOGLE_USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"
_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

_MICROSOFT_AUTHORITY = "https://login.microsoftonline.com/common"
_MICROSOFT_AUTH_URI = f"{_MICROSOFT_AUTHORITY}/oauth2/v2.0/authorize"
_MICROSOFT_TOKEN_URI = f"{_MICROSOFT_AUTHORITY}/oauth2/v2.0/token"
_MICROSOFT_DEVICE_CODE_URI = f"{_MICROSOFT_AUTHORITY}/oauth2/v2.0/devicecode"
_MICROSOFT_USERINFO_URI = "https://graph.microsoft.com/v1.0/me"


@dataclass(frozen=True)
class OAuthProviderDescriptor:
    """Runtime metadata for one OAuth provider."""

    provider_id: str
    display_name: str
    auth_uri: str
    token_uri: str
    userinfo_uri: str
    scopes: tuple[str, ...]
    supported_flows: frozenset[OAuthFlow]
    default_flow: OAuthFlow
    # Where to find the OAuth *client* config (client_id / client_secret).
    # Either both env vars are set, or ``client_config_file_env`` points to
    # a JSON file (Google's "installed app" / "web" credential file).
    client_id_env: Optional[str] = None
    client_secret_env: Optional[str] = None
    client_config_file_env: Optional[str] = None
    # Hardcoded fallback for ``client_id`` when ``client_id_env`` is unset or
    # empty. Use only for *public* client IDs (e.g. Microsoft's `8ad36cab...`
    # Graph sample app) where the same value ships with the codebase. Never
    # provide a fallback for a client secret.
    client_id_fallback: Optional[str] = None
    # Device-code endpoint (RFC 8628 §3.1). Required when "device_code" is in
    # supported_flows, otherwise ignored.
    device_authorization_uri: Optional[str] = None
    uses_pkce: bool = False
    # Extra query parameters to merge into the authorize URL (e.g. Google's
    # ``access_type=offline`` and ``prompt=consent`` to force a refresh token).
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    account_id_strategy: Literal["email", "userinfo_sub"] = "email"
    # Optional callback fired *after* the token is written to the vault
    # (e.g. gmail's MCP credential export). Receives (user_id, account_id)
    # and may return a user-facing message to append to the success result.
    post_save_hook: Optional[Callable[[str, str], Optional[str]]] = None
    # Optional callback fired when a user disconnects this provider's account
    # (mirror of post_save_hook for cleanup).
    post_clear_hook: Optional[Callable[[str, Optional[str]], Optional[str]]] = None
    # Human-readable label for documentation and the modal's scope list.
    notes: str = ""


# ---------------------------------------------------------------------------
# Scope sets (kept here so the legacy *_auth.py files can be deleted)
# ---------------------------------------------------------------------------

GOOGLE_CALENDAR_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

GOOGLE_GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

GOOGLE_DOCS_CORE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

GOOGLE_WORKSPACE_EXTRA_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.memberships",
)

GOOGLE_DOCS_SCOPES: tuple[str, ...] = GOOGLE_DOCS_CORE_SCOPES + GOOGLE_WORKSPACE_EXTRA_SCOPES

GOOGLE_ANALYTICS_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

GOOGLE_BUSINESS_PROFILE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/business.manage",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

OUTLOOK_SCOPES: tuple[str, ...] = (
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Contacts.Read",
    "ChannelMessage.Send",
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_GOOGLE_AUTH_CODE_EXTRA = {
    "access_type": "offline",  # Force refresh token issuance.
    "prompt": "consent",       # Re-prompt so consent matches scope changes.
}

_AUTH_CODE_ONLY: frozenset[OAuthFlow] = frozenset({"auth_code"})
_BOTH_FLOWS: frozenset[OAuthFlow] = frozenset({"auth_code", "device_code"})


OAUTH_PROVIDERS: dict[str, OAuthProviderDescriptor] = {
    "google_calendar": OAuthProviderDescriptor(
        provider_id="google_calendar",
        display_name="Google Calendar",
        auth_uri=_GOOGLE_AUTH_URI,
        token_uri=_GOOGLE_TOKEN_URI,
        userinfo_uri=_GOOGLE_USERINFO_URI,
        scopes=GOOGLE_CALENDAR_SCOPES,
        supported_flows=_AUTH_CODE_ONLY,
        default_flow="auth_code",
        client_config_file_env="GOOGLE_OAUTH_CREDENTIALS",
        uses_pkce=True,
        extra_authorize_params=_GOOGLE_AUTH_CODE_EXTRA,
        notes="Calendar read/write only. Other Google services (Gmail, Docs, Analytics, GBP) are separate descriptors.",
    ),
    "google_gmail": OAuthProviderDescriptor(
        provider_id="google_gmail",
        display_name="Google Gmail",
        auth_uri=_GOOGLE_AUTH_URI,
        token_uri=_GOOGLE_TOKEN_URI,
        userinfo_uri=_GOOGLE_USERINFO_URI,
        scopes=GOOGLE_GMAIL_SCOPES,
        supported_flows=_AUTH_CODE_ONLY,
        default_flow="auth_code",
        client_config_file_env="GOOGLE_OAUTH_CREDENTIALS",
        uses_pkce=True,
        extra_authorize_params=_GOOGLE_AUTH_CODE_EXTRA,
        notes="Gmail bridge used by the gmail MCP server. Tokens are also exported to the MCP credential file via post_save_hook.",
    ),
    "google_docs": OAuthProviderDescriptor(
        provider_id="google_docs",
        display_name="Google Workspace",
        auth_uri=_GOOGLE_AUTH_URI,
        token_uri=_GOOGLE_TOKEN_URI,
        userinfo_uri=_GOOGLE_USERINFO_URI,
        scopes=GOOGLE_DOCS_SCOPES,
        supported_flows=_AUTH_CODE_ONLY,
        default_flow="auth_code",
        client_config_file_env="GOOGLE_OAUTH_CREDENTIALS",
        uses_pkce=True,
        extra_authorize_params=_GOOGLE_AUTH_CODE_EXTRA,
        notes="Single OAuth covers Docs, Drive, Sheets, Tasks, Contacts, Slides, and Chat. One consent grants the union.",
    ),
    "google_analytics": OAuthProviderDescriptor(
        provider_id="google_analytics",
        display_name="Google Analytics",
        auth_uri=_GOOGLE_AUTH_URI,
        token_uri=_GOOGLE_TOKEN_URI,
        userinfo_uri=_GOOGLE_USERINFO_URI,
        scopes=GOOGLE_ANALYTICS_SCOPES,
        supported_flows=_AUTH_CODE_ONLY,
        default_flow="auth_code",
        client_config_file_env="GOOGLE_OAUTH_CREDENTIALS",
        uses_pkce=True,
        extra_authorize_params=_GOOGLE_AUTH_CODE_EXTRA,
        notes="Analytics read-only.",
    ),
    "google_business_profile": OAuthProviderDescriptor(
        provider_id="google_business_profile",
        display_name="Google Business Profile",
        auth_uri=_GOOGLE_AUTH_URI,
        token_uri=_GOOGLE_TOKEN_URI,
        userinfo_uri=_GOOGLE_USERINFO_URI,
        scopes=GOOGLE_BUSINESS_PROFILE_SCOPES,
        supported_flows=_AUTH_CODE_ONLY,
        default_flow="auth_code",
        client_config_file_env="GOOGLE_OAUTH_CREDENTIALS",
        uses_pkce=True,
        extra_authorize_params=_GOOGLE_AUTH_CODE_EXTRA,
        notes="Business Profile management scope.",
    ),
    "outlook": OAuthProviderDescriptor(
        provider_id="outlook",
        display_name="Microsoft Outlook",
        auth_uri=_MICROSOFT_AUTH_URI,
        token_uri=_MICROSOFT_TOKEN_URI,
        userinfo_uri=_MICROSOFT_USERINFO_URI,
        scopes=OUTLOOK_SCOPES,
        supported_flows=_BOTH_FLOWS,
        default_flow="auth_code",
        client_id_env="MICROSOFT_MCP_CLIENT_ID",
        # Public Microsoft Graph sample app id, mirrored from the legacy
        # outlook_auth.get_client_id() fallback. Lets the device-code flow
        # work out-of-the-box without a registered Azure app.
        client_id_fallback="8ad36cab-9646-40ee-97f5-0ddd7cd6e5c8",
        device_authorization_uri=_MICROSOFT_DEVICE_CODE_URI,
        uses_pkce=False,
        notes="Microsoft Graph. Supports both flows. Auto-degrades to device_code when NYMERIA_PUBLIC_URL is unset and use_localhost is not requested.",
    ),
}


def get_oauth_provider(provider_id: str | None) -> Optional[OAuthProviderDescriptor]:
    """Return the descriptor for ``provider_id`` (case-insensitive), or ``None``."""
    if not provider_id:
        return None
    return OAUTH_PROVIDERS.get(provider_id.strip().lower())


def list_oauth_providers() -> list[OAuthProviderDescriptor]:
    """Return all registered OAuth providers in registration order."""
    return list(OAUTH_PROVIDERS.values())


def is_known_oauth_provider(provider_id: str | None) -> bool:
    return get_oauth_provider(provider_id) is not None


def provider_supports_flow(provider_id: str | None, flow: OAuthFlow) -> bool:
    descriptor = get_oauth_provider(provider_id)
    return descriptor is not None and flow in descriptor.supported_flows


__all__ = [
    "OAUTH_PROVIDERS",
    "OAuthFlow",
    "OAuthProviderDescriptor",
    "GOOGLE_CALENDAR_SCOPES",
    "GOOGLE_GMAIL_SCOPES",
    "GOOGLE_DOCS_CORE_SCOPES",
    "GOOGLE_WORKSPACE_EXTRA_SCOPES",
    "GOOGLE_DOCS_SCOPES",
    "GOOGLE_ANALYTICS_SCOPES",
    "GOOGLE_BUSINESS_PROFILE_SCOPES",
    "OUTLOOK_SCOPES",
    "get_oauth_provider",
    "list_oauth_providers",
    "is_known_oauth_provider",
    "provider_supports_flow",
]
