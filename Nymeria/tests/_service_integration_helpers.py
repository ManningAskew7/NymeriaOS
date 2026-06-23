"""Shared helpers for the ``*_service_integrations.py`` test cluster.

Centralizes the credential-vault setup that ~33 service-integration test files
previously copied verbatim as module-local ``_repo`` / ``_use_repo`` helpers
(optimization slice 34 F1). Each consumer imports these under its existing
private names so the call sites stay byte-identical::

    from _service_integration_helpers import (  # type: ignore[import-not-found]
        bind_vault_repo as _use_repo,
        make_vault_repo as _repo,
    )

The autouse ``clear_settings_cache`` fixture that those files also duplicated now
lives once in ``tests/conftest.py``.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


def make_vault_repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    """Build a fresh ``CredentialVaultRepo`` seeded with the ``alice`` user.

    Sets a throwaway ``NYMERIA_SECRETS_KEY`` so the vault can encrypt secrets,
    backs the repo with a SQLite file under ``tmp_path``, and creates the
    ``alice`` account the integration tests authenticate as.
    """
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def bind_vault_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    """Route ``get_credential_vault_repo()`` to ``repo`` for the test."""
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)
