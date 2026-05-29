"""Request body-size limit ASGI middleware.

Rejects oversized request bodies *before* route handlers buffer them. Without
this, an unauthenticated caller can POST an arbitrarily large body to a webhook
endpoint (``await request.body()`` runs before the signature check) and exhaust
memory on a small host. Enforcement is two-stage:

1. A fast 413 when a declared ``Content-Length`` already exceeds the cap.
2. A streamed byte counter wrapped around ``receive`` so a client that omits or
   understates ``Content-Length`` (chunked transfer-encoding) still cannot
   stream past the cap.

Caps are per path-prefix (longest prefix wins) with a global default.
"""

from __future__ import annotations

import json
from typing import Iterable

# Methods that never carry a meaningful request body; skip the wrapper for them.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE", "TRACE"})


class _RequestBodyTooLarge(Exception):
    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"request body exceeds {limit} bytes")


class BodySizeLimitMiddleware:
    """ASGI middleware that caps inbound request body size per path prefix."""

    def __init__(
        self,
        app,
        *,
        default_limit: int,
        path_limits: Iterable[tuple[str, int]] = (),
    ) -> None:
        self.app = app
        self.default_limit = int(default_limit)
        # Longest prefix first so e.g. "/voice/stt" can override "/voice".
        self.path_limits = sorted(
            ((str(prefix), int(limit)) for prefix, limit in path_limits),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def _limit_for(self, path: str) -> int:
        for prefix, limit in self.path_limits:
            if path.startswith(prefix):
                return limit
        return self.default_limit

    @staticmethod
    async def _send_413(send, limit: int) -> None:
        body = json.dumps(
            {"detail": f"Request body exceeds the {limit} byte limit"}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(scope.get("path", ""))

        # Stage 1: fast reject on a declared, oversized Content-Length.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await self._send_413(send, limit)
                        return
                except (ValueError, TypeError):
                    # Malformed Content-Length: skip the fast path and let the
                    # streamed byte counter enforce the cap instead.
                    pass
                break

        # Stage 2: cap the streamed body for chunked / understated requests.
        total = 0
        response_started = False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b"") or b"")
                if total > limit:
                    raise _RequestBodyTooLarge(limit)
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _RequestBodyTooLarge as exc:
            if response_started:
                # The handler already began responding; we cannot cleanly send
                # a 413 over a started response, so let it surface.
                raise
            await self._send_413(send, exc.limit)
