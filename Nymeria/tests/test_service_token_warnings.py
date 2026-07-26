"""Service-token expiry warning sweep (backlog #107).

The sweep is the before-the-fact half of the service-token lifecycle: token
expiry is otherwise lazy (nothing reads ``expires_at`` until a caller 401s),
which is how the reference host's NYMERIA_SERVICE_TOKEN died silently. These
tests pin the read-only probe (no lazy revocation from a check), the
service-shape predicate, the phase dedup (one warning per threshold, durable
across restarts), state pruning on rotation, and the disable switch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nymeria.core import service_bootstrap as sb
from nymeria.core.accounts import AccountsRepo, TokenRecord


def _repo(tmp_path: Path, **kwargs) -> AccountsRepo:
    return AccountsRepo(tmp_path / "accounts.db", **kwargs)


def _backdate_expiry(db_path: Path, token_hash_prefix: str, expires_at: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE user_tokens SET expires_at = ? WHERE token_hash LIKE ?",
            (expires_at, f"{token_hash_prefix}%"),
        )
        conn.commit()


def _service_setup(tmp_path: Path, *, token_ttl_days: int = 10):
    """Repo with a human admin, the bot-service admin, and one service token."""
    repo = _repo(tmp_path, token_ttl_days=token_ttl_days)
    repo.create_user("alice", "alice@example.com", "Alice", role="admin")
    repo.create_user(
        sb.SLIM_SERVICE_USER_ID, sb.SLIM_SERVICE_EMAIL, sb.SLIM_SERVICE_DISPLAY_NAME,
        role="admin",
    )
    repo.issue_token(sb.SLIM_SERVICE_USER_ID, label=sb.SLIM_SERVICE_TOKEN_LABEL)
    return repo


@pytest.fixture()
def captured_notifications(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    def _capture(user_id: str, summary: str, thread_id=None, *, push=False, log_label=""):
        sent.append((user_id, summary))

    from nymeria.core import notifications

    monkeypatch.setattr(notifications, "notify_user_best_effort", _capture)
    return sent


def _record(user_id: str = "someone", label: str | None = None) -> TokenRecord:
    return TokenRecord(
        token_hash="x" * 64,
        user_id=user_id,
        label=label,
        created_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-12-31T00:00:00+00:00",
        last_used_at=None,
        revoked_at=None,
    )


def test_is_service_token_predicate_matches_conventions() -> None:
    # The bot-service user matches regardless of label; elsewhere only the
    # exact conventional labels match (case-insensitive). Free-text human
    # labels containing "service" deliberately do NOT match: a substring test
    # would spam admins about (and disclose) another user's personal token.
    assert sb.is_service_token(_record(user_id=sb.SLIM_SERVICE_USER_ID))
    assert sb.is_service_token(_record(label="slim-service"))
    assert sb.is_service_token(_record(label="nymeria_service_token"))
    assert sb.is_service_token(_record(label="service"))
    assert sb.is_service_token(_record(label="Service"))
    assert not sb.is_service_token(_record(label="My Service token"))
    assert not sb.is_service_token(_record(label="customer-service"))
    assert not sb.is_service_token(_record(label="bootstrap"))
    assert not sb.is_service_token(_record(label="cli"))
    assert not sb.is_service_token(_record(label=None))


def test_expiry_probe_is_read_only_and_includes_expired_rows(tmp_path: Path) -> None:
    repo = _service_setup(tmp_path)
    # Backdate the token past expiry WITHOUT going through the lazy-revoking
    # accessors, then probe: the row must come back and must stay unrevoked.
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    _backdate_expiry(tmp_path / "accounts.db", "", past)

    cutoff = datetime.now(timezone.utc) + timedelta(days=14)
    records = repo.list_unrevoked_tokens_expiring_before(cutoff)
    assert len(records) == 1
    assert records[0].revoked_at is None

    with sqlite3.connect(tmp_path / "accounts.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT revoked_at FROM user_tokens").fetchone()
    assert row["revoked_at"] is None, "the probe must not revoke as a side effect"


def test_expiry_probe_excludes_tokens_beyond_cutoff(tmp_path: Path) -> None:
    repo = _service_setup(tmp_path, token_ttl_days=90)
    cutoff = datetime.now(timezone.utc) + timedelta(days=14)
    assert repo.list_unrevoked_tokens_expiring_before(cutoff) == []


def test_sweep_warns_once_per_phase_and_escalates(
    tmp_path: Path, captured_notifications: list[tuple[str, str]]
) -> None:
    repo = _service_setup(tmp_path, token_ttl_days=10)
    now = datetime.now(timezone.utc)

    # Entering the 14-day window (10 days left): one warning, to the human
    # admin only (bot-service is excluded even though it is an admin).
    warned = sb.sweep_expiring_service_tokens(repo, tmp_path, warn_days=14, now=now)
    assert warned == 1
    assert [user for user, _ in captured_notifications] == ["alice"]
    # floor of ~9.9997 days: the copy never overstates the time left.
    assert "expires in 9 days" in captured_notifications[0][1]

    # Same phase an hour later: deduped, including across a "restart" (the
    # state is re-read from disk on every sweep).
    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=now + timedelta(hours=1)
    )
    assert warned == 0

    # 2 days left: the 3-day phase fires.
    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=now + timedelta(days=8)
    )
    assert warned == 1

    # 12 hours left: the 1-day phase fires.
    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=now + timedelta(days=9, hours=12)
    )
    assert warned == 1

    # Past expiry: the final EXPIRED phase fires, then stays quiet.
    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=now + timedelta(days=11)
    )
    assert warned == 1
    assert "has EXPIRED" in captured_notifications[-1][1]

    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=now + timedelta(days=12)
    )
    assert warned == 0
    assert len(captured_notifications) == 4


def test_sweep_prunes_state_for_rotated_tokens(
    tmp_path: Path, captured_notifications: list[tuple[str, str]]
) -> None:
    repo = _service_setup(tmp_path, token_ttl_days=10)
    now = datetime.now(timezone.utc)
    assert sb.sweep_expiring_service_tokens(repo, tmp_path, warn_days=14, now=now) == 1

    state_path = tmp_path / sb.SERVICE_TOKEN_WARN_STATE_FILENAME
    old_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(old_state) == 1

    # Rotate: revoke everything and mint a fresh token far from expiry.
    repo.revoke_all_tokens(sb.SLIM_SERVICE_USER_ID)
    fresh_repo = AccountsRepo(tmp_path / "accounts.db", token_ttl_days=365)
    fresh_repo.issue_token(sb.SLIM_SERVICE_USER_ID, label=sb.SLIM_SERVICE_TOKEN_LABEL)

    assert sb.sweep_expiring_service_tokens(fresh_repo, tmp_path, warn_days=14, now=now) == 0
    new_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert new_state == {}, "state for rotated-away tokens must be pruned"


def test_sweep_ignores_non_service_tokens(
    tmp_path: Path, captured_notifications: list[tuple[str, str]]
) -> None:
    repo = _repo(tmp_path, token_ttl_days=5)
    repo.create_user("alice", "alice@example.com", "Alice", role="admin")
    repo.issue_token("alice", label="cli")

    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=datetime.now(timezone.utc)
    )
    assert warned == 0
    assert captured_notifications == []


def test_sweep_disabled_at_zero_warn_days(
    tmp_path: Path, captured_notifications: list[tuple[str, str]]
) -> None:
    repo = _service_setup(tmp_path, token_ttl_days=1)
    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=0, now=datetime.now(timezone.utc)
    )
    assert warned == 0
    assert captured_notifications == []
    assert not (tmp_path / sb.SERVICE_TOKEN_WARN_STATE_FILENAME).exists()


def test_sweep_counts_warning_even_with_no_human_admin(
    tmp_path: Path, captured_notifications: list[tuple[str, str]]
) -> None:
    # Only the bot-service admin exists: nothing to notify, but the warning is
    # still counted (and logged) so the operator can see it in the API log.
    repo = _repo(tmp_path, token_ttl_days=5)
    repo.create_user(
        sb.SLIM_SERVICE_USER_ID, sb.SLIM_SERVICE_EMAIL, sb.SLIM_SERVICE_DISPLAY_NAME,
        role="admin",
    )
    repo.issue_token(sb.SLIM_SERVICE_USER_ID, label=sb.SLIM_SERVICE_TOKEN_LABEL)

    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=datetime.now(timezone.utc)
    )
    assert warned == 1
    assert captured_notifications == []


def test_sweep_survives_corrupt_state_file(
    tmp_path: Path, captured_notifications: list[tuple[str, str]]
) -> None:
    repo = _service_setup(tmp_path, token_ttl_days=5)
    state_path = tmp_path / sb.SERVICE_TOKEN_WARN_STATE_FILENAME
    state_path.write_text("{not json", encoding="utf-8")

    warned = sb.sweep_expiring_service_tokens(
        repo, tmp_path, warn_days=14, now=datetime.now(timezone.utc)
    )
    assert warned == 1
    # The corrupt file was replaced with valid state.
    assert isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict)
