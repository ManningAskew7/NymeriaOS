"""TODO and task commands: /todos group, /tasks."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot


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
        await self.bot._send_backend_command(
            interaction,
            "todos list",
            args=filter.value if filter else "",
        )

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
        parts = [task, schedule]
        if repeat:
            parts.append(repeat.value)
        elif notes:
            parts.append("")
        if notes:
            parts.append(notes)
        await self.bot._send_backend_command(
            interaction,
            "todos add",
            args=" | ".join(parts),
        )

    @todos_group.command(name="complete", description="Mark a TODO as done")
    @app_commands.describe(todo_id="The TODO ID (first 8 chars)")
    async def cmd_todos_complete(
        self, interaction: discord.Interaction, todo_id: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "todos complete",
            args=todo_id,
        )

    @todos_group.command(name="delete", description="Delete a TODO permanently")
    @app_commands.describe(todo_id="The TODO ID (first 8 chars)")
    async def cmd_todos_delete(
        self, interaction: discord.Interaction, todo_id: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "todos delete",
            args=todo_id,
        )

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
        await self.bot._send_backend_command(
            interaction,
            "tasks",
            args=status.value if status else "",
        )
