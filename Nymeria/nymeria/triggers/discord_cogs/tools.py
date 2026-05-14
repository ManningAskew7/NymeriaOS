"""Tool management commands: /tools group."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Sequence

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
        try:
            data = await self.bot.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])

            embed = discord.Embed(
                title="Core Tools",
                description=f"{len(default_names)} tools enabled by default for new threads",
                color=discord.Color.blue(),
            )

            for t in available:
                if t.get("name") in default_names:
                    desc = (t.get("description") or "").split("\n")[0][:80]
                    embed.add_field(
                        name=t["name"], value=desc or "—", inline=True
                    )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing core tools: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @tools_group.command(
        name="optional",
        description="List optional tool categories (disabled by default)",
    )
    async def cmd_tools_optional(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            data = await self.bot.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])

            tc = await self.bot.api.get_thread_config(thread_id)
            thread_extras = set(tc.get("enabled_tools", [])) if tc else set()

            cats: Dict[str, list] = {}
            for t in available:
                if t.get("name") not in default_names:
                    cat = t.get("category", "other")
                    cats.setdefault(cat, []).append(t)

            total_optional = sum(len(v) for v in cats.values())
            embed = discord.Embed(
                title="Optional Tools",
                description=(
                    f"{total_optional} tools across {len(cats)} categories, "
                    f"disabled by default for new threads.\n"
                    f"Use `/tools category <name>` to see individual tools."
                ),
                color=discord.Color.orange(),
            )

            for cat_name in sorted(cats):
                entries = cats[cat_name]
                active = sum(
                    1 for t in entries if t["name"] in thread_extras
                )
                tool_names = ", ".join(f"`{t['name']}`" for t in entries)
                if len(tool_names) > 200:
                    tool_names = tool_names[:197] + "..."
                status = f" ({active} enabled here)" if active else ""
                embed.add_field(
                    name=f"{cat_name} ({len(entries)}){status}",
                    value=tool_names,
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing optional tools: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @tools_group.command(
        name="enabled",
        description="List all tools active for this channel right now",
    )
    async def cmd_tools_enabled(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            data = await self.bot.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])

            tc = await self.bot.api.get_thread_config(thread_id)
            thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
            thread_disabled = set(
                tc.get("disabled_tools", []) if tc else []
            )

            all_enabled = (default_names | thread_extras) - thread_disabled

            embed = discord.Embed(
                title="Enabled Tools (this channel)",
                description=f"{len(all_enabled)} tools active",
                color=discord.Color.green(),
            )

            core_active = sorted(n for n in all_enabled if n in default_names)
            embed.add_field(
                name=f"Core ({len(core_active)})",
                value=", ".join(f"`{n}`" for n in core_active) or "None",
                inline=False,
            )

            disabled_core = sorted(thread_disabled & default_names)
            if disabled_core:
                embed.add_field(
                    name=f"Core — disabled here ({len(disabled_core)})",
                    value=", ".join(f"~~`{n}`~~" for n in disabled_core),
                    inline=False,
                )

            optional_active = sorted(
                n for n in all_enabled if n not in default_names
            )
            if optional_active:
                avail_by_name = {t["name"]: t for t in available}
                lines = []
                for name in optional_active:
                    t = avail_by_name.get(name, {})
                    desc = (t.get("description") or "").split("\n")[0][:50]
                    lines.append(
                        f"`{name}` — {desc}" if desc else f"`{name}`"
                    )
                text = "\n".join(lines)
                if len(text) > 1024:
                    text = text[:1020] + "..."
                embed.add_field(
                    name=f"Optional — enabled here ({len(optional_active)})",
                    value=text,
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Optional",
                    value="No optional tools enabled for this channel.",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing enabled tools: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            data = await self.bot.api.get_default_tools()
            default_names = set(data.get("default_tools", []))
            available = data.get("available_tools", [])

            tc = await self.bot.api.get_thread_config(thread_id)
            thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
            thread_disabled = set(
                tc.get("disabled_tools", []) if tc else []
            )
            all_enabled = (default_names | thread_extras) - thread_disabled

            cat_key = name.lower().strip().replace("-", "_")

            cats: Dict[str, list] = {}
            for t in available:
                cat = t.get("category", "other")
                cats.setdefault(cat, []).append(t)

            if cat_key not in cats:
                avail_cats = ", ".join(f"`{k}`" for k in sorted(cats))
                await interaction.followup.send(
                    f"Unknown category `{name}`. Available: {avail_cats}",
                    ephemeral=True,
                )
                return

            entries = cats[cat_key]
            embed = discord.Embed(
                title=f"Tools: {cat_key}",
                description=f"{len(entries)} tools",
                color=discord.Color.blue(),
            )

            for t in entries:
                tool_name = t["name"]
                enabled = tool_name in all_enabled
                is_default = tool_name in default_names
                icon = "✅" if enabled else "❌"
                desc = (t.get("description") or "").split("\n")[0][:80]
                tag = " (core)" if is_default else ""
                embed.add_field(
                    name=f"{icon} {tool_name}{tag}",
                    value=desc or "—",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing category: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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

    async def _resolve_tool_names(
        self,
        name: str,
        *,
        user_id: str = "default",
        thread_id: str | None = None,
    ) -> tuple:
        """Resolve a name to tool names — could be a category or individual tool.

        Returns (tool_names, is_category, category_name, error_msg).
        """
        name_key = name.lower().strip().replace("-", "_")

        cat_data = await self.bot.api.get_tool_categories()
        categories = cat_data.get("categories", {})

        if name_key in categories:
            return (categories[name_key], True, name_key, None)

        data = await self.bot.api.search_tools(
            name,
            user_id=user_id,
            thread_id=thread_id,
            top_k=5,
        )
        results = _mapping_sequence(data.get("results", []))
        for result in results:
            result_name = _tool_name(result)
            if name_key == result_name.lower().strip().replace("-", "_"):
                return ([result_name], False, None, None)

        cat_list = ", ".join(f"`{k}`" for k in sorted(categories))
        suggestion_lines = format_tool_search_lines(data, limit=3)
        suggestions = (
            "\n\nClosest matches:\n" + "\n\n".join(suggestion_lines)
            if suggestion_lines
            else ""
        )
        return (
            [],
            False,
            None,
            f"Unknown tool or category `{name}`. Categories: {cat_list}{suggestions}",
        )

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
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            tool_names, is_category, cat_name, error = (
                await self._resolve_tool_names(
                    name,
                    user_id=user_id,
                    thread_id=thread_id,
                )
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return

            tc = await self.bot.api.get_thread_config(
                thread_id, user_id=user_id
            )
            current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
            current_disabled = set(
                tc.get("disabled_tools", []) if tc else []
            )

            new_enabled = current_enabled | set(tool_names)
            new_disabled = current_disabled - set(tool_names)

            await self.bot.api.update_thread_config(
                thread_id,
                user_id=user_id,
                enabled_tools=sorted(new_enabled),
                disabled_tools=sorted(new_disabled),
            )

            if is_category:
                tool_list = ", ".join(f"`{t}`" for t in sorted(tool_names))
                embed = discord.Embed(
                    title=f"Enabled category: {cat_name}",
                    description=f"{len(tool_names)} tools enabled:\n{tool_list}",
                    color=discord.Color.green(),
                )
            else:
                embed = discord.Embed(
                    title=f"Enabled: {tool_names[0]}",
                    color=discord.Color.green(),
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error enabling tool: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            tool_names, is_category, cat_name, error = (
                await self._resolve_tool_names(
                    name,
                    user_id=user_id,
                    thread_id=thread_id,
                )
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return

            tc = await self.bot.api.get_thread_config(
                thread_id, user_id=user_id
            )
            current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
            current_disabled = set(
                tc.get("disabled_tools", []) if tc else []
            )

            new_enabled = current_enabled - set(tool_names)
            new_disabled = current_disabled | set(tool_names)

            await self.bot.api.update_thread_config(
                thread_id,
                user_id=user_id,
                enabled_tools=sorted(new_enabled),
                disabled_tools=sorted(new_disabled),
            )

            if is_category:
                tool_list = ", ".join(f"`{t}`" for t in sorted(tool_names))
                embed = discord.Embed(
                    title=f"Disabled category: {cat_name}",
                    description=f"{len(tool_names)} tools disabled:\n{tool_list}",
                    color=discord.Color.red(),
                )
            else:
                embed = discord.Embed(
                    title=f"Disabled: {tool_names[0]}",
                    color=discord.Color.red(),
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error disabling tool: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @cmd_tools_disable.autocomplete("name")
    async def _disable_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await self._tool_name_autocomplete(interaction, current)
