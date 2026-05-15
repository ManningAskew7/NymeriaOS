"""Microsoft Graph productivity and collaboration service tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_GRAPH_PROVIDER = "microsoft_graph"
_GRAPH_ALIASES = (
    "microsoft",
    "microsoft_graph_api",
    "microsoftGraph",
    "microsoftGraphApi",
    "graph",
    "onedrive",
    "microsoft_onedrive",
    "microsoft_todo",
    "microsoft_teams",
)
_TOKEN_FIELDS = ("access_token", "accessToken", "token", "bearer_token", "bearerToken", "value")
_BASE_FIELDS = ("base_url", "baseUrl", "url", "api_url", "apiUrl")
_TASK_STATUS_VALUES = {
    "notstarted": "notStarted",
    "not_started": "notStarted",
    "not-started": "notStarted",
    "notStarted": "notStarted",
    "inprogress": "inProgress",
    "in_progress": "inProgress",
    "in-progress": "inProgress",
    "inProgress": "inProgress",
    "completed": "completed",
    "waitingonothers": "waitingOnOthers",
    "waiting_on_others": "waitingOnOthers",
    "waiting-on-others": "waitingOnOthers",
    "waitingOnOthers": "waitingOnOthers",
    "deferred": "deferred",
}


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _filtered(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
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
    json_body: Any = None,
    content: str | bytes | None = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params) if params is not None else None,
                json=json_body if content is None else None,
                content=content,
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


def _graph_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_GRAPH_PROVIDER,
            provider_aliases=_GRAPH_ALIASES,
            field_names=_BASE_FIELDS,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("microsoft_graph_base_url")
        or _GRAPH_BASE_URL
    )
    token = _credential_value(
        provider=_GRAPH_PROVIDER,
        provider_aliases=_GRAPH_ALIASES,
        field_names=_TOKEN_FIELDS,
        tool_name=tool_name,
        config=config,
    ) or _settings_value("microsoft_graph_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_GRAPH_PROVIDER,
            field_names=_TOKEN_FIELDS,
            tool_name=tool_name,
            env_var="MICROSOFT_GRAPH_ACCESS_TOKEN",
            display_name="Microsoft Graph",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _graph_request(
    tool_name: str,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    content: str | bytes | None = None,
    content_type: str = "",
    config: Optional[RunnableConfig] = None,
) -> Any:
    base, headers_or_error = _graph_config(tool_name, config)
    if isinstance(headers_or_error, str):
        return headers_or_error
    headers = dict(headers_or_error)
    if content is not None:
        headers["Content-Type"] = content_type or "application/octet-stream"
    return _request_json(
        method,
        f"{base}/{path.strip('/')}",
        params=params,
        json_body=json_body,
        content=content,
        headers=headers,
    )


def _graph_path_id(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is required.")
    return quote(value, safe="")


def _drive_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("path is required.")
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return quote(cleaned, safe="/")


def _onedrive_item_path(*, path: str = "", item_id: str = "", drive_id: str = "") -> str:
    if drive_id.strip() and item_id.strip():
        return f"drives/{_graph_path_id(drive_id, 'drive_id')}/items/{_graph_path_id(item_id, 'item_id')}"
    if item_id.strip():
        return f"me/drive/items/{_graph_path_id(item_id, 'item_id')}"
    if path.strip():
        return f"me/drive/root:{_drive_path(path)}:"
    return "me/drive/root"


def _response_value(data: Any, *, limit: int) -> Any:
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        return data["value"][:limit]
    return data


def _task_status_filter(status: str) -> str:
    if not status.strip():
        return ""
    normalized = _TASK_STATUS_VALUES.get(status.strip(), _TASK_STATUS_VALUES.get(status.strip().lower()))
    if not normalized:
        allowed = ", ".join(sorted(set(_TASK_STATUS_VALUES.values())))
        raise ValueError(f"status must be one of: {allowed}")
    return f"status eq '{normalized}'"


def _importance_value(value: str) -> str:
    if not value.strip():
        return ""
    normalized = value.strip().lower()
    if normalized not in {"low", "normal", "high"}:
        raise ValueError("importance must be low, normal, or high.")
    return normalized


@tool
def microsoft_todo_list_task_lists(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Microsoft To Do task lists for the signed-in user."""
    try:
        data = _graph_request(
            "microsoft_todo_list_task_lists",
            "GET",
            "me/todo/lists",
            params={"$top": _limit(limit, max_value=100)},
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=_limit(limit, max_value=100)))
    except Exception as e:
        logger.error("microsoft_todo_list_task_lists failed", exc_info=True)
        return f"[Error]: Microsoft To Do task-list lookup failed: {e}"


