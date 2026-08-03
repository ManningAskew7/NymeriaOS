"""Notepad commands: the /notepad group.

The `/memory` group moved to `generated_cogs.py` (pure defer-and-relay
wrappers). `/notepad` stays hand-written because `/notepad write` maps a Discord
mode choice onto the backend's `replace:` value prefix, and the registry
deliberately leaves that command unadopted; a Discord group name has exactly one
owner, so its siblings stay with it.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot


class MemoryCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    # --- /notepad group ---

    notepad_group = app_commands.Group(
        name="notepad",
        description="Per-channel persistent notes (survive compaction)",
    )

    @notepad_group.command(
        name="read", description="Read this channel's notepad"
    )
    async def cmd_notepad_read(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "notepad read")

    @notepad_group.command(
        name="write", description="Write to this channel's notepad"
    )
    @app_commands.describe(
        content="Text to add to the notepad",
        mode="'append' (default) or 'replace'",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="append", value="append"),
            app_commands.Choice(name="replace", value="replace"),
        ]
    )
    async def cmd_notepad_write(
        self,
        interaction: discord.Interaction,
        content: str,
        mode: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        write_mode = mode.value if mode else "append"
        prefix = "replace:" if write_mode == "replace" else ""
        await self.bot._send_backend_command(
            interaction,
            "notepad write",
            args=f"{prefix}{content}",
        )

    @notepad_group.command(
        name="clear", description="Clear this channel's notepad"
    )
    async def cmd_notepad_clear(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "notepad clear")
