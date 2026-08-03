#!/usr/bin/env python3
"""Generate the Discord slash-command cog from the backend command registry.

Regenerate from `Nymeria/` with:

    python3 scripts/generate_discord_cogs.py

The output is `nymeria/triggers/discord_cogs/generated_cogs.py`, a checked-in
source file of conventional discord.py code. Everything a Discord slash command
needs (typed signature, describe copy, static choices, admin flag, and the
flatten-back into the canonical `/command args` string) is derived from ONE
declaration: the `params=(CommandParam(...),)` tuple on the command's
`service.register(...)` call. That is the point of the pass: the four things
that used to be hand-copied into a cog cannot drift from the registry any more,
because nobody writes them twice.

`build_source()` renders the module in memory so the freshness test can diff it
against the file on disk; `main()` writes it. Running the generator twice on an
unchanged registry produces identical bytes.

Selection rule (`select_commands`): every registered command with a declared
param schema, `execution_kind == "command"`, discord in `surfaces`, and discord
not in `blocked_surfaces`, MINUS three exclusions:

1. `HAND_WRITTEN_FAMILIES`: a family whose Discord group must stay hand-written
   because at least one of its members cannot be generated. Discord gives a
   command name to exactly one owner, so a split family is not expressible: the
   whole family stays with the hand cog.
2. `EXCLUDED_COMMANDS`: individual commands whose hand cog does something a
   relay cannot do.
3. Family ROOTS that are a strict prefix of another selected path. Discord
   cannot offer `/x` and `/x sub` at the same time: the root becomes a group
   and its own action is not reachable. This one is structural, so it is
   computed rather than listed.
"""

from __future__ import annotations

import keyword
import sys
from pathlib import Path

NYMERIA_ROOT = Path(__file__).resolve().parent.parent
if str(NYMERIA_ROOT) not in sys.path:
    sys.path.insert(0, str(NYMERIA_ROOT))

from nymeria.core.command_params import SCOPE_CHOICES, CommandParam  # noqa: E402
from nymeria.core.command_service import (  # noqa: E402
    CommandDefinition,
    CommandService,
)

OUTPUT_PATH = NYMERIA_ROOT / "nymeria" / "triggers" / "discord_cogs" / "generated_cogs.py"

# Discord platform caps. Every one of these is a hard API limit: crossing one
# fails the command sync at runtime, in a guild, with an opaque error, so the
# generator refuses to emit instead.
MAX_CHOICES_PER_OPTION = 25
MAX_OPTIONS_PER_COMMAND = 25
MAX_SUBCOMMANDS_PER_GROUP = 25
MAX_GLOBAL_COMMANDS = 100
MAX_NESTING_DEPTH = 3
MAX_NAME_LENGTH = 32
MAX_DESCRIPTION_LENGTH = 100

# Top-level slots left for the hand-written cogs (chat, config's /restart,
# info, tools, todos, notepad, hook, fallback). Subtracted from the global cap
# so the generated tree cannot quietly eat the budget the hand cogs need.
RESERVED_TOP_LEVEL_SLOTS = 25

# Families whose Discord group stays hand-written. A Discord name has exactly
# one owner, so a family that cannot be generated WHOLE cannot be generated at
# all: the generated members would collide with the hand cog's group.
HAND_WRITTEN_FAMILIES: dict[str, str] = {
    # info.py `/thread` relays the registry's `thread` ROOT (active-thread
    # context usage). Generating the subcommands would turn `thread` into a
    # group and take that command away, and thread switching/creation is a
    # CLI/desktop concept: a Discord thread id is derived from the channel.
    "thread": "info.py owns /thread; a group would remove the root command",
    # todos.py `/todos add` maps four Discord fields onto the backend's pipe
    # syntax and the registry leaves it unadopted (no params).
    "todos": "todos.py owns /todos add (pipe syntax, unadopted in the registry)",
    # memory.py `/notepad write` maps a mode choice onto the backend's
    # `replace:` value prefix; the registry leaves it unadopted.
    "notepad": "memory.py owns /notepad write (replace: prefix, unadopted)",
    # tools.py `/tools search` is Discord-local: it calls the tool-search API
    # directly and renders a ranked embed. It is not a registry command.
    "tools": "tools.py owns /tools search (Discord-local ranked embed)",
    # hooks.py owns the whole /hook group; the registry's hook subcommands are
    # not discord-surfaced, so generating would leave only the family root.
    "hook": "hooks.py owns /hook (registry hook subcommands are not discord-surfaced)",
    # fallback.py owns /fallback status|revert|approvals|approve|deny, which
    # the model-swap consent copy advertises as the buttonless resolve path.
    # None of them are discord-surfaced in the registry.
    "fallback": "fallback.py owns the consent-prompt resolve path (not discord-surfaced)",
    # config.py's /restart carries the bot self-restart branch, and the
    # registry family is "restart api" (id restart.api): generating it would
    # emit a `restart` group colliding with the hand command at cog load.
    "restart": "config.py restarts the bot process itself (self-restart branch)",
}

