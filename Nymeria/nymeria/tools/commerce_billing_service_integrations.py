"""Commerce, storefront, and billing service integration tools."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_STRIPE_BASE_URL = "https://api.stripe.com/v1"
_SHOPIFY_API_VERSION = "2026-01"
_CHARGEBEE_API_VERSION = "v2"

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


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _filtered(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


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


def _limit(value: int, *, default: int = 25, max_value: int = 250) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


def _shopify_host_from_shop(value: str) -> str:
    shop = value.strip().rstrip("/")
    parsed = urlparse(shop if "://" in shop else f"https://{shop}")
    host = parsed.netloc or parsed.path
    if "/" in host:
        host = host.split("/", 1)[0]
    if not host:
        raise ValueError("Shopify shop must be a shop subdomain or myshopify.com host.")
    return host if host.endswith(".myshopify.com") else f"{host}.myshopify.com"


def _parse_json(value: str, *, expected: type, label: str) -> Any:
    if not value.strip():
        return {} if expected is dict else []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} must be valid JSON: {e}") from e
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} must be a JSON {expected.__name__}.")
    return parsed


def _settings_value(name: str) -> Optional[str]:
    from ..config import get_settings

    return getattr(get_settings(), name)


def _credential_value(
    *,
    provider: str,
    field_names: tuple[str, ...],
    tool_name: str,
    config: Optional[RunnableConfig],
    provider_aliases: tuple[str, ...] = (),
) -> Optional[str]:
    from .native_credentials import get_native_credential_value

    credential = get_native_credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    )
    return credential.value if credential else None


def _setup_hint(
    *,
    provider: str,
    field_names: tuple[str, ...],
    tool_name: str,
    env_var: str,
    display_name: str,
) -> str:
    from .native_credentials import native_credential_setup_hint

    return native_credential_setup_hint(
        provider=provider,
        field_names=field_names,
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    )


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
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
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


def _auth_basic(username: str, password: str = "") -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def _stripe_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="stripe",
            provider_aliases=("stripe_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("stripe_base_url")
        or _STRIPE_BASE_URL
    )
    secret_key = _credential_value(
        provider="stripe",
        provider_aliases=("stripe_api",),
        field_names=("secret_key", "secretKey", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("stripe_secret_key")
    if not secret_key:
        return _base_url(base), _setup_hint(
            provider="stripe",
            field_names=("secret_key", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="STRIPE_SECRET_KEY",
            display_name="Stripe",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {secret_key}",
        "User-Agent": "Nymeria",
    }


def _shopify_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    provider_aliases = ("shopify_api", "shopify_access_token")
    base = _credential_value(
        provider="shopify",
        provider_aliases=provider_aliases,
        field_names=("base_url", "url", "admin_url"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("shopify_base_url")
    shop = _credential_value(
        provider="shopify",
        provider_aliases=provider_aliases,
        field_names=("shop_subdomain", "shopSubdomain", "shop", "domain"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("shopify_shop")
    api_version = (
        _credential_value(
            provider="shopify",
            provider_aliases=provider_aliases,
            field_names=("api_version", "apiVersion", "version"),
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
    access_token = _credential_value(
        provider="shopify",
        provider_aliases=provider_aliases,
        field_names=("access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("shopify_access_token")
    if access_token:
        return _base_url(base), {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Nymeria",
            "X-Shopify-Access-Token": access_token,
        }
    api_key = _credential_value(
        provider="shopify",
        provider_aliases=provider_aliases,
        field_names=("api_key", "apiKey", "username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("shopify_api_key")
    password = _credential_value(
        provider="shopify",
        provider_aliases=provider_aliases,
        field_names=("password", "api_password", "apiPassword", "secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("shopify_password")
    if not api_key or not password:
        return _base_url(base), _setup_hint(
            provider="shopify",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="SHOPIFY_ACCESS_TOKEN or SHOPIFY_API_KEY + SHOPIFY_PASSWORD",
            display_name="Shopify",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(api_key, password)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _woocommerce_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="woocommerce",
            provider_aliases=("woo_commerce", "woocommerce_api"),
            field_names=("base_url", "url", "site_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("woocommerce_base_url")
        or _settings_value("woocommerce_url")
    )
    consumer_key = _credential_value(
        provider="woocommerce",
        provider_aliases=("woo_commerce", "woocommerce_api"),
        field_names=("consumer_key", "consumerKey", "api_key", "apiKey", "username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("woocommerce_consumer_key")
    consumer_secret = _credential_value(
        provider="woocommerce",
        provider_aliases=("woo_commerce", "woocommerce_api"),
        field_names=("consumer_secret", "consumerSecret", "api_secret", "apiSecret", "password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("woocommerce_consumer_secret")
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
            provider="woocommerce",
            field_names=("consumer_key", "consumer_secret"),
            tool_name=tool_name,
            env_var="WOOCOMMERCE_CONSUMER_KEY + WOOCOMMERCE_CONSUMER_SECRET",
            display_name="WooCommerce",
        )
    return base, {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(consumer_key, consumer_secret)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _chargebee_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = _credential_value(
        provider="chargebee",
        provider_aliases=("chargebee_api",),
        field_names=("base_url", "url"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("chargebee_base_url")
    site = _credential_value(
        provider="chargebee",
        provider_aliases=("chargebee_api",),
        field_names=("site", "account_name", "accountName", "subdomain"),
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
        provider="chargebee",
        provider_aliases=("chargebee_api",),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("chargebee_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="chargebee",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="CHARGEBEE_API_KEY",
            display_name="Chargebee",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(api_key)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _normal_resource(resource: str, mapping: dict[str, str]) -> str:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in mapping:
        raise ValueError(f"Unsupported resource {resource!r}. Supported: {', '.join(sorted(mapping))}.")
    return mapping[key]


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
]
