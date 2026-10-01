"""Backend-credential catalog for the init flow's tool-family pickers.

TUI-free (like ``setup/rag_catalog.py`` and ``setup/providers.py``) so the
interactive wizard, the headless finalize path, and tests share one source of
truth. Maps each selectable ``web_search_*`` / ``fetch_url_*`` / ``image_gen_*``
backend to the single environment variable its tool reads (each tool resolves a
key in the order credential-vault -> ``settings.<field>`` -> ``os.environ``; the
canonical name is the settings field uppercased). The OpenAI and Gemini media
keys are the exception: their tools read a resolver property over two slots,
and on a gateway route the KeySpec moves to the direct slot
(``_DIRECT_MEDIA_SLOTS``). ``fetch_url_nymeria`` needs no key, SearXNG takes a
base URL rather than a secret, and Jina's key is optional.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

# The value predicates live in the config layer since #435 (the container's
# settings-file report judges the app's saved copy with them, without
# importing this package); re-exported here for hydrate and finalize, so the
# wizard and the container cannot drift.
from ..config.vendor_keys import (
    VENDOR_KEY_SLOTS,
    route_feeds_gateway,
    slot_holds_gateway_key,
    slot_holds_vendor_key,
)

if TYPE_CHECKING:
    from .state import WizardState

# Families the wizard records into ``state.extras`` as lists of concrete tool
# names, in the order their credentials should be collected.
_KEYED_FAMILIES = ("web_search", "fetch_url", "image_gen")


@dataclass(frozen=True)
class KeySpec:
    """A credential the user must provide for a selected backend.

    ``kind`` drives the wizard widget and validation: ``key`` and
    ``optional_key`` render a password input (optional_key may be left blank);
    ``url`` and ``text`` render a plain input for a non-secret value.
    ``placeholder`` overrides the input's hint text when the default
    ("Paste your <label>") fits poorly.
    """

    tool: str
    env_var: str
    label: str
    kind: str  # key | optional_key | url | text
    note: str = ""
    placeholder: str = ""

    @property
    def required(self) -> bool:
        return self.kind == "key"


# tool name -> the credential it needs. Tools with no entry (e.g.
# ``fetch_url_nymeria``) need nothing collected.
BACKEND_KEY_SPECS: dict[str, KeySpec] = {
    # web_search_*
    "web_search_perplexity": KeySpec(
        "web_search_perplexity", "PERPLEXITY_API_KEY", "Perplexity API key", "key"
    ),
    "web_search_tavily": KeySpec(
        "web_search_tavily", "TAVILY_API_KEY", "Tavily API key", "key"
    ),
    "web_search_exa_ai": KeySpec(
        "web_search_exa_ai", "EXA_API_KEY", "Exa API key", "key"
    ),
    "web_search_firecrawl": KeySpec(
        "web_search_firecrawl", "FIRECRAWL_API_KEY", "Firecrawl API key", "key"
    ),
    "web_search_brave": KeySpec(
        "web_search_brave", "BRAVE_API_KEY", "Brave Search API key", "key"
    ),
    "web_search_searxng": KeySpec(
        "web_search_searxng",
        "SEARXNG_BASE_URL",
        "SearXNG base URL",
        "url",
        note=(
            "The URL of a SearXNG instance you run yourself (not a secret); "
            "Nymeria does not start one on this install shape. Docker installs "
            "get a bundled one instead."
        ),
    ),
    # fetch_url_* (fetch_url_nymeria is keyless and intentionally absent)
    "jina_reader_fetch_url": KeySpec(
        "jina_reader_fetch_url",
        "JINA_API_KEY",
        "Jina API key (optional)",
        "optional_key",
        note="Optional: the public reader works without a key, at lower rate limits.",
    ),
    # image_gen_*
    "image_gen_openai": KeySpec(
        "image_gen_openai", "OPENAI_API_KEY", "OpenAI API key", "key"
    ),
    "image_gen_gemini": KeySpec(
        "image_gen_gemini", "GEMINI_API_KEY", "Google AI (Gemini) API key", "key"
    ),
    "image_gen_flux": KeySpec(
        "image_gen_flux",
        "BFL_API_KEY",
        "Black Forest Labs (FLUX) API key",
        "key",
        note="Black Forest Labs key for image_gen_flux.",
    ),
    "image_gen_replicate": KeySpec(
        "image_gen_replicate", "REPLICATE_API_KEY", "Replicate API key", "key"
    ),
    "image_gen_fal": KeySpec(
        "image_gen_fal", "FAL_API_KEY", "fal.ai API key", "key"
    ),
}


# Voice picks (single strings in extras["tts"]/extras["stt"], values matching
# the settings Literals) that need a credential collected. The TTS_API_KEY /
# TTS_VOICE slots are provider-specific: a Cartesia key in TTS_API_KEY is
# useless to ElevenLabs, so a provider SWITCH re-asks even when the var is
# already on disk (see required_backend_credentials).
VOICE_KEY_SPECS: dict[str, tuple[KeySpec, ...]] = {
    "tts:openai": (
        KeySpec("tts:openai", "OPENAI_API_KEY", "OpenAI API key", "key"),
    ),
    "tts:gemini": (
        KeySpec("tts:gemini", "GEMINI_API_KEY", "Google AI (Gemini) API key", "key"),
    ),
    "tts:cartesia": (
        KeySpec(
            "tts:cartesia", "TTS_API_KEY", "Cartesia API key", "key",
            note="Stored as TTS_API_KEY.",
        ),
        KeySpec(
            "tts:cartesia", "TTS_VOICE", "Cartesia voice UUID", "text",
            note="Required: Cartesia voices are account-scoped UUIDs from play.cartesia.ai.",
            placeholder="e.g. 694f9389-aac1-45b6-b726-9d9369183238",
        ),
    ),
    "tts:elevenlabs": (
        KeySpec(
            "tts:elevenlabs", "TTS_API_KEY", "ElevenLabs API key", "key",
            note="Stored as TTS_API_KEY.",
        ),
    ),
    "stt:openai": (
        KeySpec("stt:openai", "OPENAI_API_KEY", "OpenAI API key", "key"),
    ),
    "stt:groq": (
        KeySpec("stt:groq", "GROQ_API_KEY", "Groq API key", "key"),
    ),
}

# Env vars whose on-disk value belongs to the previously configured provider.
_VOICE_PROVIDER_SCOPED_VARS = {
    "tts": ("TTS_API_KEY", "TTS_VOICE"),
    "stt": ("STT_API_KEY",),
}


def _selected_backends(state: "WizardState") -> list[str]:
    """Concrete backend tool names selected across the keyed families, in order."""
    names: list[str] = []
    for family in _KEYED_FAMILIES:
        value = state.extras.get(family)
        if isinstance(value, list):
            names.extend(str(item) for item in value)
    return names


def _voice_key_specs(state: "WizardState") -> list[KeySpec]:
    """KeySpecs for the voice picks, in TTS-then-STT order."""
    specs: list[KeySpec] = []
    for kind in ("tts", "stt"):
        value = state.extras.get(kind)
        if isinstance(value, str):
            specs.extend(VOICE_KEY_SPECS.get(f"{kind}:{value}", ()))
    return specs


def _stale_voice_env(state: "WizardState") -> set[str]:
    """Provider-scoped voice vars whose on-disk value predates a provider switch.

    These must not count as "already provided": the old provider's key/voice
    is wrong for the new one, and finalize retires the stale lines anyway
    (``voice_catalog.voice_drop_env``). Values typed THIS run (optional_env)
    still satisfy.
    """
    from .voice_catalog import voice_provider_changed

    stale: set[str] = set()
    for kind, env_vars in _VOICE_PROVIDER_SCOPED_VARS.items():
        if voice_provider_changed(state, kind):
            stale.update(env_vars)
    return stale - {env for env, value in state.optional_env.items() if value}


GEMINI_LLM_KEY_ENV = "GEMINI_API_KEY"
OPENAI_LLM_KEY_ENV = "OPENAI_API_KEY"


def _slot_holds_gateway_key(state: "WizardState", slot: str, provider: str) -> bool:
    """True when this install's ``slot`` will hold a gateway's key.

    A CLIProxy route whose catalog key slot is ``slot`` writes the proxy's
    gatekeeper there, and a ``provider`` route through any base URL off the
    vendor's own hosts feeds that slot to the gateway. Checks the CLIProxy
    pick first because on a fresh run the route's provider and base URL are
    only filled in at finalize.
    """
    if state.auth_method_is_cliproxy():
        from ..cliproxy.catalog import get_cliproxy_provider
        from ..onboarding import legacy_cliproxy_provider

        pick = state.cliproxy_provider or legacy_cliproxy_provider(state.auth_method)
        cspec = get_cliproxy_provider(pick) if pick else None
        if cspec is not None:
            return cspec.key_env_var == slot
    return route_feeds_gateway(state.provider or "", state.base_url or "", vendor=provider)


def gemini_slot_holds_gateway_key(state: "WizardState") -> bool:
    """True when this install's GEMINI_API_KEY will hold a gateway's key.

    The wizard twin of ``Settings.gemini_key_is_gateway_owned`` (#152): the
    antigravity CLIProxy route, or a google route through a non-Google base
    URL. Gemini TTS and image generation call Google directly, so they need
    the direct slot instead.
    """
    return _slot_holds_gateway_key(state, GEMINI_LLM_KEY_ENV, "google")


def openai_slot_holds_gateway_key(state: "WizardState") -> bool:
    """True when this install's OPENAI_API_KEY will hold a gateway's key.

    The wizard twin of ``Settings.openai_key_is_gateway_owned`` (#428): the
    codex, gemini-cli, kimi and grok CLIProxy routes, or an openai route
    through a non-OpenAI base URL. OpenAI image generation and speech call
    OpenAI directly, so they need the direct slot instead.
    """
    return _slot_holds_gateway_key(state, OPENAI_LLM_KEY_ENV, "openai")


@dataclass(frozen=True)
class _DirectSlot:
    """The direct-call slot a media KeySpec moves to when a gateway owns its own."""

    env_var: str
    # The LLM provider whose route key lives in the shared slot.
    vendor: str
    gateway_owned: Callable[["WizardState"], bool]
    label: str
    note: str


# Keyed by the shared slot a media KeySpec normally asks for. The direct slot
# and the vendor come from the config layer's map (which also holds the
# vendor key prefix), so the wizard and the container's report agree.
_DIRECT_MEDIA_SLOTS: dict[str, _DirectSlot] = {
    GEMINI_LLM_KEY_ENV: _DirectSlot(
        VENDOR_KEY_SLOTS[GEMINI_LLM_KEY_ENV].direct,
        VENDOR_KEY_SLOTS[GEMINI_LLM_KEY_ENV].vendor,
        gemini_slot_holds_gateway_key,
        "Google AI (Gemini) API key for media",
        "Your LLM route's key only works through its gateway, and Gemini "
        "image generation and speech call Google directly, so they need "
        "a key from aistudio.google.com. Stored as GEMINI_DIRECT_API_KEY.",
    ),
    OPENAI_LLM_KEY_ENV: _DirectSlot(
        VENDOR_KEY_SLOTS[OPENAI_LLM_KEY_ENV].direct,
        VENDOR_KEY_SLOTS[OPENAI_LLM_KEY_ENV].vendor,
        openai_slot_holds_gateway_key,
        "OpenAI API key for image generation and speech",
        "Your LLM route's key only works through its gateway, and OpenAI "
        "image generation and speech call OpenAI directly, so they need "
        "a key from platform.openai.com. Stored as OPENAI_DIRECT_API_KEY.",
    ),
}


def direct_slot_vendor(slot: str) -> str | None:
    """The LLM provider whose key lives in the shared ``slot``, when that slot
    has a direct twin (``openai`` for OPENAI_API_KEY, ``google`` for
    GEMINI_API_KEY); None for any other slot."""
    direct = _DIRECT_MEDIA_SLOTS.get(slot)
    return direct.vendor if direct is not None else None


def _direct_media_spec(spec: KeySpec, state: "WizardState") -> KeySpec:
    """Retarget a media KeySpec at its direct slot on a gateway route.

    Without this the step asked for the shared slot (GEMINI_API_KEY,
    OPENAI_API_KEY), finalize dropped the typed value because the gatekeeper
    owns that slot, and the summary reported the media tools unconfigured
    with no word about why (#152, #428).
    """
    direct = _DIRECT_MEDIA_SLOTS.get(spec.env_var)
    if direct is None or not direct.gateway_owned(state):
        return spec
    return replace(spec, env_var=direct.env_var, label=direct.label, note=direct.note)


def retired_gateway_slots(state: "WizardState") -> tuple[str, ...]:
    """Shared slots holding the old route's gateway key that this run's route
    no longer feeds to a gateway: the key only worked there (#433)."""
    return tuple(
        sorted(
            slot for slot in state.gateway_env_keys if gateway_direct_slot(state, slot) is None
        )
    )


def _same_url(a: str, b: str) -> bool:
    return (a or "").strip().rstrip("/").lower() == (b or "").strip().rstrip("/").lower()


def gateway_key_would_go_elsewhere(state: "WizardState", slot: str | None) -> bool:
    """True when a blank key field would hand the OLD gateway's key to a new
    destination: the vendor (this route no longer feeds the slot to a
    gateway) or a different gateway (another base URL). A gateway's key is
    only ever kept for that same gateway (#433). Off the CLIProxy branch: on
    it, whether the slot holds the proxy's key is the gatekeeper question
    (``finalize.gatekeeper_on_disk``).
    """
    if (
        not slot
        or state.api_key.strip()
        or slot not in state.gateway_env_keys
        or state.auth_method_is_cliproxy()
    ):
        return False
    if gateway_direct_slot(state, slot) is None:
        return True
    disk_url = str(state.extras.get("llm_base_url_on_disk") or "")
    return bool(disk_url) and not _same_url(disk_url, state.base_url)


def vendor_key_would_feed_gateway(state: "WizardState", slot: str | None) -> bool:
    """True when a blank key field would hand the vendor's own key to a gateway.

    ``slot`` holds the vendor's key on disk (``vendor_env_keys``), the route
    this run configures feeds that slot to a gateway, and no new key was
    given: "keep the existing key" would make the OpenAI (or Google) key that
    gateway's bearer (#431). The connection step and finalize both refuse it.
    """
    return bool(
        slot
        and not state.api_key.strip()
        and slot in state.vendor_env_keys
        and gateway_direct_slot(state, slot) is not None
    )


def gateway_direct_slot(state: "WizardState", slot: str) -> str | None:
    """The direct slot a media key typed for ``slot`` belongs in, or None.

    Non-None only while a gateway owns ``slot`` on this install: finalize
    moves a real key given for the shared slot (``--openai-api-key``,
    ``--gemini-api-key``) there instead of dropping it under the gatekeeper.
    """
    direct = _DIRECT_MEDIA_SLOTS.get(slot)
    if direct is None or not direct.gateway_owned(state):
        return None
    return direct.env_var


def already_provided_env(state: "WizardState") -> set[str]:
    """Env vars already satisfied elsewhere, so the keys step should not re-ask.

    Covers anything already collected into ``optional_env`` (e.g. a Gemini key
    entered for the RAG step lands in EMBEDDING_API_KEY, but a separate
    GEMINI_API_KEY flag would land here) and the primary provider's own key env
    var (so an OpenAI primary provider satisfies ``image_gen_openai``).
    """
    provided = {env for env, value in state.optional_env.items() if value}
    # Reconfigure: credentials already present on disk (recorded by hydration)
    # are satisfied, so a fully-keyed existing install shows no backend-keys step.
    on_disk = set(getattr(state, "present_env_keys", set()) or set())
    for slot, direct in _DIRECT_MEDIA_SLOTS.items():
        # ...except a shared slot holding a proxy gatekeeper (a switch off a
        # CLIProxy route leaves one there until finalize retires it): it
        # cannot serve the media tools, so ask (#152 Gemini, #428 OpenAI).
        if slot in state.gatekeeper_env_keys:
            on_disk.discard(slot)
        # ...or the old route's gateway key in a slot the vendor gets back:
        # finalize retires it (#433), so it answers no media question.
        if slot in state.gateway_env_keys and not direct.gateway_owned(state):
            on_disk.discard(slot)
        # The vendor's own key on disk in a slot a gateway now takes: finalize
        # moves it to the direct slot (#431), so do not ask for it again.
        if slot in state.vendor_env_keys and direct.gateway_owned(state):
            provided.add(direct.env_var)
    provided |= on_disk
    # The primary provider's key typed this run satisfies its own slot, after
    # the on-disk discards (a new OpenAI key for an openai route does serve
    # image generation even where the old gateway key did not).
    spec = state.provider_spec()
    if spec is not None and spec.api_key_env_vars and state.api_key.strip():
        provided.add(spec.api_key_env_vars[0])
    return provided


def _bundled_env(state: "WizardState") -> set[str]:
    """Env vars this hosting shape supplies itself, so the keys step skips them.

    The Docker stacks run the SearXNG sidecar (the `search` compose profile)
    and finalize seeds its URL (`http://searxng:8080`), so asking a Docker
    install for "your self-hosted instance" invited a wrong answer that would
    have replaced the working sidecar URL (#101 entry 7).
    """
    from ..onboarding import HostingOption

    return {"SEARXNG_BASE_URL"} if state.hosting is HostingOption.DOCKER else set()


def required_backend_credentials(state: "WizardState") -> list[KeySpec]:
    """KeySpecs to prompt for, given the selected backends and what's already set.

    Deduped by env var, in family-then-voice order. Excludes any credential
    already provided (``already_provided_env``) and backends with no
    credential need; provider-scoped voice vars stop counting as provided
    when the voice provider switched this run (``_stale_voice_env``).
    """
    provided = (already_provided_env(state) - _stale_voice_env(state)) | _bundled_env(state)
    candidates: list[KeySpec] = [
        spec
        for tool in _selected_backends(state)
        if (spec := BACKEND_KEY_SPECS.get(tool)) is not None
    ]
    candidates.extend(_voice_key_specs(state))
    candidates = [_direct_media_spec(spec, state) for spec in candidates]
    seen: set[str] = set()
    specs: list[KeySpec] = []
    for spec in candidates:
        if spec.env_var in provided or spec.env_var in seen:
            continue
        seen.add(spec.env_var)
        specs.append(spec)
    return specs


def backend_keys_needed(state: "WizardState") -> bool:
    """True when at least one selected backend still needs a credential."""
    return bool(required_backend_credentials(state))


__all__ = [
    "KeySpec",
    "BACKEND_KEY_SPECS",
    "VOICE_KEY_SPECS",
    "already_provided_env",
    "required_backend_credentials",
    "backend_keys_needed",
    "direct_slot_vendor",
    "gateway_direct_slot",
    "gemini_slot_holds_gateway_key",
    "openai_slot_holds_gateway_key",
    "retired_gateway_slots",
    "route_feeds_gateway",
    "slot_holds_gateway_key",
    "slot_holds_vendor_key",
    "gateway_key_would_go_elsewhere",
    "vendor_key_would_feed_gateway",
]
