"""Embedding worker lifetime, responsiveness, serialization and failure contracts.

Events coordinate blocked inference; timeouts are deadlock guards, not latency
assertions. Network behavior and model quality belong to EmbeddingClient tests.
"""

import asyncio
import logging
import threading

import pytest

from nymeria.core.embedding_jobs import (
    run_embedding_job,
    run_embedding_job_sync,
    schedule_embedding_job,
    wait_for_pending_embedding_jobs,
)


def test_worker_keeps_loop_responsive_and_serializes_sync_async_jobs():
    async def exercise():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        release = threading.Event()
        workers = []
        order = []

        def first():
            workers.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "event loop failed to release the embedding worker"
            order.append("first")

        def second():
            workers.append(threading.get_ident())
            order.append("second")
            return run_embedding_job_sync(lambda: "nested result", site="test.nested")

        job = asyncio.create_task(run_embedding_job(first, site="test.blocked"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # This continuation executes while inference is held. An inline
            # implementation cannot reach it and first's guard fails instead.
            assert not job.done()
            sync_job = asyncio.create_task(asyncio.to_thread(
                run_embedding_job_sync, second, site="test.sync",
            ))
            await asyncio.sleep(0)
        finally:
            release.set()
        await job
        assert await sync_job == "nested result"
        assert order == ["first", "second"]
        assert len(set(workers)) == 1
        assert workers[0] != loop_thread

    asyncio.run(exercise())


def test_cancelled_waiter_does_not_free_worker_while_inference_runs():
    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        order = []
        workers = []

        def blocked():
            workers.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            order.append("inference finished")

        def following():
            workers.append(threading.get_ident())
            order.append("next job")
            return "indexed"

        task = asyncio.create_task(run_embedding_job(blocked, site="test.cancel"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "running inference must settle before cancellation escapes"
            next_task = asyncio.create_task(run_embedding_job(following, site="test.following"))
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await next_task == "indexed"
        assert order == ["inference finished", "next job"]
        assert len(set(workers)) == 1

    asyncio.run(exercise())


def test_background_jobs_drain_and_report_failures(caplog):
    caplog.set_level(logging.ERROR, logger="nymeria.core.embedding_jobs")
    values = []

    def failed():
        raise ValueError("index write failed")

    async def exercise():
        schedule_embedding_job(values.append, "persisted", site="test.persist", chunk_count=1)
        schedule_embedding_job(failed, site="test.error")
        await wait_for_pending_embedding_jobs()
        assert values == ["persisted"]

    asyncio.run(exercise())
    assert "Embedding job failed at test.error" in caplog.text
    assert "index write failed" in caplog.text


def test_cancelled_drain_still_waits_for_actual_worker_and_logs_failure(caplog):
    caplog.set_level(logging.ERROR, logger="nymeria.core.embedding_jobs")
    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        def failed():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            raise ValueError("late write failed")

        schedule_embedding_job(failed, site="test.late_failure")
        try:
            await asyncio.wait_for(entered.wait(), 5)
            observer = asyncio.create_task(wait_for_pending_embedding_jobs())
            await asyncio.sleep(0)
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer
            drain = asyncio.create_task(wait_for_pending_embedding_jobs())
            await asyncio.sleep(0)
            assert not drain.done(), "cancelled observer must not remove a running write"
        finally:
            release.set()
        await drain

    asyncio.run(exercise())
    assert "Embedding job failed at test.late_failure" in caplog.text
    assert "late write failed" in caplog.text


@pytest.mark.parametrize("sync", [False, True])
def test_foreground_job_propagates_error_and_worker_recovers(sync):
    def failed():
        raise ValueError("invalid embedding input")

    async def exercise():
        with pytest.raises(ValueError, match="invalid embedding input"):
            if sync:
                await asyncio.to_thread(run_embedding_job_sync, failed, site="test.error.sync")
            else:
                await run_embedding_job(failed, site="test.error.async")
        assert await run_embedding_job(lambda: "next index completed", site="test.recovered") == "next index completed"

    asyncio.run(exercise())


@pytest.mark.parametrize("surface", ["compact", "auto_compact", "clear_api", "clear_command"])
def test_flush_surfaces_preserve_history_off_loop(surface, tmp_path, monkeypatch):
    from functools import partial
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage

    from nymeria.api.routers.threads import create_threads_router
    from nymeria.core.agent_compaction import CompactionManager
    from nymeria.core.agent_context import flush_memories_before_trim
    from nymeria.core.command_service import CommandBackendClient
    from nymeria.core.memory_index import MemoryIndex
    from nymeria.core.thread_lock_manager import ThreadLockManager

    index = MemoryIndex(tmp_path / "flush.db", embedding_provider="none")
    loop_thread = threading.get_ident()
    embed_threads = []

    def embed(texts, input_type):
        embed_threads.append(threading.get_ident())
        return [None] * len(texts)

    monkeypatch.setattr(index, "_embed_batch", embed)
    messages = [HumanMessage(content="remember the meeting"), AIMessage(content="Tuesday at noon")]

    async def get_state(config):
        return SimpleNamespace(values={"messages": messages})

    def stored():
        return [(row["content"], row["metadata"], row["thread_id"]) for row in
                index._get_connection().execute("SELECT * FROM chunks").fetchall()]

    cleared = []

    def clear_metadata(*args):
        assert stored(), "clear must flush before discarding history"
        cleared.append(args)

    host = SimpleNamespace(
        settings=SimpleNamespace(compact_keep_messages=2),
        _thread_locks=ThreadLockManager(),
        _get_memory_index=lambda uid: index,
        profile_manager=SimpleNamespace(get_profile=lambda uid: SimpleNamespace(get_rag_preferences=lambda: {})),
        _default_async_graph=SimpleNamespace(aget_state=get_state),
        thread_metadata_manager=SimpleNamespace(delete_thread=clear_metadata),
    )
    host._flush_memories_before_trim = partial(flush_memories_before_trim, host)

    async def exercise():
        if surface in {"compact", "auto_compact"}:
            manager = CompactionManager(host)

            async def prune(*args, **kwargs):
                assert stored(), "compaction must flush before trimming history"
                return {"success": True, "summary": "meeting retained"}

            manager._run_compact_turn_and_prune = prune
            method = manager.compact_now if surface == "compact" else manager._do_auto_compact
            return await method("thread-a", "user-a")
        monkeypatch.setattr("nymeria.core.checkpoint_cleanup.delete_thread_checkpoints", lambda *args: None)
        if surface == "clear_command":
            client = CommandBackendClient(host, user=SimpleNamespace(id="user-a"), settings_fn=lambda: host.settings)
            monkeypatch.setattr(client, "_require_thread_access", lambda tid: None)
            return await client.clear_thread("thread-a")
        router = create_threads_router(
            lambda: None, lambda: "user-a", lambda: host, lambda: host.settings, lambda *args: None,
            publish_sync_event_fn=lambda **kwargs: None,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/threads/{thread_id}/clear")
        from starlette.requests import Request

        return await endpoint(
            http_request=Request({"type": "http", "headers": []}),
            thread_id="thread-a", user_id="user-a", user=None,
        )

    try:
        result = asyncio.run(exercise())
        assert result.get("success") or result.get("status") == "ok"
        assert embed_threads and all(tid != loop_thread for tid in embed_threads)
        import json

        assert [(content, json.loads(metadata), tid) for content, metadata, tid in stored()] == [
            ("User: remember the meeting\n\nAssistant: Tuesday at noon",
             {"role": "conversation_turn", "source": "pre_trim_flush", "chunk_index": 0, "total_chunks": 1}, "thread-a"),
        ]
        if surface.startswith("clear"):
            assert cleared == [("user-a", "thread-a")]
    finally:
        index.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_clear_excludes_new_turns_while_flushing_and_releases_on_cancel(cancel, monkeypatch):
    from types import SimpleNamespace

    from nymeria.core.pending_prompt_queue import get_pending_queue
    from nymeria.core.thread_deletion import clear_thread_history
    from nymeria.core.thread_lock_manager import ThreadLockManager

    removed = []
    locks = ThreadLockManager()
    thread_id = "clear-during-indexing"

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()

        async def state(config):
            return SimpleNamespace(values={"messages": ["old history"]})

        def flush(*args):
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5)
            finally:
                finished.set()

        host = SimpleNamespace(
            _thread_locks=locks,
            _default_async_graph=SimpleNamespace(aget_state=state),
            _flush_memories_before_trim=flush,
            thread_metadata_manager=SimpleNamespace(delete_thread=lambda *args: removed.append("metadata")),
        )
        monkeypatch.setattr("nymeria.core.checkpoint_cleanup.delete_thread_checkpoints",
                            lambda *args: removed.append("checkpoints"))
        task = asyncio.create_task(clear_thread_history(host, SimpleNamespace(), "u1", thread_id))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert locks.get_lock(thread_id).locked()
            assert get_pending_queue().is_releasing(thread_id)
            assert removed == []
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert locks.get_lock(thread_id).locked()
        finally:
            release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert await asyncio.to_thread(finished.wait, 5)
        assert not locks.get_lock(thread_id).locked()
        assert not get_pending_queue().is_releasing(thread_id)
        assert removed == ([] if cancel else ["metadata", "checkpoints"])

    asyncio.run(exercise())


def test_drain_waits_until_failure_reporting_finishes(monkeypatch):
    from nymeria.core import embedding_jobs

    async def exercise():
        loop = asyncio.get_running_loop()
        reporting = asyncio.Event()
        release_job = threading.Event()
        release_report = threading.Event()
        reported = []

        def failed():
            assert release_job.wait(5)
            raise ValueError("report before drain finishes")

        def report(*args, **kwargs):
            loop.call_soon_threadsafe(reporting.set)
            assert release_report.wait(5)
            reported.append(str(kwargs["exc_info"][1]))

        monkeypatch.setattr(embedding_jobs.logger, "error", report)
        schedule_embedding_job(failed, site="test.reporting")
        release_job.set()
        try:
            await asyncio.wait_for(reporting.wait(), 5)
            drain = asyncio.create_task(wait_for_pending_embedding_jobs())
            await asyncio.sleep(0)
            assert not drain.done(), "drain must include the completion report"
        finally:
            release_report.set()
        await drain
        assert reported == ["report before drain finishes"]

    asyncio.run(exercise())
