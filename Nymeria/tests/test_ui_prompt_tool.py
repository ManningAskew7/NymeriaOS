"""Unit tests for the ui_prompt tool: event publish shape, happy-path
resolve, timeout, tool_timeout clamping, abort cascade, and input validation
(mirrors test_chrome_browser_tools.py)."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig

from nymeria.core.event_bus import EventBus, set_event_bus
from nymeria.core.ui_prompt_coordinator import get_ui_prompt_coordinator
from nymeria.tools.ui_prompt import UI_PROMPT_TOOLS, ui_prompt

# `nymeria.tools` re-exports the `ui_prompt` TOOL under the submodule's name,
# so plain `import nymeria.tools.ui_prompt as mod` would grab the tool object.
tool_mod = importlib.import_module("nymeria.tools.ui_prompt")


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch):
    """Fresh coordinator + fresh in-memory bus between tests."""
    import nymeria.core.ui_prompt_coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "_coordinator", None)
    set_event_bus(EventBus())
    yield


def _config(user_id: str = "u1", thread_id: str = "t1") -> RunnableConfig:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


def _stub_settings(monkeypatch, tool_timeout: int) -> None:
    """Pin the platform tool timeout the cap derives from."""
    monkeypatch.setattr(
        tool_mod, "get_settings", lambda: SimpleNamespace(tool_timeout=tool_timeout)
    )


async def _when_registered(act) -> None:
    """Poll until the tool registers its prompt, then run ``act(prompt_id)``.

    Raises fast if the registration never appears so a broken tool cannot
    stall the test for the full user-wait.
    """
    coord = get_ui_prompt_coordinator()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if coord.pending_count() >= 1:
            break
    else:
        raise AssertionError("tool never registered a prompt")
    with coord._lock:
        prompt_id = next(iter(coord._prompts))
    act(prompt_id)


async def _invoke_with_helper(tool_args: dict, helper_coro) -> str:
    """Run the tool and a registration-triggered helper concurrently.

    The helper is awaited FIRST so its fail-fast AssertionError surfaces
    immediately instead of after the tool's full wait.
    """
    tool_task = asyncio.create_task(ui_prompt.ainvoke(tool_args, config=_config()))
    helper_task = asyncio.create_task(helper_coro)
    try:
        await helper_task
    except BaseException:
        tool_task.cancel()
        raise
    return await tool_task


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
    def resolve(prompt_id: str) -> None:
        get_ui_prompt_coordinator().resolve(
            prompt_id,
            {"ok": True, "status": "submitted", "values": {"plan": "pro"}},
        )

    raw = asyncio.run(
        _invoke_with_helper(
            {"html": "<form><input name='plan'></form>", "title": "Pick a plan"},
            _when_registered(resolve),
        )
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["status"] == "submitted"
    assert payload["values"]["plan"] == "pro"


def test_timeout_returns_error_discards_and_publishes_closure(monkeypatch) -> None:
    """Force a tiny timeout; expect a clean error string, a removed entry,
    and a ui_prompt_result closure event so open modals retract."""
    monkeypatch.setattr(tool_mod, "_MIN_TIMEOUT_SECONDS", 0)
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
    def abort(prompt_id: str) -> None:
        _ = prompt_id
        get_ui_prompt_coordinator().abort_thread("t1")

    raw = asyncio.run(
        _invoke_with_helper({"html": "<p>waiting</p>"}, _when_registered(abort))
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["status"] == "aborted"


def _drain(queue) -> list:
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    return seen


def test_publishes_ui_prompt_event_with_expected_shape(monkeypatch) -> None:
    """Confirm the ui_prompt event lands on the bus with the fields the
    desktop modal consumes, including the clamped timeout."""
    _stub_settings(monkeypatch, tool_timeout=300)
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    def resolve(prompt_id: str) -> None:
        get_ui_prompt_coordinator().resolve(
            prompt_id, {"ok": True, "status": "submitted", "values": {}}
        )

    asyncio.run(
        _invoke_with_helper(
            {
                "html": "<form><input name='q'></form>",
                "title": "Quick question",
                "timeout_seconds": 5,  # below the min; must clamp up to 15
            },
            _when_registered(resolve),
        )
    )
    seen = _drain(queue)
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


def test_timeout_clamped_to_max(monkeypatch) -> None:
    """With tool_timeout at its ceiling, a huge request clamps to
    MAX_TIMEOUT_SECONDS on the published event."""
    from nymeria.core.ui_prompt_coordinator import MAX_TIMEOUT_SECONDS

    _stub_settings(monkeypatch, tool_timeout=900)
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    def resolve(prompt_id: str) -> None:
        get_ui_prompt_coordinator().resolve(prompt_id, {"ok": False, "status": "cancelled"})

    asyncio.run(
        _invoke_with_helper(
            {"html": "<p>x</p>", "timeout_seconds": 99999},
            _when_registered(resolve),
        )
    )
    event = next(e for e in _drain(queue) if e.event_type == "ui_prompt")
    assert event.data["timeout_seconds"] == MAX_TIMEOUT_SECONDS


def test_timeout_clamped_below_platform_tool_timeout(monkeypatch) -> None:
    """SafeToolNode kills any call at settings.tool_timeout, so the effective
    wait must clamp below it (margin included) and the CLAMPED value must be
    what the event advertises, or the client countdown shows time that does
    not exist. Pins the review fix for the dead graceful-timeout branch."""
    _stub_settings(monkeypatch, tool_timeout=300)
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    def resolve(prompt_id: str) -> None:
        get_ui_prompt_coordinator().resolve(prompt_id, {"ok": False, "status": "cancelled"})

    asyncio.run(
        _invoke_with_helper(
            # The old default: exactly tool_timeout, which used to lose the race.
            {"html": "<p>x</p>", "timeout_seconds": 300},
            _when_registered(resolve),
        )
    )
    event = next(e for e in _drain(queue) if e.event_type == "ui_prompt")
    assert event.data["timeout_seconds"] == 300 - tool_mod._TOOL_TIMEOUT_MARGIN_SECONDS


def test_effective_timeout_cap_floors_at_minimum(monkeypatch) -> None:
    """A tiny tool_timeout cannot push the cap below the tool's own minimum."""
    _stub_settings(monkeypatch, tool_timeout=30)
    assert tool_mod._effective_timeout_cap() == 20
    _stub_settings(monkeypatch, tool_timeout=20)
    assert tool_mod._effective_timeout_cap() == tool_mod._MIN_TIMEOUT_SECONDS
