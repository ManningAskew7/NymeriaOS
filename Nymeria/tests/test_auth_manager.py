from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.tools.auth_manager import auth_bindings, auth_cleanup, auth_inspect


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
    updated = repo.get_credential(record.id)
    assert updated is not None
    assert "native_tool:example_tool" in updated.allowed_targets
