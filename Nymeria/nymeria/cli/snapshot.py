"""
User-data snapshot CLI, ``python run.py snapshot <action> ...``.

Operator-facing disaster recovery: create/verify/list run against a LIVE
stack (online, per-store-consistent capture); restore is offline-only and
refuses while the stack looks alive. No running API is required by any
action; like the ``users`` CLI this works directly on local state, which is
exactly what a recovery scenario needs.

Docker shape: run create/verify/list inside the api container
(``docker exec nymeria-api python run.py snapshot create``); run restore in a
one-off container while api/worker are stopped and postgres is up
(``docker compose run --rm api python run.py snapshot restore ...``).
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from nymeria.config import get_settings
from nymeria.core.snapshot import (
    ExtractedSnapshot,
    SnapshotError,
    create_snapshot,
    extract_snapshot,
    read_snapshot_meta,
    restore_snapshot,
    stack_liveness_signs,
)
from nymeria.core.snapshot_crypto import SnapshotCryptoError, sniff_artifact
from nymeria.core.snapshot_stores import SnapshotStoreError
from nymeria.core.snapshot_verify import SnapshotCheck, has_failures, verify_extracted

PASSPHRASE_ENV_VAR = "NYMERIA_SNAPSHOT_PASSPHRASE"
_MIN_PASSPHRASE_CHARS = 8

_STATUS_LABEL = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}


def _print_checks(checks: List[SnapshotCheck]) -> None:
    for check in checks:
        print(f"  {_STATUS_LABEL[check.status]} {check.name}: {check.detail}")


def _check_new_passphrase(passphrase: str) -> None:
    """Strength floor for a passphrase protecting a NEW artifact.

    Applies to every source (file, env, prompt): the artifact embeds the
    vault key, so a fat-fingered one-character cron passphrase must fail
    loudly instead of shipping a trivially brute-forceable backup.
    """
    if len(passphrase) < _MIN_PASSPHRASE_CHARS:
        print(
            f"[error] Passphrase must be at least {_MIN_PASSPHRASE_CHARS} "
            "characters (the artifact embeds the vault key).",
            file=sys.stderr,
        )
        sys.exit(2)


def _resolve_passphrase(
    args: argparse.Namespace,
    *,
    confirm: bool,
    required: bool,
) -> Optional[str]:
    """Passphrase resolution order: --passphrase-file, env, interactive.

    ``confirm=True`` marks creation of a new artifact: the strength floor is
    enforced on every source and an interactive prompt is confirmed twice.
    """
    passphrase_file = getattr(args, "passphrase_file", None)
    if passphrase_file:
        try:
            passphrase = Path(passphrase_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            if not required:
                print(f"[warn] Ignoring unreadable passphrase file: {exc}")
                return None
            print(f"[error] Cannot read passphrase file: {exc}", file=sys.stderr)
            sys.exit(2)
        if not passphrase:
            print("[error] Passphrase file is empty", file=sys.stderr)
            sys.exit(2)
        if confirm:
            _check_new_passphrase(passphrase)
        return passphrase
    env_value = os.environ.get(PASSPHRASE_ENV_VAR, "").strip()
    if env_value:
        if confirm:
            _check_new_passphrase(env_value)
        return env_value
    if not required:
        return None
    if not sys.stdin.isatty():
        print(
            f"[error] No passphrase available. Provide --passphrase-file or set "
            f"{PASSPHRASE_ENV_VAR} when running non-interactively.",
            file=sys.stderr,
        )
        sys.exit(2)
    passphrase = getpass.getpass("Snapshot passphrase: ")
    if confirm:
        _check_new_passphrase(passphrase)
        if getpass.getpass("Confirm passphrase: ") != passphrase:
            print("[error] Passphrases do not match.", file=sys.stderr)
            sys.exit(2)
    elif not passphrase:
        print("[error] Empty passphrase.", file=sys.stderr)
        sys.exit(2)
    return passphrase


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(size)} B"


def _artifact_needs_passphrase(artifact: Path) -> bool:
    return sniff_artifact(artifact) == "encrypted"


def _extract_for_inspection(
    artifact: Path, passphrase: Optional[str], workdir: Path
) -> ExtractedSnapshot:
    try:
        return extract_snapshot(artifact, passphrase, workdir)
    except (SnapshotError, SnapshotCryptoError, SnapshotStoreError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        shutil.rmtree(workdir, ignore_errors=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_create(args: argparse.Namespace) -> int:
    settings = get_settings()
    encrypt = not args.no_encrypt
    passphrase = (
        _resolve_passphrase(args, confirm=True, required=True) if encrypt else None
    )
    try:
        result = create_snapshot(
            settings,
            output=Path(args.output) if args.output else None,
            passphrase=passphrase,
            encrypt=encrypt,
            include_workspace=not args.no_workspace,
            include_code_backups=args.include_code_backups,
        )
    except (SnapshotError, SnapshotStoreError, SnapshotCryptoError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    size = result.artifact.stat().st_size
    print(f"Snapshot written: {result.artifact} ({_human_size(size)})")
    for warning in result.warnings:
        print(f"  [warn] {warning}")

    if args.no_verify:
        print("Verification skipped (--no-verify).")
        return 0

    print("Verifying artifact...")
    workdir = Path(tempfile.mkdtemp(prefix="nymeria-snapshot-verify-"))
    try:
        extracted = _extract_for_inspection(result.artifact, passphrase, workdir)
        checks = verify_extracted(extracted)
        _print_checks(checks)
        if has_failures(checks):
            print(
                "[error] Snapshot verification FAILED; do not rely on this "
                "artifact.",
                file=sys.stderr,
            )
            return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("Snapshot verified.")
    print(
        "Ship the artifact off this host (scp/rclone) and store the "
        "passphrase separately."
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact).expanduser()
    if not artifact.is_file():
        print(f"[error] No such artifact: {artifact}", file=sys.stderr)
        return 2
    passphrase = None
    if _artifact_needs_passphrase(artifact):
        passphrase = _resolve_passphrase(args, confirm=False, required=True)
    workdir = Path(tempfile.mkdtemp(prefix="nymeria-snapshot-verify-"))
    try:
        extracted = _extract_for_inspection(artifact, passphrase, workdir)
        meta = extracted.meta
        print(
            f"Snapshot {artifact.name}: created {meta.get('created_at')}, "
            f"backend {meta.get('checkpoint_backend')}, "
            f"nymeria {meta.get('nymeria_version')}"
        )
        checks = verify_extracted(extracted)
        _print_checks(checks)
        if has_failures(checks):
            print("[error] Verification FAILED.", file=sys.stderr)
            return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("Verification passed.")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    artifact = Path(args.artifact).expanduser()
    if not artifact.is_file():
        print(f"[error] No such artifact: {artifact}", file=sys.stderr)
        return 2

    signs = stack_liveness_signs(settings)
    if signs and not args.force:
        print(
            "[error] The stack appears to be running; stop it before restoring "
            "(or pass --force if these signals are stale):",
            file=sys.stderr,
        )
        for sign in signs:
            print(f"  - {sign}", file=sys.stderr)
        return 1

    passphrase = None
    if _artifact_needs_passphrase(artifact):
        passphrase = _resolve_passphrase(args, confirm=False, required=True)

    data_dir = Path(settings.data_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = data_dir / f".snapshot-restore-{stamp}"
    extracted = _extract_for_inspection(artifact, passphrase, staging)

    print("Pre-restore verification:")
    checks = verify_extracted(extracted)
    _print_checks(checks)
    if has_failures(checks):
        shutil.rmtree(staging, ignore_errors=True)
        print(
            "[error] Verification FAILED; refusing to restore from this "
            "artifact.",
            file=sys.stderr,
        )
        return 1

    meta = extracted.meta
    counts = extracted.manifest.get("counts") or {}
    print()
    print("About to restore:")
    print(f"  Artifact:    {artifact}")
    print(f"  Created:     {meta.get('created_at')} on {meta.get('hostname')}")
    print(f"  Backend:     {meta.get('checkpoint_backend')}")
    print(f"  Counts:      {counts}")
    print(f"  Target dir:  {data_dir}")
    print("  The current data dir contents will be moved aside, not deleted.")
    if not args.yes:
        if not sys.stdin.isatty():
            shutil.rmtree(staging, ignore_errors=True)
            print(
                "[error] Confirmation required; pass --yes when running "
                "non-interactively.",
                file=sys.stderr,
            )
            return 2
        try:
            answer = input("Type 'restore' to continue: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
            print()
        if answer != "restore":
            shutil.rmtree(staging, ignore_errors=True)
            print("Aborted.")
            return 1

    try:
        report = restore_snapshot(
            settings,
            extracted,
            write_env=Path(args.write_env) if args.write_env else None,
        )
    except (SnapshotError, SnapshotStoreError) as exc:
        print(f"[error] Restore failed: {exc}", file=sys.stderr)
        print(
            f"  The staging dir was left at {staging} for inspection.",
            file=sys.stderr,
        )
        return 1

    print()
    print("Restore complete.")
    if report.data_moved_aside:
        print(f"  Previous data preserved at: {report.data_moved_aside}")
    if report.postgres_rows is not None:
        print(f"  Postgres checkpoint rows restored: {report.postgres_rows}")
    for warning in report.warnings:
        print(f"  [warn] {warning}")
    _print_key_guidance(report.key_status, report.key_value)
    print("  Start the stack and run `nymeria doctor` to confirm health.")
    return 0


def _print_key_guidance(status: str, key_value: Optional[str]) -> None:
    if status == "match":
        print("  Vault key: environment already holds the matching key.")
    elif status == "absent":
        print(
            "  Vault key: none embedded in this snapshot. If the vault holds "
            "secrets, set the original NYMERIA_SECRETS_KEY before starting."
        )
    elif status == "env-missing" and key_value:
        print("  Vault key: NOT set in this environment. Add to your env file:")
        print(f"    NYMERIA_SECRETS_KEY={key_value}")
        print("  (or re-run restore with --write-env <path> to write it)")
    elif status == "mismatch" and key_value:
        print(
            "  [warn] Vault key MISMATCH: the environment key differs from the "
            "snapshot's. The restored vault needs the snapshot's key:"
        )
        print(f"    NYMERIA_SECRETS_KEY={key_value}")
        print("  Update your env file (or re-run with --write-env <path>).")


def _cmd_list(args: argparse.Namespace) -> int:
    settings = get_settings()
    directory = Path(args.dir).expanduser() if args.dir else settings.snapshots_dir
    if not directory.is_dir():
        print(f"(no snapshots directory at {directory})")
        return 0
    artifacts = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and sniff_artifact(path) is not None
        ),
        key=lambda p: p.name,
    )
    if not artifacts:
        print(f"(no snapshot artifacts in {directory})")
        return 0
    passphrase = _resolve_passphrase(args, confirm=False, required=False)
    for path in artifacts:
        kind = sniff_artifact(path)
        size = _human_size(path.stat().st_size)
        line = f"{path.name}  {size}  {kind}"
        if kind == "plain" or passphrase:
            try:
                meta = read_snapshot_meta(path, passphrase)
                line += (
                    f"  created {meta.get('created_at')}"
                    f"  backend {meta.get('checkpoint_backend')}"
                    f"  key {'yes' if meta.get('includes_key') else 'no'}"
                )
            except (SnapshotError, SnapshotCryptoError):
                line += "  (meta unreadable)"
        print(line)
    if any(sniff_artifact(p) == "encrypted" for p in artifacts) and not passphrase:
        print(
            f"(set {PASSPHRASE_ENV_VAR} or pass --passphrase-file to include "
            "encrypted artifact details)"
        )
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring, called from run.py
# ---------------------------------------------------------------------------


def _add_passphrase_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-file",
        default=None,
        help=(
            "File containing the artifact passphrase (alternatives: the "
            f"{PASSPHRASE_ENV_VAR} env var, or an interactive prompt)"
        ),
    )


def build_parser(subparsers: argparse._SubParsersAction) -> None:
    snapshot = subparsers.add_parser(
        "snapshot",
        help="User-data backup: create/verify/restore/list snapshot artifacts",
    )
    actions = snapshot.add_subparsers(dest="action", required=True)

    p_create = actions.add_parser(
        "create",
        help="Capture an encrypted snapshot of all user data (stack may be running)",
    )
    p_create.add_argument(
        "--output", default=None, help="Artifact path (default: <snapshots_dir>/snapshot-<utc>.nysnap)"
    )
    p_create.add_argument(
        "--no-workspace",
        action="store_true",
        help="Skip workspace attachments and images",
    )
    p_create.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the automatic post-create verification pass",
    )
    p_create.add_argument(
        "--no-encrypt",
        action="store_true",
        help="Write a plain tar.gz WITHOUT the vault key (not recommended)",
    )
    p_create.add_argument(
        "--include-code-backups",
        action="store_true",
        help="Also capture data/backups/ (self-modification source rollbacks)",
    )
    _add_passphrase_arg(p_create)

    p_verify = actions.add_parser(
        "verify", help="Verify an artifact is intact and restorable"
    )
    p_verify.add_argument("artifact")
    _add_passphrase_arg(p_verify)

    p_restore = actions.add_parser(
        "restore",
        help="Restore a snapshot (offline: stop the stack first)",
    )
    p_restore.add_argument("artifact")
    p_restore.add_argument(
        "--write-env",
        default=None,
        help="Env file to merge NYMERIA_SECRETS_KEY into when it is missing or differs",
    )
    p_restore.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if the stack looks live (stale heartbeats)",
    )
    p_restore.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation",
    )
    _add_passphrase_arg(p_restore)

    p_list = actions.add_parser("list", help="List snapshot artifacts")
    p_list.add_argument(
        "--dir", default=None, help="Directory to list (default: snapshots_dir)"
    )
    _add_passphrase_arg(p_list)


def dispatch(args: argparse.Namespace) -> int:
    handlers = {
        "create": _cmd_create,
        "verify": _cmd_verify,
        "restore": _cmd_restore,
        "list": _cmd_list,
    }
    action: Optional[str] = getattr(args, "action", None)
    if action is None:
        print("[error] No snapshot action specified", file=sys.stderr)
        return 2
    handler = handlers.get(action)
    if handler is None:
        print(f"[error] Unknown snapshot action: {action}", file=sys.stderr)
        return 2
    return handler(args)
