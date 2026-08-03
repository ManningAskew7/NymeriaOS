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

    artifacts_group = app_commands.Group(
        name="artifacts",
        description="Inspect recent workspace artifacts",
    )

    background_group = app_commands.Group(
        name="background",
        description="Show or set the global background/utility model tier",
    )

    config_group = app_commands.Group(
        name="config",
        description="View and update Nymeria settings",
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

    provider_group = app_commands.Group(
        name="provider",
        description="Show the active LLM provider, or browse one provider's actions",
    )

    settings_group = app_commands.Group(
        name="settings",
        description="Show or change server settings (delegates to /config)",
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

    skills_off_group = app_commands.Group(
        name="off",
        description="Turn skills off",
        parent=skills_group,
    )

    @account_group.command(
        name="current",
        description="Show details for the current user",
    )
    async def cmd_account_current(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "account current",
            require_admin=False,
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

    @artifacts_group.command(
        name="recent",
        description="List recent workspace artifacts from thread history",
    )
    @app_commands.describe(
        limit="How many artifacts to show",
    )
    async def cmd_artifacts_recent(
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
            "artifacts recent",
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

    @app_commands.command(
        name="branch",
        description="Branch the active thread into a new thread",
    )
    @app_commands.describe(
        from_="Branch from this message index (1-based)",
        title="Title for the new branch",
    )
    @app_commands.rename(
        from_="from",
    )
    async def cmd_branch(
        self,
        interaction: discord.Interaction,
        from_: Optional[int] = None,
        title: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if from_ is not None:
            parts.extend(("--from", shlex.quote(str(from_))))
        if title is not None:
            parts.append(shlex.quote(title))
        await self.bot._send_backend_command(
            interaction,
            "branch",
            args=" ".join(parts),
            require_admin=False,
        )

    @config_group.command(
        name="get",
        description="Show one server setting",
    )
    @app_commands.describe(
        key="Setting key",
    )
    async def cmd_config_get(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        await self.bot._send_backend_command(
            interaction,
            "config get",
            args=" ".join(parts),
            require_admin=False,
        )

    @config_group.command(
        name="set",
        description="Change a server setting",
    )
    @app_commands.describe(
        key="Setting key",
        value="New value (coerced to bool, int, float, or string)",
    )
    async def cmd_config_set(
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
            "config set",
            args=" ".join(parts),
            require_admin=True,
        )

    @config_group.command(
        name="show",
        description="Show server settings",
    )
    async def cmd_config_show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "config show",
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

    @mcp_group.command(
        name="remove",
        description="Remove an MCP server and its tools",
    )
    @app_commands.describe(
        server_id="Configured MCP server id",
    )
    async def cmd_mcp_remove(
        self,
        interaction: discord.Interaction,
        server_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(server_id))
        await self.bot._send_backend_command(
            interaction,
            "mcp remove",
            args=" ".join(parts),
            require_admin=True,
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

    @memory_group.command(
        name="forget",
        description="Forget a memory",
    )
    @app_commands.describe(
        key="Memory key to remove",
    )
    async def cmd_memory_forget(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(key))
        await self.bot._send_backend_command(
            interaction,
            "memory forget",
            args=" ".join(parts),
            require_admin=False,
        )

    @memory_group.command(
        name="limit",
        description="Show or change memory character limits",
    )
    @app_commands.describe(
        scope="Which limit to change (omit to show both)",
        value="Character limit, or `inherit` to drop a thread override",
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
        scope: Optional[str] = None,
        value: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if value is not None and scope is None:
            await self.bot._send_interaction_text(
                interaction,
                "Error: `value` also needs `scope`. Give both, or neither.",
            )
            return
        parts: list[str] = []
        if scope is not None:
            parts.append(shlex.quote(scope))
        if value is not None:
            parts.append(shlex.quote(value))
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

    @app_commands.command(
        name="model",
        description="Show or change the model",
    )
    @app_commands.describe(
        name="Model id to switch to",
        force="Accept a model the provider does not list",
        scope="Write the global default or this thread's override",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ],
    )
    async def cmd_model(
        self,
        interaction: discord.Interaction,
        name: Optional[str] = None,
        force: Optional[bool] = None,
        scope: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if force is True:
            parts.append("--force")
        if name is not None:
            parts.append(shlex.quote(name))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "model",
            args=" ".join(parts),
            require_admin=False,
        )

    @cmd_model.autocomplete("name")
    async def _ac_model_name(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["models"](
            self.bot, interaction, current
        )

    @app_commands.command(
        name="models",
        description="List available provider models",
    )
    async def cmd_models(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "models",
            require_admin=False,
        )

    @provider_group.command(
        name="list",
        description="List LLM providers grouped by support tier",
    )
    async def cmd_provider_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "provider list",
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
        name="set",
        description="Apply provider credentials to backend settings",
    )
    @app_commands.describe(
        provider="Provider whose credentials to write",
        values="Credential fields, e.g. api_key=<key>",
    )
    async def cmd_provider_set(
        self,
        interaction: discord.Interaction,
        provider: str,
        values: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(provider))
        parts.append(values)
        await self.bot._send_backend_command(
            interaction,
            "provider set",
            args=" ".join(parts),
            require_admin=True,
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
        mode="Thread override, `inherit`, or the global scope",
        value="Value for the global scope",
    )
    async def cmd_sequential_tools(
        self,
        interaction: discord.Interaction,
        mode: Optional[str] = None,
        value: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if value is not None and mode is None:
            await self.bot._send_interaction_text(
                interaction,
                "Error: `value` also needs `mode`. Give both, or neither.",
            )
            return
        parts: list[str] = []
        if mode is not None:
            parts.append(shlex.quote(mode))
        if value is not None:
            parts.append(shlex.quote(value))
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
            require_admin=False,
        )

    @settings_group.command(
        name="show",
        description="Show server settings",
    )
    async def cmd_settings_show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "settings show",
            require_admin=False,
        )

    @skills_group.command(
        name="disable",
        description="Disable a skill on this thread (default) or globally",
    )
    @app_commands.describe(
        name="Installed skill name",
        global_="Apply to every thread instead of only this one",
    )
    @app_commands.rename(
        global_="global",
    )
    async def cmd_skills_disable(
        self,
        interaction: discord.Interaction,
        name: str,
        global_: Optional[bool] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if global_ is True:
            parts.append("--global")
        parts.append(shlex.quote(name))
        await self.bot._send_backend_command(
            interaction,
            "skills disable",
            args=" ".join(parts),
            require_admin=False,
        )

    @skills_group.command(
        name="enable",
        description="Enable a skill on this thread (default) or globally",
    )
    @app_commands.describe(
        name="Installed skill name",
        global_="Apply to every thread instead of only this one",
    )
    @app_commands.rename(
        global_="global",
    )
    async def cmd_skills_enable(
        self,
        interaction: discord.Interaction,
        name: str,
        global_: Optional[bool] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if global_ is True:
            parts.append("--global")
        parts.append(shlex.quote(name))
        await self.bot._send_backend_command(
            interaction,
            "skills enable",
            args=" ".join(parts),
            require_admin=False,
        )

    @skills_group.command(
        name="inspect",
        description="Show full skill details (metadata, scope, tools, references)",
    )
    @app_commands.describe(
        name="Installed skill name",
    )
    async def cmd_skills_inspect(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        parts.append(shlex.quote(name))
        await self.bot._send_backend_command(
            interaction,
            "skills inspect",
            args=" ".join(parts),
            require_admin=False,
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

    @skills_off_group.command(
        name="all",
        description="Deactivate every visible skill active on this thread",
    )
    async def cmd_skills_off_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "skills off all",
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
            parts.append(query)
        await self.bot._send_backend_command(
            interaction,
            "skills search",
            args=" ".join(parts),
            require_admin=False,
        )

    @skills_group.command(
        name="show",
        description="Show a skill's markdown body without activating it",
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

    @app_commands.command(
        name="tasks",
        description="Scheduled tasks overview",
    )
    @app_commands.describe(
        filter="Status filter (default: active)",
    )
    async def cmd_tasks(
        self,
        interaction: discord.Interaction,
        filter: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        parts: list[str] = []
        if filter is not None:
            parts.append(shlex.quote(filter))
        await self.bot._send_backend_command(
            interaction,
            "tasks",
            args=" ".join(parts),
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
    "account current",
    "account platforms",
    "account tokens issue",
    "account tokens revoke",
    "activity list",
    "activity notifications",
    "artifacts recent",
    "background clear",
    "background set",
    "background set-url",
    "branch",
    "config get",
    "config set",
    "config show",
    "context",
    "doctor auth",
    "doctor model",
    "env get",
    "env set",
    "env show",
    "fast set",
    "mcp discover",
    "mcp list",
    "mcp logs",
    "mcp remove",
    "mcp retry",
    "mcp status",
    "mcp test",
    "memory forget",
    "memory limit",
    "memory list",
    "memory save",
    "memory search",
    "model",
    "models",
    "provider list",
    "provider reasoning-passback",
    "provider set",
    "provider switch",
    "provider test",
    "prune",
    "sequential-tools",
    "settings get",
    "settings set",
    "settings show",
    "skills disable",
    "skills enable",
    "skills inspect",
    "skills install",
    "skills list",
    "skills off all",
    "skills search",
    "skills show",
    "smart set",
    "status",
    "tasks",
    "team list",
    "team show",
    "think",
    "triggers delete",
    "triggers disable",
    "triggers enable",
    "triggers history",
    "triggers list",
    "usage session",
)

# Qualified Discord command name -> the registry category it
# belongs to, so /help groups these commands by subject rather
# than by the single cog class that happens to host them.
COMMAND_CATEGORIES: dict[str, str] = {
    "account current": "Personal",
    "account platforms": "Personal",
    "account tokens issue": "Personal",
    "account tokens revoke": "Personal",
    "activity list": "Personal",
    "activity notifications": "Personal",
    "artifacts recent": "Personal",
    "background clear": "LLM",
    "background set": "LLM",
    "background set-url": "LLM",
    "branch": "Thread",
    "config get": "Settings",
    "config set": "Settings",
    "config show": "Settings",
    "context": "Status",
    "doctor auth": "System",
    "doctor model": "System",
    "env get": "Settings",
    "env set": "Settings",
    "env show": "Settings",
    "fast set": "LLM",
    "mcp discover": "MCP",
    "mcp list": "MCP",
    "mcp logs": "MCP",
    "mcp remove": "MCP",
    "mcp retry": "MCP",
    "mcp status": "MCP",
    "mcp test": "MCP",
    "memory forget": "Memory",
    "memory limit": "Memory",
    "memory list": "Memory",
    "memory save": "Memory",
    "memory search": "Memory",
    "model": "LLM",
    "models": "LLM",
    "provider list": "LLM",
    "provider reasoning-passback": "LLM",
    "provider set": "LLM",
    "provider switch": "LLM",
    "provider test": "LLM",
    "prune": "Thread",
    "sequential-tools": "Tools",
    "settings get": "Settings",
    "settings set": "Settings",
    "settings show": "Settings",
    "skills disable": "Skills",
    "skills enable": "Skills",
    "skills inspect": "Skills",
    "skills install": "Skills",
    "skills list": "Skills",
    "skills off all": "Skills",
    "skills search": "Skills",
    "skills show": "Skills",
    "smart set": "LLM",
    "status": "Status",
    "tasks": "TODOs",
    "team list": "Thread",
    "team show": "Thread",
    "think": "LLM",
    "triggers delete": "Automation",
    "triggers disable": "Automation",
    "triggers enable": "Automation",
    "triggers history": "Automation",
    "triggers list": "Automation",
    "usage session": "Status",
}
