"""Tests for user-data snapshot capture, verify, and restore (core/snapshot*)."""

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.core.snapshot import (
    ExtractedSnapshot,
    SnapshotError,
    _move_into,
    create_snapshot,
    extract_snapshot,
    read_snapshot_meta,
    restore_snapshot,
    stack_liveness_signs,
)
from nymeria.core.snapshot_stores import (
    SnapshotStoreError,
    dump_postgres_checkpoints,
    iter_data_dir_files,
    restore_postgres_checkpoints,
    sqlite_consistent_copy,
    sqlite_integrity_ok,
)
from nymeria.core.snapshot_verify import has_failures, verify_extracted

PASSPHRASE = "test-passphrase-1"


@dataclass
class FakeSettings:
    data_dir: Path
    database_backend: str = "sqlite"
    postgres_uri: object = None
    api_port: int = 59321  # nothing listens here

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"


@pytest.fixture()
def snapshot_env(tmp_path, monkeypatch):
    """A populated fake data dir + workspace, with a real encrypted vault."""
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("NYMERIA_API_URL", raising=False)
    monkeypatch.setenv("NYMERIA_SERVICE_HEALTH_DIR", str(tmp_path / "health"))

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # accounts.db with a real user and a real encrypted vault secret (WAL).
    accounts = AccountsRepo(data_dir / "accounts.db")
    accounts.create_user("alice", "alice@example.com", "Alice")
    vault = CredentialVaultRepo(data_dir / "accounts.db")
    vault.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example API",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-canary-secret"},
        created_by_user_id="alice",
    )

    # A rollback-journal (non-WAL) database, like todo_schedule.db.
    with closing(sqlite3.connect(data_dir / "todo_schedule.db")) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            "INSERT INTO items (body) VALUES (?)", [(f"row-{i}",) for i in range(20)]
        )
        conn.commit()

    # JSON stores.
    (data_dir / "todos").mkdir()
    (data_dir / "todos" / "alice.json").write_text(
        json.dumps({"todos": [{"id": "t1", "title": "water plants"}]}),
        encoding="utf-8",
    )
    (data_dir / "system_prompt.md").write_text("custom soul", encoding="utf-8")

    # Excluded content that must NOT be captured.
    for name in ("snapshots", "logs", "voice", "backups", "flags"):
        (data_dir / name).mkdir()
        (data_dir / name / "marker.txt").write_text("nope", encoding="utf-8")
    # The server browser's rig: a live account token, the browser's logged-in
    # site sessions, and a 200 MB Chrome, none of which belong in a portable
    # artifact.
    rig = data_dir / "server-browser"
    (rig / "ext").mkdir(parents=True)
    (rig / "ext" / "config.json").write_text('{"token": "nym_secret"}', encoding="utf-8")
    (rig / "profile" / "Default").mkdir(parents=True)
    (rig / "profile" / "Default" / "Cookies").write_bytes(b"SQLite format 3\x00session")
    (rig / "cft" / "152.0.7977.64").mkdir(parents=True)
    (rig / "cft" / "152.0.7977.64" / "chrome").write_bytes(b"ELF")
    (rig / "rig.json").write_text('{"client_id": "nymeria-browser-x"}', encoding="utf-8")
    (data_dir / "orphan.db-wal").write_bytes(b"sidecar")
    (data_dir / "leftover.tmp").write_bytes(b"tmp")

    # Workspace with durable and scratch subtrees.
    workspace = tmp_path / "workspace"
    (workspace / "threads" / "t1" / "attachments").mkdir(parents=True)
    (workspace / "threads" / "t1" / "attachments" / "a.txt").write_text(
        "attached", encoding="utf-8"
    )
    (workspace / "images").mkdir()
    (workspace / "images" / "gen.png").write_bytes(b"\x89PNG fake")
    (workspace / "scratch").mkdir()
    (workspace / "scratch" / "junk.bin").write_bytes(b"junk")
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(workspace))

    return FakeSettings(data_dir=data_dir)


def _create(settings, **kwargs):
    kwargs.setdefault("passphrase", PASSPHRASE)
    return create_snapshot(settings, **kwargs)


