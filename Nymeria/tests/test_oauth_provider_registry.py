"""Structural integrity checks for ``nymeria.config.oauth_providers``.

The OAuth provider descriptor registry is consumed by ``credential_prompt``,
``oauth_start``, ``oauth_callback_handler``, and ``oauth_device_flow``. Each
descriptor must satisfy the invariants those callers rely on (default_flow
inside supported_flows, device_code requires a device_authorization_uri,
client config is wired somehow, etc.). This module enforces those invariants
at the unit-test level so a future seed-data PR cannot silently break the
runtime callers.
"""

from __future__ import annotations

import pytest

from nymeria.config.oauth_providers import (
    GOOGLE_TOKEN_URI,
    OAUTH_PROVIDERS,
    OAuthProviderDescriptor,
    get_oauth_provider,
    is_known_oauth_provider,
    list_oauth_providers,
    provider_supports_flow,
)


_REQUIRED_PROVIDERS = {
    "google_calendar",
    "google_gmail",
    "google_docs",
    "google_analytics",
    "google_business_profile",
    "outlook",
}


def test_seed_providers_all_present():
    missing = _REQUIRED_PROVIDERS - set(OAUTH_PROVIDERS)
    assert not missing, f"OAUTH_PROVIDERS missing seeded entries: {sorted(missing)}"


@pytest.mark.parametrize("provider_id", sorted(_REQUIRED_PROVIDERS))
def test_descriptor_basic_fields_populated(provider_id):
    descriptor = OAUTH_PROVIDERS[provider_id]
    assert isinstance(descriptor, OAuthProviderDescriptor)
    assert descriptor.provider_id == provider_id
    assert descriptor.display_name
    assert descriptor.auth_uri.startswith(("http://", "https://"))
    assert descriptor.token_uri.startswith(("http://", "https://"))
    assert descriptor.userinfo_uri.startswith(("http://", "https://"))
    assert descriptor.scopes, f"{provider_id} has no scopes"
    assert isinstance(descriptor.scopes, tuple), (
        f"{provider_id}.scopes must be a tuple (immutable), got {type(descriptor.scopes).__name__}"
    )


@pytest.mark.parametrize("provider_id", sorted(_REQUIRED_PROVIDERS))
def test_default_flow_is_supported(provider_id):
    descriptor = OAUTH_PROVIDERS[provider_id]
    assert descriptor.default_flow in descriptor.supported_flows, (
        f"{provider_id}.default_flow={descriptor.default_flow!r} not in "
        f"supported_flows={sorted(descriptor.supported_flows)}"
    )


@pytest.mark.parametrize("provider_id", sorted(_REQUIRED_PROVIDERS))
def test_device_code_descriptors_have_device_auth_uri(provider_id):
    descriptor = OAUTH_PROVIDERS[provider_id]
    if "device_code" in descriptor.supported_flows:
        assert descriptor.device_authorization_uri, (
            f"{provider_id} supports device_code but lacks device_authorization_uri"
        )
    else:
        # No requirement either way, a stale URI on an auth-code-only
        # provider is harmless, so we only check the positive direction.
        pass


@pytest.mark.parametrize("provider_id", sorted(_REQUIRED_PROVIDERS))
def test_client_config_is_wired(provider_id):
    descriptor = OAUTH_PROVIDERS[provider_id]
    has_env_pair = bool(descriptor.client_id_env)
    has_file = bool(descriptor.client_config_file_env)
    assert has_env_pair or has_file, (
        f"{provider_id} has no client config wiring "
        "(set client_id_env or client_config_file_env)"
    )


def test_client_id_fallback_only_for_env_descriptors():
    """``client_id_fallback`` makes sense only for env-driven descriptors;
    the JSON-file path always carries its own client_id. Mixing them would
    indicate confused intent."""
    for provider_id, descriptor in OAUTH_PROVIDERS.items():
        if descriptor.client_id_fallback:
            assert descriptor.client_id_env, (
                f"{provider_id} has client_id_fallback but no client_id_env to fall back from"
            )


def test_get_oauth_provider_is_case_insensitive():
    assert get_oauth_provider("Google_Gmail") is OAUTH_PROVIDERS["google_gmail"]
    assert get_oauth_provider("  outlook  ") is OAUTH_PROVIDERS["outlook"]


