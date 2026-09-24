"""A TODO file that does not load is preserved, never overwritten (#394).

Before: any load error (one invalid item, a UTF-8 BOM, truncated JSON) made
``get_todos`` answer an EMPTY list, and ``atomic_update`` saved that list on
exit, so the next write of any kind (the hourly archive sweep, slim's startup
migration, a create, a failed ``nym_todo`` update) destroyed every reminder.

Now a list that parses but fails validation is salvaged item by item, the
original bytes move to ``todos/quarantine/``, and the salvaged list is written
back; undecodable bytes are quarantined; an unreadable file refuses writes.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nymeria.core import todo_manager as todo_manager_module
from nymeria.core.ticker import Ticker
from nymeria.core.todo_manager import (
    TodoListUnavailableError,
    TodoManager,
    TodoStatus,
)
from nymeria.core.todo_schedule_db import TodoScheduleDB
from nymeria.core.time_utils import utc_now
from nymeria.tools import todo as todo_tools

USER = "owner"


@pytest.fixture
def recorders(monkeypatch):
    """Capture the audit row and the (off-thread) owner alert."""
    audits: list[tuple[str, str, str]] = []
    alerts: list[str] = []
    alerted = threading.Event()

    def _audit(store: str, detail: str, *, user_id: str = "default") -> None:
        audits.append((store, detail, user_id))

    def _alert(message: str, settings: Any, *, user_id: str, **_: Any) -> None:
        alerts.append(message)
        alerted.set()

    monkeypatch.setattr("nymeria.core.activity_log.log_external_edit", _audit)
    monkeypatch.setattr("nymeria.core.notification_dispatch.send_owner_alert", _alert)
    return SimpleNamespace(audits=audits, alerts=alerts, alerted=alerted)


def _seed(manager: TodoManager, *tasks: str, scheduled: bool = False) -> list[str]:
    ids = []
    with manager.atomic_update(USER) as todo_list:
        for task in tasks:
            item = todo_list.add_item(
                task,
                thread_id="thread-a",
                scheduled_for=utc_now().replace(year=utc_now().year + 1)
                if scheduled
                else None,
            )
            assert item is not None
            ids.append(item.id)
    return ids


def _path(manager: TodoManager, user_id: str = USER) -> Path:
    return manager.todos_dir / f"{user_id}.json"


def _corrupt_item(manager: TodoManager, todo_id: str) -> str:
    """Give one item a status the model rejects, as a hand edit would."""
    path = _path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    for raw in data["items"]:
        if raw["id"] == todo_id:
            raw["status"] = "completed"
    text = json.dumps(data, indent=2)
    path.write_text(text, encoding="utf-8")
    return text


def _quarantined(manager: TodoManager, user_id: str = USER) -> list[Path]:
    return sorted((manager.todos_dir / "quarantine").glob(f"{user_id}.corrupt-*"))


def _ids_on_disk(manager: TodoManager, user_id: str = USER) -> set[str]:
    data = json.loads(_path(manager, user_id).read_text(encoding="utf-8"))
    return {raw["id"] for raw in data["items"]}


def _config(thread_id: str) -> dict:
    return {"configurable": {"user_id": USER, "thread_id": thread_id}}


# --- B1 / B8: salvage keeps the valid items and preserves the original ------


def test_one_invalid_item_keeps_the_rest_and_preserves_the_original(
    tmp_path, recorders
):
    manager = TodoManager(tmp_path)
    keep_a, bad, keep_b = _seed(manager, "water plants", "call mum", "pay rent")
    corrupt_text = _corrupt_item(manager, bad)

    loaded = manager.get_todos(USER)

    assert {item.id for item in loaded.items} == {keep_a, keep_b}
    quarantined = _quarantined(manager)
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == corrupt_text
    # The live file was rewritten with exactly the salvaged items, so no later
    # save can overwrite the dropped one (it lives only in quarantine now).
    assert _ids_on_disk(manager) == {keep_a, keep_b}
    assert len(recorders.audits) == 1
    store, detail, audited_user = recorders.audits[0]
    assert (store, audited_user) == ("todos", USER)
    assert "kept 2" in detail and "dropped 1" in detail
    assert quarantined[0].name in detail
    assert recorders.alerted.wait(5)
    assert len(recorders.alerts) == 1
    assert recorders.alerts[0].startswith("[TODO LIST REPAIRED]")
    assert "2 item(s) were kept" in recorders.alerts[0]
    assert "1 that could not be read" in recorders.alerts[0]
    assert quarantined[0].name in recorders.alerts[0]


def test_a_second_load_after_a_repair_is_clean(tmp_path, recorders):
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    _corrupt_item(manager, bad)
    manager.get_todos(USER)
    assert recorders.alerted.wait(5)

    again = manager.get_todos(USER)

    assert [item.id for item in again.items] == [keep]
    assert len(_quarantined(manager)) == 1
    assert len(recorders.audits) == 1
    assert len(recorders.alerts) == 1


def test_an_invalid_envelope_does_not_cost_the_items(tmp_path, recorders):
    manager = TodoManager(tmp_path)
    keep_a, keep_b = _seed(manager, "water plants", "call mum")
    path = _path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created_at"] = "not a date"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = manager.get_todos(USER)

    assert {item.id for item in loaded.items} == {keep_a, keep_b}
    assert loaded.user_id == USER
    assert _ids_on_disk(manager) == {keep_a, keep_b}
    assert len(_quarantined(manager)) == 1


# --- B2: the unattended writers no longer wipe the list ---------------------


def _archive_sweep(manager: TodoManager) -> None:
    fake_ticker = SimpleNamespace(
        settings=SimpleNamespace(todo_auto_archive_days=7), todo_manager=manager
    )
    Ticker._archive_completed_todos(fake_ticker)  # type: ignore[arg-type]


def _startup_migration(manager: TodoManager) -> None:
    manager.migrate_unscoped_todos(USER)


def _create_one(manager: TodoManager) -> None:
    with manager.atomic_update(USER) as todo_list:
        assert todo_list.add_item("new one", thread_id="thread-a") is not None


@pytest.mark.parametrize(
    "writer",
    [_archive_sweep, _startup_migration, _create_one],
    ids=["archive-sweep", "startup-migration", "create"],
)
def test_unattended_and_first_writes_keep_the_valid_items(
    tmp_path, recorders, writer
):
    manager = TodoManager(tmp_path)
    keep_a, bad, keep_b = _seed(manager, "water plants", "call mum", "pay rent")
    _corrupt_item(manager, bad)

    writer(manager)

    remaining = {item.id for item in manager.get_todos(USER).items}
    assert {keep_a, keep_b} <= remaining
    assert bad not in remaining
    assert len(_quarantined(manager)) == 1


# --- B3: undecodable bytes are quarantined, the list starts fresh -----------


def test_non_json_file_is_quarantined_and_a_new_list_starts(tmp_path, recorders):
    manager = TodoManager(tmp_path)
    manager.todos_dir.mkdir(parents=True, exist_ok=True)
    truncated = '{"user_id": "owner", "items": [{"id": "abc'
    _path(manager).write_text(truncated, encoding="utf-8")

    assert manager.get_todos(USER).items == []
    _create_one(manager)

    remaining = manager.get_todos(USER).items
    assert [item.task for item in remaining] == ["new one"]
    quarantined = _quarantined(manager)
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == truncated
    assert recorders.alerted.wait(5)
    assert recorders.alerts[0].startswith("[TODO LIST CORRUPT]")
    assert quarantined[0].name in recorders.alerts[0]
    assert len(recorders.audits) == 1


# --- B4 / B11: a BOM is not corruption --------------------------------------


def test_a_bom_file_loads_fully_and_is_visible_to_the_sweeps(tmp_path, recorders):
    manager = TodoManager(tmp_path)
    ids = _seed(manager, "water plants", "call mum")
    path = _path(manager)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    assert [item.id for item in manager.get_todos(USER).items] == ids
    assert manager.get_all_users_with_todos() == [USER]
    assert manager.find_owner(ids[1]) == USER
    assert not _quarantined(manager)
    assert recorders.audits == []
    assert recorders.alerts == []


# --- B5: an unreadable file refuses writes ----------------------------------


def _deny_reads(monkeypatch, path: Path) -> dict:
    """Make ``path`` unreadable; flip the returned ``on`` flag to restore it
    (``monkeypatch.undo()`` would also undo the alert recorders)."""
    real_read_bytes = Path.read_bytes
    state = {"on": True}

    def _denied(self: Path) -> bytes:
        if state["on"] and self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _denied)
    return state


def test_an_unreadable_file_refuses_writes_and_is_left_alone(
    tmp_path, recorders, monkeypatch
):
    manager = TodoManager(tmp_path)
    _seed(manager, "water plants")
    path = _path(manager)
    before = path.read_bytes()
    denied = _deny_reads(monkeypatch, path)

    todo_list, authoritative = manager.load_todos(USER)
    assert todo_list.items == [] and authoritative is False
    with pytest.raises(TodoListUnavailableError, match="could not be read"):
        _create_one(manager)
    with pytest.raises(TodoListUnavailableError):
        manager.delete_todos_for_thread(USER, "thread-a")

    denied["on"] = False
    assert path.read_bytes() == before
    assert not _quarantined(manager)
    assert recorders.audits == []


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root reads a mode-000 file anyway",
)
def test_a_really_unreadable_file_stays_listed_and_keeps_its_schedule(
    tmp_path, recorders
):
    """A genuine EACCES: the enumerator still lists the user (so the sweeps
    and the rebuild see an unreadable list, not a missing one), the archive
    sweep leaves the file alone, and the rebuild keeps the user's rows."""
    manager = TodoManager(tmp_path)
    (keep,) = _seed(manager, "water plants", scheduled=True)
    schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")
    assert schedule_db.rebuild_from_todos(manager) == 1
    path = _path(manager)
    before = path.read_bytes()
    path.chmod(0)
    try:
        assert manager.get_all_users_with_todos() == [USER]
        _archive_sweep(manager)
        assert schedule_db.rebuild_from_todos(manager) == 0
        assert schedule_db.get_entry(keep) is not None
    finally:
        path.chmod(0o644)
    assert path.read_bytes() == before
    assert not _quarantined(manager)


