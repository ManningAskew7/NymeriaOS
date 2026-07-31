"""Empty ``allowed_targets`` denies, and the migration that made that safe.

The vault's target gate used to read an empty ``allowed_targets`` as "any call
site may resolve this credential". That made it vacuous on exactly the rows
that needed it most: nearly every creation path produced an empty list, and MCP
resolution skips the vault's owner check by design (it resolves as
SYSTEM_ACTOR), so for that one reader the target list is the ONLY gate.

Flipping empty to deny is a one-line change and a data disaster on its own,
because live rows relied on the old default. The failure would also be silent
rather than loud: ``load_token_cache`` swallows a denial at debug level, and
its on-disk fallback file is deleted after a successful vault save, so the user
is simply signed out of every OAuth integration with nothing in the log above
DEBUG.

So the flip is paired with a versioned migration that writes an explicit grant
onto every row that was relying on the default. These tests pin both halves and
the ordering between them.
"""

from __future__ import annotations

import sqlite3

import pytest
from _service_integration_helpers import (  # type: ignore[import-not-found]
    make_vault_repo,
)

from nymeria.core.credential_vault import (
    CredentialAccessDenied,
    CredentialVaultRepo,
)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    return make_vault_repo(tmp_path, monkeypatch)


def _raw_targets(db_path, credential_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT allowed_targets_json FROM credentials WHERE id = ?",
            (credential_id,),
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _force_empty(db_path, credential_id: str) -> None:
    """Recreate the pre-migration on-disk state.

    Written through raw SQL on purpose: no code path produces an empty list any
    more, so the only way to test the migration is to reproduce the shape it
    exists to fix. Clearing the ``schema_meta`` watermark is what un-stamps the
    migration; the vault deliberately does NOT use ``PRAGMA user_version``,
    because this database file is shared with three other repos and that
    counter is per-file, not per-component.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE credentials SET allowed_targets_json = '[]' WHERE id = ?",
            (credential_id,),
        )
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            ("credential_vault.schema_version",),
        )
        conn.commit()
    finally:
        conn.close()


# --- the gate ------------------------------------------------------------


def test_empty_allowed_targets_denies(repo):
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Unscoped",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    with pytest.raises(CredentialAccessDenied):
        repo.get_secret_field(
            record.id, "value", actor="alice", target_type="native_tool", target_id="x"
        )


def test_wildcard_still_allows_any_target(repo):
    """An intentionally unrestricted credential has to be able to say so."""
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Wildcard",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=["*"],
    )

    assert (
        repo.get_secret_field(
            record.id, "value", actor="alice", target_type="mcp_server", target_id="any"
        )
        == "s3cret"
    )


def test_a_scoped_credential_still_refuses_a_different_target(repo):
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Scoped",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=["native_tool:weather_check"],
    )

    assert repo.get_secret_field(
        record.id, "value", actor="alice",
        target_type="native_tool", target_id="weather_check",
    ) == "s3cret"
    with pytest.raises(CredentialAccessDenied):
        repo.get_secret_field(
            record.id, "value", actor="alice",
            target_type="mcp_server", target_id="anything",
        )


# --- the migration -------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("legacy_token_cache", ["native_auth_cache:*"]),
        ("oauth_cache", ["native_auth_cache:*"]),
        ("oauth_token", ["native_tool:*"]),
        ("service_account", ["native_tool:*"]),
        (
            "api_key",
            ["native_tool:*", "llm_provider:*", "thread:*", "custom_tool:*"],
        ),
        ("pat", ["native_tool:*", "llm_provider:*", "thread:*", "custom_tool:*"]),
        ("secret", ["native_tool:*", "llm_provider:*", "thread:*", "custom_tool:*"]),
        ("form", ["native_tool:*", "llm_provider:*", "thread:*", "custom_tool:*"]),
    ],
)
def test_migration_grants_the_union_of_a_kinds_readers(repo, kind, expected):
    """Each kind keeps every reader it had, and loses ``mcp_server``.

    The union rather than one precise target is deliberate: the goal is not to
    scope these perfectly, it is to stop a secret that was never an MCP
    credential being reachable through the one reader that skips the owner
    check. Granting the union cannot break a working deployment.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name=f"{kind} row",
        provider="manual",
        kind=kind,
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    migrated = CredentialVaultRepo(repo.db_path).get_credential(record.id)

    assert migrated is not None
    assert sorted(migrated.allowed_targets) == sorted(expected)
    assert "mcp_server:*" not in migrated.allowed_targets
    assert "*" not in migrated.allowed_targets


def test_migration_routes_an_mcp_credential_to_the_mcp_reader(repo):
    """Provider beats kind, for the one family whose reader IS the MCP path.

    MCP credentials share kinds ("secret", "env_var") with ordinary rows, so
    the kind table alone would hand this row ``native_tool:*`` and friends and
    strip the only target that actually reads it. Every MCP creation site names
    its targets, so this branch should match nothing in practice; it exists so
    that a row which did slip through empty fails toward still working rather
    than toward a silent, un-diagnosable MCP server failure.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="MCP secret",
        provider="mcp",
        kind="secret",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    migrated = CredentialVaultRepo(repo.db_path).get_credential(record.id)

    assert migrated is not None
    assert migrated.allowed_targets == ["mcp_server:*"]


def test_a_created_and_a_migrated_row_land_on_the_same_targets(repo):
    """The create default and the backfill must not drift apart.

    They answer one question ("which call sites read this kind"), so they share
    one resolver. They did not: the migration had a ``provider == "mcp"``
    override the create default lacked, so a freshly saved MCP credential was
    unreadable by MCP and readable by four paths it should never have been,
    while an identical migrated row was correct. Parametrised over the pair that
    actually diverged.
    """
    fresh = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Fresh MCP secret",
        provider="mcp",
        kind="secret",
        secret_fields={"value": "s3cret"},
    )
    legacy = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Legacy MCP secret",
        provider="mcp",
        kind="secret",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, legacy.id)

    migrated = CredentialVaultRepo(repo.db_path).get_credential(legacy.id)

    assert migrated is not None
    assert sorted(fresh.allowed_targets) == sorted(migrated.allowed_targets)
    assert fresh.allowed_targets == ["mcp_server:*"]


def test_migration_also_repairs_a_row_whose_target_json_is_blank(repo):
    """``''`` is the other shape an unset column takes on disk.

    The backfill matches ``IN ('[]', '')`` and only ``'[]'`` had coverage, so
    the second arm was untestable defensive code. A blank string is what a row
    written before the column had a default carries, and it reads back as an
    empty list, which now denies.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Blank targets",
        provider="manual",
        kind="oauth_token",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    conn = sqlite3.connect(str(repo.db_path))
    try:
        conn.execute(
            "UPDATE credentials SET allowed_targets_json = '' WHERE id = ?",
            (record.id,),
        )
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            ("credential_vault.schema_version",),
        )
        conn.commit()
    finally:
        conn.close()

    migrated = CredentialVaultRepo(repo.db_path).get_credential(record.id)

    assert migrated is not None
    assert migrated.allowed_targets == ["native_tool:*"]


