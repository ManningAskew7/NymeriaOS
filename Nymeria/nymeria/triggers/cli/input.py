"""prompt_toolkit integration: history, autocomplete, keybindings."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

if TYPE_CHECKING:
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent
    from .state import CLIState
    from .commands import CommandRegistry


class CommandCompleter(Completer):
    """Tab-completes all registered /commands and subcommands."""

    def __init__(self, registry: "CommandRegistry") -> None:
        self._registry = registry

    def get_completions(
        self, document: "Document", complete_event: "CompleteEvent"
    ):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        candidates = self._registry.get_completions()

        # If the text already has a space, we're completing subcommands
        if " " in text:
            for candidate in candidates:
                if candidate.startswith(text) and candidate != text:
                    yield Completion(
                        candidate,
                        start_position=-len(text),
                    )
        else:
            for candidate in candidates:
                if candidate.startswith(text) and candidate != text:
                    yield Completion(
                        candidate,
                        start_position=-len(text),
                    )


def create_session(
    data_dir: Path, registry: "CommandRegistry"
) -> PromptSession:
    """Create a PromptSession with history, autocomplete, and keybindings."""
    history_path = data_dir / "cli_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    bindings = KeyBindings()

    @bindings.add("c-c")
    def _clear_input(event):
        """Ctrl+C clears the current input instead of exiting."""
        event.current_buffer.reset()

    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=CommandCompleter(registry),
        key_bindings=bindings,
        complete_while_typing=False,
    )


def get_prompt(state: "CLIState") -> HTML:
    """Build the dynamic prompt string."""
    thread_label = state.get_thread_title()
    return HTML(
        f"<style fg='ansicyan' bg=''>nymeria</style>"
        f" <style fg='ansibrightblack'>[{thread_label}]</style>"
        f" <style fg='ansicyan' bg=''>&gt;</style> "
    )
