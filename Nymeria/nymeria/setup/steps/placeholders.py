"""Init-chosen tool-family steps and capability placeholders.

These follow Section B of `docs/private/core-toolset-plan.md`: at init the user
seeds members of real tool families on top of the always-on core set. The
`web_search_*`, `fetch_url_*`, and `image_gen_*` families are built, so they are
real multi-selects over the actual registered tool names. The `rag_search_*`
suite is not built yet (only a single `rag_search` exists today), so it stays a
placeholder. Text-to-speech and speech-to-text are placeholders too. Selections
are recorded on `WizardState`; wiring them into the per-user
`default_thread_tools` is separate future work.
"""

from __future__ import annotations

from ..nav import Step
from ..state import WizardState
from .base import Choice, multi_select_step, placeholder_step


# Section B, built: the six registered web_search_* backends.
_WEB_SEARCH_BACKENDS = [
    Choice(
        "web_search_perplexity",
        "Perplexity",
        "Synthesized answers; self-sufficient (needs no web fetch backend).",
    ),
    Choice("web_search_tavily", "Tavily", "Links and previews; pair with a web fetch backend."),
    Choice("web_search_exa_ai", "Exa", "Links and previews; pair with a web fetch backend."),
    Choice(
        "web_search_firecrawl",
        "Firecrawl",
        "Links and previews; pair with a web fetch backend.",
    ),
    Choice("web_search_brave", "Brave Search", "Links and previews; pair with a web fetch backend."),
    Choice("web_search_searxng", "SearXNG", "Self-hosted; pair with a web fetch backend."),
]

# Section B, built: the two registered fetch_url-family tools.
_WEB_FETCH_BACKENDS = [
    Choice(
        "fetch_url_nymeria",
        "Nymeria fetch",
        "Built-in fetcher that distills a page with a configurable model.",
    ),
    Choice("jina_reader_fetch_url", "Jina Reader", "Hosted reader endpoint (key required)."),
]

# Section B, built: the five registered image_gen_* providers (mirrors the
# web_search_* suite so users can pick and swap providers individually).
_IMAGE_GEN_BACKENDS = [
    Choice(
        "image_gen_openai",
        "OpenAI GPT Image",
        "Top-ranked all-rounder: photorealism, prompt adherence, in-image text (key required).",
    ),
    Choice(
        "image_gen_gemini",
        "Google Gemini (Nano Banana Pro)",
        "Best for in-image text, infographics/diagrams, and 4K output (key required).",
    ),
    Choice(
        "image_gen_flux",
        "Black Forest Labs FLUX.2",
        "Flagship photorealism and multi-reference; async submit and poll (key required).",
    ),
    Choice(
        "image_gen_replicate",
        "Replicate (budget)",
        "Low-cost host for FLUX.1 schnell or Z-Image Turbo; good for drafts (key required).",
    ),
    Choice(
        "image_gen_fal",
        "fal.ai (budget)",
        "Fast low-cost host for Z-Image Turbo or FLUX.1 schnell (key required).",
    ),
]


def _extras_list(step_id: str):
    def get_initial(state: WizardState) -> list[str]:
        return list(state.extras.get(step_id, []))

    def store(state: WizardState, value: list[str]) -> None:
        state.extras[step_id] = list(value)

    return get_initial, store


# Section C dependency: every web_search backend except Perplexity returns only
# links, so it needs a fetch_url backend to be useful.
_SELF_SUFFICIENT_SEARCH = "web_search_perplexity"


def has_nonperplexity_search(state: WizardState) -> bool:
    """True when a link-only (non-Perplexity) web_search backend is selected."""
    selected = state.extras.get("web_search")
    if not isinstance(selected, list):
        return False
    return any(name != _SELF_SUFFICIENT_SEARCH for name in selected)


def unmet_fetch_dependency(state: WizardState) -> bool:
    """True when a link-only search backend is selected but no fetch backend is."""
    if not has_nonperplexity_search(state):
        return False
    fetch = state.extras.get("fetch_url")
    return not (isinstance(fetch, list) and len(fetch) > 0)


def _fetch_url_warning(state: WizardState) -> str | None:
    if has_nonperplexity_search(state):
        return (
            "You picked a search backend that returns only links (everything "
            "except Perplexity). Pick a web fetch backend below so search results "
            "are readable."
        )
    return None


def make_web_search_step() -> Step:
    """Real multi-select over the built web_search_* backends (Section B)."""
    get_initial, store = _extras_list("web_search")
    return multi_select_step(
        step_id="web_search",
        title="Web search backends",
        note=(
            "Pick the web search tools for your default toolset (all six are "
            "built; recorded now, not written to your defaults yet). Every backend "
            "except Perplexity returns links only, so add a web fetch backend next."
        ),
        choices=_WEB_SEARCH_BACKENDS,
        get_initial=get_initial,
        store=store,
    )


