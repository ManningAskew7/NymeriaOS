"""Direct tests for the append-only JSONL activity log.

Covers the F3 rewrite (slice 08): O(1) append, lazy legacy-JSON migration,
periodic compaction (retention + hard cap), torn-line tolerance, deletion, and
cross-instance consistency over a shared data_dir.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria import config as config_module
from nymeria.core import activity_log as activity_module
from nymeria.core.activity_log import ActivityEntry, ActivityLog, ActivityType
from nymeria.core.time_utils import utc_now


@pytest.fixture(autouse=True)
def _reset_activity_globals():
    """Module-global append counters/singleton must not leak across tests."""
    activity_module._append_counters.clear()
    activity_module._activity_log = None
    yield
    activity_module._append_counters.clear()
    activity_module._activity_log = None


def _patch_retention(monkeypatch, hours: int):
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(activity_retention_hours=hours),
    )


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Append + read
# --------------------------------------------------------------------------- #


def test_log_appends_jsonl_and_returns_newest_first(tmp_path: Path):
    log = ActivityLog(tmp_path)
    log.log(ActivityType.USER_MESSAGE, "first", user_id="u")
    log.log(ActivityType.TODO_ADDED, "second", user_id="u")
    log.log(ActivityType.TASK_COMPLETED, "third", user_id="u")

    jsonl = tmp_path / "activity" / "u.jsonl"
    lines = _read_lines(jsonl)
    assert len(lines) == 3  # one line per event, oldest first
    assert [json.loads(line)["message"] for line in lines] == ["first", "second", "third"]

    entries = log.get_entries("u")
    assert [e.message for e in entries] == ["third", "second", "first"]


def test_get_entries_filters_and_limits(tmp_path: Path):
    log = ActivityLog(tmp_path)
    log.log(ActivityType.USER_MESSAGE, "m-a", user_id="u", thread_id="t1", metadata={"k": 1})
    log.log(ActivityType.TODO_ADDED, "m-b", user_id="u", thread_id="t2")
    log.log(ActivityType.USER_MESSAGE, "m-c", user_id="u", thread_id="t1")

    by_type = log.get_entries("u", activity_type=ActivityType.USER_MESSAGE)
    assert [e.message for e in by_type] == ["m-c", "m-a"]

    by_thread = log.get_entries("u", thread_id="t2")
    assert [e.message for e in by_thread] == ["m-b"]

    limited = log.get_entries("u", limit=1)
    assert [e.message for e in limited] == ["m-c"]

    # Metadata round-trips through model_dump(mode="json") -> model_validate.
    assert by_type[-1].metadata == {"k": 1}


def test_get_entries_since_filter(tmp_path: Path):
    log = ActivityLog(tmp_path)
    old = ActivityEntry(
        type=ActivityType.USER_MESSAGE,
        message="old",
        user_id="u",
        timestamp=utc_now() - timedelta(hours=5),
    )
    new = ActivityEntry(type=ActivityType.USER_MESSAGE, message="new", user_id="u")
    log._save_entries("u", [old, new])

    recent = log.get_entries("u", since=utc_now() - timedelta(hours=1))
    assert [e.message for e in recent] == ["new"]


def test_empty_user_returns_no_entries(tmp_path: Path):
    log = ActivityLog(tmp_path)
    assert log.get_entries("nobody") == []


# --------------------------------------------------------------------------- #
# The core performance property: no whole-file rewrite per append
# --------------------------------------------------------------------------- #


def test_append_does_not_rewrite_whole_file(tmp_path: Path, monkeypatch):
    log = ActivityLog(tmp_path)
    calls = {"save": 0}
    real_save = log._save_entries

    def counting_save(user_id, entries):
        calls["save"] += 1
        return real_save(user_id, entries)

    monkeypatch.setattr(log, "_save_entries", counting_save)

    # Below the compaction interval, no full rewrite ever happens.
    for i in range(ActivityLog.COMPACTION_APPEND_INTERVAL - 1):
        log.log(ActivityType.USER_MESSAGE, f"m{i}", user_id="u")

    assert calls["save"] == 0
    assert len(_read_lines(tmp_path / "activity" / "u.jsonl")) == ActivityLog.COMPACTION_APPEND_INTERVAL - 1


# --------------------------------------------------------------------------- #
# Compaction: cap + retention
# --------------------------------------------------------------------------- #


def test_compaction_enforces_hard_cap_via_log(tmp_path: Path, monkeypatch):
    _patch_retention(monkeypatch, hours=168)
    monkeypatch.setattr(ActivityLog, "COMPACTION_APPEND_INTERVAL", 5)
    monkeypatch.setattr(ActivityLog, "MAX_ENTRIES_FALLBACK", 3)
    log = ActivityLog(tmp_path)

    for i in range(5):
        log.log(ActivityType.USER_MESSAGE, f"m{i}", user_id="u")

    # The 5th append trips compaction, which caps the file to the last 3.
    lines = _read_lines(tmp_path / "activity" / "u.jsonl")
    assert len(lines) == 3
    assert [json.loads(line)["message"] for line in lines] == ["m2", "m3", "m4"]


def test_compaction_prunes_by_retention_via_log(tmp_path: Path, monkeypatch):
    _patch_retention(monkeypatch, hours=1)
    monkeypatch.setattr(ActivityLog, "COMPACTION_APPEND_INTERVAL", 1)
    log = ActivityLog(tmp_path)

    seeded = [
        ActivityEntry(
            type=ActivityType.USER_MESSAGE,
            message="old-3h",
            user_id="u",
            timestamp=utc_now() - timedelta(hours=3),
        ),
        ActivityEntry(
            type=ActivityType.USER_MESSAGE,
            message="old-2h",
            user_id="u",
            timestamp=utc_now() - timedelta(hours=2),
        ),
        ActivityEntry(type=ActivityType.USER_MESSAGE, message="recent", user_id="u"),
    ]
    log._save_entries("u", seeded)

    # Any append (interval=1) triggers compaction, which prunes >1h entries.
    log.log(ActivityType.TASK_COMPLETED, "fresh", user_id="u")

    messages = {e.message for e in log.get_entries("u")}
    assert messages == {"recent", "fresh"}


def test_get_entries_does_not_apply_retention(tmp_path: Path, monkeypatch):
    """Reads return on-disk entries regardless of age (historical contract)."""
    _patch_retention(monkeypatch, hours=1)
    log = ActivityLog(tmp_path)
    old = ActivityEntry(
        type=ActivityType.USER_MESSAGE,
        message="ancient",
        user_id="u",
        timestamp=utc_now() - timedelta(hours=50),
    )
    log._save_entries("u", [old])

    # No compaction has run, so the old entry is still returned by a read.
    assert [e.message for e in log.get_entries("u")] == ["ancient"]


# --------------------------------------------------------------------------- #
# Lazy migration from the legacy JSON-array format
# --------------------------------------------------------------------------- #


def _write_legacy_array(activity_dir: Path, user_id: str, entries: list[ActivityEntry]) -> Path:
    activity_dir.mkdir(parents=True, exist_ok=True)
    legacy = activity_dir / f"{user_id}.json"
    legacy.write_text(
        json.dumps([e.model_dump(mode="json") for e in entries], default=str),
        encoding="utf-8",
    )
    return legacy


def test_migrates_legacy_json_on_read(tmp_path: Path):
    activity_dir = tmp_path / "activity"
    entries = [
        ActivityEntry(type=ActivityType.USER_MESSAGE, message="legacy-1", user_id="u", thread_id="t"),
        ActivityEntry(type=ActivityType.TODO_ADDED, message="legacy-2", user_id="u"),
    ]
    legacy = _write_legacy_array(activity_dir, "u", entries)

    log = ActivityLog(tmp_path)
    read = log.get_entries("u")

    assert [e.message for e in read] == ["legacy-2", "legacy-1"]  # newest first
    assert not legacy.exists()
    assert (activity_dir / "u.jsonl").exists()
    assert len(_read_lines(activity_dir / "u.jsonl")) == 2


def test_migrates_legacy_json_on_append(tmp_path: Path):
    activity_dir = tmp_path / "activity"
    legacy = _write_legacy_array(
        activity_dir,
        "u",
        [ActivityEntry(type=ActivityType.USER_MESSAGE, message="legacy", user_id="u")],
    )

    log = ActivityLog(tmp_path)
    log.log(ActivityType.TODO_ADDED, "after", user_id="u")

    assert not legacy.exists()
    lines = _read_lines(activity_dir / "u.jsonl")
    assert [json.loads(line)["message"] for line in lines] == ["legacy", "after"]


def test_failed_jsonl_write_preserves_legacy_file(tmp_path: Path, monkeypatch):
    """If the JSONL rewrite fails during migration, the legacy file is kept."""
    activity_dir = tmp_path / "activity"
    legacy = _write_legacy_array(
        activity_dir,
        "u",
        [ActivityEntry(type=ActivityType.USER_MESSAGE, message="precious", user_id="u")],
    )

    log = ActivityLog(tmp_path)
    monkeypatch.setattr(log, "_save_entries", lambda user_id, entries: False)

    with log._get_lock("u"):
        log._migrate_legacy_locked("u")

    # No data loss: legacy still on disk, no JSONL created.
    assert legacy.exists()
    assert not (activity_dir / "u.jsonl").exists()


def test_corrupt_legacy_file_is_not_destroyed(tmp_path: Path):
    """An unreadable legacy file is left in place rather than wiped."""
    activity_dir = tmp_path / "activity"
    activity_dir.mkdir(parents=True, exist_ok=True)
    legacy = activity_dir / "u.json"
    legacy.write_text("{ this is not valid json", encoding="utf-8")

    log = ActivityLog(tmp_path)
    assert log.get_entries("u") == []
    assert legacy.exists()  # preserved for recovery
    assert not (activity_dir / "u.jsonl").exists()


def test_load_skips_malformed_middle_line(tmp_path: Path):
    activity_dir = tmp_path / "activity"
    activity_dir.mkdir(parents=True, exist_ok=True)

    def line(msg: str) -> str:
        return json.dumps(
            ActivityEntry(type=ActivityType.USER_MESSAGE, message=msg, user_id="u").model_dump(mode="json"),
            default=str,
        )

    (activity_dir / "u.jsonl").write_text(
        line("a") + "\n" + "{garbage" + "\n" + line("b") + "\n", encoding="utf-8"
    )

    log = ActivityLog(tmp_path)
    assert [e.message for e in log.get_entries("u")] == ["b", "a"]


def test_concurrent_appends_do_not_lose_entries(tmp_path: Path):
    """The per-user lock serializes appends across threads (no torn/lost lines)."""
    import threading

    log = ActivityLog(tmp_path)
    threads_count, per_thread = 8, 20  # 160 total, below the compaction interval

    def worker(n: int):
        # Mix instances to mirror the singleton + throwaway-instance reality.
        inst = log if n % 2 == 0 else ActivityLog(tmp_path)
        for i in range(per_thread):
            inst.log(ActivityType.USER_MESSAGE, f"t{n}-{i}", user_id="u")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = _read_lines(tmp_path / "activity" / "u.jsonl")
    assert len(lines) == threads_count * per_thread
    # Every line is valid JSON (no interleaved/torn writes).
    messages = {json.loads(line)["message"] for line in lines}
    assert len(messages) == threads_count * per_thread


def test_migration_is_idempotent_no_duplicates(tmp_path: Path):
    """If both files exist, JSONL is authoritative and legacy is dropped."""
    activity_dir = tmp_path / "activity"
    _write_legacy_array(
        activity_dir,
        "u",
        [ActivityEntry(type=ActivityType.USER_MESSAGE, message="legacy-stale", user_id="u")],
    )
    # Simulate a prior migration that already produced the JSONL.
    jsonl = activity_dir / "u.jsonl"
    jsonl.write_text(
        json.dumps(
            ActivityEntry(type=ActivityType.USER_MESSAGE, message="jsonl-truth", user_id="u").model_dump(mode="json"),
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )

    log = ActivityLog(tmp_path)
    read = log.get_entries("u")

    assert [e.message for e in read] == ["jsonl-truth"]  # legacy not re-combined
    assert not (activity_dir / "u.json").exists()


# --------------------------------------------------------------------------- #
# Torn / malformed lines
# --------------------------------------------------------------------------- #


def test_load_tolerates_torn_final_line(tmp_path: Path):
    activity_dir = tmp_path / "activity"
    activity_dir.mkdir(parents=True, exist_ok=True)
    good = json.dumps(
        ActivityEntry(type=ActivityType.USER_MESSAGE, message="good", user_id="u").model_dump(mode="json"),
        default=str,
    )
    # A valid line, a blank line, and a truncated/garbage final line (crash mid-append).
    (activity_dir / "u.jsonl").write_text(good + "\n\n" + '{"type": "user_mess', encoding="utf-8")

    log = ActivityLog(tmp_path)
    entries = log.get_entries("u")
    assert [e.message for e in entries] == ["good"]


# --------------------------------------------------------------------------- #
# Deletion + clear
# --------------------------------------------------------------------------- #


def test_delete_for_thread_returns_count(tmp_path: Path):
    log = ActivityLog(tmp_path)
    log.log(ActivityType.USER_MESSAGE, "keep", user_id="u", thread_id="keep")
    log.log(ActivityType.USER_MESSAGE, "drop-1", user_id="u", thread_id="drop")
    log.log(ActivityType.USER_MESSAGE, "drop-2", user_id="u", thread_id="drop")

    assert log.delete_for_thread("u", "drop") == 2
    assert [e.message for e in log.get_entries("u")] == ["keep"]
    assert log.delete_for_thread("u", "missing") == 0


def test_delete_thread_globally_across_users(tmp_path: Path):
    log = ActivityLog(tmp_path)
    log.log(ActivityType.USER_MESSAGE, "a-shared", user_id="alice", thread_id="shared")
    log.log(ActivityType.USER_MESSAGE, "a-other", user_id="alice", thread_id="other")
    log.log(ActivityType.USER_MESSAGE, "b-shared", user_id="bob", thread_id="shared")

    deleted = log.delete_thread_globally("shared")
    assert deleted == 2
    assert [e.message for e in log.get_entries("alice")] == ["a-other"]
    assert log.get_entries("bob") == []


def test_delete_thread_globally_migrates_legacy(tmp_path: Path):
    activity_dir = tmp_path / "activity"
    _write_legacy_array(
        activity_dir,
        "u",
        [
            ActivityEntry(type=ActivityType.USER_MESSAGE, message="keep", user_id="u", thread_id="keep"),
            ActivityEntry(type=ActivityType.USER_MESSAGE, message="drop", user_id="u", thread_id="drop"),
        ],
    )

    log = ActivityLog(tmp_path)
    assert log.delete_thread_globally("drop") == 1
    assert not (activity_dir / "u.json").exists()
    assert [e.message for e in log.get_entries("u")] == ["keep"]


def test_clear_removes_files_and_counter(tmp_path: Path):
    activity_dir = tmp_path / "activity"
    log = ActivityLog(tmp_path)
    log.log(ActivityType.USER_MESSAGE, "x", user_id="u")
    # A stale legacy file alongside should also be cleared.
    _write_legacy_array(activity_dir, "u", [])
    activity_module._append_counters["u"] = 42

    assert log.clear("u") is True
    assert not (activity_dir / "u.jsonl").exists()
    assert not (activity_dir / "u.json").exists()
    assert "u" not in activity_module._append_counters
    assert log.get_entries("u") == []


# --------------------------------------------------------------------------- #
# Cross-instance consistency (mirrors thread_deletion using its own instance)
# --------------------------------------------------------------------------- #


def test_separate_instances_share_disk_truth(tmp_path: Path):
    writer = ActivityLog(tmp_path)
    writer.log(ActivityType.USER_MESSAGE, "from-writer", user_id="u", thread_id="t")

    # A fresh instance over the same data_dir (as thread_deletion does) sees it.
    other = ActivityLog(tmp_path)
    assert [e.message for e in other.get_entries("u")] == ["from-writer"]
    assert other.delete_for_thread("u", "t") == 1
    assert writer.get_entries("u") == []
