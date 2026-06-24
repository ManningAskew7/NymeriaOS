"""Tests for the shared ``${env:}`` / ``${credential:}`` interpolation helpers.

These lock the behavior the three former copies (mcp_manager stdio env, mcp_manager
http headers, custom_tools http tools) now share, including the two policy axes
that used to differ between them: env-grammar scope (embedded refs) and the
missing-variable policy (raise vs empty).
"""

from __future__ import annotations

import pytest

from nymeria.core import credential_vault
from nymeria.core import secret_interpolation as si


# ---------------------------------------------------------------------------
# env interpolation
# ---------------------------------------------------------------------------


def test_env_bare_ref_resolves(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc123")
    resolved, used = si.interpolate_env_vars_with_names("${env:MY_TOKEN}")
    assert resolved == "abc123"
    assert used == {"MY_TOKEN"}


def test_env_embedded_ref_resolves(monkeypatch):
    # This is the headline fix: a placeholder that is NOT the whole value still
    # resolves (the old mcp_manager slice left it literal).
    monkeypatch.setenv("MY_TOKEN", "abc123")
    resolved, used = si.interpolate_env_vars_with_names("Bearer ${env:MY_TOKEN}")
    assert resolved == "Bearer abc123"
    assert used == {"MY_TOKEN"}


def test_env_multiple_refs_resolve(monkeypatch):
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    resolved, used = si.interpolate_env_vars_with_names("a=${env:A};b=${env:B}")
    assert resolved == "a=1;b=2"
    assert used == {"A", "B"}


def test_env_no_ref_passthrough():
    resolved, used = si.interpolate_env_vars_with_names("plain-value")
    assert resolved == "plain-value"
    assert used == set()


def test_env_grammar_allows_lowercase_and_mixed(monkeypatch):
    monkeypatch.setenv("lower_case", "lc")
    monkeypatch.setenv("MixedCase1", "mc")
    resolved, used = si.interpolate_env_vars_with_names(
        "${env:lower_case}/${env:MixedCase1}"
    )
    assert resolved == "lc/mc"
    assert used == {"lower_case", "MixedCase1"}


def test_env_non_identifier_name_left_literal(monkeypatch):
    # Hyphenated names are not valid POSIX identifiers; the placeholder is left
    # literal rather than resolved (documented, accepted edge of the convergence).
    monkeypatch.setenv("MY-VAR", "x")
    resolved, used = si.interpolate_env_vars_with_names("${env:MY-VAR}")
    assert resolved == "${env:MY-VAR}"
    assert used == set()


def test_env_missing_raise(monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(ValueError, match="DOES_NOT_EXIST"):
        si.interpolate_env_vars_with_names("${env:DOES_NOT_EXIST}", missing="raise")


def test_env_missing_empty(monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    resolved, used = si.interpolate_env_vars_with_names(
        "x=${env:DOES_NOT_EXIST}", missing="empty"
    )
    assert resolved == "x="
    assert used == set()


def test_env_missing_default_is_raise(monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(ValueError):
        si.interpolate_env_vars_with_names("${env:DOES_NOT_EXIST}")


# ---------------------------------------------------------------------------
# credential interpolation
# ---------------------------------------------------------------------------


class _FakeVault:
    """Records resolve_references calls and substitutes a known credential ref."""

    def __init__(self):
        self.calls = []

    def resolve_references(
        self,
        value,
        *,
        actor_user_id=None,
        target_type=None,
        target_id=None,
        used_credentials=None,
        redact_values=None,
    ):
        self.calls.append(
            {
                "value": value,
                "actor_user_id": actor_user_id,
                "target_type": target_type,
                "target_id": target_id,
                "used_credentials": used_credentials,
                "redact_values": redact_values,
            }
        )
        if used_credentials is not None:
            used_credentials.add("cred1")
        if redact_values is not None:
            redact_values.add("SEKRET")
        return value.replace("${credential:cred1.token}", "SEKRET")


def test_resolve_credential_refs_no_marker_skips_vault(monkeypatch):
    def boom():
        raise AssertionError("vault must not be consulted for plain values")

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", boom)
    assert si.resolve_credential_refs("no-creds-here") == "no-creds-here"


def test_resolve_credential_refs_delegates_with_kwargs(monkeypatch):
    fake = _FakeVault()
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: fake)
    used = set()
    redact = set()
    out = si.resolve_credential_refs(
        "Bearer ${credential:cred1.token}",
        target_type="mcp_server",
        target_id="srv1",
        used_credentials=used,
        actor_user_id="user-9",
        redact_values=redact,
    )
    assert out == "Bearer SEKRET"
    assert used == {"cred1"}
    assert redact == {"SEKRET"}
    call = fake.calls[0]
    assert call["actor_user_id"] == "user-9"
    assert call["target_type"] == "mcp_server"
    assert call["target_id"] == "srv1"


# ---------------------------------------------------------------------------
# combined env-then-credential
# ---------------------------------------------------------------------------


def test_resolve_env_and_credential_order_and_accumulation(monkeypatch):
    # An env var whose VALUE is a credential ref proves env resolves first, then
    # the credential pass runs over the env-resolved string.
    monkeypatch.setenv("CRED_REF", "${credential:cred1.token}")
    fake = _FakeVault()
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: fake)
    used_creds: set[str] = set()
    used_env: set[str] = set()
    out = si.resolve_env_and_credential_refs(
        "${env:CRED_REF}",
        target_type="mcp_server",
        target_id="srv1",
        used_credentials=used_creds,
        used_env_names=used_env,
    )
    assert out == "SEKRET"
    assert used_env == {"CRED_REF"}
    assert used_creds == {"cred1"}


def test_resolve_env_and_credential_missing_env_empty(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)

    def boom():
        raise AssertionError("vault must not be consulted")

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", boom)
    out = si.resolve_env_and_credential_refs("a=${env:NOPE}", missing_env="empty")
    assert out == "a="


def test_resolve_env_and_credential_missing_env_raise(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ValueError, match="NOPE"):
        si.resolve_env_and_credential_refs("${env:NOPE}", missing_env="raise")
