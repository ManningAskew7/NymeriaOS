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


def test_erpnext_list_documents_uses_env_token_auth_and_filters(monkeypatch):
    from nymeria.tools import enterprise_business_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ERPNEXT_BASE_URL", "https://erp.example")
    monkeypatch.setenv("ERPNEXT_API_KEY", "erp-key")
    monkeypatch.setenv("ERPNEXT_API_SECRET", "erp-secret")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
            }
        )
        return {"data": [{"name": "CUST-001", "customer_name": "Alice"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.erpnext_list_documents.func(
            doc_type="Customer",
            fields="name,customer_name",
            filters_json='[["Customer","customer_name","=","Alice"]]',
            limit=10,
        )
    )

    assert result == [{"name": "CUST-001", "customer_name": "Alice"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://erp.example/api/resource/Customer"
    assert captured["headers"]["Authorization"] == "token erp-key:erp-secret"
    assert captured["params"]["fields"] == '["name", "customer_name"]'
    assert captured["params"]["filters"] == '[["Customer", "customer_name", "=", "Alice"]]'
    assert captured["params"]["limit_page_length"] == 10


def test_odoo_create_record_uses_vault_credentials_and_jsonrpc(tmp_path, monkeypatch):
    from nymeria.tools import enterprise_business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Odoo",
        provider="odoo",
        kind="api_key",
        allowed_targets=["native_tool:odoo_create_record"],
        secret_fields={
            "url": "https://odoo.example",
            "username": "alice@example.com",
            "password": "odoo-key",
            "database": "prod",
        },
        created_by_user_id="alice",
    )
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        if json_body["params"]["service"] == "common":
            return {"result": 7}
        return {"result": 42}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.odoo_create_record.func(
            model="contact",
            fields_json='{"name": "Alice", "email": "alice@example.com"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"id": 42}
    assert [call["url"] for call in calls] == ["https://odoo.example/jsonrpc", "https://odoo.example/jsonrpc"]
    assert calls[0]["json_body"]["params"]["service"] == "common"
    assert calls[0]["json_body"]["params"]["args"] == ["prod", "alice@example.com", "odoo-key"]
    assert calls[1]["json_body"]["params"]["service"] == "object"
    assert calls[1]["json_body"]["params"]["method"] == "execute_kw"
    assert calls[1]["json_body"]["params"]["args"][:5] == [
        "prod",
        7,
        "odoo-key",
        "res.partner",
        "create",
    ]
    assert calls[1]["json_body"]["params"]["args"][5] == [
        {"name": "Alice", "email": "alice@example.com"}
    ]


def test_invoiceninja_create_record_uses_v5_headers_and_query(monkeypatch):
    from nymeria.tools import enterprise_business_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("INVOICENINJA_API_TOKEN", "a" * 64)
    monkeypatch.setenv("INVOICENINJA_SECRET", "ninja-secret")
    monkeypatch.setenv("INVOICENINJA_BASE_URL", "https://ninja.example")
    monkeypatch.setenv("INVOICENINJA_API_VERSION", "v5")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"data": {"id": "inv-1", "number": json_body["number"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.invoiceninja_create_record.func(
            resource="invoice",
            fields_json='{"number": "INV-001", "client_id": "client-1"}',
            query_json='{"send_email": true}',
        )
    )

    assert result == {"id": "inv-1", "number": "INV-001"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://ninja.example/api/v1/invoices"
    assert captured["headers"]["X-API-TOKEN"] == "a" * 64
    assert captured["headers"]["X-API-SECRET"] == "ninja-secret"
    assert captured["params"] == {"send_email": True}
    assert captured["json_body"] == {"number": "INV-001", "client_id": "client-1"}


def test_enterprise_business_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "erpnext_get_logged_user",
        "erpnext_list_documents",
        "erpnext_get_document",
        "odoo_get_server_version",
        "odoo_list_records",
        "odoo_get_record",
        "invoiceninja_list_records",
        "invoiceninja_get_record",
    }
    moderate_names = {
        "erpnext_create_document",
        "erpnext_update_document",
        "erpnext_delete_document",
        "odoo_create_record",
        "odoo_update_record",
        "odoo_delete_record",
        "invoiceninja_create_record",
        "invoiceninja_delete_record",
        "invoiceninja_email_invoice_or_quote",
    }

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


def test_enterprise_business_tool_schemas_hide_runtime_config():
    from nymeria.tools.enterprise_business_service_integrations import (
        erpnext_list_documents,
        invoiceninja_create_record,
        odoo_create_record,
    )

    assert "config" not in erpnext_list_documents.args_schema.model_json_schema()["properties"]
    assert "config" not in odoo_create_record.args_schema.model_json_schema()["properties"]
    assert "config" not in invoiceninja_create_record.args_schema.model_json_schema()["properties"]
