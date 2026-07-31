"""Event, webinar, and meeting service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import json
import logging
from datetime import datetime, timedelta, timezone
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
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    request_with_policy as _request_with_policy,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_DEMIO_BASE_URL = "https://my.demio.com/api/v1"
_ZOOM_BASE_URL = "https://api.zoom.us/v2"
_GOTOWEBINAR_BASE_URL = "https://api.getgo.com/G2W/rest/v2"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_DEMIO = register_provider_spec(
    ProviderCredentialSpec(
        provider="demio",
        aliases=("demio_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "api_url", "url"), required=False
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "key")),
            CredentialFieldGroup(
                role="api_secret", names=("api_secret", "apiSecret", "secret")
            ),
        ),
        hint_fields=("api_key", "api_secret"),
        env_var="DEMIO_API_KEY and DEMIO_API_SECRET",
        display_name="Demio",
    )
)

_ZOOM = register_provider_spec(
    ProviderCredentialSpec(
        provider="zoom",
        aliases=("zoom_api", "zoom_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "api_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "bearer_token", "token", "value"),
            ),
        ),
        hint_fields=("access_token",),
        env_var="ZOOM_ACCESS_TOKEN",
        display_name="Zoom",
    )
)

# Branch variant: three setup-hint field/env sets (access_token, account_key,
# organizer_key). spec.hint_fields and spec.env_var carry the access_token
# variant; the account_key/organizer_key branches keep their per-branch field and
# env_var literals inline at the _missing_gotowebinar_key call sites.
_GOTOWEBINAR = register_provider_spec(
    ProviderCredentialSpec(
        provider="gotowebinar",
        aliases=("go_to_webinar", "gotowebinar_oauth2", "go_to_webinar_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "api_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "bearer_token", "token", "value"),
            ),
            CredentialFieldGroup(role="account_key", names=("account_key", "accountKey")),
            CredentialFieldGroup(
                role="organizer_key", names=("organizer_key", "organizerKey")
            ),
        ),
        hint_fields=("access_token",),
        env_var="GOTOWEBINAR_ACCESS_TOKEN",
        display_name="GoToWebinar",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 50, max_value: int = 250) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _json_object(value: str, *, field_name: str, allow_empty: bool = True) -> dict[str, Any]:
    if not value.strip():
        if allow_empty:
            return {}
        raise ValueError(f"{field_name} is required")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _json_array(value: str, *, field_name: str, allow_empty: bool = True) -> list[Any]:
    if not value.strip():
        if allow_empty:
            return []
        raise ValueError(f"{field_name} is required")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return parsed


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any] | list[Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(
                client,
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
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                detail = first.get("message") if isinstance(first, dict) else str(first)
            elif isinstance(errors, dict):
                detail = "; ".join(f"{key}: {value}" for key, value in errors.items())
            detail = (
                detail
                or body.get("message")
                or body.get("error_description")
                or body.get("error")
                or body.get("detail")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _items_from_response(data: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    embedded = data.get("_embedded")
    if isinstance(embedded, dict):
        for key in keys:
            value = embedded.get(key)
            if isinstance(value, list):
                return value
    return []


def _utc_iso(value: Optional[datetime] = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _bearer_header_value(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _demio_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_DEMIO.provider,
            provider_aliases=_DEMIO.aliases,
            field_names=_DEMIO.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("demio_base_url")
        or _DEMIO_BASE_URL
    )
    api_key = _credential_value(
        provider=_DEMIO.provider,
        provider_aliases=_DEMIO.aliases,
        field_names=_DEMIO.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("demio_api_key")
    api_secret = _credential_value(
        provider=_DEMIO.provider,
        provider_aliases=_DEMIO.aliases,
        field_names=_DEMIO.group("api_secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("demio_api_secret")
    if not api_key or not api_secret:
        return _base_url(base), _setup_hint(
            provider=_DEMIO.provider,
            field_names=_DEMIO.hint_fields,
            tool_name=tool_name,
            env_var=_DEMIO.env_var,
            display_name=_DEMIO.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Api-Key": api_key,
        "Api-Secret": api_secret,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _zoom_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_ZOOM.provider,
            provider_aliases=_ZOOM.aliases,
            field_names=_ZOOM.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("zoom_base_url")
        or _ZOOM_BASE_URL
    )
    token = _credential_value(
        provider=_ZOOM.provider,
        provider_aliases=_ZOOM.aliases,
        field_names=_ZOOM.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("zoom_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_ZOOM.provider,
            field_names=_ZOOM.hint_fields,
            tool_name=tool_name,
            env_var=_ZOOM.env_var,
            display_name=_ZOOM.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": _bearer_header_value(token),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _gotowebinar_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str, Optional[str], Optional[str]]:
    aliases = _GOTOWEBINAR.aliases
    base = (
        _credential_value(
            provider=_GOTOWEBINAR.provider,
            provider_aliases=aliases,
            field_names=_GOTOWEBINAR.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("gotowebinar_base_url")
        or _GOTOWEBINAR_BASE_URL
    )
    token = _credential_value(
        provider=_GOTOWEBINAR.provider,
        provider_aliases=aliases,
        field_names=_GOTOWEBINAR.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("gotowebinar_access_token")
    account_key = _credential_value(
        provider=_GOTOWEBINAR.provider,
        provider_aliases=aliases,
        field_names=_GOTOWEBINAR.group("account_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("gotowebinar_account_key")
    organizer_key = _credential_value(
        provider=_GOTOWEBINAR.provider,
        provider_aliases=aliases,
        field_names=_GOTOWEBINAR.group("organizer_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("gotowebinar_organizer_key")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_GOTOWEBINAR.provider,
            field_names=_GOTOWEBINAR.hint_fields,
            tool_name=tool_name,
            env_var=_GOTOWEBINAR.env_var,
            display_name=_GOTOWEBINAR.display_name,
        ), account_key, organizer_key
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": _bearer_header_value(token),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }, account_key, organizer_key


def _missing_gotowebinar_key(tool_name: str, field_name: str, env_var: str) -> str:
    # field_name/env_var are per-branch variants (account_key / organizer_key),
    # passed by callers and kept inline; provider/display_name come from the spec.
    return _setup_hint(
        provider=_GOTOWEBINAR.provider,
        field_names=(field_name,),
        tool_name=tool_name,
        env_var=env_var,
        display_name=_GOTOWEBINAR.display_name,
    )


@tool
def demio_list_events(
    event_type: str = "upcoming",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Demio events.

    Args:
        event_type: Optional event type filter: upcoming, past, or automated.
        limit: Number of events to return, 1-250.
    """
    try:
        base_url, headers_or_error = _demio_config("demio_list_events", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"type": event_type.strip() or None}
        data = _request_json("GET", f"{base_url}/events", params=params, headers=headers_or_error)
        if isinstance(data, list):
            data = data[: _limit(limit)]
        return _dump_json(data)
    except Exception as e:
        logger.error("demio_list_events failed", exc_info=True)
        return f"[Error]: Demio event list failed: {e}"


