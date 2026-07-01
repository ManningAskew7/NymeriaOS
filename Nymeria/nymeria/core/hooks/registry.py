"""In-process hook registry (the spine's logic substrate).

The spine binds logic as plain Python callables (the Pi/opencode ``.on(event,
handler)`` shape), not persisted records: a later pass adds the ``HookDefinition``
record, storage, and authoring surface on top of the same contract. A callable
returns a typed ``HookOutcome`` (or ``None``) synchronously or asynchronously.

Thread-safety is real, not cosmetic: hooks run both on the event loop (the
concurrent async tool path) and in a ``ThreadPoolExecutor`` worker (the sequential
sync tool path), so registration and matching are guarded by a ``threading.Lock``.
Matching snapshots the registration list under the lock and iterates outside it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Union

from .base import HookContext, HookEvent, HookOutcome

# A hook fn may be sync or async; the dispatcher handles both.
HookFn = Callable[[HookContext], Union[Optional[HookOutcome], Awaitable[Optional[HookOutcome]]]]


@dataclass
class Registration:
    """A single bound hook: its event, matcher, callable, and plane."""

    id: int
    event: HookEvent
    fn: HookFn
    matcher: Optional[str]
    name: str
    observe: bool


def _matches(matcher: Optional[str], tool_name: Optional[str]) -> bool:
    """Exact-string or pipe-list matcher against a tool name.

    ``None`` matches everything (non-tool events). A set matcher only matches
    tool events whose name is in the pipe-list (``"Edit|Write|MultiEdit"``).
    """
    if matcher is None:
        return True
    if tool_name is None:
        return False
    return tool_name in matcher.split("|")


class HookRegistry:
    """A lock-guarded ordered collection of hook registrations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._regs: List[Registration] = []
        self._counter = 0

    def register(
        self,
        event: HookEvent,
        fn: HookFn,
        *,
        matcher: Optional[str] = None,
        name: Optional[str] = None,
        observe: bool = False,
    ) -> int:
        """Register a hook; returns an opaque handle for :meth:`unregister`."""
        with self._lock:
            self._counter += 1
            reg = Registration(
                id=self._counter,
                event=event,
                fn=fn,
                matcher=matcher,
                name=name or getattr(fn, "__name__", "hook"),
                observe=observe,
            )
            self._regs.append(reg)
            return reg.id

    def unregister(self, handle: int) -> bool:
        """Remove a registration by handle. Returns True if one was removed."""
        with self._lock:
            for i, reg in enumerate(self._regs):
                if reg.id == handle:
                    del self._regs[i]
                    return True
        return False

    def matching(
        self,
        event: HookEvent,
        ctx: HookContext,
        *,
        observe: Optional[bool] = None,
    ) -> List[Registration]:
        """Registrations for ``event`` whose matcher accepts ``ctx``.

        Returned in registration order. ``observe`` filters to one plane when
        given (True = observe-only, False = mutate-only).
        """
        with self._lock:
            regs = list(self._regs)
        out: List[Registration] = []
        for reg in regs:
            if reg.event is not event:
                continue
            if observe is not None and reg.observe is not observe:
                continue
            if not _matches(reg.matcher, ctx.tool_name):
                continue
            out.append(reg)
        return out

    def has_mutating(self, event: HookEvent) -> bool:
        """True if any mutate-plane hook is registered for ``event``.

        Used to gate installation of the tool-node interceptor so the hot path
        keeps its fast route when nothing is registered.
        """
        with self._lock:
            return any(reg.event is event and not reg.observe for reg in self._regs)

    def clear(self) -> None:
        """Remove all registrations (test isolation)."""
        with self._lock:
            self._regs.clear()
