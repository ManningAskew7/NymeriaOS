"""Bookmark and link-management service integration tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
    BASE_URL_ALIAS_FIELDS,
    USERNAME_FIELDS,
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
_RAINDROP_BASE_URL = "https://api.raindrop.io/rest/v1"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _limit(value: int, *, default: int = 50, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _id(value: str) -> int | str:
    clean = value.strip()
    return int(clean) if clean.lstrip("-").isdigit() else clean


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
                params=_filtered(params),
                json=json_body,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            data = response.json()
            if isinstance(data, dict) and data.get("status") == "fail":
                raise RuntimeError(str(data.get("message") or data.get("error") or "request failed"))
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
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _raindrop_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="raindrop",
            provider_aliases=("raindrop_api", "raindrop_oauth2", "raindrop_oauth2_api"),
            field_names=BASE_URL_ALIAS_FIELDS,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("raindrop_base_url")
        or _RAINDROP_BASE_URL
    )
    token = _credential_value(
        provider="raindrop",
        provider_aliases=("raindrop_api", "raindrop_oauth2", "raindrop_oauth2_api"),
        field_names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("raindrop_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="raindrop",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="RAINDROP_ACCESS_TOKEN",
            display_name="Raindrop",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _yourls_endpoint(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    raw_url = (
        _credential_value(
            provider="yourls",
            provider_aliases=("yourls_api",),
            field_names=("api_url", "apiUrl", "base_url", "baseUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("yourls_url")
    )
    signature = _credential_value(
        provider="yourls",
        provider_aliases=("yourls_api",),
        field_names=("signature", "api_signature", "apiSignature", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("yourls_signature")
    username = _credential_value(
        provider="yourls",
        provider_aliases=("yourls_api",),
        field_names=USERNAME_FIELDS,
        tool_name=tool_name,
        config=config,
    ) or _settings_value("yourls_username")
    password = _credential_value(
        provider="yourls",
        provider_aliases=("yourls_api",),
        field_names=("password", "api_password", "apiPassword"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("yourls_password")
    if not raw_url:
        return "", "[Error]: No YOURLS URL found. Save a YOURLS credential with url, or set YOURLS_URL."
    endpoint = _base_url(raw_url)
    if not endpoint.endswith("/yourls-api.php"):
        endpoint = f"{endpoint}/yourls-api.php"
    if signature:
        return endpoint, {"signature": signature}
    if username and password:
        return endpoint, {"username": username, "password": password}
    return endpoint, _setup_hint(
        provider="yourls",
        field_names=("signature", "username", "password"),
        tool_name=tool_name,
        env_var="YOURLS_SIGNATURE or YOURLS_USERNAME + YOURLS_PASSWORD",
        display_name="YOURLS",
    )


def _yourls_request(tool_name: str, config: Optional[RunnableConfig], params: dict[str, Any]) -> str:
    endpoint, auth_or_error = _yourls_endpoint(tool_name, config)
    if isinstance(auth_or_error, str):
        return auth_or_error
    data = _request_json(
        "GET",
        endpoint,
        params={"format": "json", **auth_or_error, **params},
    )
    return _dump_json(data)


@tool
def raindrop_list_bookmarks(
    collection_id: str = "0",
    search: str = "",
    tag: str = "",
    sort: str = "-created",
    page: int = 0,
    per_page: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Raindrop bookmarks in a collection."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_list_bookmarks", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base}/raindrops/{quote(str(_id(collection_id)), safe='')}",
            params={
                "search": search.strip(),
                "tag": tag.strip(),
                "sort": sort.strip() or "-created",
                "page": max(0, int(page)),
                "perpage": _limit(per_page),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_list_bookmarks failed", exc_info=True)
        return f"[Error]: Raindrop bookmark list failed: {e}"


@tool
def raindrop_get_bookmark(
    bookmark_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Raindrop bookmark by ID."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_get_bookmark", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base}/raindrop/{quote(bookmark_id, safe='')}", headers=headers_or_error)
        return _dump_json(data.get("item", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_get_bookmark failed", exc_info=True)
        return f"[Error]: Raindrop bookmark lookup failed: {e}"


@tool
def raindrop_create_bookmark(
    link: str,
    collection_id: str = "0",
    title: str = "",
    tags: str = "",
    excerpt: str = "",
    note: str = "",
    please_parse: bool = True,
    extra_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Raindrop bookmark."""
    if not link.strip():
        return "[Error]: link is required."
    try:
        base, headers_or_error = _raindrop_config("raindrop_create_bookmark", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = {
            "link": link.strip(),
            "collection": {"$id": _id(collection_id)},
        }
        if title.strip():
            body["title"] = title.strip()
        if tags.strip():
            body["tags"] = _split_csv(tags)
        if excerpt.strip():
            body["excerpt"] = excerpt.strip()
        if note.strip():
            body["note"] = note.strip()
        if please_parse:
            body["pleaseParse"] = {}
        body.update(_json_object(extra_fields_json, field_name="extra_fields_json"))
        data = _request_json("POST", f"{base}/raindrop", json_body=body, headers=headers_or_error)
        return _dump_json(data.get("item", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_create_bookmark failed", exc_info=True)
        return f"[Error]: Raindrop bookmark create failed: {e}"


@tool
def raindrop_update_bookmark(
    bookmark_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Raindrop bookmark from a JSON object."""
    try:
        body = _json_object(fields_json, field_name="fields_json")
        if "collection_id" in body and "collection" not in body:
            body["collection"] = {"$id": _id(str(body.pop("collection_id")))}
        if isinstance(body.get("tags"), str):
            body["tags"] = _split_csv(str(body["tags"]))
        base, headers_or_error = _raindrop_config("raindrop_update_bookmark", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("PUT", f"{base}/raindrop/{quote(bookmark_id, safe='')}", json_body=body, headers=headers_or_error)
        return _dump_json(data.get("item", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_update_bookmark failed", exc_info=True)
        return f"[Error]: Raindrop bookmark update failed: {e}"


@tool
def raindrop_delete_bookmark(
    bookmark_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Raindrop bookmark by ID."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_delete_bookmark", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("DELETE", f"{base}/raindrop/{quote(bookmark_id, safe='')}", headers=headers_or_error))
    except Exception as e:
        logger.error("raindrop_delete_bookmark failed", exc_info=True)
        return f"[Error]: Raindrop bookmark delete failed: {e}"


@tool
def raindrop_list_collections(
    include_child: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Raindrop collections."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_list_collections", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = "collections/childrens" if include_child else "collections"
        data = _request_json("GET", f"{base}/{endpoint}", headers=headers_or_error)
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_list_collections failed", exc_info=True)
        return f"[Error]: Raindrop collection list failed: {e}"


@tool
def raindrop_get_collection(
    collection_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Raindrop collection by ID."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_get_collection", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base}/collection/{quote(str(_id(collection_id)), safe='')}", headers=headers_or_error)
        return _dump_json(data.get("item", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_get_collection failed", exc_info=True)
        return f"[Error]: Raindrop collection lookup failed: {e}"


@tool
def raindrop_list_tags(
    collection_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Raindrop tags, optionally scoped to a collection."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_list_tags", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        suffix = f"/{quote(str(_id(collection_id)), safe='')}" if collection_id.strip() else ""
        data = _request_json("GET", f"{base}/tags{suffix}", headers=headers_or_error)
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_list_tags failed", exc_info=True)
        return f"[Error]: Raindrop tag list failed: {e}"


@tool
def raindrop_delete_tags(
    tags: str,
    collection_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete one or more Raindrop tags."""
    parsed_tags = _split_csv(tags)
    if not parsed_tags:
        return "[Error]: tags is required."
    try:
        base, headers_or_error = _raindrop_config("raindrop_delete_tags", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        suffix = f"/{quote(str(_id(collection_id)), safe='')}" if collection_id.strip() else ""
        return _dump_json(_request_json("DELETE", f"{base}/tags{suffix}", json_body={"tags": parsed_tags}, headers=headers_or_error))
    except Exception as e:
        logger.error("raindrop_delete_tags failed", exc_info=True)
        return f"[Error]: Raindrop tag delete failed: {e}"


@tool
def raindrop_get_user(
    user_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Raindrop user metadata for the current user or a specific user ID."""
    try:
        base, headers_or_error = _raindrop_config("raindrop_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        suffix = f"/{quote(user_id.strip(), safe='')}" if user_id.strip() else ""
        data = _request_json("GET", f"{base}/user{suffix}", headers=headers_or_error)
        return _dump_json(data.get("user", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("raindrop_get_user failed", exc_info=True)
        return f"[Error]: Raindrop user lookup failed: {e}"


@tool
def yourls_shorten_url(
    url: str,
    keyword: str = "",
    title: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a short URL with YOURLS."""
    if not url.strip():
        return "[Error]: url is required."
    try:
        return _yourls_request(
            "yourls_shorten_url",
            config,
            {
                "action": "shorturl",
                "url": url.strip(),
                "keyword": keyword.strip(),
                "title": title.strip(),
            },
        )
    except Exception as e:
        logger.error("yourls_shorten_url failed", exc_info=True)
        return f"[Error]: YOURLS shorten failed: {e}"


@tool
def yourls_expand_url(
    short_url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Expand a short URL with YOURLS."""
    if not short_url.strip():
        return "[Error]: short_url is required."
    try:
        return _yourls_request("yourls_expand_url", config, {"action": "expand", "shorturl": short_url.strip()})
    except Exception as e:
        logger.error("yourls_expand_url failed", exc_info=True)
        return f"[Error]: YOURLS expand failed: {e}"


@tool
def yourls_get_url_stats(
    short_url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get YOURLS stats for a short URL."""
    if not short_url.strip():
        return "[Error]: short_url is required."
    try:
        data = _yourls_request("yourls_get_url_stats", config, {"action": "url-stats", "shorturl": short_url.strip()})
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        return _dump_json(parsed.get("link", parsed) if isinstance(parsed, dict) else parsed)
    except Exception as e:
        logger.error("yourls_get_url_stats failed", exc_info=True)
        return f"[Error]: YOURLS URL stats lookup failed: {e}"


@tool
def yourls_get_db_stats(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get YOURLS database stats."""
    try:
        return _yourls_request("yourls_get_db_stats", config, {"action": "db-stats"})
    except Exception as e:
        logger.error("yourls_get_db_stats failed", exc_info=True)
        return f"[Error]: YOURLS DB stats lookup failed: {e}"


BOOKMARK_LINK_SERVICE_TOOLS = [
    raindrop_list_bookmarks,
    raindrop_get_bookmark,
    raindrop_create_bookmark,
    raindrop_update_bookmark,
    raindrop_delete_bookmark,
    raindrop_list_collections,
    raindrop_get_collection,
    raindrop_list_tags,
    raindrop_delete_tags,
    raindrop_get_user,
    yourls_shorten_url,
    yourls_expand_url,
    yourls_get_url_stats,
    yourls_get_db_stats,
]