def test_a_failed_quarantine_copy_refuses_writes(tmp_path, recorders, monkeypatch):
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    corrupt_text = _corrupt_item(manager, bad)
    monkeypatch.setattr(todo_manager_module, "quarantine_copy", lambda *_a: None)

    # Readers still see what validates...
    assert [item.id for item in manager.get_todos(USER).items] == [keep]
    # ...but nothing may be saved over the unpreserved original, not even by
    # a thread deletion (which used to bypass the refusal).
    with pytest.raises(TodoListUnavailableError):
        _create_one(manager)
    with pytest.raises(TodoListUnavailableError):
        manager.delete_todos_for_thread(USER, "thread-a")

    assert _path(manager).read_text(encoding="utf-8") == corrupt_text
    assert recorders.audits == []


def test_a_failed_write_back_leaves_the_original_live(
    tmp_path, recorders, monkeypatch
):
    """Disk full after the copy: the original stays the live file (never
    absent), the copy is withdrawn, writes refuse, and no "kept" alert."""
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    corrupt_text = _corrupt_item(manager, bad)
    monkeypatch.setattr(manager, "save_todos", lambda _todo_list: False)

    todo_list, writable = manager.load_todos(USER)

    assert [item.id for item in todo_list.items] == [keep]
    assert writable is False
    assert _path(manager).read_text(encoding="utf-8") == corrupt_text
    assert not _quarantined(manager)
    assert recorders.audits == [] and recorders.alerts == []


