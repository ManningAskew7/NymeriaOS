"""Inbound authentication for the MCP streamable-HTTP server.

The MCP process is a thin client that talks to the backend with the admin
service token and forwards a caller-supplied ``user_id`` as ``X-Nymeria-Act-As``.
Without inbound auth, any reachable caller could invoke every tool against any
victim's data (full multi-tenant compromise). This module closes that hole:

* A pure-ASGI middleware requires ``Authorization: Bearer <token>`` on every
  HTTP request, resolves it against the backend ``GET /me`` (short TTL cache),
  and rejects unknown/missing tokens with ``401``.
* A backend that cannot answer (restarting, erroring, misconfigured) is NOT a
  bad credential: that answers ``503`` with ``Retry-After`` and no Bearer
  challenge, and is never cached (#374). A ``401`` + ``WWW-Authenticate``
  tells an MCP client to authenticate, so answering it during a compose
  restart made clients drop the server as needing auth and never reconnect.
  Access stays fail-closed either way.
* Before answering that ``503`` the request is HELD while the backend comes
  back (#424): it re-resolves with backoff for up to
  ``NYMERIA_MCP_BACKEND_WAIT_SECONDS`` (default 25, 0 disables), so a client
  connecting during an API restart gets a slow answer instead of an error.
  MCP clients retry a failed connect only a few times and ignore
  ``Retry-After``, which is why the 503 alone was not enough. Only a
  Nymeria-shaped bearer on a GET/POST is held, and at most
  ``_MAX_HELD_REQUESTS`` at once, so junk tokens cannot pile holds onto the
  event loop during an outage.
* The resolved ``{user_id, role}`` is stored in a context var for the request.
* :func:`effective_act_as` pins non-admin callers to their own identity
  (ignoring any ``user_id`` argument) while letting admins keep Act-As.

STDIO mode (a local, trusted launch) sets no identity, so tool behavior there is
unchanged. The middleware passes non-HTTP scopes (lifespan) and ``OPTIONS``
straight through.
"""

from __future__ import annotations

import asyncio
import contextvars
import enum
import functools
import hashlib
import json
import logging
import os
import ssl
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
# Seconds a client is told to wait when the backend cannot resolve a token.
_RETRY_AFTER_SECONDS = 5
# How long a request is held while the backend cannot answer, before the 503
# (#424). 25 s covers the measured MCP-up/API-down window of a stack restart
# (17 to 20 s, #374) with margin, and stays inside an MCP client's connect
# budget (Claude Code's MCP_TIMEOUT defaults to 30 s): keep it below the
# client's. The cap keeps a typo from parking requests for hours.
_BACKEND_WAIT_ENV = "NYMERIA_MCP_BACKEND_WAIT_SECONDS"
_BACKEND_WAIT_DEFAULT = 25.0
_BACKEND_WAIT_MAX = 120.0
# Concurrent holds beyond this answer 503 at once. A held request costs a
# coroutine and a cheap /me attempt every second or two; the cap bounds what
# a spray of token-shaped junk can park on the event loop during an outage.
_MAX_HELD_REQUESTS = 32
_held_requests = 0
# Only these methods are held: DELETE on this stateless server always ends in
# 405, so delaying it helps no one.
_HELD_METHODS = frozenset({"GET", "POST"})
# Pause between re-resolutions while held. A restarting API refuses the
# connection at once, so polling is cheap; the steps keep the first recovery
# check quick and the rest gentle.
_BACKEND_POLL_STEPS = (0.25, 0.5, 1.0, 2.0)
# The resolver's per-attempt HTTP timeout.
_RESOLVE_TIMEOUT = 10.0
# Clock and sleep, injectable so tests drive the hold without real waiting.
_monotonic = time.monotonic
_sleep = asyncio.sleep
# The resolver's HTTP transport: None in production; tests inject an
# ``httpx.MockTransport`` to drive the real resolver through the middleware.
_HTTP_TRANSPORT: Any = None


class _Resolution(enum.Enum):
    """A resolution outcome that is neither an identity nor a rejected token."""

    UNAVAILABLE = "unavailable"  # no answer: restarting, timing out, 5xx (503)
    UNEXPECTED = "unexpected"  # an answer that is not a Nymeria /me (502)


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


