"""Dynamic autocomplete resolvers for Discord slash-command arguments.

A `CommandParam` can name a DYNAMIC value set with `choices_ref` ("models",
"tools", ...) when the valid values only exist at runtime. The registry never
enforces those, so they cannot become static Discord choices; on Discord they
become autocomplete instead.

`AUTOCOMPLETE_RESOLVERS` maps a ref name to the resolver that answers it. The
generator wires a callback for every declared ref that appears here; a ref with
no resolver simply gets no autocomplete, so adding one here is all it takes to
light up every argument that declares that ref.

Resolvers are best effort by design: an autocomplete request has a hard 3
second Discord deadline and is fired on every keystroke, so a failing backend
must degrade to "no suggestions", never to an error the user sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

import discord
from discord import app_commands

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
    return str(tool.get("name") or tool.get("id") or tool.get("tool_id") or "")


async def resolve_tools(
    bot: NymeriaDiscordBot,
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Tool and category names, categories first.

    Both are offered because the commands that declare `choices_ref="tools"`
    accept either spelling ("/tools enable email" and "/tools enable
    bash_execute" both work).
    """
    if interaction.channel_id is None:
        return []
    try:
        categories_payload = await bot.api.get_tool_categories()
        categories = categories_payload.get("categories", {})
        thread_id = make_thread_id(interaction.guild_id, interaction.channel_id)
        data = await bot.api.search_tools(current, thread_id=thread_id, top_k=20)
        available = mapping_sequence(data.get("results", []))

        choices: list[app_commands.Choice[str]] = []
        current_lower = current.lower()

        for category_name, tools in sorted(categories.items()):
            if current_lower in category_name:
                label = f"{category_name} (category: {len(tools)} tools)"
                choices.append(
                    app_commands.Choice(name=label[:MAX_LABEL], value=category_name)
                )

        seen = {choice.value for choice in choices}
        for entry in available:
            name = tool_name(entry)
            if not name or name in seen:
                continue
            description = (entry.get("description") or "").split("\n")[0][:60]
            label = f"{name}: {description}" if description else name
            choices.append(app_commands.Choice(name=label[:MAX_LABEL], value=name))
            seen.add(name)

        return choices[:MAX_CHOICES]
    except Exception:  # noqa: BLE001 - autocomplete degrades, never errors.
        return []


async def resolve_models(
    bot: NymeriaDiscordBot,
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Model ids the active provider currently lists, filtered by the typed text.

    Same source as the backend's own `/model` guard
    (`GET /models/available`), so a suggestion here is a model that command
    will accept without `--force`.
    """
    del interaction
    try:
        models = await bot.api.list_available_models()
    except Exception:  # noqa: BLE001 - autocomplete degrades, never errors.
        return []

    current_lower = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for entry in mapping_sequence(models):
        model_id = str(entry.get("id") or entry.get("name") or "")
        if not model_id or len(model_id) > MAX_LABEL:
            continue
        if current_lower and current_lower not in model_id.lower():
            continue
        choices.append(app_commands.Choice(name=model_id[:MAX_LABEL], value=model_id))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


AUTOCOMPLETE_RESOLVERS: dict[str, AutocompleteResolver] = {
    "models": resolve_models,
    "tools": resolve_tools,
}
