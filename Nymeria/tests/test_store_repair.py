"""Stores that cannot load a file never save over it (#401).

Before: thread metadata, notifications, FCM tokens, workflow ``nym.state``,
scheduler state and capability usage each answered an EMPTY default on any
load error (bad JSON, a BOM or UTF-16 file read as text, an invalid record,
an OSError), and the next ordinary save replaced the file with it: the
per-turn cost write, a notify call, the ticker's boot stamp, a tool call.

Now the original bytes are copied to ``<store dir>/quarantine/`` before
anything replaces them, the file is rewritten with what still validates, and
a file that cannot be read or preserved is served read-only. The shared
primitives live in ``core/store_repair.py``; the #394/#400 siblings are
``test_todo_store_corruption.py`` and ``test_user_profile_corruption.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nymeria.core import capability_usage as capability_usage_module
from nymeria.core import fcm, store_repair
from nymeria.core import notifications as notifications_module
from nymeria.core import scheduler_state as scheduler_state_module
from nymeria.core import thread_metadata as thread_metadata_module
from nymeria.core.capability_usage import CapabilityUsageStore
from nymeria.core.notifications import NotificationStore, NotificationsUnavailableError
from nymeria.core.scheduler_state import SchedulerStateManager
from nymeria.core.storage_paths import StoreUnavailableError
from nymeria.core.store_repair import (
    UnavailableEpisodes,
    read_json,
    replace_preserving,
    salvage_fields,
)
from nymeria.core.thread_metadata import (
    ThreadMetadata,
    ThreadMetadataManager,
    ThreadMetadataUnavailableError,
)
from nymeria.core.workflows import verbs_state

USER = "owner"


@pytest.fixture(autouse=True)
def recorders(monkeypatch):
    """Capture the audit row and the owner alert, for EVERY test here.

    Autouse so no test writes an activity row into the checkout's real data
    dir, and the alert runs synchronously: a background alert thread that
    outlived the test would call the real sender after the patch is undone.
    """
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
    monkeypatch.setattr(
        thread_metadata_module,
        "_send_alert",
        lambda user_id, message: _alert(message, None, user_id=user_id),
    )
    return SimpleNamespace(audits=audits, alerts=alerts, alerted=alerted)


@pytest.fixture(autouse=True)
def _fresh_episodes(monkeypatch):
    """Once-per-episode and failed-repair state is module-level: reset it."""
    for module in (
        thread_metadata_module,
        notifications_module,
        fcm,
        verbs_state,
        scheduler_state_module,
        capability_usage_module,
    ):
        monkeypatch.setattr(module, "_unavailable", UnavailableEpisodes())
    monkeypatch.setattr(store_repair, "_failed_repairs", {})


def _deny_reads(monkeypatch, path: Path) -> dict:
    """Make reading ``path`` raise PermissionError; returns a toggle."""
    real_read_bytes = Path.read_bytes
    toggle = {"on": True}

    def _denied(self: Path) -> bytes:
        if self == path and toggle["on"]:
            raise PermissionError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _denied)
    return toggle


def _quarantined(directory: Path, stem: str) -> list[Path]:
    return sorted((directory / "quarantine").glob(f"{stem}.corrupt-*"))


def _deny_quarantine(monkeypatch) -> None:
    """Make every preservation copy fail, as a full disk would."""

    def _refuse(path: Path, data: bytes):
        return None

    monkeypatch.setattr(store_repair, "quarantine_copy", _refuse)


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encode",
    [
        lambda text: text.encode("utf-8"),
        lambda text: b"\xef\xbb\xbf" + text.encode("utf-8"),
        lambda text: text.encode("utf-16"),
        lambda text: text.encode("utf-32"),
    ],
    ids=["utf-8", "utf-8-bom", "utf-16", "utf-32"],
)
def test_read_json_accepts_every_json_encoding(tmp_path, encode):
    path = tmp_path / "store.json"
    path.write_bytes(encode('{"a": "é"}'))
    read = read_json(path)
    assert read.parsed and read.data == {"a": "é"}


@pytest.mark.parametrize(
    "raw", [b'{"a": ', "{\"a\": \"caf\xe9\"}".encode("cp1252")], ids=["truncated", "cp1252"]
)
def test_read_json_reports_undecodable_bytes_as_not_json(tmp_path, raw):
    path = tmp_path / "store.json"
    path.write_bytes(raw)
    read = read_json(path)
    assert not read.parsed and not read.unreadable and not read.absent
    assert read.raw == raw and read.data is None
    assert read.error is not None and read.error.startswith("not valid JSON")


def test_read_json_tells_absent_from_unreadable(tmp_path):
    assert read_json(tmp_path / "missing.json").absent
    directory = tmp_path / "a-directory.json"
    directory.mkdir()
    unreadable = read_json(directory)  # IsADirectoryError is an OSError
    assert unreadable.unreadable and not unreadable.absent


def test_replace_preserving_copies_then_replaces(tmp_path):
    path = tmp_path / "store.json"
    path.write_bytes(b"{bad")
    result = replace_preserving(path, b"{bad", '{"ok": true}')
    assert result.replaced and not result.changed
    assert result.quarantine is not None
    assert result.quarantine.read_bytes() == b"{bad"
    assert result.quarantine.parent == tmp_path / "quarantine"
    assert path.read_text(encoding="utf-8") == '{"ok": true}'


def test_replace_preserving_keeps_a_file_that_changed_since_the_read(tmp_path):
    path = tmp_path / "store.json"
    path.write_bytes(b'{"newer": 1}')  # another writer landed after our read
    result = replace_preserving(path, b"{bad", '{"stale": true}')
    assert result.changed and not result.replaced
    assert path.read_bytes() == b'{"newer": 1}'
    assert result.quarantine is not None and result.quarantine.read_bytes() == b"{bad"


def test_replace_preserving_writes_nothing_without_a_copy(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    path.write_bytes(b"{bad")
    _deny_quarantine(monkeypatch)
    result = replace_preserving(path, b"{bad", "{}")
    assert result == (None, False, False)
    assert path.read_bytes() == b"{bad"


def test_a_failed_write_back_leaves_the_original_and_no_copy(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    path.write_bytes(b"{bad")

    def _fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_repair, "write_text_atomic", _fail)
    result = replace_preserving(path, b"{bad", "{}")
    assert result == (None, False, False)
    assert path.read_bytes() == b"{bad"
    assert _quarantined(tmp_path, "store") == []


def test_salvage_fields_keeps_what_validates_with_the_required_identity():
    meta, dropped = salvage_fields(
        ThreadMetadata,
        {"thread_id": 7, "title": "Taxes", "pinned": True, "total_cost_usd_micros": "x"},
        {"thread_id": "t-1"},
    )
    assert (meta.thread_id, meta.title, meta.pinned) == ("t-1", "Taxes", True)
    assert meta.total_cost_usd_micros == 0
    assert dropped == ["total_cost_usd_micros"]


def test_episodes_report_once_until_ended():
    episodes = UnavailableEpisodes()
    assert episodes.start("u") is True
    assert episodes.start("u") is False
    episodes.end("u")
    assert episodes.start("u") is True


# ---------------------------------------------------------------------------
# Thread metadata
# ---------------------------------------------------------------------------


def _metadata(tmp_path) -> ThreadMetadataManager:
    return ThreadMetadataManager(tmp_path)


def _seed_threads(manager: ThreadMetadataManager) -> None:
    manager.set_title(USER, "thread-a", "Taxes 2026")
    manager.set_pinned(USER, "thread-a", True)
    manager.set_title(USER, "thread-b", "Holiday plans")
    manager.set_title(USER, "thread-c", "Car service")


def _metadata_path(manager: ThreadMetadataManager, user_id: str = USER) -> Path:
    return manager.metadata_dir / f"{user_id}.json"


def _rows_on_disk(manager: ThreadMetadataManager) -> dict:
    return json.loads(_metadata_path(manager).read_text(encoding="utf-8"))["threads"]


def test_a_bad_field_resets_only_itself_and_the_original_is_preserved(tmp_path, recorders):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    path = _metadata_path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["threads"]["thread-a"]["total_cost_usd_micros"] = "a lot"
    data["threads"]["thread-c"] = "not a row"
    corrupt = json.dumps(data)
    path.write_text(corrupt, encoding="utf-8")

    threads = {meta.thread_id: meta for meta in manager.list_threads(USER)}

    assert set(threads) == {"thread-a", "thread-b"}
    assert (threads["thread-a"].title, threads["thread-a"].pinned) == ("Taxes 2026", True)
    assert threads["thread-a"].total_cost_usd_micros == 0
    assert threads["thread-b"].title == "Holiday plans"
    quarantined = _quarantined(manager.metadata_dir, USER)
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == corrupt
    assert set(_rows_on_disk(manager)) == {"thread-a", "thread-b"}
    assert len(recorders.audits) == 1
    store, detail, audited_user = recorders.audits[0]
    assert (store, audited_user) == ("thread_metadata", USER)
    assert "kept 2" in detail and "dropped 1" in detail and "reset 1" in detail
    assert quarantined[0].name in detail
    assert "a lot" not in detail
    assert recorders.alerted.wait(5)
    alert = recorders.alerts[0]
    assert alert.startswith("[THREAD LIST REPAIRED]")
    assert "2 threads were kept" in alert
    assert "1 unreadable row was dropped" in alert
    assert "1 unreadable field was reset" in alert
    assert quarantined[0].name in alert


def test_the_next_write_after_a_repair_keeps_the_salvaged_rows(tmp_path, recorders):
    """The per-turn cost write is what used to wipe the file."""
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    path = _metadata_path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["threads"]["thread-b"]["pinned"] = "sometimes"
    path.write_text(json.dumps(data), encoding="utf-8")

    with manager.atomic_update(USER) as store:
        store.threads["thread-a"].total_cost_usd_micros += 5

    rows = _rows_on_disk(manager)
    assert set(rows) == {"thread-a", "thread-b", "thread-c"}
    assert rows["thread-a"]["total_cost_usd_micros"] == 5
    assert rows["thread-b"]["title"] == "Holiday plans"
    assert rows["thread-b"]["pinned"] is False
    assert len(_quarantined(manager.metadata_dir, USER)) == 1


def test_a_repaired_file_loads_clean_the_second_time(tmp_path, recorders):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    _metadata_path(manager).write_text("{not json", encoding="utf-8")
    assert manager.list_threads(USER) == []
    assert manager.list_threads(USER) == []
    assert len(_quarantined(manager.metadata_dir, USER)) == 1
    assert len(recorders.audits) == 1


def test_unparseable_text_is_preserved_and_reported_as_corrupt(tmp_path, recorders):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    raw = _metadata_path(manager).read_bytes()[:40]
    _metadata_path(manager).write_bytes(raw)

    manager.set_title(USER, "thread-new", "Fresh start")

    assert set(_rows_on_disk(manager)) == {"thread-new"}
    quarantined = _quarantined(manager.metadata_dir, USER)
    assert [q.read_bytes() for q in quarantined] == [raw]
    assert recorders.alerted.wait(5)
    assert recorders.alerts[0].startswith("[THREAD LIST CORRUPT]")
    assert quarantined[0].name in recorders.alerts[0]
    assert "starts empty" in recorders.audits[0][1]


def test_the_corrupt_alert_row_keeps_the_restore_hint(tmp_path, recorders):
    """#406: the in-app row (the message cut at the cap) says an admin can
    restore the list before the detail and the quarantine path."""
    from nymeria.core.notifications import NOTIFICATION_SUMMARY_MAX_CHARS

    manager = _metadata(tmp_path)
    _seed_threads(manager)
    _metadata_path(manager).write_text("{not json", encoding="utf-8")

    assert manager.list_threads(USER) == []

    assert recorders.alerted.wait(5)
    message = recorders.alerts[0]
    row = message[:NOTIFICATION_SUMMARY_MAX_CHARS]
    assert row.startswith("[THREAD LIST CORRUPT]")
    assert "An admin can restore it from the original" in row
    assert "no conversation was deleted" in message
    quarantined = _quarantined(manager.metadata_dir, USER)
    assert f"thread_metadata/quarantine/{quarantined[0].name}" in message


def test_a_utf16_file_loads_without_repair(tmp_path, recorders):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    path = _metadata_path(manager)
    path.write_bytes(path.read_text(encoding="utf-8").encode("utf-16"))

    assert {m.thread_id for m in manager.list_threads(USER)} == {
        "thread-a",
        "thread-b",
        "thread-c",
    }
    assert _quarantined(manager.metadata_dir, USER) == []
    assert recorders.audits == []


def test_an_unreadable_file_refuses_writes_and_is_left_alone(
    tmp_path, recorders, monkeypatch, caplog
):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    path = _metadata_path(manager)
    before = path.read_bytes()
    denied = _deny_reads(monkeypatch, path)

    with caplog.at_level("ERROR", logger="nymeria.core.thread_metadata"):
        assert manager.list_threads(USER) == []
        assert manager.list_threads(USER) == []
    with pytest.raises(ThreadMetadataUnavailableError):
        manager.set_title(USER, "thread-a", "Renamed")
    with pytest.raises(ThreadMetadataUnavailableError):
        with manager.atomic_update(USER):
            pass
    assert isinstance(ThreadMetadataUnavailableError("x"), StoreUnavailableError)
    assert manager.save_store(manager.get_store(USER)) is False
    denied["on"] = False
    assert path.read_bytes() == before
    assert _quarantined(manager.metadata_dir, USER) == []
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "unavailable" in r.getMessage()]
    assert len(errors) == 1  # once per episode, not once per read
    # A read error changes nothing on disk, so the owner is not paged for it
    # (a transient one would page on every flap); the refusal says why.
    assert recorders.alerts == []


def test_an_unavailable_episode_rearms_once_the_file_loads(
    tmp_path, recorders, monkeypatch, caplog
):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    denied = _deny_reads(monkeypatch, _metadata_path(manager))
    with caplog.at_level("ERROR", logger="nymeria.core.thread_metadata"):
        manager.list_threads(USER)
        manager.list_threads(USER)
        denied["on"] = False
        assert len(manager.list_threads(USER)) == 3  # healthy again: episode over
        denied["on"] = True
        manager.list_threads(USER)
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "unavailable" in r.getMessage()]
    assert len(errors) == 2


def test_an_unpreservable_file_is_read_only_and_untouched(tmp_path, recorders, monkeypatch):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    path = _metadata_path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["threads"]["thread-c"]["pinned"] = "maybe"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    _deny_quarantine(monkeypatch)

    titles = {m.thread_id: m.title for m in manager.list_threads(USER)}
    assert titles["thread-a"] == "Taxes 2026"  # the salvaged view is served
    with pytest.raises(ThreadMetadataUnavailableError):
        manager.set_pinned(USER, "thread-b", True)
    assert path.read_bytes() == before
    assert recorders.audits == []


def test_a_list_claiming_another_user_saves_to_its_own_file(tmp_path, recorders):
    manager = _metadata(tmp_path)
    manager.set_title("victim", "thread-v", "Victim's thread")
    manager.set_title(USER, "thread-a", "Mine")
    path = _metadata_path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["user_id"] = "victim"
    path.write_text(json.dumps(data), encoding="utf-8")

    manager.set_title(USER, "thread-b", "Also mine")

    victim = json.loads(_metadata_path(manager, "victim").read_text(encoding="utf-8"))
    assert set(victim["threads"]) == {"thread-v"}
    assert set(_rows_on_disk(manager)) == {"thread-a", "thread-b"}


def test_a_healthy_read_does_not_wait_on_the_user_lock(tmp_path):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    lock = manager._get_lock(USER)
    held = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert held.wait(5)
        result: list = []
        reader = threading.Thread(target=lambda: result.append(manager.list_threads(USER)))
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "a healthy read blocked on the user lock"
        assert len(result[0]) == 3
    finally:
        release.set()
        holder.join(5)


def test_global_thread_delete_skips_an_unavailable_user(tmp_path, recorders, monkeypatch):
    manager = _metadata(tmp_path)
    manager.set_title("alice", "shared", "Alice's copy")
    manager.set_title("bob", "shared", "Bob's copy")
    alice = _metadata_path(manager, "alice")
    data = json.loads(alice.read_text(encoding="utf-8"))
    data["threads"]["shared"]["pinned"] = "?"
    alice.write_text(json.dumps(data), encoding="utf-8")
    before = alice.read_bytes()
    _deny_quarantine(monkeypatch)

    assert manager.delete_thread_globally("shared") == 1

    assert alice.read_bytes() == before
    bob = json.loads(_metadata_path(manager, "bob").read_text(encoding="utf-8"))
    assert bob["threads"] == {}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _notification_path(store: NotificationStore, user_id: str = USER) -> Path:
    return store.notifications_dir / f"{user_id}.json"


def test_one_bad_notification_costs_only_itself(tmp_path, recorders):
    store = NotificationStore(tmp_path)
    kept = store.create(USER, "Flight rebooked", thread_id="thread-a")
    store.create(USER, "Will be damaged")
    path = _notification_path(store)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[1]["created_at"] = "not a date"
    corrupt = json.dumps(data)
    path.write_text(corrupt, encoding="utf-8")

    later = store.create(USER, "Unrelated later notification")

    on_disk = [row["id"] for row in json.loads(path.read_text(encoding="utf-8"))]
    assert on_disk == [kept.id, later.id]
    quarantined = _quarantined(store.notifications_dir, USER)
    assert [q.read_text(encoding="utf-8") for q in quarantined] == [corrupt]
    assert len(recorders.audits) == 1
    audit_store, detail, audited_user = recorders.audits[0]
    assert (audit_store, audited_user) == ("notifications", USER)
    assert "kept 1, dropped 1" in detail and quarantined[0].name in detail
    assert "Will be damaged" not in detail
    assert recorders.alerts == []  # no owner alert for this store


def test_an_unparseable_history_is_preserved_not_replaced(tmp_path, recorders):
    store = NotificationStore(tmp_path)
    store.create(USER, "Flight rebooked")
    path = _notification_path(store)
    path.write_text("[{garbage", encoding="utf-8")

    assert store.get_unread(USER) == []
    store.create(USER, "New one")

    assert [q.read_text(encoding="utf-8") for q in _quarantined(store.notifications_dir, USER)] == [
        "[{garbage"
    ]
    assert "starts empty" in recorders.audits[0][1]


def test_a_read_only_history_still_delivers_but_refuses_changes(tmp_path, recorders, monkeypatch):
    store = NotificationStore(tmp_path)
    first = store.create(USER, "Flight rebooked")
    path = _notification_path(store)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.append({"summary": 5})
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    _deny_quarantine(monkeypatch)

    created = store.create(USER, "Delivered anyway")
    assert created.summary == "Delivered anyway"
    assert [n.id for n in store.get_all(USER)] == [first.id]
    for change in (
        lambda: store.mark_read(first.id, USER),
        lambda: store.mark_all_read(USER),
        lambda: store.delete(first.id, USER),
        lambda: store.delete_for_thread(USER, "thread-a"),
    ):
        with pytest.raises(NotificationsUnavailableError):
            change()
    assert path.read_bytes() == before
    assert isinstance(NotificationsUnavailableError("x"), StoreUnavailableError)


def test_global_notification_cleanup_skips_an_unavailable_user(tmp_path, recorders, monkeypatch):
    store = NotificationStore(tmp_path)
    store.create("alice", "Alice's", thread_id="doomed")
    store.create("bob", "Bob's", thread_id="doomed")
    alice = _notification_path(store, "alice")
    data = json.loads(alice.read_text(encoding="utf-8"))
    data.append({"summary": 5})
    alice.write_text(json.dumps(data), encoding="utf-8")
    before = alice.read_bytes()
    _deny_quarantine(monkeypatch)

    assert store.delete_thread_globally("doomed") == 1

    assert alice.read_bytes() == before
    assert json.loads(_notification_path(store, "bob").read_text(encoding="utf-8")) == []


def test_an_unreadable_history_logs_once_per_episode(tmp_path, recorders, monkeypatch, caplog):
    store = NotificationStore(tmp_path)
    store.create(USER, "Flight rebooked")
    path = _notification_path(store)
    real_read_bytes = Path.read_bytes

    def _denied(self: Path) -> bytes:
        if self == path:
            raise PermissionError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _denied)
    with caplog.at_level("ERROR", logger="nymeria.core.notifications"):
        for _ in range(3):
            store.get_unread(USER)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# FCM tokens
# ---------------------------------------------------------------------------


def _tokens_path(tmp_path: Path) -> Path:
    return tmp_path / "fcm_tokens.json"


def test_a_malformed_token_entry_costs_only_itself(tmp_path, recorders):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-ALICE", "android", "alice")
    path = _tokens_path(tmp_path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries += ["not an entry", {"platform": "ios", "user_id": "carol"}, {"token": 7}]
    corrupt = json.dumps(entries)
    path.write_text(corrupt, encoding="utf-8")

    fcm.register_token(data_dir, "TOKEN-BOB", "ios", "bob")

    tokens = [entry["token"] for entry in json.loads(path.read_text(encoding="utf-8"))]
    assert tokens == ["TOKEN-ALICE", "TOKEN-BOB"]
    quarantined = _quarantined(tmp_path, "fcm_tokens")
    assert [q.read_text(encoding="utf-8") for q in quarantined] == [corrupt]
    assert recorders.audits[0][0] == "fcm_tokens"
    assert "kept 1 registration(s), dropped 3" in recorders.audits[0][1]
    assert "TOKEN-ALICE" not in recorders.audits[0][1]


def test_an_unparseable_token_file_is_preserved(tmp_path, recorders):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-ALICE", "android", "alice")
    raw = _tokens_path(tmp_path).read_bytes()[:25]
    _tokens_path(tmp_path).write_bytes(raw)

    fcm.register_token(data_dir, "TOKEN-BOB", "ios", "bob")

    assert [q.read_bytes() for q in _quarantined(tmp_path, "fcm_tokens")] == [raw]
    assert [e["token"] for e in fcm.load_tokens(data_dir)] == ["TOKEN-BOB"]


def test_a_read_only_token_file_serves_pushes_and_refuses_changes(tmp_path, recorders, monkeypatch):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-ALICE", "android", "alice")
    path = _tokens_path(tmp_path)
    entries = json.loads(path.read_text(encoding="utf-8")) + [{"token": None}]
    path.write_text(json.dumps(entries), encoding="utf-8")
    before = path.read_bytes()
    _deny_quarantine(monkeypatch)

    assert [e["token"] for e in fcm.load_tokens(data_dir)] == ["TOKEN-ALICE"]
    sent: list = []
    monkeypatch.setattr(fcm, "send_push", lambda token, **kwargs: sent.append(token) or True)
    fcm.send_to_all_devices(data_dir, "hello", user_id="alice")
    assert sent == ["TOKEN-ALICE"]
    for change in (
        lambda: fcm.register_token(data_dir, "TOKEN-BOB", "ios", "bob"),
        lambda: fcm.unregister_token(data_dir, "TOKEN-ALICE"),
        lambda: fcm.remove_thread_from_tokens(data_dir, "thread-a"),
    ):
        with pytest.raises(fcm.PushTokensUnavailableError):
            change()
    assert path.read_bytes() == before
    assert isinstance(fcm.PushTokensUnavailableError("x"), StoreUnavailableError)


def test_save_tokens_is_atomic_and_private(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-ALICE", "android", "alice")
    path = _tokens_path(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()

    def _crash(self: Path, target) -> None:
        raise OSError("killed mid-write")

    monkeypatch.setattr(Path, "replace", _crash)
    with pytest.raises(OSError):
        fcm.register_token(data_dir, "TOKEN-BOB", "ios", "bob")
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_registrations_lose_no_token(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    real_save = fcm.save_tokens

    def _slow_save(directory: str, tokens: list) -> None:
        threading.Event().wait(0.02)  # widen the load-mutate-save window
        real_save(directory, tokens)

    monkeypatch.setattr(fcm, "save_tokens", _slow_save)
    threads = [
        threading.Thread(target=fcm.register_token, args=(data_dir, f"T{i}", "android", "u"))
        for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(e["token"] for e in fcm.load_tokens(data_dir)) == [f"T{i}" for i in range(6)]


def test_the_quarantine_dir_is_denied_like_the_token_file():
    from nymeria.core.resource_map import SECRET_AT_REST_CHILDREN

    assert "fcm_tokens.json" in SECRET_AT_REST_CHILDREN
    assert "quarantine" in SECRET_AT_REST_CHILDREN


# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------


def test_a_corrupt_scheduler_state_is_preserved_at_boot(tmp_path, recorders):
    manager = SchedulerStateManager(tmp_path)
    manager.set_pending_missed(["todo-1", "todo-2"], trigger_catchup_paused=True)
    manager.path.write_text("{garbage-not-json", encoding="utf-8")

    state = manager.record_start()  # what the ticker does at every boot

    assert state["last_started_at"] is not None
    on_disk = json.loads(manager.path.read_text(encoding="utf-8"))
    assert on_disk["last_started_at"] == state["last_started_at"]
    assert on_disk["pending_missed_todo_ids"] == []
    quarantined = _quarantined(tmp_path, "scheduler_state")
    assert [q.read_text(encoding="utf-8") for q in quarantined] == ["{garbage-not-json"]
    assert recorders.audits[0][0] == "scheduler_state"


def test_a_non_object_scheduler_state_is_preserved(tmp_path, recorders):
    manager = SchedulerStateManager(tmp_path)
    manager.path.write_text('["todo-1"]', encoding="utf-8")
    state = manager.record_clean_shutdown()
    quarantined = _quarantined(tmp_path, "scheduler_state")
    assert [q.read_text(encoding="utf-8") for q in quarantined] == ['["todo-1"]']
    on_disk = json.loads(manager.path.read_text(encoding="utf-8"))
    assert on_disk["last_clean_shutdown_at"] == state["last_clean_shutdown_at"] is not None


def test_an_unpreservable_scheduler_state_is_not_saved_over(tmp_path, recorders, monkeypatch):
    manager = SchedulerStateManager(tmp_path)
    manager.path.write_text("{garbage", encoding="utf-8")
    _deny_quarantine(monkeypatch)

    state = manager.record_start()
    manager.set_pending_missed(["todo-9"], trigger_catchup_paused=True)
    manager.clear_pending_missed()
    manager.record_clean_shutdown()

    assert state["last_started_at"] is not None  # boot proceeds
    assert manager.path.read_text(encoding="utf-8") == "{garbage"


def test_an_unreadable_scheduler_state_is_not_saved_over(tmp_path, recorders, monkeypatch):
    manager = SchedulerStateManager(tmp_path)
    manager.set_pending_missed(["todo-1"], trigger_catchup_paused=True)
    before = manager.path.read_bytes()
    real_read_bytes = Path.read_bytes

    def _denied(self: Path) -> bytes:
        if self == manager.path:
            raise PermissionError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _denied)
    state = manager.record_start()
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)

    assert state["last_started_at"] is not None  # boot proceeds on defaults
    assert manager.path.read_bytes() == before
    assert _quarantined(tmp_path, "scheduler_state") == []


# ---------------------------------------------------------------------------
# Capability usage
# ---------------------------------------------------------------------------


def test_capability_usage_keeps_the_buckets_that_load(tmp_path, recorders):
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.record(user_id="alice", thread_id="t1", tools=["bash_execute"])
    data = json.loads(usage.path.read_text(encoding="utf-8"))
    data["mallory"] = "not a bucket"
    data["alice"]["t2"] = ["not", "a", "thread"]
    corrupt = json.dumps(data)
    usage.path.write_text(corrupt, encoding="utf-8")

    usage.record(user_id="bob", thread_id="t9", tools=["web_search"])

    assert usage.get_tool(user_id="alice", thread_id="t1", name="bash_execute").use_count == 1
    assert usage.get_tool(user_id="bob", thread_id="t9", name="web_search").use_count == 1
    on_disk = json.loads(usage.path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"alice", "bob"} and set(on_disk["alice"]) == {"t1"}
    quarantined = _quarantined(tmp_path, "capability_usage")
    assert [q.read_text(encoding="utf-8") for q in quarantined] == [corrupt]
    assert recorders.audits[0][0] == "capability_usage"


def test_an_unparseable_capability_index_is_preserved(tmp_path, recorders):
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.record(user_id="alice", thread_id="t1", tools=["bash_execute"])
    usage.path.write_text("{broken-json", encoding="utf-8")
    usage.record(user_id="bob", thread_id="t9", tools=["web_search"])
    assert [q.read_text(encoding="utf-8") for q in _quarantined(tmp_path, "capability_usage")] == [
        "{broken-json"
    ]
    assert usage.get_tool(user_id="bob", thread_id="t9", name="web_search").use_count == 1
    assert set(json.loads(usage.path.read_text(encoding="utf-8"))) == {"bob"}


def test_an_unpreservable_capability_index_is_not_recorded_over(tmp_path, recorders, monkeypatch):
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.path.write_text("{broken-json", encoding="utf-8")
    _deny_quarantine(monkeypatch)
    usage.record(user_id="bob", thread_id="t9", tools=["web_search"])
    assert usage.path.read_text(encoding="utf-8") == "{broken-json"


# ---------------------------------------------------------------------------
# Review round: nesting, field types, modes, races, unreadable files, throttle
# ---------------------------------------------------------------------------

_DEEP = "[" * 100_000 + "]" * 100_000  # past any interpreter's JSON depth limit


def test_read_json_reports_too_deep_nesting_as_not_json(tmp_path):
    path = tmp_path / "store.json"
    path.write_text(_DEEP, encoding="utf-8")
    read = read_json(path)
    assert not read.parsed and read.raw is not None
    assert read.error is not None and "RecursionError" in read.error


def test_a_deeply_nested_scheduler_state_does_not_stop_boot(tmp_path, recorders):
    manager = SchedulerStateManager(tmp_path)
    manager.path.write_text(_DEEP, encoding="utf-8")
    state = manager.record_start()  # used to raise out of Ticker.__init__
    assert state["last_started_at"] is not None
    assert [q.read_text(encoding="utf-8") for q in _quarantined(tmp_path, "scheduler_state")] == [
        _DEEP
    ]


def test_a_deeply_nested_thread_list_is_repaired_not_raised(tmp_path, recorders):
    manager = _metadata(tmp_path)
    _metadata_path(manager).write_text(_DEEP, encoding="utf-8")
    assert manager.list_threads(USER) == []
    manager.set_title(USER, "thread-a", "Works")
    assert set(_rows_on_disk(manager)) == {"thread-a"}


@pytest.mark.parametrize(
    "stored, pending, paused",
    [
        ({"pending_missed_todo_ids": 5, "trigger_catchup_paused": True}, [], True),
        ({"pending_missed_todo_ids": "abc"}, [], False),
        ({"pending_missed_todo_ids": ["t1", 7, ""], "trigger_catchup_paused": "yes"}, ["t1"], False),
        ({"last_started_at": 12, "pending_missed_todo_ids": ["t2"]}, ["t2"], False),
    ],
    ids=["int-ids", "string-ids", "mixed-ids", "int-timestamp"],
)
def test_a_wrong_typed_scheduler_field_is_repaired(tmp_path, recorders, stored, pending, paused):
    manager = SchedulerStateManager(tmp_path)
    original = json.dumps(stored)
    manager.path.write_text(original, encoding="utf-8")

    state = manager.record_start()

    assert state["pending_missed_todo_ids"] == pending
    assert state["trigger_catchup_paused"] is paused
    on_disk = json.loads(manager.path.read_text(encoding="utf-8"))
    assert on_disk["pending_missed_todo_ids"] == pending
    assert on_disk["last_started_at"] == state["last_started_at"]
    assert isinstance(on_disk["last_started_at"], str)
    assert [q.read_text(encoding="utf-8") for q in _quarantined(tmp_path, "scheduler_state")] == [
        original
    ]
    assert recorders.audits[0][0] == "scheduler_state"


def test_set_pending_missed_saves_only_valid_types(tmp_path):
    manager = SchedulerStateManager(tmp_path)
    manager.set_pending_missed(["a", "a", "", "b"], trigger_catchup_paused=1)
    assert manager.load()["pending_missed_todo_ids"] == ["a", "b"]
    assert manager.load()["trigger_catchup_paused"] is True
    assert _quarantined(tmp_path, "scheduler_state") == []


def test_a_preserved_copy_is_no_wider_than_the_token_file(tmp_path, recorders):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-ALICE", "android", "alice")
    path = _tokens_path(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("[{broken", encoding="utf-8")  # keeps the file's mode
    fcm.load_tokens(data_dir)
    (copy,) = _quarantined(tmp_path, "fcm_tokens")
    assert copy.stat().st_mode & 0o777 == 0o600


def test_a_string_thread_filter_is_not_a_usable_registration(tmp_path, recorders):
    data_dir = str(tmp_path)
    path = _tokens_path(tmp_path)
    path.write_text(
        json.dumps([{"token": "A", "user_id": "u", "thread_ids": "thread-1"}, {"token": "B", "user_id": "u"}]),
        encoding="utf-8",
    )
    assert [e["token"] for e in fcm.load_tokens(data_dir)] == ["B"]
    assert len(_quarantined(tmp_path, "fcm_tokens")) == 1


def test_nested_capability_corruption_no_longer_stops_recording(tmp_path, recorders):
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.path.write_text(
        json.dumps(
            {
                "alice": {
                    "t1": {
                        "tools": {
                            "bad": {"use_count": "many"},
                            "good": {"use_count": 2, "last_used_at": "2026-09-01T00:00:00+00:00"},
                        },
                        "skills": ["not", "a", "map"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    usage.record(user_id="alice", thread_id="t1", tools=["new"], skills=["s"])

    assert usage.get_tool(user_id="alice", thread_id="t1", name="good").use_count == 2
    assert usage.get_tool(user_id="alice", thread_id="t1", name="bad").use_count == 0
    assert usage.get_tool(user_id="alice", thread_id="t1", name="new").use_count == 1
    assert usage.get_skill(user_id="alice", thread_id="t1", name="s").use_count == 1
    assert len(_quarantined(tmp_path, "capability_usage")) == 1


def test_an_invalid_upsert_is_refused_not_saved(tmp_path, recorders):
    manager = _metadata(tmp_path)
    _seed_threads(manager)
    with pytest.raises(Exception):
        manager.upsert_thread(USER, "thread-a", platform_meta={"k": 5})
    assert len(manager.list_threads(USER)) == 3
    assert _quarantined(manager.metadata_dir, USER) == []
    assert recorders.alerts == []


def test_incidental_metadata_writes_are_skipped_not_raised(caplog):
    from nymeria.core.thread_metadata import incidental_metadata_write

    with caplog.at_level("WARNING", logger="nymeria.core.thread_metadata"):
        with incidental_metadata_write("thread row for a test"):
            raise ThreadMetadataUnavailableError("list unreadable")
    assert any("thread row for a test" in r.getMessage() for r in caplog.records)
    with pytest.raises(ValueError):
        with incidental_metadata_write("anything else propagates"):
            raise ValueError("not a refusal")


def test_clear_still_deletes_checkpoints_when_the_thread_list_is_unavailable(monkeypatch):
    import asyncio

    from nymeria.core.thread_deletion import clear_thread_history
    from nymeria.core.thread_lock_manager import ThreadLockManager

    removed: list[str] = []

    async def _state(config):
        return SimpleNamespace(values={"messages": []})

    def _refuse(*args):
        raise ThreadMetadataUnavailableError("list unreadable")

    host = SimpleNamespace(
        _thread_locks=ThreadLockManager(),
        _default_async_graph=SimpleNamespace(aget_state=_state),
        _flush_memories_before_trim=lambda *a: None,
        thread_metadata_manager=SimpleNamespace(delete_thread=_refuse),
    )
    monkeypatch.setattr(
        "nymeria.core.checkpoint_cleanup.delete_thread_checkpoints",
        lambda *args: removed.append("checkpoints"),
    )
    asyncio.run(clear_thread_history(host, SimpleNamespace(), "u1", "clear-while-unavailable"))
    assert removed == ["checkpoints"]


# -- "the file changed during its repair", for every store ------------------


def _race_with(monkeypatch, newer: bytes) -> None:
    """Another writer replaces the file between the read and the replace."""

    def _raced(path: Path, raw: bytes, content: str, **kwargs):
        quarantine = store_repair.quarantine_copy(path, raw)
        path.write_bytes(newer)
        return store_repair.RepairResult(quarantine, False, True)

    monkeypatch.setattr(store_repair, "replace_preserving", _raced)


def _race_thread_metadata(tmp_path):
    manager = _metadata(tmp_path)
    manager.set_title(USER, "t-a", "Kept")
    return (
        _metadata_path(manager),
        lambda: {m.thread_id for m in manager.list_threads(USER)} == {"t-a"},
        lambda: manager.set_title(USER, "t-b", "After"),
        lambda: set(_rows_on_disk(manager)) == {"t-a", "t-b"},
        manager.metadata_dir,
        USER,
    )


def _race_notifications(tmp_path):
    store = NotificationStore(tmp_path)
    store.create(USER, "Kept")
    return (
        _notification_path(store),
        lambda: [n.summary for n in store.get_all(USER)] == ["Kept"],
        lambda: store.create(USER, "After"),
        lambda: {n.summary for n in store.get_all(USER)} == {"Kept", "After"},
        store.notifications_dir,
        USER,
    )


def _race_fcm(tmp_path):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-A", "android", USER)
    return (
        _tokens_path(tmp_path),
        lambda: [e["token"] for e in fcm.load_tokens(data_dir)] == ["TOKEN-A"],
        lambda: fcm.register_token(data_dir, "TOKEN-B", "ios", USER),
        lambda: [e["token"] for e in fcm.load_tokens(data_dir)] == ["TOKEN-A", "TOKEN-B"],
        tmp_path,
        "fcm_tokens",
    )


def _race_workflow_state(tmp_path):
    path = tmp_path / "wf-1" / f"{USER}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"k": "kept"}), encoding="utf-8")

    def _write() -> None:
        document = verbs_state._load_document(path, for_write=True)
        document["k2"] = "after"
        verbs_state._store_document(path, document, 100_000)

    return (
        path,
        lambda: verbs_state._load_document(path) == {"k": "kept"},
        _write,
        lambda: json.loads(path.read_text(encoding="utf-8")) == {"k": "kept", "k2": "after"},
        path.parent,
        USER,
    )


def _race_scheduler_state(tmp_path):
    manager = SchedulerStateManager(tmp_path)
    manager.set_pending_missed(["kept"], trigger_catchup_paused=True)
    return (
        manager.path,
        lambda: manager.load()["pending_missed_todo_ids"] == ["kept"],
        manager.record_start,
        lambda: json.loads(manager.path.read_text(encoding="utf-8"))["pending_missed_todo_ids"]
        == ["kept"]
        and manager.load()["last_started_at"] is not None,
        tmp_path,
        "scheduler_state",
    )


def _race_capability_usage(tmp_path):
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.record(user_id=USER, thread_id="t", tools=["kept"])
    return (
        usage.path,
        lambda: usage.get_tool(user_id=USER, thread_id="t", name="kept").use_count == 1,
        lambda: usage.record(user_id=USER, thread_id="t", tools=["kept"]),
        lambda: usage.get_tool(user_id=USER, thread_id="t", name="kept").use_count == 2,
        tmp_path,
        "capability_usage",
    )


@pytest.mark.parametrize(
    "setup",
    [
        _race_thread_metadata,
        _race_notifications,
        _race_fcm,
        _race_workflow_state,
        _race_scheduler_state,
        _race_capability_usage,
    ],
    ids=["thread_metadata", "notifications", "fcm", "workflow_state", "scheduler", "capability"],
)
def test_a_file_replaced_during_repair_is_kept_by_every_store(
    tmp_path, recorders, monkeypatch, setup
):
    path, loads_newer, write, written_on_top, store_dir, stem = setup(tmp_path)
    newer = path.read_bytes()
    path.write_text("{not json", encoding="utf-8")
    _race_with(monkeypatch, newer)

    assert loads_newer()
    write()
    assert written_on_top()
    assert [q.read_text(encoding="utf-8") for q in _quarantined(store_dir, stem)] == ["{not json"]
    assert any("changed during its repair" in detail for _, detail, _ in recorders.audits)


# -- an UNREADABLE file (OSError) is never written, in every store ----------


def test_an_unreadable_history_records_nothing_and_refuses_changes(tmp_path, recorders, monkeypatch):
    store = NotificationStore(tmp_path)
    first = store.create(USER, "Flight rebooked")
    path = _notification_path(store)
    before = path.read_bytes()
    denied = _deny_reads(monkeypatch, path)

    assert store.create(USER, "Delivered anyway").summary == "Delivered anyway"
    assert store.get_unread(USER) == []
    with pytest.raises(NotificationsUnavailableError):
        store.mark_read(first.id, USER)
    denied["on"] = False
    assert path.read_bytes() == before


def test_an_unreadable_token_file_refuses_changes(tmp_path, recorders, monkeypatch):
    data_dir = str(tmp_path)
    fcm.register_token(data_dir, "TOKEN-ALICE", "android", "alice")
    path = _tokens_path(tmp_path)
    before = path.read_bytes()
    denied = _deny_reads(monkeypatch, path)

    assert fcm.load_tokens(data_dir) == []
    with pytest.raises(fcm.PushTokensUnavailableError):
        fcm.register_token(data_dir, "TOKEN-BOB", "ios", "bob")
    denied["on"] = False
    assert path.read_bytes() == before


def test_an_unreadable_capability_index_is_not_recorded_over(tmp_path, recorders, monkeypatch):
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.record(user_id="alice", thread_id="t1", tools=["bash_execute"])
    before = usage.path.read_bytes()
    denied = _deny_reads(monkeypatch, usage.path)

    usage.record(user_id="bob", thread_id="t9", tools=["web_search"])

    denied["on"] = False
    assert usage.path.read_bytes() == before


def test_an_unreadable_workflow_state_refuses_writes(tmp_path, monkeypatch):
    from nymeria.core.workflows.registry import VerbError

    path = tmp_path / "wf-1" / f"{USER}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"k": 1}), encoding="utf-8")
    denied = _deny_reads(monkeypatch, path)
    with pytest.raises(VerbError, match="unreadable"):
        verbs_state._load_document(path, for_write=True)
    denied["on"] = False
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": 1}


# -- a repair that keeps failing is not retried on every hot-path read -----


def test_a_failed_repair_is_not_retried_until_the_bytes_change_or_time_passes(
    tmp_path, recorders, monkeypatch
):
    attempts: list[Path] = []
    monkeypatch.setattr(
        store_repair, "quarantine_copy", lambda path, data: attempts.append(path) or None
    )
    usage = CapabilityUsageStore(tmp_path / "capability_usage.json")
    usage.path.write_text("{broken", encoding="utf-8")

    for _ in range(3):
        usage.record(user_id="bob", thread_id="t9", tools=["web_search"])
    assert len(attempts) == 1  # one copy attempt, not one per tool call

    usage.path.write_text("{broken differently", encoding="utf-8")
    usage.record(user_id="bob", thread_id="t9", tools=["web_search"])
    assert len(attempts) == 2  # new bytes: tried again

    monkeypatch.setattr(store_repair, "REPAIR_RETRY_SECONDS", 0.0)
    usage.record(user_id="bob", thread_id="t9", tools=["web_search"])
    assert len(attempts) == 3  # the window passed: tried again
    assert usage.path.read_text(encoding="utf-8") == "{broken differently"
