"""GET /status/turns: live turn activity, the deploy-sync idle gate.

Plan behaviors B1-B3b in ``tmp/deploy-sync-plan.md``: auth required,
active_turns mirrors held thread locks, interactive_active mirrors the
admission gate, and busy_threads detail (cross-user metadata) is
admin-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.interactive_admission import (
    get_interactive_turn_gate,
    reset_interactive_turn_gate_for_tests,
)
from nymeria.core.thread_lock_manager import ThreadLockManager


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self._thread_locks = ThreadLockManager()

    def sync_agent_tools(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_interactive_gate():
    reset_interactive_turn_gate_for_tests()
    yield
    reset_interactive_turn_gate_for_tests()


def _client(tmp_path: Path, api_client_builder, *, role: str = "admin"):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="probe", role=role
    )
    return client, agent, token


def test_status_turns_requires_auth(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    assert client.get("/status/turns").status_code == 401


def test_idle_instance_reports_zero_everything(tmp_path: Path, api_client_builder):
    client, _, token = _client(tmp_path, api_client_builder)
    payload = client.get(
        "/status/turns", headers=api_client_builder.auth(token)
    ).json()
    assert payload == {
        "active_turns": 0,
        "interactive_active": 0,
        "background_jobs": 0,
        "busy_threads": [],
    }


def test_held_lock_counts_and_admin_sees_detail(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    lock = agent._thread_locks.get_lock("t-busy")
    lock.acquire()
    agent._thread_locks.set_lock_info("t-busy", holder="autonomous")
    try:
        payload = client.get(
            "/status/turns", headers=api_client_builder.auth(token)
        ).json()
        assert payload["active_turns"] == 1
        assert len(payload["busy_threads"]) == 1
        busy = payload["busy_threads"][0]
        assert busy["thread_id"] == "t-busy"
        assert busy["holder"] == "autonomous"
        assert busy["held_seconds"] >= 0
    finally:
        lock.release()
        agent._thread_locks.clear_lock_info("t-busy")

    after = client.get(
        "/status/turns", headers=api_client_builder.auth(token)
    ).json()
    assert after["active_turns"] == 0
    assert after["busy_threads"] == []


def test_non_admin_gets_counts_without_thread_detail(
    tmp_path: Path, api_client_builder
):
    client, agent, token = _client(tmp_path, api_client_builder, role="user")
    lock = agent._thread_locks.get_lock("t-other-user")
    lock.acquire()
    agent._thread_locks.set_lock_info("t-other-user", holder="user")
    try:
        payload = client.get(
            "/status/turns", headers=api_client_builder.auth(token)
        ).json()
        # The count (the idle predicate) is accurate; the cross-user
        # thread ids and holder labels are withheld.
        assert payload["active_turns"] == 1
        assert payload["busy_threads"] == []
    finally:
        lock.release()
        agent._thread_locks.clear_lock_info("t-other-user")


def test_running_background_bash_jobs_are_counted(
    tmp_path: Path, api_client_builder, monkeypatch
):
    from types import SimpleNamespace

    from nymeria.tools import bash_background

    records = [
        SimpleNamespace(status="running"),
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="running"),
    ]
    monkeypatch.setattr(
        bash_background,
        "get_registry",
        lambda: SimpleNamespace(records=lambda: list(records)),
    )
    client, _, token = _client(tmp_path, api_client_builder)
    payload = client.get(
        "/status/turns", headers=api_client_builder.auth(token)
    ).json()
    assert payload["background_jobs"] == 2
    assert payload["active_turns"] == 0


def test_agent_without_lock_manager_answers_503(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    del agent._thread_locks
    response = client.get("/status/turns", headers=api_client_builder.auth(token))
    # 503, not 500: the deploy-sync script treats it as unverifiable and
    # defers, and clients can distinguish "not ready" from a crash.
    assert response.status_code == 503


def test_interactive_slice_mirrors_the_admission_gate(
    tmp_path: Path, api_client_builder
):
    client, _, token = _client(tmp_path, api_client_builder)
    slot = get_interactive_turn_gate().try_acquire(limit=5)
    assert slot is not None
    try:
        payload = client.get(
            "/status/turns", headers=api_client_builder.auth(token)
        ).json()
        assert payload["interactive_active"] == 1
    finally:
        slot.release()

    payload = client.get(
        "/status/turns", headers=api_client_builder.auth(token)
    ).json()
    assert payload["interactive_active"] == 0
