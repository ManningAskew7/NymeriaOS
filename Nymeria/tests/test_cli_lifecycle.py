from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
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
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.repl_runtime import _RichReplPromptToolkitShell


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

    Rehosted from the legacy shell's stop tests. Pins the live Rich wiring
    end to end: exactly one stop call reaches the client and the prompt
    returns to idle. (The old TurnLifecycleController and its unit tests were
    removed 2026-07-04; the Rich path owns stop semantics directly in
    app._stop_current_turn_async.)
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


def make_shell_harness(
    client: FakeAgentClient, tmp_path: Path
) -> tuple[CLIApp, _RichReplRuntime, _RichReplPromptToolkitShell, Any]:
    """Harness plus the pt shell (not built) and a fake composer buffer."""
    app, renderer, runtime = make_rich_harness(client)
    shell = _RichReplPromptToolkitShell(
        cli_app=app,
        runtime=runtime,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=120, supports_color=False),
        history_path=tmp_path / "history",
    )
    buffer = SimpleNamespace(text="", cursor_position=0)
    runtime.composer_controller = SimpleNamespace(
        text_area=SimpleNamespace(buffer=buffer)
    )
    return app, runtime, shell, buffer


class RestoringStopClient(FakeAgentClient):
    """Fake client whose stop response hands a queued prompt back."""

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        result = await super().stop(thread_id, user_id)
        return {
            **result,
            "status": "stopping",
            "restored_prompts": [
                {"text": "from server", "source_label": "U", "user_id": "u1"}
            ],
        }


def test_rich_stop_restores_server_and_local_queued_texts(tmp_path: Path) -> None:
    """Stop hands queued prompts back to the composer instead of discarding.

    Backlog #16: server-restored texts come first, then the client-side
    deque entries (which never reached the backend), joined by --- lines.
    """

    async def exercise() -> tuple[_RichReplRuntime, Any]:
        client = RestoringStopClient()
        app, runtime, shell, buffer = make_shell_harness(client, tmp_path)
        runtime.queue_submission(ComposerSubmission("local one"))
        runtime.queue_submission(ComposerSubmission("local two"))

        await shell._stop_current_turn()
        return runtime, buffer

    runtime, buffer = run(exercise())

    assert buffer.text == "from server\n---\nlocal one\n---\nlocal two"
    assert runtime.queued_count == 0
    assert "returned to the composer" in runtime.status_text()


def test_rich_stop_preserves_existing_composer_draft(tmp_path: Path) -> None:
    async def exercise() -> Any:
        client = RestoringStopClient()
        app, runtime, shell, buffer = make_shell_harness(client, tmp_path)
        buffer.text = "half-typed draft"

        await shell._stop_current_turn()
        return buffer

    buffer = run(exercise())

    assert buffer.text == "half-typed draft\n---\nfrom server"


def test_rich_stop_prevents_queued_auto_send(tmp_path: Path) -> None:
    """After a stop, the submission chain must NOT auto-send queued entries.

    Pre-#16 behavior: _run_rich_submission_chain drained the deque when the
    halted turn ended, silently sending the next queued message.
    """

    async def exercise() -> tuple[StopAwareDisconnectClient, _RichReplRuntime, Any]:
        client = StopAwareDisconnectClient()
        app, runtime, shell, buffer = make_shell_harness(client, tmp_path)

        await app._submit_rich_submission_async(
            ComposerSubmission("slow"), shell.renderer, runtime=runtime
        )
        task = runtime.current_turn_task
        assert task is not None
        for _ in range(10):
            await asyncio.sleep(0)
            if runtime.busy:
                break
        assert runtime.busy is True

        # Queue while busy, then stop: the queued entry must be restored,
        # not sent as the next turn.
        runtime.queue_submission(ComposerSubmission("queued while busy"))
        await shell._stop_current_turn()
        await task
        return client, runtime, buffer

    client, runtime, buffer = run(exercise())

    assert [request.message for request in client.chat_requests] == ["slow"]
    assert buffer.text == "queued while busy"
    assert runtime.queued_count == 0


def test_rich_repeat_stop_presses_schedule_one_backend_stop(tmp_path: Path) -> None:
    """_request_stop is idempotent while a stop task is in flight (backlog #11)."""

    async def exercise() -> tuple[FakeAgentClient, _RichReplRuntime]:
        client = FakeAgentClient()
        app, runtime, shell, _buffer = make_shell_harness(client, tmp_path)

        assert shell._request_stop() is True
        assert shell._request_stop() is True  # swallowed: stop already pending
        stop_task = runtime._stop_task
        assert stop_task is not None
        await stop_task
        return client, runtime

    client, runtime = run(exercise())

    assert client.stop_count == 1
    # Terminal copy: the in-progress "Stopping..." must not be the resting
    # status-bar state after the stop completes with nothing to restore.
    assert "Stop requested." in runtime.status_text()


class SlowStopRestoringClient(FakeAgentClient):
    """Stop releases the stream immediately but its RPC response lags.

    Models the production ordering where the backend abort kills the SSE
    stream well before the POST /stop HTTP response arrives.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release_stream = asyncio.Event()
        self.release_stop = asyncio.Event()

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
        await self.release_stream.wait()
        raise RuntimeError("stream closed after stop")
        yield {}

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        result = await super().stop(thread_id, user_id)
        self.release_stream.set()
        await self.release_stop.wait()
        return {**result, "status": "stopping", "restored_prompts": []}


def test_rich_stop_race_stream_closes_before_stop_rpc_resolves(tmp_path: Path) -> None:
    """No auto-send while a stop is in flight, even if the stream dies first.

    Backlog #16 race: the halted turn's stream can close BEFORE the stop
    RPC response arrives; the submission chain used to pop and send the
    next queued entry inside that window. The chain now skips its drain
    while runtime._stop_task is pending, and the stop's drain restores the
    entry to the composer once the RPC resolves.
    """

    async def exercise() -> tuple[SlowStopRestoringClient, _RichReplRuntime, Any]:
        client = SlowStopRestoringClient()
        app, runtime, shell, buffer = make_shell_harness(client, tmp_path)

        await app._submit_rich_submission_async(
            ComposerSubmission("slow"), shell.renderer, runtime=runtime
        )
        task = runtime.current_turn_task
        assert task is not None
        for _ in range(10):
            await asyncio.sleep(0)
            if runtime.busy:
                break
        assert runtime.busy is True

        runtime.queue_submission(ComposerSubmission("queued while busy"))
        assert shell._request_stop() is True

        # Let the stop task reach client.stop(): the stream is released
        # (backend abort) while the RPC response stays in flight.
        for _ in range(10):
            await asyncio.sleep(0)
        await task  # the turn ends with the stop still pending

        # The chain must NOT have auto-sent the queued entry in the window.
        assert [request.message for request in client.chat_requests] == ["slow"]

        client.release_stop.set()
        stop_task = runtime._stop_task
        assert stop_task is not None
        await stop_task
        return client, runtime, buffer

    client, runtime, buffer = run(exercise())

    assert [request.message for request in client.chat_requests] == ["slow"]
    assert buffer.text == "queued while busy"
    assert runtime.queued_count == 0
