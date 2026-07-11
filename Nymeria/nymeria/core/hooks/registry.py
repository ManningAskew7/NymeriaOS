"""In-process hook registry (the engine's logic substrate).

The registry binds logic as plain Python callables (the Pi/opencode ``.on(event,
handler)`` shape). Persisted ``HookDefinition`` records reach it through
``bridge.build_registry``, which compiles a user's enabled definitions into a
fresh per-turn registry; tests and fixtures register callables directly. A
callable returns a typed ``HookOutcome`` (or ``None``) synchronously or
asynchronously.

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
    """A single bound hook: its event, matcher, callable, and plane.

    ``definition_id`` carries the persisted ``HookDefinition`` id when the
    registration came through the bridge (None for direct/fixture
    registrations), so the execution recorder can attribute runs.

    ``timeout`` overrides the dispatcher's per-hook budget for this one hook
    (None = use the dispatch default). The bridge sets it for actions whose
    runtime is author-configured (``run_command``), so a long-running command
    is not cut at the 5s default.

    ``single_use`` marks a definition that deletes itself after its first
    successful run; the engine only carries the flag through to the recorder
    (the product layer's ``make_execution_recorder`` does the deletion), so
    the registry stays store-agnostic.
    """

    id: int
    event: HookEvent
    fn: HookFn
    matcher: Optional[str]
    name: str
    observe: bool
    definition_id: Optional[str] = None
    timeout: Optional[float] = None
    single_use: bool = False


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
    """A lock-guarded ordered collection of hook registrations.

    ``recorder`` is an optional opaque callable the product layer attaches
    (the bridge binds it to the per-user execution log); dispatch reports each
    hook run through it. ``None`` (the default, and always the case for
    ``default_registry``) records nothing, keeping the spine and zero-hook
    paths byte-identical.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._regs: List[Registration] = []
        self._counter = 0
        self.recorder: Optional[Callable[..., None]] = None

    def register(
        self,
        event: HookEvent,
        fn: HookFn,
        *,
        matcher: Optional[str] = None,
        name: Optional[str] = None,
        observe: bool = False,
        definition_id: Optional[str] = None,
        timeout: Optional[float] = None,
        single_use: bool = False,
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
                definition_id=definition_id,
                timeout=timeout,
                single_use=single_use,
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

    def has_observe(self, event: HookEvent) -> bool:
        """True if any observe-plane hook is registered for ``event``.

        Lets the tool-node seam activate for observe-only tool hooks too (so a
        `notify`/`webhook` on `post_tool_use` runs), not just mutate hooks.
        """
        with self._lock:
            return any(reg.event is event and reg.observe for reg in self._regs)

    def clear(self) -> None:
        """Remove all registrations (test isolation)."""
        with self._lock:
            self._regs.clear()
