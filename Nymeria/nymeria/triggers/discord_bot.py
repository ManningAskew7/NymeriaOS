"""Discord bot trigger for two-way Nymeria communication.

Gateway mode (WebSocket) for local dev, webhook mode handled via webhook.py.
Supports slash commands, @mention responses, DMs, and event bus integration
for autonomous task results.
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from ..core.agent import NymeriaAgent

logger = logging.getLogger(__name__)


# =============================================================================
# Message Splitting
# =============================================================================


def split_message(content: str, max_length: int = 2000) -> List[str]:
    """
    Split a message into chunks that fit Discord's character limit.

    Preserves code blocks, paragraph boundaries, and sentence boundaries.
    Never splits mid-code-block.

    Args:
        content: The message content to split.
        max_length: Maximum length per chunk (Discord limit is 2000).

    Returns:
        List of message chunks.
    """
    if len(content) <= max_length:
        return [content]

    chunks: List[str] = []
    remaining = content

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to find a good split point
        split_at = _find_split_point(remaining, max_length)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return [c for c in chunks if c.strip()]


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best split point within max_length characters."""
    # Check if there's a code block that spans past max_length
    # If so, try to split before it starts
    code_block_start = text.rfind("```", 0, max_length)
    if code_block_start > 0:
        # Count how many ``` are before this point (odd = inside a block)
        count_before = text[:code_block_start].count("```")
        if count_before % 2 == 1:
            # We're inside a code block — find the closing ```
            closing = text.find("```", code_block_start + 3)
            if closing != -1 and closing + 3 <= len(text):
                end_of_block = closing + 3
                if end_of_block <= max_length:
                    return end_of_block

            # Code block extends past max_length — split before it opened
            block_open = text.rfind("```", 0, code_block_start)
            if block_open > max_length * 0.3:
                return block_open

    # Try paragraph boundary
    para = text.rfind("\n\n", 0, max_length)
    if para > max_length * 0.5:
        return para + 2

    # Try line boundary
    line = text.rfind("\n", 0, max_length)
    if line > max_length * 0.5:
        return line + 1

    # Try sentence boundary
    sentence = text.rfind(". ", 0, max_length)
    if sentence > max_length * 0.5:
        return sentence + 2

    # Hard split at max_length
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
) -> str:
    """
    Fetch recent messages from a Discord channel and format them as context.

    Args:
        channel: The Discord channel to fetch from.
        limit: Number of recent messages to include.
        before: Fetch messages before this message (to exclude the triggering message).

    Returns:
        Formatted string with recent channel messages, or empty string if none.
    """
    try:
        messages: List[discord.Message] = []
        async for msg in channel.history(limit=limit, before=before):
            messages.append(msg)

        if not messages:
            return ""

        # Reverse so oldest is first (history returns newest first)
        messages.reverse()

        lines = []
        for msg in messages:
            # Skip empty messages (embeds only, etc.)
            text = msg.content
            if not text:
                continue

            # Use display_name for readability
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
    """
    Parse a Discord-originated thread ID back into its components.

    Returns dict with 'guild_id' and 'channel_id' (or 'user_id' for DMs).
    """
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
    """Discord bot client for Nymeria two-way communication."""

    def __init__(
        self,
        agent: "NymeriaAgent",
        respond_mode: str = "mention",
        api_url: Optional[str] = None,
    ):
        """
        Initialize the Discord bot.

        Args:
            agent: NymeriaAgent instance for processing messages.
            respond_mode: 'mention' (only @Nymeria in guilds) or 'all' (every message).
            api_url: URL of the Nymeria API server (e.g. http://localhost:8000).
                     When set, the bot connects to the API's SSE endpoint to
                     receive autonomous task events across processes. Without
                     this, the in-memory event bus is used (only works in-process
                     or with Redis).
        """
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.dm_messages = True
        intents.guild_messages = True

        super().__init__(intents=intents)

        self.agent = agent
        self.respond_mode = respond_mode
        self.api_url = api_url.rstrip("/") if api_url else None
        self.tree = app_commands.CommandTree(self)
        self._start_time = time.time()

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

            # Fetch recent channel messages as context
            context = await fetch_channel_context(interaction.channel)
            message_with_context = f"{context}{message}" if context else message

            response = await asyncio.to_thread(
                self.agent.chat, message_with_context, thread_id, user_id
            )
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
                # Delete checkpoints directly via the checkpointer to avoid
                # triggering graph routing on empty state (which causes IndexError).
                graph = self.agent._default_graph
                checkpointer = graph.checkpointer
                # Unwrap to the underlying saver (SqliteSaver / PostgresSaver)
                saver = getattr(checkpointer, "_saver", checkpointer)
                cleared = False
                if hasattr(saver, "delete_thread"):
                    saver.delete_thread(thread_id)
                    cleared = True
                elif hasattr(saver, "conn"):
                    # Fallback: raw SQL delete for savers without delete_thread
                    cur = saver.conn.cursor()
                    cur.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                    cur.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                    saver.conn.commit()
                    cleared = True
                if cleared:
                    self.agent._token_tracker.clear(thread_id)
                    await interaction.followup.send(
                        "Conversation history cleared for this channel.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "Could not clear history — unsupported checkpointer type.",
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
                result = await self.agent.compact_now(thread_id, user_id)
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
                await interaction.followup.send(
                    f"Error: {e}", ephemeral=True
                )

        # --- /todos group ---
        todos_group = app_commands.Group(name="todos", description="Manage Nymeria TODOs")

        @todos_group.command(name="list", description="List all TODOs")
        async def cmd_todos_list(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                items = self.agent.todo_manager.list_todos(user_id)
                if not items:
                    await interaction.followup.send("No TODOs found.", ephemeral=True)
                    return

                embed = discord.Embed(
                    title="Your TODOs",
                    color=discord.Color.blue(),
                )
                for item in items[:25]:  # Discord embed limit
                    status_icon = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}.get(
                        item.status, "❓"
                    )
                    name = f"{status_icon} {item.task[:80]}"
                    value = f"ID: `{item.id[:8]}` | Status: {item.status}"
                    if item.scheduled_for:
                        value += f" | Scheduled: {item.scheduled_for}"
                    embed.add_field(name=name, value=value, inline=False)

                if len(items) > 25:
                    embed.set_footer(text=f"Showing 25 of {len(items)} TODOs")

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error listing todos: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @todos_group.command(name="add", description="Add a new TODO")
        @app_commands.describe(task="The task to add")
        async def cmd_todos_add(interaction: discord.Interaction, task: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                item = self.agent.todo_manager.add_todo(
                    user_id=user_id,
                    task=task,
                    created_by="user",
                )
                await interaction.followup.send(
                    f"Added TODO: **{task}** (ID: `{item.id[:8]}`)",
                    ephemeral=True,
                )
            except Exception as e:
                logger.error(f"Error adding todo: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @todos_group.command(name="complete", description="Mark a TODO as done")
        @app_commands.describe(todo_id="The TODO ID (first 8 chars is enough)")
        async def cmd_todos_complete(interaction: discord.Interaction, todo_id: str):
            await interaction.response.defer(ephemeral=True)
            user_id = make_user_id(interaction.user.id)
            try:
                items = self.agent.todo_manager.list_todos(user_id)
                match = None
                for item in items:
                    if item.id.startswith(todo_id):
                        match = item
                        break
                if not match:
                    await interaction.followup.send(
                        f"No TODO found matching `{todo_id}`", ephemeral=True
                    )
                    return

                self.agent.todo_manager.update_todo(
                    user_id=user_id,
                    todo_id=match.id,
                    status="done",
                )
                await interaction.followup.send(
                    f"Completed: **{match.task}**", ephemeral=True
                )
            except Exception as e:
                logger.error(f"Error completing todo: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        self.tree.add_command(todos_group)

        @self.tree.command(name="thread", description="Show current thread info")
        async def cmd_thread(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            try:
                stats = self.agent.get_context_stats(thread_id)
                embed = discord.Embed(
                    title="Thread Info",
                    color=discord.Color.green(),
                )
                embed.add_field(
                    name="Thread ID", value=f"`{thread_id}`", inline=False
                )
                embed.add_field(
                    name="Context Usage",
                    value=f"{stats['usage_percentage']}% ({stats['total_tokens']:,} / {stats['context_limit']:,} tokens)",
                    inline=True,
                )
                embed.add_field(
                    name="Compactions",
                    value=str(stats["compaction_count"]),
                    inline=True,
                )
                embed.add_field(
                    name="Context Mode",
                    value=stats["context_management"],
                    inline=True,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Error getting thread info: {e}", exc_info=True)
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="status", description="Show Nymeria system status")
        async def cmd_status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            settings = self.agent.settings
            uptime_seconds = int(time.time() - self._start_time)
            hours, remainder = divmod(uptime_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)

            embed = discord.Embed(
                title="Nymeria Status",
                color=discord.Color.gold(),
            )
            embed.add_field(
                name="Model",
                value=f"`{settings.llm_model}`",
                inline=True,
            )
            embed.add_field(
                name="Provider",
                value=settings.llm_provider,
                inline=True,
            )
            embed.add_field(
                name="Tools",
                value=str(len(self.agent.tool_registry.list_tools())),
                inline=True,
            )
            embed.add_field(
                name="Uptime",
                value=f"{hours}h {minutes}m {seconds}s",
                inline=True,
            )
            embed.add_field(
                name="Context Mode",
                value=settings.context_management,
                inline=True,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="model", description="Show current LLM model")
        async def cmd_model(interaction: discord.Interaction):
            settings = self.agent.settings
            await interaction.response.send_message(
                f"Current model: `{settings.llm_model}` (provider: {settings.llm_provider})",
                ephemeral=True,
            )

        @self.tree.command(name="help", description="Show Nymeria bot commands")
        async def cmd_help(interaction: discord.Interaction):
            embed = discord.Embed(
                title="Nymeria Bot Commands",
                description="Chat with Nymeria by @mentioning it or using `/ask`.",
                color=discord.Color.purple(),
            )
            embed.add_field(
                name="/ask <message>",
                value="Send a message without @mentioning",
                inline=False,
            )
            embed.add_field(
                name="/clear",
                value="Wipe conversation history for this channel",
                inline=False,
            )
            embed.add_field(
                name="/compact",
                value="Compress conversation to save context",
                inline=False,
            )
            embed.add_field(
                name="/todos list | add | complete",
                value="Manage your TODOs",
                inline=False,
            )
            embed.add_field(
                name="/thread",
                value="Show thread info (tokens, compactions)",
                inline=False,
            )
            embed.add_field(
                name="/status",
                value="Show system status (model, uptime, tools)",
                inline=False,
            )
            embed.add_field(
                name="/model",
                value="Show current LLM model",
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def setup_hook(self) -> None:
        """Sync slash commands when bot connects."""
        await self.tree.sync()
        logger.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        logger.info(f"Discord bot ready as {self.user} (ID: {self.user.id})")
        print(f"\nDiscord bot ready as {self.user}")
        print(f"  Bot ID: {self.user.id}")
        print(f"  Guilds: {len(self.guilds)}")
        print(f"  Respond mode: {self.respond_mode}")
        for guild in self.guilds:
            print(f"  - {guild.name} (ID: {guild.id})")

        # Start event listener for autonomous task results
        if self.api_url:
            print(f"  Event source: API SSE ({self.api_url}/autonomous/stream)")
            self.loop.create_task(self._api_sse_listener())
        else:
            print("  Event source: in-memory event bus (same-process or Redis)")
            self.loop.create_task(self._event_bus_listener())

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages."""
        # Skip own messages and other bots
        if message.author == self.user or message.author.bot:
            return

        is_dm = message.guild is None
        content = message.content

        if is_dm:
            # Always respond to DMs
            pass
        elif self.respond_mode == "all":
            # Respond to all messages in guild channels
            pass
        else:
            # Only respond when @mentioned
            if not self.user or self.user not in message.mentions:
                return
            # Strip the mention from the message
            content = re.sub(
                rf"<@!?{self.user.id}>\s*", "", content
            ).strip()

        if not content.strip():
            return

        # Generate thread/user IDs
        guild_id = message.guild.id if message.guild else None
        thread_id = make_thread_id(guild_id, message.channel.id)
        user_id = make_user_id(message.author.id)

        # Fetch recent channel messages as context (before the triggering message)
        context = await fetch_channel_context(message.channel, before=message)
        content_with_context = f"{context}{content}" if context else content

        # Show typing indicator while processing
        async with message.channel.typing():
            try:
                response = await asyncio.to_thread(
                    self.agent.chat, content_with_context, thread_id, user_id
                )
            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                response = f"Sorry, I encountered an error: {e}"

        # Split and send response
        chunks = split_message(response)
        for chunk in chunks:
            await message.channel.send(chunk)

    async def _event_bus_listener(self) -> None:
        """
        Background task that listens for autonomous task completion events
        and posts results back to the originating Discord channel.
        """
        from ..core.event_bus import get_event_bus

        subscriber_id = f"discord_bot_{self.user.id if self.user else 'init'}"
        event_bus = get_event_bus()
        queue = event_bus.subscribe(subscriber_id)

        logger.info(f"Event bus listener started (subscriber: {subscriber_id})")

        try:
            while not self.is_closed():
                try:
                    # Non-blocking poll with a short sleep
                    event = await asyncio.to_thread(queue.get, True, 1.0)
                except Exception:
                    # queue.get timeout — just loop
                    await asyncio.sleep(0.1)
                    continue

                # Only handle task_completed events for Discord threads
                if event.event_type != "task_completed":
                    continue
                if not event.thread_id.startswith("discord_"):
                    continue

                # Skip error events
                if event.data.get("error"):
                    continue

                # Parse thread ID to find the channel
                parsed = parse_thread_id(event.thread_id)
                channel_id_str = parsed.get("channel_id")
                if not channel_id_str:
                    continue

                try:
                    channel_id = int(channel_id_str)
                    channel = self.get_channel(channel_id)
                    if not channel:
                        channel = await self.fetch_channel(channel_id)

                    if channel and hasattr(channel, "send"):
                        content = event.data.get("content", "Task completed.")
                        task_label = event.data.get("task", "Scheduled task")

                        embed = discord.Embed(
                            title="Autonomous Task Completed",
                            description=task_label[:256] if task_label else None,
                            color=discord.Color.teal(),
                        )
                        if content:
                            # Truncate for embed
                            if len(content) > 4000:
                                content = content[:3997] + "..."
                            embed.add_field(
                                name="Result", value=content, inline=False
                            )

                        await channel.send(embed=embed)
                        logger.info(
                            f"Posted autonomous result to channel {channel_id}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error posting event to Discord channel: {e}",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(subscriber_id)
            logger.info("Event bus listener stopped")

    async def _api_sse_listener(self) -> None:
        """
        Background task that connects to the API's /autonomous/stream SSE
        endpoint to receive task completion events. This works across separate
        processes without Redis — same mechanism the desktop app uses.
        """
        import httpx
        import json as _json

        api_key = self.agent.settings.nymeria_api_key or ""
        url = f"{self.api_url}/autonomous/stream?user_id=default&api_key={api_key}"

        logger.info(f"API SSE listener connecting to {self.api_url}/autonomous/stream")

        reconnect_delay = 3
        max_delay = 30

        while not self.is_closed():
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            logger.error(
                                f"SSE connection failed: {resp.status_code}"
                            )
                            await asyncio.sleep(reconnect_delay)
                            reconnect_delay = min(reconnect_delay * 2, max_delay)
                            continue

                        logger.info("API SSE connected, listening for events")
                        reconnect_delay = 3  # Reset on successful connect

                        async for line in resp.aiter_lines():
                            if self.is_closed():
                                return

                            if not line or not line.startswith("data: "):
                                continue

                            raw = line[6:]  # Strip "data: " prefix
                            if raw.startswith(":"):
                                continue  # Heartbeat comment

                            try:
                                event = _json.loads(raw)
                            except _json.JSONDecodeError:
                                continue

                            await self._handle_sse_event(event)

            except httpx.ReadTimeout:
                logger.debug("SSE read timeout, reconnecting...")
            except httpx.ConnectError:
                logger.warning(
                    f"Cannot reach API at {self.api_url}, retrying in {reconnect_delay}s"
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

        # Only handle task_completed events for Discord threads
        if event_type != "task_completed":
            return
        if not thread_id.startswith("discord_"):
            return

        # Skip error events
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
                    embed.add_field(
                        name="Result", value=content, inline=False
                    )

                await channel.send(embed=embed)
                logger.info(f"Posted autonomous result to channel {channel_id}")
        except Exception as e:
            logger.error(
                f"Error posting SSE event to Discord: {e}", exc_info=True
            )
