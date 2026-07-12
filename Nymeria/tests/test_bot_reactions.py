"""Tests for the two-way emoji-reaction core (backlog #45).

Covers the turn-origin registry, the react tool, the reaction guidance block
(and its omit-when-bound rule resolving through the graph authority), the
reply-suppression marker plumbing, and the SSE dispatch routing. Platform bot
behavior lives in test_discord_reactions.py / test_telegram_reactions.py.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from nymeria.core import bot_reactions
from nymeria.core.agent_results import tool_result_extra_events
from nymeria.core.bot_reactions import (
    REPLY_SUPPRESSED_MARKER,
    clear_turn_origin,
    get_turn_origin,
    mark_reply_suppressed,
    reply_suppressed,
    set_turn_origin,
)
from nymeria.core.event_bus import AGENT_STREAM_AUTONOMOUS_EVENT_TYPES
from nymeria.tools.react import _react_impl, react, reaction_guidance_block
from nymeria.tools.schema_render import render_tool_args_schema
from nymeria.triggers.sse_consumer import dispatch_event


THREAD = "discord_123_456"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_turn_origin(THREAD)
    yield
    clear_turn_origin(THREAD)


def _config(thread_id: str = THREAD, user_id: str = "u1") -> dict:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


# ---------------------------------------------------------------------------
# Turn-origin registry
# ---------------------------------------------------------------------------


def test_turn_origin_latest_wins_and_resets_suppression():
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="1", kind="message"
    )
    assert mark_reply_suppressed(THREAD) is True
    assert reply_suppressed(THREAD) is True

    # A new origin (next turn) resets the suppress flag and the message id.
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="2", kind="reaction"
    )
    origin = get_turn_origin(THREAD)
    assert origin is not None
    assert origin["message_id"] == "2"
    assert origin["kind"] == "reaction"
    assert reply_suppressed(THREAD) is False


def test_turn_origin_absent_thread():
    assert get_turn_origin("no-such-thread") is None
    assert mark_reply_suppressed("no-such-thread") is False
    assert reply_suppressed("no-such-thread") is False


# ---------------------------------------------------------------------------
# The react tool
# ---------------------------------------------------------------------------


def test_react_requires_platform_origin():
    result = asyncio.run(_react_impl("👍", False, None, _config()))
    assert result.startswith("[react error]")
    assert "chat-platform origin" in result


def test_react_publishes_reaction_request(monkeypatch):
    published: list[dict] = []

    def _fake_publish(**kwargs):
        published.append(kwargs)

    react_module = importlib.import_module("nymeria.tools.react")

    monkeypatch.setattr(react_module, "_publish_reaction_request", _fake_publish)
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )

    result = asyncio.run(_react_impl("👍", False, None, _config()))
    assert "Reacted with 👍" in result
    assert REPLY_SUPPRESSED_MARKER not in result
    assert reply_suppressed(THREAD) is False
    assert published == [{
        "thread_id": THREAD,
        "user_id": "u1",
        "platform": "discord",
        "channel_id": "456",
        "message_id": "777",
        "emoji": "👍",
    }]


def test_react_message_id_override_and_suppress(monkeypatch):
    react_module = importlib.import_module("nymeria.tools.react")

    published: list[dict] = []
    monkeypatch.setattr(
        react_module,
        "_publish_reaction_request",
        lambda **kwargs: published.append(kwargs),
    )
    set_turn_origin(
        THREAD, platform="telegram", channel_id="555", message_id="10", kind="reaction"
    )

    result = asyncio.run(_react_impl("❤", True, "99", _config()))
    assert published[0]["message_id"] == "99"
    assert published[0]["platform"] == "telegram"
    assert REPLY_SUPPRESSED_MARKER in result
    assert reply_suppressed(THREAD) is True


def test_react_rejects_empty_and_oversized_emoji():
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )
    assert asyncio.run(_react_impl("", False, None, _config())).startswith(
        "[react error]"
    )
    assert asyncio.run(
        _react_impl("x" * 100, False, None, _config())
    ).startswith("[react error]")


def test_react_tool_invocable_via_ainvoke(monkeypatch):
    """The bound-call surface: ainvoke with the standard configurable."""
    react_module = importlib.import_module("nymeria.tools.react")

    published: list[dict] = []
    monkeypatch.setattr(
        react_module,
        "_publish_reaction_request",
        lambda **kwargs: published.append(kwargs),
    )
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )
    result = asyncio.run(react.ainvoke({"emoji": "🎉"}, _config()))
    assert "Reacted with 🎉" in result
    assert published[0]["emoji"] == "🎉"


def test_react_is_catalog_tool():
    from nymeria.tools import CATALOG_TOOLS, SEED_TOOLS

    assert "react" in CATALOG_TOOLS
    assert "react" not in {t.name for t in SEED_TOOLS}


# ---------------------------------------------------------------------------
# Guidance block: omit-when-bound resolves through the graph authority
# ---------------------------------------------------------------------------


class _AgentWithTools:
    def __init__(self, tools: list, fail: bool = False):
        self._tools = tools
        self._fail = fail
        self.calls: list = []

    def _select_tools_for_graph(self, user_id: str, thread_id: str):
        self.calls.append((user_id, thread_id))
        if self._fail:
            raise RuntimeError("boom")
        return self._tools, None


def test_guidance_block_unbound_includes_schema_and_invoke_recipe():
    agent = _AgentWithTools([])
    block = reaction_guidance_block(agent, "u1", THREAD)
    assert agent.calls == [("u1", THREAD)]
    assert "tool_invoke" in block
    # The schema is the exact schema_render output tool_search would show.
    assert render_tool_args_schema(react) in block
    assert "suppress_reply" in block
    assert 'tool_manage(action="enable"' in block


def test_guidance_block_bound_omits_schema():
    block = reaction_guidance_block(_AgentWithTools([react]), "u1", THREAD)
    assert "Schema:" not in block
    assert render_tool_args_schema(react) not in block
    assert "bound" in block


def test_guidance_block_fails_open_to_full_block():
    block = reaction_guidance_block(_AgentWithTools([], fail=True), "u1", THREAD)
    assert render_tool_args_schema(react) in block


# ---------------------------------------------------------------------------
# Reply-suppression marker -> stream event
# ---------------------------------------------------------------------------


def test_marker_in_react_result_emits_reply_suppressed_event():
    events = tool_result_extra_events(
        "react", f"Reacted with 👍. {REPLY_SUPPRESSED_MARKER}", "call-1"
    )
    assert {"type": "reply_suppressed", "tool_call_id": "call-1"} in events


def test_marker_through_tool_invoke_emits_event():
    events = tool_result_extra_events(
        "tool_invoke", f"Reacted with 👍. {REPLY_SUPPRESSED_MARKER}", "call-2"
    )
    assert any(e["type"] == "reply_suppressed" for e in events)


def test_marker_in_other_tool_result_is_inert():
    events = tool_result_extra_events(
        "fetch_url_nymeria", f"page says {REPLY_SUPPRESSED_MARKER}", "call-3"
    )
    assert not any(e["type"] == "reply_suppressed" for e in events)


def test_react_result_without_marker_is_inert():
    events = tool_result_extra_events("react", "Reacted with 👍.", "call-4")
    assert not any(e["type"] == "reply_suppressed" for e in events)


def test_reply_suppressed_mirrors_to_autonomous_bus():
    assert "reply_suppressed" in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES


def test_reaction_request_event_type_constant():
    assert bot_reactions.REACTION_REQUEST_EVENT == "reaction_request"


# ---------------------------------------------------------------------------
# SSE dispatch routing
# ---------------------------------------------------------------------------


class _RecordingHandler:
    """Minimal SSEEventHandler with the optional suppression callback."""

    def __init__(self) -> None:
        self.suppressed = 0
        self.chunks: list[str] = []
        self.flushes = 0

    async def flush_text(self, final: bool = False) -> None:
        self.flushes += 1

    async def on_thinking(self) -> None: ...
    async def on_response_chunk(self, content: str) -> None:
        self.chunks.append(content)

    async def on_compacting(self, message: str) -> None: ...
    async def on_compacted(self, summary, messages_removed, title) -> None: ...
    async def on_tool_call(self, name, args, call_id, count) -> None: ...
    async def on_tool_result(self, call_id, result, attachments) -> None: ...
    async def on_tool_reload(self, tools, ttl) -> None: ...
    async def on_workspace_artifact(self, path: str) -> None: ...
    async def on_error(self, content: str) -> None: ...
    async def on_iteration_limit(self, content: str) -> None: ...
    async def on_done(self, tool_call_count: int) -> None: ...
    async def on_stream_end(self, tool_call_count: int) -> None: ...

    async def on_reply_suppressed(self) -> None:
        self.suppressed += 1


class _LegacyHandler(_RecordingHandler):
    """Handler WITHOUT on_reply_suppressed (pre-existing consumers)."""

    def __getattribute__(self, name: str) -> Any:
        if name == "on_reply_suppressed":
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_dispatch_event_routes_reply_suppressed():
    handler = _RecordingHandler()
    count = asyncio.run(
        dispatch_event({"type": "reply_suppressed"}, handler, 0)
    )
    assert count == 0
    assert handler.suppressed == 1
    # Suppression must NOT flush (a flush would post the pending buffer).
    assert handler.flushes == 0


def test_dispatch_event_reply_suppressed_optional_for_legacy_handlers():
    handler = _LegacyHandler()
    asyncio.run(dispatch_event({"type": "reply_suppressed"}, handler, 0))
    assert handler.suppressed == 0  # no crash, silently skipped


# ---------------------------------------------------------------------------
# Chat-route helper: platform_origin stamping + reaction enrichment
# ---------------------------------------------------------------------------


def test_apply_platform_origin_records_and_enriches():
    from nymeria.api.routers.chat import _apply_platform_origin
    from nymeria.api.schemas.chat import ChatPlatformOrigin, ChatRequest

    agent = _AgentWithTools([])
    request = ChatRequest(
        message="[Reaction] Alice reacted with 👍",
        thread_id=THREAD,
        platform_origin=ChatPlatformOrigin(
            platform="discord", channel_id="456", message_id="777", kind="reaction"
        ),
    )
    message = _apply_platform_origin(agent, request, THREAD, "u1", request.message)

    origin = get_turn_origin(THREAD)
    assert origin is not None and origin["message_id"] == "777"
    assert message.startswith(request.message)
    assert render_tool_args_schema(react) in message


def test_apply_platform_origin_message_kind_no_enrichment():
    from nymeria.api.routers.chat import _apply_platform_origin
    from nymeria.api.schemas.chat import ChatPlatformOrigin, ChatRequest

    request = ChatRequest(
        message="hello",
        thread_id=THREAD,
        platform_origin=ChatPlatformOrigin(
            platform="telegram", channel_id="555", message_id="10"
        ),
    )
    message = _apply_platform_origin(
        _AgentWithTools([]), request, THREAD, "u1", request.message
    )
    assert message == "hello"
    origin = get_turn_origin(THREAD)
    assert origin is not None and origin["platform"] == "telegram"


def test_apply_platform_origin_noop_without_origin():
    from nymeria.api.routers.chat import _apply_platform_origin
    from nymeria.api.schemas.chat import ChatRequest

    request = ChatRequest(message="hello", thread_id=THREAD)
    message = _apply_platform_origin(
        _AgentWithTools([]), request, THREAD, "u1", "hello"
    )
    assert message == "hello"
    assert get_turn_origin(THREAD) is None


def test_turn_reply_suppressed_gated_on_platform_origin():
    from nymeria.api.routers.chat import _turn_reply_suppressed
    from nymeria.api.schemas.chat import ChatPlatformOrigin, ChatRequest

    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )
    mark_reply_suppressed(THREAD)

    with_origin = ChatRequest(
        message="m",
        thread_id=THREAD,
        platform_origin=ChatPlatformOrigin(
            platform="discord", channel_id="456", message_id="777"
        ),
    )
    without_origin = ChatRequest(message="m", thread_id=THREAD)
    # NOTE: _turn_reply_suppressed reads the registry, which with_origin's
    # request would have reset at turn start; here we assert the gate only.
    assert _turn_reply_suppressed(without_origin, THREAD) is False
    assert _turn_reply_suppressed(with_origin, THREAD) is True
