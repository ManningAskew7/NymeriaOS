"""Prune orphaned rows from the thread_owners table.

Before commit 7b39bf5b, opening a "New Chat" tab fired read-only GETs that
TOFU-claimed the thread, registering a permanent thread_owners row for every
unused tab. Those ghost owners survive a sidebar/metadata cleanup and can
resurface on a sync. This script removes owner rows that have no backing thread
anywhere, while keeping every owner row that corresponds to a real thread.

An owner row is KEPT when its thread_id appears in any of:
  - LangGraph checkpoints (checkpoints / writes tables in nymeria.db)
  - a thread_metadata/<user>.json "threads" map (the sidebar list)
  - thread_platform_bindings (provider-bound chat threads)
  - a thread_configs/<id>.* file
  - a thread_notes/<id>.* file
Anything else is treated as an orphan and is a deletion candidate.

Deleting an orphan owner row is safe: if the thread is ever used again, the
post-7b39bf5b backend re-establishes ownership on the first write.

Defaults to a dry run. Pass --apply to actually delete. A timestamped backup of
accounts.db is written next to the database before any deletion.

Usage (inside the slim container, where the data volume is mounted):
    python3 scripts/reconcile_thread_owners.py                # dry run
    python3 scripts/reconcile_thread_owners.py --apply        # delete orphans
    python3 scripts/reconcile_thread_owners.py --data-dir /data --apply
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def _classify(thread_id: str) -> str:
    """Bucket a thread_id for the summary report."""
    # These mirror the platform/synthetic thread-id prefixes the runtime mints
    # for bound and spawned threads. Best-effort and cosmetic: this only drives
    # the histogram, not the keep/delete decision, so a newly added prefix just
    # lands in the "named/test" bucket until this list is updated.
    for prefix in (
        "telegram_",
        "discord_",
        "twitch_",
        "slack_",
        "whatsapp_",
        "spawned-",
        "stream-",
        "skill-kit-",
        "reload-",
    ):
        if thread_id.startswith(prefix):
            return f"prefixed:{prefix}"
    # 8-4-4-4-12 UUID shape (abandoned desktop "New Chat" tabs)
    parts = thread_id.split("-")
    if len(parts) == 5 and len(thread_id) == 36:
        return "uuid"
    if len(thread_id) <= 8 and all(c in "0123456789abcdef" for c in thread_id):
        return "short-hex"
    return "named/test"


def _keep_set(data_dir: Path, nym: sqlite3.Connection, acc: sqlite3.Connection) -> set[str]:
    keep: set[str] = set()

    # Checkpoint-backed threads (real conversation history).
    for table in ("checkpoints", "writes"):
        try:
            keep |= {r[0] for r in nym.execute(f"select distinct thread_id from {table}")}
        except sqlite3.OperationalError:
            pass

    # Provider-bound threads.
    try:
        keep |= {r[0] for r in acc.execute("select thread_id from thread_platform_bindings")}
    except sqlite3.OperationalError:
        pass

    # Sidebar metadata (skip *.bak snapshots).
    for path in glob.glob(str(data_dir / "thread_metadata" / "*.json")):
        try:
            doc = json.loads(Path(path).read_text())
            keep |= set(doc.get("threads", {}).keys())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARN: could not read {path}: {exc}", file=sys.stderr)

    # Per-thread config / notes files (filename stem is the thread id).
    for sub in ("thread_configs", "thread_notes"):
        d = data_dir / sub
        if d.is_dir():
            keep |= {Path(name).stem for name in os.listdir(d)}

    return keep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="/data", help="Nymeria data dir (default: /data)")
    parser.add_argument("--apply", action="store_true", help="delete orphans (default: dry run)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    accounts_db = data_dir / "accounts.db"
    nymeria_db = data_dir / "nymeria.db"
    if not accounts_db.exists():
        print(f"ERROR: {accounts_db} not found", file=sys.stderr)
        return 1

    # Close both connections deterministically: `acc` is a writer (commit
    # below), so a clean close after commit matters more than for a read-only
    # script. try/finally covers every early return inside the block.
    acc = sqlite3.connect(accounts_db)
    nym = sqlite3.connect(nymeria_db) if nymeria_db.exists() else sqlite3.connect(":memory:")
    try:
        owners = [r[0] for r in acc.execute("select thread_id from thread_owners")]
        keep = _keep_set(data_dir, nym, acc)
        orphans = sorted(t for t in owners if t not in keep)

        print(f"Data dir:        {data_dir}")
        print(f"Owner rows:      {len(owners)}")
        print(f"Real threads:    {len(keep)}  {sorted(keep)}")
        print(f"Orphan rows:     {len(orphans)}")
        print("Orphan buckets:")
        for bucket, count in sorted(Counter(_classify(t) for t in orphans).items()):
            print(f"  {bucket:24} {count}")

        if not orphans:
            print("\nNothing to prune.")
            return 0

        if not args.apply:
            print("\nDRY RUN. Re-run with --apply to delete the orphan rows above.")
            print("First 30 orphan thread_ids:")
            for t in orphans[:30]:
                print(f"  {t}")
            return 0

        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = accounts_db.with_name(f"accounts.db.bak-{stamp}")
        shutil.copy2(accounts_db, backup)
        print(f"\nBackup written: {backup}")

        acc.executemany("delete from thread_owners where thread_id = ?", [(t,) for t in orphans])
        acc.commit()
        remaining = acc.execute("select count(*) from thread_owners").fetchone()[0]
        print(f"Deleted {len(orphans)} orphan rows. thread_owners now has {remaining} rows.")
        return 0
    finally:
        nym.close()
        acc.close()


if __name__ == "__main__":
    raise SystemExit(main())
