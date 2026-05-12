from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from cli_fixtures import FakeAgentClient, FakeTerminalCapabilities

from nymeria.triggers.cli.commands import (
    Command,
    CommandContext,
    CommandMessage,
    CommandRegistry,
    CommandResult,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
)
from nymeria.triggers.cli.state import start_turn


def run(coro):
    return asyncio.run(coro)


class FakeConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cleared = False

    def print(self, text: str = "", *args: Any, **kwargs: Any) -> None:
        self.lines.append(str(text))

    def clear(self) -> None:
        self.cleared = True


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
            handler_mode="context",
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
            handler_mode="context",
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


def test_legacy_dispatch_calls_legacy_handler_and_prints_structured_errors() -> None:
    registry = CommandRegistry(include_builtins=False)
    state = SimpleNamespace(
        console=FakeConsole(),
        thread_id="thread-1",
        user_id="alice",
        calls=[],
    )

    def handler(legacy_state, args: list[str]) -> None:
        legacy_state.calls.append(args)

    registry.register(
        Command(
            name="legacy",
            description="Legacy command",
            handler=handler,
        )
    )

    assert registry.dispatch(state, "/legacy a b") is True
    assert state.calls == [["a", "b"]]
    assert registry.dispatch(state, "/nope") is True
    assert any("Unknown command: /nope" in line for line in state.console.lines)
    assert registry.dispatch(state, "chat text") is False


def test_builtin_help_exit_and_clear_are_context_commands() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="thread",
            description="Manage threads",
            usage="/thread list",
            handler=lambda _state, _args: None,
        )
    )
    registry.register(
        Command(
            name="help",
            aliases=["/legacy-help"],
            description="Legacy help should not replace builtin help",
            handler=lambda _state, _args: None,
        )
    )
    sink = ListCommandOutputSink()
    legacy_state = SimpleNamespace(console=FakeConsole(), running=True)
    context = CommandContext(output=sink, legacy_state=legacy_state)

    help_result = run(registry.dispatch_async(context, "/help thread"))
    exit_result = run(registry.dispatch_async(context, "/exit"))
    clear_result = run(registry.dispatch_async(context, "/clear"))

    assert help_result.status == "ok"
    assert "/thread list" in sink.messages[0].content
    assert exit_result.status == "exit"
    assert legacy_state.running is False
    assert clear_result.status == "clear"
    assert legacy_state.console.cleared is True
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


def make_shell_with_registry(
    registry: CommandRegistry,
) -> FullScreenPromptToolkitShell:
    return FullScreenPromptToolkitShell(
        client=FakeAgentClient(),
        capabilities=FakeTerminalCapabilities(width=100),
        config=FullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="test-model",
            thread_label="Fixture thread",
        ),
        command_registry=registry,
    )


def test_full_screen_shell_dispatches_slash_commands_to_registry() -> None:
    registry = CommandRegistry()
    shell = make_shell_with_registry(registry)

    result = run(shell._run_command("/help"))

    assert result.status == "ok"
    assert shell.client.chat_requests == []
    assert "Commands" in shell.transcript.text
    assert "/help [query]" in shell.transcript.text


def test_full_screen_shell_renders_unknown_command_as_structured_error() -> None:
    shell = make_shell_with_registry(CommandRegistry())

    result = run(shell._run_command("/not-real"))

    assert result.status == "error"
    assert result.error_code == "unknown_command"
    assert "Error: Unknown command: /not-real" in shell.transcript.text
    assert shell.client.chat_requests == []


def test_full_screen_shell_clear_command_resets_transcript() -> None:
    shell = make_shell_with_registry(CommandRegistry())
    shell.state = start_turn(shell.state, "hello", now=1.0)
    shell._refresh_transcript()
    assert "──── You " in shell.transcript.text
    assert "\n  hello" in shell.transcript.text

    result = run(shell._run_command("/clear"))

    assert result.status == "clear"
    assert shell.transcript.text == ""
    assert "Cleared." in shell._status_text()
