"""GET /status/turns: live turn activity, the deploy-sync idle gate.

Plan behaviors B1-B3b in ``tmp/deploy-sync-plan.md``: auth required,
active_turns mirrors held thread locks, interactive_active mirrors the
admission gate, and busy_threads detail (cross-user metadata) is
admin-only. #423 (it34 plan V1-V4, coordinator decision K2): admins also
get the booted ``code_version`` and ``code_fingerprint``, read from the
boot record; everyone else gets the payload without those keys.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nymeria import _provenance as prov
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


SHA_A = "a" * 40
FP = prov.Fingerprint(files=677, mtime_ns_sum=1_209_666_476_302_492_136_879, size_sum=19_263_492)
COUNT_KEYS = {
    "active_turns", "interactive_active", "background_jobs",
    "claude_code_jobs", "busy_threads",
}


def _boot(**overrides) -> prov.BootRecord:
    fields = dict(version="0.0.0", started_at=0.0, commit=None, installed=False,
                  dist_version=None, fingerprint=None)
    fields.update(overrides)
    return prov.BootRecord(**fields)


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


def test_idle_instance_reports_zero_everything(
    tmp_path: Path, api_client_builder, monkeypatch
):
    # The exact payload an admin (deploy-sync's service token) reads: the
    # counts plus, since #423, the booted code identity.
    monkeypatch.setattr(prov, "_BOOT", _boot(commit=SHA_A, fingerprint=FP))
    client, _, token = _client(tmp_path, api_client_builder)
    payload = client.get(
        "/status/turns", headers=api_client_builder.auth(token)
    ).json()
    assert payload == {
        "active_turns": 0,
        "interactive_active": 0,
        "background_jobs": 0,
        "claude_code_jobs": 0,
        "busy_threads": [],
        "code_version": SHA_A,
        "code_fingerprint": prov.fingerprint_digest(FP),
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


def test_claude_code_runs_are_counted_across_both_registries(
    tmp_path: Path, api_client_builder
):
    """#339: a detached Claude Code / /code run holds no thread lock and is
    not a bash job, so it needs its own counter: running jobs in the tool's
    registry plus finished-but-delivering jobs in the delivery registry,
    counted once each."""
    import time

    from nymeria.core import claude_code_delivery as delivery
    from nymeria.tools import claude_code_background as bg

    def _job(job_id, thread_id):
        return bg.ClaudeCodeJob(
            id=job_id, thread_id=thread_id, user_id="u1", prompt="p", cwd="/r",
            mode="dontAsk", started_at=time.time(), detached_message="bg",
        )

    running = _job("j-run", "t1")
    tool_only = _job("j-tool", "t4")   # an agent-dispatched claude_code run:
    finished_gone = _job("j-done", "t2")  # never enters the delivery registry
    finished_gone.done.set()
    delivering = _job("j-deliver", "t3")
    delivering.done.set()
    with bg._JOBS_LOCK:
        for job in (running, tool_only, finished_gone, delivering):
            bg._JOBS[job.id] = job
    with delivery._REGISTRY_LOCK:
        delivery._ACTIVE["t1"] = running       # same job in both registries
        delivery._ACTIVE["t3"] = delivering    # finished, still delivering
    try:
        client, _, token = _client(tmp_path, api_client_builder)
        payload = client.get(
            "/status/turns", headers=api_client_builder.auth(token)
        ).json()
        assert payload["claude_code_jobs"] == 3
        assert payload["background_jobs"] == 0
        assert payload["active_turns"] == 0
    finally:
        with bg._JOBS_LOCK:
            for job in (running, tool_only, finished_gone, delivering):
                bg._JOBS.pop(job.id, None)
        with delivery._REGISTRY_LOCK:
            delivery._ACTIVE.pop("t1", None)
            delivery._ACTIVE.pop("t3", None)


def test_agent_without_lock_manager_answers_503(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    del agent._thread_locks
    response = client.get("/status/turns", headers=api_client_builder.auth(token))
    # 503, not 500: the deploy-sync script treats it as unverifiable and
    # defers, and clients can distinguish "not ready" from a crash.
    assert response.status_code == 503


# -- #423: the booted code identity, for deploy-sync's restart verify ---------


def _get(client, builder, token) -> dict:
    response = client.get("/status/turns", headers=builder.auth(token))
    assert response.status_code == 200
    return response.json()


def test_a_container_without_git_reports_its_files_but_no_commit(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """V1: a Docker container's mounts carry no .git, so the commit is null
    and the file digest is what deploy-sync compares."""
    monkeypatch.setattr(prov, "_BOOT", _boot(commit=None, fingerprint=FP))
    client, _, token = _client(tmp_path, api_client_builder)
    payload = _get(client, api_client_builder, token)
    assert payload["code_version"] is None
    assert payload["code_fingerprint"] == prov.fingerprint_digest(FP)


def test_a_boot_without_a_fingerprint_reports_a_null_digest(
    tmp_path: Path, api_client_builder, monkeypatch
):
    monkeypatch.setattr(prov, "_BOOT", _boot(commit=SHA_A, fingerprint=None))
    client, _, token = _client(tmp_path, api_client_builder)
    payload = _get(client, api_client_builder, token)
    assert payload["code_version"] == SHA_A
    assert payload["code_fingerprint"] is None


def test_identity_is_the_boot_record_not_the_files_on_disk_now(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """V2: deploy-sync asks what the process LOADED; an edit after boot
    (or a later pull) must not change the answer, or a process that never
    restarted would verify as running the new code."""
    from tests.test_runtime_provenance import _git, _package

    pkg = _package(tmp_path / "checkout")
    _git(tmp_path / "checkout", "ref: refs/heads/main", {"refs/heads/main": SHA_A})
    record = prov.capture(pkg)
    monkeypatch.setattr(prov, "_BOOT", record)
    client, _, token = _client(tmp_path, api_client_builder)

    before = _get(client, api_client_builder, token)
    edited = pkg / "core" / "mod1.py"
    edited.write_text(edited.read_text() + "# edited after boot\n")
    st = edited.stat()
    os.utime(edited, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    after = _get(client, api_client_builder, token)

    assert record.fingerprint is not None
    assert before["code_version"] == after["code_version"] == SHA_A
    assert before["code_fingerprint"] == after["code_fingerprint"]
    assert after["code_fingerprint"] == prov.fingerprint_digest(record.fingerprint)
    # The tree really did change: a fresh walk would have said so.
    assert prov.source_fingerprint(pkg) != record.fingerprint


@pytest.mark.parametrize("broken", ["boot_record", "fingerprint_digest"])
def test_an_unreadable_boot_record_never_breaks_the_idle_gate(
    tmp_path: Path, api_client_builder, monkeypatch, broken
):
    """V3: the same response is deploy-sync's idle gate; a provenance
    failure reports both identity fields null and the counts still answer."""
    monkeypatch.setattr(prov, "_BOOT", _boot(commit=SHA_A, fingerprint=FP))

    def boom(*_a, **_k):
        raise RuntimeError("provenance on fire")

    monkeypatch.setattr(prov, broken, boom)
    client, agent, token = _client(tmp_path, api_client_builder)
    lock = agent._thread_locks.get_lock("t-busy")
    lock.acquire()
    try:
        payload = _get(client, api_client_builder, token)
    finally:
        lock.release()
    assert payload["active_turns"] == 1
    assert payload["code_version"] is None
    assert payload["code_fingerprint"] is None


def test_non_admin_payload_is_unchanged_by_the_identity_fields(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """K2: any account can read /status/turns, so the identity (admin and
    service-token callers only, like busy_threads) is not just nulled for
    others but absent: their payload keeps its pre-#423 keys exactly."""
    monkeypatch.setattr(prov, "_BOOT", _boot(commit=SHA_A, fingerprint=FP))
    client, _, token = _client(tmp_path, api_client_builder, role="user")
    payload = _get(client, api_client_builder, token)
    assert set(payload) == COUNT_KEYS
    assert SHA_A not in str(payload)


