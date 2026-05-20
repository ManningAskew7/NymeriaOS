"""Tests for NotificationDestinationsRepo: CRUD, secrets, profile rename
cascade, profile delete cascade.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nymeria.core.notification_destinations import (
    DestinationAlreadyExists,
    DestinationNotFound,
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
