"""Work tracking service integration tools."""

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
_ASANA_BASE_URL = "https://app.asana.com/api/1.0"
_LINEAR_API_URL = "https://api.linear.app/graphql"


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
        if value is not None and value != "" and value != []
    }


def _limit(value: int, *, default: int = 20, max_value: int = 100) -> int:
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
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    detail = first.get("message") or str(first)
            detail = (
                detail
                or body.get("message")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _asana_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="asana",
            provider_aliases=("asana_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("asana_base_url")
        or _ASANA_BASE_URL
    )
    token = _credential_value(
        provider="asana",
        provider_aliases=("asana_api",),
        field_names=("access_token", "api_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("asana_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="asana",
            field_names=("access_token", "api_key", "value"),
            tool_name=tool_name,
            env_var="ASANA_ACCESS_TOKEN",
            display_name="Asana",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _linear_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    api_url = (
        _credential_value(
            provider="linear",
            provider_aliases=("linear_api",),
            field_names=("api_url", "graphql_url", "base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("linear_api_url")
        or _LINEAR_API_URL
    )
    api_key = _credential_value(
        provider="linear",
        provider_aliases=("linear_api",),
        field_names=("api_key", "access_token", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("linear_api_key")
    if not api_key:
        return _base_url(api_url), _setup_hint(
            provider="linear",
            field_names=("api_key", "access_token", "value"),
            tool_name=tool_name,
            env_var="LINEAR_API_KEY",
            display_name="Linear",
        )
    return _base_url(api_url), {
        "Accept": "application/json",
        "Authorization": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _asana_data(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def _asana_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {"data": _filtered_params(data)}


def _asana_csv(value: str) -> list[str] | None:
    parts = _split_csv(value)
    return parts or None


def _asana_task_body(
    *,
    name: str = "",
    notes: str = "",
    workspace_gid: str = "",
    projects: str = "",
    parent: str = "",
    assignee: str = "",
    due_on: str = "",
    due_at: str = "",
    completed: Optional[bool] = None,
) -> dict[str, Any]:
    return _filtered_params(
        {
            "name": name.strip(),
            "notes": notes.strip(),
            "workspace": workspace_gid.strip(),
            "projects": _asana_csv(projects),
            "parent": parent.strip(),
            "assignee": assignee.strip(),
            "due_on": due_on.strip(),
            "due_at": due_at.strip(),
            "completed": completed,
        }
    )


def _linear_graphql(
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
        message = errors[0].get("message") if isinstance(errors[0], dict) else str(errors[0])
        raise RuntimeError(message)
    return data.get("data", data) if isinstance(data, dict) else data


def _linear_nodes(payload: Any, field_name: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    field = payload.get(field_name)
    if isinstance(field, dict) and isinstance(field.get("nodes"), list):
        return field["nodes"]
    return []


_LINEAR_ISSUE_FIELDS = """
id
identifier
title
description
priority
url
createdAt
updatedAt
dueDate
assignee { id name displayName }
creator { id name displayName }
state { id name type }
team { id key name }
"""


@tool
def asana_get_user(
    user_gid: str = "me",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Asana user by ID or "me".

    Args:
        user_gid: Asana user GID, email, or "me".
    """
    user_gid = user_gid.strip() or "me"
    try:
        base_url, headers_or_error = _asana_config("asana_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/users/{quote(user_gid, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_get_user failed", exc_info=True)
        return f"[Error]: Asana user lookup failed: {e}"


@tool
def asana_list_users(
    workspace_gid: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List users in an Asana workspace.

    Args:
        workspace_gid: Asana workspace GID.
        limit: Number of users to return, 1-100.
    """
    workspace_gid = workspace_gid.strip()
    if not workspace_gid:
        return "[Error]: workspace_gid is required."
    try:
        base_url, headers_or_error = _asana_config("asana_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/workspaces/{quote(workspace_gid, safe='')}/users",
            params={"limit": _limit(limit)},
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_list_users failed", exc_info=True)
        return f"[Error]: Asana user list failed: {e}"


@tool
def asana_list_projects(
    workspace_gid: str = "",
    team_gid: str = "",
    archived: Optional[bool] = None,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Asana projects by workspace or team.

    Args:
        workspace_gid: Optional Asana workspace GID.
        team_gid: Optional Asana team GID.
        archived: Optional archive filter.
        limit: Number of projects to return, 1-100.
    """
    if not workspace_gid.strip() and not team_gid.strip():
        return "[Error]: workspace_gid or team_gid is required."
    try:
        base_url, headers_or_error = _asana_config("asana_list_projects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/projects",
            params={
                "workspace": workspace_gid.strip(),
                "team": team_gid.strip(),
                "archived": archived,
                "limit": _limit(limit),
            },
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_list_projects failed", exc_info=True)
        return f"[Error]: Asana project list failed: {e}"


@tool
def asana_get_project(
    project_gid: str,
    opt_fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Asana project by GID.

    Args:
        project_gid: Asana project GID.
        opt_fields: Optional comma-separated response fields.
    """
    project_gid = project_gid.strip()
    if not project_gid:
        return "[Error]: project_gid is required."
    try:
        base_url, headers_or_error = _asana_config("asana_get_project", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/projects/{quote(project_gid, safe='')}",
            params={"opt_fields": ",".join(_split_csv(opt_fields))},
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_get_project failed", exc_info=True)
        return f"[Error]: Asana project lookup failed: {e}"


@tool
def asana_create_project(
    name: str,
    workspace_gid: str,
    team_gid: str,
    notes: str = "",
    color: str = "",
    due_on: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Asana project in a team.

    Args:
        name: Project name.
        workspace_gid: Asana workspace GID.
        team_gid: Asana team GID.
        notes: Optional project notes.
        color: Optional Asana color name.
        due_on: Optional due date in YYYY-MM-DD format.
    """
    if not name.strip() or not workspace_gid.strip() or not team_gid.strip():
        return "[Error]: name, workspace_gid, and team_gid are required."
    try:
        base_url, headers_or_error = _asana_config("asana_create_project", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/teams/{quote(team_gid.strip(), safe='')}/projects",
            json_body=_asana_payload(
                {
                    "name": name.strip(),
                    "workspace": workspace_gid.strip(),
                    "notes": notes.strip(),
                    "color": color.strip(),
                    "due_on": due_on.strip(),
                }
            ),
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_create_project failed", exc_info=True)
        return f"[Error]: Asana project creation failed: {e}"


@tool
def asana_update_project(
    project_gid: str,
    name: str = "",
    notes: str = "",
    color: str = "",
    due_on: str = "",
    owner: str = "",
    team_gid: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an Asana project.

    Args:
        project_gid: Asana project GID.
        name: Optional new project name.
        notes: Optional project notes.
        color: Optional Asana color name.
        due_on: Optional due date in YYYY-MM-DD format.
        owner: Optional owner user GID.
        team_gid: Optional team GID.
    """
    project_gid = project_gid.strip()
    if not project_gid:
        return "[Error]: project_gid is required."
    body = _filtered_params(
        {
            "name": name.strip(),
            "notes": notes.strip(),
            "color": color.strip(),
            "due_on": due_on.strip(),
            "owner": owner.strip(),
            "team": team_gid.strip(),
        }
    )
    if not body:
        return "[Error]: provide at least one field to update."
    try:
        base_url, headers_or_error = _asana_config("asana_update_project", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/projects/{quote(project_gid, safe='')}",
            json_body=_asana_payload(body),
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_update_project failed", exc_info=True)
        return f"[Error]: Asana project update failed: {e}"


@tool
def asana_list_tasks(
    project_gid: str = "",
    workspace_gid: str = "",
    assignee: str = "",
    completed_since: str = "",
    modified_since: str = "",
    limit: int = 50,
    opt_fields: str = "gid,name,completed,assignee.name,due_on,permalink_url",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Asana tasks by project or workspace filters.

    Args:
        project_gid: Optional Asana project GID. Uses project-specific task endpoint when set.
        workspace_gid: Optional workspace GID for general task listing.
        assignee: Optional assignee GID or "me".
        completed_since: Optional completed-since timestamp/date.
        modified_since: Optional modified-since timestamp/date.
        limit: Number of tasks to return, 1-100.
        opt_fields: Optional comma-separated response fields.
    """
    if not project_gid.strip() and not workspace_gid.strip():
        return "[Error]: project_gid or workspace_gid is required."
    try:
        base_url, headers_or_error = _asana_config("asana_list_tasks", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = (
            f"{base_url}/projects/{quote(project_gid.strip(), safe='')}/tasks"
            if project_gid.strip()
            else f"{base_url}/tasks"
        )
        data = _request_json(
            "GET",
            endpoint,
            params={
                "workspace": workspace_gid.strip(),
                "assignee": assignee.strip(),
                "completed_since": completed_since.strip(),
                "modified_since": modified_since.strip(),
                "limit": _limit(limit),
                "opt_fields": ",".join(_split_csv(opt_fields)),
            },
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_list_tasks failed", exc_info=True)
        return f"[Error]: Asana task list failed: {e}"


@tool
def asana_search_tasks(
    workspace_gid: str,
    text: str = "",
    assignee: str = "",
    project_gid: str = "",
    completed: Optional[bool] = None,
    limit: int = 50,
    opt_fields: str = "gid,name,completed,assignee.name,due_on,permalink_url",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Asana tasks in a workspace.

    Args:
        workspace_gid: Asana workspace GID.
        text: Optional full-text search query.
        assignee: Optional assignee GID or "me".
        project_gid: Optional project GID filter.
        completed: Optional completed filter.
        limit: Number of tasks to return, 1-100.
        opt_fields: Optional comma-separated response fields.
    """
    workspace_gid = workspace_gid.strip()
    if not workspace_gid:
        return "[Error]: workspace_gid is required."
    try:
        base_url, headers_or_error = _asana_config("asana_search_tasks", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/workspaces/{quote(workspace_gid, safe='')}/tasks/search",
            params={
                "text": text.strip(),
                "assignee.any": assignee.strip(),
                "projects.any": project_gid.strip(),
                "completed": completed,
                "limit": _limit(limit),
                "opt_fields": ",".join(_split_csv(opt_fields)),
            },
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_search_tasks failed", exc_info=True)
        return f"[Error]: Asana task search failed: {e}"


@tool
def asana_get_task(
    task_gid: str,
    opt_fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Asana task by GID.

    Args:
        task_gid: Asana task GID.
        opt_fields: Optional comma-separated response fields.
    """
    task_gid = task_gid.strip()
    if not task_gid:
        return "[Error]: task_gid is required."
    try:
        base_url, headers_or_error = _asana_config("asana_get_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tasks/{quote(task_gid, safe='')}",
            params={"opt_fields": ",".join(_split_csv(opt_fields))},
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_get_task failed", exc_info=True)
        return f"[Error]: Asana task lookup failed: {e}"


@tool
def asana_create_task(
    name: str,
    workspace_gid: str = "",
    notes: str = "",
    projects: str = "",
    parent: str = "",
    assignee: str = "",
    due_on: str = "",
    due_at: str = "",
    completed: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Asana task.

    Args:
        name: Task name.
        workspace_gid: Optional workspace GID; required unless project or parent supplies context.
        notes: Optional task notes.
        projects: Optional comma-separated project GIDs.
        parent: Optional parent task GID for a subtask.
        assignee: Optional assignee GID, email, or "me".
        due_on: Optional due date in YYYY-MM-DD format.
        due_at: Optional due datetime.
        completed: Optional completed flag.
    """
    if not name.strip():
        return "[Error]: name is required."
    body = _asana_task_body(
        name=name,
        notes=notes,
        workspace_gid=workspace_gid,
        projects=projects,
        parent=parent,
        assignee=assignee,
        due_on=due_on,
        due_at=due_at,
        completed=completed,
    )
    try:
        base_url, headers_or_error = _asana_config("asana_create_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/tasks",
            json_body=_asana_payload(body),
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_create_task failed", exc_info=True)
        return f"[Error]: Asana task creation failed: {e}"


@tool
def asana_create_subtask(
    parent_task_gid: str,
    name: str,
    notes: str = "",
    assignee: str = "",
    due_on: str = "",
    completed: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Asana subtask under a parent task.

    Args:
        parent_task_gid: Parent Asana task GID.
        name: Subtask name.
        notes: Optional subtask notes.
        assignee: Optional assignee GID, email, or "me".
        due_on: Optional due date in YYYY-MM-DD format.
        completed: Optional completed flag.
    """
    if not parent_task_gid.strip() or not name.strip():
        return "[Error]: parent_task_gid and name are required."
    try:
        base_url, headers_or_error = _asana_config("asana_create_subtask", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _asana_task_body(
            name=name,
            notes=notes,
            assignee=assignee,
            due_on=due_on,
            completed=completed,
        )
        data = _request_json(
            "POST",
            f"{base_url}/tasks/{quote(parent_task_gid.strip(), safe='')}/subtasks",
            json_body=_asana_payload(body),
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_create_subtask failed", exc_info=True)
        return f"[Error]: Asana subtask creation failed: {e}"


@tool
def asana_update_task(
    task_gid: str,
    name: str = "",
    notes: str = "",
    assignee: str = "",
    due_on: str = "",
    due_at: str = "",
    completed: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an Asana task.

    Args:
        task_gid: Asana task GID.
        name: Optional new task name.
        notes: Optional task notes.
        assignee: Optional assignee GID, email, or "me".
        due_on: Optional due date in YYYY-MM-DD format.
        due_at: Optional due datetime.
        completed: Optional completed flag.
    """
    task_gid = task_gid.strip()
    if not task_gid:
        return "[Error]: task_gid is required."
    body = _asana_task_body(
        name=name,
        notes=notes,
        assignee=assignee,
        due_on=due_on,
        due_at=due_at,
        completed=completed,
    )
    if not body:
        return "[Error]: provide at least one field to update."
    try:
        base_url, headers_or_error = _asana_config("asana_update_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/tasks/{quote(task_gid, safe='')}",
            json_body=_asana_payload(body),
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_update_task failed", exc_info=True)
        return f"[Error]: Asana task update failed: {e}"


@tool
def asana_add_task_comment(
    task_gid: str,
    text: str,
    is_html: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a comment/story to an Asana task.

    Args:
        task_gid: Asana task GID.
        text: Comment text or HTML.
        is_html: When true, send text as Asana html_text.
    """
    if not task_gid.strip() or not text.strip():
        return "[Error]: task_gid and text are required."
    try:
        base_url, headers_or_error = _asana_config("asana_add_task_comment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/tasks/{quote(task_gid.strip(), safe='')}/stories",
            json_body=_asana_payload({"html_text" if is_html else "text": text}),
            headers=headers_or_error,
        )
        return _dump_json(_asana_data(data))
    except Exception as e:
        logger.error("asana_add_task_comment failed", exc_info=True)
        return f"[Error]: Asana comment creation failed: {e}"


@tool
def linear_list_teams(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Linear teams.

    Args:
        limit: Number of teams to return, 1-100.
    """
    try:
        api_url, headers_or_error = _linear_config("linear_list_teams", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            """
            query Teams($first: Int) {
              teams(first: $first) {
                nodes { id key name description }
              }
            }
            """,
            {"first": _limit(limit)},
        )
        return _dump_json(_linear_nodes(data, "teams"))
    except Exception as e:
        logger.error("linear_list_teams failed", exc_info=True)
        return f"[Error]: Linear team list failed: {e}"


@tool
def linear_list_users(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Linear users.

    Args:
        limit: Number of users to return, 1-100.
    """
    try:
        api_url, headers_or_error = _linear_config("linear_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            """
            query Users($first: Int) {
              users(first: $first) {
                nodes { id name displayName email active }
              }
            }
            """,
            {"first": _limit(limit)},
        )
        return _dump_json(_linear_nodes(data, "users"))
    except Exception as e:
        logger.error("linear_list_users failed", exc_info=True)
        return f"[Error]: Linear user list failed: {e}"


@tool
def linear_list_workflow_states(
    team_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Linear workflow states, optionally filtered by team.

    Args:
        team_id: Optional Linear team ID.
        limit: Number of states to return, 1-100.
    """
    try:
        api_url, headers_or_error = _linear_config("linear_list_workflow_states", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        variables: dict[str, Any] = {"first": _limit(limit), "filter": None}
        if team_id.strip():
            variables["filter"] = {"team": {"id": {"eq": team_id.strip()}}}
        data = _linear_graphql(
            api_url,
            headers_or_error,
            """
            query WorkflowStates($first: Int, $filter: WorkflowStateFilter) {
              workflowStates(first: $first, filter: $filter) {
                nodes { id name type team { id key name } }
              }
            }
            """,
            variables,
        )
        return _dump_json(_linear_nodes(data, "workflowStates"))
    except Exception as e:
        logger.error("linear_list_workflow_states failed", exc_info=True)
        return f"[Error]: Linear workflow state list failed: {e}"


@tool
def linear_list_issues(
    team_id: str = "",
    assignee_id: str = "",
    state_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Linear issues with optional filters.

    Args:
        team_id: Optional team ID filter.
        assignee_id: Optional assignee ID filter.
        state_id: Optional workflow state ID filter.
        limit: Number of issues to return, 1-100.
    """
    try:
        api_url, headers_or_error = _linear_config("linear_list_issues", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        filter_data: dict[str, Any] = {}
        if team_id.strip():
            filter_data["team"] = {"id": {"eq": team_id.strip()}}
        if assignee_id.strip():
            filter_data["assignee"] = {"id": {"eq": assignee_id.strip()}}
        if state_id.strip():
            filter_data["state"] = {"id": {"eq": state_id.strip()}}
        data = _linear_graphql(
            api_url,
            headers_or_error,
            f"""
            query Issues($first: Int, $filter: IssueFilter) {{
              issues(first: $first, filter: $filter, orderBy: updatedAt) {{
                nodes {{ {_LINEAR_ISSUE_FIELDS} }}
              }}
            }}
            """,
            {"first": _limit(limit), "filter": filter_data or None},
        )
        return _dump_json(_linear_nodes(data, "issues"))
    except Exception as e:
        logger.error("linear_list_issues failed", exc_info=True)
        return f"[Error]: Linear issue list failed: {e}"


@tool
def linear_get_issue(
    issue_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Linear issue by ID or identifier.

    Args:
        issue_id: Linear issue UUID or identifier, such as ENG-123.
    """
    issue_id = issue_id.strip()
    if not issue_id:
        return "[Error]: issue_id is required."
    try:
        api_url, headers_or_error = _linear_config("linear_get_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            f"""
            query Issue($id: String!) {{
              issue(id: $id) {{ {_LINEAR_ISSUE_FIELDS} }}
            }}
            """,
            {"id": issue_id},
        )
        return _dump_json(data.get("issue") if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("linear_get_issue failed", exc_info=True)
        return f"[Error]: Linear issue lookup failed: {e}"


@tool
def linear_create_issue(
    team_id: str,
    title: str,
    description: str = "",
    assignee_id: str = "",
    state_id: str = "",
    priority: Optional[int] = None,
    due_date: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Linear issue.

    Args:
        team_id: Linear team ID.
        title: Issue title.
        description: Optional issue description.
        assignee_id: Optional assignee ID.
        state_id: Optional workflow state ID.
        priority: Optional priority number, 0-4.
        due_date: Optional due date in YYYY-MM-DD format.
    """
    if not team_id.strip() or not title.strip():
        return "[Error]: team_id and title are required."
    try:
        api_url, headers_or_error = _linear_config("linear_create_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            f"""
            mutation IssueCreate($input: IssueCreateInput!) {{
              issueCreate(input: $input) {{
                success
                issue {{ {_LINEAR_ISSUE_FIELDS} }}
              }}
            }}
            """,
            {
                "input": _filtered_params(
                    {
                        "teamId": team_id.strip(),
                        "title": title.strip(),
                        "description": description.strip(),
                        "assigneeId": assignee_id.strip(),
                        "stateId": state_id.strip(),
                        "priority": priority,
                        "dueDate": due_date.strip(),
                    }
                )
            },
        )
        return _dump_json(data.get("issueCreate") if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("linear_create_issue failed", exc_info=True)
        return f"[Error]: Linear issue creation failed: {e}"


@tool
def linear_update_issue(
    issue_id: str,
    title: str = "",
    description: str = "",
    assignee_id: str = "",
    state_id: str = "",
    team_id: str = "",
    priority: Optional[int] = None,
    due_date: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Linear issue.

    Args:
        issue_id: Linear issue UUID or identifier.
        title: Optional new issue title.
        description: Optional issue description.
        assignee_id: Optional assignee ID.
        state_id: Optional workflow state ID.
        team_id: Optional team ID.
        priority: Optional priority number, 0-4.
        due_date: Optional due date in YYYY-MM-DD format.
    """
    issue_id = issue_id.strip()
    if not issue_id:
        return "[Error]: issue_id is required."
    input_data = _filtered_params(
        {
            "title": title.strip(),
            "description": description.strip(),
            "assigneeId": assignee_id.strip(),
            "stateId": state_id.strip(),
            "teamId": team_id.strip(),
            "priority": priority,
            "dueDate": due_date.strip(),
        }
    )
    if not input_data:
        return "[Error]: provide at least one field to update."
    try:
        api_url, headers_or_error = _linear_config("linear_update_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            f"""
            mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {{
              issueUpdate(id: $id, input: $input) {{
                success
                issue {{ {_LINEAR_ISSUE_FIELDS} }}
              }}
            }}
            """,
            {"id": issue_id, "input": input_data},
        )
        return _dump_json(data.get("issueUpdate") if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("linear_update_issue failed", exc_info=True)
        return f"[Error]: Linear issue update failed: {e}"


@tool
def linear_add_issue_comment(
    issue_id: str,
    body: str,
    parent_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a comment to a Linear issue.

    Args:
        issue_id: Linear issue UUID or identifier.
        body: Comment body.
        parent_id: Optional parent comment ID for a reply.
    """
    if not issue_id.strip() or not body.strip():
        return "[Error]: issue_id and body are required."
    try:
        api_url, headers_or_error = _linear_config("linear_add_issue_comment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            """
            mutation CommentCreate($input: CommentCreateInput!) {
              commentCreate(input: $input) {
                success
                comment { id body createdAt user { id name displayName } }
              }
            }
            """,
            {
                "input": _filtered_params(
                    {
                        "issueId": issue_id.strip(),
                        "body": body.strip(),
                        "parentId": parent_id.strip(),
                    }
                )
            },
        )
        return _dump_json(data.get("commentCreate") if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("linear_add_issue_comment failed", exc_info=True)
        return f"[Error]: Linear comment creation failed: {e}"


@tool
def linear_add_issue_link(
    issue_id: str,
    url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Attach a URL link to a Linear issue.

    Args:
        issue_id: Linear issue UUID or identifier.
        url: URL to attach.
    """
    if not issue_id.strip() or not url.strip():
        return "[Error]: issue_id and url are required."
    try:
        api_url, headers_or_error = _linear_config("linear_add_issue_link", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _linear_graphql(
            api_url,
            headers_or_error,
            """
            mutation AttachmentLinkURL($issueId: String!, $url: String!) {
              attachmentLinkURL(issueId: $issueId, url: $url) { success }
            }
            """,
            {"issueId": issue_id.strip(), "url": url.strip()},
        )
        return _dump_json(data.get("attachmentLinkURL") if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("linear_add_issue_link failed", exc_info=True)
        return f"[Error]: Linear issue link creation failed: {e}"


WORK_TRACKING_SERVICE_TOOLS = [
    asana_get_user,
    asana_list_users,
    asana_list_projects,
    asana_get_project,
    asana_create_project,
    asana_update_project,
    asana_list_tasks,
    asana_search_tasks,
    asana_get_task,
    asana_create_task,
    asana_create_subtask,
    asana_update_task,
    asana_add_task_comment,
    linear_list_teams,
    linear_list_users,
    linear_list_workflow_states,
    linear_list_issues,
    linear_get_issue,
    linear_create_issue,
    linear_update_issue,
    linear_add_issue_comment,
    linear_add_issue_link,
]
