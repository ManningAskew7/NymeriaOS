from __future__ import annotations

import copy
import json
from typing import Any

from cli_fixtures import run

from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import context as context_commands
from nymeria.triggers.cli.commands import model, provider, system


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
    model.register(registry)
    provider.register(registry)
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


def test_model_settings_history_and_context_commands_use_client() -> None:
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

    call_names = [name for name, _payload in client.calls]
    assert "update_thread_config" in call_names
    assert "list_available_models" in call_names
    assert ("update_settings", {"user_id": "alice", "llm_temperature": 0.4}) in client.calls
    assert ("get_history", {
        "thread_id": "thread-1",
        "user_id": "alice",
        "include_internal": True,
    }) in client.calls
    assert any("gpt-new" in message.content for message in sink.messages)
    assert any("hi there" in message.content for message in sink.messages)


def test_core_commands_emit_json_payloads(capsys: Any) -> None:
    client = CoreFakeClient()
    registry = make_registry()
    ctx = make_context(client)

    assert run(registry.dispatch_async(ctx, "/settings --json")).ok is True
    settings_payload = json.loads(capsys.readouterr().out)
    assert settings_payload["llm_model"] == "gpt-global"

    assert run(registry.dispatch_async(ctx, "/context --json")).ok is True
    context_payload = json.loads(capsys.readouterr().out)
    assert context_payload["thread_id"] == "thread-1"
    assert context_payload["total_tokens"] == 120


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
