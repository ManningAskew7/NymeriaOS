"""CLI package for Nymeria — backward-compatible entry point."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent

from ..base import BaseTrigger


class CLITrigger(BaseTrigger):
    """
    Command-line interface trigger.

    Thin wrapper that delegates to CLIApp for the actual REPL.
    Kept for backward compatibility with ``from nymeria.triggers.cli import CLITrigger``.
    """

    def __init__(self, agent: "NymeriaAgent", thread_id: Optional[str] = None):
        super().__init__(agent)
        self._thread_id = thread_id

    def start(self) -> None:
        from .app import CLIApp

        app = CLIApp(self.agent, thread_id=self._thread_id)
        app.run()

    def stop(self) -> None:
        pass  # Handled by CLIApp internally


def run_cli(
    agent: Optional["NymeriaAgent"] = None,
    thread_id: Optional[str] = None,
) -> None:
    """
    Run the CLI.

    Args:
        agent: Optional agent instance (creates default if not provided).
        thread_id: Optional thread ID for the conversation.
    """
    if agent is None:
        from ...core.agent import NymeriaAgent
        from ...tools import get_all_tools_with_agents

        agent = NymeriaAgent(tools=get_all_tools_with_agents())

    cli = CLITrigger(agent, thread_id=thread_id)
    cli.start()