def test_the_list_file_is_present_throughout_a_repair(
    tmp_path, recorders, monkeypatch
):
    """A lock-free reader must never find the file absent mid-repair: absent
    reads as an AUTHORITATIVE empty list (the ticker would unschedule, a
    worker save would write empty over the salvage)."""
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    _corrupt_item(manager, bad)
    path = _path(manager)
    seen: list[bool] = []
    real_copy = todo_manager_module.quarantine_copy
    real_save = manager.save_todos

    def _copy(target: Path, data: bytes):
        result = real_copy(target, data)
        seen.append(path.exists())
        return result

    def _save(todo_list):
        seen.append(path.exists())
        return real_save(todo_list)

    monkeypatch.setattr(todo_manager_module, "quarantine_copy", _copy)
    monkeypatch.setattr(manager, "save_todos", _save)

    manager.get_todos(USER)

    assert seen == [True, True]
    assert _ids_on_disk(manager) == {keep}


def test_a_write_from_another_process_during_repair_is_kept(
    tmp_path, recorders, monkeypatch
):
    """The per-user lock is in-process only; a Docker api and worker share
    the file. A write that lands after this process read the bad bytes must
    survive, and this process must not report a repair it did not make."""
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    healthy = _path(manager).read_text(encoding="utf-8")
    corrupt_text = _corrupt_item(manager, bad)
    real_copy = todo_manager_module.quarantine_copy

    def _copy_then_other_process_writes(target: Path, data: bytes):
        result = real_copy(target, data)
        target.write_text(healthy, encoding="utf-8")
        return result

    monkeypatch.setattr(
        todo_manager_module, "quarantine_copy", _copy_then_other_process_writes
    )

    todo_list, writable = manager.load_todos(USER)

    assert {item.id for item in todo_list.items} == {keep, bad}
    assert writable is True
    assert _path(manager).read_text(encoding="utf-8") == healthy
    assert [q.read_text(encoding="utf-8") for q in _quarantined(manager)] == [corrupt_text]
    assert recorders.audits == []


