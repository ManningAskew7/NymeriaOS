"""Structural gates for the registry-derived Discord cog (backlog #129).

`nymeria/triggers/discord_cogs/generated_cogs.py` is checked-in generated
source. These tests are what make that safe: they fail when the file stops
matching the registry it was generated from, when a selected command is missing
from the Discord command tree, and when the tree would break one of Discord's
platform caps.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from discord import AppCommandOptionType, app_commands

from cli_fixtures import run
from nymeria.core.command_service import CommandService
from nymeria.triggers.discord_cogs import ALL_COGS
from nymeria.triggers.discord_cogs.autocomplete import AUTOCOMPLETE_RESOLVERS
from nymeria.triggers.discord_cogs.generated_cogs import (
    COMMAND_CATEGORIES,
    GENERATED_COMMAND_NAMES,
    GeneratedCommandsCog,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = PROJECT_ROOT / "scripts" / "generate_discord_cogs.py"
GENERATED_PATH = (
    PROJECT_ROOT / "nymeria" / "triggers" / "discord_cogs" / "generated_cogs.py"
)


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generate_discord_cogs", GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _load_generator()


@pytest.fixture(scope="module")
def service() -> CommandService:
    return CommandService()


def _leaf_commands(cog: type) -> dict[str, app_commands.Command]:
    """Every invokable command the cog contributes, by qualified name."""
    found: dict[str, app_commands.Command] = {}
    for entry in cog.__cog_app_commands__:
        candidates = (
            entry.walk_commands() if isinstance(entry, app_commands.Group) else [entry]
        )
        for command in candidates:
            if isinstance(command, app_commands.Group):
                continue
            found[command.qualified_name] = command
    return found


def test_generated_module_is_current(generator: ModuleType) -> None:
    """The checked-in file must be byte-identical to a fresh generation."""
    assert GENERATED_PATH.read_text(encoding="utf-8") == generator.build_source(), (
        "generated_cogs.py is stale. Regenerate from Nymeria/ with: "
        "python3 scripts/generate_discord_cogs.py"
    )


def test_generation_is_deterministic(generator: ModuleType) -> None:
    assert generator.build_source() == generator.build_source()


def test_every_selected_registry_command_has_a_discord_command(
    generator: ModuleType, service: CommandService
) -> None:
    selected = {
        generator.display_path(definition.path)
        for definition in generator.select_commands(service)
    }
    assert selected, "the selection rule matched no commands at all"
    assert set(_leaf_commands(GeneratedCommandsCog)) == selected
    assert set(GENERATED_COMMAND_NAMES) == selected
    assert set(COMMAND_CATEGORIES) == selected


def test_selection_rule_matches_the_registry_predicate(
    generator: ModuleType, service: CommandService
) -> None:
    """Nothing unschema'd, non-relayable, or discord-blocked may be generated."""
    by_display = {
        generator.display_path(definition.path): definition
        for definition in service._commands.values()
    }
    for name in GENERATED_COMMAND_NAMES:
        definition = by_display[name]
        assert definition.params is not None
        assert definition.execution_kind == "command"
        assert "discord" in definition.surfaces
        assert "discord" not in definition.blocked_surfaces
        assert definition.path[0] not in generator.HAND_WRITTEN_FAMILIES
        assert definition.id not in generator.EXCLUDED_COMMANDS


def test_family_roots_that_became_groups_are_not_also_commands() -> None:
    """Discord cannot offer /x and /x sub at once, so no root may be invokable."""
    names = set(GENERATED_COMMAND_NAMES)
    for name in names:
        for other in names:
            assert not (
                other != name and other.startswith(f"{name} ")
            ), f"/{name} is both a command and the root of /{other}"


def _stub_service(definitions: list) -> object:
    class _Stub:
        _commands = {definition.id: definition for definition in definitions}

    return _Stub()


def test_selection_drops_a_root_whose_family_has_subcommands(
    generator: ModuleType, service: CommandService
) -> None:
    """The root-dropping rule itself, on a family built for the purpose."""
    import dataclasses

    base = next(
        definition
        for definition in generator.select_commands(service)
        if definition.path == ("status",)
    )
    root = dataclasses.replace(base, id="zzz", path=("zzz",))
    child = dataclasses.replace(base, id="zzz.child", path=("zzz", "child"))

    selected = generator.select_commands(_stub_service([root, child]))

    assert [definition.path for definition in selected] == [("zzz", "child")]
    # A root with no subcommands is still a command.
    lone = generator.select_commands(_stub_service([root]))
    assert [definition.path for definition in lone] == [("zzz",)]


