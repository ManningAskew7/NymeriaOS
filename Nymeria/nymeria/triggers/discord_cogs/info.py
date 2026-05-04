"""Information and status commands: /thread, /status, /context, /export,
/channel-context, /show-tools, /help."""

from __future__ import annotations

import asyncio
import io
import json as _json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..bot_helpers import context_bar, fmt_tokens
from ..discord_bot import make_thread_id

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


class InfoCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    # --- /thread ---

    @app_commands.command(
        name="thread", description="Show current thread info"
    )
    async def cmd_thread(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        thread_id = make_thread_id(
            interaction.guild_id, interaction.channel_id
        )
        try:
            stats = await self.bot.api.get_context_stats(thread_id)
            embed = discord.Embed(
                title="Thread Info", color=discord.Color.green()
            )
            embed.add_field(
                name="Thread ID", value=f"`{thread_id}`", inline=False
            )
            embed.add_field(
                name="Context Usage",
                value=f"{stats.get('usage_percentage', 0)}% ({stats.get('total_tokens', 0):,} / {stats.get('context_limit', 0):,} tokens)",
                inline=True,
            )
            embed.add_field(
                name="Compactions",
                value=str(stats.get("compaction_count", 0)),
                inline=True,
            )
            embed.add_field(
                name="Context Mode",
                value=stats.get("context_management", "unknown"),
                inline=True,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error getting thread info: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # --- /status ---

    @app_commands.command(
        name="status", description="Show Nymeria system status"
    )
    async def cmd_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            thread_id = make_thread_id(
                interaction.guild_id, interaction.channel_id
            )
            user_id = await self.bot._resolve_or_reject_interaction(
                interaction
            )
            if user_id is None:
                return
            settings, context, tools_data, todos = await asyncio.gather(
                self.bot.api.get_settings(),
                self.bot.api.get_context_stats(thread_id),
                self.bot.api.get_default_tools(),
                self.bot.api.list_todos(user_id),
                return_exceptions=True,
            )

            if isinstance(settings, Exception):
                settings = {}
            if isinstance(context, Exception):
                context = {}
            if isinstance(tools_data, Exception):
                tools_data = {}
            if isinstance(todos, Exception):
                todos = []

            uptime_seconds = int(time.time() - self.bot._start_time)
            hours, remainder = divmod(uptime_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours > 0:
                uptime_str = f"{hours}h {minutes}m"
            elif minutes > 0:
                uptime_str = f"{minutes}m {seconds}s"
            else:
                uptime_str = f"{seconds}s"

            model = settings.get("llm_model", "?")
            provider = settings.get("llm_provider", "?")
            base_url = settings.get("llm_base_url")
            if base_url and "cli-proxy" in base_url:
                provider_detail = f"{provider} (via CLIProxy)"
            elif base_url:
                provider_detail = f"{provider} ({base_url})"
            else:
                provider_detail = provider

            total_tokens = context.get("total_tokens", 0)
            context_limit = context.get("context_limit", 0)
            usage_pct = context.get("usage_percentage", 0)
            compactions = context.get("compaction_count", 0)
            context_mode = context.get(
                "context_management",
                settings.get("context_management", "?"),
            )

            default_count = len(tools_data.get("default_tools", []))
            available_count = len(tools_data.get("available_tools", []))
            callable_count = tools_data.get("callable_thread_count", 0)

            thinking = settings.get("llm_extended_thinking", False)
            reasoning = settings.get("llm_reasoning_effort")

            processing = context.get("processing", False)

            embed = discord.Embed(
                title="Nymeria Status", color=discord.Color.gold()
            )

            runtime_parts = [f"`{model}`"]
            runtime_parts.append(provider_detail)
            if thinking:
                t_label = (
                    f"thinking: {reasoning}" if reasoning else "thinking: on"
                )
                runtime_parts.append(t_label)
            else:
                runtime_parts.append("thinking: off")
            embed.add_field(
                name="Model",
                value=" | ".join(runtime_parts),
                inline=False,
            )

            ctx_lines = [
                context_bar(usage_pct, code=True),
                f"{fmt_tokens(total_tokens)} / {fmt_tokens(context_limit)} tokens",
            ]
            if compactions:
                ctx_lines.append(
                    f"{compactions} compaction{'s' if compactions != 1 else ''}"
                )
            ctx_lines.append(f"mode: {context_mode}")
            compact_threshold = settings.get("compact_threshold")
            if compact_threshold and context_mode == "auto_compact":
                ctx_lines[-1] += (
                    f" (threshold {int(compact_threshold * 100)}%)"
                )
            embed.add_field(
                name="Context", value="\n".join(ctx_lines), inline=True
            )

            sys_lines = [
                f"{default_count} core / {available_count} available",
            ]
            if callable_count:
                sys_lines[0] += f" / {callable_count} callable"
            sys_lines.append(f"uptime: {uptime_str}")
            if settings.get("watchdog_enabled"):
                sys_lines.append(
                    f"watchdog: {settings.get('watchdog_interval_minutes', '?')}m interval"
                )
            if processing:
                sys_lines.append("**processing...**")
            embed.add_field(
                name="Tools & System",
                value="\n".join(sys_lines),
                inline=True,
            )

            if todos:
                t_pending = sum(
                    1 for t in todos if t.get("status") == "pending"
                )
                t_in_prog = sum(
                    1 for t in todos if t.get("status") == "in_progress"
                )
                t_done = sum(
                    1 for t in todos if t.get("status") == "done"
                )
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

            ctx_enabled = self.bot._context_enabled.get(
                interaction.channel_id, True
            )
            discord_lines = [
                f"respond: {self.bot.respond_mode}",
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

    # --- /context ---

    @app_commands.command(
        name="context",
        description="Detailed context breakdown for this channel",
    )
    async def cmd_context(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        thread_id = make_thread_id(
            interaction.guild_id, interaction.channel_id
        )
        try:
            (
                context,
                thread_cfg,
                settings,
                categories,
                tools_data,
            ) = await asyncio.gather(
                self.bot.api.get_context_stats(thread_id),
                self.bot.api.get_thread_config(thread_id),
                self.bot.api.get_settings(),
                self.bot.api.get_tool_categories(),
                self.bot.api.get_default_tools(),
                return_exceptions=True,
            )

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
                title="Context Breakdown", color=discord.Color.teal()
            )

            # Model
            effective_model = context.get("model") or settings.get(
                "llm_model", "?"
            )
            provider = settings.get("llm_provider", "?")
            base_url = settings.get("llm_base_url")
            if base_url and "cli-proxy" in base_url:
                provider_str = f"{provider} (via CLIProxy)"
            elif base_url:
                provider_str = f"{provider} ({base_url})"
            else:
                provider_str = provider

            model_lines = [f"`{effective_model}` | {provider_str}"]

            if thread_cfg:
                llm_cfg = thread_cfg.get("llm_config") or {}
                thread_model = llm_cfg.get("model")
                if thread_model and thread_model != settings.get("llm_model"):
                    model_lines.append(
                        f"⚠️ thread override: model=`{thread_model}`"
                    )
                thread_temp = llm_cfg.get("temperature")
                if thread_temp is not None:
                    model_lines.append(f"temperature: {thread_temp}")

            embed.add_field(
                name="Model",
                value="\n".join(model_lines),
                inline=False,
            )

            # Context Window
            total_tokens = context.get("total_tokens", 0)
            context_limit = context.get("context_limit", 0)
            usage_pct = context.get("usage_percentage", 0)
            cumulative = context.get("cumulative_tokens", 0)
            compactions = context.get("compaction_count", 0)
            last_compact = context.get("last_compaction")
            ctx_mode = context.get(
                "context_management",
                settings.get("context_management", "?"),
            )

            ctx_lines = [
                context_bar(usage_pct, code=True),
                f"{fmt_tokens(total_tokens)} / {fmt_tokens(context_limit)} tokens",
            ]
            if cumulative:
                ctx_lines[-1] += f" (cumulative: {fmt_tokens(cumulative)})"
            compact_parts = []
            if compactions:
                compact_parts.append(
                    f"{compactions} compaction{'s' if compactions != 1 else ''}"
                )
            if last_compact:
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

            # Tools
            default_tools = set(tools_data.get("default_tools", []))
            available_tools = tools_data.get("available_tools", [])
            cats = (
                categories.get("categories", {})
                if isinstance(categories, dict)
                else {}
            )

            disabled = set()
            extra_enabled = set()
            if thread_cfg:
                disabled = set(thread_cfg.get("disabled_tools") or [])
                extra_enabled = set(thread_cfg.get("enabled_tools") or [])

            effective_enabled = (default_tools - disabled) | extra_enabled
            total_available = len(available_tools)

            tool_lines = [
                f"{len(effective_enabled)} enabled (of {total_available} available)"
            ]

            cat_parts = []
            for cat_name in sorted(cats.keys()):
                cat_tools = set(cats[cat_name])
                enabled_in_cat = len(cat_tools & effective_enabled)
                total_in_cat = len(cat_tools)
                if enabled_in_cat == total_in_cat:
                    cat_parts.append(f"{cat_name}: {total_in_cat}")
                else:
                    cat_parts.append(
                        f"{cat_name}: {enabled_in_cat}/{total_in_cat}"
                    )
            while cat_parts:
                chunk = cat_parts[:3]
                cat_parts = cat_parts[3:]
                tool_lines.append(" | ".join(chunk))
                if len(tool_lines) >= 9:
                    remaining = len(cat_parts)
                    if remaining:
                        tool_lines.append(
                            f"...and {remaining} more categories"
                        )
                    break

            embed.add_field(
                name="Tools",
                value="\n".join(tool_lines),
                inline=False,
            )

            # Thread Overrides
            override_lines = []
            if thread_cfg:
                instructions = thread_cfg.get("instructions")
                if instructions:
                    override_lines.append(
                        f"instructions: {len(instructions)} chars"
                    )
                if disabled:
                    override_lines.append(
                        f"disabled: {', '.join(sorted(disabled)[:8])}"
                    )
                    if len(disabled) > 8:
                        override_lines[-1] += (
                            f" (+{len(disabled) - 8} more)"
                        )
                if extra_enabled:
                    override_lines.append(
                        f"enabled: {', '.join(sorted(extra_enabled)[:8])}"
                    )
                    if len(extra_enabled) > 8:
                        override_lines[-1] += (
                            f" (+{len(extra_enabled) - 8} more)"
                        )
                if thread_cfg.get("inject_todos_in_prompt"):
                    override_lines.append("inject TODOs: yes")
                sys_prompt = thread_cfg.get("system_prompt")
                if sys_prompt:
                    override_lines.append(
                        f"custom system prompt: {len(sys_prompt)} chars"
                    )
                if thread_cfg.get("callable"):
                    cname = thread_cfg.get("callable_name", "?")
                    override_lines.append(f"callable as: {cname}")

            if not override_lines:
                override_lines.append("None — using global defaults")

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
            name="/stop",
            value="Abort the current running operation",
            inline=False,
        )
        embed.add_field(
            name="/restart [bot|api]",
            value="Restart the Discord bot or API server",
            inline=False,
        )
        embed.add_field(
            name="/clear",
            value="Clear conversation history (preserves notepad + tools)",
            inline=False,
        )
        embed.add_field(
            name="/compact",
            value="Compress conversation to save context",
            inline=False,
        )
        embed.add_field(
            name="/model [name] [scope]",
            value="Show or change the LLM model (global or per-channel)",
            inline=False,
        )
        embed.add_field(
            name="/models",
            value="List available models from the provider",
            inline=False,
        )
        embed.add_field(
            name="/think [off|on|low|medium|high]",
            value="Show or set thinking/reasoning mode",
            inline=False,
        )
        embed.add_field(
            name="/config show | get | set",
            value="View and update Nymeria settings",
            inline=False,
        )
        embed.add_field(
            name="/env show | get | set",
            value="View and set environment variables (API keys, infrastructure)",
            inline=False,
        )
        embed.add_field(
            name="/tools core | enabled | optional | category | enable | disable",
            value="View and manage available tools per-channel",
            inline=False,
        )
        embed.add_field(
            name="/memory list | save | forget | search",
            value="Manage persistent memories about you",
            inline=False,
        )
        embed.add_field(
            name="/notepad read | write | clear",
            value="Per-channel persistent notes (survive compaction)",
            inline=False,
        )
        embed.add_field(
            name="/todos add | list | complete | delete",
            value="Scheduled tasks and reminders (with repeat intervals)",
            inline=False,
        )
        embed.add_field(
            name="/thread",
            value="Show thread info (tokens, compactions)",
            inline=False,
        )
        embed.add_field(
            name="/context",
            value="Detailed context breakdown (model, tools, overrides, tokens)",
            inline=False,
        )
        embed.add_field(
            name="/status",
            value="Comprehensive system status (model, context, tools, tasks)",
            inline=False,
        )
        embed.add_field(
            name="/tasks [status]",
            value="Quick view of scheduled and autonomous tasks",
            inline=False,
        )
        embed.add_field(
            name="/export [format]",
            value="Export conversation history (markdown, json, txt)",
            inline=False,
        )
        embed.add_field(
            name="/show-tools",
            value="Toggle whether tool calls are shown in chat (off by default)",
            inline=False,
        )
        embed.add_field(
            name="/channel-context",
            value="Toggle whether Nymeria reads recent channel messages",
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed, ephemeral=True
        )
