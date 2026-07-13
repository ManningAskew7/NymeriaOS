"""Project management service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import json
import logging
from datetime import datetime, timezone
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
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered_params,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"
_MONDAY_API_URL = "https://api.monday.com/v2"
_TAIGA_BASE_URL = "https://api.taiga.io/api/v1"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_JIRA = register_provider_spec(
    ProviderCredentialSpec(
        provider="jira",
        aliases=("atlassian", "jira_api", "jira_software_cloud_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "domain", "url", "site_url")
            ),
            CredentialFieldGroup(
                role="access_token",
                names=("access_token", "bearer_token", "token", "value"),
                required=False,
            ),
            CredentialFieldGroup(role="email", names=("email", "username", "user")),
            CredentialFieldGroup(
                role="api_token", names=("api_token", "apiToken", "password")
            ),
        ),
        hint_fields=("email", "api_token", "base_url"),
        env_var="JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_BASE_URL",
        display_name="Jira",
    )
)

_CLICKUP = register_provider_spec(
    ProviderCredentialSpec(
        provider="clickup",
        aliases=("click_up", "clickup_api", "click_up_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="token", names=("access_token", "api_key", "token", "value")
            ),
        ),
        hint_fields=("access_token", "api_key", "value"),
        env_var="CLICKUP_ACCESS_TOKEN",
        display_name="ClickUp",
    )
)

_MONDAY = register_provider_spec(
    ProviderCredentialSpec(
        provider="monday",
        aliases=("monday_com", "mondaycom", "monday_api", "mondaycom_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("api_url", "graphql_url", "base_url", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token",
                names=("api_token", "apiToken", "access_token", "token", "value"),
            ),
        ),
        hint_fields=("api_token", "apiToken", "access_token", "value"),
        env_var="MONDAY_API_TOKEN",
        display_name="Monday",
    )
)

_TAIGA = register_provider_spec(
    ProviderCredentialSpec(
        provider="taiga",
        aliases=("taiga_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("api_url", "base_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("auth_token", "access_token", "bearer_token", "token", "value"),
            ),
            CredentialFieldGroup(role="username", names=("username", "email", "user")),
            CredentialFieldGroup(role="password", names=("password",)),
        ),
        hint_fields=("auth_token", "username", "password", "value"),
        env_var="TAIGA_AUTH_TOKEN or TAIGA_USERNAME and TAIGA_PASSWORD",
        display_name="Taiga",
    )
)

_WEKAN = register_provider_spec(
    ProviderCredentialSpec(
        provider="wekan",
        aliases=("wekan_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=(
                    "session_token",
                    "token",
                    "access_token",
                    "bearer_token",
                    "value",
                ),
            ),
            CredentialFieldGroup(role="username", names=("username", "email", "user")),
            CredentialFieldGroup(role="password", names=("password",)),
        ),
        hint_fields=("token", "session_token", "username", "password", "value"),
        env_var="WEKAN_TOKEN or WEKAN_USERNAME and WEKAN_PASSWORD",
        display_name="Wekan",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_int_csv(value: str) -> list[int]:
    result: list[int] = []
    for part in _split_csv(value):
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


def _limit(value: int, *, default: int = 20, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _bearer_header_value(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _json_object_arg(value: str, field_name: str) -> dict[str, Any]:
    value = value.strip()
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{field_name} must be a valid JSON object") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered_params(params),
                json=json_body,
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
            errors = body.get("errors")
            error_messages = body.get("errorMessages")
            if isinstance(error_messages, list) and error_messages:
                detail = "; ".join(str(item) for item in error_messages)
            elif isinstance(errors, list) and errors:
                first = errors[0]
                detail = first.get("message") if isinstance(first, dict) else str(first)
            elif isinstance(errors, dict):
                detail = "; ".join(f"{key}: {value}" for key, value in errors.items())
            detail = (
                detail
                or body.get("message")
                or body.get("err")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _jira_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_JIRA.provider,
            provider_aliases=_JIRA.aliases,
            field_names=_JIRA.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("jira_base_url")
    )
    access_token = _credential_value(
        provider=_JIRA.provider,
        provider_aliases=_JIRA.aliases,
        field_names=_JIRA.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jira_access_token")
    email = _credential_value(
        provider=_JIRA.provider,
        provider_aliases=_JIRA.aliases,
        field_names=_JIRA.group("email"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jira_email")
    api_token = _credential_value(
        provider=_JIRA.provider,
        provider_aliases=_JIRA.aliases,
        field_names=_JIRA.group("api_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jira_api_token")

    if not base:
        return "", (
            "[Error]: No Jira base URL found. Save a Jira credential with field "
            '"base_url" or "domain", or set JIRA_BASE_URL.'
        )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        return _base_url(base), headers
    if email and api_token:
        raw = f"{email}:{api_token}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
        return _base_url(base), headers
    return _base_url(base), _setup_hint(
        provider=_JIRA.provider,
        field_names=_JIRA.hint_fields,
        tool_name=tool_name,
        env_var=_JIRA.env_var,
        display_name=_JIRA.display_name,
    )


def _clickup_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_CLICKUP.provider,
            provider_aliases=_CLICKUP.aliases,
            field_names=_CLICKUP.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("clickup_base_url")
        or _CLICKUP_BASE_URL
    )
    token = _credential_value(
        provider=_CLICKUP.provider,
        provider_aliases=_CLICKUP.aliases,
        field_names=_CLICKUP.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("clickup_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_CLICKUP.provider,
            field_names=_CLICKUP.hint_fields,
            tool_name=tool_name,
            env_var=_CLICKUP.env_var,
            display_name=_CLICKUP.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _monday_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    api_url = (
        _credential_value(
            provider=_MONDAY.provider,
            provider_aliases=_MONDAY.aliases,
            field_names=_MONDAY.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("monday_api_url")
        or _MONDAY_API_URL
    )
    token = _credential_value(
        provider=_MONDAY.provider,
        provider_aliases=_MONDAY.aliases,
        field_names=_MONDAY.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("monday_api_token")
    if not token:
        return _base_url(api_url), _setup_hint(
            provider=_MONDAY.provider,
            field_names=_MONDAY.hint_fields,
            tool_name=tool_name,
            env_var=_MONDAY.env_var,
            display_name=_MONDAY.display_name,
        )
    return _base_url(api_url), {
        "Accept": "application/json",
        "API-Version": "2023-10",
        "Authorization": _bearer_header_value(token),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _taiga_api_base(value: str) -> str:
    base = _base_url(value)
    return base if base.endswith("/api/v1") else f"{base}/api/v1"


def _taiga_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    raw_base = (
        _credential_value(
            provider=_TAIGA.provider,
            provider_aliases=_TAIGA.aliases,
            field_names=_TAIGA.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("taiga_base_url")
        or _TAIGA_BASE_URL
    )
    base = _taiga_api_base(raw_base)
    token = _credential_value(
        provider=_TAIGA.provider,
        provider_aliases=_TAIGA.aliases,
        field_names=_TAIGA.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("taiga_auth_token")
    username = _credential_value(
        provider=_TAIGA.provider,
        provider_aliases=_TAIGA.aliases,
        field_names=_TAIGA.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("taiga_username")
    password = _credential_value(
        provider=_TAIGA.provider,
        provider_aliases=_TAIGA.aliases,
        field_names=_TAIGA.group("password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("taiga_password")

    if not token and username and password:
        data = _request_json(
            "POST",
            f"{base}/auth",
            json_body={"type": "normal", "username": username, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"},
        )
        token = data.get("auth_token") if isinstance(data, dict) else None
    if not token:
        return base, _setup_hint(
            provider=_TAIGA.provider,
            field_names=_TAIGA.hint_fields,
            tool_name=tool_name,
            env_var=_TAIGA.env_var,
            display_name=_TAIGA.display_name,
        )
    return base, {
        "Accept": "application/json",
        "Authorization": _bearer_header_value(token),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _wekan_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    raw_base = (
        _credential_value(
            provider=_WEKAN.provider,
            provider_aliases=_WEKAN.aliases,
            field_names=_WEKAN.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("wekan_base_url")
    )
    if not raw_base:
        return "", (
            "[Error]: No Wekan base URL found. Save a Wekan credential with field "
            '"url" or "base_url", or set WEKAN_BASE_URL.'
        )
    base = _base_url(raw_base)
    token = _credential_value(
        provider=_WEKAN.provider,
        provider_aliases=_WEKAN.aliases,
        field_names=_WEKAN.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("wekan_token")
    username = _credential_value(
        provider=_WEKAN.provider,
        provider_aliases=_WEKAN.aliases,
        field_names=_WEKAN.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("wekan_username")
    password = _credential_value(
        provider=_WEKAN.provider,
        provider_aliases=_WEKAN.aliases,
        field_names=_WEKAN.group("password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("wekan_password")

    if not token and username and password:
        data = _request_json(
            "POST",
            f"{base}/users/login",
            json_body={"username": username, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"},
        )
        token = data.get("token") if isinstance(data, dict) else None
    if not token:
        return base, _setup_hint(
            provider=_WEKAN.provider,
            field_names=_WEKAN.hint_fields,
            tool_name=tool_name,
            env_var=_WEKAN.env_var,
            display_name=_WEKAN.display_name,
        )
    return base, {
        "Accept": "application/json",
        "Authorization": _bearer_header_value(token),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _jira_adf_text(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _jira_fields_payload(
    *,
    summary: str = "",
    project_id: str = "",
    project_key: str = "",
    issue_type_id: str = "",
    issue_type_name: str = "",
    description: str = "",
    assignee_account_id: str = "",
    reporter_account_id: str = "",
    priority_id: str = "",
    labels: str = "",
    parent_key: str = "",
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if summary.strip():
        fields["summary"] = summary.strip()
    if project_id.strip():
        fields["project"] = {"id": project_id.strip()}
    elif project_key.strip():
        fields["project"] = {"key": project_key.strip()}
    if issue_type_id.strip():
        fields["issuetype"] = {"id": issue_type_id.strip()}
    elif issue_type_name.strip():
        fields["issuetype"] = {"name": issue_type_name.strip()}
    if description.strip():
        fields["description"] = _jira_adf_text(description.strip())
    if assignee_account_id.strip():
        fields["assignee"] = {"id": assignee_account_id.strip()}
    if reporter_account_id.strip():
        fields["reporter"] = {"id": reporter_account_id.strip()}
    if priority_id.strip():
        fields["priority"] = {"id": priority_id.strip()}
    label_list = _split_csv(labels)
    if label_list:
        fields["labels"] = label_list
    if parent_key.strip():
        fields["parent"] = {"key": parent_key.strip()}
    return fields


def _epoch_ms(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _clickup_task_body(
    *,
    name: str = "",
    description: str = "",
    markdown_content: str = "",
    assignees: str = "",
    tags: str = "",
    status: str = "",
    priority: Optional[int] = None,
    due_date: str = "",
    start_date: str = "",
    notify_all: Optional[bool] = None,
    parent: str = "",
) -> dict[str, Any]:
    body = _filtered_params(
        {
            "name": name.strip(),
            "description": description.strip(),
            "markdown_content": markdown_content.strip(),
            "assignees": _parse_int_csv(assignees),
            "tags": _split_csv(tags),
            "status": status.strip(),
            "priority": priority,
            "due_date": _epoch_ms(due_date) if due_date.strip() else None,
            "start_date": _epoch_ms(start_date) if start_date.strip() else None,
            "notify_all": notify_all,
            "parent": parent.strip(),
        }
    )
    if body.get("markdown_content"):
        body.pop("description", None)
    return body


def _monday_graphql(
    api_url: str,
    headers: dict[str, str],
    query: str,
    variables: Optional[dict[str, Any]] = None,
) -> Any:
    data = _request_json(
        "POST",
        api_url,
        json_body={"query": query, "variables": _filtered_params(variables)},
        headers=headers,
    )
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        message = first.get("message") if isinstance(first, dict) else str(first)
        raise RuntimeError(message)
    return data.get("data", data) if isinstance(data, dict) else data


def _monday_json_scalar(value: str, field_name: str) -> str | None:
    parsed = _json_object_arg(value, field_name)
    return json.dumps(parsed) if parsed else None


_TAIGA_RESOURCE_ENDPOINTS = {
    "epic": "/epics",
    "epics": "/epics",
    "issue": "/issues",
    "issues": "/issues",
    "task": "/tasks",
    "tasks": "/tasks",
    "story": "/userstories",
    "stories": "/userstories",
    "user_story": "/userstories",
    "user_stories": "/userstories",
    "userstory": "/userstories",
    "userstories": "/userstories",
}


def _taiga_resource_endpoint(resource: str) -> str:
    endpoint = _TAIGA_RESOURCE_ENDPOINTS.get(resource.strip().lower())
    if not endpoint:
        raise ValueError("resource must be one of epic, issue, task, or user_story")
    return endpoint


def _taiga_record_url(base_url: str, resource: str, record_id: str = "") -> str:
    endpoint = _taiga_resource_endpoint(resource)
    if record_id.strip():
        return f"{base_url}{endpoint}/{quote(record_id.strip(), safe='')}"
    return f"{base_url}{endpoint}"


def _wekan_api_url(base_url: str, endpoint: str) -> str:
    return f"{base_url}/api/{endpoint.strip('/')}"


def _wekan_request(
    method: str,
    base_url: str,
    endpoint: str,
    headers: dict[str, str],
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> Any:
    return _request_json(
        method,
        _wekan_api_url(base_url, endpoint),
        params=params,
        json_body=json_body,
        headers=headers,
    )


@tool
def jira_get_myself(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Jira user for the configured credential."""
    try:
        base_url, headers_or_error = _jira_config("jira_get_myself", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/rest/api/3/myself", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("jira_get_myself failed", exc_info=True)
        return f"[Error]: Jira current user lookup failed: {e}"


@tool
def jira_list_projects(
    query: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Jira projects visible to the credential.

    Args:
        query: Optional project search query.
        limit: Number of projects to return, 1-100.
    """
    try:
        base_url, headers_or_error = _jira_config("jira_list_projects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/rest/api/3/project/search",
            params={"query": query.strip(), "maxResults": _limit(limit)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("values", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("jira_list_projects failed", exc_info=True)
        return f"[Error]: Jira project list failed: {e}"


@tool
def jira_search_issues(
    jql: str,
    fields: str = "summary,status,assignee,priority,updated",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Jira issues with JQL.

    Args:
        jql: Jira Query Language query.
        fields: Comma-separated fields to return.
        limit: Number of issues to return, 1-100.
    """
    if not jql.strip():
        return "[Error]: jql is required."
    try:
        base_url, headers_or_error = _jira_config("jira_search_issues", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/rest/api/3/search/jql",
            json_body={
                "jql": jql.strip(),
                "fields": _split_csv(fields) or ["summary", "status"],
                "maxResults": _limit(limit),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("issues", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("jira_search_issues failed", exc_info=True)
        return f"[Error]: Jira issue search failed: {e}"


@tool
def jira_get_issue(
    issue_key: str,
    fields: str = "summary,status,description,assignee,priority,updated",
    expand: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Jira issue by key or ID.

    Args:
        issue_key: Jira issue key or ID.
        fields: Comma-separated fields to return.
        expand: Optional comma-separated expand values.
    """
    issue_key = issue_key.strip()
    if not issue_key:
        return "[Error]: issue_key is required."
    try:
        base_url, headers_or_error = _jira_config("jira_get_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/rest/api/3/issue/{quote(issue_key, safe='')}",
            params={"fields": ",".join(_split_csv(fields)), "expand": ",".join(_split_csv(expand))},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jira_get_issue failed", exc_info=True)
        return f"[Error]: Jira issue lookup failed: {e}"


@tool
def jira_create_issue(
    summary: str,
    project_id: str = "",
    project_key: str = "",
    issue_type_id: str = "",
    issue_type_name: str = "Task",
    description: str = "",
    assignee_account_id: str = "",
    priority_id: str = "",
    labels: str = "",
    parent_key: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Jira issue.

    Args:
        summary: Issue summary.
        project_id: Optional Jira project ID.
        project_key: Optional Jira project key.
        issue_type_id: Optional issue type ID.
        issue_type_name: Optional issue type name, defaults to Task.
        description: Optional plain-text issue description.
        assignee_account_id: Optional assignee account ID.
        priority_id: Optional priority ID.
        labels: Optional comma-separated labels.
        parent_key: Optional parent issue key for subtasks.
    """
    if not summary.strip() or (not project_id.strip() and not project_key.strip()):
        return "[Error]: summary and project_id or project_key are required."
    fields = _jira_fields_payload(
        summary=summary,
        project_id=project_id,
        project_key=project_key,
        issue_type_id=issue_type_id,
        issue_type_name=issue_type_name,
        description=description,
        assignee_account_id=assignee_account_id,
        priority_id=priority_id,
        labels=labels,
        parent_key=parent_key,
    )
    try:
        base_url, headers_or_error = _jira_config("jira_create_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/rest/api/3/issue",
            json_body={"fields": fields},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jira_create_issue failed", exc_info=True)
        return f"[Error]: Jira issue creation failed: {e}"


@tool
def jira_update_issue(
    issue_key: str,
    summary: str = "",
    description: str = "",
    assignee_account_id: str = "",
    reporter_account_id: str = "",
    priority_id: str = "",
    labels: str = "",
    transition_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Jira issue and optionally transition status.

    Args:
        issue_key: Jira issue key or ID.
        summary: Optional new summary.
        description: Optional plain-text description.
        assignee_account_id: Optional assignee account ID.
        reporter_account_id: Optional reporter account ID.
        priority_id: Optional priority ID.
        labels: Optional comma-separated labels.
        transition_id: Optional transition ID to apply after field updates.
    """
    issue_key = issue_key.strip()
    if not issue_key:
        return "[Error]: issue_key is required."
    fields = _jira_fields_payload(
        summary=summary,
        description=description,
        assignee_account_id=assignee_account_id,
        reporter_account_id=reporter_account_id,
        priority_id=priority_id,
        labels=labels,
    )
    if not fields and not transition_id.strip():
        return "[Error]: provide at least one field or transition_id."
    try:
        base_url, headers_or_error = _jira_config("jira_update_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        result: dict[str, Any] = {}
        if fields:
            result["update"] = _request_json(
                "PUT",
                f"{base_url}/rest/api/3/issue/{quote(issue_key, safe='')}",
                json_body={"fields": fields},
                headers=headers_or_error,
            )
        if transition_id.strip():
            result["transition"] = _request_json(
                "POST",
                f"{base_url}/rest/api/3/issue/{quote(issue_key, safe='')}/transitions",
                json_body={"transition": {"id": transition_id.strip()}},
                headers=headers_or_error,
            )
        return _dump_json(result or {"status": "ok"})
    except Exception as e:
        logger.error("jira_update_issue failed", exc_info=True)
        return f"[Error]: Jira issue update failed: {e}"


@tool
def jira_list_issue_transitions(
    issue_key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List available Jira transitions for an issue.

    Args:
        issue_key: Jira issue key or ID.
    """
    issue_key = issue_key.strip()
    if not issue_key:
        return "[Error]: issue_key is required."
    try:
        base_url, headers_or_error = _jira_config("jira_list_issue_transitions", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/rest/api/3/issue/{quote(issue_key, safe='')}/transitions",
            headers=headers_or_error,
        )
        return _dump_json(data.get("transitions", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("jira_list_issue_transitions failed", exc_info=True)
        return f"[Error]: Jira transition list failed: {e}"


@tool
def jira_list_users(
    query: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Jira users.

    Args:
        query: User search query.
        limit: Number of users to return, 1-100.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _jira_config("jira_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/rest/api/3/user/search",
            params={"query": query.strip(), "maxResults": _limit(limit)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jira_list_users failed", exc_info=True)
        return f"[Error]: Jira user search failed: {e}"


@tool
def jira_list_issue_comments(
    issue_key: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List comments on a Jira issue.

    Args:
        issue_key: Jira issue key or ID.
        limit: Number of comments to return, 1-100.
    """
    issue_key = issue_key.strip()
    if not issue_key:
        return "[Error]: issue_key is required."
    try:
        base_url, headers_or_error = _jira_config("jira_list_issue_comments", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/rest/api/3/issue/{quote(issue_key, safe='')}/comment",
            params={"maxResults": _limit(limit)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("comments", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("jira_list_issue_comments failed", exc_info=True)
        return f"[Error]: Jira comment list failed: {e}"


@tool
def jira_add_issue_comment(
    issue_key: str,
    body: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a plain-text comment to a Jira issue.

    Args:
        issue_key: Jira issue key or ID.
        body: Comment text.
    """
    if not issue_key.strip() or not body.strip():
        return "[Error]: issue_key and body are required."
    try:
        base_url, headers_or_error = _jira_config("jira_add_issue_comment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/rest/api/3/issue/{quote(issue_key.strip(), safe='')}/comment",
            json_body={"body": _jira_adf_text(body.strip())},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jira_add_issue_comment failed", exc_info=True)
        return f"[Error]: Jira comment creation failed: {e}"


@tool
def clickup_list_teams(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ClickUp workspaces/teams visible to the credential."""
    try:
        base_url, headers_or_error = _clickup_config("clickup_list_teams", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/team", headers=headers_or_error)
        return _dump_json(data.get("teams", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("clickup_list_teams failed", exc_info=True)
        return f"[Error]: ClickUp team list failed: {e}"


@tool
def clickup_list_spaces(
    team_id: str,
    archived: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ClickUp spaces in a workspace/team.

    Args:
        team_id: ClickUp team/workspace ID.
        archived: Optional archived filter.
    """
    team_id = team_id.strip()
    if not team_id:
        return "[Error]: team_id is required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_list_spaces", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/team/{quote(team_id, safe='')}/space",
            params={"archived": archived},
            headers=headers_or_error,
        )
        return _dump_json(data.get("spaces", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("clickup_list_spaces failed", exc_info=True)
        return f"[Error]: ClickUp space list failed: {e}"


@tool
def clickup_list_folders(
    space_id: str,
    archived: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ClickUp folders in a space.

    Args:
        space_id: ClickUp space ID.
        archived: Optional archived filter.
    """
    space_id = space_id.strip()
    if not space_id:
        return "[Error]: space_id is required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_list_folders", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/space/{quote(space_id, safe='')}/folder",
            params={"archived": archived},
            headers=headers_or_error,
        )
        return _dump_json(data.get("folders", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("clickup_list_folders failed", exc_info=True)
        return f"[Error]: ClickUp folder list failed: {e}"


@tool
def clickup_list_lists(
    folder_id: str = "",
    space_id: str = "",
    archived: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ClickUp lists in a folder or folderless space.

    Args:
        folder_id: Optional ClickUp folder ID.
        space_id: Optional ClickUp space ID for folderless lists.
        archived: Optional archived filter.
    """
    if not folder_id.strip() and not space_id.strip():
        return "[Error]: folder_id or space_id is required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_list_lists", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = (
            f"{base_url}/folder/{quote(folder_id.strip(), safe='')}/list"
            if folder_id.strip()
            else f"{base_url}/space/{quote(space_id.strip(), safe='')}/list"
        )
        data = _request_json(
            "GET",
            endpoint,
            params={"archived": archived},
            headers=headers_or_error,
        )
        return _dump_json(data.get("lists", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("clickup_list_lists failed", exc_info=True)
        return f"[Error]: ClickUp list list failed: {e}"


@tool
def clickup_get_task(
    task_id: str,
    include_subtasks: bool = False,
    include_markdown_description: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a ClickUp task by ID.

    Args:
        task_id: ClickUp task ID.
        include_subtasks: Include subtasks in the response.
        include_markdown_description: Include Markdown description.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_get_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/task/{quote(task_id, safe='')}",
            params={
                "include_subtasks": include_subtasks,
                "include_markdown_description": include_markdown_description,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("clickup_get_task failed", exc_info=True)
        return f"[Error]: ClickUp task lookup failed: {e}"


@tool
def clickup_list_tasks(
    list_id: str,
    include_closed: bool = False,
    subtasks: bool = False,
    statuses: str = "",
    assignees: str = "",
    tags: str = "",
    page: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ClickUp tasks in a list.

    Args:
        list_id: ClickUp list ID.
        include_closed: Include closed tasks.
        subtasks: Include subtasks.
        statuses: Optional comma-separated status filters.
        assignees: Optional comma-separated assignee IDs.
        tags: Optional comma-separated tag filters.
        page: Zero-based ClickUp result page.
    """
    list_id = list_id.strip()
    if not list_id:
        return "[Error]: list_id is required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_list_tasks", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/list/{quote(list_id, safe='')}/task",
            params={
                "include_closed": include_closed,
                "subtasks": subtasks,
                "statuses[]": _split_csv(statuses),
                "assignees[]": _parse_int_csv(assignees),
                "tags[]": _split_csv(tags),
                "page": max(0, int(page or 0)),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("tasks", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("clickup_list_tasks failed", exc_info=True)
        return f"[Error]: ClickUp task list failed: {e}"


@tool
def clickup_create_task(
    list_id: str,
    name: str,
    description: str = "",
    markdown_content: str = "",
    assignees: str = "",
    tags: str = "",
    status: str = "",
    priority: Optional[int] = None,
    due_date: str = "",
    start_date: str = "",
    notify_all: Optional[bool] = None,
    parent: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a ClickUp task.

    Args:
        list_id: ClickUp list ID.
        name: Task name.
        description: Optional plain-text description.
        markdown_content: Optional Markdown description. Overrides description.
        assignees: Optional comma-separated numeric assignee IDs.
        tags: Optional comma-separated tags.
        status: Optional status name.
        priority: Optional priority 1-4.
        due_date: Optional ISO date/datetime or epoch milliseconds.
        start_date: Optional ISO date/datetime or epoch milliseconds.
        notify_all: Optional notification flag.
        parent: Optional parent task ID.
    """
    if not list_id.strip() or not name.strip():
        return "[Error]: list_id and name are required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_create_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _clickup_task_body(
            name=name,
            description=description,
            markdown_content=markdown_content,
            assignees=assignees,
            tags=tags,
            status=status,
            priority=priority,
            due_date=due_date,
            start_date=start_date,
            notify_all=notify_all,
            parent=parent,
        )
        data = _request_json(
            "POST",
            f"{base_url}/list/{quote(list_id.strip(), safe='')}/task",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("clickup_create_task failed", exc_info=True)
        return f"[Error]: ClickUp task creation failed: {e}"


@tool
def clickup_update_task(
    task_id: str,
    name: str = "",
    description: str = "",
    markdown_content: str = "",
    assignees_add: str = "",
    assignees_remove: str = "",
    tags: str = "",
    status: str = "",
    priority: Optional[int] = None,
    due_date: str = "",
    start_date: str = "",
    notify_all: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a ClickUp task.

    Args:
        task_id: ClickUp task ID.
        name: Optional new task name.
        description: Optional plain-text description.
        markdown_content: Optional Markdown description. Overrides description.
        assignees_add: Optional comma-separated numeric assignee IDs to add.
        assignees_remove: Optional comma-separated numeric assignee IDs to remove.
        tags: Optional comma-separated tags to set.
        status: Optional status name.
        priority: Optional priority 1-4.
        due_date: Optional ISO date/datetime or epoch milliseconds.
        start_date: Optional ISO date/datetime or epoch milliseconds.
        notify_all: Optional notification flag.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    body = _clickup_task_body(
        name=name,
        description=description,
        markdown_content=markdown_content,
        tags=tags,
        status=status,
        priority=priority,
        due_date=due_date,
        start_date=start_date,
        notify_all=notify_all,
    )
    add = _parse_int_csv(assignees_add)
    rem = _parse_int_csv(assignees_remove)
    if add or rem:
        body["assignees"] = {"add": add, "rem": rem}
    if not body:
        return "[Error]: provide at least one field to update."
    try:
        base_url, headers_or_error = _clickup_config("clickup_update_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/task/{quote(task_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("clickup_update_task failed", exc_info=True)
        return f"[Error]: ClickUp task update failed: {e}"


@tool
def clickup_list_task_comments(
    task_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List comments on a ClickUp task.

    Args:
        task_id: ClickUp task ID.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_list_task_comments", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/task/{quote(task_id, safe='')}/comment",
            headers=headers_or_error,
        )
        return _dump_json(data.get("comments", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("clickup_list_task_comments failed", exc_info=True)
        return f"[Error]: ClickUp comment list failed: {e}"


@tool
def clickup_add_task_comment(
    task_id: str,
    comment_text: str,
    notify_all: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a comment to a ClickUp task.

    Args:
        task_id: ClickUp task ID.
        comment_text: Comment text.
        notify_all: Optional notification flag.
    """
    if not task_id.strip() or not comment_text.strip():
        return "[Error]: task_id and comment_text are required."
    try:
        base_url, headers_or_error = _clickup_config("clickup_add_task_comment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/task/{quote(task_id.strip(), safe='')}/comment",
            json_body=_filtered_params({"comment_text": comment_text.strip(), "notify_all": notify_all}),
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("clickup_add_task_comment failed", exc_info=True)
        return f"[Error]: ClickUp comment creation failed: {e}"


@tool
def monday_get_me(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Monday user for the configured credential."""
    try:
        api_url, headers_or_error = _monday_config("monday_get_me", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            "query { me { id name email is_admin is_guest is_view_only enabled } }",
        )
        return _dump_json(data.get("me", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_get_me failed", exc_info=True)
        return f"[Error]: Monday current user lookup failed: {e}"


@tool
def monday_list_boards(
    limit: int = 50,
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Monday boards visible to the credential.

    Args:
        limit: Number of boards to return, 1-100.
        page: Monday pagination page number.
    """
    try:
        api_url, headers_or_error = _monday_config("monday_list_boards", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            query ($page: Int, $limit: Int) {
              boards(page: $page, limit: $limit) {
                id
                name
                description
                state
                board_kind
                board_folder_id
                owners { id name }
              }
            }
            """,
            {"page": max(1, int(page)), "limit": _limit(limit)},
        )
        return _dump_json(data.get("boards", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_list_boards failed", exc_info=True)
        return f"[Error]: Monday board list failed: {e}"


@tool
def monday_get_board(
    board_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Monday board by ID.

    Args:
        board_id: Monday board ID.
    """
    board_id = board_id.strip()
    if not board_id:
        return "[Error]: board_id is required."
    try:
        api_url, headers_or_error = _monday_config("monday_get_board", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            query ($id: [ID!]) {
              boards(ids: $id) {
                id
                name
                description
                state
                board_kind
                board_folder_id
                owners { id name }
                groups { id title color position archived }
                columns { id title type settings_str archived }
              }
            }
            """,
            {"id": [board_id]},
        )
        return _dump_json(data.get("boards", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_get_board failed", exc_info=True)
        return f"[Error]: Monday board lookup failed: {e}"


@tool
def monday_create_board(
    name: str,
    kind: str = "public",
    template_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Monday board.

    Args:
        name: Board name.
        kind: Board kind, such as public, private, or share.
        template_id: Optional template board ID.
    """
    if not name.strip():
        return "[Error]: name is required."
    try:
        api_url, headers_or_error = _monday_config("monday_create_board", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            mutation ($name: String!, $kind: BoardKind!, $templateId: ID) {
              create_board(board_name: $name, board_kind: $kind, template_id: $templateId) {
                id
                name
                state
                board_kind
              }
            }
            """,
            {
                "name": name.strip(),
                "kind": kind.strip() or "public",
                "templateId": template_id.strip(),
            },
        )
        return _dump_json(data.get("create_board", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_create_board failed", exc_info=True)
        return f"[Error]: Monday board creation failed: {e}"


@tool
def monday_archive_board(
    board_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Archive a Monday board.

    Args:
        board_id: Monday board ID.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    try:
        api_url, headers_or_error = _monday_config("monday_archive_board", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            "mutation ($id: ID!) { archive_board(board_id: $id) { id } }",
            {"id": board_id.strip()},
        )
        return _dump_json(data.get("archive_board", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_archive_board failed", exc_info=True)
        return f"[Error]: Monday board archive failed: {e}"


@tool
def monday_list_board_columns(
    board_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List columns on a Monday board.

    Args:
        board_id: Monday board ID.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    try:
        api_url, headers_or_error = _monday_config("monday_list_board_columns", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            query ($boardId: [ID!]) {
              boards(ids: $boardId) {
                columns { id title type settings_str archived }
              }
            }
            """,
            {"boardId": [board_id.strip()]},
        )
        boards = data.get("boards", []) if isinstance(data, dict) else []
        columns = boards[0].get("columns", []) if boards else []
        return _dump_json(columns)
    except Exception as e:
        logger.error("monday_list_board_columns failed", exc_info=True)
        return f"[Error]: Monday column list failed: {e}"


@tool
def monday_create_board_column(
    board_id: str,
    title: str,
    column_type: str = "text",
    defaults_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a column on a Monday board.

    Args:
        board_id: Monday board ID.
        title: Column title.
        column_type: Monday column type, such as text, status, date, or numbers.
        defaults_json: Optional column defaults JSON object.
    """
    if not board_id.strip() or not title.strip():
        return "[Error]: board_id and title are required."
    try:
        api_url, headers_or_error = _monday_config("monday_create_board_column", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            mutation ($boardId: ID!, $title: String!, $columnType: ColumnType!, $defaults: JSON) {
              create_column(board_id: $boardId, title: $title, column_type: $columnType, defaults: $defaults) {
                id
                title
                type
              }
            }
            """,
            {
                "boardId": board_id.strip(),
                "title": title.strip(),
                "columnType": column_type.strip() or "text",
                "defaults": _monday_json_scalar(defaults_json, "defaults_json"),
            },
        )
        return _dump_json(data.get("create_column", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_create_board_column failed", exc_info=True)
        return f"[Error]: Monday column creation failed: {e}"


@tool
def monday_list_board_groups(
    board_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List groups on a Monday board.

    Args:
        board_id: Monday board ID.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    try:
        api_url, headers_or_error = _monday_config("monday_list_board_groups", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            query ($boardId: [ID!]) {
              boards(ids: $boardId) {
                groups { id title color position archived }
              }
            }
            """,
            {"boardId": [board_id.strip()]},
        )
        boards = data.get("boards", []) if isinstance(data, dict) else []
        groups = boards[0].get("groups", []) if boards else []
        return _dump_json(groups)
    except Exception as e:
        logger.error("monday_list_board_groups failed", exc_info=True)
        return f"[Error]: Monday group list failed: {e}"


@tool
def monday_create_board_group(
    board_id: str,
    group_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a group on a Monday board.

    Args:
        board_id: Monday board ID.
        group_name: New group name.
    """
    if not board_id.strip() or not group_name.strip():
        return "[Error]: board_id and group_name are required."
    try:
        api_url, headers_or_error = _monday_config("monday_create_board_group", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            "mutation ($boardId: ID!, $groupName: String!) { create_group(board_id: $boardId, group_name: $groupName) { id title } }",
            {"boardId": board_id.strip(), "groupName": group_name.strip()},
        )
        return _dump_json(data.get("create_group", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_create_board_group failed", exc_info=True)
        return f"[Error]: Monday group creation failed: {e}"


@tool
def monday_list_items(
    board_id: str,
    group_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Monday items on a board or within a group.

    Args:
        board_id: Monday board ID.
        group_id: Optional board group ID.
        limit: Number of items to return, 1-100.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    item_fields = """
      id
      name
      created_at
      state
      column_values { id text type value column { title archived description settings_str } }
    """
    if group_id.strip():
        query = f"""
        query ($boardId: [ID!], $groupId: [String], $limit: Int) {{
          boards(ids: $boardId) {{
            groups(ids: $groupId) {{
              id
              items_page(limit: $limit) {{ cursor items {{ {item_fields} }} }}
            }}
          }}
        }}
        """
        variables = {"boardId": [board_id.strip()], "groupId": [group_id.strip()], "limit": _limit(limit)}
    else:
        query = f"""
        query ($boardId: [ID!], $limit: Int) {{
          boards(ids: $boardId) {{
            items_page(limit: $limit) {{ cursor items {{ {item_fields} }} }}
          }}
        }}
        """
        variables = {"boardId": [board_id.strip()], "limit": _limit(limit)}
    try:
        api_url, headers_or_error = _monday_config("monday_list_items", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(api_url, headers_or_error, query, variables)
        boards = data.get("boards", []) if isinstance(data, dict) else []
        if not boards:
            return "[]"
        if group_id.strip():
            groups = boards[0].get("groups", [])
            items = groups[0].get("items_page", {}).get("items", []) if groups else []
        else:
            items = boards[0].get("items_page", {}).get("items", [])
        return _dump_json(items)
    except Exception as e:
        logger.error("monday_list_items failed", exc_info=True)
        return f"[Error]: Monday item list failed: {e}"


@tool
def monday_get_item(
    item_ids: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one or more Monday items by ID.

    Args:
        item_ids: Comma-separated Monday item IDs.
    """
    ids = _split_csv(item_ids)
    if not ids:
        return "[Error]: item_ids is required."
    try:
        api_url, headers_or_error = _monday_config("monday_get_item", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            query ($itemIds: [ID!]) {
              items(ids: $itemIds) {
                id
                name
                created_at
                state
                board { id name }
                column_values { id text type value column { title archived description settings_str } }
              }
            }
            """,
            {"itemIds": ids},
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_get_item failed", exc_info=True)
        return f"[Error]: Monday item lookup failed: {e}"


@tool
def monday_create_item(
    board_id: str,
    group_id: str,
    item_name: str,
    column_values_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an item on a Monday board.

    Args:
        board_id: Monday board ID.
        group_id: Monday board group ID.
        item_name: New item name.
        column_values_json: Optional column values JSON object keyed by column ID.
    """
    if not board_id.strip() or not group_id.strip() or not item_name.strip():
        return "[Error]: board_id, group_id, and item_name are required."
    try:
        api_url, headers_or_error = _monday_config("monday_create_item", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON) {
              create_item(board_id: $boardId, group_id: $groupId, item_name: $itemName, column_values: $columnValues) {
                id
                name
              }
            }
            """,
            {
                "boardId": board_id.strip(),
                "groupId": group_id.strip(),
                "itemName": item_name.strip(),
                "columnValues": _monday_json_scalar(column_values_json, "column_values_json"),
            },
        )
        return _dump_json(data.get("create_item", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_create_item failed", exc_info=True)
        return f"[Error]: Monday item creation failed: {e}"


@tool
def monday_update_item_columns(
    board_id: str,
    item_id: str,
    column_values_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update multiple Monday item column values.

    Args:
        board_id: Monday board ID.
        item_id: Monday item ID.
        column_values_json: Column values JSON object keyed by column ID.
    """
    if not board_id.strip() or not item_id.strip() or not column_values_json.strip():
        return "[Error]: board_id, item_id, and column_values_json are required."
    try:
        api_url, headers_or_error = _monday_config("monday_update_item_columns", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            """
            mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
              change_multiple_column_values(board_id: $boardId, item_id: $itemId, column_values: $columnValues) {
                id
                name
              }
            }
            """,
            {
                "boardId": board_id.strip(),
                "itemId": item_id.strip(),
                "columnValues": _monday_json_scalar(column_values_json, "column_values_json"),
            },
        )
        return _dump_json(data.get("change_multiple_column_values", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_update_item_columns failed", exc_info=True)
        return f"[Error]: Monday item column update failed: {e}"


@tool
def monday_add_item_update(
    item_id: str,
    body: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add an update/comment to a Monday item.

    Args:
        item_id: Monday item ID.
        body: Update body text.
    """
    if not item_id.strip() or not body.strip():
        return "[Error]: item_id and body are required."
    try:
        api_url, headers_or_error = _monday_config("monday_add_item_update", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            "mutation ($itemId: ID!, $body: String!) { create_update(item_id: $itemId, body: $body) { id body created_at } }",
            {"itemId": item_id.strip(), "body": body.strip()},
        )
        return _dump_json(data.get("create_update", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_add_item_update failed", exc_info=True)
        return f"[Error]: Monday item update creation failed: {e}"


@tool
def monday_move_item(
    item_id: str,
    group_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Move a Monday item to another group.

    Args:
        item_id: Monday item ID.
        group_id: Target group ID.
    """
    if not item_id.strip() or not group_id.strip():
        return "[Error]: item_id and group_id are required."
    try:
        api_url, headers_or_error = _monday_config("monday_move_item", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            "mutation ($itemId: ID!, $groupId: String!) { move_item_to_group(item_id: $itemId, group_id: $groupId) { id } }",
            {"itemId": item_id.strip(), "groupId": group_id.strip()},
        )
        return _dump_json(data.get("move_item_to_group", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_move_item failed", exc_info=True)
        return f"[Error]: Monday item move failed: {e}"


@tool
def monday_delete_item(
    item_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Monday item.

    Args:
        item_id: Monday item ID.
    """
    if not item_id.strip():
        return "[Error]: item_id is required."
    try:
        api_url, headers_or_error = _monday_config("monday_delete_item", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _monday_graphql(
            api_url,
            headers_or_error,
            "mutation ($itemId: ID!) { delete_item(item_id: $itemId) { id } }",
            {"itemId": item_id.strip()},
        )
        return _dump_json(data.get("delete_item", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("monday_delete_item failed", exc_info=True)
        return f"[Error]: Monday item deletion failed: {e}"


@tool
def taiga_list_projects(
    query: str = "",
    member_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Taiga projects visible to the credential.

    Args:
        query: Optional project search query.
        member_id: Optional member/user ID filter.
        limit: Number of projects to return, 1-100.
    """
    try:
        base_url, headers_or_error = _taiga_config("taiga_list_projects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/projects",
            params={"q": query.strip(), "member": member_id.strip(), "limit": _limit(limit)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("taiga_list_projects failed", exc_info=True)
        return f"[Error]: Taiga project list failed: {e}"


@tool
def taiga_list_records(
    resource: str,
    project_id: str = "",
    status_id: str = "",
    assigned_to: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Taiga epics, issues, tasks, or user stories.

    Args:
        resource: One of epic, issue, task, or user_story.
        project_id: Optional Taiga project ID filter.
        status_id: Optional status ID filter.
        assigned_to: Optional assignee user ID filter.
        limit: Number of records to return, 1-100.
    """
    try:
        base_url, headers_or_error = _taiga_config("taiga_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _taiga_record_url(base_url, resource),
            params={
                "project": project_id.strip(),
                "status": status_id.strip(),
                "assigned_to": assigned_to.strip(),
                "limit": _limit(limit),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("taiga_list_records failed", exc_info=True)
        return f"[Error]: Taiga record list failed: {e}"


@tool
def taiga_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Taiga epic, issue, task, or user story by ID.

    Args:
        resource: One of epic, issue, task, or user_story.
        record_id: Taiga record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        base_url, headers_or_error = _taiga_config("taiga_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _taiga_record_url(base_url, resource, record_id),
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("taiga_get_record failed", exc_info=True)
        return f"[Error]: Taiga record lookup failed: {e}"


@tool
def taiga_create_record(
    resource: str,
    project_id: str,
    subject: str,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Taiga epic, issue, task, or user story.

    Args:
        resource: One of epic, issue, task, or user_story.
        project_id: Taiga project ID.
        subject: Record subject/title.
        fields_json: Optional extra Taiga fields as a JSON object.
    """
    if not project_id.strip() or not subject.strip():
        return "[Error]: project_id and subject are required."
    body = _json_object_arg(fields_json, "fields_json")
    body.update({"project": project_id.strip(), "subject": subject.strip()})
    try:
        base_url, headers_or_error = _taiga_config("taiga_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            _taiga_record_url(base_url, resource),
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("taiga_create_record failed", exc_info=True)
        return f"[Error]: Taiga record creation failed: {e}"


@tool
def taiga_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Taiga epic, issue, task, or user story.

    Args:
        resource: One of epic, issue, task, or user_story.
        record_id: Taiga record ID.
        fields_json: Taiga fields to update as a JSON object.
    """
    if not record_id.strip() or not fields_json.strip():
        return "[Error]: record_id and fields_json are required."
    body = _json_object_arg(fields_json, "fields_json")
    if not body:
        return "[Error]: fields_json must include at least one field."
    try:
        base_url, headers_or_error = _taiga_config("taiga_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if "version" not in body:
            current = _request_json(
                "GET",
                _taiga_record_url(base_url, resource, record_id),
                headers=headers_or_error,
            )
            if isinstance(current, dict) and "version" in current:
                body["version"] = current["version"]
        data = _request_json(
            "PATCH",
            _taiga_record_url(base_url, resource, record_id),
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("taiga_update_record failed", exc_info=True)
        return f"[Error]: Taiga record update failed: {e}"


@tool
def taiga_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Taiga epic, issue, task, or user story.

    Args:
        resource: One of epic, issue, task, or user_story.
        record_id: Taiga record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        base_url, headers_or_error = _taiga_config("taiga_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _request_json(
            "DELETE",
            _taiga_record_url(base_url, resource, record_id),
            headers=headers_or_error,
        )
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("taiga_delete_record failed", exc_info=True)
        return f"[Error]: Taiga record deletion failed: {e}"


@tool
def wekan_get_current_user(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Wekan user for the configured credential."""
    try:
        base_url, headers_or_error = _wekan_config("wekan_get_current_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request("GET", base_url, "user", headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_get_current_user failed", exc_info=True)
        return f"[Error]: Wekan current user lookup failed: {e}"


@tool
def wekan_list_users(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Wekan users visible to the credential."""
    try:
        base_url, headers_or_error = _wekan_config("wekan_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request("GET", base_url, "users", headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_list_users failed", exc_info=True)
        return f"[Error]: Wekan user list failed: {e}"


@tool
def wekan_list_user_boards(
    user_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Wekan boards for a user.

    Args:
        user_id: Optional Wekan user ID. Uses current user when omitted.
        limit: Number of boards to return, 1-100.
    """
    try:
        base_url, headers_or_error = _wekan_config("wekan_list_user_boards", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        resolved_user_id = user_id.strip()
        if not resolved_user_id:
            me = _wekan_request("GET", base_url, "user", headers_or_error)
            resolved_user_id = str(me.get("_id") or me.get("id") or "") if isinstance(me, dict) else ""
        if not resolved_user_id:
            return "[Error]: user_id is required when current Wekan user response has no ID."
        data = _wekan_request(
            "GET",
            base_url,
            f"users/{quote(resolved_user_id, safe='')}/boards",
            headers_or_error,
        )
        return _dump_json(data[: _limit(limit)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("wekan_list_user_boards failed", exc_info=True)
        return f"[Error]: Wekan board list failed: {e}"


@tool
def wekan_get_board(
    board_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Wekan board by ID.

    Args:
        board_id: Wekan board ID.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_get_board", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request("GET", base_url, f"boards/{quote(board_id.strip(), safe='')}", headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_get_board failed", exc_info=True)
        return f"[Error]: Wekan board lookup failed: {e}"


@tool
def wekan_create_board(
    title: str,
    owner_id: str,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Wekan board.

    Args:
        title: Board title.
        owner_id: Wekan user ID for the board owner.
        fields_json: Optional extra board fields as a JSON object.
    """
    if not title.strip() or not owner_id.strip():
        return "[Error]: title and owner_id are required."
    body = _json_object_arg(fields_json, "fields_json")
    body.update({"title": title.strip(), "owner": owner_id.strip()})
    try:
        base_url, headers_or_error = _wekan_config("wekan_create_board", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request("POST", base_url, "boards", headers_or_error, json_body=body)
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_create_board failed", exc_info=True)
        return f"[Error]: Wekan board creation failed: {e}"


@tool
def wekan_delete_board(
    board_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Wekan board.

    Args:
        board_id: Wekan board ID.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_delete_board", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _wekan_request("DELETE", base_url, f"boards/{quote(board_id.strip(), safe='')}", headers_or_error)
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("wekan_delete_board failed", exc_info=True)
        return f"[Error]: Wekan board deletion failed: {e}"


@tool
def wekan_list_lists(
    board_id: str,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Wekan lists on a board.

    Args:
        board_id: Wekan board ID.
        limit: Number of lists to return, 1-100.
    """
    if not board_id.strip():
        return "[Error]: board_id is required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_list_lists", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request("GET", base_url, f"boards/{quote(board_id.strip(), safe='')}/lists", headers_or_error)
        return _dump_json(data[: _limit(limit)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("wekan_list_lists failed", exc_info=True)
        return f"[Error]: Wekan list list failed: {e}"


@tool
def wekan_create_list(
    board_id: str,
    title: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Wekan list on a board.

    Args:
        board_id: Wekan board ID.
        title: List title.
    """
    if not board_id.strip() or not title.strip():
        return "[Error]: board_id and title are required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_create_list", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request(
            "POST",
            base_url,
            f"boards/{quote(board_id.strip(), safe='')}/lists",
            headers_or_error,
            json_body={"title": title.strip()},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_create_list failed", exc_info=True)
        return f"[Error]: Wekan list creation failed: {e}"


@tool
def wekan_delete_list(
    board_id: str,
    list_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Wekan list.

    Args:
        board_id: Wekan board ID.
        list_id: Wekan list ID.
    """
    if not board_id.strip() or not list_id.strip():
        return "[Error]: board_id and list_id are required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_delete_list", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _wekan_request(
            "DELETE",
            base_url,
            f"boards/{quote(board_id.strip(), safe='')}/lists/{quote(list_id.strip(), safe='')}",
            headers_or_error,
        )
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("wekan_delete_list failed", exc_info=True)
        return f"[Error]: Wekan list deletion failed: {e}"


@tool
def wekan_list_cards(
    board_id: str,
    list_id: str = "",
    swimlane_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Wekan cards from a list or swimlane.

    Args:
        board_id: Wekan board ID.
        list_id: Optional Wekan list ID.
        swimlane_id: Optional Wekan swimlane ID.
        limit: Number of cards to return, 1-100.
    """
    if not board_id.strip() or (not list_id.strip() and not swimlane_id.strip()):
        return "[Error]: board_id and either list_id or swimlane_id are required."
    endpoint = (
        f"boards/{quote(board_id.strip(), safe='')}/lists/{quote(list_id.strip(), safe='')}/cards"
        if list_id.strip()
        else f"boards/{quote(board_id.strip(), safe='')}/swimlanes/{quote(swimlane_id.strip(), safe='')}/cards"
    )
    try:
        base_url, headers_or_error = _wekan_config("wekan_list_cards", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request("GET", base_url, endpoint, headers_or_error)
        return _dump_json(data[: _limit(limit)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("wekan_list_cards failed", exc_info=True)
        return f"[Error]: Wekan card list failed: {e}"


@tool
def wekan_get_card(
    board_id: str,
    list_id: str,
    card_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Wekan card by ID.

    Args:
        board_id: Wekan board ID.
        list_id: Wekan list ID.
        card_id: Wekan card ID.
    """
    if not board_id.strip() or not list_id.strip() or not card_id.strip():
        return "[Error]: board_id, list_id, and card_id are required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_get_card", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request(
            "GET",
            base_url,
            (
                f"boards/{quote(board_id.strip(), safe='')}/lists/{quote(list_id.strip(), safe='')}"
                f"/cards/{quote(card_id.strip(), safe='')}"
            ),
            headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_get_card failed", exc_info=True)
        return f"[Error]: Wekan card lookup failed: {e}"


@tool
def wekan_create_card(
    board_id: str,
    list_id: str,
    title: str,
    swimlane_id: str = "",
    author_id: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Wekan card.

    Args:
        board_id: Wekan board ID.
        list_id: Wekan list ID.
        title: Card title.
        swimlane_id: Optional swimlane ID.
        author_id: Optional author user ID.
        fields_json: Optional extra card fields as a JSON object.
    """
    if not board_id.strip() or not list_id.strip() or not title.strip():
        return "[Error]: board_id, list_id, and title are required."
    body = _json_object_arg(fields_json, "fields_json")
    body.update(_filtered_params({"title": title.strip(), "swimlaneId": swimlane_id.strip(), "authorId": author_id.strip()}))
    try:
        base_url, headers_or_error = _wekan_config("wekan_create_card", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request(
            "POST",
            base_url,
            f"boards/{quote(board_id.strip(), safe='')}/lists/{quote(list_id.strip(), safe='')}/cards",
            headers_or_error,
            json_body=body,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_create_card failed", exc_info=True)
        return f"[Error]: Wekan card creation failed: {e}"


@tool
def wekan_update_card(
    board_id: str,
    list_id: str,
    card_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Wekan card.

    Args:
        board_id: Wekan board ID.
        list_id: Wekan list ID.
        card_id: Wekan card ID.
        fields_json: Card fields to update as a JSON object.
    """
    if not board_id.strip() or not list_id.strip() or not card_id.strip() or not fields_json.strip():
        return "[Error]: board_id, list_id, card_id, and fields_json are required."
    body = _json_object_arg(fields_json, "fields_json")
    if not body:
        return "[Error]: fields_json must include at least one field."
    try:
        base_url, headers_or_error = _wekan_config("wekan_update_card", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request(
            "PUT",
            base_url,
            (
                f"boards/{quote(board_id.strip(), safe='')}/lists/{quote(list_id.strip(), safe='')}"
                f"/cards/{quote(card_id.strip(), safe='')}"
            ),
            headers_or_error,
            json_body=body,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_update_card failed", exc_info=True)
        return f"[Error]: Wekan card update failed: {e}"


@tool
def wekan_delete_card(
    board_id: str,
    list_id: str,
    card_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Wekan card.

    Args:
        board_id: Wekan board ID.
        list_id: Wekan list ID.
        card_id: Wekan card ID.
    """
    if not board_id.strip() or not list_id.strip() or not card_id.strip():
        return "[Error]: board_id, list_id, and card_id are required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_delete_card", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _wekan_request(
            "DELETE",
            base_url,
            (
                f"boards/{quote(board_id.strip(), safe='')}/lists/{quote(list_id.strip(), safe='')}"
                f"/cards/{quote(card_id.strip(), safe='')}"
            ),
            headers_or_error,
        )
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("wekan_delete_card failed", exc_info=True)
        return f"[Error]: Wekan card deletion failed: {e}"


@tool
def wekan_list_card_comments(
    board_id: str,
    card_id: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List comments on a Wekan card.

    Args:
        board_id: Wekan board ID.
        card_id: Wekan card ID.
        limit: Number of comments to return, 1-100.
    """
    if not board_id.strip() or not card_id.strip():
        return "[Error]: board_id and card_id are required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_list_card_comments", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request(
            "GET",
            base_url,
            f"boards/{quote(board_id.strip(), safe='')}/cards/{quote(card_id.strip(), safe='')}/comments",
            headers_or_error,
        )
        return _dump_json(data[: _limit(limit)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("wekan_list_card_comments failed", exc_info=True)
        return f"[Error]: Wekan card comment list failed: {e}"


@tool
def wekan_add_card_comment(
    board_id: str,
    card_id: str,
    comment: str,
    author_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a comment to a Wekan card.

    Args:
        board_id: Wekan board ID.
        card_id: Wekan card ID.
        comment: Comment text.
        author_id: Optional author user ID.
    """
    if not board_id.strip() or not card_id.strip() or not comment.strip():
        return "[Error]: board_id, card_id, and comment are required."
    try:
        base_url, headers_or_error = _wekan_config("wekan_add_card_comment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _wekan_request(
            "POST",
            base_url,
            f"boards/{quote(board_id.strip(), safe='')}/cards/{quote(card_id.strip(), safe='')}/comments",
            headers_or_error,
            json_body=_filtered_params({"comment": comment.strip(), "authorId": author_id.strip()}),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wekan_add_card_comment failed", exc_info=True)
        return f"[Error]: Wekan card comment creation failed: {e}"


PROJECT_MANAGEMENT_SERVICE_TOOLS = [
    jira_get_myself,
    jira_list_projects,
    jira_search_issues,
    jira_get_issue,
    jira_create_issue,
    jira_update_issue,
    jira_list_issue_transitions,
    jira_list_users,
    jira_list_issue_comments,
    jira_add_issue_comment,
    clickup_list_teams,
    clickup_list_spaces,
    clickup_list_folders,
    clickup_list_lists,
    clickup_get_task,
    clickup_list_tasks,
    clickup_create_task,
    clickup_update_task,
    clickup_list_task_comments,
    clickup_add_task_comment,
    monday_get_me,
    monday_list_boards,
    monday_get_board,
    monday_create_board,
    monday_archive_board,
    monday_list_board_columns,
    monday_create_board_column,
    monday_list_board_groups,
    monday_create_board_group,
    monday_list_items,
    monday_get_item,
    monday_create_item,
    monday_update_item_columns,
    monday_add_item_update,
    monday_move_item,
    monday_delete_item,
    taiga_list_projects,
    taiga_list_records,
    taiga_get_record,
    taiga_create_record,
    taiga_update_record,
    taiga_delete_record,
    wekan_get_current_user,
    wekan_list_users,
    wekan_list_user_boards,
    wekan_get_board,
    wekan_create_board,
    wekan_delete_board,
    wekan_list_lists,
    wekan_create_list,
    wekan_delete_list,
    wekan_list_cards,
    wekan_get_card,
    wekan_create_card,
    wekan_update_card,
    wekan_delete_card,
    wekan_list_card_comments,
    wekan_add_card_comment,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="project_management", tools=tuple(PROJECT_MANAGEMENT_SERVICE_TOOLS)))
