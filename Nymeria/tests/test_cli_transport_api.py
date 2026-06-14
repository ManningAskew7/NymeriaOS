from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest

from nymeria.triggers.cli.app import CLIRuntimeConfig
from nymeria.triggers.cli.credentials import CLIConnectionProfile
from nymeria.triggers.cli.events import DoneEvent, ErrorEvent, ResponseEvent
from nymeria.triggers.cli.transport.api import (
    APIAgentClient,
    APITransportStartupError,
    attempt_saved_reconnect,
    resolve_api_connection_config,
    select_agent_client,
    suggest_reachable_backend,
)
from nymeria.triggers.cli.transport.disconnected import DisconnectedAgentClient


def run(coro):
    return asyncio.run(coro)


@dataclass(slots=True)
class FakeLocalClient:
    connection_label: str = "local agent"


class FakeAPIClient:
    instances: list["FakeAPIClient"] = []
    health_result = True
    me_result: dict[str, Any] | Exception = {"id": "default", "role": "user"}
    stream_exception: Exception | None = None

    def __init__(self, *, base_url: str = "http://api", api_key: str = "secret") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.stream_events = [
            {"type": "response", "content": "hi"},
            {"type": "done", "tool_call_count": 0},
        ]
        self.autonomous_events = [
            {"type": "task_started", "thread_id": "thread-a", "task_id": "todo-1"},
            {"type": "response", "thread_id": "thread-a", "content": "done"},
            {"type": "task_completed", "thread_id": "thread-a", "task_id": "todo-1"},
        ]
        self.threads = [{"thread_id": "thread-a", "title": "Thread A"}]
        self.history = {"thread_id": "thread-a", "messages": []}
        self.context = {"thread_id": "thread-a", "total_tokens": 12}
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.health_result = True
        cls.me_result = {"id": "default", "role": "user"}
        cls.stream_exception = None

    async def chat_stream(self, **kwargs: Any):
        self.calls.append(("chat_stream", kwargs))
        if self.stream_exception is not None:
            raise self.stream_exception
        for event in self.stream_events:
            yield event

    async def autonomous_stream(self, **kwargs: Any):
        self.calls.append(("autonomous_stream", kwargs))
        for event in self.autonomous_events:
            yield event

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("stop", {"thread_id": thread_id, "user_id": user_id}))
        return {"ok": True, "thread_id": thread_id, "user_id": user_id}

    async def get_history(
        self,
        thread_id: str,
        include_internal: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "get_history",
                {
                    "thread_id": thread_id,
                    "include_internal": include_internal,
                    "user_id": user_id,
                },
            )
        )
        return self.history

    async def list_threads(self, user_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_threads", {"user_id": user_id}))
        return self.threads

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_context_stats", {"thread_id": thread_id, "user_id": user_id})
        )
        return self.context

    async def claim_thread(
        self,
        thread_id: str,
        user_id: str,
        *,
        title: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "claim_thread",
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "title": title,
                    "platform": platform,
                },
            )
        )
        return {
            "thread_id": thread_id,
            "owner": user_id,
            "title": title or "New Chat",
            "platform": platform or "desktop",
        }

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str,
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
        return {"thread_id": thread_id, "title": title, "pinned": pinned}

    async def delete_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("delete_thread", {"thread_id": thread_id, "user_id": user_id}))
        return {"ok": True, "thread_id": thread_id}

    async def health(self) -> bool:
        self.calls.append(("health", {}))
        return self.health_result

    async def get_me(self, act_as: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_me", {"act_as": act_as}))
        if isinstance(self.me_result, Exception):
            raise self.me_result
        return self.me_result

    async def close(self) -> None:
        self.closed = True


def runtime_config(**overrides: Any) -> CLIRuntimeConfig:
    return replace(CLIRuntimeConfig(), **overrides)


def http_status_error(status_code: int, detail: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://api/me")
    response = httpx.Response(status_code, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(detail, request=request, response=response)


def test_resolve_api_connection_prefers_cli_flags_over_environment() -> None:
    config = resolve_api_connection_config(
        runtime_config(api_url=" http://cli ", api_key=" cli-token "),
        environ={
            "NYMERIA_API_URL": "http://env",
            "NYMERIA_SERVICE_TOKEN": "env-token",
        },
    )

    assert config.api_url == "http://cli"
    assert config.api_key == "cli-token"
    assert config.explicit_api_url is True
    assert config.explicit_api_key is True


def test_resolve_api_connection_uses_saved_profile_before_default() -> None:
    config = resolve_api_connection_config(
        runtime_config(),
        environ={},
        saved_profile=CLIConnectionProfile(
            api_url="http://saved",
            api_key="saved-token",
            user_id="alice",
        ),
    )

    assert config.api_url == "http://saved"
    assert config.api_key == "saved-token"
    assert config.user_id == "alice"
    assert config.api_url_source == "saved"
    assert config.api_key_source == "saved"
    assert config.user_id_source == "saved"


def test_api_transport_stream_chat_normalizes_sse_events_and_options() -> None:
    api = FakeAPIClient(base_url="http://api", api_key="secret")
    client = APIAgentClient(api, default_user_id="alice")

    async def collect():
        return [
            event
            async for event in client.stream_chat(
                "hello",
                "thread-a",
                attachments=[{"file_name": "note.txt"}],
                force_unsupported_attachments=True,
                is_self_invoke=True,
                trigger_override="manual-test",
            )
        ]

    events = run(collect())

    assert events == [
        ResponseEvent(thread_id="thread-a", content="hi"),
        DoneEvent(thread_id="thread-a", tool_call_count=0),
    ]
    assert client.connection_label == "api http://api"
    assert api.calls == [
        (
            "chat_stream",
            {
                "message": "hello",
                "thread_id": "thread-a",
                "user_id": "alice",
                "attachments": [{"file_name": "note.txt"}],
                "is_self_invoke": True,
                "trigger_override": "manual-test",
                "force_unsupported_attachments": True,
            },
        )
    ]


def test_api_transport_stream_autonomous_normalizes_sse_events() -> None:
    api = FakeAPIClient(base_url="http://api", api_key="secret")
    client = APIAgentClient(api, default_user_id="alice")

    async def collect():
        return [
            event
            async for event in client.stream_autonomous(
                user_id="default",
                client_id="cli-test",
            )
        ]

    events = run(collect())

    assert [event.type for event in events] == [
        "task_started",
        "response",
        "task_completed",
    ]
    assert [event.thread_id for event in events] == [
        "thread-a",
        "thread-a",
        "thread-a",
    ]
    assert api.calls == [
        (
            "autonomous_stream",
            {"user_id": "alice", "act_as": "alice", "client_id": "cli-test"},
        )
    ]


def test_api_transport_thread_operations_delegate_to_api_client() -> None:
    api = FakeAPIClient(base_url="http://api", api_key="secret")
    client = APIAgentClient(api, default_user_id="alice")

    async def query():
        stop = await client.stop("thread-a")
        history = await client.get_history("thread-a", include_internal=True)
        threads = await client.list_threads()
        context = await client.get_context_stats("thread-a")
        created = await client.create_thread(title="Draft")
        updated = await client.update_thread_metadata(
            "thread-a",
            title="Renamed",
            pinned=True,
        )
        deleted = await client.delete_thread("thread-a")
        return stop, history, threads, context, created, updated, deleted

    stop, history, threads, context, created, updated, deleted = run(query())

    assert stop["user_id"] == "alice"
    assert history == api.history
    assert threads == api.threads
    assert context == api.context
    assert created["title"] == "Draft"
    assert created["platform"] == "cli"
    assert updated == {"thread_id": "thread-a", "title": "Renamed", "pinned": True}
    assert deleted == {"ok": True, "thread_id": "thread-a"}
    assert [name for name, _ in api.calls] == [
        "stop",
        "get_history",
        "list_threads",
        "get_context_stats",
        "claim_thread",
        "update_thread_metadata",
        "delete_thread",
    ]


def test_api_transport_stream_auth_error_becomes_error_event() -> None:
    api = FakeAPIClient(base_url="http://api", api_key="bad")
    api.stream_exception = http_status_error(401, "Invalid API key")
    client = APIAgentClient(api, default_user_id="alice")

    async def collect():
        return [event async for event in client.stream_chat("hello", "thread-a")]

    events = run(collect())

    assert events == [
        ErrorEvent(
            thread_id="thread-a",
            content="API authentication failed: Invalid API key",
            code="api_auth_error",
            details={
                "api_url": "http://api",
                "error_type": "HTTPStatusError",
                "status_code": 401,
                "detail": "Invalid API key",
            },
        )
    ]


def test_select_agent_client_api_mode_uses_healthy_api_transport() -> None:
    FakeAPIClient.reset()
    config = runtime_config(
        transport="api",
        api_url="http://api",
        api_key="secret",
        user_id="alice",
    )

    selected = run(select_agent_client(config, api_client_factory=FakeAPIClient))

    assert isinstance(selected, APIAgentClient)
    assert selected.connection_label == "api http://api"
    api = FakeAPIClient.instances[0]
    assert api.calls == [("health", {}), ("get_me", {"act_as": "alice"})]
    assert api.closed is False


def test_select_agent_client_auto_does_not_fall_back_to_local_when_api_is_unhealthy() -> None:
    FakeAPIClient.reset()
    FakeAPIClient.health_result = False
    local = FakeLocalClient()
    config = runtime_config(
        transport="auto",
        api_url="http://api",
        api_key="secret",
        user_id="alice",
    )

    with pytest.raises(APITransportStartupError) as exc_info:
        run(
            select_agent_client(
                config,
                local_client=local,
                api_client_factory=FakeAPIClient,
            )
        )

    assert exc_info.value.code == "api_unavailable"
    api = FakeAPIClient.instances[0]
    assert api.calls == [("health", {})]
    assert api.closed is True


def test_select_agent_client_api_auth_failure_is_structured() -> None:
    FakeAPIClient.reset()
    FakeAPIClient.me_result = http_status_error(403, "Act-As requires admin")
    config = runtime_config(
        transport="api",
        api_url="http://api",
        api_key="bad",
        user_id="alice",
    )

    with pytest.raises(APITransportStartupError) as exc_info:
        run(select_agent_client(config, api_client_factory=FakeAPIClient))

    error = exc_info.value
    assert error.code == "api_auth_error"
    assert error.status_code == 403
    assert error.as_event(thread_id="thread-a") == ErrorEvent(
        thread_id="thread-a",
        content="API authentication failed: Act-As requires admin",
        code="api_auth_error",
        details={
            "api_url": "http://api",
            "error_type": "HTTPStatusError",
            "status_code": 403,
            "detail": "Act-As requires admin",
        },
    )
    assert FakeAPIClient.instances[0].closed is True


def test_select_agent_client_auto_without_configured_api_key_returns_disconnected() -> None:
    FakeAPIClient.reset()
    local = FakeLocalClient()

    selected = run(
        select_agent_client(
            runtime_config(transport="auto"),
            local_client=local,
            api_client_factory=FakeAPIClient,
            environ={},
        )
    )

    assert isinstance(selected, DisconnectedAgentClient)
    assert selected.connection_label == "disconnected"
    assert FakeAPIClient.instances == []


def test_select_agent_client_saved_profile_unreachable_is_reconnectable() -> None:
    FakeAPIClient.reset()
    FakeAPIClient.health_result = False

    selected = run(
        select_agent_client(
            runtime_config(transport="api"),
            api_client_factory=FakeAPIClient,
            environ={},
            saved_profile=CLIConnectionProfile(
                api_url="http://saved",
                api_key="saved-token",
                user_id="alice",
            ),
        )
    )

    # Backend down at startup: the placeholder keeps the saved profile so the
    # CLI can auto-reconnect once the backend comes up, instead of forcing a
    # full /login with url + token re-entry.
    assert isinstance(selected, DisconnectedAgentClient)
    assert selected.default_user_id == "alice"
    assert selected.can_reconnect is True
    assert selected.reconnect_api_url == "http://saved"
    assert selected.reconnect_api_key == "saved-token"
    assert selected.reconnect_user_id == "alice"
    assert "not reachable yet" in selected.startup_error


def test_select_agent_client_saved_profile_auth_failure_is_not_reconnectable() -> None:
    FakeAPIClient.reset()
    FakeAPIClient.me_result = http_status_error(401, "Token revoked")

    selected = run(
        select_agent_client(
            runtime_config(transport="api"),
            api_client_factory=FakeAPIClient,
            environ={},
            saved_profile=CLIConnectionProfile(
                api_url="http://saved",
                api_key="saved-token",
                user_id="alice",
            ),
        )
    )

    # A bad/expired token cannot be fixed by retrying, so the placeholder does
    # not retain it and tells the user to /login.
    assert isinstance(selected, DisconnectedAgentClient)
    assert selected.can_reconnect is False
    assert "Run /login" in selected.startup_error


def test_attempt_saved_reconnect_returns_live_client_when_backend_recovers() -> None:
    FakeAPIClient.reset()
    placeholder = DisconnectedAgentClient(
        default_user_id="alice",
        reconnect_api_url="http://saved",
        reconnect_api_key="saved-token",
        reconnect_user_id="alice",
    )

    reconnected = run(
        attempt_saved_reconnect(placeholder, api_client_factory=FakeAPIClient)
    )

    assert isinstance(reconnected, APIAgentClient)
    assert reconnected.connection_label == "api http://saved"
    assert reconnected.default_user_id == "alice"
    assert FakeAPIClient.instances[0].calls == [
        ("health", {}),
        ("get_me", {"act_as": "alice"}),
    ]


def test_attempt_saved_reconnect_returns_none_while_backend_still_down() -> None:
    FakeAPIClient.reset()
    FakeAPIClient.health_result = False
    placeholder = DisconnectedAgentClient(
        default_user_id="alice",
        reconnect_api_url="http://saved",
        reconnect_api_key="saved-token",
    )

    reconnected = run(
        attempt_saved_reconnect(placeholder, api_client_factory=FakeAPIClient)
    )

    assert reconnected is None
    assert FakeAPIClient.instances[0].closed is True


def test_attempt_saved_reconnect_raises_on_auth_error() -> None:
    FakeAPIClient.reset()
    FakeAPIClient.me_result = http_status_error(401, "Token revoked")
    placeholder = DisconnectedAgentClient(
        default_user_id="alice",
        reconnect_api_url="http://saved",
        reconnect_api_key="saved-token",
    )

    with pytest.raises(APITransportStartupError) as exc_info:
        run(attempt_saved_reconnect(placeholder, api_client_factory=FakeAPIClient))

    assert exc_info.value.code == "api_auth_error"


def test_suggest_reachable_backend_finds_live_backend_on_another_port(tmp_path) -> None:
    probed: list[str] = []
    seen_keys: list[str] = []

    class PerUrlAPI:
        def __init__(self, *, base_url: str, api_key: str = "") -> None:
            self.base_url = base_url.rstrip("/")
            self.closed = False
            probed.append(self.base_url)
            seen_keys.append(api_key)

        async def health(self) -> bool:
            # The default candidate :8000 is always probed; only it answers.
            return self.base_url.endswith(":8000")

        async def close(self) -> None:
            self.closed = True

    result = run(
        suggest_reachable_backend(
            "http://127.0.0.1:8098",
            api_client_factory=PerUrlAPI,
            environ={},
            config_path=tmp_path / "cli.json",
        )
    )

    assert result == "http://127.0.0.1:8000"
    assert "http://127.0.0.1:8000" in probed
    # Regression guard: the probe must use a non-empty bearer. An empty api_key
    # builds an illegal "Bearer " header, so health() would silently always fail.
    assert seen_keys and all(key for key in seen_keys)


def test_suggest_reachable_backend_skips_remote_targets() -> None:
    probed: list[str] = []

    class API:
        def __init__(self, *, base_url: str, api_key: str = "") -> None:
            probed.append(base_url)

        async def health(self) -> bool:
            return True

        async def close(self) -> None:
            pass

    result = run(
        suggest_reachable_backend(
            "https://api.example.com:8000",
            api_client_factory=API,
            environ={},
        )
    )

    # A remote outage must never trigger a local port probe.
    assert result is None
    assert probed == []


def test_suggest_reachable_backend_returns_none_when_nothing_answers(tmp_path) -> None:
    class API:
        def __init__(self, *, base_url: str, api_key: str = "") -> None:
            self.closed = False

        async def health(self) -> bool:
            return False

        async def close(self) -> None:
            self.closed = True

    result = run(
        suggest_reachable_backend(
            "http://127.0.0.1:8098",
            api_client_factory=API,
            environ={},
            config_path=tmp_path / "cli.json",
        )
    )

    assert result is None


def test_attempt_saved_reconnect_ignores_plain_disconnected_placeholder() -> None:
    FakeAPIClient.reset()
    placeholder = DisconnectedAgentClient(default_user_id="alice")

    reconnected = run(
        attempt_saved_reconnect(placeholder, api_client_factory=FakeAPIClient)
    )

    assert reconnected is None
    assert FakeAPIClient.instances == []
