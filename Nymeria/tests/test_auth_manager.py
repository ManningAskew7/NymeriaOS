from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_tests import CredentialTestResult
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.tools.auth_manager import (
    _NormalizedFilters,
    _normalize_filters,
    auth_bindings,
    auth_cleanup,
    auth_inspect,
    auth_test,
    auth_write,
)


@dataclass
class _Settings:
    data_dir: Path


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = _Settings(data_dir=tmp_path)

    import nymeria.config as config_mod
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)

    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")

    import nymeria.core.credential_vault as vault_mod
    vault_mod._vault_repo = CredentialVaultRepo(db_path)
    return vault_mod._vault_repo


def _config(user_id: str = "alice") -> dict:
    return {"configurable": {"user_id": user_id, "thread_id": "auth-manager-test"}}


def _inspect(args: dict) -> dict:
    return json.loads(auth_inspect.invoke(args, config=_config()))


def _cleanup(args: dict) -> dict:
    return json.loads(auth_cleanup.invoke(args, config=_config()))


def _bindings(args: dict) -> dict:
    return json.loads(auth_bindings.invoke(args, config=_config()))


def test_auth_inspect_filtered_list_never_returns_secret_values(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example",
        provider="example",
        kind="api_key",
        status="active",
        secret_fields={"value": "super-secret-value"},
        created_by_user_id="alice",
    )

    body = _inspect({"view": "list", "provider": "example"})
    dumped = json.dumps(body)
    assert body["ok"] is True
    assert body["total"] == 1
    assert body["credentials"][0]["id"] == record.id
    assert "super-secret-value" not in dumped
    assert body["credentials"][0]["secret_fields"] == ["value"]


def test_auth_cleanup_stale_oauth_dry_run_then_disable(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    active = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Alex Outlook",
        provider="outlook",
        kind="oauth_token",
        status="active",
        account_label="manning@example.com",
        metadata={
            "account_id": "manning_at_example_com",
            "email": "manning@example.com",
            "expires_at": "2026-05-25T00:00:00+00:00",
        },
        secret_fields={"access_token": "new-access", "refresh_token": "new-refresh"},
        allowed_targets=["native_tool:*"],
        created_by_user_id="alice",
    )
    pending = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Pending Outlook",
        provider="outlook",
        kind="oauth_token",
        status="pending_setup",
        metadata={"oauth_pending": True, "prompt_id": "prompt-old"},
        created_by_user_id="alice",
    )
    legacy = repo.upsert_legacy_cache(
        "alice",
        "microsoft.json",
        {
            "accounts": {
                "manning_at_example_com": {
                    "email": "manning@example.com",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "expires_at": 0,
                }
            }
        },
    )

    preview = _cleanup({"operation": "stale_oauth", "provider": "outlook"})
    candidate_ids = {row["credential_id"] for row in preview["candidates"]}
    assert preview["dry_run"] is True
    assert active.id not in candidate_ids
    assert pending.id in candidate_ids
    assert legacy.id in candidate_ids
    assert repo.get_credential(pending.id).status == "pending_setup"
    assert repo.get_credential(legacy.id).status == "active"

    applied = _cleanup({"operation": "stale_oauth", "provider": "outlook", "dry_run": False})
    assert set(applied["disabled"]) == {pending.id, legacy.id}
    assert repo.get_credential(active.id).status == "active"
    assert repo.get_credential(pending.id).status == "disabled"
    assert repo.get_credential(legacy.id).status == "disabled"


def test_normalize_filters_lowercases_enums_and_strips_opaque_ids():
    result = _normalize_filters(
        provider="  OutLook ",
        kind=" OAuth_Token ",
        status=" Active ",
        account_id="  Acct-ID_42 ",
        prompt_id="  Prompt-7 ",
        limit=10,
    )
    assert isinstance(result, _NormalizedFilters)
    # provider/kind/status are case-insensitive enums: stripped and lowercased.
    assert result.provider == "outlook"
    assert result.kind == "oauth_token"
    assert result.status == "active"
    # account_id/prompt_id are opaque identifiers: stripped but case preserved.
    assert result.account_id == "Acct-ID_42"
    assert result.prompt_id == "Prompt-7"
    assert result.max_rows == 10


def test_normalize_filters_handles_empty_and_clamps_max_rows():
    # Empty strings stay empty; limit 0 falls back to the default 100.
    assert _normalize_filters("", "", "", "", "", 0) == _NormalizedFilters(
        "", "", "", "", "", 100
    )
    # max_rows clamps to the floor of 1 and ceiling of 500.
    assert _normalize_filters("", "", "", "", "", 1).max_rows == 1
    assert _normalize_filters("", "", "", "", "", 9999).max_rows == 500
    assert _normalize_filters("", "", "", "", "", -5).max_rows == 1


