"""Tests for the shared native-tool credential resolver.

`resolve_native_credential` collapses the per-provider vault -> settings -> env
fallback that the web-search and image-gen `_get_*` resolvers each hand-rolled.
These tests lock the precedence chain, the multi-env ordering, the empty-string
left-fold edge, and the forwarding of provider/aliases/field_names to the vault
lookup.
"""

from types import SimpleNamespace

import nymeria.config as config
import nymeria.tools.native_credentials as nc


class _FakeCred:
    def __init__(self, value):
        self.value = value
        self.credential_id = "c1"
        self.field_name = "api_key"


def _patch_vault(monkeypatch, result, recorder=None):
    """Patch get_native_credential_value to return `result`, recording kwargs."""

    def fake(**kwargs):
        if recorder is not None:
            recorder.update(kwargs)
        return result

    monkeypatch.setattr(nc, "get_native_credential_value", fake)


def _patch_settings(monkeypatch, **attrs):
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(**attrs))


def test_vault_value_short_circuits_settings_and_env(monkeypatch):
    _patch_vault(monkeypatch, _FakeCred("vault-key"))

    def _boom():
        raise AssertionError("settings must not be consulted on a vault hit")

    monkeypatch.setattr(config, "get_settings", _boom)
    monkeypatch.setenv("TAVILY_API_KEY", "env-key")

    result = nc.resolve_native_credential(
        provider="tavily",
        tool_name="web_search_tavily",
        settings_attr="tavily_api_key",
        env_vars=("TAVILY_API_KEY",),
    )
    assert result == "vault-key"


def test_empty_vault_value_falls_through_to_settings(monkeypatch):
    # A credential record whose secret field is empty must not win.
    _patch_vault(monkeypatch, _FakeCred(""))
    _patch_settings(monkeypatch, tavily_api_key="settings-key")

    result = nc.resolve_native_credential(
        provider="tavily",
        tool_name="web_search_tavily",
        settings_attr="tavily_api_key",
        env_vars=("TAVILY_API_KEY",),
    )
    assert result == "settings-key"


def test_settings_fallback_when_vault_misses(monkeypatch):
    _patch_vault(monkeypatch, None)
    _patch_settings(monkeypatch, exa_api_key="settings-key")
    monkeypatch.setenv("EXA_API_KEY", "env-key")

    result = nc.resolve_native_credential(
        provider="exa",
        tool_name="web_search_exa_ai",
        settings_attr="exa_api_key",
        env_vars=("EXA_API_KEY",),
    )
    assert result == "settings-key"


def test_env_fallback_when_vault_and_settings_empty(monkeypatch):
    _patch_vault(monkeypatch, None)
    _patch_settings(monkeypatch, brave_api_key=None)
    monkeypatch.setenv("BRAVE_API_KEY", "env-key")

    result = nc.resolve_native_credential(
        provider="brave",
        tool_name="web_search_brave",
        settings_attr="brave_api_key",
        env_vars=("BRAVE_API_KEY",),
    )
    assert result == "env-key"


def test_empty_string_settings_falls_through_to_env(monkeypatch):
    # Load-bearing for the left-fold equivalence: `"" or env(...)` -> env value.
    _patch_vault(monkeypatch, None)
    _patch_settings(monkeypatch, brave_api_key="")
    monkeypatch.setenv("BRAVE_API_KEY", "env-key")

    result = nc.resolve_native_credential(
        provider="brave",
        tool_name="web_search_brave",
        settings_attr="brave_api_key",
        env_vars=("BRAVE_API_KEY",),
    )
    assert result == "env-key"


def test_multi_env_returns_first_set_in_order(monkeypatch):
    # Mirrors the replicate/fal multi-env fallback ordering.
    _patch_vault(monkeypatch, None)
    _patch_settings(monkeypatch, replicate_api_key=None)
    monkeypatch.delenv("REPLICATE_API_KEY", raising=False)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "token-value")

    result = nc.resolve_native_credential(
        provider="replicate",
        tool_name="image_gen_replicate",
        settings_attr="replicate_api_key",
        env_vars=("REPLICATE_API_KEY", "REPLICATE_API_TOKEN"),
    )
    assert result == "token-value"


def test_multi_env_prefers_earlier_var(monkeypatch):
    _patch_vault(monkeypatch, None)
    _patch_settings(monkeypatch, fal_api_key=None)
    monkeypatch.setenv("FAL_API_KEY", "primary")
    monkeypatch.setenv("FAL_KEY", "secondary")

    result = nc.resolve_native_credential(
        provider="fal",
        tool_name="image_gen_fal",
        settings_attr="fal_api_key",
        env_vars=("FAL_API_KEY", "FAL_KEY"),
    )
    assert result == "primary"


def test_returns_none_when_nothing_is_set(monkeypatch):
    _patch_vault(monkeypatch, None)
    _patch_settings(monkeypatch, tavily_api_key=None)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    result = nc.resolve_native_credential(
        provider="tavily",
        tool_name="web_search_tavily",
        settings_attr="tavily_api_key",
        env_vars=("TAVILY_API_KEY",),
    )
    assert result is None


def test_default_field_names_is_api_key_trio(monkeypatch):
    recorder: dict = {}
    _patch_vault(monkeypatch, None, recorder=recorder)
    _patch_settings(monkeypatch, tavily_api_key=None)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    nc.resolve_native_credential(
        provider="tavily",
        aliases=("tavily_api", "tvly"),
        tool_name="web_search_tavily",
        config=None,
        settings_attr="tavily_api_key",
        env_vars=("TAVILY_API_KEY",),
    )
    assert recorder["provider"] == "tavily"
    assert recorder["provider_aliases"] == ("tavily_api", "tvly")
    assert recorder["field_names"] == ("api_key", "token", "value")
    assert recorder["tool_name"] == "web_search_tavily"
    assert recorder["config"] is None


def test_field_names_override_forwarded_to_vault(monkeypatch):
    # SearXNG resolves a base URL, not an api key, via a different field tuple.
    recorder: dict = {}
    _patch_vault(monkeypatch, None, recorder=recorder)
    _patch_settings(monkeypatch, searxng_base_url=None)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)

    nc.resolve_native_credential(
        provider="searxng",
        aliases=("searx", "searx_ng"),
        tool_name="web_search_searxng",
        settings_attr="searxng_base_url",
        env_vars=("SEARXNG_BASE_URL",),
        field_names=("base_url", "url", "value"),
    )
    assert recorder["field_names"] == ("base_url", "url", "value")
