"""Which settings keys hold a credential, and which decide where one goes.

Stdlib only, so ``config/settings.py`` (the runtime settings file report),
``doctor.py``, the settings router and the command layer can all share ONE
classifier without the config layer importing the FastAPI router (#434).

Keys are compared case-folded, so a field name (``openai_api_key``) and its
env-var spelling (``OPENAI_API_KEY``) classify the same.
"""

from __future__ import annotations

from typing import Literal, Optional

SECRET_SETTING_KEYS: frozenset[str] = frozenset({
    # No SECRET_KEY_SUFFIXES entry matches the bare *_key here, so the
    # CLIProxy remote-management secret needs an explicit allowlist row.
    "cliproxy_management_key",
    "openai_api_key",
    "openai_direct_api_key",
    "anthropic_api_key",
    "anthropic_direct_api_key",
    "openrouter_api_key",
    "embedding_api_key",
    "perplexity_api_key",
    "gemini_api_key",
    "gemini_direct_api_key",
    "discord_bot_token",
    "discord_webhook_url",
    "telegram_bot_token",
    "twitch_client_secret",
    "twitch_bot_access_token",
    "twitch_bot_refresh_token",
    "twitch_broadcaster_token",
    "twitch_broadcaster_refresh_token",
    "slack_bot_token",
    "slack_app_token",
    "whatsapp_access_token",
    "whatsapp_webhook_verify_token",
    "whatsapp_app_secret",
    "teams_bot_app_password",
    "microsoft_graph_access_token",
    "postgres_uri",
    "redis_url",
    "tts_api_key",
    "stt_api_key",
    "fcm_credentials_json",
})


# Name suffixes that mark a settings key as credential-bearing. Used so any
# *_api_key / *_token / *_secret / *_password / *_private_key style key is
# masked even when it was never added to the explicit allowlist above. Suffix
# matching (not substring) avoids false positives like ``llm_max_tokens``.
SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_secret_key",
    "_secret",
    "_secrets_key",
    "_access_key",
    "_private_key",
    "_signing_key",
    "_encryption_key",
    "_token",
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_session_token",
    "_verify_token",
    "_password",
    "_passwd",
    "_app_password",
    "_app_secret",
    "_webhook_secret",
    "_client_secret",
    "_credentials_json",
    "_service_account_json",
)

# The keys that decide WHERE a provider credential is sent. A stale copy of
# one of these is as dangerous as a stale key: an old proxy base URL
# resurrected under a direct route hands the vendor key to the proxy. The
# background, embedding and voice base URLs carry their own (or the main) key
# the same way. Model names are deliberately absent (a wrong model is an
# error, not a leak), and so are the service-integration base URLs, which
# carry only their own integration's credential to that integration's host.
ROUTE_SETTING_KEYS: frozenset[str] = frozenset({
    "llm_provider",
    "llm_base_url",
    "llm_provider_route",
    "openai_api_mode",
    "cliproxy_management_url",
    "llm_background_base_url",
    "embedding_base_url",
    "tts_base_url",
    "stt_base_url",
})

# A shared slot whose real vendor key has a dedicated home once a gateway owns
# the shared one (the #431 direct slots): where a user re-saves a real key
# before clearing the shared slot's app-saved copy. Anthropic has the same
# split (`get_api_key_for_provider` prefers the direct key without a base URL;
# the wizard's CLIProxy branch writes the gatekeeper to ANTHROPIC_API_KEY).
DIRECT_KEY_SLOTS: dict[str, str] = {
    "OPENAI_API_KEY": "OPENAI_DIRECT_API_KEY",
    "GEMINI_API_KEY": "GEMINI_DIRECT_API_KEY",
    "ANTHROPIC_API_KEY": "ANTHROPIC_DIRECT_API_KEY",
}

SettingKeyClass = Literal["credential", "route", "setting"]


def is_secret_setting_key(key: str) -> bool:
    """Return True if a settings key holds a credential and must be masked.

    Combines the explicit allowlist (for secret-bearing keys whose names do not
    follow a credential suffix, e.g. ``postgres_uri``, ``redis_url``,
    ``discord_webhook_url``) with name-suffix derivation so newly added secret
    settings are masked by default rather than leaking until someone remembers
    to extend the allowlist.
    """
    k = key.lower()
    if k in SECRET_SETTING_KEYS:
        return True
    return k.endswith(SECRET_KEY_SUFFIXES)


def classify_setting_key(key: str) -> SettingKeyClass:
    """``credential``, ``route`` or ``setting``, for either spelling of a key."""
    if is_secret_setting_key(key):
        return "credential"
    if key.lower() in ROUTE_SETTING_KEYS:
        return "route"
    return "setting"


def direct_key_slot(key: str) -> Optional[str]:
    """The direct slot a real vendor key in ``key`` belongs in, or None."""
    return DIRECT_KEY_SLOTS.get(key.upper())


__all__ = [
    "DIRECT_KEY_SLOTS",
    "ROUTE_SETTING_KEYS",
    "SECRET_KEY_SUFFIXES",
    "SECRET_SETTING_KEYS",
    "SettingKeyClass",
    "classify_setting_key",
    "direct_key_slot",
    "is_secret_setting_key",
]
