from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.cli import users as users_cli
from nymeria.core.accounts import AccountsRepo


def _patch_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    *,
    token_ttl_days: int = 90,
) -> None:
    monkeypatch.setattr(
        users_cli,
        "get_settings",
        lambda: SimpleNamespace(
            data_dir=data_dir,
            account_token_ttl_days=token_ttl_days,
            account_max_active_tokens_per_user=10,
            account_bootstrap_token_ttl_hours=24,
        ),
    )


def _extract_token(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Token: nym_"):
            return stripped.removeprefix("Token: ")
    raise AssertionError(f"No token line found in output: {output}")


def test_issue_token_by_user_id_preserves_existing_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_dir(monkeypatch, tmp_path)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    existing = repo.issue_token("default", label="laptop")

    result = users_cli.dispatch(
        Namespace(action="issue-token", user="default", label="desktop")
    )

    output = capsys.readouterr().out
    issued = _extract_token(output)
    tokens = repo.list_tokens_for_user("default")
    active = [token for token in tokens if token.revoked_at is None]

    assert result == 0
    assert "Existing tokens were not revoked" in output
    assert repo.verify_token(existing) is not None
    assert repo.verify_token(issued) is not None
    assert [token.label for token in active] == ["laptop", "desktop"]


def test_issue_token_replace_revokes_only_that_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machine tokens are re-baked on every provision, and the old one is dead
    the moment the new one is written over it. Without a label-scoped revoke
    they pile up against account_max_active_tokens_per_user until minting starts
    failing; `rotate-token` is not the answer because it would also revoke the
    service token the stack is running on."""
    _patch_data_dir(monkeypatch, tmp_path)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    stale = repo.issue_token("default", label="server-browser")
    other = repo.issue_token("default", label="laptop")

    result = users_cli.dispatch(
        Namespace(
            action="issue-token", user="default", label="server-browser", replace=True
        )
    )

    output = capsys.readouterr().out
    fresh = _extract_token(output)
    active = [t for t in repo.list_tokens_for_user("default") if t.revoked_at is None]

    assert result == 0
    assert repo.verify_token(stale) is None, "the replaced token still works"
    assert repo.verify_token(other) is not None, "another label was revoked too"
    assert repo.verify_token(fresh) is not None
    assert sorted(t.label for t in active) == ["laptop", "server-browser"]
    assert "Revoked 1 existing 'server-browser' token(s)" in output


def test_issue_token_replace_keeps_re_runs_under_the_active_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure this exists to prevent: the wizard's Docker path re-runs
    freely, and at the cap `issue-token` simply exits 2 with no explanation of
    why the browser stopped being provisioned."""
    _patch_data_dir(monkeypatch, tmp_path)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")

    for _ in range(12):
        assert (
            users_cli.dispatch(
                Namespace(
                    action="issue-token",
                    user="default",
                    label="server-browser",
                    replace=True,
                )
            )
            == 0
        )
        capsys.readouterr()

    active = [t for t in repo.list_tokens_for_user("default") if t.revoked_at is None]
    assert len(active) == 1


def test_issue_token_replace_without_a_label_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unlabelled --replace has no scope, and the tempting reading (revoke
    everything) is exactly what rotate-token is for."""
    _patch_data_dir(monkeypatch, tmp_path)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    existing = repo.issue_token("default", label="laptop")

    result = users_cli.dispatch(
        Namespace(action="issue-token", user="default", label=None, replace=True)
    )

    assert result == 2
    assert "--replace needs --label" in capsys.readouterr().err
    assert repo.verify_token(existing) is not None


def test_issue_token_by_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_dir(monkeypatch, tmp_path)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")

    result = users_cli.dispatch(
        Namespace(action="issue-token", user="owner@localhost", label="desktop")
    )

    output = capsys.readouterr().out
    issued = _extract_token(output)

    assert result == 0
    assert repo.verify_token(issued) is not None


def test_issue_token_honors_configured_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI repo must pass settings TTLs through (it once minted 90d always)."""
    _patch_data_dir(monkeypatch, tmp_path, token_ttl_days=365)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")

    result = users_cli.dispatch(
        Namespace(action="issue-token", user="default", label="service")
    )

    output = capsys.readouterr().out
    _extract_token(output)
    (token,) = repo.list_tokens_for_user("default")
    created = datetime.fromisoformat(token.created_at)
    expires = datetime.fromisoformat(token.expires_at)

    assert result == 0
    assert expires - created == timedelta(days=365)


def test_issue_token_reports_missing_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_dir(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        users_cli.dispatch(
            Namespace(action="issue-token", user="missing", label=None)
        )

    captured = capsys.readouterr()

    assert exc_info.value.code == 2
    assert captured.out == ""
    assert "[error] No user with id or email: missing" in captured.err


def test_issue_token_reports_active_token_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_dir(monkeypatch, tmp_path)
    repo = AccountsRepo(tmp_path / "accounts.db")
    repo.create_user("default", "owner@localhost", "Owner", role="admin")
    for index in range(10):
        repo.issue_token("default", label=f"token-{index}")

    result = users_cli.dispatch(
        Namespace(action="issue-token", user="default", label="desktop")
    )

    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert "already has the maximum 10 active token(s)" in captured.err


def test_add_refuses_non_canonical_id_and_says_what_to_do(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_dir(monkeypatch, tmp_path)

    result = users_cli.dispatch(
        Namespace(
            action="add",
            email="alice@example.com",
            role="user",
            display_name=None,
            id="alice.smith",
            label=None,
        )
    )

    captured = capsys.readouterr()
    repo = AccountsRepo(tmp_path / "accounts.db")
    assert result == 2
    assert captured.out == ""
    assert "[error]" in captured.err
    assert "stored as 'alicesmith'" in captured.err
    assert "letters, digits, '-' and '_'" in captured.err
    assert repo.get_user_by_id("alice.smith") is None
    assert repo.get_user_by_email("alice@example.com") is None


def test_add_default_slug_from_dotted_email_is_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_data_dir(monkeypatch, tmp_path)

    result = users_cli.dispatch(
        Namespace(
            action="add",
            email="alice.smith@example.com",
            role="user",
            display_name=None,
            id=None,
            label=None,
        )
    )

    output = capsys.readouterr().out
    repo = AccountsRepo(tmp_path / "accounts.db")
    assert result == 0
    assert repo.verify_token(_extract_token(output)).id == "alice_smith"
