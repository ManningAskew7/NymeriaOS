"""Integration tests for the request_credential tool ↔ credential-prompts API
round-trip. All scenarios run in a single process so the in-memory
AuthPromptCoordinator's futures are shared between the tool call and the
HTTP endpoint — which matches production (single API container).

Post-Phase-11 contract: ``request_credential`` is fire-and-forget. The tool
returns ``status="dispatched"`` immediately, and the future's done-callback
formats a resolution summary that goes through
``credential_prompt_injector.schedule_resolution_turn``. Tests stub the
injector so they assert on the captured summary rather than waiting for a
real agent turn."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig

from nymeria.core.accounts import AccountsRepo
from nymeria.core.auth_prompt_coordinator import get_auth_prompt_coordinator
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.tools.credential_prompt import request_credential
from nymeria.triggers import api as api_module


@dataclass
class _CapturedResolution:
    """Container shared between tests and the stubbed injector."""

    messages: list[str] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)

    @property
    def last(self) -> str:
        assert self.messages, "no resolution messages captured"
        return self.messages[-1]


def _patch_injector(monkeypatch) -> _CapturedResolution:
    """Stub ``schedule_resolution_turn`` so the future's done-callback
    doesn't need a live agent. Returns a capture object the test asserts on."""
    cap = _CapturedResolution()

    def fake_schedule(*, thread_id: str, user_id: str, message: str) -> None:
        cap.messages.append(message)
        cap.thread_ids.append(thread_id)
        cap.user_ids.append(user_id)

    # Patch the symbol the tool actually calls, not the source module --
    # ``request_credential`` imports ``schedule_resolution_turn`` at module
    # load time, so the bound name in ``credential_prompt`` is the one that
    # matters.
    monkeypatch.setattr(
        "nymeria.tools.credential_prompt.schedule_resolution_turn",
        fake_schedule,
    )
    return cap


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

    # Vault encryption needs a key. Tests used to inherit one from .env.docker
    # via the legacy ``*_auth.py`` modules' import-time ``load_dotenv()``; that
    # implicit hook is gone after Phase 8, so set it explicitly here.
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

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


async def _call_tool_with(provider: str, **extra) -> dict:
    """Variant of _call_tool that passes through arbitrary args (instructions,
    bind_target, etc.)."""
    payload = {"provider": provider, "kind": "api_key", "timeout_seconds": 15}
    payload.update(extra)
    config: RunnableConfig = {
        "configurable": {"user_id": "default", "thread_id": "test-thread"}
    }
    raw = await request_credential.ainvoke(payload, config=config)
    return json.loads(raw)


# ---------- scenarios ----------


def test_dispatch_then_active_submit(env, monkeypatch):
    """Tool returns ``dispatched`` immediately; submitting via HTTP fires
    the done-callback which formats a ``status=active`` resolution summary."""
    client, agent, token = env
    captured = _patch_injector(monkeypatch)

    async def run():
        # Tool returns immediately with status="dispatched"
        dispatched = await _call_tool("happy")
        assert dispatched["status"] == "dispatched"
        assert dispatched["ok"] is True
        assert dispatched["credential_id"]
        assert dispatched["prompt_id"]

        # Submit the secret via HTTP -> coordinator.resolve -> done-callback fires
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
        # Give the event loop one tick to drain the done-callback
        await asyncio.sleep(0)
        return dispatched

    dispatched = asyncio.run(run())
    assert dispatched["credential_id"]
    # The resolution callback formatted a single-line summary marking active
    assert captured.messages, "resolution callback did not fire"
    assert "status=active" in captured.last
    assert "provider=happy" in captured.last
    # thread_id propagates through the tool's RunnableConfig; LangChain
    # may normalize it, so just verify it's present and non-empty.
    assert captured.thread_ids and captured.thread_ids[0]
    assert captured.user_ids == ["default"]


def test_hosted_prompt_token_flow(env, monkeypatch):
    """Browser submits the hosted form, future resolves to active, the
    summary lands with status=active and user_message='done in browser'."""
    client, _, _ = env
    captured = _patch_injector(monkeypatch)

    async def run():
        dispatched = await _call_tool("hosted")
        assert dispatched["status"] == "dispatched"

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
        await asyncio.sleep(0)
        return dispatched

    asyncio.run(run())
    assert captured.messages
    assert "status=active" in captured.last
    assert "user_message=" in captured.last
    assert "done in browser" in captured.last


