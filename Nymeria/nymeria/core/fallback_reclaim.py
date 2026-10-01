"""Primary reclaim for a fallback hold (#439).

A hold (``ThreadConfig.active_llm_fallback``) exists to ride out an outage,
but nothing used to notice the outage ending: the thread kept the fallback
until its timer, forever for a permanent hold. This module notices.

Shape (coordinator decisions M1 to M8, design record
``docs/private/plans/llm-fallback-consent.md`` "Primary reclaim"):

- DETECTION is a turn-start probe: at the start of a held thread's turn
  (``NymeriaAgent.chat``/``astream``, under the turn lock, BEFORE the graph
  lookup) an eligible hold whose route is due gets ONE tiny completion probe
  against the thread's CONFIGURED primary, resolved by the real resolver
  with the hold ignored and built by ``create_llm`` (so a CLIProxy request
  carries the billing block, the ``claude-cli`` user agent and the
  Anthropic-Beta override, and no ``cache_control``). One request, no SDK or
  ladder retries, a short timeout.
- NON-BLOCKING: the probe runs in the background and its verdict applies at
  the NEXT turn start. During a real outage a blocking probe would add up to
  its timeout to a user turn every probe interval; one more turn on the
  fallback after recovery costs far less. At most one check per thread and
  one probe per route are in flight.
- POLICY on a healthy verdict lives in the consent layer
  (``fallback_approvals.reclaim_action``): "auto" switch mode ends a TIMED
  hold (reason ``recovered``); "ask" mode and permanent holds keep the hold
  and get ONE offer (``reclaim_offered_at`` is stamped, the hold is never
  probed again). The action happens only at a turn start, so a model never
  changes mid-turn and every switch carries its persisted note.
- A route (provider, model, base URL, credential hash, provider route, API
  mode) backs off on failure: ``interval * 2**failures`` capped at
  ``max(interval, 1 hour)``; 429/401/403 jump to the cap; a 400/422 stops
  probing that hold for good (an ambiguous signal must neither reclaim nor
  spin). A hold re-forming on a route within 30 minutes of its reclaim (a
  flap) starts that route at the cap. Route state is in-memory: a restart
  costs at most one extra probe per route.
- The turn-start seam also evicts an EXPIRED hold (reason ``expired``): the
  idle-only eviction never saw one at a turn start, because the turn's own
  lock marks the thread busy, so an expired hold used to drive one more turn.

Only the API process runs agents, so only it probes (both runtime shapes,
both TurnExecutor shapes: every turn ends in ``NymeriaAgent.chat`` or
``astream``). The worker, MCP and bot processes never import this module.
Nothing here is reachable by the agent: no tool, no command; ``/fallback
revert`` stays the human's.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

EVENT_TYPE = "fallback_hold_reclaimed"
PROBE_PROMPT = "Reply with ok."

DEFAULT_INTERVAL_SECONDS = 600
MAX_INTERVAL_SECONDS = 86400
_MIN_CAP_SECONDS = 3600
# The probe request's own timeout. The turn never waits on it (non-blocking),
# so this only bounds how long a route stays "in flight".
PROBE_TIMEOUT_SECONDS = 10
# A tiny answer; always-on-thinking Claude models (no "off" tier) get room for
# low-effort adaptive thinking plus the answer, sized so the factory's
# max_tokens-vs-thinking warning stays quiet.
PROBE_MAX_TOKENS = 64
PROBE_MAX_TOKENS_ALWAYS_THINKING = 2048
# A route whose probe has been "in flight" this long is presumed hung (a
# provider factory that ignores request_timeout): the next due check replaces
# it rather than wedging the route until restart.
_INFLIGHT_STALE_SECONDS = 120
# How long a check that found another thread's probe in flight waits on it.
_JOIN_TIMEOUT_SECONDS = 60
_FLAP_WINDOW = timedelta(minutes=30)

HEALTHY = "healthy"
PROBE_INVALID = "probe_invalid"
FLAPPED = "flapped"
# Reasons the hold itself was created for that are never probed: a refusal is
# content-scoped (a primary retry would likely re-refuse) and an invalid
# request is wire/history-scoped (a tiny probe would read healthy while the
# history still 400s). Every transport-health reason, auth_error included,
# and legacy holds with no reason are eligible.
EXCLUDED_REASONS = frozenset({"refusal", "invalid_request"})
# Probe failures that jump straight to the backoff cap.
_CAP_VERDICTS = frozenset({"rate_limited", "auth_error", PROBE_INVALID})


def _now() -> datetime:
    return utc_now()


def _spawn(target: Callable[[], None]) -> None:
    """Run a background check. A seam tests replace to run checks inline."""
    threading.Thread(target=target, name="nymeria-llm-reclaim", daemon=True).start()


# The single-flight handle for a route's probe in flight. A seam tests replace
# with an instrumented subclass to observe checks joining a probe.
_FUTURE_FACTORY = concurrent.futures.Future


@dataclass
class _RouteState:
    failures: int = 0
    next_due: Optional[datetime] = None
    last_verdict: Optional[str] = None
    verdict_at: Optional[datetime] = None
    reclaimed_at: Optional[datetime] = None
    flap_hold: Optional[tuple] = None
    inflight: Optional[concurrent.futures.Future] = None
    inflight_started_at: Optional[datetime] = None
    invalid_warned: bool = False


@dataclass
class _ThreadState:
    hold_key: tuple
    inflight: bool = False
    route_key: Optional[tuple] = None
    identity: Optional[str] = None
    verdict: Optional[str] = None
    verdict_at: Optional[datetime] = None
    next_check_at: Optional[datetime] = None
    stopped: bool = False


_LOCK = threading.Lock()
_ROUTES: dict[tuple, _RouteState] = {}
_THREADS: dict[str, _ThreadState] = {}


def reset_reclaim_state_for_tests() -> None:
    with _LOCK:
        _ROUTES.clear()
        _THREADS.clear()


# -- settings ------------------------------------------------------------------


def reclaim_interval_seconds(settings: Any) -> int:
    """The global ``llm_fallback_reclaim_interval_seconds`` (0 = off),
    clamped to its [0, 86400] bounds. Read per turn, never baked into a
    graph."""
    raw = getattr(settings, "llm_fallback_reclaim_interval_seconds", None)
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return max(0, min(MAX_INTERVAL_SECONDS, value))


def backoff_cap_seconds(interval: int) -> int:
    return max(int(interval), _MIN_CAP_SECONDS)


def _backoff_seconds(interval: int, failures: int, verdict: str) -> int:
    cap = backoff_cap_seconds(interval)
    if verdict in _CAP_VERDICTS:
        return cap
    return min(cap, int(interval) * (2 ** max(1, failures)))


# -- identities ----------------------------------------------------------------


def hold_key(hold: Any) -> tuple:
    """One hold's identity: a new hold (even onto the same model) is a new
    key, so a verdict or an offer never carries across holds."""
    return (
        ensure_aware_utc(hold.activated_at).isoformat(),
        str(hold.provider or ""),
        str(hold.model or ""),
    )


def route_key(cfg: Any) -> tuple:
    """The route a probe proves: provider, model, base URL, a hash of the
    credential (two users' vault keys are two routes, so one user's verdict
    never decides another's; the key itself is never kept), provider route
    and API mode."""
    api_key = str(getattr(cfg, "api_key", None) or "")
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else ""
    return (
        str(getattr(cfg, "provider", None) or ""),
        str(getattr(cfg, "model", None) or ""),
        str(getattr(cfg, "base_url", None) or ""),
        key_hash,
        str(getattr(cfg, "provider_route", None) or ""),
        str(getattr(cfg, "openai_api_mode", None) or ""),
    )


def _route_label(rkey: tuple) -> str:
    """Log label: provider/model plus a short hash of the whole route. The
    destination is deliberately not logged: a base URL can come from a vault
    record or an interpolated secret (see ``_reportable_destination``)."""
    digest = hashlib.sha256(repr(rkey).encode("utf-8")).hexdigest()[:8]
    return f"{rkey[0]}/{rkey[1]}#{digest}"


def _configured_fingerprint(llm_config: Any, settings: Any) -> str:
    """A no-I/O fingerprint of what the thread is configured to run (thread
    route pins plus the global route). A verdict recorded under a different
    fingerprint (the global model or base URL moved since the probe) proves
    nothing about the route the thread would switch to now."""
    from .agent_llm_config import _route_identity

    identity = (
        _route_identity(llm_config, settings),
        getattr(settings, "llm_base_url", None),
        getattr(settings, "llm_provider_route", None),
        getattr(settings, "openai_api_mode", None),
    )
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:16]


# -- the probe -----------------------------------------------------------------


def probe_config(primary: Any) -> Any:
    """The probe's LLMConfig, derived from the resolver's config for the
    primary (never assembled by hand from thread fields): a tiny answer,
    thinking off (or the model's floor where it cannot be off), a short
    timeout, zero retries, and no fallbacks, callbacks, pending note,
    prompt-cache key or custom client."""
    from ..config.model_capabilities import (
        anthropic_thinking_always_on,
        clamp_reasoning_effort,
    )

    model = str(primary.model or "")
    effort = clamp_reasoning_effort(
        str(primary.provider or ""), model, "off", primary.provider_route
    )
    return dataclasses.replace(
        primary,
        max_tokens=(
            PROBE_MAX_TOKENS_ALWAYS_THINKING
            if anthropic_thinking_always_on(model.lower())
            else PROBE_MAX_TOKENS
        ),
        extended_thinking=False,
        reasoning_effort=effort or "off",
        request_timeout=PROBE_TIMEOUT_SECONDS,
        stream_max_retries=0,
        fallbacks=[],
        active_fallback_candidate_index=0,
        fallback_activation_callback=None,
        fallback_decision_callback=None,
        pending_fallback_note=None,
        prompt_cache_key=None,
        custom_llm=None,
    )


def classify_probe_failure(exc: BaseException) -> str:
    """A probe exception's verdict, in the hold reasons' own taxonomy.

    400/422 is ``probe_invalid``: the route answered, but rejected the
    probe's shape, which says nothing about recovery (it stops probing that
    hold). Everything else is the retry ladder's reason mapping:
    ``rate_limited`` and ``auth_error`` back off to the cap, the rest
    (5xx, 408 to 425, timeouts, transport, unknown) double."""
    from ..vendor.react_agent.nodes import llm_error_status_code, llm_failure_reason

    if llm_error_status_code(exc) in (400, 422):
        return PROBE_INVALID
    return llm_failure_reason(exc)


def run_probe(primary: Any) -> str:
    """One probe request against ``primary``; never raises. Any return (text,
    empty, truncated, even a refusal) means the route serves completions."""
    from langchain_core.messages import HumanMessage

    from ..vendor.react_agent.providers import create_llm

    try:
        llm = create_llm(probe_config(primary))
        llm.invoke([HumanMessage(content=PROBE_PROMPT)])
    except Exception as exc:  # noqa: BLE001 - the verdict IS the exception class
        return classify_probe_failure(exc)
    return HEALTHY


# -- eligibility and the turn-start seam ---------------------------------------


def _expired(hold: Any, now: datetime) -> bool:
    return hold.expires_at is not None and ensure_aware_utc(hold.expires_at) <= now


def _eligible(hold: Any, interval: int, now: datetime) -> bool:
    """Checked before any I/O."""
    if interval <= 0:
        return False
    if str(hold.reason or "") in EXCLUDED_REASONS:
        return False
    if getattr(hold, "reclaim_offered_at", None) is not None:
        return False
    return now - ensure_aware_utc(hold.activated_at) >= timedelta(seconds=interval)


def _forget_thread(thread_id: str) -> None:
    with _LOCK:
        _THREADS.pop(thread_id, None)


def settle_hold_at_turn_start(
    host: Any,
    thread_id: str,
    user_id: str,
    *,
    offers: bool = True,
) -> Optional[dict[str, Any]]:
    """The turn-start seam, called by the lock holder before the graph
    lookup (never from the graph lookup itself: a mid-turn rebuild must not
    switch models).

    Evicts an expired hold; applies a healthy verdict a previous turn's probe
    recorded (returns the ``fallback_hold_reclaimed`` event for the caller to
    stream); otherwise, when the hold is eligible and due, starts ONE
    background check and returns None (the turn runs the hold exactly as
    before). ``offers=False`` (the sync ``chat()`` turn, which cannot stream)
    leaves an offer for the next streaming turn. Never raises: any fault
    leaves the turn on its hold.
    """
    if not thread_id:
        return None
    manager = getattr(host, "thread_config_manager", None)
    if manager is None:
        return None
    try:
        return _settle(host, manager, thread_id, user_id, offers=offers)
    except Exception:  # noqa: BLE001 - must never fail a turn
        logger.warning(
            "[LLM RECLAIM] thread=%s turn-start settle failed; the turn runs on "
            "its hold",
            thread_id,
            exc_info=True,
        )
        return None


def _settle(
    host: Any,
    manager: Any,
    thread_id: str,
    user_id: str,
    *,
    offers: bool,
) -> Optional[dict[str, Any]]:
    from .agent_llm_config import clear_active_llm_fallback

    tc = manager.get_config(thread_id)
    hold = getattr(tc, "active_llm_fallback", None) if tc is not None else None
    if hold is None:
        _forget_thread(thread_id)
        return None
    now = _now()
    if _expired(hold, now):
        # The lock holder evicts: the idle-only sweep cannot see this thread
        # idle at a turn start (its own lock is held), so without this the
        # expired hold drove this whole turn and ended one turn late.
        cleared = clear_active_llm_fallback(
            host, thread_id, reason="expired", expected_activated_at=hold.activated_at
        )
        _forget_thread(thread_id)
        if cleared is not None:
            logger.info(
                "[LLM RECLAIM] thread=%s evicted an expired hold at turn start "
                "(%s/%s)",
                thread_id,
                cleared.provider,
                cleared.model,
            )
        return None
    settings = getattr(host, "settings", None)
    interval = reclaim_interval_seconds(settings)
    if not _eligible(hold, interval, now):
        return None
    key = hold_key(hold)
    fingerprint = _configured_fingerprint(tc.llm_config, settings)
    cap = timedelta(seconds=backoff_cap_seconds(interval))
    with _LOCK:
        state = _THREADS.get(thread_id)
        if state is None or state.hold_key != key:
            state = _ThreadState(hold_key=key)
            _THREADS[thread_id] = state
        if state.stopped:
            return None
        ready = (
            state.verdict == HEALTHY
            and not state.inflight
            and state.verdict_at is not None
            and now - state.verdict_at <= cap
            and state.identity == fingerprint
        )
        if not ready and state.verdict == HEALTHY:
            # Stale (too old, or recorded under another configured route):
            # discard it and check again.
            state.verdict = None
            state.verdict_at = None
            state.next_check_at = None
    if ready:
        return _apply_healthy(host, manager, thread_id, tc, hold, state, settings, offers)
    with _LOCK:
        if state.inflight:
            return None
        if state.next_check_at is not None and now < state.next_check_at:
            return None
        state.inflight = True
        state.identity = fingerprint
    _spawn(lambda: _check_thread(host, thread_id, user_id, hold, key))
    return None


def _apply_healthy(
    host: Any,
    manager: Any,
    thread_id: str,
    tc: Any,
    hold: Any,
    state: _ThreadState,
    settings: Any,
    offers: bool,
) -> Optional[dict[str, Any]]:
    from .agent_llm_config import (
        _resolve_thread_llm_override,
        clear_active_llm_fallback,
        configured_provider_model,
    )
    from .fallback_approvals import reclaim_action

    llm = getattr(tc, "llm_config", None)
    switch_mode = _resolve_thread_llm_override(
        getattr(llm, "fallback_switch_mode", None) if llm is not None else None,
        getattr(settings, "llm_fallback_switch_mode", "auto"),
    )
    permanent = hold.expires_at is None
    action = reclaim_action(switch_mode=switch_mode, permanent=permanent)
    to_provider, to_model = configured_provider_model(llm, settings)
    event: dict[str, Any] = {
        "type": EVENT_TYPE,
        "thread_id": thread_id,
        "reason": "recovered",
        "from_provider": str(hold.provider or ""),
        "from_model": str(hold.model or ""),
        "to_provider": to_provider,
        "to_model": to_model,
        "expires_at": (
            ensure_aware_utc(hold.expires_at).isoformat()
            if hold.expires_at is not None
            else None
        ),
        "permanent": permanent,
    }
    label = _route_label(state.route_key) if state.route_key else f"{to_provider}/{to_model}"
    if action == "end":
        cleared = clear_active_llm_fallback(
            host,
            thread_id,
            reason="recovered",
            expected_activated_at=hold.activated_at,
        )
        _forget_thread(thread_id)
        if cleared is None:
            # A revert (or a route change) landed since the read: it wins.
            return None
        with _LOCK:
            if state.route_key is not None:
                _ROUTES.setdefault(state.route_key, _RouteState()).reclaimed_at = _now()
        logger.info(
            "[LLM RECLAIM] thread=%s route=%s outcome=ended (hold on %s/%s ended, "
            "reason recovered)",
            thread_id,
            label,
            hold.provider,
            hold.model,
        )
        return {**event, "outcome": "ended"}
    if not offers:
        # A sync turn cannot show an offer; keep the verdict for the next
        # streaming turn rather than spending the one offer on nobody.
        return None
    if not _stamp_offer(manager, thread_id, hold.activated_at):
        _forget_thread(thread_id)
        return None
    _forget_thread(thread_id)
    logger.info(
        "[LLM RECLAIM] thread=%s route=%s outcome=offered (hold on %s/%s stays, "
        "%s)",
        thread_id,
        label,
        hold.provider,
        hold.model,
        "permanent" if permanent else f"switch mode {switch_mode}",
    )
    return {**event, "outcome": "offered"}


def _stamp_offer(manager: Any, thread_id: str, activated_at: Any) -> bool:
    """Persist the once-only offer on the SAME hold (re-read: a revert that
    landed since the settle's read wins)."""
    tc = manager.get_config(thread_id)
    active = getattr(tc, "active_llm_fallback", None) if tc is not None else None
    if active is None or ensure_aware_utc(active.activated_at) != ensure_aware_utc(
        activated_at
    ):
        return False
    active.reclaim_offered_at = _now()
    return bool(manager.save_config(tc))


# -- the background check ------------------------------------------------------


def _record(
    thread_id: str,
    key: tuple,
    rkey: Optional[tuple],
    *,
    verdict: Optional[str],
    at: Optional[datetime],
    next_check_at: Optional[datetime],
) -> None:
    with _LOCK:
        state = _THREADS.get(thread_id)
        if state is None or state.hold_key != key:
            return  # the hold changed meanwhile: this verdict is about another hold
        if rkey is not None:
            state.route_key = rkey
        state.next_check_at = next_check_at
        if verdict is not None:
            state.verdict = verdict
            state.verdict_at = at
            if verdict == PROBE_INVALID:
                state.stopped = True


def _apply_flap(route: _RouteState, hold: Any, key: tuple, cap: int, now: datetime) -> bool:
    if route.reclaimed_at is None or route.flap_hold == key:
        return False
    gap = ensure_aware_utc(hold.activated_at) - route.reclaimed_at
    if not (timedelta(0) <= gap <= _FLAP_WINDOW):
        return False
    route.flap_hold = key
    start = ensure_aware_utc(hold.activated_at) + timedelta(seconds=cap)
    route.next_due = max(route.next_due or start, start)
    route.last_verdict = FLAPPED
    route.verdict_at = now
    return True


def _update_route(
    route: _RouteState, verdict: str, at: datetime, interval: int
) -> None:
    if verdict == HEALTHY:
        route.failures = 0
        route.next_due = at + timedelta(seconds=interval)
    else:
        route.failures += 1
        route.next_due = at + timedelta(
            seconds=_backoff_seconds(interval, route.failures, verdict)
        )
    route.last_verdict = verdict
    route.verdict_at = at


def _check_thread(host: Any, thread_id: str, user_id: str, hold: Any, key: tuple) -> None:
    """Background body: resolve the configured primary (hold ignored, a pure
    read), then reuse a route verdict, wait on a probe in flight, or probe."""
    from .agent_llm_config import get_llm_config_for_thread

    interval = reclaim_interval_seconds(getattr(host, "settings", None))
    rkey: Optional[tuple] = None
    try:
        if interval <= 0:
            return
        cap = backoff_cap_seconds(interval)
        cfg = get_llm_config_for_thread(
            host, thread_id, acting_user_id=user_id or None, ignore_active_fallback=True
        )
        rkey = route_key(cfg)
        label = _route_label(rkey)
        now = _now()
        activated = ensure_aware_utc(hold.activated_at)
        future: Optional[concurrent.futures.Future] = None
        owner = False
        reuse: Optional[tuple] = None
        with _LOCK:
            route = _ROUTES.setdefault(rkey, _RouteState())
            if _apply_flap(route, hold, key, cap, now):
                logger.info(
                    "[LLM RECLAIM] thread=%s route=%s verdict=flapped next_due=%s "
                    "(a hold re-formed within 30 minutes of a reclaim)",
                    thread_id,
                    label,
                    route.next_due.isoformat() if route.next_due else "",
                )
            inflight = route.inflight
            if (
                inflight is not None
                and route.inflight_started_at is not None
                and (now - route.inflight_started_at).total_seconds()
                > _INFLIGHT_STALE_SECONDS
            ):
                inflight = None  # presumed hung: replace it below
            if inflight is not None:
                future = inflight
            elif route.next_due is not None and now < route.next_due:
                # Not due. A verdict newer than this hold is reused (one
                # probe serves every thread held off the route); an older
                # one predates this hold's failure and proves nothing.
                fresh = route.verdict_at is not None and route.verdict_at >= activated
                reuse = (
                    route.last_verdict if fresh else None,
                    route.verdict_at if fresh else None,
                    route.next_due,
                )
            else:
                future = _FUTURE_FACTORY()
                route.inflight = future
                route.inflight_started_at = now
                owner = True
        if reuse is not None:
            verdict, at, next_due = reuse
            _record(thread_id, key, rkey, verdict=verdict, at=at, next_check_at=next_due)
            return
        assert future is not None
        if owner:
            verdict = run_probe(cfg)
            done_at = _now()
            warn_invalid = False
            with _LOCK:
                if route.inflight is future:
                    _update_route(route, verdict, done_at, interval)
                    route.inflight = None
                    route.inflight_started_at = None
                next_due = route.next_due
                if verdict == PROBE_INVALID and not route.invalid_warned:
                    route.invalid_warned = True
                    warn_invalid = True
            future.set_result((verdict, done_at, next_due))
            if verdict == PROBE_INVALID:
                (logger.warning if warn_invalid else logger.info)(
                    "[LLM RECLAIM] thread=%s route=%s verdict=%s next_due=%s "
                    "(the primary rejected the probe request itself; this hold "
                    "is not probed again)",
                    thread_id,
                    label,
                    verdict,
                    next_due.isoformat() if next_due else "",
                )
            else:
                logger.info(
                    "[LLM RECLAIM] thread=%s route=%s verdict=%s next_due=%s",
                    thread_id,
                    label,
                    verdict,
                    next_due.isoformat() if next_due else "",
                )
        else:
            try:
                verdict, done_at, next_due = future.result(timeout=_JOIN_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - a hung probe: check again later
                _record(
                    thread_id,
                    key,
                    rkey,
                    verdict=None,
                    at=None,
                    next_check_at=_now() + timedelta(seconds=interval),
                )
                return
        _record(thread_id, key, rkey, verdict=verdict, at=done_at, next_check_at=next_due)
    except Exception:  # noqa: BLE001 - a background check must never escape
        logger.warning(
            "[LLM RECLAIM] thread=%s check failed; next check after one interval",
            thread_id,
            exc_info=True,
        )
        _record(
            thread_id,
            key,
            rkey,
            verdict=None,
            at=None,
            next_check_at=_now() + timedelta(seconds=max(interval, 1)),
        )
    finally:
        with _LOCK:
            state = _THREADS.get(thread_id)
            if state is not None and state.hold_key == key:
                state.inflight = False


# -- status (``/fallback status``, ``GET /threads/{id}/config``) ---------------


def _iso(value: Optional[datetime]) -> Optional[str]:
    return ensure_aware_utc(value).isoformat() if value is not None else None


def reclaim_status(thread_id: str, hold: Any, settings: Any) -> Optional[dict[str, Any]]:
    """What the reclaim knows about ``thread_id``'s hold, for display.

    ``state``: ``offered`` (the primary answered while the hold stays; the
    revert was offered), ``off`` (interval 0), ``excluded`` (a refusal or
    invalid-request hold), ``expired`` (ends at the next turn start),
    ``stopped`` (the probe was rejected as invalid), ``checking`` (a probe is
    in flight), ``recovered`` (the primary answered; applies at the next turn
    start) or ``scheduled`` (``next_check_at`` is the earliest turn start
    that checks). Computed in the API process, so both command client shapes
    read it through the thread-config payload.
    """
    if hold is None:
        return None
    interval = reclaim_interval_seconds(settings)
    now = _now()
    status: dict[str, Any] = {
        "state": "scheduled",
        "interval_seconds": interval,
        "next_check_at": None,
        "last_verdict": None,
        "last_checked_at": None,
        "offered_at": _iso(getattr(hold, "reclaim_offered_at", None)),
    }
    if status["offered_at"] is not None:
        status["state"] = "offered"
        return status
    if _expired(hold, now):
        status["state"] = "expired"
        return status
    if interval <= 0:
        status["state"] = "off"
        return status
    if str(hold.reason or "") in EXCLUDED_REASONS:
        status["state"] = "excluded"
        return status
    with _LOCK:
        state = _THREADS.get(thread_id)
        if state is not None and state.hold_key != hold_key(hold):
            state = None
        snapshot = dataclasses.replace(state) if state is not None else None
    if snapshot is not None:
        status["last_verdict"] = snapshot.verdict
        status["last_checked_at"] = _iso(snapshot.verdict_at)
        if snapshot.stopped:
            status["state"] = "stopped"
            return status
        if snapshot.inflight:
            status["state"] = "checking"
            return status
        if snapshot.verdict == HEALTHY:
            status["state"] = "recovered"
            return status
    first = ensure_aware_utc(hold.activated_at) + timedelta(seconds=interval)
    next_check = first
    if snapshot is not None and snapshot.next_check_at is not None:
        next_check = max(first, snapshot.next_check_at)
    status["next_check_at"] = _iso(next_check)
    return status


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "EVENT_TYPE",
    "EXCLUDED_REASONS",
    "HEALTHY",
    "PROBE_INVALID",
    "PROBE_PROMPT",
    "backoff_cap_seconds",
    "classify_probe_failure",
    "hold_key",
    "probe_config",
    "reclaim_interval_seconds",
    "reclaim_status",
    "reset_reclaim_state_for_tests",
    "route_key",
    "run_probe",
    "settle_hold_at_turn_start",
]
