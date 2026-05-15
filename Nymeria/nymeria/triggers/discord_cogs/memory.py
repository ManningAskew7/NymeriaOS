"""Memory and notepad commands: /memory group, /notepad group."""

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

    # --- /memory group ---

    memory_group = app_commands.Group(
        name="memory", description="Manage Nymeria's memories about you"
    )

    @memory_group.command(name="list", description="List all saved memories")
    async def cmd_memory_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "memory list")

    @memory_group.command(
        name="save", description="Save a memory about you"
    )
    @app_commands.describe(
        key="Memory name (e.g., 'favorite_language')",
        value="Memory content (up to 1000 chars)",
    )
    async def cmd_memory_save(
        self, interaction: discord.Interaction, key: str, value: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "memory save",
            args=f"{key} {value}",
        )

    @memory_group.command(
        name="forget", description="Remove a saved memory"
    )
    @app_commands.describe(key="The memory key to remove")
    async def cmd_memory_forget(
        self, interaction: discord.Interaction, key: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "memory forget",
            args=key,
        )

    @memory_group.command(
        name="search", description="Search memories by keyword"
    )
    @app_commands.describe(query="Search term (matches key and value)")
    async def cmd_memory_search(
        self, interaction: discord.Interaction, query: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "memory search",
            args=query,
        )

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
