"""CLIApp — main REPL loop and orchestrator."""

from __future__ import annotations

import asyncio
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, TYPE_CHECKING

from .capabilities import TerminalCapabilities, detect_terminal_capabilities
from .commands import (
    CommandContext,
    CommandMessage,
    CommandRegistry,
    RichConsoleCommandOutputSink,
)
from .rendering.welcome import render_welcome
from .rendering.plain import PlainRenderer, strip_ansi
from .rendering.rich_repl import RichReplRenderer
from .state import CLIState
from .transport.base import AgentClient
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

    transport: TransportMode = "auto"
    renderer: RendererMode = "auto"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    user_id: str = "default"
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


class CLIApp:
    """
    The main CLI application.

    Manages the REPL loop, dispatching commands and chat messages.
    """

    def __init__(
        self,
        agent: "NymeriaAgent",
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
        self._register_all_commands()

    def _register_all_commands(self) -> None:
        """Import and register all command modules."""
        from .commands import (
            account,
            activity,
            artifacts,
            context,
            doctor,
            mcp,
            memory,
            model,
            skills,
            system,
            threads,
            todos,
            tools,
            triggers,
        )

        system.register(self.registry)
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
            )
            await shell.run_async()

        asyncio.run(launch())

    def _run_legacy_repl(self) -> None:
        """Compatibility wrapper for callers that still expect the REPL method."""

        self._run_repl(detect_terminal_capabilities(self.runtime_config))

    def _run_repl(self, capabilities: TerminalCapabilities) -> None:
        """Run the prompt_toolkit REPL with reducer-backed rich/plain rendering."""
        from .transport.api import APITransportStartupError

        try:
            self._client = asyncio.run(self._select_agent_client())
        except APITransportStartupError as exc:
            self._render_startup_error(exc.message, capabilities)
            return

        renderer = self._create_repl_renderer(capabilities)

        # Try to use prompt_toolkit; fall back to basic input if unavailable
        try:
            from .input import create_session

            data_dir = self.state.settings.data_dir
            session = create_session(data_dir, self.registry)
            use_prompt_toolkit = True
        except ImportError:
            session = None
            use_prompt_toolkit = False

        if capabilities.renderer != "plain":
            render_welcome(self.state)

        # patch_stdout intercepts background-thread writes (ticker, watchdog)
        # and redraws the prompt after they finish.
        if use_prompt_toolkit:
            from prompt_toolkit.patch_stdout import patch_stdout

            stdout_ctx = patch_stdout(raw=True)
        else:
            stdout_ctx = nullcontext()

        try:
            with stdout_ctx:
                self._repl_loop(
                    session,
                    use_prompt_toolkit,
                    capabilities,
                    renderer,
                )
        finally:
            asyncio.run(self._close_selected_client())

    async def _select_agent_client(self) -> AgentClient:
        from .transport.api import select_agent_client

        self._local_client = InProcessAgentClient(
            self.state.agent,
            default_user_id=self.state.user_id,
        )
        return await select_agent_client(
            self.runtime_config,
            local_client=self._local_client,
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
        return RichReplRenderer(capabilities=capabilities, width=width)

    def _repl_loop(
        self,
        session,
        use_prompt_toolkit: bool,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
    ) -> None:
        """Inner REPL loop, run inside patch_stdout context."""
        if use_prompt_toolkit:
            from .input import get_prompt

        while self.state.running:
            try:
                if use_prompt_toolkit:
                    prompt = get_prompt(self.state)
                    user_input = session.prompt(prompt)
                else:
                    thread_label = self.state.get_thread_title()
                    user_input = self.state.console.input(
                        f"[bold cyan]nymeria[/bold cyan] [dim][{thread_label}][/dim] > "
                    )

                if not user_input.strip():
                    continue

                stripped = user_input.strip()

                # Dispatch /commands
                if stripped.startswith("/"):
                    self._dispatch_command(stripped, capabilities, renderer)
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
                        render_welcome(self.state)
                    continue
                if lower == "help":
                    self._dispatch_command("/help", capabilities, renderer)
                    continue

                # Chat message
                self._send_message(stripped, renderer)

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
    ) -> None:
        """Dispatch one slash command through the v2 command context."""

        context = CommandContext(
            client=self._client,
            output=self._command_output_sink(capabilities),
            dispatch_state=self._dispatch_repl_action,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
            metadata={
                "capabilities": capabilities,
                "ui_state": getattr(renderer, "state", None),
            },
        )
        result = asyncio.run(self.registry.dispatch_async(context, raw_input))
        self._apply_repl_command_result(result, capabilities)

    def _command_output_sink(self, capabilities: TerminalCapabilities):
        if capabilities.renderer == "plain":
            return PlainCommandOutputSink()
        return RichConsoleCommandOutputSink(self.state.console)

    async def _dispatch_repl_action(self, action: Any) -> None:
        if not isinstance(action, dict):
            return
        action_type = action.get("type")
        if action_type == "switch_thread":
            thread_id = str(action.get("thread_id") or self.state.thread_id)
            self.state.switch_thread(thread_id)
        elif action_type == "switch_user":
            user_id = str(action.get("user_id") or self.state.user_id)
            self.state.user_id = user_id
            if self._local_client is not None:
                self._local_client.default_user_id = user_id
        elif action_type in {"set_thread_label", "set_model", "thread_config_updated"}:
            return
        elif action_type == "clear_transcript":
            self.state.console.clear()
        elif action_type == "redraw":
            return

    def _apply_repl_command_result(
        self,
        result: "CommandResult",
        capabilities: TerminalCapabilities,
    ) -> None:
        if result.status == "exit":
            self.state.running = False
        elif result.status == "clear" and capabilities.renderer != "plain":
            self.state.console.clear()
            render_welcome(self.state)

    def _send_message(self, message: str, renderer: _ReplRenderer) -> None:
        """Send a chat message through the selected client and render it."""

        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")

        if not isinstance(renderer, PlainRenderer):
            self.state.console.print()

        renderer.start_turn(
            message,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
        )
        try:
            asyncio.run(self._render_message_stream(message, renderer))
        except KeyboardInterrupt:
            self._stop_current_turn()
            self._render_cancelled(renderer)
            return

        # Auto-title on first message
        if self._client is self._local_client:
            self._maybe_auto_title(message)

    async def _render_message_stream(
        self,
        message: str,
        renderer: _ReplRenderer,
    ) -> None:
        assert self._client is not None
        events = self._client.stream_chat(
            message,
            self.state.thread_id,
            self.state.user_id,
        )
        await renderer.render_async_events(events)

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
