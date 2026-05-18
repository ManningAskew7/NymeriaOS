"""Chat commands: /ask, /clear, /compact, /stop."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..discord_bot import make_thread_id, fetch_channel_context

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


class ChatCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    @app_commands.command(name="ask", description="Send a message to Nymeria")
    @app_commands.describe(message="Your message to Nymeria")
    async def cmd_ask(self, interaction: discord.Interaction, message: str):
        await interaction.response.defer()
        if interaction.channel_id is None:
            return
        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        context = ""
        if self.bot._context_enabled.get(interaction.channel_id, True):
            bot_id = self.bot.user.id if self.bot.user else None
            context = await fetch_channel_context(
                interaction.channel, bot_user_id=bot_id
            )
        message_with_context = f"{context}{message}" if context else message

        async def _first(content: str) -> discord.Message:
            return await interaction.followup.send(content, wait=True)

        await self.bot._stream_to_channel(
            channel=interaction.channel,
            first_send=_first,
            message=message_with_context,
            thread_id=thread_id,
            user_id=user_id,
        )

    @app_commands.command(
        name="clear",
        description="Clear conversation history (preserves notepad + tool config)",
    )
    async def cmd_clear(self, interaction: discord.Interaction):
        await self.bot._send_backend_command(interaction, "clear")

    @app_commands.command(
        name="compact",
        description="Compress conversation to save context window",
    )
    async def cmd_compact(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.channel_id is None:
            return
        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            result = await self.bot.api.compact(thread_id, user_id)
            if result.get("success"):
                removed = result.get("messages_removed", 0)
                await interaction.followup.send(
                    f"Compacted conversation: {removed} messages summarized.",
                    ephemeral=True,
                )
            else:
                reason = result.get("reason", "Unknown reason")
                await interaction.followup.send(
                    f"Compaction skipped: {reason}", ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error compacting: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(
        name="stop", description="Abort the current running operation"
    )
    async def cmd_stop(self, interaction: discord.Interaction):
        await self.bot._send_backend_command(interaction, "stop")