def test_walker_excludes(snapshot_env):
    names = {rel for _, rel in iter_data_dir_files(snapshot_env.data_dir)}
    assert "accounts.db" in names
    assert "todos/alice.json" in names
    assert not any(name.startswith("snapshots/") for name in names)
    assert not any(name.startswith("logs/") for name in names)
    assert not any(name.startswith("voice/") for name in names)
    assert not any(name.startswith("backups/") for name in names)
    assert not any(name.startswith("server-browser/") for name in names)
    assert "orphan.db-wal" not in names
    assert "leftover.tmp" not in names
    with_backups = {
        rel
        for _, rel in iter_data_dir_files(
            snapshot_env.data_dir, include_code_backups=True
        )
    }
    assert "backups/marker.txt" in with_backups


def test_create_verify_restore_roundtrip(snapshot_env, tmp_path, monkeypatch):
    original_key = Fernet(os.environ["NYMERIA_SECRETS_KEY"].encode())

    result = _create(snapshot_env)
    artifact = result.artifact
    assert artifact.exists()
    assert artifact.suffix == ".nysnap"
    assert result.manifest["includes_key"] is True
    assert result.manifest["counts"]["users"] == 1
    assert result.manifest["counts"]["credentials"] == 1

    # meta is readable without streaming the whole artifact.
    meta = read_snapshot_meta(artifact, PASSPHRASE)
    assert meta["kind"] == "nymeria-user-data-snapshot"
    assert meta["checkpoint_backend"] == "sqlite"

    # Full verification passes, including the vault canary.
    workdir = tmp_path / "verify-work"
    extracted = extract_snapshot(artifact, PASSPHRASE, workdir)
    assert extracted.hash_failures == []
    checks = verify_extracted(extracted)
    assert not has_failures(checks)
    canary = next(c for c in checks if c.name == "Vault canary")
    assert canary.status == "pass"

    # Restore onto a fresh host with no vault key in the environment.
    target_data = tmp_path / "restored-data"
    target_data.mkdir()
    (target_data / "stale.txt").write_text("old install", encoding="utf-8")
    target_settings = FakeSettings(data_dir=target_data)
    restore_workspace = tmp_path / "restored-workspace"
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(restore_workspace))
    key_value = os.environ.pop("NYMERIA_SECRETS_KEY")

    staging = target_data / ".snapshot-restore-test"
    extracted2 = extract_snapshot(artifact, PASSPHRASE, staging)
    report = restore_snapshot(target_settings, extracted2)

    assert report.key_status == "env-missing"
    assert report.key_value == key_value
    # Old contents preserved, new contents in place.
    assert report.data_moved_aside is not None
    assert (report.data_moved_aside / "stale.txt").read_text() == "old install"
    assert (target_data / "todos" / "alice.json").exists()
    assert (target_data / "system_prompt.md").read_text() == "custom soul"
    ok, detail = sqlite_integrity_ok(target_data / "accounts.db")
    assert ok, detail
    # The restored vault decrypts with the artifact key.
    with closing(sqlite3.connect(target_data / "accounts.db")) as conn:
        row = conn.execute(
            "SELECT ciphertext FROM credential_secret_fields LIMIT 1"
        ).fetchone()
    assert original_key.decrypt(row[0].encode()).decode() == "sk-canary-secret"
    # Workspace durable subtrees restored; scratch was never captured.
    assert (restore_workspace / "threads" / "t1" / "attachments" / "a.txt").exists()
    assert (restore_workspace / "images" / "gen.png").exists()
    assert not (restore_workspace / "scratch").exists()
    # Staging dir is gone.
    assert not staging.exists()


def test_restore_refuses_backend_mismatch(snapshot_env, tmp_path):
    result = _create(snapshot_env)
    staging = tmp_path / "staging"
    extracted = extract_snapshot(result.artifact, PASSPHRASE, staging)
    target = FakeSettings(data_dir=tmp_path / "t", database_backend="postgres")
    target.data_dir.mkdir()
    with pytest.raises(SnapshotError, match="checkpoint_backend"):
        restore_snapshot(target, extracted)


