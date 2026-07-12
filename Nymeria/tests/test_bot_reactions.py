"""Tests for the two-way emoji-reaction core (backlog #45).

Covers the turn-origin registry, the react tool, the reaction guidance block
(and its omit-when-bound rule resolving through the graph authority), the
reply-suppression marker plumbing, and the SSE dispatch routing. Platform bot
behavior lives in test_discord_reactions.py / test_telegram_reactions.py.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
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
    bot_reactions._recent_reaction_fires.clear()
    yield
    clear_turn_origin(THREAD)
    bot_reactions._recent_reaction_fires.clear()


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
    assert "Queued a 👍 reaction" in result
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
    assert "Queued a 🎉 reaction" in result
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


def test_guidance_block_omitted_when_react_disabled_on_thread():
    # disabled_tools would make the nudged tool_invoke call refuse via the
    # same deferred gate, so no block is rendered at all.
    agent = _AgentWithTools([])
    agent.thread_config_manager = SimpleNamespace(
        get_config=lambda thread_id: SimpleNamespace(disabled_tools=["react"])
    )
    assert reaction_guidance_block(agent, "u1", THREAD) == ""


# ---------------------------------------------------------------------------
# Reply-suppression: marker + registry flag -> stream event
# ---------------------------------------------------------------------------


def _suppressed_thread() -> None:
    """Simulate a real react(suppress_reply=true) call on THREAD."""
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )
    mark_reply_suppressed(THREAD)


def test_marker_with_registry_flag_emits_reply_suppressed_event():
    _suppressed_thread()
    events = tool_result_extra_events(
        "react", f"Queued. {REPLY_SUPPRESSED_MARKER}", "call-1", THREAD
    )
    assert {"type": "reply_suppressed", "tool_call_id": "call-1"} in events


def test_marker_through_tool_invoke_of_react_emits_event():
    # The deferred path: tool_invoke(react) relays react's own result text,
    # and the react implementation set the registry flag.
    _suppressed_thread()
    events = tool_result_extra_events(
        "tool_invoke", f"Queued. {REPLY_SUPPRESSED_MARKER}", "call-2", THREAD
    )
    assert any(e["type"] == "reply_suppressed" for e in events)


def test_foreign_marker_via_tool_invoke_is_inert_without_registry_flag():
    # tool_invoke of ANY OTHER tool relays the target's text verbatim: a
    # fetched web page or MCP tool echoing the marker must never suppress
    # the bot's reply. The registry flag, set only by the real react tool,
    # is the authority.
    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )
    assert reply_suppressed(THREAD) is False
    events = tool_result_extra_events(
        "tool_invoke", f"page says {REPLY_SUPPRESSED_MARKER}", "call-3", THREAD
    )
    assert not any(e["type"] == "reply_suppressed" for e in events)


def test_marker_in_other_tool_result_is_inert_even_with_flag():
    # Name gate as defense-in-depth: even mid-suppressed-turn, a direct
    # foreign tool result carrying the marker emits nothing.
    _suppressed_thread()
    events = tool_result_extra_events(
        "fetch_url_nymeria", f"page says {REPLY_SUPPRESSED_MARKER}", "call-4", THREAD
    )
    assert not any(e["type"] == "reply_suppressed" for e in events)


def test_marker_without_thread_id_is_inert():
    # No thread context = fail closed, never emit.
    _suppressed_thread()
    events = tool_result_extra_events(
        "react", f"Queued. {REPLY_SUPPRESSED_MARKER}", "call-5"
    )
    assert not any(e["type"] == "reply_suppressed" for e in events)


def test_react_result_without_marker_is_inert():
    _suppressed_thread()
    events = tool_result_extra_events(
        "react", "Queued a 👍 reaction.", "call-6", THREAD
    )
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
# Chat-route helpers: privilege gate, per-turn stamping/clearing, enrichment
# ---------------------------------------------------------------------------


def test_privileged_platform_caller():
    from nymeria.api.routers.chat import _privileged_platform_caller

    # Bots and the worker: admin service token acting as the linked user.
    assert _privileged_platform_caller(
        SimpleNamespace(via_act_as=True, role="user")
    ) is True
    # Direct admin token.
    assert _privileged_platform_caller(
        SimpleNamespace(via_act_as=False, role="admin")
    ) is True
    # Everyone else.
    assert _privileged_platform_caller(
        SimpleNamespace(via_act_as=False, role="user")
    ) is False


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
    message = _apply_platform_origin(
        agent, request, THREAD, "u1", request.message, privileged=True
    )

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
        _AgentWithTools([]), request, THREAD, "u1", request.message, privileged=True
    )
    assert message == "hello"
    origin = get_turn_origin(THREAD)
    assert origin is not None and origin["platform"] == "telegram"


def test_apply_platform_origin_clears_stale_origin_without_field():
    # Per-turn honesty: a turn without platform_origin (desktop, CLI, worker
    # relay) clears the previous bot turn's origin, so react errors instead
    # of reacting to an ancient message.
    from nymeria.api.routers.chat import _apply_platform_origin
    from nymeria.api.schemas.chat import ChatRequest

    set_turn_origin(
        THREAD, platform="discord", channel_id="456", message_id="777", kind="message"
    )
    request = ChatRequest(message="hello", thread_id=THREAD)
    message = _apply_platform_origin(
        _AgentWithTools([]), request, THREAD, "u1", "hello", privileged=False
    )
    assert message == "hello"
    assert get_turn_origin(THREAD) is None


def test_apply_platform_origin_ignored_for_non_privileged_caller():
    # A non-admin direct API caller cannot fabricate an origin (cross-chat
    # reaction write primitive): the field is silently ignored, the entry is
    # cleared, and react then errors cleanly with no origin.
    from nymeria.api.routers.chat import _apply_platform_origin
    from nymeria.api.schemas.chat import ChatPlatformOrigin, ChatRequest

    request = ChatRequest(
        message="hello",
        thread_id=THREAD,
        platform_origin=ChatPlatformOrigin(
            platform="telegram", channel_id="someone-elses-chat", message_id="666"
        ),
    )
    message = _apply_platform_origin(
        _AgentWithTools([]), request, THREAD, "u1", "hello", privileged=False
    )
    assert message == "hello"
    assert get_turn_origin(THREAD) is None

    result = asyncio.run(_react_impl("👍", False, None, _config()))
    assert result.startswith("[react error]")


def test_turn_reply_suppressed_gated_on_origin_and_privilege():
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
    # request would have reset at turn start; here we assert the gates only.
    assert _turn_reply_suppressed(without_origin, THREAD, privileged=True) is False
    assert _turn_reply_suppressed(with_origin, THREAD, privileged=False) is False
    assert _turn_reply_suppressed(with_origin, THREAD, privileged=True) is True


# ---------------------------------------------------------------------------
# Reaction-fire debounce (bot-side)
# ---------------------------------------------------------------------------


def test_debounce_reaction_fire_ttl_and_key_scope():
    from nymeria.core.bot_reactions import (
        REACTION_DEBOUNCE_TTL_SECONDS,
        debounce_reaction_fire,
    )

    kw = dict(
        platform="discord",
        channel_id="c1",
        message_id="m1",
        reactor_id="r1",
        emoji="👍",
    )
    assert debounce_reaction_fire(**kw, now=100.0) is False
    # Toggling the same emoji within the TTL is dropped.
    assert debounce_reaction_fire(**kw, now=120.0) is True
    # After the TTL a deliberate repeat fires again.
    assert (
        debounce_reaction_fire(**kw, now=100.5 + REACTION_DEBOUNCE_TTL_SECONDS)
        is False
    )
    # A different emoji, reactor, or message is its own key.
    assert debounce_reaction_fire(**{**kw, "emoji": "❤"}, now=101.0) is False
    assert debounce_reaction_fire(**{**kw, "reactor_id": "r2"}, now=101.0) is False
    assert debounce_reaction_fire(**{**kw, "message_id": "m2"}, now=101.0) is False
