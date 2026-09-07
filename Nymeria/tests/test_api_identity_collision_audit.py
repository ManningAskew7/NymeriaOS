"""Startup audit for identity-id collisions (spec behavior 5).

``_register_identity_collision_audit`` walks the account table and the
thread index once at API startup and logs ONE warning per group of ids that
fold to the same storage segment, naming the ids and the segment. A clean
deployment logs nothing. The audit never renames, deletes, or refuses to
boot: a repo failure is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from nymeria.core.accounts import AccountsRepo
from nymeria.triggers.api import _register_identity_collision_audit

LOGGER = "nymeria.triggers.api"


def _insert_legacy_user(repo: AccountsRepo, user_id: str) -> None:
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, role, disabled, created_at, updated_at) "
            "VALUES (?, ?, ?, 'user', 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (user_id, f"{user_id}@legacy.example", user_id),
        )
        conn.commit()


def _app_for(repo: object) -> FastAPI:
    app = FastAPI()
    _register_identity_collision_audit(
        app, agent_getter=lambda: SimpleNamespace(accounts_repo=repo)
    )
    return app


def _run_startup(app: FastAPI) -> None:
    asyncio.run(app.router.on_startup[-1]())


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


def test_register_wires_exactly_one_startup_handler() -> None:
    app = FastAPI()
    n_start, n_stop = len(app.router.on_startup), len(app.router.on_shutdown)

    _register_identity_collision_audit(app, agent_getter=lambda: None)

    assert len(app.router.on_startup) == n_start + 1
    assert len(app.router.on_shutdown) == n_stop


def test_audit_logs_one_warning_per_collision_group(tmp_path: Path, caplog) -> None:
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    repo.create_user("bob", "bob@example.com", "Bob")
    for legacy in ["alice.smith", "alicesmith", "!!!", "lone.wolf"]:
        _insert_legacy_user(repo, legacy)
    repo.backfill_threads(
        ["qa-collide/1", "qa-collide1", "qa-collide.1", "solo.thread"], "default"
    )
    users_before = sorted(u.id for u in repo.list_users())
    threads_before = sorted(repo.list_thread_ids())

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _run_startup(_app_for(repo))

    warnings = _warnings(caplog)
    assert len(warnings) == 3
    (alice,) = [m for m in warnings if "'alice.smith'" in m]
    assert "'alicesmith'" in alice and "segment 'alicesmith'" in alice and "user" in alice
    (owner,) = [m for m in warnings if "'!!!'" in m]
    assert "'default'" in owner and "segment 'default'" in owner
    (threads,) = [m for m in warnings if "'qa-collide/1'" in m]
    assert "'qa-collide1'" in threads and "'qa-collide.1'" in threads
    assert "segment 'qa-collide1'" in threads and "thread" in threads
    # Lone non-canonical ids collide with nothing and are not reported.
    assert not any("lone.wolf" in m or "solo.thread" in m for m in warnings)
    # Nothing renamed or deleted.
    assert sorted(u.id for u in repo.list_users()) == users_before
    assert sorted(repo.list_thread_ids()) == threads_before


def test_audit_is_silent_on_a_clean_deployment(tmp_path: Path, caplog) -> None:
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    repo.create_user("alice", "alice@example.com", "Alice")
    repo.claim_thread("qa-collide1", "alice")
    repo.claim_thread("telegram_-100123", "default")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _run_startup(_app_for(repo))

    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_audit_failure_never_blocks_boot(caplog) -> None:
    def _locked():
        raise sqlite3.OperationalError("database is locked")

    broken = SimpleNamespace(list_users=_locked, list_thread_ids=_locked)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        _run_startup(_app_for(broken))  # must not raise

    errors = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "collision audit" in errors[0].getMessage().lower()


def test_audit_reports_case_variants_as_one_group(tmp_path: Path, caplog) -> None:
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    _insert_legacy_user(repo, "Alice")
    _insert_legacy_user(repo, "alice")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _run_startup(_app_for(repo))

    (message,) = _warnings(caplog)
    assert "'Alice'" in message and "'alice'" in message and "segment 'alice'" in message
