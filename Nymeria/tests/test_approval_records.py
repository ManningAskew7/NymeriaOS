"""Unit tests for the shared ApprovalRecordStore (backlog #106).

The three feature suites (test_hook_approvals, test_workflow_approvals,
test_fallback_consent's store section) pin the delegated per-feature
behavior; this file covers the shared store mechanics once.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

from nymeria.core.approval_records import ApprovalRecordStore, clamp_window
from nymeria.core.notifications import notify_user_best_effort
from nymeria.core.time_utils import utc_now


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, fcm_enabled=False),
    )
    return ApprovalRecordStore("things/pending", noun="thing approval")


def _record(record_id: str, *, user_id: str = "u1", expires_at=None) -> dict:
    record = {"record_id": record_id, "user_id": user_id}
    if expires_at is not None:
        record["expires_at"] = expires_at
    return record


# --- path + write/load/delete ----------------------------------------------


def test_write_load_delete_round_trip(store, tmp_path):
    store.write(_record("abc-1", expires_at="2030-01-01T00:00:00+00:00"))
    path = tmp_path / "things" / "pending" / "abc-1.json"
    assert path.is_file()
    loaded = store.load("abc-1")
    assert loaded is not None and loaded["record_id"] == "abc-1"
    assert store.load("missing") is None
    store.delete("abc-1")
    assert not path.exists()
    store.delete("abc-1")  # idempotent


def test_record_path_sanitizes_hostile_ids(store):
    path = store.record_path("../../evil")
    assert path.parent == store.dir()
    assert path.name == "evil.json"
    assert store.record_path("a/b..c_d-e").name == "abc_d-e.json"


def test_write_leaves_no_temp_litter(store):
    store.write(_record("r1"))
    assert [p.name for p in store.dir().glob("*.tmp")] == []


def test_load_rejects_corrupt_and_non_dict(store):
    store.dir().mkdir(parents=True, exist_ok=True)
    (store.dir() / "corrupt.json").write_text("{not json", encoding="utf-8")
    (store.dir() / "listy.json").write_text("[1, 2]", encoding="utf-8")
    assert store.load("corrupt") is None
    assert store.load("listy") is None


# --- list -------------------------------------------------------------------


def test_list_newest_first_filters_user_and_skips_corrupt(store):
    for index, (record_id, user_id) in enumerate(
        [("old", "u1"), ("mid", "u2"), ("new", "u1")]
    ):
        store.write(_record(record_id, user_id=user_id))
        stamp = 1_700_000_000 + index * 60
        os.utime(store.record_path(record_id), (stamp, stamp))
    (store.dir() / "junk.json").write_text("{not json", encoding="utf-8")
    os.utime(store.dir() / "junk.json", (1_700_000_000 + 999, 1_700_000_000 + 999))

    assert [r["record_id"] for r in store.list()] == ["new", "mid", "old"]
    assert [r["record_id"] for r in store.list("u1")] == ["new", "old"]
    assert store.list("nobody") == []


def test_list_on_missing_dir_is_empty(store):
    assert store.list() == []


# --- expiry primitives ------------------------------------------------------


def test_sweep_stale_removes_past_slack_and_junk_expiry(store):
    now = utc_now()
    store.write(_record("fresh", expires_at=(now + timedelta(hours=1)).isoformat()))
    store.write(_record("grace", expires_at=(now - timedelta(seconds=10)).isoformat()))
    store.write(_record("old", expires_at=(now - timedelta(hours=1)).isoformat()))
    store.write(_record("junk", expires_at="not-a-date"))
    store.write(_record("bare"))  # no expires_at at all

    assert store.sweep_stale(300) == 3  # old + junk + bare
    remaining = sorted(r["record_id"] for r in store.list())
    assert remaining == ["fresh", "grace"]


def test_sweep_stale_deletes_idless_records_by_path(store):
    """A stale record whose JSON lacks record_id must be DELETED, not counted
    as swept every pass while its file survives (delete-by-path contract)."""
    store.dir().mkdir(parents=True, exist_ok=True)
    ghost = store.dir() / "ghost.json"
    ghost.write_text(json.dumps({"user_id": "u1"}), encoding="utf-8")

    assert store.sweep_stale(300) == 1
    assert not ghost.exists()
    assert store.sweep_stale(300) == 0  # nothing left to re-count


def test_expired_lists_only_valid_past_expiry(store):
    now = utc_now()
    store.write(_record("fresh", expires_at=(now + timedelta(hours=1)).isoformat()))
    store.write(_record("done", expires_at=(now - timedelta(seconds=5)).isoformat()))
    store.write(_record("junk", expires_at="not-a-date"))

    assert [r["record_id"] for r in store.expired()] == ["done"]


# --- helpers ----------------------------------------------------------------


def test_clamp_window_bounds_and_junk():
    kwargs = dict(default=180.0, minimum=10.0, maximum=600.0)
    assert clamp_window("nope", **kwargs) == 180.0
    assert clamp_window(None, **kwargs) == 180.0
    assert clamp_window(1, **kwargs) == 10.0
    assert clamp_window(10_000, **kwargs) == 600.0
    assert clamp_window("42.5", **kwargs) == 42.5


def test_notify_truncates_row_but_pushes_full_text(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, fcm_enabled=True),
    )
    rows: list[dict] = []
    pushes: list[dict] = []
    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification",
        lambda **kwargs: rows.append(kwargs),
    )
    monkeypatch.setitem(
        sys.modules,
        "nymeria.core.fcm",
        SimpleNamespace(send_to_all_devices=lambda **kwargs: pushes.append(kwargs)),
    )
    long_summary = "x" * 300
    notify_user_best_effort("u1", long_summary, thread_id="t1", push=True)
    assert rows[0]["summary"] == "x" * 200
    assert rows[0]["thread_id"] == "t1"
    assert pushes[0]["text"] == long_summary

    pushes.clear()
    notify_user_best_effort("u1", "short", thread_id=None, push=False)
    assert pushes == []  # push off skips FCM even when enabled


def test_notify_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, fcm_enabled=False),
    )

    def _boom(**kwargs):
        raise RuntimeError("delivery down")

    monkeypatch.setattr("nymeria.core.notifications.create_notification", _boom)
    notify_user_best_effort("u1", "summary", push=True)  # must not raise


# --- serialization parity ---------------------------------------------------


def test_write_serializes_non_json_values_via_default_str(store):
    store.write(_record("stamped", expires_at=utc_now()))  # datetime object
    raw = json.loads(store.record_path("stamped").read_text(encoding="utf-8"))
    assert isinstance(raw["expires_at"], str)
