import json

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_baserow_list_rows_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Baserow",
        provider="baserow",
        kind="api_key",
        allowed_targets=["native_tool:baserow_list_rows"],
        secret_fields={"token": "baserow-token", "host": "https://baserow.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"results": [{"id": 1, "Name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.baserow_list_rows.func(
            table_id="123",
            limit=3,
            page=2,
            search="Ada",
            filters_json='{"filter__field_Name__contains": "Ada"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["results"][0]["Name"] == "Ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://baserow.example/api/database/rows/table/123/"
    assert captured["headers"]["Authorization"] == "Token baserow-token"
    assert captured["params"]["size"] == 3
    assert captured["params"]["page"] == 2
    assert captured["params"]["search"] == "Ada"
    assert captured["params"]["filter__field_Name__contains"] == "Ada"
    assert captured["params"]["user_field_names"] == "true"


def test_nocodb_update_record_uses_env_token_and_auth_header(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("NOCODB_API_TOKEN", "nocodb-token")
    monkeypatch.setenv("NOCODB_BASE_URL", "https://nocodb.example")
    monkeypatch.setenv("NOCODB_AUTH_HEADER", "xc-auth")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"records": [{"id": "row-1", "fields": {"Name": "Ada"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.nocodb_update_record.func(
            base_id="base-1",
            table_id="table-1",
            record_id="row-1",
            fields_json='{"Name": "Ada"}',
        )
    )

    assert result["records"][0]["id"] == "row-1"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://nocodb.example/api/v3/data/base-1/table-1/records"
    assert captured["headers"]["xc-auth"] == "nocodb-token"
    assert captured["json_body"] == [{"id": "row-1", "fields": {"Name": "Ada"}}]


def test_coda_create_table_row_uses_env_token(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("CODA_API_TOKEN", "coda-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"requestId": "req-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.coda_create_table_row.func(
            doc_id="doc-1",
            table_id="table-1",
            cells_json='{"Name": "Ada", "Score": 42}',
            disable_parsing=True,
        )
    )

    assert result["requestId"] == "req-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://coda.io/apis/v1/docs/doc-1/tables/table-1/rows"
    assert captured["headers"]["Authorization"] == "Bearer coda-token"
    assert captured["json_body"] == {
        "rows": [
            {
                "cells": [
                    {"column": "Name", "value": "Ada"},
                    {"column": "Score", "value": 42},
                ]
            }
        ],
        "disableParsing": True,
    }


def test_grist_delete_records_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Grist",
        provider="grist",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"api_key": "grist-key", "base_url": "https://grist.example/api"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.grist_delete_records.func(
            doc_id="doc-1",
            table_id="People",
            row_ids="1, 2",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["deleted"] == [1, 2]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://grist.example/api/docs/doc-1/tables/People/data/delete"
    assert captured["headers"]["Authorization"] == "Bearer grist-key"
    assert captured["json_body"] == [1, 2]


def test_data_table_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    baserow_result = tools.baserow_list_tables.func()
    nocodb_result = tools.nocodb_list_bases.func()
    coda_result = tools.coda_list_docs.func()
    grist_result = tools.grist_list_orgs.func()

    assert 'provider "baserow"' in baserow_result
    assert "BASEROW_API_TOKEN" in baserow_result
    assert 'provider "nocodb"' in nocodb_result
    assert "NOCODB_API_TOKEN" in nocodb_result
    assert 'provider "coda"' in coda_result
    assert "CODA_API_TOKEN" in coda_result
    assert 'provider "grist"' in grist_result
    assert "GRIST_API_KEY" in grist_result


def test_data_table_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "baserow_list_tables",
        "baserow_list_fields",
        "baserow_list_rows",
        "baserow_get_row",
        "nocodb_list_bases",
        "nocodb_get_base",
        "nocodb_list_records",
        "nocodb_get_record",
        "nocodb_count_records",
        "coda_list_docs",
        "coda_list_tables",
        "coda_list_table_rows",
        "coda_get_table_row",
        "coda_list_formulas",
        "coda_list_controls",
        "grist_list_orgs",
        "grist_list_workspaces",
        "grist_list_docs",
        "grist_list_tables",
        "grist_list_columns",
        "grist_list_records",
    ]
    moderate_names = [
        "baserow_create_row",
        "baserow_update_row",
        "baserow_delete_row",
        "nocodb_create_record",
        "nocodb_update_record",
        "nocodb_delete_record",
        "coda_create_table_row",
        "coda_update_table_row",
        "coda_delete_table_row",
        "grist_create_record",
        "grist_update_record",
        "grist_delete_records",
    ]

    for name in safe_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_data_table_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        baserow_list_rows,
        coda_create_table_row,
        grist_list_records,
        nocodb_update_record,
    )

    assert "config" not in baserow_list_rows.args_schema.model_json_schema()["properties"]
    assert "config" not in nocodb_update_record.args_schema.model_json_schema()["properties"]
    assert "config" not in coda_create_table_row.args_schema.model_json_schema()["properties"]
    assert "config" not in grist_list_records.args_schema.model_json_schema()["properties"]
