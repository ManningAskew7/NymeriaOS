"""Integration tests for POST /ui-prompts/{id}/result.

Covers: happy-path resolve, delivered=False for unknown prompt_id, cross-user
404, admin resolve, the ui_prompt_result observability event (no values on
the wire), and the values size cap (mirrors test_browser_commands_router.py).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.core.event_bus import EventBus, get_event_bus, set_event_bus
from nymeria.core.ui_prompt_coordinator import (
    get_ui_prompt_coordinator,
    new_prompt_id,
)
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
    import nymeria.core.ui_prompt_coordinator as coord_mod
    coord_mod._coordinator = None
    set_event_bus(EventBus())

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = _Settings(data_dir=tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)

    import nymeria.config as config_mod
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    import nymeria.core.credential_vault as vault_mod
    vault_mod._vault_repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")
    api_module._reset_auth_failure_rate_limiter_for_tests()

    agent = _Agent(tmp_path)
    # cast: the minimal test double stands in for NymeriaAgent.
    client = TestClient(api_module.create_api_app(cast(Any, agent)))
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
        coord = get_ui_prompt_coordinator()
        prompt_id = new_prompt_id()
        future = coord.register(
            prompt_id=prompt_id,
            user_id="alice",
            thread_id="thread-alice",
            title="Pick options",
        )

        # Fire the HTTP request from a worker thread so the main loop can
        # await the future.
        result_box: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/ui-prompts/{prompt_id}/result",
                headers=_auth(alice_token),
                json={
                    "status": "submitted",
                    "values": {"color": "teal", "notify": ["email", "push"]},
                },
            )
            result_box["resp"] = resp

        threading.Thread(target=post_result, daemon=True).start()
        resolved = await asyncio.wait_for(future, timeout=3)
        for _ in range(50):
            if "resp" in result_box:
                break
            await asyncio.sleep(0.02)
        assert result_box["resp"].status_code == 200
        assert result_box["resp"].json()["delivered"] is True
        return prompt_id, resolved

    prompt_id, resolved = asyncio.run(run())
    assert resolved["ok"] is True
    assert resolved["status"] == "submitted"
    assert resolved["values"] == {"color": "teal", "notify": ["email", "push"]}
    # Coordinator entry removed.
    assert get_ui_prompt_coordinator().get(prompt_id) is None


def test_cancelled_result_resolves_without_values(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env

    async def run() -> dict:
        coord = get_ui_prompt_coordinator()
        prompt_id = new_prompt_id()
        future = coord.register(
            prompt_id=prompt_id,
            user_id="alice",
            thread_id="thread-alice",
        )

        def post_result() -> None:
            client.post(
                f"/ui-prompts/{prompt_id}/result",
                headers=_auth(alice_token),
                json={"status": "cancelled"},
            )

        threading.Thread(target=post_result, daemon=True).start()
        return await asyncio.wait_for(future, timeout=3)

    resolved = asyncio.run(run())
    assert resolved["ok"] is False
    assert resolved["status"] == "cancelled"
    assert "values" not in resolved


def test_result_unknown_prompt_returns_delivered_false(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env
    resp = client.post(
        "/ui-prompts/uip_does_not_exist/result",
        headers=_auth(alice_token),
        json={"status": "cancelled"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] is True
    assert body["delivered"] is False


def test_result_cross_user_returns_404(env) -> None:
    client, _agent, _alice_token, bob_token, _admin_token = env

    async def run() -> tuple[str, int]:
        coord = get_ui_prompt_coordinator()
        prompt_id = new_prompt_id()
        coord.register(
            prompt_id=prompt_id,
            user_id="alice",
            thread_id="thread-alice",
        )
        result_status: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/ui-prompts/{prompt_id}/result",
                headers=_auth(bob_token),
                json={"status": "submitted", "values": {}},
            )
            result_status["code"] = resp.status_code

        thread = threading.Thread(target=post_result, daemon=True)
        thread.start()
        thread.join(timeout=3)
        await asyncio.sleep(0)
        return prompt_id, result_status["code"]

    prompt_id, status_code = asyncio.run(run())
    assert status_code == 404
    coord = get_ui_prompt_coordinator()
    assert coord.get(prompt_id) is not None
    # Clean up so the sweep loop doesn't hold the test runner alive.
    coord.discard(prompt_id)


def test_result_admin_can_resolve_other_user(env) -> None:
    client, _agent, _alice_token, _bob_token, admin_token = env

    async def run() -> dict:
        coord = get_ui_prompt_coordinator()
        prompt_id = new_prompt_id()
        future = coord.register(
            prompt_id=prompt_id,
            user_id="alice",
            thread_id="thread-alice",
        )

        def post_result() -> None:
            client.post(
                f"/ui-prompts/{prompt_id}/result",
                headers=_auth(admin_token),
                json={"status": "submitted", "values": {"by": "admin"}},
            )

        threading.Thread(target=post_result, daemon=True).start()
        return await asyncio.wait_for(future, timeout=3)

    resolved = asyncio.run(run())
    assert resolved["ok"] is True
    assert resolved["values"]["by"] == "admin"


def test_result_publishes_observability_event_without_values(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env
    queue = get_event_bus().subscribe("test-observer")

    async def run() -> dict:
        coord = get_ui_prompt_coordinator()
        prompt_id = new_prompt_id()
        future = coord.register(
            prompt_id=prompt_id,
            user_id="alice",
            thread_id="thread-alice",
        )

        def post_result() -> None:
            client.post(
                f"/ui-prompts/{prompt_id}/result",
                headers=_auth(alice_token),
                json={"status": "submitted", "values": {"secret": "hunter2"}},
            )

        threading.Thread(target=post_result, daemon=True).start()
        return await asyncio.wait_for(future, timeout=3)

    asyncio.run(run())
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    results = [e for e in seen if e.event_type == "ui_prompt_result"]
    assert len(results) == 1
    event = results[0]
    assert event.user_id == "alice"
    assert event.thread_id == "thread-alice"
    assert event.data["status"] == "submitted"
    # Values never ride the bus: they may hold sensitive user input.
    assert "values" not in event.data


def test_oversized_values_rejected(env) -> None:
    client, _agent, alice_token, _bob_token, _admin_token = env

    async def run() -> int:
        coord = get_ui_prompt_coordinator()
        prompt_id = new_prompt_id()
        coord.register(
            prompt_id=prompt_id,
            user_id="alice",
            thread_id="thread-alice",
        )
        result_status: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/ui-prompts/{prompt_id}/result",
                headers=_auth(alice_token),
                json={"status": "submitted", "values": {"blob": "x" * (64 * 1024 + 1)}},
            )
            result_status["code"] = resp.status_code

        thread = threading.Thread(target=post_result, daemon=True)
        thread.start()
        thread.join(timeout=3)
        await asyncio.sleep(0)
        coord.discard(prompt_id)
        return result_status["code"]

    assert asyncio.run(run()) == 422