def test_the_backfill_does_not_disturb_updated_at(repo):
    """The migration writes down existing reach; it is not a user-visible edit.

    ``updated_at`` is read as an ordering: the stale-OAuth sweep in
    ``auth_manager`` picks the survivor among duplicate rows with
    ``max(key=updated_at)``. Stamping every backfilled row with one timestamp
    collapses that into a tie, and the tie resolves to whichever row the query
    returned first, so the sweep can nominate the NEWER token for deletion.
    """
    first = repo.create_credential(
        owner_type="user", owner_user_id="alice", name="Older",
        provider="manual", kind="oauth_token",
        secret_fields={"value": "a"}, allowed_targets=[],
    )
    second = repo.create_credential(
        owner_type="user", owner_user_id="alice", name="Newer",
        provider="manual", kind="oauth_token",
        secret_fields={"value": "b"}, allowed_targets=[],
    )
    conn = sqlite3.connect(str(repo.db_path))
    try:
        conn.execute(
            "UPDATE credentials SET allowed_targets_json = '[]', updated_at = ? "
            "WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", first.id),
        )
        conn.execute(
            "UPDATE credentials SET allowed_targets_json = '[]', updated_at = ? "
            "WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", second.id),
        )
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            ("credential_vault.schema_version",),
        )
        conn.commit()
    finally:
        conn.close()

    migrated = CredentialVaultRepo(repo.db_path)

    older = migrated.get_credential(first.id)
    newer = migrated.get_credential(second.id)
    assert older is not None and newer is not None
    assert older.allowed_targets == ["native_tool:*"]
    assert older.updated_at == "2020-01-01T00:00:00+00:00"
    assert newer.updated_at == "2026-01-01T00:00:00+00:00"


