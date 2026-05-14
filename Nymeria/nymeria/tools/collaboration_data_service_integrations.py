"""Collaboration and data service integration tools."""

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
_SLACK_BASE_URL = "https://slack.com/api"
_NOTION_BASE_URL = "https://api.notion.com/v1"
_NOTION_VERSION = "2026-03-11"
_AIRTABLE_BASE_URL = "https://api.airtable.com/v0"


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


def _parse_json(value: str, *, expected: type, label: str) -> Any:
    if not value.strip():
        return [] if expected is list else {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} must be valid JSON: {e}") from e
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} must be a JSON {expected.__name__}.")
    return parsed


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    slack_ok: bool = False,
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
            data = response.json()
            if slack_ok and isinstance(data, dict) and data.get("ok") is False:
                raise RuntimeError(str(data.get("error") or data))
            return data
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = (
                body.get("message")
                or body.get("error")
                or body.get("error_description")
                or body.get("detail")
                or ""
            )
            if not detail and isinstance(body.get("errors"), list):
                detail = "; ".join(str(item) for item in body["errors"])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _slack_endpoint(base_url: str, method_name: str) -> str:
    clean = _base_url(base_url)
    if clean.endswith("/api"):
        return f"{clean}/{method_name}"
    return f"{clean}/api/{method_name}"


