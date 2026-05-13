from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet

from nymeria.core.http_policy import HTTPPolicyConfig
from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import (
    CredentialAccessDenied,
    CredentialVaultRepo,
    migrate_mcp_encrypted_env_vars,
)
from nymeria.core import secrets as nymeria_secrets
from nymeria.tools.http_api import _http_request_impl


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
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
        actor_user_id="alice",
        target_type="custom_tool",
        target_id="example_search",
    )
    assert resolved == "Bearer sk-test-secret"

    with pytest.raises(CredentialAccessDenied):
        repo.resolve_references(
            "Bearer ${credential:%s.value}" % record.id,
            actor_user_id="alice",
            target_type="custom_tool",
            target_id="wrong_tool",
        )


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
    assert repo.resolve_references(
        ref,
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