def test_migration_covers_a_system_owned_row(repo):
    """Ownership is orthogonal to the backfill, and must stay that way.

    A system-owned row skips the owner check on EVERY reader, not just MCP, so
    it is the row where an unmigrated empty list would be most damaging: the
    flip would take it from readable-by-anything to readable-by-nothing.
    """
    record = repo.create_credential(
        owner_type="system",
        owner_user_id=None,
        name="System key",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    migrated = CredentialVaultRepo(repo.db_path)
    record_after = migrated.get_credential(record.id)

    assert record_after is not None
    assert "native_tool:*" in record_after.allowed_targets
    assert (
        migrated.get_secret_field(
            record.id, "value", actor="alice",
            target_type="native_tool", target_id="anything",
        )
        == "s3cret"
    )


def test_the_unclassified_fallback_names_the_rows_it_left_open(repo, caplog):
    """The wildcard fallback is only defensible if it is loud.

    A kind the table does not know keeps full reach, which is not a security
    win, it is a deferral. The log line is the entire mitigation, so it has to
    name the specific rows an operator should narrow by hand, and it has to be
    at WARNING: an INFO line about 32 rows scrolls past unread.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Exotic",
        provider="manual",
        kind="some_future_kind",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    with caplog.at_level("WARNING", logger="nymeria.core.credential_vault"):
        CredentialVaultRepo(repo.db_path)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert record.id in warnings[0].getMessage()
    assert "some_future_kind" in warnings[0].getMessage()


def test_migration_leaves_an_unknown_kind_unrestricted(repo):
    """A kind with no known reader set keeps its reach, loudly.

    Narrowing a kind whose readers were never enumerated is how you get the
    silent sign-out. The wildcard is not a win, it is an honest deferral, and
    the vault logs these for an operator to narrow by hand.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Exotic",
        provider="manual",
        kind="some_future_kind",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    migrated = CredentialVaultRepo(repo.db_path).get_credential(record.id)

    assert migrated is not None
    assert migrated.allowed_targets == ["*"]


def test_migration_does_not_touch_an_already_scoped_row(repo):
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Already scoped",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=["native_tool:weather_check"],
    )
    conn = sqlite3.connect(str(repo.db_path))
    try:
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            ("credential_vault.schema_version",),
        )
        conn.commit()
    finally:
        conn.close()

    migrated = CredentialVaultRepo(repo.db_path).get_credential(record.id)

    assert migrated is not None
    assert migrated.allowed_targets == ["native_tool:weather_check"]


