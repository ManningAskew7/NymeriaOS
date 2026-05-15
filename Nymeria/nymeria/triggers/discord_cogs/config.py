"""Configuration commands: /config, /env, /model, /models, /think, /restart."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


class ConfigCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

    # --- /model ---

    @app_commands.command(name="model", description="Show or change the LLM model")
    @app_commands.describe(
        name="Model to switch to (omit to show current)",
        scope="'global' changes default, 'thread' changes this channel only",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="thread", value="thread"),
        ]
    )
    async def cmd_model(
        self,
        interaction: discord.Interaction,
        name: Optional[str] = None,
        scope: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        args = ""
        require_admin = False
        if name is not None:
            target = scope.value if scope else "global"
            args = f"{name} {target}"
            require_admin = target == "global"
        await self.bot._send_backend_command(
            interaction,
            "model",
            args=args,
            require_admin=require_admin,
        )

    # --- /models ---

    @app_commands.command(name="models", description="List available models")
    async def cmd_models(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "models")

    # --- /think ---

    @app_commands.command(
        name="think", description="Set thinking/reasoning mode"
    )
    @app_commands.describe(mode="Thinking mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="low", value="low"),
            app_commands.Choice(name="medium", value="medium"),
            app_commands.Choice(name="high", value="high"),
        ]
    )
    async def cmd_think(
        self,
        interaction: discord.Interaction,
        mode: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "think",
            args=mode.value if mode else "",
            require_admin=True,
        )

    # --- /config group ---

    config_group = app_commands.Group(
        name="config", description="View and update Nymeria settings"
    )

    @config_group.command(
        name="show", description="Show all current settings"
    )
    async def cmd_config_show(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "config show",
            require_admin=True,
        )

    @config_group.command(
        name="get", description="Get a specific setting value"
    )
    @app_commands.describe(
        key="Setting name (e.g., llm_model, context_management)"
    )
    async def cmd_config_get(
        self, interaction: discord.Interaction, key: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "config get",
            args=key,
            require_admin=True,
        )

    @config_group.command(name="set", description="Update a setting")
    @app_commands.describe(
        key="Setting name (e.g., llm_temperature, log_level)",
        value="New value",
    )
    async def cmd_config_set(
        self, interaction: discord.Interaction, key: str, value: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "config set",
            args=f"{key} {value}",
            require_admin=True,
        )

    # --- /env group ---

    env_group = app_commands.Group(
        name="env", description="View and set environment variables"
    )

    @env_group.command(
        name="show",
        description="Show all environment variables (secrets masked)",
    )
    async def cmd_env_show(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "env show",
            require_admin=True,
        )

    @env_group.command(
        name="get",
        description="Get an environment variable (unmasked)",
    )
    @app_commands.describe(key="Variable name (e.g., perplexity_api_key)")
    async def cmd_env_get(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "env get",
            args=key,
            require_admin=True,
        )

    @env_group.command(
        name="set", description="Set an environment variable"
    )
    @app_commands.describe(
        key="Variable name (e.g., perplexity_api_key)",
        value="New value",
    )
    async def cmd_env_set(
        self, interaction: discord.Interaction, key: str, value: str
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(
            interaction,
            "env set",
            args=f"{key} {value}",
            require_admin=True,
        )

    # --- /restart ---

    @app_commands.command(
        name="restart", description="Restart a Nymeria service"
    )
    @app_commands.describe(target="What to restart (default: bot)")
    @app_commands.choices(
        target=[
            app_commands.Choice(name="bot (Discord bot)", value="bot"),
            app_commands.Choice(name="api (API server)", value="api"),
        ]
    )
    async def cmd_restart(
        self,
        interaction: discord.Interaction,
        target: Optional[app_commands.Choice[str]] = None,
    ):
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return

        target_value = target.value if target else "bot"

        if target_value == "api":
            await interaction.response.send_message(
                "Restarting API server...", ephemeral=True
            )
            try:
                await self.bot.api.restart_api()
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
            ):
                pass
            except Exception as e:
                logger.error(f"Error restarting API: {e}", exc_info=True)
                await interaction.followup.send(
                    f"Error: {e}", ephemeral=True
                )
        else:
            await interaction.response.send_message(
                "Restarting bot... (back in a few seconds)", ephemeral=True
            )
            logger.info("Bot restart requested via /restart command")
            await self.bot._request_self_restart()