def test_auth_inspect_provider_filter_is_case_and_whitespace_normalized(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example",
        provider="example",
        kind="api_key",
        status="active",
        secret_fields={"value": "secret"},
        created_by_user_id="alice",
    )

    # A messy-cased, padded provider filter still matches the stored "example",
    # proving the normalization helper output flows into the record filter.
    body = _inspect({"view": "list", "provider": "  ExAmPle "})
    assert body["ok"] is True
    assert body["total"] == 1
    assert body["credentials"][0]["id"] == record.id


def test_auth_cleanup_provider_filter_is_case_and_whitespace_normalized(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example",
        provider="example",
        kind="api_key",
        status="active",
        secret_fields={"value": "secret"},
        created_by_user_id="alice",
    )

    # Same wiring proof for auth_cleanup's independent call site: a messy-cased
    # provider filter matches the stored "example" through disable_matching.
    body = _cleanup({"operation": "disable_matching", "provider": "  ExAmPle ", "dry_run": True})
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["matched_count"] == 1
    assert body["matched"][0]["id"] == record.id


def test_auth_bindings_bind_updates_allowed_targets(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example",
        provider="example",
        kind="api_key",
        status="active",
        secret_fields={"value": "secret"},
        created_by_user_id="alice",
    )

    body = _bindings(
        {
            "operation": "bind",
            "credential_id": record.id,
            "target_type": "native_tool",
            "target_id": "example_tool",
        }
    )
    assert body["ok"] is True
    binding_id = body["binding_id"]
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert "native_tool:example_tool" in updated.allowed_targets
    assert repo.list_bindings(record.id)

    unbound = _bindings({"operation": "unbind", "binding_id": binding_id})
    assert unbound["ok"] is True
    assert unbound["deleted"] is True
    assert unbound["allowed_target"] == "native_tool:example_tool"
    assert unbound["allowed_target_removed"] is True
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert "native_tool:example_tool" not in updated.allowed_targets
    assert repo.list_bindings(record.id) == []


# ---------------------------------------------------------------------------
# auth_test
# ---------------------------------------------------------------------------


def _test(args: dict, user_id: str = "alice") -> dict:
    return json.loads(asyncio.run(auth_test.ainvoke(args, config=_config(user_id))))


def _patch_probe(monkeypatch, *, ok: bool, code: str = "verified", calls: list | None = None):
    async def fake_probe(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return CredentialTestResult(
            ok=ok,
            message="probe ran" if ok else "probe failed",
            code=code if ok else "http_error",
            verified=True,
        )

    import nymeria.core.credential_tests as tests_mod

    monkeypatch.setattr(tests_mod, "test_credential_fields", fake_probe)


def test_auth_test_by_credential_id_marks_active_on_success(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="todoist key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "sk-super-secret-value"},
    )
    calls: list = []
    _patch_probe(monkeypatch, ok=True, calls=calls)
    body = _test({"credential_id": record.id})
    assert body["ok"] is True
    assert body["probe"]["code"] == "verified"
    assert body["status_updated"] is True
    assert calls and calls[0]["secret_fields"] == {"api_key": "sk-super-secret-value"}
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert updated.status == "active"
    assert updated.last_tested_at
    # The secret value never leaks into the tool output.
    assert "sk-super-secret-value" not in json.dumps(body)


def test_auth_test_marks_invalid_on_probe_failure(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="bad key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "sk-broken"},
    )
    _patch_probe(monkeypatch, ok=False)
    body = _test({"credential_id": record.id})
    assert body["ok"] is False
    assert body["probe"]["ok"] is False
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert updated.status == "invalid"


def test_auth_test_provider_prefers_user_owned_over_system(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="system",
        owner_user_id=None,
        name="system key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "system-value"},
    )
    mine = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="my key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "my-value"},
    )
    _patch_probe(monkeypatch, ok=True)
    body = _test({"provider": "todoist"})
    assert body["credential"]["id"] == mine.id
    assert body["other_matches"] == 1


def test_auth_test_system_credential_probe_gated_and_status_untouched(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="system",
        owner_user_id=None,
        name="ops key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "system-value"},
    )
    calls: list = []
    _patch_probe(monkeypatch, ok=False, calls=calls)
    body = _test({"credential_id": record.id})
    # Non-admins never live-probe system credentials (REST parity): the probe
    # function is not called and no secret fields are fetched.
    assert calls == []
    assert body["probe"]["code"] == "admin_only"
    assert body["status_updated"] is False
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert updated.status == "active"
    assert "system-value" not in json.dumps(body)


