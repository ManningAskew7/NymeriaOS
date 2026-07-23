"""Tests for the callable-team entity store (backlog #100 phase 1).

Covers store CRUD, lazy migration from legacy config name pairs (first-seen
wins on drift), quarantine of corrupt files, fingerprint-cached reads with
external-edit reload, the hot-load poll, dangling-id adoption, the shared
membership scan, and the shared team-list serializer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.team_manager import (
    TeamManager,
    make_team_id,
    serialize_thread_teams,
)
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager


def _build(tmp_path: Path, owned: list[str] | None = None):
    config_manager = ThreadConfigManager(tmp_path)
    owned_ids = list(owned or [])
    repo = SimpleNamespace(list_threads_for_user=lambda _user_id: list(owned_ids))
    manager = TeamManager(
        tmp_path,
        thread_config_manager=config_manager,
        accounts_repo=repo,
    )
    return manager, config_manager, repo


def test_make_team_id_format():
    team_id = make_team_id("Ops Team!")
    assert team_id.startswith("team-ops-team-")
    assert len(team_id.rsplit("-", 1)[-1]) == 8


def test_create_rename_describe_delete_roundtrip(tmp_path: Path):
    manager, _, _ = _build(tmp_path)

    team = manager.create_team("owner", name="  Ops   Team ", description=" helpers ")
    assert team.name == "Ops Team"  # whitespace-normalized
    assert team.description == "helpers"
    assert manager.get_team("owner", team.id) is not None
    # Empty team is a legal standing state: entity with no members.
    assert manager.members("owner", team.id) == []

    with pytest.raises(ValueError):
        manager.create_team("owner", name="ops team")  # case-insensitive collision

    other = manager.create_team("owner", name="Sales")
    with pytest.raises(ValueError):
        manager.rename_team("owner", other.id, "OPS TEAM")

    renamed = manager.rename_team("owner", team.id, "Operations")
    assert renamed is not None and renamed.name == "Operations"
    assert manager.resolve_team_name("owner", team.id) == "Operations"
    assert manager.rename_team("owner", "missing-id", "X") is None

    described = manager.describe_team("owner", team.id, "  shared ops  ")
    assert described is not None and described.description == "shared ops"
    cleared = manager.describe_team("owner", team.id, "")
    assert cleared is not None and cleared.description is None

    assert manager.delete_team("owner", team.id) is True
    assert manager.delete_team("owner", team.id) is False
    assert manager.get_team("owner", team.id) is None
    assert manager.get_team("owner", other.id) is not None


def test_lazy_migration_first_seen_name_wins(tmp_path: Path):
    manager, config_manager, _ = _build(
        tmp_path, owned=["thread-a", "thread-b", "thread-c", "thread-d"]
    )
    # Legacy on-disk configs: names still stored, with drift on team-legacy.
    config_manager.save_config(
        ThreadConfig(thread_id="thread-a", callable_team_id="team-legacy", callable_team_name="Ops")
    )
    config_manager.save_config(
        ThreadConfig(thread_id="thread-b", callable_team_id="team-legacy", callable_team_name="Operations")
    )
    config_manager.save_config(
        ThreadConfig(thread_id="thread-c", callable_team_id="team-nameless")
    )
    config_manager.save_config(ThreadConfig(thread_id="thread-d"))

    store = manager.get_store_cached("owner")
    by_id = {team.id: team for team in store.teams}
    assert by_id["team-legacy"].name == "Ops"  # thread-a first in sorted order
    assert by_id["team-nameless"].name == "team-nameless"  # id fallback
    assert set(by_id) == {"team-legacy", "team-nameless"}
    # Migration wrote the store file once; configs are untouched.
    assert (tmp_path / "teams" / "owner.json").exists()
    assert config_manager.get_config("thread-a").callable_team_name == "Ops"

    # Idempotent: a later config edit does not re-run migration.
    config_manager.save_config(
        ThreadConfig(thread_id="thread-d", callable_team_id="team-late", callable_team_name="Late")
    )
    manager._migrated.clear()  # simulate a fresh process
    manager.ensure_migrated("owner")
    assert manager.get_team("owner", "team-late") is None


def test_migration_writes_empty_marker_for_teamless_user(tmp_path: Path):
    manager, _, _ = _build(tmp_path, owned=["thread-a"])
    store = manager.get_store_cached("owner")
    assert store.teams == []
    assert (tmp_path / "teams" / "owner.json").exists()


def test_corrupt_store_quarantined_and_replaced_empty(tmp_path: Path):
    manager, _, _ = _build(tmp_path)
    path = tmp_path / "teams" / "owner.json"
    path.write_text("{not json", encoding="utf-8")

    store = manager.get_store_cached("owner")
    assert store.teams == []
    quarantine_dir = tmp_path / "teams" / "quarantine"
    assert quarantine_dir.exists() and any(quarantine_dir.iterdir())


def test_fingerprint_cache_serves_and_reloads_external_edits(tmp_path: Path):
    manager, _, _ = _build(tmp_path)
    manager.create_team("owner", name="Ops")

    first = manager.get_store_cached("owner")
    assert manager.get_store_cached("owner") is first  # cache hit, same object

    # Raw on-disk edit (different byte size so the fingerprint must change).
    path = tmp_path / "teams" / "owner.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["teams"][0]["name"] = "Ops Renamed Externally"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    reloaded = manager.get_store_cached("owner")
    assert reloaded is not first
    assert reloaded.teams[0].name == "Ops Renamed Externally"


def test_poll_external_changes_flags_raw_edits_only(tmp_path: Path):
    manager, _, _ = _build(tmp_path)
    manager.create_team("owner", name="Ops")

    assert manager.poll_external_changes() is False  # first poll primes
    manager._last_poll = 0.0  # bypass debounce for the test

    # Manager write: snapshot updated in _save, not an external change.
    manager.create_team("owner", name="Sales")
    assert manager.poll_external_changes() is False
    manager._last_poll = 0.0

    # Raw write: flagged.
    path = tmp_path / "teams" / "owner.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["teams"][0]["description"] = "edited on disk, longer content"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert manager.poll_external_changes() is True
    manager._last_poll = 0.0
    assert manager.poll_external_changes() is False  # steady state again


def test_ensure_team_exists_adopts_dangling_id(tmp_path: Path):
    manager, _, _ = _build(tmp_path)
    adopted = manager.ensure_team_exists("owner", "team-raw", fallback_name="Raw")
    assert adopted.name == "Raw"
    again = manager.ensure_team_exists("owner", "team-raw", fallback_name="Other")
    assert again.name == "Raw"  # idempotent, existing entity wins
    nameless = manager.ensure_team_exists("owner", "team-bare")
    assert nameless.name == "team-bare"


def test_membership_scan_and_thread_ids_override(tmp_path: Path):
    manager, config_manager, _ = _build(tmp_path, owned=["thread-a", "thread-b"])
    config_manager.save_config(
        ThreadConfig(thread_id="thread-a", callable_team_id="team-x")
    )
    config_manager.save_config(
        ThreadConfig(thread_id="thread-b", callable_team_id="team-x")
    )
    config_manager.save_config(
        ThreadConfig(thread_id="thread-z", callable_team_id="team-x")
    )

    # Default universe: the accounts repo listing (thread-z is not owned).
    assert manager.members("owner", "team-x") == ["thread-a", "thread-b"]
    # Explicit universe override (the CLI local transport's case).
    assert manager.all_members("owner", thread_ids=["thread-z"]) == {
        "team-x": ["thread-z"]
    }


def test_serialize_thread_teams_store_plus_dangling(tmp_path: Path):
    manager, config_manager, repo = _build(tmp_path, owned=["thread-a", "thread-b"])
    team = manager.create_team("owner", name="Ops", description="helpers")
    config_manager.save_config(
        ThreadConfig(thread_id="thread-a", callable_team_id=team.id)
    )
    # Dangling membership: id absent from the store, legacy name survives.
    config_manager.save_config(
        ThreadConfig(
            thread_id="thread-b",
            callable_team_id="team-raw",
            callable_team_name="Raw Legacy",
        )
    )
    agent = SimpleNamespace(
        team_manager=manager,
        thread_config_manager=config_manager,
        accounts_repo=repo,
    )

    result = serialize_thread_teams(agent, "owner")
    assert result["total"] == 2
    by_id = {entry["id"]: entry for entry in result["teams"]}
    assert by_id[team.id]["name"] == "Ops"
    assert by_id[team.id]["description"] == "helpers"
    assert by_id[team.id]["thread_ids"] == ["thread-a"]
    assert by_id["team-raw"]["name"] == "Raw Legacy"
    assert by_id["team-raw"]["thread_ids"] == ["thread-b"]


def test_serialize_thread_teams_degraded_without_manager(tmp_path: Path):
    """Agents without a team_manager (test fakes) fall back to a config scan."""
    config_manager = ThreadConfigManager(tmp_path)
    config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            callable_team_id="ops",
            callable_team_name="Ops",
        )
    )
    agent = SimpleNamespace(
        thread_config_manager=config_manager,
        accounts_repo=SimpleNamespace(
            list_threads_for_user=lambda _user_id: ["thread-a"]
        ),
    )
    result = serialize_thread_teams(agent, "owner")
    assert result["teams"] == [
        {"id": "ops", "name": "Ops", "description": None, "thread_ids": ["thread-a"]}
    ]
