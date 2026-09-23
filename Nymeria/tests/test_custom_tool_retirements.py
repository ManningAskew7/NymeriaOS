"""Tests for the custom-tool retirement record (#391): the tombstone that
keeps a retired id from being silently re-published by someone else.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nymeria.core.custom_tool_retirements import (
    CustomToolRetirementsRepo,
    retirement_refusal,
)


def test_record_get_clear_round_trip(tmp_path: Path):
    repo = CustomToolRetirementsRepo(tmp_path / "accounts.db")
    assert repo.get("foo") is None

    repo.record("foo", retired_by="u1")
    rec = repo.get("foo")
    assert rec is not None
    assert rec.tool_id == "foo"
    assert rec.retired_by == "u1"
    assert rec.retired_at.endswith("+00:00")

    assert repo.clear("foo") is True
    assert repo.get("foo") is None
    assert repo.clear("foo") is False


def test_a_second_retire_overwrites_the_actor(tmp_path: Path):
    repo = CustomToolRetirementsRepo(tmp_path / "accounts.db")
    repo.record("foo", retired_by="u1")
    repo.record("foo", retired_by="u2")
    assert repo.get("foo").retired_by == "u2"


def test_pre_upgrade_database_gains_the_table_on_init(tmp_path: Path):
    db = tmp_path / "accounts.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        conn.commit()
    repo = CustomToolRetirementsRepo(db)
    repo.record("foo", retired_by="u1")
    assert repo.get("foo").retired_by == "u1"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_refusal_sentence_names_who_when_why_and_the_way_out(tmp_path: Path):
    repo = CustomToolRetirementsRepo(tmp_path / "accounts.db")
    rec = repo.record("foo", retired_by="u1")
    day = rec.retired_at[:10]
    assert len(day) == 10 and day.count("-") == 2

    assert retirement_refusal(rec, "foo") == (
        f"tool_id 'foo' was retired by 'u1' on {day}. Drafting or publishing under "
        "a retired id is refused because every thread that still lists the name "
        "would bind the new tool silently (a draft under it could never publish). "
        "Choose another id, or ask an admin to release the reservation "
        "(DELETE /tools/custom/foo/retirement)."
    )
