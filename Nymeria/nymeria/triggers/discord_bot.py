"""Discord bot trigger for two-way Nymeria communication.

Thin client architecture: the bot calls the Nymeria REST API for all
operations (chat, tools, memory, etc.) instead of running its own
NymeriaAgent. This ensures Discord always reflects the same state as
the frontend app — one agent, one source of truth.

Supports slash commands, @mention responses, DMs, and SSE streaming
for autonomous task results.
"""

import asyncio
import json as _json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import discord
import httpx
from discord import app_commands

from .discord_api_client import NymeriaAPIClient

logger = logging.getLogger(__name__)


# =============================================================================
# Formatting Helpers (platform-agnostic data → Discord embeds)
# =============================================================================


def fmt_tokens(n: int) -> str:
    """Format token count: 5353 → '5.4k', 1000000 → '1.0M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def context_bar(usage_pct: float, width: int = 20) -> str:
    """Render a text progress bar: `████░░░░░░░░░░░░░░░░` 5%."""
    filled = int(width * usage_pct / 100) if usage_pct else 0
    return "`" + "\u2588" * filled + "\u2591" * (width - filled) + f"` {usage_pct}%"


# =============================================================================
# Message Splitting
# =============================================================================


def split_message(content: str, max_length: int = 2000) -> List[str]:
    """
    Split a message into chunks that fit Discord's character limit.

    Preserves code blocks, paragraph boundaries, and sentence boundaries.
    Never splits mid-code-block.
    """
    if len(content) <= max_length:
        return [content]

    chunks: List[str] = []
    remaining = content

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, max_length)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return [c for c in chunks if c.strip()]


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best split point within max_length characters."""
    code_block_start = text.rfind("```", 0, max_length)
    if code_block_start > 0:
        count_before = text[:code_block_start].count("```")
        if count_before % 2 == 1:
            closing = text.find("```", code_block_start + 3)
            if closing != -1 and closing + 3 <= len(text):
                end_of_block = closing + 3
                if end_of_block <= max_length:
                    return end_of_block
            block_open = text.rfind("```", 0, code_block_start)
            if block_open > max_length * 0.3:
                return block_open

    para = text.rfind("\n\n", 0, max_length)
    if para > max_length * 0.5:
        return para + 2

    line = text.rfind("\n", 0, max_length)
    if line > max_length * 0.5:
        return line + 1

    sentence = text.rfind(". ", 0, max_length)
    if sentence > max_length * 0.5:
        return sentence + 2

    return max_length


# =============================================================================
# Thread ID Helpers
# =============================================================================


def make_thread_id(guild_id: Optional[int], channel_id: int) -> str:
    """Generate a Nymeria thread ID from Discord IDs."""
    if guild_id:
        return f"discord_{guild_id}_{channel_id}"
    return f"discord_dm_{channel_id}"


def make_user_id(user_id: int) -> str:
    """Generate a Nymeria user ID from a Discord user ID."""
    return f"discord_{user_id}"


CONTEXT_MESSAGE_COUNT = 10


async def fetch_channel_context(
    channel: Any,
    limit: int = CONTEXT_MESSAGE_COUNT,
    before: Any = None,
    bot_user_id: Optional[int] = None,
) -> str:
    """Fetch recent messages from a Discord channel and format them as context."""
    try:
        messages: List[discord.Message] = []
        async for msg in channel.history(limit=limit, before=before):
            if bot_user_id and msg.author.id == bot_user_id:
                continue
            messages.append(msg)

        if not messages:
            return ""

        messages.reverse()

        lines = []
        for msg in messages:
            text = msg.content
            if not text:
                continue
            name = msg.author.display_name or msg.author.name
            timestamp = msg.created_at.strftime("%H:%M")
            lines.append(f"[{timestamp}] {name}: {text}")

        if not lines:
            return ""

        context = "\n".join(lines)
        return (
            f"[Discord Channel Context — last {len(lines)} messages]\n"
            f"{context}\n"
            f"[End of channel context]\n\n"
        )
    except Exception as e:
        logger.warning(f"Failed to fetch channel context: {e}")
        return ""


def parse_thread_id(thread_id: str) -> Dict[str, Any]:
    """Parse a Discord-originated thread ID back into its components."""
    if thread_id.startswith("discord_dm_"):
        return {"type": "dm", "channel_id": thread_id[len("discord_dm_"):]}
    if thread_id.startswith("discord_"):
        parts = thread_id.split("_")
        if len(parts) >= 3:
            return {"type": "guild", "guild_id": parts[1], "channel_id": parts[2]}
    return {"type": "unknown"}


# =============================================================================
# Discord Bot Client
# =============================================================================