def test_signature_hoists_required_arguments_above_optional_ones(
    generator: ModuleType, service: CommandService
) -> None:
    """Declaration order is free; a Discord signature's is not."""
    import dataclasses

    from nymeria.core.command_params import CommandParam

    base = next(
        definition
        for definition in generator.select_commands(service)
        if definition.path == ("status",)
    )
    definition = dataclasses.replace(
        base,
        params=(
            CommandParam("note", description="an optional one"),
            CommandParam("target", required=True, description="a required one"),
        ),
    )

    assert [
        param.name for param in generator.signature_params(definition)
    ] == ["target", "note"]


def test_generated_signatures_carry_the_declared_schema(
    service: CommandService,
) -> None:
    """Every declared param reaches Discord with its type, choices and copy."""
    commands = _leaf_commands(GeneratedCommandsCog)
    by_display = {
        " ".join(token.replace("_", "-") for token in definition.path): definition
        for definition in service._commands.values()
    }
    checked = 0
    for name, command in commands.items():
        params = by_display[name].params
        assert params is not None
        options = {option.display_name: option for option in command._params.values()}
        assert set(options) == {param.name for param in params}, name
        for param in params:
            option = options[param.name]
            assert option.required is param.required, f"{name}.{param.name}"
            assert str(option.description) == (param.description or "…")
            expected_choices = (
                ("global", "thread") if param.kind == "scope" else param.choices
            )
            actual_choices = tuple(
                choice.value for choice in (option.choices or ())
            )
            assert actual_choices == expected_choices, f"{name}.{param.name}"
            if param.kind == "flag":
                assert option.type is AppCommandOptionType.boolean
            elif param.type == "int":
                assert option.type is AppCommandOptionType.integer
            else:
                assert option.type is AppCommandOptionType.string
            checked += 1
    assert checked > 50, "the schema sweep covered suspiciously few arguments"


def test_required_arguments_precede_optional_ones() -> None:
    """Discord rejects a required option after an optional one at sync time."""
    for name, command in _leaf_commands(GeneratedCommandsCog).items():
        required = [option.required for option in command._params.values()]
        assert required == sorted(required, reverse=True), name


def test_autocomplete_is_wired_exactly_where_a_resolver_exists(
    generator: ModuleType, service: CommandService
) -> None:
    assert set(generator.RESOLVED_CHOICES_REFS) == set(AUTOCOMPLETE_RESOLVERS)
    commands = _leaf_commands(GeneratedCommandsCog)
    by_display = {
        generator.display_path(definition.path): definition
        for definition in service._commands.values()
    }
    wired = 0
    for name, command in commands.items():
        params = by_display[name].params or ()
        for param in params:
            option = next(
                option
                for option in command._params.values()
                if option.display_name == param.name
            )
            expected = (
                param.choices_ref in AUTOCOMPLETE_RESOLVERS and not param.choices
            )
            assert (option.autocomplete is not None) is expected, f"{name}.{param.name}"
            wired += int(expected)
    assert wired >= 1


def test_the_whole_command_tree_fits_discord_limits() -> None:
    """Generated plus hand-written cogs share one namespace and one cap."""
    top_level: list[str] = []
    children: dict[str, int] = {}
    for cog in ALL_COGS:
        for entry in cog.__cog_app_commands__:
            top_level.append(entry.name)
            if isinstance(entry, app_commands.Group):
                children[entry.qualified_name] = len(entry.commands)
                for sub in entry.walk_commands():
                    if isinstance(sub, app_commands.Group):
                        children[sub.qualified_name] = len(sub.commands)
    assert len(top_level) == len(set(top_level)), "duplicate top-level command name"
    assert len(top_level) <= 100
    for name, count in children.items():
        assert count <= 25, f"{name} has {count} subcommands"
    for command in _leaf_commands(GeneratedCommandsCog).values():
        assert len(command._params) <= 25
        assert len(command.description) <= 100
        for option in command._params.values():
            assert len(option.choices or ()) <= 25
            assert len(str(option.description)) <= 100


def test_commands_users_see_today_are_still_registered() -> None:
    """The pass may add Discord commands; it may not silently remove one."""
    registered = set()
    for cog in ALL_COGS:
        for entry in cog.__cog_app_commands__:
            registered.add(entry.qualified_name)
            if isinstance(entry, app_commands.Group):
                registered.update(sub.qualified_name for sub in entry.walk_commands())
    for name in (
        "ask",
        "channel-context",
        "clear",
        "compact",
        "config get",
        "config set",
        "config show",
        "context",
        "env get",
        "env set",
        "env show",
        "export",
        "fallback approve",
        "help",
        "hook create",
        "memory forget",
        "memory list",
        "memory save",
        "memory search",
        "model",
        "models",
        "notepad write",
        "restart",
        "show-tools",
        "status",
        "stop",
        "tasks",
        "think",
        "thread",
        "todos add",
        "todos list",
        "tools enable",
        "tools search",
    ):
        assert name in registered, f"/{name} disappeared from the Discord tree"


