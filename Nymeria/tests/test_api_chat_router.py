"""Focused tests for the extracted chat API router."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nymeria.core.accounts import AccountsRepo


class FakeThreadMetadataManager:
    def __init__(self) -> None:
        self.auto_title_calls: list[tuple[str, str, str]] = []

    def auto_title(self, user_id: str, thread_id: str, message: str) -> str:
        self.auto_title_calls.append((user_id, thread_id, message))
        return "Auto Title"


class FakeChatAgent:
    def __init__(self, data_dir: Path) -> None:
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.settings = SimpleNamespace(llm_model="fallback-model")
        self.thread_metadata_manager = FakeThreadMetadataManager()
        self.thread_config_manager = object()
        self.synced_tools = 0
        self._last_chat_tool_calls = 2
        self.chat_calls: list[dict[str, Any]] = []
        self.astream_calls: list[dict[str, Any]] = []
        self.compact_calls: list[dict[str, Any]] = []
        self.compact_result: dict[str, Any] = {
            "success": True,
            "messages_removed": 3,
            "summary": "summary",
        }
        self.compact_should_start = True

    def sync_agent_tools(self) -> None:
        self.synced_tools += 1

    def chat(self, message: str, **kwargs: Any) -> str:
        self.chat_calls.append({"message": message, **kwargs})
        return "sync response"

    async def astream(self, message: str, **kwargs: Any):
        self.astream_calls.append({"message": message, **kwargs})
        yield {"type": "thinking", "content": "working"}
        yield {"type": "response", "content": "stream response"}

    async def compact_now(self, thread_id: str, user_id: str, *, on_started=None, priority=None):
        self.compact_calls.append(
            {"thread_id": thread_id, "user_id": user_id, "priority": priority}
        )
        if self.compact_should_start and on_started is not None:
            result = on_started()
            if inspect.isawaitable(result):
                await result
        return self.compact_result

    def get_context_stats(self, thread_id: str) -> dict[str, int | str]:
        return {"thread_id": thread_id, "estimated_tokens": 123}

    def _get_llm_config_for_thread(self, thread_id: str):
        return SimpleNamespace(model=None)


def _chat_client(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeChatAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="alice",
    )
    return client, agent, token


def _sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_chat_sync_uses_authenticated_user_not_body_user_id(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    response = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={
            "message": "hello",
            "thread_id": "thread-sync",
            "user_id": "bob",
            "is_self_invoke": True,
            "trigger_override": "watchdog",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "response": "sync response",
        "thread_id": "thread-sync",
        "tool_call_count": 2,
    }
    assert agent.chat_calls == [
        {
            "message": "hello",
            "thread_id": "thread-sync",
            "user_id": "alice",
            "attachments": None,
            "images": None,
            "force_unsupported_attachments": False,
            "_is_self_invoke": True,
            "_trigger_override": "watchdog",
            "source": "user",
            "source_id": None,
            "source_label": "alice",
        }
    ]
    assert agent.accounts_repo.get_thread_owner("thread-sync") == "alice"


def test_chat_stream_preserves_sse_shape_and_attachment_conversion(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token, **{"x-nymeria-client-id": "client-1"}),
        json={
            "message": "stream this",
            "thread_id": "thread-stream",
            "user_id": "bob",
            "attachments": [
                {
                    "file_type": "image",
                    "data_url": "data:image/png;base64,AAAA",
                    "mime_type": "image/png",
                    "file_name": None,
                }
            ],
            "images": [
                {
                    "data_url": "data:image/jpeg;base64,BBBB",
                    "mime_type": "image/jpeg",
                }
            ],
            "force_unsupported_attachments": True,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    events = _sse_events(body)
    assert events == [
        {"type": "thinking", "content": "working", "thread_id": "thread-stream"},
        {"type": "response", "content": "stream response", "thread_id": "thread-stream"},
        {
            "type": "done",
            "thread_id": "thread-stream",
            "context_stats": {
                "thread_id": "thread-stream",
                "estimated_tokens": 123,
            },
            "model": "fallback-model",
            "title": "Auto Title",
            "title_source": "auto",
        },
    ]
    assert agent.astream_calls == [
        {
            "message": "stream this",
            "thread_id": "thread-stream",
            "user_id": "alice",
            "attachments": [
                {
                    "file_type": "image",
                    "data_url": "data:image/png;base64,AAAA",
                    "mime_type": "image/png",
                    "file_name": "",
                }
            ],
            "images": [
                {
                    "data_url": "data:image/jpeg;base64,BBBB",
                    "mime_type": "image/jpeg",
                }
            ],
            "force_unsupported_attachments": True,
            "_is_self_invoke": False,
            "_trigger_override": None,
            "source": "user",
            "source_id": None,
            "source_label": "alice",
        }
    ]
    assert agent.thread_metadata_manager.auto_title_calls == [
        ("alice", "thread-stream", "stream this")
    ]
    assert agent.accounts_repo.get_thread_owner("thread-stream") == "alice"


def test_chat_stream_compact_emits_status_only_after_compaction_starts(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent.compact_should_start = False
    agent.compact_result = {
        "success": False,
        "reason": "Not enough messages (1, need 4)",
    }

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/compact", "thread_id": "thread-compact"},
    ) as response:
        skipped_body = "".join(response.iter_text())

    skipped_events = _sse_events(skipped_body)
    assert response.status_code == 200
    assert not any(event["type"] == "compacting" for event in skipped_events)
    assert any(
        event["type"] == "response" and "Could not compact" in event["content"]
        for event in skipped_events
    )

    agent.compact_should_start = True
    agent.compact_result = {
        "success": True,
        "messages_removed": 5,
        "summary": "prior state",
    }

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/compact", "thread_id": "thread-compact"},
    ) as response:
        compacted_body = "".join(response.iter_text())

    compacted_events = _sse_events(compacted_body)
    assert response.status_code == 200
    assert compacted_events[0] == {
        "type": "compacting",
        "message": "Compacting thread context...",
        "thread_id": "thread-compact",
    }
    assert compacted_events[1]["type"] == "compacted"


def test_chat_stream_compact_forwards_focus_instruction(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent.compact_should_start = True
    agent.compact_result = {"success": True, "messages_removed": 2, "summary": "s"}

    # Trailing text after /compact steers the summary; original case preserved.
    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={
            "message": "/compact Keep the AuthFlow decisions",
            "thread_id": "thread-compact",
        },
    ) as response:
        _ = "".join(response.iter_text())
    assert response.status_code == 200
    assert agent.compact_calls[-1]["priority"] == "Keep the AuthFlow decisions"

    # Bare /compact carries no priority.
    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/compact", "thread_id": "thread-compact"},
    ) as response:
        _ = "".join(response.iter_text())
    assert agent.compact_calls[-1]["priority"] is None
