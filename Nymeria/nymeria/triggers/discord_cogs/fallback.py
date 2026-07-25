"""LLM fallback-consent commands: the /fallback group.

Each subcommand forwards to the backend ``/fallback`` command family via
``_send_backend_command``, mirroring the hooks cog. The consent prompt copy
advertises ``/fallback approve <id> [minutes|permanent]`` as the buttonless
resolve path, so the family must be reachable as Discord slash commands too.
The approve/deny/revert handlers are human-only backend-side (agent-blocked
in the command registry).
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot


class FallbackCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    fallback_group = app_commands.Group(
        name="fallback",
        description="LLM fallback chain, consent prompts, and active swaps",
    )

    @fallback_group.command(
        name="status", description="Show the fallback chain and any active swap"
    )
    async def cmd_fallback_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "fallback status")

    @fallback_group.command(
        name="revert", description="End an active fallback swap on this thread"
    )
    async def cmd_fallback_revert(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "fallback revert")

    @fallback_group.command(
        name="approvals", description="List pending model-swap consent prompts"
    )
    async def cmd_fallback_approvals(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "fallback approvals")

    @fallback_group.command(
        name="approve", description="Approve a pending model swap"
    )
    @app_commands.describe(
        record_id="The consent prompt's record id",
        hold="Hold length in minutes, or 'permanent' (default: the prompt's)",
    )
    async def cmd_fallback_approve(
        self,
        interaction: discord.Interaction,
        record_id: str,
        hold: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        args = record_id.strip()
        if hold and hold.strip():
            args += f" {hold.strip()}"
        await self.bot._send_backend_command(
            interaction, "fallback approve", args=args
        )

    @fallback_group.command(
        name="deny", description="Decline a pending model swap"
    )
    @app_commands.describe(record_id="The consent prompt's record id")
    async def cmd_fallback_deny(
        self, interaction: discord.Interaction, record_id: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction, "fallback deny", args=record_id.strip()
        )
