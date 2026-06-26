"""Tests for managed MCP install planning."""

from __future__ import annotations

import json
import zipfile
from types import SimpleNamespace

import pytest
from nymeria.core.mcp_sources import classify_mcp_source, extract_install_source
from cryptography.fernet import Fernet

from nymeria.core.mcp_runtime import (
    MCPInstallError,
    MCPInstallPlan,
    analyze_text_source,
    apply_config_values,
    plan_bundle_file,
    plan_text_source,
    prepare_runtime,
    save_preview,
)
from nymeria.tools.definitions.mcp_schema import MCPServerDefinition


def test_plan_npm_package_url_uses_npx_runtime():
    defn, plan = plan_text_source("https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem")

    assert plan.source_type == "npm"
    assert plan.runtime_type == "npx"
    assert plan.confirmation_required is True
    assert any(signal["id"] == "package_manager" for signal in plan.risk_signals)
    assert defn.server_command == "npx"
    assert defn.server_args[:2] == ["-y", "@modelcontextprotocol/server-filesystem"]


def test_plan_pypi_package_url_uses_uvx_runtime():
    defn, plan = plan_text_source("https://pypi.org/project/mcp-server-fetch/")

    assert plan.source_type == "pypi"
    assert plan.runtime_type == "uvx"
    assert plan.confirmation_required is True
    assert any(signal["id"] == "package_manager" for signal in plan.risk_signals)
    assert defn.server_command == "uvx"
    assert defn.server_args == ["mcp-server-fetch"]


def test_plan_git_url_requires_confirmation_without_ready_command():
    defn, plan = plan_text_source("https://github.com/example/example-mcp-server")

    assert plan.source_type == "git"
    assert plan.runtime_type == "git"
    assert plan.confirmation_required is True
    assert plan.risk_level == "medium"
    assert defn.install_status == "draft"
    assert defn.server_command == ""


def test_plan_dxt_url_requires_confirmation_without_ready_command():
    defn, plan = plan_text_source("https://example.com/downloads/example-server.dxt")

    assert plan.source_type == "bundle_url"
    assert plan.runtime_type == "bundle"
    assert plan.confirmation_required is True
    assert plan.risk_level == "medium"
    assert plan.source_url == "https://example.com/downloads/example-server.dxt"
    assert defn.install_status == "draft"
    assert defn.server_command == ""


def test_plan_registry_id_delegates_to_registry_resolver(monkeypatch):
    def resolve(server_id: str) -> MCPServerDefinition:
        assert server_id == "io.github.example/registry-mcp"
        return MCPServerDefinition(
            id="registry-mcp-123abc",
            name="Registry MCP",
            transport="stdio",
            server_command="npx",
            server_args=["-y", "@example/registry-mcp"],
        )

    monkeypatch.setattr("nymeria.core.mcp_registry_client.resolve_registry_id", resolve)

    defn, plan = plan_text_source("io.github.example/registry-mcp")

    assert plan.source_type == "registry"
    assert plan.runtime_type == "npx"
    assert plan.confirmation_required is True
    assert defn.original_source == "io.github.example/registry-mcp"
    assert defn.server_args == ["-y", "@example/registry-mcp"]


def test_analyze_multi_server_json_returns_candidates():
    source = {
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
            "files": {"command": "npx", "args": ["-y", "@example/files"]},
        }
    }

    candidates = analyze_text_source(json.dumps(source))

    assert [candidate.title for candidate in candidates] == ["fetch", "files"]
    assert candidates[0].id == "candidate-1"
    assert candidates[1].definition.server_command == "npx"


def test_plan_env_prefixed_command_extracts_env_vars():
    defn, plan = plan_text_source("API_TOKEN=secret-token-value-1234567890 npx -y @example/mcp")

    assert defn.server_command == "npx"
    assert defn.env_vars["API_TOKEN"] == ""
    assert "secret-token-value" not in defn.model_dump_json()
    assert plan.credential_requirements[0]["source"] == "env"


