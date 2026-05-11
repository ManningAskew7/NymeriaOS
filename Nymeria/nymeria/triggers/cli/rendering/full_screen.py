"""Full-screen prompt_toolkit shell for the CLI/TUI runtime."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Hashable

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from ..events import normalize_stream_event
from ..input import ComposerController, ComposerSubmission, create_full_screen_composer
from ..commands import CommandContext, CommandResult, ListCommandOutputSink
from ..lifecycle import (
    ExitSignalHandlers,
    LifecycleStopResult,
    TurnLifecycleController,
    cancel_task,
    signal_name,
    stream_error_event_from_exception,
)
from ..state import (
    AssistantMessage,
    CLIUIState,
    ResponseStep,
    SystemMessage,
    ThinkingStep,
    ToolCallStep,
    UserMessage,
    create_initial_state,
    reduce_stream_event,
    start_turn,
)
from ..transport.base import AgentClient, Attachment
from ..transport.disconnected import is_disconnected_client
from .indicator import (
    FRAME_INTERVAL_SECONDS,
    ActivityIndicator,
    activity_state_from_ui_state,
)
from .plain import tool_result_summary, truncate_plain
from .status_bar import (
    NoticeLevel,
    StatusBarContext,
    StatusBarRenderer,
    StatusNotice,
)
from .transcript import (
    TranscriptLine,
    TranscriptRenderer,
    TranscriptRenderOptions,
    render_transcript as render_transcript_snapshot,
)

DEFAULT_TRANSCRIPT_WIDTH = 100
TRANSCRIPT_VIEWPORT_CHROME_WIDTH = 4
_STREAM_DONE = object()


@dataclass(frozen=True, slots=True)
class FullScreenShellConfig:
    """Runtime labels used by the full-screen shell."""

    thread_id: str
    user_id: str = "default"
    model: str = ""
    thread_label: str = ""


class FullScreenPromptToolkitShell:
    """Retained prompt_toolkit Application backed by AgentClient events."""

    def __init__(
        self,
        *,
        client: AgentClient,
        capabilities: Any,
        config: FullScreenShellConfig,
        initial_state: CLIUIState | None = None,
        on_turn_complete: Callable[[str], None] | None = None,
        command_registry: Any | None = None,
        history_path: Path | None = None,
    ) -> None:
        self.client = client
        self.capabilities = capabilities
        self.config = config
        self.on_turn_complete = on_turn_complete
        self.command_registry = command_registry
        self.state = initial_state or create_initial_state(
            thread_id=config.thread_id,
            user_id=config.user_id,
        )
        self.indicator = ActivityIndicator()
        self.status_bar_renderer = StatusBarRenderer(indicator=self.indicator)
        self.transcript_renderer = TranscriptRenderer()
        self.transcript_lexer = TranscriptLexer()
        self._transcript_verbose = False
        self._busy = False
        self._status_notice: StatusNotice | None = None
        self._pending_submissions: deque[ComposerSubmission] = deque()
        self._current_turn_task: asyncio.Task[bool] | None = None
        self._autonomous_listener_task: asyncio.Task[None] | None = None
        self._transcript_activity_refresh_task: asyncio.Task[None] | None = None
        self._last_transcript_activity_text = ""
        self._autonomous_client_id = f"cli-{uuid.uuid4().hex}"
        self._application: Application[None] | None = None
        self._lifecycle = TurnLifecycleController(
            client=self.client,
            state_getter=lambda: self.state,
            state_setter=self._replace_state,
            thread_id_getter=lambda: self.config.thread_id,
            user_id_getter=lambda: self.config.user_id,
            is_active=lambda: self._busy,
        )

        self.transcript = TextArea(
            text="",
            read_only=True,
            lexer=self.transcript_lexer,
            scrollbar=True,
            wrap_lines=True,
            focusable=False,
        )
        self.composer_controller: ComposerController = create_full_screen_composer(
            command_registry=command_registry,
            history_path=history_path,
            on_submit=self._handle_composer_submission,
            on_error=self._handle_composer_error,
            on_stop=self._request_stop_from_composer,
            is_busy=lambda: self._busy,
            queued_count=lambda: len(self._pending_submissions),
        )
        self.composer = self.composer_controller.text_area
        self.transcript_frame = Frame(self.transcript, title="Nymeria")
        self.composer_frame = Frame(
            self.composer,
            title="Message",
            style="class:composer.frame",
        )
        self.status_bar = Window(
            FormattedTextControl(self._status_fragments),
            height=1,
            style="class:status",
        )
        self._refresh_transcript()

    def build_application(self) -> Application[None]:
        """Build the prompt_toolkit Application without running it."""

        body = HSplit(
            [
                self.transcript_frame,
                self.status_bar,
                self.composer_frame,
            ]
        )
        app: Application[None] = Application(
            layout=Layout(body, focused_element=self.composer),
            key_bindings=merge_key_bindings(
                [self._key_bindings(), self.composer_controller.key_bindings]
            ),
            full_screen=bool(getattr(self.capabilities, "alt_screen_enabled", True)),
            mouse_support=bool(getattr(self.capabilities, "mouse_enabled", False)),
            color_depth=_color_depth(self.capabilities),
            refresh_interval=FRAME_INTERVAL_SECONDS,
            style=_style(self.capabilities),
        )
        self._application = app
        return app

    async def run_async(self) -> None:
        """Run the full-screen shell until the user exits."""

        app = self._application or self.build_application()
        self._autonomous_listener_task = self._start_autonomous_listener()
        self._transcript_activity_refresh_task = asyncio.create_task(
            self._refresh_live_transcript_activity(),
            name="NymeriaCLITranscriptActivityRefresh",
        )
        try:
            with ExitSignalHandlers(lambda signum: self._schedule_signal_exit(app, signum)):
                await app.run_async()
        finally:
            await self.shutdown_active_turn(reason="shutdown")
            await cancel_task(self._transcript_activity_refresh_task)
            await cancel_task(self._autonomous_listener_task)
            close = getattr(self.client, "close", None)
            if callable(close):
                await close()

    def set_status_notice(
        self,
        message: str,
        *,
        level: NoticeLevel = "info",
        ttl_seconds: float | None = None,
    ) -> None:
        """Set a status notice from the app wrapper."""

        self._set_status_notice(message, level=level, ttl_seconds=ttl_seconds)

    async def run_chat_turn(
        self,
        message: str,
        *,
        attachments: Sequence[Attachment] | None = None,
    ) -> bool:
        """Submit one message and reduce queued stream events into UI state."""

        text = message.strip()
        if not text:
            return False
        if self._busy:
            self._set_status_notice("Turn already in progress", level="warning")
            return False

        self._busy = True
        self._lifecycle.begin_turn()
        self._status_notice = None
        started_message = text
        self.state = start_turn(
            self.state,
            started_message,
            thread_id=self.config.thread_id,
            user_id=self.config.user_id,
            attachments=tuple(dict(item) for item in (attachments or ())),
            now=time.monotonic(),
        )
        self._refresh_transcript()

        queue: asyncio.Queue[Any] = asyncio.Queue()
        publisher = asyncio.create_task(
            self._publish_stream_events(queue, started_message, attachments),
            name="NymeriaCLIFullScreenStream",
        )

        try:
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    break
                self.state = reduce_stream_event(
                    self.state,
                    item,
                    now=time.monotonic(),
                )
                self._refresh_transcript()
        finally:
            self._busy = False
            if not publisher.done():
                publisher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publisher
            self._lifecycle.finish_turn()
            self._refresh_transcript()

        if self.on_turn_complete is not None:
            self.on_turn_complete(started_message)
        return True

    async def _publish_stream_events(
        self,
        queue: asyncio.Queue[Any],
        message: str,
        attachments: Sequence[Attachment] | None,
    ) -> None:
        try:
            async for event in self.client.stream_chat(
                message,
                self.config.thread_id,
                self.config.user_id,
                attachments=attachments,
            ):
                await queue.put(event)
        except Exception as exc:  # noqa: BLE001 - stream errors must render in UI.
            await queue.put(
                stream_error_event_from_exception(
                    exc,
                    thread_id=self.config.thread_id,
                    explicit_stop_requested=self._lifecycle.stop_requested,
                )
            )
        finally:
            await queue.put(_STREAM_DONE)

    def _start_autonomous_listener(self) -> asyncio.Task[None] | None:
        stream = getattr(self.client, "stream_autonomous", None)
        if not callable(stream):
            return None
        if not str(getattr(self.client, "connection_label", "")).startswith("api "):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.create_task(
            self._run_autonomous_listener(),
            name="NymeriaCLIFullScreenAutonomousStream",
        )

    async def _run_autonomous_listener(self) -> None:
        reconnect_delay = 1.0
        max_delay = 30.0
        while True:
            try:
                await self._consume_autonomous_stream()
                reconnect_delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background listener is best-effort.
                self._set_status_notice(
                    f"Autonomous stream disconnected: {exc or exc.__class__.__name__}",
                    level="warning",
                    ttl_seconds=5,
                )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

    async def _consume_autonomous_stream(self) -> None:
        stream = getattr(self.client, "stream_autonomous", None)
        if not callable(stream):
            return
        async for event in stream(
            self.config.user_id,
            client_id=self._autonomous_client_id,
        ):
            self._apply_autonomous_event(event)

    def _apply_autonomous_event(self, event: Any) -> bool:
        normalized = normalize_stream_event(event)
        event_type = getattr(normalized, "type", "")
        event_thread_id = getattr(normalized, "thread_id", None)
        if event_thread_id != self.config.thread_id:
            if event_thread_id is None and event_type == "error":
                message = str(getattr(normalized, "content", "") or "Autonomous stream error.")
                self._set_status_notice(
                    _first_status_line(message),
                    level="warning",
                    ttl_seconds=5,
                )
            return False
        if event_type == "diagnostic":
            return False
        self.state = reduce_stream_event(
            self.state,
            normalized,
            now=time.monotonic(),
        )
        self._refresh_transcript()
        return True

    def _accept_composer_text(self, buffer: Any) -> bool:
        """Compatibility accept hook for tests and staged refactor callers."""

        return self.composer_controller.submit_buffer(buffer)

    def _handle_composer_submission(self, submission: ComposerSubmission) -> bool:
        message = submission.message.strip()
        if message.startswith("/") and self.command_registry is not None:
            return self._handle_command_submission(message)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        if self._current_turn_task is not None and not self._current_turn_task.done():
            self._pending_submissions.append(submission)
            self._set_status_notice(_queued_notice(len(self._pending_submissions)))
            return True

        self._current_turn_task = loop.create_task(
            self._run_submission_chain(submission),
            name="NymeriaCLIFullScreenComposer",
        )
        return True

    def _handle_command_submission(self, raw_input: str) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(
            self._run_command(raw_input),
            name="NymeriaCLIFullScreenCommand",
        )
        return True

    async def _run_command(self, raw_input: str) -> CommandResult:
        output = ListCommandOutputSink()
        context = CommandContext(
            client=self.client,
            output=output,
            dispatch_state=self._dispatch_command_action,
            prompt_handler=lambda prompt: self._prompt_for_input(prompt),
            secret_prompt_handler=lambda prompt: self._prompt_for_input(
                prompt,
                secret=True,
            ),
            thread_id=self.config.thread_id,
            user_id=self.config.user_id,
            metadata={
                "ui_state": self.state,
                "capabilities": self.capabilities,
                "transcript_verbose": self._transcript_verbose,
            },
        )
        result = await self.command_registry.dispatch_async(context, raw_input)
        self._apply_command_result(result, output.messages)
        return result

    async def _dispatch_command_action(self, action: Any) -> None:
        if not isinstance(action, Mapping):
            return
        action_type = action.get("type")
        if action_type == "clear_transcript":
            self._clear_transcript()
        elif action_type == "redraw":
            self._refresh_transcript()
        elif action_type == "switch_thread":
            thread_id = str(action.get("thread_id") or self.config.thread_id)
            thread_label = str(action.get("thread_label") or thread_id)
            model = str(action.get("model") or self.config.model)
            self.config = replace(
                self.config,
                thread_id=thread_id,
                thread_label=thread_label,
                model=model,
            )
            self.state = create_initial_state(
                thread_id=thread_id,
                user_id=self.config.user_id,
                now=time.monotonic(),
            )
            self._refresh_transcript()
        elif action_type == "set_thread_label":
            self.config = replace(
                self.config,
                thread_label=str(action.get("thread_label") or self.config.thread_label),
            )
            self._invalidate()
        elif action_type == "set_model":
            self.config = replace(
                self.config,
                model=str(action.get("model") or self.config.model),
            )
            self._invalidate()
        elif action_type == "set_transcript_verbose":
            self._transcript_verbose = bool(action.get("enabled"))
            self._refresh_transcript()
        elif action_type == "switch_user":
            user_id = str(action.get("user_id") or self.config.user_id)
            self.config = replace(self.config, user_id=user_id)
            self.state = create_initial_state(
                thread_id=self.config.thread_id,
                user_id=user_id,
                now=time.monotonic(),
            )
            self._refresh_transcript()
        elif action_type == "replace_client":
            await self._replace_active_client(action)

    def _apply_command_result(
        self,
        result: CommandResult,
        messages: Sequence[Any],
    ) -> None:
        suppress_transcript = bool(result.payload.get("suppress_transcript"))
        if result.status == "clear":
            self._clear_transcript()
        elif not suppress_transcript:
            self._append_command_messages(messages)

        if messages:
            level = getattr(messages[-1], "level", "info")
            self._set_status_notice(
                _first_status_line(getattr(messages[-1], "content", "")),
                level="error" if level == "error" else "info",
            )
        elif result.status == "clear":
            self._set_status_notice("Cleared.")

        if result.status == "exit" and self._application is not None:
            self._application.exit()

    def _append_command_messages(self, messages: Sequence[Any]) -> None:
        if not messages:
            return
        timestamp = time.monotonic()
        additions: list[SystemMessage] = []
        for message in messages:
            content = str(getattr(message, "content", "") or "").strip()
            if not content:
                continue
            level = str(getattr(message, "level", "info"))
            additions.append(
                SystemMessage(
                    id=f"command-{uuid.uuid4().hex[:8]}",
                    kind="error" if level == "error" else "diagnostic",
                    content=content,
                    timestamp=timestamp,
                    details={
                        "level": level,
                        "title": str(getattr(message, "title", "") or ""),
                    },
                )
            )
        if not additions:
            return
        self.state = replace(
            self.state,
            messages=self.state.messages + tuple(additions),
            updated_at=timestamp,
        )
        self._refresh_transcript()

    def _clear_transcript(self) -> None:
        self.state = create_initial_state(
            thread_id=self.config.thread_id,
            user_id=self.config.user_id,
            now=time.monotonic(),
        )
        self._refresh_transcript()

    async def _run_submission_chain(self, submission: ComposerSubmission) -> bool:
        current: ComposerSubmission | None = submission
        ran_turn = False
        while current is not None:
            ran_turn = await self.run_chat_turn(
                current.message,
                attachments=current.attachments,
            )
            current = self._pending_submissions.popleft() if self._pending_submissions else None
            if current is not None:
                self._set_status_notice(_queued_notice(len(self._pending_submissions)))
        if self._is_queued_status_notice():
            self._status_notice = None
            self._invalidate()
        return ran_turn

    def _handle_composer_error(self, message: str) -> None:
        self._set_status_notice(message, level="warning")

    def _request_stop_from_composer(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(self.stop_current_turn(), name="NymeriaCLIStopTurn")
        return True

    async def stop_current_turn(self) -> bool:
        """Request backend/local cancellation without clearing current input."""

        self._set_status_notice("Stopping active turn...")
        result = await self._lifecycle.request_stop(reason="user")
        if result.status == "no_active_turn":
            self._set_status_notice(result.message)
            return False
        if result.status == "already_stopping":
            self._set_status_notice(result.message)
            return False
        if result.status == "failed":
            self._set_status_notice(f"Stop failed: {result.message}", level="error")
            return False

        self._set_status_notice(result.message)
        return True

    async def _replace_active_client(self, action: Mapping[str, Any]) -> None:
        next_client = action.get("client")
        if next_client is None:
            return
        old_client = self.client
        await cancel_task(self._autonomous_listener_task)
        self._autonomous_listener_task = None
        self.client = next_client
        self._lifecycle.client = next_client
        user_id = str(action.get("user_id") or self.config.user_id)
        self.config = replace(self.config, user_id=user_id)
        if old_client is not next_client:
            close = getattr(old_client, "close", None)
            if callable(close):
                await close()
        if not is_disconnected_client(next_client):
            self._autonomous_listener_task = self._start_autonomous_listener()
        self._invalidate()

    async def _prompt_for_input(self, prompt: str, *, secret: bool = False) -> str:
        def read() -> str:
            if secret:
                return getpass.getpass(prompt)
            return input(prompt)

        return await run_in_terminal(read, render_cli_done=False, in_executor=True)

    async def shutdown_active_turn(
        self,
        *,
        reason: str = "shutdown",
    ) -> LifecycleStopResult | None:
        """Stop backend work and cancel the local stream task during shutdown."""

        result: LifecycleStopResult | None = None
        if self._busy:
            result = await self._lifecycle.request_stop(reason=reason)
            if result.status == "failed":
                self._set_status_notice(
                    f"Stop failed during shutdown: {result.message}",
                    level="error",
                )
        await self._cancel_current_turn()
        return result

    def _set_status_notice(
        self,
        message: str,
        *,
        level: NoticeLevel = "info",
        ttl_seconds: float | None = None,
    ) -> None:
        self._status_notice = StatusNotice(
            message=message,
            level=level,
            created_at=time.monotonic(),
            ttl_seconds=ttl_seconds,
        )
        self._invalidate()

    def _is_queued_status_notice(self) -> bool:
        return bool(
            self._status_notice
            and self._status_notice.message.startswith("Queued message")
        )

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-d")
        def _exit(event: Any) -> None:
            event.app.exit()

        @bindings.add("c-l")
        def _redraw(event: Any) -> None:
            self._refresh_transcript()
            event.app.invalidate()

        return bindings

    async def _cancel_current_turn(self) -> None:
        await cancel_task(self._current_turn_task)

    def _schedule_signal_exit(self, app: Application[None], signum: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            with contextlib.suppress(Exception):
                app.exit()
            return
        loop.create_task(
            self._exit_from_signal(app, signum),
            name=f"NymeriaCLIExit{signal_name(signum)}",
        )

    async def _exit_from_signal(self, app: Application[None], signum: int) -> None:
        self._set_status_notice(f"Exiting on {signal_name(signum)}")
        await self.shutdown_active_turn(reason=f"signal:{signal_name(signum)}")
        with contextlib.suppress(Exception):
            app.exit()

    async def _refresh_live_transcript_activity(self) -> None:
        while True:
            await asyncio.sleep(FRAME_INTERVAL_SECONDS)
            now = time.monotonic()
            if (
                activity_state_from_ui_state(self.state, now=now) is not None
                or self._last_transcript_activity_text
            ):
                self._refresh_transcript(now=now)

    def _replace_state(self, state: CLIUIState) -> None:
        self.state = state
        self._refresh_transcript()

    def _refresh_transcript(self, *, now: float | None = None) -> None:
        current_time = time.monotonic() if now is None else now
        activity_text = _transcript_activity_text(
            self.status_bar_renderer,
            self.state,
            capabilities=self.capabilities,
            busy=self._busy,
            now=current_time,
        )
        width = _transcript_render_width(self.capabilities)
        result = self.transcript_renderer.render_result(
            self.state,
            width=width,
            options=_transcript_options(
                self.capabilities,
                verbose=self._transcript_verbose,
                assistant_activity_label=activity_text,
            ),
        )
        self._last_transcript_activity_text = activity_text
        self.transcript_lexer.set_lines(result.lines)
        self.transcript.text = result.text
        self.transcript.buffer.cursor_position = len(self.transcript.text)
        self._invalidate()

    def _invalidate(self) -> None:
        if self._application is not None:
            self._application.invalidate()

    def _status_fragments(self) -> StyleAndTextTuples:
        return [("class:status", self._status_text())]

    def _status_text(self) -> str:
        width = _render_width(self.capabilities)
        return self.status_bar_renderer.render_text(
            self.state,
            capabilities=self.capabilities,
            context=StatusBarContext(
                connection_label=self.client.connection_label,
                thread_label=self.config.thread_label or self.config.thread_id,
                model=self.config.model,
                cwd=Path.cwd(),
                queued_count=len(self._pending_submissions),
                notice=self._status_notice,
                busy=self._busy,
            ),
            width=width,
            now=time.monotonic(),
        )


class TranscriptLexer(Lexer):
    """prompt_toolkit lexer backed by transcript line kinds."""

    def __init__(self) -> None:
        self._styles: tuple[str, ...] = ()

    def set_lines(self, lines: Sequence[TranscriptLine]) -> None:
        self._styles = tuple(_line_style(line) for line in lines)

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        styles = self._styles

        def get_line(lineno: int) -> StyleAndTextTuples:
            style = styles[lineno] if 0 <= lineno < len(styles) else ""
            try:
                text = document.lines[lineno]
            except IndexError:
                text = ""
            return [(style, text)]

        return get_line

    def invalidation_hash(self) -> Hashable:
        return hash(self._styles)


def render_transcript(state: CLIUIState, *, width: int | None = None) -> str:
    """Render reducer state into a simple full-screen transcript snapshot."""

    return render_transcript_snapshot(state, width=width)


def _legacy_render_transcript(state: CLIUIState, *, width: int | None = None) -> str:
    """Legacy formatter retained for compatibility during staged refactor."""

    render_width = _positive_width(width)
    lines: list[str] = []
    for message in state.messages:
        if isinstance(message, UserMessage):
            lines.extend(_wrapped_prefixed_lines("You", message.content, render_width))
        elif isinstance(message, AssistantMessage):
            lines.extend(_assistant_lines(message, render_width))
        elif isinstance(message, SystemMessage):
            lines.append(_system_line(message, render_width))
    for error in state.errors:
        lines.append(truncate_plain(f"Error: {error.content or error.code}", render_width))
    return "\n".join(lines).strip()


def _assistant_lines(message: AssistantMessage, width: int) -> list[str]:
    lines: list[str] = []
    for step in message.steps:
        if isinstance(step, ThinkingStep):
            label = "Thinking..." if message.status == "streaming" else "Thought"
            lines.append(truncate_plain(label, width))
        elif isinstance(step, ToolCallStep):
            lines.append(tool_result_summary(step, width=width))
        elif isinstance(step, ResponseStep):
            content = step.content.rstrip()
            if content:
                lines.extend(_wrapped_prefixed_lines("Nymeria", content, width))
    if not lines and message.status == "streaming":
        lines.append("Nymeria: ")
    return lines


def _system_line(message: SystemMessage, width: int) -> str:
    if message.kind == "compaction_notice":
        text = "Context compacted."
        if message.context_summary:
            text = f"{text} {message.context_summary}"
        return truncate_plain(text, width)
    if message.kind == "iteration_limit":
        return truncate_plain(message.content or "Reached turn safety limit.", width)
    if message.kind == "error":
        return truncate_plain(f"Error: {message.content}", width)
    return truncate_plain(message.content or message.kind.replace("_", " ").title(), width)


def _wrapped_prefixed_lines(prefix: str, content: str, width: int) -> list[str]:
    text = " ".join(str(content or "").split())
    if not text:
        return [f"{prefix}: "]
    available = max(8, width - len(prefix) - 2)
    words = text.split(" ")
    output: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= available:
            current = candidate
            continue
        if current:
            output.append(current)
        current = word
    if current:
        output.append(current)
    first_prefix = f"{prefix}: "
    continuation_prefix = " " * len(first_prefix)
    return [
        f"{first_prefix if index == 0 else continuation_prefix}{line}"
        for index, line in enumerate(output)
    ]


def _queued_notice(count: int) -> str:
    if count == 1:
        return "Queued message (1)"
    return f"Queued messages ({count})"


def _first_status_line(text: str) -> str:
    line = str(text or "").strip().splitlines()
    return line[0] if line else "Command completed."


def _transcript_options(
    capabilities: Any,
    *,
    verbose: bool,
    assistant_activity_label: str = "",
) -> TranscriptRenderOptions:
    return TranscriptRenderOptions(
        verbose=verbose,
        ascii_only=not bool(getattr(capabilities, "unicode_enabled", False)),
        assistant_activity_label=assistant_activity_label,
    )


def _transcript_activity_text(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    *,
    capabilities: Any,
    busy: bool = False,
    now: float | None = None,
) -> str:
    if activity_state_from_ui_state(state, now=now) is None:
        return ""
    text = renderer.activity_segment(
        state,
        capabilities=capabilities,
        now=now,
        busy=busy,
    )
    if text == "Ready":
        return ""
    return text


def _line_style(line: TranscriptLine) -> str:
    return {
        "user_header": "class:transcript.user.header",
        "user_text": "class:transcript.user.text",
        "assistant_header": "class:transcript.assistant.header",
        "autonomous_header": "class:transcript.autonomous.header",
        "thinking": "class:transcript.thinking",
        "preamble": "class:transcript.preamble",
        "tool": "class:transcript.tool",
        "tool_detail": "class:transcript.tool.detail",
        "final": "class:transcript.final",
        "system": "class:transcript.system",
        "error": "class:transcript.error",
        "artifact": "class:transcript.artifact",
        "diagnostic": "class:transcript.diagnostic",
    }.get(line.kind, "")


def _render_width(capabilities: Any) -> int:
    return _positive_width(getattr(capabilities, "width", DEFAULT_TRANSCRIPT_WIDTH))


def _transcript_render_width(capabilities: Any) -> int:
    return max(20, _render_width(capabilities) - TRANSCRIPT_VIEWPORT_CHROME_WIDTH)


def _positive_width(width: int | None) -> int:
    if width is None:
        return DEFAULT_TRANSCRIPT_WIDTH
    try:
        parsed = int(width)
    except (TypeError, ValueError):
        return DEFAULT_TRANSCRIPT_WIDTH
    return max(20, parsed)


def _color_depth(capabilities: Any) -> ColorDepth:
    if not bool(getattr(capabilities, "color_enabled", False)):
        return ColorDepth.DEPTH_1_BIT
    depth = int(getattr(capabilities, "color_depth", 0) or 0)
    if depth >= 256:
        return ColorDepth.DEPTH_8_BIT
    if depth >= 24:
        return ColorDepth.DEPTH_24_BIT
    return ColorDepth.DEPTH_4_BIT


def _style(capabilities: Any) -> Style:
    if not bool(getattr(capabilities, "color_enabled", False)):
        return Style.from_dict({"status": ""})
    return Style.from_dict(
        {
            "frame.label": "ansicyan bold",
            "frame.border": "ansibrightblack",
            "status": "ansibrightblack",
            "composer": "ansicyan",
            "composer.busy": "ansiyellow",
            "composer.queued": "ansiyellow",
            "composer.error": "ansired",
            "composer.frame": "",
            "transcript.user.header": "ansicyan bold",
            "transcript.user.text": "",
            "transcript.assistant.header": "ansigreen bold",
            "transcript.autonomous.header": "ansimagenta bold",
            "transcript.thinking": "#c0c0c0",
            "transcript.preamble": "",
            "transcript.tool": "ansiyellow",
            "transcript.tool.detail": "ansibrightblack",
            "transcript.final": "",
            "transcript.system": "ansibrightblack",
            "transcript.error": "ansired",
            "transcript.artifact": "ansicyan",
            "transcript.diagnostic": "ansibrightblack",
        }
    )


__all__ = [
    "FullScreenPromptToolkitShell",
    "FullScreenShellConfig",
    "render_transcript",
]
