"""Integration tests for POST /browser-commands/{id}/result.

Covers: happy-path resolve, 403 cross-user, 404-style delivered=False for
unknown command_id, and the observability event emission.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.browser_command_coordinator import (
    get_browser_command_coordinator,
    new_command_id,
)
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.triggers import api as api_module


@dataclass
class _Settings:
    data_dir: Path
    nymeria_api_key: str | None = None
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    context_management: str = "none"
    sliding_window_cycles: int = 20
    todo_auto_archive_days: int = 7
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
    cors_origins_list: list[str] | None = None
    nymeria_public_url: str | None = "https://nymeria.example.test"

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = ["http://localhost"]


class _ThreadMetadataManager:
    def get_store(self, user_id: str):
        _ = user_id
        return type("_Store", (), {"threads": {}})()


class _Agent:
    def __init__(self, data_dir: Path) -> None:
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.credential_vault = CredentialVaultRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = _ThreadMetadataManager()
        self.thread_config_manager = None
        self._graph_cache_lock = threading.Lock()
        self._user_graphs: dict = {}
        self._async_user_graphs: dict = {}
        self._base_system_prompt = "base"
        self._default_graph = None
        self._default_async_graph = None

    def _build_graph_with_prompt(self, prompt: str):
        return object()

    def _build_async_graph_with_prompt(self, prompt: str):
        return object()

    def sync_agent_tools(self) -> None:
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    import nymeria.core.browser_command_coordinator as coord_mod
    coord_mod._coordinator = None

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = _Settings(data_dir=tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    import nymeria.config as config_mod
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    import nymeria.core.credential_vault as vault_mod
    vault_mod._vault_repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")
    api_module._reset_auth_failure_rate_limiter_for_tests()

    agent = _Agent(tmp_path)
    client = TestClient(api_module.create_api_app(agent))
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice", role="user")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Bob", role="user")
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    alice_token = agent.accounts_repo.issue_token("alice")
    bob_token = agent.accounts_repo.issue_token("bob")
    admin_token = agent.accounts_repo.issue_token("admin")
    yield client, agent, alice_token, bob_token, admin_token
    api_module._reset_auth_failure_rate_limiter_for_tests()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_result_resolves_pending_future(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env

    async def run() -> tuple[str, dict]:
        coord = get_browser_command_coordinator()
        command_id = new_command_id()
        future = coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="navigate",
        )

        # Fire the HTTP request from a worker thread so the main loop can
        # await the future.
        result_box: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/browser-commands/{command_id}/result",
                headers=_auth(alice_token),
                json={
                    "ok": True,
                    "status": "success",
                    "data": {"final_url": "https://example.com"},
                },
            )
            result_box["resp"] = resp

        threading.Thread(target=post_result, daemon=True).start()
        resolved = await asyncio.wait_for(future, timeout=3)
        # Give the worker thread a moment to record its response.
        for _ in range(50):
            if "resp" in result_box:
                break
            await asyncio.sleep(0.02)
        return command_id, resolved

    command_id, resolved = asyncio.run(run())
    assert resolved["ok"] is True
    assert resolved["status"] == "success"
    assert resolved["data"]["final_url"] == "https://example.com"
    # Coordinator entry removed.
    assert get_browser_command_coordinator().get(command_id) is None


def test_result_unknown_command_returns_delivered_false(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env
    resp = client.post(
        "/browser-commands/bcmd_does_not_exist/result",
        headers=_auth(alice_token),
        json={"ok": False, "status": "error", "error": "test"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] is True
    assert body["delivered"] is False


def test_result_cross_user_returns_404(env) -> None:
    client, _agent, _alice_token, bob_token, _admin_token = env

    async def run() -> tuple[str, int]:
        coord = get_browser_command_coordinator()
        command_id = new_command_id()
        coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="navigate",
        )
        result_status: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/browser-commands/{command_id}/result",
                headers=_auth(bob_token),
                json={"ok": True, "status": "success", "data": {}},
            )
            result_status["code"] = resp.status_code

        thread = threading.Thread(target=post_result, daemon=True)
        thread.start()
        thread.join(timeout=3)
        # Pending command should NOT have been resolved (and the future is
        # still pending on the coordinator).
        await asyncio.sleep(0)
        return command_id, result_status["code"]

    command_id, status_code = asyncio.run(run())
    assert status_code == 404
    coord = get_browser_command_coordinator()
    assert coord.get(command_id) is not None
    # Clean up so the sweep loop doesn't hold the test runner alive.
    coord.discard(command_id)


def test_result_admin_can_resolve_other_user(env) -> None:
    client, _agent, _alice_token, _bob_token, admin_token = env

    async def run() -> dict:
        coord = get_browser_command_coordinator()
        command_id = new_command_id()
        future = coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="navigate",
        )

        def post_result() -> None:
            client.post(
                f"/browser-commands/{command_id}/result",
                headers=_auth(admin_token),
                json={"ok": True, "status": "success", "data": {"by": "admin"}},
            )

        threading.Thread(target=post_result, daemon=True).start()
        return await asyncio.wait_for(future, timeout=3)

    resolved = asyncio.run(run())
    assert resolved["ok"] is True
    assert resolved["data"]["by"] == "admin"


# ---------- single-browser routing: only the target may resolve ----------


def test_result_from_a_non_target_client_is_ignored(env) -> None:
    """A stale second instance (the #282 shape) POSTing a result for a
    command that was routed to another browser must not resolve it."""
    client, _agent, alice_token, _bob_token, _admin_token = env

    async def register() -> str:
        command_id = new_command_id()
        get_browser_command_coordinator().register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="navigate",
            target_client_id="nymeria-browser-target01",
        )
        return command_id

    command_id = asyncio.run(register())
    try:
        resp = client.post(
            f"/browser-commands/{command_id}/result",
            headers={
                **_auth(alice_token),
                "X-Nymeria-Client-Id": "nymeria-browser-stale999",
            },
            json={"ok": True, "status": "success", "data": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["delivered"] is False
        # The command is still pending for the real target.
        assert get_browser_command_coordinator().get(command_id) is not None
    finally:
        get_browser_command_coordinator().discard(command_id)


def test_result_from_the_target_client_resolves(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env

    async def run() -> dict:
        coord = get_browser_command_coordinator()
        command_id = new_command_id()
        future = coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="navigate",
            target_client_id="nymeria-browser-target01",
        )
        result_box: dict = {}

        def post_result() -> None:
            result_box["resp"] = client.post(
                f"/browser-commands/{command_id}/result",
                headers={
                    **_auth(alice_token),
                    "X-Nymeria-Client-Id": "nymeria-browser-target01",
                },
                json={"ok": True, "status": "success", "data": {}},
            )

        threading.Thread(target=post_result, daemon=True).start()
        return await asyncio.wait_for(future, timeout=3)

    resolved = asyncio.run(run())
    assert resolved["ok"] is True


def test_result_without_a_client_header_still_resolves(env) -> None:
    """Pre-header extension builds only ever receive commands the delivery
    filter routed to them, so an absent header is accepted, not refused."""
    client, _agent, alice_token, _bob_token, _admin_token = env

    async def run() -> dict:
        coord = get_browser_command_coordinator()
        command_id = new_command_id()
        future = coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="navigate",
            target_client_id="nymeria-browser-target01",
        )
        result_box: dict = {}

        def post_result() -> None:
            result_box["resp"] = client.post(
                f"/browser-commands/{command_id}/result",
                headers=_auth(alice_token),
                json={"ok": True, "status": "success", "data": {}},
            )

        threading.Thread(target=post_result, daemon=True).start()
        return await asyncio.wait_for(future, timeout=3)

    resolved = asyncio.run(run())
    assert resolved["ok"] is True
