"""Hook dispatch: run matching hooks, isolate faults, reduce to one outcome.

Two planes:

- Mutate plane (:func:`adispatch` / :func:`dispatch`): runs the mutate-plane hooks
  for an event, applies their scratch patches, and reduces their outcomes to a
  single typed ``HookOutcome`` the fire point applies in-band.
- Observe plane (:func:`adispatch_observe` / :func:`dispatch_observe`):
  side-effect hooks; faults are swallowed and no outcome is returned. The
  dispatch calls themselves still run the hooks in-band; fire points that must
  not wait use :func:`schedule_observe`, which schedules the dispatch
  off-turn (a loop task or a dedicated pool) and returns immediately.

Each plane has an async form (for the ``astream`` / ``awrap_tool_call`` seams) and
a sync form (a bridge for the sync ``chat`` path and the sequential threadpool tool
path, which have no running event loop). All hooks for an event run
("run-all-then-reduce") so side effects and scratch writes are order-independent
rather than contingent on an earlier hook short-circuiting.

Fault policy: the veto path fails closed, everything else fails open. A raising
hook on a veto event (``VETO_EVENTS``: PRE_TOOL_USE and COMMAND_SUBMIT) becomes
a ``deny``; a raising POST/PROMPT/DONE hook is logged and skipped. A hook can
never crash a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import wait as futures_wait
from dataclasses import replace
from typing import Awaitable, Callable, Dict, List, Optional, Set, cast

from .base import (
    EVENT_OUTCOME_TYPES,
    VETO_EVENTS,
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

# How many hook fires may be nested inside one another before dispatch refuses.
# Leaves room for the legitimate shape (a hook runs a workflow, that workflow
# calls a tool, that tool call fires its own hooks) while stopping the cycle
# from running away. Raising this widens a denial-of-service surface, not a
# feature.
#
# 3, not 2, because this counts DISPATCHES and not mutate hops. It was 2 when
# only the two mutate dispatchers incremented it; extending the guard to the
# observe plane (which it had to cover, since an observe hook's action can run
# a workflow just as well) meant an observe fire spends a level too, so a
# mixed-plane chain exhausted the budget in half as many mutate hops as before
# and a previously-working three-level chain started being denied. One level
# back restores the mutate budget the number was chosen for.
MAX_HOOK_FIRE_DEPTH = 3

# ContextVar, not a plain global: hook fires are concurrent across threads and
# turns, so a shared counter would let one turn's depth deny another's hooks.
# A ContextVar is naturally per-task and is inherited by tasks spawned inside a
# fire, which is exactly the chain being bounded.
_fire_depth: ContextVar[int] = ContextVar("nymeria_hook_fire_depth", default=0)


def _pool_workers(env_name: str, default: int = 4) -> int:
    """Pool size from ``env_name``, clamped to a sane floor (bad value -> default)."""
    try:
        return max(1, int(os.environ.get(env_name, "") or default))
    except (TypeError, ValueError):
        return default


# Pools for running sync hooks off the event loop (async path) and for bounding
# sync-path hooks with a timeout. SCOPED PER PLANE so the two hook classes cannot
# starve each other: a slow side-effect ``observe`` hook (e.g. a ``webhook`` that
# hangs for its whole 4.5s budget) must not occupy the worker a fast mutate-plane
# guardrail (``block_if_matches`` on the PRE seam) needs -- a starved PRE hook
# times out and, being fail-closed, would deny every tool call. Separate pools
# keep guardrails responsive under observe-side load.
_mutate_pool = ThreadPoolExecutor(
    max_workers=_pool_workers("HOOK_MUTATE_POOL_WORKERS"),
    thread_name_prefix="hook-mutate",
)
_observe_pool = ThreadPoolExecutor(
    max_workers=_pool_workers("HOOK_OBSERVE_POOL_WORKERS"),
    thread_name_prefix="hook-observe",
)

# Runs whole ``dispatch_observe`` calls off-turn for sync fire points (see
# ``schedule_observe``). DEDICATED pool, never ``_observe_pool``: a dispatcher
# blocks waiting on ``_observe_pool`` workers for each hook, so dispatchers
# sharing that pool could occupy every worker and starve the very hooks they
# wait for (spurious ``saturated`` faults).
_observe_dispatch_pool = ThreadPoolExecutor(
    max_workers=_pool_workers("HOOK_OBSERVE_DISPATCH_WORKERS", 2),
    thread_name_prefix="hook-observe-dispatch",
)

# Strong references to in-flight fire-and-forget observe work. Event loops
# hold tasks only weakly (an unreferenced task can be GC'd mid-flight), and the
# future set gives the sync path a real drain barrier (a sentinel submit cannot
# tell "queue empty" from "another worker still busy").
_observe_tasks: Set[asyncio.Task] = set()
_observe_futures: Set[Future] = set()


class _HookTimeout(Exception):
    """A hook exceeded its budget.

    ``started`` distinguishes the two very different failure modes the plain
    ``future.result(timeout)`` wait conflates:

    - ``started=True``  -- the hook body ran but overran its execution budget
      (a genuinely slow hook).
    - ``started=False`` -- the hook never got a worker within the budget (the
      pool is saturated by other hung hooks). The hook made no decision at all;
      treating this as a guardrail *veto* would let one hung hook deny unrelated
      tool calls, so it is logged distinctly.
    """

    def __init__(self, reg_name: Optional[str], *, started: bool):
        phase = "executing" if started else "queued (pool saturated)"
        super().__init__(f"hook {reg_name!r} timed out {phase}")
        self.started = started

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


@contextlib.contextmanager
def _entered_fire_depth() -> Iterator[Optional[int]]:
    """Enter one hook-fire level, or report that the budget is spent.

    Yields ``None`` when the level was entered (and releases it on the way
    out), or the depth that would have been reached when it was not, which the
    caller turns into its plane's refusal.

    All four dispatchers use this, which is the point: the guard was written
    twice by hand for the mutate plane and the observe plane was simply left
    without one, so a hook whose ACTION runs a workflow could cycle unbounded
    as long as it was registered to observe. Nothing about "observe hooks
    cannot change the turn" bounds what their side effects do.
    """
    depth = _fire_depth.get()
    if depth >= MAX_HOOK_FIRE_DEPTH:
        yield depth + 1
        return
    token = _fire_depth.set(depth + 1)
    try:
        yield None
    finally:
        _fire_depth.reset(token)


def _log_reentrance(event: HookEvent, depth: int, *, level: int = logging.ERROR) -> None:
    """Log a refused dispatch at the severity that plane's faults already use.

    ERROR on the mutate planes, where a refusal changes the turn (PRE denies).
    WARNING on observe, matching its own fault handling: nothing about the turn
    changes, and at depth this fires once per hook per tool call, so ERROR
    would bury the refusals that do mean something.
    """
    logger.log(
        level,
        "hook dispatch refused on %s: fire depth %d exceeds max %d "
        "(a hook is re-entering itself, most likely hook -> workflow -> tool)",
        event.value, depth, MAX_HOOK_FIRE_DEPTH,
    )


def _reentrance_outcome(event: HookEvent, depth: int) -> Optional[HookOutcome]:
    """Depth policy, deliberately identical to the fault policy above.

    A hook can run a workflow, a workflow can call tools, and a tool call is a
    hook fire point. That is a cycle, and nothing downstream bounds it: the
    workflow engine's own ``max_depth`` is carried in a runnable configurable
    that a hook-dispatched run does not inherit, so every hop through a hook
    restarts the count at zero.

    Refusing to fire is NOT the same as allowing the call. If a
    ``require_approval`` or ``block_if_matches`` hook silently stopped applying
    once a chain got deep enough, then driving the chain deep would be a way to
    walk straight through the gate. So PRE denies here, for the same reason it
    denies on a saturated pool: a guardrail that was not evaluated must not
    pass. The post plane gates nothing, so it simply stops, and the observe
    plane discards outcomes entirely, so it stops too.
    """
    _log_reentrance(event, depth)
    if event in VETO_EVENTS:
        return PreToolOutcome(
            decision="deny",
            reason=(
                "hook recursion limit reached "
                f"(depth {depth} > {MAX_HOOK_FIRE_DEPTH})"
            ),
        )
    return None


def _fault_outcome(event: HookEvent, reg: Registration, exc: BaseException) -> Optional[HookOutcome]:
    """Fault policy: the veto events fail closed (deny), others fail open (skip).

    A pool-saturation timeout (the hook never ran) is logged at error level and
    carries a distinct deny reason, so operators can tell a starved dispatch pool
    apart from a hook that actually raised or ran too long. It still fails closed
    on the veto events: a guardrail that could not be evaluated must not
    silently pass.
    """
    if isinstance(exc, _HookTimeout) and not exc.started:
        logger.error(
            "hook %r could not run on %s: dispatch pool saturated", reg.name, event.value
        )
        if event in VETO_EVENTS:
            return PreToolOutcome(
                decision="deny",
                reason=f"hook '{reg.name}' could not run (dispatch pool saturated)",
            )
        return None
    logger.warning("hook %r raised on %s: %s", reg.name, event.value, exc, exc_info=True)
    if event in VETO_EVENTS:
        return PreToolOutcome(decision="deny", reason=f"hook '{reg.name}' error")
    return None


def _accept(
    event: HookEvent,
    reg: Registration,
    outcome: Optional[HookOutcome],
    ctx: HookContext,
    scratch: ScratchStore,
    outcomes: List[HookOutcome],
) -> str:
    """Validate an outcome, apply its scratch patch, and collect it.

    Returns the run status for the execution recorder: ``"ok"`` (outcome
    collected), ``"no_op"`` (hook ran and returned None), or ``"illegal"``
    (wrong outcome type for the event; dropped).
    """
    if outcome is None:
        return "no_op"
    if not _legal(event, outcome):
        logger.error(
            "hook %r returned illegal outcome %s for %s; dropping",
            reg.name, type(outcome).__name__, event.value,
        )
        return "illegal"
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
    return "ok"


def _apply_observe_patch(
    event: HookEvent,
    reg: Registration,
    outcome: Optional[HookOutcome],
    ctx: HookContext,
    scratch: ScratchStore,
) -> None:
    """Apply a legal observe outcome's scratch patch; the outcome itself stays ignored.

    The observe plane is fire-and-forget for TURN effects, but scratch is engine
    state: the definition-level fire gate's ``once`` sentinel (and any observe
    hook's own patch) must persist or a once-gated observe hook would re-fire on
    every event. Mirrors the mutate plane's ``_accept`` patch handling; never
    raises (the observe runners' fault handling wraps the call).
    """
    if outcome is None or not _legal(event, outcome):
        return
    patch = getattr(outcome, "scratch_patch", None)
    if not patch:
        return
    if isinstance(patch, Mapping):
        scratch.apply_patch(ctx.thread_id, patch)
    else:
        logger.error(
            "observe hook %r scratch_patch is not a mapping (%s); ignoring",
            reg.name, type(patch).__name__,
        )


def _fault_status(exc: BaseException) -> str:
    """Execution-log status for a hook fault."""
    if isinstance(exc, _HookTimeout):
        return "timeout" if exc.started else "saturated"
    return "error"


def _outcome_detail(outcome: Optional[HookOutcome]) -> str:
    """Short human summary of an outcome for the execution log."""
    if outcome is None:
        return ""
    if isinstance(outcome, PreToolOutcome):
        if outcome.decision == "deny":
            return f"deny: {outcome.reason}" if outcome.reason else "deny"
        if outcome.decision == "modify":
            keys = (
                sorted(str(k) for k in outcome.updated_args)
                if isinstance(outcome.updated_args, Mapping)
                else []
            )
            return f"modify: {', '.join(keys)}" if keys else "modify"
        # An allow with a note is a story worth logging (an approval grant, a
        # failed-open guardrail script); a bare allow stays inert (see
        # _ACTIVITY_INERT_DETAILS).
        note = getattr(outcome, "note", None)
        return f"allow: {note}" if note else "allow"
    if isinstance(outcome, PromptOutcome):
        # An outcome with no text is a state-only carrier (e.g. a fire gate's
        # once/re-arm scratch patch); report it inert so no activity line fires.
        if not outcome.inject_context:
            return "no change"
        return f"inject {len(outcome.inject_context)} chars"
    if isinstance(outcome, PostToolOutcome):
        parts = []
        if outcome.updated_result_text is not None:
            parts.append(f"rewrite result ({len(outcome.updated_result_text)} chars)")
        if outcome.additional_context:
            parts.append(f"note {len(outcome.additional_context)} chars")
        return "; ".join(parts) or "no change"
    if isinstance(outcome, DoneOutcome):
        parts = []
        if outcome.continue_:
            parts.append("continue")
        if outcome.user_message:
            parts.append(f"user_message {len(outcome.user_message)} chars")
        return "; ".join(parts) or "no change"
    return type(outcome).__name__


# Statuses that always warrant a live activity line (the hook faulted); an "ok"
# run is meaningful only when it actually changed something (see _meaningful).
_ACTIVITY_FAULT_STATUSES = ("error", "timeout", "saturated", "illegal")
# "ok" details that mean the hook ran but changed nothing model-visible; no line.
_ACTIVITY_INERT_DETAILS = ("", "allow", "no change")


def _meaningful_activity(status: str, detail: str) -> bool:
    """True if a hook run is worth surfacing as a live in-chat activity line.

    Faults always are. A clean run is only meaningful when it did something
    (a deny/modify, an inject, a rewrite/note, a continue): a bare ``allow`` or
    ``no change`` would put a noisy line on every guarded tool call. ``no_op``
    (ran, returned None) is never surfaced; it lives in ``/hook history``.
    """
    if status in _ACTIVITY_FAULT_STATUSES:
        return True
    if status == "ok":
        return detail not in _ACTIVITY_INERT_DETAILS
    return False


def _record(
    registry: HookRegistry,
    reg: Registration,
    ctx: HookContext,
    *,
    status: str,
    duration: float,
    outcome: Optional[HookOutcome] = None,
    error: Optional[BaseException] = None,
    emit: Optional[Callable[[Dict[str, object]], None]] = None,
) -> None:
    """Report one hook run to the registry's recorder + optional activity emitter.

    The recorder is an opaque callable the product layer attaches to the
    per-turn registry (bound to the per-user execution log); the engine stays
    store-agnostic. ``default_registry`` carries no recorder, so the spine and
    zero-hook paths record nothing. The detail summary is computed in here,
    under this function's own guard, and the dispatch loops call ``_record``
    OUTSIDE their fault handling: a recording or summarization bug is confined
    to the log entry and can never alter the turn (on the PRE seam, a raise
    inside the dispatch try would become a fail-closed deny).

    ``emit`` is the mutate-plane fire point's collector for ephemeral live
    activity lines (Slice E): a plain sink (typically ``list.append``) the fire
    point drains and streams after dispatch. It runs under the same guard as the
    recorder, and only for a :func:`_meaningful_activity` run, so a broken sink
    can never crash the turn and no-op runs never reach the stream.
    """
    recorder = getattr(registry, "recorder", None)
    if recorder is None and emit is None:
        return
    try:
        if error is not None:
            detail = str(error)
        elif status == "ok":
            detail = _outcome_detail(outcome)
        elif status == "illegal":
            detail = f"dropped {type(outcome).__name__}"
        else:
            detail = ""
        if recorder is not None:
            recorder(reg, ctx, status=status, detail=detail, duration=duration)
        if emit is not None and _meaningful_activity(status, detail):
            emit({
                "name": reg.name,
                "event": ctx.event.value,
                "status": status,
                "detail": detail,
                "tool_name": ctx.tool_name,
                "tool_call_id": ctx.tool_call_id,
            })
    except Exception:  # noqa: BLE001 - recording must never raise into a turn
        logger.warning("hook execution recorder raised", exc_info=True)


async def _arun_hook(
    reg: Registration, ctx: HookContext, timeout: float, pool: ThreadPoolExecutor
) -> Optional[HookOutcome]:
    """Run one hook on the async path, bounding it by ``timeout``.

    Async hooks are awaited; sync hooks are offloaded to ``pool`` (a plane-scoped
    executor) so a blocking hook cannot stall the event loop (a real risk inside
    ``awrap_tool_call``). A timeout is re-raised as ``_HookTimeout`` carrying
    whether the hook body ever started, so the fault layer can tell a slow hook
    from a saturated pool.
    """
    if inspect.iscoroutinefunction(reg.fn):
        coro = cast(Awaitable[Optional[HookOutcome]], reg.fn(ctx))
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except (asyncio.TimeoutError, FuturesTimeoutError):
            raise _HookTimeout(reg.name, started=True) from None
    started = threading.Event()

    def _runner() -> Optional[HookOutcome]:
        started.set()
        return cast(Optional[HookOutcome], reg.fn(ctx))

    # Use our own pool (not asyncio.to_thread's default executor) so a timed-out
    # sync hook's still-running thread cannot block event-loop shutdown. The
    # context copy is what asyncio.to_thread would have done for us, and is
    # load-bearing: the re-entrance depth below is a ContextVar, and a pool
    # thread with a fresh context reads it back as zero, so a sync hook action
    # that re-enters dispatch would escape the guard entirely.
    loop = asyncio.get_running_loop()
    context = copy_context()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(pool, context.run, _runner), timeout=timeout
        )
    except (asyncio.TimeoutError, FuturesTimeoutError):
        raise _HookTimeout(reg.name, started=started.is_set()) from None
    return cast(Optional[HookOutcome], result)


def _run_hook_sync(
    reg: Registration, ctx: HookContext, timeout: float, pool: ThreadPoolExecutor
) -> Optional[HookOutcome]:
    """Run one hook on the sync path (no running loop), bounding it by ``timeout``.

    A ``future.result(timeout)`` wait conflates queue time with execution time, so
    a hook stuck behind a saturated ``pool`` looks identical to a slow hook. The
    ``started`` event separates them: on timeout, ``started.is_set()`` tells the
    fault layer whether the hook actually ran (see ``_HookTimeout``).
    """
    if inspect.iscoroutinefunction(reg.fn):
        coro = cast(Awaitable[Optional[HookOutcome]], reg.fn(ctx))
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout=timeout))
        except (asyncio.TimeoutError, FuturesTimeoutError):
            raise _HookTimeout(reg.name, started=True) from None
    started = threading.Event()

    def _runner() -> Optional[HookOutcome]:
        started.set()
        return cast(Optional[HookOutcome], reg.fn(ctx))

    # Same context copy as the async twin, for the same reason: the depth
    # counter is a ContextVar and pool.submit starts from an empty context.
    future = pool.submit(copy_context().run, _runner)
    try:
        return cast(Optional[HookOutcome], future.result(timeout=timeout))
    except FuturesTimeoutError:
        # Drops it from the queue if still pending; a no-op once running.
        future.cancel()
        raise _HookTimeout(reg.name, started=started.is_set()) from None


def _reduce(event: HookEvent, outcomes: List[HookOutcome]) -> Optional[HookOutcome]:
    """Collapse per-event outcomes into one, deterministically (registration order)."""
    if not outcomes:
        return None

    if event is HookEvent.PROMPT_SUBMIT:
        texts = [o.inject_context for o in outcomes if isinstance(o, PromptOutcome) and o.inject_context]
        return PromptOutcome(inject_context="\n".join(texts)) if texts else None

    if event in VETO_EVENTS:
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
        if event in VETO_EVENTS:
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
    emit: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Optional[HookOutcome]:
    """Async mutate-plane dispatch: run all matching hooks, reduce to one outcome.

    ``emit``, if given, is a sink for ephemeral activity records (Slice E): the
    fire point passes a collector, then streams what it collected in its own way.
    """
    registry = registry or default_registry
    scratch = scratch or default_scratch
    regs = registry.matching(event, ctx, observe=False)
    if not regs:
        return None
    with _entered_fire_depth() as refused_at:
        if refused_at is not None:
            return _reentrance_outcome(event, refused_at)
        ctx = _with_scratch(ctx, scratch)
        outcomes: List[HookOutcome] = []
        for reg in regs:
            # _accept runs inside the try so a malformed scratch_patch (or any
            # other post-run failure) is isolated per hook rather than crashing
            # the turn; _record runs after the fault handling (see its
            # docstring).
            started_at = time.monotonic()
            outcome: Optional[HookOutcome] = None
            error: Optional[BaseException] = None
            try:
                outcome = await _arun_hook(reg, ctx, reg.timeout or timeout, _mutate_pool)
                status = _accept(event, reg, outcome, ctx, scratch, outcomes)
            except Exception as exc:  # noqa: BLE001 - a hook can never crash a turn
                error, status = exc, _fault_status(exc)
                fault = _fault_outcome(event, reg, exc)
                if fault is not None:
                    outcomes.append(fault)
            _record(
                registry, reg, ctx,
                status=status, duration=time.monotonic() - started_at,
                outcome=outcome, error=error, emit=emit,
            )
    return _reduce_safe(event, outcomes)


def dispatch(
    event: HookEvent,
    ctx: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    scratch: Optional[ScratchStore] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
    emit: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Optional[HookOutcome]:
    """Sync mutate-plane dispatch (bridge for no-loop callers)."""
    registry = registry or default_registry
    scratch = scratch or default_scratch
    regs = registry.matching(event, ctx, observe=False)
    if not regs:
        return None
    with _entered_fire_depth() as refused_at:
        if refused_at is not None:
            return _reentrance_outcome(event, refused_at)
        ctx = _with_scratch(ctx, scratch)
        outcomes: List[HookOutcome] = []
        for reg in regs:
            # _accept runs inside the try so a malformed scratch_patch (or any
            # other post-run failure) is isolated per hook rather than crashing
            # the turn; _record runs after the fault handling (see its
            # docstring).
            started_at = time.monotonic()
            outcome: Optional[HookOutcome] = None
            error: Optional[BaseException] = None
            try:
                outcome = _run_hook_sync(reg, ctx, reg.timeout or timeout, _mutate_pool)
                status = _accept(event, reg, outcome, ctx, scratch, outcomes)
            except Exception as exc:  # noqa: BLE001 - a hook can never crash a turn
                error, status = exc, _fault_status(exc)
                fault = _fault_outcome(event, reg, exc)
                if fault is not None:
                    outcomes.append(fault)
            _record(
                registry, reg, ctx,
                status=status, duration=time.monotonic() - started_at,
                outcome=outcome, error=error, emit=emit,
            )
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
    with _entered_fire_depth() as refused_at:
        if refused_at is not None:
            # Nothing to return on this plane, but the cycle is just as real:
            # an observe hook's action can run a workflow, which calls tools,
            # which fire hooks.
            _log_reentrance(event, refused_at, level=logging.WARNING)
            return
        ctx = _with_scratch(ctx, scratch)
        for reg in regs:
            started_at = time.monotonic()
            status = "ok"
            error: Optional[BaseException] = None
            try:
                outcome = await _arun_hook(reg, ctx, reg.timeout or timeout, _observe_pool)
                _apply_observe_patch(event, reg, outcome, ctx, scratch)
            except Exception as exc:  # noqa: BLE001 - observe never affects the turn
                logger.warning("observe hook %r raised on %s", reg.name, event.value, exc_info=True)
                error, status = exc, _fault_status(exc)
            _record(
                registry, reg, ctx,
                status=status, duration=time.monotonic() - started_at, error=error,
            )


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
    with _entered_fire_depth() as refused_at:
        if refused_at is not None:
            _log_reentrance(event, refused_at, level=logging.WARNING)
            return
        ctx = _with_scratch(ctx, scratch)
        for reg in regs:
            started_at = time.monotonic()
            status = "ok"
            error: Optional[BaseException] = None
            try:
                outcome = _run_hook_sync(reg, ctx, reg.timeout or timeout, _observe_pool)
                _apply_observe_patch(event, reg, outcome, ctx, scratch)
            except Exception as exc:  # noqa: BLE001 - observe never affects the turn
                logger.warning("observe hook %r raised on %s", reg.name, event.value, exc_info=True)
                error, status = exc, _fault_status(exc)
            _record(
                registry, reg, ctx,
                status=status, duration=time.monotonic() - started_at, error=error,
            )


# --------------------------------------------------------------------------- #
# Fire-and-forget scheduling (observe plane)
# --------------------------------------------------------------------------- #

def schedule_observe(
    event: HookEvent,
    ctx: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    scratch: Optional[ScratchStore] = None,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
) -> None:
    """Schedule an observe-plane dispatch off-turn and return immediately.

    This is what makes the observe plane truly fire-and-forget: the fire point
    no longer waits for the hooks (previously each observe hook's run, up to
    its timeout, sat in-band on the turn tail or inside the tool call). The
    per-hook machinery (budgets, recording, fault swallowing) is unchanged;
    only where the dispatch is awaited moves.

    Never blocks and never raises. Safe from any context, including a
    ``finally`` during ``GeneratorExit`` (it awaits nothing): with a running
    loop the dispatch becomes a background task (strong-ref'd in
    ``_observe_tasks``); without one it is submitted to the dedicated
    ``_observe_dispatch_pool``. Zero-hook turns return before either.

    Semantics callers accept: side effects may complete after the turn ends
    (and after the thread lock releases); pending work is best-effort at
    process shutdown; the scratch snapshot is taken when the dispatch runs,
    after the same event's in-band mutate hooks.
    """
    registry = registry or default_registry
    try:
        if not registry.has_observe(event):
            return
        try:
            loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            try:
                task = loop.create_task(
                    adispatch_observe(
                        event, ctx, registry=registry, scratch=scratch, timeout=timeout
                    )
                )
                _observe_tasks.add(task)
                task.add_done_callback(_observe_tasks.discard)
                return
            except RuntimeError:
                pass  # loop is shutting down; fall through to the pool path
        # Context copy, as on every other thread hop in this file. The
        # create_task branch above inherits the context for free, so without
        # this the two branches of one function disagree about whether the
        # re-entrance depth carries, and which one you get depends on whether a
        # loop happened to be running.
        future = _observe_dispatch_pool.submit(
            copy_context().run,
            functools.partial(
                dispatch_observe,
                event,
                ctx,
                registry=registry,
                scratch=scratch,
                timeout=timeout,
            ),
        )
        _observe_futures.add(future)
        future.add_done_callback(_observe_futures.discard)
    except Exception:  # noqa: BLE001 - scheduling must never raise into a turn
        logger.warning("failed to schedule observe hooks on %s", event.value, exc_info=True)


async def adrain_observe() -> None:
    """Await all loop-scheduled observe dispatches (test / shutdown barrier)."""
    tasks = [t for t in list(_observe_tasks) if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def drain_observe(timeout: float = 5.0) -> None:
    """Wait for pool-scheduled observe dispatches (test / shutdown barrier).

    Only covers the no-loop path; loop-scheduled tasks belong to their loop and
    are drained with :func:`adrain_observe` from within it.
    """
    futures = [f for f in list(_observe_futures) if not f.done()]
    if futures:
        futures_wait(futures, timeout=timeout)
