"""Memory and notepad commands: /memory group, /notepad group."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from ..discord_bot import make_thread_id

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


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
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            memories = await self.bot.api.list_memories(user_id)
            if not memories:
                await interaction.followup.send(
                    "No memories saved yet.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title="Your Memories",
                description=f"{len(memories)} memories stored",
                color=discord.Color.purple(),
            )
            for mem in memories[:25]:
                value = mem.get("value", "")
                value_preview = (
                    value[:200] + "..." if len(value) > 200 else value
                )
                embed.add_field(
                    name=mem.get("key", "?"),
                    value=value_preview,
                    inline=False,
                )

            if len(memories) > 25:
                embed.set_footer(
                    text=f"Showing 25 of {len(memories)} memories"
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing memories: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            await self.bot.api.save_memory(user_id, key, value)
            await interaction.followup.send(
                f"Saved memory **{key}**.", ephemeral=True
            )
        except httpx.HTTPStatusError as e:
            await interaction.followup.send(
                f"Failed to save: {e.response.json().get('detail', str(e))}",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error saving memory: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @memory_group.command(
        name="forget", description="Remove a saved memory"
    )
    @app_commands.describe(key="The memory key to remove")
    async def cmd_memory_forget(
        self, interaction: discord.Interaction, key: str
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            await self.bot.api.forget_memory(user_id, key)
            await interaction.followup.send(
                f"Forgot memory **{key}**.", ephemeral=True
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                await interaction.followup.send(
                    f"No memory found with key `{key}`.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Error: {e}", ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error removing memory: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @memory_group.command(
        name="search", description="Search memories by keyword"
    )
    @app_commands.describe(query="Search term (matches key and value)")
    async def cmd_memory_search(
        self, interaction: discord.Interaction, query: str
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            results = await self.bot.api.search_memories(user_id, query)
            if not results:
                await interaction.followup.send(
                    f"No memories matching `{query}`.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title=f"Memory Search: {query}",
                description=f"{len(results)} results",
                color=discord.Color.purple(),
            )
            for mem in results[:25]:
                value = mem.get("value", "")
                value_preview = (
                    value[:200] + "..." if len(value) > 200 else value
                )
                embed.add_field(
                    name=mem.get("key", "?"),
                    value=value_preview,
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error searching memories: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        thread_id = make_thread_id(
            interaction.guild_id, interaction.channel_id
        )
        try:
            from ...tools.thread_notes import read_notepad

            content = read_notepad(thread_id)
            if content:
                if len(content) > 4000:
                    content = content[:3997] + "..."
                embed = discord.Embed(
                    title="Notepad",
                    description=content,
                    color=discord.Color.green(),
                )
                embed.set_footer(
                    text=f"Thread: {thread_id} | {len(content)} chars"
                )
                await interaction.followup.send(
                    embed=embed, ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "Notepad is empty for this channel.", ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error reading notepad: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        thread_id = make_thread_id(
            interaction.guild_id, interaction.channel_id
        )
        write_mode = mode.value if mode else "append"
        try:
            from ...tools.thread_notes import (
                _notepad_path,
                MAX_NOTEPAD_SIZE,
            )

            path = _notepad_path(thread_id)

            if write_mode == "append":
                existing = (
                    path.read_text(encoding="utf-8") if path.exists() else ""
                )
                if existing:
                    new_content = existing.rstrip() + "\n\n" + content
                else:
                    new_content = content
            else:
                new_content = content

            if len(new_content.encode("utf-8")) > MAX_NOTEPAD_SIZE:
                await interaction.followup.send(
                    f"Notepad would exceed {MAX_NOTEPAD_SIZE // 1024}KB limit.",
                    ephemeral=True,
                )
                return

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content, encoding="utf-8")
            size = len(new_content.encode("utf-8"))

            await interaction.followup.send(
                f"Notepad updated ({write_mode}): {size} bytes.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error writing notepad: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @notepad_group.command(
        name="clear", description="Clear this channel's notepad"
    )
    async def cmd_notepad_clear(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        thread_id = make_thread_id(
            interaction.guild_id, interaction.channel_id
        )
        try:
            from ...tools.thread_notes import delete_notepad

            if delete_notepad(thread_id):
                await interaction.followup.send(
                    "Notepad cleared.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "Notepad was already empty.", ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error clearing notepad: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
