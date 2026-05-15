"""Compatibility shim for the centralized command service."""

from __future__ import annotations

from typing import Any, Optional

from ..core.command_service import CommandContext, get_command_service


class SlashCommandDispatcher:
    """Backwards-compatible adapter for legacy parsed-command callers."""

    def __init__(
        self,
        api: Any,
        thread_id: str,
        user_id: str,
        *,
        source: str = "agent",
    ):
        self.api = api
        self.thread_id = thread_id
        self.user_id = user_id
        self.source = source

    async def dispatch(
        self,
        command: str,
        subcommand: Optional[str],
        args: list[str],
        rest: str,
    ) -> str:
        del args
        raw = f"/{command}"
        if subcommand:
            raw += f" {subcommand}"
        if rest:
            raw += f" {rest}"
        result = await get_command_service().execute(
            CommandContext(
                user_id=self.user_id,
                thread_id=self.thread_id,
                source=self.source,  # type: ignore[arg-type]
            ),
            raw,
            api=self.api,
        )
        return result.markdown
