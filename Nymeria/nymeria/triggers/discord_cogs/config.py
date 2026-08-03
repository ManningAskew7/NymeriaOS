"""Configuration commands that cannot be generated: /restart, /set-model,
/show-settings.

`/think`, the `/env` group, and the `/model` and `/settings` groups all moved to
`generated_cogs.py`: they were pure defer-and-relay wrappers, so the registry's
declared params now supply their signatures, choices, and admin flags.
`/restart` stays here because restarting the BOT is a local action (it closes
the Discord gateway and the API client and lets the supervisor bring the process
back), not something the backend command can do for us.

`/set-model` and `/show-settings` are here because backlog #131 turned `model`
and `settings` into Discord GROUPS (`model list`, `settings get|set`), and a
Discord group is a namespace, not a command: its own bare action stops being
invokable. That cost the surface two capabilities, the model SWITCH and the
settings readout, so both come back as hand commands relaying the canonical
backend spellings (`/model <name> [scope]` and `/settings`). They are named
verb-first and hyphenated like the other hand commands Discord owns
(`/show-tools`, `/channel-context`) because the group already holds the noun,
and because the #131 cutover retired the old names (`/model`, `/config show`)
on this surface deliberately: reusing either would collide with the generated
group or reopen a name the cutover closed. The generator needs no change for
them, its exclusion sets are about REGISTRY families it must not claim, and
neither name is a registry path.
"""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING, List, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from .autocomplete import AUTOCOMPLETE_RESOLVERS

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

logger = logging.getLogger(__name__)


class ConfigCog(commands.Cog):
    def __init__(self, bot: NymeriaDiscordBot):
        self.bot = bot

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
            # API restart is centralized; transient connection errors during
            # the server handoff are expected and suppressed.
            try:
                await self.bot._send_backend_command(
                    interaction, "restart", args="api", require_admin=True
                )
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
            ):
                pass
        else:
            await interaction.response.send_message(
                "Restarting bot... (back in a few seconds)", ephemeral=True
            )
            logger.info("Bot restart requested via /restart command")
            await self.bot._request_self_restart()

    @app_commands.command(
        name="set-model",
        description="Switch the LLM model for this channel or globally",
    )
    @app_commands.describe(
        name="Model id to switch to",
        scope="Write this channel's override (default) or the global default",
        force="Accept a model the provider does not list",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="thread", value="thread"),
            app_commands.Choice(name="global", value="global"),
        ],
    )
    async def cmd_set_model(
        self,
        interaction: discord.Interaction,
        name: str,
        scope: Optional[str] = None,
        force: Optional[bool] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # Flags lead, then positionals, then the trailing scope token: the
        # grammar `bind_args` parses back, and the order the generator emits.
        parts: list[str] = []
        if force is True:
            parts.append("--force")
        parts.append(shlex.quote(name))
        if scope is not None:
            parts.append(shlex.quote(scope))
        await self.bot._send_backend_command(
            interaction,
            "model",
            args=" ".join(parts),
        )

    @cmd_set_model.autocomplete("name")
    async def _set_model_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await AUTOCOMPLETE_RESOLVERS["models"](self.bot, interaction, current)

    @app_commands.command(
        name="show-settings",
        description="Show the server settings (LLM, context, system)",
    )
    async def cmd_show_settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot._send_backend_command(interaction, "settings")
