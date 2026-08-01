from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet

from nymeria.core.http_policy import HTTPPolicyConfig
from nymeria.core.accounts import AccountsRepo
from nymeria.core import credential_vault as vault_module
from nymeria.core.credential_vault import (
    SYSTEM_ACTOR,
    CredentialAccessDenied,
    CredentialVaultRepo,
    get_credential_vault_repo,
    migrate_mcp_encrypted_env_vars,
)
from nymeria.core import secrets as nymeria_secrets
from nymeria.tools.http_api import _http_request_impl


def _count_secret_field_selects(repo: CredentialVaultRepo, monkeypatch) -> dict[str, int]:
    """Patch ``repo._connect`` to count SELECTs against credential_secret_fields.

    Returns a mutable counter dict the caller reads after the traced call. The
    trace callback is installed after the real connect (so the PRAGMA setup in
    ``_connect`` is not counted).
    """
    counts = {"selects": 0}
    real_connect = repo._connect

    def traced_connect():
        conn = real_connect()

        def tracer(statement: str) -> None:
            normalized = statement.strip().upper()
            if normalized.startswith("SELECT") and "CREDENTIAL_SECRET_FIELDS" in normalized:
                counts["selects"] += 1

        conn.set_trace_callback(tracer)
        return conn

    monkeypatch.setattr(repo, "_connect", traced_connect)
    return counts


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    accounts.create_user("bob", "bob@example.com", "Bob")
    return CredentialVaultRepo(db_path)


def test_secret_fields_are_not_exposed_and_resolve_by_reference(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example API",
        provider="example",
        kind="api_key",
        allowed_targets=["custom_tool:example_search"],
        secret_fields={"value": "sk-test-secret"},
        created_by_user_id="alice",
    )

    public = record.public_dict()
    assert public["secret_fields"] == ["value"]
    assert "sk-test-secret" not in json.dumps(public)

    resolved = repo.resolve_references(
        "Bearer ${credential:%s.value}" % record.id,
        actor="alice",
        target_type="custom_tool",
        target_id="example_search",
    )
    assert resolved == "Bearer sk-test-secret"

    with pytest.raises(CredentialAccessDenied):
        repo.resolve_references(
            "Bearer ${credential:%s.value}" % record.id,
            actor="alice",
            target_type="custom_tool",
            target_id="wrong_tool",
        )


def test_secret_field_access_is_owner_scoped_in_repo(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Alice API",
        provider="example",
        kind="api_key",
        allowed_targets=["*"],
        secret_fields={"value": "alice-secret"},
        created_by_user_id="alice",
    )

    assert repo.get_secret_field(record.id, "value", actor="alice") == "alice-secret"
    with pytest.raises(CredentialAccessDenied):
        repo.get_secret_field(record.id, "value", actor="bob")


def test_delete_and_disable_are_owner_scoped_in_repo(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Alice API",
        provider="example",
        kind="api_key",
        allowed_targets=["*"],
        secret_fields={"value": "alice-secret"},
        created_by_user_id="alice",
    )

    with pytest.raises(CredentialAccessDenied):
        repo.disable_credential(record.id, actor_user_id="bob")
    assert repo.get_credential(record.id).status == "active"

    assert repo.disable_credential(record.id, actor_user_id="bob", actor_is_admin=True) is True
    with pytest.raises(CredentialAccessDenied):
        repo.delete_credential(record.id, actor_user_id="bob")
    assert repo.delete_credential(record.id, actor_user_id="bob", actor_is_admin=True) is True


