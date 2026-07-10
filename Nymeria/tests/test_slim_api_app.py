"""Tests for slim-mode wiring inside ``create_api_app``."""

from __future__ import annotations

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
        default_executor_max_workers=32,
        cors_origins_list=["http://localhost:1420"],
    )


def _patch_slim_mcp_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the real FastMCP session-manager lifecycle hook in tests.

    The session manager can only be ``run()`` once per FastMCP instance,
    and our tests construct multiple apps backed by the same module-level
    instance.  We unit-test the mount behavior here and rely on the smoke
    test (``python run.py slim`` + curl /mcp/) for the real
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


def test_create_api_app_registers_no_watchdog_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standalone watchdog lifecycle is gone (folded into the ticker).

    The stale-TODO sweep rides the agent's in-process Ticker
    (``core/watchdog_sweep.py``), so slim app construction must not
    register any watchdog startup/shutdown handlers or app.state attrs.
    """
    settings = _slim_settings(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_module,
        "_register_frontend_routes",
        lambda app, frontend_dir: None,
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
        assert not hasattr(app.state, "slim_watchdog_worker")
        assert not hasattr(app.state, "slim_watchdog_task")
        assert not hasattr(app.state, "slim_watchdog_client")
