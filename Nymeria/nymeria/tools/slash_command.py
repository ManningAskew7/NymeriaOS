"""Single-dispatch tool that lets the agent invoke backend slash commands."""

from __future__ import annotations

import asyncio
import threading
from typing import Annotated, Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool

from ..core.command_service import CommandContext, get_command_service
from .utils import get_thread_id, get_user_id


async def _dispatch_command(command: str, config: RunnableConfig) -> str:
    """Shared slash-command execution logic for sync and async tool paths."""
    thread_id = get_thread_id(config)
    user_id = get_user_id(config)
    result = await get_command_service().execute(
        CommandContext(user_id=user_id, thread_id=thread_id, source="agent"),
        command,
    )
    return result.markdown


def _run_async_from_sync(coro_factory: Callable[[], Awaitable[str]]) -> str:
    """Run async slash-command dispatch from sync tool contexts.

    Some tool callers still need a synchronous return value. Provide a sync
    wrapper here so slash_command works in both interactive and autonomous
    execution modes.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    result: dict[str, str] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error["value"] = exc

    thread = threading.Thread(
        target=_runner,
        name="slash-command-sync-bridge",
        daemon=True,
    )
    thread.start()
    thread.join()

    if "value" in error:
        raise error["value"]
    return result["value"]


def _slash_command_sync(
    command: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Invoke a Nymeria slash command on your own thread.

    Use this to inspect or modify your own backend state: LLM model,
    reasoning effort, tool set, memories, TODOs, env vars, notepad, and
    general status. Uses the same commands the Discord/Telegram bots
    expose to users.

    ALWAYS call `/help` first to see the full list of commands and their
    exact syntax. Pass natural command strings with or without the
    leading slash.

    Examples:
        /help
        /status
        /config show
        /config set llm_model claude-opus-4-6
        /env get PERPLEXITY_API_KEY
        /memory save color "deep blue"
        /tools enable browser
        /todos add Check logs | 2h | daily
        /notepad write replace:new notepad contents

    Destructive commands (/ask, /stop, /clear, /compact, /restart) are
    blocked because they would interrupt or destroy the current conversation.

    Args:
        command: The slash command string.

    Returns:
        Markdown command result.
    """
    return _run_async_from_sync(lambda: _dispatch_command(command, config))


async def _slash_command_async(
    command: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Async slash-command tool path for API/SSE chat execution."""
    return await _dispatch_command(command, config)


slash_command = StructuredTool.from_function(
    func=_slash_command_sync,
    coroutine=_slash_command_async,
    name="slash_command",
)


SLASH_COMMAND_TOOLS = [slash_command]
