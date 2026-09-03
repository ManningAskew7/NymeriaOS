from __future__ import annotations

import copy
from typing import Any

from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import (
    account,
    activity,
    artifacts,
    doctor,
    memory,
    triggers,
)
from nymeria.triggers.cli.events import (
    ResponseEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    WorkspaceArtifactEvent,
)
from nymeria.triggers.cli.state import (
    create_initial_state,
    reduce_events,
    start_turn,
)


class PersonalFakeClient:
    connection_label = "api http://test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.memories = [
            {"key": "city", "value": "Tulsa", "access_count": 2},
            {"key": "color", "value": "green", "access_count": 1},
        ]
        self.tokens = [
            {
                "token_hash_prefix": "abcd1234",
                "label": "desktop",
                "created_at": "2026-05-10T00:00:00Z",
                "last_used_at": None,
                "revoked_at": None,
            }
        ]
        self.platforms = [
            {
                "provider": "telegram",
                "provider_user_id": "42",
                "created_at": "2026-05-10T00:00:00Z",
            }
        ]
        self.triggers = [
            {
                "id": "trig-1",
                "name": "Morning webhook",
                "source_type": "webhook",
                "enabled": True,
                "health_status": "healthy",
                "thread_id": "thread-1",
            }
        ]

    async def list_memories(self, user_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_memories", {"user_id": user_id}))
        return copy.deepcopy(self.memories)

    async def search_memories(self, user_id: str, query: str) -> list[dict[str, Any]]:
        self.calls.append(("search_memories", {"user_id": user_id, "query": query}))
        return [item for item in copy.deepcopy(self.memories) if query in item["value"]]

    async def save_memory(self, user_id: str, key: str, value: str) -> dict[str, Any]:
        self.calls.append(("save_memory", {"user_id": user_id, "key": key, "value": value}))
        return {"key": key, "value": value}

    async def forget_memory(self, user_id: str, key: str) -> dict[str, Any]:
        self.calls.append(("forget_memory", {"user_id": user_id, "key": key}))
        return {"forgot": key}

    async def get_me(self, act_as: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_me", {"act_as": act_as}))
        user_id = act_as or "alice"
        return {
            "id": user_id,
            "email": f"{user_id}@example.com",
            "display_name": user_id.title(),
            "role": "admin" if user_id == "alice" else "user",
        }

    async def list_my_tokens(self, user_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_my_tokens", {"user_id": user_id}))
        return copy.deepcopy(self.tokens)

    async def issue_my_token(
        self,
        label: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("issue_my_token", {"label": label, "user_id": user_id}))
        return {
            "raw_token": "nym_raw_once",
            "metadata": {"token_hash_prefix": "efgh5678", "label": label},
        }

    async def revoke_my_token(
        self,
        token_hash_prefix: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "revoke_my_token",
                {"token_hash_prefix": token_hash_prefix, "user_id": user_id},
            )
        )
        return {"revoked": True}

    async def list_my_platforms(self, user_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_my_platforms", {"user_id": user_id}))
        return copy.deepcopy(self.platforms)

    async def list_triggers(
        self,
        user_id: str,
        *,
        enabled_only: bool = False,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "list_triggers",
                {
                    "user_id": user_id,
                    "enabled_only": enabled_only,
                    "thread_id": thread_id,
                },
            )
        )
        return copy.deepcopy(self.triggers)

    async def create_trigger(
        self,
        request: dict[str, Any],
        user_id: str = "default",
    ) -> dict[str, Any]:
        self.calls.append(("create_trigger", {"request": request, "user_id": user_id}))
        created = {"id": "trig-new", **request, "health_status": "healthy"}
        self.triggers.append(created)
        return copy.deepcopy(created)

    async def update_trigger(
        self,
        trigger_id: str,
        request: dict[str, Any],
        user_id: str = "default",
    ) -> dict[str, Any]:
        self.calls.append(
            ("update_trigger", {"trigger_id": trigger_id, "request": request, "user_id": user_id})
        )
        return {"id": trigger_id, "name": "Updated", **request}

    async def delete_trigger(self, trigger_id: str, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("delete_trigger", {"trigger_id": trigger_id, "user_id": user_id}))
        return {"deleted": True}

    async def test_trigger(self, trigger_id: str, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("test_trigger", {"trigger_id": trigger_id, "user_id": user_id}))
        return {
            "action_type": "agent_prompt",
            "conditions_pass": True,
            "rendered_output": "Trigger fired",
        }

    async def get_recent_trigger_executions(
        self,
        user_id: str = "default",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.calls.append(("get_recent_trigger_executions", {"user_id": user_id, "limit": limit}))
        return [
            {
                "trigger_id": "trig-1",
                "timestamp": "2026-05-10T00:00:00Z",
                "status": "success",
                "events_summary": "ok",
            }
        ]

    async def get_trigger_executions(
        self,
        trigger_id: str,
        user_id: str = "default",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "get_trigger_executions",
                {"trigger_id": trigger_id, "user_id": user_id, "limit": limit},
            )
        )
        return await self.get_recent_trigger_executions(user_id, limit)

    async def get_activity(
        self,
        user_id: str,
        *,
        limit: int = 50,
        activity_type: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "get_activity",
                {
                    "user_id": user_id,
                    "limit": limit,
                    "activity_type": activity_type,
                    "thread_id": thread_id,
                },
            )
        )
        return {
            "entries": [
                {
                    "id": "act-1",
                    "timestamp": "2026-05-10T00:00:00Z",
                    "type": activity_type or "task_completed",
                    "message": "Completed work",
                    "thread_id": thread_id or "thread-1",
                }
            ],
            "total": 1,
        }

    async def get_notifications(self, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("get_notifications", {"user_id": user_id}))
        return {
            "notifications": [
                {
                    "id": "note-1",
                    "summary": "Check this",
                    "created_at": "2026-05-10T00:00:00Z",
                    "read": False,
                }
            ],
            "unread_count": 1,
        }

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
        return {"messages": []}

    async def download_workspace_artifact(
        self,
        path: str,
        user_id: str | None = None,
    ) -> tuple[bytes, str, str]:
        self.calls.append(("download_workspace_artifact", {"path": path, "user_id": user_id}))
        return b"hello", path.rsplit("/", 1)[-1], "text/plain"

    async def health(self) -> bool:
        self.calls.append(("health", {}))
        return True

    async def get_llm_runtime_diagnostics(self, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_llm_runtime_diagnostics", {"user_id": user_id}))
        return {
            "provider": "openai",
            "model": "gpt-test",
            "mode": "responses",
            "base_url": "https://api.openai.com",
            "status": "available",
        }


def make_registry() -> CommandRegistry:
    registry = CommandRegistry()
    memory.register(registry)
    account.register(registry)
    triggers.register(registry)
    activity.register(registry)
    artifacts.register(registry)
    doctor.register(registry)
    return registry


def make_state():
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=1.0)
    state = start_turn(state, "make report", thread_id="thread-1", user_id="alice", now=2.0)
    return reduce_events(
        state,
        (
            ThinkingEvent(content="private chain"),
            ToolCallEvent(id="tool-1", name="file_write", args={"path": "/workspace/report.txt"}),
            WorkspaceArtifactEvent(
                tool_call_id="tool-1",
                path="/workspace/report.txt",
                artifact={
                    "path": "/workspace/report.txt",
                    "name": "report.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 5,
                },
            ),
            ToolResultEvent(id="tool-1", name="file_write", result="wrote report"),
            ResponseEvent(content="done"),
        ),
        now=3.0,
    )


def make_context(
    client: PersonalFakeClient,
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
        metadata={
            "ui_state": make_state(),
            "capabilities": FakeTerminalCapabilities(width=100),
        },
    )


def test_memory_commands_use_api_client_methods() -> None:
    # The local /todo family that used to share this test was retired by
    # #143: the whole todo surface is backend-owned now and covered by the
    # command-service tests.
    client = PersonalFakeClient()
    registry = make_registry()

    confirmed = make_context(client, output=ListCommandOutputSink())
    assert run(registry.dispatch_async(confirmed, "/memory list")).ok is True
    assert run(registry.dispatch_async(confirmed, "/memory search Tulsa")).ok is True
    assert run(registry.dispatch_async(confirmed, "/memory save timezone UTC")).ok is True
    # No `/memory forget`: backlog #131 renamed it to `/memory delete` and
    # retired the local declaration, so deletion is a backend command now.
    assert not any(name == "forget_memory" for name, _payload in client.calls)

    assert ("search_memories", {"user_id": "alice", "query": "Tulsa"}) in client.calls
    assert ("save_memory", {"user_id": "alice", "key": "timezone", "value": "UTC"}) in client.calls


def test_account_trigger_activity_artifact_details_and_doctor_commands() -> None:
    client = PersonalFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    actions: list[Any] = []
    ctx = make_context(client, output=sink, actions=actions)

    # Bare `/account` is the local identity readout: backlog #131 renamed the
    # backend command to `/account show` and retired the local `current`
    # declaration, leaving the root as the client-side fallback.
    assert run(registry.dispatch_async(ctx, "/account")).ok is True
    assert run(registry.dispatch_async(ctx, "/account tokens")).ok is True
    assert run(registry.dispatch_async(ctx, "/account tokens issue test-token")).ok is True
    revoke_result = run(registry.dispatch_async(ctx, "/account tokens revoke abcd1234"))
    assert revoke_result.ok is True
    assert sink.messages[-1].level == "warning"

    # `--yes` is the ONLY way past a CLI confirmation now (#328): the prompt
    # seam it used to share with `confirm_handler` was never wired outside tests.
    confirmed = make_context(client, output=sink, actions=actions)
    assert run(
        registry.dispatch_async(confirmed, "/account tokens revoke abcd1234 --yes")
    ).ok is True
    # The refusal path also returns ok=True (it is a completed warning), so the
    # only thing that separates confirmed from refused is the client call.
    assert any(name == "revoke_my_token" for name, _ in client.calls)
    assert run(registry.dispatch_async(confirmed, "/account switch bob")).ok is True
    assert run(registry.dispatch_async(confirmed, "/account platforms")).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers list --enabled")).ok is True
    assert run(
        registry.dispatch_async(
            confirmed,
            "/triggers create Morning --secret shh --prompt Hello --thread current",
        )
    ).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers disable trig-1")).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers enable trig-1")).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers edit trig-1 name=Renamed")).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers history trig-1 5")).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers test trig-1")).ok is True
    assert run(registry.dispatch_async(confirmed, "/triggers delete trig-1 --yes")).ok is True
    # `/activity recent` retired with backlog #131 (the backend aliases it to
    # `/activity list`); the local root keeps the filters for the fallback.
    assert run(registry.dispatch_async(confirmed, "/activity 5 --thread current")).ok is True
    assert run(registry.dispatch_async(confirmed, "/activity notifications")).ok is True
    # `/artifacts recent` is now a backend command (server-state listing); the
    # client-side `open`/`download` halves stay local and resolve against the
    # terminal's own recent list.
    assert run(registry.dispatch_async(confirmed, "/artifacts open 1")).ok is True
    assert run(registry.dispatch_async(confirmed, "/artifacts download 1")).ok is True
    assert run(registry.dispatch_async(confirmed, "/details tool tool-1")).ok is True
    assert run(registry.dispatch_async(confirmed, "/doctor terminal")).ok is True
    assert run(registry.dispatch_async(confirmed, "/doctor api")).ok is True
    assert run(registry.dispatch_async(confirmed, "/doctor auth")).ok is True
    assert run(registry.dispatch_async(confirmed, "/doctor model")).ok is True

    assert any("Raw token (shown once): nym_raw_once" in msg.content for msg in sink.messages)
    assert {"type": "switch_user", "user_id": "bob"} in actions
    assert (
        "create_trigger",
        {
            "request": {
                "name": "Morning",
                "source_type": "webhook",
                "action_type": "agent_prompt",
                "source_config": {"secret": "shh"},
                "action_config": {"prompt_template": "Hello"},
                "conditions": [],
                "cooldown_seconds": 0,
                "enabled": True,
                "thread_id": "thread-1",
            },
            "user_id": "bob",
        },
    ) in client.calls
    assert ("delete_trigger", {"trigger_id": "trig-1", "user_id": "bob"}) in client.calls
    assert (
        "get_activity",
        {"user_id": "bob", "limit": 5, "activity_type": None, "thread_id": "thread-1"},
    ) in client.calls
    assert (
        "download_workspace_artifact",
        {"path": "/workspace/report.txt", "user_id": "bob"},
    ) in client.calls
    assert any("Tool Details: file_write" in msg.content for msg in sink.messages)


def test_trigger_list_and_history_reject_bad_filters_and_parse_limit_options() -> None:
    client = PersonalFakeClient()
    registry = make_registry()
    ctx = make_context(client)

    unknown_filter = run(registry.dispatch_async(ctx, "/triggers list --bogus"))
    missing_thread = run(registry.dispatch_async(ctx, "/triggers list --thread"))
    recent_history = run(registry.dispatch_async(ctx, "/triggers history --limit 5"))
    trigger_history = run(
        registry.dispatch_async(ctx, "/triggers history trig-1 --limit=7")
    )
    missing_limit = run(registry.dispatch_async(ctx, "/triggers history --limit"))

    assert unknown_filter.ok is False
    assert unknown_filter.error_code == "usage_error"
    assert missing_thread.ok is False
    assert missing_thread.error_code == "usage_error"
    assert recent_history.ok is True
    assert trigger_history.ok is True
    assert missing_limit.ok is False
    assert missing_limit.error_code == "usage_error"
    assert (
        "get_recent_trigger_executions",
        {"user_id": "alice", "limit": 5},
    ) in client.calls
    assert (
        "get_trigger_executions",
        {"trigger_id": "trig-1", "user_id": "alice", "limit": 7},
    ) in client.calls