def test_migration_does_not_re_widen_a_deliberately_emptied_row(repo):
    """The reason this is version-gated and not condition-gated.

    Once empty means deny, ``[]`` is a legitimate way for an operator to say
    "nothing may read this". A migration that re-ran on every startup wherever
    it found an empty list would silently re-grant that row on the next boot,
    turning a deliberate lockout back into a wildcard.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Deliberately locked",
        provider="manual",
        kind="some_future_kind",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    # First open runs the migration and stamps the version.
    CredentialVaultRepo(repo.db_path)
    # The operator then empties it on purpose, WITHOUT resetting the version.
    conn = sqlite3.connect(str(repo.db_path))
    try:
        conn.execute(
            "UPDATE credentials SET allowed_targets_json = '[]' WHERE id = ?",
            (record.id,),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = CredentialVaultRepo(repo.db_path).get_credential(record.id)

    assert reopened is not None
    assert reopened.allowed_targets == []


def test_migration_is_idempotent_across_repeated_opens(repo):
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Repeat",
        provider="manual",
        kind="oauth_token",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    for _ in range(3):
        CredentialVaultRepo(repo.db_path)

    assert _raw_targets(repo.db_path, record.id) == '["native_tool:*"]'


def test_a_cold_concurrent_start_backfills_exactly_once(repo):
    """What ``BEGIN IMMEDIATE`` in ``_migrate_locked`` is for.

    On a cold Docker start, api, worker and mcp open this database at the same
    moment. Reading the version outside the write lock lets all three pass the
    check and run the same backfill: the values converge, but each writes its
    own audit row, in the subsystem whose entire job is an honest audit trail.
    Taking the write lock BEFORE the version read makes the check and the
    migration one atomic step, so the losers see the stamp and do nothing.
    """
    import threading

    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Cold start",
        provider="manual",
        kind="oauth_token",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )
    _force_empty(repo.db_path, record.id)

    ready = threading.Barrier(4)
    errors: list[BaseException] = []

    def _open() -> None:
        try:
            ready.wait(timeout=10)
            CredentialVaultRepo(repo.db_path)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_open) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert _raw_targets(repo.db_path, record.id) == '["native_tool:*"]'

    conn = sqlite3.connect(str(repo.db_path))
    try:
        backfills = conn.execute(
            """
            SELECT COUNT(*) FROM credential_audit_events
            WHERE credential_id = ? AND event_type = 'allowed_targets_backfilled'
            """,
            (record.id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert backfills == 1


# --- create/upsert defaults ----------------------------------------------


def test_an_unspecified_target_list_gets_the_kinds_readers(repo):
    """The lazy path stays usable.

    The agent writes secrets but can never read one back, so a credential it
    saves without naming a purpose has to still work. Denying by default here
    would push people back to pasting keys into config files, which is worse.
    """
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Saved in a hurry",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
    )

    assert "native_tool:*" in record.allowed_targets
    assert "mcp_server:*" not in record.allowed_targets


def test_an_explicit_empty_list_is_honoured_as_a_lockout(repo):
    """``[]`` has to stay spellable, or "nothing may read this" is unsayable."""
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Locked",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=[],
    )

    assert record.allowed_targets == []


def test_an_update_that_omits_targets_keeps_the_existing_ones(repo):
    """The data-loss case. ``upsert`` assigns every column unconditionally.

    A partial update (a token refresh, a status change) that does not mention
    targets must not rewrite the scope the user set. Before this was handled,
    omitting the argument applied the kind DEFAULT, silently discarding a
    hand-narrowed list on the next refresh.
    """
    repo.upsert_credential(
        credential_id="cred_scoped",
        owner_type="user",
        owner_user_id="alice",
        name="Scoped",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=["native_tool:weather_check"],
    )

    repo.upsert_credential(
        credential_id="cred_scoped",
        owner_type="user",
        owner_user_id="alice",
        name="Scoped (renamed)",
        provider="manual",
        kind="api_key",
        status="active",
    )

    record = repo.get_credential("cred_scoped")
    assert record is not None
    assert record.allowed_targets == ["native_tool:weather_check"]
    assert record.name == "Scoped (renamed)"


def test_an_update_can_still_deliberately_clear_the_targets(repo):
    """Preserving on omission must not make a deliberate lockout impossible."""
    repo.upsert_credential(
        credential_id="cred_scoped2",
        owner_type="user",
        owner_user_id="alice",
        name="Scoped",
        provider="manual",
        kind="api_key",
        secret_fields={"value": "s3cret"},
        allowed_targets=["native_tool:weather_check"],
    )

    repo.upsert_credential(
        credential_id="cred_scoped2",
        owner_type="user",
        owner_user_id="alice",
        name="Scoped",
        provider="manual",
        kind="api_key",
        allowed_targets=[],
    )

    record = repo.get_credential("cred_scoped2")
    assert record is not None
    assert record.allowed_targets == []


# --- the writer/reader pair that the flip would otherwise break -----------


def test_a_legacy_cache_round_trips_after_the_flip(repo):
    """The silent-sign-out regression, pinned end to end.

    ``upsert_legacy_cache`` never declared a target while ``load_legacy_cache``
    reads with ``native_auth_cache:<filename>``, so the pair agreed only
    because the gate was vacuous.

    The stored list is asserted EXACTLY, not just via a successful read. A
    round-trip alone cannot fail: with the writer's ``allowed_targets=``
    removed, the row falls through to this kind's default of
    ``native_auth_cache:*``, which also satisfies the read. Only the exact
    value distinguishes "the writer declares its own filename" from "the
    default happened to cover it".
    """
    record = repo.upsert_legacy_cache(
        "alice", "msal_cache.json", {"accounts": [{"x": 1}]}
    )

    assert record.allowed_targets == ["native_auth_cache:msal_cache.json"]
    assert repo.load_legacy_cache("alice", "msal_cache.json") == {
        "accounts": [{"x": 1}]
    }


def test_a_migrated_legacy_cache_still_reads(repo):
    """The same pair for a row that predates the writer fix."""
    repo.upsert_legacy_cache("alice", "msal_cache.json", {"accounts": []})
    rows = [
        c
        for c in repo.list_credentials(owner_user_id="alice")
        if c.kind == "legacy_token_cache"
    ]
    assert len(rows) == 1
    _force_empty(repo.db_path, rows[0].id)

    migrated = CredentialVaultRepo(repo.db_path)

    assert migrated.load_legacy_cache("alice", "msal_cache.json") == {"accounts": []}
