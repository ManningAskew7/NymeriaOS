"""Configuration commands that cannot be generated: /restart.

`/model`, `/models`, `/think`, the `/config` group, and the `/env` group all
moved to `generated_cogs.py`: they were pure defer-and-relay wrappers, so the
registry's declared params now supply their signatures, choices, and admin
flags. `/restart` stays here because restarting the BOT is a local action (it
closes the Discord gateway and the API client and lets the supervisor bring the
process back), not something the backend command can do for us.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


class ConfigCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    @app_commands.command(
        name="restart", description="Restart a Nymeria service"
    )
    @app_commands.describe(target="What to restart (default: bot)")
    @app_commands.choices(
        target=[
            app_commands.Choice(name="bot (Discord bot)", value="bot"),
            app_commands.Choice(name="api (API server)", value="api"),
        ]
    )
    async def cmd_restart(
        self,
        interaction: discord.Interaction,
        target: Optional[app_commands.Choice[str]] = None,
    ):
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return

        target_value = target.value if target else "bot"

        if target_value == "api":
            # API restart is centralized; transient connection errors during
            # the server handoff are expected and suppressed.
            try:
                await self.bot._send_backend_command(
                    interaction, "restart", args="api", require_admin=True
                )
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
            ):
                pass
        else:
            await interaction.response.send_message(
                "Restarting bot... (back in a few seconds)", ephemeral=True
            )
            logger.info("Bot restart requested via /restart command")
            await self.bot._request_self_restart()
