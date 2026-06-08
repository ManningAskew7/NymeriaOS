"""Wizard step screens and the default step list."""

from __future__ import annotations

from ..nav import Step
from .agent_settings import make_agent_settings_step
from .auth import make_auth_method_step
from .backend_keys import make_backend_keys_step
from .core_tools import make_core_tools_step
from .deployment import (
    make_external_access_step,
    make_image_tier_step,
    make_security_profile_step,
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
from .welcome import make_welcome_step


def build_default_steps() -> list[Step]:
    """The ordered wizard step list, mirroring the setup-wizard plan's flow.

    Phases: detect environment, pick a deployment target, shape the image and
    security posture, choose LLM auth and provider/model, configure optional
    capabilities, choose external access, then review and write. Conditional
    steps (image tier on container hosts, connection on providers that need it)
    drop out via their `applies` predicate. Several steps are framework-real
    placeholders until their downstream automation lands.
    """

    return [
        # Detect environment.
        make_welcome_step(),
        # Choose a deployment target, then shape the container image and posture.
        make_hosting_step(),
        make_image_tier_step(),
        make_security_profile_step(),
        # Choose LLM auth, then the provider, connection, and model.
        make_auth_method_step(),
        make_provider_step(),
        make_connection_step(),
        make_model_step(),
        # Tool families seeded on top of the always-on core set (core-toolset
        # plan, Sections A-C), the keys those backends need, then the remaining
        # capability placeholders.
        make_core_tools_step(),
        make_web_search_step(),
        make_fetch_url_step(),
        make_embedder_step(),
        make_reranker_step(),
        make_image_gen_step(),
        make_backend_keys_step(),
        make_skill_kits_step(),
        make_tts_step(),
        make_stt_step(),
        make_agent_settings_step(),
        # External access, then review and write.
        make_external_access_step(),
        make_review_step(),
    ]


__all__ = ["build_default_steps"]
