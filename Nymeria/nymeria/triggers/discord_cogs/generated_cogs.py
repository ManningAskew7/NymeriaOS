# GENERATED FILE. DO NOT EDIT BY HAND.
# Regenerate from `Nymeria/` with:
#     python3 scripts/generate_discord_cogs.py
"""Discord slash commands derived from the backend command registry.

Every command here is a defer-and-relay wrapper: it rebuilds the canonical
`/command args` string from the declared `CommandParam` schema and hands it to
`NymeriaDiscordBot._send_backend_command`. Signature, describe copy, choices,
the admin flag, and the flattening all come from the SAME declaration, so none
of them can drift from the registry.

Hand-written cogs still own the commands a relay cannot express (chat streaming,
the bot self-restart, Discord-local rendering, and the families listed in the
generator's `HAND_WRITTEN_FAMILIES`).
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .autocomplete import AUTOCOMPLETE_RESOLVERS

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot


class GeneratedCommandsCog(commands.Cog):
    """Registry-derived slash commands."""

    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    account_group = app_commands.Group(
        name="account",
        description="Inspect account, tokens, and linked platforms",
    )

    activity_group = app_commands.Group(
        name="activity",
        description="Show recent activity and notifications",
    )

    alias_group = app_commands.Group(
        name="alias",
        description="List your personal command aliases (family overview)",
    )

    artifacts_group = app_commands.Group(
        name="artifacts",
        description="Inspect recent workspace artifacts",
    )

    background_group = app_commands.Group(
        name="background",
        description="Show or set the global background/utility model tier",
    )

    browser_group = app_commands.Group(
        name="browser",
        description="Show the live browser login handoff, if one is open (family overview)",
    )

    doctor_group = app_commands.Group(
        name="doctor",
        description="Run server-side diagnostics (auth + model)",
    )

    env_group = app_commands.Group(
        name="env",
        description="View and set environment variables",
    )

    fast_group = app_commands.Group(
        name="fast",
        description="Switch this thread to the fast model tier",
    )

    mcp_group = app_commands.Group(
        name="mcp",
        description="MCP server management commands",
    )

    memory_group = app_commands.Group(
        name="memory",
        description="Manage Nymeria's memories about you",
    )

    model_group = app_commands.Group(
        name="model",
        description="Show or change the model",
    )

    provider_group = app_commands.Group(
        name="provider",
        description="Show the active LLM provider, or browse one provider's actions",
    )

    settings_group = app_commands.Group(
        name="settings",
        description="Show server settings",
    )

    skills_group = app_commands.Group(
        name="skills",
        description="List skills visible on this thread",
    )

    smart_group = app_commands.Group(
        name="smart",
        description="Switch this thread to the smart model tier",
    )

    team_group = app_commands.Group(
        name="team",
        description="List your callable-thread teams",
    )

    todos_group = app_commands.Group(
        name="todos",
        description="Manage scheduled tasks and reminders",
    )

    triggers_group = app_commands.Group(
        name="triggers",
        description="Event-trigger automation commands",
    )

    usage_group = app_commands.Group(
        name="usage",
        description="Show token usage and cost statistics",
    )

    account_tokens_group = app_commands.Group(
        name="tokens",
        description="List API tokens for the current user",
        parent=account_group,
    )

    @account_group.command(
        name="platforms",
        description="List chat platforms linked to the current user",
    )
    async def cmd_account_platforms(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "account platforms",
            require_admin=False,
        )

    @account_group.command(
        name="show",
        description="Show details for the current user",
    )
    async def cmd_account_show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "account show",
            require_admin=False,
        )

    @account_tokens_group.command(
        name="issue",
        description="Issue a new API token for the current user",
    )
    @app_commands.describe(
        label="Label recorded against the new token",
    )
    async def cmd_account_tokens_issue(
        self,
        interaction: discord.Interaction,
        label: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if label is not None:
            parts.append(shlex.quote(label))
        await self.bot._send_backend_command(
            interaction,
            "account tokens issue",
            args=" ".join(parts),
            require_admin=False,
        )

    @account_tokens_group.command(
        name="revoke",
        description="Revoke an API token by hash prefix",
    )
    @app_commands.describe(
        prefix="Token hash prefix shown by /account tokens",
    )
    async def cmd_account_tokens_revoke(
        self,
        interaction: discord.Interaction,
        prefix: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(prefix))
        await self.bot._send_backend_command(
            interaction,
            "account tokens revoke",
            args=" ".join(parts),
            require_admin=False,
        )

    @activity_group.command(
        name="list",
        description="Show the most recent activity entries",
    )
    @app_commands.describe(
        limit="How many entries to show",
        type="Activity type to filter on",
        thread="Thread id, or `current` for this thread",
    )
    async def cmd_activity_list(
        self,
        interaction: discord.Interaction,
        limit: Optional[int] = None,
        type: Optional[str] = None,
        thread: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if type is not None:
            parts.extend(("--type", shlex.quote(type)))
        if thread is not None:
            parts.extend(("--thread", shlex.quote(thread)))
        if limit is not None:
            parts.append(shlex.quote(str(limit)))
        await self.bot._send_backend_command(
            interaction,
            "activity list",
            args=" ".join(parts),
            require_admin=False,
        )

    @activity_group.command(
        name="notifications",
        description="Show notifications and unread count",
    )
    async def cmd_activity_notifications(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "activity notifications",
            require_admin=False,
        )

    @alias_group.command(
        name="create",
        description="Create a personal alias that expands to a full command, values included",
    )
    @app_commands.describe(
        name="The new spelling, one word (e.g. gpt5)",
        expansion="The command it stands for, e.g. model openai/gpt-5.5",
    )
    async def cmd_alias_create(
        self,
        interaction: discord.Interaction,
        name: str,
        expansion: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(name))
        parts.append(shlex.quote(expansion))
        await self.bot._send_backend_command(
            interaction,
            "alias create",
            args=" ".join(parts),
            require_admin=False,
        )

    @alias_group.command(
        name="delete",
        description="Delete one of your command aliases",
    )
    @app_commands.describe(
        name="Alias name",
    )
    async def cmd_alias_delete(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(name))
        await self.bot._send_backend_command(
            interaction,
            "alias delete",
            args=" ".join(parts),
            require_admin=False,
        )

    @alias_group.command(
        name="list",
        description="List your command aliases with author and health flags",
    )
    async def cmd_alias_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "alias list",
            require_admin=False,
        )

    @artifacts_group.command(
        name="list",
        description="List recent workspace artifacts from thread history",
    )
    @app_commands.describe(
        limit="How many artifacts to show",
    )
    async def cmd_artifacts_list(
        self,
        interaction: discord.Interaction,
        limit: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if limit is not None:
            parts.append(shlex.quote(str(limit)))
        await self.bot._send_backend_command(
            interaction,
            "artifacts list",
            args=" ".join(parts),
            require_admin=False,
        )

    @background_group.command(
        name="clear",
        description="Clear the background tier (falls back to the main model)",
    )
    async def cmd_background_clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "background clear",
            require_admin=False,
        )

    @background_group.command(
        name="set",
        description="Set the model id stored for the background tier",
    )
    @app_commands.describe(
        model="Model id, or provider:model to route the tier elsewhere",
    )
    async def cmd_background_set(
        self,
        interaction: discord.Interaction,
        model: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(model))
        await self.bot._send_backend_command(
            interaction,
            "background set",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_background_set.autocomplete("model")
    async def _ac_background_set_model(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["models"](
            self.bot, interaction, current
        )

    @background_group.command(
        name="set-url",
        description="Set a base URL override for the background tier",
    )
    @app_commands.describe(
        base_url="Base URL the background tier should call",
    )
    async def cmd_background_set_url(
        self,
        interaction: discord.Interaction,
        base_url: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(base_url))
        await self.bot._send_backend_command(
            interaction,
            "background set-url",
            args=" ".join(parts),
            require_admin=False,
        )

    @browser_group.command(
        name="default",
        description="Show or set the account-wide default browser for commands",
    )
    @app_commands.describe(
        browser="Which browser: label, id, or unique fragment; 'clear' unsets it; omit to show the current default",
    )
    async def cmd_browser_default(
        self,
        interaction: discord.Interaction,
        browser: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if browser is not None:
            parts.append(shlex.quote(browser))
        await self.bot._send_backend_command(
            interaction,
            "browser default",
            args=" ".join(parts),
            require_admin=False,
        )

    @browser_group.command(
        name="list",
        description="List the connected browsers and which one commands drive",
    )
    async def cmd_browser_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "browser list",
            require_admin=False,
        )

    @browser_group.command(
        name="login",
        description="Open a login window to sign the agent's browser into a site by hand",
    )
    @app_commands.describe(
        url="The sign-in page to open (a fresh tab is used)",
    )
    async def cmd_browser_login(
        self,
        interaction: discord.Interaction,
        url: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(url))
        await self.bot._send_backend_command(
            interaction,
            "browser login",
            args=" ".join(parts),
            require_admin=False,
        )

    @browser_group.command(
        name="rename",
        description="Name a browser so it is easy to pick",
    )
    @app_commands.describe(
        browser="The browser to name: current label, id, or unique fragment",
        label="The new name; omit to remove the current name",
    )
    async def cmd_browser_rename(
        self,
        interaction: discord.Interaction,
        browser: str,
        label: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(browser))
        if label is not None:
            parts.append(shlex.quote(label))
        await self.bot._send_backend_command(
            interaction,
            "browser rename",
            args=" ".join(parts),
            require_admin=False,
        )

    @browser_group.command(
        name="switch",
        description="Route this thread's browser commands to one browser",
    )
    @app_commands.describe(
        browser="Which browser: label, id, or unique fragment; 'clear' removes this thread's override",
    )
    async def cmd_browser_switch(
        self,
        interaction: discord.Interaction,
        browser: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(browser))
        await self.bot._send_backend_command(
            interaction,
            "browser switch",
            args=" ".join(parts),
            require_admin=False,
        )

    @app_commands.command(
        name="context",
        description="Detailed context and tool breakdown",
    )
    async def cmd_context(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "context",
            require_admin=False,
        )

    @doctor_group.command(
        name="auth",
        description="Show the resolved identity for the current request",
    )
    async def cmd_doctor_auth(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "doctor auth",
            require_admin=False,
        )

    @doctor_group.command(
        name="model",
        description="Show LLM provider/model diagnostics",
    )
    async def cmd_doctor_model(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "doctor model",
            require_admin=False,
        )

    @env_group.command(
        name="get",
        description="Show one unmasked environment variable",
    )
    @app_commands.describe(
        key="Environment variable name",
    )
    async def cmd_env_get(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        await self.bot._send_backend_command(
            interaction,
            "env get",
            args=" ".join(parts),
            require_admin=True,
        )

    @env_group.command(
        name="set",
        description="Change an environment variable",
    )
    @app_commands.describe(
        key="Environment variable name",
        value="New value (coerced to bool, int, float, or string)",
    )
    async def cmd_env_set(
        self,
        interaction: discord.Interaction,
        key: str,
        value: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        parts.append(shlex.quote(value))
        await self.bot._send_backend_command(
            interaction,
            "env set",
            args=" ".join(parts),
            require_admin=True,
        )

    @env_group.command(
        name="show",
        description="Show environment variables",
    )
    async def cmd_env_show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "env show",
            require_admin=True,
        )

    @fast_group.command(
        name="set",
        description="Set the model id stored for the fast tier",
    )
    @app_commands.describe(
        model="Model id, or provider:model to route the tier elsewhere",
    )
    async def cmd_fast_set(
        self,
        interaction: discord.Interaction,
        model: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(model))
        await self.bot._send_backend_command(
            interaction,
            "fast set",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_fast_set.autocomplete("model")
    async def _ac_fast_set_model(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["models"](
            self.bot, interaction, current
        )

    @mcp_group.command(
        name="delete",
        description="Remove an MCP server and its tools",
    )
    @app_commands.describe(
        server_id="Configured MCP server id",
    )
    async def cmd_mcp_delete(
        self,
        interaction: discord.Interaction,
        server_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(server_id))
        await self.bot._send_backend_command(
            interaction,
            "mcp delete",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_mcp_delete.autocomplete("server_id")
    async def _ac_mcp_delete_server_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["mcp_servers"](
            self.bot, interaction, current
        )

    @mcp_group.command(
        name="discover",
        description="Force tool rediscovery for an MCP server",
    )
    @app_commands.describe(
        server_id="Configured MCP server id",
    )
    async def cmd_mcp_discover(
        self,
        interaction: discord.Interaction,
        server_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(server_id))
        await self.bot._send_backend_command(
            interaction,
            "mcp discover",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_mcp_discover.autocomplete("server_id")
    async def _ac_mcp_discover_server_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["mcp_servers"](
            self.bot, interaction, current
        )

    @mcp_group.command(
        name="list",
        description="List configured MCP servers",
    )
    async def cmd_mcp_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "mcp list",
            require_admin=True,
        )

    @mcp_group.command(
        name="logs",
        description="Show install logs for an MCP server",
    )
    @app_commands.describe(
        server_id="Configured MCP server id",
        limit="How many trailing log lines to show",
    )
    async def cmd_mcp_logs(
        self,
        interaction: discord.Interaction,
        server_id: str,
        limit: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(server_id))
        if limit is not None:
            parts.append(shlex.quote(str(limit)))
        await self.bot._send_backend_command(
            interaction,
            "mcp logs",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_mcp_logs.autocomplete("server_id")
    async def _ac_mcp_logs_server_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["mcp_servers"](
            self.bot, interaction, current
        )

    @mcp_group.command(
        name="retry",
        description="Retry setup for a draft or failed MCP server",
    )
    @app_commands.describe(
        server_id="Configured MCP server id",
    )
    async def cmd_mcp_retry(
        self,
        interaction: discord.Interaction,
        server_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(server_id))
        await self.bot._send_backend_command(
            interaction,
            "mcp retry",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_mcp_retry.autocomplete("server_id")
    async def _ac_mcp_retry_server_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["mcp_servers"](
            self.bot, interaction, current
        )

    @mcp_group.command(
        name="status",
        description="Show MCP server status, with errors if any",
    )
    @app_commands.describe(
        server_id="Configured MCP server id (omit for every server)",
    )
    async def cmd_mcp_status(
        self,
        interaction: discord.Interaction,
        server_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if server_id is not None:
            parts.append(shlex.quote(server_id))
        await self.bot._send_backend_command(
            interaction,
            "mcp status",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_mcp_status.autocomplete("server_id")
    async def _ac_mcp_status_server_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["mcp_servers"](
            self.bot, interaction, current
        )

    @mcp_group.command(
        name="test",
        description="Test connectivity to an MCP server",
    )
    @app_commands.describe(
        server_id="Configured MCP server id",
    )
    async def cmd_mcp_test(
        self,
        interaction: discord.Interaction,
        server_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(server_id))
        await self.bot._send_backend_command(
            interaction,
            "mcp test",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_mcp_test.autocomplete("server_id")
    async def _ac_mcp_test_server_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["mcp_servers"](
            self.bot, interaction, current
        )

    @memory_group.command(
        name="delete",
        description="Forget a memory",
    )
    @app_commands.describe(
        key="Memory key to remove",
    )
    async def cmd_memory_delete(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        await self.bot._send_backend_command(
            interaction,
            "memory delete",
            args=" ".join(parts),
            require_admin=False,
        )

    @memory_group.command(
        name="limit",
        description="Show or change memory character limits",
    )
    @app_commands.describe(
        value="Character limit, or `inherit` to drop a thread override",
        scope="Which limit to change (omit both to show them)",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_memory_limit(
        self,
        interaction: discord.Interaction,
        value: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if value is not None:
            parts.append(shlex.quote(value))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "memory limit",
            args=" ".join(parts),
            require_admin=False,
        )

    @memory_group.command(
        name="list",
        description="List saved memories",
    )
    async def cmd_memory_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "memory list",
            require_admin=False,
        )

    @memory_group.command(
        name="save",
        description="Save a memory",
    )
    @app_commands.describe(
        key="Memory key",
        value="Value to store under the key",
    )
    async def cmd_memory_save(
        self,
        interaction: discord.Interaction,
        key: str,
        value: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        parts.append(shlex.quote(value))
        await self.bot._send_backend_command(
            interaction,
            "memory save",
            args=" ".join(parts),
            require_admin=False,
        )

    @memory_group.command(
        name="search",
        description="Search saved memories",
    )
    @app_commands.describe(
        query="Text to match against keys and values",
    )
    async def cmd_memory_search(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(query))
        await self.bot._send_backend_command(
            interaction,
            "memory search",
            args=" ".join(parts),
            require_admin=False,
        )

    @model_group.command(
        name="list",
        description="List available provider models",
    )
    async def cmd_model_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "model list",
            require_admin=False,
        )

    @provider_group.command(
        name="list",
        description="List LLM providers grouped by support tier",
    )
    @app_commands.describe(
        all="Include every registry provider, unverified tier included",
        tier="Show one tier only",
    )
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="cliproxy", value="cliproxy"),
            app_commands.Choice(name="native", value="native"),
            app_commands.Choice(name="gateway", value="gateway"),
            app_commands.Choice(name="unverified", value="unverified"),
        ],
    )
    async def cmd_provider_list(
        self,
        interaction: discord.Interaction,
        all: Optional[bool] = None,
        tier: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if all is True:
            parts.append("--all")
        if tier is not None:
            parts.extend(("--tier", shlex.quote(tier)))
        await self.bot._send_backend_command(
            interaction,
            "provider list",
            args=" ".join(parts),
            require_admin=False,
        )

    @provider_group.command(
        name="reasoning-passback",
        description="Show whether prior-turn reasoning is replayed to the model",
    )
    async def cmd_provider_reasoning_passback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "provider reasoning-passback",
            require_admin=False,
        )

    @provider_group.command(
        name="switch",
        description="Switch the active LLM provider globally or for this thread",
    )
    @app_commands.describe(
        provider="Provider id to switch to",
        scope="Switch globally (default) or for this thread",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_provider_switch(
        self,
        interaction: discord.Interaction,
        provider: str,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(provider))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "provider switch",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_provider_switch.autocomplete("provider")
    async def _ac_provider_switch_provider(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["providers"](
            self.bot, interaction, current
        )

    @provider_group.command(
        name="test",
        description="Test provider connectivity without saving anything",
    )
    @app_commands.describe(
        provider="Provider to test (default: the active one)",
    )
    async def cmd_provider_test(
        self,
        interaction: discord.Interaction,
        provider: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if provider is not None:
            parts.append(shlex.quote(provider))
        await self.bot._send_backend_command(
            interaction,
            "provider test",
            args=" ".join(parts),
            require_admin=True,
        )

    @cmd_provider_test.autocomplete("provider")
    async def _ac_provider_test_provider(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["providers"](
            self.bot, interaction, current
        )

    @app_commands.command(
        name="prune",
        description="Compress tool returns in the active thread (no LLM)",
    )
    @app_commands.describe(
        mode="soft truncates each tool result to 500 chars; full drops it to a placeholder",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="soft", value="soft"),
            app_commands.Choice(name="full", value="full"),
        ],
    )
    async def cmd_prune(
        self,
        interaction: discord.Interaction,
        mode: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if mode is not None:
            parts.append(shlex.quote(mode))
        await self.bot._send_backend_command(
            interaction,
            "prune",
            args=" ".join(parts),
            require_admin=False,
        )

    @app_commands.command(
        name="sequential-tools",
        description="Show or set sequential (ordered, one-at-a-time) tool execution",
    )
    @app_commands.describe(
        mode="Turn it on/off, or `inherit` to drop the override",
        scope="Write the global default or this thread's override",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="inherit", value="inherit"),
            app_commands.Choice(name="default", value="default"),
        ],
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_sequential_tools(
        self,
        interaction: discord.Interaction,
        mode: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if mode is not None:
            parts.append(shlex.quote(mode))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "sequential-tools",
            args=" ".join(parts),
            require_admin=False,
        )

    @settings_group.command(
        name="get",
        description="Show one server setting",
    )
    @app_commands.describe(
        key="Setting key",
    )
    async def cmd_settings_get(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        await self.bot._send_backend_command(
            interaction,
            "settings get",
            args=" ".join(parts),
            require_admin=False,
        )

    @settings_group.command(
        name="reload",
        description="Re-read the config files and apply what changed",
    )
    async def cmd_settings_reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "settings reload",
            require_admin=True,
        )

    @settings_group.command(
        name="set",
        description="Change a server setting",
    )
    @app_commands.describe(
        key="Setting key",
        value="New value (coerced to bool, int, float, or string)",
    )
    async def cmd_settings_set(
        self,
        interaction: discord.Interaction,
        key: str,
        value: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        parts.append(shlex.quote(value))
        await self.bot._send_backend_command(
            interaction,
            "settings set",
            args=" ".join(parts),
            require_admin=True,
        )

    @skills_group.command(
        name="disable",
        description="Disable a skill, or `all` to deactivate every active one",
    )
    @app_commands.describe(
        name="Installed skill name, or `all` for every active skill",
        scope="Write this thread's skill set (default) or the global one",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_skills_disable(
        self,
        interaction: discord.Interaction,
        name: str,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(name))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "skills disable",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_skills_disable.autocomplete("name")
    async def _ac_skills_disable_name(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["skills"](
            self.bot, interaction, current
        )

    @skills_group.command(
        name="enable",
        description="Enable a skill on this thread (default) or globally",
    )
    @app_commands.describe(
        name="Installed skill name",
        scope="Write this thread's skill set (default) or the global one",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_skills_enable(
        self,
        interaction: discord.Interaction,
        name: str,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(name))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "skills enable",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_skills_enable.autocomplete("name")
    async def _ac_skills_enable_name(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["skills"](
            self.bot, interaction, current
        )

    @skills_group.command(
        name="install",
        description="Install a skill from a marketplace",
    )
    @app_commands.describe(
        name="Marketplace skill name to install",
        source="Marketplace source to install from",
        scope="Install for this user or for everyone",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="user", value="user"),
            app_commands.Choice(name="global", value="global"),
        ],
    )
    async def cmd_skills_install(
        self,
        interaction: discord.Interaction,
        name: str,
        source: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if source is not None:
            parts.extend(("--source", shlex.quote(source)))
        if scope is not None:
            parts.extend(("--scope", shlex.quote(scope)))
        parts.append(shlex.quote(name))
        await self.bot._send_backend_command(
            interaction,
            "skills install",
            args=" ".join(parts),
            require_admin=False,
        )

    @skills_group.command(
        name="list",
        description="List skills visible on this thread",
    )
    async def cmd_skills_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "skills list",
            require_admin=False,
        )

    @skills_group.command(
        name="search",
        description="Search a skills marketplace for installable skills",
    )
    @app_commands.describe(
        query="Words to match against marketplace skill names",
        source="Marketplace source to search",
    )
    async def cmd_skills_search(
        self,
        interaction: discord.Interaction,
        query: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if source is not None:
            parts.extend(("--source", shlex.quote(source)))
        if query is not None:
            parts.append(" ".join(shlex.quote(_word) for _word in query.split()))
        await self.bot._send_backend_command(
            interaction,
            "skills search",
            args=" ".join(parts),
            require_admin=False,
        )

    @skills_group.command(
        name="show",
        description="Show a skill's metadata and markdown body without activating it",
    )
    @app_commands.describe(
        name="Installed skill name",
    )
    async def cmd_skills_show(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(name))
        await self.bot._send_backend_command(
            interaction,
            "skills show",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_skills_show.autocomplete("name")
    async def _ac_skills_show_name(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["skills"](
            self.bot, interaction, current
        )

    @smart_group.command(
        name="set",
        description="Set the model id stored for the smart tier",
    )
    @app_commands.describe(
        model="Model id, or provider:model to route the tier elsewhere",
    )
    async def cmd_smart_set(
        self,
        interaction: discord.Interaction,
        model: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(model))
        await self.bot._send_backend_command(
            interaction,
            "smart set",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_smart_set.autocomplete("model")
    async def _ac_smart_set_model(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["models"](
            self.bot, interaction, current
        )

    @app_commands.command(
        name="status",
        description="Model, context, tools, and task summary",
    )
    async def cmd_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "status",
            require_admin=False,
        )

    @team_group.command(
        name="list",
        description="List your callable-thread teams",
    )
    async def cmd_team_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "team list",
            require_admin=False,
        )

    @team_group.command(
        name="show",
        description="Show one team's members and description",
    )
    @app_commands.describe(
        team="Team id or team name",
    )
    async def cmd_team_show(
        self,
        interaction: discord.Interaction,
        team: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(team))
        await self.bot._send_backend_command(
            interaction,
            "team show",
            args=" ".join(parts),
            require_admin=False,
        )

    @app_commands.command(
        name="think",
        description="Show or change thinking mode (thread-scoped when a thread is active)",
    )
    @app_commands.describe(
        mode="Thinking state or reasoning effort level",
        scope="Write the global default or this thread's override",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="low", value="low"),
            app_commands.Choice(name="medium", value="medium"),
            app_commands.Choice(name="high", value="high"),
            app_commands.Choice(name="xhigh", value="xhigh"),
            app_commands.Choice(name="max", value="max"),
        ],
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_think(
        self,
        interaction: discord.Interaction,
        mode: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if mode is not None:
            parts.append(shlex.quote(mode))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "think",
            args=" ".join(parts),
            require_admin=False,
        )

    @todos_group.command(
        name="add",
        description="Add a TODO",
    )
    @app_commands.describe(
        task="Task text",
        schedule="When to fire (45s/2h/1d or absolute; `none` for no schedule; default 1d)",
        notes="Notes attached to the TODO",
        repeat="Recurrence interval (daily, 2h, weekly, ...)",
        thread="Thread to attach to (default: current)",
    )
    async def cmd_todos_add(
        self,
        interaction: discord.Interaction,
        task: str,
        schedule: Optional[str] = None,
        notes: Optional[str] = None,
        repeat: Optional[str] = None,
        thread: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if schedule is not None:
            parts.extend(("--schedule", shlex.quote(schedule)))
        if notes is not None:
            parts.extend(("--notes", shlex.quote(notes)))
        if repeat is not None:
            parts.extend(("--repeat", shlex.quote(repeat)))
        if thread is not None:
            parts.extend(("--thread", shlex.quote(thread)))
        parts.append(" ".join(shlex.quote(_word) for _word in task.split()))
        await self.bot._send_backend_command(
            interaction,
            "todos add",
            args=" ".join(parts),
            require_admin=False,
        )

    @todos_group.command(
        name="complete",
        description="Complete a TODO",
    )
    @app_commands.describe(
        todo_id="TODO id or unique id prefix",
    )
    async def cmd_todos_complete(
        self,
        interaction: discord.Interaction,
        todo_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(todo_id))
        await self.bot._send_backend_command(
            interaction,
            "todos complete",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_todos_complete.autocomplete("todo_id")
    async def _ac_todos_complete_todo_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["todos"](
            self.bot, interaction, current
        )

    @todos_group.command(
        name="delete",
        description="Delete a TODO",
    )
    @app_commands.describe(
        todo_id="TODO id or unique id prefix",
        yes="Accepted for compatibility; deletion does not prompt",
    )
    async def cmd_todos_delete(
        self,
        interaction: discord.Interaction,
        todo_id: str,
        yes: Optional[bool] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if yes is True:
            parts.append("--yes")
        parts.append(shlex.quote(todo_id))
        await self.bot._send_backend_command(
            interaction,
            "todos delete",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_todos_delete.autocomplete("todo_id")
    async def _ac_todos_delete_todo_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["todos"](
            self.bot, interaction, current
        )

    @todos_group.command(
        name="edit",
        description="Edit a TODO",
    )
    @app_commands.describe(
        todo_id="TODO id or unique id prefix",
        task="New task text",
        status="New status",
        notes="Replace the notes",
        schedule="New fire time",
        clear_schedule="Remove the schedule",
        repeat="New recurrence interval",
        clear_repeat="Remove the recurrence",
        thread="Rebind to a thread",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="in_progress", value="in_progress"),
            app_commands.Choice(name="done", value="done"),
        ],
    )
    async def cmd_todos_edit(
        self,
        interaction: discord.Interaction,
        todo_id: str,
        task: Optional[str] = None,
        status: Optional[str] = None,
        notes: Optional[str] = None,
        schedule: Optional[str] = None,
        clear_schedule: Optional[bool] = None,
        repeat: Optional[str] = None,
        clear_repeat: Optional[bool] = None,
        thread: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if status is not None:
            parts.extend(("--status", shlex.quote(status)))
        if notes is not None:
            parts.extend(("--notes", shlex.quote(notes)))
        if schedule is not None:
            parts.extend(("--schedule", shlex.quote(schedule)))
        if clear_schedule is True:
            parts.append("--clear-schedule")
        if repeat is not None:
            parts.extend(("--repeat", shlex.quote(repeat)))
        if clear_repeat is True:
            parts.append("--clear-repeat")
        if thread is not None:
            parts.extend(("--thread", shlex.quote(thread)))
        parts.append(shlex.quote(todo_id))
        if task is not None:
            parts.append(" ".join(shlex.quote(_word) for _word in task.split()))
        await self.bot._send_backend_command(
            interaction,
            "todos edit",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_todos_edit.autocomplete("todo_id")
    async def _ac_todos_edit_todo_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["todos"](
            self.bot, interaction, current
        )

    @todos_group.command(
        name="list",
        description="List TODOs",
    )
    @app_commands.describe(
        filter="Status filter (default: active)",
        thread="Only TODOs on this thread (`current` for the active one)",
    )
    @app_commands.choices(
        filter=[
            app_commands.Choice(name="active", value="active"),
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="in_progress", value="in_progress"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ],
    )
    async def cmd_todos_list(
        self,
        interaction: discord.Interaction,
        filter: Optional[str] = None,
        thread: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if thread is not None:
            parts.extend(("--thread", shlex.quote(thread)))
        if filter is not None:
            parts.append(shlex.quote(filter))
        await self.bot._send_backend_command(
            interaction,
            "todos list",
            args=" ".join(parts),
            require_admin=False,
        )

    @todos_group.command(
        name="repeat",
        description="Set or clear a TODO's recurrence",
    )
    @app_commands.describe(
        todo_id="TODO id or unique id prefix",
        interval="Recurrence interval (daily, 2h, weekly, ...), or `clear`",
    )
    async def cmd_todos_repeat(
        self,
        interaction: discord.Interaction,
        todo_id: str,
        interval: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(todo_id))
        parts.append(shlex.quote(interval))
        await self.bot._send_backend_command(
            interaction,
            "todos repeat",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_todos_repeat.autocomplete("todo_id")
    async def _ac_todos_repeat_todo_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["todos"](
            self.bot, interaction, current
        )

    @todos_group.command(
        name="schedule",
        description="Set or clear a TODO's fire time",
    )
    @app_commands.describe(
        todo_id="TODO id or unique id prefix",
        when="New fire time, or `clear` to remove it",
    )
    async def cmd_todos_schedule(
        self,
        interaction: discord.Interaction,
        todo_id: str,
        when: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(todo_id))
        parts.append(" ".join(shlex.quote(_word) for _word in when.split()))
        await self.bot._send_backend_command(
            interaction,
            "todos schedule",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_todos_schedule.autocomplete("todo_id")
    async def _ac_todos_schedule_todo_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["todos"](
            self.bot, interaction, current
        )

    @triggers_group.command(
        name="delete",
        description="Delete an event trigger permanently",
    )
    @app_commands.describe(
        trigger_id="Event trigger id",
    )
    async def cmd_triggers_delete(
        self,
        interaction: discord.Interaction,
        trigger_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(trigger_id))
        await self.bot._send_backend_command(
            interaction,
            "triggers delete",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_triggers_delete.autocomplete("trigger_id")
    async def _ac_triggers_delete_trigger_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["triggers"](
            self.bot, interaction, current
        )

    @triggers_group.command(
        name="disable",
        description="Disable an event trigger",
    )
    @app_commands.describe(
        trigger_id="Event trigger id",
    )
    async def cmd_triggers_disable(
        self,
        interaction: discord.Interaction,
        trigger_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(trigger_id))
        await self.bot._send_backend_command(
            interaction,
            "triggers disable",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_triggers_disable.autocomplete("trigger_id")
    async def _ac_triggers_disable_trigger_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["triggers"](
            self.bot, interaction, current
        )

    @triggers_group.command(
        name="enable",
        description="Enable an event trigger",
    )
    @app_commands.describe(
        trigger_id="Event trigger id",
    )
    async def cmd_triggers_enable(
        self,
        interaction: discord.Interaction,
        trigger_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(trigger_id))
        await self.bot._send_backend_command(
            interaction,
            "triggers enable",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_triggers_enable.autocomplete("trigger_id")
    async def _ac_triggers_enable_trigger_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["triggers"](
            self.bot, interaction, current
        )

    @triggers_group.command(
        name="history",
        description="Show recent trigger execution history",
    )
    @app_commands.describe(
        trigger_id="Only this trigger's executions",
        limit="How many executions to show",
    )
    async def cmd_triggers_history(
        self,
        interaction: discord.Interaction,
        trigger_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if limit is not None:
            parts.extend(("--limit", shlex.quote(str(limit))))
        if trigger_id is not None:
            parts.append(shlex.quote(trigger_id))
        await self.bot._send_backend_command(
            interaction,
            "triggers history",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_triggers_history.autocomplete("trigger_id")
    async def _ac_triggers_history_trigger_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["triggers"](
            self.bot, interaction, current
        )

    @triggers_group.command(
        name="list",
        description="List configured event triggers",
    )
    @app_commands.describe(
        enabled_only="Hide disabled triggers",
        thread="Only triggers bound to this thread (`current` for the active one)",
    )
    async def cmd_triggers_list(
        self,
        interaction: discord.Interaction,
        enabled_only: Optional[bool] = None,
        thread: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if enabled_only is True:
            parts.append("--enabled-only")
        if thread is not None:
            parts.extend(("--thread", shlex.quote(thread)))
        await self.bot._send_backend_command(
            interaction,
            "triggers list",
            args=" ".join(parts),
            require_admin=False,
        )

    @triggers_group.command(
        name="resume",
        description="Clear a trigger's auto-pause and failure history",
    )
    @app_commands.describe(
        trigger_id="Event trigger id",
    )
    async def cmd_triggers_resume(
        self,
        interaction: discord.Interaction,
        trigger_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(trigger_id))
        await self.bot._send_backend_command(
            interaction,
            "triggers resume",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_triggers_resume.autocomplete("trigger_id")
    async def _ac_triggers_resume_trigger_id(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["triggers"](
            self.bot, interaction, current
        )

    @usage_group.command(
        name="session",
        description="Show session-wide (cumulative) token usage for this thread",
    )
    async def cmd_usage_session(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "usage session",
            require_admin=False,
        )


GENERATED_COMMAND_NAMES: tuple[str, ...] = (
    "account platforms",
    "account show",
    "account tokens issue",
    "account tokens revoke",
    "activity list",
    "activity notifications",
    "alias create",
    "alias delete",
    "alias list",
    "artifacts list",
    "background clear",
    "background set",
    "background set-url",
    "browser default",
    "browser list",
    "browser login",
    "browser rename",
    "browser switch",
    "context",
    "doctor auth",
    "doctor model",
    "env get",
    "env set",
    "env show",
    "fast set",
    "mcp delete",
    "mcp discover",
    "mcp list",
    "mcp logs",
    "mcp retry",
    "mcp status",
    "mcp test",
    "memory delete",
    "memory limit",
    "memory list",
    "memory save",
    "memory search",
    "model list",
    "provider list",
    "provider reasoning-passback",
    "provider switch",
    "provider test",
    "prune",
    "sequential-tools",
    "settings get",
    "settings reload",
    "settings set",
    "skills disable",
    "skills enable",
    "skills install",
    "skills list",
    "skills search",
    "skills show",
    "smart set",
    "status",
    "team list",
    "team show",
    "think",
    "todos add",
    "todos complete",
    "todos delete",
    "todos edit",
    "todos list",
    "todos repeat",
    "todos schedule",
    "triggers delete",
    "triggers disable",
    "triggers enable",
    "triggers history",
    "triggers list",
    "triggers resume",
    "usage session",
)

# Qualified Discord command name -> the registry category it
# belongs to, so /help groups these commands by subject rather
# than by the single cog class that happens to host them.
COMMAND_CATEGORIES: dict[str, str] = {
    "account platforms": "Personal",
    "account show": "Personal",
    "account tokens issue": "Personal",
    "account tokens revoke": "Personal",
    "activity list": "Personal",
    "activity notifications": "Personal",
    "alias create": "Settings",
    "alias delete": "Settings",
    "alias list": "Settings",
    "artifacts list": "Personal",
    "background clear": "LLM",
    "background set": "LLM",
    "background set-url": "LLM",
    "browser default": "Tools",
    "browser list": "Tools",
    "browser login": "Tools",
    "browser rename": "Tools",
    "browser switch": "Tools",
    "context": "Status",
    "doctor auth": "System",
    "doctor model": "System",
    "env get": "Settings",
    "env set": "Settings",
    "env show": "Settings",
    "fast set": "LLM",
    "mcp delete": "MCP",
    "mcp discover": "MCP",
    "mcp list": "MCP",
    "mcp logs": "MCP",
    "mcp retry": "MCP",
    "mcp status": "MCP",
    "mcp test": "MCP",
    "memory delete": "Memory",
    "memory limit": "Memory",
    "memory list": "Memory",
    "memory save": "Memory",
    "memory search": "Memory",
    "model list": "LLM",
    "provider list": "LLM",
    "provider reasoning-passback": "LLM",
    "provider switch": "LLM",
    "provider test": "LLM",
    "prune": "Thread",
    "sequential-tools": "Tools",
    "settings get": "Settings",
    "settings reload": "Settings",
    "settings set": "Settings",
    "skills disable": "Skills",
    "skills enable": "Skills",
    "skills install": "Skills",
    "skills list": "Skills",
    "skills search": "Skills",
    "skills show": "Skills",
    "smart set": "LLM",
    "status": "Status",
    "team list": "Thread",
    "team show": "Thread",
    "think": "LLM",
    "todos add": "TODOs",
    "todos complete": "TODOs",
    "todos delete": "TODOs",
    "todos edit": "TODOs",
    "todos list": "TODOs",
    "todos repeat": "TODOs",
    "todos schedule": "TODOs",
    "triggers delete": "Automation",
    "triggers disable": "Automation",
    "triggers enable": "Automation",
    "triggers history": "Automation",
    "triggers list": "Automation",
    "triggers resume": "Automation",
    "usage session": "Status",
}
