"""Tests for the snapshot CLI (nymeria/cli/snapshot.py) at the dispatch level."""

import json
import sqlite3
from argparse import Namespace
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nymeria.cli import snapshot as snapshot_cli


@dataclass
class FakeSettings:
    data_dir: Path
    database_backend: str = "sqlite"
    postgres_uri: object = None
    api_port: int = 59322

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("NYMERIA_API_URL", raising=False)
    monkeypatch.delenv("NYMERIA_SNAPSHOT_PASSPHRASE", raising=False)
    monkeypatch.setenv("NYMERIA_SERVICE_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path / "workspace"))

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with closing(sqlite3.connect(data_dir / "accounts.db")) as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users VALUES ('alice')")
        conn.commit()
    (data_dir / "todos").mkdir()
    (data_dir / "todos" / "alice.json").write_text(
        json.dumps({"todos": []}), encoding="utf-8"
    )

    settings = FakeSettings(data_dir=data_dir)
    monkeypatch.setattr(snapshot_cli, "get_settings", lambda: settings)

    passphrase_file = tmp_path / "passphrase.txt"
    passphrase_file.write_text("cli-test-passphrase\n", encoding="utf-8")
    return settings, passphrase_file


def _ns(action: str, **kwargs) -> Namespace:
    defaults = {
        "passphrase_file": None,
        "output": None,
        "no_workspace": False,
        "no_verify": False,
        "no_encrypt": False,
        "include_code_backups": False,
        "artifact": None,
        "write_env": None,
        "force": False,
        "yes": False,
        "dir": None,
    }
    defaults.update(kwargs)
    return Namespace(action=action, **defaults)


def test_create_verify_list_flow(cli_env, capsys):
    settings, passphrase_file = cli_env

    rc = snapshot_cli.dispatch(
        _ns("create", passphrase_file=str(passphrase_file))
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Snapshot written:" in out
    assert "Snapshot verified." in out

    artifacts = list(settings.snapshots_dir.glob("*.nysnap"))
    assert len(artifacts) == 1

    rc = snapshot_cli.dispatch(
        _ns("verify", artifact=str(artifacts[0]), passphrase_file=str(passphrase_file))
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Verification passed." in out

    rc = snapshot_cli.dispatch(
        _ns("list", passphrase_file=str(passphrase_file))
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert artifacts[0].name in out
    assert "encrypted" in out


def test_list_without_passphrase_still_lists(cli_env, capsys):
    _, passphrase_file = cli_env
    assert snapshot_cli.dispatch(
        _ns("create", passphrase_file=str(passphrase_file), no_verify=True)
    ) == 0
    capsys.readouterr()
    rc = snapshot_cli.dispatch(_ns("list"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "encrypted" in out
    assert "NYMERIA_SNAPSHOT_PASSPHRASE" in out


def test_create_without_passphrase_fails_noninteractive(cli_env, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as excinfo:
        snapshot_cli.dispatch(_ns("create"))
    assert excinfo.value.code == 2
    assert "passphrase" in capsys.readouterr().err.lower()


def test_restore_refuses_when_stack_live(cli_env, capsys, monkeypatch):
    settings, passphrase_file = cli_env
    assert snapshot_cli.dispatch(
        _ns("create", passphrase_file=str(passphrase_file), no_verify=True)
    ) == 0
    capsys.readouterr()
    artifact = next(settings.snapshots_dir.glob("*.nysnap"))
    monkeypatch.setattr(
        snapshot_cli, "stack_liveness_signs", lambda s: ["API responded"]
    )
    rc = snapshot_cli.dispatch(
        _ns("restore", artifact=str(artifact), passphrase_file=str(passphrase_file))
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "appears to be running" in err


def test_restore_roundtrip_with_yes(cli_env, capsys, monkeypatch, tmp_path):
    settings, passphrase_file = cli_env
    assert snapshot_cli.dispatch(
        _ns("create", passphrase_file=str(passphrase_file), no_verify=True)
    ) == 0
    capsys.readouterr()
    artifact = next(settings.snapshots_dir.glob("*.nysnap"))

    target = FakeSettings(data_dir=tmp_path / "restore-target")
    target.data_dir.mkdir()
    monkeypatch.setattr(snapshot_cli, "get_settings", lambda: target)
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path / "restore-ws"))

    rc = snapshot_cli.dispatch(
        _ns(
            "restore",
            artifact=str(artifact),
            passphrase_file=str(passphrase_file),
            yes=True,
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Restore complete." in out
    assert (target.data_dir / "todos" / "alice.json").exists()


def test_restore_missing_artifact(cli_env, capsys):
    rc = snapshot_cli.dispatch(_ns("restore", artifact="/nonexistent/x.nysnap"))
    assert rc == 2
    assert "No such artifact" in capsys.readouterr().err


def test_force_never_bypasses_verification(cli_env, capsys, monkeypatch):
    """--force overrides the liveness guard ONLY; verification still gates."""
    settings, passphrase_file = cli_env
    assert snapshot_cli.dispatch(
        _ns("create", passphrase_file=str(passphrase_file), no_verify=True)
    ) == 0
    capsys.readouterr()
    artifact = next(settings.snapshots_dir.glob("*.nysnap"))

    from nymeria.core.snapshot_verify import SnapshotCheck

    monkeypatch.setattr(
        snapshot_cli, "stack_liveness_signs", lambda s: ["API responded"]
    )
    monkeypatch.setattr(
        snapshot_cli,
        "verify_extracted",
        lambda extracted: [SnapshotCheck("Manifest", "fail", "forced failure")],
    )
    rc = snapshot_cli.dispatch(
        _ns(
            "restore",
            artifact=str(artifact),
            passphrase_file=str(passphrase_file),
            force=True,
            yes=True,
        )
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Verification FAILED" in captured.err
    # Staging must be cleaned up on the refusal path.
    assert not list(settings.data_dir.glob(".snapshot-restore-*"))


def test_create_rejects_short_passphrase_from_env(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("NYMERIA_SNAPSHOT_PASSPHRASE", "short")
    with pytest.raises(SystemExit) as excinfo:
        snapshot_cli.dispatch(_ns("create"))
    assert excinfo.value.code == 2
    assert "at least" in capsys.readouterr().err


def test_verify_accepts_short_passphrase_for_existing_artifact(
    cli_env, capsys, monkeypatch, tmp_path
):
    """The strength floor applies to NEW artifacts only, never to reading."""
    _, passphrase_file = cli_env
    short_file = tmp_path / "short.txt"
    short_file.write_text("short", encoding="utf-8")
    # Resolution for verify (confirm=False) must return the short value.
    ns = _ns("verify", passphrase_file=str(short_file))
    assert (
        snapshot_cli._resolve_passphrase(ns, confirm=False, required=True) == "short"
    )


def test_unknown_action(capsys):
    assert snapshot_cli.dispatch(Namespace(action="explode")) == 2
    assert "Unknown snapshot action" in capsys.readouterr().err
