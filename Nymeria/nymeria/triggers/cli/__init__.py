"""CLI package for Nymeria — backward-compatible entry point."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent
    from .app import CLIRuntimeConfig


class CLITrigger:
    """
    Command-line interface trigger.

    Thin wrapper that delegates to CLIApp for the actual REPL.
    """

    def __init__(
        self,
        agent: "NymeriaAgent | None",
        thread_id: Optional[str] = None,
        runtime_config: "CLIRuntimeConfig | None" = None,
    ):
        self.agent = agent
        self._thread_id = thread_id
        self._runtime_config = runtime_config

    def start(self) -> None:
        from .app import CLIApp

        app = CLIApp(
            self.agent,
            thread_id=self._thread_id,
            runtime_config=self._runtime_config,
        )
        app.run()


def run_cli(
    agent: Optional["NymeriaAgent"] = None,
    thread_id: Optional[str] = None,
    runtime_config: Any = None,
) -> None:
    """
    Run the CLI.

    Args:
        agent: Optional agent instance (creates default if not provided).
        thread_id: Optional thread ID for the conversation.
        runtime_config: Parsed launch options for future CLI transports/renderers.
    """
    if agent is None and getattr(runtime_config, "transport", "api") == "local":
        from ...core.agent import NymeriaAgent
        from ...tools import ALL_TOOLS

        agent = NymeriaAgent(tools=list(ALL_TOOLS))

    cli = CLITrigger(agent, thread_id=thread_id, runtime_config=runtime_config)
    cli.start()
