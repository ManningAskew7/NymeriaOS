"""Tests for the enable-time credential nudges (dev-todo #8) and their
rendering in ``_format_binding_result``.

A new file (rather than an append to tests/test_dynamic_tool_binding.py) because
these need the pytest tmp-vault fixture pattern from
tests/test_credential_registry.py, which does not fit that file's unittest.TestCase
style. The fixture: NYMERIA_SECRETS_KEY set, config.get_settings monkeypatched to
a data_dir dataclass, an AccountsRepo user "alice" created FIRST (FK requirement),
then a CredentialVaultRepo on the same db bound onto credential_vault._vault_repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from nymeria.tools.tool_search import (
    _BindingDelta,
    _credential_nudges,
    _format_binding_result,
    _ReloadDecision,
)


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


# ---------------------------------------------------------------------------
# _credential_nudges
# ---------------------------------------------------------------------------


def test_nudge_needs_setup_line(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)

    lines = _credential_nudges(["todoist_list_tasks"], "alice")
    assert len(lines) == 1
    assert 'provider "todoist"' in lines[0]
    assert "no credential saved" in lines[0]


def test_nudge_connected_suppressed(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _vault(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="todoist key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "sk-x"},
    )
    assert _credential_nudges(["todoist_list_tasks"], "alice") == []


def test_nudge_pending_line(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _vault(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="pending todoist",
        provider="todoist",
        kind="api_key",
        status="pending_setup",
    )
    lines = _credential_nudges(["todoist_list_tasks"], "alice")
    assert len(lines) == 1
    assert 'provider "todoist"' in lines[0]
    assert "pending" in lines[0]


def test_nudge_unknown_and_empty_tool_names(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    # Non-integration / unknown tool -> no provider spec -> nothing.
    assert _credential_nudges(["definitely_not_an_integration_tool"], "alice") == []
    # Empty list short-circuits.
    assert _credential_nudges([], "alice") == []


def test_nudges_multi_provider_dedup_and_sorted(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    # Two todoist tools (dedup to one line) + one trello tool -> two lines,
    # provider-sorted (todoist before trello).
    lines = _credential_nudges(
        ["todoist_list_tasks", "todoist_create_task", "trello_get_board"],
        "alice",
    )
    assert len(lines) == 2
    assert 'provider "todoist"' in lines[0]
    assert 'provider "trello"' in lines[1]


def test_nudge_optional_line(tmp_path, monkeypatch):
    # searxng has no required credential groups -> "optional" -> optional line.
    import nymeria.tools.web_search_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    lines = _credential_nudges(["web_search_searxng"], "alice")
    assert len(lines) == 1
    assert 'provider "searxng"' in lines[0]
    assert "works without a credential" in lines[0]


def test_nudges_survive_vault_outage_degrade_to_needs_setup(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _vault(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("vault down")

    monkeypatch.setattr(repo, "list_credentials", boom)

    # The raise is swallowed INSIDE provider_credential_status_map (records=[]),
    # so todoist degrades to needs_setup and still produces a line (not []).
    lines = _credential_nudges(["todoist_list_tasks"], "alice")
    assert len(lines) == 1
    assert 'provider "todoist"' in lines[0]
    assert "no credential saved" in lines[0]


# ---------------------------------------------------------------------------
# _format_binding_result: nudges render after warnings
# ---------------------------------------------------------------------------


def _binding() -> _BindingDelta:
    return _BindingDelta(
        newly_added=["todoist_list_tasks"],
        refreshed=[],
        promoted=[],
        already_default=[],
        already_permanent=[],
        un_disabled=[],
        new_enabled={"todoist_list_tasks"},
        new_temporary={},
        new_disabled=set(),
        original_enabled=[],
        original_disabled=[],
        original_temporary={},
        prior_count=5,
        new_count=6,
    )


def _reload() -> _ReloadDecision:
    return _ReloadDecision(
        will_reload=False,
        cap_hit=False,
        reload_tools=["todoist_list_tasks"],
        current_reloads=0,
        reload_cap=1,
    )


def test_format_binding_result_renders_nudges():
    nudge = '[Credential]: provider "todoist": no credential saved.'
    text = _format_binding_result(
        binding=_binding(),
        reload=_reload(),
        valid=["todoist_list_tasks"],
        invalid=[],
        unloadable=[],
        warnings=[],
        ttl_key="2h",
        ttl_seconds=7200,
        credential_nudges=[nudge],
    )
    assert nudge in text


def test_format_binding_result_omits_nudges_when_none():
    text = _format_binding_result(
        binding=_binding(),
        reload=_reload(),
        valid=["todoist_list_tasks"],
        invalid=[],
        unloadable=[],
        warnings=[],
        ttl_key="2h",
        ttl_seconds=7200,
        credential_nudges=None,
    )
    assert "[Credential]" not in text
