"""Single-dispatch tool that lets the agent invoke backend slash commands.

The agent passes a natural command string (e.g. "/config set llm_model
claude-opus-4-6", "/memory save color blue", "/help") and the tool routes
it through the same REST endpoints the Discord/Telegram bots use. Tell
the agent to call `/help` first to see the full command list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import threading
from pathlib import Path
from typing import Annotated, Awaitable, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool

from .utils import get_thread_id, get_user_id

# NymeriaAPIClient and SlashCommandDispatcher are imported lazily inside
# slash_command() — their parent package (nymeria.triggers) imports from
# nymeria.tools, which would otherwise cause a circular import at module
# load time.

logger = logging.getLogger(__name__)

# Commands that take a subcommand as the second token.
GROUPED = {"config", "env", "tools", "memory", "notepad", "todos"}


def _resolve_base_url() -> str:
    """Pick the right local API URL for the current environment."""
    explicit = os.environ.get("NYMERIA_API_URL")
    if explicit:
        return explicit.rstrip("/")
    if Path("/.dockerenv").exists():
        return "http://api:8000"
    return "http://localhost:8000"


def _parse(raw: str) -> tuple[Optional[str], Optional[str], list[str], str]:
    """Parse a command string.

    Returns (command, subcommand, args, rest) where:
      command:    lowercased primary command (no slash), e.g. "config"
      subcommand: lowercased subcommand for grouped commands, else None
      args:       shlex-split argument tokens after the subcommand
      rest:       raw remainder after the subcommand (preserves pipes)
    """
    s = raw.strip()
    if not s:
        return None, None, [], ""
    if s.startswith("/"):
        s = s[1:].lstrip()
    if not s:
        return None, None, [], ""

    head, _, tail = s.partition(" ")
    command = head.lower()

    if command in GROUPED:
        tail = tail.strip()
        if not tail:
            return command, None, [], ""
        sub_head, _, sub_tail = tail.partition(" ")
        subcommand = sub_head.lower()
        rest = sub_tail.strip()
    else:
        subcommand = None
        rest = tail.strip()

    try:
        args = shlex.split(rest, posix=True) if rest else []
    except ValueError:
        # Unbalanced quotes etc — fall back to whitespace split.
        args = rest.split()

    return command, subcommand, args, rest


async def _dispatch_command(command: str, config: RunnableConfig) -> str:
    """Shared slash-command execution logic for sync and async tool paths."""
    cmd, sub, args, rest = _parse(command)
    if cmd is None:
        return "[Error]: Empty command. Try /help."

    thread_id = get_thread_id(config)
    user_id = get_user_id(config)

    # Lazy imports — see module docstring.
    from ..triggers.discord_api_client import NymeriaAPIClient
    from ..triggers.slash_dispatcher import SlashCommandDispatcher
    from ..config import get_settings

    settings = get_settings()
    # Use the admin-role service token for internal API calls — slash
    # commands run on behalf of the current thread's user, so we act-as
    # that user via headers inside NymeriaAPIClient on each call.
    api_key = settings.nymeria_service_token
    if not api_key:
        return (
            "[Error]: slash_command requires NYMERIA_SERVICE_TOKEN to be set "
            "(it authenticates as the admin service account and acts-as the "
            "current user). Ask an administrator to provision the bot-service "
            "admin and paste its token into the server's environment."
        )
    base_url = _resolve_base_url()

    client = NymeriaAPIClient(base_url=base_url, api_key=api_key)
    dispatcher = SlashCommandDispatcher(api=client, thread_id=thread_id, user_id=user_id)

    logger.info(
        "slash_command: cmd=%s sub=%s args=%s thread=%s",
        cmd, sub, args, thread_id,
    )

    return await dispatcher.dispatch(cmd, sub, args, rest)


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
        Plain-text result prefixed with [Success], [Error], or [Info].
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
