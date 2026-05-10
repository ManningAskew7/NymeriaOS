from __future__ import annotations

import asyncio
import copy
from typing import Any

from cli_fixtures import FakeTerminalCapabilities

from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import context as context_commands
from nymeria.triggers.cli.commands import model, system, threads
from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
)


def run(coro):
    return asyncio.run(coro)


class CoreFakeClient:
    connection_label = "api http://test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.threads = [
            {
                "thread_id": "thread-1",
                "title": "Current",
                "pinned": False,
                "platform": "desktop",
                "updated_at": "2026-05-10T12:00:00Z",
            },
            {
                "thread_id": "thread-2",
                "title": "Next",
                "pinned": True,
                "platform": "desktop",
                "updated_at": "2026-05-10T13:00:00Z",
            },
        ]
        self.settings = {
            "llm_provider": "openai",
            "llm_model": "gpt-global",
            "llm_temperature": 0.2,
            "llm_max_tokens": 1024,
            "llm_extended_thinking": False,
            "llm_use_model_defaults": True,
            "context_management": "auto_compact",
            "compact_threshold": 0.8,
            "compact_keep_messages": 4,
            "tool_output_max_chars": 12000,
            "watchdog_enabled": True,
            "watchdog_interval_minutes": 15,
            "log_level": "INFO",
        }
        self.thread_config = {
            "thread_id": "thread-1",
            "enabled_tools": ["memory"],
            "disabled_tools": [],
            "enabled_skills": [],
            "disabled_skills": [],
            "llm_config": {"model": "gpt-thread"},
            "callable": False,
        }
        self.history = {
            "messages": [
                {"role": "human", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi there"}],
                    "toolCalls": [{"name": "search_memory"}],
                },
            ]
        }
        self.context_stats = {
            "thread_id": "thread-1",
            "total_tokens": 120,
            "context_limit": 1000,
            "input_tokens": 80,
            "output_tokens": 40,
            "usage_percentage": 12,
            "context_management": "auto_compact",
            "compaction_count": 1,
            "last_compaction": None,
            "model": "gpt-thread",
        }
        self.models = [
            {"id": "gpt-thread", "context_length": 128000},
            {"id": "gpt-other", "context_length": 64000},
        ]

    async def list_threads(self, user_id: str = "default") -> list[dict[str, Any]]:
        self.calls.append(("list_threads", {"user_id": user_id}))
        return copy.deepcopy(self.threads)

    async def create_thread(
        self,
        user_id: str = "default",
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        selected = thread_id or "thread-new"
        self.calls.append(
            (
                "create_thread",
                {"user_id": user_id, "thread_id": thread_id, "title": title},
            )
        )
        created = {
            "thread_id": selected,
            "title": title or "New Chat",
            "pinned": False,
            "platform": "desktop",
        }
        self.threads.append(created)
        return copy.deepcopy(created)

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "update_thread_metadata",
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "title": title,
                    "pinned": pinned,
                },
            )
        )
        for item in self.threads:
            if item["thread_id"] == thread_id:
                if title is not None:
                    item["title"] = title
                if pinned is not None:
                    item["pinned"] = pinned
                return copy.deepcopy(item)
        return {"thread_id": thread_id, "title": title, "pinned": pinned}

    async def delete_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("delete_thread", {"thread_id": thread_id, "user_id": user_id}))
        self.threads = [item for item in self.threads if item["thread_id"] != thread_id]
        return {"ok": True, "thread_id": thread_id}

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("stop", {"thread_id": thread_id, "user_id": user_id}))
        return {"ok": True, "thread_id": thread_id}

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "get_history",
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "include_internal": include_internal,
                },
            )
        )
        return copy.deepcopy(self.history)

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_context_stats", {"thread_id": thread_id, "user_id": user_id})
        )
        return copy.deepcopy({**self.context_stats, "thread_id": thread_id})

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_settings", {"user_id": user_id}))
        return copy.deepcopy(self.settings)

    async def update_settings(
        self,
        *,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(("update_settings", {"user_id": user_id, **kwargs}))
        self.settings.update(kwargs)
        return {"updated": list(kwargs), "restart_required": False}

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_thread_config", {"thread_id": thread_id, "user_id": user_id})
        )
        return copy.deepcopy({**self.thread_config, "thread_id": thread_id})

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            ("update_thread_config", {"thread_id": thread_id, "user_id": user_id, **kwargs})
        )
        self.thread_config.update(kwargs)
        return copy.deepcopy(self.thread_config)

    async def compact(self, thread_id: str, user_id: str) -> dict[str, Any]:
        self.calls.append(("compact", {"thread_id": thread_id, "user_id": user_id}))
        return {
            "success": True,
            "messages_removed": 3,
            "messages_before": 8,
            "messages_after": 5,
        }

    async def list_available_models(
        self,
        provider: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("list_available_models", {"provider": provider, "user_id": user_id})
        )
        return copy.deepcopy(self.models)


def make_registry() -> CommandRegistry:
    registry = CommandRegistry()
    system.register(registry)
    context_commands.register(registry)
    threads.register(registry)
    model.register(registry)
    return registry


