"""CLIApp — main REPL loop and orchestrator."""

from __future__ import annotations

import asyncio
import getpass
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TYPE_CHECKING

from .capabilities import TerminalCapabilities, detect_terminal_capabilities
from .commands import (
    CommandContext,
    ListCommandOutputSink,
    CommandMessage,
    CommandRegistry,
    RichConsoleCommandOutputSink,
)
from .autonomous import AutonomousStreamMonitor
from .header import CLIHeaderSnapshot, build_header_snapshot, concise_connection_label
from .rendering.indicator import FRAME_INTERVAL_SECONDS
from .rendering.status_bar import StatusBarContext, StatusBarRenderer, StatusNotice
from .rendering.welcome import render_welcome
from .rendering.plain import PlainRenderer, strip_ansi
from .rendering.rich_repl import RichReplRenderer
from .state import CLIState, create_initial_state
from .theme import CLITheme, DEFAULT_CLI_THEME, load_cli_theme, ptk_style
from .transport.base import AgentClient, Attachment
from .transport.disconnected import DISCONNECTED_MESSAGE, is_disconnected_client
from .transport.in_process import InProcessAgentClient

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent
    from .commands import CommandResult


TransportMode = Literal["api", "local", "auto"]
RendererMode = Literal["full", "rich", "plain", "auto"]
ColorMode = Literal["auto", "always", "never"]


@dataclass(frozen=True, slots=True)
class CLIRuntimeConfig:
    """Launch-time CLI options shared by future transport and renderer tasks."""

    transport: TransportMode = "api"
    renderer: RendererMode = "auto"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    user_id: str = "default"
    user_id_explicit: bool = False
    alt_screen: bool = True
    animation: bool = True
    ascii_only: bool = False
    color: ColorMode = "auto"


class _ReplRenderer(Protocol):
    def start_turn(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        attachments: Any = None,
        now: float | None = None,
    ) -> Any:
        """Start a visible chat turn."""

    async def render_async_events(
        self,
        events: Any,
        *,
        now: float | None = None,
    ) -> Any:
        """Render an async normalized event stream."""


class PlainCommandOutputSink:
    """Plain output adapter for slash-command results in non-Rich modes."""

    def __init__(self, file: Any = None) -> None:
        self.file = file or sys.stderr

    def emit(self, message: CommandMessage) -> None:
        content = strip_ansi(str(message.content or ""))
        if content:
            self.file.write(f"{content}\n")
            self.file.flush()