def test_apply_config_values_converts_plaintext_secret_env_to_vault(tmp_path, monkeypatch):
    from nymeria.core import credential_vault
    from nymeria.core.accounts import AccountsRepo
    from nymeria.core.credential_vault import CREDENTIAL_REF_PATTERN, CredentialVaultRepo

    db_path = tmp_path / "credentials.db"
    AccountsRepo(db_path).create_user("alice", "alice@example.com", "Alice")
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode("ascii"))

    source = {
        "mcpServers": {
            "example": {
                "command": "npx",
                "args": ["-y", "@example/mcp"],
                "env": {"EXAMPLE_API_KEY": "sk-test-secret-value-1234567890"},
            }
        }
    }
    defn, plan = plan_text_source(json.dumps(source))

    assert defn.env_vars["EXAMPLE_API_KEY"] == ""
    assert "sk-test-secret-value" not in defn.model_dump_json()

    defn, missing = apply_config_values(
        defn,
        plan,
        credential_values={"EXAMPLE_API_KEY": "sk-test-secret-value-1234567890"},
        user_id="alice",
    )

    assert missing == []
    ref = defn.env_vars["EXAMPLE_API_KEY"]
    match = CREDENTIAL_REF_PATTERN.fullmatch(ref)
    assert match is not None
    credential_id = match.group(1)
    record = repo.get_credential(credential_id)
    assert record is not None
    assert record.owner_user_id == "alice"
    assert record.allowed_targets == [f"mcp_server:{defn.id}"]
    assert repo.get_secret_field(
        credential_id,
        "value",
        target_type="mcp_server",
        target_id=defn.id,
    ) == "sk-test-secret-value-1234567890"
    assert "sk-test-secret" not in defn.model_dump_json()
    assert defn.encrypted_env_vars == {}


