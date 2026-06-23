"""Behavior lock for the shared service-integration test helpers (slice 34 F1)."""

import os

import nymeria.core.credential_vault as credential_vault
from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo,
    make_vault_repo,
)
from nymeria.core.credential_vault import CredentialVaultRepo


def test_make_vault_repo_seeds_alice_and_returns_working_repo(tmp_path, monkeypatch):
    repo = make_vault_repo(tmp_path, monkeypatch)

    assert isinstance(repo, CredentialVaultRepo)
    # A throwaway secrets key was installed so the vault can encrypt.
    assert os.environ["NYMERIA_SECRETS_KEY"]

    # Creating a user-owned credential proves the seeded ``alice`` account
    # exists, and the secret round-trips through encryption with the key above.
    record = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Example",
        provider="example",
        kind="api_key",
        secret_fields={"token": "alice-token"},
        created_by_user_id="alice",
    )
    assert repo.get_secret_field(record.id, "token", actor_user_id="alice") == "alice-token"


def test_bind_vault_repo_routes_global_resolver(tmp_path, monkeypatch):
    repo = make_vault_repo(tmp_path, monkeypatch)

    bind_vault_repo(monkeypatch, repo)

    assert credential_vault.get_credential_vault_repo() is repo
