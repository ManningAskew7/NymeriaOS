"""Detach-and-deliver: hand a finished background result to its origin thread.

Three producers outlive the tool call that started them and must deliver their
result to the thread that asked, later: background bash jobs
(``tools/bash_background.py``), detached Claude Code runs
(``tools/claude_code_background.py``), and thread requests
(``core/thread_requests.py``: another thread's ``reply_to_thread`` answer to a
request nobody is waiting on inline, plus the no-reply nudge and expiry
notices). They share one choreography, which this module owns so it cannot
drift between them:

- Guard: the origin thread must still be owned by the user the result was
  produced for (fail closed on a lookup error: this is an auth boundary, the
  same rule as the callable ownership gate). A set abort flag on the origin
  thread drops the delivery only when the producer asked for that
  (``drop_on_abort``): the background tools do (a user who just stopped a
  thread does not want it waking itself up), thread replies do not (the
  callee did real work on the caller's behalf, and the wake-up turn itself
  clears the stale flag at start).
- Route: if the origin thread is busy, enqueue a pending prompt that the
  running turn absorbs at its next sub-turn boundary; if the queue is closing
  (the holder is releasing) or the thread is idle, fire an autonomous turn.
- Turn: ``AutonomousTurnEmitter`` bookends (``task_started`` guaranteed before
  ``task_completed``), the producer's extra payload fields, ``partial`` on an
  iteration limit, an activity-log entry, and an error bookend plus
  ``TASK_FAILED`` activity when the turn raises.

A producer that must not lose its result (``drop_on_abort=False``) also
follows a QUEUED delivery through: every enqueued prompt is woken exactly
once (absorbed by the holder, or abandoned/restored/evicted/inject-failed by
a stop, an abort, or a drain failure), so ``submit_completion`` waits for
that outcome and re-delivers when the prompt never reached the model. The
background tools return as soon as the prompt is queued, as before.

``deliver_without_turn`` is the model-free sibling of ``fire_autonomous_turn``
for a producer whose result is ALREADY the text the user should read (the
user's ``/code`` command relaying Claude Code's answer): it holds the thread
as a short holder turn (``hold_thread``: the lock, holder metadata, the
queue's release window), lets the caller record the exchange into history
under that hold, feeds the turn stream buffer so bots and the desktop attach
to THIS turn rather than the previous one, and publishes the same bookends.
No model runs, so it works while the agent's LLM path is broken.

``InlineLatch`` is the one-way inline-or-detached decision every bounded
inline wait shares (detached Claude Code runs, callable-ask continuations):
the caller claims inline only if the run finished WITHIN its budget, the
producer claims detached after a grace window, and whichever side settles
first wins, so a run finishing just after the budget is delivered once.

The event-bus, stream-bridge, and activity-log symbols are imported
function-locally so tests that monkeypatch them on their source modules keep
working (the convention the two tool modules already follow).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

RESULT_DROPPED = "dropped"
RESULT_QUEUED = "queued"
RESULT_FIRED = "fired"
# ``deliver_without_turn`` only: a live turn kept the thread lock past the
# caller's wait, so nothing was delivered (the caller picks a fallback).
RESULT_BUSY = "busy"

# A queued must-deliver result that keeps being cleared before absorption is
# retried this many times (with a short backoff) before it is given up on.
QUEUED_REDELIVERY_ATTEMPTS = 8


@dataclass
class InlineLatch:
    """One-way inline-or-detached decision for a bounded inline wait.

    ``done`` is set by the producer when the result exists; ``inline_decided``
    is set by the caller once it has claimed either way. ``wait_inline`` claims
    inline only if the run finished within the budget (not merely "is done
    now"), so a budget that overshoots the runtime's kill timeout can never
    resurrect an inline claim after the caller moved on; ``settle`` is the
    producer's claim after its grace window.
    """

    done: threading.Event = field(default_factory=threading.Event)
    inline_decided: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _resolution: Optional[str] = None  # "inline" | "detached"

    def wait_inline(self, budget: float) -> str:
        """Caller side: wait up to ``budget``; returns "inline" or "detached"."""
        finished_in_time = self.done.wait(budget) if budget > 0 else self.done.is_set()
        with self._lock:
            if self._resolution is None:
                self._resolution = "inline" if finished_in_time else "detached"
            decision = self._resolution
        self.inline_decided.set()
        return decision

    def detach(self) -> str:
        """Caller side, without waiting: claim detached because nobody will
        render inline (a queued follow-up has no caller). Must be called
        BEFORE the producer can finish: ``wait_inline(0)`` claims inline
        when the result is already there, which for a run nobody waits on
        means a report with no renderer."""
        with self._lock:
            if self._resolution is None:
                self._resolution = "detached"
            decision = self._resolution
        self.inline_decided.set()
        return decision

    def settle(self, grace: float) -> str:
        """Producer side, after ``done``: wait ``grace`` for the caller's claim,
        else claim detached (the caller is gone). Returns the resolution."""
        self.inline_decided.wait(timeout=grace)
        with self._lock:
            if self._resolution is None:
                self._resolution = "detached"
            return self._resolution

    @property
    def resolution(self) -> Optional[str]:
        return self._resolution


@dataclass
class CompletionDelivery:
    """Everything needed to wake ``thread_id`` with ``prompt_text``.

    ``source`` is the turn-source key (``prompts.SOURCE_TRIGGER_LABELS``), so
    the ``[Trigger:]`` header names the producer whether the prompt is
    absorbed mid-turn or starts a turn of its own; ``trigger_override`` wins
    over that label on the autonomous-turn path when set. ``started_data`` and
    ``completed_data`` are the producer's extra fields on the two lifecycle
    events (``prompt``/``source`` and ``content``/``source`` are added here).
    ``label`` names the producer in log lines and the error bookend.
    """

    thread_id: str
    user_id: str
    prompt_text: str
    source: str
    source_id: str
    source_label: str
    task_id: str
    label: str
    trigger_override: Optional[str] = None
    started_data: Dict[str, Any] = field(default_factory=dict)
    completed_data: Dict[str, Any] = field(default_factory=dict)
    activity_message: str = ""
    activity_metadata: Dict[str, Any] = field(default_factory=dict)
    # Activity type logged when the turn completes; None means TASK_COMPLETED.
    # Producers whose underlying job failed pass TASK_FAILED so the ledger
    # records the job outcome, not the notification's.
    activity_type: Optional[Any] = None
    drop_on_abort: bool = True


def drop_reason(agent: Any, delivery: CompletionDelivery) -> Optional[str]:
    """Why this delivery must not reach its thread, or None to proceed.

    Owner mismatch and owner-lookup failure always drop (fail closed). The
    abort flag drops only under ``drop_on_abort``; a thread whose abort flag
    is set has been stopped and has not started a turn since (``astream``
    clears the flag at turn start).
    """
    if delivery.drop_on_abort:
        try:
            abort_event = agent._thread_locks.get_abort_event(delivery.thread_id)
            if abort_event.is_set():
                logger.info(
                    "Thread %s aborted; dropping %s completion %s",
                    delivery.thread_id,
                    delivery.label,
                    delivery.source_id,
                )
                return "aborted"
        except Exception as exc:  # noqa: BLE001 - guard must never raise
            logger.warning(
                "Failed to check abort state for %s completion %s: %s",
                delivery.label,
                delivery.source_id,
                exc,
            )

    repo = getattr(agent, "accounts_repo", None)
    get_owner = getattr(repo, "get_thread_owner", None)
    if callable(get_owner):
        try:
            owner = get_owner(delivery.thread_id)
        except Exception as exc:  # noqa: BLE001 - fail closed
            logger.warning(
                "Failed to verify owner for %s completion %s: %s",
                delivery.label,
                delivery.source_id,
                exc,
            )
            return "owner_lookup_failed"
        if owner != delivery.user_id:
            logger.info(
                "Thread owner mismatch for %s completion %s: thread=%s owner=%s "
                "record_user=%s; dropping completion",
                delivery.label,
                delivery.source_id,
                delivery.thread_id,
                owner,
                delivery.user_id,
            )
            return "owner_mismatch"
    return None


def submit_completion(
    agent: Any,
    delivery: CompletionDelivery,
    *,
    fire: Optional[Callable[[], None]] = None,
) -> str:
    """Route the delivery: drop, enqueue behind a busy turn, or fire a turn.

    ``fire`` defaults to :func:`fire_autonomous_turn`; producers pass their own
    module-level wrapper so the seam their tests patch stays theirs. Returns
    one of ``RESULT_DROPPED`` / ``RESULT_QUEUED`` / ``RESULT_FIRED``.

    For a ``drop_on_abort=False`` delivery, QUEUED means the prompt was
    absorbed by the running turn: the call blocks until the queue reports the
    outcome and re-delivers (as a wake-up turn once the thread is free) when a
    stop, an abort, or a drain failure cleared the prompt first. Call it from
    a thread that may wait.
    """
    return _submit(agent, delivery, fire, attempt=0)


def _submit(
    agent: Any,
    delivery: CompletionDelivery,
    fire: Optional[Callable[[], None]],
    *,
    attempt: int,
) -> str:
    from .pending_prompt_queue import (
        PendingPromptQueueClosingError,
        get_pending_queue,
        make_pending_prompt,
    )

    if drop_reason(agent, delivery) is not None:
        return RESULT_DROPPED

    thread_locks = agent._thread_locks
    if thread_locks.is_thread_busy(delivery.thread_id):
        pending = make_pending_prompt(
            message=delivery.prompt_text,
            source=delivery.source,
            source_id=delivery.source_id,
            source_label=delivery.source_label,
            user_id=delivery.user_id,
            is_autonomous=True,
            fanout_mailbox=None,
            consumer_loop=None,
        )
        try:
            get_pending_queue().enqueue(delivery.thread_id, pending)
            logger.info(
                "Queued %s completion prompt %s on thread %s",
                delivery.label,
                delivery.source_id,
                delivery.thread_id,
            )
            if delivery.drop_on_abort:
                return RESULT_QUEUED
            return _follow_queued_outcome(agent, delivery, pending, fire, attempt)
        except PendingPromptQueueClosingError:
            logger.info(
                "Thread %s releasing; firing %s completion %s as next turn",
                delivery.thread_id,
                delivery.label,
                delivery.source_id,
            )
        if drop_reason(agent, delivery) is not None:
            return RESULT_DROPPED

    if fire is None:
        fire_autonomous_turn(agent, delivery)
    else:
        fire()
    return RESULT_FIRED


def _follow_queued_outcome(
    agent: Any,
    delivery: CompletionDelivery,
    pending: Any,
    fire: Optional[Callable[[], None]],
    attempt: int,
) -> str:
    """Wait for a queued must-deliver prompt's outcome; re-deliver if lost.

    Every enqueued prompt is woken exactly once (``notify_batch_absorbed`` on
    the success path; ``_wake_prompt`` with a flag or error code on every
    other), so this wait cannot strand. A user stop restores only user-source
    prompts and discards the rest, an abort cascade clears the queue, and an
    inject failure abandons the batch: in each case the model never saw the
    result, so it goes around again (the thread is idle or releasing by then,
    so it fires as a wake-up turn; if a new turn grabbed the lock first it
    queues behind that one).
    """
    pending.notify_event.wait()
    lost = bool(
        getattr(pending, "abandoned", False)
        or getattr(pending, "restored", False)
        or getattr(pending, "error_code", None)
    )
    if not lost:
        return RESULT_QUEUED
    if attempt >= QUEUED_REDELIVERY_ATTEMPTS:
        logger.warning(
            "%s completion %s was cleared %d times before absorption on thread %s; giving up",
            delivery.label,
            delivery.source_id,
            attempt + 1,
            delivery.thread_id,
        )
        return RESULT_DROPPED
    logger.info(
        "%s completion %s was cleared before absorption on thread %s (%s); re-delivering",
        delivery.label,
        delivery.source_id,
        delivery.thread_id,
        getattr(pending, "error_code", None) or "cleared",
    )
    # Let the stopped holder finish releasing its lock before going around.
    time.sleep(min(5.0, 0.25 * (2 ** attempt)))
    return _submit(agent, delivery, fire, attempt=attempt + 1)


def fire_autonomous_turn(agent: Any, delivery: CompletionDelivery) -> None:
    """Run the delivery as an autonomous turn on its thread, with SSE bookends."""
    if drop_reason(agent, delivery) is not None:
        return

    from .activity_log import ActivityType
    from .autonomous_turn import AutonomousTurnEmitter
    from .stream_bridge import stream_and_collect

    emitter = AutonomousTurnEmitter(
        thread_id=delivery.thread_id,
        user_id=delivery.user_id,
        task_id=delivery.task_id,
        started_data={
            "prompt": delivery.prompt_text,
            "source": delivery.source,
            **delivery.started_data,
        },
    )

    def stream_error_message(chunk: dict) -> str:
        error_content = chunk.get("content", "")
        error_code = chunk.get("code", "unknown")
        return error_content or f"{delivery.label} stream error (code={error_code})"

    astream_kwargs: Dict[str, Any] = {
        "message": delivery.prompt_text,
        "thread_id": delivery.thread_id,
        "user_id": delivery.user_id,
        "_is_self_invoke": True,
        "source": delivery.source,
        "source_id": delivery.source_id,
        "source_label": delivery.source_label,
    }
    if delivery.trigger_override:
        astream_kwargs["_trigger_override"] = delivery.trigger_override

    try:
        result = stream_and_collect(
            agent,
            astream_kwargs=astream_kwargs,
            on_chunk=emitter.handle_chunk,
            error_message_factory=stream_error_message,
        )
        emitter.publish_started()
        response = result.response_text(fallback_to_thinking=True)
        completed: Dict[str, Any] = {
            "content": response,
            "source": delivery.source,
            **delivery.completed_data,
        }
        if result.iteration_limit_hit:
            completed["partial"] = True
        emitter.publish_completed(completed)
        log_delivery_activity(
            delivery.activity_type or ActivityType.TASK_COMPLETED,
            delivery.activity_message or f"{delivery.label} {delivery.source_id} completed",
            delivery,
            metadata={**delivery.activity_metadata, "partial": result.iteration_limit_hit},
        )
    except Exception as exc:  # noqa: BLE001 - a wake-up must never die silently
        safe_error = str(exc)[:300] or "Unknown error"
        logger.exception(
            "%s autonomous turn failed %s thread=%s",
            delivery.label,
            delivery.source_id,
            delivery.thread_id,
        )
        emitter.publish_started()
        emitter.publish_completed(
            {
                "content": f"{delivery.label} {delivery.source_id} notification failed: {safe_error}",
                "source": delivery.source,
                **delivery.completed_data,
                "status": "error",
                "error": safe_error,
                "error_message": safe_error,
            }
        )
        log_delivery_activity(
            ActivityType.TASK_FAILED,
            f"{delivery.label} {delivery.source_id} notification failed: {safe_error[:120]}",
            delivery,
            metadata={**delivery.activity_metadata, "error": safe_error},
        )


@contextmanager
def hold_thread(
    agent: Any,
    thread_id: str,
    *,
    holder: str,
    task_id: str,
    timeout: float,
) -> Iterator[bool]:
    """Hold ``thread_id`` as a short model-free holder turn.

    Mirrors what a real turn does around the lock, so the rest of the
    platform sees an ordinary holder: the lock is taken (waiting up to
    ``timeout`` for a live turn to end), the pending queue's release window
    is closed so a contender arriving now blocks on the lock and runs as the
    NEXT turn (``astream``'s ``is_releasing`` arm) instead of queueing behind
    a holder with no sub-turn boundary to absorb it, anything that queued in
    the gap is handed back the way a stop does, holder metadata is set (so
    ``/stop`` and the status route see the hold), and the abort flag is
    cleared as a turn start would. Yields True while held; yields False,
    holding nothing, when the lock stayed taken past ``timeout``.
    """
    from .pending_prompt_queue import get_pending_queue

    locks = agent._thread_locks
    lock = locks.get_lock(thread_id)
    if not lock.acquire(timeout=timeout):
        yield False
        return
    backend = get_pending_queue()
    try:
        backend.begin_release(thread_id)
        restored, discarded = backend.clear_with_restore(thread_id)
        if restored or discarded:
            logger.info(
                "Thread %s: %d user prompt(s) restored and %d dropped from the "
                "queue while a model-free holder took the thread",
                thread_id,
                len(restored),
                discarded,
            )
        locks.set_lock_info(thread_id, holder=holder, task_id=task_id)
        locks.clear_abort(thread_id)
        yield True
    finally:
        locks.clear_lock_info(thread_id)
        lock.release()
        backend.end_release(thread_id)


def deliver_without_turn(
    agent: Any,
    delivery: CompletionDelivery,
    content: str,
    *,
    record: Optional[Callable[[], Optional[str]]] = None,
    timeout: float,
) -> str:
    """Deliver ready-made ``content`` to the thread as a model-free holder turn.

    ``record`` runs under the hold before the turn opens (the producer's
    history write) and returns the anchor message id for live-attach
    viewers, or None. The turn stream buffer opens BEFORE ``task_started``
    is published: bots attach on that event to the thread's current buffer,
    so publishing first would hand them the previous turn's retained buffer
    and swallow this result. Returns ``RESULT_DROPPED`` (guard),
    ``RESULT_BUSY`` (lock held past ``timeout``, nothing delivered), or
    ``RESULT_FIRED``.
    """
    if drop_reason(agent, delivery) is not None:
        return RESULT_DROPPED

    from .activity_log import ActivityType
    from .autonomous_turn import AutonomousTurnEmitter
    from .stream_bridge import begin_holder_turn_tee

    with hold_thread(
        agent,
        delivery.thread_id,
        holder=delivery.source,
        task_id=delivery.task_id,
        timeout=timeout,
    ) as held:
        if not held:
            return RESULT_BUSY
        anchor: Optional[str] = None
        if record is not None:
            try:
                anchor = record()
            except Exception:  # noqa: BLE001 - the record is not the delivery
                logger.exception(
                    "%s %s: history record failed", delivery.label, delivery.source_id
                )
        tee = begin_holder_turn_tee(
            agent,
            thread_id=delivery.thread_id,
            user_id=delivery.user_id,
            source=delivery.source,
            source_label=delivery.source_label,
            user_message_id=anchor,
        )
        emitter = AutonomousTurnEmitter(
            thread_id=delivery.thread_id,
            user_id=delivery.user_id,
            task_id=delivery.task_id,
            started_data={
                "prompt": delivery.prompt_text,
                "source": delivery.source,
                **delivery.started_data,
            },
        )
        try:
            chunk = {"type": "response", "content": content}
            tee.record(chunk)
            emitter.handle_chunk(chunk)
            tee.finish_done()
            emitter.publish_completed(
                {"content": content, "source": delivery.source, **delivery.completed_data}
            )
        finally:
            tee.finish_aborted_if_live()

    log_delivery_activity(
        delivery.activity_type or ActivityType.TASK_COMPLETED,
        delivery.activity_message or f"{delivery.label} {delivery.source_id} completed",
        delivery,
        metadata=dict(delivery.activity_metadata),
    )
    return RESULT_FIRED


def log_delivery_activity(
    activity_type: Any,
    message: str,
    delivery: CompletionDelivery,
    *,
    metadata: Dict[str, Any],
) -> None:
    try:
        from .activity_log import log_activity

        log_activity(
            activity_type,
            message,
            user_id=delivery.user_id,
            thread_id=delivery.thread_id,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 - the ledger is best-effort
        logger.warning("Failed to log %s activity: %s", delivery.label, exc)


__all__ = [
    "CompletionDelivery",
    "InlineLatch",
    "QUEUED_REDELIVERY_ATTEMPTS",
    "RESULT_DROPPED",
    "RESULT_FIRED",
    "RESULT_QUEUED",
    "drop_reason",
    "fire_autonomous_turn",
    "submit_completion",
]
