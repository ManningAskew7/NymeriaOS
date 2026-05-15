"""Tests for CLI backend slash-command proxy registration."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from nymeria.triggers.cli.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import memory, model
from nymeria.triggers.cli.commands.backend import BackendCommandProvider


class _FakeCommandClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: Optional[str] = None,
        source: str = "cli",
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "command": command,
                "thread_id": thread_id,
                "source": source,
                "actor": actor,
                "surface": surface,
                "user_id": user_id,
            }
        )
        return {
            "success": True,
            "markdown": f"backend result for {command}",
            "command": command.lstrip("/"),
            "level": "success",
        }


def run(coro):
    return asyncio.run(coro)


def command_info(name: str, *, category: str = "Memory") -> dict[str, Any]:
    path = name.split()
    return {
        "id": ".".join(path),
        "name": name,
        "path": path,
        "usage": f"/{name}",
        "description": f"Backend {name}",
        "category": category,
        "execution_kind": "command",
        "aliases": [],
    }


def make_context(client: Any, output: ListCommandOutputSink) -> CommandContext:
    return CommandContext(
        client=client,
        output=output,
        thread_id="cli-thread",
        user_id="alice",
    )


def test_backend_provider_overrides_duplicate_memory_subcommand():
    registry = CommandRegistry(include_builtins=False)
    memory.register(registry)
    BackendCommandProvider([command_info("memory save")]).register(registry)

    client = _FakeCommandClient()
    sink = ListCommandOutputSink()
    result = run(
        registry.dispatch_async(
            make_context(client, sink),
            "/memory save color deep blue",
        )
    )

    assert result.ok is True
    assert client.calls == [
        {
            "command": "/memory save color deep blue",
            "thread_id": "cli-thread",
            "source": "cli",
            "actor": "user",
            "surface": "cli",
            "user_id": "alice",
        }
    ]
    assert sink.messages[0].content == "backend result for /memory save color deep blue"


def test_backend_provider_preserves_local_model_subcommands_while_proxying_root():
    registry = CommandRegistry(include_builtins=False)
    model.register(registry)
    BackendCommandProvider([command_info("model", category="LLM")]).register(registry)

    assert registry.resolve("/model show").path == ("model", "show")
    assert registry.resolve("/model show").command.metadata.get("backend_command") is None

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/model gpt-test thread",
        )
    )

    assert result.ok is True
    assert client.calls[0]["command"] == "/model gpt-test thread"


def test_backend_provider_adds_new_group_subcommands_to_help_once():
    registry = CommandRegistry(include_builtins=True)
    BackendCommandProvider(
        [
            command_info("todos list", category="TODOs"),
            command_info("todos add", category="TODOs"),
        ]
    ).register(registry)

    entries = registry.get_palette_entries()
    labels = [entry.usage for entry in entries]

    assert labels.count("/todos list") == 1
    assert "/todos add" in labels
    assert "/help [query]" in labels


def test_backend_provider_rejects_unmanaged_local_conflict():
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="custom",
            description="Local custom group",
            usage="/custom local",
            handler=lambda *_args: None,
            subcommands={
                "run": Command(
                    name="run",
                    description="Local run",
                    usage="run",
                    handler=lambda *_args: None,
                )
            },
        )
    )

    with pytest.raises(ValueError, match="conflicts with backend command"):
        BackendCommandProvider([command_info("custom run")]).register(registry)