class _RichReplRuntime:
    """Runtime services that only exist for the scrollback-native Rich REPL."""

    def __init__(
        self,
        *,
        app: "CLIApp",
        renderer: RichReplRenderer,
        capabilities: TerminalCapabilities,
    ) -> None:
        self.app = app
        self.renderer = renderer
        self.capabilities = capabilities
        self.status_bar_renderer = StatusBarRenderer()
        self.render_lock = threading.RLock()
        self.application: Any | None = None
        self.composer_controller: Any | None = None
        self._busy = False
        self._status_notice: StatusNotice | None = None
        self._autonomous_client_id = f"cli-{uuid.uuid4().hex}"
        self._autonomous_task: asyncio.Task[None] | None = None
        self._pending_submissions: deque[Any] = deque()
        self.current_turn_task: asyncio.Task[bool] | None = None
        self._autonomous_monitor = AutonomousStreamMonitor(
            client_getter=lambda: self.app._client,
            user_id_getter=lambda: self.app.state.user_id,
            thread_id_getter=lambda: self.app.state.thread_id,
            client_id=self._autonomous_client_id,
            apply_event=self._apply_autonomous_event,
            set_notice=lambda message, level, ttl_seconds: self.set_status_notice(
                message,
                level=level,
                ttl_seconds=ttl_seconds,
            ),
        )

    @property
    def busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.invalidate()

    @property
    def queued_count(self) -> int:
        return len(self._pending_submissions)

    def queue_submission(self, submission: Any) -> None:
        self._pending_submissions.append(submission)
        self.set_status_notice(_queued_notice(len(self._pending_submissions)))

    def next_queued_submission(self) -> Any | None:
        if not self._pending_submissions:
            return None
        return self._pending_submissions.popleft()

    def clear_queued_notice_if_idle(self) -> None:
        if (
            self._status_notice is not None
            and self._status_notice.message.startswith("Queued message")
        ):
            self._status_notice = None
            self.invalidate()

    def set_status_notice(
        self,
        message: str,
        *,
        level: str = "info",
        ttl_seconds: float | None = None,
    ) -> None:
        self._status_notice = StatusNotice(
            message=message,
            level=level if level in {"info", "warning", "error"} else "info",
            created_at=time.monotonic(),
            ttl_seconds=ttl_seconds,
        )
        self.invalidate()

    def bind_application(self, application: Any, composer_controller: Any) -> None:
        self.application = application
        self.composer_controller = composer_controller

    def bottom_toolbar(self):
        """Return prompt_toolkit toolbar fragments for the live Rich prompt."""

        return list(self.status_fragments())

    def status_fragments(self):
        return list(
            self.status_bar_renderer.render_fragments(
                self.renderer.state,
                capabilities=self.capabilities,
                context=self._status_context(),
                width=self.terminal_width(),
                now=time.monotonic(),
            )
        )

    def prompt_fragments(self, prompt: str | None = None):
        label = str(prompt or ("Busy: " if self._busy else "You: "))
        prompt_style = "class:prompt.busy" if self._busy else "class:prompt"
        return list(self.status_fragments()) + [("", "\n"), (prompt_style, label)]

    def status_text(self) -> str:
        return self.status_bar_renderer.render_text(
            self.renderer.state,
            capabilities=self.capabilities,
            context=self._status_context(),
            width=self.terminal_width(),
            now=time.monotonic(),
        )

    def _status_context(self) -> StatusBarContext:
        return StatusBarContext(
            connection_label=self.app._status_connection_label(),
            thread_label=self._thread_label(),
            model=self._model_label(),
            cwd=Path.cwd(),
            queued_count=len(self._pending_submissions),
            notice=self._status_notice,
            busy=self._busy,
        )

    def prompt_kwargs(self) -> dict[str, Any]:
        return {
            "refresh_interval": FRAME_INTERVAL_SECONDS,
            "style": _repl_prompt_style(self.capabilities, theme=self.app.theme),
        }

    def _thread_label(self) -> str:
        if self.app._repl_thread_label:
            return self.app._repl_thread_label
        with suppress(Exception):
            return self.app.state.get_thread_title()
        return self.app.state.thread_id

    def _model_label(self) -> str:
        if self.app._repl_model_label:
            return self.app._repl_model_label
        with suppress(Exception):
            return self.app.state.get_effective_model()
        return ""

    def terminal_width(self) -> int:
        app = self.application
        if app is not None:
            with suppress(Exception):
                return max(1, int(app.output.get_size().columns))
        return max(1, int(getattr(self.capabilities, "width", 80) or 80))

    def start_autonomous_listener(self) -> None:
        if self._autonomous_task is not None and not self._autonomous_task.done():
            return
        if not self._autonomous_monitor.can_start():
            return
        with suppress(RuntimeError):
            self._autonomous_task = asyncio.create_task(
                self._run_autonomous_listener(),
                name="NymeriaCLIRichAutonomousStream",
            )

    async def stop_autonomous_listener_async(self) -> None:
        task = self._autonomous_task
        self._autonomous_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def stop_autonomous_listener(self) -> None:
        task = self._autonomous_task
        self._autonomous_task = None
        if task is not None and not task.done():
            task.cancel()

    def restart_autonomous_listener(self) -> None:
        self.stop_autonomous_listener()
        self.start_autonomous_listener()

    async def render_above_prompt(self, callback: Callable[[], Any]) -> Any:
        """Run terminal output above the fixed Rich status/composer area."""

        def run_locked() -> Any:
            with self.render_lock:
                return callback()

        app = self.application
        if app is not None and getattr(app, "is_running", False):
            from prompt_toolkit.application import run_in_terminal

            result = run_in_terminal(run_locked, render_cli_done=False)
            return await result
        return run_locked()

    async def render_event_above_prompt(self, event: Any) -> None:
        await self.render_above_prompt(
            lambda: self.renderer.render_event(event, now=time.monotonic())
        )

    async def redraw(self) -> None:
        """Clear visible terminal cells, replay reducer transcript, and repaint."""

        def repaint() -> None:
            self._clear_prompt_toolkit_screen()
            self.renderer.reset_state(self.renderer.state)
            self.renderer.render_state()

        await self.render_above_prompt(repaint)
        self.invalidate()

    def _clear_prompt_toolkit_screen(self) -> None:
        app = self.application
        if app is None:
            self.app.state.console.clear()
            return
        try:
            renderer = app.renderer
            output = renderer.output
            output.reset_attributes()
            output.erase_screen()
            output.cursor_goto(0, 0)
            output.flush()
            renderer.reset(leave_alternate_screen=False)
        except Exception:  # noqa: BLE001 - redraw recovery is best effort.
            self.app.state.console.clear()

    def invalidate(self) -> None:
        app = self.application
        if app is not None:
            with suppress(Exception):
                app.invalidate()

    def exit(self) -> None:
        app = self.application
        if app is not None:
            with suppress(Exception):
                app.exit()

    async def _run_autonomous_listener(self) -> None:
        await self._autonomous_monitor.run_forever()

    async def _consume_autonomous_stream(self) -> None:
        await self._autonomous_monitor.consume_once()

    async def _apply_autonomous_event(self, normalized: Any) -> bool:
        await self.render_event_above_prompt(normalized)
        return True


