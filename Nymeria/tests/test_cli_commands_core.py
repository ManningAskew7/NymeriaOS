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
from nymeria.triggers.cli.commands import conversation, model, system
from nymeria.triggers.cli.state.model import AssistantMessage, CLIUIState, UserMessage


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
    model.register(registry)
    return registry


def make_context(
    client: CoreFakeClient,
    *,
    output: ListCommandOutputSink | None = None,
    actions: list[Any] | None = None,
) -> CommandContext:
    return CommandContext(
        client=client,
        output=output or ListCommandOutputSink(),
        dispatch_state=(actions.append if actions is not None else None),
        thread_id="thread-1",
        user_id="alice",
    )


def test_model_history_and_context_commands_use_client() -> None:
    client = CoreFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    ctx = make_context(client, output=sink)

    assert run(registry.dispatch_async(ctx, "/model show")).ok is True
    assert run(registry.dispatch_async(ctx, "/model set gpt-new")).ok is True
    assert run(registry.dispatch_async(ctx, "/model available openai")).ok is True
    assert run(registry.dispatch_async(ctx, "/history --internal 5")).ok is True
    assert run(registry.dispatch_async(ctx, "/context")).ok is True

    call_names = [name for name, _payload in client.calls]
    assert "update_thread_config" in call_names
    assert "list_available_models" in call_names
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

    assert run(registry.dispatch_async(ctx, "/context --json")).ok is True
    context_payload = json.loads(capsys.readouterr().out)
    assert context_payload["thread_id"] == "thread-1"
    assert context_payload["total_tokens"] == 120


def test_cli_context_shows_last_compaction_in_the_user_timezone(monkeypatch: Any) -> None:
    """The CLI printed ``last_compaction`` straight off the wire.

    The backend now stamps it as aware UTC (it used to be a naive local clock,
    fixed 2026-08-26), so the terminal showed a raw ISO instant in a zone that
    is not the reader's. The backend /usage renders the same field through
    format_user_time_compact; this is the CLI half of that sweep.
    """
    import zoneinfo

    monkeypatch.setattr(
        "nymeria.core.time_utils.get_user_tz",
        lambda: zoneinfo.ZoneInfo("Australia/Sydney"),
    )

    client = CoreFakeClient()
    client.context_stats["last_compaction"] = "2026-05-18T10:00:00+00:00"
    registry = make_registry()
    sink = ListCommandOutputSink()
    ctx = make_context(client, output=sink)

    assert run(registry.dispatch_async(ctx, "/context")).ok is True
    rendered = "\n".join(message.content for message in sink.messages)

    assert "2026-05-18 20:00 AEST" in rendered
    assert "2026-05-18T10:00" not in rendered


def test_cli_context_still_says_never_without_a_compaction() -> None:
    client = CoreFakeClient()  # fixture leaves last_compaction None
    registry = make_registry()
    sink = ListCommandOutputSink()
    ctx = make_context(client, output=sink)

    assert run(registry.dispatch_async(ctx, "/context")).ok is True
    rendered = "\n".join(message.content for message in sink.messages)
    assert "Never" in rendered


# ---------------------------------------------------------------------------
# /undo confirmation contract (#328)
#
# `--yes` is the WHOLE confirmation contract for the CLI now that the dead
# `confirm_handler` seam is gone, and /undo is the only CLI-local command that
# still gates on it (every other former gate resolves to a backend proxy
# handler). These tests pin both arms so a regression in the flag plumbing is
# red rather than silently destructive.
# ---------------------------------------------------------------------------


class UndoFakeClient:
    connection_label = "api http://test"

    def __init__(self) -> None:
        self.rewinds: list[tuple[str, int, str]] = []

    async def rewind_thread(self, thread_id: str, *, steps: int, user_id: str) -> dict[str, Any]:
        self.rewinds.append((thread_id, steps, user_id))
        return {"ok": True}


