"""Hook dispatch: run matching hooks, isolate faults, reduce to one outcome.

Two planes:

- Mutate plane (:func:`adispatch` / :func:`dispatch`): runs the mutate-plane hooks
  for an event, applies their scratch patches, and reduces their outcomes to a
  single typed ``HookOutcome`` the fire point applies in-band.
- Observe plane (:func:`adispatch_observe` / :func:`dispatch_observe`):
  fire-and-forget side effects; never blocks or affects the turn.

Each plane has an async form (for the ``astream`` / ``awrap_tool_call`` seams) and
a sync form (a bridge for the sync ``chat`` path and the sequential threadpool tool
path, which have no running event loop). All hooks for an event run
("run-all-then-reduce") so side effects and scratch writes are order-independent
rather than contingent on an earlier hook short-circuiting.

Fault policy: the veto path fails closed, everything else fails open. A raising
PRE_TOOL_USE hook becomes a ``deny``; a raising POST/PROMPT/DONE hook is logged and
skipped. A hook can never crash a turn.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Awaitable, List, Optional, cast

from .base import (
    EVENT_OUTCOME_TYPES,
    DoneOutcome,
    HookContext,
    HookEvent,
    HookOutcome,
    PostToolOutcome,
    PreToolOutcome,
    PromptOutcome,
)
from .registry import HookRegistry, Registration
from .scratch import ScratchStore

logger = logging.getLogger(__name__)

# Per-hook wall-clock budget. Generous for the spine (fixtures are fast); a real
# blocking action must not stall a turn indefinitely.
DEFAULT_HOOK_TIMEOUT = 5.0

# Shared pool for running sync hooks off the event loop (async path) and for
# bounding sync-path hooks with a timeout.
_sync_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hook-sync")

# Module-level defaults shared by the fire points and tests.
default_registry = HookRegistry()
default_scratch = ScratchStore()


# --------------------------------------------------------------------------- #
# Convenience surface (bound to the module defaults)
# --------------------------------------------------------------------------- #

def register(event, fn, *, matcher=None, name=None, observe=False) -> int:
    """Register a hook on the default registry."""
    return default_registry.register(event, fn, matcher=matcher, name=name, observe=observe)


def unregister(handle: int) -> bool:
    """Unregister a hook from the default registry."""
    return default_registry.unregister(handle)


def reset() -> None:
    """Clear the default registry and scratch store (test isolation)."""
    default_registry.clear()
    default_scratch.clear()


def tool_hooks_active(registry: Optional[HookRegistry] = None) -> bool:
    """True if any PRE/POST tool hook (mutate OR observe) is registered.

    Lets the tool-node seam keep the parent's fast path (and unchanged error
    semantics) on every tool call when nothing is registered. Observe-plane tool
    hooks (e.g. a `notify`/`webhook` on `post_tool_use`) must also activate the
    seam, or they would never fire. Accepts an optional per-turn registry
    (defaults to the module ``default_registry``) so the fast-path decision is
    accurate for the thread being run.
    """
    from .base import HookEvent
    registry = registry or default_registry
    return (
        registry.has_mutating(HookEvent.PRE_TOOL_USE)
        or registry.has_mutating(HookEvent.POST_TOOL_USE)
        or registry.has_observe(HookEvent.PRE_TOOL_USE)
        or registry.has_observe(HookEvent.POST_TOOL_USE)
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _with_scratch(ctx: HookContext, scratch: ScratchStore) -> HookContext:
    """Return ``ctx`` with a fresh read-only scratch snapshot injected."""
    return replace(ctx, scratch=scratch.snapshot(ctx.thread_id))


def _legal(event: HookEvent, outcome: HookOutcome) -> bool:
    """True if ``outcome`` is the allowed type for ``event``."""
    return isinstance(outcome, EVENT_OUTCOME_TYPES[event])


def _fault_outcome(event: HookEvent, reg: Registration, exc: BaseException) -> Optional[HookOutcome]:
    """Fault policy: PRE fails closed (deny), others fail open (skip)."""
    logger.warning("hook %r raised on %s: %s", reg.name, event.value, exc, exc_info=True)
    if event is HookEvent.PRE_TOOL_USE:
        return PreToolOutcome(decision="deny", reason=f"hook '{reg.name}' error")
    return None


def _accept(
    event: HookEvent,
    reg: Registration,
    outcome: Optional[HookOutcome],
    ctx: HookContext,
    scratch: ScratchStore,
    outcomes: List[HookOutcome],
) -> None:
    """Validate an outcome, apply its scratch patch, and collect it."""
    if outcome is None:
        return
    if not _legal(event, outcome):
        logger.error(
            "hook %r returned illegal outcome %s for %s; dropping",
            reg.name, type(outcome).__name__, event.value,
        )
        return
    patch = getattr(outcome, "scratch_patch", None)
    if patch:
        if isinstance(patch, Mapping):
            scratch.apply_patch(ctx.thread_id, patch)
        else:
            logger.error(
                "hook %r scratch_patch is not a mapping (%s); ignoring",
                reg.name, type(patch).__name__,
            )
    outcomes.append(outcome)


async def _arun_hook(reg: Registration, ctx: HookContext, timeout: float) -> Optional[HookOutcome]:
    """Run one hook on the async path, bounding it by ``timeout``.

    Async hooks are awaited; sync hooks are offloaded to a thread so a blocking
    hook cannot stall the event loop (a real risk inside ``awrap_tool_call``).
    """
    if inspect.iscoroutinefunction(reg.fn):
        coro = cast(Awaitable[Optional[HookOutcome]], reg.fn(ctx))
        return await asyncio.wait_for(coro, timeout=timeout)
    # Use our own pool (not asyncio.to_thread's default executor) so a timed-out
    # sync hook's still-running thread cannot block event-loop shutdown.
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(loop.run_in_executor(_sync_pool, reg.fn, ctx), timeout=timeout)
    return cast(Optional[HookOutcome], result)


def _run_hook_sync(reg: Registration, ctx: HookContext, timeout: float) -> Optional[HookOutcome]:
    """Run one hook on the sync path (no running loop), bounding it by ``timeout``."""
    if inspect.iscoroutinefunction(reg.fn):
        coro = cast(Awaitable[Optional[HookOutcome]], reg.fn(ctx))
        return asyncio.run(asyncio.wait_for(coro, timeout=timeout))
    future = _sync_pool.submit(reg.fn, ctx)
    return cast(Optional[HookOutcome], future.result(timeout=timeout))


def _reduce(event: HookEvent, outcomes: List[HookOutcome]) -> Optional[HookOutcome]:
    """Collapse per-event outcomes into one, deterministically (registration order)."""
    if not outcomes:
        return None

    if event is HookEvent.PROMPT_SUBMIT:
        texts = [o.inject_context for o in outcomes if isinstance(o, PromptOutcome) and o.inject_context]
        return PromptOutcome(inject_context="\n".join(texts)) if texts else None

    if event is HookEvent.PRE_TOOL_USE:
        pre = [o for o in outcomes if isinstance(o, PreToolOutcome)]
        denies = [o for o in pre if o.decision == "deny"]
        if denies:
            reasons = [o.reason for o in denies if o.reason]
            return PreToolOutcome(decision="deny", reason="\n".join(reasons) if reasons else None)
        merged: dict = {}
        any_modify = False
        for o in pre:
            # A modify with a malformed (non-mapping) updated_args is ignored
            # rather than crashing the reduction; modify is not the veto path.
            if o.decision == "modify" and isinstance(o.updated_args, Mapping) and o.updated_args:
                merged.update(o.updated_args)
                any_modify = True
        if any_modify:
            return PreToolOutcome(decision="modify", updated_args=merged)
        return PreToolOutcome(decision="allow")

    if event is HookEvent.POST_TOOL_USE:
        post = [o for o in outcomes if isinstance(o, PostToolOutcome)]
        updated: Optional[str] = None
        notes: List[str] = []
        for o in post:
            if o.updated_result_text is not None:
                updated = o.updated_result_text  # last non-None wins
            if o.additional_context:
                notes.append(o.additional_context)
        if updated is None and not notes:
            return None
        return PostToolOutcome(
            updated_result_text=updated,
            additional_context="\n".join(notes) if notes else None,
        )

    if event is HookEvent.DONE:
        done = [o for o in outcomes if isinstance(o, DoneOutcome)]
        cont = any(o.continue_ for o in done)
        reasons = [o.reason for o in done if o.continue_ and o.reason]
        msgs = [o.user_message for o in done if o.user_message]
        if not cont and not msgs:
            return None
        return DoneOutcome(
            continue_=cont,
            reason="\n".join(reasons) if reasons else None,
            user_message="\n".join(msgs) if msgs else None,
        )

    return None


def _reduce_safe(event: HookEvent, outcomes: List[HookOutcome]) -> Optional[HookOutcome]:
    """Reduce, guaranteeing no raise: a reduction bug must not crash a turn.

    ``_reduce`` is defensive about known bad-outcome shapes, but this wrapper is
    the hard guarantee that dispatch never propagates an exception into a turn.
    The veto path fails closed on a reduction error; everything else fails open.
    """
    try:
        return _reduce(event, outcomes)
    except Exception:  # noqa: BLE001 - dispatch never raises into a turn
        logger.exception("hook outcome reduction failed on %s", event.value)
        if event is HookEvent.PRE_TOOL_USE:
            return PreToolOutcome(decision="deny", reason="hook reduction error")
        return None


# --------------------------------------------------------------------------- #
# Mutate plane
# --------------------------------------------------------------------------- #

async def adispatch(
    event: HookEvent,
    ctx: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    scratch: Optional[ScratchStore] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
) -> Optional[HookOutcome]:
    """Async mutate-plane dispatch: run all matching hooks, reduce to one outcome."""
    registry = registry or default_registry
    scratch = scratch or default_scratch
    regs = registry.matching(event, ctx, observe=False)
    if not regs:
        return None
    ctx = _with_scratch(ctx, scratch)
    outcomes: List[HookOutcome] = []
    for reg in regs:
        # _accept runs inside the try so a malformed scratch_patch (or any other
        # post-run failure) is isolated per hook rather than crashing the turn.
        try:
            outcome = await _arun_hook(reg, ctx, timeout)
            _accept(event, reg, outcome, ctx, scratch, outcomes)
        except Exception as exc:  # noqa: BLE001 - a hook can never crash a turn
            fault = _fault_outcome(event, reg, exc)
            if fault is not None:
                outcomes.append(fault)
    return _reduce_safe(event, outcomes)


def dispatch(
    event: HookEvent,
    ctx: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    scratch: Optional[ScratchStore] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
) -> Optional[HookOutcome]:
    """Sync mutate-plane dispatch (bridge for no-loop callers)."""
    registry = registry or default_registry
    scratch = scratch or default_scratch
    regs = registry.matching(event, ctx, observe=False)
    if not regs:
        return None
    ctx = _with_scratch(ctx, scratch)
    outcomes: List[HookOutcome] = []
    for reg in regs:
        # _accept runs inside the try so a malformed scratch_patch (or any other
        # post-run failure) is isolated per hook rather than crashing the turn.
        try:
            outcome = _run_hook_sync(reg, ctx, timeout)
            _accept(event, reg, outcome, ctx, scratch, outcomes)
        except Exception as exc:  # noqa: BLE001 - a hook can never crash a turn
            fault = _fault_outcome(event, reg, exc)
            if fault is not None:
                outcomes.append(fault)
    return _reduce_safe(event, outcomes)


# --------------------------------------------------------------------------- #
# Observe plane (fire-and-forget)
# --------------------------------------------------------------------------- #

async def adispatch_observe(
    event: HookEvent,
    ctx: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    scratch: Optional[ScratchStore] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
) -> None:
    """Async observe-plane dispatch: run side-effect hooks, swallow all faults."""
    registry = registry or default_registry
    scratch = scratch or default_scratch
    regs = registry.matching(event, ctx, observe=True)
    if not regs:
        return
    ctx = _with_scratch(ctx, scratch)
    for reg in regs:
        try:
            await _arun_hook(reg, ctx, timeout)
        except Exception:  # noqa: BLE001 - observe never affects the turn
            logger.warning("observe hook %r raised on %s", reg.name, event.value, exc_info=True)


def dispatch_observe(
    event: HookEvent,
    ctx: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    scratch: Optional[ScratchStore] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
) -> None:
    """Sync observe-plane dispatch (bridge for no-loop callers)."""
    registry = registry or default_registry
    scratch = scratch or default_scratch
    regs = registry.matching(event, ctx, observe=True)
    if not regs:
        return
    ctx = _with_scratch(ctx, scratch)
    for reg in regs:
        try:
            _run_hook_sync(reg, ctx, timeout)
        except Exception:  # noqa: BLE001 - observe never affects the turn
            logger.warning("observe hook %r raised on %s", reg.name, event.value, exc_info=True)
