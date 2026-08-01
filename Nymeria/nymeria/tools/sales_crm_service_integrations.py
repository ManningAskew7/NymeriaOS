"""Sales CRM service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote

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
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered_params,
    parse_json as _parse_json,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_PIPEDRIVE_V2_BASE_URL = "https://api.pipedrive.com/api/v2"
_PIPEDRIVE_V1_BASE_URL = "https://api.pipedrive.com/v1"
_SALESFORCE_API_VERSION = "v59.0"
_ZOHO_CRM_BASE_URL = "https://www.zohoapis.com/crm/v2"
_FRESHWORKS_CRM_BASE_URL = "https://{domain}.myfreshworks.com/crm/sales/api"
_SALESMATE_BASE_URL = "https://apis.salesmate.io"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_PIPEDRIVE = register_provider_spec(
    ProviderCredentialSpec(
        provider="pipedrive",
        aliases=("pipedrive_api", "pipedrive_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="api_token", names=("api_token", "apiToken", "token", "value")
            ),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "bearer_token")
            ),
        ),
        hint_fields=("api_token", "access_token", "token", "value"),
        env_var="PIPEDRIVE_API_TOKEN or PIPEDRIVE_ACCESS_TOKEN",
        display_name="Pipedrive",
    )
)

_SALESFORCE = register_provider_spec(
    ProviderCredentialSpec(
        provider="salesforce",
        aliases=("salesforce_oauth2", "salesforce_api"),
        groups=(
            CredentialFieldGroup(
                role="instance_url",
                names=("instance_url", "instanceUrl", "base_url", "url"),
            ),
            CredentialFieldGroup(
                role="access_token",
                names=("access_token", "accessToken", "bearer_token", "token", "value"),
            ),
            CredentialFieldGroup(
                role="api_version", names=("api_version", "apiVersion"), required=False
            ),
        ),
        hint_fields=("access_token", "instance_url"),
        env_var="SALESFORCE_ACCESS_TOKEN",
        display_name="Salesforce",
    )
)

_ZOHO_CRM = register_provider_spec(
    ProviderCredentialSpec(
        provider="zoho_crm",
        aliases=("zoho", "zoho_oauth2", "zoho_crm_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "api_domain", "apiDomain", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="access_token",
                names=("access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="ZOHO_CRM_ACCESS_TOKEN",
        display_name="Zoho CRM",
    )
)

_FRESHWORKS_CRM = register_provider_spec(
    ProviderCredentialSpec(
        provider="freshworks_crm",
        aliases=("freshworks", "freshsales", "freshworks_crm_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url"), required=False
            ),
            CredentialFieldGroup(role="domain", names=("domain", "subdomain")),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "apiKey", "token", "value")
            ),
        ),
        hint_fields=("api_key", "domain"),
        env_var="FRESHWORKS_CRM_API_KEY",
        display_name="Freshworks CRM",
    )
)

_SALESMATE = register_provider_spec(
    ProviderCredentialSpec(
        provider="salesmate",
        aliases=("salesmate_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url_base", "api_url"), required=False
            ),
            CredentialFieldGroup(
                role="link_name", names=("link_name", "linkName", "url", "domain")
            ),
            CredentialFieldGroup(
                role="session_token",
                names=("session_token", "sessionToken", "token", "value"),
            ),
        ),
        hint_fields=("session_token", "link_name"),
        env_var="SALESMATE_SESSION_TOKEN",
        display_name="Salesmate",
    )
)

_V2_RESOURCES = {
    "activities": "activities",
    "activity": "activities",
    "deals": "deals",
    "deal": "deals",
    "notes": "notes",
    "note": "notes",
    "organizations": "organizations",
    "organization": "organizations",
    "orgs": "organizations",
    "org": "organizations",
    "persons": "persons",
    "person": "persons",
    "people": "persons",
    "products": "products",
    "product": "products",
}
_V1_RESOURCES = {"leads": "leads", "lead": "leads"}
_SEARCH_RESOURCES = {
    "deals": "deals",
    "deal": "deals",
    "organizations": "organizations",
    "organization": "organizations",
    "orgs": "organizations",
    "org": "organizations",
    "persons": "persons",
    "person": "persons",
    "people": "persons",
    "products": "products",
    "product": "products",
    "leads": "leads",
    "lead": "leads",
}
_SALESFORCE_OBJECTS = {
    "account": "Account",
    "accounts": "Account",
    "contact": "Contact",
    "contacts": "Contact",
    "lead": "Lead",
    "leads": "Lead",
    "opportunity": "Opportunity",
    "opportunities": "Opportunity",
    "case": "Case",
    "cases": "Case",
    "task": "Task",
    "tasks": "Task",
    "user": "User",
    "users": "User",
    "campaign": "Campaign",
    "campaigns": "Campaign",
}
_ZOHO_MODULES = {
    "account": "Accounts",
    "accounts": "Accounts",
    "contact": "Contacts",
    "contacts": "Contacts",
    "deal": "Deals",
    "deals": "Deals",
    "lead": "Leads",
    "leads": "Leads",
    "product": "Products",
    "products": "Products",
    "invoice": "Invoices",
    "invoices": "Invoices",
    "quote": "Quotes",
    "quotes": "Quotes",
    "sales_order": "Sales_Orders",
    "sales_orders": "Sales_Orders",
    "purchase_order": "Purchase_Orders",
    "purchase_orders": "Purchase_Orders",
    "vendor": "Vendors",
    "vendors": "Vendors",
}
_FRESHWORKS_RESOURCES = {
    "account": "sales_accounts",
    "accounts": "sales_accounts",
    "sales_account": "sales_accounts",
    "sales_accounts": "sales_accounts",
    "contact": "contacts",
    "contacts": "contacts",
    "deal": "deals",
    "deals": "deals",
    "task": "tasks",
    "tasks": "tasks",
    "appointment": "appointments",
    "appointments": "appointments",
    "note": "notes",
    "notes": "notes",
    "sales_activity": "sales_activities",
    "sales_activities": "sales_activities",
}
_SALESMATE_RESOURCES = {
    "company": "companies",
    "companies": "companies",
    "contact": "contacts",
    "contacts": "contacts",
    "deal": "deals",
    "deals": "deals",
    "activity": "activities",
    "activities": "activities",
}


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _limit(value: int, *, default: int = 50, max_value: int = 500) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _json_records(value: str, *, label: str) -> list[dict[str, Any]]:
    parsed = json.loads(value)
    records = parsed if isinstance(parsed, list) else [parsed]
    if not records or not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{label} must be a JSON object or array of objects.")
    return records


def _api_identifier(value: str, *, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    if any(char not in allowed for char in cleaned):
        raise ValueError(f"{label} may only contain letters, numbers, and underscores.")
    return cleaned


def _salesforce_object(object_name: str) -> str:
    key = object_name.strip().lower()
    if key in _SALESFORCE_OBJECTS:
        return _SALESFORCE_OBJECTS[key]
    return _api_identifier(object_name, label="object_name")


def _zoho_module(resource: str) -> str:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _ZOHO_MODULES:
        allowed = ", ".join(sorted(set(_ZOHO_MODULES)))
        raise ValueError(f"unsupported Zoho CRM resource '{resource}'. Use one of: {allowed}")
    return _ZOHO_MODULES[key]


def _freshworks_resource(resource: str) -> str:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _FRESHWORKS_RESOURCES:
        allowed = ", ".join(sorted(set(_FRESHWORKS_RESOURCES)))
        raise ValueError(f"unsupported Freshworks CRM resource '{resource}'. Use one of: {allowed}")
    return _FRESHWORKS_RESOURCES[key]


def _salesmate_resource(resource: str) -> str:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _SALESMATE_RESOURCES:
        allowed = ", ".join(sorted(set(_SALESMATE_RESOURCES)))
        raise ValueError(f"unsupported Salesmate resource '{resource}'. Use one of: {allowed}")
    return _SALESMATE_RESOURCES[key]


def _pipedrive_resource(resource: str) -> tuple[str, str]:
    key = resource.strip().lower()
    if key in _V2_RESOURCES:
        return _V2_RESOURCES[key], "v2"
    if key in _V1_RESOURCES:
        return _V1_RESOURCES[key], "v1"
    allowed = sorted(set(_V2_RESOURCES) | set(_V1_RESOURCES))
    raise ValueError(f"unsupported Pipedrive resource '{resource}'. Use one of: {', '.join(allowed)}")


def _pipedrive_search_resource(resource: str) -> str:
    key = resource.strip().lower()
    if key not in _SEARCH_RESOURCES:
        allowed = sorted(set(_SEARCH_RESOURCES))
        raise ValueError(f"unsupported Pipedrive search resource '{resource}'. Use one of: {', '.join(allowed)}")
    return _SEARCH_RESOURCES[key]


def _pipedrive_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str], dict[str, str] | str]:
    base_from_vault = _credential_value(
        provider=_PIPEDRIVE.provider,
        provider_aliases=_PIPEDRIVE.aliases,
        field_names=_PIPEDRIVE.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = (
        base_from_vault
        or _settings_value("pipedrive_base_url")
        or _PIPEDRIVE_V2_BASE_URL
    )
    api_token_from_vault = _credential_value(
        provider=_PIPEDRIVE.provider,
        provider_aliases=_PIPEDRIVE.aliases,
        field_names=_PIPEDRIVE.group("api_token"),
        tool_name=tool_name,
        config=config,
    )
    api_token = api_token_from_vault or _settings_value("pipedrive_api_token")
    access_token_from_vault = _credential_value(
        provider=_PIPEDRIVE.provider,
        provider_aliases=_PIPEDRIVE.aliases,
        field_names=_PIPEDRIVE.group("access_token"),
        tool_name=tool_name,
        config=config,
    )
    access_token = access_token_from_vault or _settings_value("pipedrive_access_token")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    auth_params: dict[str, str] = {}
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request: a record holding base_url + api_token clears slice B's "some
    # anchor", and the bearer branch would then send the operator's access token
    # to the address that record chose.
    if access_token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=access_token_from_vault,
            secret=access_token,
            provider=_PIPEDRIVE.provider,
        )
        headers["Authorization"] = f"Bearer {access_token}"
    elif api_token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=api_token_from_vault,
            secret=api_token,
            provider=_PIPEDRIVE.provider,
        )
        auth_params["api_token"] = api_token
    else:
        return _base_url(base), _PIPEDRIVE_V1_BASE_URL, headers, _setup_hint(
            provider=_PIPEDRIVE.provider,
            field_names=_PIPEDRIVE.hint_fields,
            tool_name=tool_name,
            env_var=_PIPEDRIVE.env_var,
            display_name=_PIPEDRIVE.display_name,
        )

    base_v2 = _base_url(base)
    if base_v2.endswith("/v1"):
        base_v1 = base_v2
        base_v2 = base_v2.removesuffix("/v1") + "/api/v2"
    elif base_v2.endswith("/api/v2"):
        base_v1 = base_v2.removesuffix("/api/v2") + "/v1"
    else:
        base_v1 = base_v2.rstrip("/") + "/v1"
    return base_v2, base_v1, headers, auth_params


def _salesforce_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    instance_url = (
        _credential_value(
            provider=_SALESFORCE.provider,
            provider_aliases=_SALESFORCE.aliases,
            field_names=_SALESFORCE.group("instance_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("salesforce_instance_url")
        or _settings_value("salesforce_base_url")
    )
    token = _credential_value(
        provider=_SALESFORCE.provider,
        provider_aliases=_SALESFORCE.aliases,
        field_names=_SALESFORCE.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("salesforce_access_token")
    if not instance_url:
        return "", (
            '[Error]: No Salesforce instance URL found. Save a Salesforce credential with "instance_url" '
            "or set SALESFORCE_INSTANCE_URL."
        )
    if not token:
        return _base_url(instance_url), _setup_hint(
            provider=_SALESFORCE.provider,
            field_names=_SALESFORCE.hint_fields,
            tool_name=tool_name,
            env_var=_SALESFORCE.env_var,
            display_name=_SALESFORCE.display_name,
        )
    api_version = (
        _credential_value(
            provider=_SALESFORCE.provider,
            provider_aliases=_SALESFORCE.aliases,
            field_names=_SALESFORCE.group("api_version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("salesforce_api_version")
        or _SALESFORCE_API_VERSION
    )
    base = _base_url(instance_url)
    if "/services/data/" not in base:
        base = f"{base}/services/data/{api_version.strip().lstrip('/')}"
    return base, {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _zoho_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_ZOHO_CRM.provider,
            provider_aliases=_ZOHO_CRM.aliases,
            field_names=_ZOHO_CRM.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("zoho_crm_api_domain")
        or _settings_value("zoho_crm_base_url")
        or _ZOHO_CRM_BASE_URL
    )
    token = _credential_value(
        provider=_ZOHO_CRM.provider,
        provider_aliases=_ZOHO_CRM.aliases,
        field_names=_ZOHO_CRM.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("zoho_crm_access_token")
    base = _base_url(base)
    if not base.endswith("/crm/v2"):
        base = f"{base}/crm/v2"
    if not token:
        return base, _setup_hint(
            provider=_ZOHO_CRM.provider,
            field_names=_ZOHO_CRM.hint_fields,
            tool_name=tool_name,
            env_var=_ZOHO_CRM.env_var,
            display_name=_ZOHO_CRM.display_name,
        )
    return base, {
        "Accept": "application/json",
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _freshworks_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_FRESHWORKS_CRM.provider,
            provider_aliases=_FRESHWORKS_CRM.aliases,
            field_names=_FRESHWORKS_CRM.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshworks_crm_base_url")
    )
    domain = (
        _credential_value(
            provider=_FRESHWORKS_CRM.provider,
            provider_aliases=_FRESHWORKS_CRM.aliases,
            field_names=_FRESHWORKS_CRM.group("domain"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshworks_crm_domain")
    )
    api_key = _credential_value(
        provider=_FRESHWORKS_CRM.provider,
        provider_aliases=_FRESHWORKS_CRM.aliases,
        field_names=_FRESHWORKS_CRM.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("freshworks_crm_api_key")
    if not base:
        if not domain:
            return "", (
                '[Error]: No Freshworks CRM domain found. Save a Freshworks CRM credential with "domain" '
                "or set FRESHWORKS_CRM_DOMAIN."
            )
        base = _FRESHWORKS_CRM_BASE_URL.format(domain=domain.strip().replace(".myfreshworks.com", ""))
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_FRESHWORKS_CRM.provider,
            field_names=_FRESHWORKS_CRM.hint_fields,
            tool_name=tool_name,
            env_var=_FRESHWORKS_CRM.env_var,
            display_name=_FRESHWORKS_CRM.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Token token={api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _salesmate_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_SALESMATE.provider,
            provider_aliases=_SALESMATE.aliases,
            field_names=_SALESMATE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("salesmate_base_url")
        or _SALESMATE_BASE_URL
    )
    link_name = (
        _credential_value(
            provider=_SALESMATE.provider,
            provider_aliases=_SALESMATE.aliases,
            field_names=_SALESMATE.group("link_name"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("salesmate_link_name")
    )
    token = _credential_value(
        provider=_SALESMATE.provider,
        provider_aliases=_SALESMATE.aliases,
        field_names=_SALESMATE.group("session_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("salesmate_session_token")
    if not link_name:
        return _base_url(base), (
            '[Error]: No Salesmate link name found. Save a Salesmate credential with "link_name" '
            "or set SALESMATE_LINK_NAME."
        )
    if not token:
        return _base_url(base), _setup_hint(
            provider=_SALESMATE.provider,
            field_names=_SALESMATE.hint_fields,
            tool_name=tool_name,
            env_var=_SALESMATE.env_var,
            display_name=_SALESMATE.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "sessionToken": token,
        "x-linkname": link_name.strip(),
    }


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    auth_params: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    request_params = _filtered_params({**(params or {}), **(auth_params or {})})
    try:
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(client, method, url, params=request_params, json=json_body, headers=headers)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            payload = response.json()
            if isinstance(payload, dict) and payload.get("success") is False:
                detail = payload.get("error") or payload.get("error_info") or payload.get("message") or "request failed"
                raise RuntimeError(str(detail))
            return payload
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = body.get("error") or body.get("error_info") or body.get("message") or ""
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _pipedrive_request(
    tool_name: str,
    method: str,
    endpoint: str,
    *,
    api_version: str,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    config: Optional[RunnableConfig],
) -> Any:
    base_v2, base_v1, headers, auth_params_or_error = _pipedrive_config(tool_name, config)
    if isinstance(auth_params_or_error, str):
        return auth_params_or_error
    base = base_v1 if api_version == "v1" else base_v2
    return _request_json(
        method,
        f"{base}{endpoint}",
        params=params,
        json_body=json_body,
        headers=headers,
        auth_params=auth_params_or_error,
    )


def _pipedrive_items(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data", payload)
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    return data


def _zoho_data(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return payload


def _salesmate_data(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    data = payload.get("Data", payload.get("data", payload))
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


@tool
def salesforce_query_records(
    soql: str,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a Salesforce SOQL query.

    Args:
        soql: SOQL SELECT query.
        limit: Maximum rows to return when the query does not already include a LIMIT.
    """
    query = soql.strip()
    if not query.lower().startswith("select "):
        return "[Error]: soql must be a SELECT query."
    if " limit " not in f" {query.lower()} ":
        query = f"{query} LIMIT {_limit(limit, max_value=2000)}"
    try:
        base, headers_or_error = _salesforce_config("salesforce_query_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base}/query", params={"q": query}, headers=headers_or_error)
        return _dump_json(data.get("records", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("salesforce_query_records failed", exc_info=True)
        return f"[Error]: Salesforce query failed: {e}"


@tool
def salesforce_get_record(
    object_name: str,
    record_id: str,
    fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Salesforce object record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        sf_object = _salesforce_object(object_name)
        base, headers_or_error = _salesforce_config("salesforce_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"fields": ",".join(_split_csv(fields))}
        data = _request_json(
            "GET",
            f"{base}/sobjects/{quote(sf_object, safe='')}/{quote(record_id.strip(), safe='')}",
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("salesforce_get_record failed", exc_info=True)
        return f"[Error]: Salesforce record lookup failed: {e}"


@tool
def salesforce_create_record(
    object_name: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Salesforce object record from a JSON object."""
    try:
        sf_object = _salesforce_object(object_name)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _salesforce_config("salesforce_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base}/sobjects/{quote(sf_object, safe='')}",
            json_body=fields_payload,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("salesforce_create_record failed", exc_info=True)
        return f"[Error]: Salesforce record creation failed: {e}"


@tool
def salesforce_update_record(
    object_name: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Salesforce object record from a JSON object."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        sf_object = _salesforce_object(object_name)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _salesforce_config("salesforce_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base}/sobjects/{quote(sf_object, safe='')}/{quote(record_id.strip(), safe='')}",
            json_body=fields_payload,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("salesforce_update_record failed", exc_info=True)
        return f"[Error]: Salesforce record update failed: {e}"


@tool
def salesforce_delete_record(
    object_name: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Salesforce object record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        sf_object = _salesforce_object(object_name)
        base, headers_or_error = _salesforce_config("salesforce_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base}/sobjects/{quote(sf_object, safe='')}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("salesforce_delete_record failed", exc_info=True)
        return f"[Error]: Salesforce record deletion failed: {e}"


@tool
def zoho_crm_list_records(
    resource: str = "leads",
    fields: str = "",
    page: int = 1,
    per_page: int = 50,
    sort_by: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Zoho CRM records for a module."""
    try:
        module = _zoho_module(resource)
        base, headers_or_error = _zoho_config("zoho_crm_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = _filtered_params(
            {
                "fields": ",".join(_split_csv(fields)),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, max_value=200),
                "sort_by": sort_by.strip(),
            }
        )
        data = _request_json("GET", f"{base}/{module}", params=params, headers=headers_or_error)
        return _dump_json(_zoho_data(data))
    except Exception as e:
        logger.error("zoho_crm_list_records failed", exc_info=True)
        return f"[Error]: Zoho CRM record list failed: {e}"


@tool
def zoho_crm_search_records(
    resource: str,
    criteria: str = "",
    email: str = "",
    phone: str = "",
    word: str = "",
    page: int = 1,
    per_page: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Zoho CRM records by criteria, email, phone, or word."""
    if not any(value.strip() for value in (criteria, email, phone, word)):
        return "[Error]: provide criteria, email, phone, or word."
    try:
        module = _zoho_module(resource)
        base, headers_or_error = _zoho_config("zoho_crm_search_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = _filtered_params(
            {
                "criteria": criteria.strip(),
                "email": email.strip(),
                "phone": phone.strip(),
                "word": word.strip(),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, max_value=200),
            }
        )
        data = _request_json("GET", f"{base}/{module}/search", params=params, headers=headers_or_error)
        return _dump_json(_zoho_data(data))
    except Exception as e:
        logger.error("zoho_crm_search_records failed", exc_info=True)
        return f"[Error]: Zoho CRM record search failed: {e}"


@tool
def zoho_crm_get_record(
    resource: str,
    record_id: str,
    fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Zoho CRM record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        module = _zoho_module(resource)
        base, headers_or_error = _zoho_config("zoho_crm_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"fields": ",".join(_split_csv(fields))}
        data = _request_json(
            "GET",
            f"{base}/{module}/{quote(record_id.strip(), safe='')}",
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(_zoho_data(data))
    except Exception as e:
        logger.error("zoho_crm_get_record failed", exc_info=True)
        return f"[Error]: Zoho CRM record lookup failed: {e}"


@tool
def zoho_crm_create_records(
    resource: str,
    records_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create one or more Zoho CRM records from JSON object(s)."""
    try:
        module = _zoho_module(resource)
        records = _json_records(records_json, label="records_json")
        base, headers_or_error = _zoho_config("zoho_crm_create_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base}/{module}", json_body={"data": records}, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("zoho_crm_create_records failed", exc_info=True)
        return f"[Error]: Zoho CRM record creation failed: {e}"


@tool
def zoho_crm_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update one Zoho CRM record from a JSON object."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        module = _zoho_module(resource)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _zoho_config("zoho_crm_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base}/{module}/{quote(record_id.strip(), safe='')}",
            json_body={"data": [fields_payload]},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zoho_crm_update_record failed", exc_info=True)
        return f"[Error]: Zoho CRM record update failed: {e}"


@tool
def zoho_crm_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete one Zoho CRM record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        module = _zoho_module(resource)
        base, headers_or_error = _zoho_config("zoho_crm_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base}/{module}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zoho_crm_delete_record failed", exc_info=True)
        return f"[Error]: Zoho CRM record deletion failed: {e}"


@tool
def freshworks_crm_list_records(
    resource: str = "contacts",
    view_id: str = "",
    page: int = 1,
    per_page: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Freshworks CRM records."""
    try:
        resource_path = _freshworks_resource(resource)
        endpoint = f"/{resource_path}/view/{quote(view_id.strip(), safe='')}" if view_id.strip() else f"/{resource_path}"
        base, headers_or_error = _freshworks_config("freshworks_crm_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base}{endpoint}",
            params={"page": max(1, int(page or 1)), "per_page": _limit(per_page, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshworks_crm_list_records failed", exc_info=True)
        return f"[Error]: Freshworks CRM record list failed: {e}"


@tool
def freshworks_crm_search_records(
    term: str,
    include: str = "",
    page: int = 1,
    per_page: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Freshworks CRM records."""
    if not term.strip():
        return "[Error]: term is required."
    try:
        base, headers_or_error = _freshworks_config("freshworks_crm_search_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "q": term.strip(),
            "include": ",".join(_split_csv(include)),
            "page": max(1, int(page or 1)),
            "per_page": _limit(per_page, max_value=100),
        }
        data = _request_json("GET", f"{base}/search", params=params, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshworks_crm_search_records failed", exc_info=True)
        return f"[Error]: Freshworks CRM record search failed: {e}"


@tool
def freshworks_crm_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Freshworks CRM record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        resource_path = _freshworks_resource(resource)
        base, headers_or_error = _freshworks_config("freshworks_crm_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base}/{resource_path}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshworks_crm_get_record failed", exc_info=True)
        return f"[Error]: Freshworks CRM record lookup failed: {e}"


@tool
def freshworks_crm_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create one Freshworks CRM record from a JSON object."""
    try:
        resource_path = _freshworks_resource(resource)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _freshworks_config("freshworks_crm_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base}/{resource_path}", json_body=fields_payload, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshworks_crm_create_record failed", exc_info=True)
        return f"[Error]: Freshworks CRM record creation failed: {e}"


@tool
def freshworks_crm_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update one Freshworks CRM record from a JSON object."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        resource_path = _freshworks_resource(resource)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _freshworks_config("freshworks_crm_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base}/{resource_path}/{quote(record_id.strip(), safe='')}",
            json_body=fields_payload,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshworks_crm_update_record failed", exc_info=True)
        return f"[Error]: Freshworks CRM record update failed: {e}"


@tool
def freshworks_crm_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete one Freshworks CRM record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        resource_path = _freshworks_resource(resource)
        base, headers_or_error = _freshworks_config("freshworks_crm_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base}/{resource_path}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshworks_crm_delete_record failed", exc_info=True)
        return f"[Error]: Freshworks CRM record deletion failed: {e}"


@tool
def salesmate_list_users(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List active Salesmate users."""
    try:
        base, headers_or_error = _salesmate_config("salesmate_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base}/v1/users/active", headers=headers_or_error)
        return _dump_json(_salesmate_data(data))
    except Exception as e:
        logger.error("salesmate_list_users failed", exc_info=True)
        return f"[Error]: Salesmate user list failed: {e}"


@tool
def salesmate_search_records(
    resource: str = "companies",
    query_json: str = "",
    fields: str = "",
    page_no: int = 1,
    rows: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Salesmate records."""
    try:
        resource_path = _salesmate_resource(resource)
        query = _parse_json(query_json, expected=dict, label="query_json")
        field_list = _split_csv(fields) or ["name", "id"]
        body = {"fields": field_list, "query": query}
        base, headers_or_error = _salesmate_config("salesmate_search_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base}/v2/{resource_path}/search",
            params={"pageNo": max(1, int(page_no or 1)), "rows": _limit(rows, max_value=250)},
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(_salesmate_data(data))
    except Exception as e:
        logger.error("salesmate_search_records failed", exc_info=True)
        return f"[Error]: Salesmate record search failed: {e}"


@tool
def salesmate_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Salesmate record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        resource_path = _salesmate_resource(resource)
        base, headers_or_error = _salesmate_config("salesmate_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base}/v1/{resource_path}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(_salesmate_data(data))
    except Exception as e:
        logger.error("salesmate_get_record failed", exc_info=True)
        return f"[Error]: Salesmate record lookup failed: {e}"


@tool
def salesmate_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create one Salesmate record from a JSON object."""
    try:
        resource_path = _salesmate_resource(resource)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _salesmate_config("salesmate_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base}/v1/{resource_path}", json_body=fields_payload, headers=headers_or_error)
        return _dump_json(_salesmate_data(data))
    except Exception as e:
        logger.error("salesmate_create_record failed", exc_info=True)
        return f"[Error]: Salesmate record creation failed: {e}"


@tool
def salesmate_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update one Salesmate record from a JSON object."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        resource_path = _salesmate_resource(resource)
        fields_payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields_payload:
            return "[Error]: fields_json must contain at least one field."
        base, headers_or_error = _salesmate_config("salesmate_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base}/v1/{resource_path}/{quote(record_id.strip(), safe='')}",
            json_body=fields_payload,
            headers=headers_or_error,
        )
        return _dump_json(_salesmate_data(data))
    except Exception as e:
        logger.error("salesmate_update_record failed", exc_info=True)
        return f"[Error]: Salesmate record update failed: {e}"


@tool
def salesmate_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete one Salesmate record by ID."""
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        resource_path = _salesmate_resource(resource)
        base, headers_or_error = _salesmate_config("salesmate_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base}/v1/{resource_path}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("salesmate_delete_record failed", exc_info=True)
        return f"[Error]: Salesmate record deletion failed: {e}"


@tool
def pipedrive_list_records(
    resource: str = "deals",
    limit: int = 50,
    cursor: str = "",
    start: int = 0,
    filter_id: str = "",
    owner_id: str = "",
    person_id: str = "",
    organization_id: str = "",
    deal_id: str = "",
    extra_params_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Pipedrive CRM records.

    Args:
        resource: Resource type: deals, persons, organizations, activities, leads, notes, or products.
        limit: Number of records to return, 1-500.
        cursor: Optional v2 pagination cursor.
        start: Optional v1 pagination offset, mostly for leads.
        filter_id: Optional Pipedrive filter ID.
        owner_id: Optional owner/user ID filter.
        person_id: Optional person ID filter.
        organization_id: Optional organization ID filter.
        deal_id: Optional deal ID filter.
        extra_params_json: Optional extra query params object as JSON.
    """
    try:
        resource_name, version = _pipedrive_resource(resource)
        params = _filtered_params(
            {
                "limit": _limit(limit),
                "cursor": cursor.strip() if version == "v2" else None,
                "start": max(0, int(start or 0)) if version == "v1" else None,
                "filter_id": filter_id.strip(),
                "owner_id": owner_id.strip(),
                "person_id": person_id.strip(),
                "organization_id": organization_id.strip(),
                "deal_id": deal_id.strip(),
                **_parse_json(extra_params_json, expected=dict, label="extra_params_json"),
            }
        )
        payload = _pipedrive_request(
            "pipedrive_list_records",
            "GET",
            f"/{resource_name}",
            api_version=version,
            params=params,
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_list_records failed", exc_info=True)
        return f"[Error]: Pipedrive record list failed: {e}"


@tool
def pipedrive_search_records(
    resource: str,
    term: str,
    exact_match: bool = False,
    fields: str = "",
    limit: int = 50,
    extra_params_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Pipedrive CRM records.

    Args:
        resource: Search resource: deals, persons, organizations, products, or leads.
        term: Search term.
        exact_match: Return only exact full matches.
        fields: Optional comma-separated fields for supported resources.
        limit: Number of results to return, 1-500.
        extra_params_json: Optional extra query params object as JSON.
    """
    if not term.strip():
        return "[Error]: term is required."
    try:
        resource_name = _pipedrive_search_resource(resource)
        params = _filtered_params(
            {
                "term": term.strip(),
                "exact_match": str(bool(exact_match)).lower(),
                "fields": ",".join(_split_csv(fields)),
                "limit": _limit(limit),
                **_parse_json(extra_params_json, expected=dict, label="extra_params_json"),
            }
        )
        payload = _pipedrive_request(
            "pipedrive_search_records",
            "GET",
            f"/{resource_name}/search",
            api_version="v1",
            params=params,
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_search_records failed", exc_info=True)
        return f"[Error]: Pipedrive record search failed: {e}"


@tool
def pipedrive_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Pipedrive CRM record by ID.

    Args:
        resource: Resource type: deals, persons, organizations, activities, leads, notes, or products.
        record_id: Pipedrive record ID.
    """
    record_id = record_id.strip()
    if not record_id:
        return "[Error]: record_id is required."
    try:
        resource_name, version = _pipedrive_resource(resource)
        payload = _pipedrive_request(
            "pipedrive_get_record",
            "GET",
            f"/{resource_name}/{quote(record_id, safe='')}",
            api_version=version,
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_get_record failed", exc_info=True)
        return f"[Error]: Pipedrive record lookup failed: {e}"


@tool
def pipedrive_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Pipedrive CRM record.

    Args:
        resource: Resource type: deals, persons, organizations, activities, leads, notes, or products.
        fields_json: Record fields object as JSON, such as {"title":"New deal"} for a deal.
    """
    if not fields_json.strip():
        return "[Error]: fields_json is required."
    try:
        resource_name, version = _pipedrive_resource(resource)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        payload = _pipedrive_request(
            "pipedrive_create_record",
            "POST",
            f"/{resource_name}",
            api_version=version,
            json_body=body,
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_create_record failed", exc_info=True)
        return f"[Error]: Pipedrive record creation failed: {e}"


@tool
def pipedrive_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Pipedrive CRM record.

    Args:
        resource: Resource type: deals, persons, organizations, activities, leads, notes, or products.
        record_id: Pipedrive record ID.
        fields_json: Record fields object as JSON.
    """
    record_id = record_id.strip()
    if not record_id or not fields_json.strip():
        return "[Error]: record_id and fields_json are required."
    try:
        resource_name, version = _pipedrive_resource(resource)
        method = "PUT" if version == "v1" else "PATCH"
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        payload = _pipedrive_request(
            "pipedrive_update_record",
            method,
            f"/{resource_name}/{quote(record_id, safe='')}",
            api_version=version,
            json_body=body,
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_update_record failed", exc_info=True)
        return f"[Error]: Pipedrive record update failed: {e}"


@tool
def pipedrive_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Pipedrive CRM record.

    Args:
        resource: Resource type: deals, persons, organizations, activities, leads, notes, or products.
        record_id: Pipedrive record ID.
    """
    record_id = record_id.strip()
    if not record_id:
        return "[Error]: record_id is required."
    try:
        resource_name, version = _pipedrive_resource(resource)
        payload = _pipedrive_request(
            "pipedrive_delete_record",
            "DELETE",
            f"/{resource_name}/{quote(record_id, safe='')}",
            api_version=version,
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_delete_record failed", exc_info=True)
        return f"[Error]: Pipedrive record deletion failed: {e}"


@tool
def pipedrive_list_users(
    limit: int = 50,
    start: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Pipedrive users.

    Args:
        limit: Number of users to return, 1-500.
        start: Pagination offset.
    """
    try:
        payload = _pipedrive_request(
            "pipedrive_list_users",
            "GET",
            "/users",
            api_version="v1",
            params={"limit": _limit(limit), "start": max(0, int(start or 0))},
            config=config,
        )
        if isinstance(payload, str):
            return payload
        return _dump_json(_pipedrive_items(payload))
    except Exception as e:
        logger.error("pipedrive_list_users failed", exc_info=True)
        return f"[Error]: Pipedrive user list failed: {e}"


SALES_CRM_SERVICE_TOOLS = [
    salesforce_query_records,
    salesforce_get_record,
    salesforce_create_record,
    salesforce_update_record,
    salesforce_delete_record,
    zoho_crm_list_records,
    zoho_crm_search_records,
    zoho_crm_get_record,
    zoho_crm_create_records,
    zoho_crm_update_record,
    zoho_crm_delete_record,
    freshworks_crm_list_records,
    freshworks_crm_search_records,
    freshworks_crm_get_record,
    freshworks_crm_create_record,
    freshworks_crm_update_record,
    freshworks_crm_delete_record,
    salesmate_list_users,
    salesmate_search_records,
    salesmate_get_record,
    salesmate_create_record,
    salesmate_update_record,
    salesmate_delete_record,
    pipedrive_list_records,
    pipedrive_search_records,
    pipedrive_get_record,
    pipedrive_create_record,
    pipedrive_update_record,
    pipedrive_delete_record,
    pipedrive_list_users,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="sales_crm", tools=tuple(SALES_CRM_SERVICE_TOOLS)))
