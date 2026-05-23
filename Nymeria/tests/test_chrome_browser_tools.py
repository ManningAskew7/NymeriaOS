"""Unit tests for chrome_* tools — disconnected fail-fast, happy-path
dispatch, timeout, and abort cascade behaviour."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.runnables import RunnableConfig

from nymeria.core import chrome_subscribers
from nymeria.core.browser_command_coordinator import get_browser_command_coordinator
from nymeria.core.event_bus import set_event_bus, EventBus
from nymeria.tools.chrome_browser import (
    CHROME_BROWSER_TOOLS,
    chrome_navigate,
    chrome_snapshot,
    chrome_tabs,
)


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch):
    """Fresh coordinator + clean chrome-subscriber set between tests."""
    import nymeria.core.browser_command_coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "_coordinator", None)
    set_event_bus(EventBus())
    chrome_subscribers.reset_for_tests()
    yield
    chrome_subscribers.reset_for_tests()


def _config(user_id: str = "u1", thread_id: str = "t1") -> RunnableConfig:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def test_tool_list_has_12_tools() -> None:
    assert len(CHROME_BROWSER_TOOLS) == 12
    names = {t.name for t in CHROME_BROWSER_TOOLS}
    assert names == {
        "chrome_tabs",
        "chrome_navigate",
        "chrome_history",
        "chrome_snapshot",
        "chrome_act",
        "chrome_press_key",
        "chrome_scroll",
        "chrome_extract_text",
        "chrome_screenshot",
        "chrome_console",
        "chrome_dialog",
        "chrome_cdp",
    }


def test_fails_fast_when_no_chrome_connected() -> None:
    """With no Chrome subscriber registered, the tool returns immediately."""
    async def run() -> str:
        return await chrome_navigate.ainvoke(
            {"tab_id": 1, "url": "https://example.com"},
            config=_config(),
        )

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "No Nymeria browser extension connected" in out


def test_happy_path_resolves_via_coordinator() -> None:
    """Mark a Chrome subscriber, then resolve the future from a worker thread."""
    chrome_subscribers.add_chrome_subscriber(
        user_id="u1", subscriber_id="nymeria-browser-test"
    )

    async def run() -> str:
        async def resolve_later() -> None:
            # Wait for the tool to register a future, then resolve it.
            coord = get_browser_command_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            else:
                raise AssertionError("tool never registered a command")
            with coord._lock:
                command_id = next(iter(coord._commands))
            coord.resolve(
                command_id,
                {"ok": True, "status": "success", "data": {"final_url": "https://example.com"}},
            )

        resolver = asyncio.create_task(resolve_later())
        result = await chrome_navigate.ainvoke(
            {"tab_id": 7, "url": "https://example.com"},
            config=_config(),
        )
        await resolver
        return result

    raw = asyncio.run(run())
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["data"]["final_url"] == "https://example.com"


def test_timeout_returns_error_and_discards(monkeypatch) -> None:
    """Force a tiny timeout; expect a clean error string and a removed entry."""
    import nymeria.tools.chrome_browser as mod
    monkeypatch.setitem(mod._TIMEOUTS, "snapshot", 0)
    chrome_subscribers.add_chrome_subscriber(
        user_id="u1", subscriber_id="nymeria-browser-test"
    )

    async def run() -> str:
        return await chrome_snapshot.ainvoke({"tab_id": 1}, config=_config())

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "timed out" in out
    assert get_browser_command_coordinator().pending_count() == 0


def test_abort_thread_releases_pending_command() -> None:
    """When the cascade aborts the thread, an awaiting tool resolves with
    status='aborted' instead of waiting for its timeout."""
    chrome_subscribers.add_chrome_subscriber(
        user_id="u1", subscriber_id="nymeria-browser-test"
    )

    async def run() -> str:
        async def abort_later() -> None:
            coord = get_browser_command_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            coord.abort_thread("t1")

        aborter = asyncio.create_task(abort_later())
        result = await chrome_tabs.ainvoke(
            {"action": "list"},
            config=_config(),
        )
        await aborter
        return result

    raw = asyncio.run(run())
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["status"] == "aborted"


def test_chrome_navigate_publishes_event() -> None:
    """Confirm the browser_command event lands on the bus with the expected
    shape so the extension can consume it."""
    chrome_subscribers.add_chrome_subscriber(
        user_id="u1", subscriber_id="nymeria-browser-test"
    )
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    async def run() -> None:
        async def resolve_when_published() -> None:
            coord = get_browser_command_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            with coord._lock:
                command_id = next(iter(coord._commands))
            coord.resolve(command_id, {"ok": True, "status": "success"})

        resolver = asyncio.create_task(resolve_when_published())
        await chrome_navigate.ainvoke(
            {"tab_id": 42, "url": "https://example.com"},
            config=_config(),
        )
        await resolver

    asyncio.run(run())
    # Drain the queue and find our event.
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    types = [e.event_type for e in seen]
    assert "browser_command" in types
    cmd_event = next(e for e in seen if e.event_type == "browser_command")
    assert cmd_event.user_id == "u1"
    assert cmd_event.thread_id == "t1"
    assert cmd_event.data["command_type"] == "navigate"
    assert cmd_event.data["args"]["tab_id"] == 42
    assert cmd_event.data["args"]["url"] == "https://example.com"
    assert "command_id" in cmd_event.data
    assert "timeout_seconds" in cmd_event.data


def test_chrome_subscriber_tracking_round_trip() -> None:
    assert chrome_subscribers.is_chrome_connected("u1") is False
    chrome_subscribers.add_chrome_subscriber(
        user_id="u1", subscriber_id="nymeria-browser-1"
    )
    chrome_subscribers.add_chrome_subscriber(
        user_id="u1", subscriber_id="nymeria-browser-2"
    )
    assert chrome_subscribers.is_chrome_connected("u1") is True
    chrome_subscribers.remove_chrome_subscriber("nymeria-browser-1")
    assert chrome_subscribers.is_chrome_connected("u1") is True
    chrome_subscribers.remove_chrome_subscriber("nymeria-browser-2")
    assert chrome_subscribers.is_chrome_connected("u1") is False


def test_chrome_client_id_detection() -> None:
    assert chrome_subscribers.is_chrome_client_id("nymeria-browser-abc") is True
    assert chrome_subscribers.is_chrome_client_id("nymeria-desktop-abc") is False
    assert chrome_subscribers.is_chrome_client_id(None) is False
    assert chrome_subscribers.is_chrome_client_id("") is False
