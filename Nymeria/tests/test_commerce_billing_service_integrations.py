import base64
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


def test_stripe_create_customer_uses_env_key_and_form(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "form_data": form_data,
                "headers": headers,
            }
        )
        return {"id": "cus_123", "email": "alice@example.com"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.stripe_create_customer.func(
            email="alice@example.com",
            name="Alice",
            description="Test customer",
            metadata_json='{"source":"nymeria"}',
        )
    )

    assert result["id"] == "cus_123"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.stripe.com/v1/customers"
    assert captured["headers"]["Authorization"] == "Bearer sk_test_123"
    assert captured["form_data"]["email"] == "alice@example.com"
    assert captured["form_data"]["name"] == "Alice"
    assert captured["form_data"]["description"] == "Test customer"
    assert captured["form_data"]["metadata[source]"] == "nymeria"


def test_stripe_search_records_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Stripe",
        provider="stripe",
        kind="api_key",
        allowed_targets=["native_tool:stripe_search_records"],
        secret_fields={
            "secret_key": "sk_live_123",
            "base_url": "https://stripe.example/v1",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"data": [{"id": "cus_123"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.stripe_search_records.func(
            resource="customers",
            query='email:"alice@example.com"',
            limit=5,
            page="next-page",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"id": "cus_123"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://stripe.example/v1/customers/search"
    assert captured["params"]["query"] == 'email:"alice@example.com"'
    assert captured["params"]["limit"] == 5
    assert captured["params"]["page"] == "next-page"
    assert captured["headers"]["Authorization"] == "Bearer sk_live_123"


def test_shopify_list_records_uses_env_access_token_and_shop(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SHOPIFY_SHOP", "example")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shop-token")
    monkeypatch.setenv("SHOPIFY_API_VERSION", "2026-01")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"products": [{"id": 1, "title": "Backpack"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.shopify_list_records.func(
            resource="products",
            limit=10,
            status="active",
            fields="id,title",
        )
    )

    assert result == [{"id": 1, "title": "Backpack"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://example.myshopify.com/admin/api/2026-01/products.json"
    assert captured["params"]["limit"] == 10
    assert captured["params"]["status"] == "active"
    assert captured["params"]["fields"] == "id,title"
    assert captured["headers"]["X-Shopify-Access-Token"] == "shop-token"


def test_shopify_update_product_uses_vault_basic_auth(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Shopify legacy",
        provider="shopify",
        kind="api_key",
        allowed_targets=["native_tool:shopify_update_product"],
        secret_fields={
            "shopSubdomain": "vault-shop.myshopify.com",
            "apiKey": "legacy-key",
            "password": "legacy-pass",
            "apiVersion": "2026-01",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"product": {"id": 123, "title": "Updated"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.shopify_update_product.func(
            product_id="123",
            fields_json='{"title":"Updated"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_auth = base64.b64encode(b"legacy-key:legacy-pass").decode()
    assert result["title"] == "Updated"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://vault-shop.myshopify.com/admin/api/2026-01/products/123.json"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["json_body"] == {"product": {"title": "Updated", "id": 123}}


def test_woocommerce_create_record_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("WOOCOMMERCE_URL", "https://store.example")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_KEY", "ck_123")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_SECRET", "cs_456")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": 10, "name": "Backpack"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.woocommerce_create_record.func(
            resource="products",
            fields_json='{"name":"Backpack","type":"simple"}',
        )
    )

    expected_auth = base64.b64encode(b"ck_123:cs_456").decode()
    assert result["id"] == 10
    assert captured["method"] == "POST"
    assert captured["url"] == "https://store.example/wp-json/wc/v3/products"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["json_body"] == {"name": "Backpack", "type": "simple"}


def test_chargebee_create_customer_uses_vault_site_and_key(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Chargebee",
        provider="chargebee",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "site": "testsite",
            "api_key": "cb-key",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"customer": {"id": "cust-1"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.chargebee_create_customer.func(
            customer_id="cust-1",
            email="alice@example.com",
            first_name="Alice",
            company="Example Inc",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_auth = base64.b64encode(b"cb-key:").decode()
    assert result["customer"]["id"] == "cust-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://testsite.chargebee.com/api/v2/customers"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["form_data"]["id"] == "cust-1"
    assert captured["form_data"]["email"] == "alice@example.com"
    assert captured["form_data"]["first_name"] == "Alice"
    assert captured["form_data"]["company"] == "Example Inc"


def test_commerce_billing_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SHOPIFY_SHOP", "example")
    monkeypatch.setenv("WOOCOMMERCE_URL", "https://store.example")
    monkeypatch.setenv("CHARGEBEE_SITE", "testsite")

    stripe_result = tools.stripe_get_balance.func()
    shopify_result = tools.shopify_list_records.func(resource="products")
    woo_result = tools.woocommerce_list_records.func(resource="products")
    chargebee_result = tools.chargebee_list_records.func(resource="customers")

    assert 'provider "stripe"' in stripe_result
    assert "STRIPE_SECRET_KEY" in stripe_result
    assert 'allowed target "native_tool:stripe_get_balance"' in stripe_result
    assert 'provider "shopify"' in shopify_result
    assert "SHOPIFY_ACCESS_TOKEN or SHOPIFY_API_KEY + SHOPIFY_PASSWORD" in shopify_result
    assert 'allowed target "native_tool:shopify_list_records"' in shopify_result
    assert 'provider "woocommerce"' in woo_result
    assert "WOOCOMMERCE_CONSUMER_KEY + WOOCOMMERCE_CONSUMER_SECRET" in woo_result
    assert 'allowed target "native_tool:woocommerce_list_records"' in woo_result
    assert 'provider "chargebee"' in chargebee_result
    assert "CHARGEBEE_API_KEY" in chargebee_result
    assert 'allowed target "native_tool:chargebee_list_records"' in chargebee_result


def test_commerce_billing_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "stripe_list_records",
        "stripe_search_records",
        "stripe_get_record",
        "stripe_get_balance",
        "shopify_list_records",
        "shopify_get_record",
        "woocommerce_list_records",
        "woocommerce_get_record",
        "chargebee_list_records",
        "chargebee_get_record",
    ]
    moderate_names = [
        "stripe_create_customer",
        "stripe_update_customer",
        "shopify_create_product",
        "shopify_update_product",
        "woocommerce_create_record",
        "woocommerce_update_record",
        "chargebee_create_customer",
        "chargebee_update_customer",
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


def test_commerce_billing_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        chargebee_create_customer,
        shopify_list_records,
        stripe_create_customer,
        woocommerce_update_record,
    )

    assert "config" not in stripe_create_customer.args_schema.model_json_schema()["properties"]
    assert "config" not in shopify_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in woocommerce_update_record.args_schema.model_json_schema()["properties"]
    assert "config" not in chargebee_create_customer.args_schema.model_json_schema()["properties"]