def test_generator_refuses_a_tree_over_discords_global_command_cap(
    generator: ModuleType, service: CommandService
) -> None:
    """The platform caps are enforced, not documented."""
    import dataclasses

    base = next(
        definition
        for definition in generator.select_commands(service)
        if definition.path == ("status",)
    )
    budget = generator.MAX_GLOBAL_COMMANDS - generator.RESERVED_TOP_LEVEL_SLOTS
    too_many = [
        dataclasses.replace(base, id=f"zzz{index}", path=(f"zzz{index}",))
        for index in range(budget + 1)
    ]

    with pytest.raises(generator.GeneratorError, match="global cap"):
        generator.enforce_limits(service, too_many, [])


def test_generator_refuses_an_over_long_description(
    generator: ModuleType, service: CommandService
) -> None:
    import dataclasses

    selected = generator.select_commands(service)
    broken = dataclasses.replace(selected[0], description="x" * 101)
    with pytest.raises(generator.GeneratorError, match="description is 101"):
        generator.enforce_limits(service, [broken], generator.group_paths([broken]))


# ── the shared endpoint resolver behind every ref (#110 slice 6) ─────────────


class _OptionsApi:
    def __init__(self, options: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.options = options if options is not None else [
            {"id": "hook-a", "label": "Morning brief", "meta": "enabled, post_turn"},
            {"id": "hook-b", "label": "Lint gate", "meta": "disabled, pre_tool_use"},
        ]

    async def list_command_options(self, ref: str, **kwargs) -> list[dict]:
        self.calls.append((ref, kwargs))
        return self.options


class _ResolverBot:
    def __init__(self, api: _OptionsApi, user_id: str | None = "alice") -> None:
        self.api = api
        self._user_id = user_id

    async def resolve_user_id(self, discord_user_id: int) -> str | None:
        return self._user_id


def _interaction(channel_id: int | None = 42) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=123),
        guild_id=7,
        channel_id=channel_id,
    )


def test_endpoint_resolver_maps_options_to_scoped_choices() -> None:
    from nymeria.triggers.discord_cogs.autocomplete import endpoint_resolver
    from nymeria.triggers.discord_bot import make_thread_id

    api = _OptionsApi()
    bot = _ResolverBot(api)

    choices = run(endpoint_resolver("hooks")(bot, _interaction(), ""))

    assert [choice.value for choice in choices] == ["hook-a", "hook-b"]
    assert choices[0].name == "Morning brief · enabled, post_turn"
    # The call acted as the linked user, in the channel's thread, with the
    # server-side cap so a keystroke never pulls a whole catalog.
    (ref, kwargs), = api.calls
    assert ref == "hooks"
    assert kwargs["user_id"] == "alice"
    assert kwargs["thread_id"] == make_thread_id(7, 42)
    assert kwargs["q"] is None  # empty typed text sends no filter
    assert kwargs["limit"] == 25


def test_endpoint_resolver_filters_on_value_label_and_meta() -> None:
    from nymeria.triggers.discord_cogs.autocomplete import endpoint_resolver

    bot = _ResolverBot(_OptionsApi())

    by_meta = run(endpoint_resolver("hooks")(bot, _interaction(), "disabled"))
    assert [choice.value for choice in by_meta] == ["hook-b"]

    by_label = run(endpoint_resolver("hooks")(bot, _interaction(), "morning"))
    assert [choice.value for choice in by_label] == ["hook-a"]


def test_endpoint_resolver_unlinked_user_gets_no_suggestions() -> None:
    from nymeria.triggers.discord_cogs.autocomplete import endpoint_resolver

    api = _OptionsApi()
    bot = _ResolverBot(api, user_id=None)

    assert run(endpoint_resolver("hooks")(bot, _interaction(), "")) == []
    assert api.calls == []  # never reached the backend as the service account


def test_endpoint_resolver_degrades_on_backend_fault_and_caps_at_25() -> None:
    from nymeria.triggers.discord_cogs.autocomplete import (
        MAX_CHOICES,
        endpoint_resolver,
    )

    class _Broken(_OptionsApi):
        async def list_command_options(self, ref: str, **kwargs) -> list[dict]:
            raise RuntimeError("backend down")

    assert run(endpoint_resolver("hooks")(_ResolverBot(_Broken()), _interaction(), "")) == []

    many = _OptionsApi(options=[{"id": f"t-{index:03d}"} for index in range(40)])
    capped = run(endpoint_resolver("threads")(_ResolverBot(many), _interaction(), ""))
    assert len(capped) == MAX_CHOICES
