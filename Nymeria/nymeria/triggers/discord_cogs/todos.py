"""TODO and task commands: /todos group, /tasks."""

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


class TodosCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    todos_group = app_commands.Group(
        name="todos", description="Manage scheduled tasks and reminders"
    )

    @todos_group.command(name="list", description="List TODOs")
    @app_commands.describe(filter="Filter by status (default: active)")
    @app_commands.choices(
        filter=[
            app_commands.Choice(name="active (pending + in progress)", value="active"),
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="in progress", value="in_progress"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ]
    )
    async def cmd_todos_list(
        self,
        interaction: discord.Interaction,
        filter: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            items = await self.bot.api.list_todos(user_id)
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

                parts = [f"ID: `{todo_id}`"]
                scheduled = item.get("scheduled_for")
                if scheduled:
                    parts.append(f"fires: {scheduled[:16]}")
                recurrence = item.get("recurrence")
                if recurrence:
                    parts.append(f"repeat: {recurrence}")
                thread = item.get("thread_id")
                if thread:
                    parts.append(f"thread: `{thread[:20]}`")

                status_marker = {
                    "pending": "⏳",
                    "in_progress": "▶",
                    "done": "✅",
                }.get(status, "?")
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

    @todos_group.command(
        name="add", description="Create a scheduled task or reminder"
    )
    @app_commands.describe(
        task="What should Nymeria do?",
        schedule="When to fire: '30m', '2h', '1d', or '2024-12-25 14:00'",
        repeat="Repeat interval (omit for one-shot)",
        notes="Additional context or instructions",
    )
    @app_commands.choices(
        repeat=[
            app_commands.Choice(name="every 5 minutes", value="5min"),
            app_commands.Choice(name="every 10 minutes", value="10min"),
            app_commands.Choice(name="every 15 minutes", value="15min"),
            app_commands.Choice(name="every 30 minutes", value="30min"),
            app_commands.Choice(name="every hour", value="hourly"),
            app_commands.Choice(name="every day", value="daily"),
            app_commands.Choice(name="every week", value="weekly"),
            app_commands.Choice(name="every month", value="monthly"),
        ]
    )
    async def cmd_todos_add(
        self,
        interaction: discord.Interaction,
        task: str,
        schedule: str = "1d",
        repeat: Optional[app_commands.Choice[str]] = None,
        notes: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
        try:
            result = await self.bot.api.add_todo(
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
            lines.append("Thread: this channel")

            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except httpx.HTTPStatusError as e:
            detail = (
                e.response.json().get("detail", str(e)) if e.response else str(e)
            )
            await interaction.followup.send(f"Error: {detail}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error adding todo: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @todos_group.command(name="complete", description="Mark a TODO as done")
    @app_commands.describe(todo_id="The TODO ID (first 8 chars)")
    async def cmd_todos_complete(
        self, interaction: discord.Interaction, todo_id: str
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            items = await self.bot.api.list_todos(user_id)
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

            result = await self.bot.api.complete_todo(user_id, match["id"])

            task = match.get("task", "")
            recurrence = result.get("recurrence")
            if recurrence and result.get("status") == "pending":
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
    async def cmd_todos_delete(
        self, interaction: discord.Interaction, todo_id: str
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            items = await self.bot.api.list_todos(user_id)
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

            await self.bot.api.delete_todo(user_id, match["id"])
            await interaction.followup.send(
                f"Deleted: **{match.get('task', '')}**", ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error deleting todo: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(
        name="tasks",
        description="Quick view of scheduled and autonomous tasks",
    )
    @app_commands.describe(status="Filter by status (default: active)")
    @app_commands.choices(
        status=[
            app_commands.Choice(
                name="active (pending + in progress)", value="active"
            ),
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="in progress", value="in_progress"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ]
    )
    async def cmd_tasks(
        self,
        interaction: discord.Interaction,
        status: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            items = await self.bot.api.list_todos(user_id)
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
            summary = (
                f"{len(items)} tasks ({', '.join(parts)})"
                if parts
                else f"{len(items)} tasks"
            )

            def sort_key(item):
                s = item.get("scheduled_for") or ""
                return (0 if s else 1, s)

            items.sort(key=sort_key)

            embed = discord.Embed(
                title="Scheduled Tasks",
                description=summary,
                color=discord.Color.orange(),
            )

            status_icons = {
                "pending": "⏳",
                "in_progress": "▶",
                "done": "✅",
            }
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
                embed.set_footer(
                    text=f"Showing 10 of {len(items)} — use /todos list for full view"
                )
            else:
                embed.set_footer(text="Use /todos for full task management")

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing tasks: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
