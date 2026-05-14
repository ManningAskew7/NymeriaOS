"""Relationship CRM service integration tools."""

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
_COPPER_BASE_URL = "https://api.copper.com/developer_api/v1"
_AGILE_PLACEHOLDER_BASE_URL = "https://example.agilecrm.com/dev"
_MONICA_BASE_URL = "https://app.monicahq.com/api"

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


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _filtered(values: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (values or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _limit(value: int, *, default: int = 50, max_value: int = 200) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


def _json_object(value: str, *, field_name: str) -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
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
            provider="copper",
            provider_aliases=("copper_api",),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("copper_base_url")
        or _COPPER_BASE_URL
    )
    api_key = _credential_value(
        provider="copper",
        provider_aliases=("copper_api",),
        field_names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("copper_api_key")
    email = _credential_value(
        provider="copper",
        provider_aliases=("copper_api",),
        field_names=("email", "user_email", "userEmail"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("copper_email")
    if not api_key or not email:
        return _base_url(base), _setup_hint(
            provider="copper",
            field_names=("api_key", "email"),
            tool_name=tool_name,
            env_var="COPPER_API_KEY and COPPER_EMAIL",
            display_name="Copper",
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
        provider="agilecrm",
        provider_aliases=("agile_crm", "agilecrm_api", "agile_crm_api"),
        field_names=("subdomain", "domain"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("agilecrm_subdomain")
    base = (
        _credential_value(
            provider="agilecrm",
            provider_aliases=("agile_crm", "agilecrm_api", "agile_crm_api"),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("agilecrm_base_url")
        or (f"https://{subdomain}.agilecrm.com/dev" if subdomain else _AGILE_PLACEHOLDER_BASE_URL)
    )
    email = _credential_value(
        provider="agilecrm",
        provider_aliases=("agile_crm", "agilecrm_api", "agile_crm_api"),
        field_names=("email", "username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("agilecrm_email")
    api_key = _credential_value(
        provider="agilecrm",
        provider_aliases=("agile_crm", "agilecrm_api", "agile_crm_api"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("agilecrm_api_key")
    if not email or not api_key or base == _AGILE_PLACEHOLDER_BASE_URL:
        return _base_url(base), _setup_hint(
            provider="agilecrm",
            field_names=("email", "api_key", "subdomain"),
            tool_name=tool_name,
            env_var="AGILECRM_EMAIL, AGILECRM_API_KEY, and AGILECRM_SUBDOMAIN",
            display_name="Agile CRM",
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
            provider="monica",
            provider_aliases=("monica_crm", "monicacrm", "monica_crm_api"),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "domain", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("monica_base_url")
        or _MONICA_BASE_URL
    )
    token = _credential_value(
        provider="monica",
        provider_aliases=("monica_crm", "monicacrm", "monica_crm_api"),
        field_names=("api_token", "apiToken", "access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("monica_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="monica",
            field_names=("api_token", "access_token", "value"),
            tool_name=tool_name,
            env_var="MONICA_ACCESS_TOKEN",
            display_name="Monica CRM",
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
]