def test_write_env_merges_key(snapshot_env, tmp_path, monkeypatch):
    result = _create(snapshot_env)
    key_value = os.environ["NYMERIA_SECRETS_KEY"]
    monkeypatch.delenv("NYMERIA_SECRETS_KEY")
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path / "ws2"))

    target_data = tmp_path / "restored"
    target_data.mkdir()
    env_file = tmp_path / "restore.env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    staging = target_data / ".snapshot-restore-x"
    extracted = extract_snapshot(result.artifact, PASSPHRASE, staging)
    report = restore_snapshot(
        FakeSettings(data_dir=target_data), extracted, write_env=env_file
    )
    assert report.key_value is None  # written, not printed
    content = env_file.read_text(encoding="utf-8")
    assert "OTHER=1" in content
    assert f"NYMERIA_SECRETS_KEY={key_value}" in content


def test_plain_snapshot_never_embeds_key(snapshot_env, tmp_path):
    result = create_snapshot(snapshot_env, encrypt=False)
    assert result.artifact.name.endswith(".tar.gz")
    assert result.manifest["includes_key"] is False
    assert any("NOT encrypted" in w for w in result.warnings)
    extracted = extract_snapshot(result.artifact, None, tmp_path / "plainwork")
    assert not (extracted.root / "secrets" / "fernet.key").exists()
    key_check = next(
        c for c in verify_extracted(extracted) if c.name == "Vault key"
    )
    assert key_check.status == "warn"  # vault has secrets, no key embedded


def test_create_requires_passphrase_when_encrypting(snapshot_env):
    with pytest.raises(SnapshotError, match="passphrase"):
        create_snapshot(snapshot_env, encrypt=True, passphrase=None)


def test_verify_detects_tampered_member(snapshot_env, tmp_path):
    result = create_snapshot(snapshot_env, encrypt=False)
    workdir = tmp_path / "tamperwork"
    extracted = extract_snapshot(result.artifact, None, workdir)
    # Corrupt an extracted JSON store, then re-check hashes via a fresh
    # manifest comparison: simulate by editing the file and re-verifying the
    # SQLite/JSON layer (hash layer is covered in the crypto tests).
    (workdir / "data" / "todos" / "alice.json").write_text("{broken", "utf-8")
    checks = verify_extracted(extracted)
    assert has_failures(checks)
    json_check = next(c for c in checks if c.name == "JSON stores")
    assert json_check.status == "fail"


def test_capture_with_live_writer_stays_consistent(snapshot_env):
    """The backup API must produce an integrity-clean copy under writes."""
    db = snapshot_env.data_dir / "todo_schedule.db"
    stop = threading.Event()

    def hammer():
        with closing(sqlite3.connect(db, timeout=30.0)) as conn:
            i = 0
            while not stop.is_set():
                conn.execute("INSERT INTO items (body) VALUES (?)", (f"live-{i}",))
                conn.commit()
                i += 1
                time.sleep(0.001)

    writer = threading.Thread(target=hammer, daemon=True)
    writer.start()
    try:
        copies = snapshot_env.data_dir.parent / "copies"
        for round_no in range(3):
            target = copies / f"copy-{round_no}.db"
            sqlite_consistent_copy(db, target)
            ok, detail = sqlite_integrity_ok(target)
            assert ok, detail
    finally:
        stop.set()
        writer.join(timeout=10)


def test_liveness_signs_from_heartbeat(snapshot_env, tmp_path, monkeypatch):
    assert stack_liveness_signs(snapshot_env) == []
    health_dir = tmp_path / "health"
    health_dir.mkdir(exist_ok=True)
    (health_dir / "worker.json").write_text('{"status": "ok"}', encoding="utf-8")
    signs = stack_liveness_signs(snapshot_env)
    assert any("worker heartbeat" in sign for sign in signs)


