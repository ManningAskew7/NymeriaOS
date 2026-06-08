import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

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


def test_magento_list_records_uses_env_bearer_and_search_criteria(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAGENTO_BASE_URL", "https://store.example")
    monkeypatch.setenv("MAGENTO_ACCESS_TOKEN", "magento-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"items": [{"entity_id": 1, "email": "ada@example.com"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.magento_list_records.func(
            resource="customers",
            search_criteria_json='{"searchCriteria": {"filterGroups": [{"filters": [{"field": "email", "value": "ada@example.com", "conditionType": "eq"}]}]}}',
            limit=10,
            current_page=2,
        )
    )

    assert result[0]["email"] == "ada@example.com"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://store.example/rest/default/V1/customers/search"
    assert captured["headers"]["Authorization"] == "Bearer magento-token"
    assert captured["params"]["searchCriteria[pageSize]"] == 10
    assert captured["params"]["searchCriteria[currentPage]"] == 2
    assert captured["params"]["searchCriteria[filterGroups][0][filters][0][field]"] == "email"


def test_magento_create_product_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Magento",
        provider="magento",
        kind="api_key",
        allowed_targets=["native_tool:magento_create_product"],
        secret_fields={"host": "https://magento.example", "accessToken": "magento-token"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"sku": json_body["product"]["sku"], "name": json_body["product"]["name"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.magento_create_product.func(
            sku="SKU-1",
            name="Backpack",
            attribute_set_id=4,
            price=25.5,
            fields_json='{"status": 1}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["sku"] == "SKU-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://magento.example/rest/default/V1/products"
    assert captured["headers"]["Authorization"] == "Bearer magento-token"
    assert captured["json_body"]["product"]["attribute_set_id"] == 4
    assert captured["json_body"]["product"]["price"] == 25.5
    assert captured["json_body"]["product"]["status"] == 1


def test_unleashed_list_stock_on_hand_signs_query(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("UNLEASHED_API_ID", "api-id")
    monkeypatch.setenv("UNLEASHED_API_KEY", "api-key")
    monkeypatch.setenv("UNLEASHED_BASE_URL", "https://unleashed.example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"Items": [{"ProductCode": "SKU-1", "QtyOnHand": 3}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.unleashed_list_stock_on_hand.func(
            filters_json='{"warehouseCode": "MAIN"}',
            limit=25,
            page=2,
        )
    )

    expected_signature = base64.b64encode(
        hmac.new(b"api-key", urlencode(captured["params"], doseq=True).encode(), hashlib.sha256).digest()
    ).decode()
    assert result[0]["ProductCode"] == "SKU-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://unleashed.example/StockOnHand/2"
    assert captured["params"] == {"warehouseCode": "MAIN", "pageSize": 25}
    assert captured["headers"]["api-auth-id"] == "api-id"
    assert captured["headers"]["api-auth-signature"] == expected_signature


def test_quickbooks_query_uses_env_token_realm_and_sandbox(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("QUICKBOOKS_ACCESS_TOKEN", "qb-token")
    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "realm-1")
    monkeypatch.setenv("QUICKBOOKS_ENVIRONMENT", "sandbox")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"QueryResponse": {"Customer": [{"Id": "1", "DisplayName": "Ada"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.quickbooks_query.func(query="SELECT * FROM Customer", limit=3))

    assert result["Customer"][0]["DisplayName"] == "Ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://sandbox-quickbooks.api.intuit.com/v3/company/realm-1/query"
    assert captured["params"]["query"] == "SELECT * FROM Customer MAXRESULTS 3 STARTPOSITION 1"
    assert captured["params"]["minorversion"] == "75"
    assert captured["headers"]["Authorization"] == "Bearer qb-token"


def test_quickbooks_create_invoice_uses_vault_token_and_line_items(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="QuickBooks",
        provider="quickbooks",
        kind="api_key",
        allowed_targets=["native_tool:quickbooks_create_invoice"],
        secret_fields={
            "accessToken": "qb-token",
            "realmId": "realm-1",
            "base_url": "https://quickbooks.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"Invoice": {"Id": "inv-1", "CustomerRef": json_body["CustomerRef"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.quickbooks_create_invoice.func(
            customer_id="cust-1",
            line_items_json='[{"description":"Consulting","quantity":2,"unit_price":100,"item_id":"1"}]',
            due_date="2026-06-01",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["Id"] == "inv-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://quickbooks.example/v3/company/realm-1/invoice"
    assert captured["headers"]["Authorization"] == "Bearer qb-token"
    assert captured["json_body"]["CustomerRef"] == {"value": "cust-1"}
    assert captured["json_body"]["DueDate"] == "2026-06-01"
    line = captured["json_body"]["Line"][0]
    assert line["Amount"] == 200.0
    assert line["SalesItemLineDetail"]["ItemRef"] == {"value": "1"}


def test_xero_list_records_uses_env_token_and_tenant(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("XERO_ACCESS_TOKEN", "xero-token")
    monkeypatch.setenv("XERO_TENANT_ID", "tenant-1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"Contacts": [{"ContactID": "contact-1", "Name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.xero_list_records.func(resource="contacts", where='Name=="Ada"', page=2))

    assert result[0]["Name"] == "Ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.xero.com/api.xro/2.0/Contacts"
    assert captured["params"] == {"where": 'Name=="Ada"', "order": "", "page": 2}
    assert captured["headers"]["Authorization"] == "Bearer xero-token"
    assert captured["headers"]["Xero-tenant-id"] == "tenant-1"


def test_xero_create_contact_uses_vault_token_and_tenant(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Xero",
        provider="xero",
        kind="api_key",
        allowed_targets=["native_tool:xero_create_contact"],
        secret_fields={
            "access_token": "xero-token",
            "tenant_id": "tenant-1",
            "base_url": "https://xero.example/api.xro/2.0",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"Contacts": [{"ContactID": "contact-1", "Name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.xero_create_contact.func(
            name="Ada",
            email="ada@example.com",
            fields_json='{"ContactStatus":"ACTIVE"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result[0]["ContactID"] == "contact-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://xero.example/api.xro/2.0/Contacts"
    assert captured["headers"]["Authorization"] == "Bearer xero-token"
    assert captured["headers"]["Xero-tenant-id"] == "tenant-1"
    assert captured["json_body"] == {
        "Contacts": [
            {
                "ContactStatus": "ACTIVE",
                "Name": "Ada",
                "EmailAddress": "ada@example.com",
            }
        ]
    }


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


def test_paddle_list_products_uses_sandbox_env_credentials(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PADDLE_VENDOR_ID", "123")
    monkeypatch.setenv("PADDLE_VENDOR_AUTH_CODE", "auth-code")
    monkeypatch.setenv("PADDLE_SANDBOX", "true")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"success": True, "response": {"products": [{"id": 1, "name": "Plan"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.paddle_list_products.func(limit=10))

    assert result == [{"id": 1, "name": "Plan"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://sandbox-vendors.paddle.com/api/2.0/product/get_products"
    assert captured["json_body"]["vendor_id"] == "123"
    assert captured["json_body"]["vendor_auth_code"] == "auth-code"


def test_profitwell_get_metrics_simplifies_daily_response(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PROFITWELL_API_TOKEN", "profit-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {
            "data": {
                "recurring_revenue": [{"date": "2026-05-01", "value": 100}],
                "active_customers": [{"date": "2026-05-01", "value": 5}],
            }
        }

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.profitwell_get_metrics.func(
            metric_type="daily",
            month="2026-05",
            metrics="recurring_revenue, active_customers",
        )
    )

    assert result == [{"date": "2026-05-01", "recurring_revenue": 100, "active_customers": 5}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.profitwell.com/v2/metrics/daily"
    assert captured["params"]["month"] == "2026-05"
    assert captured["params"]["metrics"] == "recurring_revenue,active_customers"
    assert captured["headers"]["Authorization"] == "profit-token"


def test_tapfiliate_create_affiliate_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Tapfiliate",
        provider="tapfiliate",
        kind="api_key",
        allowed_targets=["native_tool:tapfiliate_create_affiliate"],
        secret_fields={
            "api_key": "tap-key",
            "base_url": "https://tap.example/1.6",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "aff_1", "email": "alice@example.com"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.tapfiliate_create_affiliate.func(
            email="alice@example.com",
            first_name="Alice",
            last_name="Example",
            company_name="Example Inc",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "aff_1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://tap.example/1.6/affiliates/"
    assert captured["headers"]["Api-Key"] == "tap-key"
    assert captured["json_body"]["firstname"] == "Alice"
    assert captured["json_body"]["lastname"] == "Example"
    assert captured["json_body"]["company"] == {"name": "Example Inc"}


def test_commerce_billing_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import commerce_billing_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SHOPIFY_SHOP", "example")
    monkeypatch.setenv("WOOCOMMERCE_URL", "https://store.example")
    monkeypatch.setenv("CHARGEBEE_SITE", "testsite")
    monkeypatch.setenv("MAGENTO_BASE_URL", "https://store.example")
    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "realm-1")
    monkeypatch.setenv("XERO_TENANT_ID", "tenant-1")
    monkeypatch.delenv("QUICKBOOKS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("XERO_ACCESS_TOKEN", raising=False)

    stripe_result = tools.stripe_get_balance.func()
    shopify_result = tools.shopify_list_records.func(resource="products")
    woo_result = tools.woocommerce_list_records.func(resource="products")
    chargebee_result = tools.chargebee_list_records.func(resource="customers")
    paddle_result = tools.paddle_list_products.func()
    profitwell_result = tools.profitwell_get_settings.func()
    tapfiliate_result = tools.tapfiliate_list_affiliates.func()
    magento_result = tools.magento_list_records.func(resource="customers")
    unleashed_result = tools.unleashed_list_sales_orders.func()
    quickbooks_result = tools.quickbooks_query.func(query="SELECT * FROM Customer")
    xero_result = tools.xero_list_records.func(resource="contacts")

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
    assert 'provider "paddle"' in paddle_result
    assert "PADDLE_VENDOR_ID + PADDLE_VENDOR_AUTH_CODE" in paddle_result
    assert 'allowed target "native_tool:paddle_list_products"' in paddle_result
    assert 'provider "profitwell"' in profitwell_result
    assert "PROFITWELL_API_TOKEN" in profitwell_result
    assert 'allowed target "native_tool:profitwell_get_settings"' in profitwell_result
    assert 'provider "tapfiliate"' in tapfiliate_result
    assert "TAPFILIATE_API_KEY" in tapfiliate_result
    assert 'allowed target "native_tool:tapfiliate_list_affiliates"' in tapfiliate_result
    assert "No Magento credential found" in magento_result
    assert "MAGENTO_ACCESS_TOKEN" in magento_result
    assert "No Unleashed credential found" in unleashed_result
    assert "UNLEASHED_API_ID + UNLEASHED_API_KEY" in unleashed_result
    assert 'provider "quickbooks"' in quickbooks_result
    assert "QUICKBOOKS_ACCESS_TOKEN" in quickbooks_result
    assert 'provider "xero"' in xero_result
    assert "XERO_ACCESS_TOKEN" in xero_result


def test_commerce_billing_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
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
        "paddle_list_products",
        "paddle_list_plans",
        "paddle_list_subscription_users",
        "paddle_list_payments",
        "paddle_get_order",
        "paddle_list_coupons",
        "profitwell_get_settings",
        "profitwell_get_metrics",
        "tapfiliate_list_affiliates",
        "tapfiliate_get_affiliate",
        "tapfiliate_list_program_affiliates",
        "tapfiliate_get_program_affiliate",
        "magento_list_records",
        "magento_get_record",
        "unleashed_list_sales_orders",
        "unleashed_list_stock_on_hand",
        "unleashed_get_stock_on_hand",
        "quickbooks_query",
        "quickbooks_list_records",
        "quickbooks_get_record",
        "xero_list_tenants",
        "xero_list_records",
        "xero_get_record",
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
        "paddle_create_coupon",
        "paddle_update_coupon",
        "paddle_reschedule_payment",
        "tapfiliate_create_affiliate",
        "tapfiliate_delete_affiliate",
        "tapfiliate_add_affiliate_metadata",
        "tapfiliate_remove_affiliate_metadata",
        "tapfiliate_update_affiliate_metadata",
        "tapfiliate_add_program_affiliate",
        "tapfiliate_approve_program_affiliate",
        "tapfiliate_disapprove_program_affiliate",
        "magento_create_customer",
        "magento_update_customer",
        "magento_create_product",
        "magento_update_product",
        "magento_delete_record",
        "magento_create_invoice",
        "magento_cancel_order",
        "magento_ship_order",
        "quickbooks_create_customer",
        "quickbooks_update_customer",
        "quickbooks_create_invoice",
        "xero_create_contact",
        "xero_update_contact",
        "xero_create_invoice",
        "xero_update_invoice",
    ]

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_commerce_billing_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        chargebee_create_customer,
        magento_create_product,
        paddle_create_coupon,
        quickbooks_create_invoice,
        shopify_list_records,
        stripe_create_customer,
        tapfiliate_create_affiliate,
        unleashed_list_sales_orders,
        woocommerce_update_record,
        xero_create_invoice,
    )

    assert "config" not in stripe_create_customer.args_schema.model_json_schema()["properties"]
    assert "config" not in shopify_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in woocommerce_update_record.args_schema.model_json_schema()["properties"]
    assert "config" not in chargebee_create_customer.args_schema.model_json_schema()["properties"]
    assert "config" not in paddle_create_coupon.args_schema.model_json_schema()["properties"]
    assert "config" not in quickbooks_create_invoice.args_schema.model_json_schema()["properties"]
    assert "config" not in tapfiliate_create_affiliate.args_schema.model_json_schema()["properties"]
    assert "config" not in magento_create_product.args_schema.model_json_schema()["properties"]
    assert "config" not in unleashed_list_sales_orders.args_schema.model_json_schema()["properties"]
    assert "config" not in xero_create_invoice.args_schema.model_json_schema()["properties"]
