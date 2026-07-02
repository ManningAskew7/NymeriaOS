"""Lifecycle-hook authoring commands: the /hook group.

Each subcommand assembles the backend ``/hook`` flag grammar and forwards it via
``_send_backend_command``. Values are ``shlex.quote``-d because the backend
command dispatcher shlex-splits the line back into tokens, so a multi-word
``--text``/``--reason``/``matcher`` must survive as a single token. Event/action
legality (which action is valid for which event) is validated backend-side, so
the cog lists every choice and lets the backend return a friendly error.
"""

from __future__ import annotations

import shlex

import discord
from discord import app_commands
from discord.ext import commands

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot


_EVENT_CHOICES = [
    app_commands.Choice(name="prompt submitted", value="prompt_submit"),
    app_commands.Choice(name="before a tool runs", value="pre_tool_use"),
    app_commands.Choice(name="after a tool runs", value="post_tool_use"),
    app_commands.Choice(name="turn done", value="done"),
]

_ACTION_CHOICES = [
    app_commands.Choice(name="inject context", value="inject_context"),
    app_commands.Choice(name="block if matches", value="block_if_matches"),
    app_commands.Choice(name="rewrite arg", value="rewrite_arg"),
    app_commands.Choice(name="notify", value="notify"),
    app_commands.Choice(name="create todo", value="create_todo"),
    app_commands.Choice(name="webhook", value="webhook"),
]

_CREATE_SCOPE_CHOICES = [
    app_commands.Choice(name="this thread", value="thread"),
    app_commands.Choice(name="global (all threads)", value="global"),
]

_LIST_SCOPE_CHOICES = [
    app_commands.Choice(name="all", value="all"),
    app_commands.Choice(name="this thread + global", value="thread"),
    app_commands.Choice(name="global only", value="global"),
]


def _opt(flag: str, value: Optional[str]) -> list[str]:
    """A ``--flag value`` pair (quoted), or nothing when the value is empty."""
    if not value:
        return []
    return [flag, shlex.quote(value)]


def _kv(key: str, value: Optional[str]) -> list[str]:
    """A ``key=value`` edit token (value quoted), or nothing when unset."""
    if value is None or value == "":
        return []
    return [f"{key}={shlex.quote(value)}"]


class HooksCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    hook_group = app_commands.Group(
        name="hook", description="Author lifecycle hooks (when/logic/return)"
    )

    @hook_group.command(name="list", description="List lifecycle hooks")
    @app_commands.describe(
        scope="Which hooks to show", enabled_only="Only show enabled hooks"
    )
    @app_commands.choices(scope=_LIST_SCOPE_CHOICES)
    async def cmd_hook_list(
        self,
        interaction: discord.Interaction,
        scope: Optional[app_commands.Choice[str]] = None,
        enabled_only: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if scope and scope.value == "thread":
            parts += ["--thread", "current"]
        elif scope and scope.value == "global":
            parts.append("--global")
        if enabled_only:
            parts.append("--enabled-only")
        await self.bot._send_backend_command(
            interaction, "hook list", args=" ".join(parts)
        )

    @hook_group.command(name="create", description="Create a lifecycle hook")
    @app_commands.describe(
        name="Hook name",
        event="When it fires",
        action="What it does",
        text="Injected/notify/todo text (inject_context, notify, create_todo)",
        matcher="Tool filter for tool events, e.g. bash or Edit|Write",
        condition="One condition: 'field operator value' (e.g. command contains rm -rf)",
        reason="Denial message (block_if_matches)",
        set_arg="Argument rewrite 'arg=value' (rewrite_arg)",
        url="Webhook URL (webhook)",
        scope="Thread-scoped (default) or global",
        disabled="Create it disabled",
    )
    @app_commands.choices(
        event=_EVENT_CHOICES, action=_ACTION_CHOICES, scope=_CREATE_SCOPE_CHOICES
    )
    async def cmd_hook_create(
        self,
        interaction: discord.Interaction,
        name: str,
        event: app_commands.Choice[str],
        action: app_commands.Choice[str],
        text: Optional[str] = None,
        matcher: Optional[str] = None,
        condition: Optional[str] = None,
        reason: Optional[str] = None,
        set_arg: Optional[str] = None,
        url: Optional[str] = None,
        scope: Optional[app_commands.Choice[str]] = None,
        disabled: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        parts = [shlex.quote(name), "--event", event.value, "--action", action.value]
        parts += _opt("--text", text)
        parts += _opt("--matcher", matcher)
        parts += _opt("--cond", condition)
        parts += _opt("--reason", reason)
        parts += _opt("--set", set_arg)
        parts += _opt("--url", url)
        if scope:
            parts += ["--scope", scope.value]
        if disabled:
            parts.append("--disabled")
        await self.bot._send_backend_command(
            interaction, "hook create", args=" ".join(parts)
        )

    @hook_group.command(name="show", description="Show one hook's configuration")
    @app_commands.describe(hook_id="The hook ID (first 8 chars is enough)")
    async def cmd_hook_show(self, interaction: discord.Interaction, hook_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction, "hook show", args=shlex.quote(hook_id)
        )

    @hook_group.command(name="test", description="Dry-run a hook and preview its output")
    @app_commands.describe(hook_id="The hook ID (first 8 chars is enough)")
    async def cmd_hook_test(self, interaction: discord.Interaction, hook_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction, "hook test", args=shlex.quote(hook_id)
        )

    @hook_group.command(name="enable", description="Enable a hook")
    @app_commands.describe(hook_id="The hook ID (first 8 chars is enough)")
    async def cmd_hook_enable(self, interaction: discord.Interaction, hook_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction, "hook enable", args=shlex.quote(hook_id)
        )

    @hook_group.command(name="disable", description="Disable a hook")
    @app_commands.describe(hook_id="The hook ID (first 8 chars is enough)")
    async def cmd_hook_disable(self, interaction: discord.Interaction, hook_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction, "hook disable", args=shlex.quote(hook_id)
        )

    @hook_group.command(name="delete", description="Delete a hook permanently")
    @app_commands.describe(hook_id="The hook ID (first 8 chars is enough)")
    async def cmd_hook_delete(self, interaction: discord.Interaction, hook_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction, "hook delete", args=f"{shlex.quote(hook_id)} --yes"
        )

    @hook_group.command(
        name="edit", description="Edit a hook's fields, condition, or rewrite"
    )
    @app_commands.describe(
        hook_id="The hook ID (first 8 chars is enough)",
        name="New name",
        enabled="Enable or disable",
        event="New event",
        action="Switch the action",
        matcher="New tool filter, e.g. bash or Edit|Write",
        text="New injected/notify/todo text",
        reason="New denial message",
        url="New webhook URL",
        condition="Replace conditions with one 'field operator value'",
        set_arg="Replace rewrites with one 'arg=value'",
    )
    @app_commands.choices(event=_EVENT_CHOICES, action=_ACTION_CHOICES)
    async def cmd_hook_edit(
        self,
        interaction: discord.Interaction,
        hook_id: str,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        event: Optional[app_commands.Choice[str]] = None,
        action: Optional[app_commands.Choice[str]] = None,
        matcher: Optional[str] = None,
        text: Optional[str] = None,
        reason: Optional[str] = None,
        url: Optional[str] = None,
        condition: Optional[str] = None,
        set_arg: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        parts = [shlex.quote(hook_id)]
        parts += _kv("name", name)
        if enabled is not None:
            parts.append(f"enabled={'true' if enabled else 'false'}")
        if event:
            parts.append(f"event={event.value}")
        if action:
            parts.append(f"action={action.value}")
        parts += _kv("matcher", matcher)
        parts += _kv("text", text)
        parts += _kv("reason", reason)
        parts += _kv("url", url)
        parts += _opt("--cond", condition)
        parts += _opt("--set", set_arg)
        await self.bot._send_backend_command(
            interaction, "hook edit", args=" ".join(parts)
        )