def test_restore_refuses_digest_failures(snapshot_env, tmp_path):
    """restore_snapshot must never swap in content that failed its digests."""
    result = _create(snapshot_env)
    staging = tmp_path / "digest-staging"
    extracted = extract_snapshot(result.artifact, PASSPHRASE, staging)
    poisoned = ExtractedSnapshot(
        root=extracted.root,
        meta=extracted.meta,
        manifest=extracted.manifest,
        hash_failures=["sha256 mismatch: data/accounts.db"],
    )
    with pytest.raises(SnapshotError, match="digest failures"):
        restore_snapshot(FakeSettings(data_dir=snapshot_env.data_dir), poisoned)


def test_extract_detects_tampered_member(snapshot_env, tmp_path):
    """A same-size content change must surface as a sha256 hash failure."""
    import gzip
    import io
    import tarfile

    result = create_snapshot(snapshot_env, encrypt=False)
    raw_tar = gzip.decompress(result.artifact.read_bytes())

    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as src, tarfile.open(
        fileobj=out, mode="w:", format=tarfile.PAX_FORMAT
    ) as dst:
        for member in src:
            fileobj = src.extractfile(member) if member.isfile() else None
            payload = fileobj.read() if fileobj is not None else b""
            if member.name == "data/system_prompt.md":
                payload = b"tamper soul"  # same length as "custom soul"
            dst.addfile(member, io.BytesIO(payload))
    tampered = tmp_path / "tampered.tar.gz"
    tampered.write_bytes(gzip.compress(out.getvalue()))

    extracted = extract_snapshot(tampered, None, tmp_path / "tamper-work")
    assert any("system_prompt.md" in failure for failure in extracted.hash_failures)
    with pytest.raises(SnapshotError, match="digest failures"):
        restore_snapshot(FakeSettings(data_dir=tmp_path / "unused"), extracted)


def test_truncated_encrypted_artifact_fails_extraction(snapshot_env, tmp_path):
    """Dropping the trailing frames must fail extraction, not extract clean."""
    from nymeria.core.snapshot_crypto import SnapshotDecryptError

    result = _create(snapshot_env)
    blob = result.artifact.read_bytes()
    truncated = tmp_path / "truncated.nysnap"
    truncated.write_bytes(blob[: len(blob) - 21])  # exactly the final frame
    with pytest.raises(SnapshotDecryptError):
        extract_snapshot(truncated, PASSPHRASE, tmp_path / "trunc-work")


def test_move_into_falls_back_to_copy_across_filesystems(tmp_path, monkeypatch):
    """_move_into must survive EXDEV-style rename failures (Docker volumes)."""
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "file.txt").write_text("payload", encoding="utf-8")
    dst = tmp_path / "dst"

    real_rename = Path.rename

    def exdev_rename(self, target):
        if self == src:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", exdev_rename)
    _move_into(src, dst)
    assert (dst / "nested" / "file.txt").read_text() == "payload"
    assert not src.exists()


def test_vanished_sqlite_is_skipped_not_captured_empty(snapshot_env, monkeypatch):
    """A DB deleted mid-walk must be warned-skipped, never captured empty."""
    from nymeria.core import snapshot as snapshot_module

    victim = snapshot_env.data_dir / "todo_schedule.db"
    real_copy = snapshot_module.sqlite_consistent_copy

    def vanishing_copy(source, target):
        if source == victim:
            victim.unlink(missing_ok=True)
        return real_copy(source, target)

    monkeypatch.setattr(snapshot_module, "sqlite_consistent_copy", vanishing_copy)
    result = _create(snapshot_env)
    assert any("todo_schedule.db" in w for w in result.warnings)
    assert "data/todo_schedule.db" not in result.manifest["files"]
    # The capture path must not have resurrected an empty database.
    assert not victim.exists()


def test_sqlite_consistent_copy_missing_source_raises(tmp_path):
    from nymeria.core.snapshot_stores import (
        SnapshotStoreError as StoreError,
        sqlite_consistent_copy as copy_fn,
    )

    missing = tmp_path / "missing.db"
    with pytest.raises(StoreError, match="vanished|failed"):
        copy_fn(missing, tmp_path / "out.db")
    assert not missing.exists()  # connect must not create the source


# ---------------------------------------------------------------------------
# Postgres dump/restore layer (fake psycopg connection)
# ---------------------------------------------------------------------------


