"""Unit tests for ``AccountsRepo.issue_token`` (optimization slice 04, F13).

The fix folds the user-existence check into the same ``self._lock`` hold as the
token insert, so a concurrent ``delete_user_cascade`` (which takes the same
non-reentrant lock, and whose ``user_tokens.user_id`` FK is ``ON DELETE
CASCADE`` under ``PRAGMA foreign_keys = ON``) cannot delete the user between the
check and the insert. Previously the pre-lock ``get_user_by_id`` call released
the lock before the insert block re-acquired it, leaving a window in which the
insert would hit the FK and raise a raw ``sqlite3.IntegrityError`` instead of a
clean ``UserNotFound``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from nymeria.core.accounts import (
    AccountsRepo,
    TokenLimitExceeded,
    UserAlreadyExists,
    UserNotFound,
)


def _make_repo(tmp_path: Path, **kwargs) -> AccountsRepo:
    return AccountsRepo(tmp_path / "accounts.db", **kwargs)


def test_issue_token_unknown_user_raises_and_creates_no_row(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    with pytest.raises(UserNotFound):
        repo.issue_token("ghost")

    # No token row should leak for a user that never existed.
    assert repo.list_tokens_for_user("ghost") == []


def test_issue_token_happy_path_is_verifiable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.create_user("u1", "u1@example.com", "User One")

    raw = repo.issue_token("u1", label="cli")

    assert raw.startswith("nym_")
    resolved = repo.verify_token(raw)
    assert resolved is not None and resolved.id == "u1"


def test_issue_token_enforces_active_token_limit(tmp_path: Path) -> None:
    # Guard the limit branch that the fix relocated below the inlined check.
    repo = _make_repo(tmp_path, max_active_tokens_per_user=2)
    repo.create_user("u1", "u1@example.com", "User One")

    repo.issue_token("u1")
    repo.issue_token("u1")
    with pytest.raises(TokenLimitExceeded):
        repo.issue_token("u1")


class _CountingLock:
    """Wraps a real lock and counts ``with`` (context-manager) acquisitions."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.enter_count = 0

    def __enter__(self):
        self.enter_count += 1
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def acquire(self, *args, **kwargs):
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        return self._inner.release()


def test_issue_token_acquires_lock_exactly_once(tmp_path: Path) -> None:
    """The check and insert run under one lock hold.

    Before the fix, ``issue_token`` acquired ``self._lock`` twice: once via the
    pre-lock ``get_user_by_id`` existence check, then again for the insert
    block. The TOCTOU window lived in that gap. Folding the inlined check into
    the insert block collapses it to a single acquisition.
    """
    repo = _make_repo(tmp_path)
    repo.create_user("u1", "u1@example.com", "User One")

    counting = _CountingLock(repo._lock)
    repo._lock = counting  # type: ignore[assignment]

    repo.issue_token("u1")

    assert counting.enter_count == 1


def test_issue_token_under_concurrent_user_churn_never_leaks_integrity_error(
    tmp_path: Path,
) -> None:
    """End-to-end guard: with the existence check and insert under one lock,
    racing token issuance against user delete/recreate only ever surfaces a
    clean ``UserNotFound`` (or succeeds), never a raw FK ``IntegrityError``.

    A high active-token cap keeps the run focused on the user-existence race
    rather than the token-limit branch. The iteration counts are kept modest so
    the run stays well under the repo-wide 30s per-test timeout even on a busy
    host.
    """
    repo = _make_repo(tmp_path, max_active_tokens_per_user=100_000)
    repo.create_user("u1", "u1@example.com", "User One")

    errors: list[Exception] = []
    successes = 0
    successes_lock = threading.Lock()
    churn_done = threading.Event()

    def issuer() -> None:
        nonlocal successes
        while not churn_done.is_set():
            try:
                repo.issue_token("u1")
            except UserNotFound:
                pass  # acceptable: user was concurrently deleted
            except Exception as exc:  # noqa: BLE001 - test asserts none leak
                errors.append(exc)
                return
            else:
                with successes_lock:
                    successes += 1

    def churner() -> None:
        try:
            for _ in range(40):
                try:
                    repo.delete_user_cascade("u1")
                except UserNotFound:
                    pass
                try:
                    repo.create_user("u1", "u1@example.com", "User One")
                except UserAlreadyExists:
                    pass
        except Exception as exc:  # noqa: BLE001 - surface to the assert
            errors.append(exc)
        finally:
            churn_done.set()

    threads = [threading.Thread(target=issuer) for _ in range(2)]
    threads.append(threading.Thread(target=churner))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not any(t.is_alive() for t in threads), "threads did not finish in time"
    assert errors == [], f"unexpected errors leaked under contention: {errors!r}"
    # Guard against a vacuous pass where every issuance happened to hit the
    # deleted window: at least one token must have been issued successfully.
    assert successes > 0, "no token was issued successfully under contention"


def test_issue_token_cross_process_delete_race_surfaces_user_not_found(
    tmp_path: Path,
) -> None:
    """A cross-process delete that lands between the in-lock existence check and
    the insert must surface a clean ``UserNotFound``, not a raw
    ``sqlite3.IntegrityError``.

    ``self._lock`` only serializes one ``AccountsRepo`` instance, so this
    simulates another process (e.g. the ``nymeria users`` CLI) by deleting the
    user over a separate raw connection from inside issue_token's lock, after
    the existence check and before the insert. The insert then trips the
    ``ON DELETE CASCADE`` FK, which issue_token converts to ``UserNotFound``.
    """
    repo = _make_repo(tmp_path)
    repo.create_user("u1", "u1@example.com", "User One")

    original = repo._revoke_expired_tokens_locked
    fired = threading.Event()

    def hook(conn, *, user_id=None):
        # Runs inside issue_token's lock, after the existence check and before
        # the insert. Delete the user over a connection that bypasses
        # self._lock to stand in for a concurrent process.
        if not fired.is_set():
            fired.set()
            side = sqlite3.connect(str(repo.db_path), timeout=30.0)
            try:
                side.execute("PRAGMA foreign_keys = ON")
                side.execute("DELETE FROM users WHERE id = ?", (user_id,))
                side.commit()
            finally:
                side.close()
        return original(conn, user_id=user_id)

    repo._revoke_expired_tokens_locked = hook  # type: ignore[assignment]

    with pytest.raises(UserNotFound):
        repo.issue_token("u1")

    assert fired.is_set()
    # The insert was rolled back, so no orphaned token row remains.
    assert repo.list_tokens_for_user("u1") == []
