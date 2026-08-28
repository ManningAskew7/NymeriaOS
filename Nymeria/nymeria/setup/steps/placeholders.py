"""Init-chosen tool-family steps and voice (TTS/STT) provider steps.

These follow Section B of `docs/private/plans/core-toolset-plan.md`: at init
the user
seeds members of real tool families on top of the default (seed) core set. The
`web_search_*`, `fetch_url_*`, and `image_gen_*` families are built, so they are
real multi-selects over the actual registered tool names, and the picks are
written to the bootstrap admin's `default_thread_tools` at finalize (via
`tool_seed.default_thread_tools_for_state` and `finalize.seed_bootstrap_profile`)
so a new thread inherits them. The text-to-speech and speech-to-text picks are
single-selects over `setup/voice_catalog.py`; finalize writes them as
`TTS_PROVIDER` / `STT_PROVIDER` (plus Docker sidecar base URLs), and the keys
they need ride the shared backend-keys step.
"""

from __future__ import annotations

from .. import family_catalog, voice_catalog
from ..nav import Step
from ..state import WizardState
from .base import Choice, multi_select_step, single_select_step


def _choices(family_choices) -> list[Choice]:
    """Wrap the TUI-free catalog ``FamilyChoice`` list into wizard ``Choice``s."""
    return [Choice(c.value, c.label, c.description) for c in family_choices]


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
            "built and saved to your default thread tools). Every backend "
            "except Perplexity returns links only, so add a web fetch backend next."
        ),
        choices=_choices(family_catalog.web_search_choices()),
        get_initial=get_initial,
        store=store,
    )


def make_fetch_url_step() -> Step:
    """Real multi-select over the built fetch_url-family tools (Section B/C).

    `fetch_url_nymeria` is default-checked: it can extract from pages with the
    configured primary LLM, so it needs no separate key and is a safe default
    (the quick path seeds the same one, see `quick.QUICK_FETCH_DEFAULT`).
    """
    _, store = _extras_list("fetch_url")

    def get_initial(state: WizardState) -> list[str]:
        stored = state.extras.get("fetch_url")
        if isinstance(stored, list):
            return list(stored)
        return family_catalog.default_checked_fetch_url()

    return multi_select_step(
        step_id="fetch_url",
        title="Web fetch backends",
        note=(
            "Pick the tools that read full page content from a URL. Web search "
            "backends other than Perplexity return only links, so they need one of "
            "these to be useful. The built-in Nymeria fetcher is on by default and "
            "needs no key (it uses your configured LLM)."
        ),
        choices=_choices(family_catalog.fetch_url_choices()),
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
            "are built and saved to your default thread tools). Each needs "
            "its provider API key set. The budget hosts (Replicate, fal.ai) cost "
            "far less than the flagship providers."
        ),
        choices=_choices(family_catalog.image_gen_choices()),
        get_initial=get_initial,
        store=store,
    )


def _voice_step(step_id: str, title: str, note: str,
                choices: "tuple[voice_catalog.VoiceChoice, ...]") -> Step:
    """Single-select over the voice catalog; the pick lands in extras[step_id].

    Initial value: this run's pick, else the on-disk provider recorded by
    hydrate (so a reconfigure shows the current setting), else off.
    """

    def get_initial(state: WizardState):
        value = state.extras.get(step_id)
        if isinstance(value, str):
            return value
        on_disk = state.extras.get(f"{step_id}_on_disk")
        return on_disk if isinstance(on_disk, str) else "none"

    def store(state: WizardState, value) -> None:
        state.extras[step_id] = value

    return single_select_step(
        step_id=step_id,
        title=title,
        note=note,
        choices=[Choice(c.value, c.label, c.description) for c in choices],
        get_initial=get_initial,
        store=store,
    )


def make_tts_step() -> Step:
    return _voice_step(
        step_id="tts",
        title="Text-to-speech",
        note=(
            "Pick the voice Nymeria speaks with (watch replies, Telegram voice "
            "notes). Local Kokoro is free and CPU-friendly; hosted picks "
            "collect their key on the next screen. Bare-metal local voice "
            "needs the voice extra: `pip install 'nymeriaos[voice-local]'`."
        ),
        choices=voice_catalog.TTS_CHOICES,
    )


def make_stt_step() -> Step:
    return _voice_step(
        step_id="stt",
        title="Speech-to-text",
        note=(
            "Pick how Nymeria transcribes voice messages (watch mic, Telegram "
            "voice notes). Local faster-whisper is free and CPU-friendly; "
            "hosted picks collect their key on the next screen."
        ),
        choices=voice_catalog.STT_CHOICES,
    )


def make_skill_kits_step() -> Step:
    """Real multi-select over the bundled, default-on capability kits (Section D).

    Seeds the chosen kit names into the bootstrap admin's `enabled_global_skills`
    (see `finalize.seed_bootstrap_profile`). The offered kits are discovered live
    from `skills_bundled/` (`family_catalog.skill_kit_choices`); the curated
    default-checked set is `family_catalog.default_checked_skill_kits`. Each kit
    only appears in the agent's skill index and loads its tools when activated, so
    leaving them on is cheap. The `self-improve` guidance skill stays on
    regardless of this pick.
    """
    _, store = _extras_list("skill_kits")

    def get_initial(state: WizardState) -> list[str]:
        stored = state.extras.get("skill_kits")
        if isinstance(stored, list):
            return list(stored)
        return family_catalog.default_checked_skill_kits()

    return multi_select_step(
        step_id="skill_kits",
        title="Default skill kits",
        note=(
            "Pick the capability kits Nymeria keeps on by default. Each shows up in "
            "the agent's skill index and loads its tools only when the task needs "
            "them, so leaving them on is cheap. The self-improve guidance skill "
            "stays on regardless. You can change these later in settings."
        ),
        choices=_choices(family_catalog.skill_kit_choices()),
        get_initial=get_initial,
        store=store,
    )


def seeded_tool_names(state: WizardState) -> list[str]:
    """Concrete tool names the init flow seeds from the built families.

    Only the built `web_search_*`, `fetch_url_*`, and `image_gen_*` families
    resolve to real tool names today; the placeholder families contribute nothing
    until their suites land. Shared by the review screen and finalize seeding
    (`tool_seed.default_thread_tools_for_state`) so they agree on one source.
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
    non-interactive run that did not pass `--skill-kit`), defaults to the curated
    default-on kit set, matching the interactive step's default check.
    `self-improve` is added separately by the finalize seeding, not here.
    """
    value = state.extras.get("skill_kits")
    if isinstance(value, list):
        return [str(item) for item in value]
    return family_catalog.default_checked_skill_kits()


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
