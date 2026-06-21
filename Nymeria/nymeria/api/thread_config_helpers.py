"""Shared helpers for thread configuration API routes."""

import re
import uuid
from typing import Any, NamedTuple

from fastapi import HTTPException

_CALLABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_THREAD_TEAM_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


class EffectiveLLM(NamedTuple):
    """A thread's resolved provider/model after applying global fallbacks."""

    provider: str
    model: str


def effective_provider_model(agent: Any, thread_id: str) -> EffectiveLLM:
    """Resolve the effective LLM provider and model for a thread.

    A thread-level ``llm_config`` override wins; otherwise the global
    ``settings`` defaults apply. Returned as a named tuple so a caller that
    only needs the model can read ``.model`` and one that needs both can
    unpack ``provider, model``. This single rule had been re-expressed at
    five call sites across the chat and thread-operation routers.
    """
    cfg = agent._get_llm_config_for_thread(thread_id)
    return EffectiveLLM(
        provider=cfg.provider or agent.settings.llm_provider,
        model=cfg.model or agent.settings.llm_model,
    )


def validate_callable_name(name: str) -> None:
    """Reject callable names that would fail LLM tool/function binding."""
    if not _CALLABLE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid callable name '{name}': must match "
                "[a-zA-Z0-9_-]{1,64} for LLM tool binding (no spaces, dots, "
                "or punctuation)."
            ),
        )


def normalize_thread_team_name(name: str) -> str:
    """Normalize a user-provided callable-team display name."""
    normalized = " ".join((name or "").strip().split())
    if not normalized:
        raise HTTPException(status_code=400, detail="Team name is required")
    if len(normalized) > 120:
        raise HTTPException(
            status_code=400,
            detail="Team name must be 120 characters or fewer",
        )
    return normalized


def make_thread_team_id(name: str) -> str:
    """Build a stable-enough team id with a readable slug plus random suffix."""
    slug = _THREAD_TEAM_SLUG_RE.sub("-", name.strip().lower()).strip("-_")
    if not slug:
        slug = "team"
    return f"team-{slug[:48]}-{uuid.uuid4().hex[:8]}"
