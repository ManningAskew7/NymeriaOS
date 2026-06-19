from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from nymeria.triggers.cli.app import CLIApp
from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import model
from nymeria.triggers.cli.commands.base import CommandResult
from nymeria.triggers.cli.rendering.form_panel import (
    FormField,
    FormResult,
    FormSpec,
    FormTab,
)


def run(coro):
    return asyncio.run(coro)


class FakeModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.models = [
            {"id": "gpt-5.5", "context_length": 400000},
            {"id": "claude-opus-4-8", "context_length": 200000},
            {"id": "claude-sonnet-4-6", "context_length": 200000},
        ]
        self.thread_config: dict[str, Any] = {"llm_config": {"model": ""}}

    async def list_available_models(
        self,
        provider: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("list_available_models", {"provider": provider, "user_id": user_id})
        )
        return [dict(entry) for entry in self.models]

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_settings", {"user_id": user_id}))
        return {"llm_model": "gpt-5.5"}

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_thread_config", {"thread_id": thread_id, "user_id": user_id})
        )
        return {"llm_config": dict(self.thread_config["llm_config"])}

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_context_stats", {"thread_id": thread_id, "user_id": user_id})
        )
        return {}

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
        return dict(self.thread_config)


def _make_context(
    client: FakeModelClient,
    *,
    renderer: str,
    actions: list[Any] | None,
) -> CommandContext:
    return CommandContext(
        client=client,
        output=ListCommandOutputSink(),
        dispatch_state=(actions.append if actions is not None else None),
        thread_id="thread-1",
        user_id="alice",
        metadata={"capabilities": SimpleNamespace(renderer=renderer)},
    )


def _registry() -> CommandRegistry:
    registry = CommandRegistry()
    model.register(registry)
    return registry


def test_model_command_opens_form_in_rich_repl() -> None:
    client = FakeModelClient()
    actions: list[Any] = []
    ctx = _make_context(client, renderer="rich", actions=actions)

    result = run(_registry().dispatch_async(ctx, "/model"))

    assert result.ok is True
    assert result.payload.get("suppress_transcript") is True

    open_actions = [action for action in actions if action.get("type") == "open_form"]
    assert len(open_actions) == 1
    spec = open_actions[0]["spec"]
    assert isinstance(spec, FormSpec)
    assert spec.title == "Select model"

    radio = spec.tabs[0].fields[1]
    assert radio.kind == "radio"
    assert [option.id for option in radio.options if option.current] == ["gpt-5.5"]
    # the search field comes first so the user can filter model names
    assert spec.tabs[0].fields[0].kind == "search"


def test_form_confirm_persists_and_dispatches_set_model() -> None:
    client = FakeModelClient()
    actions: list[Any] = []
    ctx = _make_context(client, renderer="rich", actions=actions)

    run(_registry().dispatch_async(ctx, "/model"))
    spec = next(a["spec"] for a in actions if a.get("type") == "open_form")

    confirm = run(
        spec.on_confirm(
            FormResult(
                spec_title="Select model",
                tab_label="Models",
                radio_value="claude-opus-4-8",
            )
        )
    )

    assert confirm.ok is True
    assert any(
        name == "update_thread_config"
        and payload.get("llm_config") == {"model": "claude-opus-4-8"}
        for name, payload in client.calls
    )
    assert {"type": "set_model", "model": "claude-opus-4-8"} in actions


def test_model_command_falls_back_to_show_without_forms() -> None:
    client = FakeModelClient()
    actions: list[Any] = []
    ctx = _make_context(client, renderer="plain", actions=actions)

    result = run(_registry().dispatch_async(ctx, "/model"))

    assert result.ok is True
    assert not any(action.get("type") == "open_form" for action in actions)
    rendered = "\n".join(message.content for message in result.messages)
    assert "gpt-5.5" in rendered


def test_dispatch_repl_action_open_form_calls_runtime() -> None:
    class _FakeRuntime:
        def __init__(self) -> None:
            self.opened: list[FormSpec] = []

        def open_form(self, spec: FormSpec) -> None:
            self.opened.append(spec)

    class _Stub:
        def __init__(self, runtime: _FakeRuntime) -> None:
            self._active_rich_runtime = runtime

    async def _noop(_result: FormResult) -> CommandResult:
        return CommandResult.completed()

    spec = FormSpec(
        title="Select model",
        tabs=(FormTab(label="Models", fields=(FormField(kind="radio", key="model"),)),),
        on_confirm=_noop,
    )
    runtime = _FakeRuntime()
    stub = _Stub(runtime)

    run(
        CLIApp._dispatch_repl_action(
            cast(CLIApp, stub), {"type": "open_form", "spec": spec}
        )
    )

    assert runtime.opened == [spec]