@tool
def microsoft_todo_list_tasks(
    list_id: str,
    status: str = "",
    top: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List tasks from a Microsoft To Do list."""
    if not list_id.strip():
        return "[Error]: list_id is required."
    try:
        limit = _limit(top, max_value=100)
        params = {"$top": limit}
        filter_value = _task_status_filter(status)
        if filter_value:
            params["$filter"] = filter_value
        data = _graph_request(
            "microsoft_todo_list_tasks",
            "GET",
            f"me/todo/lists/{_graph_path_id(list_id, 'list_id')}/tasks",
            params=params,
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=limit))
    except Exception as e:
        logger.error("microsoft_todo_list_tasks failed", exc_info=True)
        return f"[Error]: Microsoft To Do task lookup failed: {e}"


@tool
def microsoft_todo_create_task(
    list_id: str,
    title: str,
    body: str = "",
    due_date_time: str = "",
    timezone: str = "UTC",
    importance: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Microsoft To Do task."""
    if not list_id.strip() or not title.strip():
        return "[Error]: list_id and title are required."
    try:
        payload: dict[str, Any] = {"title": title.strip()}
        if body.strip():
            payload["body"] = {"content": body, "contentType": "text"}
        if due_date_time.strip():
            payload["dueDateTime"] = {
                "dateTime": due_date_time.strip(),
                "timeZone": timezone.strip() or "UTC",
            }
        importance_value = _importance_value(importance)
        if importance_value:
            payload["importance"] = importance_value
        data = _graph_request(
            "microsoft_todo_create_task",
            "POST",
            f"me/todo/lists/{_graph_path_id(list_id, 'list_id')}/tasks",
            json_body=payload,
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(data)
    except Exception as e:
        logger.error("microsoft_todo_create_task failed", exc_info=True)
        return f"[Error]: Microsoft To Do task creation failed: {e}"


@tool
def microsoft_todo_update_task(
    list_id: str,
    task_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Microsoft To Do task with a JSON object of Graph task fields."""
    if not list_id.strip() or not task_id.strip():
        return "[Error]: list_id and task_id are required."
    try:
        payload = _parse_json(fields_json, expected=dict, label="fields_json")
        if not payload:
            return "[Error]: fields_json must include at least one field."
        data = _graph_request(
            "microsoft_todo_update_task",
            "PATCH",
            f"me/todo/lists/{_graph_path_id(list_id, 'list_id')}/tasks/{_graph_path_id(task_id, 'task_id')}",
            json_body=payload,
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(data)
    except Exception as e:
        logger.error("microsoft_todo_update_task failed", exc_info=True)
        return f"[Error]: Microsoft To Do task update failed: {e}"


@tool
def microsoft_onedrive_list_children(
    path: str = "",
    item_id: str = "",
    drive_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List OneDrive child files and folders by path, item ID, or drive item."""
    try:
        max_items = _limit(limit, max_value=200)
        endpoint = f"{_onedrive_item_path(path=path, item_id=item_id, drive_id=drive_id)}/children"
        data = _graph_request(
            "microsoft_onedrive_list_children",
            "GET",
            endpoint,
            params={"$top": max_items},
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=max_items))
    except Exception as e:
        logger.error("microsoft_onedrive_list_children failed", exc_info=True)
        return f"[Error]: OneDrive child listing failed: {e}"


@tool
def microsoft_onedrive_get_item(
    path: str = "",
    item_id: str = "",
    drive_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get OneDrive file or folder metadata by path, item ID, or drive item."""
    try:
        data = _graph_request(
            "microsoft_onedrive_get_item",
            "GET",
            _onedrive_item_path(path=path, item_id=item_id, drive_id=drive_id),
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(data)
    except Exception as e:
        logger.error("microsoft_onedrive_get_item failed", exc_info=True)
        return f"[Error]: OneDrive item lookup failed: {e}"


@tool
def microsoft_onedrive_search(
    query: str,
    drive_id: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search OneDrive files and folders."""
    if not query.strip():
        return "[Error]: query is required."
    try:
        max_items = _limit(limit, max_value=200)
        q = quote(query.strip().replace("'", "''"), safe="")
        root = f"drives/{_graph_path_id(drive_id, 'drive_id')}/root" if drive_id.strip() else "me/drive/root"
        data = _graph_request(
            "microsoft_onedrive_search",
            "GET",
            f"{root}/search(q='{q}')",
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=max_items))
    except Exception as e:
        logger.error("microsoft_onedrive_search failed", exc_info=True)
        return f"[Error]: OneDrive search failed: {e}"


@tool
def microsoft_onedrive_upload_text_file(
    path: str,
    content: str,
    conflict_behavior: str = "replace",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload or replace a UTF-8 text file in OneDrive."""
    if not path.strip():
        return "[Error]: path is required."
    try:
        behavior = conflict_behavior.strip().lower() or "replace"
        if behavior not in {"fail", "replace", "rename"}:
            return "[Error]: conflict_behavior must be fail, replace, or rename."
        data = _graph_request(
            "microsoft_onedrive_upload_text_file",
            "PUT",
            f"me/drive/root:{_drive_path(path)}:/content",
            params={"@microsoft.graph.conflictBehavior": behavior},
            content=content,
            content_type="text/plain; charset=utf-8",
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(data)
    except Exception as e:
        logger.error("microsoft_onedrive_upload_text_file failed", exc_info=True)
        return f"[Error]: OneDrive text upload failed: {e}"


@tool
def microsoft_teams_list_joined_teams(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Microsoft Teams teams joined by the signed-in user."""
    try:
        max_items = _limit(limit, max_value=100)
        data = _graph_request(
            "microsoft_teams_list_joined_teams",
            "GET",
            "me/joinedTeams",
            params={"$top": max_items},
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=max_items))
    except Exception as e:
        logger.error("microsoft_teams_list_joined_teams failed", exc_info=True)
        return f"[Error]: Microsoft Teams joined-team listing failed: {e}"


@tool
def microsoft_teams_list_channels(
    team_id: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List channels in a Microsoft Teams team."""
    if not team_id.strip():
        return "[Error]: team_id is required."
    try:
        max_items = _limit(limit, max_value=100)
        data = _graph_request(
            "microsoft_teams_list_channels",
            "GET",
            f"teams/{_graph_path_id(team_id, 'team_id')}/channels",
            params={"$top": max_items},
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=max_items))
    except Exception as e:
        logger.error("microsoft_teams_list_channels failed", exc_info=True)
        return f"[Error]: Microsoft Teams channel listing failed: {e}"


@tool
def microsoft_teams_list_channel_messages(
    team_id: str,
    channel_id: str,
    top: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List recent messages from a Microsoft Teams channel."""
    if not team_id.strip() or not channel_id.strip():
        return "[Error]: team_id and channel_id are required."
    try:
        max_items = _limit(top, max_value=100)
        data = _graph_request(
            "microsoft_teams_list_channel_messages",
            "GET",
            f"teams/{_graph_path_id(team_id, 'team_id')}/channels/{_graph_path_id(channel_id, 'channel_id')}/messages",
            params={"$top": max_items},
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(_response_value(data, limit=max_items))
    except Exception as e:
        logger.error("microsoft_teams_list_channel_messages failed", exc_info=True)
        return f"[Error]: Microsoft Teams channel message listing failed: {e}"


@tool
def microsoft_teams_send_channel_message(
    team_id: str,
    channel_id: str,
    content: str,
    content_type: str = "text",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Microsoft Teams channel message."""
    if not team_id.strip() or not channel_id.strip() or not content.strip():
        return "[Error]: team_id, channel_id, and content are required."
    try:
        normalized_type = content_type.strip().lower() or "text"
        if normalized_type not in {"text", "html"}:
            return "[Error]: content_type must be text or html."
        body = {"body": {"contentType": normalized_type, "content": content}}
        data = _graph_request(
            "microsoft_teams_send_channel_message",
            "POST",
            f"teams/{_graph_path_id(team_id, 'team_id')}/channels/{_graph_path_id(channel_id, 'channel_id')}/messages",
            json_body=body,
            config=config,
        )
        return data if isinstance(data, str) and data.startswith("[Error]:") else _dump_json(data)
    except Exception as e:
        logger.error("microsoft_teams_send_channel_message failed", exc_info=True)
        return f"[Error]: Microsoft Teams channel message send failed: {e}"


MICROSOFT_GRAPH_SERVICE_TOOLS = [
    microsoft_todo_list_task_lists,
    microsoft_todo_list_tasks,
    microsoft_todo_create_task,
    microsoft_todo_update_task,
    microsoft_onedrive_list_children,
    microsoft_onedrive_get_item,
    microsoft_onedrive_search,
    microsoft_onedrive_upload_text_file,
    microsoft_teams_list_joined_teams,
    microsoft_teams_list_channels,
    microsoft_teams_list_channel_messages,
    microsoft_teams_send_channel_message,
]