class _RichReplPromptToolkitShell:
    """Non-full-screen prompt_toolkit shell for Rich REPL status/composer."""

    def __init__(
        self,
        *,
        cli_app: "CLIApp",
        runtime: _RichReplRuntime,
        renderer: _ReplRenderer,
        capabilities: TerminalCapabilities,
        history_path: Path,
    ) -> None:
        self.cli_app = cli_app
        self.runtime = runtime
        self.renderer = renderer
        self.capabilities = capabilities
        self.history_path = history_path
        self.composer_controller: Any | None = None
        self.application: Any | None = None

    def build_application(self) -> Any:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        from .input import create_rich_repl_composer

        controller = create_rich_repl_composer(
            command_registry=self.cli_app.registry,
            history_path=self.history_path,
            cwd=Path.cwd(),
            on_submit=self._handle_submission,
            on_error=self._handle_composer_error,
            on_stop=self._request_stop,
            is_busy=lambda: self.runtime.busy,
            queued_count=lambda: self.runtime.queued_count,
        )
        status_bar = Window(
            FormattedTextControl(self.runtime.status_fragments),
            height=1,
            style="class:status",
            wrap_lines=False,
        )
        body = HSplit([status_bar, controller.text_area])
        bindings = KeyBindings()

        @bindings.add("c-d")
        def _exit(event: Any) -> None:
            self.cli_app.state.running = False
            event.app.exit()

        @bindings.add("c-l")
        def _redraw(event: Any) -> None:
            event.app.create_background_task(self.runtime.redraw())

        app = Application(
            layout=Layout(body, focused_element=controller.text_area),
            key_bindings=merge_key_bindings([bindings, controller.key_bindings]),
            full_screen=False,
            mouse_support=False,
            refresh_interval=FRAME_INTERVAL_SECONDS,
            style=_repl_prompt_style(self.capabilities, theme=self.cli_app.theme),
        )
        self.composer_controller = controller
        self.application = app
        self.runtime.bind_application(app, controller)
        return app

    async def run_async(self) -> None:
        app = self.application or self.build_application()
        self.runtime.start_autonomous_listener()
        try:
            await app.run_async()
        finally:
            await self.runtime.stop_autonomous_listener_async()
            task = self.runtime.current_turn_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _handle_submission(self, submission: Any) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(
            self._handle_submission_async(submission),
            name="NymeriaCLIRichComposerSubmission",
        )
        return True

    async def _handle_submission_async(self, submission: Any) -> None:
        message = str(getattr(submission, "message", "") or "").strip()
        if not message:
            return
        if message.startswith("/"):
            await self.cli_app._dispatch_command_async(
                message,
                self.capabilities,
                self.renderer,
                runtime=self.runtime,
            )
            return

        lower = message.lower()
        if lower in {"exit", "quit", "q"}:
            self.cli_app.state.running = False
            await self.runtime.render_above_prompt(
                lambda: self.cli_app.state.console.print("[dim]Goodbye![/dim]")
            )
            self.runtime.exit()
            return
        if lower == "clear":
            self.cli_app._reset_active_repl_state()
            await self.cli_app._refresh_header_snapshot_async(self.capabilities)
            await self.runtime.render_above_prompt(self.cli_app.state.console.clear)
            if self.capabilities.renderer != "plain":
                await self.runtime.render_above_prompt(
                    lambda: self.cli_app._render_current_header(self.capabilities)
                )
            return
        if lower == "help":
            await self.cli_app._dispatch_command_async(
                "/help",
                self.capabilities,
                self.renderer,
                runtime=self.runtime,
            )
            return

        await self.cli_app._submit_rich_submission_async(
            submission,
            self.renderer,
            runtime=self.runtime,
        )

    def _handle_composer_error(self, message: str) -> None:
        self.runtime.set_status_notice(message, level="warning")

    def _request_stop(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(
            self._stop_current_turn(),
            name="NymeriaCLIRichStopTurn",
        )
        return True

    async def _stop_current_turn(self) -> None:
        await self.cli_app._stop_current_turn_async()
        self.runtime.set_status_notice("Stop requested")


class CLIApp:
    """
    The main CLI application.

    Manages the REPL loop, dispatching commands and chat messages.
    """

    def __init__(
        self,
        agent: "NymeriaAgent | None",
        thread_id: Optional[str] = None,
        user_id: str = "default",
        runtime_config: CLIRuntimeConfig | None = None,
    ) -> None:
        self.runtime_config = runtime_config or CLIRuntimeConfig(user_id=user_id)
        self.state = CLIState(
            agent,
            thread_id=thread_id,
            user_id=self.runtime_config.user_id,
        )
        self.registry = CommandRegistry()
        self._local_client: InProcessAgentClient | None = None
        self._client: AgentClient | None = None
        self._active_repl_renderer: _ReplRenderer | None = None
        self._active_rich_runtime: _RichReplRuntime | None = None
        self._active_capabilities: TerminalCapabilities | None = None
        self._repl_thread_label: str | None = None
        self._repl_model_label: str | None = None
        self._header_snapshot: CLIHeaderSnapshot | None = None
        self._header_refresh_pending = False
        self.theme = load_cli_theme()
        self._register_all_commands()

    def _register_all_commands(self) -> None:
        """Import and register all command modules."""
        from .commands import (
            account,
            activity,
            artifacts,
            connection,
            context,
            doctor,
            mcp,
            memory,
            model,
            skills,
            system,
            theme,
            threads,
            todos,
            tools,
            triggers,
        )

        system.register(self.registry)
        connection.register(self.registry)
        context.register(self.registry)
        threads.register(self.registry)
        model.register(self.registry)
        tools.register(self.registry)
        skills.register(self.registry)
        mcp.register(self.registry)
        todos.register(self.registry)
        memory.register(self.registry)
        account.register(self.registry)
        triggers.register(self.registry)
        activity.register(self.registry)
        artifacts.register(self.registry)
        doctor.register(self.registry)
        theme.register(self.registry)

    def run(self) -> None:
        """Main REPL loop."""
        capabilities = detect_terminal_capabilities(self.runtime_config)
        if capabilities.renderer == "full":
            self._run_full_screen(capabilities)
            return

        self._run_repl(capabilities)

    def _run_full_screen(self, capabilities: TerminalCapabilities) -> None:
        """Run the retained full-screen TUI shell."""
        from .rendering.full_screen import (
            FullScreenPromptToolkitShell,
            FullScreenShellConfig,
        )
        from .transport.api import APITransportStartupError

        async def launch() -> None:
            try:
                client = await self._select_agent_client()
            except APITransportStartupError as exc:
                self.state.console.print(f"[red]Error: {exc.message}[/red]")
                return

            on_turn_complete = (
                self._maybe_auto_title if client is self._local_client else None
            )
            shell = FullScreenPromptToolkitShell(
                client=client,
                capabilities=capabilities,
                config=FullScreenShellConfig(
                    thread_id=self.state.thread_id,
                    user_id=self.state.user_id,
                    model=self.state.get_effective_model(),
                    thread_label=self.state.get_thread_title(),
                ),
                on_turn_complete=on_turn_complete,
                command_registry=self.registry,
                history_path=self.state.settings.data_dir / "cli_history",
                theme=self.theme,
            )
            if is_disconnected_client(client):
                shell.set_status_notice(DISCONNECTED_MESSAGE, level="warning")
            await shell.run_async()

        asyncio.run(launch())

    def _run_legacy_repl(self) -> None:
        """Compatibility wrapper for callers that still expect the REPL method."""

        self._run_repl(detect_terminal_capabilities(self.runtime_config))

    def _run_repl(self, capabilities: TerminalCapabilities) -> None:
        """Run the prompt_toolkit REPL with reducer-backed rich/plain rendering."""
        from .transport.api import APITransportStartupError

        self._active_capabilities = capabilities
        try:
            self._client = asyncio.run(self._select_agent_client())
        except APITransportStartupError as exc:
            self._render_startup_error(exc.message, capabilities)
            self._active_capabilities = None
            return

        # Try to use prompt_toolkit; fall back to basic input if unavailable
        try:
            from .input import create_session

            data_dir = self.state.settings.data_dir
            session = create_session(
                data_dir,
                self.registry,
                erase_when_done=capabilities.renderer == "rich",
            )
            use_prompt_toolkit = True
        except ImportError:
            session = None
            use_prompt_toolkit = False

        if capabilities.renderer != "plain":
            self._refresh_header_snapshot(capabilities)
            render_welcome(
                self.state,
                self._header_snapshot,
                theme=self.theme,
                capabilities=capabilities,
            )
        self._render_disconnected_notice(self._client, capabilities)

        # patch_stdout intercepts background-thread writes (ticker, watchdog)
        # and redraws the prompt after they finish.
        if use_prompt_toolkit:
            from prompt_toolkit.patch_stdout import patch_stdout

            stdout_ctx = patch_stdout(raw=True)
        else:
            stdout_ctx = nullcontext()

        renderer: _ReplRenderer | None = None
        runtime: _RichReplRuntime | None = None
        try:
            with stdout_ctx:
                renderer = self._create_repl_renderer(capabilities)
                if isinstance(renderer, RichReplRenderer):
                    runtime = _RichReplRuntime(
                        app=self,
                        renderer=renderer,
                        capabilities=capabilities,
                    )
                    if is_disconnected_client(self._client):
                        runtime.set_status_notice(
                            DISCONNECTED_MESSAGE,
                            level="warning",
                        )
                self._active_repl_renderer = renderer
                self._active_rich_runtime = runtime
                if use_prompt_toolkit and runtime is not None:
                    shell = _RichReplPromptToolkitShell(
                        cli_app=self,
                        runtime=runtime,
                        renderer=renderer,
                        capabilities=capabilities,
                        history_path=self.state.settings.data_dir / "cli_history",
                    )
                    asyncio.run(
                        shell.run_async()
                    )
                else:
                    self._repl_loop(
                        session,
                        use_prompt_toolkit,
                        capabilities,
                        renderer,
                        runtime=runtime,
                    )
        finally:
            if runtime is not None:
                runtime.stop_autonomous_listener()
            self._active_capabilities = None
            self._active_rich_runtime = None
            self._active_repl_renderer = None
            asyncio.run(self._close_selected_client())

    async def _select_agent_client(self) -> AgentClient:
        from .transport.api import APITransportStartupError, DEFAULT_API_URL, select_agent_client

        local_client = None
        if self.runtime_config.transport == "local":
            if self.state.agent is None:
                raise APITransportStartupError(
                    "Local transport requested without a NymeriaAgent.",
                    code="local_transport_unavailable",
                    api_url=DEFAULT_API_URL,
                )
            self._local_client = InProcessAgentClient(
                self.state.agent,
                default_user_id=self.state.user_id,
            )
            local_client = self._local_client
        selected = await select_agent_client(
            self.runtime_config,
            local_client=local_client,
        )
        self._apply_selected_client_user(selected)
        return selected

    def _status_connection_label(self) -> str:
        return concise_connection_label(self._header_snapshot, self._client)

    def _render_current_header(self, capabilities: TerminalCapabilities | None = None) -> None:
        selected_capabilities = capabilities or self._active_capabilities
        render_welcome(
            self.state,
            self._header_snapshot,
            theme=self.theme,
            capabilities=selected_capabilities,
        )

    def _refresh_header_snapshot(
        self,
        capabilities: TerminalCapabilities | None = None,
    ) -> CLIHeaderSnapshot | None:
        if self._client is None:
            self._header_snapshot = None
            return None
        snapshot = asyncio.run(
            build_header_snapshot(
                self.state,
                self._client,
                runtime_config=self.runtime_config,
            )
        )
        self._header_snapshot = snapshot
        self._header_refresh_pending = False
        runtime = self._active_rich_runtime
        if runtime is not None:
            runtime.invalidate()
        return snapshot

    async def _refresh_header_snapshot_async(
        self,
        capabilities: TerminalCapabilities | None = None,
    ) -> CLIHeaderSnapshot | None:
        del capabilities
        if self._client is None:
            self._header_snapshot = None
            return None
        snapshot = await build_header_snapshot(
            self.state,
            self._client,
            runtime_config=self.runtime_config,
        )
        self._header_snapshot = snapshot
        self._header_refresh_pending = False
        runtime = self._active_rich_runtime
        if runtime is not None:
            runtime.invalidate()
        return snapshot

    def _mark_header_refresh_pending(self) -> None:
        self._header_refresh_pending = True

    def _refresh_and_render_pending_header(
        self,
        capabilities: TerminalCapabilities,
    ) -> None:
        if not self._header_refresh_pending or capabilities.renderer == "plain":
            return
        self._refresh_header_snapshot(capabilities)
        self._render_current_header(capabilities)

    async def _refresh_and_render_pending_header_async(
        self,
        runtime: _RichReplRuntime,
    ) -> None:
        if not self._header_refresh_pending or runtime.capabilities.renderer == "plain":
            return
        await self._refresh_header_snapshot_async(runtime.capabilities)
        await runtime.render_above_prompt(
            lambda: self._render_current_header(runtime.capabilities)
        )

    async def _close_selected_client(self) -> None:
        client = self._client
        self._client = None
        close = getattr(client, "close", None)
        if callable(close):
            await close()

    def _create_repl_renderer(
        self,
        capabilities: TerminalCapabilities,
    ) -> _ReplRenderer:
        width = getattr(capabilities, "width", None)
        if capabilities.renderer == "plain":
            return PlainRenderer(width=width)
        return RichReplRenderer(
            capabilities=capabilities,
            width=width,
            theme=self.theme,
        )

    async def _repl_loop_async(
        self,
        session: Any,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        runtime: _RichReplRuntime,
    ) -> None:
        """Async Rich REPL loop that keeps the prompt/status bar active."""

        try:
            while self.state.running:
                try:
                    user_input = await session.prompt_async(
                        runtime.prompt_fragments,
                        **runtime.prompt_kwargs(),
                    )

                    if not user_input.strip():
                        continue

                    stripped = user_input.strip()

                    if stripped.startswith("/"):
                        await self._dispatch_command_async(
                            stripped,
                            capabilities,
                            renderer,
                            session=session,
                            runtime=runtime,
                        )
                        continue

                    lower = stripped.lower()
                    if lower in ("exit", "quit", "q"):
                        self.state.running = False
                        self.state.console.print("[dim]Goodbye![/dim]")
                        continue
                    if lower == "clear":
                        self.state.console.clear()
                        if capabilities.renderer != "plain":
                            await self._refresh_header_snapshot_async(capabilities)
                            self._render_current_header(capabilities)
                        self._reset_active_repl_state()
                        continue
                    if lower == "help":
                        await self._dispatch_command_async(
                            "/help",
                            capabilities,
                            renderer,
                            session=session,
                            runtime=runtime,
                        )
                        continue

                    await self._submit_repl_message_async(
                        stripped,
                        renderer,
                        runtime=runtime,
                    )

                except KeyboardInterrupt:
                    if runtime.busy:
                        await self._stop_current_turn_async()
                        runtime.set_status_notice("Stop requested")
                    else:
                        self.state.console.print()
                    continue
                except EOFError:
                    self.state.running = False
                    self.state.console.print("[dim]Goodbye![/dim]")
        finally:
            task = runtime.current_turn_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _repl_loop(
        self,
        session,
        use_prompt_toolkit: bool,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        """Inner REPL loop, run inside patch_stdout context."""
        if use_prompt_toolkit:
            from .input import get_prompt

        while self.state.running:
            try:
                if use_prompt_toolkit:
                    prompt = get_prompt(
                        self.state,
                        busy=bool(runtime and runtime.busy),
                        theme=self.theme,
                    )
                    prompt_kwargs = runtime.prompt_kwargs() if runtime else {}
                    user_input = session.prompt(prompt, **prompt_kwargs)
                else:
                    user_input = self.state.console.input(
                        "[bold cyan]You:[/bold cyan] "
                    )

                if not user_input.strip():
                    continue

                stripped = user_input.strip()

                # Dispatch /commands
                if stripped.startswith("/"):
                    self._dispatch_command(
                        stripped,
                        capabilities,
                        renderer,
                        session=session if use_prompt_toolkit else None,
                    )
                    continue

                # Legacy bare commands (no / prefix)
                lower = stripped.lower()
                if lower in ("exit", "quit", "q"):
                    self.state.running = False
                    self.state.console.print("[dim]Goodbye![/dim]")
                    continue
                if lower == "clear":
                    self.state.console.clear()
                    if capabilities.renderer != "plain":
                        self._refresh_header_snapshot(capabilities)
                        self._render_current_header(capabilities)
                    continue
                if lower == "help":
                    self._dispatch_command("/help", capabilities, renderer)
                    continue

                # Chat message
                self._submit_repl_message(stripped, renderer, runtime=runtime)

            except KeyboardInterrupt:
                # At the prompt, Ctrl+C clears input (prompt_toolkit handles it).
                # If we get here, just continue.
                self.state.console.print()
                continue
            except EOFError:
                self.state.running = False
                self.state.console.print("[dim]Goodbye![/dim]")

    def _dispatch_command(
        self,
        raw_input: str,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        session: Any | None = None,
    ) -> None:
        """Dispatch one slash command through the v2 command context."""

        context = CommandContext(
            client=self._client,
            output=self._command_output_sink(capabilities),
            dispatch_state=self._dispatch_repl_action,
            prompt_handler=lambda prompt: self._prompt_for_input(
                prompt,
                session=session,
            ),
            secret_prompt_handler=lambda prompt: self._prompt_for_input(
                prompt,
                secret=True,
                session=session,
            ),
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
            metadata={
                "capabilities": capabilities,
                "ui_state": getattr(renderer, "state", None),
                "transcript_verbose": bool(
                    getattr(renderer, "transcript_verbose", False)
                ),
            },
        )
        result = asyncio.run(self.registry.dispatch_async(context, raw_input))
        self._apply_repl_command_result(result, capabilities)

    async def _dispatch_command_async(
        self,
        raw_input: str,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        *,
        session: Any | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        """Dispatch one slash command from the async Rich prompt loop."""

        output_sink = (
            ListCommandOutputSink()
            if runtime is not None and capabilities.renderer != "plain"
            else self._command_output_sink(capabilities)
        )
        context = CommandContext(
            client=self._client,
            output=output_sink,
            dispatch_state=self._dispatch_repl_action,
            prompt_handler=lambda prompt: self._prompt_for_input_async(
                prompt,
                session=session,
                runtime=runtime,
            ),
            secret_prompt_handler=lambda prompt: self._prompt_for_input_async(
                prompt,
                secret=True,
                session=session,
                runtime=runtime,
            ),
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
            metadata={
                "capabilities": capabilities,
                "ui_state": getattr(renderer, "state", None),
                "transcript_verbose": bool(
                    getattr(renderer, "transcript_verbose", False)
                ),
            },
        )
        result = await self.registry.dispatch_async(context, raw_input)
        if runtime is not None:
            await self._apply_repl_command_result_async(result, capabilities, runtime)
        else:
            self._apply_repl_command_result(result, capabilities)
        if runtime is not None and isinstance(output_sink, ListCommandOutputSink):
            await self._render_command_messages_above_prompt(output_sink.messages, runtime)
        if runtime is not None:
            await self._refresh_and_render_pending_header_async(runtime)

    def _command_output_sink(self, capabilities: TerminalCapabilities):
        if capabilities.renderer == "plain":
            return PlainCommandOutputSink()
        return RichConsoleCommandOutputSink(self.state.console)

    async def _dispatch_repl_action(self, action: Any) -> None:
        if not isinstance(action, dict):
            return
        action_type = action.get("type")
        refresh_header = False
        if action_type == "switch_thread":
            thread_id = str(action.get("thread_id") or self.state.thread_id)
            self.state.switch_thread(thread_id)
            self._repl_thread_label = str(action.get("thread_label") or thread_id)
            if action.get("model"):
                self._repl_model_label = str(action.get("model"))
            self._reset_active_repl_state()
            refresh_header = True
        elif action_type == "switch_user":
            user_id = str(action.get("user_id") or self.state.user_id)
            self.state.user_id = user_id
            self._repl_thread_label = None
            self._repl_model_label = None
            if self._local_client is not None:
                self._local_client.default_user_id = user_id
            self.runtime_config = replace(
                self.runtime_config,
                user_id=user_id,
                user_id_explicit=True,
            )
            self._reset_active_repl_state()
            refresh_header = True
        elif action_type == "replace_client":
            await self._replace_repl_client(action)
            runtime = self._active_rich_runtime
            if runtime is not None:
                if is_disconnected_client(self._client):
                    runtime.set_status_notice(
                        DISCONNECTED_MESSAGE,
                        level="warning",
                    )
                runtime.restart_autonomous_listener()
            refresh_header = True
        elif action_type == "set_thread_label":
            self._repl_thread_label = str(
                action.get("thread_label")
                or self._repl_thread_label
                or self.state.thread_id
            )
            refresh_header = True
        elif action_type == "set_model":
            self._repl_model_label = str(action.get("model") or "")
            refresh_header = True
        elif action_type == "thread_config_updated":
            refresh_header = True
        elif action_type in {
            "thread_metadata_updated",
            "thread_context_updated",
            "tools_updated",
            "skills_updated",
            "mcp_updated",
            "triggers_updated",
        }:
            refresh_header = True
        elif action_type == "set_transcript_verbose":
            renderer = self._active_repl_renderer
            if renderer is not None and hasattr(renderer, "transcript_verbose"):
                setattr(renderer, "transcript_verbose", bool(action.get("enabled")))
        elif action_type == "theme_updated":
            theme = action.get("theme")
            if isinstance(theme, CLITheme):
                self.theme = theme
                renderer = self._active_repl_renderer
                if renderer is not None and hasattr(renderer, "set_theme"):
                    renderer.set_theme(theme)
                runtime = self._active_rich_runtime
                if runtime is not None and runtime.application is not None:
                    runtime.application.style = _repl_prompt_style(
                        runtime.capabilities,
                        theme=theme,
                    )
                    runtime.invalidate()
                refresh_header = True
        elif action_type == "clear_transcript":
            self._reset_active_repl_state()
            runtime = self._active_rich_runtime
            if runtime is not None:
                await runtime.render_above_prompt(self.state.console.clear)
            else:
                self.state.console.clear()
            refresh_header = True
        elif action_type == "redraw":
            runtime = self._active_rich_runtime
            if runtime is not None:
                await runtime.redraw()
            return
        if refresh_header:
            self._mark_header_refresh_pending()

    def _apply_repl_command_result(
        self,
        result: "CommandResult",
        capabilities: TerminalCapabilities,
    ) -> None:
        if result.status == "exit":
            self.state.running = False
        elif result.status == "clear" and capabilities.renderer != "plain":
            self.state.console.clear()
            self._refresh_header_snapshot(capabilities)
            self._render_current_header(capabilities)
        self._refresh_and_render_pending_header(capabilities)

    async def _apply_repl_command_result_async(
        self,
        result: "CommandResult",
        capabilities: TerminalCapabilities,
        runtime: _RichReplRuntime,
    ) -> None:
        if result.status == "exit":
            self.state.running = False
            runtime.exit()
            return
        if result.status == "clear" and capabilities.renderer != "plain":
            self._reset_active_repl_state()

            def clear_and_welcome() -> None:
                self.state.console.clear()
                self._render_current_header(capabilities)

            await self._refresh_header_snapshot_async(capabilities)
            await runtime.render_above_prompt(clear_and_welcome)
            return

    async def _render_command_messages_above_prompt(
        self,
        messages: Sequence[CommandMessage],
        runtime: _RichReplRuntime,
    ) -> None:
        if not messages:
            return

        def render_messages() -> None:
            sink = RichConsoleCommandOutputSink(self.state.console)
            for message in messages:
                sink.emit(message)

        await runtime.render_above_prompt(render_messages)

    def _submit_repl_message(
        self,
        raw_input: str,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        from .input import parse_composer_submission

        submission = parse_composer_submission(raw_input, cwd=Path.cwd())
        if submission.attachment_errors:
            message = submission.attachment_errors[0]
            if runtime is not None:
                runtime.set_status_notice(message, level="warning")
            elif isinstance(renderer, PlainRenderer):
                sys.stderr.write(f"{message}\n")
                sys.stderr.flush()
            else:
                self.state.console.print(f"[yellow]{message}[/yellow]")
            return
        self._send_message(
            submission.message,
            renderer,
            attachments=submission.attachments,
            runtime=runtime,
        )

    async def _submit_repl_message_async(
        self,
        raw_input: str,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime,
    ) -> None:
        from .input import parse_composer_submission

        submission = parse_composer_submission(raw_input, cwd=Path.cwd())
        if submission.attachment_errors:
            runtime.set_status_notice(submission.attachment_errors[0], level="warning")
            return

        await self._submit_rich_submission_async(
            submission,
            renderer,
            runtime=runtime,
        )

    async def _submit_rich_submission_async(
        self,
        submission: Any,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime,
    ) -> None:
        active_task = runtime.current_turn_task
        if runtime.busy or (active_task is not None and not active_task.done()):
            runtime.queue_submission(submission)
            return

        runtime.current_turn_task = asyncio.create_task(
            self._run_rich_submission_chain(
                submission,
                renderer,
                runtime=runtime,
            ),
            name="NymeriaCLIRichSubmission",
        )

    async def _run_rich_submission_chain(
        self,
        submission: Any,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime,
    ) -> bool:
        current = submission
        ran_turn = False
        try:
            while current is not None:
                ran_turn = await self._send_message_async(
                    current.message,
                    renderer,
                    attachments=current.attachments,
                    runtime=runtime,
                )
                current = runtime.next_queued_submission()
        finally:
            runtime.clear_queued_notice_if_idle()
            runtime.current_turn_task = None
        return ran_turn

    def _send_message(
        self,
        message: str,
        renderer: _ReplRenderer,
        *,
        attachments: Sequence[Attachment] | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        """Send a chat message through the selected client and render it."""

        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")

        render_context = runtime.render_lock if runtime is not None else nullcontext()
        if runtime is not None:
            runtime.set_busy(True)
        try:
            with render_context:
                if not isinstance(renderer, PlainRenderer):
                    self.state.console.print()

                renderer.start_turn(
                    message,
                    thread_id=self.state.thread_id,
                    user_id=self.state.user_id,
                    attachments=attachments,
                )
                try:
                    asyncio.run(
                        self._render_message_stream(
                            message,
                            renderer,
                            attachments=attachments,
                        )
                    )
                except KeyboardInterrupt:
                    self._stop_current_turn()
                    self._render_cancelled(renderer)
                    return
        finally:
            if runtime is not None:
                runtime.set_busy(False)

        # Auto-title on first message
        if self._client is self._local_client:
            self._maybe_auto_title(message)

    async def _send_message_async(
        self,
        message: str,
        renderer: _ReplRenderer,
        *,
        attachments: Sequence[Attachment] | None = None,
        runtime: _RichReplRuntime,
    ) -> bool:
        """Send a chat message while the Rich prompt remains active."""

        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")

        runtime.set_busy(True)
        try:
            def start_visible_turn() -> None:
                if not isinstance(renderer, PlainRenderer):
                    self.state.console.print()
                renderer.start_turn(
                    message,
                    thread_id=self.state.thread_id,
                    user_id=self.state.user_id,
                    attachments=attachments,
                )

            await runtime.render_above_prompt(start_visible_turn)
            await self._render_message_stream(
                message,
                renderer,
                attachments=attachments,
                runtime=runtime,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the interactive prompt alive.
            runtime.set_status_notice(
                f"Stream failed: {exc or exc.__class__.__name__}",
                level="error",
            )
            return False
        finally:
            runtime.set_busy(False)

        if self._client is self._local_client:
            self._maybe_auto_title(message)
        return True

    async def _render_message_stream(
        self,
        message: str,
        renderer: _ReplRenderer,
        *,
        attachments: Sequence[Attachment] | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        assert self._client is not None
        events = self._client.stream_chat(
            message,
            self.state.thread_id,
            self.state.user_id,
            attachments=attachments,
        )
        if runtime is not None:
            async for event in events:
                await runtime.render_event_above_prompt(event)
            return
        await renderer.render_async_events(events)

    def _reset_active_repl_state(self) -> None:
        renderer = self._active_repl_renderer
        if renderer is None or not hasattr(renderer, "reset_state"):
            return
        renderer.reset_state(
            create_initial_state(
                thread_id=self.state.thread_id,
                user_id=self.state.user_id,
                now=time.monotonic(),
            )
        )

    def _stop_current_turn(self) -> None:
        if self._client is None:
            return

        async def stop() -> None:
            assert self._client is not None
            await self._client.stop(self.state.thread_id, self.state.user_id)

        try:
            asyncio.run(stop())
        except Exception:  # noqa: BLE001 - cancellation feedback is best effort.
            return

    async def _stop_current_turn_async(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop(self.state.thread_id, self.state.user_id)
        except Exception:  # noqa: BLE001 - cancellation feedback is best effort.
            return

    def _render_cancelled(self, renderer: _ReplRenderer) -> None:
        if isinstance(renderer, PlainRenderer):
            sys.stderr.write("Cancelled.\n")
            sys.stderr.flush()
            return
        self.state.console.print("[dim]Cancelled.[/dim]")

    def _render_startup_error(
        self,
        message: str,
        capabilities: TerminalCapabilities,
    ) -> None:
        if capabilities.renderer == "plain":
            sys.stderr.write(f"Error: {strip_ansi(message)}\n")
            sys.stderr.flush()
            return
        self.state.console.print(f"[red]Error: {message}[/red]")

    def _maybe_auto_title(self, first_message: str) -> None:
        """Auto-generate a thread title if this looks like the first message."""
        store = self.state.thread_metadata_manager.get_store(self.state.user_id)
        meta = store.threads.get(self.state.thread_id)
        if meta and meta.title_source != "default":
            return  # Already titled

        # Use the first message as a rough title
        title = first_message[:50].strip()
        if len(first_message) > 50:
            title += "..."

        self.state.thread_metadata_manager.upsert_thread(
            self.state.user_id,
            self.state.thread_id,
            title=title,
            title_source="auto",
        )

    async def _replace_repl_client(self, action: Any) -> None:
        next_client = action.get("client") if isinstance(action, dict) else None
        if next_client is None:
            return
        old_client = self._client
        self._client = next_client
        self._apply_selected_client_user(next_client, action.get("user_id"))
        if old_client is not None and old_client is not next_client:
            close = getattr(old_client, "close", None)
            if callable(close):
                await close()

    def _apply_selected_client_user(
        self,
        client: Any,
        user_id: Any | None = None,
    ) -> None:
        selected_user_id = str(
            user_id
            or getattr(client, "default_user_id", None)
            or self.state.user_id
            or "default"
        )
        self.state.user_id = selected_user_id
        if self._local_client is not None:
            self._local_client.default_user_id = selected_user_id
        self.runtime_config = replace(
            self.runtime_config,
            user_id=selected_user_id,
            user_id_explicit=True,
        )

    def _prompt_for_input(
        self,
        prompt: str,
        *,
        secret: bool = False,
        session: Any | None = None,
    ) -> str:
        if session is not None:
            return session.prompt(prompt, is_password=secret)
        if secret:
            return getpass.getpass(prompt)
        return self.state.console.input(prompt)

    async def _prompt_for_input_async(
        self,
        prompt: str,
        *,
        secret: bool = False,
        session: Any | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> str:
        if session is not None:
            prompt_kwargs = runtime.prompt_kwargs() if runtime is not None else {}
            prompt_message = (
                runtime.prompt_fragments(prompt)
                if runtime is not None
                else prompt
            )
            return await session.prompt_async(
                prompt_message,
                is_password=secret,
                **prompt_kwargs,
            )
        if runtime is not None and runtime.application is not None:
            from prompt_toolkit.application import run_in_terminal

            def read_prompt() -> str:
                if secret:
                    return getpass.getpass(prompt)
                return input(prompt)

            return str(await run_in_terminal(read_prompt, in_executor=True))
        if secret:
            return await asyncio.to_thread(getpass.getpass, prompt)
        return await asyncio.to_thread(self.state.console.input, prompt)

    def _render_disconnected_notice(
        self,
        client: AgentClient | None,
        capabilities: TerminalCapabilities,
    ) -> None:
        if not is_disconnected_client(client):
            return
        startup_error = str(getattr(client, "startup_error", "") or "")
        message = startup_error or DISCONNECTED_MESSAGE
        if capabilities.renderer == "plain":
            sys.stderr.write(f"{strip_ansi(message)}\n")
            sys.stderr.flush()
            return
        self.state.console.print(f"[yellow]{message}[/yellow]")


def _repl_prompt_style(
    capabilities: TerminalCapabilities,
    *,
    theme: CLITheme | None = None,
):
    from prompt_toolkit.styles import Style

    selected_theme = theme or DEFAULT_CLI_THEME
    style_keys = _repl_prompt_style_dict(selected_theme)
    if not bool(getattr(capabilities, "color_enabled", False)):
        return Style.from_dict({key: "" for key in style_keys})
    return Style.from_dict(style_keys)


def _repl_prompt_style_dict(theme: CLITheme) -> dict[str, str]:
    return {
        "status": ptk_style(theme, "status_fg", bg_slot="status_bg"),
        "status.separator": ptk_style(theme, "separator", bg_slot="status_bg"),
        "status.accent": ptk_style(theme, "status_accent", bg_slot="status_bg"),
        "status.spinner": ptk_style(theme, "spinner", bg_slot="status_bg"),
        "status.notice.warning": ptk_style(
            theme,
            "prompt_busy",
            bg_slot="status_bg",
        ),
        "status.notice.error": ptk_style(theme, "error", bg_slot="status_bg"),
        "prompt": ptk_style(theme, "prompt", bold=True),
        "prompt.busy": ptk_style(theme, "prompt_busy", bold=True),
        "prompt.error": ptk_style(theme, "prompt_error", bold=True),
        "composer": ptk_style(theme, "prompt", bold=True),
        "composer.busy": ptk_style(theme, "prompt_busy", bold=True),
        "composer.error": ptk_style(theme, "prompt_error", bold=True),
        "composer.queued": ptk_style(theme, "prompt_busy", bold=True),
        "text-area": "",
        "text-area.prompt": "",
    }


def _queued_notice(count: int) -> str:
    if count == 1:
        return "Queued message (1)"
    return f"Queued messages ({count})"
