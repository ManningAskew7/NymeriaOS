"""Relationship CRM service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import json
import logging
import re
from typing import Annotated, Any, Optional
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .service_integration_base import (
    API_KEY_ALIAS_FIELDS,
    API_KEY_FIELDS,
    BASE_URL_ALIAS_FIELDS,
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    json_object as _json_object,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_COPPER_BASE_URL = "https://api.copper.com/developer_api/v1"
_AGILE_PLACEHOLDER_BASE_URL = "https://example.agilecrm.com/dev"
_MONICA_BASE_URL = "https://app.monicahq.com/api"
_AFFINITY_BASE_URL = "https://api.affinity.co"
_KEAP_BASE_URL = "https://api.infusionsoft.com/crm/rest/v1"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_COPPER = register_provider_spec(
    ProviderCredentialSpec(
        provider="copper",
        aliases=("copper_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=(
                    "api_key",
                    "apiKey",
                    "access_token",
                    "accessToken",
                    "token",
                    "value",
                ),
            ),
            CredentialFieldGroup(
                role="email", names=("email", "user_email", "userEmail")
            ),
        ),
        hint_fields=("api_key", "email"),
        env_var="COPPER_API_KEY and COPPER_EMAIL",
        display_name="Copper",
    )
)

_AGILECRM = register_provider_spec(
    ProviderCredentialSpec(
        provider="agilecrm",
        aliases=("agile_crm", "agilecrm_api", "agile_crm_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(role="api_key", names=API_KEY_ALIAS_FIELDS),
            CredentialFieldGroup(role="email", names=("email", "username")),
            CredentialFieldGroup(role="subdomain", names=("subdomain", "domain")),
        ),
        hint_fields=("email", "api_key", "subdomain"),
        env_var="AGILECRM_EMAIL, AGILECRM_API_KEY, and AGILECRM_SUBDOMAIN",
        display_name="Agile CRM",
    )
)

_MONICA = register_provider_spec(
    ProviderCredentialSpec(
        provider="monica",
        aliases=("monica_crm", "monicacrm", "monica_crm_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "domain", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token",
                names=(
                    "api_token",
                    "apiToken",
                    "access_token",
                    "accessToken",
                    "token",
                    "value",
                ),
            ),
        ),
        hint_fields=("api_token", "access_token", "value"),
        env_var="MONICA_ACCESS_TOKEN",
        display_name="Monica CRM",
    )
)

_AFFINITY = register_provider_spec(
    ProviderCredentialSpec(
        provider="affinity",
        aliases=("affinity_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(role="api_key", names=API_KEY_ALIAS_FIELDS),
        ),
        hint_fields=API_KEY_FIELDS,
        env_var="AFFINITY_API_KEY",
        display_name="Affinity",
    )
)

_KEAP = register_provider_spec(
    ProviderCredentialSpec(
        provider="keap",
        aliases=("keap_oauth2", "keap_oauth2_api", "infusionsoft"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(
                role="access_token",
                names=(
                    "access_token",
                    "accessToken",
                    "bearer_token",
                    "bearerToken",
                    "token",
                    "value",
                ),
            ),
        ),
        hint_fields=("access_token", "value"),
        env_var="KEAP_ACCESS_TOKEN",
        display_name="Keap",
    )
)

_COPPER_RESOURCES = {
    "companies": "companies",
    "company": "companies",
    "people": "people",
    "person": "people",
    "persons": "people",
    "leads": "leads",
    "lead": "leads",
    "opportunities": "opportunities",
    "opportunity": "opportunities",
    "projects": "projects",
    "project": "projects",
    "tasks": "tasks",
    "task": "tasks",
    "users": "users",
    "user": "users",
    "customer_sources": "customer_sources",
    "customer_source": "customer_sources",
}
_AGILE_RESOURCES = {
    "contacts": "contacts",
    "contact": "contacts",
    "companies": "companies",
    "company": "companies",
    "deals": "deals",
    "deal": "deals",
    "opportunities": "deals",
    "opportunity": "deals",
}
_MONICA_RESOURCES = {
    "activities": "activities",
    "activity": "activities",
    "calls": "calls",
    "call": "calls",
    "contacts": "contacts",
    "contact": "contacts",
    "contact_fields": "contactfields",
    "contact_field": "contactfields",
    "conversations": "conversations",
    "conversation": "conversations",
    "journal_entries": "journal_entries",
    "journal_entry": "journal_entries",
    "notes": "notes",
    "note": "notes",
    "reminders": "reminders",
    "reminder": "reminders",
    "tags": "tags",
    "tag": "tags",
    "tasks": "tasks",
    "task": "tasks",
}
_AFFINITY_RESOURCES = {
    "lists": "lists",
    "list": "lists",
    "organizations": "organizations",
    "organization": "organizations",
    "orgs": "organizations",
    "org": "organizations",
    "companies": "organizations",
    "company": "organizations",
    "persons": "persons",
    "person": "persons",
    "people": "persons",
}
_AFFINITY_ENTITY_RESOURCES = {
    "organizations": "organizations",
    "organization": "organizations",
    "orgs": "organizations",
    "org": "organizations",
    "companies": "organizations",
    "company": "organizations",
    "persons": "persons",
    "person": "persons",
    "people": "persons",
}
_KEAP_RESOURCES = {
    "companies": "companies",
    "company": "companies",
    "contacts": "contacts",
    "contact": "contacts",
    "notes": "notes",
    "note": "notes",
    "contact_notes": "notes",
    "contact_note": "notes",
    "orders": "orders",
    "order": "orders",
    "ecommerce_orders": "orders",
    "ecommerce_order": "orders",
    "products": "products",
    "product": "products",
    "ecommerce_products": "products",
    "ecommerce_product": "products",
    "emails": "emails",
    "email": "emails",
    "files": "files",
    "file": "files",
    "tags": "tags",
    "tag": "tags",
    "users": "users",
    "user": "users",
}


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 50, max_value: int = 200) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _csv_to_ints(value: str, *, field_name: str) -> list[int]:
    numbers: list[int] = []
    for part in _csv_to_list(value):
        try:
            numbers.append(int(part))
        except ValueError as exc:
            raise ValueError(f"{field_name} must contain comma-separated integer IDs") from exc
    return numbers


def _snake_key(key: str) -> str:
    normalized = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return normalized.replace("-", "_").replace(" ", "_").lower()


def _snake_case_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {_snake_key(str(key)): _snake_case_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snake_case_data(item) for item in value]
    return value


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params),
                json=json_body,
                data=data,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            return response.json()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = (
                body.get("message")
                or body.get("detail")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _resource(name: str, resources: dict[str, str], label: str) -> str:
    key = name.strip().lower()
    if key not in resources:
        allowed = ", ".join(sorted(set(resources)))
        raise ValueError(f"unsupported {label} resource '{name}'. Use one of: {allowed}")
    return resources[key]


def _copper_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_COPPER.provider,
            provider_aliases=_COPPER.aliases,
            field_names=_COPPER.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("copper_base_url")
        or _COPPER_BASE_URL
    )
    api_key = _credential_value(
        provider=_COPPER.provider,
        provider_aliases=_COPPER.aliases,
        field_names=_COPPER.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("copper_api_key")
    email = _credential_value(
        provider=_COPPER.provider,
        provider_aliases=_COPPER.aliases,
        field_names=_COPPER.group("email"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("copper_email")
    if not api_key or not email:
        return _base_url(base), _setup_hint(
            provider=_COPPER.provider,
            field_names=_COPPER.hint_fields,
            tool_name=tool_name,
            env_var=_COPPER.env_var,
            display_name=_COPPER.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-PW-AccessToken": api_key,
        "X-PW-Application": "developer_api",
        "X-PW-UserEmail": email,
        "User-Agent": "Nymeria",
    }


def _agile_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    subdomain = _credential_value(
        provider=_AGILECRM.provider,
        provider_aliases=_AGILECRM.aliases,
        field_names=_AGILECRM.group("subdomain"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("agilecrm_subdomain")
    base = (
        _credential_value(
            provider=_AGILECRM.provider,
            provider_aliases=_AGILECRM.aliases,
            field_names=_AGILECRM.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("agilecrm_base_url")
        or (f"https://{subdomain}.agilecrm.com/dev" if subdomain else _AGILE_PLACEHOLDER_BASE_URL)
    )
    email = _credential_value(
        provider=_AGILECRM.provider,
        provider_aliases=_AGILECRM.aliases,
        field_names=_AGILECRM.group("email"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("agilecrm_email")
    api_key = _credential_value(
        provider=_AGILECRM.provider,
        provider_aliases=_AGILECRM.aliases,
        field_names=_AGILECRM.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("agilecrm_api_key")
    if not email or not api_key or base == _AGILE_PLACEHOLDER_BASE_URL:
        return _base_url(base), _setup_hint(
            provider=_AGILECRM.provider,
            field_names=_AGILECRM.hint_fields,
            tool_name=tool_name,
            env_var=_AGILECRM.env_var,
            display_name=_AGILECRM.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": _basic_auth(email, api_key),
        "User-Agent": "Nymeria",
    }


def _monica_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_MONICA.provider,
            provider_aliases=_MONICA.aliases,
            field_names=_MONICA.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("monica_base_url")
        or _MONICA_BASE_URL
    )
    token = _credential_value(
        provider=_MONICA.provider,
        provider_aliases=_MONICA.aliases,
        field_names=_MONICA.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("monica_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_MONICA.provider,
            field_names=_MONICA.hint_fields,
            tool_name=tool_name,
            env_var=_MONICA.env_var,
            display_name=_MONICA.display_name,
        )
    clean = _base_url(base)
    if not clean.endswith("/api"):
        clean = f"{clean}/api"
    return clean, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _affinity_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_AFFINITY.provider,
            provider_aliases=_AFFINITY.aliases,
            field_names=_AFFINITY.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("affinity_base_url")
        or _AFFINITY_BASE_URL
    )
    api_key = _credential_value(
        provider=_AFFINITY.provider,
        provider_aliases=_AFFINITY.aliases,
        field_names=_AFFINITY.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("affinity_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_AFFINITY.provider,
            field_names=_AFFINITY.hint_fields,
            tool_name=tool_name,
            env_var=_AFFINITY.env_var,
            display_name=_AFFINITY.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": _basic_auth("", api_key),
        "User-Agent": "Nymeria",
    }


def _keap_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_KEAP.provider,
            provider_aliases=_KEAP.aliases,
            field_names=_KEAP.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("keap_base_url")
        or _KEAP_BASE_URL
    )
    access_token = _credential_value(
        provider=_KEAP.provider,
        provider_aliases=_KEAP.aliases,
        field_names=_KEAP.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("keap_access_token")
    if not access_token:
        return _base_url(base), _setup_hint(
            provider=_KEAP.provider,
            field_names=_KEAP.hint_fields,
            tool_name=tool_name,
            env_var=_KEAP.env_var,
            display_name=_KEAP.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Nymeria",
    }


@tool
def copper_list_records(
    resource: str,
    filter_json: str = "",
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Copper CRM records for companies, people, leads, opportunities, projects, tasks, users, or customer sources."""
    base, auth = _copper_config("copper_list_records", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _COPPER_RESOURCES, "Copper")
    if plural in {"users", "customer_sources"}:
        data = _request_json("GET", f"{base}/{plural}", params={"page_size": _limit(limit)}, headers=auth)
    else:
        body = _json_object(filter_json, field_name="filter_json")
        body.setdefault("page_size", _limit(limit))
        data = _request_json("POST", f"{base}/{plural}/search", json_body=body, headers=auth)
    return _dump_json(data)


