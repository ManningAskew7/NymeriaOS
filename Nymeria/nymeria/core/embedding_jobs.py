"""Serialize embedding work off event loops, with an observable background tail.

Submit whole indexing/flush operations so their SQLite work stays on the same
worker as inference. Sync callers can join this worker too; nested jobs execute
inline on it to avoid waiting on their own executor.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nymeria-embed")
_WORKER = threading.local()
_PENDING: set[_EmbeddingJob] = set()
_PENDING_LOCK = threading.Lock()


def pending_embedding_job_count() -> int:
    """Unsettled jobs, including index writes after a holder releases its lock."""
    with _PENDING_LOCK:
        return len(_PENDING)


def _execute(fn: Callable[..., _T], args: tuple, kwargs: dict, site: str, chunk_count: int) -> _T:
    started = time.monotonic()
    _WORKER.active = True
    try:
        return fn(*args, **kwargs)
    finally:
        _WORKER.active = False
        logger.info("Embedding job site=%s chunks=%d ms=%.1f", site, chunk_count,
                    (time.monotonic() - started) * 1000)


async def run_embedding_job(
    fn: Callable[..., _T], *args: Any, site: str, chunk_count: int = 0,
    thread_key: str | None = None, cancel_queued: bool = True, **kwargs: Any,
) -> _T:
    native = schedule_embedding_job(
        fn, *args, site=site, chunk_count=chunk_count, thread_key=thread_key, **kwargs,
    )
    wrapped = asyncio.wrap_future(native)
    try:
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        # Queued work can be cancelled outright. Running sync work cannot:
        # settle it before the caller releases its turn/maintenance lock.
        if cancel_queued and native.cancel():
            raise
        while not wrapped.done():
            try:
                await asyncio.shield(wrapped)
            except asyncio.CancelledError:
                continue
            except Exception:
                break  # native completion callback already reports the error
        if not wrapped.cancelled():
            wrapped.exception()
        raise


def run_embedding_job_sync(
    fn: Callable[..., _T], *args: Any, site: str, chunk_count: int = 0,
    thread_key: str | None = None, **kwargs: Any,
) -> _T:
    if getattr(_WORKER, "active", False):
        return fn(*args, **kwargs)
    return schedule_embedding_job(
        fn, *args, site=site, chunk_count=chunk_count, thread_key=thread_key, **kwargs,
    ).result()


@dataclass(eq=False)
class _EmbeddingJob:
    thread_key: str | None
    start_after: threading.Event | None = None
    lock: Any = field(default_factory=threading.Lock)
    cancelled: bool = False
    future: Future[Any] | None = None
    settled: Future[None] = field(default_factory=Future)

    def run(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
        if self.start_after is not None:
            self.start_after.wait()
        with self.lock:
            if not self.cancelled:
                return fn(*args, **kwargs)


def schedule_embedding_job(
    fn: Callable[..., Any], *args: Any, site: str, chunk_count: int = 0,
    thread_key: str | None = None, start_after: threading.Event | None = None, **kwargs: Any,
) -> Future[Any]:
    """Submit a tail job whose lifetime belongs to the worker, not its loop.

    Cancellation of an async observer cannot erase its completion or error.
    Thread deletion cancels queued writes and waits out a write already running.
    """
    job = _EmbeddingJob(thread_key, start_after)
    context = contextvars.copy_context()
    with _PENDING_LOCK:
        future = _EMBED_EXECUTOR.submit(
            context.run, _execute, job.run, (fn, args, kwargs), {}, site, chunk_count,
        )
        job.future = future
        _PENDING.add(job)

    def finished(done: Future[Any]) -> None:
        try:
            if not done.cancelled() and (error := done.exception()) is not None:
                logger.error("Embedding job failed at %s", site,
                             exc_info=(type(error), error, error.__traceback__))
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(job)
            job.settled.set_result(None)

    future.add_done_callback(finished)
    return future


def cancel_thread_embedding_jobs(thread_id: str) -> None:
    """Fence tail writes before deletion, called while holding the turn lock.

    A queued job is marked without waiting for unrelated threads' inference.
    A running job completes before deletion proceeds. Never call on an event loop.
    """
    with _PENDING_LOCK:
        jobs = [job for job in _PENDING if job.thread_key == thread_id]
    for job in jobs:
        with job.lock:
            job.cancelled = True


async def wait_for_pending_embedding_jobs() -> None:
    """Drain actual background work across loops without cancelling its futures."""
    while True:
        with _PENDING_LOCK:
            pending = [job.settled for job in _PENDING]
        if not pending:
            return
        # Settlement follows error reporting. These futures never raise, so a
        # cancelled observer cannot leave an unobserved inference exception.
        await asyncio.shield(asyncio.gather(
            *(asyncio.wrap_future(future) for future in pending),
            return_exceptions=True,
        ))
