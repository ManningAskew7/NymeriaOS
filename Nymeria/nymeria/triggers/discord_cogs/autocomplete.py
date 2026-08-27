"""Dynamic autocomplete resolvers for Discord slash-command arguments.

A `CommandParam` can name a DYNAMIC value set with `choices_ref` ("models",
"tools", ...) when the valid values only exist at runtime. The registry never
enforces those, so they cannot become static Discord choices; on Discord they
become autocomplete instead.

Every ref is answered by the backend's shared option-resolver registry
(`core/command_option_resolvers.py`) through `GET /commands/options/{ref}`,
acting as the INVOKING user's linked account, so a Discord suggestion list
shows exactly what that user's picker or `/... list` command would (and an
unlinked Discord user gets no suggestions, not another account's data).

`AUTOCOMPLETE_RESOLVERS` derives its keys from that registry: adding a
backend resolver is all it takes to light up every argument that declares
the ref, after a cog regen (`scripts/generate_discord_cogs.py` reads the
same table).

Resolvers are best effort by design: an autocomplete request has a hard 3
second Discord deadline and is fired on every keystroke, so a failing backend
must degrade to "no suggestions", never to an error the user sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

import discord
from discord import app_commands

from ...core.command_option_resolvers import OPTION_RESOLVERS
from ..discord_bot import make_thread_id

if TYPE_CHECKING:
    from ..discord_bot import NymeriaDiscordBot

# Discord returns at most 25 autocomplete suggestions, and truncates a longer
# list silently.
MAX_CHOICES = 25
# Choice labels are capped at 100 characters; a VALUE longer than that cannot
# be sent at all, so such an entry is skipped rather than truncated into
# something that would not resolve.
MAX_LABEL = 100

AutocompleteResolver = Callable[
    ["NymeriaDiscordBot", discord.Interaction, str],
    Awaitable[list[app_commands.Choice[str]]],
]


def mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    """The mapping items of a JSON list, ignoring anything else."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def tool_name(tool: Mapping[str, Any]) -> str:
    """A tool-search row's display name (shared with the /tools search cog)."""
    return str(tool.get("name") or tool.get("id") or tool.get("tool_id") or "")


def endpoint_resolver(ref: str) -> AutocompleteResolver:
    """Build the resolver for one ref over the shared options endpoint.

    The typed text filters against the option's value, label, AND meta
    (typing "disabled" narrows a hook list to disabled hooks); the choice
    name shows the label with the meta badges, while the choice VALUE is
    always the exact token the command accepts.
    """

    async def resolve(
        bot: NymeriaDiscordBot,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            user_id = await bot.resolve_user_id(
                interaction.user.id, guild_id=interaction.guild_id
            )
            if user_id is None:
                return []
            thread_id = None
            if interaction.channel_id is not None:
                thread_id = make_thread_id(
                    interaction.guild_id, interaction.channel_id
                )
            options = await bot.api.list_command_options(
                ref,
                thread_id=thread_id,
                user_id=user_id,
                # Narrow server-side: a keystroke must never pull a whole
                # catalog through the 3s Discord deadline. The client-side
                # filter below stays as the belt (and covers older backends
                # that ignore unknown query params).
                q=current.strip() or None,
                limit=MAX_CHOICES,
            )
        except Exception:  # noqa: BLE001 - autocomplete degrades, never errors.
            return []

        needle = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for option in mapping_sequence(options):
            value = str(option.get("id") or "")
            if not value or len(value) > MAX_LABEL:
                continue
            label = str(option.get("label") or value)
            meta = str(option.get("meta") or "")
            if needle and needle not in f"{value} {label} {meta}".lower():
                continue
            name = f"{label} · {meta}" if meta else label
            choices.append(
                app_commands.Choice(name=name[:MAX_LABEL], value=value)
            )
            if len(choices) >= MAX_CHOICES:
                break
        return choices

    resolve.__name__ = f"resolve_{ref}"
    return resolve


AUTOCOMPLETE_RESOLVERS: dict[str, AutocompleteResolver] = {
    ref: endpoint_resolver(ref) for ref in OPTION_RESOLVERS
}
