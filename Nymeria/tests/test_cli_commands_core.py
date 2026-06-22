from __future__ import annotations

import copy
import json
from typing import Any

from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import context as context_commands
from nymeria.triggers.cli.commands import fast, smart
from nymeria.triggers.cli.commands import model, provider, system, threads, usage
from nymeria.triggers.cli.rendering.full_screen_legacy import (
    LegacyFullScreenPromptToolkitShell,
    LegacyFullScreenShellConfig,
)


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
        self.thread_teams: list[dict[str, Any]] = []
        self.settings = {
            "llm_provider": "openai",
            "llm_model": "gpt-global",
            "llm_fast_model": "gpt-fast",
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
        self.env_entries = [
            {"name": "openai_api_key", "is_set": False},
            {"name": "anthropic_api_key", "is_set": False},
            {"name": "anthropic_direct_api_key", "is_set": False},
            {"name": "openrouter_api_key", "is_set": False},
        ]

    async def list_threads(self, user_id: str = "default") -> list[dict[str, Any]]:
        self.calls.append(("list_threads", {"user_id": user_id}))
        return copy.deepcopy(self.threads)

    async def list_thread_teams(self, user_id: str = "default") -> list[dict[str, Any]]:
        self.calls.append(("list_thread_teams", {"user_id": user_id}))
        return copy.deepcopy(self.thread_teams)

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
            "platform": "cli",
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
        for entry in self.env_entries:
            if entry["name"] in kwargs and kwargs[entry["name"]]:
                entry["is_set"] = True
        return {"updated": list(kwargs), "restart_required": False}

    async def get_env_vars(self, *, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_env_vars", {"user_id": user_id}))
        return {"entries": copy.deepcopy(self.env_entries)}

    async def get_env_var(
        self,
        key: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("get_env_var", {"key": key, "user_id": user_id}))
        values = {
            "openai_api_key": "sk-server-openai",
            "anthropic_api_key": "sk-server-anthropic",
            "anthropic_direct_api_key": "sk-server-anthropic-direct",
            "openrouter_api_key": "sk-server-openrouter",
        }
        return {"name": key, "value": values.get(key)}

    async def test_llm_provider_config(
        self,
        request: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "test_llm_provider_config",
                {"request": copy.deepcopy(request), "user_id": user_id},
            )
        )
        return {
            "ok": bool(request.get("api_key")),
            "provider": request.get("llm_provider"),
            "model": request.get("llm_model"),
            "message": "Provider test succeeded.",
        }

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
    fast.register(registry)
    smart.register(registry)
    provider.register(registry)
    usage.register(registry)
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


def test_thread_resolver_matches_ids_titles_substrings_and_ambiguity() -> None:
    thread_list = [
        {"thread_id": "alpha-111", "title": "Quarterly Planning"},
        {"thread_id": "alpha-222", "title": "Quarterly Review"},
        {"thread_id": "bravo-333", "title": "Supplier Followup"},
    ]

    assert threads.resolve_thread_reference(thread_list, "bravo-333").thread == thread_list[2]
    assert threads.resolve_thread_reference(thread_list, "bravo").thread == thread_list[2]
    assert (
        threads.resolve_thread_reference(thread_list, "Quarterly Planning").thread
        == thread_list[0]
    )
    assert threads.resolve_thread_reference(thread_list, "supplier").thread == thread_list[2]
    assert threads.resolve_thread_reference(thread_list, "alpha").status == "ambiguous"
    assert threads.resolve_thread_reference(thread_list, "missing").status == "missing"


def test_thread_list_formats_backend_teams_before_ungrouped_threads() -> None:
    thread_list = [
        {
            "thread_id": "recent",
            "title": "Recent unpinned",
            "pinned": False,
            "updated_at": "2026-05-10T12:00:00Z",
            "platform": "desktop",
        },
        {
            "thread_id": "pinned",
            "title": "Pinned ungrouped",
            "pinned": True,
            "updated_at": "2026-05-10T10:00:00Z",
            "platform": "desktop",
        },
        {
            "thread_id": "team-a",
            "title": "Team A",
            "pinned": False,
            "updated_at": "2026-05-10T09:00:00Z",
            "platform": "callable",
        },
    ]
    lines = threads._format_thread_list(
        thread_list,
        active_thread_id="recent",
        teams=[{"id": "ops", "name": "Ops", "thread_ids": ["team-a"]}],
    )

    team_index = lines.index("  Team: Ops")
    pinned_index = lines.index("  Pinned")
    recent_index = lines.index("  Recent")
    assert team_index < pinned_index < recent_index
    assert any("team-a" in line and "Team A" in line for line in lines)
    assert any("pinned" in line and "Pinned ungrouped" in line for line in lines)


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
    assert run(registry.dispatch_async(ctx, "/usage session")).ok is True
    session_extra = run(registry.dispatch_async(ctx, "/usage session extra"))
    assert run(registry.dispatch_async(ctx, "/thread compact")).ok is True

    assert session_extra.ok is False
    assert session_extra.error_code == "usage_error"
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
    assert any("gpt-new" in message.content for message in sink.messages)
    assert any("hi there" in message.content for message in sink.messages)


def test_core_commands_emit_json_payloads(capsys: Any) -> None:
    client = CoreFakeClient()
    registry = make_registry()
    ctx = make_context(client)

    assert run(registry.dispatch_async(ctx, "/thread list --json")).ok is True
    threads_payload = json.loads(capsys.readouterr().out)
    assert isinstance(threads_payload, list)
    assert {thread["thread_id"] for thread in threads_payload} == {
        "thread-1",
        "thread-2",
    }

    assert run(registry.dispatch_async(ctx, "/settings --json")).ok is True
    settings_payload = json.loads(capsys.readouterr().out)
    assert settings_payload["llm_model"] == "gpt-global"

    assert run(registry.dispatch_async(ctx, "/context --json")).ok is True
    context_payload = json.loads(capsys.readouterr().out)
    assert context_payload["thread_id"] == "thread-1"
    assert context_payload["total_tokens"] == 120

    assert run(registry.dispatch_async(ctx, "/usage --json")).ok is True
    usage_payload = json.loads(capsys.readouterr().out)
    assert usage_payload["model"] == "gpt-thread"
    assert usage_payload["total_tokens"] == 120


def test_fast_command_toggles_models_and_sets_fast_model() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    actions: list[Any] = []
    ctx = make_context(client, actions=actions)

    on_result = run(registry.dispatch_async(ctx, "/fast on"))
    off_result = run(registry.dispatch_async(ctx, "/fast off"))
    set_result = run(registry.dispatch_async(ctx, "/fast set gpt-tiny"))

    assert on_result.ok is True
    assert off_result.ok is True
    assert set_result.ok is True
    assert (
        "update_thread_config",
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "llm_config": {"provider": "openai", "model": "gpt-fast"},
        },
    ) in client.calls
    assert (
        "update_thread_config",
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "llm_config": {"provider": "openai", "model": "gpt-global"},
        },
    ) in client.calls
    assert ("update_settings", {"user_id": "alice", "llm_fast_model": "gpt-tiny"}) in (
        client.calls
    )
    assert actions[:2] == [
        {"type": "set_model", "model": "gpt-fast", "fast_mode": True},
        {"type": "set_model", "model": "gpt-global", "fast_mode": False},
    ]