@tool
def demio_get_event(
    event_id: str,
    active: Optional[bool] = None,
    date_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Demio event or a specific event date/session.

    Args:
        event_id: Demio event ID.
        active: Optional active-session filter for event dates.
        date_id: Optional Demio date/session ID. When supplied, fetches that event date.
    """
    if not event_id.strip():
        return "[Error]: event_id is required."
    try:
        base_url, headers_or_error = _demio_config("demio_get_event", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if date_id.strip():
            url = f"{base_url}/event/{quote(event_id.strip(), safe='')}/date/{quote(date_id.strip(), safe='')}"
            data = _request_json("GET", url, headers=headers_or_error)
        else:
            data = _request_json(
                "GET",
                f"{base_url}/event/{quote(event_id.strip(), safe='')}",
                params={"active": active},
                headers=headers_or_error,
            )
        return _dump_json(data)
    except Exception as e:
        logger.error("demio_get_event failed", exc_info=True)
        return f"[Error]: Demio event lookup failed: {e}"


@tool
def demio_register_event(
    event_id: str,
    name: str,
    email: str,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Register an attendee for a Demio event.

    Args:
        event_id: Demio event ID.
        name: Registrant name.
        email: Registrant email address.
        fields_json: Optional JSON object with extra Demio registration fields or custom field IDs.
    """
    if not event_id.strip() or not name.strip() or not email.strip():
        return "[Error]: event_id, name, and email are required."
    try:
        base_url, headers_or_error = _demio_config("demio_register_event", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {
            "id": event_id.strip(),
            "name": name.strip(),
            "email": email.strip(),
        }
        body.update(_json_object(fields_json, field_name="fields_json"))
        data = _request_json(
            "PUT",
            f"{base_url}/event/register",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("demio_register_event failed", exc_info=True)
        return f"[Error]: Demio event registration failed: {e}"


@tool
def demio_get_session_participants(
    date_id: str,
    status: str = "",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Demio participant report rows for an event date/session.

    Args:
        date_id: Demio date/session ID.
        status: Optional participant status filter.
        limit: Number of participants to return, 1-250.
    """
    if not date_id.strip():
        return "[Error]: date_id is required."
    try:
        base_url, headers_or_error = _demio_config("demio_get_session_participants", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/report/{quote(date_id.strip(), safe='')}/participants",
            params={"status": status.strip() or None},
            headers=headers_or_error,
        )
        participants = data.get("participants", data) if isinstance(data, dict) else data
        if isinstance(participants, list):
            participants = participants[: _limit(limit)]
        return _dump_json(participants)
    except Exception as e:
        logger.error("demio_get_session_participants failed", exc_info=True)
        return f"[Error]: Demio participant report failed: {e}"


@tool
def zoom_list_meetings(
    meeting_type: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Zoom meetings for the authenticated user.

    Args:
        meeting_type: Optional Zoom type filter, such as scheduled, live, or upcoming.
        limit: Number of meetings to return, 1-300.
    """
    try:
        base_url, headers_or_error = _zoom_config("zoom_list_meetings", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/users/me/meetings",
            params={
                "type": meeting_type.strip() or None,
                "page_size": _limit(limit, max_value=300),
            },
            headers=headers_or_error,
        )
        meetings = data.get("meetings", data) if isinstance(data, dict) else data
        if isinstance(meetings, list):
            meetings = meetings[: _limit(limit, max_value=300)]
        return _dump_json(meetings)
    except Exception as e:
        logger.error("zoom_list_meetings failed", exc_info=True)
        return f"[Error]: Zoom meeting list failed: {e}"


@tool
def zoom_get_meeting(
    meeting_id: str,
    occurrence_id: str = "",
    show_previous_occurrences: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Zoom meeting by ID.

    Args:
        meeting_id: Zoom meeting ID.
        occurrence_id: Optional occurrence ID for recurring meetings.
        show_previous_occurrences: Include previous occurrences when supported.
    """
    if not meeting_id.strip():
        return "[Error]: meeting_id is required."
    try:
        base_url, headers_or_error = _zoom_config("zoom_get_meeting", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/meetings/{quote(meeting_id.strip(), safe='')}",
            params={
                "occurrence_id": occurrence_id.strip() or None,
                "show_previous_occurrences": show_previous_occurrences,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zoom_get_meeting failed", exc_info=True)
        return f"[Error]: Zoom meeting lookup failed: {e}"


@tool
def zoom_create_meeting(
    topic: str,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Zoom meeting.

    Args:
        topic: Meeting topic.
        fields_json: Optional JSON object with Zoom meeting fields, such as type, start_time,
            duration, timezone, password, agenda, schedule_for, or settings.
    """
    if not topic.strip():
        return "[Error]: topic is required."
    try:
        base_url, headers_or_error = _zoom_config("zoom_create_meeting", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _json_object(fields_json, field_name="fields_json")
        body["topic"] = topic.strip()
        data = _request_json(
            "POST",
            f"{base_url}/users/me/meetings",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zoom_create_meeting failed", exc_info=True)
        return f"[Error]: Zoom meeting creation failed: {e}"


@tool
def zoom_update_meeting(
    meeting_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Zoom meeting.

    Args:
        meeting_id: Zoom meeting ID.
        fields_json: JSON object with fields to update, such as topic, type, start_time,
            duration, timezone, password, agenda, schedule_for, or settings.
    """
    if not meeting_id.strip():
        return "[Error]: meeting_id is required."
    try:
        body = _json_object(fields_json, field_name="fields_json", allow_empty=False)
        base_url, headers_or_error = _zoom_config("zoom_update_meeting", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/meetings/{quote(meeting_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        if isinstance(data, dict) and data.get("status") == "ok":
            data = {"success": True}
        return _dump_json(data)
    except Exception as e:
        logger.error("zoom_update_meeting failed", exc_info=True)
        return f"[Error]: Zoom meeting update failed: {e}"


@tool
def zoom_delete_meeting(
    meeting_id: str,
    occurrence_id: str = "",
    schedule_for_reminder: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Zoom meeting.

    Args:
        meeting_id: Zoom meeting ID.
        occurrence_id: Optional occurrence ID for a recurring meeting.
        schedule_for_reminder: Whether to send a scheduling reminder.
    """
    if not meeting_id.strip():
        return "[Error]: meeting_id is required."
    try:
        base_url, headers_or_error = _zoom_config("zoom_delete_meeting", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _request_json(
            "DELETE",
            f"{base_url}/meetings/{quote(meeting_id.strip(), safe='')}",
            params={
                "occurrence_id": occurrence_id.strip() or None,
                "schedule_for_reminder": schedule_for_reminder,
            },
            headers=headers_or_error,
        )
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("zoom_delete_meeting failed", exc_info=True)
        return f"[Error]: Zoom meeting deletion failed: {e}"


@tool
def gotowebinar_list_webinars(
    from_time: str = "",
    to_time: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GoToWebinar webinars for the configured account.

    Args:
        from_time: Optional ISO start time. Defaults to one year ago.
        to_time: Optional ISO end time. Defaults to now.
        limit: Number of webinars to return, 1-250.
    """
    try:
        base_url, headers_or_error, account_key, _organizer_key = _gotowebinar_config(
            "gotowebinar_list_webinars",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not account_key:
            return _missing_gotowebinar_key("gotowebinar_list_webinars", "account_key", "GOTOWEBINAR_ACCOUNT_KEY")
        data = _request_json(
            "GET",
            f"{base_url}/accounts/{quote(account_key.strip(), safe='')}/webinars",
            params={
                "fromTime": from_time.strip() or _utc_iso(datetime.now(timezone.utc) - timedelta(days=365)),
                "toTime": to_time.strip() or _utc_iso(),
                "limit": _limit(limit),
            },
            headers=headers_or_error,
        )
        webinars = _items_from_response(data, ("webinars",))
        return _dump_json(webinars[: _limit(limit)])
    except Exception as e:
        logger.error("gotowebinar_list_webinars failed", exc_info=True)
        return f"[Error]: GoToWebinar webinar list failed: {e}"


@tool
def gotowebinar_get_webinar(
    webinar_key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a GoToWebinar webinar by key.

    Args:
        webinar_key: GoToWebinar webinar key.
    """
    if not webinar_key.strip():
        return "[Error]: webinar_key is required."
    try:
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_get_webinar",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_get_webinar",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "GET",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("gotowebinar_get_webinar failed", exc_info=True)
        return f"[Error]: GoToWebinar webinar lookup failed: {e}"


@tool
def gotowebinar_create_webinar(
    subject: str,
    times_json: str,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a GoToWebinar webinar.

    Args:
        subject: Webinar subject.
        times_json: JSON array of time ranges with startTime and endTime.
        fields_json: Optional JSON object with additional webinar fields.
    """
    if not subject.strip():
        return "[Error]: subject is required."
    try:
        times = _json_array(times_json, field_name="times_json", allow_empty=False)
        body = _json_object(fields_json, field_name="fields_json")
        body.update({"subject": subject.strip(), "times": times})
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_create_webinar",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_create_webinar",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "POST",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("gotowebinar_create_webinar failed", exc_info=True)
        return f"[Error]: GoToWebinar webinar creation failed: {e}"


@tool
def gotowebinar_update_webinar(
    webinar_key: str,
    fields_json: str,
    notify_participants: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a GoToWebinar webinar.

    Args:
        webinar_key: GoToWebinar webinar key.
        fields_json: JSON object with fields to update.
        notify_participants: Whether to notify participants of the update.
    """
    if not webinar_key.strip():
        return "[Error]: webinar_key is required."
    try:
        body = _json_object(fields_json, field_name="fields_json", allow_empty=False)
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_update_webinar",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_update_webinar",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "PUT",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}",
            params={"notifyParticipants": notify_participants},
            json_body=body,
            headers=headers_or_error,
        )
        if isinstance(data, dict) and data.get("status") == "ok":
            data = {"success": True}
        return _dump_json(data)
    except Exception as e:
        logger.error("gotowebinar_update_webinar failed", exc_info=True)
        return f"[Error]: GoToWebinar webinar update failed: {e}"


@tool
def gotowebinar_list_sessions(
    webinar_key: str = "",
    from_time: str = "",
    to_time: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GoToWebinar sessions.

    Args:
        webinar_key: Optional webinar key. When omitted, lists organizer sessions.
        from_time: Optional ISO start time. Defaults to one year ago.
        to_time: Optional ISO end time. Defaults to now.
        limit: Number of sessions to return, 1-250.
    """
    try:
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_list_sessions",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_list_sessions",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        org = quote(organizer_key.strip(), safe="")
        if webinar_key.strip():
            path = f"organizers/{org}/webinars/{quote(webinar_key.strip(), safe='')}/sessions"
        else:
            path = f"organizers/{org}/sessions"
        headers = dict(headers_or_error)
        headers["Accept"] = "application/vnd.citrix.g2wapi-v1.1+json"
        data = _request_json(
            "GET",
            f"{base_url}/{path}",
            params={
                "fromTime": from_time.strip() or _utc_iso(datetime.now(timezone.utc) - timedelta(days=365)),
                "toTime": to_time.strip() or _utc_iso(),
                "limit": _limit(limit),
            },
            headers=headers,
        )
        sessions = _items_from_response(data, ("sessionInfoResources", "sessions"))
        return _dump_json(sessions[: _limit(limit)])
    except Exception as e:
        logger.error("gotowebinar_list_sessions failed", exc_info=True)
        return f"[Error]: GoToWebinar session list failed: {e}"


@tool
def gotowebinar_get_session(
    webinar_key: str,
    session_key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a GoToWebinar session by key.

    Args:
        webinar_key: GoToWebinar webinar key.
        session_key: GoToWebinar session key.
    """
    if not webinar_key.strip() or not session_key.strip():
        return "[Error]: webinar_key and session_key are required."
    try:
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_get_session",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_get_session",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "GET",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}/sessions/{quote(session_key.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("gotowebinar_get_session failed", exc_info=True)
        return f"[Error]: GoToWebinar session lookup failed: {e}"


@tool
def gotowebinar_list_registrants(
    webinar_key: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List registrants for a GoToWebinar webinar.

    Args:
        webinar_key: GoToWebinar webinar key.
        limit: Number of registrants to return, 1-250.
    """
    if not webinar_key.strip():
        return "[Error]: webinar_key is required."
    try:
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_list_registrants",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_list_registrants",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "GET",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}/registrants",
            params={"limit": _limit(limit)},
            headers=headers_or_error,
        )
        registrants = _items_from_response(data, ("registrants",))
        return _dump_json(registrants[: _limit(limit)])
    except Exception as e:
        logger.error("gotowebinar_list_registrants failed", exc_info=True)
        return f"[Error]: GoToWebinar registrant list failed: {e}"


@tool
def gotowebinar_get_registrant(
    webinar_key: str,
    registrant_key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a GoToWebinar registrant by key.

    Args:
        webinar_key: GoToWebinar webinar key.
        registrant_key: GoToWebinar registrant key.
    """
    if not webinar_key.strip() or not registrant_key.strip():
        return "[Error]: webinar_key and registrant_key are required."
    try:
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_get_registrant",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_get_registrant",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "GET",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}/registrants/{quote(registrant_key.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("gotowebinar_get_registrant failed", exc_info=True)
        return f"[Error]: GoToWebinar registrant lookup failed: {e}"


@tool
def gotowebinar_create_registrant(
    webinar_key: str,
    first_name: str,
    last_name: str,
    email: str,
    fields_json: str = "",
    resend_confirmation: Optional[bool] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a GoToWebinar registrant.

    Args:
        webinar_key: GoToWebinar webinar key.
        first_name: Registrant first name.
        last_name: Registrant last name.
        email: Registrant email address.
        fields_json: Optional JSON object with extra registrant fields or responses.
        resend_confirmation: Whether to resend the confirmation email.
    """
    if not webinar_key.strip() or not first_name.strip() or not last_name.strip() or not email.strip():
        return "[Error]: webinar_key, first_name, last_name, and email are required."
    try:
        body = _json_object(fields_json, field_name="fields_json")
        body.update(
            {
                "firstName": first_name.strip(),
                "lastName": last_name.strip(),
                "email": email.strip(),
            }
        )
        body.setdefault("responses", [])
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_create_registrant",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_create_registrant",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        data = _request_json(
            "POST",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}/registrants",
            params={"resendConfirmation": resend_confirmation},
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("gotowebinar_create_registrant failed", exc_info=True)
        return f"[Error]: GoToWebinar registrant creation failed: {e}"


@tool
def gotowebinar_delete_registrant(
    webinar_key: str,
    registrant_key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a GoToWebinar registrant.

    Args:
        webinar_key: GoToWebinar webinar key.
        registrant_key: GoToWebinar registrant key.
    """
    if not webinar_key.strip() or not registrant_key.strip():
        return "[Error]: webinar_key and registrant_key are required."
    try:
        base_url, headers_or_error, _account_key, organizer_key = _gotowebinar_config(
            "gotowebinar_delete_registrant",
            config,
        )
        if isinstance(headers_or_error, str):
            return headers_or_error
        if not organizer_key:
            return _missing_gotowebinar_key(
                "gotowebinar_delete_registrant",
                "organizer_key",
                "GOTOWEBINAR_ORGANIZER_KEY",
            )
        _request_json(
            "DELETE",
            f"{base_url}/organizers/{quote(organizer_key.strip(), safe='')}/webinars/{quote(webinar_key.strip(), safe='')}/registrants/{quote(registrant_key.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("gotowebinar_delete_registrant failed", exc_info=True)
        return f"[Error]: GoToWebinar registrant deletion failed: {e}"


EVENT_MEETING_SERVICE_TOOLS = [
    demio_list_events,
    demio_get_event,
    demio_register_event,
    demio_get_session_participants,
    zoom_list_meetings,
    zoom_get_meeting,
    zoom_create_meeting,
    zoom_update_meeting,
    zoom_delete_meeting,
    gotowebinar_list_webinars,
    gotowebinar_get_webinar,
    gotowebinar_create_webinar,
    gotowebinar_update_webinar,
    gotowebinar_list_sessions,
    gotowebinar_get_session,
    gotowebinar_list_registrants,
    gotowebinar_get_registrant,
    gotowebinar_create_registrant,
    gotowebinar_delete_registrant,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="event_meeting", tools=tuple(EVENT_MEETING_SERVICE_TOOLS)))