# Individual commands the generator must never claim, even if they later gain a
# param schema. Each names the hand behavior a defer-and-relay cannot reproduce,
# or the reason a relay must not exist at all.
EXCLUDED_COMMANDS: dict[str, str] = {
    "clear": "chat.py: act-now command, deliberately never strict-parsed",
    "stop": "chat.py: act-now command, deliberately never strict-parsed",
    "compact": "chat.py streams the turn (execution_kind chat_stream)",
    # Typed key=value pairs on /provider set carry credentials; a first-class
    # Discord command would invite pasting secrets into a chat platform's
    # transport, the exact threat blocked_surfaces exists for. The backend
    # enforcement sweep beyond the two chain flows is backlog #130; until it
    # lands, the generator simply never offers the command on Discord.
    "provider.set": "credential key=value pairs must not ride Discord (#130)",
}

# Descriptions for group roots the registry does not describe. The first three
# are the copy Discord users already see on these groups today.
GROUP_DESCRIPTIONS: dict[tuple[str, ...], str] = {
    ("config",): "View and update Nymeria settings",
    ("env",): "View and set environment variables",
    ("memory",): "Manage Nymeria's memories about you",
    ("skills", "off"): "Turn skills off",
}

# Family roots the group rule drops from Discord (a group is not invokable,
# so each of these loses its OWN bare action there; children still generate).
# select_commands() asserts the computed set equals this list, so growth is a
# deliberate edit here, never a silent registry side effect.
EXPECTED_DROPPED_ROOTS: tuple[str, ...] = (
    "account",
    "account.tokens",
    "activity",
    "artifacts",
    "background",
    "doctor",
    "fast",
    "mcp",
    "provider",
    "settings",
    "skills",
    "smart",
    "team",
    "triggers",
    "usage",
)

# `choices_ref` names with a live resolver in the backend's shared option
# registry (`core/command_option_resolvers.py`). Discord's
# AUTOCOMPLETE_RESOLVERS derives from the SAME table, so this is one source
# of truth: a new backend resolver lights up autocomplete here on the next
# regen, and a ref without one gets no callback at all rather than one that
# always answers empty and costs a round trip per keystroke.
# `tests/test_discord_generated_cogs.py` pins this against the real registry.
from nymeria.core.command_option_resolvers import OPTION_RESOLVERS  # noqa: E402

RESOLVED_CHOICES_REFS: tuple[str, ...] = tuple(sorted(OPTION_RESOLVERS))

HEADER = '''\
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
'''


class GeneratorError(RuntimeError):
    """A registry state the Discord surface cannot express."""


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_commands(service: CommandService | None = None) -> list[CommandDefinition]:
    """The registry commands this generator owns, sorted by command id."""
    # The drop-list pin below guards the REAL catalog only; tests inject
    # stub registries to exercise the selection rules in isolation.
    is_default_registry = service is None
    service = service or CommandService()
    candidates: dict[tuple[str, ...], CommandDefinition] = {}
    for definition in service._commands.values():
        if definition.params is None:
            continue
        if definition.execution_kind != "command":
            continue
        if "discord" not in definition.surfaces:
            continue
        if "discord" in definition.blocked_surfaces:
            continue
        if definition.path[0] in HAND_WRITTEN_FAMILIES:
            continue
        if definition.id in EXCLUDED_COMMANDS:
            continue
        candidates[definition.path] = definition

    paths = set(candidates)
    # A path that another selected path extends must become a Discord group,
    # and a group is not invokable, so the root's own action is dropped.
    roots = {
        path
        for path in paths
        for other in paths
        if len(other) > len(path) and other[: len(path)] == path
    }
    # The drop is silent by construction, and a dropped root's own action
    # (bare /fast, bare /usage) simply does not exist on Discord. Pin the
    # list so a registry change cannot silently grow it, and so the doc
    # claims in public/chat-apps/discord-bot.md stay honest.
    dropped_ids = sorted(candidates[path].id for path in roots)
    if is_default_registry and set(dropped_ids) != set(EXPECTED_DROPPED_ROOTS):
        raise SystemExit(
            "Discord root-drop list changed (a group is not invokable, so each "
            f"dropped root's own action vanishes from Discord). Actual: "
            f"{dropped_ids}. Update EXPECTED_DROPPED_ROOTS deliberately and "
            "re-check docs/chat-apps/discord-bot.md still tells the truth."
        )
    return sorted(
        (d for path, d in candidates.items() if path not in roots),
        key=lambda d: d.id,
    )


