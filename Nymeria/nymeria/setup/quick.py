"""Quick-path (quickstart tier / `--quick`) step subset and defaults.

The quickstart tier (picked on the chooser screen after welcome, or preset via
`--quick`) asks only the irreducible questions (hosting, the LLM auth +
provider/connection/model/key, a timezone confirm, external access) and applies
sensible, no-extra-auth defaults for everything else, so a user reaches a
working install with the fewest prompts and zero paid services. The interactive
flow honors this two ways: the step list is gated to `QUICK_KEEP_STEP_IDS` (see
`steps.build_default_steps`), and `apply_quick_defaults` seeds the skipped
steps' defaults onto `WizardState` (at chooser pick time, or before the wizard
runs when the tier came from a flag) so the review screen is accurate. The
hosting-dependent defaults re-resolve whenever hosting is chosen
(`apply_quick_hosting_defaults`), and switching the chooser back to Full
unwinds exactly what was seeded (`unapply_quick_defaults`).

Defaults chosen for zero extra auth or cost:
- RAG: the free, private local stack (granite + Ettin), the same default a RAG
  skip applies (see `rag_catalog.apply_quickstart_rag`).
- Web fetch: `fetch_url_nymeria`, which can extract from pages with your already
  configured primary LLM, so it needs no separate key. (It is also the
  default-checked option in the full path's fetch step.)
- Web search: keyless, by hosting shape. Docker gets `web_search_searxng`
  backed by the bundled SearXNG sidecar (finalize enables the `search` compose
  profile and writes SEARXNG_BASE_URL); bare-metal gets `web_search_ddgs`, the
  in-process keyless metasearch (no infra at all).
- Voice: the free local pair (kokoro TTS + faster-whisper STT) on bare-metal
  hosting only; it runs in-process and needs the voice-local extra, which
  finalize points out. Docker quickstart leaves voice off: the slim image
  ships no voice engines (see `voice_catalog.slim_docker_local_voice`).
- Image generation: none seeded; every provider needs a key.
- Skill kits: left unset, which the seeding fallback resolves to the curated
  default-checked set (`family_catalog.default_checked_skill_kits`; newly
  bundled kits are offered, not auto-checked; kits load their tools on
  demand, so on-by-default is cheap).
- Docker stack: slim (the single-container shape), the simplest single-user
  default; only consulted when hosting is Docker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..onboarding import DockerStack, HostingOption
from . import family_catalog
from .rag_catalog import (
    QUICKSTART_EMBEDDER,
    QUICKSTART_RERANKER,
    apply_quickstart_rag,
)

if TYPE_CHECKING:
    from .state import WizardState


# The only steps the quick path still prompts for; every other step is gated off
# and defaulted. Hosting and the LLM (auth method, then provider/connection/
# model/key or the cliproxy_* subscription branch) cannot be defaulted;
# external access matters too much to silently default (a remote-access intent
# needs the guided tunnel steps); start_now stays a single end question;
# welcome, tier, and review are the entry, mode, and confirmation screens.
QUICK_KEEP_STEP_IDS = frozenset(
    {
        "welcome",
        "tier",
        "hosting",
        "auth_method",
        "provider",
        "connection",
        "model",
        "cliproxy_disclaimer",
        "cliproxy_endpoint",
        "cliproxy_provider",
        "cliproxy_login",
        "cliproxy_model",
        # Timezone stays a visible confirm step: detection can be wrong, and a
        # wrong timezone silently skews schedules and TODO deadlines.
        "timezone",
        "external_access",
        "external_access_tailscale",
        "external_access_cloudflare",
        "start_now",
        "review",
    }
)

# Keyless web fetch default: extracts from pages with the configured primary LLM,
# so it needs no separate key. Seeded into default_thread_tools via the fetch_url
# family (see steps/placeholders.seeded_tool_names). Single source of truth lives
# in family_catalog so the full-path fetch step and the quick path agree.
QUICK_FETCH_DEFAULT = family_catalog.default_checked_fetch_url()

# Keyless web search defaults, by hosting shape. Docker stacks bundle the
# SearXNG sidecar (the more robust self-hosted aggregator); bare-metal installs
# get the in-process ddgs metasearch, which needs no infra at all.
QUICK_WEB_SEARCH_DOCKER = ("web_search_searxng",)
QUICK_WEB_SEARCH_LOCAL = ("web_search_ddgs",)

# Free local voice pair (bare-metal hosting only; in Docker the slim image has
# no voice engines, so quickstart leaves voice off there).
QUICK_TTS_DEFAULT = "kokoro"
QUICK_STT_DEFAULT = "faster-whisper"


# Ordered ids of every wizard step, kept TUI-free so the non-interactive path can
# validate `init <section>` jumps without importing the Textual step modules.
# Must mirror `steps.__init__._default_step_list()` exactly; a parity test pins
# the two lists together.
DEFAULT_STEP_IDS: tuple[str, ...] = (
    "welcome",
    "tier",
    "hosting",
    "api_port",
    "docker_stack",
    "security_profile",
    "auth_method",
    "cliproxy_disclaimer",
    "cliproxy_endpoint",
    "cliproxy_provider",
    "cliproxy_login",
    "cliproxy_model",
    "provider",
    "connection",
    "model",
    "llm_tuning",
    "core_tools",
    "web_search",
    "fetch_url",
    "embedder",
    "reranker",
    "image_gen",
    "tts",
    "stt",
    "backend_keys",
    "skill_kits",
    "timezone",
    "context",
    "agent_limits",
    "external_access",
    "external_access_tailscale",
    "external_access_cloudflare",
    "start_now",
    "review",
)


def validate_section_id(section: str) -> None:
    """Reject an unknown `init <section>` name with the jumpable-id listing.

    Shared by the interactive and non-interactive paths so both produce the
    same error message.
    """
    if section not in DEFAULT_STEP_IDS:
        jumpable = ", ".join(
            s for s in DEFAULT_STEP_IDS if s not in {"welcome", "review"}
        )
        raise SystemExit(f"Unknown section '{section}'. Choose one of: {jumpable}")


# Section-jump (`nymeria init <section>`) dependency closure: steps that must stay
# resolvable when jumping to a section, beyond the section itself plus the welcome
# and review bookends. The provider/connection/model trio is one LLM unit; the
# tool-family steps need the backend-keys step to collect any new credential; the
# reranker follows the embedder. Steps not listed keep just themselves. Each step
# keeps its own `applies`, so conditional members still drop out when irrelevant.
_LLM_SECTION_STEPS = frozenset(
    {
        "auth_method",
        "provider",
        "connection",
        "model",
        "llm_tuning",
        "cliproxy_disclaimer",
        "cliproxy_endpoint",
        "cliproxy_provider",
        "cliproxy_login",
        "cliproxy_model",
    }
)

_EXTERNAL_ACCESS_SECTION_STEPS = frozenset(
    {
        "external_access",
        "external_access_tailscale",
        "external_access_cloudflare",
    }
)

SECTION_DEPENDENCIES: dict[str, frozenset[str]] = {
    # The whole LLM unit travels together: jumping to any of its steps must be
    # able to switch between the API-key trio and the CLIProxy branch (the
    # applies predicates pick the active side).
    "auth_method": _LLM_SECTION_STEPS,
    "provider": _LLM_SECTION_STEPS,
    "connection": _LLM_SECTION_STEPS,
    "model": _LLM_SECTION_STEPS,
    "cliproxy_disclaimer": _LLM_SECTION_STEPS,
    "cliproxy_endpoint": _LLM_SECTION_STEPS,
    "cliproxy_provider": _LLM_SECTION_STEPS,
    "cliproxy_login": _LLM_SECTION_STEPS,
    "cliproxy_model": _LLM_SECTION_STEPS,
    "web_search": frozenset({"web_search", "backend_keys"}),
    "fetch_url": frozenset({"fetch_url", "backend_keys"}),
    "image_gen": frozenset({"image_gen", "backend_keys"}),
    # Voice picks collect their provider key on the keys screen; a scoped
    # provider switch must be able to re-ask it (the old provider's key is
    # retired by voice_drop_env).
    "tts": frozenset({"tts", "backend_keys"}),
    "stt": frozenset({"stt", "backend_keys"}),
    # A tuning-only jump re-runs just itself (the LLM unit pulls it along on
    # its own jumps via _LLM_SECTION_STEPS; hydrate restores the provider its
    # applies predicate needs).
    "llm_tuning": frozenset({"llm_tuning"}),
    "embedder": frozenset({"embedder", "reranker"}),
    "reranker": frozenset({"embedder", "reranker"}),
    # The external-access choice gates the guided tailscale/cloudflare setup
    # steps, so a jump to ANY of the three must carry all three (jumping
    # straight to a tunnel step would otherwise build an empty wizard when
    # the choice is not hydrated).
    "external_access": _EXTERNAL_ACCESS_SECTION_STEPS,
    "external_access_tailscale": _EXTERNAL_ACCESS_SECTION_STEPS,
    "external_access_cloudflare": _EXTERNAL_ACCESS_SECTION_STEPS,
}


def section_keep_ids(section: str) -> frozenset[str]:
    """Step ids to keep applicable for a section jump: section + deps + bookends."""
    deps = SECTION_DEPENDENCIES.get(section, frozenset({section}))
    return frozenset({"welcome", "review"} | set(deps))


def apply_quick_defaults(state: "WizardState") -> None:
    """Seed defaults for the steps the quick path skips, onto ``state``.

    Idempotent; runs when the chooser picks Quickstart (or before the wizard
    starts when the tier came from ``--quick``) so the review screen reflects
    the defaults. Only fills what was not already set, so explicit flags
    (e.g. ``--fetch-url``, ``--embedding-api-key``) still win. Everything
    seeded here is recorded so :func:`unapply_quick_defaults` can unwind a
    chooser switch back to Full.
    """
    seeded = state.extras.setdefault("quick_seeded", {})
    # Free local RAG (same default a RAG skip applies); marks rag_quickstarted so
    # review shows it and the reranker step stays consistent. The condition keeps
    # an explicit embedder pick or embedding key untouched.
    if state.embedder is None and not state.optional_env.get("EMBEDDING_API_KEY"):
        apply_quickstart_rag(state)
        seeded["rag"] = True
    # Keyless web fetch, unless the fetch family was already decided (a flag
    # pick, a hydrated reconfigure value, or an explicit `--fetch-url none`
    # empty list; key presence, not truthiness, so an empty pick survives).
    if "fetch_url" not in state.extras:
        state.extras["fetch_url"] = list(QUICK_FETCH_DEFAULT)
        seeded["fetch_url"] = list(QUICK_FETCH_DEFAULT)
    # Default the Docker shape to slim (simplest single-user container); the
    # docker_stack step is gated off in quick mode, so seed it for review.
    if state.docker_stack is None:
        state.docker_stack = DockerStack.SLIM
    # Hosting-dependent seeds (web search, voice) when hosting is already
    # known (a --hosting flag or a hydrated reconfigure); the hosting step's
    # store re-runs this for the interactive pick.
    apply_quick_hosting_defaults(state)


def apply_quick_hosting_defaults(state: "WizardState") -> None:
    """Seed (and on a hosting change, re-seed) the hosting-dependent defaults.

    Called from :func:`apply_quick_defaults` and from the hosting step's store,
    so the keyless web-search backend and the voice pair always match the
    chosen shape. A value is only (re)written when this function seeded it
    itself (tracked in ``extras["quick_seeded"]``), so flag picks, hydrated
    reconfigure values, and on-disk voice providers always win.
    """
    if not getattr(state, "quick", False) or state.hosting is None:
        return
    seeded = state.extras.setdefault("quick_seeded", {})
    docker = state.hosting is HostingOption.DOCKER

    desired_search = list(
        QUICK_WEB_SEARCH_DOCKER if docker else QUICK_WEB_SEARCH_LOCAL
    )
    if "web_search" not in state.extras or (
        state.extras.get("web_search") == seeded.get("web_search")
    ):
        state.extras["web_search"] = desired_search
        seeded["web_search"] = list(desired_search)

    # Free local voice runs in-process, so it is seeded for bare-metal hosting
    # only; the slim Docker image ships no voice engines (see
    # voice_catalog.slim_docker_local_voice), so a hosting switch to Docker
    # retires our own seed rather than writing dead config. An on-disk
    # provider (a reconfigure) is never overridden.
    for key, desired in (("tts", QUICK_TTS_DEFAULT), ("stt", QUICK_STT_DEFAULT)):
        ours = key in seeded and state.extras.get(key) == seeded.get(key)
        if docker:
            if ours:
                state.extras.pop(key, None)
                seeded.pop(key, None)
        elif (key not in state.extras or ours) and state.extras.get(
            f"{key}_on_disk"
        ) is None:
            state.extras[key] = desired
            seeded[key] = desired


def unapply_quick_defaults(state: "WizardState") -> None:
    """Unwind :func:`apply_quick_defaults` when the chooser switches to Full.

    Pops exactly the values the quick path seeded and that still hold the
    seeded value, so the full walk starts from the normal step defaults
    (without this, ``rag_quickstarted`` would gate the reranker step off and
    the seeded picks would masquerade as user choices). Values the user set
    via flags, or that hydrate restored, are never touched.
    """
    seeded = state.extras.get("quick_seeded")
    if not isinstance(seeded, dict):
        return
    if (
        seeded.get("rag")
        and state.rag_quickstarted
        and state.embedder == QUICKSTART_EMBEDDER
        and state.reranker == QUICKSTART_RERANKER
    ):
        state.embedder = None
        state.reranker = None
        state.rag_quickstarted = False
    for key in ("fetch_url", "web_search", "tts", "stt"):
        if key in seeded and state.extras.get(key) == seeded.get(key):
            state.extras.pop(key, None)
    state.extras.pop("quick_seeded", None)


__all__ = [
    "DEFAULT_STEP_IDS",
    "QUICK_KEEP_STEP_IDS",
    "QUICK_FETCH_DEFAULT",
    "QUICK_STT_DEFAULT",
    "QUICK_TTS_DEFAULT",
    "QUICK_WEB_SEARCH_DOCKER",
    "QUICK_WEB_SEARCH_LOCAL",
    "apply_quick_defaults",
    "apply_quick_hosting_defaults",
    "unapply_quick_defaults",
    "SECTION_DEPENDENCIES",
    "section_keep_ids",
    "validate_section_id",
]