def test_get_oauth_provider_unknown_returns_none():
    assert get_oauth_provider("nope") is None
    assert get_oauth_provider("") is None
    assert get_oauth_provider(None) is None


def test_is_known_oauth_provider_matches_dict():
    for provider_id in OAUTH_PROVIDERS:
        assert is_known_oauth_provider(provider_id)
    assert not is_known_oauth_provider("not_a_provider")
    assert not is_known_oauth_provider(None)


def test_provider_supports_flow_matches_descriptor():
    assert provider_supports_flow("google_calendar", "auth_code")
    assert not provider_supports_flow("google_calendar", "device_code")
    assert provider_supports_flow("outlook", "auth_code")
    assert provider_supports_flow("outlook", "device_code")
    assert not provider_supports_flow("does_not_exist", "auth_code")


def test_outlook_is_only_device_code_provider():
    """Plan locks in outlook as the only seeded device-code provider in v1.
    Google's device-flow registration requires a Limited-Input-Device client
    type that we don't ship. If this changes, update this test deliberately."""
    device_capable = {
        pid for pid, d in OAUTH_PROVIDERS.items() if "device_code" in d.supported_flows
    }
    assert device_capable == {"outlook"}, (
        f"expected only outlook to support device_code, got {sorted(device_capable)}"
    )


def test_gmail_has_post_save_and_post_clear_hooks():
    """Gmail uniquely exports an MCP credentials file after save; the same
    file must be cleaned up on disconnect. Both hooks must be wired."""
    descriptor = OAUTH_PROVIDERS["google_gmail"]
    assert descriptor.post_save_hook is not None
    assert descriptor.post_clear_hook is not None


def test_google_descriptors_use_pkce_and_offline_consent():
    """Google requires PKCE for installed-app clients and
    ``access_type=offline + prompt=consent`` to issue a refresh token. Any
    Google descriptor that drifts off this setting will silently stop
    receiving refresh tokens after the first consent."""
    for provider_id, descriptor in OAUTH_PROVIDERS.items():
        if not provider_id.startswith("google_"):
            continue
        assert descriptor.uses_pkce, f"{provider_id} must use PKCE"
        assert descriptor.extra_authorize_params.get("access_type") == "offline"
        assert descriptor.extra_authorize_params.get("prompt") == "consent"


def test_outlook_has_public_client_fallback():
    """Outlook ships with the Microsoft Graph sample app's public client_id so
    the device-code flow works out of the box. Removing this fallback breaks
    new installs that haven't registered their own Azure app."""
    descriptor = OAUTH_PROVIDERS["outlook"]
    assert descriptor.client_id_env == "MICROSOFT_MCP_CLIENT_ID"
    assert descriptor.client_id_fallback, "outlook needs a public client_id_fallback"


def test_list_oauth_providers_returns_all_in_order():
    listed = list_oauth_providers()
    assert {d.provider_id for d in listed} == set(OAUTH_PROVIDERS)
    # Listing must be deterministic so docs / UI menus don't shuffle.
    assert [d.provider_id for d in listed] == list(OAUTH_PROVIDERS.keys())


def test_token_endpoint_consumers_take_the_registry_value_verbatim():
    """The refresh path resolves ``token_uri`` from here, never from an account.

    A token endpoint receives whatever proves the grant, so this registry is a
    security boundary rather than a convenience table (see the note beside
    ``__all__`` in the module). Asserting the resolver agrees with every
    descriptor keeps a future descriptor from being added without the refresh
    path following it: a provider the resolver does not recognize falls through
    to the empty string, which cannot refresh at all.
    """
    from nymeria.tools.auth_cache_utils import _resolve_provider_token_uri

    for descriptor in list_oauth_providers():
        assert _resolve_provider_token_uri(descriptor.provider_id) == descriptor.token_uri

    # Legacy cache filenames can still produce an unregistered Google-family id.
    assert _resolve_provider_token_uri("google_unknown_future") == GOOGLE_TOKEN_URI
    assert _resolve_provider_token_uri("some_other_vendor") == ""
