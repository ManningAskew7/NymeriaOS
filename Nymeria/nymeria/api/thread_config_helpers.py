"""Shared helpers for thread configuration API routes."""

from typing import Any, NamedTuple

from fastapi import HTTPException

from ..core.callable_names import CALLABLE_NAME_RE


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
    """Reject callable names that would fail LLM tool/function binding.

    The grammar (``^[a-zA-Z0-9_-]{1,64}$``) is shared with the branch and import
    callable-name paths via :data:`nymeria.core.callable_names.CALLABLE_NAME_RE`,
    so those copies cannot silently drift. The agent-threads create endpoint
    enforces the same shape through a separate Pydantic
    ``Field(pattern=..., min_length, max_length)`` that is not wired to this
    constant.
    """
    if not CALLABLE_NAME_RE.match(name):
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


