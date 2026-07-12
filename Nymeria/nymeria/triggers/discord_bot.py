"""Discord bot trigger for two-way Nymeria communication.

Thin client architecture: the bot calls the Nymeria REST API for all
operations (chat, tools, memory, etc.) instead of running its own
NymeriaAgent. This ensures Discord always reflects the same state as
the frontend app — one agent, one source of truth.

Supports slash commands, @mention responses, DMs, and SSE streaming
for autonomous task results.
"""

# Annotations are kept lazy (PEP 563) so signatures like
# ``message: discord.Message`` do not evaluate the SDK at import time on a lean
# install that omits discord.py.
from __future__ import annotations

import asyncio
import io
import json as _json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from . import attachment_helpers
from .api_client import NymeriaAPIClient
from .bot_helpers import UserResolver
from .message_splitter import split_discord_message as split_message
from .sse_consumer import (
    consume_autonomous_firehose,
    consume_sse_stream,
    dispatch_event,
    format_hook_approval_message,
    parse_attach_paths,
)
from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat

try:  # pragma: no cover - discord.py ships in the optional nymeriaos[discord] extra.
    import discord
    from discord.ext import commands
except ImportError:  # pragma: no cover - lean installs omit discord.py.
    # Fallback is typed Any (not None) so type-checking treats these as the
    # imported SDK symbols; at runtime they are None, which SDK_AVAILABLE detects.
    _MISSING: Any = None
    discord = _MISSING
    commands = _MISSING

#: True when discord.py is importable. run.py checks this for a friendly error.
SDK_AVAILABLE = discord is not None

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

# Approve/Deny views outlive the backend hold (window ceiling 600s) by a
# margin, so a click on a just-expired prompt gets a clean "no longer
# pending" answer; on view timeout the buttons are dropped as a fallback if
# the resolved event never reached this process.
HOOK_APPROVAL_VIEW_TIMEOUT_SECONDS = 30 * 60


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


# Fall back to ``object`` so this module still imports on a lean install without
# discord.py. run.py refuses to start the bot (via SDK_AVAILABLE) before this
# class is ever instantiated, so the object base is never actually used.
# Typed Any so the dynamic base class is accepted by the type checker.
_BotBase: Any = commands.Bot if commands is not None else object


