"""Tests for the slim-mode service-token bootstrap helper."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.service_bootstrap import (
    SLIM_SERVICE_TOKEN_FILENAME,
    SLIM_SERVICE_TOKEN_LABEL,
    SLIM_SERVICE_USER_ID,
    ensure_slim_service_token,
)


def _make_repo(tmp_path: Path) -> AccountsRepo:
    return AccountsRepo(tmp_path / "accounts.db")


def test_bootstrap_creates_bot_service_when_absent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    raw = ensure_slim_service_token(repo, tmp_path)

    assert raw.startswith("nym_")
    user = repo.get_user_by_id(SLIM_SERVICE_USER_ID)
    assert user is not None
    assert user.role == "admin"
    assert user.disabled is False

    token_file = tmp_path / SLIM_SERVICE_TOKEN_FILENAME
    assert token_file.is_file()
    assert token_file.read_text(encoding="utf-8").strip() == raw

    resolved = repo.verify_token(raw)
    assert resolved is not None
    assert resolved.id == SLIM_SERVICE_USER_ID
    assert resolved.role == "admin"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="chmod is best-effort on Windows")
def test_bootstrap_writes_token_file_with_restrictive_permissions(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    ensure_slim_service_token(repo, tmp_path)

    token_file = tmp_path / SLIM_SERVICE_TOKEN_FILENAME
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600


def test_bootstrap_reuses_valid_token_on_second_call(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    first = ensure_slim_service_token(repo, tmp_path)
    second = ensure_slim_service_token(repo, tmp_path)

    assert first == second
    # Only one slim-service token should be active.
    tokens = [
        t
        for t in repo.list_tokens_for_user(SLIM_SERVICE_USER_ID)
        if t.label == SLIM_SERVICE_TOKEN_LABEL and t.revoked_at is None
    ]
    assert len(tokens) == 1


def test_bootstrap_rotates_when_stored_token_is_invalid(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (tmp_path / SLIM_SERVICE_TOKEN_FILENAME).write_text(
        "nym_definitely-not-a-real-token\n",
        encoding="utf-8",
    )

    raw = ensure_slim_service_token(repo, tmp_path)

    assert raw.startswith("nym_")
    assert raw != "nym_definitely-not-a-real-token"
    resolved = repo.verify_token(raw)
    assert resolved is not None and resolved.role == "admin"


def test_bootstrap_rotates_when_stored_token_revoked(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first = ensure_slim_service_token(repo, tmp_path)

    tokens = repo.list_tokens_for_user(SLIM_SERVICE_USER_ID)
    assert len(tokens) == 1
    repo.revoke_token(SLIM_SERVICE_USER_ID, tokens[0].token_hash[:16])

    second = ensure_slim_service_token(repo, tmp_path)
    assert second != first
    resolved = repo.verify_token(second)
    assert resolved is not None and resolved.role == "admin"


def test_bootstrap_prefers_configured_token_when_valid(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.create_user(
        user_id="ops-admin",
        email="ops-admin@example.com",
        display_name="Ops",
        role="admin",
    )
    operator_token = repo.issue_token("ops-admin", label="operator")

    raw = ensure_slim_service_token(
        repo,
        tmp_path,
        configured_token=operator_token,
    )

    assert raw == operator_token
    # Slim should NOT have provisioned a bot-service user when an explicit
    # operator token is in use.
    assert repo.get_user_by_id(SLIM_SERVICE_USER_ID) is None
    assert not (tmp_path / SLIM_SERVICE_TOKEN_FILENAME).exists()


def test_bootstrap_ignores_configured_token_when_invalid(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    raw = ensure_slim_service_token(
        repo,
        tmp_path,
        configured_token="nym_garbage",
    )

    assert raw.startswith("nym_") and raw != "nym_garbage"
    user = repo.get_user_by_id(SLIM_SERVICE_USER_ID)
    assert user is not None and user.role == "admin"


def test_bootstrap_promotes_non_admin_bot_service(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.create_user(
        user_id=SLIM_SERVICE_USER_ID,
        email="bot-service@localhost",
        display_name="Bot Service",
        role="user",
    )

    ensure_slim_service_token(repo, tmp_path)

    user = repo.get_user_by_id(SLIM_SERVICE_USER_ID)
    assert user is not None
    assert user.role == "admin"
    assert user.disabled is False


def test_bootstrap_reenables_disabled_bot_service(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Need a second admin so disabling bot-service doesn't trip the last-admin
    # guard.
    repo.create_user(
        user_id="other-admin",
        email="other-admin@example.com",
        display_name="Other Admin",
        role="admin",
    )
    repo.create_user(
        user_id=SLIM_SERVICE_USER_ID,
        email="bot-service@localhost",
        display_name="Bot Service",
        role="admin",
    )
    repo.set_disabled(SLIM_SERVICE_USER_ID, True)

    ensure_slim_service_token(repo, tmp_path)

    user = repo.get_user_by_id(SLIM_SERVICE_USER_ID)
    assert user is not None
    assert user.disabled is False
