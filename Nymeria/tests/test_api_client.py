"""Tests for the thin Nymeria REST API client."""

from __future__ import annotations

import asyncio
from typing import Any

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

    assert len(FakeAsyncClient.instances) == 2
    assert all(fake.closed for fake in FakeAsyncClient.instances)
    assert [fake.close_count for fake in FakeAsyncClient.instances] == [1, 1]


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
