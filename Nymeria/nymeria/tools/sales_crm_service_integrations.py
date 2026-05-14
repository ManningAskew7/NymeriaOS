"""Sales CRM service integration tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_PIPEDRIVE_V2_BASE_URL = "https://api.pipedrive.com/api/v2"
_PIPEDRIVE_V1_BASE_URL = "https://api.pipedrive.com/v1"

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


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _filtered_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _limit(value: int, *, default: int = 50, max_value: int = 500) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


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
    base = (
        _credential_value(
            provider="pipedrive",
            provider_aliases=("pipedrive_api", "pipedrive_oauth2"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pipedrive_base_url")
        or _PIPEDRIVE_V2_BASE_URL
    )
    api_token = _credential_value(
        provider="pipedrive",
        provider_aliases=("pipedrive_api", "pipedrive_oauth2"),
        field_names=("api_token", "apiToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("pipedrive_api_token")
    access_token = _credential_value(
        provider="pipedrive",
        provider_aliases=("pipedrive_api", "pipedrive_oauth2"),
        field_names=("access_token", "bearer_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("pipedrive_access_token")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    auth_params: dict[str, str] = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    elif api_token:
        auth_params["api_token"] = api_token
    else:
        return _base_url(base), _PIPEDRIVE_V1_BASE_URL, headers, _setup_hint(
            provider="pipedrive",
            field_names=("api_token", "access_token", "token", "value"),
            tool_name=tool_name,
            env_var="PIPEDRIVE_API_TOKEN or PIPEDRIVE_ACCESS_TOKEN",
            display_name="Pipedrive",
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
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(method, url, params=request_params, json=json_body, headers=headers)
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
    pipedrive_list_records,
    pipedrive_search_records,
    pipedrive_get_record,
    pipedrive_create_record,
    pipedrive_update_record,
    pipedrive_delete_record,
    pipedrive_list_users,
]
