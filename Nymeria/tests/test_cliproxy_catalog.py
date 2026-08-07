"""Route-shape invariants for the CLIProxy provider catalog.

The catalog is the single source of truth for how each CLI subscription
routes into Nymeria's LLM settings (provider, base-URL shape, key setting,
API mode). The wizard, the /cliproxy router, and both frontends consume it,
so these tests pin the shapes the runtime depends on (see
core/agent_llm_config.py for the ANTHROPIC_API_KEY vs ANTHROPIC_DIRECT_API_KEY
split and vendor/react_agent/providers.py for the root-vs-/v1 handling).
"""

from __future__ import annotations

from nymeria.cliproxy.catalog import (
    CLIPROXY_PROVIDERS,
    cliproxy_data_plane_url,
    get_cliproxy_provider,
    list_cliproxy_providers,
)


def test_every_entry_has_a_complete_route_shape():
    for spec in list_cliproxy_providers():
        assert spec.id and spec.label and spec.oauth_endpoint, spec
        assert spec.flow in ("browser", "device"), spec.id
        assert spec.nymeria_provider in ("anthropic", "openai", "google"), spec.id
        assert spec.url_shape in ("root", "v1"), spec.id
        # By catalog invariant every key_setting is a ServerSettingsUpdate
        # field name (the global apply-route setattrs it verbatim).
        assert spec.key_setting in (
            "anthropic_api_key",
            "openai_api_key",
            "gemini_api_key",
        ), spec.id
        assert spec.default_model, spec.id
        # Browser flows complete via POST /oauth-callback and need the
        # normalizer name; device flows complete server-side without one.
        if spec.flow == "browser":
            assert spec.callback_provider, spec.id
        else:
            assert spec.callback_provider is None, spec.id


def test_claude_routes_anthropic_root_with_gatekeeper_key():
    spec = get_cliproxy_provider("claude")
    assert spec is not None
    assert spec.nymeria_provider == "anthropic"
    assert spec.url_shape == "root"
    # The cpx- gatekeeper goes to ANTHROPIC_API_KEY, never the DIRECT slot.
    assert spec.key_env_var == "ANTHROPIC_API_KEY"
    assert spec.api_mode == ""


def test_codex_routes_openai_v1_responses():
    spec = get_cliproxy_provider("codex")
    assert spec is not None
    assert spec.nymeria_provider == "openai"
    assert spec.url_shape == "v1"
    assert spec.api_mode == "responses"
    assert spec.key_env_var == "OPENAI_API_KEY"


def test_generic_clis_route_openai_v1_chat_completions():
    for provider_id in ("gemini-cli", "kimi", "grok"):
        spec = get_cliproxy_provider(provider_id)
        assert spec is not None, provider_id
        assert spec.nymeria_provider == "openai", provider_id
        assert spec.url_shape == "v1", provider_id
        assert spec.api_mode == "chat_completions", provider_id


def test_antigravity_routes_google_native_at_the_proxy_root():
    """Antigravity rides the native Gemini wire, not the OpenAI shim.

    The native inbound surface is the lossless one: real functionCall
    thoughtSignatures round-trip (the OpenAI translator replaces every one
    with the bypass sentinel, which measurably degrades long agentic
    loops), and the translator never manufactures an empty model turn.
    Root URL because the google-genai SDK appends /v1beta/models/... to
    its base itself; the key lands in GEMINI_API_KEY, which the google
    factory reads. Live-verified 2026-08-07
    (docs: private/cliproxy-gemini-native-inbound-2026-08.md; the
    surface audit's antigravity x native row corroborates)."""
    spec = get_cliproxy_provider("antigravity")
    assert spec is not None
    assert spec.nymeria_provider == "google"
    assert spec.url_shape == "root"
    assert spec.api_mode == ""
    assert spec.key_env_var == "GEMINI_API_KEY"
    # Bare host root: the SDK appends /v1beta itself; a /v1 here would
    # produce /v1/v1beta/... and break routing.
    assert (
        cliproxy_data_plane_url("http://localhost:8318/", spec)
        == "http://localhost:8318"
    )