class FakeCopy:
    def __init__(self, rows=None, sink=None):
        self._rows = rows or []
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._rows)

    def write(self, chunk):
        assert self._sink is not None
        self._sink.append(bytes(chunk))


@dataclass
class FakeCursor:
    tables: dict
    migration_version: int = 4
    loaded: dict = field(default_factory=dict)
    executed: list = field(default_factory=list)
    _last: object = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.startswith("SELECT to_regclass") and params:
            self._last = (params[0],) if params[0] in self.tables else (None,)
        elif "max(v)" in sql:
            self._last = (self.migration_version,)
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def copy(self, sql):
        for table in self.tables:
            if f" {table} " in sql:
                if "TO STDOUT" in sql:
                    return FakeCopy(rows=self.tables[table])
                sink = self.loaded.setdefault(table, [])
                return FakeCopy(sink=sink)
        raise AssertionError(f"unexpected COPY: {sql}")


@dataclass
class FakeConn:
    cursor_obj: FakeCursor
    committed: bool = False
    rolled_back: bool = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _fake_tables(rows_per_table=2):
    return {
        table: [f"{table}-row-{i}\n".encode() for i in range(rows_per_table)]
        for table in (
            "checkpoint_migrations",
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
        )
    }


def test_postgres_dump_and_restore_roundtrip(tmp_path):
    tables = _fake_tables()
    dump_cursor = FakeCursor(tables=tables)
    summary = dump_postgres_checkpoints(
        "postgresql://fake",
        tmp_path / "pg",
        connect=lambda uri: FakeConn(cursor_obj=dump_cursor),
    )
    assert summary["migration_version"] == 4
    assert summary["row_counts"]["checkpoints"] == 2
    assert (tmp_path / "pg" / "checkpoints.copy").read_bytes() == b"".join(
        tables["checkpoints"]
    )
    assert any("REPEATABLE READ" in sql for sql in dump_cursor.executed)

    restore_cursor = FakeCursor(tables=tables)
    conn = FakeConn(cursor_obj=restore_cursor)
    rows = restore_postgres_checkpoints(
        "postgresql://fake",
        tmp_path / "pg",
        expected_migration_version=4,
        connect=lambda uri: conn,
    )
    assert rows["checkpoints"] == 2
    assert conn.committed
    assert any(sql.startswith("TRUNCATE") for sql in restore_cursor.executed)
    assert b"".join(restore_cursor.loaded["checkpoints"]) == b"".join(
        tables["checkpoints"]
    )


def test_postgres_restore_refuses_version_mismatch(tmp_path):
    tables = _fake_tables()
    dump_postgres_checkpoints(
        "postgresql://fake",
        tmp_path / "pg",
        connect=lambda uri: FakeConn(cursor_obj=FakeCursor(tables=tables)),
    )
    mismatched = FakeCursor(tables=tables, migration_version=9)
    with pytest.raises(SnapshotStoreError, match="migration"):
        restore_postgres_checkpoints(
            "postgresql://fake",
            tmp_path / "pg",
            expected_migration_version=4,
            connect=lambda uri: FakeConn(cursor_obj=mismatched),
        )


def test_postgres_dump_refuses_missing_table(tmp_path):
    tables = _fake_tables()
    tables.pop("checkpoint_blobs")
    with pytest.raises(SnapshotStoreError, match="checkpoint_blobs"):
        dump_postgres_checkpoints(
            "postgresql://fake",
            tmp_path / "pg",
            connect=lambda uri: FakeConn(cursor_obj=FakeCursor(tables=tables)),
        )


def test_snapshot_never_carries_the_server_browsers_token_or_sessions(snapshot_env, tmp_path):
    """The rig holds a live account token and the browser's logged-in site
    sessions; a snapshot is a portable artifact, so a restore must not lay
    either back down on the target host."""
    result = _create(snapshot_env)
    extracted = extract_snapshot(result.artifact, PASSPHRASE, tmp_path / "work")
    laid_down = [p for p in extracted.root.rglob("*") if "server-browser" in p.parts]
    assert laid_down == []
    assert not any(
        b"nym_secret" in path.read_bytes()
        for path in extracted.root.rglob("*")
        if path.is_file()
    )