def _undo_context(
    client: UndoFakeClient,
    *,
    actions: list[Any] | None = None,
) -> CommandContext:
    ui_state = CLIUIState(
        thread_id="thread-1",
        user_id="alice",
        messages=(
            UserMessage(id="u1", content="hello"),
            AssistantMessage(id="a1", content="hi", status="complete"),
        ),
    )
    return CommandContext(
        client=client,
        output=ListCommandOutputSink(),
        dispatch_state=(actions.append if actions is not None else None),
        thread_id="thread-1",
        user_id="alice",
        metadata={"ui_state": ui_state},
    )


def _undo_registry() -> CommandRegistry:
    registry = CommandRegistry()
    conversation.register(registry)
    return registry


def test_undo_without_the_flag_refuses_and_rewinds_nothing() -> None:
    client = UndoFakeClient()
    actions: list[Any] = []
    result = run(_undo_registry().dispatch_async(_undo_context(client, actions=actions), "/undo"))

    assert result.ok is True
    assert "--yes" in "\n".join(message.content for message in result.messages)
    # The refusal must be inert: no rewind call, no transcript mutation.
    assert client.rewinds == []
    assert actions == []


def test_undo_with_the_flag_rewinds_one_step_and_drops_the_exchange() -> None:
    client = UndoFakeClient()
    actions: list[Any] = []
    result = run(
        _undo_registry().dispatch_async(_undo_context(client, actions=actions), "/undo --yes")
    )

    assert result.ok is True
    assert client.rewinds == [("thread-1", 1, "alice")]
    assert actions == [{"type": "undo_last_exchange"}]


def test_undo_accepts_the_short_flag_and_rejects_anything_else() -> None:
    client = UndoFakeClient()
    assert run(_undo_registry().dispatch_async(_undo_context(client), "/undo -y")).ok is True
    assert client.rewinds == [("thread-1", 1, "alice")]

    stray = run(_undo_registry().dispatch_async(_undo_context(client), "/undo now"))
    assert stray.ok is False
    assert stray.error_code == "usage_error"
    # A stray token must not be read as consent.
    assert client.rewinds == [("thread-1", 1, "alice")]


def test_undo_with_nothing_to_undo_never_reaches_the_backend() -> None:
    """The empty-transcript guard is what stops a confirmed /undo going destructive.

    `--yes` satisfies the confirmation, so this guard is the ONLY thing between a
    fresh thread and a `rewind_thread` call that would drop an exchange the local
    transcript does not know about.
    """
    client = UndoFakeClient()
    for messages in (
        (),                                            # nothing at all
        (UserMessage(id="u1", content="hello"),),      # a prompt with no reply yet
    ):
        ctx = CommandContext(
            client=client,
            output=ListCommandOutputSink(),
            thread_id="thread-1",
            user_id="alice",
            metadata={"ui_state": CLIUIState(thread_id="thread-1", messages=messages)},
        )
        result = run(_undo_registry().dispatch_async(ctx, "/undo --yes"))
        assert result.ok is False
        assert "No exchange to undo." in " ".join(m.content for m in result.messages)
    assert client.rewinds == []


def test_undo_without_a_transcript_never_reaches_the_backend() -> None:
    client = UndoFakeClient()
    ctx = CommandContext(
        client=client,
        output=ListCommandOutputSink(),
        thread_id="thread-1",
        user_id="alice",
        metadata={},
    )
    result = run(_undo_registry().dispatch_async(ctx, "/undo --yes"))
    assert result.ok is False
    assert client.rewinds == []


def test_undo_is_not_shadowed_by_the_backend_catalog() -> None:
    """#328/#329: /undo's confirmation only matters while /undo is REACHABLE.

    The registry registers the backend catalog LAST, so any name the backend also
    declares wins and the local handler goes dead, which is exactly what already
    happened to the other four CLI confirm sites. If `registry_defaults.py` ever
    declares `undo`, the tests above keep passing against a handler nobody runs.
    """
    from nymeria.triggers.cli.commands import backend as backend_commands

    registry = CommandRegistry()
    conversation.register(registry)
    backend_commands.register(registry)

    resolved = registry.get("undo")
    assert resolved is not None
    assert resolved.handler is conversation._handle_undo