def test_inline_retry_after_test_failure(env, monkeypatch):
    """Repeated test failures bump attempts; final active resolution carries
    the failure count through to the summary."""
    client, _, token = env
    captured = _patch_injector(monkeypatch)

    async def run():
        dispatched = await _call_tool("retry")
        assert dispatched["status"] == "dispatched"

        pid = await _pid_for("retry")
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

        r2 = client.post(
            f"/credential-prompts/{pid}/submit",
            headers=_auth(token),
            json={"secret_fields": {"value": "sk-good"}},
        )
        assert r2.status_code == 200
        b2 = r2.json()
        assert b2["ok"] is True
        await asyncio.sleep(0)

    asyncio.run(run())
    assert captured.messages
    assert "status=active" in captured.last
    assert "attempts=1" in captured.last


def test_exit_returns_user_exited_with_last_error(env, monkeypatch):
    """Modal dismiss without saving resolves with user_exited and preserves
    the last test error + user note in the resolution summary."""
    client, _, token = env
    captured = _patch_injector(monkeypatch)

    async def run():
        dispatched = await _call_tool("exit")
        assert dispatched["status"] == "dispatched"

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
        await asyncio.sleep(0)

    asyncio.run(run())
    assert captured.messages
    assert "status=user_exited" in captured.last
    assert "Invalid scope" in captured.last
    assert "I'll find it later" in captured.last


