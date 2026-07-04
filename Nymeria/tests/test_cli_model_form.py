from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from cli_fixtures import run
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

    async def execute_command(self, command: str, **_kwargs: Any) -> dict[str, Any]:
        """Emulate the backend /model handlers under the form contract."""

        self.calls.append(("execute_command", {"command": command}))
        tokens = command.lstrip("/").split()
        if tokens == ["model"]:
            options = [
                {
                    "id": str(entry["id"]),
                    "label": str(entry["id"]),
                    "meta": "",
                    "description": "",
                    "current": entry["id"] == "gpt-5.5",
                }
                for entry in self.models
            ]
            return {
                "success": True,
                "markdown": "global: gpt-5.5 (openai)",
                "command": "model",
                "level": "success",
                "data": {
                    "form": {
                        "version": 1,
                        "title": "Select model",
                        "footer_hint": "",
                        "tabs": [
                            {
                                "label": "Models",
                                "fields": [
                                    {"kind": "search", "key": "filter", "placeholder": ""},
                                    {"kind": "radio", "key": "model", "options": options},
                                ],
                            }
                        ],
                        "submit": {"command": "model {model} thread"},
                    }
                },
            }
        if len(tokens) == 3 and tokens[0] == "model" and tokens[2] == "thread":
            self.thread_config["llm_config"] = {"model": tokens[1]}
            return {
                "success": True,
                "markdown": f"**Done.** Model for this thread set to {tokens[1]}.",
                "command": "model",
                "level": "success",
                "data": {"state": {"model": tokens[1]}},
            }
        return {"success": False, "markdown": "unexpected", "command": "model", "level": "error"}


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


def _backend_registry() -> CommandRegistry:
    """Registry as assembled on a live connection: backend proxy owns /model."""

    from nymeria.triggers.cli.commands.backend import BackendCommandProvider

    registry = _registry()
    BackendCommandProvider(
        [
            {
                "id": "model",
                "name": "model",
                "path": ["model"],
                "usage": "/model",
                "description": "Show or set model",
                "category": "LLM",
                "execution_kind": "command",
                "aliases": [],
            }
        ]
    ).register(registry)
    return registry


def test_model_root_opens_backend_declared_form_in_rich_repl() -> None:
    client = FakeModelClient()
    actions: list[Any] = []
    ctx = _make_context(client, renderer="rich", actions=actions)

    result = run(_backend_registry().dispatch_async(ctx, "/model"))

    assert result.ok is True
    assert result.payload.get("suppress_transcript") is True
    assert ("execute_command", {"command": "/model"}) in client.calls

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


def test_form_confirm_round_trips_backend_set_and_state_hint() -> None:
    client = FakeModelClient()
    actions: list[Any] = []
    ctx = _make_context(client, renderer="rich", actions=actions)

    run(_backend_registry().dispatch_async(ctx, "/model"))
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
    assert (
        "execute_command",
        {"command": "/model claude-opus-4-8 thread"},
    ) in client.calls
    assert client.thread_config["llm_config"] == {"model": "claude-opus-4-8"}
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