def group_paths(commands_: list[CommandDefinition]) -> list[tuple[str, ...]]:
    """Every group path the selected commands need, parents before children."""
    groups: set[tuple[str, ...]] = set()
    for definition in commands_:
        for depth in range(1, len(definition.path)):
            groups.add(definition.path[:depth])
    return sorted(groups, key=lambda path: (len(path), path))


def group_description(service: CommandService, path: tuple[str, ...]) -> str:
    override = GROUP_DESCRIPTIONS.get(path)
    if override:
        return override
    definition = service._commands.get(".".join(path))
    if definition is not None and definition.description:
        return definition.description
    raise GeneratorError(
        f"Group /{display_path(path)} has no description: the registry does "
        "not register that path and the generator has no GROUP_DESCRIPTIONS "
        "entry for it."
    )


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def display_path(path: tuple[str, ...]) -> str:
    """The path as users type it: registry underscores render as hyphens."""
    return " ".join(token.replace("_", "-") for token in path)


def discord_name(token: str) -> str:
    return token.replace("_", "-")


def group_attr(path: tuple[str, ...]) -> str:
    return "_".join(path) + "_group"

def method_name(definition: CommandDefinition) -> str:
    return "cmd_" + definition.id.replace(".", "_")


def autocomplete_method_name(definition: CommandDefinition, param: CommandParam) -> str:
    return f"_ac_{definition.id.replace('.', '_')}_{param.name}"


def python_name(param: CommandParam) -> str:
    """A valid Python identifier for a param name; keywords get a trailing _."""
    return f"{param.name}_" if keyword.iskeyword(param.name) else param.name


def annotation(param: CommandParam) -> str:
    if param.kind == "flag":
        return "Optional[bool]"
    base = "int" if param.type == "int" else "str"
    return base if param.required else f"Optional[{base}]"


def signature_params(definition: CommandDefinition) -> list[CommandParam]:
    """Declared params reordered so required ones come first.

    Python (and Discord) both forbid a required argument after an optional
    one; the declaration order is free of that constraint and is preserved for
    the flattening, which is what the backend parses.
    """
    params = definition.params or ()
    return [p for p in params if p.required] + [p for p in params if not p.required]


def choices_for(param: CommandParam) -> tuple[str, ...]:
    if param.kind == "scope":
        return SCOPE_CHOICES
    return param.choices


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def enforce_limits(
    service: CommandService,
    commands_: list[CommandDefinition],
    groups: list[tuple[str, ...]],
) -> None:
    """Refuse to emit a tree Discord would reject at sync time."""
    top_level = {d.path[0] for d in commands_}
    budget = MAX_GLOBAL_COMMANDS - RESERVED_TOP_LEVEL_SLOTS
    if len(top_level) > budget:
        raise GeneratorError(
            f"{len(top_level)} generated top-level commands exceeds the "
            f"{budget} available of Discord's {MAX_GLOBAL_COMMANDS}-command "
            f"global cap ({RESERVED_TOP_LEVEL_SLOTS} reserved for hand cogs)."
        )

    children: dict[tuple[str, ...], int] = {path: 0 for path in groups}
    for definition in commands_:
        if len(definition.path) > MAX_NESTING_DEPTH:
            raise GeneratorError(
                f"/{display_path(definition.path)} nests "
                f"{len(definition.path)} levels deep; Discord allows "
                f"{MAX_NESTING_DEPTH}."
            )
        if len(definition.path) > 1:
            children[definition.path[:-1]] += 1
        _check_name(definition.path[-1], f"/{display_path(definition.path)}")
        _check_description(
            definition.description, f"/{display_path(definition.path)}"
        )
        params = definition.params or ()
        if len(params) > MAX_OPTIONS_PER_COMMAND:
            raise GeneratorError(
                f"/{display_path(definition.path)} declares {len(params)} "
                f"arguments; Discord allows {MAX_OPTIONS_PER_COMMAND}."
            )
        for param in params:
            label = f"/{display_path(definition.path)} argument {param.name}"
            _check_name(param.name, label)
            _check_description(param.description, label, allow_empty=True)
            if len(choices_for(param)) > MAX_CHOICES_PER_OPTION:
                raise GeneratorError(
                    f"{label} declares {len(choices_for(param))} choices; "
                    f"Discord allows {MAX_CHOICES_PER_OPTION}."
                )

    for path in groups:
        if path[:-1] in children:
            children[path[:-1]] += 1
        _check_name(path[-1], f"group /{display_path(path)}")
        _check_description(group_description(service, path), f"group /{display_path(path)}")
    for path, count in sorted(children.items()):
        if count > MAX_SUBCOMMANDS_PER_GROUP:
            raise GeneratorError(
                f"Group /{display_path(path)} has {count} children; Discord "
                f"allows {MAX_SUBCOMMANDS_PER_GROUP}."
            )