def make_context(
    client: CoreFakeClient,
    *,
    output: ListCommandOutputSink | None = None,
    actions: list[Any] | None = None,
    confirm: bool = False,
) -> CommandContext:
    return CommandContext(
        client=client,
        output=output or ListCommandOutputSink(),
        dispatch_state=(actions.append if actions is not None else None),
        confirm_handler=lambda _prompt: confirm,
        thread_id="thread-1",
        user_id="alice",
    )


def test_thread_commands_use_client_and_dispatch_thread_actions() -> None:
    client = CoreFakeClient()
    actions: list[Any] = []
    sink = ListCommandOutputSink()
    registry = make_registry()
    ctx = make_context(client, output=sink, actions=actions)

    list_result = run(registry.dispatch_async(ctx, "/thread list"))
    switch_result = run(registry.dispatch_async(ctx, "/t s thread-2"))
    new_result = run(registry.dispatch_async(ctx, "/threads new Draft title"))
    rename_result = run(registry.dispatch_async(ctx, "/thread rename Renamed"))
    pin_result = run(registry.dispatch_async(ctx, "/thread pin thread-2 off"))

    assert list_result.ok is True
    assert "Current" in sink.messages[0].content
    assert switch_result.payload["thread_id"] == "thread-2"
    assert new_result.payload["thread_id"] == "thread-new"
    assert rename_result.ok is True
    assert pin_result.payload == {"thread_id": "thread-2", "pinned": False}
    assert actions[:2] == [
        {"type": "switch_thread", "thread_id": "thread-2", "thread_label": "Next"},
        {
            "type": "switch_thread",
            "thread_id": "thread-new",
            "thread_label": "Draft title",
        },
    ]
    assert ("update_thread_metadata", {
        "thread_id": "thread-new",
        "user_id": "alice",
        "title": "Renamed",
        "pinned": None,
    }) in client.calls


def test_thread_delete_requires_confirmation_then_deletes() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    unconfirmed = make_context(client, output=sink, confirm=False)

    result = run(registry.dispatch_async(unconfirmed, "/thread delete thread-2"))

    assert result.ok is True
    assert sink.messages[-1].level == "warning"
    assert not any(name == "delete_thread" for name, _payload in client.calls)

    confirmed = make_context(client, output=ListCommandOutputSink(), confirm=True)
    result = run(registry.dispatch_async(confirmed, "/thread delete thread-2"))

    assert result.ok is True
    assert ("delete_thread", {"thread_id": "thread-2", "user_id": "alice"}) in client.calls


def test_model_settings_history_context_and_compact_commands_use_client() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    ctx = make_context(client, output=sink, confirm=True)

    assert run(registry.dispatch_async(ctx, "/model show")).ok is True
    assert run(registry.dispatch_async(ctx, "/model set gpt-new")).ok is True
    assert run(registry.dispatch_async(ctx, "/model available openai")).ok is True
    assert run(registry.dispatch_async(ctx, "/settings view")).ok is True
    assert run(
        registry.dispatch_async(ctx, "/settings patch llm_temperature=0.4")
    ).ok is True
    assert run(registry.dispatch_async(ctx, "/history --internal 5")).ok is True
    assert run(registry.dispatch_async(ctx, "/context")).ok is True
    assert run(registry.dispatch_async(ctx, "/thread compact")).ok is True
    assert run(registry.dispatch_async(ctx, "/thread stop")).ok is True

    call_names = [name for name, _payload in client.calls]
    assert "update_thread_config" in call_names
    assert "list_available_models" in call_names
    assert ("update_settings", {"user_id": "alice", "llm_temperature": 0.4}) in client.calls
    assert ("get_history", {
        "thread_id": "thread-1",
        "user_id": "alice",
        "include_internal": True,
    }) in client.calls
    assert ("compact", {"thread_id": "thread-1", "user_id": "alice"}) in client.calls
    assert ("stop", {"thread_id": "thread-1", "user_id": "alice"}) in client.calls
    assert any("gpt-new" in message.content for message in sink.messages)
    assert any("hi there" in message.content for message in sink.messages)


def test_settings_patch_rejects_secret_or_unknown_fields() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    ctx = make_context(client, output=sink, confirm=True)

    result = run(
        registry.dispatch_async(ctx, "/settings patch openai_api_key=secret")
    )

    assert result.status == "error"
    assert result.error_code == "unsafe_settings_patch"
    assert not any(name == "update_settings" for name, _payload in client.calls)


def test_full_screen_shell_core_commands_update_runtime_context() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    shell = FullScreenPromptToolkitShell(
        client=client,
        capabilities=FakeTerminalCapabilities(width=100),
        config=FullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="gpt-thread",
            thread_label="Current",
        ),
        command_registry=registry,
    )

    switch_result = run(shell._run_command("/thread switch thread-2"))
    model_result = run(shell._run_command("/model set gpt-new"))
    redraw_result = run(shell._run_command("/redraw"))

    assert switch_result.ok is True
    assert model_result.ok is True
    assert redraw_result.ok is True
    assert shell.config.thread_id == "thread-2"
    assert shell.config.thread_label == "Next"
    assert shell.config.model == "gpt-new"
    assert "Switched to thread-2 Next" in shell.transcript.text
    assert "Model set to: gpt-new" in shell.transcript.text
