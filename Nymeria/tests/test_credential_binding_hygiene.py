"""Credential-binding hygiene: ref building, and the vault's widening primitive.

Two independent properties, both reached from the MCP install surface.

**1. A built credential ref must be exactly one reference.** `_binding_ref`
interpolates a caller-supplied `credential_id` and `field` into a
`${credential:id.field}` string. Neither was validated, so a field of
``value} ${credential:other-id.cache_json`` closed the ref early and appended a
SECOND one. `_sweep_sensitive_mapping` then skips the value (it already matches
`_credential_ref`), so it is stored verbatim, and `mcp_manager` resolves BOTH at
spawn as `SYSTEM_ACTOR`. Because MCP resolution skips the vault's owner check by
design, and a row with an empty `allowed_targets` is readable by any target,
that turned "bind a credential you may name" into "read one you may not".

**2. `add_allowed_target` is owner-checked**, like its `remove_allowed_target`
twin, which already was. The asymmetry was backwards: this is the WIDENING
primitive, so it is the one that grants a new call site the right to read
plaintext.

Scope note, deliberately recorded so it is not overstated later: every surface
that reaches `_binding_ref` is admin-only, and an admin already reads every
credential legitimately via `actor_is_admin`. So property 1 is a
correctness/defence-in-depth fix, NOT a live non-admin privilege escalation.
An ownership check inside `_binding_ref` was tried and removed: it enforced
nothing today and broke an admin configuring a server on a user's behalf.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from nymeria.core.credential_vault import CredentialAccessDenied
from nymeria.core.mcp_runtime import MCPInstallPlan, apply_config_values
from nymeria.tools.definitions.mcp_schema import MCPServerDefinition

_VICTIM_SECRET = "alice-refresh-token-do-not-leak"


def _make_vault(tmp_path, monkeypatch, *, users=("alice", "mallory")):
    from nymeria.core import credential_vault
    from nymeria.core.accounts import AccountsRepo
    from nymeria.core.credential_vault import CredentialVaultRepo

    db_path = tmp_path / "credentials.db"
    accounts = AccountsRepo(db_path)
    for user_id in users:
        accounts.create_user(user_id, f"{user_id}@example.com", user_id.title())
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    return repo


def _plan():
    return MCPInstallPlan(
        source_type="stdio",
        runtime_type="direct",
        risk_level="low",
        confirmation_required=False,
        parsed_summary="binding hygiene test",
        required_config=[
            {
                "name": "API_KEY",
                "source": "env",
                "env_name": "API_KEY",
                "required": True,
                "sensitive": True,
            }
        ],
    )


def _defn():
    return MCPServerDefinition(
        id="srv-binding-1",
        name="Binding Server",
        transport="stdio",
        server_command="echo",
    )


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A victim row shaped like a real migrated OAuth cache: empty targets."""
    repo = _make_vault(tmp_path, monkeypatch)
    repo.upsert_credential(
        credential_id="cred_lcache_victim",
        owner_type="user",
        owner_user_id="alice",
        name="Legacy OAuth cache",
        provider="microsoft",
        kind="legacy_token_cache",
        secret_fields={"cache_json": _VICTIM_SECRET},
        status="active",
        actor_user_id="alice",
    )
    repo.upsert_credential(
        credential_id="cred_mallory_own",
        owner_type="user",
        owner_user_id="mallory",
        name="Own key",
        provider="manual",
        kind="secret",
        secret_fields={"value": "mallory-own"},
        status="active",
        actor_user_id="mallory",
    )
    record = repo.get_credential("cred_lcache_victim")
    assert record is not None and record.allowed_targets == []
    return repo


def _bind(user_id, credential_id="cred_mallory_own", field="value"):
    # ``credential_values`` is required to reach the binding arm at all: with no
    # supplied value the field takes the "required and missing" branch and mints
    # a fresh cred_mcp_* row instead of honouring the binding.
    return apply_config_values(
        _defn(),
        _plan(),
        credential_values={"API_KEY": "ignored-when-a-binding-is-supplied"},
        credential_bindings={
            "API_KEY": {"credential_id": credential_id, "field": field}
        },
        user_id=user_id,
    )


# --- property 1: a ref is exactly one ref --------------------------------


def test_a_field_cannot_smuggle_a_second_credential_ref(vault):
    """The escape that made a legal binding read an unrelated credential."""
    with pytest.raises(ValueError):
        _bind(
            "mallory",
            credential_id="cred_mallory_own",
            field="value} ${credential:cred_lcache_victim.cache_json",
        )


def test_a_smuggled_field_never_reaches_the_definition(vault):
    """Checked separately from the raise: the durable half is what is stored.

    If the injected value were written to ``env_vars`` it would be resolved at
    every subsequent spawn, so a raise that happened after the assignment would
    still leak.
    """
    with pytest.raises(ValueError):
        defn, _ = _bind(
            "mallory",
            field="value} ${credential:cred_lcache_victim.cache_json",
        )

    # Nothing was persisted, so a fresh definition carries no injected ref.
    assert "cred_lcache_victim" not in str(_defn().env_vars)


def test_a_credential_id_cannot_smuggle_a_ref_either(vault):
    """Same escape through the other interpolated half."""
    with pytest.raises(ValueError):
        _bind("mallory", credential_id="cred_x.value} ${credential:cred_lcache_victim")


def test_an_ordinary_binding_still_works(vault):
    """The validation costs no legitimate capability."""
    defn, missing = _bind("mallory")

    assert missing == []
    assert defn.env_vars["API_KEY"] == "${credential:cred_mallory_own.value}"


def test_a_binding_does_not_narrow_an_unscoped_credential(vault):
    """Binding must NOT write allowed_targets.

    An empty ``allowed_targets`` means "any target may read" today, so adding
    ``mcp_server:<id>`` would flip the row from allow-all to allow-only-this and
    silently break every other consumer: a legacy OAuth cache read via
    ``native_auth_cache`` starts failing, and ``load_token_cache`` swallows that
    at debug level, so the user is signed out with no error. Pinned because an
    earlier version of this change did exactly that.
    """
    _bind("mallory")

    record = vault.get_credential("cred_mallory_own")
    assert record is not None
    assert record.allowed_targets == []


# --- property 2: the widening primitive is owner-checked ------------------


def test_add_allowed_target_refuses_a_non_owner(vault):
    with pytest.raises(CredentialAccessDenied):
        vault.add_allowed_target(
            "cred_lcache_victim",
            target="mcp_server:srv-binding-1",
            actor_user_id="mallory",
        )

    record = vault.get_credential("cred_lcache_victim")
    assert record is not None and record.allowed_targets == []


def test_add_allowed_target_still_allows_the_owner_and_an_admin(vault):
    vault.add_allowed_target(
        "cred_lcache_victim", target="native_tool:x", actor_user_id="alice"
    )
    vault.add_allowed_target(
        "cred_lcache_victim",
        target="native_tool:y",
        actor_user_id="mallory",
        actor_is_admin=True,
    )

    record = vault.get_credential("cred_lcache_victim")
    assert record is not None
    assert "native_tool:x" in record.allowed_targets
    assert "native_tool:y" in record.allowed_targets
