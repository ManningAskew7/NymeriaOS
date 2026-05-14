"""Project management service integration tools."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


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


def _filtered_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != [] and value != {}
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
            provider="jira",
            provider_aliases=("atlassian", "jira_api", "jira_software_cloud_api"),
            field_names=("base_url", "domain", "url", "site_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("jira_base_url")
    )
    access_token = _credential_value(
        provider="jira",
        provider_aliases=("atlassian", "jira_api", "jira_software_cloud_api"),
        field_names=("access_token", "bearer_token", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jira_access_token")
    email = _credential_value(
        provider="jira",
        provider_aliases=("atlassian", "jira_api", "jira_software_cloud_api"),
        field_names=("email", "username", "user"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jira_email")
    api_token = _credential_value(
        provider="jira",
        provider_aliases=("atlassian", "jira_api", "jira_software_cloud_api"),
        field_names=("api_token", "apiToken", "password"),
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
        provider="jira",
        field_names=("email", "api_token", "base_url"),
        tool_name=tool_name,
        env_var="JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_BASE_URL",
        display_name="Jira",
    )


def _clickup_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="clickup",
            provider_aliases=("click_up", "clickup_api", "click_up_api"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("clickup_base_url")
        or _CLICKUP_BASE_URL
    )
    token = _credential_value(
        provider="clickup",
        provider_aliases=("click_up", "clickup_api", "click_up_api"),
        field_names=("access_token", "api_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("clickup_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="clickup",
            field_names=("access_token", "api_key", "value"),
            tool_name=tool_name,
            env_var="CLICKUP_ACCESS_TOKEN",
            display_name="ClickUp",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": token,
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
]
