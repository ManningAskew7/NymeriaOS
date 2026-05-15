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
_ACTIONNETWORK_BASE_URL = "https://actionnetwork.org/api/v2"
_AUTOPILOT_BASE_URL = "https://api2.autopilothq.com/v1"
_EGOI_BASE_URL = "https://api.egoiapp.com"
_VERO_BASE_URL = "https://api.getvero.com/api/v2"


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
    data: Optional[dict[str, Any]] = None,
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
                data=_filtered(data) if data is not None else None,
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


def _token_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    field_names: tuple[str, ...],
    settings_token_name: str,
    settings_base_name: str,
    default_base: str,
    env_var: str,
    display_name: str,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_base_name)
        or default_base
    )
    token = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(settings_token_name)
    if not token:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return _base_url(base), str(token)


def _actionnetwork_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, token_or_error = _token_config(
        provider="actionnetwork",
        provider_aliases=("action_network", "actionnetwork_api", "action_network_api"),
        field_names=("api_key", "apiKey", "token", "value"),
        settings_token_name="actionnetwork_api_key",
        settings_base_name="actionnetwork_base_url",
        default_base=_ACTIONNETWORK_BASE_URL,
        env_var="ACTIONNETWORK_API_KEY",
        display_name="Action Network",
        tool_name=tool_name,
        config=config,
    )
    if token_or_error.startswith("[Error]:"):
        return base, token_or_error
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OSDI-API-Token": token_or_error,
        "User-Agent": "Nymeria",
    }


def _actionnetwork_person_link(base: str, person_id: str) -> dict[str, Any]:
    return {"_links": {"osdi:person": {"href": f"{base}/people/{quote(person_id.strip(), safe='')}"}}}


def _actionnetwork_endpoint(resource: str, record_id: str = "", parent_id: str = "") -> tuple[str, str]:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "event": ("events", "/events"),
        "events": ("events", "/events"),
        "person": ("people", "/people"),
        "people": ("people", "/people"),
        "petition": ("petitions", "/petitions"),
        "petitions": ("petitions", "/petitions"),
        "tag": ("tags", "/tags"),
        "tags": ("tags", "/tags"),
    }
    if key in mapping:
        embedded_key, endpoint = mapping[key]
    elif key in {"attendance", "attendances"}:
        if not parent_id.strip():
            raise ValueError("parent_id must be the event ID for attendance records")
        embedded_key = "attendances"
        endpoint = f"/events/{quote(parent_id.strip(), safe='')}/attendances"
    elif key in {"signature", "signatures"}:
        if not parent_id.strip():
            raise ValueError("parent_id must be the petition ID for signature records")
        embedded_key = "signatures"
        endpoint = f"/petitions/{quote(parent_id.strip(), safe='')}/signatures"
    elif key in {"person_tag", "person_tags", "tagging", "taggings"}:
        if not parent_id.strip():
            raise ValueError("parent_id must be the tag ID for person tag records")
        embedded_key = "taggings"
        endpoint = f"/tags/{quote(parent_id.strip(), safe='')}/taggings"
    else:
        raise ValueError("resource must be event, person, petition, tag, attendance, signature, or person_tag")
    if record_id.strip():
        endpoint = f"{endpoint}/{quote(record_id.strip(), safe='')}"
    return embedded_key, endpoint


def _actionnetwork_list_items(data: Any, embedded_key: str) -> list[Any]:
    if not isinstance(data, dict):
        return []
    embedded = data.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    return list(embedded.get(f"osdi:{embedded_key}") or [])


def _autopilot_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, key_or_error = _token_config(
        provider="autopilot",
        provider_aliases=("autopilot_api",),
        field_names=("api_key", "apiKey", "token", "value"),
        settings_token_name="autopilot_api_key",
        settings_base_name="autopilot_base_url",
        default_base=_AUTOPILOT_BASE_URL,
        env_var="AUTOPILOT_API_KEY",
        display_name="Autopilot",
        tool_name=tool_name,
        config=config,
    )
    if key_or_error.startswith("[Error]:"):
        return base, key_or_error
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "autopilotapikey": key_or_error,
    }


