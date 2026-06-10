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
    default_model: str = ""
    tos_warning: str = ""
    # The `provider` string this CLI's entries carry in GET /auth-files.
    auth_file_provider: str = ""

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
    ),
    CLIProxyProviderSpec(
        id="gemini-cli",
        label="Gemini CLI (Google account)",
        description=(
            "Google Code Assist OAuth with GCP project onboarding. Routes "
            "through the proxy's OpenAI-compatible /v1 endpoint."
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
    ),
    CLIProxyProviderSpec(
        id="antigravity",
        label="Antigravity (Google account)",
        description=(
            "Antigravity OAuth (separate Google client). Routes through the "
            "proxy's OpenAI-compatible /v1 endpoint."
        ),
        oauth_endpoint="antigravity",
        flow="browser",
        callback_provider="antigravity",
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="chat_completions",
        default_model="gemini-3-pro-preview",
        auth_file_provider="antigravity",
    ),
    CLIProxyProviderSpec(
        id="kimi",
        label="Kimi (Moonshot subscription)",
        description=(
            "Kimi device-code login (approve on kimi.com, no callback). "
            "Routes through the proxy's OpenAI-compatible /v1 endpoint."
        ),
        oauth_endpoint="kimi",
        flow="device",
        callback_provider=None,
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="chat_completions",
        default_model="kimi-k2.5",
        auth_file_provider="kimi",
    ),
    CLIProxyProviderSpec(
        id="grok",
        label="Grok (SuperGrok/X Premium subscription)",
        description=(
            "xAI OAuth (7.1.x proxies only). Routes through the proxy's "
            "OpenAI-compatible /v1 endpoint."
        ),
        oauth_endpoint="xai",
        flow="browser",
        callback_provider="xai",
        nymeria_provider="openai",
        url_shape="v1",
        key_setting="openai_api_key",
        api_mode="chat_completions",
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


def cliproxy_data_plane_url(management_url: str, spec: CLIProxyProviderSpec) -> str:
    """Derive the LLM base URL for a CLI from the proxy management URL.

    The management URL is the proxy host root (data plane and control plane
    share one port). Claude uses the root (the Anthropic SDK appends
    /v1/messages); everything else uses the OpenAI-compatible /v1 path.
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
    "cliproxy_data_plane_url",
    "get_cliproxy_provider",
    "list_cliproxy_providers",
]
