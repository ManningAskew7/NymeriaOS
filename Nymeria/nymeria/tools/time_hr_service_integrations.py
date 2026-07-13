"""Time tracking and HR service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import json
import logging
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
    API_KEY_FIELDS,
    BASE_URL_ALIAS_FIELDS,
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_BAMBOOHR_GATEWAY_BASE_URL = "https://api.bamboohr.com/api/gateway.php"
_BEEMINDER_BASE_URL = "https://www.beeminder.com/api/v1"
_CLOCKIFY_BASE_URL = "https://api.clockify.me/api/v1"
_HARVEST_BASE_URL = "https://api.harvestapp.com/v2"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_BAMBOOHR = register_provider_spec(
    ProviderCredentialSpec(
        provider="bamboohr",
        aliases=("bamboo_hr", "bamboohr_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=(
                    "base_url",
                    "baseUrl",
                    "api_url",
                    "apiUrl",
                    "gateway_url",
                    "gatewayUrl",
                ),
                required=False,
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "token", "value"),
            ),
            CredentialFieldGroup(
                role="subdomain", names=("subdomain", "company_domain", "companyDomain")
            ),
        ),
        hint_fields=("api_key", "subdomain"),
        env_var="BAMBOOHR_API_KEY and BAMBOOHR_SUBDOMAIN",
        display_name="BambooHR",
    )
)

_BEEMINDER = register_provider_spec(
    ProviderCredentialSpec(
        provider="beeminder",
        aliases=("beeminder_api", "beeminder_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=(
                    "auth_token",
                    "authToken",
                    "access_token",
                    "accessToken",
                    "api_key",
                    "apiKey",
                    "token",
                    "value",
                ),
            ),
        ),
        hint_fields=("auth_token", "access_token", "api_key", "value"),
        env_var="BEEMINDER_ACCESS_TOKEN",
        display_name="Beeminder",
    )
)

_CLOCKIFY = register_provider_spec(
    ProviderCredentialSpec(
        provider="clockify",
        aliases=("clockify_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "x_api_key", "xApiKey", "token", "value"),
            ),
        ),
        hint_fields=API_KEY_FIELDS,
        env_var="CLOCKIFY_API_KEY",
        display_name="Clockify",
    )
)

_HARVEST = register_provider_spec(
    ProviderCredentialSpec(
        provider="harvest",
        aliases=("harvest_api", "harvest_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="account_id",
                names=(
                    "account_id",
                    "accountId",
                    "harvest_account_id",
                    "harvestAccountId",
                ),
            ),
        ),
        hint_fields=("access_token", "account_id"),
        env_var="HARVEST_ACCESS_TOKEN and HARVEST_ACCOUNT_ID",
        display_name="Harvest",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _csv_to_list(value: str, *, max_items: int | None = None) -> list[str]:
    parts = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    return parts[:max_items] if max_items is not None else parts


def _csv(value: str) -> str:
    return ",".join(_csv_to_list(value))


def _json_object(value: str, *, label: str = "fields_json") -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
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
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                payload: dict[str, Any] = {
                    "status": "ok",
                    "status_code": response.status_code,
                }
                location = response.headers.get("Location")
                if location:
                    payload["location"] = location
                return payload
            try:
                return response.json()
            except ValueError:
                return {
                    "status": "ok",
                    "status_code": response.status_code,
                    "text": response.text,
                }
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            if isinstance(body, dict):
                errors = body.get("errors")
                if isinstance(errors, list) and errors:
                    detail = "; ".join(str(item.get("message", item)) for item in errors[:3] if isinstance(item, dict))
                detail = (
                    detail
                    or str(body.get("message") or body.get("error") or body.get("error_description") or "")
                )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _auth_header_basic(username: str, password: str = "x") -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _bamboohr_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_BAMBOOHR.provider,
            provider_aliases=_BAMBOOHR.aliases,
            field_names=_BAMBOOHR.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("bamboohr_base_url")
        or _BAMBOOHR_GATEWAY_BASE_URL
    )
    subdomain = _credential_value(
        provider=_BAMBOOHR.provider,
        provider_aliases=_BAMBOOHR.aliases,
        field_names=_BAMBOOHR.group("subdomain"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("bamboohr_subdomain")
    api_key = _credential_value(
        provider=_BAMBOOHR.provider,
        provider_aliases=_BAMBOOHR.aliases,
        field_names=_BAMBOOHR.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("bamboohr_api_key")
    if not subdomain or not api_key:
        return _base_url(base), _setup_hint(
            provider=_BAMBOOHR.provider,
            field_names=_BAMBOOHR.hint_fields,
            tool_name=tool_name,
            env_var=_BAMBOOHR.env_var,
            display_name=_BAMBOOHR.display_name,
        )
    root = f"{_base_url(base)}/{quote(subdomain.strip(), safe='')}/v1"
    return root, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": _auth_header_basic(api_key),
        "User-Agent": "Nymeria",
    }


def _beeminder_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | str]:
    base = (
        _credential_value(
            provider=_BEEMINDER.provider,
            provider_aliases=_BEEMINDER.aliases,
            field_names=_BEEMINDER.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("beeminder_base_url")
        or _BEEMINDER_BASE_URL
    )
    token = _credential_value(
        provider=_BEEMINDER.provider,
        provider_aliases=_BEEMINDER.aliases,
        field_names=_BEEMINDER.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("beeminder_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_BEEMINDER.provider,
            field_names=_BEEMINDER.hint_fields,
            tool_name=tool_name,
            env_var=_BEEMINDER.env_var,
            display_name=_BEEMINDER.display_name,
        )
    return _base_url(base), token


def _clockify_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_CLOCKIFY.provider,
            provider_aliases=_CLOCKIFY.aliases,
            field_names=_CLOCKIFY.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("clockify_base_url")
        or _CLOCKIFY_BASE_URL
    )
    api_key = _credential_value(
        provider=_CLOCKIFY.provider,
        provider_aliases=_CLOCKIFY.aliases,
        field_names=_CLOCKIFY.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("clockify_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_CLOCKIFY.provider,
            field_names=_CLOCKIFY.hint_fields,
            tool_name=tool_name,
            env_var=_CLOCKIFY.env_var,
            display_name=_CLOCKIFY.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "User-Agent": "Nymeria",
    }


def _harvest_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_HARVEST.provider,
            provider_aliases=_HARVEST.aliases,
            field_names=_HARVEST.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("harvest_base_url")
        or _HARVEST_BASE_URL
    )
    token = _credential_value(
        provider=_HARVEST.provider,
        provider_aliases=_HARVEST.aliases,
        field_names=_HARVEST.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("harvest_access_token")
    account_id = _credential_value(
        provider=_HARVEST.provider,
        provider_aliases=_HARVEST.aliases,
        field_names=_HARVEST.group("account_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("harvest_account_id")
    if not token or not account_id:
        return _base_url(base), _setup_hint(
            provider=_HARVEST.provider,
            field_names=_HARVEST.hint_fields,
            tool_name=tool_name,
            env_var=_HARVEST.env_var,
            display_name=_HARVEST.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Harvest-Account-Id": account_id,
        "User-Agent": "Nymeria",
    }


def _require_headers(config_result: tuple[str, dict[str, str] | str]) -> tuple[str, dict[str, str]] | str:
    base, headers = config_result
    if isinstance(headers, str):
        return headers
    return base, headers


def _beeminder_request(
    tool_name: str,
    config: Optional[RunnableConfig],
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> str:
    base, token_or_hint = _beeminder_config(tool_name, config)
    if isinstance(token_or_hint, str) and token_or_hint.startswith("[Error]:"):
        return token_or_hint
    params = {**(params or {}), "auth_token": token_or_hint}
    return _dump_json(
        _request_json(
            method,
            f"{base}/{path.lstrip('/')}",
            params=params,
            json_body=_filtered(json_body),
            headers={"Accept": "application/json", "User-Agent": "Nymeria"},
        )
    )


@tool
def bamboohr_list_employees(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List employees from the BambooHR company directory."""
    resolved = _require_headers(_bamboohr_config("bamboohr_list_employees", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/employees/directory", headers=headers))


@tool
def bamboohr_get_employee(
    employee_id: str,
    fields: str = "displayName,firstName,lastName,jobTitle,workEmail,department,location,supervisor",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a BambooHR employee by ID with selected field names."""
    resolved = _require_headers(_bamboohr_config("bamboohr_get_employee", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/employees/{quote(employee_id, safe='')}",
            params={"fields": _csv(fields)},
            headers=headers,
        )
    )


@tool
def bamboohr_create_employee(
    first_name: str,
    last_name: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a BambooHR employee with optional extra fields as a JSON object."""
    resolved = _require_headers(_bamboohr_config("bamboohr_create_employee", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    body = {"firstName": first_name, "lastName": last_name, **_json_object(fields_json)}
    return _dump_json(_request_json("POST", f"{base}/employees", json_body=body, headers=headers))


@tool
def bamboohr_update_employee(
    employee_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update BambooHR employee fields from a JSON object."""
    body = _json_object(fields_json)
    if not body:
        return "[Error]: fields_json must include at least one field to update."
    resolved = _require_headers(_bamboohr_config("bamboohr_update_employee", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "POST",
            f"{base}/employees/{quote(employee_id, safe='')}",
            json_body=body,
            headers=headers,
        )
    )


@tool
def bamboohr_get_company_report(
    report_id: str,
    only_current: bool = True,
    format: str = "json",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Run a BambooHR company report."""
    resolved = _require_headers(_bamboohr_config("bamboohr_get_company_report", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    report_format = format.lower().strip() or "json"
    if report_format not in {"json", "csv", "pdf", "xlsx", "xml"}:
        return "[Error]: format must be one of json, csv, pdf, xlsx, or xml."
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/reports/{quote(report_id, safe='')}/",
            params={"format": report_format, "onlyCurrent": str(bool(only_current)).lower()},
            headers=headers,
        )
    )


@tool
def beeminder_get_user(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get the authenticated Beeminder user."""
    return _beeminder_request("beeminder_get_user", config, "GET", "/users/me.json")


@tool
def beeminder_list_goals(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Beeminder goals for the authenticated user."""
    return _beeminder_request("beeminder_list_goals", config, "GET", "/users/me/goals.json")


@tool
def beeminder_get_goal(
    goal_slug: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Beeminder goal by slug."""
    return _beeminder_request(
        "beeminder_get_goal",
        config,
        "GET",
        f"/users/me/goals/{quote(goal_slug, safe='')}.json",
    )


@tool
def beeminder_list_datapoints(
    goal_slug: str,
    page: int = 1,
    per_page: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List datapoints for a Beeminder goal."""
    return _beeminder_request(
        "beeminder_list_datapoints",
        config,
        "GET",
        f"/users/me/goals/{quote(goal_slug, safe='')}/datapoints.json",
        params={"page": max(1, int(page)), "per": _limit(per_page, default=50, max_value=100)},
    )


@tool
def beeminder_create_datapoint(
    goal_slug: str,
    value: float,
    comment: str = "",
    timestamp: Optional[int] = None,
    request_id: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Beeminder datapoint."""
    body = _filtered(
        {
            "value": value,
            "comment": comment,
            "timestamp": timestamp,
            "requestid": request_id,
        }
    )
    return _beeminder_request(
        "beeminder_create_datapoint",
        config,
        "POST",
        f"/users/me/goals/{quote(goal_slug, safe='')}/datapoints.json",
        json_body=body,
    )


@tool
def beeminder_update_datapoint(
    goal_slug: str,
    datapoint_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Beeminder datapoint from a JSON object."""
    body = _json_object(fields_json)
    if not body:
        return "[Error]: fields_json must include at least one field to update."
    return _beeminder_request(
        "beeminder_update_datapoint",
        config,
        "PUT",
        f"/users/me/goals/{quote(goal_slug, safe='')}/datapoints/{quote(datapoint_id, safe='')}.json",
        json_body=body,
    )


@tool
def beeminder_delete_datapoint(
    goal_slug: str,
    datapoint_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Beeminder datapoint."""
    return _beeminder_request(
        "beeminder_delete_datapoint",
        config,
        "DELETE",
        f"/users/me/goals/{quote(goal_slug, safe='')}/datapoints/{quote(datapoint_id, safe='')}.json",
    )


@tool
def clockify_list_workspaces(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Clockify workspaces."""
    resolved = _require_headers(_clockify_config("clockify_list_workspaces", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/workspaces", headers=headers))


@tool
def clockify_list_users(
    workspace_id: str,
    status: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Clockify users in a workspace."""
    resolved = _require_headers(_clockify_config("clockify_list_users", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/users",
            params={"status": status.upper() or None, "page-size": _limit(limit)},
            headers=headers,
        )
    )


@tool
def clockify_list_projects(
    workspace_id: str,
    name: str = "",
    archived: Optional[bool] = None,
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Clockify projects in a workspace."""
    resolved = _require_headers(_clockify_config("clockify_list_projects", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/projects",
            params={"name": name, "archived": archived, "page-size": _limit(limit)},
            headers=headers,
        )
    )


@tool
def clockify_create_project(
    workspace_id: str,
    name: str,
    client_id: str = "",
    is_public: bool = False,
    billable: bool = True,
    color: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Clockify project."""
    resolved = _require_headers(_clockify_config("clockify_create_project", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    body = _filtered(
        {
            "name": name,
            "clientId": client_id,
            "isPublic": is_public,
            "billable": billable,
            "color": color,
        }
    )
    return _dump_json(
        _request_json(
            "POST",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/projects",
            json_body=body,
            headers=headers,
        )
    )


@tool
def clockify_list_time_entries(
    workspace_id: str,
    user_id: str,
    start: str = "",
    end: str = "",
    project_id: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Clockify time entries for a workspace user."""
    resolved = _require_headers(_clockify_config("clockify_list_time_entries", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/user/{quote(user_id, safe='')}/time-entries",
            params={
                "start": start,
                "end": end,
                "project": project_id,
                "page-size": _limit(limit),
            },
            headers=headers,
        )
    )


@tool
def clockify_create_time_entry(
    workspace_id: str,
    start: str,
    end: str = "",
    project_id: str = "",
    description: str = "",
    task_id: str = "",
    tag_ids: str = "",
    billable: Optional[bool] = None,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Clockify time entry using ISO 8601 start/end timestamps."""
    resolved = _require_headers(_clockify_config("clockify_create_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    body = _filtered(
        {
            "start": start,
            "end": end,
            "projectId": project_id,
            "description": description,
            "taskId": task_id,
            "tagIds": _csv_to_list(tag_ids),
            "billable": billable,
        }
    )
    return _dump_json(
        _request_json(
            "POST",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/time-entries",
            json_body=body,
            headers=headers,
        )
    )


@tool
def clockify_update_time_entry(
    workspace_id: str,
    time_entry_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Clockify time entry from a JSON object."""
    body = _json_object(fields_json)
    if not body:
        return "[Error]: fields_json must include at least one field to update."
    resolved = _require_headers(_clockify_config("clockify_update_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "PUT",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/time-entries/{quote(time_entry_id, safe='')}",
            json_body=body,
            headers=headers,
        )
    )


@tool
def clockify_delete_time_entry(
    workspace_id: str,
    time_entry_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Clockify time entry."""
    resolved = _require_headers(_clockify_config("clockify_delete_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "DELETE",
            f"{base}/workspaces/{quote(workspace_id, safe='')}/time-entries/{quote(time_entry_id, safe='')}",
            headers=headers,
        )
    )


@tool
def harvest_get_me(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get the authenticated Harvest user."""
    resolved = _require_headers(_harvest_config("harvest_get_me", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/users/me", headers=headers))


@tool
def harvest_get_company(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get the Harvest account company profile."""
    resolved = _require_headers(_harvest_config("harvest_get_company", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/company", headers=headers))


@tool
def harvest_list_clients(
    active: Optional[bool] = None,
    updated_since: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Harvest clients."""
    resolved = _require_headers(_harvest_config("harvest_list_clients", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/clients",
            params={"is_active": active, "updated_since": updated_since, "per_page": _limit(limit)},
            headers=headers,
        )
    )


@tool
def harvest_list_projects(
    client_id: str = "",
    active: Optional[bool] = None,
    updated_since: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Harvest projects."""
    resolved = _require_headers(_harvest_config("harvest_list_projects", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/projects",
            params={
                "client_id": client_id,
                "is_active": active,
                "updated_since": updated_since,
                "per_page": _limit(limit),
            },
            headers=headers,
        )
    )


@tool
def harvest_list_tasks(
    active: Optional[bool] = None,
    updated_since: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Harvest tasks."""
    resolved = _require_headers(_harvest_config("harvest_list_tasks", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/tasks",
            params={"is_active": active, "updated_since": updated_since, "per_page": _limit(limit)},
            headers=headers,
        )
    )


@tool
def harvest_list_time_entries(
    user_id: str = "",
    project_id: str = "",
    client_id: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Harvest time entries."""
    resolved = _require_headers(_harvest_config("harvest_list_time_entries", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/time_entries",
            params={
                "user_id": user_id,
                "project_id": project_id,
                "client_id": client_id,
                "from": from_date,
                "to": to_date,
                "per_page": _limit(limit),
            },
            headers=headers,
        )
    )


@tool
def harvest_create_time_entry(
    project_id: str,
    task_id: str,
    spent_date: str,
    hours: float,
    notes: str = "",
    external_reference_id: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Harvest time entry by duration."""
    resolved = _require_headers(_harvest_config("harvest_create_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    body = _filtered(
        {
            "project_id": project_id,
            "task_id": task_id,
            "spent_date": spent_date,
            "hours": hours,
            "notes": notes,
            "external_reference": {"id": external_reference_id} if external_reference_id else None,
        }
    )
    return _dump_json(_request_json("POST", f"{base}/time_entries", json_body=body, headers=headers))


@tool
def harvest_update_time_entry(
    time_entry_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Harvest time entry from a JSON object."""
    body = _json_object(fields_json)
    if not body:
        return "[Error]: fields_json must include at least one field to update."
    resolved = _require_headers(_harvest_config("harvest_update_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "PATCH",
            f"{base}/time_entries/{quote(time_entry_id, safe='')}",
            json_body=body,
            headers=headers,
        )
    )


@tool
def harvest_stop_time_entry(
    time_entry_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Stop a running Harvest time entry."""
    resolved = _require_headers(_harvest_config("harvest_stop_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json("PATCH", f"{base}/time_entries/{quote(time_entry_id, safe='')}/stop", headers=headers)
    )


@tool
def harvest_delete_time_entry(
    time_entry_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Harvest time entry."""
    resolved = _require_headers(_harvest_config("harvest_delete_time_entry", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json("DELETE", f"{base}/time_entries/{quote(time_entry_id, safe='')}", headers=headers)
    )


TIME_HR_SERVICE_TOOLS = [
    bamboohr_list_employees,
    bamboohr_get_employee,
    bamboohr_create_employee,
    bamboohr_update_employee,
    bamboohr_get_company_report,
    beeminder_get_user,
    beeminder_list_goals,
    beeminder_get_goal,
    beeminder_list_datapoints,
    beeminder_create_datapoint,
    beeminder_update_datapoint,
    beeminder_delete_datapoint,
    clockify_list_workspaces,
    clockify_list_users,
    clockify_list_projects,
    clockify_create_project,
    clockify_list_time_entries,
    clockify_create_time_entry,
    clockify_update_time_entry,
    clockify_delete_time_entry,
    harvest_get_me,
    harvest_get_company,
    harvest_list_clients,
    harvest_list_projects,
    harvest_list_tasks,
    harvest_list_time_entries,
    harvest_create_time_entry,
    harvest_update_time_entry,
    harvest_stop_time_entry,
    harvest_delete_time_entry,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="time_hr", tools=tuple(TIME_HR_SERVICE_TOOLS)))
