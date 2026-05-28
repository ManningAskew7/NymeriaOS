"""Regression tests for Twitch tool runtime registration."""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nymeria.core.twitch_runtime import (
    TwitchRuntimeUnavailable,
    get_twitch_runtime,
    register_twitch_bot,
    unregister_twitch_bot,
)


@pytest.fixture(autouse=True)
def _clear_twitch_runtime():
    unregister_twitch_bot()
    yield
    unregister_twitch_bot()


class _FakeBuffer:
    def __init__(self, label: str):
        self._messages = [
            SimpleNamespace(
                username="viewer",
                display_name="Viewer",
                message=f"{label} message",
                timestamp=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
                user_id="42",
                message_id=f"msg-{label}",
                badges=[],
                is_system=False,
            )
        ]

    def get_recent(self, count: int):
        return self._messages[-count:]

    def __len__(self) -> int:
        return len(self._messages)


class _FakeBot:
    def __init__(self, label: str):
        self._buffer = _FakeBuffer(label)
        self._channel_name = label
        self._stopped = False


def test_twitch_tools_resolve_current_runtime_after_tool_module_reload():
    import nymeria.tools.twitch as twitch_tools

    old_read_chat = twitch_tools.twitch_read_chat
    first_bot = _FakeBot("first")
    register_twitch_bot(first_bot)
    assert "first message" in old_read_chat.func(1)

    reloaded_twitch_tools = importlib.reload(twitch_tools)
    second_bot = _FakeBot("second")
    register_twitch_bot(second_bot)

    assert "second message" in old_read_chat.func(1)
    assert "second message" in reloaded_twitch_tools.twitch_read_chat.func(1)


def test_late_unregister_from_old_bot_does_not_clear_new_runtime():
    old_bot = _FakeBot("old")
    new_bot = _FakeBot("new")

    register_twitch_bot(old_bot)
    register_twitch_bot(new_bot)
    unregister_twitch_bot(old_bot)

    assert "new message" in get_twitch_runtime().read_chat(1)


def test_runtime_reports_clear_error_when_twitch_bot_is_not_registered():
    with pytest.raises(TwitchRuntimeUnavailable, match="pending migration"):
        get_twitch_runtime().read_chat(1)