def _egoi_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, key_or_error = _token_config(
        provider="egoi",
        provider_aliases=("e_goi", "egoi_api", "e_goi_api"),
        field_names=("api_key", "apiKey", "token", "value"),
        settings_token_name="egoi_api_key",
        settings_base_name="egoi_base_url",
        default_base=_EGOI_BASE_URL,
        env_var="EGOI_API_KEY",
        display_name="E-goi",
        tool_name=tool_name,
        config=config,
    )
    if key_or_error.startswith("[Error]:"):
        return base, key_or_error
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Apikey": key_or_error,
        "User-Agent": "Nymeria",
    }


def _vero_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    return _token_config(
        provider="vero",
        provider_aliases=("vero_api",),
        field_names=("auth_token", "authToken", "api_key", "apiKey", "token", "value"),
        settings_token_name="vero_auth_token",
        settings_base_name="vero_base_url",
        default_base=_VERO_BASE_URL,
        env_var="VERO_AUTH_TOKEN",
        display_name="Vero",
        tool_name=tool_name,
        config=config,
    )


def _vero_request(
    tool_name: str,
    method: str,
    endpoint: str,
    body: Optional[dict[str, Any]] = None,
    *,
    config: Optional[RunnableConfig] = None,
) -> Any:
    base, token_or_error = _vero_config(tool_name, config)
    if token_or_error is None or token_or_error.startswith("[Error]:"):
        return token_or_error or ""
    return _request_json(
        method,
        f"{base}{endpoint}",
        data={"auth_token": token_or_error, **(body or {})},
        headers={"Accept": "application/json", "User-Agent": "Nymeria"},
    )


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