def test_gemini_cli_carries_tos_warning():
    spec = get_cliproxy_provider("gemini-cli")
    assert spec is not None
    assert spec.tos_warning


def test_gemini_cli_is_demoted_to_legacy_below_antigravity():
    """Every Gemini-channel surface steers users to Antigravity.

    Consumers (wizard, /provider list, GET /cliproxy/status, desktop
    picker) render the catalog in declaration order and show the label
    and description verbatim, so the ordering and the wording ARE the
    steering mechanism; there is no separate UI rule."""
    ids = [spec.id for spec in list_cliproxy_providers()]
    assert ids.index("antigravity") < ids.index("gemini-cli")
    spec = get_cliproxy_provider("gemini-cli")
    assert spec is not None
    assert "legacy" in spec.label.lower()
    assert spec.description.startswith("LEGACY")
    assert "antigravity" in spec.description.lower()


def test_kimi_is_a_device_flow():
    spec = get_cliproxy_provider("kimi")
    assert spec is not None
    assert spec.flow == "device"
    assert spec.callback_provider is None


def test_grok_uses_the_xai_oauth_endpoint():
    spec = get_cliproxy_provider("grok")
    assert spec is not None
    assert spec.oauth_endpoint == "xai"
    assert spec.callback_provider == "xai"
    # Lookup by endpoint name resolves too (callback handling convenience).
    assert get_cliproxy_provider("xai") is spec


def test_catalog_ids_are_unique():
    ids = [spec.id for spec in CLIPROXY_PROVIDERS]
    assert len(ids) == len(set(ids))


def test_data_plane_url_root_vs_v1():
    claude = get_cliproxy_provider("claude")
    codex = get_cliproxy_provider("codex")
    assert claude is not None and codex is not None
    base = "http://cli-proxy-api:8317"
    assert cliproxy_data_plane_url(base, claude) == "http://cli-proxy-api:8317"
    assert cliproxy_data_plane_url(base, codex) == "http://cli-proxy-api:8317/v1"
    # Trailing slashes and an accidental /v1 on the management URL normalize.
    assert (
        cliproxy_data_plane_url("http://localhost:8318/v1/", claude)
        == "http://localhost:8318"
    )
    assert (
        cliproxy_data_plane_url("http://localhost:8318/", codex)
        == "http://localhost:8318/v1"
    )


def test_default_models_resolve_real_context_windows(monkeypatch):
    """Every catalog default_model must resolve its context window from a
    real tier (curated row, bundled catalog, or family rule), never the
    128k _default: the default is what a fresh subscription login compacts
    against, and a silent 128k cap on a 1M-window model was a review
    finding (2026-08-05, antigravity's gemini-3.6-flash-high)."""
    from nymeria.config import model_capabilities as mc

    monkeypatch.setattr(mc, "_fetch_openrouter_models", lambda: {})
    for spec in CLIPROXY_PROVIDERS:
        limit = mc.get_context_limit(spec.default_model)
        assert limit and limit > 128000, (spec.id, spec.default_model, limit)


def test_model_owner_set_only_where_verified():
    """owned_by attribution for the proxy's flat /v1/models pool. Verified
    live on v7.1.61 for claude/codex/gemini-cli and, since the first real
    antigravity login (2026-08-05), for antigravity too. Kimi and grok
    MUST stay unset until observed on a live login: consumers partition on
    this field, and a wrong guess would bury real models."""
    owners = {
        spec.id: spec.model_owner for spec in CLIPROXY_PROVIDERS
    }
    assert owners == {
        "claude": "anthropic",
        "codex": "openai",
        "gemini-cli": "google",
        "antigravity": "antigravity",
        "kimi": "",
        "grok": "",
    }
