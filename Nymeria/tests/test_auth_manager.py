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
