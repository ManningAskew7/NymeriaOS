"""The tool-index warm lifecycle: unconditional registration + thread-safe wake."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager


class _FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1

    def invalidate_thread_config_cache(self, thread_id: str):
        pass


def test_warm_lifecycle_registered_unconditionally(tmp_path, api_client_builder, monkeypatch):
    """The warm task registers in the slim shape too (disable_ticker is False),
    unlike the dream/spawn housekeeping tasks which are Docker-only. This proves
    the registration is unconditional, not gated like the others."""
    from nymeria.triggers import api as api_module

    settings = api_client_builder.settings(tmp_path)  # redis off -> slim shape
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    recorded = {"warm": 0, "dream": 0, "spawn": 0}
    monkeypatch.setattr(
        api_module,
        "_register_tool_index_warm_lifecycle",
        lambda app: recorded.__setitem__("warm", recorded["warm"] + 1),
    )
    monkeypatch.setattr(
        api_module,
        "_register_dream_scheduler_lifecycle",
        lambda app, **kw: recorded.__setitem__("dream", recorded["dream"] + 1),
    )
    monkeypatch.setattr(
        api_module,
        "_register_spawn_thread_housekeeping_lifecycle",
        lambda app, **kw: recorded.__setitem__("spawn", recorded["spawn"] + 1),
    )

    api_module.create_api_app(_FakeAgent(tmp_path))

    assert recorded["warm"] == 1
    assert recorded["dream"] == 0
    assert recorded["spawn"] == 0


def test_warm_loop_wake_signal_is_threadsafe():
    """``mark_tool_search_dirty`` is called from request/worker threads, but the
    asyncio wake event may only be set on the loop thread. Verify the
    ``call_soon_threadsafe`` hand-off in ``_signal_warm`` works end to end."""
    from fastapi import FastAPI

    from nymeria.core.tool_search_index import get_tool_search_index
    from nymeria.triggers.api import _register_tool_index_warm_lifecycle

    app = FastAPI()
    n_start = len(app.router.on_startup)
    n_stop = len(app.router.on_shutdown)
    _register_tool_index_warm_lifecycle(app)
    assert len(app.router.on_startup) == n_start + 1
    assert len(app.router.on_shutdown) == n_stop + 1

    async def run() -> None:
        await app.router.on_startup[-1]()  # start the warm task
        await asyncio.sleep(0.05)  # let the task register the loop + wake event

        index = get_tool_search_index()  # the offline keyless singleton
        wake = app.state.tool_index_warm_wake
        assert wake is not None and not wake.is_set()

        done = threading.Event()

        def worker() -> None:
            index.mark_dirty()  # fires _signal_warm from a non-loop thread
            done.set()

        threading.Thread(target=worker).start()
        assert done.wait(timeout=2)
        await asyncio.sleep(0.05)  # let the loop process call_soon_threadsafe
        assert wake.is_set()

        await app.router.on_shutdown[-1]()  # clean stop

    asyncio.run(run())