def test_cancel_returns_cancelled(env, monkeypatch):
    """Cancel button resolves the prompt with status=cancelled."""
    client, _, token = env
    captured = _patch_injector(monkeypatch)

    async def run():
        dispatched = await _call_tool("cancel")
        assert dispatched["status"] == "dispatched"

        pid = await _pid_for("cancel")
        resp = client.post(
            f"/credential-prompts/{pid}/cancel",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        await asyncio.sleep(0)

    asyncio.run(run())
    assert captured.messages
    assert "status=cancelled" in captured.last


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


def test_tool_dispatches_immediately(env, monkeypatch):
    """Sanity check: the tool returns within milliseconds even with a long
    timeout, because there is no longer any await on the future."""
    _patch_injector(monkeypatch)

    async def run():
        # 270s would have wedged the old blocking contract for 4+ minutes.
        # The new contract should return synchronously.
        dispatched = await _call_tool("dispatch_test", timeout_seconds=270)
        assert dispatched["status"] == "dispatched"
        assert dispatched["timeout_seconds"] == 270

    asyncio.run(run())


def test_cross_user_submit_is_404(env, monkeypatch):
    """A user authenticated as someone else can't resolve another user's
    prompt. After the legitimate owner cancels, the resolution summary
    still surfaces with status=cancelled."""
    client, agent, token = env
    captured = _patch_injector(monkeypatch)
    agent.accounts_repo.create_user("intruder", "x@example.com", "X", role="user")
    intruder_token = agent.accounts_repo.issue_token("intruder")

    async def run():
        dispatched = await _call_tool("xuser")
        assert dispatched["status"] == "dispatched"

        pid = await _pid_for("xuser")
        # Intruder gets 404 (don't leak existence).
        resp = client.post(
            f"/credential-prompts/{pid}/submit",
            headers=_auth(intruder_token),
            json={"secret_fields": {"value": "evil"}},
        )
        assert resp.status_code == 404
        # Owner cancels normally so the future resolves and the callback fires.
        client.post(f"/credential-prompts/{pid}/cancel", headers=_auth(token))
        await asyncio.sleep(0)

    asyncio.run(run())
    assert captured.messages
    assert "status=cancelled" in captured.last


# ---------- Phase 11 additions: instructions + bind_target ----------


def test_instructions_arg_lands_in_auth_prompt_event(env, monkeypatch):
    """The new ``instructions`` arg flows into the auth_prompt SSE payload
    so the desktop modal can render it as a callout above the form."""
    _patch_injector(monkeypatch)

    captured_events: list[dict] = []
    import nymeria.tools.credential_prompt as cp_mod

    real_publish = cp_mod.publish_autonomous_event

    def _capture(**kw):
        if kw.get("event_type") == "auth_prompt":
            captured_events.append(kw.get("data") or {})
        return real_publish(**kw)

    monkeypatch.setattr(cp_mod, "publish_autonomous_event", _capture)

    instructions_text = (
        "1. Open resend.com/api-keys\n"
        "2. Click **Create API key**\n"
        "3. Name it `nymeria-test` and copy the value below"
    )

    async def run():
        dispatched = await _call_tool_with(
            "resend",
            description="Resend transactional email API",
            instructions=instructions_text,
        )
        assert dispatched["status"] == "dispatched"

    asyncio.run(run())
    assert captured_events
    event = captured_events[0]
    assert event["description"] == "Resend transactional email API"
    assert event["instructions"] == instructions_text


def test_bind_target_invalid_format_returns_early(env, monkeypatch):
    """Malformed bind_target rejects before any prompt is created so the
    coordinator never sees a registration for the bad call."""
    _patch_injector(monkeypatch)

    async def run():
        result = await _call_tool_with("badbind", bind_target="not::a::valid:target")
        assert result["status"] == "invalid_bind_target"
        assert result["ok"] is False

    asyncio.run(run())
    # No prompt should have been registered.
    coord = get_auth_prompt_coordinator()
    with coord._lock:
        for prompt in coord._prompts.values():
            assert prompt.provider != "badbind"


def test_bind_target_mcp_server_binds_and_restarts(env, monkeypatch):
    """Successful submit with bind_target=mcp_server:<id> triggers
    add_allowed_target + bind_credential + shutdown_server, and the
    resolution summary mentions the restart outcome."""
    client, _, token = env
    captured = _patch_injector(monkeypatch)

    bind_calls: list[tuple[str, str, str]] = []
    shutdown_calls: list[str] = []

    import nymeria.core.credential_vault as vault_mod
    real_repo = vault_mod.get_credential_vault_repo()
    real_add = real_repo.add_allowed_target
    real_bind = real_repo.bind_credential

    def _wrap_add(credential_id, *, target, actor_user_id=None):
        bind_calls.append(("add_allowed_target", credential_id, target))
        return real_add(credential_id, target=target, actor_user_id=actor_user_id)

    def _wrap_bind(credential_id, *, target_type, target_id, binding_name=None, actor_user_id=None):
        bind_calls.append(("bind_credential", credential_id, f"{target_type}:{target_id}"))
        return real_bind(
            credential_id,
            target_type=target_type,
            target_id=target_id,
            binding_name=binding_name,
            actor_user_id=actor_user_id,
        )

    monkeypatch.setattr(real_repo, "add_allowed_target", _wrap_add)
    monkeypatch.setattr(real_repo, "bind_credential", _wrap_bind)

    class _FakeManager:
        def shutdown_server(self, server_id, *, skip_if_active=True):
            shutdown_calls.append(server_id)
            return "not_running"

    import nymeria.core.mcp_manager as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_manager", lambda: _FakeManager())

    async def run():
        dispatched = await _call_tool_with(
            "resend",
            bind_target="mcp_server:resend-mcp",
        )
        assert dispatched["status"] == "dispatched"

        pid = await _pid_for("resend")
        resp = client.post(
            f"/credential-prompts/{pid}/submit",
            headers=_auth(token),
            json={"secret_fields": {"value": "re_test_xxx"}},
        )
        assert resp.status_code == 200
        await asyncio.sleep(0)

    asyncio.run(run())

    assert any(call[0] == "add_allowed_target" for call in bind_calls)
    assert any(call[0] == "bind_credential" for call in bind_calls)
    add_call = next(c for c in bind_calls if c[0] == "add_allowed_target")
    assert add_call[2] == "mcp_server:resend-mcp"
    bind_call = next(c for c in bind_calls if c[0] == "bind_credential")
    assert bind_call[2] == "mcp_server:resend-mcp"
    assert shutdown_calls == ["resend-mcp"]

    assert captured.messages
    assert "status=active" in captured.last
    assert "bind=" in captured.last
    assert "resend-mcp" in captured.last
