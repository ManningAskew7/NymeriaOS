"""Wizard step screens and the default step list."""

from __future__ import annotations

from ..nav import Step
from .agent_settings import make_agent_settings_step
from .hosting import make_hosting_step
from .placeholders import (
    make_embeddings_step,
    make_image_gen_step,
    make_stt_step,
    make_tools_step,
    make_tts_step,
    make_web_search_step,
)
from .provider import make_connection_step, make_provider_step
from .review import make_review_step


def build_default_steps() -> list[Step]:
    """The ordered wizard step list (real steps + placeholders + review)."""

    return [
        make_hosting_step(),
        make_provider_step(),
        make_connection_step(),
        make_web_search_step(),
        make_embeddings_step(),
        make_image_gen_step(),
        make_tts_step(),
        make_stt_step(),
        make_tools_step(),
        make_agent_settings_step(),
        make_review_step(),
    ]


__all__ = ["build_default_steps"]
