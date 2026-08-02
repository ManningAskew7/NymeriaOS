"""Information and status commands: /thread, /status, /context, /export,
/channel-context, /show-tools, /help."""

from __future__ import annotations

import io
import json as _json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..discord_bot import make_thread_id

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


DISCORD_LOCAL_COMMANDS = (
    ("Chat", "/ask <message>", "Send a message without @mentioning."),
    ("Chat", "/stop", "Abort the current running operation."),
    ("Chat", "/clear", "Clear conversation history while preserving notepad and tool config."),
    ("Chat", "/compact", "Compress conversation context for this channel."),
    ("Discord", "/export [format]", "Export conversation history as markdown, JSON, or text."),
    ("Discord", "/show-tools", "Toggle whether tool calls are shown in chat."),
    ("Discord", "/channel-context", "Toggle recent channel-message context."),
    ("Discord", "/restart [bot|api]", "Restart the Discord bot or API server."),
    ("Tools", "/tools search <query>", "Search tools by name, category, or description."),
)


def _curated_command_name(usage: str) -> str:
    """The qualified command name a curated usage string describes.

    Takes the leading command tokens and stops at the first argument
    placeholder: "/tools search <query>" -> "tools search".
    """
    parts: list[str] = []
    for token in usage.lstrip("/").split():
        if token.startswith(("<", "[")):
            break
        parts.append(token)
    return " ".join(parts)


class InfoCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    # --- /thread ---

    @app_commands.command(
        name="thread", description="Show current thread info"
    )
    async def cmd_thread(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "thread")

    # --- /status ---

    @app_commands.command(
        name="status", description="Show Nymeria system status"
    )
    async def cmd_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "status")

    # --- /context ---

    @app_commands.command(
        name="context",
        description="Detailed context breakdown for this channel",
    )
    async def cmd_context(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "context")

    # --- /export ---

    @app_commands.command(
        name="export",
        description="Export conversation history as a file",
    )
    @app_commands.describe(format="Output format (default: markdown)")
    @app_commands.choices(
        format=[
            app_commands.Choice(name="markdown", value="markdown"),
            app_commands.Choice(name="json", value="json"),
            app_commands.Choice(name="txt", value="txt"),
        ]
    )
    async def cmd_export(
        self,
        interaction: discord.Interaction,
        format: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.channel_id is None:
            return
        thread_id = make_thread_id(
            interaction.guild_id, interaction.channel_id
        )
        fmt = format.value if format else "markdown"
        try:
            data = await self.bot.api.get_history(thread_id)
            messages = data.get("messages", [])

            if not messages:
                await interaction.followup.send(
                    "No conversation history to export.", ephemeral=True
                )
                return

            if fmt == "json":
                content = _json.dumps(
                    messages, indent=2, ensure_ascii=False
                )
                ext = "json"
            elif fmt == "txt":
                lines = []
                for msg in messages:
                    role = msg.get("role", "unknown").capitalize()
                    steps = msg.get("steps", [])
                    if steps:
                        lines.append(f"[{role}]")
                        for step in steps:
                            stype = step.get("type", "")
                            if stype == "thinking":
                                lines.append(
                                    f"  [Thinking] {step.get('content', '')}"
                                )
                            elif stype == "tool_call":
                                name = step.get("name", "?")
                                args = step.get("arguments") or {}
                                result = step.get("result", "")
                                args_str = (
                                    _json.dumps(args, ensure_ascii=False)
                                    if args
                                    else ""
                                )
                                lines.append(
                                    f"  [Tool: {name}] {args_str}"
                                )
                                if result:
                                    lines.append(
                                        f"    → {str(result)[:200]}"
                                    )
                            elif stype == "response":
                                lines.append(step.get("content", ""))
                    else:
                        text = msg.get("content", "")
                        if isinstance(text, list):
                            text = "\n".join(
                                b.get("text", "")
                                for b in text
                                if isinstance(b, dict) and b.get("text")
                            )
                        lines.append(f"[{role}] {text}")
                    lines.append("")
                content = "\n".join(lines)
                ext = "txt"
            else:
                parts = []
                for msg in messages:
                    role = msg.get("role", "unknown").capitalize()
                    steps = msg.get("steps", [])
                    if steps:
                        parts.append(f"### {role}")
                        for step in steps:
                            stype = step.get("type", "")
                            if stype == "thinking":
                                parts.append(
                                    f"> *Thinking:* {step.get('content', '')}"
                                )
                            elif stype == "tool_call":
                                name = step.get("name", "?")
                                args = step.get("arguments") or {}
                                result = step.get("result", "")
                                parts.append(
                                    f"**Tool: {name}**\n"
                                    f"```json\n{_json.dumps(args, indent=2, ensure_ascii=False)}\n```"
                                )
                                if result:
                                    result_str = str(result)
                                    if len(result_str) > 500:
                                        result_str = (
                                            result_str[:497] + "..."
                                        )
                                    parts.append(
                                        f"**Result:**\n```\n{result_str}\n```"
                                    )
                            elif stype == "response":
                                parts.append(step.get("content", ""))
                    else:
                        text = msg.get("content", "")
                        if isinstance(text, list):
                            text = "\n".join(
                                b.get("text", "")
                                for b in text
                                if isinstance(b, dict) and b.get("text")
                            )
                        parts.append(f"### {role}\n\n{text}")
                    parts.append("---")
                content = "\n\n".join(parts)
                ext = "md"

            channel_name = "export"
            if (
                hasattr(interaction.channel, "name")
                and interaction.channel.name
            ):
                channel_name = interaction.channel.name
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            filename = f"nymeria-{channel_name}-{date_str}.{ext}"

            encoded = content.encode("utf-8")
            if len(encoded) > 25 * 1024 * 1024:
                await interaction.followup.send(
                    f"Export too large ({len(encoded) / 1024 / 1024:.1f} MB). "
                    "Discord limits file uploads to 25 MB.",
                    ephemeral=True,
                )
                return

            buf = io.BytesIO(encoded)
            file = discord.File(buf, filename=filename)
            await interaction.followup.send(
                f"Exported {len(messages)} messages as `{filename}`",
                file=file,
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error exporting history: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # --- /channel-context ---

    @app_commands.command(
        name="channel-context",
        description="Toggle whether Nymeria reads recent channel messages",
    )
    async def cmd_channel_context(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("Error: no channel context.", ephemeral=True)
            return
        currently_enabled = self.bot._context_enabled.get(channel_id, True)
        new_state = not currently_enabled
        self.bot._context_enabled[channel_id] = new_state
        state_str = "enabled" if new_state else "disabled"
        await interaction.response.send_message(
            f"Channel context is now **{state_str}** for this channel.\n"
            f"{'Nymeria will include recent user messages from this channel with each prompt.' if new_state else 'Nymeria will only see messages sent directly to her.'}",
            ephemeral=True,
        )

    # --- /show-tools ---

    @app_commands.command(
        name="show-tools",
        description="Toggle whether tool calls are shown in chat",
    )
    async def cmd_show_tools(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("Error: no channel context.", ephemeral=True)
            return
        currently_shown = self.bot._show_tool_calls.get(channel_id, False)
        new_state = not currently_shown
        self.bot._show_tool_calls[channel_id] = new_state
        state_str = "shown" if new_state else "hidden"
        await interaction.response.send_message(
            f"Tool calls are now **{state_str}** in this channel.\n"
            f"{'Tool names, arguments, and results will appear as embeds during responses.' if new_state else 'Only the final response text will be shown.'}",
            ephemeral=True,
        )

    # --- /help ---

    @app_commands.command(
        name="help", description="Show Nymeria bot commands"
    )
    async def cmd_help(self, interaction: discord.Interaction):
        # Defer immediately: user resolution is a backend call, and Discord's
        # 3-second acknowledgement deadline is easy to miss without it.
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return

        embed = discord.Embed(
            title="Nymeria Bot Commands",
            description="Chat with Nymeria by @mentioning it or using `/ask`.",
            color=discord.Color.purple(),
        )

        # Only the registered app commands are listed. On Discord they are
        # the ONLY invokable commands: the client refuses unregistered slash
        # commands, and plain "/" text goes to the agent as chat. The old
        # merged backend catalog advertised ~110 rows that could not be run
        # here, and at ~9,600 chars it exceeded Discord's 6,000-char embed
        # total, so /help itself failed with Discord's generic error. The
        # live command tree is the source of truth; the curated
        # DISCORD_LOCAL_COMMANDS rows override it where they exist (their
        # usage strings show arguments).
        by_category: dict[str, list[str]] = {}
        # A curated row replaces exactly the tree command it names (full
        # qualified name, argument placeholders stripped), never its whole
        # root: "/tools search <query>" must not swallow the other five
        # /tools subcommands.
        curated_names = {
            _curated_command_name(usage)
            for _category, usage, _description in DISCORD_LOCAL_COMMANDS
        }
        tree = getattr(self.bot, "tree", None)
        if tree is not None:
            for command in sorted(
                tree.walk_commands(), key=lambda c: c.qualified_name
            ):
                if isinstance(command, app_commands.Group):
                    continue  # groups render through their subcommands
                if command.qualified_name in curated_names or command.qualified_name == "help":
                    continue
                binding = getattr(command, "binding", None)
                category = type(binding).__name__.removesuffix("Cog") if binding else "Other"
                by_category.setdefault(category, []).append(
                    f"`/{command.qualified_name}` - {command.description}"
                )
        for category, usage, description in DISCORD_LOCAL_COMMANDS:
            by_category.setdefault(category, []).append(f"`{usage}` - {description}")

        # Discord limits: 1,024 chars per field value, 6,000 per embed total.
        total_chars = len(embed.title or "") + len(embed.description or "")
        embed_full = False
        for category in sorted(by_category):
            if embed_full:
                break
            lines = by_category[category]
            chunk: list[str] = []
            current_len = 0
            part = 1

            def _flush(name: str, values: list[str]) -> bool:
                nonlocal total_chars
                value = "\n".join(values)
                if total_chars + len(name) + len(value) > 5600:
                    embed.add_field(
                        name="…",
                        value="Truncated to fit Discord's embed size limit.",
                        inline=False,
                    )
                    return False
                embed.add_field(name=name, value=value, inline=False)
                total_chars += len(name) + len(value)
                return True

            for line in lines:
                line_len = len(line) + 1
                if chunk and current_len + line_len > 1000:
                    suffix = f" ({part})" if part > 1 else ""
                    if not _flush(f"{category}{suffix}", chunk):
                        embed_full = True
                        chunk = []
                        break
                    chunk = []
                    current_len = 0
                    part += 1
                chunk.append(line)
                current_len += line_len
            if chunk and not embed_full:
                suffix = f" ({part})" if part > 1 else ""
                if not _flush(f"{category}{suffix}", chunk):
                    embed_full = True
        embed.set_footer(
            text=(
                "The full command catalog lives on the desktop app, CLI, and "
                "other chat surfaces."
            )
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
