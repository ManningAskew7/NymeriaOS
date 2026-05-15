"""Tool management commands: /tools group."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Mapping, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from ..discord_bot import make_thread_id

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _tool_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("name") or tool.get("id") or tool.get("tool_id") or "")


def format_tool_search_lines(data: Mapping[str, Any], *, limit: int = 8) -> list[str]:
    """Format ranked tool search rows for Discord embeds and tests."""
    results = _mapping_sequence(data.get("results", []))
    lines: list[str] = []
    for result in results[:limit]:
        name = _tool_name(result)
        category = str(result.get("category") or result.get("tool_type") or "tool")
        status = str(result.get("status") or "available")
        hint = str(result.get("enable_hint") or f"/tools enable {name}")
        description = str(result.get("description") or "").split("\n")[0][:90]
        line = f"`{name}` ({category}, {status})\n{description}\n`{hint}`"
        lines.append(line[:1000])
    return lines


class ToolsCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    tools_group = app_commands.Group(
        name="tools", description="View and manage available tools"
    )

    @tools_group.command(
        name="core",
        description="List core tools (enabled by default for new threads)",
    )
    async def cmd_tools_core(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "tools core")

    @tools_group.command(
        name="optional",
        description="List optional tool categories (disabled by default)",
    )
    async def cmd_tools_optional(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "tools optional")

    @tools_group.command(
        name="enabled",
        description="List all tools active for this channel right now",
    )
    async def cmd_tools_enabled(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "tools enabled")

    @tools_group.command(
        name="category",
        description="List tools in a specific category",
    )
    @app_commands.describe(
        name="Category name (e.g., email, browser, calendar)"
    )
    async def cmd_tools_category(
        self, interaction: discord.Interaction, name: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "tools category",
            args=name,
        )

    @tools_group.command(
        name="search",
        description="Search tools by name, category, or description",
    )
    @app_commands.describe(query="Search text")
    async def cmd_tools_search(
        self, interaction: discord.Interaction, query: str
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            data = await self.bot.api.search_tools(
                query,
                user_id=user_id,
                thread_id=thread_id,
                top_k=8,
            )
            results = _mapping_sequence(data.get("results", []))
            embed = discord.Embed(
                title=f"Tool Search: {query}",
                description=(
                    f"{len(results)} result(s), mode: {data.get('mode', 'substring')}"
                ),
                color=discord.Color.blue(),
            )
            warning = data.get("warning")
            if warning:
                embed.add_field(
                    name="Warning",
                    value=str(warning)[:1000],
                    inline=False,
                )
            lines = format_tool_search_lines(data)
            if not lines:
                embed.add_field(name="Results", value="No matching tools found.", inline=False)
            else:
                for index, line in enumerate(lines, start=1):
                    embed.add_field(name=f"Result {index}", value=line, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error searching tools: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    async def _tool_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete for tool/category names."""
        try:
            cat_data = await self.bot.api.get_tool_categories()
            categories = cat_data.get("categories", {})
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            data = await self.bot.api.search_tools(
                current,
                thread_id=thread_id,
                top_k=20,
            )
            available = _mapping_sequence(data.get("results", []))

            choices: List[app_commands.Choice[str]] = []
            current_lower = current.lower()

            for cat_name, tools in sorted(categories.items()):
                if current_lower in cat_name:
                    label = f"{cat_name} (category: {len(tools)} tools)"
                    choices.append(
                        app_commands.Choice(name=label[:100], value=cat_name)
                    )

            seen_values = {choice.value for choice in choices}
            for t in available:
                tool_name = _tool_name(t)
                if tool_name in seen_values:
                    continue
                desc = (t.get("description") or "").split("\n")[0][:60]
                label = f"{tool_name}: {desc}" if desc else tool_name
                choices.append(
                    app_commands.Choice(name=label[:100], value=tool_name)
                )
                seen_values.add(tool_name)

            return choices[:25]
        except Exception:
            return []

    @tools_group.command(
        name="enable",
        description="Enable a tool or category for this channel",
    )
    @app_commands.describe(
        name="Tool name or category (e.g., email, bash_execute)"
    )
    async def cmd_tools_enable(
        self, interaction: discord.Interaction, name: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "tools enable",
            args=name,
        )

    @cmd_tools_enable.autocomplete("name")
    async def _enable_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await self._tool_name_autocomplete(interaction, current)

    @tools_group.command(
        name="disable",
        description="Disable a tool or category for this channel",
    )
    @app_commands.describe(
        name="Tool name or category (e.g., email, bash_execute)"
    )
    async def cmd_tools_disable(
        self, interaction: discord.Interaction, name: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "tools disable",
            args=name,
        )

    @cmd_tools_disable.autocomplete("name")
    async def _disable_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await self._tool_name_autocomplete(interaction, current)
