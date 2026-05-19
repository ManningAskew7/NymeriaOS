"""Tests for the drive_with_fanout helper.

Validates that every event yielded from the holder's stream is also
mirrored into each attached PendingPrompt's mailbox so the queuer's
consumer can render the response in real time.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


from nymeria.core.agent_streaming import (
    GraphStreamProcessor,
    drive_with_fanout,
)
from nymeria.core.pending_prompt_queue import (
    FanoutMailbox,
    _SENTINEL_PROMPT_ABSORBED,
    make_pending_prompt,
)


class _FakeGraph:
    def __init__(self, events: list[dict[str, Any]]):
        self.events = events

    async def astream_events(self, input_state, config=None, version=None):
        for event in self.events:
            yield event


def _make_processor() -> GraphStreamProcessor:
    return GraphStreamProcessor(
        thread_id="t1",
        config={"configurable": {"thread_id": "t1"}},
        abort_event=threading.Event(),
        is_self_invoke=False,
        response_parts=[],
        clean_tool_result=lambda result: f"display:{result}",
        tool_result_extra_events=lambda *args: [],
    )


def test_drive_with_fanout_mirrors_to_each_mailbox():
    """Every event yielded by the holder is fanned out to each
    attached prompt's mailbox, in order."""

    events = [
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content="hello ")}},
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content="world")}},
    ]
    graph = _FakeGraph(events)
    processor = _make_processor()

    async def _scenario():
        loop = asyncio.get_running_loop()
        mailbox_a = FanoutMailbox(loop)
        mailbox_b = FanoutMailbox(loop)
        prompt_a = make_pending_prompt(
            message="q1",
            source="user",
            source_id=None,
            source_label="u",
            user_id="u",
            is_autonomous=False,
            fanout_mailbox=mailbox_a,
            consumer_loop=loop,
        )
        prompt_b = make_pending_prompt(
            message="q2",
            source="callable",
            source_id=None,
            source_label="c",
            user_id="u",
            is_autonomous=False,
            fanout_mailbox=mailbox_b,
            consumer_loop=loop,
        )

        yielded: list[dict] = []
        async for evt in drive_with_fanout(
            processor,
            graph,
            {"messages": []},
            [prompt_a, prompt_b],
        ):
            yielded.append(evt)

        # Holder saw the events.
        contents = [e.get("content") for e in yielded if e.get("type") == "response"]
        assert contents == ["hello ", "world"]

        # Each mailbox received the same events.
        async def drain(mb: FanoutMailbox, expect_count: int) -> list[dict]:
            mb.close()
            out = []
            while True:
                evt = await mb.get()
                if evt.get("type") == _SENTINEL_PROMPT_ABSORBED:
                    break
                if evt.get("type") == "fanout_dropped":
                    continue
                out.append(evt)
            return out

        events_a = await drain(mailbox_a, 2)
        events_b = await drain(mailbox_b, 2)
        assert [e.get("content") for e in events_a if e.get("type") == "response"] == [
            "hello ",
            "world",
        ]
        assert events_a == events_b

    asyncio.run(_scenario())


def test_drive_with_fanout_does_not_break_on_prompts_without_mailbox():
    events = [
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content="ok")}},
    ]
    graph = _FakeGraph(events)
    processor = _make_processor()

    async def _scenario():
        # Capture the consumer loop just to mirror real usage even
        # though the autonomous prompt itself has no fanout_mailbox.
        _ = asyncio.get_running_loop()
        autonomous_prompt = make_pending_prompt(
            message="trigger evt",
            source="trigger",
            source_id="trig-1",
            source_label="My Trigger",
            user_id="u",
            is_autonomous=True,
            fanout_mailbox=None,  # no observer
            consumer_loop=None,
        )

        yielded = []
        async for evt in drive_with_fanout(
            processor,
            graph,
            {"messages": []},
            [autonomous_prompt],
        ):
            yielded.append(evt)
        # Should not have raised; the autonomous prompt simply doesn't
        # receive a fanout copy.
        assert yielded  # at least one event yielded
        contents = [e.get("content") for e in yielded if e.get("type") == "response"]
        assert contents == ["ok"]

    asyncio.run(_scenario())


class _Chunk:
    """Minimal stand-in for langchain BaseMessageChunk used by
    GraphStreamProcessor's chat_model_stream handler. Only attributes
    referenced by the processor are needed: ``content``,
    ``tool_call_chunks``, ``additional_kwargs``."""

    def __init__(self, *, content: str = "", tool_call_chunks=None, additional_kwargs=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []
        self.additional_kwargs = additional_kwargs or {}
