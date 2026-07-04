from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from cli_fixtures import run

from nymeria.triggers.cli.commands import (
    Command,
    CommandContext,
    CommandMessage,
    CommandRegistry,
    CommandResult,
    ListCommandOutputSink,
)


def test_parse_and_resolve_root_alias_and_subcommand_alias() -> None:
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="thread",
            aliases=["/t"],
            description="Manage threads",
            handler=lambda _state, _args: None,
            subcommands={
                "switch": Command(
                    name="switch",
                    aliases=["s"],
                    description="Switch thread",
                    handler=lambda _state, _args: None,
                )
            },
        )
    )

    invocation = registry.parse('/t s "thread one"')
    match = registry.resolve(invocation)

    assert invocation is not None
    assert invocation.command_name == "t"
    assert match is not None
    assert match.path == ("thread", "switch")
    assert match.args == ("thread one",)


def test_async_context_handler_receives_client_and_helpers() -> None:
    registry = CommandRegistry(include_builtins=False)
    client = object()
    actions: list[Any] = []
    prompts: list[str] = []

    async def handler(ctx: CommandContext, args: list[str]) -> CommandResult:
        assert ctx.client is client
        assert ctx.thread_id == "thread-1"
        assert ctx.user_id == "alice"
        prompts.append("confirm")
        assert await ctx.confirm("confirm?") is True
        await ctx.dispatch({"type": "command", "args": args})
        return CommandResult.completed(
            CommandMessage(f"ran {' '.join(args)}", level="success")
        )

    registry.register(
        Command(
            name="ping",
            description="Ping test command",
            handler=handler,
        )
    )
    sink = ListCommandOutputSink()
    context = CommandContext(
        client=client,
        output=sink,
        dispatch_state=actions.append,
        confirm_handler=lambda _prompt: True,
        thread_id="thread-1",
        user_id="alice",
    )

    result = run(registry.dispatch_async(context, "/ping hello world"))

    assert result.ok is True
    assert result.command_path == ("ping",)
    assert actions == [{"type": "command", "args": ["hello", "world"]}]
    assert prompts == ["confirm"]
    assert sink.messages == [
        CommandMessage("ran hello world", level="success")
    ]


def test_json_flag_strips_args_and_emits_result_payload(capsys: Any) -> None:
    registry = CommandRegistry(include_builtins=False)
    sink = ListCommandOutputSink()

    async def handler(ctx: CommandContext, args: list[str]) -> CommandResult:
        assert ctx.metadata["json_output"] is True
        assert args == ["alpha"]
        return CommandResult.completed(
            "human text",
            json_payload={"args": args, "mode": "json"},
        )

    registry.register(
        Command(
            name="echo",
            description="Echo test command",
            handler=handler,
        )
    )
    context = CommandContext(output=sink)

    result = run(registry.dispatch_async(context, "/echo --json alpha"))

    captured = capsys.readouterr()
    assert result.ok is True
    assert json.loads(captured.out) == {"args": ["alpha"], "mode": "json"}
    assert captured.err == ""
    assert sink.messages == []


def test_unknown_command_returns_structured_error() -> None:
    registry = CommandRegistry(include_builtins=False)
    sink = ListCommandOutputSink()

    result = run(
        registry.dispatch_async(CommandContext(output=sink), "/missing value")
    )

    assert result.handled is True
    assert result.status == "error"
    assert result.error_code == "unknown_command"
    assert result.payload["command"] == "missing"
    assert sink.messages[0].level == "error"
    assert "Unknown command: /missing" in sink.messages[0].content


def test_non_command_input_is_unhandled() -> None:
    registry = CommandRegistry(include_builtins=False)
    sink = ListCommandOutputSink()

    result = run(registry.dispatch_async(CommandContext(output=sink), "chat text"))

    assert result.handled is False
    assert result.status == "unhandled"
    assert sink.messages == []


def test_builtin_help_exit_and_clear_are_context_commands() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="thread",
            description="Manage threads",
            usage="/thread list",
            handler=lambda _ctx, _args: None,
        )
    )
    registry.register(
        Command(
            name="help",
            aliases=["/legacy-help"],
            description="A re-registration should not replace builtin help",
            handler=lambda _ctx, _args: None,
        )
    )
    sink = ListCommandOutputSink()
    context = CommandContext(output=sink)

    help_result = run(registry.dispatch_async(context, "/help thread"))
    exit_result = run(registry.dispatch_async(context, "/exit"))
    clear_result = run(registry.dispatch_async(context, "/cls"))

    assert help_result.status == "ok"
    assert "/thread list" in sink.messages[0].content
    assert exit_result.status == "exit"
    assert clear_result.status == "clear"
    assert "/legacy-help" in registry.get_completions()


def test_help_deduplicates_default_subcommands_and_wraps_long_rows() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="tools",
            description="List/manage tools",
            usage="/tools list",
            handler=lambda _state, _args: None,
            category="Tools",
            subcommands={
                "list": Command(
                    name="list",
                    description="List tools",
                    usage="list",
                    handler=lambda _state, _args: None,
                    category="Tools",
                ),
                "test": Command(
                    name="test",
                    description="Test a custom tool",
                    usage="test <custom-tool-id> [json-or-key=value...]",
                    handler=lambda _state, _args: None,
                    category="Tools",
                ),
            },
        )
    )
    sink = ListCommandOutputSink()
    context = CommandContext(
        output=sink,
        metadata={"capabilities": SimpleNamespace(width=60)},
    )

    result = run(registry.dispatch_async(context, "/help tools"))

    assert result.status == "ok"
    content = sink.messages[0].content
    assert content.count("/tools list") == 1
    assert "List/manage tools" not in content
    assert "Tools" in content
    lines = content.splitlines()
    long_usage_index = next(
        index for index, line in enumerate(lines) if "/tools test" in line
    )
    assert lines[long_usage_index].strip().startswith("/tools test")
    assert lines[long_usage_index + 1].strip() == "Test a custom tool"


def test_completion_items_and_palette_entries_include_descriptions() -> None:
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="tools",
            description="Manage tools",
            usage="/tools",
            handler=lambda _state, _args: None,
            subcommands={
                "enable": Command(
                    name="enable",
                    aliases=["on"],
                    description="Enable a tool",
                    usage="enable <name>",
                    handler=lambda _state, _args: None,
                )
            },
        )
    )

    completions = registry.get_completion_items()
    palette_entries = registry.get_palette_entries()

    assert any(
        item.text == "/tools enable"
        and item.description == "Enable a tool"
        for item in completions
    )
    assert any(item.text == "/tools on" for item in completions)
    assert any(
        entry.text == "/tools enable"
        and entry.usage == "/tools enable <name>"
        for entry in palette_entries
    )