def test_snapshot_exclusion_matches_the_rigs_default_home(monkeypatch):
    """Pins the excluded name to the launcher's own default: move the rig home
    without moving this and every snapshot silently starts carrying a token.

    The env guard is load-bearing: `resolve_rig_home` reads SERVER_BROWSER_HOME
    first, and a dogfood host exports it, so without this the test would assert
    against that host's path instead of the default."""
    from nymeria import server_browser as sb
    from nymeria.core.snapshot_stores import DEFAULT_EXCLUDED_TOP_LEVEL

    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    root = Path("/srv/nymeria")
    home = sb.resolve_rig_home(root)
    assert home.path.parent.name == "data"
    assert home.path.name in DEFAULT_EXCLUDED_TOP_LEVEL


def test_a_moved_rig_is_excluded_by_path_not_only_by_name(tmp_path, monkeypatch):
    """`SERVER_BROWSER_HOME` and `nymeria browser configure --home` can put the
    rig anywhere, including inside the data dir under another name, and a
    name-only exclusion would quietly resume capturing a live account token and
    every site session the browser is signed into."""
    from nymeria.core.snapshot_stores import iter_data_dir_files

    data_dir = tmp_path / "data"
    rig = data_dir / "browser-rig"
    (rig / "ext").mkdir(parents=True)
    (rig / "ext" / "config.json").write_text('{"token": "nym_secret"}', encoding="utf-8")
    (rig / "profile" / "Default").mkdir(parents=True)
    (rig / "profile" / "Default" / "Cookies").write_bytes(b"session")
    (data_dir / "todos").mkdir()
    (data_dir / "todos" / "alice.json").write_text("{}", encoding="utf-8")

    monkeypatch.delenv("SERVER_BROWSER_HOME", raising=False)
    before = {rel for _, rel in iter_data_dir_files(data_dir)}
    assert "browser-rig/ext/config.json" in before  # nothing says it is a rig yet

    monkeypatch.setenv("SERVER_BROWSER_HOME", str(rig))
    after = {rel for _, rel in iter_data_dir_files(data_dir)}
    assert not any(name.startswith("browser-rig/") for name in after)
    assert "todos/alice.json" in after


def test_a_rig_outside_the_data_dir_is_simply_out_of_reach(tmp_path, monkeypatch):
    """Not an exclusion so much as a fact worth pinning: the walker never
    leaves the data dir, so a relocated rig needs no special handling."""
    from nymeria.core.snapshot_stores import server_browser_rig_path

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SERVER_BROWSER_HOME", str(tmp_path / "elsewhere"))
    assert server_browser_rig_path(data_dir) is None


@pytest.fixture()
def settings_root(tmp_path, monkeypatch):
    """Point `get_settings()` at a throwaway project root.

    `server_browser_rig_path` resolves the rig through the launcher, which
    reads the root's env files and the `configure --home` pointer, so a test
    about either needs a root it owns. The cache is cleared on the way out too:
    a Settings built against tmp_path must not leak into the next test.
    """
    from nymeria.config import settings as settings_module

    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)
    settings_module.get_settings.cache_clear()
    yield tmp_path
    settings_module.get_settings.cache_clear()


def test_a_rig_named_only_by_the_configure_pointer_is_excluded(settings_root, monkeypatch):
    """`nymeria browser configure --home` writes no env key: it records the rig
    in a pointer file instead. A snapshot that resolved the home itself (env
    key, then setting) therefore looked straight past a hand-configured rig and
    captured the live token and site sessions it exists to keep out. Resolving
    through the launcher's own chain is what closes it, and is why this must
    never go back to reading the env key here.
    """
    from nymeria import server_browser as sb
    from nymeria.core.snapshot_stores import iter_data_dir_files

    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    data_dir = settings_root / "data"
    rig = data_dir / "hand-configured"
    (rig / "ext").mkdir(parents=True)
    (rig / "ext" / "config.json").write_text('{"token": "nym_secret"}', encoding="utf-8")
    (rig / "profile" / "Default").mkdir(parents=True)
    (rig / "profile" / "Default" / "Cookies").write_bytes(b"session")
    (data_dir / "todos").mkdir()
    (data_dir / "todos" / "alice.json").write_text("{}", encoding="utf-8")

    before = {rel for _, rel in iter_data_dir_files(data_dir)}
    assert "hand-configured/ext/config.json" in before  # nothing names it a rig yet

    sb.write_rig_home_pointer(settings_root, sb.RigHome(rig), log=lambda _m: None)
    after = {rel for _, rel in iter_data_dir_files(data_dir)}
    assert not any(name.startswith("hand-configured/") for name in after)
    assert "todos/alice.json" in after


