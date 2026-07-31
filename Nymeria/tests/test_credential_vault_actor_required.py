"""The credential-vault read path requires an explicit actor.

Before this, ``get_secret_field``/``get_secret_fields_for_test``/
``resolve_references`` took ``actor_user_id: Optional[str] = None`` and the
owner check began with "no actor supplied, so allow". Every caller that simply
did not pass the argument therefore decrypted any user's secret, and the audit
log recorded ``None`` for who asked.

The vector that made this more than theoretical: OAuth caches migrated into the
vault land as ``cred_lcache_*`` records with an EMPTY ``allowed_targets`` list
(so target scoping does not narrow them either) and hold live refresh tokens.
Their ids are derived from the account label, so a caller who can reach any
unattributed read path does not need to enumerate anything.

These tests pin the two halves of the fix: the read gate has no "no actor"
branch, and omitting the argument is a ``TypeError`` at the call site rather
than a silent allow.
"""

from __future__ import annotations

import sqlite3

import pytest
from _service_integration_helpers import (  # type: ignore[import-not-found]
    make_vault_repo,
)

from nymeria.core.credential_vault import (
    SYSTEM_ACTOR,
    CredentialAccessDenied,
)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    return make_vault_repo(tmp_path, monkeypatch)


@pytest.fixture()
def alice_oauth_cache(repo):
    """An unscoped, user-owned record shaped like a migrated OAuth cache."""
    return repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Legacy OAuth cache",
        provider="google",
        kind="oauth_cache",
        secret_fields={"refresh_token": "alice-refresh-token"},
        allowed_targets=[],
        credential_id="cred_lcache_alice_google",
    )


def test_reading_another_users_secret_is_denied(repo, alice_oauth_cache):
    """The case the old ``None`` default silently allowed."""
    with pytest.raises(CredentialAccessDenied):
        repo.get_secret_field(
            alice_oauth_cache.id, "refresh_token", actor="mallory"
        )


def test_the_owner_still_reads_their_own_secret(repo, alice_oauth_cache):
    assert (
        repo.get_secret_field(alice_oauth_cache.id, "refresh_token", actor="alice")
        == "alice-refresh-token"
    )


def test_empty_allowed_targets_does_not_narrow_the_record(repo, alice_oauth_cache):
    """Why the actor check has to carry this on its own.

    An empty ``allowed_targets`` currently means "no target restriction", so
    target scoping contributes nothing here and the owner check is the only
    thing standing between a caller and the refresh token. If this assertion
    ever flips (empty meaning deny), that is a deliberate hardening and this
    test should be updated to match, not deleted.
    """
    assert alice_oauth_cache.allowed_targets == []
    assert (
        repo.get_secret_field(
            alice_oauth_cache.id,
            "refresh_token",
            actor="alice",
            target_type="native_tool",
            target_id="anything_at_all",
        )
        == "alice-refresh-token"
    )


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda repo, cid: repo.get_secret_field(cid, "refresh_token"),
            id="get_secret_field",
        ),
        pytest.param(
            lambda repo, cid: repo.get_secret_fields_for_test(cid),
            id="get_secret_fields_for_test",
        ),
        pytest.param(
            lambda repo, cid: repo.resolve_references(
                f"${{credential:{cid}.refresh_token}}"
            ),
            id="resolve_references",
        ),
    ],
)
def test_omitting_the_actor_is_an_error_not_an_allow(repo, alice_oauth_cache, call):
    """The load-bearing half: you cannot reach plaintext by saying nothing.

    A ``TypeError`` here is the point. It means a new unattributed read cannot
    be introduced by forgetting a keyword argument, only by explicitly passing
    ``SYSTEM_ACTOR``, which review and grep can both see.
    """
    with pytest.raises(TypeError, match="actor"):
        call(repo, alice_oauth_cache.id)


def test_system_actor_is_the_documented_way_to_opt_out(repo, alice_oauth_cache):
    """Server-startup paths with no user principal still work, visibly."""
    assert (
        repo.get_secret_field(
            alice_oauth_cache.id, "refresh_token", actor=SYSTEM_ACTOR
        )
        == "alice-refresh-token"
    )


def _read_event_actors(repo, credential_id: str) -> set[str | None]:
    """Actors recorded against secret READS ("used") of one credential.

    Deliberately not every event for the credential: the mutation path still
    takes an optional ``actor_user_id``, so a fixture that creates a record
    without naming an actor legitimately leaves a ``None`` "created" row. This
    pass hardened reads, and this is the set that pins it.
    """
    conn = sqlite3.connect(str(repo.db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT actor_user_id FROM credential_audit_events "
                "WHERE credential_id = ? AND event_type = 'used'",
                (credential_id,),
            )
        }
    finally:
        conn.close()


def test_every_secret_read_is_attributed_in_the_audit_log(repo, alice_oauth_cache):
    """``SYSTEM_ACTOR`` reads are attributable, where ``None`` reads were not."""
    repo.get_secret_field(alice_oauth_cache.id, "refresh_token", actor=SYSTEM_ACTOR)
    repo.get_secret_field(alice_oauth_cache.id, "refresh_token", actor="alice")
    assert _read_event_actors(repo, alice_oauth_cache.id) == {"__system__", "alice"}
