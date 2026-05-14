"""Marketing and contact-list service integration tools."""

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
_ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL = "https://example.api-us1.com"
_CONVERTKIT_BASE_URL = "https://api.convertkit.com/v3"
_GETRESPONSE_BASE_URL = "https://api.getresponse.com/v3"
_MAILERLITE_BASE_URL = "https://connect.mailerlite.com/api"
_MAILERLITE_CLASSIC_BASE_URL = "https://api.mailerlite.com/api/v2"
_CUSTOMERIO_TRACK_BASE_URL = "https://track.customer.io/api/v1"
_CUSTOMERIO_TRACK_EU_BASE_URL = "https://track-eu.customer.io/api/v1"
_CUSTOMERIO_APP_BASE_URL = "https://api.customer.io/v1"
_CUSTOMERIO_APP_EU_BASE_URL = "https://api-eu.customer.io/v1"
_ITERABLE_BASE_URL = "https://api.iterable.com/api"
_POSTHOG_BASE_URL = "https://app.posthog.com"
_SEGMENT_BASE_URL = "https://api.segment.io/v1"


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


def _json_array_or_object(value: str, *, field_name: str) -> list[dict[str, Any]]:
    if not value or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    records = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{field_name} must be a JSON object or array of objects")
    return records


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


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
                params=_filtered(params),
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
                body.get("message")
                or body.get("detail")
                or body.get("title")
                or body.get("error_description")
                or body.get("error")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _customerio_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[dict[str, Any], str | None]:
    region = (
        _credential_value(
            provider="customerio",
            provider_aliases=("customer_io", "customerio_api", "customer_io_api"),
            field_names=("region", "tracking_region", "trackingRegion"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("customerio_region")
        or "track.customer.io"
    )
    region_key = str(region).lower()
    track_base = (
        _credential_value(
            provider="customerio",
            provider_aliases=("customer_io", "customerio_api", "customer_io_api"),
            field_names=("tracking_base_url", "trackingBaseUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("customerio_tracking_base_url")
        or (_CUSTOMERIO_TRACK_EU_BASE_URL if "eu" in region_key else _CUSTOMERIO_TRACK_BASE_URL)
    )
    app_base = (
        _credential_value(
            provider="customerio",
            provider_aliases=("customer_io", "customerio_api", "customer_io_api"),
            field_names=("app_base_url", "appBaseUrl", "base_url", "baseUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("customerio_app_base_url")
        or (_CUSTOMERIO_APP_EU_BASE_URL if "eu" in region_key else _CUSTOMERIO_APP_BASE_URL)
    )
    site_id = _credential_value(
        provider="customerio",
        provider_aliases=("customer_io", "customerio_api", "customer_io_api"),
        field_names=("tracking_site_id", "trackingSiteId", "site_id", "siteId"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("customerio_tracking_site_id")
    tracking_key = _credential_value(
        provider="customerio",
        provider_aliases=("customer_io", "customerio_api", "customer_io_api"),
        field_names=("tracking_api_key", "trackingApiKey", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("customerio_tracking_api_key")
    app_key = _credential_value(
        provider="customerio",
        provider_aliases=("customer_io", "customerio_api", "customer_io_api"),
        field_names=("app_api_key", "appApiKey", "access_token", "accessToken"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("customerio_app_api_key")

    tracking_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    app_headers = dict(tracking_headers)
    if site_id and tracking_key:
        raw = f"{site_id}:{tracking_key}".encode()
        tracking_headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    if app_key:
        app_headers["Authorization"] = f"Bearer {app_key}"
    missing: list[str] = []
    if not (site_id and tracking_key):
        missing.append("tracking")
    if not app_key:
        missing.append("app")
    return {
        "tracking_base": _base_url(track_base),
        "app_base": _base_url(app_base),
        "tracking_headers": tracking_headers,
        "app_headers": app_headers,
        "has_tracking": bool(site_id and tracking_key),
        "has_app": bool(app_key),
    }, ",".join(missing) if missing else None


def _customerio_auth_error(tool_name: str, *, app: bool) -> str:
    fields = ("app_api_key",) if app else ("tracking_site_id", "tracking_api_key")
    env_var = "CUSTOMERIO_APP_API_KEY" if app else "CUSTOMERIO_TRACKING_SITE_ID and CUSTOMERIO_TRACKING_API_KEY"
    return _setup_hint(
        provider="customerio",
        field_names=fields,
        tool_name=tool_name,
        env_var=env_var,
        display_name="Customer.io",
    )


def _iterable_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="iterable",
            provider_aliases=("iterable_api",),
            field_names=("base_url", "baseUrl", "region", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("iterable_base_url")
        or _ITERABLE_BASE_URL
    )
    api_key = _credential_value(
        provider="iterable",
        provider_aliases=("iterable_api",),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("iterable_api_key")
    base = _base_url(base)
    if not base.endswith("/api"):
        base = f"{base}/api"
    if not api_key:
        return base, _setup_hint(
            provider="iterable",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="ITERABLE_API_KEY",
            display_name="Iterable",
        )
    return base, {
        "Accept": "application/json",
        "Api_Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _posthog_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider="posthog",
            provider_aliases=("posthog_api", "post_hog"),
            field_names=("base_url", "baseUrl", "url", "host"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("posthog_base_url")
        or _POSTHOG_BASE_URL
    )
    api_key = _credential_value(
        provider="posthog",
        provider_aliases=("posthog_api", "post_hog"),
        field_names=("api_key", "apiKey", "project_api_key", "projectApiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("posthog_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="posthog",
            field_names=("api_key", "project_api_key", "value"),
            tool_name=tool_name,
            env_var="POSTHOG_API_KEY",
            display_name="PostHog",
        )
    return _base_url(base), api_key


def _segment_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="segment",
            provider_aliases=("segment_api",),
            field_names=("base_url", "baseUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("segment_base_url")
        or _SEGMENT_BASE_URL
    )
    write_key = _credential_value(
        provider="segment",
        provider_aliases=("segment_api",),
        field_names=("write_key", "writeKey", "writekey", "api_key", "apiKey", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("segment_write_key")
    if not write_key:
        return _base_url(base), _setup_hint(
            provider="segment",
            field_names=("write_key", "value"),
            tool_name=tool_name,
            env_var="SEGMENT_WRITE_KEY",
            display_name="Segment",
        )
    raw = f"{write_key}:".encode()
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {base64.b64encode(raw).decode()}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


@tool
def customerio_list_campaigns(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Customer.io campaigns."""
    cfg, _missing = _customerio_config("customerio_list_campaigns", config)
    if not cfg["has_app"]:
        return _customerio_auth_error("customerio_list_campaigns", app=True)
    return _dump_json(_request_json("GET", f"{cfg['app_base']}/campaigns", headers=cfg["app_headers"]))


@tool
def customerio_get_campaign(
    campaign_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Customer.io campaign by ID."""
    cfg, _missing = _customerio_config("customerio_get_campaign", config)
    if not cfg["has_app"]:
        return _customerio_auth_error("customerio_get_campaign", app=True)
    return _dump_json(
        _request_json("GET", f"{cfg['app_base']}/campaigns/{quote(campaign_id, safe='')}", headers=cfg["app_headers"])
    )


@tool
def customerio_upsert_customer(
    customer_id: str,
    fields_json: str = "",
    email: str = "",
    created_at: int = 0,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update a Customer.io customer profile."""
    if not customer_id.strip():
        return "[Error]: customer_id is required."
    cfg, _missing = _customerio_config("customerio_upsert_customer", config)
    if not cfg["has_tracking"]:
        return _customerio_auth_error("customerio_upsert_customer", app=False)
    body = _json_object(fields_json, field_name="fields_json")
    if email:
        body["email"] = email
    if created_at:
        body["created_at"] = int(created_at)
    return _dump_json(
        _request_json(
            "PUT",
            f"{cfg['tracking_base']}/customers/{quote(customer_id.strip(), safe='')}",
            json_body=body,
            headers=cfg["tracking_headers"],
        )
    )


@tool
def customerio_track_event(
    customer_id: str,
    event_name: str,
    data_json: str = "",
    event_type: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a Customer.io event for a known customer."""
    if not customer_id.strip() or not event_name.strip():
        return "[Error]: customer_id and event_name are required."
    cfg, _missing = _customerio_config("customerio_track_event", config)
    if not cfg["has_tracking"]:
        return _customerio_auth_error("customerio_track_event", app=False)
    body: dict[str, Any] = {"name": event_name.strip(), "data": _json_object(data_json, field_name="data_json")}
    if event_type:
        body["data"]["type"] = event_type
    return _dump_json(
        _request_json(
            "POST",
            f"{cfg['tracking_base']}/customers/{quote(customer_id.strip(), safe='')}/events",
            json_body=body,
            headers=cfg["tracking_headers"],
        )
    )


@tool
def customerio_track_anonymous_event(
    event_name: str,
    data_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a Customer.io event without a known customer ID."""
    if not event_name.strip():
        return "[Error]: event_name is required."
    cfg, _missing = _customerio_config("customerio_track_anonymous_event", config)
    if not cfg["has_tracking"]:
        return _customerio_auth_error("customerio_track_anonymous_event", app=False)
    body = {"name": event_name.strip(), "data": _json_object(data_json, field_name="data_json")}
    return _dump_json(_request_json("POST", f"{cfg['tracking_base']}/events", json_body=body, headers=cfg["tracking_headers"]))


@tool
def customerio_update_segment(
    segment_id: str,
    customer_ids: str,
    action: str = "add",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add or remove customers from a Customer.io manual segment."""
    ids = _csv_to_list(customer_ids)
    if not segment_id.strip() or not ids:
        return "[Error]: segment_id and customer_ids are required."
    action_key = action.strip().lower()
    if action_key not in {"add", "remove"}:
        return "[Error]: action must be add or remove."
    cfg, _missing = _customerio_config("customerio_update_segment", config)
    if not cfg["has_tracking"]:
        return _customerio_auth_error("customerio_update_segment", app=False)
    endpoint = "add_customers" if action_key == "add" else "remove_customers"
    body = {"id": segment_id.strip(), "ids": ids}
    return _dump_json(
        _request_json(
            "POST",
            f"{cfg['tracking_base']}/segments/{quote(segment_id.strip(), safe='')}/{endpoint}",
            json_body=body,
            headers=cfg["tracking_headers"],
        )
    )


@tool
def iterable_get_user(
    identifier: str,
    value: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Iterable user by email or user ID."""
    if not value.strip():
        return "[Error]: value is required."
    base, auth = _iterable_config("iterable_get_user", config)
    if isinstance(auth, str):
        return auth
    if identifier.strip().lower() == "user_id":
        return _dump_json(_request_json("GET", f"{base}/users/byUserId/{quote(value.strip(), safe='')}", headers=auth))
    return _dump_json(_request_json("GET", f"{base}/users/getByEmail", params={"email": value.strip()}, headers=auth))


@tool
def iterable_upsert_user(
    identifier: str,
    value: str,
    data_fields_json: str = "",
    prefer_user_id: bool = False,
    merge_nested_objects: bool = True,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update an Iterable user."""
    if not value.strip():
        return "[Error]: value is required."
    base, auth = _iterable_config("iterable_upsert_user", config)
    if isinstance(auth, str):
        return auth
    data_fields = _json_object(data_fields_json, field_name="data_fields_json")
    body: dict[str, Any] = {
        "dataFields": data_fields,
        "mergeNestedObjects": bool(merge_nested_objects),
    }
    if identifier.strip().lower() == "user_id":
        body["userId"] = value.strip()
        body["preferUserId"] = bool(prefer_user_id)
    else:
        body["email"] = value.strip()
    return _dump_json(_request_json("POST", f"{base}/users/update", json_body=body, headers=auth))


@tool
def iterable_track_event(
    event_name: str,
    email: str = "",
    user_id: str = "",
    data_fields_json: str = "",
    created_at: str = "",
    campaign_id: int = 0,
    template_id: int = 0,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track an Iterable event."""
    if not event_name.strip() or not (email.strip() or user_id.strip()):
        return "[Error]: event_name and either email or user_id are required."
    base, auth = _iterable_config("iterable_track_event", config)
    if isinstance(auth, str):
        return auth
    body = {
        "eventName": event_name.strip(),
        "dataFields": _json_object(data_fields_json, field_name="data_fields_json"),
    }
    if email.strip():
        body["email"] = email.strip()
    else:
        body["userId"] = user_id.strip()
    if created_at.strip():
        body["createdAt"] = created_at.strip()
    if campaign_id:
        body["campaignId"] = int(campaign_id)
    if template_id:
        body["templateId"] = int(template_id)
    return _dump_json(_request_json("POST", f"{base}/events/trackBulk", json_body={"events": [body]}, headers=auth))


@tool
def iterable_list_lists(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Iterable static lists."""
    base, auth = _iterable_config("iterable_list_lists", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/lists", headers=auth))


@tool
def iterable_update_list_subscribers(
    list_id: int,
    values: str,
    identifier: str = "email",
    action: str = "add",
    campaign_id: int = 0,
    channel_unsubscribe: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe or unsubscribe Iterable users by email or user ID."""
    subscribers = _csv_to_list(values)
    if not subscribers:
        return "[Error]: values is required."
    action_key = action.strip().lower()
    if action_key not in {"add", "remove"}:
        return "[Error]: action must be add or remove."
    base, auth = _iterable_config("iterable_update_list_subscribers", config)
    if isinstance(auth, str):
        return auth
    key = "userId" if identifier.strip().lower() == "user_id" else "email"
    body = {"listId": int(list_id), "subscribers": [{key: value} for value in subscribers]}
    endpoint = "subscribe" if action_key == "add" else "unsubscribe"
    if action_key == "remove":
        if campaign_id:
            body["campaignId"] = int(campaign_id)
        body["channelUnsubscribe"] = bool(channel_unsubscribe)
    return _dump_json(_request_json("POST", f"{base}/lists/{endpoint}", json_body=body, headers=auth))


@tool
def posthog_capture_event(
    event_name: str,
    distinct_id: str,
    properties_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Capture one PostHog event."""
    if not event_name.strip() or not distinct_id.strip():
        return "[Error]: event_name and distinct_id are required."
    base, api_key = _posthog_config("posthog_capture_event", config)
    if api_key is None or api_key.startswith("[Error]:"):
        return api_key or ""
    properties = _json_object(properties_json, field_name="properties_json")
    properties["distinct_id"] = distinct_id.strip()
    body = {"api_key": api_key, "event": event_name.strip(), "properties": properties}
    if timestamp.strip():
        body["timestamp"] = timestamp.strip()
    return _dump_json(_request_json("POST", f"{base}/capture", json_body=body, headers={"User-Agent": "Nymeria"}))


@tool
def posthog_identify(
    distinct_id: str,
    properties_json: str = "",
    context_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Identify a PostHog user and set properties."""
    if not distinct_id.strip():
        return "[Error]: distinct_id is required."
    base, api_key = _posthog_config("posthog_identify", config)
    if api_key is None or api_key.startswith("[Error]:"):
        return api_key or ""
    body = {
        "api_key": api_key,
        "event": "$identify",
        "distinct_id": distinct_id.strip(),
        "properties": _json_object(properties_json, field_name="properties_json"),
    }
    if context_json.strip():
        body["context"] = _json_object(context_json, field_name="context_json")
    if timestamp.strip():
        body["timestamp"] = timestamp.strip()
    return _dump_json(_request_json("POST", f"{base}/batch", json_body=body, headers={"User-Agent": "Nymeria"}))


@tool
def posthog_create_alias(
    distinct_id: str,
    alias: str,
    context_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a PostHog alias for a distinct ID."""
    if not distinct_id.strip() or not alias.strip():
        return "[Error]: distinct_id and alias are required."
    base, api_key = _posthog_config("posthog_create_alias", config)
    if api_key is None or api_key.startswith("[Error]:"):
        return api_key or ""
    body = {
        "api_key": api_key,
        "event": "$create_alias",
        "properties": {"distinct_id": distinct_id.strip(), "alias": alias.strip()},
    }
    if context_json.strip():
        body["context"] = _json_object(context_json, field_name="context_json")
    if timestamp.strip():
        body["timestamp"] = timestamp.strip()
    return _dump_json(_request_json("POST", f"{base}/batch", json_body=body, headers={"User-Agent": "Nymeria"}))


@tool
def posthog_track_page_or_screen(
    kind: str,
    distinct_id: str,
    name: str,
    properties_json: str = "",
    context_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a PostHog page or screen view."""
    kind_key = kind.strip().lower()
    if kind_key not in {"page", "screen"}:
        return "[Error]: kind must be page or screen."
    if not distinct_id.strip() or not name.strip():
        return "[Error]: distinct_id and name are required."
    base, api_key = _posthog_config("posthog_track_page_or_screen", config)
    if api_key is None or api_key.startswith("[Error]:"):
        return api_key or ""
    properties = _json_object(properties_json, field_name="properties_json")
    properties["distinct_id"] = distinct_id.strip()
    properties["name"] = name.strip()
    body: dict[str, Any] = {
        "api_key": api_key,
        "event": "$page" if kind_key == "page" else "$screen",
        "properties": properties,
    }
    if context_json.strip():
        body["context"] = _json_object(context_json, field_name="context_json")
    if timestamp.strip():
        body["timestamp"] = timestamp.strip()
    return _dump_json(_request_json("POST", f"{base}/batch", json_body=body, headers={"User-Agent": "Nymeria"}))


@tool
def segment_identify(
    traits_json: str = "",
    user_id: str = "",
    anonymous_id: str = "",
    context_json: str = "",
    integrations_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Segment identify call."""
    if not (user_id.strip() or anonymous_id.strip()):
        return "[Error]: user_id or anonymous_id is required."
    base, auth = _segment_config("segment_identify", config)
    if isinstance(auth, str):
        return auth
    body = {
        "traits": _json_object(traits_json, field_name="traits_json"),
        "context": _json_object(context_json, field_name="context_json"),
        "integrations": _json_object(integrations_json, field_name="integrations_json"),
    }
    if user_id.strip():
        body["userId"] = user_id.strip()
    else:
        body["anonymousId"] = anonymous_id.strip()
    return _dump_json(_request_json("POST", f"{base}/identify", json_body=_filtered(body), headers=auth))


@tool
def segment_track(
    event: str,
    user_id: str = "",
    anonymous_id: str = "",
    properties_json: str = "",
    context_json: str = "",
    integrations_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Segment track event."""
    if not event.strip() or not (user_id.strip() or anonymous_id.strip()):
        return "[Error]: event and user_id or anonymous_id are required."
    base, auth = _segment_config("segment_track", config)
    if isinstance(auth, str):
        return auth
    body = {
        "event": event.strip(),
        "properties": _json_object(properties_json, field_name="properties_json"),
        "context": _json_object(context_json, field_name="context_json"),
        "integrations": _json_object(integrations_json, field_name="integrations_json"),
    }
    if user_id.strip():
        body["userId"] = user_id.strip()
    else:
        body["anonymousId"] = anonymous_id.strip()
    return _dump_json(_request_json("POST", f"{base}/track", json_body=_filtered(body), headers=auth))


@tool
def segment_group(
    group_id: str,
    user_id: str = "",
    anonymous_id: str = "",
    traits_json: str = "",
    context_json: str = "",
    integrations_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Segment group call."""
    if not group_id.strip() or not (user_id.strip() or anonymous_id.strip()):
        return "[Error]: group_id and user_id or anonymous_id are required."
    base, auth = _segment_config("segment_group", config)
    if isinstance(auth, str):
        return auth
    body = {
        "groupId": group_id.strip(),
        "traits": _json_object(traits_json, field_name="traits_json"),
        "context": _json_object(context_json, field_name="context_json"),
        "integrations": _json_object(integrations_json, field_name="integrations_json"),
    }
    if user_id.strip():
        body["userId"] = user_id.strip()
    else:
        body["anonymousId"] = anonymous_id.strip()
    return _dump_json(_request_json("POST", f"{base}/group", json_body=_filtered(body), headers=auth))


def _activecampaign_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="activecampaign",
            provider_aliases=("active_campaign", "activecampaign_api", "active_campaign_api"),
            field_names=("api_url", "apiUrl", "base_url", "baseUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("activecampaign_base_url")
        or _ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL
    )
    api_key = _credential_value(
        provider="activecampaign",
        provider_aliases=("active_campaign", "activecampaign_api", "active_campaign_api"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("activecampaign_api_key")
    if not api_key or base == _ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL:
        return _base_url(base), _setup_hint(
            provider="activecampaign",
            field_names=("api_key", "api_url"),
            tool_name=tool_name,
            env_var="ACTIVECAMPAIGN_API_KEY and ACTIVECAMPAIGN_BASE_URL",
            display_name="ActiveCampaign",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Api-Token": api_key,
        "User-Agent": "Nymeria",
    }


def _convertkit_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | str]:
    base = (
        _credential_value(
            provider="convertkit",
            provider_aliases=("convert_kit", "convertkit_api", "kit"),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("convertkit_base_url")
        or _CONVERTKIT_BASE_URL
    )
    secret = _credential_value(
        provider="convertkit",
        provider_aliases=("convert_kit", "convertkit_api", "kit"),
        field_names=("api_secret", "apiSecret", "secret", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("convertkit_api_secret")
    if not secret:
        return _base_url(base), _setup_hint(
            provider="convertkit",
            field_names=("api_secret", "value"),
            tool_name=tool_name,
            env_var="CONVERTKIT_API_SECRET",
            display_name="ConvertKit",
        )
    return _base_url(base), secret


def _getresponse_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="getresponse",
            provider_aliases=("get_response", "getresponse_api", "get_response_api"),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("getresponse_base_url")
        or _GETRESPONSE_BASE_URL
    )
    api_key = _credential_value(
        provider="getresponse",
        provider_aliases=("get_response", "getresponse_api", "get_response_api"),
        field_names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("getresponse_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="getresponse",
            field_names=("api_key", "access_token", "value"),
            tool_name=tool_name,
            env_var="GETRESPONSE_API_KEY",
            display_name="GetResponse",
        )
    prefix = "api-key " if not api_key.lower().startswith(("api-key ", "bearer ")) else ""
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Auth-Token": f"{prefix}{api_key}",
        "User-Agent": "Nymeria",
    }


def _mailerlite_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    classic = (
        _credential_value(
            provider="mailerlite",
            provider_aliases=("mailer_lite", "mailerlite_api", "mailer_lite_api"),
            field_names=("classic_api", "classicApi"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailerlite_classic_api")
    )
    default_base = _MAILERLITE_CLASSIC_BASE_URL if str(classic).lower() in {"1", "true", "yes"} else _MAILERLITE_BASE_URL
    configured_base = (
        _credential_value(
            provider="mailerlite",
            provider_aliases=("mailer_lite", "mailerlite_api", "mailer_lite_api"),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailerlite_base_url")
    )
    if default_base == _MAILERLITE_CLASSIC_BASE_URL and configured_base == _MAILERLITE_BASE_URL:
        configured_base = None
    base = configured_base or default_base
    api_key = _credential_value(
        provider="mailerlite",
        provider_aliases=("mailer_lite", "mailerlite_api", "mailer_lite_api"),
        field_names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailerlite_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="mailerlite",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="MAILERLITE_API_KEY",
            display_name="MailerLite",
        )
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"}
    if _base_url(base).endswith("/api/v2"):
        headers["X-MailerLite-ApiKey"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return _base_url(base), headers


@tool
def activecampaign_list_contacts(
    search: str = "",
    email: str = "",
    list_id: str = "",
    tag_id: str = "",
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ActiveCampaign contacts with optional search, email, list, or tag filters."""
    base, auth = _activecampaign_config("activecampaign_list_contacts", config)
    if isinstance(auth, str):
        return auth
    params = {
        "search": search,
        "email": email,
        "listid": list_id,
        "tagid": tag_id,
        "limit": _limit(limit, max_value=100),
    }
    return _dump_json(_request_json("GET", f"{base}/api/3/contacts", params=params, headers=auth))


@tool
def activecampaign_get_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one ActiveCampaign contact by ID."""
    base, auth = _activecampaign_config("activecampaign_get_contact", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/api/3/contacts/{quote(contact_id, safe='')}", headers=auth))


@tool
def activecampaign_sync_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    phone: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update an ActiveCampaign contact using contact sync."""
    base, auth = _activecampaign_config("activecampaign_sync_contact", config)
    if isinstance(auth, str):
        return auth
    contact = {"email": email, "firstName": first_name, "lastName": last_name, "phone": phone}
    contact.update(_json_object(fields_json, field_name="fields_json"))
    body = {"contact": _filtered(contact)}
    return _dump_json(_request_json("POST", f"{base}/api/3/contact/sync", json_body=body, headers=auth))


@tool
def activecampaign_update_contact(
    contact_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an ActiveCampaign contact by ID with a JSON object of contact fields."""
    base, auth = _activecampaign_config("activecampaign_update_contact", config)
    if isinstance(auth, str):
        return auth
    body = {"contact": _json_object(fields_json, field_name="fields_json")}
    return _dump_json(
        _request_json("PUT", f"{base}/api/3/contacts/{quote(contact_id, safe='')}", json_body=body, headers=auth)
    )


@tool
def activecampaign_list_lists(
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ActiveCampaign contact lists."""
    base, auth = _activecampaign_config("activecampaign_list_lists", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(
        _request_json("GET", f"{base}/api/3/lists", params={"limit": _limit(limit, default=50, max_value=100)}, headers=auth)
    )


@tool
def activecampaign_list_tags(
    search: str = "",
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ActiveCampaign tags."""
    base, auth = _activecampaign_config("activecampaign_list_tags", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/api/3/tags",
            params={"search": search, "limit": _limit(limit, default=50, max_value=100)},
            headers=auth,
        )
    )


@tool
def activecampaign_add_contact_to_list(
    contact_id: str,
    list_id: str,
    status: int = 1,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe or unsubscribe an ActiveCampaign contact to a list. Use status 1 to subscribe, 2 to unsubscribe."""
    base, auth = _activecampaign_config("activecampaign_add_contact_to_list", config)
    if isinstance(auth, str):
        return auth
    body = {"contactList": {"list": list_id, "contact": contact_id, "status": status}}
    return _dump_json(_request_json("POST", f"{base}/api/3/contactLists", json_body=body, headers=auth))


@tool
def activecampaign_add_contact_tag(
    contact_id: str,
    tag_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add an ActiveCampaign tag to a contact."""
    base, auth = _activecampaign_config("activecampaign_add_contact_tag", config)
    if isinstance(auth, str):
        return auth
    body = {"contactTag": {"contact": contact_id, "tag": tag_id}}
    return _dump_json(_request_json("POST", f"{base}/api/3/contactTags", json_body=body, headers=auth))


@tool
def convertkit_get_account(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """Get ConvertKit account details."""
    base, secret = _convertkit_config("convertkit_get_account", config)
    if secret.startswith("[Error]:"):
        return secret
    return _dump_json(_request_json("GET", f"{base}/account", params={"api_secret": secret}))


@tool
def convertkit_list_forms(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """List ConvertKit forms."""
    base, secret = _convertkit_config("convertkit_list_forms", config)
    if secret.startswith("[Error]:"):
        return secret
    return _dump_json(_request_json("GET", f"{base}/forms", params={"api_secret": secret}))


@tool
def convertkit_list_tags(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """List ConvertKit tags."""
    base, secret = _convertkit_config("convertkit_list_tags", config)
    if secret.startswith("[Error]:"):
        return secret
    return _dump_json(_request_json("GET", f"{base}/tags", params={"api_secret": secret}))


@tool
def convertkit_list_subscribers(
    email: str = "",
    limit: int = 50,
    page: int = 1,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ConvertKit subscribers, optionally filtered by email."""
    base, secret = _convertkit_config("convertkit_list_subscribers", config)
    if secret.startswith("[Error]:"):
        return secret
    params = {"api_secret": secret, "email_address": email, "per_page": _limit(limit, default=50, max_value=100), "page": page}
    return _dump_json(_request_json("GET", f"{base}/subscribers", params=params))


@tool
def convertkit_add_subscriber_to_form(
    form_id: str,
    email: str,
    first_name: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe an email address to a ConvertKit form."""
    base, secret = _convertkit_config("convertkit_add_subscriber_to_form", config)
    if secret.startswith("[Error]:"):
        return secret
    body = {"api_secret": secret, "email": email, "first_name": first_name, "fields": _json_object(fields_json, field_name="fields_json")}
    return _dump_json(
        _request_json("POST", f"{base}/forms/{quote(form_id, safe='')}/subscribe", json_body=_filtered(body))
    )


@tool
def convertkit_add_subscriber_to_tag(
    tag_id: str,
    email: str,
    first_name: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe an email address to a ConvertKit tag."""
    base, secret = _convertkit_config("convertkit_add_subscriber_to_tag", config)
    if secret.startswith("[Error]:"):
        return secret
    body = {"api_secret": secret, "email": email, "first_name": first_name, "fields": _json_object(fields_json, field_name="fields_json")}
    return _dump_json(_request_json("POST", f"{base}/tags/{quote(tag_id, safe='')}/subscribe", json_body=_filtered(body)))


@tool
def getresponse_list_campaigns(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """List GetResponse campaigns."""
    base, auth = _getresponse_config("getresponse_list_campaigns", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/campaigns", headers=auth))


@tool
def getresponse_list_contacts(
    email: str = "",
    campaign_id: str = "",
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List GetResponse contacts with optional email and campaign filters."""
    base, auth = _getresponse_config("getresponse_list_contacts", config)
    if isinstance(auth, str):
        return auth
    params: dict[str, Any] = {"perPage": _limit(limit, max_value=100)}
    if email:
        params["query[email]"] = email
    if campaign_id:
        params["query[campaignId]"] = campaign_id
    return _dump_json(_request_json("GET", f"{base}/contacts", params=params, headers=auth))


@tool
def getresponse_get_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a GetResponse contact by ID."""
    base, auth = _getresponse_config("getresponse_get_contact", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/contacts/{quote(contact_id, safe='')}", headers=auth))


@tool
def getresponse_create_contact(
    email: str,
    campaign_id: str,
    name: str = "",
    day_of_cycle: Optional[int] = None,
    custom_fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a GetResponse contact."""
    base, auth = _getresponse_config("getresponse_create_contact", config)
    if isinstance(auth, str):
        return auth
    body = {
        "email": email,
        "name": name,
        "campaign": {"campaignId": campaign_id},
        "dayOfCycle": day_of_cycle,
        "customFieldValues": _json_object(custom_fields_json, field_name="custom_fields_json").get("customFieldValues"),
    }
    return _dump_json(_request_json("POST", f"{base}/contacts", json_body=_filtered(body), headers=auth))


@tool
def getresponse_update_contact(
    contact_id: str,
    name: str = "",
    campaign_id: str = "",
    custom_fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a GetResponse contact."""
    base, auth = _getresponse_config("getresponse_update_contact", config)
    if isinstance(auth, str):
        return auth
    body = {
        "name": name,
        "campaign": {"campaignId": campaign_id} if campaign_id else None,
        "customFieldValues": _json_object(custom_fields_json, field_name="custom_fields_json").get("customFieldValues"),
    }
    return _dump_json(
        _request_json("POST", f"{base}/contacts/{quote(contact_id, safe='')}", json_body=_filtered(body), headers=auth)
    )


@tool
def getresponse_delete_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a GetResponse contact."""
    base, auth = _getresponse_config("getresponse_delete_contact", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("DELETE", f"{base}/contacts/{quote(contact_id, safe='')}", headers=auth))


@tool
def mailerlite_list_subscribers(
    status: str = "",
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List MailerLite subscribers."""
    base, auth = _mailerlite_config("mailerlite_list_subscribers", config)
    if isinstance(auth, str):
        return auth
    params = {"limit": _limit(limit, max_value=100), "filter[status]": status}
    return _dump_json(_request_json("GET", f"{base}/subscribers", params=params, headers=auth))


@tool
def mailerlite_get_subscriber(
    subscriber_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one MailerLite subscriber by ID or email."""
    base, auth = _mailerlite_config("mailerlite_get_subscriber", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/subscribers/{quote(subscriber_id, safe='')}", headers=auth))


@tool
def mailerlite_create_subscriber(
    email: str,
    name: str = "",
    fields_json: str = "",
    groups: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a MailerLite subscriber."""
    base, auth = _mailerlite_config("mailerlite_create_subscriber", config)
    if isinstance(auth, str):
        return auth
    body = {"email": email, "name": name, "fields": _json_object(fields_json, field_name="fields_json")}
    group_list = _csv_to_list(groups)
    if group_list:
        body["groups"] = group_list
    return _dump_json(_request_json("POST", f"{base}/subscribers", json_body=_filtered(body), headers=auth))


@tool
def mailerlite_update_subscriber(
    subscriber_id: str,
    fields_json: str,
    status: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a MailerLite subscriber with a JSON object of fields."""
    base, auth = _mailerlite_config("mailerlite_update_subscriber", config)
    if isinstance(auth, str):
        return auth
    body = _json_object(fields_json, field_name="fields_json")
    if status:
        body["status"] = status
    return _dump_json(
        _request_json("PUT", f"{base}/subscribers/{quote(subscriber_id, safe='')}", json_body=body, headers=auth)
    )


@tool
def mailerlite_list_groups(
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List MailerLite groups."""
    base, auth = _mailerlite_config("mailerlite_list_groups", config)
    if isinstance(auth, str):
        return auth
    return _dump_json(_request_json("GET", f"{base}/groups", params={"limit": _limit(limit, max_value=100)}, headers=auth))


MARKETING_CONTACT_SERVICE_TOOLS = [
    customerio_list_campaigns,
    customerio_get_campaign,
    customerio_upsert_customer,
    customerio_track_event,
    customerio_track_anonymous_event,
    customerio_update_segment,
    iterable_list_lists,
    iterable_get_user,
    iterable_upsert_user,
    iterable_track_event,
    iterable_update_list_subscribers,
    posthog_capture_event,
    posthog_identify,
    posthog_create_alias,
    posthog_track_page_or_screen,
    segment_identify,
    segment_track,
    segment_group,
    activecampaign_list_contacts,
    activecampaign_get_contact,
    activecampaign_sync_contact,
    activecampaign_update_contact,
    activecampaign_list_lists,
    activecampaign_list_tags,
    activecampaign_add_contact_to_list,
    activecampaign_add_contact_tag,
    convertkit_get_account,
    convertkit_list_forms,
    convertkit_list_tags,
    convertkit_list_subscribers,
    convertkit_add_subscriber_to_form,
    convertkit_add_subscriber_to_tag,
    getresponse_list_campaigns,
    getresponse_list_contacts,
    getresponse_get_contact,
    getresponse_create_contact,
    getresponse_update_contact,
    getresponse_delete_contact,
    mailerlite_list_subscribers,
    mailerlite_get_subscriber,
    mailerlite_create_subscriber,
    mailerlite_update_subscriber,
    mailerlite_list_groups,
]
