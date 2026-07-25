"""In-process rendezvous between the agent-side ``ui_prompt`` tool and the
desktop-side result POSTs.

The flow (backlog #5, the browser-command round trip applied to interactive
HTML prompts):

1. The ``ui_prompt`` tool allocates a ``prompt_id`` and calls
   :meth:`UiPromptCoordinator.register` to get an ``asyncio.Future``.
2. The tool publishes a ``ui_prompt`` autonomous event carrying the
   ``prompt_id``, title, and agent-authored HTML, then awaits the future
   with the caller's timeout.
3. The nymeria-desktop app consumes the event via ``/autonomous/stream``,
   renders the HTML in a sandboxed iframe modal, and POSTs the user's
   submission (or dismissal) to ``POST /ui-prompts/{prompt_id}/result``.
4. The endpoint calls :meth:`resolve`, which wakes the awaiting tool.

Differences from :class:`BrowserCommandCoordinator`:

* A human fills these in, so prompts live minutes, not seconds. The tool
  clamps ``timeout_seconds`` to at most ``MAX_TIMEOUT_SECONDS`` and the
  orphan TTL sits one minute above that cap, so the sweep can never
  resolve a prompt whose tool is still awaiting it.
* :meth:`abort_thread` serves the same cancellation cascade
  (``agent_callable_lifecycle.abort_with_cascade``) so a blocked prompt
  snaps back immediately on ``POST /threads/{id}/stop``.

Single-process only: the future lives in this process. If the API is ever
scaled to multiple workers, the resolver path needs to use Redis pub/sub
to fan out to whichever worker owns the pending prompt.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .future_rendezvous import FutureRendezvous

logger = logging.getLogger(__name__)

# The tool clamps timeout_seconds into [MIN, MAX]; the orphan TTL must stay
# above MAX plus grace so the sweep never races a live prompt.
MAX_TIMEOUT_SECONDS = 600
_ORPHAN_TTL_SECONDS = MAX_TIMEOUT_SECONDS + 60
_SWEEP_INTERVAL_SECONDS = 30


@dataclass
class PendingUiPrompt:
    prompt_id: str
    user_id: str
    thread_id: str
    title: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


class UiPromptCoordinator(FutureRendezvous[PendingUiPrompt]):
    """Tracks in-flight interactive HTML prompts keyed by ``prompt_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=_ORPHAN_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="ui_prompt_coordinator",
        )

    @property
    def _prompts(self) -> dict[str, PendingUiPrompt]:
        """Read alias for the shared registry (kept for readability/tests)."""
        return self._items

    def register(
        self,
        *,
        prompt_id: str,
        user_id: str,
        thread_id: str,
        title: str = "",
    ) -> asyncio.Future:
        """Create the in-process future resolved by the result endpoint."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        prompt = PendingUiPrompt(
            prompt_id=prompt_id,
            user_id=user_id,
            thread_id=thread_id,
            title=title,
            future=future,
        )
        self._add(prompt_id, prompt)
        return future

    def resolve(self, prompt_id: str, result: dict[str, Any]) -> bool:
        """Wake the tool with ``result``. Returns False if there's nothing to
        wake (already resolved, swept, or never registered)."""
        return self._resolve(prompt_id, lambda prompt: result)

    def abort_thread(self, thread_id: str) -> int:
        """Resolve every pending prompt for ``thread_id`` with
        ``status="aborted"``. Returns the number of prompts aborted.

        Called by ``agent_callable_lifecycle.abort_with_cascade`` so
        ``POST /threads/{id}/stop`` snaps a blocked ``ui_prompt`` back
        immediately instead of waiting for its timeout.
        """
        matched = self._drain_matching(lambda prompt: prompt.thread_id == thread_id)
        aborted = 0
        for prompt in matched:
            if self._wake(
                prompt.future,
                {"ok": False, "status": "aborted", "error": "thread aborted"},
            ):
                aborted += 1
        if aborted:
            logger.info(
                "ui_prompt_coordinator aborted %d pending prompt(s) for thread %s",
                aborted,
                thread_id,
            )
        return aborted

    def _swept_result(self, record: PendingUiPrompt) -> dict[str, Any]:
        return {"ok": False, "status": "swept", "error": "prompt orphaned"}

    def _on_orphan_swept(self, record: PendingUiPrompt) -> None:
        logger.warning(
            "ui_prompt_coordinator swept orphaned prompt %s "
            "(user=%s, thread=%s, age>%ds)",
            record.prompt_id,
            record.user_id,
            record.thread_id,
            self._ttl_seconds,
        )


_coordinator: Optional[UiPromptCoordinator] = None


def get_ui_prompt_coordinator() -> UiPromptCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = UiPromptCoordinator()
    return _coordinator


def new_prompt_id() -> str:
    return f"uip_{secrets.token_urlsafe(16)}"


__all__ = [
    "MAX_TIMEOUT_SECONDS",
    "PendingUiPrompt",
    "UiPromptCoordinator",
    "get_ui_prompt_coordinator",
    "new_prompt_id",
]
