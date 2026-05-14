"""Operations, monitoring, and infrastructure service integration tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 80_000
_NETLIFY_BASE_URL = "https://api.netlify.com/api/v1"
_UPTIMEROBOT_BASE_URL = "https://api.uptimerobot.com/v2"
_PAGERDUTY_BASE_URL = "https://api.pagerduty.com"
_SENTRY_BASE_URL = "https://sentry.io"
_CLOUDFLARE_BASE_URL = "https://api.cloudflare.com/client/v4"


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


def _limit(value: int, *, default: int = 25, max_value: int = 500) -> int:
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
    json_body: Optional[dict[str, Any]] = None,
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
                detail = (
                    body.get("message")
                    or body.get("error")
                    or body.get("error_description")
                    or body.get("detail")
                    or ""
                )
                errors = body.get("errors")
                if not detail and isinstance(errors, list):
                    detail = "; ".join(str(item) for item in errors[:3])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def _csv_to_dash(value: str) -> str:
    return "-".join(_csv_to_list(value.replace("-", ",")))


def _truthy(value: bool | str | int) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bearer_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    token_fields: tuple[str, ...],
    env_token: str,
    settings_token_name: str,
    settings_base_name: str,
    default_base: str,
    tool_name: str,
    display_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_base_name)
        or default_base
    )
    token = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=token_fields,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(settings_token_name)
    if not token:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=token_fields,
            tool_name=tool_name,
            env_var=env_token,
            display_name=display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _netlify_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider="netlify",
        provider_aliases=("netlify_api",),
        token_fields=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
        env_token="NETLIFY_ACCESS_TOKEN",
        settings_token_name="netlify_access_token",
        settings_base_name="netlify_base_url",
        default_base=_NETLIFY_BASE_URL,
        tool_name=tool_name,
        display_name="Netlify",
        config=config,
    )


def _uptimerobot_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | dict[str, str]]:
    base = (
        _credential_value(
            provider="uptimerobot",
            provider_aliases=("uptime_robot", "uptimerobot_api", "uptime_robot_api"),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("uptimerobot_base_url")
        or _UPTIMEROBOT_BASE_URL
    )
    api_key = _credential_value(
        provider="uptimerobot",
        provider_aliases=("uptime_robot", "uptimerobot_api", "uptime_robot_api"),
        field_names=("api_key", "apiKey", "key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("uptimerobot_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="uptimerobot",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="UPTIMEROBOT_API_KEY",
            display_name="UptimeRobot",
        )
    return _base_url(base), api_key


def _uptimerobot_request(
    endpoint: str,
    *,
    payload: dict[str, Any],
    tool_name: str,
    config: Optional[RunnableConfig],
) -> Any:
    base_url, key_or_error = _uptimerobot_config(tool_name, config)
    if isinstance(key_or_error, dict):
        return key_or_error
    if key_or_error.startswith("[Error]:"):
        return key_or_error
    data = {"api_key": key_or_error, "format": "json", **payload}
    return _request_json(
        "POST",
        f"{base_url}/{endpoint.lstrip('/')}",
        form_data=data,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Nymeria",
        },
    )


def _pagerduty_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="pagerduty",
            provider_aliases=("pagerduty_api", "pagerduty_oauth2_api"),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pagerduty_base_url")
        or _PAGERDUTY_BASE_URL
    )
    access_token = _credential_value(
        provider="pagerduty",
        provider_aliases=("pagerduty_oauth2_api",),
        field_names=("access_token", "accessToken", "bearer_token", "bearerToken"),
        tool_name=tool_name,
        config=config,
    )
    api_token = (
        access_token
        or _credential_value(
            provider="pagerduty",
            provider_aliases=("pagerduty_api",),
            field_names=("api_token", "apiToken", "api_key", "apiKey", "token", "value"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pagerduty_api_token")
    )
    if not api_token:
        return _base_url(base), _setup_hint(
            provider="pagerduty",
            field_names=("api_token", "access_token", "token", "value"),
            tool_name=tool_name,
            env_var="PAGERDUTY_API_TOKEN",
            display_name="PagerDuty",
        )
    auth_value = f"Bearer {access_token}" if access_token else f"Token token={api_token}"
    return _base_url(base), {
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Authorization": auth_value,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _pagerduty_from_email(
    *,
    tool_name: str,
    config: Optional[RunnableConfig],
    explicit: str,
) -> str | None:
    return (
        explicit.strip()
        or _credential_value(
            provider="pagerduty",
            provider_aliases=("pagerduty_api",),
            field_names=("from_email", "fromEmail", "email"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pagerduty_from_email")
    )


def _sentry_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider="sentry",
        provider_aliases=("sentry_io", "sentryio", "sentry_io_api", "sentry_io_server_api"),
        token_fields=("auth_token", "authToken", "access_token", "accessToken", "api_key", "token", "value"),
        env_token="SENTRY_AUTH_TOKEN",
        settings_token_name="sentry_auth_token",
        settings_base_name="sentry_base_url",
        default_base=_SENTRY_BASE_URL,
        tool_name=tool_name,
        display_name="Sentry",
        config=config,
    )


def _cloudflare_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider="cloudflare",
        provider_aliases=("cloudflare_api",),
        token_fields=("api_token", "apiToken", "access_token", "accessToken", "token", "value"),
        env_token="CLOUDFLARE_API_TOKEN",
        settings_token_name="cloudflare_api_token",
        settings_base_name="cloudflare_base_url",
        default_base=_CLOUDFLARE_BASE_URL,
        tool_name=tool_name,
        display_name="Cloudflare",
        config=config,
    )


@tool
def netlify_list_sites(
    limit: int = 25,
    filter_mode: str = "all",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Netlify sites available to the configured token.

    Args:
        limit: Number of sites to return, 1-100.
        filter_mode: Netlify site filter, usually all.
    """
    try:
        base_url, headers_or_error = _netlify_config("netlify_list_sites", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/sites",
            params={"filter": filter_mode.strip() or "all", "per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_list_sites failed", exc_info=True)
        return f"[Error]: Netlify site listing failed: {e}"


@tool
def netlify_get_site(
    site_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Netlify site by ID, name, or domain."""
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_get_site", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/sites/{quote(site_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_get_site failed", exc_info=True)
        return f"[Error]: Netlify site lookup failed: {e}"


@tool
def netlify_list_deploys(
    site_id: str,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Netlify deploys for a site.

    Args:
        site_id: Netlify site ID, name, or domain.
        limit: Number of deploys to return, 1-100.
    """
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_list_deploys", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/sites/{quote(site_id.strip(), safe='')}/deploys",
            params={"per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_list_deploys failed", exc_info=True)
        return f"[Error]: Netlify deploy listing failed: {e}"


@tool
def netlify_get_deploy(
    site_id: str,
    deploy_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Netlify deploy by site and deploy ID."""
    if not site_id.strip() or not deploy_id.strip():
        return "[Error]: site_id and deploy_id are required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_get_deploy", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/sites/{quote(site_id.strip(), safe='')}/deploys/{quote(deploy_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_get_deploy failed", exc_info=True)
        return f"[Error]: Netlify deploy lookup failed: {e}"


@tool
def netlify_cancel_deploy(
    deploy_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Cancel a Netlify deploy by deploy ID."""
    if not deploy_id.strip():
        return "[Error]: deploy_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_cancel_deploy", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/deploys/{quote(deploy_id.strip(), safe='')}/cancel",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_cancel_deploy failed", exc_info=True)
        return f"[Error]: Netlify deploy cancellation failed: {e}"


@tool
def netlify_delete_site(
    site_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Netlify site by ID, name, or domain."""
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_delete_site", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/sites/{quote(site_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_delete_site failed", exc_info=True)
        return f"[Error]: Netlify site deletion failed: {e}"


@tool
def uptimerobot_get_account(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get UptimeRobot account quota and monitor counts."""
    try:
        data = _uptimerobot_request("getAccountDetails", payload={}, tool_name="uptimerobot_get_account", config=config)
        if isinstance(data, str):
            return data
        return _dump_json(data.get("account", data))
    except Exception as e:
        logger.error("uptimerobot_get_account failed", exc_info=True)
        return f"[Error]: UptimeRobot account lookup failed: {e}"


@tool
def uptimerobot_list_monitors(
    limit: int = 25,
    monitor_ids: str = "",
    statuses: str = "",
    types: str = "",
    include_logs: bool = False,
    include_response_times: bool = False,
    include_alert_contacts: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List UptimeRobot monitors.

    Args:
        limit: Number of monitors to return, 1-100.
        monitor_ids: Optional comma- or dash-separated monitor IDs.
        statuses: Optional comma-separated UptimeRobot status codes.
        types: Optional comma-separated UptimeRobot monitor type codes.
        include_logs: Include monitor logs.
        include_response_times: Include response time data.
        include_alert_contacts: Include alert contacts.
    """
    try:
        payload: dict[str, Any] = {"limit": _limit(limit, max_value=100)}
        if monitor_ids.strip():
            payload["monitors"] = _csv_to_dash(monitor_ids)
        if statuses.strip():
            payload["statuses"] = _csv_to_dash(statuses)
        if types.strip():
            payload["types"] = _csv_to_dash(types)
        if include_logs:
            payload["logs"] = 1
        if include_response_times:
            payload["response_times"] = 1
        if include_alert_contacts:
            payload["alert_contacts"] = 1
        data = _uptimerobot_request(
            "getMonitors",
            payload=payload,
            tool_name="uptimerobot_list_monitors",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitors", data))
    except Exception as e:
        logger.error("uptimerobot_list_monitors failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor listing failed: {e}"


@tool
def uptimerobot_get_monitor(
    monitor_id: str,
    include_logs: bool = False,
    include_response_times: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an UptimeRobot monitor by ID."""
    if not monitor_id.strip():
        return "[Error]: monitor_id is required."
    try:
        payload: dict[str, Any] = {"monitors": monitor_id.strip()}
        if include_logs:
            payload["logs"] = 1
        if include_response_times:
            payload["response_times"] = 1
        data = _uptimerobot_request(
            "getMonitors",
            payload=payload,
            tool_name="uptimerobot_get_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitors", data))
    except Exception as e:
        logger.error("uptimerobot_get_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor lookup failed: {e}"


@tool
def uptimerobot_create_monitor(
    friendly_name: str,
    url: str,
    monitor_type: int = 1,
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an UptimeRobot monitor.

    Args:
        friendly_name: Monitor display name.
        url: URL, IP, or heartbeat URL target.
        monitor_type: UptimeRobot type code, e.g. 1 HTTP(S), 2 keyword, 3 ping, 4 port, 5 heartbeat.
        fields_json: Additional UptimeRobot newMonitor fields as JSON.
    """
    if not friendly_name.strip() or not url.strip():
        return "[Error]: friendly_name and url are required."
    try:
        payload = {
            "friendly_name": friendly_name.strip(),
            "url": url.strip(),
            "type": int(monitor_type),
            **_parse_json(fields_json, expected=dict, label="fields_json"),
        }
        data = _uptimerobot_request(
            "newMonitor",
            payload=payload,
            tool_name="uptimerobot_create_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_create_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor creation failed: {e}"


@tool
def uptimerobot_update_monitor(
    monitor_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an UptimeRobot monitor.

    Args:
        monitor_id: Monitor ID.
        fields_json: UptimeRobot editMonitor fields as JSON.
    """
    if not monitor_id.strip() or not fields_json.strip():
        return "[Error]: monitor_id and fields_json are required."
    try:
        payload = {"id": monitor_id.strip(), **_parse_json(fields_json, expected=dict, label="fields_json")}
        data = _uptimerobot_request(
            "editMonitor",
            payload=payload,
            tool_name="uptimerobot_update_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_update_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor update failed: {e}"


@tool
def uptimerobot_delete_monitor(
    monitor_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an UptimeRobot monitor."""
    if not monitor_id.strip():
        return "[Error]: monitor_id is required."
    try:
        data = _uptimerobot_request(
            "deleteMonitor",
            payload={"id": monitor_id.strip()},
            tool_name="uptimerobot_delete_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_delete_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor deletion failed: {e}"


@tool
def uptimerobot_reset_monitor(
    monitor_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Reset an UptimeRobot monitor's stats."""
    if not monitor_id.strip():
        return "[Error]: monitor_id is required."
    try:
        data = _uptimerobot_request(
            "resetMonitor",
            payload={"id": monitor_id.strip()},
            tool_name="uptimerobot_reset_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_reset_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor reset failed: {e}"


@tool
def pagerduty_list_incidents(
    limit: int = 25,
    statuses: str = "",
    service_ids: str = "",
    user_ids: str = "",
    since: str = "",
    until: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List PagerDuty incidents.

    Args:
        limit: Number of incidents to return, 1-100.
        statuses: Optional comma-separated statuses, e.g. triggered,acknowledged,resolved.
        service_ids: Optional comma-separated service IDs.
        user_ids: Optional comma-separated user IDs.
        since: Optional ISO timestamp lower bound.
        until: Optional ISO timestamp upper bound.
    """
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_list_incidents", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {"limit": _limit(limit, max_value=100), "total": "false"}
        if statuses.strip():
            params["statuses[]"] = _csv_to_list(statuses)
        if service_ids.strip():
            params["service_ids[]"] = _csv_to_list(service_ids)
        if user_ids.strip():
            params["user_ids[]"] = _csv_to_list(user_ids)
        if since.strip():
            params["since"] = since.strip()
        if until.strip():
            params["until"] = until.strip()
        data = _request_json("GET", f"{base_url}/incidents", params=params, headers=headers_or_error)
        return _dump_json(data.get("incidents", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_list_incidents failed", exc_info=True)
        return f"[Error]: PagerDuty incident listing failed: {e}"


@tool
def pagerduty_get_incident(
    incident_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a PagerDuty incident by ID."""
    if not incident_id.strip():
        return "[Error]: incident_id is required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_get_incident", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/incidents/{quote(incident_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("incident", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_get_incident failed", exc_info=True)
        return f"[Error]: PagerDuty incident lookup failed: {e}"


@tool
def pagerduty_create_incident(
    title: str,
    service_id: str,
    from_email: str = "",
    urgency: str = "",
    details: str = "",
    incident_key: str = "",
    priority_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a PagerDuty incident.

    Args:
        title: Incident title.
        service_id: PagerDuty service ID.
        from_email: Optional PagerDuty account email for the From header.
        urgency: Optional high or low.
        details: Optional incident body details.
        incident_key: Optional deduplication key.
        priority_id: Optional priority ID.
    """
    if not title.strip() or not service_id.strip():
        return "[Error]: title and service_id are required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_create_incident", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        headers = dict(headers_or_error)
        email = _pagerduty_from_email(tool_name="pagerduty_create_incident", config=config, explicit=from_email)
        if email:
            headers["From"] = email
        incident: dict[str, Any] = {
            "type": "incident",
            "title": title.strip(),
            "service": {"id": service_id.strip(), "type": "service_reference"},
        }
        if details.strip():
            incident["body"] = {"type": "incident_body", "details": details.strip()}
        if urgency.strip():
            incident["urgency"] = urgency.strip()
        if incident_key.strip():
            incident["incident_key"] = incident_key.strip()
        if priority_id.strip():
            incident["priority"] = {"id": priority_id.strip(), "type": "priority_reference"}
        data = _request_json("POST", f"{base_url}/incidents", json_body={"incident": incident}, headers=headers)
        return _dump_json(data.get("incident", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_create_incident failed", exc_info=True)
        return f"[Error]: PagerDuty incident creation failed: {e}"


@tool
def pagerduty_update_incident(
    incident_id: str,
    from_email: str = "",
    status: str = "",
    title: str = "",
    urgency: str = "",
    resolution: str = "",
    details: str = "",
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a PagerDuty incident.

    Args:
        incident_id: PagerDuty incident ID.
        from_email: Optional PagerDuty account email for the From header.
        status: Optional status such as acknowledged or resolved.
        title: Optional new title.
        urgency: Optional high or low.
        resolution: Optional resolution text when resolving.
        details: Optional body details.
        fields_json: Additional incident update fields as JSON.
    """
    if not incident_id.strip():
        return "[Error]: incident_id is required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_update_incident", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        headers = dict(headers_or_error)
        email = _pagerduty_from_email(tool_name="pagerduty_update_incident", config=config, explicit=from_email)
        if email:
            headers["From"] = email
        incident = {"type": "incident", **_parse_json(fields_json, expected=dict, label="fields_json")}
        if status.strip():
            incident["status"] = status.strip()
        if title.strip():
            incident["title"] = title.strip()
        if urgency.strip():
            incident["urgency"] = urgency.strip()
        if resolution.strip():
            incident["resolution"] = resolution.strip()
        if details.strip():
            incident["body"] = {"type": "incident_body", "details": details.strip()}
        data = _request_json(
            "PUT",
            f"{base_url}/incidents/{quote(incident_id.strip(), safe='')}",
            json_body={"incident": incident},
            headers=headers,
        )
        return _dump_json(data.get("incident", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_update_incident failed", exc_info=True)
        return f"[Error]: PagerDuty incident update failed: {e}"


@tool
def pagerduty_add_incident_note(
    incident_id: str,
    content: str,
    from_email: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a note to a PagerDuty incident."""
    if not incident_id.strip() or not content.strip():
        return "[Error]: incident_id and content are required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_add_incident_note", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        headers = dict(headers_or_error)
        email = _pagerduty_from_email(tool_name="pagerduty_add_incident_note", config=config, explicit=from_email)
        if email:
            headers["From"] = email
        data = _request_json(
            "POST",
            f"{base_url}/incidents/{quote(incident_id.strip(), safe='')}/notes",
            json_body={"note": {"content": content.strip()}},
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("pagerduty_add_incident_note failed", exc_info=True)
        return f"[Error]: PagerDuty incident note creation failed: {e}"


@tool
def pagerduty_list_services(
    limit: int = 25,
    query: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List PagerDuty services."""
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_list_services", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/services",
            params={"limit": _limit(limit, max_value=100), "query": query.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("services", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_list_services failed", exc_info=True)
        return f"[Error]: PagerDuty service listing failed: {e}"


@tool
def pagerduty_get_user(
    user_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a PagerDuty user by ID."""
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/users/{quote(user_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data.get("user", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_get_user failed", exc_info=True)
        return f"[Error]: PagerDuty user lookup failed: {e}"


@tool
def sentry_list_organizations(
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Sentry organizations available to the token."""
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_organizations", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/organizations/",
            params={"limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_organizations failed", exc_info=True)
        return f"[Error]: Sentry organization listing failed: {e}"


@tool
def sentry_list_projects(
    organization_slug: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Sentry projects, optionally scoped to an organization."""
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_projects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        path = (
            f"/api/0/organizations/{quote(organization_slug.strip(), safe='')}/projects/"
            if organization_slug.strip()
            else "/api/0/projects/"
        )
        data = _request_json(
            "GET",
            f"{base_url}{path}",
            params={"limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_projects failed", exc_info=True)
        return f"[Error]: Sentry project listing failed: {e}"


@tool
def sentry_list_project_issues(
    organization_slug: str,
    project_slug: str,
    query: str = "",
    stats_period: str = "24h",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List issues for a Sentry project."""
    if not organization_slug.strip() or not project_slug.strip():
        return "[Error]: organization_slug and project_slug are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_project_issues", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/projects/{quote(organization_slug.strip(), safe='')}/{quote(project_slug.strip(), safe='')}/issues/",
            params={
                "query": query.strip(),
                "statsPeriod": stats_period.strip() or "24h",
                "limit": _limit(limit, max_value=100),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_project_issues failed", exc_info=True)
        return f"[Error]: Sentry issue listing failed: {e}"


@tool
def sentry_get_issue(
    issue_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Sentry issue by issue/group ID."""
    if not issue_id.strip():
        return "[Error]: issue_id is required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_get_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/api/0/issues/{quote(issue_id.strip(), safe='')}/", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_get_issue failed", exc_info=True)
        return f"[Error]: Sentry issue lookup failed: {e}"


@tool
def sentry_update_issue(
    organization_slug: str,
    issue_id: str,
    status: str = "",
    assigned_to: str = "",
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Sentry issue.

    Args:
        organization_slug: Sentry organization slug.
        issue_id: Sentry issue/group ID.
        status: Optional status such as resolved or unresolved.
        assigned_to: Optional assignee identifier.
        fields_json: Additional Sentry issue attributes as JSON.
    """
    if not organization_slug.strip() or not issue_id.strip():
        return "[Error]: organization_slug and issue_id are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_update_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        if status.strip():
            body["status"] = status.strip()
        if assigned_to.strip():
            body["assignedTo"] = assigned_to.strip()
        data = _request_json(
            "PUT",
            f"{base_url}/api/0/organizations/{quote(organization_slug.strip(), safe='')}/issues/{quote(issue_id.strip(), safe='')}/",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_update_issue failed", exc_info=True)
        return f"[Error]: Sentry issue update failed: {e}"


@tool
def sentry_list_project_events(
    organization_slug: str,
    project_slug: str,
    full: bool = False,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List events for a Sentry project."""
    if not organization_slug.strip() or not project_slug.strip():
        return "[Error]: organization_slug and project_slug are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_project_events", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/projects/{quote(organization_slug.strip(), safe='')}/{quote(project_slug.strip(), safe='')}/events/",
            params={"full": str(bool(full)).lower(), "limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_project_events failed", exc_info=True)
        return f"[Error]: Sentry event listing failed: {e}"


@tool
def sentry_get_event(
    organization_slug: str,
    project_slug: str,
    event_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Sentry event by project and event ID."""
    if not organization_slug.strip() or not project_slug.strip() or not event_id.strip():
        return "[Error]: organization_slug, project_slug, and event_id are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_get_event", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/projects/{quote(organization_slug.strip(), safe='')}/{quote(project_slug.strip(), safe='')}/events/{quote(event_id.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_get_event failed", exc_info=True)
        return f"[Error]: Sentry event lookup failed: {e}"


@tool
def cloudflare_list_zones(
    name: str = "",
    status: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Cloudflare zones."""
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_list_zones", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones",
            params={"name": name.strip(), "status": status.strip(), "per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_list_zones failed", exc_info=True)
        return f"[Error]: Cloudflare zone listing failed: {e}"


@tool
def cloudflare_list_dns_records(
    zone_id: str,
    name: str = "",
    record_type: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Cloudflare DNS records for a zone."""
    if not zone_id.strip():
        return "[Error]: zone_id is required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_list_dns_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records",
            params={"name": name.strip(), "type": record_type.strip().upper(), "per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_list_dns_records failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record listing failed: {e}"


@tool
def cloudflare_create_dns_record(
    zone_id: str,
    record_type: str,
    name: str,
    content: str,
    ttl: int = 1,
    proxied: bool = False,
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Cloudflare DNS record.

    Args:
        zone_id: Cloudflare zone ID.
        record_type: DNS record type such as A, AAAA, CNAME, TXT, MX.
        name: DNS record name.
        content: DNS record content.
        ttl: TTL in seconds; 1 means automatic in Cloudflare.
        proxied: Whether the record is proxied through Cloudflare.
        fields_json: Additional Cloudflare DNS record fields as JSON.
    """
    if not zone_id.strip() or not record_type.strip() or not name.strip() or not content.strip():
        return "[Error]: zone_id, record_type, name, and content are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_create_dns_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {
            "type": record_type.strip().upper(),
            "name": name.strip(),
            "content": content.strip(),
            "ttl": int(ttl),
            "proxied": bool(proxied),
            **_parse_json(fields_json, expected=dict, label="fields_json"),
        }
        data = _request_json(
            "POST",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_create_dns_record failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record creation failed: {e}"


@tool
def cloudflare_update_dns_record(
    zone_id: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Patch a Cloudflare DNS record.

    Args:
        zone_id: Cloudflare zone ID.
        record_id: DNS record ID.
        fields_json: Cloudflare DNS record fields to patch as JSON.
    """
    if not zone_id.strip() or not record_id.strip() or not fields_json.strip():
        return "[Error]: zone_id, record_id, and fields_json are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_update_dns_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records/{quote(record_id.strip(), safe='')}",
            json_body=_parse_json(fields_json, expected=dict, label="fields_json"),
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_update_dns_record failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record update failed: {e}"


@tool
def cloudflare_delete_dns_record(
    zone_id: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Cloudflare DNS record."""
    if not zone_id.strip() or not record_id.strip():
        return "[Error]: zone_id and record_id are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_delete_dns_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_delete_dns_record failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record deletion failed: {e}"


@tool
def cloudflare_list_origin_certificates(
    zone_id: str,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Cloudflare zone-level authenticated origin pull certificates."""
    if not zone_id.strip():
        return "[Error]: zone_id is required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_list_origin_certificates", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth",
            params={"per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_list_origin_certificates failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate listing failed: {e}"


@tool
def cloudflare_get_origin_certificate(
    zone_id: str,
    certificate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Cloudflare zone-level authenticated origin pull certificate."""
    if not zone_id.strip() or not certificate_id.strip():
        return "[Error]: zone_id and certificate_id are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_get_origin_certificate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth/{quote(certificate_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_get_origin_certificate failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate lookup failed: {e}"


@tool
def cloudflare_upload_origin_certificate(
    zone_id: str,
    certificate_pem: str,
    private_key_pem: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload a Cloudflare zone-level authenticated origin pull certificate."""
    if not zone_id.strip() or not certificate_pem.strip() or not private_key_pem.strip():
        return "[Error]: zone_id, certificate_pem, and private_key_pem are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_upload_origin_certificate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth",
            json_body={"certificate": certificate_pem, "private_key": private_key_pem},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_upload_origin_certificate failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate upload failed: {e}"


@tool
def cloudflare_delete_origin_certificate(
    zone_id: str,
    certificate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Cloudflare zone-level authenticated origin pull certificate."""
    if not zone_id.strip() or not certificate_id.strip():
        return "[Error]: zone_id and certificate_id are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_delete_origin_certificate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth/{quote(certificate_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_delete_origin_certificate failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate deletion failed: {e}"


OPERATIONS_MONITORING_SERVICE_TOOLS = [
    netlify_list_sites,
    netlify_get_site,
    netlify_list_deploys,
    netlify_get_deploy,
    netlify_cancel_deploy,
    netlify_delete_site,
    uptimerobot_get_account,
    uptimerobot_list_monitors,
    uptimerobot_get_monitor,
    uptimerobot_create_monitor,
    uptimerobot_update_monitor,
    uptimerobot_delete_monitor,
    uptimerobot_reset_monitor,
    pagerduty_list_incidents,
    pagerduty_get_incident,
    pagerduty_create_incident,
    pagerduty_update_incident,
    pagerduty_add_incident_note,
    pagerduty_list_services,
    pagerduty_get_user,
    sentry_list_organizations,
    sentry_list_projects,
    sentry_list_project_issues,
    sentry_get_issue,
    sentry_update_issue,
    sentry_list_project_events,
    sentry_get_event,
    cloudflare_list_zones,
    cloudflare_list_dns_records,
    cloudflare_create_dns_record,
    cloudflare_update_dns_record,
    cloudflare_delete_dns_record,
    cloudflare_list_origin_certificates,
    cloudflare_get_origin_certificate,
    cloudflare_upload_origin_certificate,
    cloudflare_delete_origin_certificate,
]