def test_preview_artifact_does_not_store_plaintext_secret(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nymeria.core import mcp_runtime

    monkeypatch.setattr(
        mcp_runtime,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    source = json.dumps({
        "mcpServers": {
            "example": {
                "command": "npx",
                "args": ["-y", "@example/mcp"],
                "env": {"EXAMPLE_API_KEY": "sk-test-secret-value-1234567890"},
            }
        }
    })

    candidates = analyze_text_source(source)
    token = save_preview(
        candidates[0].definition,
        candidates[0].plan,
        source=source,
        candidates=candidates,
    )

    preview_text = (tmp_path / "mcp_install_previews" / f"{token}.json").read_text()
    assert "sk-test-secret-value" not in preview_text
    assert "${credential:redacted}" in preview_text


def test_plan_claude_json_detects_missing_secret_env():
    source = {
        "mcpServers": {
            "example": {
                "command": "uvx",
                "args": ["example-mcp"],
                "env": {"EXAMPLE_API_KEY": ""},
            }
        }
    }

    _defn, plan = plan_text_source(json.dumps(source))

    assert plan.source_type == "json"
    assert plan.runtime_type == "uvx"
    assert plan.required_config[0]["name"] == "EXAMPLE_API_KEY"
    assert plan.required_config[0]["sensitive"] is True


def test_plan_extracts_command_from_markdown_fence():
    source = """
Install with:

```bash
npx -y @example/mcp-server
```
"""

    defn, plan = plan_text_source(source)

    assert plan.source_type == "stdio"
    assert plan.runtime_type == "npx"
    assert defn.server_command == "npx"
    assert defn.server_args == ["-y", "@example/mcp-server"]


def test_prepare_runtime_blocks_unsandboxed_managed_package_installs(monkeypatch):
    from nymeria.core import mcp_runtime

    monkeypatch.setattr(
        mcp_runtime,
        "get_settings",
        lambda: SimpleNamespace(nymeria_allow_unsandboxed_mcp_install=False),
    )
    defn, plan = plan_text_source("npx -y @example/mcp-server")

    with pytest.raises(MCPInstallError, match="Managed MCP installs"):
        prepare_runtime(defn, plan)


def test_bundle_download_blocks_private_network_targets(tmp_path, monkeypatch):
    from nymeria.core import mcp_runtime
    import requests

    def fail_get(*args, **kwargs):
        raise AssertionError("blocked download should not issue an HTTP request")

    monkeypatch.setattr(requests, "get", fail_get)

    with pytest.raises(MCPInstallError, match="egress policy"):
        mcp_runtime._download("http://127.0.0.1:8000/server.mcpb", tmp_path / "server.mcpb")


def test_mcp_registry_base_url_blocks_private_network():
    from nymeria.core.mcp_registry_client import OfficialMCPRegistryFetcher, RegistryError

    with pytest.raises(RegistryError, match="egress policy"):
        OfficialMCPRegistryFetcher(base_url="http://127.0.0.1:8000")


def test_shared_source_classifier_covers_install_surfaces():
    assert classify_mcp_source("https://www.npmjs.com/package/@example/mcp").kind == "npm"
    assert classify_mcp_source("https://pypi.org/project/example-mcp/").kind == "pypi"
    assert classify_mcp_source("https://github.com/example/mcp").kind == "git"
    assert classify_mcp_source("https://example.com/mcp/example.mcpb").kind == "bundle_url"
    assert classify_mcp_source("https://example.com/mcp/example.dxt").kind == "bundle_url"
    assert classify_mcp_source("io.github.example/mcp").kind == "registry"
    assert classify_mcp_source("npx -y @example/mcp").kind == "stdio"
    assert (
        extract_install_source("Run this:\n\n```bash\nnpx -y @example/mcp\n```")
        == "npx -y @example/mcp"
    )


def test_plan_bundle_upload_reads_manifest_user_config(tmp_path):
    bundle = tmp_path / "example.mcpb"
    manifest = {
        "name": "example-bundle",
        "display_name": "Example Bundle",
        "description": "Example MCP bundle",
        "user_config": {
            "token": {
                "title": "API token",
                "type": "string",
                "required": True,
                "sensitive": True,
            }
        },
        "server": {
            "type": "node",
            "entry_point": "server.js",
        },
    }
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("server.js", "console.error('noop');")

    defn, plan = plan_bundle_file(bundle, original_name="example.mcpb")

    assert defn.name == "Example Bundle"
    assert plan.source_type == "bundle_upload"
    assert plan.runtime_type == "bundle"
    assert plan.confirmation_required is True
    assert plan.required_config[0]["name"] == "token"
    assert plan.required_config[0]["sensitive"] is True


# ---------------------------------------------------------------------------
# apply_config_values characterization tests.
#
# These lock the per-branch behavior of apply_config_values before the F7
# decomposition (slice 05) and serve as the regression guard afterward. The
# function had only one direct test (the sensitive-env-with-value path); these
# cover the header/auth-prefix path, the credential-binding path, the
# non-sensitive required/missing/default branches, the env/header sweep
# branches, and the cross-loop interaction where loop 1 writes a plaintext
# non-sensitive value that the loop 2 sweep then converts to a vault reference.
# ---------------------------------------------------------------------------

_SECRET = "sk-test-secret-value-1234567890"


def _make_vault(tmp_path, monkeypatch, *, user_id="alice", with_secrets_key=True):
    """Wire a real CredentialVaultRepo and point mcp_runtime's lookups at it."""
    from nymeria.core import credential_vault
    from nymeria.core.accounts import AccountsRepo
    from nymeria.core.credential_vault import CredentialVaultRepo

    db_path = tmp_path / "credentials.db"
    AccountsRepo(db_path).create_user(user_id, f"{user_id}@example.com", user_id.title())
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)
    if with_secrets_key:
        monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    else:
        monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    return repo


def _stdio_plan(required_config):
    return MCPInstallPlan(
        source_type="stdio",
        runtime_type="direct",
        risk_level="low",
        confirmation_required=False,
        parsed_summary="characterization test",
        required_config=list(required_config),
    )


def _stdio_defn(**kwargs):
    return MCPServerDefinition(
        id="srv-characterization-1",
        name="Characterization Server",
        transport="stdio",
        server_command="echo",
        **kwargs,
    )


def _stored_secret(repo, ref, defn):
    from nymeria.core.credential_vault import CREDENTIAL_REF_PATTERN

    assert "${credential:" in ref
    inner = ref.split("${credential:", 1)[1].rstrip("}")
    credential_id = inner.split(".", 1)[0]
    assert CREDENTIAL_REF_PATTERN.fullmatch(f"${{credential:{credential_id}.value}}")
    return repo.get_secret_field(
        credential_id,
        "value",
        target_type="mcp_server",
        target_id=defn.id,
    )


def test_apply_config_values_header_auth_prefix_strips_and_reprefixes(tmp_path, monkeypatch):
    repo = _make_vault(tmp_path, monkeypatch)
    plan = _stdio_plan([
        {
            "name": "API_KEY",
            "source": "header",
            "header_name": "Authorization",
            "auth_prefix": "Bearer ",
            "required": True,
            "sensitive": True,
        }
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(
        defn,
        plan,
        credential_values={"API_KEY": f"Bearer {_SECRET}"},
        user_id="alice",
    )

    assert missing == []
    header = defn.headers["Authorization"]
    assert header.startswith("Bearer ${credential:")
    # The auth prefix is stripped before storage and re-applied to the ref.
    assert _stored_secret(repo, header, defn) == _SECRET
    assert _SECRET not in defn.model_dump_json()


def test_apply_config_values_uses_supplied_binding_without_minting(tmp_path, monkeypatch):
    repo = _make_vault(tmp_path, monkeypatch)
    repo.upsert_credential(
        credential_id="cred_existing_key",
        owner_type="user",
        owner_user_id="alice",
        name="Existing Key",
        provider="manual",
        kind="secret",
        secret_fields={"value": _SECRET},
        allowed_targets=["mcp_server:srv-characterization-1"],
        status="active",
        actor_user_id="alice",
    )
    plan = _stdio_plan([
        {"name": "API_KEY", "source": "env", "env_name": "API_KEY", "required": True, "sensitive": True}
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(
        defn,
        plan,
        credential_values={"API_KEY": _SECRET},
        credential_bindings={"API_KEY": {"credential_id": "cred_existing_key", "field": "value"}},
        user_id="alice",
    )

    assert missing == []
    # The binding short-circuits minting: the existing credential ref is reused
    # and no new cred_mcp_* credential is created.
    assert defn.env_vars["API_KEY"] == "${credential:cred_existing_key.value}"
    assert repo.get_credential("cred_existing_key") is not None


def test_apply_config_values_non_sensitive_required_missing_is_reported():
    plan = _stdio_plan([
        {"name": "PORT", "source": "env", "env_name": "PORT", "required": True, "sensitive": False}
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(defn, plan)

    assert missing == [plan.required_config[0]]
    assert "PORT" not in defn.env_vars


def test_apply_config_values_non_sensitive_value_written_plain():
    # config_values-only path (the tools/search_mcp.py _mcp_configure_credentials caller).
    plan = _stdio_plan([
        {"name": "REGION", "source": "env", "env_name": "REGION", "required": True, "sensitive": False}
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(defn, plan, config_values={"REGION": "us-east-1"})

    assert missing == []
    assert defn.env_vars["REGION"] == "us-east-1"
    assert defn.encrypted_env_vars == {}


def test_apply_config_values_applies_default_when_value_absent():
    plan = _stdio_plan([
        {
            "name": "TIMEOUT",
            "source": "env",
            "env_name": "TIMEOUT",
            "required": True,
            "sensitive": False,
            "default": "30",
        }
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(defn, plan)

    assert missing == []
    assert defn.env_vars["TIMEOUT"] == "30"


def test_apply_config_values_sweep_resolves_env_placeholder(tmp_path, monkeypatch):
    repo = _make_vault(tmp_path, monkeypatch)
    plan = _stdio_plan([])
    defn = _stdio_defn(env_vars={"SOME_TOKEN": "${env:SOME_TOKEN}"})

    defn, missing = apply_config_values(
        defn,
        plan,
        credential_values={"SOME_TOKEN": _SECRET},
        user_id="alice",
    )

    ref = defn.env_vars["SOME_TOKEN"]
    assert ref.startswith("${credential:")
    assert _stored_secret(repo, ref, defn) == _SECRET


def test_apply_config_values_sweep_strips_header_auth_prefix(tmp_path, monkeypatch):
    repo = _make_vault(tmp_path, monkeypatch)
    plan = _stdio_plan([])
    defn = _stdio_defn(headers={"Authorization": f"Bearer {_SECRET}"})

    defn, missing = apply_config_values(defn, plan, user_id="alice")

    header = defn.headers["Authorization"]
    assert header.startswith("Bearer ${credential:")
    assert _stored_secret(repo, header, defn) == _SECRET
    assert _SECRET not in defn.model_dump_json()


def test_apply_config_values_cross_loop_converts_secretlike_nonsensitive_value(tmp_path, monkeypatch):
    # Loop 1 writes a non-sensitive plaintext value; loop 2's sweep detects it is
    # secret-looking by VALUE (the key name is not secret-looking) and converts it.
    repo = _make_vault(tmp_path, monkeypatch)
    secretlike = "abcd1234abcd1234abcd1234abcd1234"  # 32 chars -> matches SECRET_VALUE_RE
    plan = _stdio_plan([
        {"name": "EXAMPLE_SETTING", "source": "env", "env_name": "EXAMPLE_SETTING", "required": True, "sensitive": False}
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(
        defn,
        plan,
        config_values={"EXAMPLE_SETTING": secretlike},
        user_id="alice",
    )

    ref = defn.env_vars["EXAMPLE_SETTING"]
    assert ref.startswith("${credential:")
    assert _stored_secret(repo, ref, defn) == secretlike
    assert secretlike not in defn.model_dump_json()


def test_apply_config_values_credential_values_take_precedence_over_config(tmp_path, monkeypatch):
    # Pins the supplied_value lookup order: credential_values (secret_values) is
    # consulted before config_values for the same key.
    repo = _make_vault(tmp_path, monkeypatch)
    plan = _stdio_plan([
        {"name": "API_KEY", "source": "env", "env_name": "API_KEY", "required": True, "sensitive": True}
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(
        defn,
        plan,
        config_values={"API_KEY": "config-value-should-not-win"},
        credential_values={"API_KEY": _SECRET},
        user_id="alice",
    )

    assert missing == []
    assert _stored_secret(repo, defn.env_vars["API_KEY"], defn) == _SECRET


def test_apply_config_values_missing_secrets_key_marks_pending(tmp_path, monkeypatch):
    # Characterizes the UNTOUCHED _credential_ref_for_secret no-key branch reached
    # via the sensitive path: with no NYMERIA_SECRETS_KEY the secret cannot be
    # stored, so it is reported in missing with the key-required error.
    _make_vault(tmp_path, monkeypatch, with_secrets_key=False)
    plan = _stdio_plan([
        {"name": "API_KEY", "source": "env", "env_name": "API_KEY", "required": True, "sensitive": True}
    ])
    defn = _stdio_defn()

    defn, missing = apply_config_values(
        defn,
        plan,
        credential_values={"API_KEY": _SECRET},
        user_id="alice",
    )

    assert len(missing) == 1
    assert "NYMERIA_SECRETS_KEY" in str(missing[0].get("error", ""))
    assert defn.env_vars["API_KEY"].startswith("${credential:")
