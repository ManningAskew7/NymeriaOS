"""The plain (sync-loop) REPL must serve command prompts through the async
prompt path.

``CLIApp._dispatch_command`` runs a slash command under ``asyncio.run``; a
command that awaits ``context.prompt`` (``/login``'s token prompt) therefore
runs INSIDE a live event loop, where prompt_toolkit's synchronous
``session.prompt`` refuses with "asyncio.run() cannot be called from a running
event loop" (first public-beta Windows test, 2026-09-07; reproduced on Linux
with ``--renderer plain``). The rich follow-footer shell already used the async
twin; this pins the sync loop to it as well.
"""

from __future__ import annotations

from typing import Any

from cli_fixtures import FakeTerminalCapabilities

from nymeria.triggers.cli.app import CLIApp
from nymeria.triggers.cli.commands.base import Command, CommandContext, CommandResult
from nymeria.triggers.cli.rendering.plain import PlainRenderer


class _LoopBoundSession:
    """prompt_toolkit-shaped session: the sync ``prompt`` refuses inside a
    running loop exactly the way the real one does; ``prompt_async`` works."""

    def __init__(self) -> None:
        self.sync_calls = 0
        self.async_calls: list[tuple[Any, bool]] = []
        # prompt_toolkit persists every prompt kwarg on the session
        # (PromptSession.prompt_async: `if is_password is not None:
        # self.is_password = is_password`), so a secret prompt that is not
        # undone masks every later prompt of the session as asterisks.
        self.is_password: bool = False

    def prompt(self, message: Any, **kwargs: Any) -> str:
        self.sync_calls += 1
        raise RuntimeError("asyncio.run() cannot be called from a running event loop")

    async def prompt_async(self, message: Any, **kwargs: Any) -> str:
        if kwargs.get("is_password") is not None:
            self.is_password = bool(kwargs["is_password"])
        self.async_calls.append((message, self.is_password))
        return "tok-123" if self.is_password else "http://example.test"


def test_plain_repl_dispatch_serves_command_prompts_inside_the_dispatch_loop():
    app = CLIApp(None, thread_id="thread-1")
    seen: list[str] = []

    async def probe(context: CommandContext, args: list[str]) -> CommandResult:
        seen.append(await context.prompt("API token: ", secret=True))
        seen.append(await context.prompt("API URL: "))
        return CommandResult()

    app.registry.register(Command(name="probe", description="prompt probe", handler=probe))
    session = _LoopBoundSession()
    capabilities = FakeTerminalCapabilities(width=100, renderer="plain")

    app._dispatch_command("/probe", capabilities, PlainRenderer(), session=session)

    # Both prompts were answered (the command did not die on the nested loop)...
    assert seen == ["tok-123", "http://example.test"]
    # ...through prompt_async, with the secret flag carried, and never the sync API.
    assert session.sync_calls == 0
    assert [is_password for _message, is_password in session.async_calls] == [True, False]
    # ...and the secret prompt did not leave the session masking the REPL's
    # own next prompt (the sync loop passes no is_password of its own).
    assert session.is_password is False


def test_secret_prompt_does_not_leave_the_session_masked():
    app = CLIApp(None, thread_id="thread-1")

    async def probe(context: CommandContext, args: list[str]) -> CommandResult:
        await context.prompt("API token: ", secret=True)
        return CommandResult()

    app.registry.register(Command(name="probe", description="prompt probe", handler=probe))
    session = _LoopBoundSession()
    app._dispatch_command(
        "/probe", FakeTerminalCapabilities(width=100, renderer="plain"), PlainRenderer(), session=session
    )
    assert session.async_calls == [("API token: ", True)]
    assert session.is_password is False
