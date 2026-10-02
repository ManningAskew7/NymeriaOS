"""Primary reclaim for a fallback hold (#439).

A hold (``ThreadConfig.active_llm_fallback``) exists to ride out an outage,
but nothing used to notice the outage ending: the thread kept the fallback
until its timer, forever for a permanent hold. This module notices.

Shape (coordinator decisions M1 to M8 plus the review fixes, design record
``docs/private/plans/llm-fallback-consent.md`` "Primary reclaim"):

- DETECTION is a turn-start probe: at the start of a held thread's turn
  (``NymeriaAgent.chat``/``astream``, under the turn lock, BEFORE the graph
  lookup) an eligible hold whose route is due gets ONE tiny completion probe
  against the thread's CONFIGURED primary, resolved by the real resolver
  with the hold ignored and built by ``create_llm`` (so a CLIProxy request
  carries the billing block, the ``claude-cli`` user agent and the
  Anthropic-Beta override, and no ``cache_control``). One request, no SDK or
  ladder retries, a timeout of ``PROBE_TIMEOUT_SECONDS``.
- NON-BLOCKING: the probe runs in the background and its verdict applies at
  the NEXT turn start. During a real outage a blocking probe would add up to
  its timeout to a user turn every probe interval; one more turn on the
  fallback after recovery costs far less. Single flight per route: a thread
  whose route already has a probe in flight starts none and reads the
  route's verdict at its next turn start. A probe (or a thread's check) in
  flight longer than the timeout plus a margin is presumed hung and
  discarded, so nothing stays "checking".
- POLICY on a healthy verdict lives in the consent layer
  (``fallback_approvals.reclaim_action``): a hold a human chose
  (``hold_origin`` "user", or permanent) is never auto-ended: it stays and
  gets ONE offer (``reclaim_offered_at`` is stamped, the hold is never
  probed again); an "automatic" hold ends (reason ``recovered``); a legacy
  hold with no recorded origin follows the thread's switch mode ("auto"
  ends, "ask" offers). The action happens only at a turn start, so a model
  never changes mid-turn and every switch carries its persisted note.
- An OFFER is spent only on a turn that can show it
  (``fallback_approvals.reclaim_offer_renders``: a human-driven streaming
  turn from a chat bot, or from a client that declared it renders the
  event). Any other turn neither probes for an offer nor spends it, so a
  desktop or autonomous turn never burns the one notice on nobody.
- A route (provider, model, base URL, credential hash, provider route, API
  mode) backs off on failure: the n-th consecutive failure waits
  ``interval * 2**n`` (20 then 40 minutes at the default 600 s), capped at
  ``max(interval, 1 hour)``; 429/401/403 and a probe that could not run on
  our side jump to the cap; a 400/422 stops probing that hold for good (an
  ambiguous signal must neither reclaim nor spin). A verdict is about a hold
  only when its probe was SENT after that hold formed. A hold re-forming on
  a route within 30 minutes of its reclaim (a flap) starts that route at
  the cap. Route state is in-memory: a restart costs at most one extra probe
  per route.
- The turn-start seam also evicts an EXPIRED hold (reason ``expired``), a
  ``/resume`` re-drive included (its note rides the next prompted turn): the
  idle-only eviction never saw one at a turn start, because the turn's own
  lock marks the thread busy, so an expired hold used to drive one more
  turn.

Only the API process runs agents, so only it probes (both runtime shapes,
both TurnExecutor shapes: every turn ends in ``NymeriaAgent.chat`` or
``astream``). The worker, MCP and bot processes never import this module.
Nothing here is reachable by the agent: no tool, no command; ``/fallback
revert`` stays the human's.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
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
# so this only bounds how long a route stays "in flight"; generous, so a slow
# but healthy primary (a non-streaming reply through a loaded proxy) is not
# misjudged as down.
PROBE_TIMEOUT_SECONDS = 30
# A tiny answer; always-on-thinking Claude models (no "off" tier) get room for
# low-effort adaptive thinking plus the answer, sized so the factory's
# max_tokens-vs-thinking warning stays quiet. A ceiling, not a charge: the
# spend is the actual output of "Reply with ok.".
PROBE_MAX_TOKENS = 64
PROBE_MAX_TOKENS_ALWAYS_THINKING = 2048
# A probe (or a thread's background check) in flight this long is presumed
# hung (a provider factory that ignores request_timeout): it is discarded,
# so the route can be probed again and no status stays "checking". A late
# result from it never overwrites a newer probe's verdict.
_INFLIGHT_STALE_SECONDS = PROBE_TIMEOUT_SECONDS + 30
_FLAP_WINDOW = timedelta(minutes=30)

HEALTHY = "healthy"
PROBE_INVALID = "probe_invalid"
# The probe could not run on OUR side (a bug or a misconfiguration building or
# driving the client): never a provider verdict. Logged with its traceback and
# backed off to the cap, never read as recovery or as an outage.
PROBE_ERROR = "probe_error"
FLAPPED = "flapped"
# Reasons the hold itself was created for that are never probed: a refusal is
# content-scoped (a primary retry would likely re-refuse) and an invalid
# request is wire/history-scoped (a tiny probe would read healthy while the
# history still 400s). Every transport-health reason, auth_error included,
# and legacy holds with no reason are eligible.
EXCLUDED_REASONS = frozenset({"refusal", "invalid_request"})
# Probe verdicts that jump straight to the backoff cap.
_CAP_VERDICTS = frozenset({"rate_limited", "auth_error", PROBE_INVALID, PROBE_ERROR})

# A probe that failed ONLY because its tiny output budget ran out still
# proves the route serves (the model generated until the cap), so it is
# HEALTHY, never a failure: a reasoning model can spend a 64-token budget on
# reasoning alone. Shapes: openai's LengthFinishReasonError (chat completions
# finish_reason "length"), a Responses reply with status "incomplete" for
# max_output_tokens surfaced as an error by a gateway or a client library,
# and relayed finish/stop reasons (Anthropic "max_tokens", Gemini
# "MAX_TOKENS"). NOT a request REJECTED over its max_tokens (a 400 such as
# "max_tokens must be greater than thinking.budget_tokens" generated
# nothing): those carry none of these markers and stay probe_invalid.
_BUDGET_EXHAUSTED_TYPES = frozenset({"LengthFinishReasonError"})
_BUDGET_EXHAUSTED_PATTERNS = (
    re.compile(r"length limit was reached"),
    re.compile(r"incomplete\W+(?:\w+\W+){0,6}?max_(?:output_)?tokens"),
    re.compile(r"finish_?reason\W{1,6}(?:length|max_tokens)\b"),
    re.compile(r"stop_?reason\W{1,6}max_tokens\b"),
)
# Exception types that, with no HTTP status anywhere in the chain, are our own
# fault (a programming error), never a provider's answer.
_PROGRAMMING_ERROR_TYPES: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    KeyError,
    IndexError,
    AssertionError,
    NotImplementedError,
    ImportError,
    RecursionError,
)


def _now() -> datetime:
    return utc_now()


def _spawn(target: Callable[[], None]) -> None:
    """Run a background check. A seam tests replace to run checks inline."""
    threading.Thread(target=target, name="nymeria-llm-reclaim", daemon=True).start()


@dataclass
class _RouteState:
    failures: int = 0
    next_due: Optional[datetime] = None
    last_verdict: Optional[str] = None
    # When the probe behind ``last_verdict`` finished, and when it was SENT
    # (freshness: a verdict is about a hold only when its probe was sent at
    # or after that hold formed; one sent earlier predates the failure).
    verdict_at: Optional[datetime] = None
    verdict_sent_at: Optional[datetime] = None
    reclaimed_at: Optional[datetime] = None
    flap_hold: Optional[tuple] = None
    # The one probe in flight: when it was sent and the token that owns it.
    inflight_since: Optional[datetime] = None
    inflight_token: Optional[object] = None
    invalid_warned: bool = False


@dataclass
class _ThreadState:
    hold_key: tuple
    # The configured-route fingerprint this state is about: a different one
    # (the global model or base URL moved, a thread route pin changed) starts
    # a fresh state, so a verdict about another route never applies.
    identity: str
    route_key: Optional[tuple] = None
    # This thread's background check in flight: since when, and its token.
    check_since: Optional[datetime] = None
    check_token: Optional[object] = None
    # After a check that could not resolve the route (a resolver fault).
    retry_at: Optional[datetime] = None
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
    """The wait after the ``failures``-th consecutive failure (counted
    including the one just recorded): ``interval * 2**failures`` capped,
    so 2x then 4x the interval; cap verdicts wait the cap at once."""
    cap = backoff_cap_seconds(interval)
    if verdict in _CAP_VERDICTS:
        return cap
    return min(cap, int(interval) * (2**failures))


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


def hold_action(llm_config: Any, hold: Any, settings: Any) -> str:
    """"end" or "offer" for this hold when its primary answers again: the
    consent layer's ``reclaim_action`` over the hold's recorded origin, its
    permanence and the thread's EFFECTIVE switch mode (thread override else
    global; only a hold with no recorded origin reads the mode)."""
    from .agent_llm_config import _resolve_thread_llm_override
    from .fallback_approvals import reclaim_action

    switch_mode = _resolve_thread_llm_override(
        getattr(llm_config, "fallback_switch_mode", None) if llm_config is not None else None,
        getattr(settings, "llm_fallback_switch_mode", "auto"),
    )
    return reclaim_action(
        switch_mode=switch_mode,
        permanent=hold.expires_at is None,
        origin=getattr(hold, "hold_origin", None),
    )


# -- the probe -----------------------------------------------------------------


def probe_config(primary: Any) -> Any:
    """The probe's LLMConfig, derived from the resolver's config for the
    primary (never assembled by hand from thread fields): a tiny answer,
    thinking off (or the model's floor where it cannot be off), a bounded
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


def output_budget_exhausted(exc: BaseException) -> bool:
    """True when ``exc`` says only that the probe's output budget ran out
    (the model generated until the cap), in any provider family's shape."""
    from ..vendor.react_agent.nodes import llm_error_text

    chain: list[BaseException] = []
    current: Optional[BaseException] = exc
    while current is not None and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(type(item).__name__ in _BUDGET_EXHAUSTED_TYPES for item in chain):
        return True
    text = llm_error_text(exc)
    return any(pattern.search(text) for pattern in _BUDGET_EXHAUSTED_PATTERNS)


def classify_probe_failure(exc: BaseException) -> str:
    """A probe exception's verdict, in the hold reasons' own taxonomy.

    An exhausted output budget is HEALTHY (the route generated). 400/422 is
    ``probe_invalid``: the route answered, but rejected the probe's shape,
    which says nothing about recovery (it stops probing that hold). A
    programming error with no HTTP status is ``probe_error`` (ours, never
    the provider's). Everything else is the retry ladder's reason mapping:
    ``rate_limited`` and ``auth_error`` back off to the cap, the rest (5xx,
    408 to 425, timeouts, transport, unknown) double."""
    from ..vendor.react_agent.nodes import llm_error_status_code, llm_failure_reason

    if output_budget_exhausted(exc):
        return HEALTHY
    status = llm_error_status_code(exc)
    if status in (400, 422):
        return PROBE_INVALID
    if status is None and isinstance(exc, _PROGRAMMING_ERROR_TYPES):
        return PROBE_ERROR
    return llm_failure_reason(exc)


def run_probe(primary: Any) -> str:
    """One probe request against ``primary``; never raises. Any return (text,
    empty, truncated, even a refusal) means the route serves completions.
    A failure to BUILD the client is always ``probe_error`` (nothing was
    sent); a ``probe_error`` is logged with its traceback, since it is a
    bug or a misconfiguration to fix, not an outage to wait out."""
    from langchain_core.messages import HumanMessage

    from ..vendor.react_agent.providers import create_llm

    try:
        llm = create_llm(probe_config(primary))
    except Exception:  # noqa: BLE001 - nothing was sent: never a provider verdict
        _log_probe_error(primary, "building the probe client failed")
        return PROBE_ERROR
    try:
        llm.invoke([HumanMessage(content=PROBE_PROMPT)])
    except Exception as exc:  # noqa: BLE001 - the verdict IS the exception class
        verdict = classify_probe_failure(exc)
        if verdict == PROBE_ERROR:
            _log_probe_error(primary, "the probe call raised a programming error")
        return verdict
    return HEALTHY


def _log_probe_error(primary: Any, what: str) -> None:
    logger.warning(
        "[LLM RECLAIM] route=%s verdict=%s (%s; not a provider verdict, backing "
        "off to the cap)",
        _route_label(route_key(primary)),
        PROBE_ERROR,
        what,
        exc_info=True,
    )


# -- route reads (callers hold _LOCK) ------------------------------------------


def _stale(since: Optional[datetime], now: datetime) -> bool:
    return since is not None and (now - since).total_seconds() > _INFLIGHT_STALE_SECONDS


def _route_inflight(route: _RouteState, now: datetime) -> bool:
    return route.inflight_since is not None and not _stale(route.inflight_since, now)


def _check_running(state: _ThreadState, now: datetime) -> bool:
    return state.check_since is not None and not _stale(state.check_since, now)


def _fresh_verdict(route: Optional[_RouteState], hold: Any) -> Optional[str]:
    """The route's last verdict when its evidence was gathered (the probe
    SENT) at or after ``hold`` formed; None otherwise. A probe sent before
    the hold's failure proves nothing about it, even if it finished after."""
    if route is None or route.last_verdict is None or route.verdict_sent_at is None:
        return None
    if route.verdict_sent_at < ensure_aware_utc(hold.activated_at):
        return None
    return route.last_verdict


def _ready(route: Optional[_RouteState], hold: Any, now: datetime, cap: timedelta) -> bool:
    """A healthy verdict this hold may act on: fresh for the hold and no
    older than the cap."""
    return (
        route is not None
        and _fresh_verdict(route, hold) == HEALTHY
        and route.verdict_at is not None
        and now - route.verdict_at <= cap
    )


def _not_due(route: _RouteState, now: datetime) -> bool:
    return route.next_due is not None and now < route.next_due


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


def _thread_state(thread_id: str, key: tuple, fingerprint: str) -> _ThreadState:
    """This thread's state for THIS hold under THIS configured route (caller
    holds _LOCK); anything else starts fresh."""
    state = _THREADS.get(thread_id)
    if state is None or state.hold_key != key or state.identity != fingerprint:
        state = _ThreadState(hold_key=key, identity=fingerprint)
        _THREADS[thread_id] = state
    return state


def settle_hold_at_turn_start(
    host: Any,
    thread_id: str,
    user_id: str,
    *,
    offers: bool = True,
    resume: bool = False,
) -> Optional[dict[str, Any]]:
    """The turn-start seam, called by the lock holder before the graph
    lookup (never from the graph lookup itself: a mid-turn rebuild must not
    switch models).

    Evicts an expired hold; applies a healthy verdict a previous turn's probe
    recorded (returns the ``fallback_hold_reclaimed`` event for the caller to
    stream); otherwise, when the hold is eligible and due, starts ONE
    background check and returns None (the turn runs the hold exactly as
    before). ``offers`` says whether THIS turn can show an offer
    (``fallback_approvals.reclaim_offer_renders``; False for the sync
    ``chat()`` turn): a hold whose action is an offer is neither probed nor
    offered on a turn that cannot show it. ``resume`` (a ``/resume``
    re-drive) only evicts an expired hold: there is no prompt for a note,
    which then rides the next prompted turn. Never raises: any fault leaves
    the turn on its hold.
    """
    if not thread_id:
        return None
    manager = getattr(host, "thread_config_manager", None)
    if manager is None:
        return None
    try:
        return _settle(host, manager, thread_id, user_id, offers=offers, resume=resume)
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
    resume: bool,
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
    if resume:
        return None
    settings = getattr(host, "settings", None)
    interval = reclaim_interval_seconds(settings)
    if not _eligible(hold, interval, now):
        return None
    llm_config = getattr(tc, "llm_config", None)
    action = hold_action(llm_config, hold, settings)
    if action == "offer" and not offers:
        # This turn cannot show an offer: neither probe for one (a verdict
        # nobody can see only goes stale) nor spend it. It stays pending for
        # a turn that can show it.
        return None
    key = hold_key(hold)
    fingerprint = _configured_fingerprint(llm_config, settings)
    cap = timedelta(seconds=backoff_cap_seconds(interval))
    ready = False
    token = object()
    with _LOCK:
        state = _thread_state(thread_id, key, fingerprint)
        if state.stopped or _check_running(state, now):
            return None
        route = _ROUTES.get(state.route_key) if state.route_key is not None else None
        if route is not None:
            if _fresh_verdict(route, hold) == PROBE_INVALID:
                state.stopped = True
                return None
            if _ready(route, hold, now, cap):
                ready = True
            elif _route_inflight(route, now) or _not_due(route, now):
                # Another thread's probe is in flight, or the route is backing
                # off: read its verdict at a later turn start.
                return None
        if not ready:
            if state.retry_at is not None and now < state.retry_at:
                return None
            state.check_since = now
            state.check_token = token
    if ready:
        return _apply_healthy(host, manager, thread_id, hold, state, llm_config, settings, action)
    _spawn(lambda: _check_thread(host, thread_id, user_id, hold, key, token))
    return None


def _apply_healthy(
    host: Any,
    manager: Any,
    thread_id: str,
    hold: Any,
    state: _ThreadState,
    llm_config: Any,
    settings: Any,
    action: str,
) -> Optional[dict[str, Any]]:
    from .agent_llm_config import clear_active_llm_fallback, configured_provider_model

    permanent = hold.expires_at is None
    to_provider, to_model = configured_provider_model(llm_config, settings)
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
    if not _stamp_offer(manager, thread_id, hold.activated_at):
        # A revert (or a newer hold) landed since the read: it wins.
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
        "permanent" if permanent else f"origin {getattr(hold, 'hold_origin', None) or 'unrecorded'}",
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


def _apply_flap(route: _RouteState, hold: Any, key: tuple, cap: int, now: datetime) -> bool:
    if route.reclaimed_at is None or route.flap_hold == key:
        return False
    activated = ensure_aware_utc(hold.activated_at)
    gap = activated - route.reclaimed_at
    if not (timedelta(0) <= gap <= _FLAP_WINDOW):
        return False
    route.flap_hold = key
    start = activated + timedelta(seconds=cap)
    route.next_due = max(route.next_due or start, start)
    # The evidence is the hold re-forming itself, so it is "sent" then: it
    # replaces the healthy verdict that proved wrong.
    route.last_verdict = FLAPPED
    route.verdict_at = now
    route.verdict_sent_at = activated
    return True


def _update_route(
    route: _RouteState, verdict: str, sent: datetime, done: datetime, interval: int
) -> None:
    if verdict == HEALTHY:
        route.failures = 0
        route.next_due = done + timedelta(seconds=interval)
    else:
        route.failures += 1
        route.next_due = done + timedelta(
            seconds=_backoff_seconds(interval, route.failures, verdict)
        )
    route.last_verdict = verdict
    route.verdict_at = done
    route.verdict_sent_at = sent


def _check_thread(host: Any, thread_id: str, user_id: str, hold: Any, key: tuple, token: object) -> None:
    """Background body: resolve the configured primary (hold ignored, a pure
    read), record the thread's route, and probe it when the route is due and
    no probe is in flight. A route that is not due, already has a usable
    verdict, or has a probe in flight is left alone: the thread's next turn
    start reads the route."""
    from .agent_llm_config import get_llm_config_for_thread

    interval = reclaim_interval_seconds(getattr(host, "settings", None))
    failed = False
    try:
        if interval <= 0:
            return
        cap = backoff_cap_seconds(interval)
        cfg = get_llm_config_for_thread(
            host, thread_id, acting_user_id=user_id or None, ignore_active_fallback=True
        )
        rkey = route_key(cfg)
        label = _route_label(rkey)
        sent = _now()
        probe_token: Optional[object] = None
        with _LOCK:
            state = _THREADS.get(thread_id)
            if state is not None and state.check_token is token:
                state.route_key = rkey
            route = _ROUTES.setdefault(rkey, _RouteState())
            if _apply_flap(route, hold, key, cap, sent):
                logger.info(
                    "[LLM RECLAIM] thread=%s route=%s verdict=flapped next_due=%s "
                    "(a hold re-formed within 30 minutes of a reclaim)",
                    thread_id,
                    label,
                    route.next_due.isoformat() if route.next_due else "",
                )
            if not (
                _ready(route, hold, sent, timedelta(seconds=cap))
                or _route_inflight(route, sent)
                or _not_due(route, sent)
            ):
                probe_token = object()
                route.inflight_since = sent
                route.inflight_token = probe_token
        if probe_token is None:
            return
        verdict = run_probe(cfg)
        done = _now()
        warn_invalid = False
        with _LOCK:
            if route.inflight_token is probe_token:
                _update_route(route, verdict, sent, done, interval)
                route.inflight_since = None
                route.inflight_token = None
            next_due = route.next_due
            if verdict == PROBE_INVALID and not route.invalid_warned:
                route.invalid_warned = True
                warn_invalid = True
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
    except Exception:  # noqa: BLE001 - a background check must never escape
        failed = True
        logger.warning(
            "[LLM RECLAIM] thread=%s check failed; next check after one interval",
            thread_id,
            exc_info=True,
        )
    finally:
        with _LOCK:
            state = _THREADS.get(thread_id)
            if state is not None and state.check_token is token:
                state.check_since = None
                state.check_token = None
                if failed:
                    state.retry_at = _now() + timedelta(seconds=max(interval, 1))


# -- status (``/fallback status``, ``GET /threads/{id}/config``) ---------------


def _iso(value: Optional[datetime]) -> Optional[str]:
    return ensure_aware_utc(value).isoformat() if value is not None else None


def reclaim_status(
    thread_id: str, hold: Any, settings: Any, llm_config: Any = None
) -> Optional[dict[str, Any]]:
    """What the reclaim knows about ``thread_id``'s hold, for display.

    ``llm_config`` is the thread's ``ThreadConfig.llm_config`` (the action
    and the configured-route fingerprint depend on it). ``action`` is what a
    healthy primary does: ``end`` or ``offer``. ``state``: ``offered`` (the
    primary answered while the hold stays; the revert was offered), ``off``
    (interval 0), ``excluded`` (a refusal or invalid-request hold),
    ``expired`` (ends at the next turn start), ``stopped`` (the probe was
    rejected as invalid), ``checking`` (a probe is in flight),
    ``recovered`` (the primary answered; the hold ends at the next turn
    start), ``offer_pending`` (the primary answered; the one offer waits for
    a turn that can show it) or ``scheduled`` (``next_check_at`` is the
    earliest turn start that checks, the route's backoff included).
    Computed in the API process, so both command client shapes read it
    through the thread-config payload.
    """
    if hold is None:
        return None
    interval = reclaim_interval_seconds(settings)
    now = _now()
    action = hold_action(llm_config, hold, settings)
    status: dict[str, Any] = {
        "state": "scheduled",
        "action": action,
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
    fingerprint = _configured_fingerprint(llm_config, settings)
    with _LOCK:
        state = _THREADS.get(thread_id)
        if state is not None and (
            state.hold_key != hold_key(hold) or state.identity != fingerprint
        ):
            state = None
        snapshot = dataclasses.replace(state) if state is not None else None
        live = (
            _ROUTES.get(snapshot.route_key)
            if snapshot is not None and snapshot.route_key is not None
            else None
        )
        route = dataclasses.replace(live) if live is not None else None
    cap = timedelta(seconds=backoff_cap_seconds(interval))
    verdict = _fresh_verdict(route, hold)
    if verdict is not None and route is not None:
        status["last_verdict"] = verdict
        status["last_checked_at"] = _iso(route.verdict_at)
    if snapshot is not None and (snapshot.stopped or verdict == PROBE_INVALID):
        status["state"] = "stopped"
        return status
    if (snapshot is not None and _check_running(snapshot, now)) or (
        route is not None and _route_inflight(route, now)
    ):
        status["state"] = "checking"
        return status
    if _ready(route, hold, now, cap):
        status["state"] = "offer_pending" if action == "offer" else "recovered"
        return status
    next_check = ensure_aware_utc(hold.activated_at) + timedelta(seconds=interval)
    if route is not None and route.next_due is not None:
        next_check = max(next_check, route.next_due)
    if snapshot is not None and snapshot.retry_at is not None:
        next_check = max(next_check, snapshot.retry_at)
    status["next_check_at"] = _iso(next_check)
    return status


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "EVENT_TYPE",
    "EXCLUDED_REASONS",
    "HEALTHY",
    "PROBE_ERROR",
    "PROBE_INVALID",
    "PROBE_PROMPT",
    "PROBE_TIMEOUT_SECONDS",
    "backoff_cap_seconds",
    "classify_probe_failure",
    "hold_action",
    "hold_key",
    "output_budget_exhausted",
    "probe_config",
    "reclaim_interval_seconds",
    "reclaim_status",
    "reset_reclaim_state_for_tests",
    "route_key",
    "run_probe",
    "settle_hold_at_turn_start",
]
