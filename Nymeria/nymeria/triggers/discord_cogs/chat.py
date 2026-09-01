"""Chat commands: /ask, /clear, /compact, /stop."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..discord_bot import (
    compose_incoming_prompt,
    fetch_channel_context,
    is_backend_command,
    make_thread_id,
    sender_display_name,
)

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

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
        if not is_backend_command(message) and self.bot._context_enabled.get(
            interaction.channel_id, True
        ):
            bot_id = self.bot.user.id if self.bot.user else None
            context = await fetch_channel_context(
                interaction.channel, bot_user_id=bot_id
            )
        # An /ask is an interaction, never a channel message, so it cannot
        # appear in the context block: without this the agent has no way at
        # all to tell who asked.
        message_with_context = compose_incoming_prompt(
            message,
            sender_name=sender_display_name(interaction.user),
            context=context,
            is_dm=interaction.guild_id is None,
        )

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
    @app_commands.describe(focus="Optional: what to prioritize in the summary")
    async def cmd_compact(self, interaction: discord.Interaction, focus: str = ""):
        await interaction.response.defer()
        if interaction.channel_id is None or interaction.channel is None:
            return
        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return

        async def _first(content: str) -> discord.Message:
            return await interaction.followup.send(content, wait=True)

        compact_message = f"/compact {focus}".strip()
        await self.bot._stream_to_channel(
            channel=interaction.channel,
            first_send=_first,
            message=compact_message,
            thread_id=thread_id,
            user_id=user_id,
        )

    @app_commands.command(
        name="stop", description="Abort the current running operation"
    )
    async def cmd_stop(self, interaction: discord.Interaction):
        await self.bot._send_backend_command(interaction, "stop")
