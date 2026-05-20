"""Integration tests for the request_credential tool ↔ credential-prompts API
round-trip. All scenarios run in a single process so the in-memory
AuthPromptCoordinator's futures are shared between the tool call and the
HTTP endpoint — which matches production (single API container)."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig

from nymeria.core.accounts import AccountsRepo
from nymeria.core.auth_prompt_coordinator import get_auth_prompt_coordinator
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.tools.credential_prompt import request_credential
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
        """No-op for tests — production agent syncs callable threads into
        the tool registry, but that's not exercised here."""
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Spin up an isolated app + TestClient + admin user + auth token."""
    # Reset the coordinator singleton between tests so leftover prompts from
    # a previous case don't survive into this one.
    import nymeria.core.auth_prompt_coordinator as coord_mod
    coord_mod._coordinator = None

    settings = _Settings(data_dir=tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    # Patch nymeria.config.get_settings too — get_credential_vault_repo()
    # derives db_path from it lazily and will rebuild its singleton if the
    # path doesn't match what we seed below.
    import nymeria.config as config_mod
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    # Seed the vault singleton with our isolated DB.
    import nymeria.core.credential_vault as vault_mod
    vault_mod._vault_repo = vault_mod.CredentialVaultRepo(tmp_path / "accounts.db")
    # Reset rate limiter — required by conftest invariants.
    api_module._reset_auth_failure_rate_limiter_for_tests()

    agent = _Agent(tmp_path)
    client = TestClient(api_module.create_api_app(agent))
    agent.accounts_repo.create_user("default", "default@example.com", "Default", role="admin")
    token = agent.accounts_repo.issue_token("default")
    yield client, agent, token
    api_module._reset_auth_failure_rate_limiter_for_tests()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _call_tool(provider: str, *, timeout_seconds: int = 15) -> dict:
    config: RunnableConfig = {
        "configurable": {"user_id": "default", "thread_id": "test-thread"}
    }
    raw = await request_credential.ainvoke(
        {"provider": provider, "kind": "api_key", "timeout_seconds": timeout_seconds},
        config=config,
    )
    return json.loads(raw)


async def _pid_for(provider: str) -> str:
    coord = get_auth_prompt_coordinator()
    for _ in range(50):
        await asyncio.sleep(0.02)
        with coord._lock:
            for pid, prompt in coord._prompts.items():
                if prompt.provider == provider:
                    return pid
    raise AssertionError(f"no pending prompt for provider={provider}")


# ---------- scenarios ----------


def test_happy_path_returns_active(env):
    client, agent, token = env

    async def run():
        async def submitter():
            pid = await _pid_for("happy")
            resp = client.post(
                f"/credential-prompts/{pid}/submit",
                headers=_auth(token),
                json={"secret_fields": {"value": "sk-real"}},
            )
            assert resp.status_code == 200, f"submit failed: {resp.text}"
            body = resp.json()
            assert body["ok"] is True
            assert body["status"] == "active"

        tool, _ = await asyncio.gather(_call_tool("happy"), submitter())
        return tool

    result = asyncio.run(run())
    assert result["ok"] is True
    assert result["status"] == "active"
    assert result["credential_id"]


def test_hosted_prompt_token_flow(env):
    client, _, _ = env

    async def run():
        async def submitter():
            pid = await _pid_for("hosted")
            prompt = get_auth_prompt_coordinator().get(pid)
            assert prompt is not None
            connect_url = prompt.metadata["connect_url"]
            assert connect_url.startswith(
                f"https://nymeria.example.test/connect/credentials/{pid}#"
            )
            token = connect_url.rsplit("#", 1)[1]

            bad = client.get(
                f"/connect/credentials/{pid}/prompt",
                headers={"Authorization": "Bearer bad-token"},
            )
            assert bad.status_code == 404

            auth = {"Authorization": f"Bearer {token}"}
            meta = client.get(f"/connect/credentials/{pid}/prompt", headers=auth)
            assert meta.status_code == 200
            meta_body = meta.json()
            assert meta_body["prompt_id"] == pid
            assert meta_body["provider"] == "hosted"
            assert meta_body["expires_at"] == prompt.token_expires_at_iso

            test_resp = client.post(
                f"/connect/credentials/{pid}/test",
                headers=auth,
                json={"secret_fields": {}},
            )
            assert test_resp.status_code == 200
            test_body = test_resp.json()
            assert test_body["ok"] is False
            assert test_body["status"] == "test_failed"
            assert test_body["attempts"] == 1
            assert test_body["code"] == "missing_secret"

            submit_resp = client.post(
                f"/connect/credentials/{pid}/submit",
                headers=auth,
                json={
                    "secret_fields": {"value": "sk-good"},
                    "user_message": "done in browser",
                },
            )
            assert submit_resp.status_code == 200, submit_resp.text
            submit_body = submit_resp.json()
            assert submit_body["ok"] is True
            assert submit_body["status"] == "active"
            assert submit_body["tested"] is False
            assert submit_body["test_status"] == "not_verified"

        tool, _ = await asyncio.gather(_call_tool("hosted"), submitter())
        return tool

    result = asyncio.run(run())
    assert result["ok"] is True
    assert result["status"] == "active"
    assert result["user_message"] == "done in browser"
    assert result["tested"] is False
    assert result["test_status"] == "not_verified"


def test_inline_retry_after_test_failure(env):
    client, _, token = env

    async def run():
        async def submitter():
            pid = await _pid_for("retry")
            # First attempt: no secret fields -> generic test fails.
            r1 = client.post(
                f"/credential-prompts/{pid}/submit",
                headers=_auth(token),
                json={"secret_fields": {}},
            )
            assert r1.status_code == 200
            b1 = r1.json()
            assert b1["ok"] is False
            assert b1["status"] == "test_failed"
            assert b1["attempts"] == 1
            # Retry with a real value.
            r2 = client.post(
                f"/credential-prompts/{pid}/submit",
                headers=_auth(token),
                json={"secret_fields": {"value": "sk-good"}},
            )
            assert r2.status_code == 200
            b2 = r2.json()
            assert b2["ok"] is True

        tool, _ = await asyncio.gather(_call_tool("retry"), submitter())
        return tool

    result = asyncio.run(run())
    assert result["status"] == "active"
    assert result["attempts"] == 1  # the one failed attempt is reported


def test_exit_returns_user_exited_with_last_error(env):
    client, _, token = env

    async def run():
        async def exiter():
            pid = await _pid_for("exit")
            resp = client.post(
                f"/credential-prompts/{pid}/exit",
                headers=_auth(token),
                json={
                    "last_test_error": "Invalid scope",
                    "attempts": 2,
                    "user_message": "I'll find it later",
                },
            )
            assert resp.status_code == 200

        tool, _ = await asyncio.gather(_call_tool("exit"), exiter())
        return tool

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["status"] == "user_exited"
    assert result["last_test_error"] == "Invalid scope"
    assert result["user_message"] == "I'll find it later"


def test_cancel_returns_cancelled(env):
    client, _, token = env

    async def run():
        async def canceller():
            pid = await _pid_for("cancel")
            resp = client.post(
                f"/credential-prompts/{pid}/cancel",
                headers=_auth(token),
            )
            assert resp.status_code == 200

        tool, _ = await asyncio.gather(_call_tool("cancel"), canceller())
        return tool

    result = asyncio.run(run())
    assert result["status"] == "cancelled"


def test_chat_message_resolves_pending_auth_prompt(env):
    client, _, token = env
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    ready.wait()

    async def register_prompt():
        return get_auth_prompt_coordinator().register(
            prompt_id="prompt_chat",
            credential_id="cred_chat",
            user_id="default",
            thread_id="interlock-thread",
            provider="chat",
        )

    async def await_prompt(future: asyncio.Future):
        return await future

    try:
        future = asyncio.run_coroutine_threadsafe(register_prompt(), loop).result(2)
        resp = client.post(
            "/chat",
            headers=_auth(token),
            json={
                "thread_id": "interlock-thread",
                "message": "I need to ask a question first",
            },
        )
        assert resp.status_code == 200, resp.text
        assert '"auth_prompt_status": "user_message"' in resp.text
        result = asyncio.run_coroutine_threadsafe(await_prompt(future), loop).result(2)
        assert result["ok"] is False
        assert result["status"] == "user_message"
        assert result["user_message"] == "I need to ask a question first"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_tool_timeout_returns_pending(env):
    # Use the minimum allowed timeout (15s) so the test runs fast — nobody
    # submits, so the tool should report pending.
    result = asyncio.run(_call_tool("timeout", timeout_seconds=15))
    assert result["ok"] is False
    assert result["status"] == "pending"


def test_cross_user_submit_is_404(env):
    """A user authenticated as someone else can't resolve another user's prompt."""
    client, agent, token = env
    agent.accounts_repo.create_user("intruder", "x@example.com", "X", role="user")
    intruder_token = agent.accounts_repo.issue_token("intruder")

    async def run():
        async def cross_submitter():
            pid = await _pid_for("xuser")
            resp = client.post(
                f"/credential-prompts/{pid}/submit",
                headers=_auth(intruder_token),
                json={"secret_fields": {"value": "evil"}},
            )
            # Should be 404 (don't leak existence) per design.
            assert resp.status_code == 404
            # Resolve normally so the tool returns.
            client.post(f"/credential-prompts/{pid}/cancel", headers=_auth(token))

        tool, _ = await asyncio.gather(_call_tool("xuser"), cross_submitter())
        return tool

    result = asyncio.run(run())
    assert result["status"] == "cancelled"
