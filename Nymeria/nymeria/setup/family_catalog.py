"""Live catalog of init-selectable tool families and skill kits (TUI-free).

The growing-list wizard steps (web_search / fetch_url / image_gen backends and
the default-on skill kits) used to hardcode their option lists, which silently
drifted from the runtime catalogs. This module derives the *membership* of each
family from the live runtime source instead, so adding a backend tool or a
bundled kit surfaces it in the wizard with no edit here.

Presentation (label + short description) comes from a per-tool override map that
preserves the curated wizard copy; a tool with no override falls back to its own
``.description`` (LLM-facing, but a safe default until nicer copy is added). The
default-checked sets are kept separate from the offered set on purpose.

Kept TUI-free (returns the plain ``FamilyChoice`` dataclass, not the Textual
``Choice``) so the headless finalize/hydration paths can import it without
pulling in Textual. All heavy imports (``nymeria.tools``, ``nymeria.skills``)
are function-local, mirroring ``steps/core_tools.py`` and ``rag_catalog.py``,
to keep the ``setup`` package import-light.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FamilyChoice:
    """A selectable family member: ``value`` is the concrete tool/kit name."""

    value: str
    label: str
    description: str


# --- presentation overrides (verbatim from the previous hardcoded literals) ---
# Keyed by the concrete tool/skill name. Membership is derived live; only the
# label/description live here. A missing entry degrades to the tool's own
# .description, so a newly registered backend still renders (just less prettily).

_WEB_SEARCH_LABELS: dict[str, tuple[str, str]] = {
    "web_search_perplexity": (
        "Perplexity",
        "Synthesized answers; self-sufficient (needs no web fetch backend).",
    ),
    "web_search_tavily": ("Tavily", "Links and previews; pair with a web fetch backend."),
    "web_search_exa_ai": ("Exa", "Links and previews; pair with a web fetch backend."),
    "web_search_firecrawl": ("Firecrawl", "Links and previews; pair with a web fetch backend."),
    "web_search_brave": ("Brave Search", "Links and previews; pair with a web fetch backend."),
    "web_search_searxng": ("SearXNG", "Self-hosted; pair with a web fetch backend."),
    "web_search_ddgs": (
        "DDGS metasearch",
        "Keyless, no setup; rotates scraped engines; pair with a web fetch backend.",
    ),
}

_FETCH_URL_LABELS: dict[str, tuple[str, str]] = {
    "fetch_url_nymeria": (
        "Nymeria fetch",
        "Built-in fetcher that can extract from a page with a configurable model.",
    ),
    "jina_reader_fetch_url": ("Jina Reader", "Hosted reader endpoint (key required)."),
}

_IMAGE_GEN_LABELS: dict[str, tuple[str, str]] = {
    "image_gen_openai": (
        "OpenAI GPT Image",
        "Top-ranked all-rounder: photorealism, prompt adherence, in-image text (key required).",
    ),
    "image_gen_gemini": (
        "Google Gemini (Nano Banana Pro)",
        "Best for in-image text, infographics/diagrams, and 4K output (key required).",
    ),
    "image_gen_flux": (
        "Black Forest Labs FLUX.2",
        "Flagship photorealism and multi-reference; async submit and poll (key required).",
    ),
    "image_gen_replicate": (
        "Replicate (budget)",
        "Low-cost host for FLUX.1 schnell or Z-Image Turbo; good for drafts (key required).",
    ),
    "image_gen_fal": (
        "fal.ai (budget)",
        "Fast low-cost host for Z-Image Turbo or FLUX.1 schnell (key required).",
    ),
}

_SKILL_KIT_LABELS: dict[str, tuple[str, str]] = {
    "tool-management": (
        "Tool management",
        "Find, enable, and build tools, including HTTP/API-backed ones.",
    ),
    "skill-management": (
        "Skill management",
        "Find, install, create, and edit Skills and Skill Kits.",
    ),
    "mcp-management": (
        "MCP management",
        "Find, install, test, and manage MCP servers.",
    ),
    "credential-management": (
        "Credential management",
        "Request, inspect, and clean up service credentials and connections.",
    ),
    "callable-thread-builder": (
        "Callable thread builder",
        "Build callable threads: specialist threads other threads use as tools.",
    ),
    "trigger-management": (
        "Trigger management",
        "Create and manage trigger sources (webhooks, RSS, polls, email).",
    ),
}

# The curated default-checked kit set: the six kits that existed when the
# default was widened on 2026-06-12. Deliberately a literal, NOT derived from
# skill_kit_choices: newly bundled kits are offered but not auto-checked, so
# bundling a kit is never silently a default-on decision (users can still
# enable them globally in Settings or per thread).
_DEFAULT_CHECKED_KITS: tuple[str, ...] = (
    "tool-management",
    "skill-management",
    "mcp-management",
    "credential-management",
    "callable-thread-builder",
    "trigger-management",
)

# Keyless web fetch default: extracts from pages with the configured primary LLM,
# so it needs no separate key. Single source of truth for the full path's default
# check and the quick path (see quick.QUICK_FETCH_DEFAULT).
_DEFAULT_FETCH_URL = ["fetch_url_nymeria"]


def _present(value: str, overrides: dict[str, tuple[str, str]], fallback_desc: str) -> FamilyChoice:
    label, desc = overrides.get(value, (_prettify(value), fallback_desc))
    return FamilyChoice(value=value, label=label, description=desc)


def _prettify(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().capitalize()


def _family_choices(tools, overrides: dict[str, tuple[str, str]]) -> list[FamilyChoice]:
    return [_present(t.name, overrides, t.description) for t in tools]


def web_search_choices() -> list[FamilyChoice]:
    """All registered web_search_* backends, in runtime order."""
    from ..tools import WEB_SEARCH_SERVICE_TOOLS, WEB_SEARCH_INTEGRATION_TOOLS

    return _family_choices(
        [*WEB_SEARCH_SERVICE_TOOLS, *WEB_SEARCH_INTEGRATION_TOOLS], _WEB_SEARCH_LABELS
    )


def fetch_url_choices() -> list[FamilyChoice]:
    """The fetch_url family: the built-in fetcher plus the Jina reader.

    ``jina_reader_fetch_url`` lives in a different runtime tool group, so it is
    referenced by name rather than via a single group list.
    """
    from ..tools import WEB_FETCH_TOOLS, jina_reader_fetch_url

    return _family_choices([*WEB_FETCH_TOOLS, jina_reader_fetch_url], _FETCH_URL_LABELS)


def image_gen_choices() -> list[FamilyChoice]:
    """All registered image_gen_* providers, in runtime order."""
    from ..tools import IMAGE_GEN_INTEGRATION_TOOLS

    return _family_choices(IMAGE_GEN_INTEGRATION_TOOLS, _IMAGE_GEN_LABELS)


def skill_kit_choices() -> list[FamilyChoice]:
    """Bundled capability kits discovered live from ``skills_bundled/``.

    A kit is a bundled, non-internal Skill that binds required tools
    (``Skill.is_skill_kit``). Discovering them live means a newly bundled kit
    appears in the wizard with no edit here. Falls back to the curated default
    set (with its nice labels) if the bundled dir cannot be scanned.
    """
    bundled: dict = {}
    try:
        from ..config.settings import Settings
        from ..skills import SkillManager

        bundled = SkillManager._scan_dir(Settings().bundled_skills_dir, scope="bundled")
    except Exception:
        bundled = {}
    kits = [
        (name, skill)
        for name, skill in sorted(bundled.items())
        if skill.is_skill_kit and not skill.is_internal
    ]
    if not kits:
        # Resilient fallback: the curated default-on kits with their known copy.
        return [
            FamilyChoice(value=name, label=label, description=desc)
            for name, (label, desc) in _SKILL_KIT_LABELS.items()
        ]
    return [
        _present(name, _SKILL_KIT_LABELS, getattr(skill, "description", "") or "")
        for name, skill in kits
    ]


def default_checked_skill_kits() -> list[str]:
    """The curated default-on kit set (all six existing kits as of 2026-06-12).

    Widened from the four-kit ``DEFAULT_GLOBAL_SKILLS`` subset so fresh
    installs start with the full current toolkit (kits load their tools only
    on activation, so on-by-default is cheap), but still decoupled from the
    offered set (``skill_kit_choices``): newly bundled kits are offered, not
    auto-checked. The backend fallback for profiles that never chose
    (``user_profile.DEFAULT_GLOBAL_SKILLS``) intentionally stays its own
    narrower set so existing users are unaffected. ``self-improve`` is a
    guidance skill, not a kit, and is added on separately by finalize seeding.
    """
    return list(_DEFAULT_CHECKED_KITS)


def default_checked_fetch_url() -> list[str]:
    return list(_DEFAULT_FETCH_URL)


__all__ = [
    "FamilyChoice",
    "web_search_choices",
    "fetch_url_choices",
    "image_gen_choices",
    "skill_kit_choices",
    "default_checked_skill_kits",
    "default_checked_fetch_url",
]
