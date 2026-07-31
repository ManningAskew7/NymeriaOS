"""Productivity service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

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
    BASE_URL_FIELDS,
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    request_with_policy as _request_with_policy,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_TODOIST_BASE_URL = "https://api.todoist.com/api/v1"
_TRELLO_BASE_URL = "https://api.trello.com/1"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_TODOIST = register_provider_spec(
    ProviderCredentialSpec(
        provider="todoist",
        aliases=("todoist_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=BASE_URL_FIELDS, required=False),
            CredentialFieldGroup(
                role="token", names=("api_key", "access_token", "token", "value")
            ),
        ),
        hint_fields=("api_key", "access_token", "value"),
        env_var="TODOIST_API_KEY",
        display_name="Todoist",
    )
)

_TRELLO = register_provider_spec(
    ProviderCredentialSpec(
        provider="trello",
        aliases=("trello_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=BASE_URL_FIELDS, required=False),
            CredentialFieldGroup(role="api_key", names=("api_key", "key")),
            CredentialFieldGroup(role="api_token", names=("api_token", "token", "value")),
        ),
        hint_fields=("api_key", "api_token"),
        env_var="TRELLO_API_KEY and TRELLO_API_TOKEN",
        display_name="Trello",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _csv(value: str) -> str:
    return ",".join(_split_csv(value))


def _filtered_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != []
    }


def _limit(value: int, *, default: int = 20, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


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
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(
                client,
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
            detail = (
                body.get("error")
                or body.get("message")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _todoist_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_TODOIST.provider,
            provider_aliases=_TODOIST.aliases,
            field_names=_TODOIST.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("todoist_base_url")
        or _TODOIST_BASE_URL
    )
    token = _credential_value(
        provider=_TODOIST.provider,
        provider_aliases=_TODOIST.aliases,
        field_names=_TODOIST.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("todoist_api_key")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_TODOIST.provider,
            field_names=_TODOIST.hint_fields,
            tool_name=tool_name,
            env_var=_TODOIST.env_var,
            display_name=_TODOIST.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _trello_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_TRELLO.provider,
            provider_aliases=_TRELLO.aliases,
            field_names=_TRELLO.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("trello_base_url")
        or _TRELLO_BASE_URL
    )
    key = _credential_value(
        provider=_TRELLO.provider,
        provider_aliases=_TRELLO.aliases,
        field_names=_TRELLO.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("trello_api_key")
    token = _credential_value(
        provider=_TRELLO.provider,
        provider_aliases=_TRELLO.aliases,
        field_names=_TRELLO.group("api_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("trello_api_token")
    if not key or not token:
        return _base_url(base), _setup_hint(
            provider=_TRELLO.provider,
            field_names=_TRELLO.hint_fields,
            tool_name=tool_name,
            env_var=_TRELLO.env_var,
            display_name=_TRELLO.display_name,
        )
    return _base_url(base), {"key": key, "token": token}


def _todoist_results(data: Any) -> Any:
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data


def _todoist_task_body(
    *,
    content: str = "",
    description: str = "",
    project_id: str = "",
    section_id: str = "",
    parent_id: str = "",
    labels: str = "",
    priority: Optional[int] = None,
    due_string: str = "",
    due_date: str = "",
    due_datetime: str = "",
    due_lang: str = "",
) -> dict[str, Any]:
    return _filtered_params(
        {
            "content": content.strip(),
            "description": description.strip(),
            "project_id": project_id.strip(),
            "section_id": section_id.strip(),
            "parent_id": parent_id.strip(),
            "labels": _split_csv(labels),
            "priority": priority,
            "due_string": due_string.strip(),
            "due_date": due_date.strip(),
            "due_datetime": due_datetime.strip(),
            "due_lang": due_lang.strip(),
        }
    )


@tool
def todoist_list_tasks(
    project_id: str = "",
    section_id: str = "",
    label: str = "",
    filter_query: str = "",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Todoist tasks.

    Args:
        project_id: Optional Todoist project ID filter.
        section_id: Optional Todoist section ID filter.
        label: Optional label name filter.
        filter_query: Optional Todoist filter query.
        limit: Number of tasks to return, 1-100.
    """
    try:
        base_url, headers_or_error = _todoist_config("todoist_list_tasks", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"{base_url}/tasks"
        params: dict[str, Any] = {
            "project_id": project_id.strip(),
            "section_id": section_id.strip(),
            "label": label.strip(),
            "limit": _limit(limit, max_value=100),
        }
        if filter_query.strip():
            endpoint = f"{base_url}/tasks/filter"
            params = {
                "query": filter_query.strip(),
                "limit": _limit(limit, max_value=100),
            }
        data = _request_json(
            "GET",
            endpoint,
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(_todoist_results(data))
    except Exception as e:
        logger.error("todoist_list_tasks failed", exc_info=True)
        return f"[Error]: Todoist task list failed: {e}"


@tool
def todoist_get_task(
    task_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Todoist task by ID.

    Args:
        task_id: Todoist task ID.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    try:
        base_url, headers_or_error = _todoist_config("todoist_get_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(
            _request_json("GET", f"{base_url}/tasks/{quote(task_id, safe='')}", headers=headers_or_error)
        )
    except Exception as e:
        logger.error("todoist_get_task failed", exc_info=True)
        return f"[Error]: Todoist task lookup failed: {e}"


@tool
def todoist_create_task(
    content: str,
    description: str = "",
    project_id: str = "",
    section_id: str = "",
    parent_id: str = "",
    labels: str = "",
    priority: Optional[int] = None,
    due_string: str = "",
    due_date: str = "",
    due_datetime: str = "",
    due_lang: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Todoist task.

    Args:
        content: Task content.
        description: Optional task description.
        project_id: Optional project ID.
        section_id: Optional section ID.
        parent_id: Optional parent task ID.
        labels: Optional comma-separated labels.
        priority: Optional priority 1-4.
        due_string: Optional natural-language due string.
        due_date: Optional due date in YYYY-MM-DD format.
        due_datetime: Optional due datetime.
        due_lang: Optional due-string language.
    """
    if not content.strip():
        return "[Error]: content is required."
    try:
        base_url, headers_or_error = _todoist_config("todoist_create_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _todoist_task_body(
            content=content,
            description=description,
            project_id=project_id,
            section_id=section_id,
            parent_id=parent_id,
            labels=labels,
            priority=priority,
            due_string=due_string,
            due_date=due_date,
            due_datetime=due_datetime,
            due_lang=due_lang,
        )
        data = _request_json("POST", f"{base_url}/tasks", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("todoist_create_task failed", exc_info=True)
        return f"[Error]: Todoist task creation failed: {e}"


@tool
def todoist_update_task(
    task_id: str,
    content: str = "",
    description: str = "",
    labels: str = "",
    priority: Optional[int] = None,
    due_string: str = "",
    due_date: str = "",
    due_datetime: str = "",
    due_lang: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Todoist task.

    Args:
        task_id: Todoist task ID.
        content: Optional new task content.
        description: Optional task description.
        labels: Optional comma-separated labels.
        priority: Optional priority 1-4.
        due_string: Optional natural-language due string.
        due_date: Optional due date in YYYY-MM-DD format.
        due_datetime: Optional due datetime.
        due_lang: Optional due-string language.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    try:
        base_url, headers_or_error = _todoist_config("todoist_update_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _todoist_task_body(
            content=content,
            description=description,
            labels=labels,
            priority=priority,
            due_string=due_string,
            due_date=due_date,
            due_datetime=due_datetime,
            due_lang=due_lang,
        )
        if not body:
            return "[Error]: provide at least one field to update."
        data = _request_json(
            "POST",
            f"{base_url}/tasks/{quote(task_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("todoist_update_task failed", exc_info=True)
        return f"[Error]: Todoist task update failed: {e}"


@tool
def todoist_close_task(
    task_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Close a Todoist task.

    Args:
        task_id: Todoist task ID.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    try:
        base_url, headers_or_error = _todoist_config("todoist_close_task", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/tasks/{quote(task_id, safe='')}/close",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("todoist_close_task failed", exc_info=True)
        return f"[Error]: Todoist task close failed: {e}"


@tool
def todoist_list_projects(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Todoist projects.

    Args:
        limit: Number of projects to return, 1-100.
    """
    try:
        base_url, headers_or_error = _todoist_config("todoist_list_projects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/projects",
            params={"limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(_todoist_results(data))
    except Exception as e:
        logger.error("todoist_list_projects failed", exc_info=True)
        return f"[Error]: Todoist project list failed: {e}"


@tool
def todoist_get_project(
    project_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Todoist project by ID.

    Args:
        project_id: Todoist project ID.
    """
    project_id = project_id.strip()
    if not project_id:
        return "[Error]: project_id is required."
    try:
        base_url, headers_or_error = _todoist_config("todoist_get_project", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/projects/{quote(project_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("todoist_get_project failed", exc_info=True)
        return f"[Error]: Todoist project lookup failed: {e}"


@tool
def todoist_create_project(
    name: str,
    color: str = "",
    parent_id: str = "",
    is_favorite: Optional[bool] = None,
    view_style: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Todoist project.

    Args:
        name: Project name.
        color: Optional Todoist color name.
        parent_id: Optional parent project ID.
        is_favorite: Optional favorite flag.
        view_style: Optional view style, e.g. "list" or "board".
    """
    if not name.strip():
        return "[Error]: name is required."
    try:
        base_url, headers_or_error = _todoist_config("todoist_create_project", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params(
            {
                "name": name.strip(),
                "color": color.strip(),
                "parent_id": parent_id.strip(),
                "is_favorite": is_favorite,
                "view_style": view_style.strip(),
            }
        )
        data = _request_json("POST", f"{base_url}/projects", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("todoist_create_project failed", exc_info=True)
        return f"[Error]: Todoist project creation failed: {e}"


@tool
def trello_search(
    query: str,
    model_types: str = "boards,cards",
    limit: int = 10,
    partial: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Trello boards and cards.

    Args:
        query: Search query.
        model_types: Comma-separated model types, e.g. "boards,cards".
        limit: Number of boards/cards to return per model type, 1-50.
        partial: Enable partial word search.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, auth_or_error = _trello_config("trello_search", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        item_limit = _limit(limit, max_value=50)
        params = {
            **auth_or_error,
            "query": query.strip(),
            "modelTypes": _csv(model_types) or "boards,cards",
            "partial": str(partial).lower(),
            "boards_limit": item_limit,
            "cards_limit": item_limit,
        }
        data = _request_json("GET", f"{base_url}/search", params=params)
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_search failed", exc_info=True)
        return f"[Error]: Trello search failed: {e}"


@tool
def trello_get_board(
    board_id: str,
    fields: str = "name,desc,url,closed,dateLastActivity",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Trello board metadata.

    Args:
        board_id: Trello board ID.
        fields: Comma-separated fields to return.
    """
    board_id = board_id.strip()
    if not board_id:
        return "[Error]: board_id is required."
    try:
        base_url, auth_or_error = _trello_config("trello_get_board", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        data = _request_json(
            "GET",
            f"{base_url}/boards/{quote(board_id, safe='')}",
            params={**auth_or_error, "fields": _csv(fields) or "name,desc,url"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_get_board failed", exc_info=True)
        return f"[Error]: Trello board lookup failed: {e}"


@tool
def trello_list_board_lists(
    board_id: str,
    filter_value: str = "open",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Trello lists on a board.

    Args:
        board_id: Trello board ID.
        filter_value: List filter, e.g. "open", "closed", or "all".
        limit: Number of lists to return, 1-100.
    """
    board_id = board_id.strip()
    if not board_id:
        return "[Error]: board_id is required."
    try:
        base_url, auth_or_error = _trello_config("trello_list_board_lists", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        data = _request_json(
            "GET",
            f"{base_url}/boards/{quote(board_id, safe='')}/lists",
            params={**auth_or_error, "filter": filter_value.strip() or "open", "limit": _limit(limit)},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_list_board_lists failed", exc_info=True)
        return f"[Error]: Trello list lookup failed: {e}"


@tool
def trello_list_cards(
    list_id: str,
    filter_value: str = "open",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Trello cards in a list.

    Args:
        list_id: Trello list ID.
        filter_value: Card filter, e.g. "open", "closed", or "all".
        limit: Number of cards to return, 1-100.
    """
    list_id = list_id.strip()
    if not list_id:
        return "[Error]: list_id is required."
    try:
        base_url, auth_or_error = _trello_config("trello_list_cards", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        data = _request_json(
            "GET",
            f"{base_url}/lists/{quote(list_id, safe='')}/cards",
            params={**auth_or_error, "filter": filter_value.strip() or "open", "limit": _limit(limit)},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_list_cards failed", exc_info=True)
        return f"[Error]: Trello card list failed: {e}"


@tool
def trello_get_card(
    card_id: str,
    fields: str = "name,desc,url,closed,due,idList,idBoard,labels,dateLastActivity",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Trello card metadata.

    Args:
        card_id: Trello card ID or shortlink.
        fields: Comma-separated fields to return.
    """
    card_id = card_id.strip()
    if not card_id:
        return "[Error]: card_id is required."
    try:
        base_url, auth_or_error = _trello_config("trello_get_card", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        data = _request_json(
            "GET",
            f"{base_url}/cards/{quote(card_id, safe='')}",
            params={**auth_or_error, "fields": _csv(fields) or "name,desc,url"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_get_card failed", exc_info=True)
        return f"[Error]: Trello card lookup failed: {e}"


@tool
def trello_create_card(
    list_id: str,
    name: str,
    description: str = "",
    due: str = "",
    labels: str = "",
    position: str = "bottom",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Trello card.

    Args:
        list_id: Destination Trello list ID.
        name: Card name.
        description: Optional card description.
        due: Optional due date/datetime.
        labels: Optional comma-separated label IDs.
        position: Card position, e.g. "top", "bottom", or a number.
    """
    if not list_id.strip() or not name.strip():
        return "[Error]: list_id and name are required."
    try:
        base_url, auth_or_error = _trello_config("trello_create_card", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        body = _filtered_params(
            {
                "idList": list_id.strip(),
                "name": name.strip(),
                "desc": description.strip(),
                "due": due.strip(),
                "idLabels": _csv(labels),
                "pos": position.strip() or "bottom",
            }
        )
        data = _request_json("POST", f"{base_url}/cards", params=auth_or_error, json_body=body)
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_create_card failed", exc_info=True)
        return f"[Error]: Trello card creation failed: {e}"


@tool
def trello_update_card(
    card_id: str,
    name: str = "",
    description: str = "",
    list_id: str = "",
    due: str = "",
    closed: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Trello card.

    Args:
        card_id: Trello card ID or shortlink.
        name: Optional new card name.
        description: Optional new card description.
        list_id: Optional destination list ID.
        due: Optional due date/datetime.
        closed: Optional closed/archive flag.
    """
    card_id = card_id.strip()
    if not card_id:
        return "[Error]: card_id is required."
    try:
        base_url, auth_or_error = _trello_config("trello_update_card", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        body = _filtered_params(
            {
                "name": name.strip(),
                "desc": description.strip(),
                "idList": list_id.strip(),
                "due": due.strip(),
                "closed": closed,
            }
        )
        if not body:
            return "[Error]: provide at least one field to update."
        data = _request_json(
            "PUT",
            f"{base_url}/cards/{quote(card_id, safe='')}",
            params=auth_or_error,
            json_body=body,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_update_card failed", exc_info=True)
        return f"[Error]: Trello card update failed: {e}"


@tool
def trello_add_card_comment(
    card_id: str,
    text: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a comment to a Trello card.

    Args:
        card_id: Trello card ID or shortlink.
        text: Comment text.
    """
    if not card_id.strip() or not text.strip():
        return "[Error]: card_id and text are required."
    try:
        base_url, auth_or_error = _trello_config("trello_add_card_comment", config)
        if isinstance(auth_or_error, str):
            return auth_or_error
        data = _request_json(
            "POST",
            f"{base_url}/cards/{quote(card_id.strip(), safe='')}/actions/comments",
            params=auth_or_error,
            json_body={"text": text},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("trello_add_card_comment failed", exc_info=True)
        return f"[Error]: Trello card comment failed: {e}"


PRODUCTIVITY_SERVICE_TOOLS = [
    todoist_list_tasks,
    todoist_get_task,
    todoist_create_task,
    todoist_update_task,
    todoist_close_task,
    todoist_list_projects,
    todoist_get_project,
    todoist_create_project,
    trello_search,
    trello_get_board,
    trello_list_board_lists,
    trello_list_cards,
    trello_get_card,
    trello_create_card,
    trello_update_card,
    trello_add_card_comment,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="productivity", tools=tuple(PRODUCTIVITY_SERVICE_TOOLS)))
