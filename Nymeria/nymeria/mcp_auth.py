"""Inbound authentication for the MCP streamable-HTTP server.

The MCP process is a thin client that talks to the backend with the admin
service token and forwards a caller-supplied ``user_id`` as ``X-Nymeria-Act-As``.
Without inbound auth, any reachable caller could invoke every tool against any
victim's data (full multi-tenant compromise). This module closes that hole:

* A pure-ASGI middleware requires ``Authorization: Bearer <token>`` on every
  HTTP request, resolves it against the backend ``GET /me`` (short TTL cache),
  and rejects unknown/missing tokens with ``401``.
* The resolved ``{user_id, role}`` is stored in a context var for the request.
* :func:`effective_act_as` pins non-admin callers to their own identity
  (ignoring any ``user_id`` argument) while letting admins keep Act-As.

STDIO mode (a local, trusted launch) sets no identity, so tool behavior there is
unchanged. The middleware passes non-HTTP scopes (lifespan) and ``OPTIONS``
straight through.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Resolved inbound identity for the current request, or None outside HTTP auth
# (e.g. STDIO mode). Shape: {"user_id": str, "role": str}.
_current_identity: contextvars.ContextVar[Optional[dict[str, str]]] = contextvars.ContextVar(
    "nymeria_mcp_identity", default=None
)

_RESOLVE_CACHE: dict[str, tuple[float, Optional[dict[str, str]]]] = {}
_RESOLVE_TTL_OK = 60.0
_RESOLVE_TTL_BAD = 10.0
_RESOLVE_CACHE_MAX = 512


def set_identity(identity: Optional[dict[str, str]]):
    """Set the resolved identity for the current context; returns the token."""
    return _current_identity.set(identity)


def reset_identity(token) -> None:
    _current_identity.reset(token)


def current_identity() -> Optional[dict[str, str]]:
    return _current_identity.get()


def effective_act_as(requested_user_id: Optional[str]) -> Optional[str]:
    """Resolve the Act-As user for a tool call given the inbound identity.

    - No inbound identity (STDIO/local): preserve the caller-supplied value.
    - Admin: honor an explicit non-default ``user_id`` (Act-As preserved),
      else act as the admin's own id.
    - Non-admin: always pinned to their own resolved id; the argument is
      ignored so a caller cannot read or mutate another user's data.
    """
    identity = _current_identity.get()
    if identity is None:
        return requested_user_id
    resolved = identity.get("user_id")
    if identity.get("role") == "admin":
        if requested_user_id and requested_user_id != "default":
            return requested_user_id
        return resolved
    return resolved


def _allow_unauthenticated() -> bool:
    return os.environ.get("NYMERIA_MCP_ALLOW_UNAUTHENTICATED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cache_get(token_hash: str) -> tuple[bool, Optional[dict[str, str]]]:
    entry = _RESOLVE_CACHE.get(token_hash)
    if entry is None:
        return False, None
    expiry, identity = entry
    if time.monotonic() >= expiry:
        _RESOLVE_CACHE.pop(token_hash, None)
        return False, None
    return True, identity


def _cache_put(token_hash: str, identity: Optional[dict[str, str]]) -> None:
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
        _RESOLVE_CACHE.clear()
    ttl = _RESOLVE_TTL_OK if identity is not None else _RESOLVE_TTL_BAD
    _RESOLVE_CACHE[token_hash] = (time.monotonic() + ttl, identity)


async def _resolve_token(base_url: str, token: str) -> Optional[dict[str, str]]:
    """Resolve an inbound bearer to ``{user_id, role}`` via backend ``/me``.

    Returns None for an invalid token or any backend failure (fail closed).
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    hit, identity = _cache_get(token_hash)
    if hit:
        return identity

    import httpx

    resolved: Optional[dict[str, str]] = None
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code == 200:
            data = response.json()
            user_id = data.get("id")
            if user_id:
                resolved = {"user_id": str(user_id), "role": str(data.get("role", "user"))}
        elif response.status_code in (401, 403):
            resolved = None
        else:
            logger.warning("MCP auth: unexpected /me status %s", response.status_code)
            resolved = None
    except Exception as exc:
        logger.warning("MCP auth: token resolution failed: %s", exc)
        resolved = None

    _cache_put(token_hash, resolved)
    return resolved


def _extract_bearer(scope) -> Optional[str]:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1").strip()
            if text.lower().startswith("bearer "):
                return text[7:].strip()
            return None
    return None


class MCPAuthMiddleware:
    """Require and resolve an inbound bearer token for MCP HTTP requests."""

    def __init__(self, app, *, resolve_base_url: Any):
        self.app = app
        # Callable returning the backend base URL at request time (the URL may
        # be configured after the app is constructed).
        self._resolve_base_url = resolve_base_url

    @staticmethod
    async def _reject(send, status: int, message: str) -> None:
        body = json.dumps({"error": message, "type": "unauthorized"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if _allow_unauthenticated():
            await self.app(scope, receive, send)
            return

        token = _extract_bearer(scope)
        if not token:
            await self._reject(send, 401, "Missing bearer token")
            return

        resolver = self._resolve_base_url
        base_url = str(resolver() if callable(resolver) else resolver)
        identity = await _resolve_token(base_url, token)
        if identity is None:
            await self._reject(send, 401, "Invalid or unknown bearer token")
            return

        ctx_token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(ctx_token)
