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
_PADDLE_BASE_URL = "https://vendors.paddle.com/api"
_PADDLE_SANDBOX_BASE_URL = "https://sandbox-vendors.paddle.com/api"
_PROFITWELL_BASE_URL = "https://api.profitwell.com/v2"
_TAPFILIATE_BASE_URL = "https://api.tapfiliate.com/1.6"

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


def _split_csv_ints(value: str) -> list[int]:
    return [int(part) for part in _split_csv(value)]


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


def _truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "sandbox"}


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


def _paddle_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    provider_aliases = ("paddle_api",)
    sandbox_value = _credential_value(
        provider="paddle",
        provider_aliases=provider_aliases,
        field_names=("sandbox", "use_sandbox", "useSandbox"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("paddle_sandbox")
    default_base = _PADDLE_SANDBOX_BASE_URL if _truthy(str(sandbox_value)) else _PADDLE_BASE_URL
    base = (
        _credential_value(
            provider="paddle",
            provider_aliases=provider_aliases,
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("paddle_base_url")
        or default_base
    )
    vendor_id = _credential_value(
        provider="paddle",
        provider_aliases=provider_aliases,
        field_names=("vendor_id", "vendorId"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("paddle_vendor_id")
    auth_code = _credential_value(
        provider="paddle",
        provider_aliases=provider_aliases,
        field_names=("vendor_auth_code", "vendorAuthCode", "auth_code", "authCode", "api_key", "apiKey", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("paddle_vendor_auth_code")
    if not vendor_id or not auth_code:
        return _base_url(base), _setup_hint(
            provider="paddle",
            field_names=("vendor_id", "vendor_auth_code"),
            tool_name=tool_name,
            env_var="PADDLE_VENDOR_ID + PADDLE_VENDOR_AUTH_CODE",
            display_name="Paddle",
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
            provider="profitwell",
            provider_aliases=("profitwell_api",),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("profitwell_base_url")
        or _PROFITWELL_BASE_URL
    )
    token = _credential_value(
        provider="profitwell",
        provider_aliases=("profitwell_api",),
        field_names=("access_token", "accessToken", "api_token", "apiToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("profitwell_api_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="profitwell",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="PROFITWELL_API_TOKEN",
            display_name="ProfitWell",
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
            provider="tapfiliate",
            provider_aliases=("tapfiliate_api",),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("tapfiliate_base_url")
        or _TAPFILIATE_BASE_URL
    )
    key = _credential_value(
        provider="tapfiliate",
        provider_aliases=("tapfiliate_api",),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("tapfiliate_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider="tapfiliate",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="TAPFILIATE_API_KEY",
            display_name="Tapfiliate",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Api-Key": key,
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
]
