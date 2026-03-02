"""CLIApp — main REPL loop and orchestrator."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Optional, TYPE_CHECKING

from .state import CLIState
from .commands import CommandRegistry
from .rendering.welcome import render_welcome
from .rendering.stream import StreamRenderer

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent


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
    ) -> None:
        self.state = CLIState(agent, thread_id=thread_id, user_id=user_id)
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
        # Try to use prompt_toolkit; fall back to basic input if unavailable
        try:
            from .input import create_session, get_prompt

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

        events = self.state.agent.stream(
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
