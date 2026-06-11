"""Voice (TTS/STT) provider catalog for the init flow.

TUI-free (like ``setup/rag_catalog.py`` and ``setup/tool_keys.py``) so the
interactive wizard, the headless finalize path, and tests share one source of
truth. Choice values are exactly the ``tts_provider`` / ``stt_provider``
settings Literal values, plus ``"none"`` for off.

finalize maps the picks to env vars via :func:`voice_env_for_state`. Base URLs
are written only for Docker full-stack hosting (the speaches/qwen3 sidecars).
Everywhere else kokoro/faster-whisper run in-process via the
``nymeriaos[voice-local]`` extra, qwen3 falls back to its localhost sidecar
default, and hosted providers use their built-in URLs.

Model, voice, and base-URL values are provider-specific (a Cartesia voice UUID
breaks Kokoro; a sidecar URL would hijack a hosted provider), so switching
providers retires the stale lines via :func:`voice_drop_env`. Hydrate records
the on-disk provider under ``extras["tts_on_disk"]`` / ``extras["stt_on_disk"]``
to make that switch detectable; a value the new run produces always wins over
the drop list (see ``finalize.write_config``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..onboarding import DockerStack, HostingOption

if TYPE_CHECKING:
    from .state import WizardState


@dataclass(frozen=True)
class VoiceChoice:
    value: str
    label: str
    description: str


TTS_CHOICES: tuple[VoiceChoice, ...] = (
    VoiceChoice("none", "Off", "No spoken replies. Enable later in settings."),
    VoiceChoice(
        "kokoro",
        "Local: Kokoro",
        "Free, runs on CPU in this process (Docker full stack uses the "
        "speaches sidecar). ~350 MB one-time model download.",
    ),
    VoiceChoice(
        "edge",
        "Edge (free, hosted)",
        "Microsoft Edge neural voices, no API key. Unofficial endpoint: "
        "a great zero-cost default, but it could break without notice.",
    ),
    VoiceChoice("openai", "OpenAI", "gpt-4o-mini-tts, 13 voices. Uses your OpenAI key."),
    VoiceChoice(
        "elevenlabs",
        "ElevenLabs",
        "Highest quality, priciest. eleven_flash_v2_5 by default; key required.",
    ),
    VoiceChoice("gemini", "Gemini", "Google's hosted TTS. Uses your Gemini key."),
    VoiceChoice(
        "cartesia",
        "Cartesia Sonic",
        "Hosted, very low latency. Needs an API key and a voice UUID "
        "from play.cartesia.ai.",
    ),
    VoiceChoice(
        "qwen3",
        "Local: Qwen3-TTS (GPU)",
        "Self-hosted sidecar with voice cloning. Needs an NVIDIA GPU.",
    ),
)

STT_CHOICES: tuple[VoiceChoice, ...] = (
    VoiceChoice("none", "Off", "No voice transcription. Enable later in settings."),
    VoiceChoice(
        "faster-whisper",
        "Local: faster-whisper",
        "Free, runs on CPU in this process (Docker full stack uses the "
        "speaches sidecar). Whisper 'small' by default.",
    ),
    VoiceChoice("openai", "OpenAI", "gpt-4o-mini-transcribe. Uses your OpenAI key."),
    VoiceChoice(
        "groq",
        "Groq",
        "Hosted whisper-large-v3-turbo at roughly $0.04 per audio hour: "
        "the cheapest hosted option, and more accurate than local 'small'.",
    ),
)

TTS_VALUES = frozenset(choice.value for choice in TTS_CHOICES)
STT_VALUES = frozenset(choice.value for choice in STT_CHOICES)

_TTS_LABELS = {choice.value: choice.label for choice in TTS_CHOICES}
_STT_LABELS = {choice.value: choice.label for choice in STT_CHOICES}

# Local providers that run in-process when no base URL is set.
LOCAL_TTS_PROVIDERS = frozenset({"kokoro"})
LOCAL_STT_PROVIDERS = frozenset({"faster-whisper"})

# Docker full-stack compose service URLs. The speaches sidecar serves both
# capabilities; qwen3-tts is the GPU profile.
_DOCKER_TTS_URLS = {
    "kokoro": "http://speaches:8000/v1",
    "qwen3": "http://qwen3-tts:8000/v1",
}
_DOCKER_STT_URLS = {
    "faster-whisper": "http://speaches:8000/v1",
}

# Provider-specific lines that must not survive a provider switch.
_TTS_STALE_VARS = ("TTS_BASE_URL", "TTS_MODEL", "TTS_VOICE", "TTS_API_KEY")
_STT_STALE_VARS = ("STT_BASE_URL", "STT_MODEL", "STT_API_KEY")


def selected_tts(state: "WizardState") -> str | None:
    """The TTS pick, or None when the step was never reached (write nothing)."""
    value = state.extras.get("tts")
    return value if isinstance(value, str) and value in TTS_VALUES else None


def selected_stt(state: "WizardState") -> str | None:
    """The STT pick, or None when the step was never reached (write nothing)."""
    value = state.extras.get("stt")
    return value if isinstance(value, str) and value in STT_VALUES else None


def tts_label(value: str) -> str:
    return _TTS_LABELS.get(value, value)


def stt_label(value: str) -> str:
    return _STT_LABELS.get(value, value)


def _docker_full_stack(state: "WizardState") -> bool:
    return (
        state.hosting is HostingOption.DOCKER
        and state.docker_stack is DockerStack.FULL
    )


def uses_voice_sidecar(state: "WizardState") -> bool:
    """True when the picks point at the speaches sidecar (Docker full stack).

    Speaches only (kokoro TTS / faster-whisper STT); the qwen3 GPU sidecar
    lives in its own compose profile, see :func:`uses_qwen3_sidecar`.
    """
    if not _docker_full_stack(state):
        return False
    return (
        selected_tts(state) in LOCAL_TTS_PROVIDERS
        or selected_stt(state) in LOCAL_STT_PROVIDERS
    )


def uses_qwen3_sidecar(state: "WizardState") -> bool:
    """True when the TTS pick is the qwen3 GPU sidecar (Docker full stack)."""
    return _docker_full_stack(state) and selected_tts(state) == "qwen3"


def needs_local_voice_extra(state: "WizardState") -> bool:
    """True when a pick will run in-process here (non-Docker local provider)."""
    if state.hosting is HostingOption.DOCKER:
        return False
    return (
        selected_tts(state) in LOCAL_TTS_PROVIDERS
        or selected_stt(state) in LOCAL_STT_PROVIDERS
    )


def slim_docker_local_voice(state: "WizardState") -> bool:
    """True for the broken combo: single-container Docker + a local voice pick.

    The slim image ships neither the in-process engines nor a speaches
    sidecar, so the pick cannot work without extra operator action; finalize
    warns instead of writing silently dead config.
    """
    if state.hosting is not HostingOption.DOCKER or _docker_full_stack(state):
        return False
    return (
        selected_tts(state) in LOCAL_TTS_PROVIDERS
        or selected_stt(state) in LOCAL_STT_PROVIDERS
    )


def voice_env_for_state(state: "WizardState") -> dict[str, str]:
    """TTS/STT env lines for the picks; empty when the steps were skipped."""
    out: dict[str, str] = {}
    tts = selected_tts(state)
    if tts is not None:
        out["TTS_PROVIDER"] = tts
        if _docker_full_stack(state):
            url = _DOCKER_TTS_URLS.get(tts)
            if url:
                out["TTS_BASE_URL"] = url
    stt = selected_stt(state)
    if stt is not None:
        out["STT_PROVIDER"] = stt
        if _docker_full_stack(state):
            url = _DOCKER_STT_URLS.get(stt)
            if url:
                out["STT_BASE_URL"] = url
    return out


def voice_provider_changed(state: "WizardState", kind: str) -> bool:
    """True when this run's ``kind`` ("tts"/"stt") pick differs from disk.

    Picking "none" on an install that never had a provider recorded is NOT a
    change: a marker-less config may carry hand-staged voice lines for later,
    and merely Entering through the wizard must not retire them.
    """
    selected = selected_tts(state) if kind == "tts" else selected_stt(state)
    if selected is None:
        return False
    on_disk = state.extras.get(f"{kind}_on_disk")
    if on_disk is None and selected == "none":
        return False
    return selected != on_disk


def voice_drop_env(state: "WizardState") -> tuple[str, ...]:
    """Stale provider-specific lines to retire on a provider switch.

    Only fires when the pick changed, plus one hosting-shape case: a
    wizard-written sidecar URL whose shape no longer runs the sidecar (e.g.
    Docker full stack reconfigured to slim or bare metal with the local pick
    unchanged) is retired so the local engines fall back in-process instead
    of dialing a dead compose hostname. Hand-set URLs survive: only the
    wizard's own sidecar values are recognized. Values produced by this run
    (e.g. a freshly collected TTS_API_KEY or a sidecar URL) win over the
    drop inside write_config.
    """
    drops: tuple[str, ...] = ()
    if voice_provider_changed(state, "tts"):
        drops += _TTS_STALE_VARS
    if voice_provider_changed(state, "stt"):
        drops += _STT_STALE_VARS
    produced = voice_env_for_state(state)
    if (
        "TTS_BASE_URL" not in produced
        and selected_tts(state) is not None
        and state.extras.get("tts_base_url_on_disk") in _DOCKER_TTS_URLS.values()
    ):
        drops += ("TTS_BASE_URL",)
    if (
        "STT_BASE_URL" not in produced
        and selected_stt(state) is not None
        and state.extras.get("stt_base_url_on_disk") in _DOCKER_STT_URLS.values()
    ):
        drops += ("STT_BASE_URL",)
    # Dedupe, preserving order (a provider switch may already carry the URL).
    return tuple(dict.fromkeys(drops))


__all__ = [
    "LOCAL_STT_PROVIDERS",
    "LOCAL_TTS_PROVIDERS",
    "STT_CHOICES",
    "STT_VALUES",
    "TTS_CHOICES",
    "TTS_VALUES",
    "VoiceChoice",
    "needs_local_voice_extra",
    "selected_stt",
    "selected_tts",
    "stt_label",
    "tts_label",
    "uses_voice_sidecar",
    "voice_drop_env",
    "voice_env_for_state",
    "voice_provider_changed",
]
