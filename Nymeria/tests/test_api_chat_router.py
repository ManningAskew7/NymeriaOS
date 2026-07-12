"""Focused tests for the extracted chat API router."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nymeria.core.accounts import AccountsRepo


class _FakeThreadMeta:
    def __init__(
        self,
        *,
        title: str | None = None,
        title_source: str | None = None,
        platform: str | None = None,
        platform_meta: dict[str, Any] | None = None,
    ) -> None:
        self.title = title
        self.title_source = title_source
        self.platform = platform
        self.platform_meta = dict(platform_meta or {})


class FakeThreadMetadataManager:
    def __init__(self) -> None:
        self.auto_title_calls: list[tuple[str, str, str]] = []
        self.threads: dict[tuple[str, str], _FakeThreadMeta] = {}

    def auto_title(self, user_id: str, thread_id: str, message: str) -> str:
        self.auto_title_calls.append((user_id, thread_id, message))
        return "Auto Title"

    def upsert_thread(
        self,
        user_id: str,
        thread_id: str,
        *,
        title: str | None = None,
        title_source: str | None = None,
        platform: str | None = None,
        platform_meta: dict[str, Any] | None = None,
    ) -> _FakeThreadMeta:
        # Merge semantics mirror the real ThreadMetadataManager: only provided
        # fields are written, so a title-less platform_meta upsert (e.g. the
        # idle-clock refresh) preserves the existing title.
        meta = self.threads.get((user_id, thread_id))
        if meta is None:
            meta = _FakeThreadMeta()
            self.threads[(user_id, thread_id)] = meta
        if title is not None:
            meta.title = title
        if title_source is not None:
            meta.title_source = title_source
        if platform is not None:
            meta.platform = platform
        if platform_meta is not None:
            meta.platform_meta = dict(platform_meta)
        return meta

    def get_thread(self, user_id: str, thread_id: str) -> _FakeThreadMeta | None:
        return self.threads.get((user_id, thread_id))


class _FakeThreadLocks:
    """Scripted busy-probe: pops queued responses, then reports idle."""

    def __init__(self) -> None:
        self.busy_responses: list[bool] = []

    def is_thread_busy(self, thread_id: str) -> bool:
        if self.busy_responses:
            return self.busy_responses.pop(0)
        return False


class FakeChatAgent:
    def __init__(self, data_dir: Path) -> None:
        from nymeria.core.hook_manager import HookManager

        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.settings = SimpleNamespace(
            llm_provider="fallback-provider", llm_model="fallback-model"
        )
        self._thread_locks = _FakeThreadLocks()
        self.hook_manager = HookManager(data_dir)
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
        # Record the holder-turn callback as a presence flag so the exact
        # kwargs pins below stay comparable (the real value is a closure).
        # Deliberately NOT invoked: these tests pin the pre-buffer wire
        # shape; the tee/turn_started behavior is pinned in
        # test_turn_stream_buffer.py.
        kwargs["_on_turn_started"] = kwargs.get("_on_turn_started") is not None
        # Same presence-flag treatment: the route mints a random uuid per
        # turn (None on /resume), so pin "a string id was passed" instead of
        # the value.
        kwargs["_turn_user_message_id"] = isinstance(
            kwargs.get("_turn_user_message_id"), str
        )
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
        # Mirror the real LLMConfig shape (both provider and model present);
        # None here means "inherit the settings default".
        return SimpleNamespace(provider=None, model=None)


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
            "_resume_halted_turn": False,
        }
    ]
    assert agent.accounts_repo.get_thread_owner("thread-sync") == "alice"


def test_chat_sync_runs_turn_off_event_loop(
    tmp_path: Path,
    api_client_builder,
):
    # The whole sync turn must dispatch via to_thread: running it inline in
    # the async endpoint would freeze every SSE stream and probe in the
    # process for the turn's duration.
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    on_loop: list[bool] = []
    original_chat = agent.chat

    def _recording_chat(message: str, **kwargs: Any) -> str:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return original_chat(message, **kwargs)

    agent.chat = _recording_chat

    response = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "hello", "thread_id": "thread-off-loop"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "sync response"
    assert on_loop == [False]


def test_run_sync_turn_tool_count_is_per_thread_under_concurrency():
    # Two sync turns running concurrently on worker threads must each report
    # their own tool count. The barrier forces both turns' writes to land
    # before either read, which the legacy shared _last_chat_tool_calls
    # attribute cannot survive (last writer wins for both).
    import threading

    from nymeria.api.routers.chat import run_sync_turn_with_tool_count

    class StubAgent:
        def __init__(self) -> None:
            self._chat_turn_local = threading.local()
            self._last_chat_tool_calls = 0
            self._barrier = threading.Barrier(2)

        def chat(self, message: str, **kwargs: Any) -> str:
            count = int(message)
            self._chat_turn_local.tool_calls = count
            self._last_chat_tool_calls = count
            self._barrier.wait(timeout=5)
            return f"resp-{count}"

    agent = StubAgent()

    async def _scenario():
        return await asyncio.gather(
            asyncio.to_thread(run_sync_turn_with_tool_count, agent, "1"),
            asyncio.to_thread(run_sync_turn_with_tool_count, agent, "2"),
        )

    results = asyncio.run(_scenario())
    assert sorted(results) == [("resp-1", 1), ("resp-2", 2)]


def test_run_sync_turn_tool_count_falls_back_to_shared_attribute():
    # Agents without the per-thread metadata channel (test fakes, stubs)
    # still report via the legacy shared attribute.
    from nymeria.api.routers.chat import run_sync_turn_with_tool_count

    class LegacyAgent:
        _last_chat_tool_calls = 7

        def chat(self, message: str, **kwargs: Any) -> str:
            return "r"

    assert run_sync_turn_with_tool_count(LegacyAgent(), "m") == ("r", 7)


def test_startup_handler_resizes_default_executor(
    tmp_path: Path,
    api_client_builder,
):
    # create_api_app registers a startup handler that installs a right-sized
    # default executor (the stock one is only min(32, cores + 4) threads and
    # carries nearly all of the runtime's blocking work).
    settings = api_client_builder.settings(
        tmp_path, default_executor_max_workers=9
    )
    agent = FakeChatAgent(tmp_path)
    client = api_client_builder.client(agent, settings)

    handlers = [
        h
        for h in client.app.router.on_startup
        if getattr(h, "__name__", "") == "_resize_default_executor"
    ]
    assert len(handlers) == 1

    async def _scenario():
        import threading

        await handlers[0]()
        loop = asyncio.get_running_loop()
        name = await loop.run_in_executor(
            None, lambda: threading.current_thread().name
        )
        return name, loop._default_executor._max_workers

    name, max_workers = asyncio.run(_scenario())
    assert name.startswith("nym-default")
    assert max_workers == 9


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
            "_on_turn_started": True,
            "_resume_halted_turn": False,
            "_turn_user_message_id": True,
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


def _quick_thread_id_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "dispatched":
            return event["target_thread_id"]
    raise AssertionError(f"no dispatched event in {events}")


def test_quick_stream_creates_temporary_thread_and_streams_inline(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/quick summarize the news", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = _sse_events(body)
    quick_id = _quick_thread_id_from_events(events)
    assert quick_id.startswith("spawned-quick-")

    # The turn runs on the fresh thread with the mention prefix stripped.
    assert agent.astream_calls == [
        {
            "message": "summarize the news",
            "thread_id": quick_id,
            "user_id": "alice",
            "attachments": None,
            "images": None,
            "force_unsupported_attachments": False,
            "_is_self_invoke": False,
            "_trigger_override": None,
            "source": "user",
            "source_id": None,
            "source_label": "alice",
            "_on_turn_started": True,
            "_resume_halted_turn": False,
            "_turn_user_message_id": True,
        }
    ]

    # Every streamed event is re-tagged to the caller thread (inline display),
    # while the dispatch envelope points at the fresh thread.
    dispatched = next(e for e in events if e["type"] == "dispatched")
    assert dispatched["thread_id"] == "caller-1"
    assert dispatched["target_thread_id"] == quick_id
    for event in events:
        if event["type"] in {"thinking", "response", "done"}:
            assert event["thread_id"] == "caller-1"

    # The continue-footer is a trailing response chunk, before done, that names
    # the fresh thread so the user can resume it.
    assert [e["type"] for e in events][-1] == "done"
    footer = events[-2]
    assert footer["type"] == "response"
    assert "Continue this thread" in footer["content"]
    assert quick_id in footer["content"]
    assert footer["thread_id"] == "caller-1"

    # The fresh thread persists as a temporary (idle-swept) thread owned by the
    # user; nothing runs on the caller thread.
    meta = agent.thread_metadata_manager.get_thread("alice", quick_id)
    assert meta is not None
    assert meta.platform_meta["lifetime"] == "temporary"
    assert meta.platform_meta["idle_timeout_hours"] == "24"
    assert meta.platform_meta["last_active_at"]
    assert agent.accounts_repo.get_thread_owner(quick_id) == "alice"
    assert all(call["thread_id"] == quick_id for call in agent.astream_calls)


def test_quick_stream_empty_prompt_errors_without_creating_thread(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/quick", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = _sse_events(body)
    assert any(
        e["type"] == "response" and "Usage: `/quick" in e["content"] for e in events
    )
    assert agent.astream_calls == []
    assert agent.thread_metadata_manager.threads == {}


def test_quick_sync_creates_thread_and_appends_footer(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    response = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "/quick hello there", "thread_id": "caller-1"},
    )

    assert response.status_code == 200
    data = response.json()
    quick_id = data["thread_id"]
    assert quick_id.startswith("spawned-quick-")
    assert data["response"].startswith("sync response")
    assert "Continue this thread" in data["response"]
    assert quick_id in data["response"]
    assert agent.chat_calls == [
        {
            "message": "hello there",
            "thread_id": quick_id,
            "user_id": "alice",
            "attachments": None,
            "images": None,
            "force_unsupported_attachments": False,
            "_is_self_invoke": False,
            "_trigger_override": None,
            "source": "user",
            "source_id": None,
            "source_label": "alice",
            "_resume_halted_turn": False,
        }
    ]
    meta = agent.thread_metadata_manager.get_thread("alice", quick_id)
    assert meta is not None
    assert meta.platform_meta["lifetime"] == "temporary"
    assert agent.accounts_repo.get_thread_owner(quick_id) == "alice"


def test_quick_sync_empty_prompt_errors(tmp_path: Path, api_client_builder):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    response = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "/quick", "thread_id": "caller-1"},
    )

    assert response.status_code == 200
    assert "Usage: `/quick" in response.json()["response"]
    assert agent.chat_calls == []
    assert agent.thread_metadata_manager.threads == {}


def test_interactive_turn_on_temporary_spawned_thread_refreshes_idle_clock(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    # A temporary spawned thread with a stale activity timestamp.
    stale = "2000-01-01T00:00:00+00:00"
    agent.thread_metadata_manager.upsert_thread(
        "alice",
        "spawned-quick-existing",
        title="Quick query",
        title_source="auto",
        platform_meta={
            "lifetime": "temporary",
            "idle_timeout_hours": "24",
            "last_active_at": stale,
        },
    )

    # Continuing it with a normal (non-/quick) turn must reset the idle clock,
    # otherwise the sweep could reap an actively-used thread.
    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "keep going", "thread_id": "spawned-quick-existing"},
    ) as response:
        _ = "".join(response.iter_text())

    assert response.status_code == 200
    meta = agent.thread_metadata_manager.get_thread("alice", "spawned-quick-existing")
    assert meta is not None
    assert meta.platform_meta["last_active_at"] != stale
    assert meta.platform_meta["lifetime"] == "temporary"


# --- /done: one-shot DONE-hook arming (backlog #70) --------------------------

def test_done_stream_busy_arms_single_use_hook(tmp_path: Path, api_client_builder):
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent._thread_locks.busy_responses = [True, True]  # probe + race re-check

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/done check the tests", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = _sse_events(body)
    assert any(
        e["type"] == "response" and "Follow-up armed" in e["content"] for e in events
    )
    # No turn ran; the hook is stored, single-use, bound to the busy thread.
    assert agent.astream_calls == []
    (hook,) = agent.hook_manager.get_hooks("alice")
    assert hook.event == "done"
    assert hook.logic.text == "check the tests"
    assert hook.single_use is True
    assert hook.once is True
    assert hook.scope == "thread"
    assert hook.thread_id == "caller-1"
    assert hook.created_by == "user"


def test_done_stream_hooks_disabled_errors_without_arming(
    tmp_path: Path, api_client_builder
):
    # The DONE fire would never pick the hook up with the engine off, so
    # /done must fail honestly instead of acking a dead follow-up.
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent._thread_locks.busy_responses = [True, True]
    agent.settings.hooks_enabled = False

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/done check the tests", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    events = _sse_events(body)
    assert any(
        e["type"] == "response" and "hooks are disabled" in e["content"]
        for e in events
    )
    assert agent.astream_calls == []
    assert agent.hook_manager.get_hooks("alice") == []


def test_done_stream_idle_runs_prompt_now(tmp_path: Path, api_client_builder):
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    # No busy responses queued: the thread is idle (degenerate case).

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/done check the tests", "thread_id": "caller-1"},
    ) as response:
        "".join(response.iter_text())

    assert response.status_code == 200
    assert len(agent.astream_calls) == 1
    assert agent.astream_calls[0]["message"] == "check the tests"
    assert agent.astream_calls[0]["thread_id"] == "caller-1"
    assert agent.hook_manager.get_hooks("alice") == []


def test_done_stream_empty_prompt_is_usage_error(tmp_path: Path, api_client_builder):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/done", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    events = _sse_events(body)
    assert any(
        e["type"] == "response" and "Usage: `/done" in e["content"] for e in events
    )
    assert agent.astream_calls == []
    assert agent.hook_manager.get_hooks("alice") == []


def test_done_stream_race_claimed_back_runs_now(tmp_path: Path, api_client_builder):
    # Turn ends between the busy probe and the create: the delete claim
    # succeeds (the hook never fired), so the prompt runs as a normal turn.
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent._thread_locks.busy_responses = [True, False]

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/done follow up", "thread_id": "caller-1"},
    ) as response:
        "".join(response.iter_text())

    assert len(agent.astream_calls) == 1
    assert agent.astream_calls[0]["message"] == "follow up"
    assert agent.hook_manager.get_hooks("alice") == []


def test_done_stream_race_already_fired_acks(
    tmp_path: Path, api_client_builder, monkeypatch
):
    # Turn ends AND the DONE fire consumes the hook before the re-check: the
    # delete claim fails, so the ack stands (the prompt already ran).
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent._thread_locks.busy_responses = [True, False]
    monkeypatch.setattr(agent.hook_manager, "delete_hook", lambda *a, **k: False)

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/done follow up", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    events = _sse_events(body)
    assert any(
        e["type"] == "response" and "Follow-up armed" in e["content"] for e in events
    )
    assert agent.astream_calls == []


def test_done_sync_parity(tmp_path: Path, api_client_builder):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    # Busy: arm the hook, no turn.
    agent._thread_locks.busy_responses = [True, True]
    resp = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "/done wrap up", "thread_id": "caller-1"},
    )
    assert resp.status_code == 200
    assert "Follow-up armed" in resp.json()["response"]
    assert agent.chat_calls == []
    (hook,) = agent.hook_manager.get_hooks("alice")
    assert hook.single_use is True and hook.logic.text == "wrap up"

    # Idle: the prompt runs as a normal sync turn.
    agent.hook_manager.delete_hook("alice", hook.id)
    resp = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "/done wrap up", "thread_id": "caller-1"},
    )
    assert resp.status_code == 200
    assert len(agent.chat_calls) == 1
    assert agent.chat_calls[0]["message"] == "wrap up"


def test_resume_stream_busy_acks_error_without_queueing(
    tmp_path: Path, api_client_builder
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)
    agent._thread_locks.busy_responses = [True]

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/resume", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = _sse_events(body)
    assert any(
        e["type"] == "response" and "already running" in e["content"] for e in events
    )
    # /resume must never run or queue a turn on a busy thread.
    assert agent.astream_calls == []


def test_resume_stream_idle_runs_message_less_resume(
    tmp_path: Path, api_client_builder
):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    with client.stream(
        "POST",
        "/chat",
        headers=api_client_builder.auth(token),
        json={"message": "/resume", "thread_id": "caller-1"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert len(agent.astream_calls) == 1
    call = agent.astream_calls[0]
    # Message-less resume: nothing is added to history, the agent-side
    # resume mode does the validation and re-drive.
    assert call["message"] == ""
    assert call["_resume_halted_turn"] is True
    assert call["thread_id"] == "caller-1"
    # A resume adds no HumanMessage, so no anchor id is minted (the
    # presence-flag in FakeChatAgent.astream records None as False).
    assert call["_turn_user_message_id"] is False
    # The streamed continuation still flows to the client.
    assert any(e["type"] == "response" for e in _sse_events(body))


def test_resume_sync_parity(tmp_path: Path, api_client_builder):
    client, agent, token = _chat_client(tmp_path, api_client_builder)

    # Busy: explicit error, never queued.
    agent._thread_locks.busy_responses = [True]
    resp = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "/resume", "thread_id": "caller-1"},
    )
    assert resp.status_code == 200
    assert "already running" in resp.json()["response"]
    assert agent.chat_calls == []

    # Idle: the message-less resume runs as a sync turn.
    resp = client.post(
        "/chat/sync",
        headers=api_client_builder.auth(token),
        json={"message": "/resume", "thread_id": "caller-1"},
    )
    assert resp.status_code == 200
    assert len(agent.chat_calls) == 1
    assert agent.chat_calls[0]["message"] == ""
    assert agent.chat_calls[0]["_resume_halted_turn"] is True
