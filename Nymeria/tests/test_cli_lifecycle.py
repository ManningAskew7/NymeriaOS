from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from cli_fixtures import (
    CapturedRenderOutput,
    ChatRequest,
    FakeAgentClient,
    FakeTerminalCapabilities,
    run,
)

from nymeria.triggers.cli.app import CLIApp, CLIRuntimeConfig, _RichReplRuntime
from nymeria.triggers.cli.input import ComposerSubmission
from nymeria.triggers.cli.lifecycle import (
    TurnLifecycleController,
    stream_error_event_from_exception,
)
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.state import (
    CLIUIState,
    create_initial_state,
    reduce_stream_event,
    start_turn,
)


class DummyAgent:
    pass


def make_rich_harness(
    client: FakeAgentClient,
) -> tuple[CLIApp, RichReplRenderer, _RichReplRuntime]:
    """Rich-runtime harness mirroring the live REPL wiring (no pt Application)."""
    app = CLIApp(
        DummyAgent(),
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", transport="local"),
    )
    app._client = client
    app._maybe_auto_title = lambda _message: None
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(supports_color=False),
        stdout=CapturedRenderOutput().stdout,
        width=100,
    )
    runtime = _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=120, supports_color=False),
    )
    return app, renderer, runtime


def state_with_running_tool() -> CLIUIState:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = start_turn(state, "use tool", now=0.1)
    return reduce_stream_event(
        state,
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "search_memory",
            "thread_id": "thread-1",
        },
        now=0.2,
    )


def test_lifecycle_stop_is_idempotent_and_marks_running_tools_cancelled() -> None:
    client = FakeAgentClient()
    active = True
    state = state_with_running_tool()

    def set_state(next_state: CLIUIState) -> None:
        nonlocal state
        state = next_state

    controller = TurnLifecycleController(
        client=client,
        state_getter=lambda: state,
        state_setter=set_state,
        thread_id_getter=lambda: "thread-1",
        user_id_getter=lambda: "alice",
        is_active=lambda: active,
    )
    controller.begin_turn()

    first = run(controller.request_stop(reason="ctrl_c", now=1.0))
    duplicate = run(controller.request_stop(reason="ctrl_c", now=1.1))

    assert first.status == "stopping"
    assert duplicate.status == "already_stopping"
    assert client.stop_count == 1
    assert state.turn_status == "cancelling"
    assert state.active_tool_calls["call-1"].status == "cancelled"


def test_lifecycle_stop_noops_without_active_turn() -> None:
    client = FakeAgentClient()
    state = create_initial_state(thread_id="thread-1", user_id="alice")
    controller = TurnLifecycleController(
        client=client,
        state_getter=lambda: state,
        state_setter=lambda next_state: None,
        thread_id_getter=lambda: "thread-1",
        user_id_getter=lambda: "alice",
        is_active=lambda: False,
    )

    result = run(controller.request_stop(reason="ctrl_c"))

    assert result.status == "no_active_turn"
    assert client.stop_count == 0


class FailingStopClient(FakeAgentClient):
    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        await super().stop(thread_id, user_id)
        raise RuntimeError("stop endpoint unavailable")


def test_lifecycle_stop_failure_can_be_retried() -> None:
    client = FailingStopClient()
    state = state_with_running_tool()
    controller = TurnLifecycleController(
        client=client,
        state_getter=lambda: state,
        state_setter=lambda next_state: None,
        thread_id_getter=lambda: "thread-1",
        user_id_getter=lambda: "alice",
        is_active=lambda: True,
    )

    first = run(controller.request_stop(reason="ctrl_c"))
    second = run(controller.request_stop(reason="ctrl_c"))

    assert first.status == "failed"
    assert second.status == "failed"
    assert client.stop_count == 2


def test_stream_exception_event_distinguishes_disconnect_from_explicit_stop() -> None:
    disconnect = stream_error_event_from_exception(
        RuntimeError("network dropped"),
        thread_id="thread-1",
        explicit_stop_requested=False,
    )
    cancelled = stream_error_event_from_exception(
        RuntimeError("read cancelled"),
        thread_id="thread-1",
        explicit_stop_requested=True,
    )

    assert disconnect.code == "cli_stream_error"
    assert disconnect.details["explicit_stop"] is False
    assert cancelled.code == "cancelled"
    assert cancelled.details["explicit_stop"] is True


class RaisingStreamClient(FakeAgentClient):
    async def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Mapping[str, Any]] | None = None,
        **options: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.chat_requests.append(
            ChatRequest(
                message=message,
                thread_id=thread_id,
                user_id=user_id,
                attachments=tuple(attachments or ()),
                options=options,
            )
        )
        raise RuntimeError("network dropped")
        yield {}


def test_rich_stream_exception_keeps_prompt_alive_with_status_notice() -> None:
    """A raw stream exception must not kill the turn task or the prompt.

    Rehosted from the legacy shell (which converted the exception into a
    transcript error event): the Rich runtime instead surfaces a status-bar
    notice and frees the prompt.
    """

    async def exercise() -> tuple[RaisingStreamClient, _RichReplRuntime]:
        client = RaisingStreamClient()
        app, renderer, runtime = make_rich_harness(client)

        await app._submit_rich_submission_async(
            ComposerSubmission("hello"), renderer, runtime=runtime
        )
        task = runtime.current_turn_task
        assert task is not None
        await task
        return client, runtime

    client, runtime = run(exercise())

    assert client.chat_requests[0].message == "hello"
    assert client.stop_count == 0
    assert runtime.busy is False
    assert runtime.current_turn_task is None
    assert "Stream failed: network dropped" in runtime.status_text()


class StopAwareDisconnectClient(FakeAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.stop_event = asyncio.Event()

    async def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Mapping[str, Any]] | None = None,
        **options: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.chat_requests.append(
            ChatRequest(
                message=message,
                thread_id=thread_id,
                user_id=user_id,
                attachments=tuple(attachments or ()),
                options=options,
            )
        )
        await self.stop_event.wait()
        raise RuntimeError("stream closed after stop")
        yield {}

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        result = await super().stop(thread_id, user_id)
        self.stop_event.set()
        return result


def test_rich_explicit_stop_reaches_client_once_and_frees_prompt() -> None:
    """Explicit stop wiring on the Rich path: one client.stop, prompt recovers.

    Rehosted from the legacy shell's stop tests. The at-most-once stop
    semantics are pinned separately by the TurnLifecycleController unit tests
    above; this pins the live Rich wiring end to end.
    """

    async def exercise() -> tuple[StopAwareDisconnectClient, _RichReplRuntime]:
        client = StopAwareDisconnectClient()
        app, renderer, runtime = make_rich_harness(client)

        await app._submit_rich_submission_async(
            ComposerSubmission("slow"), renderer, runtime=runtime
        )
        task = runtime.current_turn_task
        assert task is not None
        for _ in range(10):
            await asyncio.sleep(0)
            if runtime.busy:
                break
        assert runtime.busy is True

        await app._stop_current_turn_async()
        await task
        return client, runtime

    client, runtime = run(exercise())

    assert client.stop_count == 1
    assert runtime.busy is False
    assert runtime.current_turn_task is None
