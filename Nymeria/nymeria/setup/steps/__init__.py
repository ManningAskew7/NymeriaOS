"""Wizard step screens and the default step list."""

from __future__ import annotations

from ..nav import Step
from ..quick import QUICK_KEEP_STEP_IDS, section_keep_ids
from .agent_settings import make_agent_settings_step
from .auth import make_auth_method_step
from .backend_keys import make_backend_keys_step
from .cliproxy import (
    make_cliproxy_disclaimer_step,
    make_cliproxy_endpoint_step,
    make_cliproxy_login_step,
    make_cliproxy_model_step,
    make_cliproxy_provider_step,
)
from .core_tools import make_core_tools_step
from .deployment import (
    make_docker_stack_step,
    make_external_access_step,
    make_security_profile_step,
)
from .external_access import (
    make_external_access_cloudflare_step,
    make_external_access_tailscale_step,
)
from .hosting import make_hosting_step
from .placeholders import (
    make_fetch_url_step,
    make_image_gen_step,
    make_skill_kits_step,
    make_stt_step,
    make_tts_step,
    make_web_search_step,
)
from .model import make_model_step
from .rag import make_embedder_step, make_reranker_step
from .provider import make_connection_step, make_provider_step
from .review import make_review_step
from .start_now import make_start_now_step
from .welcome import make_welcome_step


def _quick_gated(step: Step) -> Step:
    """Gate a non-essential step off when `WizardState.quick` is set.

    Composes with the step's own `applies` so conditional steps still drop out
    normally; steps in `QUICK_KEEP_STEP_IDS` pass through unchanged. Centralizing
    the quick filter here keeps the per-step factories unaware of the quick path.
    """
    if step.id in QUICK_KEEP_STEP_IDS:
        return step
    base_applies = step.applies
    return Step(
        id=step.id,
        applies=lambda state: not getattr(state, "quick", False) and base_applies(state),
        build=step.build,
    )


def build_default_steps() -> list[Step]:
    """The ordered wizard step list, mirroring the setup-wizard plan's flow.

    Phases: detect environment, pick a deployment target, shape the Docker stack
    and security posture, choose LLM auth and provider/model, configure optional
    capabilities, choose external access, then review and write. Conditional
    steps (the Docker stack on Docker hosts, connection on providers that need it)
    drop out via their `applies` predicate. Several steps are framework-real
    placeholders until their downstream automation lands. In `--quick` mode every
    step outside `QUICK_KEEP_STEP_IDS` is gated off (see `_quick_gated`).
    """

    steps = _default_step_list()
    return [_quick_gated(step) for step in steps]


def _default_step_list() -> list[Step]:
    return [
        # Detect environment.
        make_welcome_step(),
        # Choose a deployment target, then the Docker stack and security posture.
        make_hosting_step(),
        make_docker_stack_step(),
        make_security_profile_step(),
        # Choose LLM auth, then the provider, connection, and model. The
        # CLIProxy subscription branch replaces the provider/connection/model
        # trio when chosen; each side drops out via its applies predicate.
        make_auth_method_step(),
        make_cliproxy_disclaimer_step(),
        make_cliproxy_endpoint_step(),
        make_cliproxy_provider_step(),
        make_cliproxy_login_step(),
        make_cliproxy_model_step(),
        make_provider_step(),
        make_connection_step(),
        make_model_step(),
        # Tool families seeded on top of the default (seed) core set (core-toolset
        # plan, Sections A-C) and the voice providers, then the keys those
        # backends need, then the remaining capability placeholders.
        make_core_tools_step(),
        make_web_search_step(),
        make_fetch_url_step(),
        make_embedder_step(),
        make_reranker_step(),
        make_image_gen_step(),
        make_tts_step(),
        make_stt_step(),
        make_backend_keys_step(),
        make_skill_kits_step(),
        make_agent_settings_step(),
        # External access (the choice, then the guided tailscale/cloudflare
        # setup), then offer to start the backend, then review and write.
        make_external_access_step(),
        make_external_access_tailscale_step(),
        make_external_access_cloudflare_step(),
        make_start_now_step(),
        make_review_step(),
    ]


def default_step_ids() -> list[str]:
    """Ordered ids of every wizard step (the valid `init <section>` names)."""
    return [step.id for step in _default_step_list()]


def _section_gated(step: Step, keep: frozenset[str]) -> Step:
    """Gate a step off unless its id is in the section keep-set.

    Mirrors `_quick_gated`: kept steps retain their own `applies` (so conditional
    members like `connection`/`backend_keys`/`reranker` still drop out when
    irrelevant); everything else is made inapplicable.
    """
    if step.id in keep:
        return step
    return Step(id=step.id, applies=lambda _state: False, build=step.build)


def build_section_steps(section: str) -> list[Step]:
    """The default step list filtered to a single `init <section>` jump.

    Keeps the section, its dependency closure, and the welcome/review bookends
    (see `quick.section_keep_ids`); all other steps are gated off.
    """
    keep = section_keep_ids(section)
    return [_section_gated(step, keep) for step in _default_step_list()]


__all__ = ["build_default_steps", "build_section_steps", "default_step_ids"]