def test_auth_test_system_credential_admin_probes_but_never_mutates_status(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="system",
        owner_user_id=None,
        name="ops key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "system-value"},
    )
    calls: list = []
    _patch_probe(monkeypatch, ok=False, calls=calls)

    import nymeria.tools.auth_manager as auth_manager_mod

    monkeypatch.setattr(auth_manager_mod, "is_admin", lambda user_id, **kwargs: True)
    body = _test({"credential_id": record.id})
    assert calls, "admin probe should run against the system credential"
    assert body["probe"]["ok"] is False
    # Write-conservatism: even an admin's auth_test never flips a system
    # record's status (the REST route is the admin mutation surface).
    assert body["status_updated"] is False
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert updated.status == "active"


def test_auth_test_unmapped_tool_reports_not_required(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    body = _test({"tool_name": "definitely_not_an_integration_tool"})
    assert body["ok"] is True
    assert body["auth"] == "not_required"


def test_auth_test_mapped_tool_resolves_provider_and_hints_setup(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401  (registers specs)

    _setup(tmp_path, monkeypatch)
    body = _test({"tool_name": "todoist_list_tasks"})
    assert body["ok"] is False
    assert body["provider"] == "todoist"
    assert body["error"] == "no active credential found"
    assert "todoist" in body["setup_hint"]


def test_auth_test_reports_missing_required_fields(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _setup(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="half a trello",
        provider="trello",
        kind="api_key",
        secret_fields={"api_key": "the-key-only"},
    )
    _patch_probe(monkeypatch, ok=True)
    body = _test({"provider": "trello"})
    assert body["fields_ok"] is False
    roles = {entry["role"] for entry in body["missing_fields"]}
    assert roles == {"api_token"}
    # Probe succeeded but a required field is missing: overall not ok.
    assert body["ok"] is False


def test_auth_test_cross_user_credential_is_not_found(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    AccountsRepo(tmp_path / "accounts.db").create_user("bob", "bob@example.com", "Bob")
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="bob",
        name="bobs key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "bobs-secret"},
    )
    body = _test({"credential_id": record.id})
    assert body["ok"] is False
    assert body["error"] == "credential not found"


def test_auth_test_requires_a_target(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    body = _test({})
    assert body["ok"] is False
    assert "provide credential_id" in body["error"]


def test_auth_test_pending_only_reports_pending(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="pending",
        provider="todoist",
        kind="api_key",
        status="pending_setup",
    )
    body = _test({"provider": "todoist"})
    assert body["ok"] is False
    assert body["pending_setup"] == 1


# ---------------------------------------------------------------------------
# auth_write
# ---------------------------------------------------------------------------


def _write(args: dict, user_id: str = "alice") -> dict:
    return json.loads(asyncio.run(auth_write.ainvoke(args, config=_config(user_id))))


def test_auth_write_create_saves_probes_and_never_echoes_secrets(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401  (registers specs)

    repo = _setup(tmp_path, monkeypatch)
    calls: list = []
    _patch_probe(monkeypatch, ok=True, calls=calls)
    body = _write(
        {
            "operation": "create",
            "provider": "todoist",
            "secret_fields": json.dumps({"api_key": "sk-pasted-in-chat"}),
        }
    )
    assert body["ok"] is True
    assert body["operation"] == "create"
    assert body["credential"]["provider"] == "todoist"
    # Field NAMES only, never values, anywhere in the response.
    assert body["credential"]["secret_fields"] == ["api_key"]
    assert "sk-pasted-in-chat" not in json.dumps(body)
    assert body["warnings"] == []
    assert "reminder" in body
    # Auto-test ran against the stored ciphertext and marked the record.
    assert calls and calls[0]["secret_fields"] == {"api_key": "sk-pasted-in-chat"}
    assert body["probe"]["ok"] is True
    assert body["status_updated"] is True
    stored = repo.get_credential(body["credential"]["id"])
    assert stored is not None
    assert stored.status == "active"
    assert stored.last_tested_at


def test_auth_write_create_unknown_provider_warns_with_nearest_known(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _setup(tmp_path, monkeypatch)
    _patch_probe(monkeypatch, ok=True)
    body = _write(
        {
            "operation": "create",
            "provider": "todist",
            "secret_fields": json.dumps({"api_key": "sk-x"}),
        }
    )
    assert body["ok"] is True  # saved anyway: warning, not refusal
    assert any("not a known provider" in w for w in body["warnings"])
    assert any("todoist" in w for w in body["warnings"])


def test_auth_write_create_known_provider_missing_required_field_warns(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _setup(tmp_path, monkeypatch)
    _patch_probe(monkeypatch, ok=True)
    body = _write(
        {
            "operation": "create",
            "provider": "trello",
            "secret_fields": json.dumps({"api_key": "key-only"}),
        }
    )
    assert body["ok"] is True
    assert any("api_token" in w for w in body["warnings"])


def test_auth_write_create_with_bind_target_binds_and_allows(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    _patch_probe(monkeypatch, ok=True)
    body = _write(
        {
            "operation": "create",
            "provider": "example",
            "secret_fields": json.dumps({"value": "sk-x"}),
            "bind_target": "native_tool:example_tool",
        }
    )
    assert body["ok"] is True
    assert body["binding_id"]
    stored = repo.get_credential(body["credential"]["id"])
    assert "native_tool:example_tool" in stored.allowed_targets
    bindings = repo.list_bindings(stored.id)
    assert bindings and bindings[0]["target_type"] == "native_tool"


def test_auth_write_create_run_test_false_skips_probe(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls: list = []
    _patch_probe(monkeypatch, ok=True, calls=calls)
    body = _write(
        {
            "operation": "create",
            "provider": "example",
            "secret_fields": json.dumps({"value": "sk-x"}),
            "run_test": False,
        }
    )
    assert body["ok"] is True
    assert calls == []
    assert "probe" not in body


def test_auth_write_update_renames_provider_and_keeps_secrets(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="my todoist",
        provider="todoist-personal",
        kind="api_key",
        secret_fields={"api_key": "sk-keepme"},
        metadata={"note": "old", "stale": "yes"},
        scopes=["tasks:read"],
        allowed_targets=["native_tool:todoist_list_tasks"],
        account_label="personal",
        expires_at="2027-01-01T00:00:00+00:00",
    )
    body = _write(
        {
            "operation": "update",
            "credential_id": record.id,
            "provider": "todoist",
            "metadata": json.dumps({"note": "new", "stale": None}),
        }
    )
    assert body["ok"] is True
    updated = repo.get_credential(record.id)
    assert updated.provider == "todoist"
    assert updated.secret_fields == ["api_key"]  # secrets untouched
    assert updated.metadata == {"note": "new"}  # merge + null removal
    # Fields the tool must re-thread through upsert_credential survive.
    assert updated.scopes == ["tasks:read"]
    assert updated.allowed_targets == ["native_tool:todoist_list_tasks"]
    assert updated.account_label == "personal"
    assert updated.expires_at == "2027-01-01T00:00:00+00:00"
    # The runtime lookup now finds it under the canonical provider key.
    from nymeria.tools.native_credentials import get_native_credential_value

    value = get_native_credential_value(
        provider="todoist",
        field_names=("api_key",),
        tool_name="todoist_list_tasks",
        config=_config("alice"),
    )
    assert value is not None and value.value == "sk-keepme"
    assert "sk-keepme" not in json.dumps(body)


def test_auth_write_update_canonicalizes_alias_provider_rename(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="x",
        provider="todoist-personal",
        kind="api_key",
        secret_fields={"api_key": "sk-x"},
    )
    body = _write({"operation": "update", "credential_id": record.id, "provider": "todoist_api"})
    assert body["ok"] is True
    # Renaming to a known alias stores the canonical provider key.
    assert repo.get_credential(record.id).provider == "todoist"


def test_auth_write_replace_secret_failing_probe_marks_successor_invalid(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    _patch_probe(monkeypatch, ok=False)
    old = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="x",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-old"},
    )
    body = _write(
        {
            "operation": "replace_secret",
            "credential_id": old.id,
            "secret_fields": json.dumps({"value": "sk-bad"}),
        }
    )
    # Documented tradeoff: the old record is disabled regardless; a failing
    # probe leaves the successor marked invalid and the response says so.
    assert body["probe"]["ok"] is False
    assert repo.get_credential(old.id).status == "disabled"
    assert repo.get_credential(body["credential"]["id"]).status == "invalid"


def test_auth_write_create_bind_without_test_returns_fresh_snapshot(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    body = _write(
        {
            "operation": "create",
            "provider": "example",
            "secret_fields": json.dumps({"value": "sk-x"}),
            "bind_target": "native_tool:example_tool",
            "run_test": False,
        }
    )
    assert body["ok"] is True
    # The returned snapshot reflects the bind even with the probe skipped.
    assert body["credential"]["allowed_targets"] == ["native_tool:example_tool"]


def test_auth_write_update_rejects_secret_fields(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="x",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-x"},
    )
    body = _write(
        {
            "operation": "update",
            "credential_id": record.id,
            "secret_fields": json.dumps({"value": "sk-new"}),
        }
    )
    assert body["ok"] is False
    assert "replace_secret" in body["error"]


def test_auth_write_cross_user_and_system_records_denied(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    accounts = AccountsRepo(tmp_path / "accounts.db")
    accounts.create_user("bob", "bob@example.com", "Bob")
    bobs = repo.create_credential(
        owner_type="user",
        owner_user_id="bob",
        name="bobs",
        provider="example",
        kind="api_key",
        secret_fields={"value": "bob-secret"},
    )
    system = repo.create_credential(
        owner_type="system",
        owner_user_id=None,
        name="ops",
        provider="example",
        kind="api_key",
        secret_fields={"value": "system-secret"},
    )
    for cid in (bobs.id, system.id):
        for op in ("update", "replace_secret"):
            body = _write(
                {
                    "operation": op,
                    "credential_id": cid,
                    "provider": "renamed",
                    "secret_fields": json.dumps({"value": "sk-evil"}) if op == "replace_secret" else "",
                }
            )
            assert body["ok"] is False
            assert body["error"] == "credential not found"


def test_auth_write_replace_secret_disables_old_and_copies_everything(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    calls: list = []
    _patch_probe(monkeypatch, ok=True, calls=calls)
    old = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="rotating key",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-old"},
        metadata={"env": "prod"},
        allowed_targets=["native_tool:example_tool"],
    )
    repo.bind_credential(
        old.id,
        target_type="native_tool",
        target_id="example_tool",
        actor_user_id="alice",
    )
    body = _write(
        {
            "operation": "replace_secret",
            "credential_id": old.id,
            "secret_fields": json.dumps({"value": "sk-new"}),
        }
    )
    assert body["ok"] is True
    assert body["replaced_credential_id"] == old.id
    assert body["replaced_status"] == "disabled"
    successor_id = body["credential"]["id"]
    assert successor_id != old.id
    # Old disabled, successor carries metadata/targets/bindings.
    assert repo.get_credential(old.id).status == "disabled"
    successor = repo.get_credential(successor_id)
    assert successor.status == "active"
    assert successor.metadata == {"env": "prod"}
    assert successor.allowed_targets == ["native_tool:example_tool"]
    assert body["copied_bindings"] == 1
    bindings = repo.list_bindings(successor_id)
    assert bindings and bindings[0]["target_id"] == "example_tool"
    # The probe ran against the NEW secret; neither value leaks.
    assert calls and calls[-1]["secret_fields"] == {"value": "sk-new"}
    assert "sk-new" not in json.dumps(body) and "sk-old" not in json.dumps(body)


def test_auth_write_invalid_json_and_bind_target_are_loud(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    body = _write({"operation": "create", "provider": "x", "secret_fields": "not json"})
    assert body["ok"] is False and "not valid JSON" in body["error"]
    body = _write(
        {
            "operation": "create",
            "provider": "x",
            "secret_fields": json.dumps({"value": "sk"}),
            "bind_target": "missing-colon",
        }
    )
    assert body["ok"] is False and "target_type:target_id" in body["error"]
    body = _write(
        {
            "operation": "create",
            "provider": "x",
            "secret_fields": json.dumps({"value": ""}),
        }
    )
    assert body["ok"] is False and "non-empty" in body["error"]
    body = _write(
        {
            "operation": "create",
            "provider": "x",
            "secret_fields": json.dumps({"value": "   "}),  # whitespace-only
        }
    )
    assert body["ok"] is False and "non-empty" in body["error"]


def test_auth_write_bind_target_rejected_outside_create(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="x",
        provider="example",
        kind="api_key",
        secret_fields={"value": "sk-x"},
    )
    body = _write(
        {
            "operation": "update",
            "credential_id": record.id,
            "name": "renamed",
            "bind_target": "native_tool:example_tool",
        }
    )
    assert body["ok"] is False and "auth_bindings" in body["error"]


def test_auth_write_update_reports_previous_provider_on_rename(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="x",
        provider="old-key",
        kind="api_key",
        secret_fields={"value": "sk-x"},
    )
    body = _write({"operation": "update", "credential_id": record.id, "provider": "new-key"})
    assert body["ok"] is True
    assert body["previous_provider"] == "old-key"
    body = _write({"operation": "update", "credential_id": record.id, "name": "just a rename"})
    assert body["ok"] is True
    assert "previous_provider" not in body
