"""Global admission gate for interactive agent turns (backlog #83).

Interactive chat has per-user request-rate limiting but no process-wide
concurrency bound; a burst of concurrent interactive turns degrades every
user together. This module bounds the number of concurrent interactive
HOLDER turns in the API process, mirroring how autonomous work is bounded by
``max_concurrent_autonomous`` on the ticker executor.

Scope and shape (see docs/private/plans/interactive-admission-control.md for
the full decision record):

- One in-process gate. Both runtime shapes make the API process the single
  agent runtime (the Docker worker relays turns to the API), so an in-process
  counter covers all interactive turns by construction, exactly like
  ``ThreadLockManager`` and ``PendingPromptQueue`` stay in-process.
- The unit is a TURN (a would-be thread-lock holder), not an LLM round trip.
- Bounded-wait-then-shed: at the ceiling a request may wait
  ``interactive_admission_wait_seconds`` for a slot, then is shed (the HTTP
  routes translate the shed into 429 + Retry-After).
- Exemptions live in :func:`admit_interactive_turn`: ``is_self_invoke``
  relays (already bounded by the autonomous ceiling) and prompts aimed at a
  busy thread (they queue onto the running holder turn instead of starting
  new concurrency).
- Disconnected turns keep their slot until the turn truly ends: the platform
  deliberately lets abandoned holder turns finish (they are re-attachable and
  do real work), so they keep drawing against the ceiling.

Concurrency model: single event loop. ``acquire`` runs on the API loop and
``TurnSlot.release`` is called from loop context on every gated route, so the
counter and waiter deque need no lock. Waiters are FIFO; a releasing turn
hands its count directly to the first live waiter (no barging). The limit is
read per acquire (settings hot-reload via ``PATCH /settings``), so lowering
the limit at runtime simply sheds new admissions until in-flight turns drain.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Advisory client back-off hint on a shed (seconds). The server cannot know
# when a running turn will end; 10s is turn-scale without inviting hammering.
ADMISSION_RETRY_AFTER_SECONDS = 10

# The exact user-facing detail string for a shed, documented in
# docs/api.md. A plain string (not a dict) so every existing client
# render path (bots' http_error_detail, the CLI's _response_detail, the GUIs'
# non-OK chat branch) shows clean text; it also distinguishes the capacity
# shed from the per-user rate limiter's "Too many requests" 429.
CAPACITY_DETAIL = (
    "The server is at its interactive turn limit; your message was not "
    "started. Try again in a moment."
)


class InteractiveCapacityError(Exception):
    """An interactive turn was shed at the global concurrency ceiling."""

    def __init__(self, active: int, limit: int) -> None:
        super().__init__(CAPACITY_DETAIL)
        self.active = active
        self.limit = limit
        self.detail = CAPACITY_DETAIL
        self.retry_after = ADMISSION_RETRY_AFTER_SECONDS


class TurnSlot:
    """One admitted turn's hold on the gate. ``release()`` is idempotent."""

    __slots__ = ("_gate", "_released")

    def __init__(self, gate: "InteractiveTurnGate") -> None:
        self._gate = gate
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._release()