def _check_name(token: str, label: str) -> None:
    name = discord_name(token)
    if not name or len(name) > MAX_NAME_LENGTH:
        raise GeneratorError(
            f"{label}: Discord names must be 1 to {MAX_NAME_LENGTH} "
            f"characters, got {len(name)}."
        )
    if name != name.lower():
        raise GeneratorError(f"{label}: Discord names must be lowercase.")


def _check_description(text: str, label: str, *, allow_empty: bool = False) -> None:
    if not text and not allow_empty:
        raise GeneratorError(f"{label}: Discord requires a description.")
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise GeneratorError(
            f"{label}: description is {len(text)} characters; Discord allows "
            f"{MAX_DESCRIPTION_LENGTH}."
        )


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _lit(value: str) -> str:
    """A Python string literal, double-quoted like the rest of the codebase."""
    if '"' not in value and "\\" not in value:
        escaped = value.replace("\n", "\\n")
        return f'"{escaped}"'
    return repr(value)


def _render_groups(service: CommandService, groups: list[tuple[str, ...]]) -> list[str]:
    lines: list[str] = []
    for path in groups:
        parent = f", parent={group_attr(path[:-1])}" if len(path) > 1 else ""
        lines.append("")
        lines.append(f"    {group_attr(path)} = app_commands.Group(")
        lines.append(f"        name={_lit(discord_name(path[-1]))},")
        lines.append(f"        description={_lit(group_description(service, path))},")
        if parent:
            lines.append(f"        parent={group_attr(path[:-1])},")
        lines.append("    )")
    return lines


def _render_decorators(definition: CommandDefinition) -> list[str]:
    path = definition.path
    if len(path) > 1:
        opener = f"    @{group_attr(path[:-1])}.command("
    else:
        opener = "    @app_commands.command("
    lines = [
        opener,
        f"        name={_lit(discord_name(path[-1]))},",
        f"        description={_lit(definition.description)},",
        "    )",
    ]

    params = signature_params(definition)
    described = [p for p in params if p.description]
    if described:
        lines.append("    @app_commands.describe(")
        for param in described:
            lines.append(f"        {python_name(param)}={_lit(param.description)},")
        lines.append("    )")

    renamed = [p for p in params if python_name(p) != p.name]
    if renamed:
        lines.append("    @app_commands.rename(")
        for param in renamed:
            lines.append(f"        {python_name(param)}={_lit(param.name)},")
        lines.append("    )")

    with_choices = [p for p in params if choices_for(p)]
    if with_choices:
        lines.append("    @app_commands.choices(")
        for param in with_choices:
            lines.append(f"        {python_name(param)}=[")
            for choice in choices_for(param):
                lines.append(
                    f"            app_commands.Choice(name={_lit(choice)}, "
                    f"value={_lit(choice)}),"
                )
            lines.append("        ],")
        lines.append("    )")
    return lines


def _render_signature(definition: CommandDefinition) -> list[str]:
    params = signature_params(definition)
    name = method_name(definition)
    if not params:
        return [f"    async def {name}(self, interaction: discord.Interaction) -> None:"]
    lines = [f"    async def {name}(", "        self,", "        interaction: discord.Interaction,"]
    for param in params:
        default = "" if param.required else " = None"
        lines.append(f"        {python_name(param)}: {annotation(param)}{default},")
    lines.append("    ) -> None:")
    return lines


def _render_gap_guards(definition: CommandDefinition) -> list[str]:
    """Reject a positional gap Discord's independent options can produce.

    Discord options are named and independent; the backend's positionals are
    ordered. When an OPTIONAL positional precedes another value-bearing one,
    filling only the later field would flatten to a string that binds the value
    to the earlier param. Say so instead of relaying a lie.
    """
    values = [p for p in (definition.params or ()) if p.kind in ("positional", "rest")]
    lines: list[str] = []
    for index, later in enumerate(values):
        for earlier in values[:index]:
            if earlier.required:
                continue
            lines.append(
                f"        if {python_name(later)} is not None "
                f"and {python_name(earlier)} is None:"
            )
            lines.append("            await self.bot._send_interaction_text(")
            lines.append("                interaction,")
            lines.append(
                "                "
                + _lit(
                    f"Error: `{later.name}` also needs `{earlier.name}`. "
                    f"Give both, or neither."
                )
                + ","
            )
            lines.append("            )")
            lines.append("            return")
    return lines