class NymeriaDiscordBot(_BotBase):
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
        # Reaction events (guild + DM) for the emoji-reaction trigger.
        # Enabled unconditionally (intents cannot be flipped after login);
        # the DISCORD_REACTION_TRIGGER_ENABLED toggle gates the handler.
        intents.reactions = True

        super().__init__(command_prefix="!", intents=intents)

        self.api = api
        self.respond_mode = respond_mode
        self._start_time = time.time()
        self._context_enabled: Dict[int, bool] = {}
        self._show_tool_calls: Dict[int, bool] = {}
        self._autonomous_state: Dict[str, Dict[str, Any]] = {}
        # record_id -> (message, view, monotonic deadline) for pending
        # hook-approval prompts, so the resolved event can edit the message.
        self._hook_approval_messages: Dict[str, tuple[Any, Any, float]] = {}
        self._user_resolver = UserResolver(self.api, "discord", logger=logger)
        self._health_task: Optional[asyncio.Task] = None

    async def _request_self_restart(self) -> None:
        """Gracefully stop this bot process so the supervisor restarts it."""
        try:
            await self.api.close()
        finally:
            await self.close()

    async def setup_hook(self) -> None:
        """Load Cog modules that register all slash commands."""
        from .discord_cogs import ALL_COGS

        for cog_cls in ALL_COGS:
            await self.add_cog(cog_cls(self))

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

    async def _send_interaction_text(
        self,
        interaction: "discord.Interaction",
        content: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        """Send text through either the initial interaction response or followup."""
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    async def _send_backend_command(
        self,
        interaction: "discord.Interaction",
        command: str,
        *,
        args: str = "",
        require_admin: bool = False,
        ephemeral: bool = True,
    ) -> Optional[dict]:
        """Execute a global backend slash command for a Discord interaction."""
        user_id = await self._resolve_or_reject_interaction(
            interaction,
            require_admin=require_admin,
        )
        if user_id is None:
            return None

        command_text = command if command.startswith("/") else f"/{command}"
        if args.strip():
            command_text = f"{command_text} {args.strip()}"
        if interaction.channel_id is None:
            return None
        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)

        try:
            result = await self.api.execute_command(
                command_text,
                thread_id=thread_id,
                source="user",
                actor="user",
                surface="discord",
                user_id=user_id,
            )
        except httpx.HTTPStatusError as e:
            detail = str(e)
            if e.response is not None:
                try:
                    detail = str(e.response.json().get("detail", detail))
                except Exception:  # noqa: BLE001
                    detail = e.response.text or detail
            await self._send_interaction_text(
                interaction,
                f"Error: {detail}",
                ephemeral=ephemeral,
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.error("Discord backend command failed: %s", command_text, exc_info=True)
            await self._send_interaction_text(
                interaction,
                f"Error: {e}",
                ephemeral=ephemeral,
            )
            return None

        markdown = str(result.get("markdown") or "").strip()
        if not markdown:
            markdown = "Done." if result.get("success", True) else "Command returned no output."

        for chunk in split_message(markdown):
            await self._send_interaction_text(
                interaction,
                chunk,
                ephemeral=ephemeral,
            )
        return result

    # =========================================================================
    # SSE handler for interactive chat (implements SSEEventHandler protocol)
    # =========================================================================

    class _InteractiveChatHandler:
        """SSE event handler for Discord interactive chat.

        Implements :class:`~triggers.sse_consumer.SSEEventHandler` and owns
        the per-stream buffer, progressive-edit, and typing state.
        """

        EDIT_INTERVAL = 1.5

        def __init__(
            self,
            bot: "NymeriaDiscordBot",
            channel: Any,
            first_send,
        ) -> None:
            self._bot = bot
            self._channel = channel
            self._first_send = first_send

            self._text_buffer = ""
            self._current_msg: Optional[discord.Message] = None
            self._last_edit = 0.0
            self._first_sent = False
            self._tool_msgs: Dict[str, discord.Message] = {}
            self._reply_suppressed = False

        async def _send(self, content: str) -> discord.Message:
            if not self._first_sent:
                self._first_sent = True
                return await self._first_send(content)
            return await self._channel.send(content)

        async def flush_text(self, final: bool = False) -> None:
            if self._reply_suppressed:
                self._text_buffer = ""
                if final:
                    self._current_msg = None
                return
            if not self._text_buffer:
                if final:
                    self._current_msg = None
                return
            try:
                if self._current_msg is None:
                    self._current_msg = await self._send(self._text_buffer)
                    self._last_edit = time.monotonic()
                else:
                    await self._current_msg.edit(content=self._text_buffer)
                    self._last_edit = time.monotonic()
            except discord.HTTPException:
                try:
                    self._current_msg = await self._channel.send(self._text_buffer)
                    self._last_edit = time.monotonic()
                except Exception:
                    logger.warning("Failed to send fallback Discord message", exc_info=True)
            if final:
                self._text_buffer = ""
                self._current_msg = None

        async def _send_compaction_embed(
            self,
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
                await self._channel.send(embed=embed)
            except Exception as e:
                logger.warning("Failed to send compaction embed: %s", e)

        # -- SSEEventHandler callbacks ----------------------------------------

        async def on_thinking(self) -> None:
            try:
                await self._channel.trigger_typing()
            except Exception:
                logger.debug("Failed to send typing indicator")

        async def on_response_chunk(self, content: str) -> None:
            if self._reply_suppressed:
                return
            self._text_buffer += content
            if len(self._text_buffer) > 1800:
                await self.flush_text(final=True)
            elif time.monotonic() - self._last_edit >= self.EDIT_INTERVAL:
                await self.flush_text()

        async def on_reply_suppressed(self) -> None:
            # The react tool asked to hide the reply: drop the pending buffer
            # and ignore any later response text. Text already flushed to the
            # channel stays (suppression is forward-looking).
            self._reply_suppressed = True
            self._text_buffer = ""

        async def on_compacting(self, message: str) -> None:
            await self._send(message)

        async def on_compacted(
            self,
            summary: str,
            messages_removed: int,
            title: str,
        ) -> None:
            await self._send_compaction_embed(summary, messages_removed, title)

        async def on_tool_call(
            self,
            name: str,
            args: Dict[str, Any],
            call_id: str,
            count: int,
        ) -> None:
            show_tools = self._bot._show_tool_calls.get(self._channel.id, False)
            if show_tools:
                await self.flush_text(final=True)
                args_str = _json.dumps(args, indent=2, ensure_ascii=False) if args else "—"
                if len(args_str) > 1000:
                    args_str = args_str[:997] + "..."
                embed = discord.Embed(
                    title=f"🔧 {name}",
                    description=f"```json\n{args_str}\n```" if args else None,
                    color=discord.Color.blue(),
                )
                try:
                    tool_msg = await self._channel.send(embed=embed)
                    self._tool_msgs[call_id] = tool_msg
                except Exception as e:
                    logger.warning(f"Failed to send tool call embed: {e}")
            try:
                await self._channel.trigger_typing()
            except Exception:
                logger.debug("Failed to send typing indicator")

        async def on_tool_result(
            self,
            call_id: str,
            result: str,
            attachments: List[str],
        ) -> None:
            show_tools = self._bot._show_tool_calls.get(self._channel.id, False)
            if not show_tools:
                if self._text_buffer and "──────" not in self._text_buffer[-20:]:
                    self._text_buffer += "\n\n──────────────────────────────\n\n"
            else:
                tool_msg = self._tool_msgs.get(call_id)
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
            for attach_path in attachments:
                await self._bot._send_workspace_attachment(self._channel, attach_path)

        async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
            names = ", ".join(tools) if tools else "tools"
            embed = discord.Embed(
                description=f"**{names}** ({ttl})",
                color=discord.Color.dark_grey(),
            )
            embed.set_author(name="⚙️ Tool Binding")
            try:
                await self._channel.send(embed=embed)
            except Exception as e:
                logger.warning(f"Failed to send tool reload embed: {e}")

        async def on_workspace_artifact(self, path: str) -> None:
            await self._bot._send_workspace_attachment(self._channel, path)

        async def on_error(self, content: str) -> None:
            try:
                await self._send(f"Sorry, I encountered an error: {content}")
            except Exception:
                logger.warning("Failed to send error notification to Discord", exc_info=True)

        async def on_iteration_limit(self, content: str) -> None:
            try:
                await self._channel.send(f"⚠️ {content}")
            except Exception:
                logger.warning("Failed to send iteration-limit warning to Discord", exc_info=True)

        async def on_done(self, tool_call_count: int) -> None:
            if self._reply_suppressed:
                await self.flush_text(final=True)
                return
            if tool_call_count and self._text_buffer:
                self._text_buffer += f"\n\n-# Tool calls: {tool_call_count}"
            elif tool_call_count and self._current_msg:
                try:
                    old_content = self._current_msg.content or ""
                    await self._current_msg.edit(
                        content=old_content + f"\n\n-# Tool calls: {tool_call_count}"
                    )
                except Exception:
                    logger.debug("Failed to edit Discord message with tool-call footer")
            await self.flush_text(final=True)

        async def on_stream_end(self, tool_call_count: int) -> None:
            if self._reply_suppressed:
                return
            if self._text_buffer:
                if tool_call_count:
                    self._text_buffer += f"\n\n-# Tool calls: {tool_call_count}"
                await self.flush_text(final=True)

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
        platform_origin: Optional[Dict[str, Any]] = None,
        is_self_invoke: bool = False,
        trigger_override: Optional[str] = None,
        source: Optional[str] = None,
        source_label: Optional[str] = None,
        publish_autonomous_events: Optional[bool] = None,
    ) -> None:
        """Stream SSE chat events to a Discord channel as multiple messages.

        ``platform_origin`` stamps the turn's originating Discord message for
        the ``react`` tool; the self-invoke kwargs are set by the reaction
        trigger (`on_raw_reaction_add`), which dispatches its synthetic prompt
        through this same path with ``publish_autonomous_events=False`` so the
        interactive stream rendered here is the turn's only delivery.
        """
        handler = self._InteractiveChatHandler(self, channel, first_send)
        try:
            await consume_sse_stream(
                self.api.chat_stream(
                    message,
                    thread_id,
                    user_id,
                    is_self_invoke=is_self_invoke,
                    trigger_override=trigger_override,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                    source=source,
                    source_label=source_label,
                    publish_autonomous_events=publish_autonomous_events,
                    platform_origin=platform_origin,
                ),
                handler,
            )
        except Exception as e:
            logger.error(f"Streaming failed, falling back to sync: {e}", exc_info=True)
            try:
                data = await self.api.chat(
                    message,
                    thread_id,
                    user_id,
                    attachments=attachments,
                    force_unsupported_attachments=bool(attachments),
                    trigger_override=trigger_override,
                    platform_origin=platform_origin,
                )
                if data.get("suppress_reply"):
                    # The turn's react call hid the reply; the reaction was
                    # delivered via the reaction_request event.
                    return
                response = data.get("response", "")
                tc = data.get("tool_call_count", 0)
                if tc:
                    response += f"\n\n-# Tool calls: {tc}"
                chunks = split_message(response)
                for chunk in chunks:
                    if not handler._first_sent:
                        handler._first_sent = True
                        await first_send(chunk)
                    else:
                        await channel.send(chunk)
                await self._send_latest_history_artifacts(channel, thread_id)
            except Exception as e2:
                logger.error(f"Sync fallback also failed: {e2}", exc_info=True)
                try:
                    if not handler._first_sent:
                        handler._first_sent = True
                        await first_send(f"Sorry, I encountered an error: {e2}")
                    else:
                        await channel.send(f"Sorry, I encountered an error: {e2}")
                except Exception:
                    logger.warning("Failed to send last-resort error notification to Discord", exc_info=True)

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


    async def on_ready(self) -> None:
        """Called when the bot is connected and ready."""
        assert self.user is not None, "Bot user must be set after login"
        logger.info(f"Discord bot ready as {self.user} (ID: {self.user.id})")
        print(f"\nDiscord bot ready as {self.user}")
        print(f"  Bot ID: {self.user.id}")
        print(f"  Guilds: {len(self.guilds)}")
        print(f"  Respond mode: {self.respond_mode}")
        print(f"  API: {self.api.base_url}")
        for guild in self.guilds:
            print(f"  - {guild.name} (ID: {guild.id})")

        self._start_health_heartbeat()

        # Sync per-guild only (instant updates, no duplicates).
        assert self.application_id is not None, "application_id must be set after login"
        await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        logger.info("Global commands cleared from Discord")
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Slash commands synced to guild: {guild.name}")

        # Start SSE listener for autonomous task results
        print(f"  Event source: API SSE ({self.api.base_url}/autonomous/stream)")
        self.loop.create_task(self._api_sse_listener())

    def _start_health_heartbeat(self) -> None:
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = self.loop.create_task(self._health_heartbeat_loop())

    async def _health_heartbeat_loop(self) -> None:
        """Publish health only while Discord and the Nymeria API are usable."""
        while not self.is_closed():
            try:
                api_ok = await self.api.health()
                client_connected = self.is_ready() and not self.is_closed()
                latency = getattr(self, "latency", None)
                write_service_heartbeat(
                    "discord-bot",
                    status="ok" if api_ok and client_connected else "unhealthy",
                    details={
                        "api_ok": api_ok,
                        "client_connected": client_connected,
                        "guild_count": len(self.guilds),
                        "latency_ms": round(latency * 1000, 2)
                        if isinstance(latency, (int, float))
                        else None,
                    },
                )
            except Exception:
                logger.warning("Discord health heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

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
                logger.warning("Failed to send attachment error to Discord", exc_info=True)

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
            platform_origin={
                "platform": "discord",
                "channel_id": str(message.channel.id),
                "message_id": str(message.id),
                "kind": "message",
            },
        )

    # =========================================================================
    # Emoji-reaction trigger (inbound half of backlog #45)
    # =========================================================================

    @staticmethod
    def _reaction_emoji_text(emoji: Any) -> str:
        """Render a reaction emoji for the synthetic prompt and the wire.

        Unicode emojis pass through; custom guild emojis render as ``:name:``
        (their id is meaningless to the model).
        """
        try:
            if getattr(emoji, "id", None):
                name = getattr(emoji, "name", None) or "custom"
                return f":{name}:"
            return str(emoji)
        except Exception:  # noqa: BLE001 - never fail the handler on rendering
            return "an emoji"

    @staticmethod
    def _reaction_excerpt(text: str, limit: int = 200) -> str:
        """Whitespace-collapsed excerpt of the reacted-to message."""
        collapsed = " ".join(str(text or "").split())
        if len(collapsed) > limit:
            collapsed = collapsed[: limit - 3].rstrip() + "..."
        return collapsed

    async def on_raw_reaction_add(self, payload: Any) -> None:
        """Fire an agent turn when a user reacts to one of the bot's messages.

        Mirrors the Hermes Feishu pattern: drop bot-origin reactions (the loop
        guard), gate to reactions on the BOT'S OWN messages, then route a
        synthetic prompt through the same guarded chat path a real message
        uses. Any emoji fires; gated by DISCORD_REACTION_TRIGGER_ENABLED
        (default off).
        """
        from ..config import get_settings
        from ..core.bot_reactions import debounce_reaction_fire

        try:
            if not get_settings().discord_reaction_trigger_enabled:
                return
        except Exception:  # noqa: BLE001 - settings failure means stay off
            return

        # Loop guard: ignore our own reactions and other guild bots. (DM
        # payloads carry no member; foreign bots there are unlinked and drop
        # at the resolve step below.)
        if self.user is None or payload.user_id == self.user.id:
            return
        member = getattr(payload, "member", None)
        if member is not None and getattr(member, "bot", False):
            return

        # Cheap own-message pre-gate: the raw payload carries the reacted
        # message's author id on REACTION_ADD, so with the toggle on, a
        # reaction to someone else's message costs no message fetch. The
        # authoritative gate below (on the fetched message) stays as the
        # backstop for payloads without the field.
        author_id = getattr(payload, "message_author_id", None)
        if author_id is not None and author_id != self.user.id:
            return

        emoji_text = self._reaction_emoji_text(payload.emoji)

        # Short-TTL dedupe: emoji toggling or gateway replays must not fire
        # repeated full agent turns for the same (message, reactor, emoji).
        if debounce_reaction_fire(
            platform="discord",
            channel_id=str(payload.channel_id),
            message_id=str(payload.message_id),
            reactor_id=str(payload.user_id),
            emoji=emoji_text,
        ):
            return

        user_id = await self.resolve_user_id(payload.user_id)
        if user_id is None:
            # A reaction is a one-tap gesture; replying with link-your-account
            # onboarding would let anyone spam the channel by tapping emojis.
            logger.debug(
                "Reaction trigger: unlinked Discord user %s ignored", payload.user_id
            )
            return

        try:
            channel = self.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.fetch_channel(payload.channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                return
            reacted_message = await channel.fetch_message(payload.message_id)
        except Exception as e:  # noqa: BLE001 - deleted message, missing perms
            logger.debug("Reaction trigger: could not fetch message: %s", e)
            return

        # Scope: only reactions on the bot's own messages fire (Hermes gate;
        # authoritative form of the payload pre-gate above).
        if reacted_message.author.id != self.user.id:
            return
        reactor = member.display_name if member is not None else None
        if not reactor:
            user_obj = self.get_user(payload.user_id)
            reactor = getattr(user_obj, "display_name", None) or "The user"
        excerpt = self._reaction_excerpt(reacted_message.content)
        if excerpt:
            prompt = (
                f"[Reaction] {reactor} reacted with {emoji_text} to your "
                f'message: "{excerpt}"'
            )
        else:
            prompt = (
                f"[Reaction] {reactor} reacted with {emoji_text} to one of "
                "your messages."
            )

        thread_id = make_thread_id(payload.guild_id, payload.channel_id)
        logger.info(
            "Reaction trigger: %s on message %s -> thread %s",
            emoji_text,
            payload.message_id,
            thread_id,
        )
        await self._stream_to_channel(
            channel=channel,
            first_send=lambda content: channel.send(content),
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            is_self_invoke=True,
            trigger_override="reaction",
            source="trigger",
            source_label=f"reaction {emoji_text}",
            publish_autonomous_events=False,
            platform_origin={
                "platform": "discord",
                "channel_id": str(payload.channel_id),
                "message_id": str(payload.message_id),
                "kind": "reaction",
            },
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

        return attachment_helpers.finalize_attachments(attachments, errors)

    async def _api_sse_listener(self) -> None:
        """
        Background task that connects to the API's /autonomous/stream SSE
        endpoint to receive task completion events.
        """
        # Subscribe to the firehose — every user's autonomous events reach
        # this listener and we route them to Discord channels by decoding
        # the event's thread_id prefix (`discord_<guild>_<channel>`). Works
        # because the service token is admin-role; non-admin tokens can't
        # request the wildcard and get HTTP 403. The reconnect/parse loop is
        # shared with the other bots via consume_autonomous_firehose; passing
        # self.is_closed preserves the close-aware exit and trailing-sleep guard.
        await consume_autonomous_firehose(
            base_url=self.api.base_url,
            api_key=self.api.api_key,
            on_event=self._handle_sse_event,
            log_label="API",
            logger=logger,
            should_stop=self.is_closed,
        )

    # =========================================================================
    # SSE handler for autonomous stream (implements SSEEventHandler protocol)
    # =========================================================================

    class _AutonomousSSEHandler:
        """SSE event handler for Discord autonomous task delivery.

        Implements :class:`~triggers.sse_consumer.SSEEventHandler` and owns
        per-thread buffer state, progressive editing, and artifact dedup.
        """

        EDIT_INTERVAL = 1.5

        def __init__(
            self,
            bot: "NymeriaDiscordBot",
            channel: Any,
            channel_id: int,
        ) -> None:
            self._bot = bot
            self._channel = channel
            self._channel_id = channel_id

            self._text_buffer = ""
            self._current_msg: Optional[discord.Message] = None
            self._last_edit = 0.0
            self._tool_msgs: Dict[str, discord.Message] = {}
            self._tool_count = 0
            self._response_seen = False
            self._sent_artifacts: set = set()
            self._reply_suppressed = False

        async def _send_text(self, content: str) -> Optional[discord.Message]:
            last_msg = None
            for chunk in split_message(content):
                last_msg = await self._channel.send(chunk)
            return last_msg

        async def flush_text(self, final: bool = False) -> None:
            if self._reply_suppressed:
                self._text_buffer = ""
                if final:
                    self._current_msg = None
                return
            if not self._text_buffer:
                if final:
                    self._current_msg = None
                return
            try:
                if len(self._text_buffer) > 2000:
                    self._current_msg = await self._send_text(self._text_buffer)
                elif self._current_msg is None:
                    self._current_msg = await self._channel.send(self._text_buffer)
                else:
                    await self._current_msg.edit(content=self._text_buffer)
                self._last_edit = time.monotonic()
            except discord.HTTPException:
                try:
                    self._current_msg = await self._send_text(self._text_buffer)
                    self._last_edit = time.monotonic()
                except Exception:
                    logger.warning("Failed to send fallback autonomous Discord message", exc_info=True)
            if final:
                self._text_buffer = ""
                self._current_msg = None

        async def _send_compaction_embed(
            self,
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
            await self._channel.send(embed=embed)

        async def _send_workspace_attachment_once(self, path: str) -> None:
            if not path or path in self._sent_artifacts:
                return
            if await self._bot._send_workspace_attachment(self._channel, path):
                self._sent_artifacts.add(path)

        # -- SSEEventHandler callbacks ----------------------------------------

        async def on_thinking(self) -> None:
            try:
                await self._channel.trigger_typing()
            except Exception:
                logger.debug("Failed to send autonomous typing indicator")

        async def on_response_chunk(self, content: str) -> None:
            self._response_seen = True
            if self._reply_suppressed:
                return
            self._text_buffer += content
            if len(self._text_buffer) > 1800:
                await self.flush_text(final=True)
            elif time.monotonic() - self._last_edit >= self.EDIT_INTERVAL:
                await self.flush_text()

        async def on_reply_suppressed(self) -> None:
            self._reply_suppressed = True
            self._text_buffer = ""

        async def on_compacting(self, message: str) -> None:
            await self._send_text(message)

        async def on_compacted(
            self,
            summary: str,
            messages_removed: int,
            title: str,
        ) -> None:
            await self._send_compaction_embed(summary, messages_removed, title)

        async def on_tool_call(
            self,
            name: str,
            args: Dict[str, Any],
            call_id: str,
            count: int,
        ) -> None:
            self._tool_count = count
            show_tools = self._bot._show_tool_calls.get(self._channel_id, False)
            if show_tools:
                await self.flush_text(final=True)
                args_str = _json.dumps(args, indent=2, ensure_ascii=False) if args else ""
                if len(args_str) > 1000:
                    args_str = args_str[:997] + "..."
                embed = discord.Embed(
                    title=f"🔧 {name}",
                    description=f"```json\n{args_str}\n```" if args_str else None,
                    color=discord.Color.blue(),
                )
                tool_msg = await self._channel.send(embed=embed)
                self._tool_msgs[call_id] = tool_msg
            try:
                await self._channel.trigger_typing()
            except Exception:
                logger.debug("Failed to send autonomous typing indicator")

        async def on_tool_result(
            self,
            call_id: str,
            result: str,
            attachments: List[str],
        ) -> None:
            show_tools = self._bot._show_tool_calls.get(self._channel_id, False)
            if not show_tools:
                if self._text_buffer and "──────" not in self._text_buffer[-20:]:
                    self._text_buffer += "\n\n──────────────────────────────\n\n"
            else:
                tool_msg = self._tool_msgs.get(call_id)
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
            for attach_path in attachments:
                await self._send_workspace_attachment_once(attach_path)

        async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
            names = ", ".join(str(tool) for tool in tools) if tools else "tools"
            embed = discord.Embed(
                description=f"**{names}**{f' ({ttl})' if ttl else ''}",
                color=discord.Color.dark_grey(),
            )
            embed.set_author(name="Tool Binding")
            await self._channel.send(embed=embed)

        async def on_workspace_artifact(self, path: str) -> None:
            await self._send_workspace_attachment_once(path)

        async def on_error(self, content: str) -> None:
            await self._send_text(f"Sorry, I encountered an error: {content}")

        async def on_iteration_limit(self, content: str) -> None:
            await self._channel.send(f"⚠️ {content}")

        async def on_done(self, tool_call_count: int) -> None:
            pass  # autonomous uses task_completed, not done

        async def on_stream_end(self, tool_call_count: int) -> None:
            pass  # autonomous stream is event-by-event, not consumed as stream

    def _make_hook_approval_view(self, record_id: str) -> Any:
        """Build an Approve/Deny button view for a hook-approval hold.

        Defined lazily (inside the method) so the module still imports on a
        lean install without discord.py. The backend is the authorization
        authority: the click resolves via the REST endpoint under the
        clicker's identity, so owner-or-admin is enforced server-side (404 =
        not yours, 409 = no longer pending). The public message edit happens
        via the ``hook_approval_resolved`` firehose event, giving every
        resolution surface one shared edit path; the click itself only gets
        an ephemeral acknowledgement.
        """
        bot = self

        class _HookApprovalView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=HOOK_APPROVAL_VIEW_TIMEOUT_SECONDS)
                self.resolved = False
                self._message: Any = None

            async def on_timeout(self) -> None:
                # Fallback only: the resolved event normally retracts the
                # buttons long before the view times out.
                message = self._message
                if message is None:
                    return
                try:
                    await message.edit(view=None)
                except Exception:  # noqa: BLE001
                    pass

            async def _resolve(
                self, interaction: "discord.Interaction", approved: bool
            ) -> None:
                if self.resolved:
                    await interaction.response.send_message(
                        "Already resolved.", ephemeral=True
                    )
                    return
                user_id = await bot.resolve_user_id(interaction.user.id)
                if user_id is None:
                    await interaction.response.send_message(
                        "This Discord account isn't linked to a Nymeria user "
                        "yet, so it can't resolve approvals.",
                        ephemeral=True,
                    )
                    return
                try:
                    await bot.api.resolve_hook_approval(
                        record_id, approved, user_id=user_id
                    )
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    if status == 404:
                        await interaction.response.send_message(
                            "Only the requester or an admin can resolve this.",
                            ephemeral=True,
                        )
                    elif status == 409:
                        self.resolved = True
                        await interaction.response.send_message(
                            "No longer pending.", ephemeral=True
                        )
                        try:
                            if interaction.message is not None:
                                await interaction.message.edit(view=None)
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        logger.warning("Hook approval resolve failed: %s", e)
                        await interaction.response.send_message(
                            "Couldn't resolve the approval.", ephemeral=True
                        )
                    return
                except Exception as e:  # noqa: BLE001
                    logger.warning("Hook approval resolve failed: %s", e)
                    await interaction.response.send_message(
                        "Couldn't resolve the approval.", ephemeral=True
                    )
                    return
                self.resolved = True
                await interaction.response.send_message(
                    "Approved." if approved else "Denied.", ephemeral=True
                )

            @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
            async def approve(
                self, interaction: "discord.Interaction", button: "discord.ui.Button"
            ) -> None:
                await self._resolve(interaction, True)

            @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
            async def deny(
                self, interaction: "discord.Interaction", button: "discord.ui.Button"
            ) -> None:
                await self._resolve(interaction, False)

        return _HookApprovalView()

    def _prune_hook_approval_messages(self) -> None:
        now = time.monotonic()
        expired = [
            record_id
            for record_id, (_, _, deadline) in self._hook_approval_messages.items()
            if deadline <= now
        ]
        for record_id in expired:
            self._hook_approval_messages.pop(record_id, None)

    async def _on_hook_approval_event(self, channel: Any, event: Dict[str, Any]) -> None:
        """Post an approval prompt with Approve/Deny buttons.

        The body is the shared text fallback (it carries the ``/hook approve
        <id>`` commands, so the hold stays resolvable even if buttons fail).
        """
        record_id = str(event.get("record_id") or "")
        if not record_id:
            return
        self._prune_hook_approval_messages()
        view = self._make_hook_approval_view(record_id)
        text = format_hook_approval_message(event)
        try:
            message = await channel.send(text[:2000], view=view)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to send hook approval prompt: %s", e)
            return
        view._message = message
        self._hook_approval_messages[record_id] = (
            message,
            view,
            time.monotonic() + HOOK_APPROVAL_VIEW_TIMEOUT_SECONDS,
        )

    async def _on_hook_approval_resolved_event(self, event: Dict[str, Any]) -> None:
        """Edit the original prompt on resolution: outcome line, no buttons.

        Fires for every resolution shape (button, /hook command, REST,
        desktop, timeout, abort), so the buttons are always retracted no
        matter where the decision came from.
        """
        record_id = str(event.get("record_id") or "")
        entry = self._hook_approval_messages.pop(record_id, None)
        if entry is None:
            return
        message, view, _ = entry
        outcome = str(event.get("outcome") or "")
        resolved_by = str(event.get("resolved_by") or "").strip()
        note = str(event.get("note") or "").strip()
        if outcome == "approved":
            line = "✅ Approved" + (f" by {resolved_by}" if resolved_by else "")
        elif outcome == "denied":
            line = "\U0001f6ab Denied" + (f" by {resolved_by}" if resolved_by else "")
        elif outcome == "timeout":
            line = "⏰ No answer in time; the tool call was denied."
        elif outcome == "aborted":
            line = "Turn cancelled; the tool call was denied."
        else:
            line = "No longer pending."
        if note:
            line += f": {note}"
        tool_name = str(event.get("tool_name") or "tool call")
        try:
            view.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await message.edit(
                content=f"Approval request for {tool_name}.\n\n{line}"[:2000],
                view=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to edit hook approval message: %s", e)

    async def _on_reaction_request_event(self, event: Dict[str, Any]) -> None:
        """Execute an outbound ``react`` tool send: add the emoji reaction.

        The API process has no Discord connection, so the react tool
        publishes a ``reaction_request`` event and this bot performs the
        platform call. Best-effort: failures (deleted message, unknown
        emoji, missing permission) log and drop, matching notifications.
        """
        emoji = str(event.get("emoji") or "").strip()
        try:
            channel_id = int(event.get("channel_id") or 0)
            message_id = int(event.get("message_id") or 0)
        except (TypeError, ValueError):
            return
        if not emoji or not channel_id or not message_id:
            return
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                return
            message = await channel.fetch_message(message_id)
            await message.add_reaction(emoji)
            logger.info(
                "Posted reaction %s to Discord message %s", emoji, message_id
            )
        except Exception as e:  # noqa: BLE001 - reaction delivery is best-effort
            logger.warning(
                "Failed to post Discord reaction %s to message %s: %s",
                emoji,
                message_id,
                e,
            )

    async def _handle_sse_event(self, event: Dict[str, Any]) -> None:
        """Process a single event from the API SSE autonomous stream.

        Routes Discord-prefixed thread events through the shared
        :func:`dispatch_event` for standard SSE types and handles
        autonomous-specific types (notification, task_started,
        task_completed) directly.
        """
        event_type = event.get("type", "")
        thread_id = event.get("thread_id", "")

        # Outbound reaction sends are platform-keyed (the event carries its
        # own channel/message ids), so route them before the thread gate.
        if event_type == "reaction_request":
            if event.get("platform") == "discord":
                await self._on_reaction_request_event(event)
            return

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

            # Approval holds are time-critical and independent of the
            # autonomous handler state, so route them before it.
            if event_type == "hook_approval":
                await self._on_hook_approval_event(channel, event)
                return
            if event_type == "hook_approval_resolved":
                await self._on_hook_approval_resolved_event(event)
                return

            # Get or create handler for this thread
            state = self._autonomous_state.get(thread_id)
            if state is None:
                handler = self._AutonomousSSEHandler(self, channel, channel_id)
                state = {"handler": handler, "prompt": event.get("prompt", "")}
                self._autonomous_state[thread_id] = state
            else:
                handler = state["handler"]
                handler._channel = channel
                if event.get("prompt") and not state.get("prompt"):
                    state["prompt"] = event.get("prompt", "")

            # Autonomous-only event types not in the shared SSE protocol
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
                    await handler._send_text(str(message))
                return

            if event_type == "task_started":
                return

            if event_type == "task_completed":
                try:
                    if event.get("error"):
                        await handler.flush_text(final=True)
                        err = (
                            event.get("content")
                            or event.get("error_message")
                            or "Unknown error"
                        )
                        await handler._send_text(f"Autonomous task error: {err}")
                    elif handler._reply_suppressed:
                        # The turn's react call hid the reply; drop the
                        # aggregate content and footer.
                        await handler.flush_text(final=True)
                    else:
                        if not handler._response_seen and not handler._text_buffer:
                            fallback = event.get("content") or ""
                            if fallback:
                                handler._text_buffer = fallback
                        tc = handler._tool_count
                        if tc and handler._text_buffer:
                            handler._text_buffer += f"\n\n-# Tool calls: {tc}"
                        elif tc and handler._current_msg:
                            try:
                                old_content = handler._current_msg.content or ""
                                await handler._current_msg.edit(
                                    content=old_content
                                    + f"\n\n-# Tool calls: {tc}"
                                )
                            except Exception:
                                logger.debug("Failed to edit autonomous message with tool-call footer")
                        await handler.flush_text(final=True)
                    logger.info(f"Streamed autonomous result to Discord channel {channel_id}")
                finally:
                    self._autonomous_state.pop(thread_id, None)
                return

            # Standard SSE event types — delegate to shared dispatcher
            handler._tool_count = await dispatch_event(
                event, handler, handler._tool_count
            )

        except Exception as e:
            logger.error(f"Error posting SSE event to Discord: {e}", exc_info=True)
            self._autonomous_state.pop(thread_id, None)
