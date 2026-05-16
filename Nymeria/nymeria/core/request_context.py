"""Per-request context for logs and cross-cutting diagnostics."""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar(
    "nymeria_request_id",
    default=None,
)


def get_request_id() -> str | None:
    """Return the request ID active in the current context, if any."""
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str | None]:
    """Set the active request ID and return a token for resetting it."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Reset the active request ID to its previous value."""
    _request_id.reset(token)