def test_health_field_set_is_unchanged(tmp_path: Path, api_client_builder, monkeypatch):
    """/health is public and deliberately coarse; the identity stays off it."""
    monkeypatch.setattr(prov, "_BOOT", _boot(commit=SHA_A, fingerprint=FP))
    settings = api_client_builder.settings(tmp_path)
    client = api_client_builder.client(FakeAgent(tmp_path), settings)
    payload = client.get("/health").json()
    assert set(payload) == {"status", "version", "configured"}


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


@pytest.mark.parametrize("work_kind", ["embedding", "runner"])
def test_detached_work_prevents_idle_after_thread_lock_releases(
    work_kind, tmp_path, api_client_builder,
):
    import asyncio
    import threading

    import httpx

    from nymeria.core.embedding_jobs import schedule_embedding_job, wait_for_pending_embedding_jobs
    from nymeria.core.turn_runner import TurnSpec, open_turn_runner, start_turn
    from tests.test_turn_stream_buffer import _FakeAgent

    async def exercise():
        open_turn_runner()
        client, _, token = _client(tmp_path, api_client_builder)
        entered = asyncio.Event()
        release_sync = threading.Event()
        release_async = asyncio.Event()
        loop = asyncio.get_running_loop()
        task = None

        def index():
            loop.call_soon_threadsafe(entered.set)
            assert release_sync.wait(10)

        class WaitingAgent(_FakeAgent):
            async def astream(self, message, **kwargs):
                entered.set()
                await release_async.wait()
                kwargs["_on_turn_started"]()
                yield {"type": "response", "content": "finished"}

        if work_kind == "embedding":
            schedule_embedding_job(index, site="test.idle")
        else:
            task = start_turn(WaitingAgent(), TurnSpec("hello", "idle-gap", "probe"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as reader:
                before = (await reader.get("/status/turns", headers=api_client_builder.auth(token))).json()
                assert before["active_turns"] == before["interactive_active"] == 0
                assert before["background_jobs"] == 1
                release_sync.set()
                release_async.set()
                if task is not None:
                    await task
                await wait_for_pending_embedding_jobs()
                after = (await reader.get("/status/turns", headers=api_client_builder.auth(token))).json()
                assert after["background_jobs"] == 0
        finally:
            release_sync.set()
            release_async.set()
            if task is not None:
                await task
            await wait_for_pending_embedding_jobs()

    asyncio.run(exercise())
