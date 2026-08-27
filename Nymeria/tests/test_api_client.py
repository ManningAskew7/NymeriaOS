"""Tests for the thin Nymeria REST API client."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

import pytest

from nymeria.triggers import api_client
from nymeria.triggers.api_client import NymeriaAPIClient


class FakeResponse:
    def __init__(
        self,
        json_data: Any = None,
        *,
        lines: list[str] | None = None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data if json_data is not None else {"ok": True}
        self._lines = lines or []
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.raised = False

    def raise_for_status(self) -> None:
        self.raised = True
        if self.status_code >= 400:
            request = api_client.httpx.Request("GET", "http://api/test")
            raise api_client.httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=self
            )

    def json(self) -> Any:
        return self._json_data

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeAsyncClient:
    instances: list["FakeAsyncClient"] = []

    def __init__(self, *, timeout) -> None:
        self.timeout = timeout
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.close_count = 0
        self.__class__.instances.append(self)

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse({"ok": True, "method": method})

    def stream(self, method: str, url: str, **kwargs) -> FakeStreamContext:
        self.requests.append({"method": f"STREAM {method}", "url": url, **kwargs})
        return FakeStreamContext(
            FakeResponse(
                lines=[
                    "",
                    ": keepalive",
                    'data: {"type": "response", "content": "hi"}',
                    "data: not-json",
                    'data: {"type": "done"}',
                ]
            )
        )

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return FakeResponse(
            content=b"hello",
            headers={
                "content-type": "text/plain",
                "content-disposition": 'attachment; filename="hello.txt"',
            },
        )

    async def aclose(self) -> None:
        self.closed = True
        self.close_count += 1


def _patch_async_client(monkeypatch) -> None:
    FakeAsyncClient.instances.clear()
    monkeypatch.setattr(api_client.httpx, "AsyncClient", FakeAsyncClient)


def test_api_client_reuses_one_httpx_client_for_json_requests(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api/", api_key="secret")
        try:
            assert await client.get_settings() == {"ok": True, "method": "GET"}
            assert await client.add_todo("user-1", "Ship it") == {
                "ok": True,
                "method": "POST",
            }
            assert await client.update_thread_config(
                "thread-1", user_id="user-1", llm_model="test-model"
            ) == {"ok": True, "method": "PATCH"}
        finally:
            await client.close()

    asyncio.run(run())

    assert len(FakeAsyncClient.instances) == 1
    fake = FakeAsyncClient.instances[0]
    assert fake.timeout is api_client._DEFAULT_TIMEOUT
    assert fake.closed is True
    assert [request["method"] for request in fake.requests] == [
        "GET",
        "POST",
        "PATCH",
    ]
    assert fake.requests[0]["timeout"] is api_client._DEFAULT_TIMEOUT
    assert fake.requests[1]["timeout"] is api_client._CHAT_TIMEOUT
    assert fake.requests[2]["timeout"] is api_client._DEFAULT_TIMEOUT
    assert fake.requests[2]["headers"]["X-Nymeria-Act-As"] == "user-1"


def test_api_client_uses_loop_local_httpx_clients(monkeypatch):
    _patch_async_client(monkeypatch)

    client = NymeriaAPIClient(base_url="http://api/", api_key="secret")

    async def request_once() -> None:
        assert await client.get_settings() == {"ok": True, "method": "GET"}

    asyncio.run(request_once())
    asyncio.run(request_once())
    asyncio.run(client.close())

    # One httpx client is created per event loop (loop-local).
    assert len(FakeAsyncClient.instances) == 2
    first, second = FakeAsyncClient.instances
    # The first loop's client is pruned when that loop closes (its sockets
    # cannot be aclosed from a dead loop), so close() only closes the
    # surviving second-loop client rather than every client ever created.
    assert first.close_count == 0
    assert second.closed is True
    assert second.close_count == 1


def test_api_client_prunes_clients_from_closed_loops(monkeypatch):
    """A long-lived client reused across many one-shot ``asyncio.run`` loops
    must not accumulate one httpx client per dead loop. Each loop still gets
    its own client, but stale closed-loop entries are pruned on next access so
    the map stays bounded (the CLI REPL reuses one client across every
    command)."""
    _patch_async_client(monkeypatch)

    client = NymeriaAPIClient(base_url="http://api/", api_key="secret")

    async def request_once() -> None:
        await client.get_settings()

    for _ in range(5):
        asyncio.run(request_once())
        # The map only ever retains the most-recent loop's entry; the prior
        # loop's (now-closed) client was dropped on this call's access.
        assert len(client._clients) == 1

    # Five short-lived loops were used, so five clients were created over the
    # run, but the map never grew past one entry (without the prune it would
    # hold all five dead-loop clients and their sockets).
    assert len(FakeAsyncClient.instances) == 5
    asyncio.run(client.close())


def test_client_for_loop_keeps_current_loop_when_sibling_dead(monkeypatch):
    """The prune must drop only closed *sibling* loops, never the current
    loop's own client. Without the ``other is not loop`` guard a re-access
    could evict the client the in-flight request is about to use."""
    _patch_async_client(monkeypatch)

    client = NymeriaAPIClient(base_url="http://api/", api_key="secret")

    # A real, already-closed event loop standing in for a one-shot caller's
    # dead loop (is_closed() is genuinely True, unlike a stub).
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()

    async def run() -> None:
        await client.get_settings()  # binds a client to the current (live) loop
        loop = asyncio.get_running_loop()
        client._clients[dead_loop] = api_client.httpx.AsyncClient(
            timeout=api_client._DEFAULT_TIMEOUT
        )
        assert len(client._clients) == 2

        client._client_for_loop()  # triggers the prune

        # The dead sibling is dropped; the current live loop's client survives.
        assert dead_loop not in client._clients
        assert loop in client._clients
        assert len(client._clients) == 1

    asyncio.run(run())


def test_api_client_reuses_client_for_streaming_and_workspace_download(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> tuple[list[dict[str, Any]], tuple]:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            events = [
                event
                async for event in client.chat_stream(
                    message="hello",
                    thread_id="thread-1",
                    user_id="user-1",
                )
            ]
            downloaded = await client.download_workspace_file("/tmp/report.txt")
            return events, downloaded
        finally:
            await client.close()

    events, downloaded = asyncio.run(run())

    assert len(FakeAsyncClient.instances) == 1
    fake = FakeAsyncClient.instances[0]
    assert events == [
        {"type": "response", "content": "hi"},
        {"type": "done"},
    ]
    assert downloaded == (b"hello", "hello.txt", "text/plain")
    assert [request["method"] for request in fake.requests] == ["STREAM POST", "GET"]
    assert fake.requests[0]["timeout"] is api_client._CHAT_TIMEOUT
    assert fake.requests[0]["json"]["thread_id"] == "thread-1"
    assert fake.requests[1]["params"] == {"path": "/tmp/report.txt"}
    assert fake.closed is True


def test_api_client_async_context_manager_closes_client(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> None:
        async with NymeriaAPIClient(base_url="http://api", api_key="secret") as client:
            assert FakeAsyncClient.instances[0].closed is False
            assert client.base_url == "http://api"

    asyncio.run(run())

    assert len(FakeAsyncClient.instances) == 1
    assert FakeAsyncClient.instances[0].closed is True
    assert FakeAsyncClient.instances[0].close_count == 1


def test_api_client_account_wrappers_preserve_paths_headers_and_bodies(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            await client.update_me("Alex", user_id="user-1")
            await client.list_my_tokens(user_id="user-1")
            await client.issue_my_token(label="cli", user_id="user-1")
            await client.revoke_my_token("abc/123", user_id="user-1")
            await client.list_user_platforms("user-1")
            await client.link_user_platform("user-1", "telegram", "42")
            await client.unlink_user_platform("user-1", "telegram", "42")
            await client.list_my_platforms(user_id="user-1")
        finally:
            await client.close()

    asyncio.run(run())

    requests = FakeAsyncClient.instances[0].requests
    assert [request["method"] for request in requests] == [
        "PATCH",
        "GET",
        "POST",
        "DELETE",
        "GET",
        "POST",
        "DELETE",
        "GET",
    ]
    assert requests[0]["url"] == "http://api/me"
    assert requests[0]["json"] == {"display_name": "Alex"}
    assert requests[0]["headers"]["X-Nymeria-Act-As"] == "user-1"
    assert requests[3]["url"] == "http://api/me/tokens/abc%2F123"
    assert requests[5]["url"] == "http://api/admin/users/user-1/platforms"
    assert requests[5]["json"] == {
        "provider": "telegram",
        "provider_user_id": "42",
    }
    assert (
        requests[6]["url"]
        == "http://api/admin/users/user-1/platforms/telegram/42"
    )
    assert requests[7]["url"] == "http://api/me/platforms"
    assert requests[7]["headers"]["X-Nymeria-Act-As"] == "user-1"


def test_api_client_fallback_approval_wrappers(monkeypatch):
    """The consent wrappers hit the llm-fallback router with the resolve
    payload shape (hold fields only when chosen) and Act-As for the clicker."""
    _patch_async_client(monkeypatch)

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            await client.get_fallback_approvals(user_id="user-1")
            await client.resolve_fallback_approval(
                "rec/1", True, hold_seconds=600, user_id="user-1"
            )
            await client.resolve_fallback_approval(
                "rec-2", True, hold_permanent=True, user_id="user-1"
            )
            await client.resolve_fallback_approval("rec-3", False, user_id="user-1")
        finally:
            await client.close()

    asyncio.run(run())

    requests = FakeAsyncClient.instances[0].requests
    assert [request["method"] for request in requests] == [
        "GET", "POST", "POST", "POST",
    ]
    assert requests[0]["url"] == "http://api/llm/fallback-approvals"
    assert requests[1]["url"] == "http://api/llm/fallback-approvals/rec%2F1/resolve"
    assert requests[1]["json"] == {"approved": True, "hold_seconds": 600}
    assert requests[2]["json"] == {"approved": True, "hold_permanent": True}
    assert requests[3]["json"] == {"approved": False}
    assert requests[3]["headers"]["X-Nymeria-Act-As"] == "user-1"


def test_api_client_cli_domain_wrappers_use_desktop_api_routes(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            await client.list_skills("user-1", scope="user")
            await client.install_skill(
                {"source": "anthropic", "name": "python", "scope": "user"},
                user_id="user-1",
            )
            await client.create_mcp_server(
                {"id": "srv", "name": "Server"},
                thread_id="thread-1",
                user_id="user-1",
            )
            await client.list_triggers(
                "user-1",
                enabled_only=True,
                thread_id="thread-1",
            )
            await client.get_activity(
                "user-1",
                limit=10,
                activity_type="tool",
                thread_id="thread-1",
            )
            await client.list_todos(
                "user-1",
                filter_status="all",
                thread_id="thread-1",
            )
            await client.mark_notification_read("n-1", user_id="user-1")
            await client.test_llm_provider_config(
                {"provider": "openai", "model": "gpt-test"},
                user_id="admin",
            )
            await client.delete_thread_config("thread-1", user_id="user-1")
            await client.export_thread("thread-1", user_id="user-1")
            await client.import_thread({"version": 1}, user_id="user-1")
        finally:
            await client.close()

    asyncio.run(run())

    requests = FakeAsyncClient.instances[0].requests
    assert [request["method"] for request in requests] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "GET",
        "GET",
        "POST",
        "POST",
        "DELETE",
        "GET",
        "POST",
    ]
    assert requests[0]["url"] == "http://api/skills"
    assert requests[0]["params"] == {"user_id": "user-1", "scope": "user"}
    assert requests[0]["headers"]["X-Nymeria-Act-As"] == "user-1"
    assert requests[1]["url"] == "http://api/skills/install"
    assert requests[1]["json"]["name"] == "python"
    assert requests[1]["timeout"] is api_client._CHAT_TIMEOUT
    assert requests[2]["url"] == "http://api/mcp-servers"
    assert requests[2]["params"] == {"thread_id": "thread-1"}
    assert requests[2]["headers"]["X-Nymeria-Act-As"] == "user-1"
    assert requests[3]["url"] == "http://api/triggers"
    assert requests[3]["params"] == {
        "user_id": "user-1",
        "enabled_only": True,
        "thread_id": "thread-1",
    }
    assert requests[4]["url"] == "http://api/activity"
    assert requests[4]["params"] == {
        "user_id": "user-1",
        "limit": 10,
        "activity_type": "tool",
        "thread_id": "thread-1",
    }
    assert requests[5]["url"] == "http://api/todos"
    assert requests[5]["params"] == {
        "user_id": "user-1",
        "filter_status": "all",
        "thread_id": "thread-1",
    }
    assert requests[5]["headers"]["X-Nymeria-Act-As"] == "user-1"
    assert requests[6]["url"] == "http://api/notifications/n-1/read"
    assert requests[7]["url"] == "http://api/settings/llm/test"
    assert requests[7]["headers"]["X-Nymeria-Act-As"] == "admin"
    assert requests[8]["url"] == "http://api/threads/thread-1/config"
    assert requests[9]["url"] == "http://api/threads/thread-1/export"
    assert requests[10]["url"] == "http://api/threads/import"
    assert requests[10]["json"] == {"version": 1}


def test_api_client_get_thread_overview_uses_act_as_and_encoded_path(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            await client.get_thread_overview("thread/with space", user_id="user-1")
        finally:
            await client.close()

    asyncio.run(run())

    request = FakeAsyncClient.instances[0].requests[0]
    assert request["method"] == "GET"
    assert request["url"] == "http://api/threads/thread%2Fwith%20space/overview"
    assert request["params"] is None
    assert request["headers"]["X-Nymeria-Act-As"] == "user-1"


def test_api_client_tool_wrappers_cover_unified_defaults_and_custom_tools(monkeypatch):
    _patch_async_client(monkeypatch)

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            await client.get_tools()
            await client.get_optional_tools("user-1")
            await client.search_tools(
                "browser",
                user_id="user-1",
                thread_id="thread-1",
                top_k=3,
            )
            await client.get_unified_tools("user-1")
            await client.set_unified_tool_enabled("bash", False, "user-1")
            await client.set_unified_tool_description("bash", "Safer shell", "user-1")
            await client.set_unified_tool_config("bash", {"timeout": 10}, "user-1")
            await client.set_default_tools(["bash"], "user-1")
            await client.reset_default_tools("user-1")
            await client.test_custom_tool("weather/tool", {"city": "SF"})
        finally:
            await client.close()

    asyncio.run(run())

    requests = FakeAsyncClient.instances[0].requests
    assert [request["method"] for request in requests] == [
        "GET",
        "GET",
        "GET",
        "GET",
        "PUT",
        "PUT",
        "PUT",
        "PUT",
        "DELETE",
        "POST",
    ]
    assert requests[0]["url"] == "http://api/tools"
    assert requests[1]["url"] == "http://api/tools/optional"
    assert requests[1]["headers"]["X-Nymeria-Act-As"] == "user-1"
    assert requests[2]["url"] == "http://api/users/user-1/tools/search"
    assert requests[2]["params"] == {
        "query": "browser",
        "thread_id": "thread-1",
        "top_k": 3,
        "include_status": "true",
    }
    assert requests[3]["url"] == "http://api/users/user-1/tools/unified"
    assert (
        requests[4]["url"]
        == "http://api/users/user-1/tools/unified/bash/enable"
    )
    assert requests[4]["json"] == {"enabled": False}
    assert (
        requests[5]["url"]
        == "http://api/users/user-1/tools/unified/bash/description"
    )
    assert requests[5]["json"] == {"description": "Safer shell"}
    assert (
        requests[6]["url"]
        == "http://api/users/user-1/tools/unified/bash/config"
    )
    assert requests[7]["url"] == "http://api/tools/defaults"
    assert requests[7]["params"] == {"user_id": "user-1"}
    assert requests[7]["json"] == {"tool_names": ["bash"]}
    assert requests[8]["url"] == "http://api/tools/defaults"
    assert requests[9]["url"] == "http://api/tools/custom/weather%2Ftool/test"
    assert requests[9]["json"] == {"params": {"city": "SF"}}


# ── Service-token refresh-and-retry on 401 ──────────────────────────────────
#
# In the full Docker stack the api self-mints the service token onto the
# shared volume; a long-running worker/bot holds the old token after a
# re-mint. The client refreshes via an injected refresher and retries once.


class ScriptedAsyncClient:
    """Fake httpx.AsyncClient serving queued responses, recording auth headers."""

    instances: list["ScriptedAsyncClient"] = []
    script: list[FakeResponse] = []
    auth_headers: list[str | None] = []

    def __init__(self, *, timeout) -> None:
        self.timeout = timeout
        self.__class__.instances.append(self)

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        ScriptedAsyncClient.auth_headers.append(
            (kwargs.get("headers") or {}).get("Authorization")
        )
        return ScriptedAsyncClient.script.pop(0)

    def stream(self, method: str, url: str, **kwargs) -> FakeStreamContext:
        ScriptedAsyncClient.auth_headers.append(
            (kwargs.get("headers") or {}).get("Authorization")
        )
        return FakeStreamContext(ScriptedAsyncClient.script.pop(0))

    async def aclose(self) -> None:
        return None


def _patch_scripted_client(monkeypatch, script: list[FakeResponse]) -> None:
    ScriptedAsyncClient.instances.clear()
    ScriptedAsyncClient.script = list(script)
    ScriptedAsyncClient.auth_headers = []
    monkeypatch.setattr(api_client.httpx, "AsyncClient", ScriptedAsyncClient)


class RecordingRefresher:
    def __init__(self, token: str | None = None, error: Exception | None = None):
        self.token = token
        self.error = error
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.token


_CHAT_LINES = [
    'data: {"type": "response", "content": "hi"}',
    'data: {"type": "done"}',
]


def test_request_refreshes_and_retries_once_on_401(monkeypatch):
    _patch_scripted_client(
        monkeypatch,
        [FakeResponse(status_code=401), FakeResponse({"ok": True})],
    )
    refresher = RecordingRefresher(token="nym_new")

    async def run() -> Any:
        client = NymeriaAPIClient(
            base_url="http://api", api_key="nym_old", token_refresher=refresher
        )
        try:
            return await client.get_settings()
        finally:
            await client.close()

    assert asyncio.run(run()) == {"ok": True}
    assert refresher.calls == 1
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old", "Bearer nym_new"]


def test_request_no_retry_when_token_unchanged(monkeypatch):
    _patch_scripted_client(monkeypatch, [FakeResponse(status_code=401)])
    refresher = RecordingRefresher(token="nym_old")

    async def run() -> None:
        client = NymeriaAPIClient(
            base_url="http://api", api_key="nym_old", token_refresher=refresher
        )
        try:
            with pytest.raises(api_client.httpx.HTTPStatusError):
                await client.get_settings()
        finally:
            await client.close()

    asyncio.run(run())
    assert refresher.calls == 1
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old"]


def test_request_no_refresher_propagates_401(monkeypatch):
    _patch_scripted_client(monkeypatch, [FakeResponse(status_code=401)])

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="nym_old")
        try:
            with pytest.raises(api_client.httpx.HTTPStatusError):
                await client.get_settings()
        finally:
            await client.close()

    asyncio.run(run())
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old"]


def test_request_refresher_exception_propagates_original_401(monkeypatch):
    _patch_scripted_client(monkeypatch, [FakeResponse(status_code=401)])
    refresher = RecordingRefresher(error=RuntimeError("volume gone"))

    async def run() -> None:
        client = NymeriaAPIClient(
            base_url="http://api", api_key="nym_old", token_refresher=refresher
        )
        try:
            with pytest.raises(api_client.httpx.HTTPStatusError):
                await client.get_settings()
        finally:
            await client.close()

    asyncio.run(run())
    assert refresher.calls == 1
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old"]


def test_request_non_401_not_retried(monkeypatch):
    _patch_scripted_client(monkeypatch, [FakeResponse(status_code=500)])
    refresher = RecordingRefresher(token="nym_new")

    async def run() -> None:
        client = NymeriaAPIClient(
            base_url="http://api", api_key="nym_old", token_refresher=refresher
        )
        try:
            with pytest.raises(api_client.httpx.HTTPStatusError):
                await client.get_settings()
        finally:
            await client.close()

    asyncio.run(run())
    assert refresher.calls == 0
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old"]


def test_chat_stream_refreshes_and_retries_on_401_before_first_chunk(monkeypatch):
    _patch_scripted_client(
        monkeypatch,
        [FakeResponse(status_code=401), FakeResponse(lines=_CHAT_LINES)],
    )
    refresher = RecordingRefresher(token="nym_new")

    async def run() -> list[dict[str, Any]]:
        client = NymeriaAPIClient(
            base_url="http://api", api_key="nym_old", token_refresher=refresher
        )
        try:
            return [
                event
                async for event in client.chat_stream(
                    message="hello", thread_id="thread-1", user_id="user-1"
                )
            ]
        finally:
            await client.close()

    events = asyncio.run(run())
    assert events == [{"type": "response", "content": "hi"}, {"type": "done"}]
    assert refresher.calls == 1
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old", "Bearer nym_new"]


def test_chat_stream_no_retry_after_chunks_yielded(monkeypatch):
    # Real httpx never raises HTTPStatusError from aiter_lines (mid-stream
    # failures surface as RequestError subtypes, which are deliberately not
    # retried); this stand-in exists purely to lock in the `yielded` guard
    # against any 401-shaped error after output started.
    class MidStreamFailingResponse(FakeResponse):
        async def aiter_lines(self):
            yield 'data: {"type": "response", "content": "hi"}'
            request = api_client.httpx.Request("POST", "http://api/chat")
            raise api_client.httpx.HTTPStatusError(
                "mid-stream 401",
                request=request,
                response=FakeResponse(status_code=401),
            )

    _patch_scripted_client(monkeypatch, [MidStreamFailingResponse(lines=[])])
    refresher = RecordingRefresher(token="nym_new")

    async def run() -> list[dict[str, Any]]:
        client = NymeriaAPIClient(
            base_url="http://api", api_key="nym_old", token_refresher=refresher
        )
        seen: list[dict[str, Any]] = []
        try:
            with pytest.raises(api_client.httpx.HTTPStatusError):
                async for event in client.chat_stream(
                    message="hello", thread_id="thread-1", user_id="user-1"
                ):
                    seen.append(event)
        finally:
            await client.close()
        return seen

    seen = asyncio.run(run())
    assert seen == [{"type": "response", "content": "hi"}]
    assert refresher.calls == 0
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old"]


def test_chat_stream_no_refresher_propagates_401(monkeypatch):
    _patch_scripted_client(monkeypatch, [FakeResponse(status_code=401)])

    async def run() -> None:
        client = NymeriaAPIClient(base_url="http://api", api_key="nym_old")
        try:
            with pytest.raises(api_client.httpx.HTTPStatusError):
                async for _ in client.chat_stream(
                    message="hello", thread_id="thread-1", user_id="user-1"
                ):
                    pass
        finally:
            await client.close()

    asyncio.run(run())
    assert ScriptedAsyncClient.auth_headers == ["Bearer nym_old"]


def test_maybe_refresh_token_retries_when_another_call_already_refreshed():
    # Two turns share one client. Turn A 401s and refreshes api_key old->new;
    # turn B, whose stream opened with the OLD token, then 401s too. The guard
    # must compare against the token B actually sent, not the already-updated
    # api_key, so B still gets its retry (with the new headers left intact).
    refresher = RecordingRefresher(token="nym_new")
    client = NymeriaAPIClient(
        base_url="http://api", api_key="nym_new", token_refresher=refresher
    )

    assert client._maybe_refresh_token("nym_old") is True
    assert client.api_key == "nym_new"
    assert client._headers == {"Authorization": "Bearer nym_new"}


def test_maybe_refresh_token_declines_when_disk_matches_failed_token():
    refresher = RecordingRefresher(token="nym_old")
    client = NymeriaAPIClient(
        base_url="http://api", api_key="nym_old", token_refresher=refresher
    )

    assert client._maybe_refresh_token("nym_old") is False
    assert client.api_key == "nym_old"


# ---------------------------------------------------------------------------
# download_workspace_file narrowed-catch behavior (slice 23 F7)
# ---------------------------------------------------------------------------


def _patch_get_client(monkeypatch, *, get_result=None, get_raises=None):
    """Patch httpx.AsyncClient with one whose .get returns/raises as configured."""

    class _GetClient:
        def __init__(self, *, timeout) -> None:
            self.timeout = timeout

        async def get(self, url: str, **kwargs):
            if get_raises is not None:
                raise get_raises
            return get_result

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _GetClient)


def _download_once() -> Any:
    async def run() -> Any:
        client = NymeriaAPIClient(base_url="http://api", api_key="secret")
        try:
            return await client.download_workspace_file("/tmp/report.txt")
        finally:
            await client.close()

    return asyncio.run(run())


def test_download_workspace_file_returns_none_on_http_status_error(monkeypatch):
    # A 4xx/5xx makes raise_for_status throw HTTPStatusError -> None, not a raise.
    _patch_get_client(monkeypatch, get_result=FakeResponse(status_code=404))
    assert _download_once() is None


def test_download_workspace_file_returns_none_on_network_error(monkeypatch):
    request = api_client.httpx.Request("GET", "http://api/workspace/download")
    _patch_get_client(
        monkeypatch,
        get_raises=api_client.httpx.ConnectError("boom", request=request),
    )
    assert _download_once() is None


def test_download_workspace_file_propagates_non_httpx_error(monkeypatch):
    # A non-httpx (programming) error is no longer swallowed into a silent None.
    _patch_get_client(monkeypatch, get_raises=ValueError("unexpected bug"))
    with pytest.raises(ValueError):
        _download_once()


# ---------------------------------------------------------------------------
# resolve_platform_user 404 discrimination: only the route's own
# {"detail": "Not linked"} answer means "confirmed unlinked"; any other 404
# (proxy error page, wrong path prefix, moved route) must raise so bots
# render infrastructure copy instead of treating every sender as unlinked
# (which the Discord default-account fallback would convert into silent
# shared-account access for a whole guild).
# ---------------------------------------------------------------------------


def _client_with_404(*, json_body=None, text_body=None) -> NymeriaAPIClient:
    client = NymeriaAPIClient(base_url="http://api/", api_key="secret")
    request = httpx.Request("GET", "http://api/platform/resolve")
    if json_body is not None:
        response = httpx.Response(404, json=json_body, request=request)
    else:
        response = httpx.Response(404, text=text_body or "", request=request)

    async def _get(path, **kwargs):
        raise httpx.HTTPStatusError("404", request=request, response=response)

    client._get = _get
    return client


def test_resolve_platform_user_not_linked_404_is_confirmed_unlinked():
    client = _client_with_404(json_body={"detail": "Not linked"})
    assert asyncio.run(client.resolve_platform_user("discord", "42")) is None


def test_resolve_platform_user_foreign_404_raises():
    client = _client_with_404(text_body="<html>proxy error</html>")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.resolve_platform_user("discord", "42"))


def test_resolve_platform_user_other_detail_404_raises():
    client = _client_with_404(json_body={"detail": "Not Found"})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.resolve_platform_user("discord", "42"))
