"""Unit tests for the ui_prompt tool: event publish shape, happy-path
resolve, timeout, abort cascade, and input validation (mirrors
test_chrome_browser_tools.py)."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.runnables import RunnableConfig

from nymeria.core.event_bus import EventBus, set_event_bus
from nymeria.core.ui_prompt_coordinator import get_ui_prompt_coordinator
from nymeria.tools.ui_prompt import UI_PROMPT_TOOLS, ui_prompt


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch):
    """Fresh coordinator + fresh in-memory bus between tests."""
    import nymeria.core.ui_prompt_coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "_coordinator", None)
    set_event_bus(EventBus())
    yield


def _config(user_id: str = "u1", thread_id: str = "t1") -> RunnableConfig:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def test_tool_group_and_catalog_registration() -> None:
    assert [t.name for t in UI_PROMPT_TOOLS] == ["ui_prompt"]
    from nymeria.tools import CATALOG_TOOLS, SEED_TOOLS
    assert "ui_prompt" in CATALOG_TOOLS
    assert all(t.name != "ui_prompt" for t in SEED_TOOLS)


def test_empty_html_rejected() -> None:
    async def run() -> str:
        return await ui_prompt.ainvoke({"html": "   "}, config=_config())

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "non-empty" in out
    assert get_ui_prompt_coordinator().pending_count() == 0


def test_oversized_html_rejected() -> None:
    async def run() -> str:
        return await ui_prompt.ainvoke(
            {"html": "<p>" + "x" * (256 * 1024) + "</p>"},
            config=_config(),
        )

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "too large" in out
    assert get_ui_prompt_coordinator().pending_count() == 0


def test_happy_path_resolves_via_coordinator() -> None:
    """Resolve the registered future and confirm the tool returns its JSON."""
    async def run() -> str:
        async def resolve_later() -> None:
            coord = get_ui_prompt_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            else:
                raise AssertionError("tool never registered a prompt")
            with coord._lock:
                prompt_id = next(iter(coord._prompts))
            coord.resolve(
                prompt_id,
                {"ok": True, "status": "submitted", "values": {"plan": "pro"}},
            )

        resolver = asyncio.create_task(resolve_later())
        result = await ui_prompt.ainvoke(
            {"html": "<form><input name='plan'></form>", "title": "Pick a plan"},
            config=_config(),
        )
        await resolver
        return result

    raw = asyncio.run(run())
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["status"] == "submitted"
    assert payload["values"]["plan"] == "pro"


def test_timeout_returns_error_discards_and_publishes_closure(monkeypatch) -> None:
    """Force a tiny timeout; expect a clean error string, a removed entry,
    and a ui_prompt_result closure event so open modals retract."""
    # importlib: `nymeria.tools` re-exports the `ui_prompt` TOOL under the
    # submodule's name, so plain `import ... as mod` would grab the tool.
    import importlib
    mod = importlib.import_module("nymeria.tools.ui_prompt")
    monkeypatch.setattr(mod, "_MIN_TIMEOUT_SECONDS", 0)
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    async def run() -> str:
        return await ui_prompt.ainvoke(
            {"html": "<p>hi</p>", "timeout_seconds": 0},
            config=_config(),
        )

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "timed out" in out
    assert get_ui_prompt_coordinator().pending_count() == 0
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    closures = [e for e in seen if e.event_type == "ui_prompt_result"]
    assert len(closures) == 1
    assert closures[0].data["status"] == "timeout"


def test_abort_thread_releases_pending_prompt() -> None:
    """When the cascade aborts the thread, an awaiting tool resolves with
    status='aborted' instead of waiting for its timeout."""
    async def run() -> str:
        async def abort_later() -> None:
            coord = get_ui_prompt_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            coord.abort_thread("t1")

        aborter = asyncio.create_task(abort_later())
        result = await ui_prompt.ainvoke(
            {"html": "<p>waiting</p>"},
            config=_config(),
        )
        await aborter
        return result

    raw = asyncio.run(run())
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["status"] == "aborted"


def test_publishes_ui_prompt_event_with_expected_shape() -> None:
    """Confirm the ui_prompt event lands on the bus with the fields the
    desktop modal consumes, including the clamped timeout."""
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    async def run() -> None:
        async def resolve_when_published() -> None:
            coord = get_ui_prompt_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            with coord._lock:
                prompt_id = next(iter(coord._prompts))
            coord.resolve(prompt_id, {"ok": True, "status": "submitted", "values": {}})

        resolver = asyncio.create_task(resolve_when_published())
        await ui_prompt.ainvoke(
            {
                "html": "<form><input name='q'></form>",
                "title": "Quick question",
                "timeout_seconds": 5,  # below the min; must clamp up to 15
            },
            config=_config(),
        )
        await resolver

    asyncio.run(run())
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    types = [e.event_type for e in seen]
    assert "ui_prompt" in types
    event = next(e for e in seen if e.event_type == "ui_prompt")
    assert event.user_id == "u1"
    assert event.thread_id == "t1"
    assert event.data["title"] == "Quick question"
    assert event.data["html"] == "<form><input name='q'></form>"
    assert event.data["timeout_seconds"] == 15
    assert event.data["prompt_id"].startswith("uip_")
    assert "expires_at" in event.data


def test_timeout_clamped_to_max() -> None:
    """A huge timeout clamps to MAX_TIMEOUT_SECONDS on the published event."""
    from nymeria.core.ui_prompt_coordinator import MAX_TIMEOUT_SECONDS

    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    async def run() -> None:
        async def resolve_soon() -> None:
            coord = get_ui_prompt_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            with coord._lock:
                prompt_id = next(iter(coord._prompts))
            coord.resolve(prompt_id, {"ok": False, "status": "cancelled"})

        resolver = asyncio.create_task(resolve_soon())
        await ui_prompt.ainvoke(
            {"html": "<p>x</p>", "timeout_seconds": 99999},
            config=_config(),
        )
        await resolver

    asyncio.run(run())
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    event = next(e for e in seen if e.event_type == "ui_prompt")
    assert event.data["timeout_seconds"] == MAX_TIMEOUT_SECONDS
