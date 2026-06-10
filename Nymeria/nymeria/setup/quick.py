"""Quick-path (`--quick`) step subset and defaults.

`nymeria init --quick` asks only the irreducible questions (hosting and the LLM
provider/connection/model/key) and applies sensible, no-extra-auth defaults for
everything else, so a user reaches a working install with the fewest prompts and
zero paid services. The interactive flow honors this two ways: the step list is
gated to `QUICK_KEEP_STEP_IDS` (see `steps.build_default_steps`), and
`apply_quick_defaults` seeds the skipped steps' defaults onto `WizardState`
before the wizard runs so the review screen is accurate.

Defaults chosen for zero extra auth or cost:
- RAG: the free, private local stack (granite + Ettin), the same default a RAG
  skip applies (see `rag_catalog.apply_quickstart_rag`).
- Web fetch: `fetch_url_nymeria`, which distills pages with your already
  configured primary LLM, so it needs no separate key. (It is also the
  default-checked option in the full path's fetch step.)
- Web search and image generation: none seeded. Every web_search backend needs
  either an API key (Perplexity, Tavily, Exa, Firecrawl, Brave) or a self-hosted
  instance URL (SearXNG), and every image_gen provider needs a key, so none is
  truly zero-config. The user adds one later in settings.
- Skill kits: left unset, which the seeding fallback resolves to all bundled
  kits on (they load on demand, so this is cheap).
- Docker stack: slim (the single-container shape), the simplest single-user
  default; only consulted when hosting is Docker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..onboarding import DockerStack
from . import family_catalog
from .rag_catalog import apply_quickstart_rag

if TYPE_CHECKING:
    from .state import WizardState


# The only steps the quick path still prompts for; every other step is gated off
# and defaulted. Hosting and the LLM (provider/connection/model/key) cannot be
# defaulted; start_now stays a single end question; welcome and review are the
# entry and confirmation screens. The cliproxy_* steps are the LLM essentials
# of the subscription branch (entered in quick mode via --auth-method, since
# the auth step itself is quick-gated); their own applies predicates keep them
# off the API-key path.
QUICK_KEEP_STEP_IDS = frozenset(
    {
        "welcome",
        "hosting",
        "provider",
        "connection",
        "model",
        "cliproxy_disclaimer",
        "cliproxy_endpoint",
        "cliproxy_provider",
        "cliproxy_login",
        "cliproxy_model",
        "start_now",
        "review",
    }
)

# Keyless web fetch default: distills pages with the configured primary LLM, so
# it needs no separate key. Seeded into default_thread_tools via the fetch_url
# family (see steps/placeholders.seeded_tool_names). Single source of truth lives
# in family_catalog so the full-path fetch step and the quick path agree.
QUICK_FETCH_DEFAULT = family_catalog.default_checked_fetch_url()


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

    Idempotent and meant to run once before the wizard starts so the review
    screen reflects the defaults. Only fills what was not already set, so
    explicit flags (e.g. ``--fetch-url``, ``--embedding-api-key``) still win.
    """
    # Free local RAG (same default a RAG skip applies); marks rag_quickstarted so
    # review shows it and the reranker step stays consistent. The condition keeps
    # an explicit embedder pick or embedding key untouched.
    if state.embedder is None and not state.optional_env.get("EMBEDDING_API_KEY"):
        apply_quickstart_rag(state)
    # Keyless web fetch, unless a flag already seeded the fetch family.
    if not state.extras.get("fetch_url"):
        state.extras["fetch_url"] = list(QUICK_FETCH_DEFAULT)
    # Default the Docker shape to slim (simplest single-user container); the
    # docker_stack step is gated off in quick mode, so seed it for review.
    if state.docker_stack is None:
        state.docker_stack = DockerStack.SLIM


__all__ = [
    "QUICK_KEEP_STEP_IDS",
    "QUICK_FETCH_DEFAULT",
    "apply_quick_defaults",
    "SECTION_DEPENDENCIES",
    "section_keep_ids",
]
