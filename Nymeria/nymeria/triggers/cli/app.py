"""CLIApp — main REPL loop and orchestrator."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal, Optional, TYPE_CHECKING

from ...core.stream_bridge import iter_agent_astream
from .capabilities import TerminalCapabilities, detect_terminal_capabilities
from .state import CLIState
from .commands import CommandRegistry
from .rendering.welcome import render_welcome
from .rendering.stream import StreamRenderer
from .transport.in_process import InProcessAgentClient

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent


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
        self.renderer = StreamRenderer(self.state)
        self._register_all_commands()

    def _register_all_commands(self) -> None:
        """Import and register all command modules."""
        from .commands import system, context, threads, model, tools, todos, memory

        system.register(self.registry)
        context.register(self.registry)
        threads.register(self.registry)
        model.register(self.registry)
        tools.register(self.registry)
        todos.register(self.registry)
        memory.register(self.registry)

    def run(self) -> None:
        """Main REPL loop."""
        capabilities = detect_terminal_capabilities(self.runtime_config)
        if capabilities.renderer == "full":
            self._run_full_screen(capabilities)
            return

        self._run_legacy_repl()

    def _run_full_screen(self, capabilities: TerminalCapabilities) -> None:
        """Run the retained full-screen TUI shell."""
        from .rendering.full_screen import (
            FullScreenPromptToolkitShell,
            FullScreenShellConfig,
        )
        from .transport.api import APITransportStartupError, select_agent_client

        async def launch() -> None:
            local_client = InProcessAgentClient(
                self.state.agent,
                default_user_id=self.state.user_id,
            )
            try:
                client = await select_agent_client(
                    self.runtime_config,
                    local_client=local_client,
                )
            except APITransportStartupError as exc:
                self.state.console.print(f"[red]Error: {exc.message}[/red]")
                return

            on_turn_complete = (
                self._maybe_auto_title if client is local_client else None
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
            )
            await shell.run_async()

        asyncio.run(launch())

    def _run_legacy_repl(self) -> None:
        """Run the original prompt_toolkit/Rich REPL."""
        # Try to use prompt_toolkit; fall back to basic input if unavailable
        try:
            from .input import create_session

            data_dir = self.state.settings.data_dir
            session = create_session(data_dir, self.registry)
            use_prompt_toolkit = True
        except ImportError:
            session = None
            use_prompt_toolkit = False

        render_welcome(self.state)

        # patch_stdout intercepts background-thread writes (ticker, watchdog)
        # and redraws the prompt after they finish.
        if use_prompt_toolkit:
            from prompt_toolkit.patch_stdout import patch_stdout

            stdout_ctx = patch_stdout(raw=True)
        else:
            stdout_ctx = nullcontext()

        with stdout_ctx:
            self._repl_loop(session, use_prompt_toolkit)

    def _repl_loop(self, session, use_prompt_toolkit: bool) -> None:
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
                    self.registry.dispatch(self.state, stripped)
                    continue

                # Legacy bare commands (no / prefix)
                lower = stripped.lower()
                if lower in ("exit", "quit", "q"):
                    self.state.running = False
                    self.state.console.print("[dim]Goodbye![/dim]")
                    continue
                if lower == "clear":
                    self.state.console.clear()
                    render_welcome(self.state)
                    continue
                if lower == "help":
                    self.registry.dispatch(self.state, "/help")
                    continue

                # Chat message
                self._send_message(stripped)

            except KeyboardInterrupt:
                # At the prompt, Ctrl+C clears input (prompt_toolkit handles it).
                # If we get here, just continue.
                self.state.console.print()
                continue
            except EOFError:
                self.state.running = False
                self.state.console.print("[dim]Goodbye![/dim]")

    def _send_message(self, message: str) -> None:
        """Send a chat message and render the streaming response."""
        self.state.console.print()

        events = iter_agent_astream(
            self.state.agent,
            message,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
        )
        self.renderer.render_stream(events)

        # Auto-title on first message
        self._maybe_auto_title(message)

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