class NymeriaDiscordBot(discord.Client):
    """Discord bot client — thin API client for Nymeria."""

    def __init__(
        self,
        api: NymeriaAPIClient,
        respond_mode: str = "mention",
    ):
        """
        Initialize the Discord bot.

        Args:
            api: NymeriaAPIClient for all backend operations.
            respond_mode: 'mention' (only @Nymeria in guilds) or 'all' (every message).
        """
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.dm_messages = True
        intents.guild_messages = True

        super().__init__(intents=intents)

        self.api = api
        self.respond_mode = respond_mode
        self.tree = app_commands.CommandTree(self)
        self._start_time = time.time()
        self._context_enabled: Dict[int, bool] = {}  # channel_id -> enabled

        # Register slash commands
        self._register_commands()

    def _register_commands(self) -> None:
        """Register all slash commands with the command tree."""

        @self.tree.command(name="ask", description="Send a message to Nymeria")
        @app_commands.describe(message="Your message to Nymeria")
        async def cmd_ask(interaction: discord.Interaction, message: str):
            await interaction.response.defer()
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            user_id = make_user_id(interaction.user.id)

            context = ""
            if self._context_enabled.get(interaction.channel_id, True):
                bot_id = self.user.id if self.user else None
                context = await fetch_channel_context(
                    interaction.channel, bot_user_id=bot_id
                )
            message_with_context = f"{context}{message}" if context else message

            try:
                response = await self.api.chat(message_with_context, thread_id, user_id)
            except Exception as e:
                logger.error(f"Error in /ask: {e}", exc_info=True)
                response = f"Sorry, I encountered an error: {e}"

            chunks = split_message(response)
            await interaction.followup.send(chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)

        @self.tree.command(name="clear", description="Wipe conversation history for this channel")
        async def cmd_clear(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                await self.api.delete_thread(thread_id)
                await interaction.followup.send(
                    "Conversation history cleared for this channel.",
                    ephemeral=True,
                )
            except Exception as e:
                logger.error(f"Error clearing thread: {e}", exc_info=True)
                await interaction.followup.send(
                    f"Error clearing history: {e}", ephemeral=True
                )

        @self.tree.command(name="compact", description="Compress conversation to save context window")
        async def cmd_compact(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            user_id = make_user_id(interaction.user.id)
            try:
                result = await self.api.compact(thread_id, user_id)
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

        # --- /todos group ---
        todos_group = app_commands.Group(name="todos", description="Manage scheduled tasks and reminders")

        @todos_group.command(name="list", description="List TODOs")
        @app_commands.describe(
            filter="Filter by status (default: active)",
        )
        @app_commands.choices(filter=[
            app_commands.Choice(name="active (pending + in progress)", value="active"),
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="in progress", value="in_progress"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ])
        async def cmd_todos_list(
            interaction: discord.Interaction,
            filter: app_commands.Choice[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                items = await self.api.list_todos(user_id)
                filter_val = filter.value if filter else "active"

                if filter_val == "active":
                    items = [i for i in items if i.get("status") != "done"]
                elif filter_val != "all":
                    items = [i for i in items if i.get("status") == filter_val]

                if not items:
                    await interaction.followup.send(
                        f"No {filter_val} TODOs found.", ephemeral=True
                    )
                    return

                embed = discord.Embed(
                    title=f"TODOs ({filter_val})",
                    description=f"{len(items)} items",
                    color=discord.Color.blue(),
                )
                for item in items[:25]:
                    status = item.get("status", "pending")
                    task = item.get("task", "")[:80]
                    todo_id = item.get("id", "")[:8]

                    # Build detail line
                    parts = [f"ID: `{todo_id}`"]
                    scheduled = item.get("scheduled_for")
                    if scheduled:
                        # Parse ISO and show relative
                        parts.append(f"fires: {scheduled[:16]}")
                    recurrence = item.get("recurrence")
                    if recurrence:
                        parts.append(f"repeat: {recurrence}")
                    thread = item.get("thread_id")
                    if thread:
                        parts.append(f"thread: `{thread[:20]}`")

                    status_marker = {"pending": "\u23f3", "in_progress": "\u25b6", "done": "\u2705"}.get(status, "?")
                    embed.add_field(
                        name=f"{status_marker} {task}",
                        value=" | ".join(parts),
                        inline=False,
                    )

                if len(items) > 25:
                    embed.set_footer(text=f"Showing 25 of {len(items)}")

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing todos: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @todos_group.command(name="add", description="Create a scheduled task or reminder")
        @app_commands.describe(
            task="What should Nymeria do?",
            schedule="When to fire: '30m', '2h', '1d', or '2024-12-25 14:00'",
            repeat="Repeat interval (omit for one-shot)",
            notes="Additional context or instructions",
        )
        @app_commands.choices(repeat=[
            app_commands.Choice(name="every 5 minutes", value="5min"),
            app_commands.Choice(name="every 10 minutes", value="10min"),
            app_commands.Choice(name="every 15 minutes", value="15min"),
            app_commands.Choice(name="every 30 minutes", value="30min"),
            app_commands.Choice(name="every hour", value="hourly"),
            app_commands.Choice(name="every day", value="daily"),
            app_commands.Choice(name="every week", value="weekly"),
            app_commands.Choice(name="every month", value="monthly"),
        ])
        async def cmd_todos_add(
            interaction: discord.Interaction,
            task: str,
            schedule: str = "1d",
            repeat: app_commands.Choice[str] = None,
            notes: Optional[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                result = await self.api.add_todo(
                    user_id=user_id,
                    task=task,
                    scheduled_for=schedule,
                    notes=notes,
                    recurrence=repeat.value if repeat else None,
                    thread_id=thread_id,
                )
                todo_id = result.get("id", "")[:8]
                scheduled = result.get("scheduled_for", "")

                lines = [f"Created TODO `{todo_id}`: **{task}**"]
                if scheduled:
                    lines.append(f"Fires: {scheduled[:16]}")
                if repeat:
                    lines.append(f"Repeats: {repeat.name}")
                lines.append(f"Thread: this channel")

                await interaction.followup.send(
                    "\n".join(lines), ephemeral=True
                )
            except httpx.HTTPStatusError as e:
                detail = e.response.json().get("detail", str(e)) if e.response else str(e)
                await interaction.followup.send(f"Error: {detail}", ephemeral=True)
            except Exception as e:
                logger.error(f"Error adding todo: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @todos_group.command(name="complete", description="Mark a TODO as done")
        @app_commands.describe(todo_id="The TODO ID (first 8 chars)")
        async def cmd_todos_complete(interaction: discord.Interaction, todo_id: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                items = await self.api.list_todos(user_id)
                match = None
                for item in items:
                    if item.get("id", "").startswith(todo_id):
                        match = item
                        break
                if not match:
                    await interaction.followup.send(
                        f"No TODO found matching `{todo_id}`.", ephemeral=True
                    )
                    return

                result = await self.api.complete_todo(user_id, match["id"])

                task = match.get("task", "")
                recurrence = result.get("recurrence")
                if recurrence and result.get("status") == "pending":
                    # Auto-rescheduled
                    next_fire = result.get("scheduled_for", "")[:16]
                    await interaction.followup.send(
                        f"Completed: **{task}**\nRescheduled ({recurrence}): next fire {next_fire}",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"Completed: **{task}**", ephemeral=True
                    )
            except Exception as e:
                logger.error(f"Error completing todo: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @todos_group.command(name="delete", description="Delete a TODO permanently")
        @app_commands.describe(todo_id="The TODO ID (first 8 chars)")
        async def cmd_todos_delete(interaction: discord.Interaction, todo_id: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                items = await self.api.list_todos(user_id)
                match = None
                for item in items:
                    if item.get("id", "").startswith(todo_id):
                        match = item
                        break
                if not match:
                    await interaction.followup.send(
                        f"No TODO found matching `{todo_id}`.", ephemeral=True
                    )
                    return

                await self.api.delete_todo(user_id, match["id"])
                await interaction.followup.send(
                    f"Deleted: **{match.get('task', '')}**", ephemeral=True
                )
            except Exception as e:
                logger.error(f"Error deleting todo: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(todos_group)

        @self.tree.command(name="thread", description="Show current thread info")
        async def cmd_thread(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                stats = await self.api.get_context_stats(thread_id)
                embed = discord.Embed(title="Thread Info", color=discord.Color.green())
                embed.add_field(name="Thread ID", value=f"`{thread_id}`", inline=False)
                embed.add_field(
                    name="Context Usage",
                    value=f"{stats.get('usage_percentage', 0)}% ({stats.get('total_tokens', 0):,} / {stats.get('context_limit', 0):,} tokens)",
                    inline=True,
                )
                embed.add_field(name="Compactions", value=str(stats.get("compaction_count", 0)), inline=True)
                embed.add_field(name="Context Mode", value=stats.get("context_management", "unknown"), inline=True)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error getting thread info: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="status", description="Show Nymeria system status")
        async def cmd_status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )

                # Fetch all data in parallel
                settings, context, tools_data = await asyncio.gather(
                    self.api.get_settings(),
                    self.api.get_context_stats(thread_id),
                    self.api.get_default_tools(),
                    return_exceptions=True,
                )

                # Handle errors gracefully
                if isinstance(settings, Exception):
                    settings = {}
                if isinstance(context, Exception):
                    context = {}
                if isinstance(tools_data, Exception):
                    tools_data = {}

                # Uptime
                uptime_seconds = int(time.time() - self._start_time)
                hours, remainder = divmod(uptime_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                if hours > 0:
                    uptime_str = f"{hours}h {minutes}m"
                elif minutes > 0:
                    uptime_str = f"{minutes}m {seconds}s"
                else:
                    uptime_str = f"{seconds}s"

                # Model & provider
                model = settings.get("llm_model", "?")
                provider = settings.get("llm_provider", "?")
                base_url = settings.get("llm_base_url")
                if base_url and "cli-proxy" in base_url:
                    provider_detail = f"{provider} (via CLIProxy)"
                elif base_url:
                    provider_detail = f"{provider} ({base_url})"
                else:
                    provider_detail = provider

                # Context
                total_tokens = context.get("total_tokens", 0)
                context_limit = context.get("context_limit", 0)
                usage_pct = context.get("usage_percentage", 0)
                compactions = context.get("compaction_count", 0)
                context_mode = context.get("context_management", settings.get("context_management", "?"))

                # Tools
                default_count = len(tools_data.get("default_tools", []))
                available_count = len(tools_data.get("available_tools", []))
                callable_count = tools_data.get("callable_thread_count", 0)

                # Thinking
                thinking = settings.get("llm_extended_thinking", False)
                thinking_str = "on" if thinking else "off"
                reasoning = settings.get("llm_reasoning_effort")
                if reasoning:
                    thinking_str += f" ({reasoning})"

                processing = context.get("processing", False)

                embed = discord.Embed(
                    title="Nymeria Status",
                    color=discord.Color.gold(),
                )

                # Model & Runtime
                runtime_parts = [f"`{model}`"]
                runtime_parts.append(provider_detail)
                if thinking:
                    t_label = f"thinking: {reasoning}" if reasoning else "thinking: on"
                    runtime_parts.append(t_label)
                else:
                    runtime_parts.append("thinking: off")
                embed.add_field(
                    name="Model",
                    value=" | ".join(runtime_parts),
                    inline=False,
                )

                # Context Window
                ctx_lines = [
                    context_bar(usage_pct),
                    f"{fmt_tokens(total_tokens)} / {fmt_tokens(context_limit)} tokens",
                ]
                if compactions:
                    ctx_lines.append(f"{compactions} compaction{'s' if compactions != 1 else ''}")
                ctx_lines.append(f"mode: {context_mode}")
                compact_threshold = settings.get("compact_threshold")
                if compact_threshold and context_mode == "auto_compact":
                    ctx_lines[-1] += f" (threshold {int(compact_threshold * 100)}%)"
                embed.add_field(
                    name="Context",
                    value="\n".join(ctx_lines),
                    inline=True,
                )

                # Tools & System
                sys_lines = [
                    f"{default_count} core / {available_count} available",
                ]
                if callable_count:
                    sys_lines[0] += f" / {callable_count} callable"
                sys_lines.append(f"uptime: {uptime_str}")
                if settings.get("watchdog_enabled"):
                    sys_lines.append(f"watchdog: {settings.get('watchdog_interval_minutes', '?')}m interval")
                if processing:
                    sys_lines.append("**processing...**")
                embed.add_field(
                    name="Tools & System",
                    value="\n".join(sys_lines),
                    inline=True,
                )

                embed.set_footer(text=f"thread: {thread_id}")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error getting status: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="model", description="Show or change the LLM model")
        @app_commands.describe(
            name="Model to switch to (omit to show current)",
            scope="'global' changes default, 'thread' changes this channel only",
        )
        @app_commands.choices(scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ])
        async def cmd_model(
            interaction: discord.Interaction,
            name: Optional[str] = None,
            scope: app_commands.Choice[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            try:
                if name is None:
                    # Show current model
                    settings = await self.api.get_settings()
                    thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
                    tc = await self.api.get_thread_config(thread_id)
                    llm_cfg = (tc or {}).get("llm_config") or {}
                    thread_model = llm_cfg.get("model")

                    lines = [f"**Global:** `{settings.get('llm_model', '?')}` ({settings.get('llm_provider', '?')})"]
                    if thread_model:
                        lines.append(f"**This channel:** `{thread_model}` (override)")
                    else:
                        lines.append("**This channel:** using global default")
                    await interaction.followup.send("\n".join(lines), ephemeral=True)
                else:
                    # Set model
                    target = scope.value if scope else "global"
                    if target == "thread":
                        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
                        await self.api.update_thread_config(
                            thread_id, llm_config={"model": name}
                        )
                        await interaction.followup.send(
                            f"Model for this channel set to `{name}`.", ephemeral=True
                        )
                    else:
                        await self.api.update_settings(llm_model=name)
                        await interaction.followup.send(
                            f"Global model set to `{name}`.", ephemeral=True
                        )
            except httpx.HTTPStatusError as e:
                detail = e.response.json().get("detail", str(e)) if e.response else str(e)
                await interaction.followup.send(f"Error: {detail}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="models", description="List available models")
        async def cmd_models(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                models = await self.api.list_available_models()
                settings = await self.api.get_settings()
                current = settings.get("llm_model", "")

                if not models:
                    await interaction.followup.send(
                        "No models returned from provider. This may not be supported for your current provider/proxy setup.",
                        ephemeral=True,
                    )
                    return

                embed = discord.Embed(
                    title="Available Models",
                    description=f"{len(models)} models from {settings.get('llm_provider', '?')}",
                    color=discord.Color.blue(),
                )

                for m in models[:25]:
                    model_id = m.get("id") or m.get("name", "?")
                    ctx = m.get("context_length") or m.get("context_window")
                    ctx_str = f" | {fmt_tokens(ctx)} ctx" if ctx else ""
                    marker = " **(current)**" if model_id == current else ""
                    embed.add_field(
                        name=f"`{model_id}`{marker}",
                        value=f"{m.get('name', model_id)}{ctx_str}",
                        inline=True,
                    )

                if len(models) > 25:
                    embed.set_footer(text=f"Showing 25 of {len(models)} models")

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing models: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="think", description="Set thinking/reasoning mode")
        @app_commands.describe(mode="Thinking mode")
        @app_commands.choices(mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="low", value="low"),
            app_commands.Choice(name="medium", value="medium"),
            app_commands.Choice(name="high", value="high"),
        ])
        async def cmd_think(
            interaction: discord.Interaction,
            mode: app_commands.Choice[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            try:
                if mode is None:
                    # Show current
                    settings = await self.api.get_settings()
                    thinking = settings.get("llm_extended_thinking", False)
                    effort = settings.get("llm_reasoning_effort")
                    logger.info(f"[/think] thinking={thinking!r} effort={effort!r}")
                    if not thinking:
                        status = "off"
                    elif effort:
                        status = f"on (effort: {effort})"
                    else:
                        status = "on"
                    await interaction.followup.send(
                        f"Thinking is currently **{status}**.", ephemeral=True
                    )
                    return

                value = mode.value
                if value == "off":
                    await self.api.update_settings(
                        llm_extended_thinking=False, llm_reasoning_effort=None
                    )
                    await interaction.followup.send(
                        "Thinking **disabled**.", ephemeral=True
                    )
                elif value == "on":
                    await self.api.update_settings(llm_extended_thinking=True)
                    await interaction.followup.send(
                        "Thinking **enabled**.", ephemeral=True
                    )
                else:
                    # low/medium/high
                    await self.api.update_settings(
                        llm_extended_thinking=True, llm_reasoning_effort=value
                    )
                    await interaction.followup.send(
                        f"Thinking **enabled**, effort: **{value}**.", ephemeral=True
                    )
            except Exception as e:
                logger.error(f"Error setting thinking: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        # --- /config group ---
        config_group = app_commands.Group(
            name="config", description="View and update Nymeria settings"
        )

        @config_group.command(name="show", description="Show all current settings")
        async def cmd_config_show(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                settings = await self.api.get_settings()

                embed = discord.Embed(
                    title="Settings",
                    color=discord.Color.greyple(),
                )

                # LLM
                llm_lines = [
                    f"provider: `{settings.get('llm_provider', '?')}`",
                    f"model: `{settings.get('llm_model', '?')}`",
                    f"temperature: {settings.get('llm_temperature', '?')}",
                    f"thinking: {'on' if settings.get('llm_extended_thinking') else 'off'}",
                ]
                effort = settings.get("llm_reasoning_effort")
                if effort:
                    llm_lines.append(f"reasoning effort: {effort}")
                base_url = settings.get("llm_base_url")
                if base_url:
                    llm_lines.append(f"base url: `{base_url}`")
                embed.add_field(name="LLM", value="\n".join(llm_lines), inline=False)

                # Context
                ctx_lines = [
                    f"mode: {settings.get('context_management', '?')}",
                    f"compact threshold: {int((settings.get('compact_threshold', 0) or 0) * 100)}%",
                    f"keep messages: {settings.get('compact_keep_messages', '?')}",
                ]
                compact_model = settings.get("compact_model")
                if compact_model:
                    ctx_lines.append(f"compact model: `{compact_model}`")
                embed.add_field(name="Context", value="\n".join(ctx_lines), inline=True)

                # System
                sys_lines = [
                    f"log level: {settings.get('log_level', '?')}",
                    f"watchdog: {'on' if settings.get('watchdog_enabled') else 'off'}",
                ]
                if settings.get("watchdog_enabled"):
                    sys_lines.append(f"watchdog interval: {settings.get('watchdog_interval_minutes', '?')}m")
                embed.add_field(name="System", value="\n".join(sys_lines), inline=True)

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error showing config: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @config_group.command(name="get", description="Get a specific setting value")
        @app_commands.describe(key="Setting name (e.g., llm_model, context_management)")
        async def cmd_config_get(interaction: discord.Interaction, key: str):
            await interaction.response.defer(ephemeral=True)
            try:
                settings = await self.api.get_settings()
                if key in settings:
                    value = settings[key]
                    await interaction.followup.send(
                        f"**{key}** = `{value}`", ephemeral=True
                    )
                else:
                    available = ", ".join(f"`{k}`" for k in sorted(settings.keys())[:30])
                    await interaction.followup.send(
                        f"Unknown setting `{key}`. Available: {available}",
                        ephemeral=True,
                    )
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @config_group.command(name="set", description="Update a setting")
        @app_commands.describe(
            key="Setting name (e.g., llm_temperature, log_level)",
            value="New value",
        )
        async def cmd_config_set(interaction: discord.Interaction, key: str, value: str):
            await interaction.response.defer(ephemeral=True)
            try:
                # Auto-convert value types
                if value.lower() in ("true", "false"):
                    parsed = value.lower() == "true"
                elif value.lower() == "none":
                    parsed = None
                else:
                    try:
                        parsed = int(value)
                    except ValueError:
                        try:
                            parsed = float(value)
                        except ValueError:
                            parsed = value

                await self.api.update_settings(**{key: parsed})
                await interaction.followup.send(
                    f"**{key}** set to `{parsed}`.", ephemeral=True
                )
            except httpx.HTTPStatusError as e:
                detail = e.response.json().get("detail", str(e)) if e.response else str(e)
                await interaction.followup.send(f"Error: {detail}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(config_group)

        @self.tree.command(name="channel-context", description="Toggle whether Nymeria reads recent channel messages")
        async def cmd_channel_context(interaction: discord.Interaction):
            channel_id = interaction.channel_id
            currently_enabled = self._context_enabled.get(channel_id, True)
            new_state = not currently_enabled
            self._context_enabled[channel_id] = new_state
            state_str = "enabled" if new_state else "disabled"
            await interaction.response.send_message(
                f"Channel context is now **{state_str}** for this channel.\n"
                f"{'Nymeria will include recent user messages from this channel with each prompt.' if new_state else 'Nymeria will only see messages sent directly to her.'}",
                ephemeral=True,
            )

        # --- /stop ---
        @self.tree.command(name="stop", description="Abort the current running operation")
        async def cmd_stop(interaction: discord.Interaction):
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                await self.api.stop(thread_id)
                await interaction.response.send_message(
                    "Abort signal sent. The current operation will stop shortly.",
                    ephemeral=True,
                )
            except Exception as e:
                logger.error(f"Error aborting thread: {e}", exc_info=True)
                await interaction.response.send_message(
                    f"Error sending abort: {e}", ephemeral=True
                )

        # --- /tools group ---
        tools_group = app_commands.Group(
            name="tools", description="View and manage available tools"
        )

        @tools_group.command(
            name="core",
            description="List core tools (enabled by default for new threads)",
        )
        async def cmd_tools_core(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                data = await self.api.get_default_tools()
                default_names = set(data.get("default_tools", []))
                available = data.get("available_tools", [])

                embed = discord.Embed(
                    title="Core Tools",
                    description=f"{len(default_names)} tools — enabled by default for new threads",
                    color=discord.Color.blue(),
                )

                for t in available:
                    if t.get("name") in default_names:
                        desc = (t.get("description") or "").split("\n")[0][:80]
                        embed.add_field(name=t["name"], value=desc or "\u2014", inline=True)

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing core tools: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @tools_group.command(
            name="optional",
            description="List optional tool categories (disabled by default)",
        )
        async def cmd_tools_optional(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                data = await self.api.get_default_tools()
                default_names = set(data.get("default_tools", []))
                available = data.get("available_tools", [])

                # Get thread-specific overrides
                tc = await self.api.get_thread_config(thread_id)
                thread_extras = set(tc.get("enabled_tools", [])) if tc else set()

                # Group non-default tools by category
                cats: Dict[str, list] = {}
                for t in available:
                    if t.get("name") not in default_names:
                        cat = t.get("category", "other")
                        cats.setdefault(cat, []).append(t)

                total_optional = sum(len(v) for v in cats.values())
                embed = discord.Embed(
                    title="Optional Tools",
                    description=(
                        f"{total_optional} tools across {len(cats)} categories "
                        f"— disabled by default for new threads.\n"
                        f"Use `/tools category <name>` to see individual tools."
                    ),
                    color=discord.Color.orange(),
                )

                for cat_name in sorted(cats):
                    entries = cats[cat_name]
                    active = sum(1 for t in entries if t["name"] in thread_extras)
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
        async def cmd_tools_enabled(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                data = await self.api.get_default_tools()
                default_names = set(data.get("default_tools", []))
                available = data.get("available_tools", [])

                # Get thread-specific overrides
                tc = await self.api.get_thread_config(thread_id)
                thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
                thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()

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
                        name=f"Core \u2014 disabled here ({len(disabled_core)})",
                        value=", ".join(f"~~`{n}`~~" for n in disabled_core),
                        inline=False,
                    )

                optional_active = sorted(n for n in all_enabled if n not in default_names)
                if optional_active:
                    avail_by_name = {t["name"]: t for t in available}
                    lines = []
                    for name in optional_active:
                        t = avail_by_name.get(name, {})
                        desc = (t.get("description") or "").split("\n")[0][:50]
                        lines.append(f"`{name}` \u2014 {desc}" if desc else f"`{name}`")
                    text = "\n".join(lines)
                    if len(text) > 1024:
                        text = text[:1020] + "..."
                    embed.add_field(
                        name=f"Optional \u2014 enabled here ({len(optional_active)})",
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
        @app_commands.describe(name="Category name (e.g., email, browser, calendar)")
        async def cmd_tools_category(interaction: discord.Interaction, name: str):
            await interaction.response.defer(ephemeral=True)
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                data = await self.api.get_default_tools()
                default_names = set(data.get("default_tools", []))
                available = data.get("available_tools", [])

                tc = await self.api.get_thread_config(thread_id)
                thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
                thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()
                all_enabled = (default_names | thread_extras) - thread_disabled

                cat_key = name.lower().strip().replace("-", "_")

                # Group all tools by category
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
                    icon = "\u2705" if enabled else "\u274c"
                    desc = (t.get("description") or "").split("\n")[0][:80]
                    tag = " (core)" if is_default else ""
                    embed.add_field(
                        name=f"{icon} {tool_name}{tag}",
                        value=desc or "\u2014",
                        inline=False,
                    )

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing category: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        async def _resolve_tool_names(
            self_bot: "NymeriaDiscordBot",
            name: str,
        ) -> tuple:
            """Resolve a name to tool names — could be a category or individual tool.

            Returns (tool_names, is_category, category_name, error_msg).
            If error_msg is set, the other fields are empty/None.
            """
            name_key = name.lower().strip().replace("-", "_")

            # Check categories first
            cat_data = await self_bot.api.get_tool_categories()
            categories = cat_data.get("categories", {})

            if name_key in categories:
                return (categories[name_key], True, name_key, None)

            # Check if it's an individual tool name
            data = await self_bot.api.get_default_tools()
            available = data.get("available_tools", [])
            all_names = {t["name"] for t in available}

            if name_key in all_names:
                return ([name_key], False, None, None)

            # Not found — build suggestions
            cat_list = ", ".join(f"`{k}`" for k in sorted(categories))
            return ([], False, None, f"Unknown tool or category `{name}`. Categories: {cat_list}")

        async def _tool_name_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> List[app_commands.Choice[str]]:
            """Autocomplete for tool/category names."""
            try:
                cat_data = await self.api.get_tool_categories()
                categories = cat_data.get("categories", {})
                data = await self.api.get_default_tools()
                available = data.get("available_tools", [])

                choices: List[app_commands.Choice[str]] = []
                current_lower = current.lower()

                # Categories first (prefixed for clarity)
                for cat_name, tools in sorted(categories.items()):
                    if current_lower in cat_name:
                        label = f"{cat_name} (category — {len(tools)} tools)"
                        choices.append(app_commands.Choice(name=label[:100], value=cat_name))

                # Then individual tools
                for t in available:
                    tool_name = t["name"]
                    if current_lower in tool_name.lower():
                        desc = (t.get("description") or "").split("\n")[0][:60]
                        label = f"{tool_name} — {desc}" if desc else tool_name
                        choices.append(app_commands.Choice(name=label[:100], value=tool_name))

                return choices[:25]  # Discord max
            except Exception:
                return []

        @tools_group.command(
            name="enable",
            description="Enable a tool or category for this channel",
        )
        @app_commands.describe(name="Tool name or category (e.g., email, bash_execute)")
        @app_commands.autocomplete(name=_tool_name_autocomplete)
        async def cmd_tools_enable(interaction: discord.Interaction, name: str):
            await interaction.response.defer(ephemeral=True)
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                tool_names, is_category, cat_name, error = await _resolve_tool_names(self, name)
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return

                # Read current thread config
                tc = await self.api.get_thread_config(thread_id)
                current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
                current_disabled = set(tc.get("disabled_tools", [])) if tc else set()

                # Add to enabled, remove from disabled
                new_enabled = current_enabled | set(tool_names)
                new_disabled = current_disabled - set(tool_names)

                await self.api.update_thread_config(
                    thread_id,
                    enabled_tools=sorted(new_enabled),
                    disabled_tools=sorted(new_disabled),
                )

                # Confirmation embed
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

        @tools_group.command(
            name="disable",
            description="Disable a tool or category for this channel",
        )
        @app_commands.describe(name="Tool name or category (e.g., email, bash_execute)")
        @app_commands.autocomplete(name=_tool_name_autocomplete)
        async def cmd_tools_disable(interaction: discord.Interaction, name: str):
            await interaction.response.defer(ephemeral=True)
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                tool_names, is_category, cat_name, error = await _resolve_tool_names(self, name)
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return

                # Read current thread config
                tc = await self.api.get_thread_config(thread_id)
                current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
                current_disabled = set(tc.get("disabled_tools", [])) if tc else set()

                # Remove from enabled, add to disabled
                new_enabled = current_enabled - set(tool_names)
                new_disabled = current_disabled | set(tool_names)

                await self.api.update_thread_config(
                    thread_id,
                    enabled_tools=sorted(new_enabled),
                    disabled_tools=sorted(new_disabled),
                )

                # Confirmation embed
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

        self.tree.add_command(tools_group)

        # --- /memory group ---
        memory_group = app_commands.Group(
            name="memory", description="Manage Nymeria's memories about you"
        )

        @memory_group.command(name="list", description="List all saved memories")
        async def cmd_memory_list(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                memories = await self.api.list_memories(user_id)
                if not memories:
                    await interaction.followup.send("No memories saved yet.", ephemeral=True)
                    return

                embed = discord.Embed(
                    title="Your Memories",
                    description=f"{len(memories)} memories stored",
                    color=discord.Color.purple(),
                )
                for mem in memories[:25]:
                    value = mem.get("value", "")
                    value_preview = value[:200] + "..." if len(value) > 200 else value
                    embed.add_field(name=mem.get("key", "?"), value=value_preview, inline=False)

                if len(memories) > 25:
                    embed.set_footer(text=f"Showing 25 of {len(memories)} memories")

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing memories: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @memory_group.command(name="save", description="Save a memory about you")
        @app_commands.describe(
            key="Memory name (e.g., 'favorite_language')",
            value="Memory content (up to 1000 chars)",
        )
        async def cmd_memory_save(interaction: discord.Interaction, key: str, value: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                await self.api.save_memory(user_id, key, value)
                await interaction.followup.send(f"Saved memory **{key}**.", ephemeral=True)
            except httpx.HTTPStatusError as e:
                await interaction.followup.send(
                    f"Failed to save: {e.response.json().get('detail', str(e))}",
                    ephemeral=True,
                )
            except Exception as e:
                logger.error(f"Error saving memory: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @memory_group.command(name="forget", description="Remove a saved memory")
        @app_commands.describe(key="The memory key to remove")
        async def cmd_memory_forget(interaction: discord.Interaction, key: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                await self.api.forget_memory(user_id, key)
                await interaction.followup.send(f"Forgot memory **{key}**.", ephemeral=True)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    await interaction.followup.send(
                        f"No memory found with key `{key}`.", ephemeral=True
                    )
                else:
                    await interaction.followup.send(f"Error: {e}", ephemeral=True)
            except Exception as e:
                logger.error(f"Error removing memory: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @memory_group.command(name="search", description="Search memories by keyword")
        @app_commands.describe(query="Search term (matches key and value)")
        async def cmd_memory_search(interaction: discord.Interaction, query: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                results = await self.api.search_memories(user_id, query)
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
                    value_preview = value[:200] + "..." if len(value) > 200 else value
                    embed.add_field(name=mem.get("key", "?"), value=value_preview, inline=False)

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error searching memories: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(memory_group)

        # --- /notepad group ---
        notepad_group = app_commands.Group(
            name="notepad", description="Per-channel persistent notes (survive compaction)"
        )

        @notepad_group.command(name="read", description="Read this channel's notepad")
        async def cmd_notepad_read(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                from ..tools.thread_notes import read_notepad
                content = read_notepad(thread_id)
                if content:
                    if len(content) > 4000:
                        content = content[:3997] + "..."
                    embed = discord.Embed(
                        title="Notepad",
                        description=content,
                        color=discord.Color.green(),
                    )
                    embed.set_footer(text=f"Thread: {thread_id} | {len(content)} chars")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(
                        "Notepad is empty for this channel.", ephemeral=True
                    )
            except Exception as e:
                logger.error(f"Error reading notepad: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @notepad_group.command(name="write", description="Write to this channel's notepad")
        @app_commands.describe(
            content="Text to add to the notepad",
            mode="'append' (default) or 'replace'",
        )
        @app_commands.choices(mode=[
            app_commands.Choice(name="append", value="append"),
            app_commands.Choice(name="replace", value="replace"),
        ])
        async def cmd_notepad_write(
            interaction: discord.Interaction,
            content: str,
            mode: app_commands.Choice[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            write_mode = mode.value if mode else "append"
            try:
                from ..tools.thread_notes import (
                    read_notepad, _notepad_path, MAX_NOTEPAD_SIZE
                )
                path = _notepad_path(thread_id)

                if write_mode == "append":
                    existing = path.read_text(encoding="utf-8") if path.exists() else ""
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

        @notepad_group.command(name="clear", description="Clear this channel's notepad")
        async def cmd_notepad_clear(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                from ..tools.thread_notes import delete_notepad
                if delete_notepad(thread_id):
                    await interaction.followup.send("Notepad cleared.", ephemeral=True)
                else:
                    await interaction.followup.send("Notepad was already empty.", ephemeral=True)
            except Exception as e:
                logger.error(f"Error clearing notepad: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(notepad_group)

        # --- /help ---
        @self.tree.command(name="help", description="Show Nymeria bot commands")
        async def cmd_help(interaction: discord.Interaction):
            embed = discord.Embed(
                title="Nymeria Bot Commands",
                description="Chat with Nymeria by @mentioning it or using `/ask`.",
                color=discord.Color.purple(),
            )
            embed.add_field(name="/ask <message>", value="Send a message without @mentioning", inline=False)
            embed.add_field(name="/stop", value="Abort the current running operation", inline=False)
            embed.add_field(name="/clear", value="Wipe conversation history for this channel", inline=False)
            embed.add_field(name="/compact", value="Compress conversation to save context", inline=False)
            embed.add_field(name="/model [name] [scope]", value="Show or change the LLM model (global or per-channel)", inline=False)
            embed.add_field(name="/models", value="List available models from the provider", inline=False)
            embed.add_field(name="/think [off|on|low|medium|high]", value="Show or set thinking/reasoning mode", inline=False)
            embed.add_field(name="/config show | get | set", value="View and update Nymeria settings", inline=False)
            embed.add_field(name="/tools core | enabled | optional | category | enable | disable", value="View and manage available tools per-channel", inline=False)
            embed.add_field(name="/memory list | save | forget | search", value="Manage persistent memories about you", inline=False)
            embed.add_field(name="/notepad read | write | clear", value="Per-channel persistent notes (survive compaction)", inline=False)
            embed.add_field(name="/todos add | list | complete | delete", value="Scheduled tasks and reminders (with repeat intervals)", inline=False)
            embed.add_field(name="/thread", value="Show thread info (tokens, compactions)", inline=False)
            embed.add_field(name="/status", value="Show comprehensive system status", inline=False)
            embed.add_field(name="/channel-context", value="Toggle whether Nymeria reads recent channel messages", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def setup_hook(self) -> None:
        """No-op — commands are synced per-guild in on_ready."""
        pass

    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        logger.info(f"Discord bot ready as {self.user} (ID: {self.user.id})")
        print(f"\nDiscord bot ready as {self.user}")
        print(f"  Bot ID: {self.user.id}")
        print(f"  Guilds: {len(self.guilds)}")
        print(f"  Respond mode: {self.respond_mode}")
        print(f"  API: {self.api.base_url}")
        for guild in self.guilds:
            print(f"  - {guild.name} (ID: {guild.id})")

        # Sync per-guild only (instant updates, no duplicates).
        await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        logger.info("Global commands cleared from Discord")
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Slash commands synced to guild: {guild.name}")

        # Start SSE listener for autonomous task results
        print(f"  Event source: API SSE ({self.api.base_url}/autonomous/stream)")
        self.loop.create_task(self._api_sse_listener())

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages."""
        if message.author == self.user or message.author.bot:
            return

        is_dm = message.guild is None
        content = message.content

        if is_dm:
            pass
        elif self.respond_mode == "all":
            pass
        else:
            if not self.user or self.user not in message.mentions:
                return
            content = re.sub(rf"<@!?{self.user.id}>\s*", "", content).strip()

        if not content.strip():
            return

        guild_id = message.guild.id if message.guild else None
        thread_id = make_thread_id(guild_id, message.channel.id)
        user_id = make_user_id(message.author.id)

        # Fetch recent channel messages as context (if enabled)
        context = ""
        if self._context_enabled.get(message.channel.id, True):
            bot_id = self.user.id if self.user else None
            context = await fetch_channel_context(
                message.channel, before=message, bot_user_id=bot_id
            )
        content_with_context = f"{context}{content}" if context else content

        # Show typing indicator while processing
        async with message.channel.typing():
            try:
                response = await self.api.chat(content_with_context, thread_id, user_id)
            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                response = f"Sorry, I encountered an error: {e}"

        chunks = split_message(response)
        for chunk in chunks:
            await message.channel.send(chunk)

    async def _api_sse_listener(self) -> None:
        """
        Background task that connects to the API's /autonomous/stream SSE
        endpoint to receive task completion events.
        """
        url = f"{self.api.base_url}/autonomous/stream?user_id=default&api_key={self.api.api_key}"

        logger.info(f"API SSE listener connecting to {self.api.base_url}/autonomous/stream")

        reconnect_delay = 3
        max_delay = 30

        while not self.is_closed():
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            logger.error(f"SSE connection failed: {resp.status_code}")
                            await asyncio.sleep(reconnect_delay)
                            reconnect_delay = min(reconnect_delay * 2, max_delay)
                            continue

                        logger.info("API SSE connected, listening for events")
                        reconnect_delay = 3

                        async for line in resp.aiter_lines():
                            if self.is_closed():
                                return

                            if not line or not line.startswith("data: "):
                                continue

                            raw = line[6:]
                            if raw.startswith(":"):
                                continue

                            try:
                                event = _json.loads(raw)
                            except _json.JSONDecodeError:
                                continue

                            await self._handle_sse_event(event)

            except httpx.ReadTimeout:
                logger.debug("SSE read timeout, reconnecting...")
            except httpx.ConnectError:
                logger.warning(
                    f"Cannot reach API at {self.api.base_url}, retrying in {reconnect_delay}s"
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"SSE listener error: {e}", exc_info=True)

            if not self.is_closed():
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)

        logger.info("API SSE listener stopped")

    async def _handle_sse_event(self, event: Dict[str, Any]) -> None:
        """Process a single event from the API SSE stream."""
        event_type = event.get("type", "")
        thread_id = event.get("thread_id", "")

        if event_type != "task_completed":
            return
        if not thread_id.startswith("discord_"):
            return
        if event.get("error"):
            return

        parsed = parse_thread_id(thread_id)
        channel_id_str = parsed.get("channel_id")
        if not channel_id_str:
            return

        try:
            channel_id = int(channel_id_str)
            channel = self.get_channel(channel_id)
            if not channel:
                channel = await self.fetch_channel(channel_id)

            if channel and hasattr(channel, "send"):
                content = event.get("content", "Task completed.")
                task_label = event.get("task", "Scheduled task")

                embed = discord.Embed(
                    title="Autonomous Task Completed",
                    description=task_label[:256] if task_label else None,
                    color=discord.Color.teal(),
                )
                if content:
                    if len(content) > 4000:
                        content = content[:3997] + "..."
                    embed.add_field(name="Result", value=content, inline=False)

                await channel.send(embed=embed)
                logger.info(f"Posted autonomous result to channel {channel_id}")
        except Exception as e:
            logger.error(f"Error posting SSE event to Discord: {e}", exc_info=True)