def test_the_rig_home_pointer_is_host_local_and_never_travels(settings_root, monkeypatch):
    """The pointer holds no secret, but it is an absolute path that is true
    only on the host that wrote it. Captured, a restore elsewhere would aim the
    install's rig at a directory that does not exist there."""
    from nymeria import server_browser as sb
    from nymeria.core.snapshot_stores import SERVER_BROWSER_POINTER, iter_data_dir_files

    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    data_dir = settings_root / "data"
    data_dir.mkdir()
    # Pins the excluded name to the launcher's own: move one without the other
    # and the pointer starts travelling again.
    assert sb.rig_home_pointer(settings_root).name == SERVER_BROWSER_POINTER

    sb.write_rig_home_pointer(
        settings_root, sb.RigHome(settings_root / "elsewhere"), log=lambda _m: None
    )
    assert sb.rig_home_pointer(settings_root).exists()
    assert SERVER_BROWSER_POINTER not in {rel for _, rel in iter_data_dir_files(data_dir)}


def test_a_restore_leaves_the_live_rig_where_it_is(tmp_path, monkeypatch):
    """The rig is never captured, so the artifact has nothing to put in its
    place: sweeping it into .pre-restore would relocate a RUNNING Chrome's
    user-data-dir, and Chrome would silently recreate an empty profile with
    every site login gone while config.env still claimed a working browser."""
    from nymeria.core.snapshot_stores import SERVER_BROWSER_DIR, server_browser_rig_path

    data_dir = tmp_path / "data"
    (data_dir / SERVER_BROWSER_DIR / "profile").mkdir(parents=True)
    (data_dir / "todos").mkdir()
    monkeypatch.delenv("SERVER_BROWSER_HOME", raising=False)

    # The rule the restore loop applies, exercised directly: the default home is
    # kept by name, and a moved one by resolved path.
    kept_by_name = SERVER_BROWSER_DIR
    assert (data_dir / kept_by_name).name == kept_by_name
    monkeypatch.setenv("SERVER_BROWSER_HOME", str(data_dir / SERVER_BROWSER_DIR))
    assert server_browser_rig_path(data_dir) == (data_dir / SERVER_BROWSER_DIR).resolve()


def test_restore_keeps_the_rig_and_still_swaps_everything_else(snapshot_env, tmp_path):
    """End to end through the real restore: an in-place DR restore must not
    move the live rig aside."""
    result = _create(snapshot_env)
    extracted = extract_snapshot(result.artifact, PASSPHRASE, tmp_path / "work")

    data_dir = snapshot_env.data_dir
    rig = data_dir / "server-browser"
    marker = rig / "profile" / "Default" / "Cookies"
    # The pointer is excluded from capture, so sweeping it aside would delete
    # the only record of a hand-configured rig's home: the next `nymeria init`
    # would resolve the default, find nothing, and mint a SECOND rig.
    pointer = data_dir / "server-browser-home"
    pointer.write_text(f"{rig}\n", encoding="utf-8")
    (data_dir / "todos" / "bob.json").write_text("{}", encoding="utf-8")

    restore_snapshot(snapshot_env, extracted)

    assert marker.exists(), "the live rig was swept into .pre-restore"
    assert pointer.exists(), "the rig home pointer was swept into .pre-restore"
    # The rest of the data dir really was swapped: a file created after the
    # capture is gone from its place.
    assert not (data_dir / "todos" / "bob.json").exists()
