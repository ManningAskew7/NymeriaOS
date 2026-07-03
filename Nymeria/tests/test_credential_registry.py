"""Integrity and status tests for the provider credential-spec registry.

Permanent guardrails replacing the one-time migration verifier
(``scripts/verify_credential_spec_migration.py``): registry invariants, alias
resolution, taxonomy-based tool association, and the vault-backed
``provider_credential_status`` helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nymeria.tools.credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    get_provider_spec,
    iter_provider_specs,
    provider_credential_status,
    register_provider_spec,
    spec_for_tool,
)


def _spec(provider: str, **overrides) -> ProviderCredentialSpec:
    defaults = dict(
        provider=provider,
        aliases=(f"{provider}_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="token", names=("api_key", "token", "value")),
        ),
        hint_fields=("api_key", "value"),
        env_var="TESTREG_KEY",
        display_name=provider.title(),
    )
    defaults.update(overrides)
    return ProviderCredentialSpec(**defaults)


# ---------------------------------------------------------------------------
# Registration semantics
# ---------------------------------------------------------------------------


def test_register_is_idempotent_for_identical_specs():
    spec = register_provider_spec(_spec("testreg_idem"))
    assert register_provider_spec(_spec("testreg_idem")) is spec


def test_conflicting_registration_raises():
    register_provider_spec(_spec("testreg_conflict"))
    with pytest.raises(ValueError, match="conflicting"):
        register_provider_spec(_spec("testreg_conflict", env_var="OTHER"))


def test_alias_overlap_resolves_to_canonical_owner_regardless_of_order():
    # Alias registered first, canonical later: canonical takes the name over.
    one = register_provider_spec(_spec("testreg_ov_one", aliases=("testreg_ov_shared_a",)))
    shared_a = register_provider_spec(_spec("testreg_ov_shared_a", aliases=()))
    assert get_provider_spec("testreg_ov_shared_a") is shared_a
    assert get_provider_spec("testreg_ov_one") is one
    # Canonical registered first, alias later: canonical keeps the name.
    shared_b = register_provider_spec(_spec("testreg_ov_shared_b", aliases=()))
    two = register_provider_spec(_spec("testreg_ov_two", aliases=("testreg_ov_shared_b",)))
    assert get_provider_spec("testreg_ov_shared_b") is shared_b
    assert get_provider_spec("testreg_ov_two") is two


def test_alias_overlap_between_two_aliases_first_registrant_wins():
    first = register_provider_spec(_spec("testreg_aa_first", aliases=("testreg_aa_shared",)))
    second = register_provider_spec(_spec("testreg_aa_second", aliases=("testreg_aa_shared",)))
    assert get_provider_spec("testreg_aa_shared") is first
    assert get_provider_spec("testreg_aa_second") is second
    # The losing spec's aliases tuple is untouched (runtime lookup unaffected).
    assert second.aliases == ("testreg_aa_shared",)


def test_colliding_canonical_provider_names_raise():
    register_provider_spec(_spec("testreg_canon_clash"))
    with pytest.raises(ValueError, match="canonical provider names collide"):
        register_provider_spec(_spec("testreg-canon-clash", aliases=()))


def test_duplicate_group_roles_rejected():
    with pytest.raises(ValueError, match="duplicate group roles"):
        register_provider_spec(
            _spec(
                "testreg_dup_roles",
                groups=(
                    CredentialFieldGroup(role="token", names=("a",)),
                    CredentialFieldGroup(role="token", names=("b",)),
                ),
            )
        )


def test_lookup_resolves_canonical_alias_and_dash_underscore_variants():
    spec = register_provider_spec(_spec("testreg_look_up"))
    assert get_provider_spec("testreg_look_up") is spec
    assert get_provider_spec("testreg_look_up_api") is spec
    assert get_provider_spec("testreg-look-up") is spec
    assert get_provider_spec("TESTREG_LOOK_UP") is spec
    assert get_provider_spec("unknown_provider_name") is None


def test_group_lookup_is_loud_on_role_typos():
    spec = _spec("testreg_roles")
    assert spec.group("token") == ("api_key", "token", "value")
    with pytest.raises(KeyError, match="no credential field group"):
        spec.group("tokn")


# ---------------------------------------------------------------------------
# Ground-truth spot checks (exemplar module) and tool association
# ---------------------------------------------------------------------------


def test_todoist_and_trello_specs_match_pre_refactor_literals():
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    todoist = get_provider_spec("todoist")
    assert todoist is not None
    assert todoist.aliases == ("todoist_api",)
    assert todoist.group("token") == ("api_key", "access_token", "token", "value")
    assert todoist.group("base_url") == ("base_url", "url")
    base = next(g for g in todoist.groups if g.role == "base_url")
    assert base.required is False
    assert todoist.hint_fields == ("api_key", "access_token", "value")
    assert todoist.env_var == "TODOIST_API_KEY"

    trello = get_provider_spec("trello_api")
    assert trello is not None and trello.provider == "trello"
    assert {g.names for g in trello.groups} == {
        ("base_url", "url"),
        ("api_key", "key"),
        ("api_token", "token", "value"),
    }
    assert {g.role for g in trello.groups if g.required} == {"api_key", "api_token"}


def test_spec_for_tool_resolves_via_taxonomy_and_defaults_to_none():
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    spec = spec_for_tool("todoist_list_tasks")
    assert spec is not None and spec.provider == "todoist"
    assert spec_for_tool("definitely_not_an_integration_tool") is None
    assert spec_for_tool("") is None


def test_every_spec_is_reachable_from_some_catalog_tool():
    """Every registered provider must be resolvable from at least one catalog
    tool name, via the taxonomy or an explicit ``tools`` claim; otherwise its
    ``services``/``tools`` claim is a typo."""
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.integration_taxonomy import get_integration_grouping

    catalog = set(CATALOG_TOOLS)
    services_in_catalog = set()
    for name in catalog:
        grouping = get_integration_grouping(name)
        if grouping:
            services_in_catalog.add(grouping["service"])

    unreachable = [
        spec.provider
        for spec in iter_provider_specs()
        if not spec.provider.startswith("testreg_")
        and not set(spec.service_keys) & services_in_catalog
        and not set(spec.tools) & catalog
    ]
    assert unreachable == [], f"specs unreachable from any catalog tool: {unreachable}"


def test_spec_for_tool_prefers_explicit_tool_claim_over_taxonomy():
    import nymeria.tools.web_search_integrations  # noqa: F401

    # All web_search_* tools share the coarse taxonomy service "web"; the
    # explicit spec.tools claims must disambiguate them.
    for tool_name, provider in (
        ("web_search_tavily", "tavily"),
        ("web_search_exa_ai", "exa"),
        ("web_search_brave", "brave"),
        ("web_search_searxng", "searxng"),
    ):
        spec = spec_for_tool(tool_name)
        assert spec is not None and spec.provider == provider


# ---------------------------------------------------------------------------
# provider_credential_status against a real tmp vault
# ---------------------------------------------------------------------------


@dataclass
class _Settings:
    data_dir: Path


def _vault(tmp_path, monkeypatch):
    from nymeria.core.accounts import AccountsRepo
    from nymeria.core.credential_vault import CredentialVaultRepo

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = _Settings(data_dir=tmp_path)

    import nymeria.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)

    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")

    import nymeria.core.credential_vault as vault_mod

    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(vault_mod, "_vault_repo", repo)
    return repo


def test_status_needs_setup_pending_connected(tmp_path, monkeypatch):
    repo = _vault(tmp_path, monkeypatch)
    spec = _spec("testreg_status")

    assert provider_credential_status(spec, "alice") == "needs_setup"

    pending = repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="pending one",
        provider="testreg_status",
        kind="api_key",
        status="pending_setup",
    )
    assert pending is not None
    assert provider_credential_status(spec, "alice") == "pending"

    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="working one",
        provider="testreg_status_api",  # alias-named record still matches
        kind="api_key",
        secret_fields={"api_key": "sk-test-value"},
    )
    assert provider_credential_status(spec, "alice") == "connected"
    # Another user sees nothing.
    assert provider_credential_status(spec, "bob") == "needs_setup"


def test_status_requires_every_required_group(tmp_path, monkeypatch):
    repo = _vault(tmp_path, monkeypatch)
    spec = _spec(
        "testreg_twofield",
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "key")),
            CredentialFieldGroup(role="api_token", names=("api_token", "token")),
        ),
        hint_fields=("api_key", "api_token"),
    )
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="half saved",
        provider="testreg_twofield",
        kind="api_key",
        secret_fields={"api_key": "only-the-key"},
    )
    assert provider_credential_status(spec, "alice") == "needs_setup"
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="fully saved",
        provider="testreg_twofield",
        kind="api_key",
        secret_fields={"key": "the-key", "token": "the-token"},  # alt names count
    )
    assert provider_credential_status(spec, "alice") == "connected"


def test_status_optional_when_no_required_groups(tmp_path, monkeypatch):
    repo = _vault(tmp_path, monkeypatch)
    spec = _spec(
        "testreg_optional",
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
        ),
        hint_fields=(),
    )
    assert provider_credential_status(spec, "alice") == "optional"
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="override",
        provider="testreg_optional",
        kind="api_key",
        secret_fields={"base_url": "https://example.test"},
    )
    assert provider_credential_status(spec, "alice") == "connected"


def test_status_counts_system_credentials(tmp_path, monkeypatch):
    repo = _vault(tmp_path, monkeypatch)
    spec = _spec("testreg_system")
    repo.create_credential(
        owner_type="system",
        owner_user_id=None,
        name="ops-provisioned",
        provider="testreg_system",
        kind="api_key",
        secret_fields={"api_key": "system-key"},
    )
    assert provider_credential_status(spec, "alice") == "connected"