def make_fetch_url_step() -> Step:
    """Real multi-select over the built fetch_url-family tools (Section B/C)."""
    get_initial, store = _extras_list("fetch_url")
    return multi_select_step(
        step_id="fetch_url",
        title="Web fetch backends",
        note=(
            "Pick the tools that read full page content from a URL. Web search "
            "backends other than Perplexity return only links, so they need one of "
            "these to be useful."
        ),
        choices=_WEB_FETCH_BACKENDS,
        get_initial=get_initial,
        store=store,
        warning_fn=_fetch_url_warning,
    )


def make_image_gen_step() -> Step:
    """Real multi-select over the built image_gen_* providers (Section B)."""
    get_initial, store = _extras_list("image_gen")
    return multi_select_step(
        step_id="image_gen",
        title="Image generation providers",
        note=(
            "Pick the image generation tools for your default toolset (all five "
            "are built; recorded now, not written to your defaults yet). Each needs "
            "its provider API key set. The budget hosts (Replicate, fal.ai) cost "
            "far less than the flagship providers."
        ),
        choices=_IMAGE_GEN_BACKENDS,
        get_initial=get_initial,
        store=store,
    )


def make_tts_step() -> Step:
    return placeholder_step(
        step_id="tts",
        title="Text-to-speech",
        note="Placeholder. Voice synthesis options are not wired up yet.",
        options=[
            Choice("cartesia", "Cartesia Sonic", "Hosted (key required)."),
            Choice("openai", "OpenAI TTS", "Hosted (key required)."),
            Choice("gemini", "Gemini TTS", "Hosted (key required)."),
            Choice("qwen3", "Qwen3-TTS", "Runs locally."),
        ],
    )


def make_stt_step() -> Step:
    return placeholder_step(
        step_id="stt",
        title="Speech-to-text",
        note="Placeholder. Transcription options are not wired up yet.",
        options=[
            Choice("openai", "OpenAI", "Hosted (key required)."),
            Choice("faster-whisper", "Faster-Whisper", "Runs locally."),
        ],
    )


# Section D, built: the bundled, default-on capability kits that the agent loads
# on demand. self-improve (the text-only guidance skill) stays on separately and
# is not a toggle here.
_SKILL_KITS = [
    Choice(
        "tool-management",
        "Tool management",
        "Find, enable, and build tools, including HTTP/API-backed ones.",
    ),
    Choice(
        "skill-management",
        "Skill management",
        "Find, install, create, and edit Skills and Skill Kits.",
    ),
    Choice(
        "mcp-management",
        "MCP management",
        "Find, install, test, and manage MCP servers.",
    ),
    Choice(
        "credential-management",
        "Credential management",
        "Request, inspect, and clean up service credentials and connections.",
    ),
]

_SKILL_KIT_IDS = [choice.value for choice in _SKILL_KITS]


def make_skill_kits_step() -> Step:
    """Real multi-select over the bundled, default-on capability kits (Section D).

    Seeds the chosen kit names into the bootstrap admin's `enabled_global_skills`
    (see `finalize.seed_bootstrap_profile`). Each kit only appears in the agent's
    skill index and loads its tools when activated, so all four are checked by
    default. The `self-improve` guidance skill stays on regardless of this pick.
    """
    _, store = _extras_list("skill_kits")

    def get_initial(state: WizardState) -> list[str]:
        stored = state.extras.get("skill_kits")
        if isinstance(stored, list):
            return list(stored)
        return list(_SKILL_KIT_IDS)

    return multi_select_step(
        step_id="skill_kits",
        title="Default skill kits",
        note=(
            "Pick the capability kits Nymeria keeps on by default. Each shows up in "
            "the agent's skill index and loads its tools only when the task needs "
            "them, so leaving all four on is cheap. The self-improve guidance skill "
            "stays on regardless. You can change these later in settings."
        ),
        choices=_SKILL_KITS,
        get_initial=get_initial,
        store=store,
    )


def seeded_tool_names(state: WizardState) -> list[str]:
    """Concrete tool names the init flow would seed from the built families.

    Only the built `web_search_*`, `fetch_url_*`, and `image_gen_*` families
    resolve to real tool names today; the placeholder families contribute nothing
    until their suites land. Kept here so review and any future finalize wiring
    share one source.
    """
    names: list[str] = []
    for family in ("web_search", "fetch_url", "image_gen"):
        value = state.extras.get(family)
        if isinstance(value, list):
            names.extend(str(item) for item in value)
    return names


def seeded_global_skills(state: WizardState) -> list[str]:
    """The capability kit names the init flow seeds as default-on global skills.

    Reads the `skill_kits` multi-select. When the step was never reached (e.g. a
    non-interactive run that did not pass `--skill-kit`), defaults to all bundled
    kits, matching the interactive step's all-checked default. `self-improve` is
    added separately by the finalize seeding, not here.
    """
    value = state.extras.get("skill_kits")
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(_SKILL_KIT_IDS)


__all__ = [
    "make_web_search_step",
    "make_fetch_url_step",
    "make_image_gen_step",
    "make_tts_step",
    "make_stt_step",
    "make_skill_kits_step",
    "seeded_tool_names",
    "seeded_global_skills",
    "has_nonperplexity_search",
    "unmet_fetch_dependency",
]
