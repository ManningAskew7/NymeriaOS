"""Commerce, storefront, and billing service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import hashlib
import hmac
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlencode, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import policy_http_client as _http_client
from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .service_integration_base import (
    base_url as _base_url,
    basic_auth as _auth_basic,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    parse_json as _parse_json,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_STRIPE_BASE_URL = "https://api.stripe.com/v1"
_SHOPIFY_API_VERSION = "2026-01"
_CHARGEBEE_API_VERSION = "v2"
_PADDLE_BASE_URL = "https://vendors.paddle.com/api"
_PADDLE_SANDBOX_BASE_URL = "https://sandbox-vendors.paddle.com/api"
_PROFITWELL_BASE_URL = "https://api.profitwell.com/v2"
_TAPFILIATE_BASE_URL = "https://api.tapfiliate.com/1.6"
_UNLEASHED_BASE_URL = "https://api.unleashedsoftware.com"
_QUICKBOOKS_PROD_BASE_URL = "https://quickbooks.api.intuit.com"
_QUICKBOOKS_SANDBOX_BASE_URL = "https://sandbox-quickbooks.api.intuit.com"
_XERO_BASE_URL = "https://api.xero.com/api.xro/2.0"
_XERO_CONNECTIONS_URL = "https://api.xero.com/connections"

_STRIPE_RESOURCES = {
    "customer": "customers",
    "customers": "customers",
    "charge": "charges",
    "charges": "charges",
    "payment_intent": "payment_intents",
    "payment_intents": "payment_intents",
    "invoice": "invoices",
    "invoices": "invoices",
    "subscription": "subscriptions",
    "subscriptions": "subscriptions",
    "product": "products",
    "products": "products",
    "price": "prices",
    "prices": "prices",
}
_STRIPE_SEARCH_RESOURCES = {
    "customers",
    "charges",
    "payment_intents",
    "invoices",
    "subscriptions",
    "products",
    "prices",
}
_SHOPIFY_RESOURCES = {
    "product": ("products", "product"),
    "products": ("products", "product"),
    "order": ("orders", "order"),
    "orders": ("orders", "order"),
    "customer": ("customers", "customer"),
    "customers": ("customers", "customer"),
}
_WOOCOMMERCE_RESOURCES = {
    "product": "products",
    "products": "products",
    "order": "orders",
    "orders": "orders",
    "customer": "customers",
    "customers": "customers",
}
_CHARGEBEE_RESOURCES = {
    "customer": "customers",
    "customers": "customers",
    "subscription": "subscriptions",
    "subscriptions": "subscriptions",
    "invoice": "invoices",
    "invoices": "invoices",
    "transaction": "transactions",
    "transactions": "transactions",
    "item": "items",
    "items": "items",
    "item_price": "item_prices",
    "item_prices": "item_prices",
    "plan": "plans",
    "plans": "plans",
}
_MAGENTO_LIST_ENDPOINTS = {
    "customer": "customers/search",
    "customers": "customers/search",
    "order": "orders",
    "orders": "orders",
    "product": "products",
    "products": "products",
}
_MAGENTO_GET_ENDPOINTS = {
    "customer": "customers/{id}",
    "customers": "customers/{id}",
    "order": "orders/{id}",
    "orders": "orders/{id}",
    "product": "products/{id}",
    "products": "products/{id}",
}
_MAGENTO_DELETE_ENDPOINTS = {
    "customer": "customers/{id}",
    "customers": "customers/{id}",
    "product": "products/{id}",
    "products": "products/{id}",
}
_QUICKBOOKS_RESOURCES = {
    "bill": ("Bill", "bill"),
    "bills": ("Bill", "bill"),
    "customer": ("Customer", "customer"),
    "customers": ("Customer", "customer"),
    "employee": ("Employee", "employee"),
    "employees": ("Employee", "employee"),
    "estimate": ("Estimate", "estimate"),
    "estimates": ("Estimate", "estimate"),
    "invoice": ("Invoice", "invoice"),
    "invoices": ("Invoice", "invoice"),
    "item": ("Item", "item"),
    "items": ("Item", "item"),
    "payment": ("Payment", "payment"),
    "payments": ("Payment", "payment"),
    "purchase": ("Purchase", "purchase"),
    "purchases": ("Purchase", "purchase"),
    "vendor": ("Vendor", "vendor"),
    "vendors": ("Vendor", "vendor"),
}
_XERO_RESOURCES = {
    "contact": ("Contacts", "ContactID"),
    "contacts": ("Contacts", "ContactID"),
    "invoice": ("Invoices", "InvoiceID"),
    "invoices": ("Invoices", "InvoiceID"),
}

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_STRIPE = register_provider_spec(
    ProviderCredentialSpec(
        provider="stripe",
        aliases=("stripe_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="secret_key",
                names=("secret_key", "secretKey", "api_key", "apiKey", "token", "value"),
            ),
        ),
        hint_fields=("secret_key", "api_key", "token", "value"),
        env_var="STRIPE_SECRET_KEY",
        display_name="Stripe",
    )
)

_SHOPIFY = register_provider_spec(
    ProviderCredentialSpec(
        provider="shopify",
        aliases=("shopify_api", "shopify_access_token"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "admin_url"), required=False
            ),
            CredentialFieldGroup(
                role="shop", names=("shop_subdomain", "shopSubdomain", "shop", "domain")
            ),
            CredentialFieldGroup(
                role="api_version",
                names=("api_version", "apiVersion", "version"),
                required=False,
            ),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "accessToken", "token", "value")
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "username")),
            CredentialFieldGroup(
                role="password", names=("password", "api_password", "apiPassword", "secret")
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="SHOPIFY_ACCESS_TOKEN or SHOPIFY_API_KEY + SHOPIFY_PASSWORD",
        display_name="Shopify",
    )
)

_WOOCOMMERCE = register_provider_spec(
    ProviderCredentialSpec(
        provider="woocommerce",
        aliases=("woo_commerce", "woocommerce_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "site_url"), required=False
            ),
            CredentialFieldGroup(
                role="consumer_key",
                names=("consumer_key", "consumerKey", "api_key", "apiKey", "username"),
            ),
            CredentialFieldGroup(
                role="consumer_secret",
                names=("consumer_secret", "consumerSecret", "api_secret", "apiSecret", "password"),
            ),
        ),
        hint_fields=("consumer_key", "consumer_secret"),
        env_var="WOOCOMMERCE_CONSUMER_KEY + WOOCOMMERCE_CONSUMER_SECRET",
        display_name="WooCommerce",
    )
)

_CHARGEBEE = register_provider_spec(
    ProviderCredentialSpec(
        provider="chargebee",
        aliases=("chargebee_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="site",
                names=("site", "account_name", "accountName", "subdomain"),
                required=False,
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "token", "value"),
        env_var="CHARGEBEE_API_KEY",
        display_name="Chargebee",
    )
)

_PADDLE = register_provider_spec(
    ProviderCredentialSpec(
        provider="paddle",
        aliases=("paddle_api",),
        groups=(
            CredentialFieldGroup(
                role="sandbox",
                names=("sandbox", "use_sandbox", "useSandbox"),
                required=False,
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="vendor_id", names=("vendor_id", "vendorId")),
            CredentialFieldGroup(
                role="vendor_auth_code",
                names=(
                    "vendor_auth_code",
                    "vendorAuthCode",
                    "auth_code",
                    "authCode",
                    "api_key",
                    "apiKey",
                    "value",
                ),
            ),
        ),
        hint_fields=("vendor_id", "vendor_auth_code"),
        env_var="PADDLE_VENDOR_ID + PADDLE_VENDOR_AUTH_CODE",
        display_name="Paddle",
    )
)

_PROFITWELL = register_provider_spec(
    ProviderCredentialSpec(
        provider="profitwell",
        aliases=("profitwell_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="access_token",
                names=("access_token", "accessToken", "api_token", "apiToken", "token", "value"),
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="PROFITWELL_API_TOKEN",
        display_name="ProfitWell",
    )
)

_TAPFILIATE = register_provider_spec(
    ProviderCredentialSpec(
        provider="tapfiliate",
        aliases=("tapfiliate_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "value"),
        env_var="TAPFILIATE_API_KEY",
        display_name="Tapfiliate",
    )
)

_MAGENTO = register_provider_spec(
    ProviderCredentialSpec(
        provider="magento",
        aliases=("magento2", "magento2_api", "magento_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("host", "base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "accessToken", "token", "value")
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="MAGENTO_ACCESS_TOKEN",
        display_name="Magento",
    )
)

_UNLEASHED = register_provider_spec(
    ProviderCredentialSpec(
        provider="unleashed",
        aliases=("unleashed_software", "unleashed_software_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(role="api_id", names=("api_id", "apiId", "id")),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "key", "value")),
        ),
        hint_fields=("api_id", "api_key"),
        env_var="UNLEASHED_API_ID + UNLEASHED_API_KEY",
        display_name="Unleashed",
    )
)

_QUICKBOOKS = register_provider_spec(
    ProviderCredentialSpec(
        provider="quickbooks",
        aliases=(
            "quickbooks_online",
            "quickbooks_oauth2",
            "quick_books_oauth2_api",
            "quickbooks_api",
        ),
        groups=(
            CredentialFieldGroup(
                role="environment",
                names=("environment", "env", "sandbox"),
                required=False,
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="realm_id",
                names=("realm_id", "realmId", "company_id", "companyId"),
                required=False,
            ),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "accessToken", "token", "value")
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="QUICKBOOKS_ACCESS_TOKEN",
        display_name="QuickBooks Online",
    )
)

_XERO = register_provider_spec(
    ProviderCredentialSpec(
        provider="xero",
        aliases=("xero_oauth2", "xero_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="tenant_id",
                names=("tenant_id", "tenantId", "organization_id", "organizationId"),
                required=False,
            ),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "accessToken", "token", "value")
            ),
            CredentialFieldGroup(
                role="connections_url",
                names=("connections_url", "connectionsUrl"),
                required=False,
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="XERO_ACCESS_TOKEN",
        display_name="Xero",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _split_csv_ints(value: str) -> list[int]:
    return [int(part) for part in _split_csv(value)]


def _flatten_form_fields(data: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in data.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        field = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten_form_fields(value, prefix=field))
            continue
        flattened[field] = value
    return flattened


def _flatten_query_fields(data: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in data.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        field = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flattened.update(_flatten_query_fields(value, prefix=field))
            continue
        if isinstance(value, list):
            for index, item in enumerate(value):
                item_field = f"{field}[{index}]"
                if isinstance(item, dict):
                    flattened.update(_flatten_query_fields(item, prefix=item_field))
                else:
                    flattened[item_field] = item
            continue
        flattened[field] = value
    return flattened


def _limit(value: int, *, default: int = 25, max_value: int = 250) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _shopify_host_from_shop(value: str) -> str:
    shop = value.strip().rstrip("/")
    parsed = urlparse(shop if "://" in shop else f"https://{shop}")
    host = parsed.netloc or parsed.path
    if "/" in host:
        host = host.split("/", 1)[0]
    if not host:
        raise ValueError("Shopify shop must be a shop subdomain or myshopify.com host.")
    return host if host.endswith(".myshopify.com") else f"{host}.myshopify.com"


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    form_data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(
                client,
                method,
                url,
                params=_filtered(params) if params is not None else None,
                json=json_body,
                data=_flatten_form_fields(form_data) if form_data is not None else None,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except ValueError:
                return {"status": "ok", "status_code": response.status_code, "text": response.text}
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or error)
            detail = (
                detail
                or body.get("message")
                or body.get("error_description")
                or body.get("detail")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "sandbox"}


def _stripe_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_STRIPE.provider,
            provider_aliases=_STRIPE.aliases,
            field_names=_STRIPE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("stripe_base_url")
        or _STRIPE_BASE_URL
    )
    secret_key = _credential_value(
        provider=_STRIPE.provider,
        provider_aliases=_STRIPE.aliases,
        field_names=_STRIPE.group("secret_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("stripe_secret_key")
    if not secret_key:
        return _base_url(base), _setup_hint(
            provider=_STRIPE.provider,
            field_names=_STRIPE.hint_fields,
            tool_name=tool_name,
            env_var=_STRIPE.env_var,
            display_name=_STRIPE.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {secret_key}",
        "User-Agent": "Nymeria",
    }


def _shopify_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    provider_aliases = _SHOPIFY.aliases
    base_from_vault = _credential_value(
        provider=_SHOPIFY.provider,
        provider_aliases=provider_aliases,
        field_names=_SHOPIFY.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value("shopify_base_url")
    shop = _credential_value(
        provider=_SHOPIFY.provider,
        provider_aliases=provider_aliases,
        field_names=_SHOPIFY.group("shop"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("shopify_shop")
    api_version = (
        _credential_value(
            provider=_SHOPIFY.provider,
            provider_aliases=provider_aliases,
            field_names=_SHOPIFY.group("api_version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("shopify_api_version")
        or _SHOPIFY_API_VERSION
    )
    if not base:
        if not shop:
            return "", (
                "[Error]: No Shopify shop found. Save a Shopify credential with "
                '"shop_subdomain" / "shop", or set SHOPIFY_SHOP.'
            )
        base = f"https://{_shopify_host_from_shop(shop)}/admin/api/{api_version.strip()}"
    access_token_from_vault = _credential_value(
        provider=_SHOPIFY.provider,
        provider_aliases=provider_aliases,
        field_names=_SHOPIFY.group("access_token"),
        tool_name=tool_name,
        config=config,
    )
    access_token = access_token_from_vault or _settings_value("shopify_access_token")
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request. A record holding base_url + password clears slice B's "some
    # anchor", and the access-token branch would then send the operator's token
    # to the address that record chose.
    if access_token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=access_token_from_vault,
            secret=access_token,
            provider=_SHOPIFY.provider,
        )
        return _base_url(base), {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Nymeria",
            "X-Shopify-Access-Token": access_token,
        }
    api_key_from_vault = _credential_value(
        provider=_SHOPIFY.provider,
        provider_aliases=provider_aliases,
        field_names=_SHOPIFY.group("api_key"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value("shopify_api_key")
    password_from_vault = _credential_value(
        provider=_SHOPIFY.provider,
        provider_aliases=provider_aliases,
        field_names=_SHOPIFY.group("password"),
        tool_name=tool_name,
        config=config,
    )
    password = password_from_vault or _settings_value("shopify_password")
    if not api_key or not password:
        return _base_url(base), _setup_hint(
            provider=_SHOPIFY.provider,
            field_names=_SHOPIFY.hint_fields,
            tool_name=tool_name,
            env_var=_SHOPIFY.env_var,
            display_name=_SHOPIFY.display_name,
        )
    # Both halves of the pair ride in the Basic header, so each needs its own
    # guard: either one arriving from settings while the address came from the
    # vault is a disclosure.
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=api_key_from_vault,
        secret=api_key,
        provider=_SHOPIFY.provider,
    )
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=password_from_vault,
        secret=password,
        provider=_SHOPIFY.provider,
    )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(api_key, password)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _woocommerce_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_from_vault = _credential_value(
        provider=_WOOCOMMERCE.provider,
        provider_aliases=_WOOCOMMERCE.aliases,
        field_names=_WOOCOMMERCE.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = (
        base_from_vault
        or _settings_value("woocommerce_base_url")
        or _settings_value("woocommerce_url")
    )
    consumer_key_from_vault = _credential_value(
        provider=_WOOCOMMERCE.provider,
        provider_aliases=_WOOCOMMERCE.aliases,
        field_names=_WOOCOMMERCE.group("consumer_key"),
        tool_name=tool_name,
        config=config,
    )
    consumer_key = consumer_key_from_vault or _settings_value("woocommerce_consumer_key")
    consumer_secret_from_vault = _credential_value(
        provider=_WOOCOMMERCE.provider,
        provider_aliases=_WOOCOMMERCE.aliases,
        field_names=_WOOCOMMERCE.group("consumer_secret"),
        tool_name=tool_name,
        config=config,
    )
    consumer_secret = consumer_secret_from_vault or _settings_value("woocommerce_consumer_secret")
    if not base:
        return "", (
            "[Error]: No WooCommerce base URL found. Save a WooCommerce credential "
            'with "base_url" / "url", or set WOOCOMMERCE_URL or WOOCOMMERCE_BASE_URL.'
        )
    base = _base_url(base)
    if not base.endswith("/wp-json/wc/v3"):
        base = base.rstrip("/") + "/wp-json/wc/v3"
    if not consumer_key or not consumer_secret:
        return base, _setup_hint(
            provider=_WOOCOMMERCE.provider,
            field_names=_WOOCOMMERCE.hint_fields,
            tool_name=tool_name,
            env_var=_WOOCOMMERCE.env_var,
            display_name=_WOOCOMMERCE.display_name,
        )
    # Both halves of the pair ride in the Basic header, so each needs its own
    # guard: either one arriving from settings while the address came from the
    # vault is a disclosure.
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=consumer_key_from_vault,
        secret=consumer_key,
        provider=_WOOCOMMERCE.provider,
    )
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=consumer_secret_from_vault,
        secret=consumer_secret,
        provider=_WOOCOMMERCE.provider,
    )
    return base, {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(consumer_key, consumer_secret)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _chargebee_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = _credential_value(
        provider=_CHARGEBEE.provider,
        provider_aliases=_CHARGEBEE.aliases,
        field_names=_CHARGEBEE.group("base_url"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("chargebee_base_url")
    site = _credential_value(
        provider=_CHARGEBEE.provider,
        provider_aliases=_CHARGEBEE.aliases,
        field_names=_CHARGEBEE.group("site"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("chargebee_site")
    if not base:
        if not site:
            return "", (
                "[Error]: No Chargebee site found. Save a Chargebee credential with "
                '"site" / "account_name", or set CHARGEBEE_SITE.'
            )
        base = f"https://{site.strip().removesuffix('.chargebee.com')}.chargebee.com/api/{_CHARGEBEE_API_VERSION}"
    api_key = _credential_value(
        provider=_CHARGEBEE.provider,
        provider_aliases=_CHARGEBEE.aliases,
        field_names=_CHARGEBEE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("chargebee_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_CHARGEBEE.provider,
            field_names=_CHARGEBEE.hint_fields,
            tool_name=tool_name,
            env_var=_CHARGEBEE.env_var,
            display_name=_CHARGEBEE.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(api_key)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _paddle_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    provider_aliases = _PADDLE.aliases
    sandbox_value = _credential_value(
        provider=_PADDLE.provider,
        provider_aliases=provider_aliases,
        field_names=_PADDLE.group("sandbox"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("paddle_sandbox")
    default_base = _PADDLE_SANDBOX_BASE_URL if _truthy(str(sandbox_value)) else _PADDLE_BASE_URL
    base = (
        _credential_value(
            provider=_PADDLE.provider,
            provider_aliases=provider_aliases,
            field_names=_PADDLE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("paddle_base_url")
        or default_base
    )
    vendor_id = _credential_value(
        provider=_PADDLE.provider,
        provider_aliases=provider_aliases,
        field_names=_PADDLE.group("vendor_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("paddle_vendor_id")
    auth_code = _credential_value(
        provider=_PADDLE.provider,
        provider_aliases=provider_aliases,
        field_names=_PADDLE.group("vendor_auth_code"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("paddle_vendor_auth_code")
    if not vendor_id or not auth_code:
        return _base_url(base), _setup_hint(
            provider=_PADDLE.provider,
            field_names=_PADDLE.hint_fields,
            tool_name=tool_name,
            env_var=_PADDLE.env_var,
            display_name=_PADDLE.display_name,
        )
    return _base_url(base), {
        "vendor_id": str(vendor_id),
        "vendor_auth_code": str(auth_code),
    }


def _paddle_request(
    tool_name: str,
    endpoint: str,
    *,
    method: str = "POST",
    body: Optional[dict[str, Any]] = None,
    config: Optional[RunnableConfig] = None,
) -> Any:
    base_url, auth_or_error = _paddle_config(tool_name, config)
    if isinstance(auth_or_error, str):
        return auth_or_error
    payload = _filtered({**auth_or_error, **(body or {})})
    data = _request_json(
        method,
        f"{base_url}{endpoint}",
        params=payload if method.upper() == "GET" else None,
        json_body=payload if method.upper() != "GET" else None,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"},
    )
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data.get("error") or data.get("message") or data))
    return data


def _paddle_response(data: Any, path: str = "response") -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return data
    return current if current is not None else data


def _profitwell_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_PROFITWELL.provider,
            provider_aliases=_PROFITWELL.aliases,
            field_names=_PROFITWELL.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("profitwell_base_url")
        or _PROFITWELL_BASE_URL
    )
    token = _credential_value(
        provider=_PROFITWELL.provider,
        provider_aliases=_PROFITWELL.aliases,
        field_names=_PROFITWELL.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("profitwell_api_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_PROFITWELL.provider,
            field_names=_PROFITWELL.hint_fields,
            tool_name=tool_name,
            env_var=_PROFITWELL.env_var,
            display_name=_PROFITWELL.display_name,
        )
    return _base_url(base), {"Accept": "application/json", "Authorization": token, "User-Agent": "Nymeria"}


def _profitwell_simplify_metrics(data: Any, metric_type: str) -> Any:
    metrics = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(metrics, dict) or not metrics:
        return data
    if metric_type == "daily":
        first_series = next((series for series in metrics.values() if isinstance(series, list)), [])
        rows: list[dict[str, Any]] = []
        for index, point in enumerate(first_series):
            if not isinstance(point, dict):
                continue
            row = {"date": point.get("date")}
            for key, series in metrics.items():
                if isinstance(series, list) and index < len(series) and isinstance(series[index], dict):
                    row[key] = series[index].get("value")
            rows.append(row)
        return rows
    row: dict[str, Any] = {}
    for key, series in metrics.items():
        if isinstance(series, list) and series and isinstance(series[-1], dict):
            row[key] = series[-1].get("value")
            row["date"] = series[-1].get("date")
    return row or data


def _tapfiliate_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_TAPFILIATE.provider,
            provider_aliases=_TAPFILIATE.aliases,
            field_names=_TAPFILIATE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("tapfiliate_base_url")
        or _TAPFILIATE_BASE_URL
    )
    key = _credential_value(
        provider=_TAPFILIATE.provider,
        provider_aliases=_TAPFILIATE.aliases,
        field_names=_TAPFILIATE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("tapfiliate_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider=_TAPFILIATE.provider,
            field_names=_TAPFILIATE.hint_fields,
            tool_name=tool_name,
            env_var=_TAPFILIATE.env_var,
            display_name=_TAPFILIATE.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Api-Key": key,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _magento_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_MAGENTO.provider,
            provider_aliases=_MAGENTO.aliases,
            field_names=_MAGENTO.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("magento_base_url")
        or _settings_value("magento_host")
    )
    access_token = _credential_value(
        provider=_MAGENTO.provider,
        provider_aliases=_MAGENTO.aliases,
        field_names=_MAGENTO.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("magento_access_token")
    if not base:
        return "", (
            "[Error]: No Magento base URL found. Save a Magento credential with "
            '"host" / "base_url", or set MAGENTO_BASE_URL or MAGENTO_HOST.'
        )
    if not access_token:
        return _base_url(base), _setup_hint(
            provider=_MAGENTO.provider,
            field_names=_MAGENTO.hint_fields,
            tool_name=tool_name,
            env_var=_MAGENTO.env_var,
            display_name=_MAGENTO.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _magento_url(base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    path = endpoint.lstrip("/")
    if "/rest/" in base:
        return f"{base}/{path}"
    return f"{base}/rest/default/V1/{path}"


def _magento_search_params(search_criteria_json: str, *, limit: int, current_page: int) -> dict[str, Any]:
    if search_criteria_json.strip():
        parsed = _parse_json(search_criteria_json, expected=dict, label="search_criteria_json")
        params = _flatten_query_fields(parsed)
    else:
        params = {}
    if not any(key in params for key in ("searchCriteria[pageSize]", "search_criteria[page_size]")):
        params["searchCriteria[pageSize]"] = _limit(limit, default=50, max_value=100)
    if not any(key in params for key in ("searchCriteria[currentPage]", "search_criteria[current_page]")):
        params["searchCriteria[currentPage]"] = max(1, int(current_page))
    return params


def _unleashed_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_UNLEASHED.provider,
            provider_aliases=_UNLEASHED.aliases,
            field_names=_UNLEASHED.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("unleashed_base_url")
        or _UNLEASHED_BASE_URL
    )
    api_id = _credential_value(
        provider=_UNLEASHED.provider,
        provider_aliases=_UNLEASHED.aliases,
        field_names=_UNLEASHED.group("api_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("unleashed_api_id")
    api_key = _credential_value(
        provider=_UNLEASHED.provider,
        provider_aliases=_UNLEASHED.aliases,
        field_names=_UNLEASHED.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("unleashed_api_key")
    if not api_id or not api_key:
        return _base_url(base), _setup_hint(
            provider=_UNLEASHED.provider,
            field_names=_UNLEASHED.hint_fields,
            tool_name=tool_name,
            env_var=_UNLEASHED.env_var,
            display_name=_UNLEASHED.display_name,
        )
    return _base_url(base), {"api_id": api_id, "api_key": api_key}


def _unleashed_request(
    tool_name: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    page_number: Optional[int] = None,
    config: Optional[RunnableConfig] = None,
) -> Any:
    base_url, auth_or_error = _unleashed_config(tool_name, config)
    if isinstance(auth_or_error, str):
        return auth_or_error
    query = _filtered(params)
    signature_payload = urlencode(query, doseq=True)
    signature = base64.b64encode(
        hmac.new(auth_or_error["api_key"].encode(), signature_payload.encode(), hashlib.sha256).digest()
    ).decode()
    suffix = f"/{max(1, int(page_number))}" if page_number else ""
    return _request_json(
        "GET",
        f"{base_url}/{path.strip('/')}{suffix}",
        params=query,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Nymeria",
            "api-auth-id": auth_or_error["api_id"],
            "api-auth-signature": signature,
        },
    )


def _normal_resource(resource: str, mapping: dict[str, str]) -> str:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in mapping:
        raise ValueError(f"Unsupported resource {resource!r}. Supported: {', '.join(sorted(mapping))}.")
    return mapping[key]


def _quickbooks_resource(resource: str) -> tuple[str, str]:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _QUICKBOOKS_RESOURCES:
        raise ValueError(f"Unsupported QuickBooks resource {resource!r}. Supported: {', '.join(sorted(_QUICKBOOKS_RESOURCES))}.")
    return _QUICKBOOKS_RESOURCES[key]


def _xero_resource(resource: str) -> tuple[str, str]:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _XERO_RESOURCES:
        raise ValueError(f"Unsupported Xero resource {resource!r}. Supported: {', '.join(sorted(_XERO_RESOURCES))}.")
    return _XERO_RESOURCES[key]


def _quickbooks_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    provider_aliases = _QUICKBOOKS.aliases
    environment = (
        _credential_value(
            provider=_QUICKBOOKS.provider,
            provider_aliases=provider_aliases,
            field_names=_QUICKBOOKS.group("environment"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("quickbooks_environment")
        or ""
    )
    default_base = _QUICKBOOKS_SANDBOX_BASE_URL if _truthy(str(environment)) else _QUICKBOOKS_PROD_BASE_URL
    base = (
        _credential_value(
            provider=_QUICKBOOKS.provider,
            provider_aliases=provider_aliases,
            field_names=_QUICKBOOKS.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("quickbooks_base_url")
        or default_base
    )
    realm_id = _credential_value(
        provider=_QUICKBOOKS.provider,
        provider_aliases=provider_aliases,
        field_names=_QUICKBOOKS.group("realm_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("quickbooks_realm_id")
    if not realm_id:
        return "", (
            "[Error]: No QuickBooks company/realm ID found. Save a QuickBooks credential with "
            '"realm_id" / "company_id", or set QUICKBOOKS_REALM_ID.'
        )
    access_token = _credential_value(
        provider=_QUICKBOOKS.provider,
        provider_aliases=provider_aliases,
        field_names=_QUICKBOOKS.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("quickbooks_access_token")
    if not access_token:
        return _base_url(base), _setup_hint(
            provider=_QUICKBOOKS.provider,
            field_names=_QUICKBOOKS.hint_fields,
            tool_name=tool_name,
            env_var=_QUICKBOOKS.env_var,
            display_name=_QUICKBOOKS.display_name,
        )
    base_url = _base_url(base)
    if "/v3/company/" not in base_url:
        base_url = f"{base_url}/v3/company/{quote(str(realm_id).strip(), safe='')}"
    return base_url, {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _quickbooks_request(
    tool_name: str,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    minor_version: str = "75",
    config: Optional[RunnableConfig] = None,
) -> Any:
    base_url, headers_or_error = _quickbooks_config(tool_name, config)
    if isinstance(headers_or_error, str):
        return headers_or_error
    query = dict(params or {})
    if minor_version.strip():
        query["minorversion"] = minor_version.strip()
    return _request_json(method, f"{base_url}/{path.strip('/')}", params=query, json_body=json_body, headers=headers_or_error)


def _quickbooks_invoice_lines(line_items_json: str) -> list[dict[str, Any]]:
    items = _parse_json(line_items_json, expected=list, label="line_items_json")
    lines: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("line_items_json entries must be JSON objects.")
        if item.get("DetailType"):
            lines.append(item)
            continue
        quantity = item.get("quantity", item.get("qty", item.get("Qty")))
        unit_price = item.get("unit_price", item.get("unitPrice", item.get("UnitPrice")))
        amount = item.get("amount", item.get("Amount"))
        if amount is None and quantity is not None and unit_price is not None:
            amount = float(quantity) * float(unit_price)
        detail = _filtered(
            {
                "Qty": quantity,
                "UnitPrice": unit_price,
            }
        )
        item_id = item.get("item_id", item.get("itemId", item.get("ItemRef")))
        if item_id:
            detail["ItemRef"] = item_id if isinstance(item_id, dict) else {"value": str(item_id)}
        tax_code = item.get("tax_code", item.get("taxCode"))
        if tax_code:
            detail["TaxCodeRef"] = {"value": str(tax_code)}
        line = _filtered(
            {
                "Amount": amount,
                "Description": item.get("description", item.get("Description", "")),
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": detail,
            }
        )
        lines.append(line)
    if not lines:
        raise ValueError("line_items_json must contain at least one line item.")
    return lines


def _xero_config(
    tool_name: str,
    config: Optional[RunnableConfig],
    *,
    tenant_id: str = "",
    require_tenant: bool = True,
) -> tuple[str, dict[str, str] | str]:
    provider_aliases = _XERO.aliases
    base = (
        _credential_value(
            provider=_XERO.provider,
            provider_aliases=provider_aliases,
            field_names=_XERO.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("xero_base_url")
        or _XERO_BASE_URL
    )
    resolved_tenant = tenant_id.strip() or (
        _credential_value(
            provider=_XERO.provider,
            provider_aliases=provider_aliases,
            field_names=_XERO.group("tenant_id"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("xero_tenant_id")
        or ""
    )
    if require_tenant and not resolved_tenant:
        return "", (
            "[Error]: No Xero tenant ID found. Save a Xero credential with "
            '"tenant_id" / "organization_id", pass tenant_id, or set XERO_TENANT_ID.'
        )
    access_token = _credential_value(
        provider=_XERO.provider,
        provider_aliases=provider_aliases,
        field_names=_XERO.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("xero_access_token")
    if not access_token:
        return _base_url(base), _setup_hint(
            provider=_XERO.provider,
            field_names=_XERO.hint_fields,
            tool_name=tool_name,
            env_var=_XERO.env_var,
            display_name=_XERO.display_name,
        )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    if resolved_tenant:
        headers["Xero-tenant-id"] = resolved_tenant
    return _base_url(base), headers


def _xero_request(
    tool_name: str,
    method: str,
    path: str,
    *,
    tenant_id: str = "",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    use_connections: bool = False,
    config: Optional[RunnableConfig] = None,
) -> Any:
    base_url, headers_or_error = _xero_config(
        tool_name,
        config,
        tenant_id=tenant_id,
        require_tenant=not use_connections,
    )
    if isinstance(headers_or_error, str):
        return headers_or_error
    if use_connections:
        url = (
            _credential_value(
                provider=_XERO.provider,
                provider_aliases=_XERO.aliases,
                field_names=_XERO.group("connections_url"),
                tool_name=tool_name,
                config=config,
            )
            or _settings_value("xero_connections_url")
            or _XERO_CONNECTIONS_URL
        )
    else:
        url = f"{base_url}/{path.strip('/')}"
    return _request_json(method, url, params=params, json_body=json_body, headers=headers_or_error)


def _xero_line_items(line_items_json: str) -> list[dict[str, Any]]:
    items = _parse_json(line_items_json, expected=list, label="line_items_json")
    lines: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("line_items_json entries must be JSON objects.")
        if any(key[:1].isupper() for key in item):
            lines.append(item)
            continue
        lines.append(
            _filtered(
                {
                    "Description": item.get("description"),
                    "Quantity": item.get("quantity", item.get("qty")),
                    "UnitAmount": item.get("unit_amount", item.get("unitAmount")),
                    "AccountCode": item.get("account_code", item.get("accountCode")),
                    "ItemCode": item.get("item_code", item.get("itemCode")),
                    "TaxType": item.get("tax_type", item.get("taxType")),
                    "TaxAmount": item.get("tax_amount", item.get("taxAmount")),
                    "LineAmount": item.get("line_amount", item.get("lineAmount")),
                    "DiscountRate": item.get("discount_rate", item.get("discountRate")),
                }
            )
        )
    if not lines:
        raise ValueError("line_items_json must contain at least one line item.")
    return lines


@tool
def stripe_list_records(
    resource: str,
    limit: int = 20,
    starting_after: str = "",
    email: str = "",
    customer_id: str = "",
    status: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Stripe records for safe inspection.

    Args:
        resource: One of customers, charges, payment_intents, invoices, subscriptions, products, or prices.
        limit: Number of records to return, 1-100.
        starting_after: Optional Stripe pagination cursor.
        email: Optional customer email filter for customers.
        customer_id: Optional customer filter for invoices, subscriptions, charges, or payment intents.
        status: Optional status filter where supported by Stripe.
    """
    try:
        path = _normal_resource(resource, _STRIPE_RESOURCES)
        base_url, headers_or_error = _stripe_config("stripe_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{path}",
            params={
                "limit": _limit(limit, default=20, max_value=100),
                "starting_after": starting_after.strip(),
                "email": email.strip() if path == "customers" else "",
                "customer": customer_id.strip(),
                "status": status.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("stripe_list_records failed", exc_info=True)
        return f"[Error]: Stripe record list failed: {e}"


@tool
def stripe_search_records(
    resource: str,
    query: str,
    limit: int = 10,
    page: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Stripe records with Stripe Search query syntax.

    Args:
        resource: One of customers, charges, payment_intents, invoices, subscriptions, products, or prices.
        query: Stripe search query, such as `email:"alice@example.com"`.
        limit: Number of results to return, 1-100.
        page: Optional Stripe search page cursor.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        path = _normal_resource(resource, _STRIPE_RESOURCES)
        if path not in _STRIPE_SEARCH_RESOURCES:
            return f"[Error]: Stripe search is not supported for {resource}."
        base_url, headers_or_error = _stripe_config("stripe_search_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{path}/search",
            params={"query": query.strip(), "limit": _limit(limit, default=10, max_value=100), "page": page.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("stripe_search_records failed", exc_info=True)
        return f"[Error]: Stripe record search failed: {e}"


@tool
def stripe_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Stripe record by ID.

    Args:
        resource: One of customers, charges, payment_intents, invoices, subscriptions, products, or prices.
        record_id: Stripe record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path = _normal_resource(resource, _STRIPE_RESOURCES)
        base_url, headers_or_error = _stripe_config("stripe_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/{path}/{quote(record_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("stripe_get_record failed", exc_info=True)
        return f"[Error]: Stripe record lookup failed: {e}"


@tool
def stripe_get_balance(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Stripe account balance."""
    try:
        base_url, headers_or_error = _stripe_config("stripe_get_balance", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/balance", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("stripe_get_balance failed", exc_info=True)
        return f"[Error]: Stripe balance lookup failed: {e}"


@tool
def stripe_create_customer(
    email: str = "",
    name: str = "",
    description: str = "",
    metadata_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Stripe customer.

    Args:
        email: Optional customer email address.
        name: Optional customer name.
        description: Optional description.
        metadata_json: Optional metadata object as JSON.
    """
    if not email.strip() and not name.strip():
        return "[Error]: email or name is required."
    try:
        metadata = _parse_json(metadata_json, expected=dict, label="metadata_json")
        body = _filtered({"email": email.strip(), "name": name.strip(), "description": description.strip()})
        if metadata:
            body.update({f"metadata[{key}]": value for key, value in metadata.items() if value is not None})
        base_url, headers_or_error = _stripe_config("stripe_create_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/customers", form_data=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("stripe_create_customer failed", exc_info=True)
        return f"[Error]: Stripe customer create failed: {e}"


@tool
def stripe_update_customer(
    customer_id: str,
    email: str = "",
    name: str = "",
    description: str = "",
    metadata_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Stripe customer.

    Args:
        customer_id: Stripe customer ID.
        email: Optional new email address.
        name: Optional new name.
        description: Optional new description.
        metadata_json: Optional metadata object as JSON.
    """
    if not customer_id.strip():
        return "[Error]: customer_id is required."
    try:
        metadata = _parse_json(metadata_json, expected=dict, label="metadata_json")
        body = _filtered({"email": email.strip(), "name": name.strip(), "description": description.strip()})
        if metadata:
            body.update({f"metadata[{key}]": value for key, value in metadata.items() if value is not None})
        if not body:
            return "[Error]: Provide email, name, description, or metadata_json."
        base_url, headers_or_error = _stripe_config("stripe_update_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/customers/{quote(customer_id.strip(), safe='')}",
            form_data=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("stripe_update_customer failed", exc_info=True)
        return f"[Error]: Stripe customer update failed: {e}"


@tool
def shopify_list_records(
    resource: str,
    limit: int = 50,
    status: str = "",
    since_id: str = "",
    created_at_min: str = "",
    updated_at_min: str = "",
    fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Shopify Admin REST records.

    Args:
        resource: One of products, orders, or customers.
        limit: Number of records to return, 1-250.
        status: Optional status filter, mainly for orders/products.
        since_id: Optional numeric pagination cursor.
        created_at_min: Optional lower created-at filter.
        updated_at_min: Optional lower updated-at filter.
        fields: Optional comma-separated fields selector.
    """
    try:
        plural, _ = _SHOPIFY_RESOURCES[resource.strip().lower().replace("-", "_").replace(" ", "_")]
        base_url, headers_or_error = _shopify_config("shopify_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{plural}.json",
            params={
                "limit": _limit(limit, default=50, max_value=250),
                "status": status.strip(),
                "since_id": since_id.strip(),
                "created_at_min": created_at_min.strip(),
                "updated_at_min": updated_at_min.strip(),
                "fields": fields.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get(plural, data) if isinstance(data, dict) else data)
    except KeyError:
        return "[Error]: resource must be one of products, orders, or customers."
    except Exception as e:
        logger.error("shopify_list_records failed", exc_info=True)
        return f"[Error]: Shopify record list failed: {e}"


@tool
def shopify_get_record(
    resource: str,
    record_id: str,
    fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Shopify record by ID.

    Args:
        resource: One of products, orders, or customers.
        record_id: Shopify record ID.
        fields: Optional comma-separated fields selector.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        plural, singular = _SHOPIFY_RESOURCES[resource.strip().lower().replace("-", "_").replace(" ", "_")]
        base_url, headers_or_error = _shopify_config("shopify_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{plural}/{quote(record_id.strip(), safe='')}.json",
            params={"fields": fields.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get(singular, data) if isinstance(data, dict) else data)
    except KeyError:
        return "[Error]: resource must be one of products, orders, or customers."
    except Exception as e:
        logger.error("shopify_get_record failed", exc_info=True)
        return f"[Error]: Shopify record lookup failed: {e}"


@tool
def shopify_create_product(
    title: str,
    body_html: str = "",
    vendor: str = "",
    product_type: str = "",
    status: str = "active",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Shopify product.

    Args:
        title: Product title.
        body_html: Optional product HTML description.
        vendor: Optional vendor.
        product_type: Optional product type.
        status: Product status, such as active, draft, or archived.
        fields_json: Optional extra product fields as JSON.
    """
    if not title.strip():
        return "[Error]: title is required."
    try:
        product = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "title": title.strip(),
                    "body_html": body_html,
                    "vendor": vendor.strip(),
                    "product_type": product_type.strip(),
                    "status": status.strip() or "active",
                }
            ),
        }
        base_url, headers_or_error = _shopify_config("shopify_create_product", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/products.json", json_body={"product": product}, headers=headers_or_error)
        return _dump_json(data.get("product", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("shopify_create_product failed", exc_info=True)
        return f"[Error]: Shopify product create failed: {e}"


@tool
def shopify_update_product(
    product_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Shopify product.

    Args:
        product_id: Shopify product ID.
        fields_json: Product fields to update as JSON.
    """
    if not product_id.strip() or not fields_json.strip():
        return "[Error]: product_id and fields_json are required."
    try:
        product = _parse_json(fields_json, expected=dict, label="fields_json")
        product["id"] = int(product_id) if product_id.isdigit() else product_id
        base_url, headers_or_error = _shopify_config("shopify_update_product", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/products/{quote(product_id.strip(), safe='')}.json",
            json_body={"product": product},
            headers=headers_or_error,
        )
        return _dump_json(data.get("product", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("shopify_update_product failed", exc_info=True)
        return f"[Error]: Shopify product update failed: {e}"


@tool
def woocommerce_list_records(
    resource: str,
    per_page: int = 50,
    page: int = 1,
    search: str = "",
    status: str = "",
    email: str = "",
    sku: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List WooCommerce records.

    Args:
        resource: One of products, orders, or customers.
        per_page: Records per page, 1-100.
        page: Page number.
        search: Optional search term.
        status: Optional status filter.
        email: Optional customer email filter.
        sku: Optional product SKU filter.
    """
    try:
        path = _normal_resource(resource, _WOOCOMMERCE_RESOURCES)
        base_url, headers_or_error = _woocommerce_config("woocommerce_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{path}",
            params={
                "per_page": _limit(per_page, default=50, max_value=100),
                "page": max(1, int(page)),
                "search": search.strip(),
                "status": status.strip(),
                "email": email.strip(),
                "sku": sku.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("woocommerce_list_records failed", exc_info=True)
        return f"[Error]: WooCommerce record list failed: {e}"


@tool
def woocommerce_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a WooCommerce record by ID.

    Args:
        resource: One of products, orders, or customers.
        record_id: WooCommerce record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path = _normal_resource(resource, _WOOCOMMERCE_RESOURCES)
        base_url, headers_or_error = _woocommerce_config("woocommerce_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/{path}/{quote(record_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("woocommerce_get_record failed", exc_info=True)
        return f"[Error]: WooCommerce record lookup failed: {e}"


@tool
def woocommerce_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a WooCommerce product, order, or customer.

    Args:
        resource: One of products, orders, or customers.
        fields_json: Record fields as JSON.
    """
    if not fields_json.strip():
        return "[Error]: fields_json is required."
    try:
        path = _normal_resource(resource, _WOOCOMMERCE_RESOURCES)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        base_url, headers_or_error = _woocommerce_config("woocommerce_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/{path}", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("woocommerce_create_record failed", exc_info=True)
        return f"[Error]: WooCommerce record create failed: {e}"


@tool
def woocommerce_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a WooCommerce product, order, or customer.

    Args:
        resource: One of products, orders, or customers.
        record_id: WooCommerce record ID.
        fields_json: Record fields as JSON.
    """
    if not record_id.strip() or not fields_json.strip():
        return "[Error]: record_id and fields_json are required."
    try:
        path = _normal_resource(resource, _WOOCOMMERCE_RESOURCES)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        base_url, headers_or_error = _woocommerce_config("woocommerce_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/{path}/{quote(record_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("woocommerce_update_record failed", exc_info=True)
        return f"[Error]: WooCommerce record update failed: {e}"


@tool
def chargebee_list_records(
    resource: str,
    limit: int = 50,
    offset: str = "",
    customer_id: str = "",
    status: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Chargebee billing records.

    Args:
        resource: One of customers, subscriptions, invoices, transactions, items, item_prices, or plans.
        limit: Number of records to return, 1-100.
        offset: Optional Chargebee pagination offset.
        customer_id: Optional customer filter where supported.
        status: Optional status filter where supported.
    """
    try:
        path = _normal_resource(resource, _CHARGEBEE_RESOURCES)
        base_url, headers_or_error = _chargebee_config("chargebee_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{path}",
            params={
                "limit": _limit(limit, default=50, max_value=100),
                "offset": offset.strip(),
                "customer_id[is]": customer_id.strip(),
                "status[is]": status.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("list", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("chargebee_list_records failed", exc_info=True)
        return f"[Error]: Chargebee record list failed: {e}"


@tool
def chargebee_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Chargebee record by ID.

    Args:
        resource: One of customers, subscriptions, invoices, transactions, items, item_prices, or plans.
        record_id: Chargebee record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path = _normal_resource(resource, _CHARGEBEE_RESOURCES)
        base_url, headers_or_error = _chargebee_config("chargebee_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/{path}/{quote(record_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("chargebee_get_record failed", exc_info=True)
        return f"[Error]: Chargebee record lookup failed: {e}"


@tool
def chargebee_create_customer(
    customer_id: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Chargebee customer.

    Args:
        customer_id: Optional explicit Chargebee customer ID.
        email: Optional customer email.
        first_name: Optional first name.
        last_name: Optional last name.
        company: Optional company name.
        fields_json: Optional extra customer fields as JSON.
    """
    if not email.strip() and not customer_id.strip() and not company.strip():
        return "[Error]: email, customer_id, or company is required."
    try:
        body = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "id": customer_id.strip(),
                    "email": email.strip(),
                    "first_name": first_name.strip(),
                    "last_name": last_name.strip(),
                    "company": company.strip(),
                }
            ),
        }
        base_url, headers_or_error = _chargebee_config("chargebee_create_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/customers", form_data=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("chargebee_create_customer failed", exc_info=True)
        return f"[Error]: Chargebee customer create failed: {e}"


@tool
def chargebee_update_customer(
    customer_id: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Chargebee customer.

    Args:
        customer_id: Chargebee customer ID.
        email: Optional new email.
        first_name: Optional first name.
        last_name: Optional last name.
        company: Optional company name.
        fields_json: Optional extra customer fields as JSON.
    """
    if not customer_id.strip():
        return "[Error]: customer_id is required."
    try:
        body = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "email": email.strip(),
                    "first_name": first_name.strip(),
                    "last_name": last_name.strip(),
                    "company": company.strip(),
                }
            ),
        }
        if not body:
            return "[Error]: Provide email, first_name, last_name, company, or fields_json."
        base_url, headers_or_error = _chargebee_config("chargebee_update_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/customers/{quote(customer_id.strip(), safe='')}",
            form_data=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("chargebee_update_customer failed", exc_info=True)
        return f"[Error]: Chargebee customer update failed: {e}"


@tool
def paddle_list_products(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Paddle products.

    Args:
        limit: Number of products to return, 1-200.
    """
    try:
        data = _paddle_request("paddle_list_products", "/2.0/product/get_products", config=config)
        if isinstance(data, str):
            return data
        products = _paddle_response(data, "response.products")
        if isinstance(products, list):
            products = products[: _limit(limit, default=50, max_value=200)]
        return _dump_json(products)
    except Exception as e:
        logger.error("paddle_list_products failed", exc_info=True)
        return f"[Error]: Paddle product list failed: {e}"


@tool
def paddle_list_plans(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Paddle subscription plans.

    Args:
        limit: Number of plans to return, 1-200.
    """
    try:
        data = _paddle_request("paddle_list_plans", "/2.0/subscription/plans", config=config)
        if isinstance(data, str):
            return data
        plans = _paddle_response(data)
        if isinstance(plans, list):
            plans = plans[: _limit(limit, default=50, max_value=200)]
        return _dump_json(plans)
    except Exception as e:
        logger.error("paddle_list_plans failed", exc_info=True)
        return f"[Error]: Paddle plan list failed: {e}"


@tool
def paddle_list_subscription_users(
    state: str = "",
    plan_id: str = "",
    subscription_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Paddle subscription users.

    Args:
        state: Optional subscription state filter.
        plan_id: Optional plan ID filter.
        subscription_id: Optional subscription ID filter.
        limit: Number of users to return, 1-200.
    """
    try:
        body = {
            "state": state.strip(),
            "plan_id": plan_id.strip(),
            "subscription_id": subscription_id.strip(),
            "results_per_page": _limit(limit, default=50, max_value=200),
        }
        data = _paddle_request(
            "paddle_list_subscription_users",
            "/2.0/subscription/users",
            body=body,
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(_paddle_response(data))
    except Exception as e:
        logger.error("paddle_list_subscription_users failed", exc_info=True)
        return f"[Error]: Paddle subscription user list failed: {e}"


@tool
def paddle_list_payments(
    subscription_id: str = "",
    plan: str = "",
    state: str = "",
    is_paid: Optional[bool] = None,
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Paddle subscription payments.

    Args:
        subscription_id: Optional subscription ID filter.
        plan: Optional plan filter.
        state: Optional payment state filter.
        is_paid: Optional paid/unpaid filter.
        from_date: Optional start date, YYYY-MM-DD.
        to_date: Optional end date, YYYY-MM-DD.
        limit: Number of payments to return, 1-200.
    """
    try:
        body = {
            "subscription_id": subscription_id.strip(),
            "plan": plan.strip(),
            "state": state.strip(),
            "is_paid": int(is_paid) if is_paid is not None else "",
            "from": from_date.strip(),
            "to": to_date.strip(),
            "results_per_page": _limit(limit, default=50, max_value=200),
        }
        data = _paddle_request("paddle_list_payments", "/2.0/subscription/payments", body=body, config=config)
        if isinstance(data, str):
            return data
        return _dump_json(_paddle_response(data))
    except Exception as e:
        logger.error("paddle_list_payments failed", exc_info=True)
        return f"[Error]: Paddle payment list failed: {e}"


@tool
def paddle_get_order(
    checkout_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Paddle order by checkout ID.

    Args:
        checkout_id: Paddle checkout ID.
    """
    if not checkout_id.strip():
        return "[Error]: checkout_id is required."
    try:
        data = _paddle_request(
            "paddle_get_order",
            "/1.0/order",
            method="GET",
            body={"checkout_id": checkout_id.strip()},
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data)
    except Exception as e:
        logger.error("paddle_get_order failed", exc_info=True)
        return f"[Error]: Paddle order lookup failed: {e}"


@tool
def paddle_list_coupons(
    product_id: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Paddle coupons for a product.

    Args:
        product_id: Paddle product ID.
        limit: Number of coupons to return, 1-200.
    """
    if not product_id.strip():
        return "[Error]: product_id is required."
    try:
        data = _paddle_request(
            "paddle_list_coupons",
            "/2.0/product/list_coupons",
            body={"product_id": product_id.strip()},
            config=config,
        )
        if isinstance(data, str):
            return data
        coupons = _paddle_response(data)
        if isinstance(coupons, list):
            coupons = coupons[: _limit(limit, default=50, max_value=200)]
        return _dump_json(coupons)
    except Exception as e:
        logger.error("paddle_list_coupons failed", exc_info=True)
        return f"[Error]: Paddle coupon list failed: {e}"


@tool
def paddle_create_coupon(
    coupon_type: str,
    discount_type: str,
    discount_amount: float,
    product_ids: str = "",
    currency: str = "",
    coupon_code: str = "",
    coupon_prefix: str = "",
    description: str = "",
    group: str = "",
    allowed_uses: int = 0,
    number_of_coupons: int = 0,
    expires: str = "",
    recurring: bool = False,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create Paddle coupon codes.

    Args:
        coupon_type: "checkout" or "product".
        discount_type: "flat" or "percentage".
        discount_amount: Discount amount.
        product_ids: Comma-separated product IDs when coupon_type is "product".
        currency: Currency for flat discounts.
        coupon_code: Optional explicit coupon code.
        coupon_prefix: Optional generated coupon prefix.
        description: Optional coupon description.
        group: Optional coupon group.
        allowed_uses: Optional allowed uses.
        number_of_coupons: Optional generated coupon count.
        expires: Optional expiration date, YYYY-MM-DD.
        recurring: Apply to recurring payments.
        fields_json: Optional extra Paddle fields as JSON.
    """
    normalized_coupon_type = coupon_type.strip().lower()
    normalized_discount_type = discount_type.strip().lower()
    if normalized_coupon_type not in {"checkout", "product"}:
        return '[Error]: coupon_type must be "checkout" or "product".'
    if normalized_discount_type not in {"flat", "percentage"}:
        return '[Error]: discount_type must be "flat" or "percentage".'
    try:
        body = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "coupon_type": normalized_coupon_type,
                    "discount_type": normalized_discount_type,
                    "discount_amount": discount_amount,
                    "product_ids": _split_csv_ints(product_ids),
                    "currency": currency.strip().upper(),
                    "coupon_code": coupon_code.strip(),
                    "coupon_prefix": coupon_prefix.strip(),
                    "description": description.strip(),
                    "group": group.strip(),
                    "allowed_uses": allowed_uses if allowed_uses > 0 else "",
                    "num_coupons": number_of_coupons if number_of_coupons > 0 else "",
                    "expires": expires.strip(),
                    "recurring": 1 if recurring else 0,
                }
            ),
        }
        data = _paddle_request("paddle_create_coupon", "/2.1/product/create_coupon", body=body, config=config)
        if isinstance(data, str):
            return data
        return _dump_json(_paddle_response(data, "response.coupon_codes"))
    except Exception as e:
        logger.error("paddle_create_coupon failed", exc_info=True)
        return f"[Error]: Paddle coupon creation failed: {e}"


@tool
def paddle_update_coupon(
    coupon_code: str = "",
    group: str = "",
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update Paddle coupon metadata by coupon code or group.

    Args:
        coupon_code: Coupon code to update.
        group: Coupon group to update.
        fields_json: JSON object of Paddle update fields.
    """
    if bool(coupon_code.strip()) == bool(group.strip()):
        return "[Error]: provide exactly one of coupon_code or group."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json is required."
        body = {
            **fields,
            **_filtered({"coupon_code": coupon_code.strip(), "group": group.strip()}),
        }
        data = _paddle_request("paddle_update_coupon", "/2.1/product/update_coupon", body=body, config=config)
        if isinstance(data, str):
            return data
        return _dump_json(_paddle_response(data))
    except Exception as e:
        logger.error("paddle_update_coupon failed", exc_info=True)
        return f"[Error]: Paddle coupon update failed: {e}"


@tool
def paddle_reschedule_payment(
    payment_id: int,
    date: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Reschedule a Paddle subscription payment.

    Args:
        payment_id: Paddle payment ID.
        date: New payment date, YYYY-MM-DD.
    """
    if not date.strip():
        return "[Error]: date is required."
    try:
        data = _paddle_request(
            "paddle_reschedule_payment",
            "/2.0/subscription/payments_reschedule",
            body={"payment_id": int(payment_id), "date": date.strip()},
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data)
    except Exception as e:
        logger.error("paddle_reschedule_payment failed", exc_info=True)
        return f"[Error]: Paddle payment reschedule failed: {e}"


@tool
def profitwell_get_settings(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get ProfitWell account settings.

    Args:
        config: Runtime context injected by Nymeria.
    """
    try:
        base_url, headers_or_error = _profitwell_config("profitwell_get_settings", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/company/settings/", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("profitwell_get_settings failed", exc_info=True)
        return f"[Error]: ProfitWell settings lookup failed: {e}"


@tool
def profitwell_get_metrics(
    metric_type: str,
    month: str = "",
    metrics: str = "",
    plan_id: str = "",
    simplify: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get ProfitWell daily or monthly metrics.

    Args:
        metric_type: "daily" or "monthly".
        month: Required for daily metrics, YYYY-MM.
        metrics: Optional comma-separated metric names.
        plan_id: Optional plan ID filter.
        simplify: Return simplified rows instead of raw ProfitWell data.
    """
    normalized_type = metric_type.strip().lower()
    if normalized_type not in {"daily", "monthly"}:
        return '[Error]: metric_type must be "daily" or "monthly".'
    if normalized_type == "daily" and not month.strip():
        return "[Error]: month is required for daily metrics."
    try:
        base_url, headers_or_error = _profitwell_config("profitwell_get_metrics", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/metrics/{normalized_type}",
            params={
                "month": month.strip(),
                "metrics": ",".join(_split_csv(metrics)),
                "plan_id": plan_id.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(_profitwell_simplify_metrics(data, normalized_type) if simplify else data)
    except Exception as e:
        logger.error("profitwell_get_metrics failed", exc_info=True)
        return f"[Error]: ProfitWell metrics lookup failed: {e}"


@tool
def tapfiliate_list_affiliates(
    email: str = "",
    affiliate_group_id: str = "",
    parent_id: str = "",
    source_id: str = "",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Tapfiliate affiliates.

    Args:
        email: Optional email filter.
        affiliate_group_id: Optional affiliate group filter.
        parent_id: Optional parent affiliate filter.
        source_id: Optional source filter.
        limit: Number of affiliates to return, 1-1000.
    """
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_list_affiliates", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/affiliates/",
            params={
                "email": email.strip(),
                "affiliate_group_id": affiliate_group_id.strip(),
                "parent_id": parent_id.strip(),
                "source_id": source_id.strip(),
            },
            headers=headers_or_error,
        )
        if isinstance(data, list):
            data = data[: _limit(limit, default=100, max_value=1000)]
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_list_affiliates failed", exc_info=True)
        return f"[Error]: Tapfiliate affiliate list failed: {e}"


@tool
def tapfiliate_get_affiliate(
    affiliate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Tapfiliate affiliate by ID.

    Args:
        affiliate_id: Tapfiliate affiliate ID.
    """
    if not affiliate_id.strip():
        return "[Error]: affiliate_id is required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_get_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/affiliates/{quote(affiliate_id.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_get_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate affiliate lookup failed: {e}"


@tool
def tapfiliate_create_affiliate(
    email: str,
    first_name: str,
    last_name: str,
    company_name: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Tapfiliate affiliate.

    Args:
        email: Affiliate email address.
        first_name: Affiliate first name.
        last_name: Affiliate last name.
        company_name: Optional company name.
        fields_json: Optional extra affiliate fields as JSON.
    """
    if not email.strip() or not first_name.strip() or not last_name.strip():
        return "[Error]: email, first_name, and last_name are required."
    try:
        body = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            "email": email.strip(),
            "firstname": first_name.strip(),
            "lastname": last_name.strip(),
        }
        if company_name.strip():
            body["company"] = {"name": company_name.strip()}
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_create_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/affiliates/", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_create_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate affiliate creation failed: {e}"


@tool
def tapfiliate_delete_affiliate(
    affiliate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Tapfiliate affiliate.

    Args:
        affiliate_id: Tapfiliate affiliate ID.
    """
    if not affiliate_id.strip():
        return "[Error]: affiliate_id is required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_delete_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/affiliates/{quote(affiliate_id.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_delete_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate affiliate delete failed: {e}"


@tool
def tapfiliate_add_affiliate_metadata(
    affiliate_id: str,
    metadata_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add metadata fields to a Tapfiliate affiliate.

    Args:
        affiliate_id: Tapfiliate affiliate ID.
        metadata_json: JSON object of metadata key/value pairs.
    """
    if not affiliate_id.strip():
        return "[Error]: affiliate_id is required."
    try:
        metadata = _parse_json(metadata_json, expected=dict, label="metadata_json")
        if not metadata:
            return "[Error]: metadata_json cannot be empty."
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_add_affiliate_metadata", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        results = {}
        for key, value in metadata.items():
            results[str(key)] = _request_json(
                "PUT",
                f"{base_url}/affiliates/{quote(affiliate_id.strip(), safe='')}/meta-data/{quote(str(key), safe='')}/",
                json_body={"value": value},
                headers=headers_or_error,
            )
        return _dump_json({"success": True, "results": results})
    except Exception as e:
        logger.error("tapfiliate_add_affiliate_metadata failed", exc_info=True)
        return f"[Error]: Tapfiliate metadata add failed: {e}"


@tool
def tapfiliate_remove_affiliate_metadata(
    affiliate_id: str,
    key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Remove a metadata field from a Tapfiliate affiliate.

    Args:
        affiliate_id: Tapfiliate affiliate ID.
        key: Metadata key to remove.
    """
    if not affiliate_id.strip() or not key.strip():
        return "[Error]: affiliate_id and key are required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_remove_affiliate_metadata", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/affiliates/{quote(affiliate_id.strip(), safe='')}/meta-data/{quote(key.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_remove_affiliate_metadata failed", exc_info=True)
        return f"[Error]: Tapfiliate metadata removal failed: {e}"


@tool
def tapfiliate_update_affiliate_metadata(
    affiliate_id: str,
    key: str,
    value: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a metadata field on a Tapfiliate affiliate.

    Args:
        affiliate_id: Tapfiliate affiliate ID.
        key: Metadata key.
        value: New metadata value.
    """
    if not affiliate_id.strip() or not key.strip():
        return "[Error]: affiliate_id and key are required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_update_affiliate_metadata", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/affiliates/{quote(affiliate_id.strip(), safe='')}/meta-data/",
            json_body={key.strip(): value},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_update_affiliate_metadata failed", exc_info=True)
        return f"[Error]: Tapfiliate metadata update failed: {e}"


@tool
def tapfiliate_list_program_affiliates(
    program_id: str,
    filters_json: str = "",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List affiliates in a Tapfiliate program.

    Args:
        program_id: Tapfiliate program ID.
        filters_json: Optional JSON object of Tapfiliate filters.
        limit: Number of program affiliates to return, 1-1000.
    """
    if not program_id.strip():
        return "[Error]: program_id is required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_list_program_affiliates", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/programs/{quote(program_id.strip(), safe='')}/affiliates/",
            params=_parse_json(filters_json, expected=dict, label="filters_json"),
            headers=headers_or_error,
        )
        if isinstance(data, list):
            data = data[: _limit(limit, default=100, max_value=1000)]
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_list_program_affiliates failed", exc_info=True)
        return f"[Error]: Tapfiliate program affiliate list failed: {e}"


@tool
def tapfiliate_get_program_affiliate(
    program_id: str,
    affiliate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Tapfiliate affiliate in a program.

    Args:
        program_id: Tapfiliate program ID.
        affiliate_id: Tapfiliate affiliate ID.
    """
    if not program_id.strip() or not affiliate_id.strip():
        return "[Error]: program_id and affiliate_id are required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_get_program_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/programs/{quote(program_id.strip(), safe='')}/affiliates/{quote(affiliate_id.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_get_program_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate program affiliate lookup failed: {e}"


@tool
def tapfiliate_add_program_affiliate(
    program_id: str,
    affiliate_id: str,
    approved: Optional[bool] = None,
    coupon: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a Tapfiliate affiliate to a program.

    Args:
        program_id: Tapfiliate program ID.
        affiliate_id: Tapfiliate affiliate ID.
        approved: Optional approval status.
        coupon: Optional affiliate coupon.
    """
    if not program_id.strip() or not affiliate_id.strip():
        return "[Error]: program_id and affiliate_id are required."
    try:
        body = {
            "affiliate": {"id": affiliate_id.strip()},
            **_filtered({"approved": approved, "coupon": coupon.strip()}),
        }
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_add_program_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/programs/{quote(program_id.strip(), safe='')}/affiliates/",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_add_program_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate program affiliate add failed: {e}"


@tool
def tapfiliate_approve_program_affiliate(
    program_id: str,
    affiliate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Approve a Tapfiliate affiliate for a program.

    Args:
        program_id: Tapfiliate program ID.
        affiliate_id: Tapfiliate affiliate ID.
    """
    if not program_id.strip() or not affiliate_id.strip():
        return "[Error]: program_id and affiliate_id are required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_approve_program_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/programs/{quote(program_id.strip(), safe='')}/affiliates/{quote(affiliate_id.strip(), safe='')}/approved/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_approve_program_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate program affiliate approval failed: {e}"


@tool
def tapfiliate_disapprove_program_affiliate(
    program_id: str,
    affiliate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Disapprove a Tapfiliate affiliate for a program.

    Args:
        program_id: Tapfiliate program ID.
        affiliate_id: Tapfiliate affiliate ID.
    """
    if not program_id.strip() or not affiliate_id.strip():
        return "[Error]: program_id and affiliate_id are required."
    try:
        base_url, headers_or_error = _tapfiliate_config("tapfiliate_disapprove_program_affiliate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/programs/{quote(program_id.strip(), safe='')}/affiliates/{quote(affiliate_id.strip(), safe='')}/approved/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("tapfiliate_disapprove_program_affiliate failed", exc_info=True)
        return f"[Error]: Tapfiliate program affiliate disapproval failed: {e}"


@tool
def magento_list_records(
    resource: str,
    search_criteria_json: str = "",
    limit: int = 50,
    current_page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Magento customers, orders, or products with optional searchCriteria JSON."""
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _MAGENTO_LIST_ENDPOINTS:
        return "[Error]: resource must be one of customers, orders, or products."
    try:
        base_url, headers_or_error = _magento_config("magento_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _magento_url(base_url, _MAGENTO_LIST_ENDPOINTS[key]),
            params=_magento_search_params(search_criteria_json, limit=limit, current_page=current_page),
            headers=headers_or_error,
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("magento_list_records failed", exc_info=True)
        return f"[Error]: Magento record list failed: {e}"


@tool
def magento_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Magento customer, order, or product by ID or SKU."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _MAGENTO_GET_ENDPOINTS:
        return "[Error]: resource must be one of customers, orders, or products."
    try:
        base_url, headers_or_error = _magento_config("magento_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = _MAGENTO_GET_ENDPOINTS[key].format(id=quote(record_id.strip(), safe=""))
        data = _request_json("GET", _magento_url(base_url, endpoint), headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_get_record failed", exc_info=True)
        return f"[Error]: Magento record lookup failed: {e}"


@tool
def magento_create_customer(
    email: str,
    firstname: str,
    lastname: str,
    password: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Magento customer."""
    if not email.strip() or not firstname.strip() or not lastname.strip():
        return "[Error]: email, firstname, and lastname are required."
    try:
        customer = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            "email": email.strip(),
            "firstname": firstname.strip(),
            "lastname": lastname.strip(),
        }
        body: dict[str, Any] = {"customer": customer}
        if password:
            body["password"] = password
        base_url, headers_or_error = _magento_config("magento_create_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", _magento_url(base_url, "customers"), json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_create_customer failed", exc_info=True)
        return f"[Error]: Magento customer create failed: {e}"


@tool
def magento_update_customer(
    customer_id: str,
    email: str,
    firstname: str,
    lastname: str,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Magento customer."""
    if not customer_id.strip() or not email.strip() or not firstname.strip() or not lastname.strip():
        return "[Error]: customer_id, email, firstname, and lastname are required."
    try:
        customer = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            "id": int(customer_id) if customer_id.isdigit() else customer_id,
            "email": email.strip(),
            "firstname": firstname.strip(),
            "lastname": lastname.strip(),
        }
        base_url, headers_or_error = _magento_config("magento_update_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            _magento_url(base_url, f"customers/{quote(customer_id.strip(), safe='')}"),
            json_body={"customer": customer},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_update_customer failed", exc_info=True)
        return f"[Error]: Magento customer update failed: {e}"


@tool
def magento_create_product(
    sku: str,
    name: str,
    attribute_set_id: int,
    price: float,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Magento product."""
    if not sku.strip() or not name.strip():
        return "[Error]: sku and name are required."
    try:
        product = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            "sku": sku.strip(),
            "name": name.strip(),
            "attribute_set_id": int(attribute_set_id),
            "price": float(price),
        }
        base_url, headers_or_error = _magento_config("magento_create_product", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", _magento_url(base_url, "products"), json_body={"product": product}, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_create_product failed", exc_info=True)
        return f"[Error]: Magento product create failed: {e}"


@tool
def magento_update_product(
    sku: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Magento product by SKU."""
    if not sku.strip() or not fields_json.strip():
        return "[Error]: sku and fields_json are required."
    try:
        product = {"sku": sku.strip(), **_parse_json(fields_json, expected=dict, label="fields_json")}
        base_url, headers_or_error = _magento_config("magento_update_product", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            _magento_url(base_url, f"products/{quote(sku.strip(), safe='')}"),
            json_body={"product": product},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_update_product failed", exc_info=True)
        return f"[Error]: Magento product update failed: {e}"


@tool
def magento_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Magento customer or product."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _MAGENTO_DELETE_ENDPOINTS:
        return "[Error]: resource must be customer or product."
    try:
        base_url, headers_or_error = _magento_config("magento_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = _MAGENTO_DELETE_ENDPOINTS[key].format(id=quote(record_id.strip(), safe=""))
        data = _request_json("DELETE", _magento_url(base_url, endpoint), headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_delete_record failed", exc_info=True)
        return f"[Error]: Magento record delete failed: {e}"


@tool
def magento_create_invoice(
    order_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an invoice for a Magento order."""
    if not order_id.strip():
        return "[Error]: order_id is required."
    try:
        base_url, headers_or_error = _magento_config("magento_create_invoice", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", _magento_url(base_url, f"order/{quote(order_id.strip(), safe='')}/invoice"), headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_create_invoice failed", exc_info=True)
        return f"[Error]: Magento invoice create failed: {e}"


@tool
def magento_cancel_order(
    order_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Cancel a Magento order."""
    if not order_id.strip():
        return "[Error]: order_id is required."
    try:
        base_url, headers_or_error = _magento_config("magento_cancel_order", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", _magento_url(base_url, f"orders/{quote(order_id.strip(), safe='')}/cancel"), headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_cancel_order failed", exc_info=True)
        return f"[Error]: Magento order cancel failed: {e}"


@tool
def magento_ship_order(
    order_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a shipment for a Magento order."""
    if not order_id.strip():
        return "[Error]: order_id is required."
    try:
        base_url, headers_or_error = _magento_config("magento_ship_order", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", _magento_url(base_url, f"order/{quote(order_id.strip(), safe='')}/ship"), headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("magento_ship_order failed", exc_info=True)
        return f"[Error]: Magento order ship failed: {e}"


@tool
def unleashed_list_sales_orders(
    filters_json: str = "",
    limit: int = 50,
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Unleashed sales orders."""
    try:
        params = _parse_json(filters_json, expected=dict, label="filters_json")
        params["pageSize"] = _limit(limit, default=50, max_value=1000)
        data = _unleashed_request("unleashed_list_sales_orders", "SalesOrders", params=params, page_number=page, config=config)
        if isinstance(data, str):
            return data
        return _dump_json(data.get("Items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("unleashed_list_sales_orders failed", exc_info=True)
        return f"[Error]: Unleashed sales order list failed: {e}"


@tool
def unleashed_list_stock_on_hand(
    filters_json: str = "",
    limit: int = 50,
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Unleashed stock-on-hand records."""
    try:
        params = _parse_json(filters_json, expected=dict, label="filters_json")
        params["pageSize"] = _limit(limit, default=50, max_value=1000)
        data = _unleashed_request("unleashed_list_stock_on_hand", "StockOnHand", params=params, page_number=page, config=config)
        if isinstance(data, str):
            return data
        return _dump_json(data.get("Items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("unleashed_list_stock_on_hand failed", exc_info=True)
        return f"[Error]: Unleashed stock-on-hand list failed: {e}"


@tool
def unleashed_get_stock_on_hand(
    product_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Unleashed stock-on-hand for one product."""
    if not product_id.strip():
        return "[Error]: product_id is required."
    try:
        data = _unleashed_request("unleashed_get_stock_on_hand", f"StockOnHand/{quote(product_id.strip(), safe='')}", config=config)
        if isinstance(data, str):
            return data
        return _dump_json(data)
    except Exception as e:
        logger.error("unleashed_get_stock_on_hand failed", exc_info=True)
        return f"[Error]: Unleashed stock-on-hand lookup failed: {e}"


@tool
def quickbooks_query(
    query: str,
    limit: int = 50,
    start_position: int = 1,
    minor_version: str = "75",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a QuickBooks Online read-only SQL-style query.

    Args:
        query: QuickBooks query starting with SELECT.
        limit: MAXRESULTS value to append when query does not specify one.
        start_position: STARTPOSITION value to append when query does not specify one.
        minor_version: Optional QuickBooks API minor version.
    """
    clean_query = query.strip().rstrip(";")
    if not clean_query.lower().startswith("select"):
        return "[Error]: query must start with SELECT."
    lower_query = clean_query.lower()
    if "maxresults" not in lower_query:
        clean_query += f" MAXRESULTS {_limit(limit, default=50, max_value=1000)}"
    if "startposition" not in lower_query:
        clean_query += f" STARTPOSITION {max(1, int(start_position))}"
    try:
        data = _quickbooks_request(
            "quickbooks_query",
            "GET",
            "query",
            params={"query": clean_query},
            minor_version=minor_version,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("QueryResponse", data))
    except Exception as e:
        logger.error("quickbooks_query failed", exc_info=True)
        return f"[Error]: QuickBooks query failed: {e}"


@tool
def quickbooks_list_records(
    resource: str,
    where_clause: str = "",
    limit: int = 50,
    start_position: int = 1,
    minor_version: str = "75",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List QuickBooks Online records.

    Args:
        resource: Customer, Vendor, Item, Invoice, Bill, Payment, Purchase, Estimate, or Employee.
        where_clause: Optional QuickBooks WHERE clause, with or without the WHERE keyword.
        limit: Maximum records to return.
        start_position: QuickBooks start position.
        minor_version: Optional QuickBooks API minor version.
    """
    try:
        entity, _path = _quickbooks_resource(resource)
        query = f"SELECT * FROM {entity}"
        if where_clause.strip():
            clause = where_clause.strip()
            query += f" {clause if clause.lower().startswith('where ') else f'WHERE {clause}'}"
        query += f" MAXRESULTS {_limit(limit, default=50, max_value=1000)} STARTPOSITION {max(1, int(start_position))}"
        data = _quickbooks_request(
            "quickbooks_list_records",
            "GET",
            "query",
            params={"query": query},
            minor_version=minor_version,
            config=config,
        )
        if isinstance(data, str):
            return data
        query_response = data.get("QueryResponse", data) if isinstance(data, dict) else data
        return _dump_json(query_response.get(entity, query_response) if isinstance(query_response, dict) else query_response)
    except Exception as e:
        logger.error("quickbooks_list_records failed", exc_info=True)
        return f"[Error]: QuickBooks record list failed: {e}"


@tool
def quickbooks_get_record(
    resource: str,
    record_id: str,
    minor_version: str = "75",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a QuickBooks Online record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        entity, path = _quickbooks_resource(resource)
        data = _quickbooks_request(
            "quickbooks_get_record",
            "GET",
            f"{path}/{quote(record_id.strip(), safe='')}",
            minor_version=minor_version,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get(entity, data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("quickbooks_get_record failed", exc_info=True)
        return f"[Error]: QuickBooks record lookup failed: {e}"


@tool
def quickbooks_create_customer(
    display_name: str,
    given_name: str = "",
    family_name: str = "",
    company_name: str = "",
    email: str = "",
    phone: str = "",
    billing_address_json: str = "",
    fields_json: str = "",
    minor_version: str = "75",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a QuickBooks Online customer."""
    if not display_name.strip():
        return "[Error]: display_name is required."
    try:
        customer = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "DisplayName": display_name.strip(),
                    "GivenName": given_name.strip(),
                    "FamilyName": family_name.strip(),
                    "CompanyName": company_name.strip(),
                    "PrimaryEmailAddr": {"Address": email.strip()} if email.strip() else {},
                    "PrimaryPhone": {"FreeFormNumber": phone.strip()} if phone.strip() else {},
                    "BillAddr": _parse_json(billing_address_json, expected=dict, label="billing_address_json"),
                }
            ),
        }
        data = _quickbooks_request(
            "quickbooks_create_customer",
            "POST",
            "customer",
            json_body=customer,
            minor_version=minor_version,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Customer", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("quickbooks_create_customer failed", exc_info=True)
        return f"[Error]: QuickBooks customer create failed: {e}"


@tool
def quickbooks_update_customer(
    customer_id: str,
    sync_token: str,
    fields_json: str,
    minor_version: str = "75",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a QuickBooks Online customer using sparse update fields."""
    if not customer_id.strip() or not sync_token.strip() or not fields_json.strip():
        return "[Error]: customer_id, sync_token, and fields_json are required."
    try:
        customer = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            "Id": customer_id.strip(),
            "SyncToken": sync_token.strip(),
            "sparse": True,
        }
        data = _quickbooks_request(
            "quickbooks_update_customer",
            "POST",
            "customer",
            json_body=customer,
            minor_version=minor_version,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Customer", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("quickbooks_update_customer failed", exc_info=True)
        return f"[Error]: QuickBooks customer update failed: {e}"


@tool
def quickbooks_create_invoice(
    customer_id: str,
    line_items_json: str,
    due_date: str = "",
    doc_number: str = "",
    customer_memo: str = "",
    fields_json: str = "",
    minor_version: str = "75",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a QuickBooks Online invoice."""
    if not customer_id.strip():
        return "[Error]: customer_id is required."
    try:
        invoice = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "CustomerRef": {"value": customer_id.strip()},
                    "Line": _quickbooks_invoice_lines(line_items_json),
                    "DueDate": due_date.strip(),
                    "DocNumber": doc_number.strip(),
                    "CustomerMemo": {"value": customer_memo.strip()} if customer_memo.strip() else {},
                }
            ),
        }
        data = _quickbooks_request(
            "quickbooks_create_invoice",
            "POST",
            "invoice",
            json_body=invoice,
            minor_version=minor_version,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Invoice", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("quickbooks_create_invoice failed", exc_info=True)
        return f"[Error]: QuickBooks invoice create failed: {e}"


@tool
def xero_list_tenants(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Xero tenants connected to the saved OAuth token."""
    try:
        data = _xero_request("xero_list_tenants", "GET", "", use_connections=True, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("xero_list_tenants failed", exc_info=True)
        return f"[Error]: Xero tenant list failed: {e}"


@tool
def xero_list_records(
    resource: str,
    tenant_id: str = "",
    where: str = "",
    order: str = "",
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Xero contacts or invoices."""
    try:
        path, _id_field = _xero_resource(resource)
        data = _xero_request(
            "xero_list_records",
            "GET",
            path,
            tenant_id=tenant_id,
            params={"where": where.strip(), "order": order.strip(), "page": max(1, int(page))},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get(path, data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("xero_list_records failed", exc_info=True)
        return f"[Error]: Xero record list failed: {e}"


@tool
def xero_get_record(
    resource: str,
    record_id: str,
    tenant_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Xero contact or invoice by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path, _id_field = _xero_resource(resource)
        data = _xero_request(
            "xero_get_record",
            "GET",
            f"{path}/{quote(record_id.strip(), safe='')}",
            tenant_id=tenant_id,
            config=config,
        )
        result = data.get(path, data) if isinstance(data, dict) else data
        if isinstance(result, list) and len(result) == 1:
            result = result[0]
        return data if isinstance(data, str) else _dump_json(result)
    except Exception as e:
        logger.error("xero_get_record failed", exc_info=True)
        return f"[Error]: Xero record lookup failed: {e}"


@tool
def xero_create_contact(
    name: str,
    tenant_id: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Xero contact."""
    if not name.strip():
        return "[Error]: name is required."
    try:
        contact = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "Name": name.strip(),
                    "EmailAddress": email.strip(),
                    "FirstName": first_name.strip(),
                    "LastName": last_name.strip(),
                }
            ),
        }
        data = _xero_request(
            "xero_create_contact",
            "POST",
            "Contacts",
            tenant_id=tenant_id,
            json_body={"Contacts": [contact]},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Contacts", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("xero_create_contact failed", exc_info=True)
        return f"[Error]: Xero contact create failed: {e}"


@tool
def xero_update_contact(
    contact_id: str,
    tenant_id: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Xero contact."""
    if not contact_id.strip() or not fields_json.strip():
        return "[Error]: contact_id and fields_json are required."
    try:
        contact = {"ContactID": contact_id.strip(), **_parse_json(fields_json, expected=dict, label="fields_json")}
        data = _xero_request(
            "xero_update_contact",
            "POST",
            f"Contacts/{quote(contact_id.strip(), safe='')}",
            tenant_id=tenant_id,
            json_body={"Contacts": [contact]},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Contacts", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("xero_update_contact failed", exc_info=True)
        return f"[Error]: Xero contact update failed: {e}"


@tool
def xero_create_invoice(
    contact_id: str,
    line_items_json: str,
    tenant_id: str = "",
    invoice_type: str = "ACCREC",
    due_date: str = "",
    status: str = "DRAFT",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Xero invoice."""
    if not contact_id.strip():
        return "[Error]: contact_id is required."
    try:
        invoice = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "Type": invoice_type.strip().upper() or "ACCREC",
                    "Contact": {"ContactID": contact_id.strip()},
                    "LineItems": _xero_line_items(line_items_json),
                    "DueDate": due_date.strip(),
                    "Status": status.strip().upper() or "DRAFT",
                }
            ),
        }
        data = _xero_request(
            "xero_create_invoice",
            "POST",
            "Invoices",
            tenant_id=tenant_id,
            json_body={"Invoices": [invoice]},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Invoices", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("xero_create_invoice failed", exc_info=True)
        return f"[Error]: Xero invoice create failed: {e}"


@tool
def xero_update_invoice(
    invoice_id: str,
    tenant_id: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Xero invoice."""
    if not invoice_id.strip() or not fields_json.strip():
        return "[Error]: invoice_id and fields_json are required."
    try:
        invoice = {"InvoiceID": invoice_id.strip(), **_parse_json(fields_json, expected=dict, label="fields_json")}
        data = _xero_request(
            "xero_update_invoice",
            "POST",
            f"Invoices/{quote(invoice_id.strip(), safe='')}",
            tenant_id=tenant_id,
            json_body={"Invoices": [invoice]},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("Invoices", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("xero_update_invoice failed", exc_info=True)
        return f"[Error]: Xero invoice update failed: {e}"


COMMERCE_BILLING_SERVICE_TOOLS = [
    stripe_list_records,
    stripe_search_records,
    stripe_get_record,
    stripe_get_balance,
    stripe_create_customer,
    stripe_update_customer,
    shopify_list_records,
    shopify_get_record,
    shopify_create_product,
    shopify_update_product,
    woocommerce_list_records,
    woocommerce_get_record,
    woocommerce_create_record,
    woocommerce_update_record,
    chargebee_list_records,
    chargebee_get_record,
    chargebee_create_customer,
    chargebee_update_customer,
    paddle_list_products,
    paddle_list_plans,
    paddle_list_subscription_users,
    paddle_list_payments,
    paddle_get_order,
    paddle_list_coupons,
    paddle_create_coupon,
    paddle_update_coupon,
    paddle_reschedule_payment,
    profitwell_get_settings,
    profitwell_get_metrics,
    tapfiliate_list_affiliates,
    tapfiliate_get_affiliate,
    tapfiliate_create_affiliate,
    tapfiliate_delete_affiliate,
    tapfiliate_add_affiliate_metadata,
    tapfiliate_remove_affiliate_metadata,
    tapfiliate_update_affiliate_metadata,
    tapfiliate_list_program_affiliates,
    tapfiliate_get_program_affiliate,
    tapfiliate_add_program_affiliate,
    tapfiliate_approve_program_affiliate,
    tapfiliate_disapprove_program_affiliate,
    magento_list_records,
    magento_get_record,
    magento_create_customer,
    magento_update_customer,
    magento_create_product,
    magento_update_product,
    magento_delete_record,
    magento_create_invoice,
    magento_cancel_order,
    magento_ship_order,
    unleashed_list_sales_orders,
    unleashed_list_stock_on_hand,
    unleashed_get_stock_on_hand,
    quickbooks_query,
    quickbooks_list_records,
    quickbooks_get_record,
    quickbooks_create_customer,
    quickbooks_update_customer,
    quickbooks_create_invoice,
    xero_list_tenants,
    xero_list_records,
    xero_get_record,
    xero_create_contact,
    xero_update_contact,
    xero_create_invoice,
    xero_update_invoice,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="commerce_billing", tools=tuple(COMMERCE_BILLING_SERVICE_TOOLS)))
