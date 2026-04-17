"""Single-dispatch tool that lets the agent invoke backend slash commands.

The agent passes a natural command string (e.g. "/config set llm_model
claude-opus-4-6", "/memory save color blue", "/help") and the tool routes
it through the same REST endpoints the Discord/Telegram bots use. Tell
the agent to call `/help` first to see the full command list.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

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


@tool
async def slash_command(
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
    blocked — they would interrupt or destroy the current conversation.

    Args:
        command: The slash command string.

    Returns:
        Plain-text result prefixed with [Success], [Error], or [Info].
    """
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
    api_key = settings.nymeria_api_key or ""
    base_url = _resolve_base_url()

    client = NymeriaAPIClient(base_url=base_url, api_key=api_key)
    dispatcher = SlashCommandDispatcher(api=client, thread_id=thread_id, user_id=user_id)

    logger.info(
        "slash_command: cmd=%s sub=%s args=%s thread=%s",
        cmd, sub, args, thread_id,
    )

    return await dispatcher.dispatch(cmd, sub, args, rest)


SLASH_COMMAND_TOOLS = [slash_command]