def test_smart_command_toggles_to_primary_when_unset_and_sets_smart_model() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    actions: list[Any] = []
    ctx = make_context(client, actions=actions)

    # llm_smart_model is unset on the fake client, so "smart" resolves to the
    # primary model (gpt-global). Setting a smart model writes llm_smart_model.
    on_result = run(registry.dispatch_async(ctx, "/smart on"))
    set_result = run(registry.dispatch_async(ctx, "/smart set gpt-pro"))

    assert on_result.ok is True
    assert set_result.ok is True
    assert (
        "update_thread_config",
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "llm_config": {"provider": "openai", "model": "gpt-global"},
        },
    ) in client.calls
    assert (
        "update_settings",
        {"user_id": "alice", "llm_smart_model": "gpt-pro"},
    ) in client.calls


def test_smart_prompt_returns_one_turn_payload() -> None:
    client = CoreFakeClient()
    client.settings["llm_smart_model"] = "gpt-pro"
    registry = make_registry()
    actions: list[Any] = []
    ctx = make_context(client, actions=actions)

    result = run(registry.dispatch_async(ctx, "/smart think hard about this"))

    assert result.ok is True
    assert result.payload["fast_prompt"] == "think hard about this"
    assert result.payload["fast_model"] == "gpt-pro"
    assert not any(name == "update_thread_config" for name, _payload in client.calls)


def test_fast_prompt_returns_one_turn_payload_without_persistent_toggle() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    actions: list[Any] = []
    ctx = make_context(client, actions=actions)

    result = run(registry.dispatch_async(ctx, "/fast summarize this briefly"))

    assert result.ok is True
    assert result.payload["fast_prompt"] == "summarize this briefly"
    assert result.payload["fast_model"] == "gpt-fast"
    assert not any(name == "update_thread_config" for name, _payload in client.calls)
    assert actions == []