@tool
def actionnetwork_list_records(
    resource: str,
    parent_id: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Action Network records.

    Args:
        resource: One of event, person, petition, tag, attendance, signature, or person_tag.
        parent_id: Event ID for attendances, petition ID for signatures, or tag ID for person_tag records.
        limit: Number of records to return, 1-100.
    """
    try:
        base, auth = _actionnetwork_config("actionnetwork_list_records", config)
        if isinstance(auth, str):
            return auth
        embedded_key, endpoint = _actionnetwork_endpoint(resource, parent_id=parent_id)
        page = 1
        items: list[Any] = []
        max_items = _limit(limit, default=25, max_value=100)
        while len(items) < max_items:
            data = _request_json("GET", f"{base}{endpoint}", params={"per_page": 25, "page": page}, headers=auth)
            chunk = _actionnetwork_list_items(data, embedded_key)
            items.extend(chunk)
            if len(items) >= max_items or not (isinstance(data, dict) and data.get("_links", {}).get("next")):
                break
            page += 1
        return _dump_json(items[:max_items])
    except Exception as e:
        logger.error("actionnetwork_list_records failed", exc_info=True)
        return f"[Error]: Action Network list failed: {e}"


@tool
def actionnetwork_get_record(
    resource: str,
    record_id: str,
    parent_id: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Action Network record by ID.

    Args:
        resource: One of event, person, petition, tag, attendance, signature, or person_tag.
        record_id: Record ID.
        parent_id: Event ID for attendances, petition ID for signatures, or tag ID for person_tag records.
    """
    try:
        if not record_id.strip():
            return "[Error]: record_id is required."
        base, auth = _actionnetwork_config("actionnetwork_get_record", config)
        if isinstance(auth, str):
            return auth
        _, endpoint = _actionnetwork_endpoint(resource, record_id=record_id, parent_id=parent_id)
        return _dump_json(_request_json("GET", f"{base}{endpoint}", headers=auth))
    except Exception as e:
        logger.error("actionnetwork_get_record failed", exc_info=True)
        return f"[Error]: Action Network lookup failed: {e}"


@tool
def actionnetwork_create_person(
    email: str,
    given_name: str = "",
    family_name: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network person.

    Args:
        email: Primary email address.
        given_name: Optional given name.
        family_name: Optional family name.
        fields_json: Optional extra person fields as JSON.
    """
    if not email.strip():
        return "[Error]: email is required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_person", config)
        if isinstance(auth, str):
            return auth
        person = {
            **_json_object(fields_json, field_name="fields_json"),
            "given_name": given_name.strip(),
            "family_name": family_name.strip(),
            "email_addresses": [{"address": email.strip(), "primary": True}],
        }
        return _dump_json(_request_json("POST", f"{base}/people", json_body={"person": _filtered(person)}, headers=auth))
    except Exception as e:
        logger.error("actionnetwork_create_person failed", exc_info=True)
        return f"[Error]: Action Network person creation failed: {e}"


@tool
def actionnetwork_update_person(
    person_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an Action Network person with a JSON object of fields."""
    if not person_id.strip():
        return "[Error]: person_id is required."
    try:
        fields = _json_object(fields_json, field_name="fields_json",)
        if not fields:
            return "[Error]: fields_json cannot be empty."
        base, auth = _actionnetwork_config("actionnetwork_update_person", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json("PUT", f"{base}/people/{quote(person_id.strip(), safe='')}", json_body=fields, headers=auth)
        )
    except Exception as e:
        logger.error("actionnetwork_update_person failed", exc_info=True)
        return f"[Error]: Action Network person update failed: {e}"


@tool
def actionnetwork_create_event(
    title: str,
    origin_system: str = "nymeria",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network event."""
    if not title.strip():
        return "[Error]: title is required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_event", config)
        if isinstance(auth, str):
            return auth
        body = {"origin_system": origin_system.strip() or "nymeria", "title": title.strip()}
        body.update(_json_object(fields_json, field_name="fields_json"))
        return _dump_json(_request_json("POST", f"{base}/events", json_body=body, headers=auth))
    except Exception as e:
        logger.error("actionnetwork_create_event failed", exc_info=True)
        return f"[Error]: Action Network event creation failed: {e}"


@tool
def actionnetwork_create_petition(
    title: str,
    origin_system: str = "nymeria",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network petition."""
    if not title.strip():
        return "[Error]: title is required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_petition", config)
        if isinstance(auth, str):
            return auth
        body = {"origin_system": origin_system.strip() or "nymeria", "title": title.strip()}
        body.update(_json_object(fields_json, field_name="fields_json"))
        return _dump_json(_request_json("POST", f"{base}/petitions", json_body=body, headers=auth))
    except Exception as e:
        logger.error("actionnetwork_create_petition failed", exc_info=True)
        return f"[Error]: Action Network petition creation failed: {e}"


@tool
def actionnetwork_create_attendance(
    event_id: str,
    person_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network attendance for an event and person."""
    if not event_id.strip() or not person_id.strip():
        return "[Error]: event_id and person_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_attendance", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json(
                "POST",
                f"{base}/events/{quote(event_id.strip(), safe='')}/attendances",
                json_body=_actionnetwork_person_link(base, person_id),
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_create_attendance failed", exc_info=True)
        return f"[Error]: Action Network attendance creation failed: {e}"


@tool
def actionnetwork_create_signature(
    petition_id: str,
    person_id: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network petition signature for a person."""
    if not petition_id.strip() or not person_id.strip():
        return "[Error]: petition_id and person_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_signature", config)
        if isinstance(auth, str):
            return auth
        body = _actionnetwork_person_link(base, person_id)
        body.update(_json_object(fields_json, field_name="fields_json"))
        return _dump_json(
            _request_json(
                "POST",
                f"{base}/petitions/{quote(petition_id.strip(), safe='')}/signatures",
                json_body=body,
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_create_signature failed", exc_info=True)
        return f"[Error]: Action Network signature creation failed: {e}"


@tool
def actionnetwork_add_person_tag(
    tag_id: str,
    person_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Tag an Action Network person."""
    if not tag_id.strip() or not person_id.strip():
        return "[Error]: tag_id and person_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_add_person_tag", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json(
                "POST",
                f"{base}/tags/{quote(tag_id.strip(), safe='')}/taggings",
                json_body=_actionnetwork_person_link(base, person_id),
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_add_person_tag failed", exc_info=True)
        return f"[Error]: Action Network tag add failed: {e}"


@tool
def actionnetwork_remove_person_tag(
    tag_id: str,
    tagging_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove an Action Network person tag by tagging ID."""
    if not tag_id.strip() or not tagging_id.strip():
        return "[Error]: tag_id and tagging_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_remove_person_tag", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json(
                "DELETE",
                f"{base}/tags/{quote(tag_id.strip(), safe='')}/taggings/{quote(tagging_id.strip(), safe='')}",
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_remove_person_tag failed", exc_info=True)
        return f"[Error]: Action Network tag removal failed: {e}"


@tool
def autopilot_list_contacts(
    list_id: str = "",
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Autopilot contacts, optionally scoped to a list."""
    try:
        base, auth = _autopilot_config("autopilot_list_contacts", config)
        if isinstance(auth, str):
            return auth
        endpoint = f"/list/{quote(list_id.strip(), safe='')}/contacts" if list_id.strip() else "/contacts"
        data = _request_json("GET", f"{base}{endpoint}", params={"limit": _limit(limit, default=100, max_value=500)}, headers=auth)
        contacts = data.get("contacts", data) if isinstance(data, dict) else data
        if isinstance(contacts, list):
            contacts = contacts[: _limit(limit, default=100, max_value=500)]
        return _dump_json(contacts)
    except Exception as e:
        logger.error("autopilot_list_contacts failed", exc_info=True)
        return f"[Error]: Autopilot contact list failed: {e}"


@tool
def autopilot_get_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Autopilot contact by ID."""
    if not contact_id.strip():
        return "[Error]: contact_id is required."
    try:
        base, auth = _autopilot_config("autopilot_get_contact", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/contact/{quote(contact_id.strip(), safe='')}", headers=auth))
    except Exception as e:
        logger.error("autopilot_get_contact failed", exc_info=True)
        return f"[Error]: Autopilot contact lookup failed: {e}"


@tool
def autopilot_upsert_contact(
    email: str,
    fields_json: str = "",
    list_id: str = "",
    session_id: str = "",
    new_email: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update an Autopilot contact."""
    if not email.strip():
        return "[Error]: email is required."
    try:
        base, auth = _autopilot_config("autopilot_upsert_contact", config)
        if isinstance(auth, str):
            return auth
        contact = {"Email": email.strip(), **_json_object(fields_json, field_name="fields_json")}
        if list_id.strip():
            contact["_autopilot_list"] = list_id.strip()
        if session_id.strip():
            contact["_autopilot_session_id"] = session_id.strip()
        if new_email.strip():
            contact["_NewEmail"] = new_email.strip()
        return _dump_json(_request_json("POST", f"{base}/contact", json_body={"contact": contact}, headers=auth))
    except Exception as e:
        logger.error("autopilot_upsert_contact failed", exc_info=True)
        return f"[Error]: Autopilot contact upsert failed: {e}"


@tool
def autopilot_delete_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete an Autopilot contact."""
    if not contact_id.strip():
        return "[Error]: contact_id is required."
    try:
        base, auth = _autopilot_config("autopilot_delete_contact", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("DELETE", f"{base}/contact/{quote(contact_id.strip(), safe='')}", headers=auth)
        return _dump_json(data or {"success": True})
    except Exception as e:
        logger.error("autopilot_delete_contact failed", exc_info=True)
        return f"[Error]: Autopilot contact delete failed: {e}"


@tool
def autopilot_list_lists(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Autopilot lists."""
    try:
        base, auth = _autopilot_config("autopilot_list_lists", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("GET", f"{base}/lists", headers=auth)
        lists = data.get("lists", data) if isinstance(data, dict) else data
        if isinstance(lists, list):
            lists = lists[: _limit(limit, default=100, max_value=500)]
        return _dump_json(lists)
    except Exception as e:
        logger.error("autopilot_list_lists failed", exc_info=True)
        return f"[Error]: Autopilot list lookup failed: {e}"


@tool
def autopilot_create_list(
    name: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Autopilot list."""
    if not name.strip():
        return "[Error]: name is required."
    try:
        base, auth = _autopilot_config("autopilot_create_list", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("POST", f"{base}/list", json_body={"name": name.strip()}, headers=auth))
    except Exception as e:
        logger.error("autopilot_create_list failed", exc_info=True)
        return f"[Error]: Autopilot list creation failed: {e}"


@tool
def autopilot_update_contact_list_membership(
    list_id: str,
    contact_id: str,
    action: str = "add",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add, remove, or check an Autopilot contact's list membership."""
    normalized = action.strip().lower()
    if normalized not in {"add", "remove", "check"}:
        return '[Error]: action must be "add", "remove", or "check".'
    if not list_id.strip() or not contact_id.strip():
        return "[Error]: list_id and contact_id are required."
    try:
        base, auth = _autopilot_config("autopilot_update_contact_list_membership", config)
        if isinstance(auth, str):
            return auth
        method = {"add": "POST", "remove": "DELETE", "check": "GET"}[normalized]
        data = _request_json(
            method,
            f"{base}/list/{quote(list_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}",
            headers=auth,
        )
        if normalized == "check":
            return _dump_json({"exists": True, "response": data})
        return _dump_json(data or {"success": True})
    except Exception as e:
        if normalized == "check":
            return _dump_json({"exists": False, "error": str(e)})
        logger.error("autopilot_update_contact_list_membership failed", exc_info=True)
        return f"[Error]: Autopilot list membership update failed: {e}"


@tool
def autopilot_add_contact_to_journey(
    trigger_id: str,
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add an Autopilot contact to a journey trigger."""
    if not trigger_id.strip() or not contact_id.strip():
        return "[Error]: trigger_id and contact_id are required."
    try:
        base, auth = _autopilot_config("autopilot_add_contact_to_journey", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "POST",
            f"{base}/trigger/{quote(trigger_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}",
            headers=auth,
        )
        return _dump_json(data or {"success": True})
    except Exception as e:
        logger.error("autopilot_add_contact_to_journey failed", exc_info=True)
        return f"[Error]: Autopilot journey update failed: {e}"


@tool
def egoi_list_lists(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List E-goi lists."""
    try:
        base, auth = _egoi_config("egoi_list_lists", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("GET", f"{base}/lists", params={"offset": 0, "count": _limit(limit, default=100, max_value=500)}, headers=auth)
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("egoi_list_lists failed", exc_info=True)
        return f"[Error]: E-goi list lookup failed: {e}"


@tool
def egoi_list_contacts(
    list_id: str,
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List contacts in an E-goi list."""
    if not list_id.strip():
        return "[Error]: list_id is required."
    try:
        base, auth = _egoi_config("egoi_list_contacts", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "GET",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts",
            params={"offset": 0, "count": _limit(limit, default=100, max_value=500)},
            headers=auth,
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("egoi_list_contacts failed", exc_info=True)
        return f"[Error]: E-goi contact list failed: {e}"


@tool
def egoi_get_contact(
    list_id: str,
    contact_id: str = "",
    email: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an E-goi contact by contact ID or email."""
    if not list_id.strip() or not (contact_id.strip() or email.strip()):
        return "[Error]: list_id and one of contact_id or email are required."
    try:
        base, auth = _egoi_config("egoi_get_contact", config)
        if isinstance(auth, str):
            return auth
        if contact_id.strip():
            url = f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(contact_id.strip(), safe='')}"
            data = _request_json("GET", url, headers=auth)
        else:
            url = f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts"
            data = _request_json("GET", url, params={"email": email.strip()}, headers=auth)
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("egoi_get_contact failed", exc_info=True)
        return f"[Error]: E-goi contact lookup failed: {e}"


def _egoi_contact_body(email: str = "", fields_json: str = "") -> dict[str, Any]:
    fields = _json_object(fields_json, field_name="fields_json")
    base_fields = fields.pop("base", fields)
    extra_fields = fields.pop("extra", [])
    body = {"base": _filtered({"email": email.strip(), **base_fields}), "extra": extra_fields}
    return body


def _egoi_attach_tags(base: str, auth: dict[str, str], list_id: str, contact_id: Any, tag_ids: str) -> None:
    for tag_id in _csv_to_list(tag_ids):
        _request_json(
            "POST",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/actions/attach-tag",
            json_body={"tag_id": tag_id, "contacts": [contact_id]},
            headers=auth,
        )


@tool
def egoi_create_contact(
    list_id: str,
    email: str,
    fields_json: str = "",
    tag_ids: str = "",
    resolve: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an E-goi contact."""
    if not list_id.strip() or not email.strip():
        return "[Error]: list_id and email are required."
    try:
        base, auth = _egoi_config("egoi_create_contact", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "POST",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts",
            json_body=_egoi_contact_body(email, fields_json),
            headers=auth,
        )
        contact_id = data.get("contact_id") if isinstance(data, dict) else None
        if contact_id and tag_ids.strip():
            _egoi_attach_tags(base, auth, list_id, contact_id, tag_ids)
        if resolve and contact_id:
            data = _request_json("GET", f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(str(contact_id), safe='')}", headers=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("egoi_create_contact failed", exc_info=True)
        return f"[Error]: E-goi contact creation failed: {e}"


@tool
def egoi_update_contact(
    list_id: str,
    contact_id: str,
    fields_json: str,
    tag_ids: str = "",
    resolve: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an E-goi contact."""
    if not list_id.strip() or not contact_id.strip():
        return "[Error]: list_id and contact_id are required."
    try:
        base, auth = _egoi_config("egoi_update_contact", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "PATCH",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(contact_id.strip(), safe='')}",
            json_body=_egoi_contact_body("", fields_json),
            headers=auth,
        )
        if tag_ids.strip():
            _egoi_attach_tags(base, auth, list_id, contact_id, tag_ids)
        if resolve:
            data = _request_json("GET", f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(contact_id.strip(), safe='')}", headers=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("egoi_update_contact failed", exc_info=True)
        return f"[Error]: E-goi contact update failed: {e}"


@tool
def vero_identify_user(
    user_id: str,
    email: str = "",
    data_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update a Vero user profile."""
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        body = {"id": user_id.strip(), "email": email.strip(), "data": json.dumps(_json_object(data_json, field_name="data_json")) if data_json.strip() else ""}
        data = _vero_request("vero_identify_user", "POST", "/users/track", body, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_identify_user failed", exc_info=True)
        return f"[Error]: Vero user identify failed: {e}"


@tool
def vero_alias_user(
    user_id: str,
    new_user_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Alias a Vero user ID to a new user ID."""
    if not user_id.strip() or not new_user_id.strip():
        return "[Error]: user_id and new_user_id are required."
    try:
        data = _vero_request("vero_alias_user", "PUT", "/users/reidentify", {"id": user_id.strip(), "new_id": new_user_id.strip()}, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_alias_user failed", exc_info=True)
        return f"[Error]: Vero user alias failed: {e}"


@tool
def vero_update_user_subscription(
    user_id: str,
    action: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Unsubscribe, resubscribe, or delete a Vero user."""
    normalized = action.strip().lower()
    if normalized not in {"unsubscribe", "resubscribe", "delete"}:
        return '[Error]: action must be "unsubscribe", "resubscribe", or "delete".'
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        data = _vero_request("vero_update_user_subscription", "POST", f"/users/{normalized}", {"id": user_id.strip()}, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_update_user_subscription failed", exc_info=True)
        return f"[Error]: Vero user subscription update failed: {e}"


@tool
def vero_update_user_tags(
    user_id: str,
    add_tags: str = "",
    remove_tags: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add or remove Vero user tags."""
    if not user_id.strip():
        return "[Error]: user_id is required."
    if not add_tags.strip() and not remove_tags.strip():
        return "[Error]: add_tags or remove_tags is required."
    try:
        body: dict[str, Any] = {"id": user_id.strip()}
        if add_tags.strip():
            body["add"] = json.dumps(_csv_to_list(add_tags))
        if remove_tags.strip():
            body["remove"] = json.dumps(_csv_to_list(remove_tags))
        data = _vero_request("vero_update_user_tags", "PUT", "/users/tags/edit", body, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_update_user_tags failed", exc_info=True)
        return f"[Error]: Vero user tag update failed: {e}"


@tool
def vero_track_event(
    user_id: str,
    email: str,
    event_name: str,
    data_json: str = "",
    extras_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a Vero event for a user."""
    if not user_id.strip() or not email.strip() or not event_name.strip():
        return "[Error]: user_id, email, and event_name are required."
    try:
        body = {
            "identity": json.dumps({"id": user_id.strip(), "email": email.strip()}),
            "email": email.strip(),
            "event_name": event_name.strip(),
            "data": json.dumps(_json_object(data_json, field_name="data_json")) if data_json.strip() else "",
            "extras": json.dumps(_json_object(extras_json, field_name="extras_json")) if extras_json.strip() else "",
        }
        data = _vero_request("vero_track_event", "POST", "/events/track", body, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_track_event failed", exc_info=True)
        return f"[Error]: Vero event tracking failed: {e}"


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
    actionnetwork_list_records,
    actionnetwork_get_record,
    actionnetwork_create_person,
    actionnetwork_update_person,
    actionnetwork_create_event,
    actionnetwork_create_petition,
    actionnetwork_create_attendance,
    actionnetwork_create_signature,
    actionnetwork_add_person_tag,
    actionnetwork_remove_person_tag,
    autopilot_list_contacts,
    autopilot_get_contact,
    autopilot_upsert_contact,
    autopilot_delete_contact,
    autopilot_list_lists,
    autopilot_create_list,
    autopilot_update_contact_list_membership,
    autopilot_add_contact_to_journey,
    egoi_list_lists,
    egoi_list_contacts,
    egoi_get_contact,
    egoi_create_contact,
    egoi_update_contact,
    vero_identify_user,
    vero_alias_user,
    vero_update_user_subscription,
    vero_update_user_tags,
    vero_track_event,
]