def _backend_wait_seconds() -> float:
    """The configured hold, clamped to [0, _BACKEND_WAIT_MAX]; bad values default."""
    return _parse_backend_wait(os.environ.get(_BACKEND_WAIT_ENV, "").strip())


@functools.lru_cache(maxsize=8)
def _parse_backend_wait(raw: str) -> float:
    """Parse once per distinct value, so a bad one warns once, not per request.

    Negative counts as 0 (no hold); NaN and non-numbers fall back to the default.
    """
    if not raw:
        return _BACKEND_WAIT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if value != value:  # NaN, or not a number at all
        logger.warning(
            "MCP auth: %s=%r is not a number; using %.0f s",
            _BACKEND_WAIT_ENV,
            raw,
            _BACKEND_WAIT_DEFAULT,
        )
        return _BACKEND_WAIT_DEFAULT
    return max(0.0, min(value, _BACKEND_WAIT_MAX))


@functools.cache
def _tls_context() -> ssl.SSLContext:
    """One TLS context for every resolution, with httpx's default trust.

    Building an ``httpx.AsyncClient`` without one loads the CA bundle
    synchronously, about 35 ms (100 ms in the 0.5-CPU container) of blocked
    event loop per /me attempt; a held request makes a dozen attempts, so
    that cost multiplied across concurrent holds froze the loop (#424 review).
    """
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def _holdable(scope, token: str) -> bool:
    """Whether this request may be held through an outage.

    Only a bearer shaped like a Nymeria account token (junk cannot buy a
    hold) on a method worth delaying.
    """
    from .core.accounts import TOKEN_PREFIX

    return token.startswith(TOKEN_PREFIX) and scope.get("method") in _HELD_METHODS


async def _resolve_riding_out_restarts(
    base_url: str, token: str, *, hold: bool = True
) -> Optional[dict[str, str]] | _Resolution:
    """``_resolve_token``, re-tried while the backend cannot answer (#424).

    Only ``UNAVAILABLE`` is retried: a rejected token and a non-Nymeria answer
    are final. Every attempt's HTTP timeout is capped to the time left (0.5 s
    minimum), so a blackholed API cannot overrun the hold by a whole client
    timeout. Each attempt checks the token cache first, so a request held
    while another one resolved the same token picks that up. Without
    ``hold``, or past ``_MAX_HELD_REQUESTS`` concurrent holds, this is one
    plain attempt.
    """
    global _held_requests

    wait = _backend_wait_seconds() if hold else 0.0
    first = await _resolve_token(
        base_url, token, timeout=min(_RESOLVE_TIMEOUT, max(0.5, wait)) if wait else _RESOLVE_TIMEOUT
    )
    if first is not _Resolution.UNAVAILABLE or not wait:
        return first
    if _held_requests >= _MAX_HELD_REQUESTS:
        logger.warning(
            "MCP auth: %d requests already held for the backend; answering 503 at once",
            _held_requests,
        )
        return first
    _held_requests += 1
    try:
        return await _hold_for_backend(base_url, token, wait)
    finally:
        _held_requests -= 1


async def _hold_for_backend(
    base_url: str, token: str, wait: float
) -> Optional[dict[str, str]] | _Resolution:
    """Re-resolve with backoff until the backend answers or ``wait`` passes.

    Logs at info when a hold ends well and at warning if it gives up; the
    per-attempt outage lines are debug.
    """
    started = _monotonic()
    deadline = started + wait
    attempt = 0
    outcome: Optional[dict[str, str]] | _Resolution = _Resolution.UNAVAILABLE
    while True:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            logger.warning(
                "MCP auth: backend still not answering after %.1f s; answering 503",
                _monotonic() - started,
            )
            return outcome
        step = _BACKEND_POLL_STEPS[min(attempt, len(_BACKEND_POLL_STEPS) - 1)]
        await _sleep(min(step, remaining))
        attempt += 1
        timeout = min(_RESOLVE_TIMEOUT, max(0.5, deadline - _monotonic()))
        outcome = await _resolve_token(
            base_url, token, timeout=timeout, log_unavailable=False
        )
        if outcome is not _Resolution.UNAVAILABLE:
            logger.info(
                "MCP auth: backend answered after %.1f s; request held, not refused",
                _monotonic() - started,
            )
            return outcome