@tool
def copper_get_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one Copper CRM record by ID."""
    base, auth = _copper_config("copper_get_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _COPPER_RESOURCES, "Copper")
    return _dump_json(_request_json("GET", f"{base}/{plural}/{quote(record_id, safe='')}", headers=auth))


@tool
def copper_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Copper CRM record from a JSON object."""
    base, auth = _copper_config("copper_create_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _COPPER_RESOURCES, "Copper")
    return _dump_json(_request_json("POST", f"{base}/{plural}", json_body=_json_object(fields_json, field_name="fields_json"), headers=auth))


@tool
def copper_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Copper CRM record from a JSON object."""
    base, auth = _copper_config("copper_update_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _COPPER_RESOURCES, "Copper")
    return _dump_json(
        _request_json("PUT", f"{base}/{plural}/{quote(record_id, safe='')}", json_body=_json_object(fields_json, field_name="fields_json"), headers=auth)
    )


@tool
def copper_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Copper CRM record by ID."""
    base, auth = _copper_config("copper_delete_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _COPPER_RESOURCES, "Copper")
    return _dump_json(_request_json("DELETE", f"{base}/{plural}/{quote(record_id, safe='')}", headers=auth))


@tool
def agilecrm_list_records(
    resource: str,
    filter_json: str = "",
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Agile CRM contacts, companies, or deals."""
    base, auth = _agile_config("agilecrm_list_records", config)
    if isinstance(auth, str):
        return auth
    normalized = _resource(resource, _AGILE_RESOURCES, "Agile CRM")
    if normalized == "deals":
        data = _request_json("GET", f"{base}/api/opportunity", params={"page_size": _limit(limit)}, headers=auth)
    else:
        filters = _json_object(filter_json, field_name="filter_json")
        filters.setdefault("contact_type", "COMPANY" if normalized == "companies" else "PERSON")
        body = {"filterJson": json.dumps(filters), "page_size": _limit(limit)}
        data = _request_json("POST", f"{base}/api/filters/filter/dynamic-filter", data=body, headers=auth)
    return _dump_json(data)


@tool
def agilecrm_get_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one Agile CRM contact, company, or deal by ID."""
    base, auth = _agile_config("agilecrm_get_record", config)
    if isinstance(auth, str):
        return auth
    normalized = _resource(resource, _AGILE_RESOURCES, "Agile CRM")
    endpoint = "api/opportunity" if normalized == "deals" else "api/contacts"
    return _dump_json(_request_json("GET", f"{base}/{endpoint}/{quote(record_id, safe='')}", headers=auth))


@tool
def agilecrm_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Agile CRM contact, company, or deal from a JSON object."""
    base, auth = _agile_config("agilecrm_create_record", config)
    if isinstance(auth, str):
        return auth
    normalized = _resource(resource, _AGILE_RESOURCES, "Agile CRM")
    body = _json_object(fields_json, field_name="fields_json")
    if normalized == "deals":
        endpoint = "api/opportunity"
    else:
        endpoint = "api/contacts"
        body.setdefault("type", "COMPANY" if normalized == "companies" else "PERSON")
    return _dump_json(_request_json("POST", f"{base}/{endpoint}", json_body=body, headers=auth))


@tool
def agilecrm_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an Agile CRM contact, company, or deal from a JSON object."""
    base, auth = _agile_config("agilecrm_update_record", config)
    if isinstance(auth, str):
        return auth
    normalized = _resource(resource, _AGILE_RESOURCES, "Agile CRM")
    body = _json_object(fields_json, field_name="fields_json")
    if normalized == "deals":
        body.setdefault("id", record_id)
        endpoint = "api/opportunity/partial-update"
    else:
        body.setdefault("id", record_id)
        endpoint = "api/contacts/edit-properties"
    return _dump_json(_request_json("PUT", f"{base}/{endpoint}", json_body=body, headers=auth))


@tool
def agilecrm_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete an Agile CRM contact, company, or deal by ID."""
    base, auth = _agile_config("agilecrm_delete_record", config)
    if isinstance(auth, str):
        return auth
    normalized = _resource(resource, _AGILE_RESOURCES, "Agile CRM")
    endpoint = "api/opportunity" if normalized == "deals" else "api/contacts"
    return _dump_json(_request_json("DELETE", f"{base}/{endpoint}/{quote(record_id, safe='')}", headers=auth))


@tool
def monica_list_records(
    resource: str,
    limit: int = 50,
    page: int = 1,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Monica CRM records such as contacts, activities, calls, notes, reminders, tags, or tasks."""
    base, auth = _monica_config("monica_list_records", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _MONICA_RESOURCES, "Monica CRM")
    params = {"limit": _limit(limit, max_value=100), "page": max(1, int(page))}
    return _dump_json(_request_json("GET", f"{base}/{plural}", params=params, headers=auth))


@tool
def monica_get_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one Monica CRM record by ID."""
    base, auth = _monica_config("monica_get_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _MONICA_RESOURCES, "Monica CRM")
    return _dump_json(_request_json("GET", f"{base}/{plural}/{quote(record_id, safe='')}", headers=auth))


@tool
def monica_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Monica CRM record from a JSON object."""
    base, auth = _monica_config("monica_create_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _MONICA_RESOURCES, "Monica CRM")
    return _dump_json(_request_json("POST", f"{base}/{plural}", json_body=_json_object(fields_json, field_name="fields_json"), headers=auth))


@tool
def monica_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Monica CRM record from a JSON object."""
    base, auth = _monica_config("monica_update_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _MONICA_RESOURCES, "Monica CRM")
    return _dump_json(
        _request_json("PUT", f"{base}/{plural}/{quote(record_id, safe='')}", json_body=_json_object(fields_json, field_name="fields_json"), headers=auth)
    )


@tool
def monica_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Monica CRM record by ID."""
    base, auth = _monica_config("monica_delete_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _MONICA_RESOURCES, "Monica CRM")
    return _dump_json(_request_json("DELETE", f"{base}/{plural}/{quote(record_id, safe='')}", headers=auth))


@tool
def affinity_list_records(
    resource: str,
    term: str = "",
    limit: int = 50,
    page_token: str = "",
    with_interaction_dates: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Affinity lists, people, or organizations."""
    base, auth = _affinity_config("affinity_list_records", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _AFFINITY_RESOURCES, "Affinity")
    params: dict[str, Any] = {}
    if plural in {"persons", "organizations"}:
        params["page_size"] = _limit(limit, max_value=500)
        if term:
            params["term"] = term
        if page_token:
            params["page_token"] = page_token
        if with_interaction_dates:
            params["with_interaction_dates"] = True
    return _dump_json(_request_json("GET", f"{base}/{plural}", params=params, headers=auth))


@tool
def affinity_get_record(
    resource: str,
    record_id: str,
    with_interaction_dates: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one Affinity list, person, or organization by ID."""
    base, auth = _affinity_config("affinity_get_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _AFFINITY_RESOURCES, "Affinity")
    params = {"with_interaction_dates": True} if with_interaction_dates and plural in {"persons", "organizations"} else {}
    return _dump_json(_request_json("GET", f"{base}/{plural}/{quote(record_id, safe='')}", params=params, headers=auth))


@tool
def affinity_create_person(
    first_name: str,
    last_name: str,
    emails_csv: str,
    organization_ids_csv: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Affinity person with one or more email addresses."""
    base, auth = _affinity_config("affinity_create_person", config)
    if isinstance(auth, str):
        return auth
    emails = _csv_to_list(emails_csv)
    if not emails:
        raise ValueError("emails_csv must contain at least one email address")
    body = _json_object(fields_json, field_name="fields_json")
    body.update({"first_name": first_name, "last_name": last_name, "emails": emails})
    if organization_ids_csv:
        body["organization_ids"] = _csv_to_ints(organization_ids_csv, field_name="organization_ids_csv")
    return _dump_json(_request_json("POST", f"{base}/persons", json_body=body, headers=auth))


@tool
def affinity_create_organization(
    name: str,
    domain: str = "",
    person_ids_csv: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Affinity organization."""
    base, auth = _affinity_config("affinity_create_organization", config)
    if isinstance(auth, str):
        return auth
    body = _json_object(fields_json, field_name="fields_json")
    body["name"] = name
    if domain:
        body["domain"] = domain
    if person_ids_csv:
        body["person_ids"] = _csv_to_ints(person_ids_csv, field_name="person_ids_csv")
    return _dump_json(_request_json("POST", f"{base}/organizations", json_body=body, headers=auth))


@tool
def affinity_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an Affinity person or organization from a JSON object."""
    base, auth = _affinity_config("affinity_update_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _AFFINITY_ENTITY_RESOURCES, "Affinity entity")
    return _dump_json(
        _request_json("PUT", f"{base}/{plural}/{quote(record_id, safe='')}", json_body=_json_object(fields_json, field_name="fields_json"), headers=auth)
    )


@tool
def affinity_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete an Affinity person or organization by ID."""
    base, auth = _affinity_config("affinity_delete_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _AFFINITY_ENTITY_RESOURCES, "Affinity entity")
    return _dump_json(_request_json("DELETE", f"{base}/{plural}/{quote(record_id, safe='')}", headers=auth))


@tool
def affinity_list_entries(
    list_id: str,
    limit: int = 50,
    page_token: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List entries in an Affinity list."""
    base, auth = _affinity_config("affinity_list_entries", config)
    if isinstance(auth, str):
        return auth
    params: dict[str, Any] = {"page_size": _limit(limit, max_value=500)}
    if page_token:
        params["page_token"] = page_token
    return _dump_json(_request_json("GET", f"{base}/lists/{quote(list_id, safe='')}/list-entries", params=params, headers=auth))


@tool
def affinity_create_list_entry(
    list_id: str,
    entity_id: int,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Affinity list entry for a person, organization, or opportunity entity."""
    base, auth = _affinity_config("affinity_create_list_entry", config)
    if isinstance(auth, str):
        return auth
    body = _json_object(fields_json, field_name="fields_json")
    body["entity_id"] = int(entity_id)
    return _dump_json(_request_json("POST", f"{base}/lists/{quote(list_id, safe='')}/list-entries", json_body=body, headers=auth))


@tool
def affinity_delete_list_entry(
    list_id: str,
    list_entry_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete an Affinity list entry by ID."""
    base, auth = _affinity_config("affinity_delete_list_entry", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("DELETE", f"{base}/lists/{quote(list_id, safe='')}/list-entries/{quote(list_entry_id, safe='')}", headers=auth))


@tool
def keap_list_records(
    resource: str,
    filters_json: str = "",
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Keap records such as companies, contacts, notes, orders, products, emails, files, tags, or users."""
    base, auth = _keap_config("keap_list_records", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _KEAP_RESOURCES, "Keap")
    params = _snake_case_data(_json_object(filters_json, field_name="filters_json"))
    params.setdefault("limit", _limit(limit))
    return _dump_json(_request_json("GET", f"{base}/{plural}", params=params, headers=auth))


@tool
def keap_get_record(
    resource: str,
    record_id: str,
    fields_csv: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one Keap record by ID."""
    base, auth = _keap_config("keap_get_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _KEAP_RESOURCES, "Keap")
    params = {"optional_properties": ",".join(_csv_to_list(fields_csv))} if fields_csv else {}
    return _dump_json(_request_json("GET", f"{base}/{plural}/{quote(record_id, safe='')}", params=params, headers=auth))


@tool
def keap_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Keap record from an API-shaped JSON object; contacts are upserted."""
    base, auth = _keap_config("keap_create_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _KEAP_RESOURCES, "Keap")
    body = _snake_case_data(_json_object(fields_json, field_name="fields_json"))
    method = "PUT" if plural == "contacts" else "POST"
    return _dump_json(_request_json(method, f"{base}/{plural}", json_body=body, headers=auth))


@tool
def keap_update_note(
    note_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Keap contact note from a JSON object."""
    base, auth = _keap_config("keap_update_note", config)
    if isinstance(auth, str):
        return auth
    body = _snake_case_data(_json_object(fields_json, field_name="fields_json"))
    return _dump_json(_request_json("PATCH", f"{base}/notes/{quote(note_id, safe='')}", json_body=body, headers=auth))


@tool
def keap_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Keap record by ID."""
    base, auth = _keap_config("keap_delete_record", config)
    if isinstance(auth, str):
        return auth
    plural = _resource(resource, _KEAP_RESOURCES, "Keap")
    return _dump_json(_request_json("DELETE", f"{base}/{plural}/{quote(record_id, safe='')}", headers=auth))


@tool
def keap_list_contact_tags(
    contact_id: str,
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List tags applied to a Keap contact."""
    base, auth = _keap_config("keap_list_contact_tags", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/contacts/{quote(contact_id, safe='')}/tags", params={"limit": _limit(limit)}, headers=auth))


@tool
def keap_apply_tags(
    contact_id: str,
    tag_ids_csv: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Apply one or more Keap tags to a contact."""
    base, auth = _keap_config("keap_apply_tags", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(
        _request_json(
            "POST",
            f"{base}/contacts/{quote(contact_id, safe='')}/tags",
            json_body={"tagIds": _csv_to_ints(tag_ids_csv, field_name="tag_ids_csv")},
            headers=auth,
        )
    )


@tool
def keap_remove_tags(
    contact_id: str,
    tag_ids_csv: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove one or more Keap tags from a contact."""
    base, auth = _keap_config("keap_remove_tags", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(
        _request_json(
            "DELETE",
            f"{base}/contacts/{quote(contact_id, safe='')}/tags",
            params={"ids": ",".join(str(item) for item in _csv_to_ints(tag_ids_csv, field_name="tag_ids_csv"))},
            headers=auth,
        )
    )


@tool
def keap_send_email(
    user_id: int,
    contact_ids_csv: str,
    subject: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Queue an email through Keap for one or more contacts."""
    base, auth = _keap_config("keap_send_email", config)
    if isinstance(auth, str):
        return auth
    body = _snake_case_data(_json_object(fields_json, field_name="fields_json"))
    body.update(
        {
            "user_id": int(user_id),
            "contacts": _csv_to_ints(contact_ids_csv, field_name="contact_ids_csv"),
            "subject": subject,
        }
    )
    return _dump_json(_request_json("POST", f"{base}/emails/queue", json_body=body, headers=auth))


RELATIONSHIP_CRM_SERVICE_TOOLS = [
    copper_list_records,
    copper_get_record,
    copper_create_record,
    copper_update_record,
    copper_delete_record,
    agilecrm_list_records,
    agilecrm_get_record,
    agilecrm_create_record,
    agilecrm_update_record,
    agilecrm_delete_record,
    monica_list_records,
    monica_get_record,
    monica_create_record,
    monica_update_record,
    monica_delete_record,
    affinity_list_records,
    affinity_get_record,
    affinity_create_person,
    affinity_create_organization,
    affinity_update_record,
    affinity_delete_record,
    affinity_list_entries,
    affinity_create_list_entry,
    affinity_delete_list_entry,
    keap_list_records,
    keap_get_record,
    keap_create_record,
    keap_update_note,
    keap_delete_record,
    keap_list_contact_tags,
    keap_apply_tags,
    keap_remove_tags,
    keap_send_email,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="relationship_crm", tools=tuple(RELATIONSHIP_CRM_SERVICE_TOOLS)))