# --- non-UTF-8 bytes and wrong shapes are corruption, not crashes -----------


def _with_literal_accent(manager: TodoManager) -> str:
    """The list's text with a literal non-ASCII character, as a hand edit in
    a Windows editor leaves it (the store itself writes ASCII escapes)."""
    text = _path(manager).read_text(encoding="utf-8").replace("\\u00e9", "\u00e9")
    assert "\u00e9" in text
    return text


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "utf-8-sig"])
def test_a_utf16_utf32_or_bom_file_loads_untouched(tmp_path, recorders, encoding):
    """PowerShell 5.1 redirects write UTF-16 and a Windows editor adds a BOM:
    that is still valid JSON text, so it loads (and the sweeps see it)."""
    manager = TodoManager(tmp_path)
    ids = _seed(manager, "caf\u00e9 run")
    _path(manager).write_bytes(_with_literal_accent(manager).encode(encoding))

    assert manager.get_all_users_with_todos() == [USER]
    assert manager.find_owner(ids[0]) == USER
    todo_list, writable = manager.load_todos(USER)

    assert [item.task for item in todo_list.items] == ["caf\u00e9 run"]
    assert writable is True
    assert not _quarantined(manager)
    assert recorders.audits == []


def test_non_utf8_text_is_quarantined_not_raised(tmp_path, recorders):
    """Set-Content writes cp1252: a non-ASCII byte is not valid UTF-8, so the
    file is corrupt. It may not escape as an exception (that broke the
    user's turns and left the ticker's marker claimed)."""
    manager = TodoManager(tmp_path)
    ids = _seed(manager, "caf\u00e9 run")
    encoded = _with_literal_accent(manager).encode("cp1252")
    _path(manager).write_bytes(encoded)

    assert manager.get_all_users_with_todos() == [USER]
    todo_list, writable = manager.load_todos(USER)

    assert todo_list.items == [] and writable is True
    assert [q.read_bytes() for q in _quarantined(manager)] == [encoded]
    assert ids[0] not in _ids_on_disk(manager)
    assert recorders.alerted.wait(5)
    assert recorders.alerts[0].startswith("[TODO LIST CORRUPT]")


