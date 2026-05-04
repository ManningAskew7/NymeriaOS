"""Configuration commands: /config, /env, /model, /models, /think, /restart."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from ..bot_helpers import coerce_value, fmt_tokens
from ..discord_bot import make_thread_id

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
        user_id = await self.bot._resolve_or_reject_interaction(interaction)
        if user_id is None:
            return
        try:
            if name is None:
                settings = await self.bot.api.get_settings()
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
                tc = await self.bot.api.get_thread_config(
                    thread_id, user_id=user_id
                )
                llm_cfg = (tc or {}).get("llm_config") or {}
                thread_model = llm_cfg.get("model")

                lines = [
                    f"**Global:** `{settings.get('llm_model', '?')}` ({settings.get('llm_provider', '?')})"
                ]
                if thread_model:
                    lines.append(
                        f"**This channel:** `{thread_model}` (override)"
                    )
                else:
                    lines.append("**This channel:** using global default")
                await interaction.followup.send(
                    "\n".join(lines), ephemeral=True
                )
            else:
                target = scope.value if scope else "global"
                if target == "thread":
                    thread_id = make_thread_id(
                        interaction.guild_id, interaction.channel_id
                    )
                    await self.bot.api.update_thread_config(
                        thread_id, user_id=user_id, llm_config={"model": name}
                    )
                    await interaction.followup.send(
                        f"Model for this channel set to `{name}`.",
                        ephemeral=True,
                    )
                else:
                    if (
                        await self.bot._resolve_or_reject_interaction(
                            interaction, require_admin=True
                        )
                        is None
                    ):
                        return
                    await self.bot.api.update_settings(llm_model=name)
                    await interaction.followup.send(
                        f"Global model set to `{name}`.", ephemeral=True
                    )
        except httpx.HTTPStatusError as e:
            detail = (
                e.response.json().get("detail", str(e)) if e.response else str(e)
            )
            await interaction.followup.send(f"Error: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # --- /models ---

    @app_commands.command(name="models", description="List available models")
    async def cmd_models(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            models = await self.bot.api.list_available_models()
            settings = await self.bot.api.get_settings()
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
                embed.set_footer(
                    text=f"Showing 25 of {len(models)} models"
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error listing models: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            if mode is None:
                settings = await self.bot.api.get_settings()
                thinking = settings.get("llm_extended_thinking", False)
                effort = settings.get("llm_reasoning_effort")
                logger.info(
                    f"[/think] thinking={thinking!r} effort={effort!r}"
                )
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
                await self.bot.api.update_settings(
                    llm_extended_thinking=False, llm_reasoning_effort=None
                )
                await interaction.followup.send(
                    "Thinking **disabled**.", ephemeral=True
                )
            elif value == "on":
                await self.bot.api.update_settings(
                    llm_extended_thinking=True
                )
                await interaction.followup.send(
                    "Thinking **enabled**.", ephemeral=True
                )
            else:
                await self.bot.api.update_settings(
                    llm_extended_thinking=True, llm_reasoning_effort=value
                )
                await interaction.followup.send(
                    f"Thinking **enabled**, effort: **{value}**.",
                    ephemeral=True,
                )
        except Exception as e:
            logger.error(f"Error setting thinking: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # --- /config group ---

    config_group = app_commands.Group(
        name="config", description="View and update Nymeria settings"
    )

    @config_group.command(
        name="show", description="Show all current settings"
    )
    async def cmd_config_show(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            settings = await self.bot.api.get_settings()

            embed = discord.Embed(title="Settings", color=discord.Color.greyple())

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
            embed.add_field(
                name="LLM", value="\n".join(llm_lines), inline=False
            )

            ctx_lines = [
                f"mode: {settings.get('context_management', '?')}",
                f"compact threshold: {int((settings.get('compact_threshold', 0) or 0) * 100)}%",
                f"keep messages: {settings.get('compact_keep_messages', '?')}",
            ]
            compact_model = settings.get("compact_model")
            if compact_model:
                ctx_lines.append(f"compact model: `{compact_model}`")
            embed.add_field(
                name="Context", value="\n".join(ctx_lines), inline=True
            )

            sys_lines = [
                f"log level: {settings.get('log_level', '?')}",
                f"watchdog: {'on' if settings.get('watchdog_enabled') else 'off'}",
            ]
            if settings.get("watchdog_enabled"):
                sys_lines.append(
                    f"watchdog interval: {settings.get('watchdog_interval_minutes', '?')}m"
                )
            embed.add_field(
                name="System", value="\n".join(sys_lines), inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error showing config: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            settings = await self.bot.api.get_settings()
            if key in settings:
                value = settings[key]
                await interaction.followup.send(
                    f"**{key}** = `{value}`", ephemeral=True
                )
            else:
                available = ", ".join(
                    f"`{k}`" for k in sorted(settings.keys())[:30]
                )
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
    async def cmd_config_set(
        self, interaction: discord.Interaction, key: str, value: str
    ):
        await interaction.response.defer(ephemeral=True)
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            parsed = coerce_value(value)
            result = await self.bot.api.update_settings(**{key: parsed})
            msg = f"**{key}** set to `{parsed}`."
            if result.get("restart_required"):
                msg += "\nThis change requires `/restart api` to take effect."
            await interaction.followup.send(msg, ephemeral=True)
        except httpx.HTTPStatusError as e:
            detail = (
                e.response.json().get("detail", str(e)) if e.response else str(e)
            )
            await interaction.followup.send(f"Error: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            from typing import Dict

            data = await self.bot.api.get_env_vars()
            entries = data.get("entries", [])

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
                            lines.append(f"✅ `{e['name']}` = `{val}`")
                    else:
                        lines.append(f"❌ `{e['name']}`")
                text = "\n".join(lines)
                if len(text) > 1024:
                    text = text[:1020] + "..."
                embed.add_field(name=cat, value=text, inline=False)

            embed.set_footer(text="Use /env get <key> for unmasked values")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error showing env vars: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @env_group.command(
        name="get",
        description="Get an environment variable (unmasked)",
    )
    @app_commands.describe(key="Variable name (e.g., perplexity_api_key)")
    async def cmd_env_get(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            data = await self.bot.api.get_env_var(key)
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
                await interaction.followup.send(
                    f"Error: {e}", ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
        if (
            await self.bot._resolve_or_reject_interaction(
                interaction, require_admin=True
            )
            is None
        ):
            return
        try:
            parsed = coerce_value(value)
            result = await self.bot.api.update_settings(**{key: parsed})
            msg = f"**{key}** set to `{parsed}`."
            if result.get("restart_required"):
                msg += "\nThis change requires `/restart api` to take effect."
            await interaction.followup.send(msg, ephemeral=True)
        except httpx.HTTPStatusError as e:
            detail = (
                e.response.json().get("detail", str(e)) if e.response else str(e)
            )
            await interaction.followup.send(f"Error: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

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
