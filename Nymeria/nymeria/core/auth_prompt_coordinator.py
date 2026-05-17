"""In-process rendezvous between the agent's ``request_credential`` tool and
the credential-prompt HTTP endpoints.

The flow:

1. ``request_credential`` (an async tool) creates a pending credential, then
   calls :meth:`AuthPromptCoordinator.register` to get an ``asyncio.Future``.
2. The tool publishes an ``auth_prompt`` SSE event with the ``prompt_id`` and
   awaits the future (up to a per-tool timeout — typically 180s).
3. The desktop frontend renders a modal. When the user submits, the
   ``POST /api/credential-prompts/{prompt_id}/submit`` endpoint writes the
   secrets and either:
       - on test failure, returns the error inline (modal shows retry) and
         calls :meth:`record_attempt` to track the failure
       - on test success, calls :meth:`resolve` to wake the tool
4. ``/exit`` and ``/cancel`` similarly resolve the future with a non-success
   payload so the agent can react.

Single-process only: the future lives in this process. If the API is ever
scaled to multiple workers, the resolver path needs to use Redis pub/sub
to fan out to whichever worker is awaiting the future.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How long an orphaned prompt sits in memory before it's swept. Slightly
# longer than the longest expected per-tool timeout (180s) so a graceful
# tool-side timeout always wins the race.
_ORPHAN_TTL_SECONDS = 600
_SWEEP_INTERVAL_SECONDS = 60


@dataclass
class PendingPrompt:
    prompt_id: str
    credential_id: str
    user_id: str
    thread_id: str
    provider: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    last_test_error: Optional[str] = None


class AuthPromptCoordinator:
    """Tracks in-flight credential prompts keyed by ``prompt_id``."""

    def __init__(self) -> None:
        self._prompts: dict[str, PendingPrompt] = {}
        self._lock = threading.Lock()
        self._sweep_task: Optional[asyncio.Task] = None
        self._sweep_started = False

    def register(
        self,
        *,
        prompt_id: str,
        credential_id: str,
        user_id: str,
        thread_id: str,
        provider: str,
    ) -> asyncio.Future:
        """Create a future the agent tool will await on. Must be called from
        within a running event loop (tools run via ``SafeToolNode.ainvoke``)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        prompt = PendingPrompt(
            prompt_id=prompt_id,
            credential_id=credential_id,
            user_id=user_id,
            thread_id=thread_id,
            provider=provider,
            future=future,
        )
        with self._lock:
            self._prompts[prompt_id] = prompt
        self._start_sweep_locked()
        return future

    def get(self, prompt_id: str) -> Optional[PendingPrompt]:
        with self._lock:
            return self._prompts.get(prompt_id)

    def record_attempt(self, prompt_id: str, *, error: Optional[str]) -> int:
        """Bump the attempt counter (call after a failed test, before the
        modal shows its inline retry)."""
        with self._lock:
            prompt = self._prompts.get(prompt_id)
            if prompt is None:
                return 0
            prompt.attempts += 1
            prompt.last_test_error = error
            return prompt.attempts

    def resolve(self, prompt_id: str, result: dict[str, Any]) -> bool:
        """Wake the tool with ``result``. Returns False if there's nothing to
        wake (already resolved, swept, or never registered)."""
        with self._lock:
            prompt = self._prompts.pop(prompt_id, None)
        if prompt is None:
            return False
        future = prompt.future
        if future.done():
            return False
        # Fill in the running totals so the agent always sees them.
        enriched = {
            "attempts": prompt.attempts,
            "last_test_error": prompt.last_test_error,
            **result,
        }
        future.get_loop().call_soon_threadsafe(_safe_set_result, future, enriched)
        return True

    def discard(self, prompt_id: str) -> None:
        """Remove a prompt without resolving its future. Use only when the
        tool has already returned (e.g. cleanup after a tool-side timeout)."""
        with self._lock:
            self._prompts.pop(prompt_id, None)

    def _start_sweep_locked(self) -> None:
        if self._sweep_started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweep_started = True
        self._sweep_task = loop.create_task(self._sweep_forever())

    async def _sweep_forever(self) -> None:
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
                self._sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("auth_prompt_coordinator sweep failed")

    def _sweep_once(self) -> None:
        cutoff = time.monotonic() - _ORPHAN_TTL_SECONDS
        orphans: list[PendingPrompt] = []
        with self._lock:
            for prompt_id in list(self._prompts.keys()):
                if self._prompts[prompt_id].created_at < cutoff:
                    orphans.append(self._prompts.pop(prompt_id))
        for orphan in orphans:
            future = orphan.future
            if future.done():
                continue
            logger.warning(
                "auth_prompt_coordinator swept orphaned prompt %s "
                "(provider=%s, user=%s, age>%ds)",
                orphan.prompt_id,
                orphan.provider,
                orphan.user_id,
                _ORPHAN_TTL_SECONDS,
            )
            future.get_loop().call_soon_threadsafe(
                _safe_set_result,
                future,
                {
                    "ok": False,
                    "status": "swept",
                    "attempts": orphan.attempts,
                    "last_test_error": orphan.last_test_error,
                },
            )


def _safe_set_result(future: asyncio.Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)


_coordinator: Optional[AuthPromptCoordinator] = None


def get_auth_prompt_coordinator() -> AuthPromptCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = AuthPromptCoordinator()
    return _coordinator


def new_prompt_id() -> str:
    return f"prompt_{secrets.token_urlsafe(16)}"


__all__ = [
    "AuthPromptCoordinator",
    "PendingPrompt",
    "get_auth_prompt_coordinator",
    "new_prompt_id",
]
