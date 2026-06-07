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


__all__ = [
    "make_web_search_step",
    "make_fetch_url_step",
    "make_image_gen_step",
    "make_tts_step",
    "make_stt_step",
    "seeded_tool_names",
]