def test_fast_prompt_allows_control_words_inside_prompt() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    actions: list[Any] = []
    ctx = make_context(client, actions=actions)

    result = run(registry.dispatch_async(ctx, "/fast on the topic of status reports"))

    assert result.ok is True
    assert result.payload["fast_prompt"] == "on the topic of status reports"
    assert result.payload["fast_model"] == "gpt-fast"
    assert not any(name == "update_thread_config" for name, _payload in client.calls)
    assert actions == []


def test_provider_command_saves_applies_lists_and_tests_credentials(tmp_path) -> None:
    client = CoreFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    ctx = make_context(client, output=sink)
    credentials_path = tmp_path / ".nymeria" / "credentials.json"
    ctx.metadata["provider_credentials_path"] = credentials_path

    set_result = run(
        registry.dispatch_async(ctx, "/provider set openai api_key=sk-local-openai")
    )
    list_result = run(registry.dispatch_async(ctx, "/provider list"))
    test_result = run(registry.dispatch_async(ctx, "/provider test openai"))
    switch_result = run(registry.dispatch_async(ctx, "/provider switch openai"))

    assert set_result.ok is True
    assert list_result.ok is True
    assert test_result.ok is True
    assert switch_result.ok is True
    assert credentials_path.exists()
    assert credentials_path.stat().st_mode & 0o777 == 0o600
    data = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert data["providers"]["openai"]["api_key"] == "sk-local-openai"
    assert not any("sk-local-openai" in message.content for message in sink.messages)
    assert (
        "update_settings",
        {"user_id": "alice", "openai_api_key": "sk-local-openai"},
    ) in client.calls
    assert (
        "update_settings",
        {
            "user_id": "alice",
            "llm_provider": "openai",
            "openai_api_key": "sk-local-openai",
        },
    ) in client.calls
    test_calls = [
        payload
        for name, payload in client.calls
        if name == "test_llm_provider_config"
    ]
    assert test_calls[0]["request"]["api_key"] == "sk-local-openai"


def test_provider_list_emits_json_without_secrets(tmp_path, capsys: Any) -> None:
    client = CoreFakeClient()
    registry = make_registry()
    ctx = make_context(client)
    ctx.metadata["provider_credentials_path"] = tmp_path / "credentials.json"

    assert run(
        registry.dispatch_async(ctx, "/provider set anthropic api_key=sk-secret")
    ).ok is True
    assert run(registry.dispatch_async(ctx, "/provider list --json")).ok is True

    payload = json.loads(capsys.readouterr().out)
    # /provider list now consumes the full registry (Phase 1 tier refactor),
    # so the JSON payload covers every known provider, not just the three
    # Nymeria can manage credentials for. Verify the credential-storing ones
    # are still present and tagged correctly, then assert the new tier/notes
    # fields flow through, and that no secrets leak.
    providers = {entry["provider"] for entry in payload}
    assert {"anthropic", "openai", "openrouter"}.issubset(providers)
    assert "sk-secret" not in json.dumps(payload)
    anthropic = next(entry for entry in payload if entry["provider"] == "anthropic")
    assert anthropic["status"] == "authenticated"
    assert anthropic["tier"] == "native"
    # An unverified provider should be present with status "n/a" (Nymeria
    # doesn't manage its credentials locally).
    deepseek = next(entry for entry in payload if entry["provider"] == "deepseek")
    assert deepseek["tier"] == "unverified"
    assert deepseek["status"] == "n/a"


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
    shell = LegacyFullScreenPromptToolkitShell(
        client=client,
        capabilities=FakeTerminalCapabilities(width=100),
        config=LegacyFullScreenShellConfig(
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


def test_format_settings_view_renders_compact_trigger_by_mode() -> None:
    base = {
        "llm_provider": "openai",
        "llm_model": "gpt-global",
        "compact_threshold": 0.8,
        "compact_threshold_mode": "percentage",
        "compact_threshold_tokens": 200_000,
    }

    percent_view = system.format_settings_view(base)
    tokens_view = system.format_settings_view(
        {**base, "compact_threshold_mode": "tokens"}
    )

    assert "Compact at" in percent_view
    assert "80%" in percent_view
    assert "200000 tokens" in tokens_view
    assert "80%" not in tokens_view


def test_format_thread_usage_compact_cap_honors_trigger_tokens() -> None:
    stats = {
        "model": "gpt-thread",
        "input_tokens": 80,
        "output_tokens": 40,
        "total_tokens": 120_000,
        "context_limit": 400_000,
        "usage_percentage": 30,
    }

    tokens_view = usage._format_thread_usage(stats, compact_trigger=200_000)
    unscaled_view = usage._format_thread_usage(stats, compact_trigger=400_000)

    assert "Until compact" in tokens_view
    assert "of 200.0k" in tokens_view
    assert "Until compact" not in unscaled_view