class InteractiveTurnGate:
    """Process-wide counter of in-flight interactive holder turns."""

    def __init__(self) -> None:
        self._active = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def active(self) -> int:
        """Interactive turns currently counted against the ceiling."""
        return self._active

    def try_acquire(self, limit: int) -> Optional[TurnSlot]:
        """Take a slot without waiting. ``limit <= 0`` means unlimited.

        The count is maintained even when unlimited, so enabling a ceiling at
        runtime starts from an accurate in-flight number.
        """
        if limit > 0 and self._active >= limit:
            return None
        self._active += 1
        return TurnSlot(self)

    async def acquire(self, limit: int, wait_seconds: float) -> TurnSlot:
        """Take a slot, waiting up to ``wait_seconds`` at the ceiling.

        Raises :class:`InteractiveCapacityError` when the gate stays
        saturated past the bounded wait. Cancellation of the waiting request
        is propagated without leaking a slot (a handoff that lands in the
        same loop step as the cancel is given back).
        """
        slot = self.try_acquire(limit)
        if slot is not None:
            return slot
        if wait_seconds > 0:
            waiter: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, wait_seconds)
                # A releasing turn transferred its count to this waiter.
                return TurnSlot(self)
            except asyncio.TimeoutError:
                # A handoff can land in the same loop step the timeout fires;
                # honor it instead of leaking the transferred count.
                if waiter.done() and not waiter.cancelled():
                    return TurnSlot(self)
            except asyncio.CancelledError:
                if waiter.done() and not waiter.cancelled():
                    # Give the transferred count back (and pass it on).
                    TurnSlot(self).release()
                raise
            finally:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    # Already removed by a release()'s handoff pop.
                    pass
        raise InteractiveCapacityError(self._active, limit)

    def _release(self) -> None:
        if self._active <= 0:  # defensive; release is slot-guarded
            logger.warning("InteractiveTurnGate released below zero")
            self._active = 0
            return
        self._active -= 1
        # Hand the freed count to the first live waiter (FIFO, no barging).
        # Deliberately limit-blind: the limit at release time is unknown here,
        # and a briefly-lowered limit admitting one queued waiter is
        # acceptable capacity policy.
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self._active += 1
                waiter.set_result(True)
                break


_GATE = InteractiveTurnGate()


def get_interactive_turn_gate() -> InteractiveTurnGate:
    """Return the process-wide interactive turn gate."""
    return _GATE


def reset_interactive_turn_gate_for_tests() -> None:
    """Swap in a fresh gate. Outstanding TurnSlots keep their old gate."""
    global _GATE
    _GATE = InteractiveTurnGate()


def interactive_admission_config(settings: Any) -> tuple[int, float]:
    """Read (limit, wait_seconds) defensively from a settings-like object.

    Fakes and partial settings objects degrade to (0, 0.0): unlimited, shed
    immediately if a limit is somehow enforced.
    """
    try:
        limit = int(getattr(settings, "max_concurrent_interactive", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        wait = float(
            getattr(settings, "interactive_admission_wait_seconds", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        wait = 0.0
    return limit, wait


async def admit_interactive_turn(
    agent: Any,
    settings: Any,
    thread_id: str,
    *,
    is_self_invoke: bool = False,
) -> Optional[TurnSlot]:
    """Admission-check one would-be interactive holder turn.

    Returns a :class:`TurnSlot` the caller must release when the turn ends,
    or ``None`` when the request is exempt:

    - ``is_self_invoke`` relay turns (the Docker worker's autonomous relays)
      are bounded by ``max_concurrent_autonomous`` on the dispatch side;
      counting them here would double-charge one budget against another.
    - Prompts aimed at a BUSY thread queue onto the running holder turn
      (``prompt_queued``) and never start new concurrency.

    Raises :class:`InteractiveCapacityError` when the ceiling stays saturated
    past the bounded wait.

    The busy probe races the thread-lock acquisition inside the turn; that is
    accepted capacity policy, not correctness. An admitted request that loses
    the lock race is queued instead of holding: the streaming routes release
    its slot early on the ``prompt_queued`` signal.
    """
    if is_self_invoke:
        return None
    locks = getattr(agent, "_thread_locks", None)
    if locks is not None:
        try:
            if locks.is_thread_busy(thread_id):
                return None
        except Exception:  # noqa: BLE001 - a broken probe must not block chat
            logger.debug("Interactive admission busy probe failed", exc_info=True)
    limit, wait_seconds = interactive_admission_config(settings)
    try:
        return await _GATE.acquire(limit, wait_seconds)
    except InteractiveCapacityError as exc:
        logger.warning(
            "Interactive turn shed at capacity (thread=%s active=%d limit=%d)",
            thread_id,
            exc.active,
            exc.limit,
        )
        raise
