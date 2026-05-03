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
    ) -> None:
        self._json_data = json_data if json_data is not None else {"ok": True}
        self._lines = lines or []
        self.content = content
        self.headers = headers or {}
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