def test_an_unreadable_list_logs_one_error_per_episode(
    tmp_path, recorders, monkeypatch, caplog
):
    """Every prompt build reads the list: an unreadable file must not write
    an ERROR line per read, but a new episode after recovery logs again."""
    manager = TodoManager(tmp_path)
    _seed(manager, "water plants")
    path = _path(manager)
    denied = _deny_reads(monkeypatch, path)
    caplog.set_level("DEBUG", logger="nymeria.core.todo_manager")

    for _ in range(3):
        manager.get_todos(USER)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1

    denied["on"] = False
    assert manager.load_todos(USER)[1] is True
    denied["on"] = True
    manager.get_todos(USER)
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 2


@pytest.mark.parametrize("document", ["[]", "null", '"text"', '{"items": 5}'])
def test_a_wrongly_shaped_document_is_listed_and_repaired(
    tmp_path, recorders, document
):
    manager = TodoManager(tmp_path)
    manager.todos_dir.mkdir(parents=True, exist_ok=True)
    _path(manager).write_text(document, encoding="utf-8")
    _path(manager, "friend").write_text(
        json.dumps({"user_id": "friend", "items": []}), encoding="utf-8"
    )

    assert manager.get_all_users_with_todos() == [USER]
    assert manager.find_owner("anything") is None
    assert manager.get_todos(USER).items == []
    assert [q.read_text(encoding="utf-8") for q in _quarantined(manager)] == [document]
    assert recorders.alerted.wait(5)
    assert recorders.alerts[0].startswith("[TODO LIST CORRUPT]")
    assert "kept" not in recorders.audits[0][1]


def test_startup_migration_survives_one_unrepairable_list(
    tmp_path, recorders, monkeypatch
):
    from nymeria.core.agent import NymeriaAgent

    manager = TodoManager(tmp_path)
    with manager.atomic_update("friend") as friend_list:
        assert friend_list.add_item("friend's", thread_id="t") is not None
    keep, bad = _seed(manager, "water plants", "call mum")
    _corrupt_item(manager, bad)
    monkeypatch.setattr(todo_manager_module, "quarantine_copy", lambda *_a: None)
    migrated: list[str] = []
    real_migrate = manager.migrate_unscoped_todos

    def _migrate(user_id: str, default_thread_id: str = "legacy") -> int:
        migrated.append(user_id)
        return real_migrate(user_id, default_thread_id)

    monkeypatch.setattr(manager, "migrate_unscoped_todos", _migrate)

    NymeriaAgent._migrate_unscoped_todos(SimpleNamespace(todo_manager=manager))  # type: ignore[arg-type]

    assert sorted(migrated) == ["friend", USER]


def test_thread_deletion_warns_and_continues_past_an_unrepairable_list(
    tmp_path, recorders, monkeypatch
):
    from nymeria.core.thread_deletion import ThreadDeletionResult, _delete_todos

    manager = TodoManager(tmp_path)
    with manager.atomic_update("friend") as friend_list:
        assert friend_list.add_item("friend's", thread_id="thread-a") is not None
    _, bad = _seed(manager, "water plants", "call mum")
    _corrupt_item(manager, bad)
    monkeypatch.setattr(todo_manager_module, "quarantine_copy", lambda *_a: None)
    result = ThreadDeletionResult(thread_id="thread-a", user_id=USER)

    _delete_todos(
        SimpleNamespace(todo_manager=manager, _schedule_db=None),  # type: ignore[arg-type]
        "thread-a",
        result,
    )

    assert result.deleted.get("todos_deleted") == 1
    assert manager.get_todos("friend").items == []
    assert any(USER in warning for warning in result.warnings)


