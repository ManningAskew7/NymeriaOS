"""Personal health, activity, and home device service tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
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
_OURA_BASE_URL = "https://api.ouraring.com/v2"
_STRAVA_BASE_URL = "https://www.strava.com/api/v3"
_PHILIPS_HUE_BASE_URL = "https://api.meethue.com/route"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


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


def _csv_to_list(value: str, *, max_items: int | None = None) -> list[str]:
    parts = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    return parts[:max_items] if max_items is not None else parts


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    form_data: Optional[dict[str, Any]] = None,
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
                data=_filtered(form_data) if form_data is not None else None,
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


def _bearer_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    token_fields: tuple[str, ...],
    token_setting: str,
    base_setting: str,
    default_base: str,
    tool_name: str,
    config: Optional[RunnableConfig],
    display_name: str,
    env_var: str,
) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=BASE_URL_ALIAS_FIELDS,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(base_setting)
        or default_base
    )
    token = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=token_fields,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(token_setting)
    if not token:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=token_fields,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _oura_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider="oura",
        provider_aliases=("oura_api",),
        token_fields=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
        token_setting="oura_access_token",
        base_setting="oura_base_url",
        default_base=_OURA_BASE_URL,
        tool_name=tool_name,
        config=config,
        display_name="Oura",
        env_var="OURA_ACCESS_TOKEN",
    )


def _strava_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider="strava",
        provider_aliases=("strava_oauth2", "strava_oauth2_api"),
        token_fields=("access_token", "accessToken", "token", "value"),
        token_setting="strava_access_token",
        base_setting="strava_base_url",
        default_base=_STRAVA_BASE_URL,
        tool_name=tool_name,
        config=config,
        display_name="Strava",
        env_var="STRAVA_ACCESS_TOKEN",
    )


def _homeassistant_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="homeassistant",
            provider_aliases=("home_assistant", "homeassistant_api"),
            field_names=BASE_URL_ALIAS_FIELDS,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("homeassistant_base_url")
        or ""
    )
    token = _credential_value(
        provider="homeassistant",
        provider_aliases=("home_assistant", "homeassistant_api"),
        field_names=("access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("homeassistant_access_token")
    if not base or not token:
        base = base or "http://homeassistant.local:8123/api"
        return _base_url(base), _setup_hint(
            provider="homeassistant",
            field_names=("base_url", "access_token"),
            tool_name=tool_name,
            env_var="HOMEASSISTANT_BASE_URL and HOMEASSISTANT_ACCESS_TOKEN",
            display_name="Home Assistant",
        )
    clean = _base_url(base)
    if not clean.endswith("/api"):
        clean = f"{clean}/api"
    return clean, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _philips_hue_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str]] | str:
    base, headers_or_hint = _bearer_config(
        provider="philips_hue",
        provider_aliases=("philips_hue_oauth2", "philips_hue_api", "hue"),
        token_fields=("access_token", "accessToken", "token", "value"),
        token_setting="philips_hue_access_token",
        base_setting="philips_hue_base_url",
        default_base=_PHILIPS_HUE_BASE_URL,
        tool_name=tool_name,
        config=config,
        display_name="Philips Hue",
        env_var="PHILIPS_HUE_ACCESS_TOKEN and PHILIPS_HUE_USERNAME",
    )
    if isinstance(headers_or_hint, str):
        return headers_or_hint
    username = _credential_value(
        provider="philips_hue",
        provider_aliases=("philips_hue_oauth2", "philips_hue_api", "hue"),
        field_names=("username", "user", "bridge_username", "bridgeUsername"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("philips_hue_username")
    if not username:
        return _setup_hint(
            provider="philips_hue",
            field_names=("access_token", "username"),
            tool_name=tool_name,
            env_var="PHILIPS_HUE_ACCESS_TOKEN and PHILIPS_HUE_USERNAME",
            display_name="Philips Hue",
        )
    return base, username, {**headers_or_hint, "Content-Type": "application/json"}


def _require_headers(config_result: tuple[str, dict[str, str] | str]) -> tuple[str, dict[str, str]] | str:
    base, headers = config_result
    if isinstance(headers, str):
        return headers
    return base, headers


@tool
def oura_get_profile(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get the authenticated Oura personal profile."""
    resolved = _require_headers(_oura_config("oura_get_profile", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/usercollection/personal_info", headers=headers))


@tool
def oura_get_daily_activity(
    start_date: str = "",
    end_date: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Oura daily activity summaries."""
    resolved = _require_headers(_oura_config("oura_get_daily_activity", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    data = _request_json(
        "GET",
        f"{base}/usercollection/daily_activity",
        params={"start_date": start_date, "end_date": end_date},
        headers=headers,
    )
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data["data"] = data["data"][: _limit(limit)]
    return _dump_json(data)


@tool
def oura_get_daily_readiness(
    start_date: str = "",
    end_date: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Oura daily readiness summaries."""
    resolved = _require_headers(_oura_config("oura_get_daily_readiness", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    data = _request_json(
        "GET",
        f"{base}/usercollection/daily_readiness",
        params={"start_date": start_date, "end_date": end_date},
        headers=headers,
    )
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data["data"] = data["data"][: _limit(limit)]
    return _dump_json(data)


@tool
def oura_get_daily_sleep(
    start_date: str = "",
    end_date: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Oura daily sleep summaries."""
    resolved = _require_headers(_oura_config("oura_get_daily_sleep", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    data = _request_json(
        "GET",
        f"{base}/usercollection/daily_sleep",
        params={"start_date": start_date, "end_date": end_date},
        headers=headers,
    )
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data["data"] = data["data"][: _limit(limit)]
    return _dump_json(data)


@tool
def strava_list_activities(
    before: Optional[int] = None,
    after: Optional[int] = None,
    limit: int = 30,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Strava activities for the authenticated athlete."""
    resolved = _require_headers(_strava_config("strava_list_activities", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/athlete/activities",
            params={"before": before, "after": after, "per_page": _limit(limit, default=30, max_value=200)},
            headers=headers,
        )
    )


@tool
def strava_get_activity(
    activity_id: str,
    include_all_efforts: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Strava activity by ID."""
    resolved = _require_headers(_strava_config("strava_get_activity", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/activities/{quote(activity_id, safe='')}",
            params={"include_all_efforts": include_all_efforts},
            headers=headers,
        )
    )


@tool
def strava_create_activity(
    name: str,
    sport_type: str,
    start_date_local: str,
    elapsed_time_seconds: int,
    description: str = "",
    distance_meters: Optional[float] = None,
    trainer: bool = False,
    commute: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a manual Strava activity."""
    resolved = _require_headers(_strava_config("strava_create_activity", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    body = _filtered(
        {
            "name": name,
            "sport_type": sport_type,
            "start_date_local": start_date_local,
            "elapsed_time": elapsed_time_seconds,
            "description": description,
            "distance": distance_meters,
            "trainer": 1 if trainer else None,
            "commute": 1 if commute else None,
        }
    )
    return _dump_json(_request_json("POST", f"{base}/activities", form_data=body, headers=headers))


@tool
def strava_update_activity(
    activity_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Strava activity from a JSON object."""
    body = _json_object(fields_json)
    if not body:
        return "[Error]: fields_json must include at least one field to update."
    resolved = _require_headers(_strava_config("strava_update_activity", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json("PUT", f"{base}/activities/{quote(activity_id, safe='')}", form_data=body, headers=headers)
    )


@tool
def strava_list_activity_comments(
    activity_id: str,
    limit: int = 30,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List comments on a Strava activity."""
    resolved = _require_headers(_strava_config("strava_list_activity_comments", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/activities/{quote(activity_id, safe='')}/comments",
            params={"per_page": _limit(limit, default=30, max_value=200)},
            headers=headers,
        )
    )


@tool
def strava_get_activity_streams(
    activity_id: str,
    keys: str = "time,distance,latlng,altitude,heartrate,cadence,watts,temp,moving,grade_smooth,velocity_smooth",
    key_by_type: bool = True,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Strava activity streams for selected stream keys."""
    resolved = _require_headers(_strava_config("strava_get_activity_streams", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "GET",
            f"{base}/activities/{quote(activity_id, safe='')}/streams",
            params={"keys": ",".join(_csv_to_list(keys)), "key_by_type": key_by_type},
            headers=headers,
        )
    )


@tool
def homeassistant_get_config(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Home Assistant configuration metadata."""
    resolved = _require_headers(_homeassistant_config("homeassistant_get_config", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/config", headers=headers))


@tool
def homeassistant_check_config(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Run Home Assistant core configuration checks."""
    resolved = _require_headers(_homeassistant_config("homeassistant_check_config", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("POST", f"{base}/config/core/check_config", headers=headers))


@tool
def homeassistant_list_states(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Home Assistant entity states."""
    resolved = _require_headers(_homeassistant_config("homeassistant_list_states", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    data = _request_json("GET", f"{base}/states", headers=headers)
    if isinstance(data, list):
        data = data[: _limit(limit, default=100, max_value=500)]
    return _dump_json(data)


@tool
def homeassistant_get_state(
    entity_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Home Assistant entity state."""
    resolved = _require_headers(_homeassistant_config("homeassistant_get_state", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(_request_json("GET", f"{base}/states/{quote(entity_id, safe='')}", headers=headers))


@tool
def homeassistant_set_state(
    entity_id: str,
    state: str,
    attributes_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update a Home Assistant entity state."""
    resolved = _require_headers(_homeassistant_config("homeassistant_set_state", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    body = {"state": state, "attributes": _json_object(attributes_json, label="attributes_json")}
    return _dump_json(
        _request_json("POST", f"{base}/states/{quote(entity_id, safe='')}", json_body=body, headers=headers)
    )


@tool
def homeassistant_list_services(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Home Assistant service domains."""
    resolved = _require_headers(_homeassistant_config("homeassistant_list_services", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    data = _request_json("GET", f"{base}/services", headers=headers)
    if isinstance(data, list):
        data = data[: _limit(limit, default=100, max_value=500)]
    return _dump_json(data)


@tool
def homeassistant_call_service(
    domain: str,
    service: str,
    data_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Call a Home Assistant service."""
    resolved = _require_headers(_homeassistant_config("homeassistant_call_service", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "POST",
            f"{base}/services/{quote(domain, safe='')}/{quote(service, safe='')}",
            json_body=_json_object(data_json, label="data_json"),
            headers=headers,
        )
    )


@tool
def homeassistant_list_events(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Home Assistant event types."""
    resolved = _require_headers(_homeassistant_config("homeassistant_list_events", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    data = _request_json("GET", f"{base}/events", headers=headers)
    if isinstance(data, list):
        data = data[: _limit(limit, default=100, max_value=500)]
    return _dump_json(data)


@tool
def homeassistant_fire_event(
    event_type: str,
    data_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Fire a Home Assistant event."""
    resolved = _require_headers(_homeassistant_config("homeassistant_fire_event", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json(
            "POST",
            f"{base}/events/{quote(event_type, safe='')}",
            json_body=_json_object(data_json, label="data_json"),
            headers=headers,
        )
    )


@tool
def homeassistant_render_template(
    template: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Render a Home Assistant template."""
    resolved = _require_headers(_homeassistant_config("homeassistant_render_template", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    return _dump_json(
        _request_json("POST", f"{base}/template", json_body={"template": template}, headers=headers)
    )


@tool
def homeassistant_get_logbook(
    start_time: str = "",
    end_time: str = "",
    entity_id: str = "",
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Home Assistant logbook entries."""
    resolved = _require_headers(_homeassistant_config("homeassistant_get_logbook", config))
    if isinstance(resolved, str):
        return resolved
    base, headers = resolved
    path = f"/logbook/{quote(start_time, safe=':-TZ+')}" if start_time else "/logbook"
    data = _request_json(
        "GET",
        f"{base}{path}",
        params={"end_time": end_time, "entity": entity_id},
        headers=headers,
    )
    if isinstance(data, list):
        data = data[: _limit(limit, default=100, max_value=500)]
    return _dump_json(data)


@tool
def philips_hue_list_lights(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Philips Hue lights."""
    resolved = _philips_hue_config("philips_hue_list_lights", config)
    if isinstance(resolved, str):
        return resolved
    base, username, headers = resolved
    data = _request_json("GET", f"{base}/api/{quote(username, safe='')}/lights", headers=headers)
    if isinstance(data, dict):
        data = dict(list(data.items())[: _limit(limit, default=100, max_value=500)])
    return _dump_json(data)


@tool
def philips_hue_get_light(
    light_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Philips Hue light."""
    resolved = _philips_hue_config("philips_hue_get_light", config)
    if isinstance(resolved, str):
        return resolved
    base, username, headers = resolved
    return _dump_json(
        _request_json("GET", f"{base}/api/{quote(username, safe='')}/lights/{quote(light_id, safe='')}", headers=headers)
    )


@tool
def philips_hue_update_light_state(
    light_id: str,
    on: Optional[bool] = None,
    brightness: Optional[int] = None,
    hue: Optional[int] = None,
    saturation: Optional[int] = None,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Philips Hue light state."""
    resolved = _philips_hue_config("philips_hue_update_light_state", config)
    if isinstance(resolved, str):
        return resolved
    base, username, headers = resolved
    body = _filtered(
        {
            "on": on,
            "bri": brightness,
            "hue": hue,
            "sat": saturation,
            **_json_object(fields_json),
        }
    )
    if not body:
        return "[Error]: supply at least one state field."
    return _dump_json(
        _request_json(
            "PUT",
            f"{base}/api/{quote(username, safe='')}/lights/{quote(light_id, safe='')}/state",
            json_body=body,
            headers=headers,
        )
    )


@tool
def philips_hue_delete_light(
    light_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Philips Hue light from the bridge."""
    resolved = _philips_hue_config("philips_hue_delete_light", config)
    if isinstance(resolved, str):
        return resolved
    base, username, headers = resolved
    return _dump_json(
        _request_json(
            "DELETE",
            f"{base}/api/{quote(username, safe='')}/lights/{quote(light_id, safe='')}",
            headers=headers,
        )
    )


PERSONAL_DEVICE_SERVICE_TOOLS = [
    oura_get_profile,
    oura_get_daily_activity,
    oura_get_daily_readiness,
    oura_get_daily_sleep,
    strava_list_activities,
    strava_get_activity,
    strava_create_activity,
    strava_update_activity,
    strava_list_activity_comments,
    strava_get_activity_streams,
    homeassistant_get_config,
    homeassistant_check_config,
    homeassistant_list_states,
    homeassistant_get_state,
    homeassistant_set_state,
    homeassistant_list_services,
    homeassistant_call_service,
    homeassistant_list_events,
    homeassistant_fire_event,
    homeassistant_render_template,
    homeassistant_get_logbook,
    philips_hue_list_lights,
    philips_hue_get_light,
    philips_hue_update_light_state,
    philips_hue_delete_light,
]