def _slack_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="slack",
            provider_aliases=("slack_api", "slack_oauth2"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("slack_base_url")
        or _SLACK_BASE_URL
    )
    token = (
        _credential_value(
            provider="slack",
            provider_aliases=("slack_api", "slack_oauth2"),
            field_names=("bot_token", "access_token", "token", "value"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("slack_bot_token")
        or _settings_value("slack_access_token")
    )
    if not token:
        return _base_url(base), _setup_hint(
            provider="slack",
            field_names=("bot_token", "access_token", "token", "value"),
            tool_name=tool_name,
            env_var="SLACK_BOT_TOKEN or SLACK_ACCESS_TOKEN",
            display_name="Slack",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Nymeria",
    }


def _notion_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="notion",
            provider_aliases=("notion_api", "notion_oauth2"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("notion_base_url")
        or _NOTION_BASE_URL
    )
    token = _credential_value(
        provider="notion",
        provider_aliases=("notion_api", "notion_oauth2"),
        field_names=("api_key", "access_token", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("notion_api_key")
    version = (
        _credential_value(
            provider="notion",
            provider_aliases=("notion_api", "notion_oauth2"),
            field_names=("notion_version", "version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("notion_version")
        or _NOTION_VERSION
    )
    if not token:
        return _base_url(base), _setup_hint(
            provider="notion",
            field_names=("api_key", "access_token", "token", "value"),
            tool_name=tool_name,
            env_var="NOTION_API_KEY",
            display_name="Notion",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": version,
        "User-Agent": "Nymeria",
    }


def _airtable_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="airtable",
            provider_aliases=("airtable_api", "airtable_token_api", "airtable_oauth2"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("airtable_base_url")
        or _AIRTABLE_BASE_URL
    )
    token = (
        _credential_value(
            provider="airtable",
            provider_aliases=("airtable_api", "airtable_token_api", "airtable_oauth2"),
            field_names=("access_token", "api_key", "token", "value"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("airtable_access_token")
        or _settings_value("airtable_api_key")
    )
    if not token:
        return _base_url(base), _setup_hint(
            provider="airtable",
            field_names=("access_token", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="AIRTABLE_ACCESS_TOKEN or AIRTABLE_API_KEY",
            display_name="Airtable",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


@tool
def slack_list_channels(
    types: str = "public_channel,private_channel",
    exclude_archived: bool = True,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Slack conversations visible to the credential.

    Args:
        types: Comma-separated Slack conversation types.
        exclude_archived: Exclude archived conversations.
        limit: Number of conversations to return, 1-200.
    """
    try:
        base_url, headers_or_error = _slack_config("slack_list_channels", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _slack_endpoint(base_url, "conversations.list"),
            params={
                "types": ",".join(_split_csv(types)),
                "exclude_archived": str(bool(exclude_archived)).lower(),
                "limit": _limit(limit, default=100, max_value=200),
            },
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data.get("channels", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("slack_list_channels failed", exc_info=True)
        return f"[Error]: Slack channel list failed: {e}"


@tool
def slack_get_channel_history(
    channel_id: str,
    limit: int = 20,
    oldest: str = "",
    latest: str = "",
    inclusive: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get recent messages from a Slack conversation.

    Args:
        channel_id: Slack conversation ID.
        limit: Number of messages to return, 1-100.
        oldest: Optional oldest message timestamp.
        latest: Optional latest message timestamp.
        inclusive: Include messages matching oldest/latest timestamps.
    """
    channel_id = channel_id.strip()
    if not channel_id:
        return "[Error]: channel_id is required."
    try:
        base_url, headers_or_error = _slack_config("slack_get_channel_history", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _slack_endpoint(base_url, "conversations.history"),
            params={
                "channel": channel_id,
                "limit": _limit(limit),
                "oldest": oldest.strip(),
                "latest": latest.strip(),
                "inclusive": str(bool(inclusive)).lower(),
            },
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data.get("messages", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("slack_get_channel_history failed", exc_info=True)
        return f"[Error]: Slack channel history lookup failed: {e}"


@tool
def slack_search_messages(
    query: str,
    limit: int = 20,
    sort: str = "timestamp",
    sort_dir: str = "desc",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Slack messages.

    Args:
        query: Slack message search query.
        limit: Number of matches to return, 1-100.
        sort: Slack sort mode, usually timestamp or score.
        sort_dir: Sort direction, asc or desc.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _slack_config("slack_search_messages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _slack_endpoint(base_url, "search.messages"),
            params={
                "query": query.strip(),
                "count": _limit(limit),
                "sort": sort.strip() or "timestamp",
                "sort_dir": sort_dir.strip() or "desc",
            },
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data.get("messages", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("slack_search_messages failed", exc_info=True)
        return f"[Error]: Slack message search failed: {e}"


@tool
def slack_list_users(
    include_deleted: bool = False,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Slack users visible to the credential.

    Args:
        include_deleted: Include deleted/deactivated users.
        limit: Number of users to return, 1-200.
    """
    try:
        base_url, headers_or_error = _slack_config("slack_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _slack_endpoint(base_url, "users.list"),
            params={"include_deleted": str(bool(include_deleted)).lower(), "limit": _limit(limit, max_value=200)},
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data.get("members", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("slack_list_users failed", exc_info=True)
        return f"[Error]: Slack user list failed: {e}"


@tool
def slack_get_user(
    user_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Slack user profile metadata.

    Args:
        user_id: Slack user ID.
    """
    user_id = user_id.strip()
    if not user_id:
        return "[Error]: user_id is required."
    try:
        base_url, headers_or_error = _slack_config("slack_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _slack_endpoint(base_url, "users.info"),
            params={"user": user_id},
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data.get("user", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("slack_get_user failed", exc_info=True)
        return f"[Error]: Slack user lookup failed: {e}"


@tool
def slack_post_message(
    channel_id: str,
    text: str,
    thread_ts: str = "",
    blocks_json: str = "",
    unfurl_links: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Post a message to Slack.

    Args:
        channel_id: Slack channel, DM, or App Home target ID.
        text: Message text.
        thread_ts: Optional thread timestamp to reply in a thread.
        blocks_json: Optional JSON array of Slack Block Kit blocks.
        unfurl_links: Optional Slack link unfurl setting.
    """
    if not channel_id.strip() or not text.strip():
        return "[Error]: channel_id and text are required."
    try:
        base_url, headers_or_error = _slack_config("slack_post_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params(
            {
                "channel": channel_id.strip(),
                "text": text,
                "thread_ts": thread_ts.strip(),
                "unfurl_links": unfurl_links,
            }
        )
        blocks = _parse_json(blocks_json, expected=list, label="blocks_json")
        if blocks:
            body["blocks"] = blocks
        data = _request_json(
            "POST",
            _slack_endpoint(base_url, "chat.postMessage"),
            json_body=body,
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("slack_post_message failed", exc_info=True)
        return f"[Error]: Slack message post failed: {e}"


@tool
def slack_update_message(
    channel_id: str,
    timestamp: str,
    text: str = "",
    blocks_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Slack message.

    Args:
        channel_id: Slack conversation ID.
        timestamp: Slack message timestamp (`ts`) to update.
        text: Optional replacement text.
        blocks_json: Optional JSON array of Slack Block Kit blocks.
    """
    if not channel_id.strip() or not timestamp.strip():
        return "[Error]: channel_id and timestamp are required."
    if not text.strip() and not blocks_json.strip():
        return "[Error]: provide text or blocks_json."
    try:
        base_url, headers_or_error = _slack_config("slack_update_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params({"channel": channel_id.strip(), "ts": timestamp.strip(), "text": text})
        blocks = _parse_json(blocks_json, expected=list, label="blocks_json")
        if blocks:
            body["blocks"] = blocks
        data = _request_json(
            "POST",
            _slack_endpoint(base_url, "chat.update"),
            json_body=body,
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("slack_update_message failed", exc_info=True)
        return f"[Error]: Slack message update failed: {e}"


@tool
def slack_add_reaction(
    channel_id: str,
    timestamp: str,
    name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a reaction to a Slack message.

    Args:
        channel_id: Slack conversation ID.
        timestamp: Slack message timestamp (`ts`).
        name: Emoji/reaction name without colons.
    """
    if not channel_id.strip() or not timestamp.strip() or not name.strip():
        return "[Error]: channel_id, timestamp, and name are required."
    try:
        base_url, headers_or_error = _slack_config("slack_add_reaction", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            _slack_endpoint(base_url, "reactions.add"),
            json_body={"channel": channel_id.strip(), "timestamp": timestamp.strip(), "name": name.strip().strip(":")},
            headers=headers_or_error,
            slack_ok=True,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("slack_add_reaction failed", exc_info=True)
        return f"[Error]: Slack reaction add failed: {e}"


@tool
def notion_search(
    query: str = "",
    object_type: str = "",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Notion pages and data sources shared with the credential.

    Args:
        query: Optional title search query.
        object_type: Optional filter, `page` or `data_source`.
        limit: Number of results to return, 1-100.
    """
    try:
        base_url, headers_or_error = _notion_config("notion_search", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = {"page_size": _limit(limit)}
        if query.strip():
            body["query"] = query.strip()
        if object_type.strip():
            value = object_type.strip()
            if value not in {"page", "data_source"}:
                return "[Error]: object_type must be page or data_source."
            body["filter"] = {"property": "object", "value": value}
        data = _request_json("POST", f"{base_url}/search", json_body=body, headers=headers_or_error)
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("notion_search failed", exc_info=True)
        return f"[Error]: Notion search failed: {e}"


@tool
def notion_get_page(
    page_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Retrieve Notion page properties.

    Args:
        page_id: Notion page ID.
    """
    page_id = page_id.strip()
    if not page_id:
        return "[Error]: page_id is required."
    try:
        base_url, headers_or_error = _notion_config("notion_get_page", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/pages/{quote(page_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("notion_get_page failed", exc_info=True)
        return f"[Error]: Notion page lookup failed: {e}"


@tool
def notion_get_block_children(
    block_id: str,
    page_size: int = 50,
    start_cursor: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Retrieve child blocks for a Notion page or block.

    Args:
        block_id: Notion block or page ID.
        page_size: Number of children to return, 1-100.
        start_cursor: Optional pagination cursor.
    """
    block_id = block_id.strip()
    if not block_id:
        return "[Error]: block_id is required."
    try:
        base_url, headers_or_error = _notion_config("notion_get_block_children", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/blocks/{quote(block_id, safe='')}/children",
            params={"page_size": _limit(page_size), "start_cursor": start_cursor.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("notion_get_block_children failed", exc_info=True)
        return f"[Error]: Notion block children lookup failed: {e}"


@tool
def notion_query_data_source(
    data_source_id: str,
    filter_json: str = "",
    sorts_json: str = "",
    page_size: int = 50,
    start_cursor: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Query a Notion data source.

    Args:
        data_source_id: Notion data source ID.
        filter_json: Optional Notion filter object as JSON.
        sorts_json: Optional Notion sorts array as JSON.
        page_size: Number of results to return, 1-100.
        start_cursor: Optional pagination cursor.
    """
    data_source_id = data_source_id.strip()
    if not data_source_id:
        return "[Error]: data_source_id is required."
    try:
        base_url, headers_or_error = _notion_config("notion_query_data_source", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params(
            {
                "filter": _parse_json(filter_json, expected=dict, label="filter_json"),
                "sorts": _parse_json(sorts_json, expected=list, label="sorts_json"),
                "page_size": _limit(page_size),
                "start_cursor": start_cursor.strip(),
            }
        )
        data = _request_json(
            "POST",
            f"{base_url}/data_sources/{quote(data_source_id, safe='')}/query",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("notion_query_data_source failed", exc_info=True)
        return f"[Error]: Notion data source query failed: {e}"


@tool
def notion_create_page(
    parent_id: str,
    parent_type: str = "page",
    title: str = "",
    properties_json: str = "",
    children_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Notion page under a page or data source.

    Args:
        parent_id: Parent page or data source ID.
        parent_type: `page` or `data_source`.
        title: Simple title when not supplying properties_json.
        properties_json: Optional Notion properties object as JSON.
        children_json: Optional Notion children block array as JSON.
    """
    parent_id = parent_id.strip()
    parent_type = parent_type.strip()
    if not parent_id or parent_type not in {"page", "data_source"}:
        return "[Error]: parent_id is required and parent_type must be page or data_source."
    try:
        base_url, headers_or_error = _notion_config("notion_create_page", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        parent_key = "page_id" if parent_type == "page" else "data_source_id"
        properties = _parse_json(properties_json, expected=dict, label="properties_json")
        if not properties and title.strip():
            properties = {"title": {"title": [{"type": "text", "text": {"content": title.strip()}}]}}
        if not properties:
            return "[Error]: provide title or properties_json."
        body = {
            "parent": {parent_key: parent_id},
            "properties": properties,
        }
        children = _parse_json(children_json, expected=list, label="children_json")
        if children:
            body["children"] = children
        data = _request_json("POST", f"{base_url}/pages", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("notion_create_page failed", exc_info=True)
        return f"[Error]: Notion page creation failed: {e}"


@tool
def notion_update_page(
    page_id: str,
    properties_json: str = "",
    archived: Optional[bool] = None,
    in_trash: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update Notion page properties or archive/trash state.

    Args:
        page_id: Notion page ID.
        properties_json: Optional Notion properties object as JSON.
        archived: Optional archived state.
        in_trash: Optional trash state.
    """
    page_id = page_id.strip()
    if not page_id:
        return "[Error]: page_id is required."
    try:
        properties = _parse_json(properties_json, expected=dict, label="properties_json")
        body = _filtered_params({"properties": properties, "archived": archived, "in_trash": in_trash})
        if not body:
            return "[Error]: provide properties_json, archived, or in_trash."
        base_url, headers_or_error = _notion_config("notion_update_page", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/pages/{quote(page_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("notion_update_page failed", exc_info=True)
        return f"[Error]: Notion page update failed: {e}"


@tool
def notion_append_block_children(
    block_id: str,
    children_json: str,
    position_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Append child blocks to a Notion page or block.

    Args:
        block_id: Notion block or page ID.
        children_json: Notion children block array as JSON.
        position_json: Optional Notion position object as JSON.
    """
    block_id = block_id.strip()
    if not block_id:
        return "[Error]: block_id is required."
    try:
        children = _parse_json(children_json, expected=list, label="children_json")
        if not children:
            return "[Error]: children_json must contain at least one block."
        body = {"children": children}
        position = _parse_json(position_json, expected=dict, label="position_json")
        if position:
            body["position"] = position
        base_url, headers_or_error = _notion_config("notion_append_block_children", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/blocks/{quote(block_id, safe='')}/children",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("notion_append_block_children failed", exc_info=True)
        return f"[Error]: Notion append block children failed: {e}"


@tool
def airtable_list_bases(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Airtable bases visible to the credential."""
    try:
        base_url, headers_or_error = _airtable_config("airtable_list_bases", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/meta/bases", headers=headers_or_error)
        return _dump_json(data.get("bases", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("airtable_list_bases failed", exc_info=True)
        return f"[Error]: Airtable base list failed: {e}"


@tool
def airtable_get_base_schema(
    base_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Airtable base schema.

    Args:
        base_id: Airtable base ID.
    """
    base_id = base_id.strip()
    if not base_id:
        return "[Error]: base_id is required."
    try:
        base_url, headers_or_error = _airtable_config("airtable_get_base_schema", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/meta/bases/{quote(base_id, safe='')}/tables", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("airtable_get_base_schema failed", exc_info=True)
        return f"[Error]: Airtable base schema lookup failed: {e}"


@tool
def airtable_list_records(
    base_id: str,
    table_name_or_id: str,
    max_records: int = 100,
    view: str = "",
    filter_by_formula: str = "",
    fields: str = "",
    sort_json: str = "",
    page_size: int = 100,
    offset: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Airtable records in a table.

    Args:
        base_id: Airtable base ID.
        table_name_or_id: Airtable table name or ID.
        max_records: Maximum records to request.
        view: Optional Airtable view.
        filter_by_formula: Optional Airtable formula filter.
        fields: Optional comma-separated field names to include.
        sort_json: Optional Airtable sort array as JSON.
        page_size: Page size, 1-100.
        offset: Optional pagination offset.
    """
    base_id = base_id.strip()
    table_name_or_id = table_name_or_id.strip()
    if not base_id or not table_name_or_id:
        return "[Error]: base_id and table_name_or_id are required."
    try:
        base_url, headers_or_error = _airtable_config("airtable_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {
            "maxRecords": _limit(max_records, default=100, max_value=100),
            "pageSize": _limit(page_size, default=100, max_value=100),
            "view": view.strip(),
            "filterByFormula": filter_by_formula.strip(),
            "offset": offset.strip(),
        }
        field_list = _split_csv(fields)
        if field_list:
            params["fields[]"] = field_list
        sorts = _parse_json(sort_json, expected=list, label="sort_json")
        if sorts:
            params["sort"] = json.dumps(sorts, separators=(",", ":"))
        data = _request_json(
            "GET",
            f"{base_url}/{quote(base_id, safe='')}/{quote(table_name_or_id, safe='')}",
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("airtable_list_records failed", exc_info=True)
        return f"[Error]: Airtable record list failed: {e}"


@tool
def airtable_get_record(
    base_id: str,
    table_name_or_id: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Airtable record by ID.

    Args:
        base_id: Airtable base ID.
        table_name_or_id: Airtable table name or ID.
        record_id: Airtable record ID.
    """
    if not base_id.strip() or not table_name_or_id.strip() or not record_id.strip():
        return "[Error]: base_id, table_name_or_id, and record_id are required."
    try:
        base_url, headers_or_error = _airtable_config("airtable_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{quote(base_id.strip(), safe='')}/{quote(table_name_or_id.strip(), safe='')}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("airtable_get_record failed", exc_info=True)
        return f"[Error]: Airtable record lookup failed: {e}"


@tool
def airtable_create_records(
    base_id: str,
    table_name_or_id: str,
    records_json: str,
    typecast: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create one or more Airtable records.

    Args:
        base_id: Airtable base ID.
        table_name_or_id: Airtable table name or ID.
        records_json: JSON object of fields for one record, or array of record objects.
        typecast: Let Airtable coerce values to field types.
    """
    if not base_id.strip() or not table_name_or_id.strip() or not records_json.strip():
        return "[Error]: base_id, table_name_or_id, and records_json are required."
    try:
        parsed = json.loads(records_json)
        records = parsed if isinstance(parsed, list) else [{"fields": parsed}]
        body = {"records": [record if "fields" in record else {"fields": record} for record in records], "typecast": typecast}
        base_url, headers_or_error = _airtable_config("airtable_create_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/{quote(base_id.strip(), safe='')}/{quote(table_name_or_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("airtable_create_records failed", exc_info=True)
        return f"[Error]: Airtable record creation failed: {e}"


@tool
def airtable_update_records(
    base_id: str,
    table_name_or_id: str,
    records_json: str,
    typecast: bool = False,
    replace: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update one or more Airtable records.

    Args:
        base_id: Airtable base ID.
        table_name_or_id: Airtable table name or ID.
        records_json: JSON object/array with `id` and `fields` keys.
        typecast: Let Airtable coerce values to field types.
        replace: Use PUT replacement instead of PATCH partial update.
    """
    if not base_id.strip() or not table_name_or_id.strip() or not records_json.strip():
        return "[Error]: base_id, table_name_or_id, and records_json are required."
    try:
        parsed = json.loads(records_json)
        records = parsed if isinstance(parsed, list) else [parsed]
        if not all(isinstance(record, dict) and record.get("id") and isinstance(record.get("fields"), dict) for record in records):
            return '[Error]: records_json must contain record object(s) with "id" and "fields".'
        body = {"records": records, "typecast": typecast}
        base_url, headers_or_error = _airtable_config("airtable_update_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT" if replace else "PATCH",
            f"{base_url}/{quote(base_id.strip(), safe='')}/{quote(table_name_or_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("airtable_update_records failed", exc_info=True)
        return f"[Error]: Airtable record update failed: {e}"


@tool
def airtable_delete_record(
    base_id: str,
    table_name_or_id: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Airtable record.

    Args:
        base_id: Airtable base ID.
        table_name_or_id: Airtable table name or ID.
        record_id: Airtable record ID.
    """
    if not base_id.strip() or not table_name_or_id.strip() or not record_id.strip():
        return "[Error]: base_id, table_name_or_id, and record_id are required."
    try:
        base_url, headers_or_error = _airtable_config("airtable_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/{quote(base_id.strip(), safe='')}/{quote(table_name_or_id.strip(), safe='')}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("airtable_delete_record failed", exc_info=True)
        return f"[Error]: Airtable record delete failed: {e}"


COLLABORATION_DATA_SERVICE_TOOLS = [
    slack_list_channels,
    slack_get_channel_history,
    slack_search_messages,
    slack_list_users,
    slack_get_user,
    slack_post_message,
    slack_update_message,
    slack_add_reaction,
    notion_search,
    notion_get_page,
    notion_get_block_children,
    notion_query_data_source,
    notion_create_page,
    notion_update_page,
    notion_append_block_children,
    airtable_list_bases,
    airtable_get_base_schema,
    airtable_list_records,
    airtable_get_record,
    airtable_create_records,
    airtable_update_records,
    airtable_delete_record,
]
