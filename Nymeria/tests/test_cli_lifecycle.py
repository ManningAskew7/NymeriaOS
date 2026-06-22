from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from cli_fixtures import (
    ChatRequest,
    DelayedEvent,
    FakeAgentClient,
    FakeTerminalCapabilities,
    run,
)

from nymeria.triggers.cli.input import ComposerSubmission
from nymeria.triggers.cli.lifecycle import (
    TurnLifecycleController,
    stream_error_event_from_exception,
)
from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
)
from nymeria.triggers.cli.state import (
    CLIUIState,
    create_initial_state,
    reduce_stream_event,
    start_turn,
)


def make_shell(client: Any) -> FullScreenPromptToolkitShell:
    return FullScreenPromptToolkitShell(
        client=client,
        capabilities=FakeTerminalCapabilities(width=100),
        config=FullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="test-model",
            thread_label="Fixture thread",
        ),
    )


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


def test_full_screen_stream_exception_renders_structured_error() -> None:
    client = RaisingStreamClient()
    shell = make_shell(client)

    assert run(shell.run_chat_turn("hello")) is True

    assert shell.state.turn_status == "error"
    assert shell.state.errors[-1].code == "cli_stream_error"
    assert "network dropped" in shell.transcript.text
    assert client.stop_count == 0


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


def test_full_screen_explicit_stop_converts_followup_disconnect_to_cancelled() -> None:
    async def exercise() -> FullScreenPromptToolkitShell:
        client = StopAwareDisconnectClient()
        shell = make_shell(client)

        assert shell._handle_composer_submission(ComposerSubmission("slow"))
        await asyncio.sleep(0)
        assert shell._busy is True
        assert await shell.stop_current_turn() is True
        assert shell._current_turn_task is not None
        await shell._current_turn_task
        return shell

    shell = run(exercise())

    assert shell.state.turn_status == "cancelling"
    assert shell.state.errors[-1].code == "cancelled"
    assert "Cancelled." in shell.transcript.text


def test_full_screen_shutdown_stops_active_turn_and_cancels_task() -> None:
    async def exercise() -> tuple[FullScreenPromptToolkitShell, FakeAgentClient]:
        client = FakeAgentClient(
            streams={
                "slow": [
                    DelayedEvent(
                        60.0,
                        {
                            "type": "response",
                            "content": "late",
                            "thread_id": "thread-1",
                        },
                    )
                ]
            },
            real_sleep=True,
        )
        shell = make_shell(client)

        assert shell._handle_composer_submission(ComposerSubmission("slow")) is True
        await asyncio.sleep(0)
        assert shell._busy is True
        result = await shell.shutdown_active_turn(reason="shutdown")

        assert result is not None
        assert result.status == "stopping"
        assert shell._current_turn_task is not None
        assert shell._current_turn_task.done()
        return shell, client

    shell, client = run(exercise())

    assert client.stop_count == 1
    assert shell.state.turn_status == "cancelling"