def test_schedule_sync_leaves_the_row_of_an_unreadable_list(
    tmp_path, recorders, monkeypatch
):
    manager = TodoManager(tmp_path)
    (keep,) = _seed(manager, "water plants", scheduled=True)
    schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")
    manager.sync_schedule_to_db(USER, keep, schedule_db)
    _deny_reads(monkeypatch, _path(manager))

    manager.sync_schedule_to_db(USER, keep, schedule_db)

    assert schedule_db.get_entry(keep) is not None


# --- B6: the agent tool on a salvaged list ----------------------------------


def test_updating_a_dropped_item_reports_not_found_without_wiping(
    tmp_path, recorders, monkeypatch
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    keep, bad = _seed(manager, "water plants", "call mum")
    _corrupt_item(manager, bad)
    nym_todo: Any = todo_tools.nym_todo

    result = nym_todo.func(todo_id=bad, status="done", config=_config("thread-a"))

    assert "not found" in result.lower()
    assert _ids_on_disk(manager) == {keep}
    assert manager.get_todo_by_id(USER, keep) is not None


# --- B7: a list naming another user stays bound to its own file ------------


def test_a_list_claiming_another_user_saves_to_its_own_file(tmp_path, recorders):
    manager = TodoManager(tmp_path)
    with manager.atomic_update("victim") as victim_list:
        assert victim_list.add_item("victim's own", thread_id="t") is not None
    victim_before = _path(manager, "victim").read_bytes()
    _seed(manager, "water plants")
    path = _path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["user_id"] = "victim"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert manager.get_todos(USER).user_id == USER
    _create_one(manager)

    assert _path(manager, "victim").read_bytes() == victim_before
    tasks = {item.task for item in manager.get_todos(USER).items}
    assert tasks == {"water plants", "new one"}


def test_a_sweep_loading_by_filename_stem_keeps_the_canonical_id(tmp_path):
    """The sweeps pass ``path.stem`` (the sanitized id); that is the same
    file, so the canonical embedded id must survive the load."""
    manager = TodoManager(tmp_path)
    with manager.atomic_update("mate.one") as todo_list:
        assert todo_list.add_item("x", thread_id="t") is not None
    assert manager.get_all_users_with_todos() == ["mateone"]

    assert manager.get_todos("mateone").user_id == "mate.one"


# --- B9: the schedule index survives a salvage ------------------------------


def test_rebuild_after_a_salvage_indexes_the_valid_scheduled_items(
    tmp_path, recorders
):
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum", scheduled=True)
    _corrupt_item(manager, bad)
    schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")

    assert schedule_db.rebuild_from_todos(manager) == 1

    far_future = utc_now().replace(year=utc_now().year + 2).timestamp()
    assert [entry.todo_id for entry in schedule_db.get_due(before=far_future)] == [keep]


# --- locking: the repair re-reads under the lock, the happy path takes none --


def _hold_lock_in_other_thread(lock) -> tuple[threading.Event, threading.Thread]:
    held = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(5)
    return release, holder


def test_a_healthy_read_does_not_wait_on_the_user_lock(tmp_path):
    """rebuild_from_todos reads lists while holding the schedule index lock,
    and atomic_update bodies take that index lock while holding the TODO
    lock: a plain read that blocked on the TODO lock would deadlock them."""
    manager = TodoManager(tmp_path)
    ids = _seed(manager, "water plants")
    release, holder = _hold_lock_in_other_thread(manager._get_lock(USER))
    result: list = []
    reader = threading.Thread(
        target=lambda: result.append(manager.get_todos(USER)), daemon=True
    )
    try:
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "a healthy read blocked on the user lock"
        assert [item.id for item in result[0].items] == ids
    finally:
        release.set()
        holder.join(5)


def test_the_repair_rereads_under_the_lock(tmp_path, recorders, monkeypatch):
    """A reader that saw the bad bytes must not move aside a file that a
    concurrent save fixed while the reader waited for the lock."""
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    healthy = _path(manager).read_text(encoding="utf-8")
    _corrupt_item(manager, bad)
    lock = manager._get_lock(USER)
    result: list = []
    repairs: list[str] = []
    real_repair = manager._repair_unloadable

    def _spy(user_id: str):
        repairs.append(user_id)
        return real_repair(user_id)

    monkeypatch.setattr(manager, "_repair_unloadable", _spy)
    reader = threading.Thread(
        target=lambda: result.append(manager.get_todos(USER)), daemon=True
    )
    with lock:
        reader.start()
        reader.join(0.5)
        assert reader.is_alive(), "the repair should wait for the user lock"
        # The writer holding the lock replaces the bad file with a good one.
        _path(manager).write_text(healthy, encoding="utf-8")
    reader.join(5)

    assert repairs == [USER], "the reader never took the repair path"
    assert {item.id for item in result[0].items} == {keep, bad}
    assert not _quarantined(manager)
    assert _path(manager).read_text(encoding="utf-8") == healthy
    assert recorders.audits == []


def test_a_busy_lock_serves_a_read_only_salvaged_view(
    tmp_path, recorders, monkeypatch
):
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    corrupt_text = _corrupt_item(manager, bad)
    monkeypatch.setattr(todo_manager_module, "_REPAIR_LOCK_TIMEOUT_SECONDS", 0.2)
    release, holder = _hold_lock_in_other_thread(manager._get_lock(USER))
    try:
        todo_list, writable = manager._load(USER)
    finally:
        release.set()
        holder.join(5)

    assert [item.id for item in todo_list.items] == [keep]
    assert writable is False
    assert _path(manager).read_text(encoding="utf-8") == corrupt_text
    assert not _quarantined(manager)
    # The next load, with the lock free, performs the repair.
    assert [item.id for item in manager.get_todos(USER).items] == [keep]
    assert len(_quarantined(manager)) == 1


def test_a_busy_lock_serves_the_file_its_holder_repaired(
    tmp_path, recorders, monkeypatch
):
    """The realistic timeout: the lock holder (an atomic_update) repaired the
    file while this reader waited, so the re-read is healthy and must be
    served as the authoritative list, not discarded for an empty one."""
    manager = TodoManager(tmp_path)
    keep, bad = _seed(manager, "water plants", "call mum")
    _corrupt_item(manager, bad)
    monkeypatch.setattr(todo_manager_module, "_REPAIR_LOCK_TIMEOUT_SECONDS", 1.5)
    lock = manager._get_lock(USER)
    held = threading.Event()
    release = threading.Event()

    repair_now = threading.Event()

    def _holder() -> None:
        with lock:
            held.set()
            repair_now.wait(10)
            manager.get_todos(USER)  # repairs under the held (re-entrant) lock
            release.wait(10)  # keep holding past the reader's timeout

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(5)
    # The holder repairs only after the reader has started waiting, and keeps
    # the lock until the reader has timed out and answered.
    timer = threading.Timer(0.2, repair_now.set)
    timer.start()
    try:
        todo_list, writable = manager._load(USER)
    finally:
        release.set()
        holder.join(5)
        timer.cancel()

    assert [item.id for item in todo_list.items] == [keep]
    assert writable is True
    assert len(_quarantined(manager)) == 1


# --- B10 companion: the normal path is unchanged ----------------------------


def test_a_healthy_list_round_trips_without_repair(tmp_path, recorders):
    manager = TodoManager(tmp_path)
    ids = _seed(manager, "water plants", "call mum")
    with manager.atomic_update(USER) as todo_list:
        item = todo_list.get_item(ids[0])
        assert item is not None
        item.status = TodoStatus.DONE

    loaded = manager.get_todos(USER)
    assert [item.id for item in loaded.items] == ids
    assert loaded.get_item(ids[0]).status == TodoStatus.DONE  # type: ignore[union-attr]
    assert not (manager.todos_dir / "quarantine").exists()
    assert recorders.audits == []