def test_legacy_cache_round_trips_through_vault(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    cache = {
        "accounts": {
            "acct": {
                "email": "alice@example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "scopes": ["scope-a"],
            }
        }
    }

    record = repo.upsert_legacy_cache("alice", "google_calendar.json", cache)
    assert record.provider == "google_calendar"
    assert record.account_label == "alice@example.com"
    assert "cache_json" in record.secret_fields

    loaded = repo.load_legacy_cache("alice", "google_calendar.json")
    assert loaded == cache


def test_mcp_encrypted_env_var_migration_creates_system_credential(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    servers_dir = tmp_path / "mcp_servers"
    servers_dir.mkdir()
    ciphertext = nymeria_secrets.encrypt("token-value")
    server_path = servers_dir / "linear.json"
    server_path.write_text(
        json.dumps(
            {
                "id": "linear",
                "name": "Linear",
                "transport": "stdio",
                "server_command": "npx",
                "server_args": ["linear-mcp"],
                "env_vars": {},
                "encrypted_env_vars": {"LINEAR_API_KEY": ciphertext},
            }
        )
    )

    logs = migrate_mcp_encrypted_env_vars(servers_dir, repo)
    updated = json.loads(server_path.read_text())

    assert any("linear" in line for line in logs)
    assert updated["encrypted_env_vars"] == {}
    ref = updated["env_vars"]["LINEAR_API_KEY"]
    assert ref.startswith("${credential:cred_mcp_")
    # SYSTEM_ACTOR: the migration mints an owner_type="system" credential, so
    # there is no user principal to name on the read back.
    assert repo.resolve_references(
        ref,
        actor=SYSTEM_ACTOR,
        target_type="mcp_server",
        target_id="linear",
    ) == "token-value"


def test_http_result_redacts_resolved_credential_values_from_metadata():
    secret = "secret-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"echoed_url": str(request.url), "echoed_auth": request.headers.get("authorization")},
            request=request,
        )

    result = _http_request_impl(
        "POST",
        f"https://api.example.test/search?token={secret}",
        headers={"Authorization": f"Bearer {secret}"},
        query={"api_key": secret},
        body={"token": secret},
        transport=httpx.MockTransport(handler),
        policy_config=HTTPPolicyConfig(resolve_dns=False),
        used_credentials=["cred_example"],
        redact_values=[secret],
    )

    serialized = json.dumps(result, default=str)
    assert result["ok"] is True
    assert "cred_example" in serialized
    assert secret not in serialized
    assert "[redacted]" in serialized


def test_list_credentials_groups_secret_field_queries(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    for i in range(5):
        repo.create_credential(
            owner_type="user",
            owner_user_id="alice",
            name=f"cred-{i}",
            provider="example",
            kind="api_key",
            allowed_targets=["*"],
            secret_fields={"value": f"secret-{i}", "extra": f"extra-{i}"},
            created_by_user_id="alice",
        )
    # A credential with no secret fields must still resolve to an empty list.
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="cred-empty",
        provider="example",
        kind="api_key",
        allowed_targets=["*"],
        secret_fields={},
        created_by_user_id="alice",
    )

    counts = _count_secret_field_selects(repo, monkeypatch)
    records = repo.list_credentials(owner_user_id="alice")

    assert len(records) == 6
    assert counts["selects"] == 1  # one grouped query, not N+1
    by_name = {record.name: record for record in records}
    for i in range(5):
        assert by_name[f"cred-{i}"].secret_fields == ["extra", "value"]  # sorted
    assert by_name["cred-empty"].secret_fields == []


def test_list_credentials_empty_result_issues_no_secret_field_query(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    counts = _count_secret_field_selects(repo, monkeypatch)
    records = repo.list_credentials(owner_user_id="bob")
    assert records == []
    assert counts["selects"] == 0


def test_secret_field_names_for_batches_large_id_lists(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(repo, "_SECRET_FIELDS_BATCH", 2)
    ids = []
    expected: dict[str, list[str]] = {}
    for i in range(5):
        # Distinct field names per credential so a cross-batch stitching bug
        # (fields attributed to the wrong id) would be caught.
        fields = {f"k{i}_a": f"secret-{i}-a", f"k{i}_b": f"secret-{i}-b"}
        record = repo.create_credential(
            owner_type="user",
            owner_user_id="alice",
            name=f"cred-{i}",
            provider="example",
            kind="api_key",
            allowed_targets=["*"],
            secret_fields=fields,
            created_by_user_id="alice",
        )
        ids.append(record.id)
        expected[record.id] = sorted(fields)

    counts = _count_secret_field_selects(repo, monkeypatch)
    with repo._lock, repo._connect() as conn:
        grouped = repo._secret_field_names_for(conn, ids)

    assert set(grouped) == set(ids)
    assert {cid: sorted(names) for cid, names in grouped.items()} == expected
    # 5 ids batched at size 2 -> ceil(5/2) == 3 grouped queries.
    assert counts["selects"] == 3


def test_get_credential_vault_repo_is_singleton_and_swaps_on_db_change(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(vault_module, "_vault_repo", None)
    db_one = tmp_path / "one.db"
    db_two = tmp_path / "two.db"

    first = get_credential_vault_repo(db_one)
    assert get_credential_vault_repo(db_one) is first  # cached for same db

    second = get_credential_vault_repo(db_two)
    assert second is not first  # swapped on db_path change
    assert get_credential_vault_repo(db_two) is second


def _capture_binding_selects(repo: CredentialVaultRepo, monkeypatch) -> list[str]:
    """Record normalized SELECTs issued against ``credential_bindings``.

    Mirrors ``_count_secret_field_selects``: the trace callback is installed
    after the real connect (so the ``_connect`` PRAGMA setup is not recorded).
    The caller reads the returned list after the traced call.
    """
    statements: list[str] = []
    real_connect = repo._connect

    def traced_connect():
        conn = real_connect()

        def tracer(statement: str) -> None:
            normalized = " ".join(statement.strip().upper().split())
            if normalized.startswith("SELECT") and "CREDENTIAL_BINDINGS" in normalized:
                statements.append(normalized)

        conn.set_trace_callback(tracer)
        return conn

    monkeypatch.setattr(repo, "_connect", traced_connect)
    return statements


def test_get_binding_returns_correct_row_and_none(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Alice API",
        provider="example",
        kind="api_key",
        allowed_targets=["*"],
        secret_fields={"value": "alice-secret"},
        created_by_user_id="alice",
    )
    first_id = repo.bind_credential(
        record.id,
        target_type="custom_tool",
        target_id="tool_a",
        binding_name="A",
        actor_user_id="alice",
    )
    second_id = repo.bind_credential(
        record.id,
        target_type="custom_tool",
        target_id="tool_b",
        binding_name="B",
        actor_user_id="alice",
    )

    first = repo.get_binding(first_id)
    assert first is not None
    assert first["id"] == first_id
    assert first["credential_id"] == record.id
    assert first["target_type"] == "custom_tool"
    assert first["target_id"] == "tool_a"
    # Same row shape as list_bindings, so CredentialBindingResponse(**row) holds.
    assert first == next(r for r in repo.list_bindings(record.id) if r["id"] == first_id)

    assert repo.get_binding(second_id)["target_id"] == "tool_b"
    assert repo.get_binding("cbind-does-not-exist") is None

    # Deleting one binding does not affect lookups of the other.
    assert repo.delete_binding(first_id, actor_user_id="alice") is True
    assert repo.get_binding(first_id) is None
    assert repo.get_binding(second_id) is not None


def test_get_binding_is_indexed_single_row_lookup(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Alice API",
        provider="example",
        kind="api_key",
        allowed_targets=["*"],
        secret_fields={"value": "alice-secret"},
        created_by_user_id="alice",
    )
    target = repo.bind_credential(
        record.id,
        target_type="custom_tool",
        target_id="tool_target",
        actor_user_id="alice",
    )
    for i in range(4):
        repo.bind_credential(
            record.id,
            target_type="custom_tool",
            target_id=f"other_{i}",
            actor_user_id="alice",
        )

    statements = _capture_binding_selects(repo, monkeypatch)
    row = repo.get_binding(target)

    assert row is not None and row["id"] == target
    # Exactly one binding read, filtered by primary key rather than scanning
    # every binding (guards against reverting to a list_bindings() full scan).
    assert len(statements) == 1
    assert "FROM CREDENTIAL_BINDINGS" in statements[0]
    assert "WHERE ID =" in statements[0]
    assert "ORDER BY" not in statements[0]


def _count_connect_calls(repo: CredentialVaultRepo, monkeypatch) -> dict[str, int]:
    """Count ``repo._connect()`` invocations during a traced call.

    A write method that re-reads its fresh record via ``get_credential`` after
    the lock releases opens a *second* connection; reading via the lock-free
    ``_record_locked`` on the still-open connection opens only one. The counter
    is the regression guard for that optimization (F11).
    """
    counts = {"connects": 0}
    real_connect = repo._connect

    def traced_connect():
        counts["connects"] += 1
        return real_connect()

    monkeypatch.setattr(repo, "_connect", traced_connect)
    return counts


def test_create_credential_returns_in_transaction_record_without_reopening(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    counts = _count_connect_calls(repo, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Alice API",
        provider="example",
        kind="api_key",
        allowed_targets=["custom_tool:example_search"],
        secret_fields={"value": "sk-secret", "extra": "x"},
        created_by_user_id="alice",
    )
    # One connection for the write block; no second open for a post-block re-read.
    assert counts["connects"] == 1
    # Behavior-preservation: the in-transaction record equals what the unchanged
    # get_credential() returns from a fresh connection (public_dict includes the
    # sorted secret-field names).
    fresh = repo.get_credential(record.id)
    assert fresh is not None
    assert record.public_dict() == fresh.public_dict()
    assert record.secret_fields == ["extra", "value"]  # sorted, decrypted names only


def test_upsert_credential_insert_path_returns_record_without_reopening(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    counts = _count_connect_calls(repo, monkeypatch)
    record = repo.upsert_credential(
        credential_id="cred_fixed_1",
        owner_type="user",
        owner_user_id="alice",
        name="Alice Upsert",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-1"},
        actor_user_id="alice",
    )
    assert counts["connects"] == 1
    assert record.id == "cred_fixed_1"
    fresh = repo.get_credential("cred_fixed_1")
    assert fresh is not None
    assert record.public_dict() == fresh.public_dict()


def test_upsert_credential_update_path_returns_fresh_record_without_reopening(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    repo.upsert_credential(
        credential_id="cred_fixed_2",
        owner_type="user",
        owner_user_id="alice",
        name="Original",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-old"},
        actor_user_id="alice",
    )
    counts = _count_connect_calls(repo, monkeypatch)
    updated = repo.upsert_credential(
        credential_id="cred_fixed_2",
        owner_type="user",
        owner_user_id="alice",
        name="Renamed",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-new"},
        actor_user_id="alice",
    )
    assert counts["connects"] == 1
    # The returned record reflects the update, identical to a fresh read.
    assert updated.name == "Renamed"
    fresh = repo.get_credential("cred_fixed_2")
    assert fresh is not None
    assert updated.public_dict() == fresh.public_dict()


# --- possession probes must not be recorded as use -------------------------
#
# ``has_secret_values`` exists because the destination-join control (E10-02-D)
# has to ask "could this record serve this secret" about records it is about to
# REJECT. Answering that through ``get_secret_field`` worked, but that is the
# accounting path: it bumps ``last_used_at`` and writes a ``used`` audit row per
# call, so one address lookup produced five ``used`` rows where it should have
# produced one, and marked a record used that was never used. These tests pin
# the distinction; they fail if anyone routes the probe back through the read.


def _used_events(repo: CredentialVaultRepo, credential_id: str) -> list[str]:
    with repo._connect() as conn:
        return [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM credential_audit_events WHERE credential_id = ?"
                " ORDER BY id",
                (credential_id,),
            ).fetchall()
        ]


def _probe_record(repo: CredentialVaultRepo, **extra_secrets: str) -> str:
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Elastic",
        provider="elasticsearch",
        kind="api_key",
        # Explicit, so these tests are about possession and the audit trail
        # rather than about allowed_targets (an empty list denies).
        allowed_targets=["*"],
        secret_fields={
            "api_key": "REAL-KEY",
            "base_url": "https://self.hosted",
            **extra_secrets,
        },
    )
    return record.id


def test_has_secret_values_reports_possession_by_value(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    # An empty value CAN be stored (the write is an upsert with no delete arm,
    # and rows predating the schema validator carry them), and must NOT count as
    # possession: that gap is the whole bypass this closes.
    cred_id = _probe_record(repo, token="")

    held = repo.has_secret_values(
        cred_id, ["api_key", "token", "never_stored"], actor="alice"
    )
    assert held == frozenset({"api_key"}), (
        "possession must be judged by VALUE: 'token' is stored but empty, and "
        "'never_stored' is absent, so neither is held"
    )


def test_has_secret_values_does_not_record_a_use(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    cred_id = _probe_record(repo)
    before_events = _used_events(repo, cred_id)
    before_used_at = repo.get_credential(cred_id).last_used_at

    for _ in range(3):
        repo.has_secret_values(cred_id, ["api_key", "base_url"], actor="alice")

    assert _used_events(repo, cred_id) == before_events, (
        "a possession probe must not write an audit event; once probes are "
        "indistinguishable from reads the log cannot answer 'was this "
        "credential actually read?'"
    )
    assert repo.get_credential(cred_id).last_used_at == before_used_at, (
        "a possession probe must not bump last_used_at"
    )

    # The real read still does both, so the control is the probe's restraint and
    # not an accounting regression.
    repo.get_secret_field(cred_id, "api_key", actor="alice")
    assert "used" in _used_events(repo, cred_id)
    assert repo.get_credential(cred_id).last_used_at != before_used_at


def test_has_secret_values_enforces_the_same_access_checks_as_a_read(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    cred_id = _probe_record(repo)
    # A caller who may not read the secret must not learn whether it exists, and
    # more directly: a record this actor cannot reach must not be able to win
    # the destination right, so "denied" and "holds nothing" agree at the call site.
    with pytest.raises(CredentialAccessDenied):
        repo.has_secret_values(cred_id, ["api_key"], actor="bob")


# The write-path half of the same rule. It lives here, next to the read-path
# half, rather than in an API-schema test, because the two only make sense
# together: the schema stops the shape being STORED, and ``has_secret_values``
# covers rows stored before the schema existed plus any writer that does not
# come through it. Splitting them leaves each looking like belt-and-braces.


def test_the_rest_surface_refuses_a_blank_anchor_like_its_agent_facing_twin():
    from pydantic import ValidationError

    from nymeria.api.schemas.credentials import (
        CredentialCreateRequest,
        CredentialUpdateRequest,
    )

    planted = {"base_url": "https://attacker.invalid", "api_key": ""}
    for model, extra in (
        (CredentialCreateRequest, {"name": "n", "provider": "elasticsearch"}),
        (CredentialUpdateRequest, {}),
    ):
        with pytest.raises(ValidationError):
            model(secret_fields=planted, **extra)
        # Whitespace is the same shape; ``.strip()`` is what catches it, matching
        # tools/auth_manager.py::auth_write, which has always enforced this.
        with pytest.raises(ValidationError):
            model(secret_fields={"base_url": "https://x.invalid", "api_key": "  "}, **extra)
        # A real record still saves, and omitting the field entirely is still a
        # metadata-only update. The control must cost no legitimate capability.
        model(secret_fields={"base_url": "https://self.hosted", "api_key": "K"}, **extra)
        model(**extra)
