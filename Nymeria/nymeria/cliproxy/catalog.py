"""Provider catalog for CLIProxy subscription OAuth.

One frozen spec per CLI subscription CLIProxy can log into, carrying both the
OAuth wiring (which `/v0/management/{x}-auth-url` endpoint, browser vs device
flow, the `POST /oauth-callback` provider name) and the route shape Nymeria
writes when that CLI becomes an LLM route (provider, base-URL shape, key
setting, API mode). This table is the single source of truth consumed by the
setup wizard, the `/cliproxy` API router, and (through that router) the
desktop and mobile frontends; frontends must not re-derive route shapes.

The catalog is intentionally a superset of what any one proxy binary serves:
support is probe-gated at runtime (`CLIProxyManagementClient.probe_providers`
treats a 404 on the auth-url endpoint as unsupported). Baseline on the pinned
v7.1.61 binary (2026-06-10): claude, codex, gemini-cli, antigravity, kimi and
grok respond; qwen and iflow were removed upstream after the v6.9.x line, so
they have no entries here (re-adding one is a single spec if an older binary
is ever pinned again).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

OAuthFlow = Literal["browser", "device"]
UrlShape = Literal["root", "v1"]
ApiMode = Literal["", "chat_completions", "responses"]
# Mirrors LLMConfig.provider_route plus "" for single-route providers.
ProviderRoute = Literal["", "native", "openai_compat", "anthropic_messages"]

GEMINI_CLI_TOS_WARNING = (
    "Google treats third-party use of the Gemini CLI OAuth client as a policy "
    "violation. Prefer a direct Gemini API key if that risk is unacceptable."
)


@dataclass(frozen=True)
class CLIProxyProviderSpec:
    """How one CLI subscription logs in and routes into Nymeria."""

    id: str
    label: str
    description: str
    # OAuth wiring: GET /v0/management/{oauth_endpoint}-auth-url starts the
    # flow; callback_provider is the name POST /oauth-callback's normalizer
    # accepts (None for device flows, which complete server-side by polling).
    oauth_endpoint: str
    flow: OAuthFlow
    callback_provider: Optional[str]
    # Route shape: what finalize / apply-route writes into Nymeria settings.
    nymeria_provider: str
    url_shape: UrlShape
    key_setting: str
    api_mode: ApiMode = ""
    # Provider ROUTE the target requires, for multi-route providers
    # (google: native vs openai_compat). Both apply paths pin it so a
    # stale per-thread/global route toggle cannot silently downgrade the
    # wire (openai_compat is checked BEFORE the provider dispatch in
    # create_llm). Empty = single-route provider, apply clears the thread
    # field and leaves the global untouched.
    provider_route: ProviderRoute = ""
    default_model: str = ""
    tos_warning: str = ""
    # The `provider` string this CLI's entries carry in GET /auth-files.
    # NOT a single stable spelling across proxy versions: the auth FILE's
    # own JSON carries the token type ("gemini", gemini_token.go), but the
    # proxy's synthesizer rewrites gemini to "gemini-cli" on load
    # (oauth_model_alias.go) and the listing is built from that rewritten
    # value (auth_files.go); match via `auth_file_providers`.
    auth_file_provider: str = ""
    # The `owned_by` value this CLI's models carry in the proxy's
    # /v1/models listing (one flat pool across every logged-in
    # subscription; verified live on v7.1.61 for claude/codex/gemini-cli).
    # Empty means unknown/unverified: consumers must degrade to the
    # unpartitioned list, never filter on a guess (kimi and grok stay
    # unset until observed on a live login).
    model_owner: str = ""

    @property
    def auth_file_providers(self) -> tuple[str, ...]:
        """Accepted spellings of a listed entry's provider for this target.

        The union of `auth_file_provider` and the target id: the live
        v7.1.61 proxy lists the gemini file as provider "gemini-cli" where
        the catalog (and the file's own type field, and older binaries)
        says "gemini"; dogfood-observed 2026-08-02, when the exact-match
        read a completed Gemini login as logged-out. Also covers grok,
        whose id ("grok") and file provider ("xai") diverge by design.
        """
        return tuple(
            sorted(
                {
                    s.lower()
                    for s in (self.auth_file_provider, self.id)
                    if s
                }
            )
        )

    @property
    def key_env_var(self) -> str:
        """Env var the gatekeeper key is written to (mirrors settings naming)."""
        return self.key_setting.upper()


CLIPROXY_PROVIDERS: tuple[CLIProxyProviderSpec, ...] = (
    CLIProxyProviderSpec(
        id="claude",
        label="Claude (Max/Pro subscription)",
        description=(
            "Anthropic OAuth via the Claude Code client. Routes as the native "
            "anthropic provider at the proxy root URL."
        ),
        oauth_endpoint="anthropic",
        flow="browser",
        callback_provider="anthropic",
        nymeria_provider="anthropic",
        url_shape="root",
        key_setting="anthropic_api_key",
        default_model="claude-opus-4-7",
        auth_file_provider="claude",
        model_owner="anthropic",
    ),
    CLIProxyProviderSpec(
        id="codex",
        label="Codex (ChatGPT Plus/Pro subscription)",
        description=(
            "OpenAI Codex OAuth. Routes as the openai provider in responses "
            "mode at the proxy /v1 URL."
        ),
        oauth_endpoint="codex",
        flow="browser",
        callback_provider="codex",
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="responses",
        default_model="gpt-5.5",
        auth_file_provider="codex",
        model_owner="openai",
    ),
    CLIProxyProviderSpec(
        id="antigravity",
        label="Antigravity (Google account)",
        description=(
            "Antigravity OAuth (separate Google client). Routes as the "
            "native google provider at the proxy root URL: the Gemini SDK "
            "appends /v1beta itself, and the native wire is the lossless "
            "one (real thought signatures round-trip; the OpenAI-compat "
            "path replaces them with a bypass sentinel, which measurably "
            "degrades long agentic loops). Verified live 2026-08-07."
        ),
        oauth_endpoint="antigravity",
        flow="browser",
        callback_provider="antigravity",
        nymeria_provider="google",
        url_shape="root",
        key_setting="gemini_api_key",
        provider_route="native",
        # Antigravity's own upstream naming (quality-suffixed ids, not the
        # gemini-cli -preview spellings). Live-verified 2026-08-05 on the
        # first real login: the pool carried gemini-3.6-flash-high (this
        # pick, proven serving), gemini-3.5-flash-low/-extra-low,
        # gemini-3.1-pro-low, gemini-3-flash(-agent), plus claude and
        # gpt-oss entries; no plain gemini-3-pro-high appeared.
        default_model="gemini-3.6-flash-high",
        auth_file_provider="antigravity",
        # NOT "google": antigravity is the one channel whose model list is
        # fetched live from Google per login, and entries carry owned_by
        # "antigravity" (antigravity_executor.go; live-confirmed 2026-08-05
        # on a real login's /v1/models).
        model_owner="antigravity",
    ),
    CLIProxyProviderSpec(
        id="gemini-cli",
        label="Gemini CLI (Google account, legacy)",
        description=(
            "LEGACY: prefer Antigravity for Gemini (lossless native wire, "
            "richer model pool). Google Code Assist OAuth with GCP project "
            "onboarding, kept as a separate-quota fallback for when "
            "Antigravity is unavailable. Deliberately stays on the proxy's "
            "OpenAI-compatible /v1 endpoint: the proxy's native inbound for "
            "this channel destroys ALL thought signatures, so the "
            "sentinel-stamped compat wire is the least-bad option "
            "(measured 2026-08-07)."
        ),
        oauth_endpoint="gemini-cli",
        flow="browser",
        callback_provider="gemini",
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="chat_completions",
        default_model="gemini-3-pro-preview",
        tos_warning=GEMINI_CLI_TOS_WARNING,
        auth_file_provider="gemini",
        model_owner="google",
    ),
    CLIProxyProviderSpec(
        id="kimi",
        label="Kimi (Moonshot subscription)",
        # chat_completions is DELIBERATE, not a fallback: on this channel
        # that pair is a byte-level passthrough (reasoning_content and
        # Moonshot fields survive verbatim), while the proxy's responses
        # translator drops replayed reasoning and sampling params
        # (docs/private/cliproxy-kimi-channel-audit-2026-08.md).
        description=(
            "Kimi device-code login (approve on kimi.com, no callback). "
            "Routes through the proxy's OpenAI-compatible /v1 endpoint in "
            "chat_completions mode, the highest-fidelity wire for this "
            "channel (reasoning streams and replays)."
        ),
        oauth_endpoint="kimi",
        flow="device",
        callback_provider=None,
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="chat_completions",
        # Newest/largest kimi (1M context, low/high/max thinking levels) per
        # the proxy's live model registry; source-audit-verified only, no
        # live kimi login observed yet (entitlement unconfirmed).
        default_model="kimi-k3",
        auth_file_provider="kimi",
    ),
    CLIProxyProviderSpec(
        id="grok",
        label="Grok (SuperGrok/X Premium subscription)",
        # responses is the channel's passthrough wire: the xai channel's
        # upstream format is literally codex, so /v1/responses streams and
        # replays reasoning items with no injected effort default and
        # surfaces upstream failures, while its chat_completions translator
        # drops replayed reasoning and injects a medium effort when the
        # field is omitted
        # (docs/private/cliproxy-xai-channel-audit-2026-08.md).
        description=(
            "xAI OAuth (7.1.x proxies only). Routes as the openai provider "
            "in responses mode at the proxy /v1 URL, the highest-fidelity "
            "wire for this channel (reasoning streams and replays)."
        ),
        oauth_endpoint="xai",
        flow="browser",
        callback_provider="xai",
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="responses",
        # grok-4.3: full none/low/medium/high ladder + 1M context + the only
        # flagship inside the pinned v7.1.61 reasoning allowlist. grok-4.5
        # stays non-default until the proxy upgrade (backlog #151): v7.1.61
        # deletes its reasoning config before the POST, so it thinks
        # invisibly.
        default_model="grok-4.3",
        auth_file_provider="xai",
    ),
)

_BY_ID = {spec.id: spec for spec in CLIPROXY_PROVIDERS}


def list_cliproxy_providers() -> tuple[CLIProxyProviderSpec, ...]:
    """Return the full catalog (support is probe-gated by callers)."""
    return CLIPROXY_PROVIDERS


def get_cliproxy_provider(provider_id: str) -> Optional[CLIProxyProviderSpec]:
    """Look up a catalog entry by id (also accepts the oauth endpoint name)."""
    normalized = (provider_id or "").strip().lower()
    spec = _BY_ID.get(normalized)
    if spec is not None:
        return spec
    for candidate in CLIPROXY_PROVIDERS:
        if candidate.oauth_endpoint == normalized:
            return candidate
    return None


# Display names for the channels a CLIProxy request can be served by, used
# by the banner/overview provider labels ("cliproxy {channel}"). Short names,
# not the catalog labels: the label strings carry subscription detail the
# one-line banner has no room for.
_CHANNEL_BY_PROVIDER: dict[str, str] = {
    # These provider slots are single-channel: the slot alone names it.
    "anthropic": "Claude",
    "google": "Antigravity",
}
_CHANNEL_BY_MODEL_PREFIX: tuple[tuple[str, str], ...] = (
    # Order matters: gpt-oss before gpt-. gpt-oss ids were observed in the
    # ANTIGRAVITY pool (2026-08-05 live login), so the Codex guess would be
    # wrong; they stay unknown and callers fall back to a channel-blind
    # label.
    ("gpt-oss", ""),
    ("gpt-", "Codex"),
    ("codex-", "Codex"),
    # The same 2026-08-05 pool observation also listed claude entries, so a
    # claude id on the compat wire COULD be antigravity-served; unlike
    # gpt-oss the label still names the model's own channel sensibly, so
    # the mapping is kept (deliberate, reviewed 2026-08-07).
    ("claude-", "Claude"),
    # Either Google channel (antigravity or legacy gemini-cli) can serve a
    # gemini id on the compat wire; which one is the proxy's routing
    # decision, so the channel stays generic here.
    ("gemini-", "Gemini"),
    ("kimi-", "Kimi"),
    ("grok-", "Grok"),
)


def cliproxy_channel_label(provider: str, model: str) -> str:
    """Best-effort display name of the CLIProxy channel serving a route.

    ``provider`` is the Nymeria provider slot, ``model`` the effective model
    id. Callers must already have established that the base URL is
    CLIProxy-shaped (``looks_like_cliproxy_url``); this function only names
    the channel. Returns "" when the channel is not inferable, in which case
    callers keep their channel-blind label.

    The anthropic and google slots map one-to-one onto catalog channels.
    The openai slot is the shared OpenAI-compat surface (codex, gemini-cli,
    kimi, grok, and stale pre-native antigravity routes), where the model id
    is the proxy's routing key and therefore also ours.
    """
    slot = (provider or "").strip().casefold()
    direct = _CHANNEL_BY_PROVIDER.get(slot)
    if direct is not None:
        return direct
    if slot != "openai":
        return ""
    model_id = (model or "").strip().casefold()
    for prefix, channel in _CHANNEL_BY_MODEL_PREFIX:
        if model_id.startswith(prefix):
            return channel
    return ""


def cliproxy_data_plane_url(management_url: str, spec: CLIProxyProviderSpec) -> str:
    """Derive the LLM base URL for a CLI from the proxy management URL.

    The management URL is the proxy host root (data plane and control plane
    share one port). Root-shape targets hand the SDK the bare host (the
    Anthropic SDK appends /v1/messages; the google-genai SDK appends
    /v1beta/models/...); v1-shape targets use the OpenAI-compatible /v1
    path.
    """
    base = (management_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    if spec.url_shape == "v1":
        return f"{base}/v1"
    return base


__all__ = [
    "CLIPROXY_PROVIDERS",
    "CLIProxyProviderSpec",
    "GEMINI_CLI_TOS_WARNING",
    "cliproxy_channel_label",
    "cliproxy_data_plane_url",
    "get_cliproxy_provider",
    "list_cliproxy_providers",
]
