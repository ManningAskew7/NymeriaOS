"""Tests for NotificationDestinationsRepo: CRUD, secrets, profile rename
cascade, profile delete cascade.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nymeria.core.notification_destinations import (
    DestinationAlreadyExists,
    DestinationNotFound,
    DestinationTombstoned,
    NotificationDestinationsRepo,
    ProfileAlreadyExists,
    ProfileNotFound,
)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> NotificationDestinationsRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    return NotificationDestinationsRepo(tmp_path / "accounts.db")


# -- destinations CRUD --------------------------------------------------------


def test_create_and_get_destination(repo):
    dest = repo.create_destination(
        user_id="alice",
        name="my-phone",
        type="telegram",
        config={"chat_id": "12345"},
    )
    assert dest.id
    assert dest.user_id == "alice"
    assert dest.name == "my-phone"
    assert dest.type == "telegram"
    assert dest.config == {"chat_id": "12345"}
    assert dest.enabled is True

    fetched = repo.get_destination(user_id="alice", dest_id=dest.id)
    assert fetched is not None
    assert fetched.id == dest.id
    by_name = repo.get_destination_by_name(user_id="alice", name="my-phone")
    assert by_name is not None
    assert by_name.id == dest.id


def test_duplicate_name_per_user_rejected(repo):
    repo.create_destination(user_id="alice", name="x", type="discord")
    with pytest.raises(DestinationAlreadyExists):
        repo.create_destination(user_id="alice", name="x", type="discord")


def test_same_name_different_user_allowed(repo):
    a = repo.create_destination(user_id="alice", name="phone", type="telegram")
    b = repo.create_destination(user_id="bob", name="phone", type="telegram")
    assert a.id != b.id


def test_secret_field_roundtrip(repo):
    dest = repo.create_destination(
        user_id="alice",
        name="hook",
        type="webhook",
        config={"url": "https://example.com/hook"},
        secret_fields={"bearer_token": "super-secret"},
    )
    plain = repo.get_secret_field(dest_id=dest.id, field_name="bearer_token")
    assert plain == "super-secret"
    field_names = repo.list_secret_field_names(dest_id=dest.id)
    assert field_names == ["bearer_token"]


def test_update_destination_partial(repo):
    dest = repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://old.example.com"},
    )
    updated = repo.update_destination(
        user_id="alice", dest_id=dest.id,
        config={"url": "https://new.example.com"},
        enabled=False,
    )
    assert updated.config == {"url": "https://new.example.com"}
    assert updated.enabled is False
    assert updated.name == "hook"  # unchanged


def test_update_destination_clears_secret(repo):
    dest = repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        secret_fields={"bearer_token": "v1"},
    )
    repo.update_destination(
        user_id="alice", dest_id=dest.id,
        secret_fields={"bearer_token": None},
    )
    assert repo.get_secret_field(dest_id=dest.id, field_name="bearer_token") is None
    assert repo.list_secret_field_names(dest_id=dest.id) == []


def test_update_destination_replaces_secret(repo):
    dest = repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        secret_fields={"bearer_token": "v1"},
    )
    repo.update_destination(
        user_id="alice", dest_id=dest.id,
        secret_fields={"bearer_token": "v2"},
    )
    assert repo.get_secret_field(dest_id=dest.id, field_name="bearer_token") == "v2"


def test_update_missing_destination_raises(repo):
    with pytest.raises(DestinationNotFound):
        repo.update_destination(
            user_id="alice", dest_id="nonexistent", name="anything",
        )


def test_delete_destination_cascades_secrets(repo):
    dest = repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        secret_fields={"bearer_token": "v"},
    )
    assert repo.delete_destination(user_id="alice", dest_id=dest.id) is True
    assert repo.get_destination(user_id="alice", dest_id=dest.id) is None
    # Secret field row should be gone via FK ON DELETE CASCADE.
    assert repo.list_secret_field_names(dest_id=dest.id) == []


def test_list_destinations_user_scoped(repo):
    repo.create_destination(user_id="alice", name="a1", type="discord")
    repo.create_destination(user_id="alice", name="a2", type="discord")
    repo.create_destination(user_id="bob", name="b1", type="discord")
    assert sorted(d.name for d in repo.list_destinations(user_id="alice")) == ["a1", "a2"]
    assert [d.name for d in repo.list_destinations(user_id="bob")] == ["b1"]


# -- profiles CRUD ------------------------------------------------------------


def test_create_and_get_profile(repo):
    profile = repo.create_profile(
        user_id="alice", name="default",
        destination_names=["my-phone", "work-email"],
    )
    assert profile.id
    assert profile.destination_names == ["my-phone", "work-email"]

    by_id = repo.get_profile(user_id="alice", profile_id=profile.id)
    assert by_id is not None
    assert by_id.name == "default"
    by_name = repo.get_profile_by_name(user_id="alice", name="default")
    assert by_name is not None
    assert by_name.id == profile.id


def test_duplicate_profile_name_per_user_rejected(repo):
    repo.create_profile(user_id="alice", name="default")
    with pytest.raises(ProfileAlreadyExists):
        repo.create_profile(user_id="alice", name="default")


def test_update_profile_destination_list(repo):
    profile = repo.create_profile(
        user_id="alice", name="default", destination_names=["a"],
    )
    updated = repo.update_profile(
        user_id="alice", profile_id=profile.id,
        destination_names=["a", "b", "c"],
    )
    assert updated.destination_names == ["a", "b", "c"]


def test_update_missing_profile_raises(repo):
    with pytest.raises(ProfileNotFound):
        repo.update_profile(user_id="alice", profile_id="nonexistent", name="x")


def test_delete_profile(repo):
    profile = repo.create_profile(user_id="alice", name="default")
    assert repo.delete_profile(user_id="alice", profile_id=profile.id) is True
    assert repo.get_profile(user_id="alice", profile_id=profile.id) is None


# -- cascade behaviour --------------------------------------------------------


def test_rename_destination_cascades_to_profiles(repo):
    dest = repo.create_destination(
        user_id="alice", name="phone", type="telegram",
    )
    repo.create_profile(
        user_id="alice", name="default", destination_names=["phone"],
    )
    repo.create_profile(
        user_id="alice", name="urgent", destination_names=["phone", "other"],
    )
    repo.update_destination(user_id="alice", dest_id=dest.id, name="my-phone")
    p1 = repo.get_profile_by_name(user_id="alice", name="default")
    p2 = repo.get_profile_by_name(user_id="alice", name="urgent")
    assert p1.destination_names == ["my-phone"]
    assert p2.destination_names == ["my-phone", "other"]


def test_delete_destination_cascades_removal_from_profiles(repo):
    dest = repo.create_destination(
        user_id="alice", name="phone", type="telegram",
    )
    repo.create_profile(
        user_id="alice", name="default",
        destination_names=["phone", "email"],
    )
    repo.delete_destination(user_id="alice", dest_id=dest.id)
    p = repo.get_profile_by_name(user_id="alice", name="default")
    assert p.destination_names == ["email"]


def test_destination_rename_only_affects_owner(repo):
    a = repo.create_destination(user_id="alice", name="phone", type="telegram")
    repo.create_destination(user_id="bob", name="phone", type="telegram")
    repo.create_profile(
        user_id="bob", name="default", destination_names=["phone"],
    )
    repo.update_destination(user_id="alice", dest_id=a.id, name="alice-phone")
    bob_profile = repo.get_profile_by_name(user_id="bob", name="default")
    assert bob_profile.destination_names == ["phone"]


# -- tombstones (#263) --------------------------------------------------------
#
# A deleted or renamed-away name leaves a tombstone so the auto-seed in
# notification_channels never resurrects a default the user removed. The
# invariant is that a tombstone never coexists with a live destination of
# that name: creating (or renaming onto) the name clears it.


def _assert_no_tombstone_shadows_a_live_destination(repo, user_id):
    live = {d.name for d in repo.list_destinations(user_id=user_id)}
    assert repo.tombstoned_destination_names(user_id=user_id) & live == set()


def test_delete_records_a_tombstone_and_recreate_clears_it(repo):
    dest = repo.create_destination(user_id="alice", name="telegram-default", type="telegram")
    assert repo.tombstoned_destination_names(user_id="alice") == set()

    repo.delete_destination(user_id="alice", dest_id=dest.id)
    assert repo.tombstoned_destination_names(user_id="alice") == {"telegram-default"}

    repo.create_destination(user_id="alice", name="telegram-default", type="telegram")
    assert repo.tombstoned_destination_names(user_id="alice") == set()
    _assert_no_tombstone_shadows_a_live_destination(repo, "alice")


def test_rename_tombstones_the_old_name_and_clears_the_new(repo):
    gone = repo.create_destination(user_id="alice", name="phone", type="telegram")
    repo.delete_destination(user_id="alice", dest_id=gone.id)
    dest = repo.create_destination(user_id="alice", name="telegram-default", type="telegram")

    repo.update_destination(user_id="alice", dest_id=dest.id, name="phone")

    assert repo.tombstoned_destination_names(user_id="alice") == {"telegram-default"}
    assert repo.get_destination_by_name(user_id="alice", name="phone") is not None
    _assert_no_tombstone_shadows_a_live_destination(repo, "alice")


def test_update_without_a_rename_leaves_tombstones_alone(repo):
    gone = repo.create_destination(user_id="alice", name="old", type="telegram")
    repo.delete_destination(user_id="alice", dest_id=gone.id)
    dest = repo.create_destination(user_id="alice", name="keep", type="telegram")

    repo.update_destination(user_id="alice", dest_id=dest.id, enabled=False)

    assert repo.tombstoned_destination_names(user_id="alice") == {"old"}
    _assert_no_tombstone_shadows_a_live_destination(repo, "alice")


def test_seed_create_honours_a_tombstone_written_after_the_seed_looked(repo):
    """The auto-seed reads tombstones, then creates. A delete that lands in
    between must win: the seed-flavoured create re-checks in-transaction and
    leaves the tombstone in place instead of lifting it.
    """
    dest = repo.create_destination(user_id="alice", name="telegram-default", type="telegram")
    repo.delete_destination(user_id="alice", dest_id=dest.id)

    with pytest.raises(DestinationTombstoned):
        repo.create_destination(
            user_id="alice", name="telegram-default", type="telegram", seed=True,
        )

    assert repo.tombstoned_destination_names(user_id="alice") == {"telegram-default"}
    assert repo.get_destination_by_name(user_id="alice", name="telegram-default") is None
    # Without a tombstone the seed create is an ordinary create.
    repo.create_destination(user_id="alice", name="discord-default", type="discord", seed=True)
    assert repo.get_destination_by_name(user_id="alice", name="discord-default") is not None


def test_tombstoned_names_can_be_scoped_to_a_name_set(repo):
    for name in ("a", "b", "c"):
        d = repo.create_destination(user_id="alice", name=name, type="discord")
        repo.delete_destination(user_id="alice", dest_id=d.id)

    assert repo.tombstoned_destination_names(user_id="alice", names=["b", "zzz"]) == {"b"}
    assert repo.tombstoned_destination_names(user_id="alice", names=[]) == set()
    assert repo.tombstoned_destination_names(user_id="alice") == {"a", "b", "c"}


def test_tombstones_are_per_user(repo):
    a = repo.create_destination(user_id="alice", name="telegram-default", type="telegram")
    repo.create_destination(user_id="bob", name="telegram-default", type="telegram")
    repo.delete_destination(user_id="alice", dest_id=a.id)

    assert repo.tombstoned_destination_names(user_id="alice") == {"telegram-default"}
    assert repo.tombstoned_destination_names(user_id="bob") == set()
    assert repo.get_destination_by_name(user_id="bob", name="telegram-default") is not None


def test_pre_upgrade_database_gains_the_tombstone_table_on_init(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    first = NotificationDestinationsRepo(db_path)
    dest = first.create_destination(user_id="alice", name="telegram-default", type="telegram")
    # Simulate an accounts.db written before the tombstone table existed.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE notification_destination_tombstones")
        conn.commit()

    upgraded = NotificationDestinationsRepo(db_path)
    assert upgraded.delete_destination(user_id="alice", dest_id=dest.id) is True
    assert upgraded.tombstoned_destination_names(user_id="alice") == {"telegram-default"}
