"""Shared helpers for thread configuration API routes."""

import re
import uuid

from fastapi import HTTPException

_CALLABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_THREAD_TEAM_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


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
