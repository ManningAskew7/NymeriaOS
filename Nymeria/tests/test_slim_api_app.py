"""Tests for slim-mode wiring inside ``create_api_app``."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.routing import Mount

from nymeria.core.accounts import AccountsRepo
from nymeria.triggers import api as api_module


class _FakeAgent:
    """Minimal stand-in for NymeriaAgent. Only fields the API touches."""

    def __init__(self, data_dir: Path) -> None:
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.settings = SimpleNamespace(llm_model="fake-model")
        self.thread_metadata_manager = SimpleNamespace()
        self.thread_config_manager = SimpleNamespace()
        self._synced = 0

    def sync_agent_tools(self) -> None:
        self._synced += 1


class _StubWatchdogWorker:
    instances: list["_StubWatchdogWorker"] = []

    def __init__(self, *, client: Any, settings: Any) -> None:
        self.client = client
        self.settings = settings
        self.started = asyncio.Event()
        self.stop_called = False
        self._stop = asyncio.Event()
        _StubWatchdogWorker.instances.append(self)

    async def run(self) -> None:
        self.started.set()
        await self._stop.wait()

    def stop(self) -> None:
        self.stop_called = True
        self._stop.set()


class _StubAPIClient:
    instances: list["_StubAPIClient"] = []

    def __init__(self, base_url: str | None, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.closed = False
        _StubAPIClient.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_stub_state():
    _StubWatchdogWorker.instances = []
    _StubAPIClient.instances = []
    yield
    _StubWatchdogWorker.instances = []
    _StubAPIClient.instances = []


def _slim_settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        data_dir=tmp_path,
        nymeria_service_token=None,
        redis_enabled=False,
        redis_url=None,
        fcm_enabled=False,
        fcm_credentials_json=None,
        watchdog_enabled=True,
        watchdog_interval_minutes=1,
        todo_staleness_minutes=10,
        api_host="127.0.0.1",
        api_port=8000,
        api_docs_enabled=False,
        nymeria_debug=False,
        cors_origins_list=["http://localhost:1420"],
    )


def _patch_slim_mcp_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the real FastMCP session-manager lifecycle hook in tests.

    The session manager can only be ``run()`` once per FastMCP instance,
    and our tests construct multiple apps backed by the same module-level
    instance.  We unit-test the mount + watchdog behavior here and rely on
    the smoke test (``python run.py slim`` + curl /mcp/) for the real
    session-manager path.
    """
    monkeypatch.setattr(
        api_module,
        "_register_slim_mcp_lifecycle",
        lambda app, fastmcp_app: None,
    )


def test_create_api_app_slim_mounts_mcp_before_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _slim_settings(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    monkeypatch.setattr(
        api_module,
        "_register_frontend_routes",
        lambda app, frontend_dir: None,
    )
    monkeypatch.setattr(
        "nymeria.triggers.watchdog_worker.WatchdogWorker",
        _StubWatchdogWorker,
    )
    monkeypatch.setattr(
        "nymeria.triggers.api_client.NymeriaAPIClient",
        _StubAPIClient,
    )
    _patch_slim_mcp_lifecycle(monkeypatch)

    mounted = {"called": False}

    def _fake_factory(api_url=None, service_token=None):  # noqa: ANN001
        mounted["called"] = True
        mounted["api_url"] = api_url
        mounted["service_token"] = service_token

        async def _asgi(scope, receive, send):  # pragma: no cover - never called
            return

        return _asgi

    monkeypatch.setattr("nymeria.mcp_server.create_mcp_asgi_app", _fake_factory)

    agent = _FakeAgent(tmp_path)

    app = api_module.create_api_app(
        agent,
        slim_mode=True,
        slim_base_url="http://127.0.0.1:8000",
    )

    assert mounted["called"] is True
    assert mounted["api_url"] == "http://127.0.0.1:8000"
    assert isinstance(mounted["service_token"], str) and mounted["service_token"].startswith("nym_")

    mcp_routes = [r for r in app.routes if isinstance(r, Mount) and r.path == "/mcp"]
    assert len(mcp_routes) == 1, "slim mode must mount the MCP ASGI app at /mcp"


def test_create_api_app_slim_starts_and_stops_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _slim_settings(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    monkeypatch.setattr(
        api_module,
        "_register_frontend_routes",
        lambda app, frontend_dir: None,
    )
    monkeypatch.setattr(
        "nymeria.triggers.watchdog_worker.WatchdogWorker",
        _StubWatchdogWorker,
    )
    monkeypatch.setattr(
        "nymeria.triggers.api_client.NymeriaAPIClient",
        _StubAPIClient,
    )
    _patch_slim_mcp_lifecycle(monkeypatch)

    async def _noop_asgi(scope, receive, send):  # pragma: no cover - never called
        return

    monkeypatch.setattr(
        "nymeria.mcp_server.create_mcp_asgi_app",
        lambda **kwargs: _noop_asgi,
    )

    agent = _FakeAgent(tmp_path)
    app = api_module.create_api_app(
        agent,
        slim_mode=True,
        slim_base_url="http://127.0.0.1:8000",
    )

    from fastapi.testclient import TestClient

    with TestClient(app):
        # FastAPI's TestClient context manager runs startup; verify the
        # watchdog stub was instantiated and started.
        assert len(_StubWatchdogWorker.instances) == 1
        worker = _StubWatchdogWorker.instances[0]
        # The startup handler creates an asyncio task that calls worker.run(),
        # which sets the `started` event. The TestClient's context allows
        # this to run before we observe state.
        for _ in range(50):
            if worker.started.is_set():
                break
            time.sleep(0.02)
        assert worker.started.is_set() is True
        assert worker.stop_called is False
        assert _StubAPIClient.instances[0].closed is False

    # Exiting the context triggers shutdown; both worker and client should
    # have been stopped and closed by the registered shutdown handler.
    assert worker.stop_called is True
    assert _StubAPIClient.instances[0].closed is True


def test_create_api_app_slim_disables_watchdog_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _slim_settings(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_module,
        "_register_frontend_routes",
        lambda app, frontend_dir: None,
    )
    monkeypatch.setattr(
        "nymeria.triggers.watchdog_worker.WatchdogWorker",
        _StubWatchdogWorker,
    )
    monkeypatch.setattr(
        "nymeria.triggers.api_client.NymeriaAPIClient",
        _StubAPIClient,
    )
    _patch_slim_mcp_lifecycle(monkeypatch)

    async def _noop_asgi(scope, receive, send):  # pragma: no cover
        return

    monkeypatch.setattr(
        "nymeria.mcp_server.create_mcp_asgi_app",
        lambda **kwargs: _noop_asgi,
    )

    agent = _FakeAgent(tmp_path)
    app = api_module.create_api_app(
        agent,
        slim_mode=True,
        slim_base_url="http://127.0.0.1:8000",
        enable_slim_watchdog=False,
    )

    from fastapi.testclient import TestClient

    with TestClient(app):
        # No watchdog should have been created when the flag is off.
        assert _StubWatchdogWorker.instances == []
