"""Discord bot trigger for two-way Nymeria communication.

Thin client architecture: the bot calls the Nymeria REST API for all
operations (chat, tools, memory, etc.) instead of running its own
NymeriaAgent. This ensures Discord always reflects the same state as
the frontend app — one agent, one source of truth.

Supports slash commands, @mention responses, DMs, and SSE streaming
for autonomous task results.
"""

import asyncio
import io
import json as _json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
import httpx
from discord import app_commands

from . import attachment_helpers
from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver, coerce_value, context_bar, fmt_tokens
from .message_splitter import split_discord_message as split_message
from .sse_consumer import parse_attach_paths

logger = logging.getLogger(__name__)


# =============================================================================
# Thread ID Helpers
# =============================================================================


def make_thread_id(guild_id: Optional[int], channel_id: int) -> str:
    """Generate a Nymeria thread ID from Discord IDs."""
    if guild_id:
        return f"discord_{guild_id}_{channel_id}"
    return f"discord_dm_{channel_id}"


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
            f"[Discord Channel Context: last {len(lines)} messages]\n"
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
        self._show_tool_calls: Dict[int, bool] = {}  # channel_id -> show tool embeds
        self._autonomous_state: Dict[str, Dict[str, Any]] = {}
        self._user_resolver = UserResolver(self.api, "discord", logger=logger)

        # Register slash commands
        self._register_commands()

    async def _request_self_restart(self) -> None:
        """Gracefully stop this bot process so the supervisor restarts it."""
        try:
            await self.api.close()
        finally:
            await self.close()

    def _register_commands(self) -> None:
        """Register all slash commands with the command tree."""

        @self.tree.command(name="ask", description="Send a message to Nymeria")
        @app_commands.describe(message="Your message to Nymeria")
        async def cmd_ask(interaction: discord.Interaction, message: str):
            await interaction.response.defer()
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
            context = ""
            if self._context_enabled.get(interaction.channel_id, True):
                bot_id = self.user.id if self.user else None
                context = await fetch_channel_context(
                    interaction.channel, bot_user_id=bot_id
                )
            message_with_context = f"{context}{message}" if context else message

            async def _first(content: str) -> discord.Message:
                return await interaction.followup.send(content, wait=True)

            await self._stream_to_channel(
                channel=interaction.channel,
                first_send=_first,
                message=message_with_context,
                thread_id=thread_id,
                user_id=user_id,
            )

        @self.tree.command(name="clear", description="Clear conversation history (preserves notepad + tool config)")
        async def cmd_clear(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
            try:
                await self.api.clear_thread(thread_id, user_id)
                await interaction.followup.send(
                    "Conversation history cleared. Notepad and tool config preserved.",
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
                user_id = await self._resolve_or_reject_interaction(interaction)
                if user_id is None:
                    return
                # Fetch all data in parallel
                settings, context, tools_data, todos = await asyncio.gather(
                    self.api.get_settings(),
                    self.api.get_context_stats(thread_id),
                    self.api.get_default_tools(),
                    self.api.list_todos(user_id),
                    return_exceptions=True,
                )

                # Handle errors gracefully
                if isinstance(settings, Exception):
                    settings = {}
                if isinstance(context, Exception):
                    context = {}
                if isinstance(tools_data, Exception):
                    tools_data = {}
                if isinstance(todos, Exception):
                    todos = []

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
                    context_bar(usage_pct, code=True),
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

                # Tasks
                if todos:
                    t_pending = sum(1 for t in todos if t.get("status") == "pending")
                    t_in_prog = sum(1 for t in todos if t.get("status") == "in_progress")
                    t_done = sum(1 for t in todos if t.get("status") == "done")
                    task_parts = []
                    if t_pending:
                        task_parts.append(f"{t_pending} pending")
                    if t_in_prog:
                        task_parts.append(f"{t_in_prog} in progress")
                    if t_done:
                        task_parts.append(f"{t_done} done")
                    embed.add_field(
                        name="Tasks",
                        value=" / ".join(task_parts) if task_parts else "none",
                        inline=True,
                    )

                # Discord
                ctx_enabled = self._context_enabled.get(interaction.channel_id, True)
                discord_lines = [
                    f"respond: {self.respond_mode}",
                    f"channel context: {'on' if ctx_enabled else 'off'}",
                ]
                embed.add_field(
                    name="Discord",
                    value=" | ".join(discord_lines),
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
            try:
                if name is None:
                    # Show current model
                    settings = await self.api.get_settings()
                    thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
                    tc = await self.api.get_thread_config(thread_id, user_id=user_id)
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
                            thread_id, user_id=user_id, llm_config={"model": name}
                        )
                        await interaction.followup.send(
                            f"Model for this channel set to `{name}`.", ephemeral=True
                        )
                    else:
                        # Global model change — admin only.
                        if await self._resolve_or_reject_interaction(
                            interaction, require_admin=True
                        ) is None:
                            return
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
            # /think mutates global settings via update_settings(); without
            # this gate any platform user could toggle reasoning effort for
            # every Nymeria user, since the bot calls the API with the admin
            # service token.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
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
            # Admin-gated: /config and /env touch global settings/secrets.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
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
            # Admin-gated: /config and /env touch global settings/secrets.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
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
            # Admin-gated: /config and /env touch global settings/secrets.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
            try:
                parsed = coerce_value(value)

                result = await self.api.update_settings(**{key: parsed})
                msg = f"**{key}** set to `{parsed}`."
                if result.get("restart_required"):
                    msg += "\nThis change requires `/restart api` to take effect."
                await interaction.followup.send(msg, ephemeral=True)
            except httpx.HTTPStatusError as e:
                detail = e.response.json().get("detail", str(e)) if e.response else str(e)
                await interaction.followup.send(f"Error: {detail}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(config_group)

        # --- /env group ---
        env_group = app_commands.Group(
            name="env", description="View and set environment variables"
        )

        @env_group.command(name="show", description="Show all environment variables (secrets masked)")
        async def cmd_env_show(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            # Admin-gated: /config and /env touch global settings/secrets.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
            try:
                data = await self.api.get_env_vars()
                entries = data.get("entries", [])

                # Group by category
                by_cat: Dict[str, list] = {}
                for e in entries:
                    by_cat.setdefault(e["category"], []).append(e)

                embed = discord.Embed(
                    title="Environment Variables",
                    description=f"{len(entries)} variables ({sum(1 for e in entries if e['is_set'])} set)",
                    color=discord.Color.greyple(),
                )

                for cat, items in by_cat.items():
                    lines = []
                    for e in items:
                        if e["is_set"]:
                            val = e["value"]
                            if e["is_secret"]:
                                lines.append(f"\U0001f512 `{e['name']}` = `{val}`")
                            else:
                                lines.append(f"\u2705 `{e['name']}` = `{val}`")
                        else:
                            lines.append(f"\u274c `{e['name']}`")
                    text = "\n".join(lines)
                    if len(text) > 1024:
                        text = text[:1020] + "..."
                    embed.add_field(name=cat, value=text, inline=False)

                embed.set_footer(text="Use /env get <key> for unmasked values")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error showing env vars: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @env_group.command(name="get", description="Get an environment variable (unmasked)")
        @app_commands.describe(key="Variable name (e.g., perplexity_api_key)")
        async def cmd_env_get(interaction: discord.Interaction, key: str):
            await interaction.response.defer(ephemeral=True)
            # Admin-gated: /config and /env touch global settings/secrets.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
            try:
                data = await self.api.get_env_var(key)
                val = data.get("value")
                name = data.get("name", key)
                if val:
                    await interaction.followup.send(
                        f"**{name}** = `{val}`", ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        f"**{name}** is not set.", ephemeral=True
                    )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    await interaction.followup.send(
                        f"Unknown variable `{key}`.", ephemeral=True
                    )
                else:
                    await interaction.followup.send(f"Error: {e}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @env_group.command(name="set", description="Set an environment variable")
        @app_commands.describe(
            key="Variable name (e.g., perplexity_api_key)",
            value="New value",
        )
        async def cmd_env_set(interaction: discord.Interaction, key: str, value: str):
            await interaction.response.defer(ephemeral=True)
            # Admin-gated: /config and /env touch global settings/secrets.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return
            try:
                parsed = coerce_value(value)

                result = await self.api.update_settings(**{key: parsed})
                msg = f"**{key}** set to `{parsed}`."
                if result.get("restart_required"):
                    msg += "\nThis change requires `/restart api` to take effect."
                await interaction.followup.send(msg, ephemeral=True)
            except httpx.HTTPStatusError as e:
                detail = e.response.json().get("detail", str(e)) if e.response else str(e)
                await interaction.followup.send(f"Error: {detail}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(env_group)

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

        @self.tree.command(name="show-tools", description="Toggle whether tool calls are shown in chat")
        async def cmd_show_tools(interaction: discord.Interaction):
            channel_id = interaction.channel_id
            currently_shown = self._show_tool_calls.get(channel_id, False)
            new_state = not currently_shown
            self._show_tool_calls[channel_id] = new_state
            state_str = "shown" if new_state else "hidden"
            await interaction.response.send_message(
                f"Tool calls are now **{state_str}** in this channel.\n"
                f"{'Tool names, arguments, and results will appear as embeds during responses.' if new_state else 'Only the final response text will be shown.'}",
                ephemeral=True,
            )

        # --- /export ---
        @self.tree.command(name="export", description="Export conversation history as a file")
        @app_commands.describe(
            format="Output format (default: markdown)",
        )
        @app_commands.choices(format=[
            app_commands.Choice(name="markdown", value="markdown"),
            app_commands.Choice(name="json", value="json"),
            app_commands.Choice(name="txt", value="txt"),
        ])
        async def cmd_export(
            interaction: discord.Interaction,
            format: app_commands.Choice[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            fmt = format.value if format else "markdown"
            try:
                data = await self.api.get_history(thread_id)
                messages = data.get("messages", [])

                if not messages:
                    await interaction.followup.send(
                        "No conversation history to export.", ephemeral=True
                    )
                    return

                # Format the messages
                if fmt == "json":
                    content = _json.dumps(messages, indent=2, ensure_ascii=False)
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
                                    lines.append(f"  [Thinking] {step.get('content', '')}")
                                elif stype == "tool_call":
                                    name = step.get("name", "?")
                                    args = step.get("arguments") or {}
                                    result = step.get("result", "")
                                    args_str = _json.dumps(args, ensure_ascii=False) if args else ""
                                    lines.append(f"  [Tool: {name}] {args_str}")
                                    if result:
                                        lines.append(f"    → {str(result)[:200]}")
                                elif stype == "response":
                                    lines.append(step.get("content", ""))
                        else:
                            text = msg.get("content", "")
                            if isinstance(text, list):
                                text = "\n".join(
                                    b.get("text", "") for b in text
                                    if isinstance(b, dict) and b.get("text")
                                )
                            lines.append(f"[{role}] {text}")
                        lines.append("")
                    content = "\n".join(lines)
                    ext = "txt"
                else:
                    # Markdown (default)
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
                                            result_str = result_str[:497] + "..."
                                        parts.append(f"**Result:**\n```\n{result_str}\n```")
                                elif stype == "response":
                                    parts.append(step.get("content", ""))
                        else:
                            text = msg.get("content", "")
                            if isinstance(text, list):
                                text = "\n".join(
                                    b.get("text", "") for b in text
                                    if isinstance(b, dict) and b.get("text")
                                )
                            parts.append(f"### {role}\n\n{text}")
                        parts.append("---")
                    content = "\n\n".join(parts)
                    ext = "md"

                # Build filename
                channel_name = "export"
                if hasattr(interaction.channel, "name") and interaction.channel.name:
                    channel_name = interaction.channel.name
                date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
                filename = f"nymeria-{channel_name}-{date_str}.{ext}"

                # Check size (Discord 25MB limit)
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

        # --- /tasks ---
        @self.tree.command(name="tasks", description="Quick view of scheduled and autonomous tasks")
        @app_commands.describe(status="Filter by status (default: active)")
        @app_commands.choices(status=[
            app_commands.Choice(name="active (pending + in progress)", value="active"),
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="in progress", value="in_progress"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ])
        async def cmd_tasks(
            interaction: discord.Interaction,
            status: app_commands.Choice[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
            try:
                items = await self.api.list_todos(user_id)
                filter_val = status.value if status else "active"

                if filter_val == "active":
                    items = [i for i in items if i.get("status") != "done"]
                elif filter_val != "all":
                    items = [i for i in items if i.get("status") == filter_val]

                if not items:
                    await interaction.followup.send(
                        f"No {filter_val} tasks.", ephemeral=True
                    )
                    return

                # Count by status
                pending = sum(1 for i in items if i.get("status") == "pending")
                in_prog = sum(1 for i in items if i.get("status") == "in_progress")
                done = sum(1 for i in items if i.get("status") == "done")
                parts = []
                if pending:
                    parts.append(f"{pending} pending")
                if in_prog:
                    parts.append(f"{in_prog} in progress")
                if done:
                    parts.append(f"{done} done")
                summary = f"{len(items)} tasks ({', '.join(parts)})" if parts else f"{len(items)} tasks"

                # Sort: scheduled items first (by scheduled_for asc), then non-scheduled
                def sort_key(item):
                    s = item.get("scheduled_for") or ""
                    return (0 if s else 1, s)
                items.sort(key=sort_key)

                embed = discord.Embed(
                    title="Scheduled Tasks",
                    description=summary,
                    color=discord.Color.orange(),
                )

                status_icons = {"pending": "\u23f3", "in_progress": "\u25b6", "done": "\u2705"}
                for item in items[:10]:
                    st = item.get("status", "pending")
                    icon = status_icons.get(st, "?")
                    task = item.get("task", "")[:60]

                    detail_parts = []
                    scheduled = item.get("scheduled_for")
                    if scheduled:
                        detail_parts.append(f"fires: {scheduled[:16]}")
                    recurrence = item.get("recurrence")
                    if recurrence:
                        detail_parts.append(f"repeat: {recurrence}")
                    thread = item.get("thread_id")
                    if thread:
                        detail_parts.append(f"thread: `{thread[:25]}`")

                    embed.add_field(
                        name=f"{icon} {task}",
                        value=" | ".join(detail_parts) if detail_parts else "no schedule",
                        inline=False,
                    )

                if len(items) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(items)} \u2014 use /todos list for full view")
                else:
                    embed.set_footer(text="Use /todos for full task management")

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing tasks: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        # --- /context ---
        @self.tree.command(name="context", description="Detailed context breakdown for this channel")
        async def cmd_context(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                # Fetch all data in parallel
                context, thread_cfg, settings, categories, tools_data = await asyncio.gather(
                    self.api.get_context_stats(thread_id),
                    self.api.get_thread_config(thread_id),
                    self.api.get_settings(),
                    self.api.get_tool_categories(),
                    self.api.get_default_tools(),
                    return_exceptions=True,
                )

                # Handle errors gracefully
                if isinstance(context, Exception):
                    context = {}
                if isinstance(thread_cfg, Exception):
                    thread_cfg = None
                if isinstance(settings, Exception):
                    settings = {}
                if isinstance(categories, Exception):
                    categories = {}
                if isinstance(tools_data, Exception):
                    tools_data = {}

                embed = discord.Embed(
                    title="Context Breakdown",
                    color=discord.Color.teal(),
                )

                # --- Model ---
                effective_model = context.get("model") or settings.get("llm_model", "?")
                provider = settings.get("llm_provider", "?")
                base_url = settings.get("llm_base_url")
                if base_url and "cli-proxy" in base_url:
                    provider_str = f"{provider} (via CLIProxy)"
                elif base_url:
                    provider_str = f"{provider} ({base_url})"
                else:
                    provider_str = provider

                model_lines = [f"`{effective_model}` | {provider_str}"]

                # Check for thread-level model override
                if thread_cfg:
                    llm_cfg = thread_cfg.get("llm_config") or {}
                    thread_model = llm_cfg.get("model")
                    if thread_model and thread_model != settings.get("llm_model"):
                        model_lines.append(f"\u26a0\ufe0f thread override: model=`{thread_model}`")
                    thread_temp = llm_cfg.get("temperature")
                    if thread_temp is not None:
                        model_lines.append(f"temperature: {thread_temp}")

                embed.add_field(
                    name="Model",
                    value="\n".join(model_lines),
                    inline=False,
                )

                # --- Context Window ---
                total_tokens = context.get("total_tokens", 0)
                context_limit = context.get("context_limit", 0)
                usage_pct = context.get("usage_percentage", 0)
                cumulative = context.get("cumulative_tokens", 0)
                compactions = context.get("compaction_count", 0)
                last_compact = context.get("last_compaction")
                ctx_mode = context.get("context_management", settings.get("context_management", "?"))

                ctx_lines = [
                    context_bar(usage_pct, code=True),
                    f"{fmt_tokens(total_tokens)} / {fmt_tokens(context_limit)} tokens",
                ]
                if cumulative:
                    ctx_lines[-1] += f" (cumulative: {fmt_tokens(cumulative)})"
                compact_parts = []
                if compactions:
                    compact_parts.append(f"{compactions} compaction{'s' if compactions != 1 else ''}")
                if last_compact:
                    # Show truncated ISO timestamp
                    compact_parts.append(f"last: {last_compact[:16]}")
                if compact_parts:
                    ctx_lines.append(" | ".join(compact_parts))
                mode_str = f"mode: {ctx_mode}"
                compact_threshold = settings.get("compact_threshold")
                if compact_threshold and ctx_mode == "auto_compact":
                    mode_str += f" (threshold {int(compact_threshold * 100)}%)"
                ctx_lines.append(mode_str)

                embed.add_field(
                    name="Context Window",
                    value="\n".join(ctx_lines),
                    inline=False,
                )

                # --- Tools ---
                default_tools = set(tools_data.get("default_tools", []))
                available_tools = tools_data.get("available_tools", [])
                cats = categories.get("categories", {}) if isinstance(categories, dict) else {}

                # Determine effective enabled tools for this thread
                disabled = set()
                extra_enabled = set()
                if thread_cfg:
                    disabled = set(thread_cfg.get("disabled_tools") or [])
                    extra_enabled = set(thread_cfg.get("enabled_tools") or [])

                effective_enabled = (default_tools - disabled) | extra_enabled
                total_available = len(available_tools)

                tool_lines = [f"{len(effective_enabled)} enabled (of {total_available} available)"]

                # Show categories with counts
                cat_parts = []
                for cat_name in sorted(cats.keys()):
                    cat_tools = set(cats[cat_name])
                    enabled_in_cat = len(cat_tools & effective_enabled)
                    total_in_cat = len(cat_tools)
                    if enabled_in_cat == total_in_cat:
                        cat_parts.append(f"{cat_name}: {total_in_cat}")
                    else:
                        cat_parts.append(f"{cat_name}: {enabled_in_cat}/{total_in_cat}")
                # Join categories into compact lines, ~3 per line
                while cat_parts:
                    chunk = cat_parts[:3]
                    cat_parts = cat_parts[3:]
                    tool_lines.append(" | ".join(chunk))
                    if len(tool_lines) >= 9:  # Cap to avoid embed overflow
                        remaining = len(cat_parts)
                        if remaining:
                            tool_lines.append(f"...and {remaining} more categories")
                        break

                embed.add_field(
                    name="Tools",
                    value="\n".join(tool_lines),
                    inline=False,
                )

                # --- Thread Overrides ---
                override_lines = []
                if thread_cfg:
                    instructions = thread_cfg.get("instructions")
                    if instructions:
                        override_lines.append(f"instructions: {len(instructions)} chars")
                    if disabled:
                        override_lines.append(f"disabled: {', '.join(sorted(disabled)[:8])}")
                        if len(disabled) > 8:
                            override_lines[-1] += f" (+{len(disabled) - 8} more)"
                    if extra_enabled:
                        override_lines.append(f"enabled: {', '.join(sorted(extra_enabled)[:8])}")
                        if len(extra_enabled) > 8:
                            override_lines[-1] += f" (+{len(extra_enabled) - 8} more)"
                    if thread_cfg.get("inject_todos_in_prompt"):
                        override_lines.append("inject TODOs: yes")
                    sys_prompt = thread_cfg.get("system_prompt")
                    if sys_prompt:
                        override_lines.append(f"custom system prompt: {len(sys_prompt)} chars")
                    if thread_cfg.get("callable"):
                        cname = thread_cfg.get("callable_name", "?")
                        override_lines.append(f"callable as: {cname}")

                if not override_lines:
                    override_lines.append("None \u2014 using global defaults")

                embed.add_field(
                    name="Thread Overrides",
                    value="\n".join(override_lines),
                    inline=False,
                )

                embed.set_footer(text=f"thread: {thread_id}")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error getting context: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

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

        # --- /restart ---
        @self.tree.command(name="restart", description="Restart a Nymeria service")
        @app_commands.describe(target="What to restart (default: bot)")
        @app_commands.choices(target=[
            app_commands.Choice(name="bot (Discord bot)", value="bot"),
            app_commands.Choice(name="api (API server)", value="api"),
        ])
        async def cmd_restart(
            interaction: discord.Interaction,
            target: app_commands.Choice[str] = None,
        ):
            # Admin-gated: restart affects every user, so non-admins are
            # rejected even if they're linked Nymeria users.
            if await self._resolve_or_reject_interaction(
                interaction, require_admin=True
            ) is None:
                return

            target_value = target.value if target else "bot"

            if target_value == "api":
                await interaction.response.send_message(
                    "Restarting API server...", ephemeral=True
                )
                try:
                    await self.api.restart_api()
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError):
                    # API process died before sending response — that means it worked
                    pass
                except Exception as e:
                    logger.error(f"Error restarting API: {e}", exc_info=True)
                    await interaction.followup.send(
                        f"Error: {e}", ephemeral=True
                    )
            else:
                # Bot self-restart: send message, then exit.
                # Docker restart policy (unless-stopped) brings us back.
                await interaction.response.send_message(
                    "Restarting bot... (back in a few seconds)", ephemeral=True
                )
                logger.info("Bot restart requested via /restart command")
                await self._request_self_restart()

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
                    description=f"{len(default_names)} tools enabled by default for new threads",
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
                        f"{total_optional} tools across {len(cats)} categories, "
                        f"disabled by default for new threads.\n"
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
                        label = f"{cat_name} (category: {len(tools)} tools)"
                        choices.append(app_commands.Choice(name=label[:100], value=cat_name))

                # Then individual tools
                for t in available:
                    tool_name = t["name"]
                    if current_lower in tool_name.lower():
                        desc = (t.get("description") or "").split("\n")[0][:60]
                        label = f"{tool_name}: {desc}" if desc else tool_name
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                tool_names, is_category, cat_name, error = await _resolve_tool_names(self, name)
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return

                # Read current thread config
                tc = await self.api.get_thread_config(thread_id, user_id=user_id)
                current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
                current_disabled = set(tc.get("disabled_tools", [])) if tc else set()

                # Add to enabled, remove from disabled
                new_enabled = current_enabled | set(tool_names)
                new_disabled = current_disabled - set(tool_names)

                await self.api.update_thread_config(
                    thread_id,
                    user_id=user_id,
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
            try:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                tool_names, is_category, cat_name, error = await _resolve_tool_names(self, name)
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return

                # Read current thread config
                tc = await self.api.get_thread_config(thread_id, user_id=user_id)
                current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
                current_disabled = set(tc.get("disabled_tools", [])) if tc else set()

                # Remove from enabled, add to disabled
                new_enabled = current_enabled - set(tool_names)
                new_disabled = current_disabled | set(tool_names)

                await self.api.update_thread_config(
                    thread_id,
                    user_id=user_id,
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            user_id = await self._resolve_or_reject_interaction(interaction)
            if user_id is None:
                return
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
            embed.add_field(name="/restart [bot|api]", value="Restart the Discord bot or API server", inline=False)
            embed.add_field(name="/clear", value="Clear conversation history (preserves notepad + tools)", inline=False)
            embed.add_field(name="/compact", value="Compress conversation to save context", inline=False)
            embed.add_field(name="/model [name] [scope]", value="Show or change the LLM model (global or per-channel)", inline=False)
            embed.add_field(name="/models", value="List available models from the provider", inline=False)
            embed.add_field(name="/think [off|on|low|medium|high]", value="Show or set thinking/reasoning mode", inline=False)
            embed.add_field(name="/config show | get | set", value="View and update Nymeria settings", inline=False)
            embed.add_field(name="/env show | get | set", value="View and set environment variables (API keys, infrastructure)", inline=False)
            embed.add_field(name="/tools core | enabled | optional | category | enable | disable", value="View and manage available tools per-channel", inline=False)
            embed.add_field(name="/memory list | save | forget | search", value="Manage persistent memories about you", inline=False)
            embed.add_field(name="/notepad read | write | clear", value="Per-channel persistent notes (survive compaction)", inline=False)
            embed.add_field(name="/todos add | list | complete | delete", value="Scheduled tasks and reminders (with repeat intervals)", inline=False)
            embed.add_field(name="/thread", value="Show thread info (tokens, compactions)", inline=False)
            embed.add_field(name="/context", value="Detailed context breakdown (model, tools, overrides, tokens)", inline=False)
            embed.add_field(name="/status", value="Comprehensive system status (model, context, tools, tasks)", inline=False)
            embed.add_field(name="/tasks [status]", value="Quick view of scheduled and autonomous tasks", inline=False)
            embed.add_field(name="/export [format]", value="Export conversation history (markdown, json, txt)", inline=False)
            embed.add_field(name="/show-tools", value="Toggle whether tool calls are shown in chat (off by default)", inline=False)
            embed.add_field(name="/channel-context", value="Toggle whether Nymeria reads recent channel messages", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # =========================================================================
    # Platform identity resolution
    # =========================================================================

    async def resolve_user_id(self, discord_user_id: int) -> Optional[str]:
        """
        Resolve a Discord user id to the Nymeria account it's linked to.
        Returns ``None`` for unlinked Discord users or transient lookup
        failures.
        """
        return await self._user_resolver.resolve(discord_user_id)

    async def _reject_unlinked(self, message: "discord.Message") -> None:
        """Reply to an unlinked Discord user with a polite rejection."""
        try:
            await message.channel.send(
                "This Discord account isn't linked to a Nymeria user yet. "
                "Ask the admin to run: "
                f"`python run.py users link-platform <email> discord {message.author.id}`"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not send unlinked rejection: %s", e)

    async def _resolve_or_reject_interaction(
        self,
        interaction: "discord.Interaction",
        *,
        require_admin: bool = False,
    ) -> Optional[str]:
        """Resolve the slash-command caller's Nymeria user_id.

        Returns ``user_id`` on success. On failure (unlinked Discord user, or
        ``require_admin=True`` and caller isn't an admin) sends an ephemeral
        rejection through the interaction and returns ``None``. The caller
        should ``return`` immediately when this returns ``None``.

        Handles both pre-defer and post-defer states — if the interaction has
        already been responded to (e.g. ``defer(ephemeral=True)``), uses
        followup; otherwise responds directly.
        """
        user_id = await self.resolve_user_id(interaction.user.id)

        async def _send(msg: str) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not send rejection: %s", e)

        if user_id is None:
            await _send(
                "This Discord account isn't linked to a Nymeria user yet. "
                "Ask the admin to run: "
                f"`python run.py users link-platform <email> discord {interaction.user.id}`"
            )
            return None

        if require_admin:
            try:
                me = await self.api.get_me(act_as=user_id)
                if me.get("role") != "admin":
                    await _send("Admin only.")
                    return None
            except Exception as e:  # noqa: BLE001
                logger.warning("Admin check failed for %s: %s", user_id, e)
                await _send("Couldn't verify permissions; try again later.")
                return None

        return user_id

    # =========================================================================
    # Streaming chat dispatcher
    # =========================================================================

    async def _stream_to_channel(
        self,
        channel: Any,
        first_send,  # Callable[[str], Awaitable[discord.Message]]
        message: str,
        thread_id: str,
        user_id: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Stream SSE chat events to a Discord channel as multiple messages.

        Args:
            channel: Discord channel to send messages to.
            first_send: Callable for the first message (interaction.followup.send
                        for /ask, channel.send for @mentions).
            message: The user message to send to the agent.
            thread_id: Nymeria thread ID.
            user_id: Nymeria user ID.
        """
        EDIT_INTERVAL = 1.5  # seconds between message edits

        # Per-invocation state
        text_buffer = ""
        current_msg: Optional[discord.Message] = None
        last_edit = 0.0
        tool_call_count = 0
        first_sent = False
        tool_msgs: Dict[str, discord.Message] = {}

        async def _send(content: str) -> discord.Message:
            """Send a message, using first_send for the first one."""
            nonlocal first_sent
            if not first_sent:
                first_sent = True
                return await first_send(content)
            return await channel.send(content)

        async def _flush_buffer(final: bool = False):
            """Send or edit the current text buffer to Discord."""
            nonlocal text_buffer, current_msg, last_edit
            if not text_buffer:
                if final:
                    current_msg = None
                return
            try:
                if current_msg is None:
                    current_msg = await _send(text_buffer)
                    last_edit = time.monotonic()
                else:
                    await current_msg.edit(content=text_buffer)
                    last_edit = time.monotonic()
            except discord.HTTPException:
                # Edit failed (rate limit, token expired) — send new message
                try:
                    current_msg = await channel.send(text_buffer)
                    last_edit = time.monotonic()
                except Exception:
                    pass
            if final:
                text_buffer = ""
                current_msg = None

        async def _finalize_text():
            """Finalize current text segment (flush + reset for next segment)."""
            await _flush_buffer(final=True)

        async def _send_compaction_embed(
            summary: Any,
            messages_removed: int = 0,
            title: str = "Context compacted",
        ) -> None:
            summary_text = str(summary or "").strip()
            if len(summary_text) > 1000:
                summary_text = summary_text[:997].rstrip() + "..."
            embed = discord.Embed(color=discord.Color.dark_grey())
            embed.set_author(name=title)
            if messages_removed:
                embed.description = f"{messages_removed} messages summarized."
            if summary_text:
                embed.add_field(name="Summary", value=summary_text, inline=False)
            try:
                await channel.send(embed=embed)
            except Exception as e:
                logger.warning("Failed to send compaction embed: %s", e)

        try:
            async for event in self.api.chat_stream(
                message,
                thread_id,
                user_id,
                attachments=attachments,
                # Chat clients can't surface the desktop's compatibility
                # modal — auto-accept the risk when the user attached files.
                force_unsupported_attachments=bool(attachments),
            ):
                etype = event.get("type", "")
                if etype == "thinking":
                    try:
                        await channel.trigger_typing()
                    except Exception:
                        pass

                elif etype == "compacting":
                    await _finalize_text()
                    await _send(event.get("message") or "Compacting context...")

                elif etype == "compacted":
                    await _finalize_text()
                    await _send_compaction_embed(
                        event.get("summary", ""),
                        int(event.get("messages_removed") or 0),
                    )

                elif etype == "context_attached":
                    await _finalize_text()
                    await _send_compaction_embed(
                        event.get("summary", ""),
                        title="Context summary attached",
                    )

                elif etype == "response":
                    chunk = event.get("content", "")
                    if chunk:
                        text_buffer += chunk
                        # Check for message overflow
                        if len(text_buffer) > 1800:
                            await _flush_buffer(final=True)
                        # Throttled edit
                        elif time.monotonic() - last_edit >= EDIT_INTERVAL:
                            await _flush_buffer()

                elif etype == "tool_call":
                    tool_call_count += 1
                    show_tools = self._show_tool_calls.get(channel.id, False)
                    if show_tools:
                        await _finalize_text()
                        name = event.get("name", "?")
                        args = event.get("args", {})
                        args_str = _json.dumps(args, indent=2, ensure_ascii=False) if args else "—"
                        if len(args_str) > 1000:
                            args_str = args_str[:997] + "..."
                        embed = discord.Embed(
                            title=f"🔧 {name}",
                            description=f"```json\n{args_str}\n```" if args else None,
                            color=discord.Color.blue(),
                        )
                        try:
                            tool_msg = await channel.send(embed=embed)
                            tool_msgs[event.get("id", "")] = tool_msg
                        except Exception as e:
                            logger.warning(f"Failed to send tool call embed: {e}")
                    try:
                        await channel.trigger_typing()
                    except Exception:
                        pass

                elif etype == "tool_result":
                    show_tools = self._show_tool_calls.get(channel.id, False)
                    if not show_tools:
                        # Inject separator so post-tool text is visually
                        # distinct from pre-tool text within the same message
                        if text_buffer and "──────" not in text_buffer[-20:]:
                            text_buffer += "\n\n──────────────────────────────\n\n"
                    if show_tools:
                        tc_id = event.get("id", "")
                        result = event.get("result", "")
                        tool_msg = tool_msgs.get(tc_id)
                        if tool_msg:
                            result_str = str(result)
                            if len(result_str) > 1000:
                                result_str = result_str[:997] + "..."
                            try:
                                old_embed = tool_msg.embeds[0] if tool_msg.embeds else discord.Embed()
                                old_embed.color = discord.Color.green()
                                old_embed.add_field(
                                    name="Result",
                                    value=f"```\n{result_str}\n```" if result_str else "*(empty)*",
                                    inline=False,
                                )
                                await tool_msg.edit(embed=old_embed)
                            except Exception as e:
                                logger.warning(f"Failed to edit tool result: {e}")
                    for attach_path in parse_attach_paths(event.get("result", "")):
                        await self._send_workspace_attachment(channel, attach_path)

                elif etype == "tool_reload":
                    await _flush_buffer(final=True)
                    tools = event.get("tools", [])
                    ttl = event.get("ttl", "")
                    names = ", ".join(tools) if tools else "tools"
                    embed = discord.Embed(
                        description=f"**{names}** ({ttl})",
                        color=discord.Color.dark_grey(),
                    )
                    embed.set_author(name="⚙️ Tool Binding")
                    try:
                        await channel.send(embed=embed)
                    except Exception as e:
                        logger.warning(f"Failed to send tool reload embed: {e}")

                elif etype == "workspace_artifact":
                    attach_path = event.get("path")
                    if isinstance(attach_path, str) and attach_path:
                        await self._send_workspace_attachment(channel, attach_path)

                elif etype == "error":

                    await _finalize_text()
                    error_content = event.get("content", "Unknown error")
                    try:
                        await _send(f"Sorry, I encountered an error: {error_content}")
                    except Exception:
                        pass

                elif etype == "iteration_limit":
                    content = event.get("content", "")
                    if content:
                        try:
                            await channel.send(f"⚠️ {content}")
                        except Exception:
                            pass

                elif etype == "done":

                    # Append tool call footer to remaining buffer
                    if tool_call_count and text_buffer:
                        text_buffer += f"\n\n-# Tool calls: {tool_call_count}"
                    elif tool_call_count and current_msg:
                        # Buffer empty but we have an existing message to edit
                        try:
                            old_content = current_msg.content or ""
                            await current_msg.edit(
                                content=old_content + f"\n\n-# Tool calls: {tool_call_count}"
                            )
                        except Exception:
                            pass
                    await _flush_buffer(final=True)

                # Silently ignore: queued

            # Stream ended — flush any remaining buffer
            await _clear_thinking()
            if text_buffer:
                if tool_call_count:
                    text_buffer += f"\n\n-# Tool calls: {tool_call_count}"
                await _flush_buffer(final=True)

        except Exception as e:
            logger.error(f"Streaming failed, falling back to sync: {e}", exc_info=True)
            await _clear_thinking()
            # Sync fallback
            try:
                data = await self.api.chat(
                    message,
                    thread_id,
                    user_id,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                )
                response = data.get("response", "")
                tc = data.get("tool_call_count", 0)
                if tc:
                    response += f"\n\n-# Tool calls: {tc}"
                chunks = split_message(response)
                for chunk in chunks:
                    await _send(chunk) if not first_sent else await channel.send(chunk)
                await self._send_latest_history_artifacts(channel, thread_id)
            except Exception as e2:
                logger.error(f"Sync fallback also failed: {e2}", exc_info=True)
                try:
                    await _send(f"Sorry, I encountered an error: {e2}")
                except Exception:
                    pass

    async def _send_workspace_attachment(self, channel: Any, file_path: str) -> bool:
        """Download a workspace file from the API and upload it to Discord."""
        result = await self.api.download_workspace_file(file_path)
        if result is None:
            return False

        raw_bytes, filename, _content_type = result
        buf = io.BytesIO(raw_bytes)
        buf.name = filename

        try:
            await channel.send(
                content=f"Generated file: `{filename}`",
                file=discord.File(buf, filename=filename),
            )
            return True
        except Exception as e:
            logger.warning("Failed to send Discord workspace attachment %s: %s", file_path, e)
            return False

    async def _send_latest_history_artifacts(self, channel: Any, thread_id: str) -> None:
        """Send artifact attachments from the latest assistant turn after sync fallback."""
        try:
            history = await self.api.get_history(thread_id)
        except Exception as e:
            logger.warning("Failed to load history for artifact fallback on %s: %s", thread_id, e)
            return

        messages = history.get("messages", [])
        if not isinstance(messages, list):
            return

        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue

            tool_calls = message.get("tool_calls") or []
            sent_paths: set[str] = set()

            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue

                    artifacts = tool_call.get("artifacts") or []
                    if isinstance(artifacts, list):
                        for artifact in artifacts:
                            if not isinstance(artifact, dict):
                                continue
                            path = artifact.get("path")
                            if isinstance(path, str) and path and path not in sent_paths:
                                if await self._send_workspace_attachment(channel, path):
                                    sent_paths.add(path)

                    result = tool_call.get("result", "")
                    for path in parse_attach_paths(result):
                        if path not in sent_paths and await self._send_workspace_attachment(channel, path):
                            sent_paths.add(path)
            break

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

        attachments, attach_errors = await self._collect_attachments(message)

        for err in attach_errors:
            try:
                await message.channel.send(err)
            except Exception:
                pass

        if not content.strip() and not attachments:
            return

        if not content.strip() and attachments:
            content = "[attachment]" if len(attachments) == 1 else "[attachments]"

        guild_id = message.guild.id if message.guild else None
        thread_id = make_thread_id(guild_id, message.channel.id)
        user_id = await self.resolve_user_id(message.author.id)
        if user_id is None:
            await self._reject_unlinked(message)
            return

        # Fetch recent channel messages as context (if enabled)
        context = ""
        if self._context_enabled.get(message.channel.id, True):
            bot_id = self.user.id if self.user else None
            context = await fetch_channel_context(
                message.channel, before=message, bot_user_id=bot_id
            )
        content_with_context = f"{context}{content}" if context else content

        await self._stream_to_channel(
            channel=message.channel,
            first_send=lambda content: message.channel.send(content),
            message=content_with_context,
            thread_id=thread_id,
            user_id=user_id,
            attachments=attachments or None,
        )

    async def _collect_attachments(
        self, message: discord.Message
    ) -> "tuple[List[Dict[str, Any]], List[str]]":
        """Download and validate Discord message attachments.

        Returns ``(attachments, errors)``. Mirrors the desktop frontend's
        MIME / size constraints via ``attachment_helpers``. Oversized or
        unsupported files are skipped with a short user-facing error
        instead of failing the whole message.
        """
        attachments: List[Dict[str, Any]] = []
        errors: List[str] = []

        for att in message.attachments:
            ok, size_err = attachment_helpers.size_within_limit(
                att.size, att.content_type, att.filename
            )
            if not ok and size_err:
                errors.append(size_err)
                continue
            try:
                raw = await att.read()
            except Exception as e:
                logger.warning(f"Failed to download Discord attachment {att.filename}: {e}")
                errors.append(f"Couldn't download {att.filename}. Try resending.")
                continue
            built, err = attachment_helpers.build_attachment(
                raw, att.content_type, att.filename
            )
            if built:
                attachments.append(built)
            elif err:
                errors.append(err)

        if len(attachments) > attachment_helpers.MAX_FILES_PER_MESSAGE:
            extra = len(attachments) - attachment_helpers.MAX_FILES_PER_MESSAGE
            attachments = attachments[: attachment_helpers.MAX_FILES_PER_MESSAGE]
            errors.append(
                f"Skipped {extra} extra file(s). Max is "
                f"{attachment_helpers.MAX_FILES_PER_MESSAGE} per message."
            )

        return attachments, errors

    async def _api_sse_listener(self) -> None:
        """
        Background task that connects to the API's /autonomous/stream SSE
        endpoint to receive task completion events.
        """
        # Subscribe to the firehose — every user's autonomous events reach
        # this listener and we route them to Discord channels by decoding
        # the event's thread_id prefix (`discord_<guild>_<channel>`). Works
        # because the service token is admin-role; non-admin tokens can't
        # request the wildcard and get HTTP 403.
        url = f"{self.api.base_url}/autonomous/stream"
        headers = {
            "Authorization": f"Bearer {self.api.api_key}",
            "X-Nymeria-Act-As": "*",
        }

        logger.info(f"API SSE listener connecting to {self.api.base_url}/autonomous/stream")

        reconnect_delay = 3
        max_delay = 30

        while not self.is_closed():
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url, headers=headers) as resp:
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

        if not thread_id.startswith("discord_"):
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

            if not channel or not hasattr(channel, "send"):
                return

            state = self._autonomous_state.get(thread_id)

            def _ensure_state() -> Dict[str, Any]:
                nonlocal state
                if state is None:
                    state = {
                        "channel": channel,
                        "buffer": "",
                        "current_msg": None,
                        "last_edit": 0.0,
                        "tool_count": 0,
                        "tool_msgs": {},
                        "prompt": event.get("prompt", ""),
                        "response_seen": False,
                        "sent_artifacts": set(),
                    }
                    self._autonomous_state[thread_id] = state
                else:
                    state["channel"] = channel
                    if event.get("prompt") and not state.get("prompt"):
                        state["prompt"] = event.get("prompt", "")
                return state

            async def _send_text(content: str) -> Optional[discord.Message]:
                last_msg = None
                for chunk in split_message(content):
                    last_msg = await channel.send(chunk)
                return last_msg

            async def _flush_buffer(final: bool = False) -> None:
                s = _ensure_state()
                text_buffer = s.get("buffer", "")
                if not text_buffer:
                    if final:
                        s["current_msg"] = None
                    return
                try:
                    current_msg = s.get("current_msg")
                    if len(text_buffer) > 2000:
                        current_msg = await _send_text(text_buffer)
                    elif current_msg is None:
                        current_msg = await channel.send(text_buffer)
                    else:
                        await current_msg.edit(content=text_buffer)
                    s["current_msg"] = current_msg
                    s["last_edit"] = time.monotonic()
                except discord.HTTPException:
                    try:
                        s["current_msg"] = await _send_text(text_buffer)
                        s["last_edit"] = time.monotonic()
                    except Exception:
                        pass
                if final:
                    s["buffer"] = ""
                    s["current_msg"] = None

            async def _finalize_text() -> None:
                await _flush_buffer(final=True)

            async def _send_compaction_embed(
                summary: Any,
                messages_removed: int = 0,
                title: str = "Context compacted",
            ) -> None:
                summary_text = str(summary or "").strip()
                if len(summary_text) > 1000:
                    summary_text = summary_text[:997].rstrip() + "..."
                embed = discord.Embed(color=discord.Color.dark_grey())
                embed.set_author(name=title)
                if messages_removed:
                    embed.description = f"{messages_removed} messages summarized."
                if summary_text:
                    embed.add_field(name="Summary", value=summary_text, inline=False)
                await channel.send(embed=embed)

            async def _send_workspace_attachment_once(path: str) -> None:
                if not path:
                    return
                s = _ensure_state()
                sent_artifacts = s.setdefault("sent_artifacts", set())
                if path in sent_artifacts:
                    return
                if await self._send_workspace_attachment(channel, path):
                    sent_artifacts.add(path)

            if event_type == "notification":
                if event.get("in_app_only"):
                    return
                message = (
                    event.get("message")
                    or event.get("summary")
                    or event.get("content")
                    or ""
                )
                if str(message).strip():
                    await _send_text(str(message))
                return

            if event_type == "task_started":
                _ensure_state()
                return

            if event_type == "thinking" or event_type == "tool_call_delta":
                try:
                    await channel.trigger_typing()
                except Exception:
                    pass
                return

            if event_type == "compacting":
                await _finalize_text()
                await _send_text(event.get("message") or "Compacting context...")
                return

            if event_type == "compacted":
                await _finalize_text()
                await _send_compaction_embed(
                    event.get("summary", ""),
                    int(event.get("messages_removed") or 0),
                )
                return

            if event_type == "context_attached":
                await _finalize_text()
                await _send_compaction_embed(
                    event.get("summary", ""),
                    title="Context summary attached",
                )
                return

            if event_type == "response":
                s = _ensure_state()
                chunk = event.get("content", "")
                if not chunk:
                    return
                s["response_seen"] = True
                s["buffer"] += chunk
                if len(s["buffer"]) > 1800:
                    await _flush_buffer(final=True)
                elif time.monotonic() - s.get("last_edit", 0.0) >= 1.5:
                    await _flush_buffer()
                return

            if event_type == "tool_call":
                s = _ensure_state()
                s["tool_count"] += 1
                show_tools = self._show_tool_calls.get(channel_id, False)
                if show_tools:
                    await _finalize_text()
                    name = event.get("name", "?")
                    args = event.get("args", {})
                    args_str = _json.dumps(args, indent=2, ensure_ascii=False) if args else ""
                    if len(args_str) > 1000:
                        args_str = args_str[:997] + "..."
                    embed = discord.Embed(
                        title=f"🔧 {name}",
                        description=f"```json\n{args_str}\n```" if args_str else None,
                        color=discord.Color.blue(),
                    )
                    tool_msg = await channel.send(embed=embed)
                    s["tool_msgs"][event.get("id", "")] = tool_msg
                try:
                    await channel.trigger_typing()
                except Exception:
                    pass
                return

            if event_type == "tool_result":
                s = _ensure_state()
                show_tools = self._show_tool_calls.get(channel_id, False)
                result = event.get("result", "")
                if not show_tools:
                    if s.get("buffer") and "──────" not in s["buffer"][-20:]:
                        s["buffer"] += "\n\n──────────────────────────────\n\n"
                else:
                    tc_id = event.get("id", "")
                    tool_msg = s["tool_msgs"].get(tc_id)
                    if tool_msg:
                        result_str = str(result)
                        if len(result_str) > 1000:
                            result_str = result_str[:997] + "..."
                        try:
                            old_embed = tool_msg.embeds[0] if tool_msg.embeds else discord.Embed()
                            old_embed.color = discord.Color.green()
                            old_embed.add_field(
                                name="Result",
                                value=f"```\n{result_str}\n```" if result_str else "*(empty)*",
                                inline=False,
                            )
                            await tool_msg.edit(embed=old_embed)
                        except Exception as e:
                            logger.warning(f"Failed to edit autonomous tool result: {e}")
                for attach_path in parse_attach_paths(result):
                    await _send_workspace_attachment_once(attach_path)
                return

            if event_type == "tool_reload":
                _ensure_state()
                await _flush_buffer(final=True)
                tools = event.get("tools") or []
                ttl = event.get("ttl") or ""
                names = ", ".join(str(tool) for tool in tools) if tools else "tools"
                embed = discord.Embed(
                    description=f"**{names}**{f' ({ttl})' if ttl else ''}",
                    color=discord.Color.dark_grey(),
                )
                embed.set_author(name="Tool Binding")
                await channel.send(embed=embed)
                return

            if event_type == "workspace_artifact":
                attach_path = event.get("path")
                if isinstance(attach_path, str) and attach_path:
                    await _send_workspace_attachment_once(attach_path)
                return

            if event_type == "iteration_limit":
                content = event.get("content", "")
                if content:
                    await channel.send(f"⚠️ {content}")
                return

            if event_type == "error":
                await _finalize_text()
                error_content = event.get("content", "Unknown error")
                await _send_text(f"Sorry, I encountered an error: {error_content}")
                return

            if event_type == "task_completed":
                s = _ensure_state()
                try:
                    if event.get("error"):
                        await _finalize_text()
                        err = (
                            event.get("content")
                            or event.get("error_message")
                            or "Unknown error"
                        )
                        await _send_text(f"Autonomous task error: {err}")
                    else:
                        if not s.get("response_seen") and not s.get("buffer"):
                            fallback = event.get("content") or ""
                            if fallback:
                                s["buffer"] = fallback
                        if s.get("tool_count") and s.get("buffer"):
                            s["buffer"] += f"\n\n-# Tool calls: {s['tool_count']}"
                        elif s.get("tool_count") and s.get("current_msg"):
                            try:
                                old_content = s["current_msg"].content or ""
                                await s["current_msg"].edit(
                                    content=old_content
                                    + f"\n\n-# Tool calls: {s['tool_count']}"
                                )
                            except Exception:
                                pass
                        await _flush_buffer(final=True)
                    logger.info(f"Streamed autonomous result to Discord channel {channel_id}")
                finally:
                    self._autonomous_state.pop(thread_id, None)
        except Exception as e:
            logger.error(f"Error posting SSE event to Discord: {e}", exc_info=True)
            self._autonomous_state.pop(thread_id, None)
