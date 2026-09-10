"""Turn lifetime contracts through real ASGI disconnects and shared buffers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nymeria.core.interactive_admission import (
    get_interactive_turn_gate,
    reset_interactive_turn_gate_for_tests,
)
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.core.turn_stream_buffer import get_turn_stream_registry, reset_turn_stream_registry
from tests.test_api_chat_router import FakeChatAgent, _asgi_scope, _capacity_client


@pytest.fixture(autouse=True)
def fresh_turn_state():
    reset_interactive_turn_gate_for_tests()
    reset_turn_stream_registry()
    yield
    reset_interactive_turn_gate_for_tests()
    reset_turn_stream_registry()


@pytest.mark.parametrize("before_first_byte", [False, True])
def test_asgi_disconnect_preserves_holder_work_slot_and_replay(
    before_first_byte, tmp_path, api_client_builder,
):
    class ToolAgent(FakeChatAgent):
        def __init__(self):
            super().__init__(tmp_path)
            self._thread_locks = ThreadLockManager()
            self.history = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()
            self.cancelled = False

        async def astream(self, message, **kwargs):
            lock = self._thread_locks.get_lock(kwargs["thread_id"])
            assert lock.acquire(False)
            try:
                kwargs["_on_turn_started"]()
                self.entered.set()
                yield {"type": "tool_call", "name": "slow_tool", "args": {}}
                await self.release.wait()
                self.history.append("TOOL_FINISHED")
                yield {"type": "tool_result", "name": "slow_tool", "content": "TOOL_FINISHED"}
                yield {"type": "response", "content": "Completed after disconnect"}
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            finally:
                lock.release()
                self.finished.set()

    async def exercise():
        agent = ToolAgent()
        client, _, token = _capacity_client(tmp_path, api_client_builder, agent)
        app = client.app
        disconnect = asyncio.Event()
        body = json.dumps({"message": "run tool", "thread_id": "detached-holder"}).encode()
        delivered = False
        sent = []

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(event: dict[str, Any]):
            sent.append(event)
            if b'"type": "tool_call"' in event.get("body", b""):
                disconnect.set()

        if before_first_byte:
            disconnect.set()
        observer = asyncio.create_task(app(_asgi_scope("/chat", token, body), receive, send))
        try:
            await asyncio.wait_for(observer, 5)
            await asyncio.wait_for(agent.entered.wait(), 5)
            assert not agent.cancelled, "HTTP disconnect cancelled the tool"
            assert agent._thread_locks.is_thread_busy("detached-holder")
            assert get_interactive_turn_gate().active == 1
            agent.release.set()
            await asyncio.wait_for(agent.finished.wait(), 5)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as reader:
                response = await asyncio.wait_for(reader.get(
                    "/threads/detached-holder/turn/stream",
                    headers=api_client_builder.auth(token),
                ), 5)
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert [event["type"] for event in events] == [
                "turn_attach", "turn_started", "tool_call", "tool_result", "response", "done",
            ]
            assert events[3]["content"] == "TOOL_FINISHED"
            assert events[-1]["title"] == "Auto Title"
            assert agent.history == ["TOOL_FINISHED"]
            assert get_interactive_turn_gate().active == 0
            assert not agent._thread_locks.is_thread_busy("detached-holder")
            buffer = get_turn_stream_registry().get("detached-holder")
            assert buffer.snapshot()["state"] == "done"
        finally:
            agent.release.set()
            if not observer.done():
                observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            if agent.entered.is_set():
                await asyncio.wait_for(agent.finished.wait(), 5)

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_cancel_before_first_runner_step_releases_slot():
    from nymeria.core.turn_runner import AsyncTurnSink, TurnSpec, active_turn_task_count, start_turn

    slot = get_interactive_turn_gate().try_acquire(1)
    sink = AsyncTurnSink()
    task = start_turn(object(), TurnSpec("hello", "not-started", "alice"), sink, slot)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await sink.get() is None
    assert get_interactive_turn_gate().active == 0
    assert active_turn_task_count() == 0
    assert get_turn_stream_registry().get("not-started") is None


@pytest.mark.asyncio
async def test_detached_queuer_ends_observation_but_keeps_prompt():
    from nymeria.core.pending_prompt_queue import InMemoryPendingPromptQueue, make_pending_prompt
    from nymeria.core.turn_runner import AsyncTurnSink, TurnSpec, active_turn_task_count, start_turn

    pending = InMemoryPendingPromptQueue()
    closed = asyncio.Event()

    class Queuer:
        async def astream(self, message, **kwargs):
            prompt = make_pending_prompt(message=message, source="user", source_id=None,
                                         source_label="Alice", user_id="alice", is_autonomous=False)
            pending.enqueue(kwargs["thread_id"], prompt)
            try:
                yield {"type": "queued", "content": "Waiting"}
                yield {"type": "prompt_queued", "position": 1}
                await asyncio.Event().wait()
            finally:
                closed.set()

    sink = AsyncTurnSink()
    slot = get_interactive_turn_gate().try_acquire(1)
    task = start_turn(Queuer(), TurnSpec("Keep this prompt", "queuer", "alice"), sink, slot)
    assert (await sink.get())["type"] == "queued"
    receipt = await sink.get()
    assert receipt == {"type": "prompt_queued", "position": 1, "thread_id": "queuer"}
    assert get_interactive_turn_gate().active == 0
    sink.detach()
    await asyncio.wait_for(closed.wait(), 5)
    await asyncio.gather(task, return_exceptions=True)
    assert active_turn_task_count() == 0
    assert get_turn_stream_registry().get("queuer") is None
    assert [prompt.message for prompt in pending.drain("queuer")] == ["Keep this prompt"]


@pytest.mark.asyncio
async def test_waiting_prompt_promotes_after_its_observer_leaves():
    from nymeria.core.turn_runner import AsyncTurnSink, TurnSpec, start_turn
    from tests.test_turn_stream_buffer import _FakeAgent

    release = asyncio.Event()

    class PromotingAgent(_FakeAgent):
        async def astream(self, message, **kwargs):
            yield {"type": "queued", "content": "Waiting for release"}
            await release.wait()
            kwargs["_on_turn_started"]()
            yield {"type": "response", "content": "PROMOTED_AND_FINISHED"}

    sink = AsyncTurnSink()
    task = start_turn(PromotingAgent(), TurnSpec("next", "promoted", "alice"), sink)
    assert (await sink.get())["type"] == "queued"
    sink.detach()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.response_parts == ["PROMOTED_AND_FINISHED"]
    entries = [json.loads(payload) async for payload in result.buffer.stream_payloads()]
    assert [event["type"] for event in entries] == ["turn_started", "response", "done"]
    assert not result.fanout_observed


@pytest.mark.asyncio
async def test_autonomous_mirror_and_notification_finish_after_detach():
    from nymeria.core.turn_runner import AsyncTurnSink, TurnSpec, start_turn
    from tests.test_turn_stream_buffer import _FakeAgent

    release = asyncio.Event()
    bookends, mirrored, notifications = [], [], []

    class AutonomousAgent(_FakeAgent):
        async def astream(self, message, **kwargs):
            kwargs["_on_turn_started"]()
            yield {"type": "thinking", "content": "before disconnect"}
            await release.wait()
            yield {"type": "response", "content": "after disconnect"}

    sink = AsyncTurnSink()
    task = start_turn(AutonomousAgent(), TurnSpec(
        "work", "autonomous-detach", "alice", is_self_invoke=True,
        autonomous_task_id="task-1", source="trigger", trigger_fields={"trigger_name": "Probe"},
        publish_autonomous_event=lambda **event: bookends.append(event),
        publish_agent_stream_chunk=lambda chunk, **kw: mirrored.append(chunk),
        create_autonomous_notification=lambda **event: notifications.append(event),
        should_notify_autonomous=lambda *_: True,
    ), sink)
    marker = await sink.get()
    assert marker["type"] == "holder_started"
    sink.detach()
    release.set()
    await asyncio.wait_for(task, 5)
    assert [event["event_type"] for event in bookends] == ["task_started", "task_completed"]
    assert bookends[-1]["data"]["content"] == "after disconnect"
    assert bookends[-1]["data"]["trigger_name"] == "Probe"
    assert mirrored[-1] == {"type": "response", "content": "after disconnect"}
    assert all("seq" not in chunk for chunk in mirrored)
    assert [notice["summary"] for notice in notifications] == ["after disconnect"]


@pytest.mark.asyncio
async def test_new_holder_preserves_finishing_predecessor_and_typed_replay():
    from nymeria.core.turn_runner import HolderTurn, TurnSpec
    from tests.test_turn_stream_buffer import _FakeAgent

    first = HolderTurn(_FakeAgent(), TurnSpec("one", "handoff", "alice"))
    old_buffer = first.begin()
    first.record({"type": "response", "content": "first result"})
    reader = old_buffer.stream_entries()
    assert (await anext(reader))[1] == "turn_started"
    second = HolderTurn(_FakeAgent(), TurnSpec("two", "handoff", "alice"))
    new_buffer = second.begin()
    assert old_buffer.state == "live"
    first.finish_done()
    second.record({"type": "response", "content": "second result"})
    second.finish_done()
    tail = [entry async for entry in reader]
    assert [(seq, kind) for seq, kind, _ in tail] == [(2, "response"), (3, "done")]
    assert json.loads(tail[0][2])["content"] == "first result"
    assert old_buffer.state == new_buffer.state == "done"
    assert get_turn_stream_registry().get("handoff") is new_buffer


@pytest.mark.asyncio
async def test_slow_http_observer_gets_replay_gap(monkeypatch):
    from nymeria.api.schemas.chat import ChatRequest
    from nymeria.core import turn_stream_buffer
    from tests.test_turn_stream_buffer import (
        _AuthUser, _FakeAgent, _FakeRequest, _PublishRecorder, _build_router, _chat_endpoint, _drain, _sse_events,
    )

    monkeypatch.setattr(turn_stream_buffer, "MAX_EVENTS_PER_TURN", 2)
    agent = _FakeAgent(chunks=[{"type": "response", "content": str(i)} for i in range(10)])
    response = await _chat_endpoint(_build_router(agent, _PublishRecorder()))(
        http_request=_FakeRequest(), request=ChatRequest(message="hello", thread_id="slow-observer"),
        user=_AuthUser(),
    )
    events = _sse_events(await _drain(response))
    assert events == [{"type": "turn_replay_gap", "thread_id": "slow-observer",
                       "turn_id": get_turn_stream_registry().get("slow-observer").turn_id}]
    assert get_turn_stream_registry().get("slow-observer").state == "done"


@pytest.mark.asyncio
async def test_shutdown_drains_turns_on_both_owner_loops():
    import threading

    from nymeria.core.turn_runner import TurnSpec, active_turn_task_count, open_turn_runner, shutdown_turns, start_turn
    from tests.test_turn_stream_buffer import _FakeAgent

    ready = threading.Event()
    remote_started = threading.Event()
    ended = []
    remote_loop = asyncio.new_event_loop()

    class HangingAgent(_FakeAgent):
        async def astream(self, message, **kwargs):
            kwargs["_on_turn_started"]()
            try:
                if kwargs["thread_id"] == "remote-loop":
                    remote_started.set()
                yield {"type": "tool_call", "name": "unfinished_tool"}
                await asyncio.Event().wait()
            finally:
                ended.append((kwargs["thread_id"], threading.get_ident()))

    def serve():
        asyncio.set_event_loop(remote_loop)
        ready.set()
        remote_loop.run_forever()
        remote_loop.close()

    worker = threading.Thread(target=serve)
    worker.start()
    assert await asyncio.to_thread(ready.wait, 5)

    async def remote_start():
        return start_turn(HangingAgent(), TurnSpec("remote", "remote-loop", "alice"))

    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(remote_start(), remote_loop))
        assert await asyncio.to_thread(remote_started.wait, 5)
        local = start_turn(HangingAgent(), TurnSpec("local", "local-loop", "alice"))
        await asyncio.sleep(0)
        assert active_turn_task_count() == 2
        await asyncio.wait_for(shutdown_turns(), 5)
        assert local.cancelled()
        assert active_turn_task_count() == 0
        assert sorted(thread for thread, _ in ended) == ["local-loop", "remote-loop"]
        assert len({owner for _, owner in ended}) == 2
        assert all(get_turn_stream_registry().get(thread).state == "aborted" for thread, _ in ended)
    finally:
        await shutdown_turns()
        open_turn_runner()
        remote_loop.call_soon_threadsafe(remote_loop.stop)
        await asyncio.to_thread(worker.join, 5)


@pytest.mark.asyncio
async def test_fully_evicted_aborted_buffer_reports_gap():
    from nymeria.core.turn_stream_buffer import MAX_BYTES_PER_TURN, TurnReplayGapError, TurnStreamBuffer

    buffer = TurnStreamBuffer("oversized", "alice")
    buffer.append({"type": "response", "content": "x" * (MAX_BYTES_PER_TURN + 1)})
    buffer.finish("aborted")
    with pytest.raises(TurnReplayGapError):
        _ = [event async for event in buffer.stream_entries()]
    # A reader that already consumed that event has no missing suffix.
    assert [event async for event in buffer.stream_entries(buffer.last_seq)] == []


@pytest.mark.asyncio
async def test_real_agent_queuer_detach_closes_mailbox_without_withdrawal():
    from nymeria.core.pending_prompt_queue import get_pending_queue, reset_pending_queue_for_tests
    from nymeria.core.turn_runner import AsyncTurnSink, TurnSpec, start_turn
    from tests.test_agent_turn_loops import _stream_agent

    reset_pending_queue_for_tests()
    agent = _stream_agent("real-queuer")
    lock = agent._thread_locks.get_lock("real-queuer")
    lock.acquire()
    sink = AsyncTurnSink()
    task = start_turn(agent, TurnSpec("still queued", "real-queuer", "alice"), sink)
    try:
        assert (await asyncio.wait_for(sink.get(), 5))["type"] == "queued"
        assert (await asyncio.wait_for(sink.get(), 5))["type"] == "prompt_queued"
        sink.detach()
        await asyncio.gather(task, return_exceptions=True)
        prompts = get_pending_queue().drain("real-queuer")
        assert [prompt.message for prompt in prompts] == ["still queued"]
        prompt = prompts[0]
        assert not prompt.abandoned and not prompt.restored and not prompt.notify_event.is_set()
        prompt.fanout_mailbox.put({"type": "response", "content": "should not be retained"})
        assert (await asyncio.wait_for(prompt.fanout_mailbox.get(), 1))["type"] == "prompt_absorbed_sentinel"
    finally:
        lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        reset_pending_queue_for_tests()


@pytest.mark.asyncio
async def test_uvicorn_shutdown_reaches_turn_drain_with_connected_observer(monkeypatch):
    import socket

    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from nymeria.core.turn_runner import TurnSpec, open_turn_runner, shutdown_turns, start_turn
    from nymeria.triggers import api as api_module
    from tests.test_turn_stream_buffer import _FakeAgent

    app = FastAPI()
    entered = asyncio.Event()
    closed = asyncio.Event()
    pools_closed = []
    buffered = []
    server = None

    class HangingAgent(_FakeAgent):
        async def astream(self, message, **kwargs):
            kwargs["_on_turn_started"]()
            try:
                entered.set()
                yield {"type": "tool_call", "name": "waiting_tool"}
                await asyncio.Event().wait()
            finally:
                closed.set()

    @app.get('/stream')
    async def stream():
        start_turn(HangingAgent(), TurnSpec("wait", "shutdown-http", "alice"))
        await entered.wait()
        buffer = get_turn_stream_registry().get("shutdown-http")
        buffered.append(buffer)
        return StreamingResponse(buffer.stream_payloads(), media_type="text/event-stream")

    async def close_pools():
        assert closed.is_set(), "provider pool closed before the turn was drained"
        pools_closed.append(True)

    app.router.add_event_handler("startup", open_turn_runner)
    app.router.add_event_handler("shutdown", shutdown_turns)
    app.router.add_event_handler("shutdown", close_pools)
    monkeypatch.setattr(api_module, "create_api_app", lambda *args, **kwargs: app)

    def start_server(configured_app, **kwargs):
        nonlocal server
        server = uvicorn.Server(uvicorn.Config(configured_app, log_level="critical", **kwargs))

    monkeypatch.setattr(uvicorn, "run", start_server)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    api_module.run_api(host='127.0.0.1', port=port)
    serve_task = asyncio.create_task(server.serve(sockets=[listener]))
    reader_task = None
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async def observe():
                try:
                    async with client.stream('GET', f'http://127.0.0.1:{port}/stream') as response:
                        async for _ in response.aiter_bytes():
                            pass
                except httpx.TransportError:
                    pass  # Uvicorn may close the observer before lifespan drain.

            reader_task = asyncio.create_task(observe())
            await asyncio.wait_for(entered.wait(), 5)
            assert not closed.is_set()
            server.should_exit = True
            await asyncio.wait_for(asyncio.shield(serve_task), 10)
            await asyncio.wait_for(reader_task, 5)
        assert closed.is_set()
        assert pools_closed == [True]
        assert buffered[0].state == "aborted"
    finally:
        server.should_exit = True
        await shutdown_turns()
        open_turn_runner()
        if reader_task is not None:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        if not serve_task.done():
            serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)
        listener.close()
        open_turn_runner()


@pytest.mark.asyncio
async def test_stop_endpoint_cooperatively_ends_runner_and_restores_queue(tmp_path, api_client_builder):
    import threading

    from nymeria.core.agent import NymeriaAgent
    from nymeria.core.pending_prompt_queue import (
        FanoutMailbox, get_pending_queue, make_pending_prompt, reset_pending_queue_for_tests,
    )
    from nymeria.core.thread_lock_manager import async_event_wait

    reset_pending_queue_for_tests()
    entered = asyncio.Event()

    class StoppableAgent(FakeChatAgent):
        abort_with_cascade = NymeriaAgent.abort_with_cascade

        def __init__(self):
            super().__init__(tmp_path)
            self._thread_locks = ThreadLockManager()
            self._invocations_lock = threading.Lock()
            self._active_callable_invocations = {}
            self.task_cancelled = False

        async def astream(self, message, **kwargs):
            thread = kwargs["thread_id"]
            lock = self._thread_locks.get_lock(thread)
            assert lock.acquire(False)
            self._thread_locks.set_lock_info(thread, "user")
            try:
                kwargs["_on_turn_started"]()
                entered.set()
                yield {"type": "tool_call", "name": "controlled_tool"}
                assert await async_event_wait(self._thread_locks.get_abort_event(thread), 5)
                yield {"type": "error", "code": "cancelled", "content": "Stopped by user."}
            except asyncio.CancelledError:
                self.task_cancelled = True
                raise
            finally:
                self._thread_locks.clear_lock_info(thread)
                lock.release()

    agent = StoppableAgent()
    client, _, token = _capacity_client(tmp_path, api_client_builder, agent)
    mailbox = FanoutMailbox(asyncio.get_running_loop())
    pending = make_pending_prompt(
        message="return this to composer", source="user", source_id=None, source_label="Alice",
        user_id="alice", is_autonomous=False, fanout_mailbox=mailbox,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as reader:
            post = asyncio.create_task(reader.post('/chat', headers=api_client_builder.auth(token),
                                                  json={"message": "run", "thread_id": "cooperative-stop"}))
            await asyncio.wait_for(entered.wait(), 5)
            get_pending_queue().enqueue("cooperative-stop", pending)
            stopped = await reader.post('/threads/cooperative-stop/stop', headers=api_client_builder.auth(token))
            assert [prompt["text"] for prompt in stopped.json()["restored_prompts"]] == ["return this to composer"]
            completed = await asyncio.wait_for(post, 5)
        events = [json.loads(line[6:]) for line in completed.text.splitlines() if line.startswith('data: ')]
        assert [event["code"] for event in events if event["type"] == "error"] == ["cancelled"]
        assert not agent.task_cancelled
        assert get_interactive_turn_gate().active == 0
        assert get_turn_stream_registry().get("cooperative-stop").state != "live"
        assert (await mailbox.get())["code"] == "restored"
        assert pending.restored and get_pending_queue().size("cooperative-stop") == 0
    finally:
        agent._thread_locks.signal_abort("cooperative-stop")
        reset_pending_queue_for_tests()


@pytest.mark.asyncio
async def test_eviction_during_cached_batch_still_reports_terminal_gap():
    from nymeria.core.turn_stream_buffer import MAX_BYTES_PER_TURN, TurnReplayGapError, TurnStreamBuffer

    buffer = TurnStreamBuffer("cached-gap", "alice")
    buffer.append({"type": "response", "content": "first"})
    buffer.append({"type": "response", "content": "second"})
    reader = buffer.stream_entries()
    assert (await anext(reader))[0] == 1
    buffer.append({"type": "response", "content": "x" * (MAX_BYTES_PER_TURN + 1)})
    buffer.finish("aborted")
    assert (await anext(reader))[0] == 2
    with pytest.raises(TurnReplayGapError):
        await anext(reader)


@pytest.mark.asyncio
async def test_delayed_post_observer_replays_its_own_replaced_buffer():
    from nymeria.api.schemas.chat import ChatRequest
    from tests.test_turn_stream_buffer import (
        _AuthUser, _FakeAgent, _FakeRequest, _PublishRecorder, _build_router, _chat_endpoint, _drain, _sse_events,
    )

    responses = []
    for content in ("FIRST TURN", "SECOND TURN"):
        agent = _FakeAgent(chunks=[{"type": "response", "content": content}])
        endpoint = _chat_endpoint(_build_router(agent, _PublishRecorder()))
        responses.append(await endpoint(http_request=_FakeRequest(),
                                        request=ChatRequest(message=content, thread_id="delayed-post"), user=_AuthUser()))
        await asyncio.sleep(0)  # producer finishes before either observer starts
    first, second = [_sse_events(await _drain(response)) for response in responses]
    assert [event["content"] for event in first if event["type"] == "response"] == ["FIRST TURN"]
    assert [event["content"] for event in second if event["type"] == "response"] == ["SECOND TURN"]
    assert first[0]["turn_id"] != second[0]["turn_id"]
    assert first[-1]["type"] == second[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_production_app_drains_runner_before_closing_provider_pool(tmp_path, api_client_builder, monkeypatch):
    import inspect

    from nymeria.core.turn_runner import TurnSpec, open_turn_runner, start_turn
    from nymeria.vendor.react_agent import providers
    from tests.test_turn_stream_buffer import _FakeAgent

    entered, closed = asyncio.Event(), asyncio.Event()
    pools_closed = []

    class HangingAgent(_FakeAgent):
        async def astream(self, message, **kwargs):
            kwargs["_on_turn_started"]()
            try:
                entered.set()
                yield {"type": "response", "content": "started"}
                await asyncio.Event().wait()
            finally:
                closed.set()

    async def close_pool():
        assert closed.is_set(), "production shutdown closed the pool with a live turn"
        pools_closed.append(True)

    monkeypatch.setattr(providers, "close_provider_async_http_pools_for_loop", close_pool)
    client, _, _ = _capacity_client(tmp_path, api_client_builder)
    task = start_turn(HangingAgent(), TurnSpec("wait", "production-shutdown", "alice"))
    await entered.wait()
    try:
        for handler in client.app.router.on_shutdown:
            result = handler()
            if inspect.isawaitable(result):
                await result
            if pools_closed:
                break
        assert pools_closed == [True]
        assert task.cancelled()
        assert get_turn_stream_registry().get("production-shutdown").state == "aborted"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        open_turn_runner()


@pytest.mark.asyncio
async def test_slow_queuer_sink_bounds_fanout_but_preserves_receipt_and_error(monkeypatch):
    from nymeria.core import turn_runner

    monkeypatch.setattr(turn_runner, "FANOUT_MAILBOX_MAXSIZE", 4)
    sink = turn_runner.AsyncTurnSink()
    sink.put({"type": "queued"})
    sink.put({"type": "prompt_queued", "position": 1})
    for number in range(8):
        sink.put({"type": "response", "content": str(number)})
    sink.put({"type": "error", "code": "restored", "content": "Returned to composer"})
    sink.close()
    events = []
    while (event := await sink.get()) is not None:
        events.append(event)
    assert [event["type"] for event in events] == [
        "fanout_dropped", "queued", "prompt_queued", "response", "error",
    ]
    assert events[0]["dropped_count"] == 7
    assert events[2]["position"] == 1
    assert events[3]["content"] == "7"
    assert events[-1]["code"] == "restored"