def _render_flatten(definition: CommandDefinition) -> list[str]:
    """Rebuild the canonical command line from the declared params.

    Options and flags lead, then positionals and the rest tail in declared
    order, then the trailing scope token: exactly the grammar `bind_args`
    parses back. Values are `shlex.quote`d because the dispatcher shlex-splits
    the line, so a multi-word value must survive as one token. A REPEATABLE
    positional is the exception: it is meant to arrive as several tokens, so
    its space-separated text goes through verbatim.
    """
    params = definition.params or ()
    ordered = (
        [p for p in params if p.kind in ("option", "flag")]
        + [p for p in params if p.kind in ("positional", "rest")]
        + [p for p in params if p.kind == "scope"]
    )
    lines: list[str] = []
    for param in ordered:
        name = python_name(param)
        indent = "        "
        if param.kind == "flag":
            lines.append(f"{indent}if {name} is True:")
            lines.append(f'{indent}    parts.append("{param.option_spelling}")')
            continue
        if not param.required:
            lines.append(f"{indent}if {name} is not None:")
            indent += "    "
        if param.repeatable:
            value = name
        elif param.type == "int":
            value = f"shlex.quote(str({name}))"
        else:
            value = f"shlex.quote({name})"
        if param.kind == "option":
            lines.append(f'{indent}parts.extend(("{param.option_spelling}", {value}))')
        else:
            lines.append(f"{indent}parts.append({value})")
    return lines


def _render_body(definition: CommandDefinition) -> list[str]:
    lines = ["        await interaction.response.defer(ephemeral=True)"]
    lines += _render_gap_guards(definition)
    flatten = _render_flatten(definition)
    if flatten:
        lines.append("        parts: list[str] = []")
        lines += flatten
    lines.append("        await self.bot._send_backend_command(")
    lines.append("            interaction,")
    lines.append(f"            {_lit(display_path(definition.path))},")
    if flatten:
        lines.append('            args=" ".join(parts),')
    lines.append(f"            require_admin={definition.requires_admin},")
    lines.append("        )")
    return lines


def _render_autocompletes(definition: CommandDefinition) -> list[str]:
    lines: list[str] = []
    for param in definition.params or ():
        ref = param.choices_ref
        if not ref or ref not in RESOLVED_CHOICES_REFS:
            continue
        if choices_for(param):
            # Discord rejects autocomplete on an option with static choices.
            continue
        lines.append("")
        lines.append(
            f"    @{method_name(definition)}.autocomplete({_lit(param.name)})"
        )
        lines.append(f"    async def {autocomplete_method_name(definition, param)}(")
        lines.append("        self,")
        lines.append("        interaction: discord.Interaction,")
        lines.append("        current: str,")
        lines.append("    ) -> list[app_commands.Choice[str]]:")
        lines.append(
            f"        return await AUTOCOMPLETE_RESOLVERS[{_lit(ref)}]("
        )
        lines.append("            self.bot, interaction, current")
        lines.append("        )")
    return lines


def build_source(service: CommandService | None = None) -> str:
    """Render the generated cog module as source text."""
    service = service or CommandService()
    selected = select_commands(service)
    groups = group_paths(selected)
    enforce_limits(service, selected, groups)

    lines = HEADER.splitlines()
    lines += _render_groups(service, groups)
    for definition in selected:
        lines.append("")
        lines += _render_decorators(definition)
        lines += _render_signature(definition)
        lines += _render_body(definition)
        lines += _render_autocompletes(definition)

    lines.append("")
    lines.append("")
    lines.append("GENERATED_COMMAND_NAMES: tuple[str, ...] = (")
    for definition in selected:
        lines.append(f"    {_lit(display_path(definition.path))},")
    lines.append(")")
    lines.append("")
    lines.append("# Qualified Discord command name -> the registry category it")
    lines.append("# belongs to, so /help groups these commands by subject rather")
    lines.append("# than by the single cog class that happens to host them.")
    lines.append("COMMAND_CATEGORIES: dict[str, str] = {")
    for definition in selected:
        lines.append(
            f"    {_lit(display_path(definition.path))}: "
            f"{_lit(definition.category)},"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        source = build_source()
    except GeneratorError as error:
        print(f"generate_discord_cogs: {error}", file=sys.stderr)
        return 1
    OUTPUT_PATH.write_text(source, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(NYMERIA_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