async def _resolve_token(
    base_url: str,
    token: str,
    *,
    timeout: float = _RESOLVE_TIMEOUT,
    log_unavailable: bool = True,
) -> Optional[dict[str, str]] | _Resolution:
    """Resolve an inbound bearer to ``{user_id, role}`` via backend ``/me``.

    Returns the identity; ``None`` when the backend REJECTED the token (401 or
    403, cached briefly; 429 too, uncached, see below);
    ``_Resolution.UNAVAILABLE`` when it could not answer (connection error,
    timeout, 5xx); or ``_Resolution.UNEXPECTED`` for an answer that is not a
    Nymeria ``/me`` (another status, e.g. a base URL pointing elsewhere, or a
    200 without an identity). Neither says anything about the token, so
    neither is cached. Every non-identity outcome denies access.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    hit, identity = _cache_get(token_hash)
    if hit:
        return identity

    import httpx

    # A held request re-resolves every second or so; its retries log at debug
    # so a restart costs each request its first warning (and one more only if
    # the hold gives up), not a dozen.
    log_outage = logger.warning if log_unavailable else logger.debug
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            transport=_HTTP_TRANSPORT,
            verify=_tls_context(),
        ) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        log_outage("MCP auth: backend unavailable for token resolution: %s", exc)
        return _Resolution.UNAVAILABLE

    if response.status_code in (401, 403):
        _cache_put(token_hash, None)
        return None
    if response.status_code == 429:
        # The API's per-IP auth-FAILURE limiter (triggers/api.py) is the only
        # source of a /me 429 and runs only after the token failed, so this is
        # a rejection. Every MCP resolution shares one client IP there, so a
        # spray of junk tokens trips it for everyone: answering 503 would turn
        # every genuinely revoked token into "retry shortly" (a client would
        # never re-authenticate). Not cached: that keeps a junk spray from
        # filling the cache, whose overflow clears valid identities too.
        return None
    if response.status_code >= 500:
        log_outage(
            "MCP auth: backend unavailable for token resolution: /me answered %s",
            response.status_code,
        )
        return _Resolution.UNAVAILABLE
    if response.status_code != 200:
        logger.warning(
            "MCP auth: /me answered %s; is the MCP server's API URL a Nymeria API?",
            response.status_code,
        )
        return _Resolution.UNEXPECTED
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("id"):
        logger.warning("MCP auth: /me answered 200 without an identity")
        return _Resolution.UNEXPECTED
    resolved = {"user_id": str(data["id"]), "role": str(data.get("role", "user"))}
    _cache_put(token_hash, resolved)
    return resolved


def _extract_bearer(scope) -> Optional[str]:
    """The bearer token, or None when absent or not RFC 6750 token-shaped
    (printable ASCII, no whitespace): such a value cannot be a Nymeria token,
    and forwarding it would fail inside the HTTP client as a false outage."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1").strip()
            if text.lower().startswith("bearer "):
                token = text[7:].strip()
                if token and all("!" <= ch <= "~" for ch in token):
                    return token
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
        # Only a 401 challenges for a bearer; a 5xx never says "authenticate".
        kind = {401: "unauthorized", 502: "unexpected"}.get(status, "unavailable")
        body = json.dumps({"error": message, "type": kind}).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        elif status == 503:
            headers.append((b"retry-after", str(_RETRY_AFTER_SECONDS).encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
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
        identity = await _resolve_riding_out_restarts(
            base_url, token, hold=_holdable(scope, token)
        )
        if identity is _Resolution.UNAVAILABLE:
            await self._reject(
                send, 503, "The Nymeria backend is not answering yet; retry shortly"
            )
            return
        if identity is _Resolution.UNEXPECTED:
            await self._reject(
                send, 502, "The MCP server's backend did not answer as a Nymeria API"
            )
            return
        if identity is None:
            await self._reject(send, 401, "Invalid or unknown bearer token")
            return

        ctx_token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_identity(ctx_token)
