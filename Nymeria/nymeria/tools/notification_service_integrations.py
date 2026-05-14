"""Notification, push, and alerting service integration tools."""

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
_PUSHBULLET_BASE_URL = "https://api.pushbullet.com/v2"
_PUSHCUT_BASE_URL = "https://api.pushcut.io/v1"
_PUSHOVER_BASE_URL = "https://api.pushover.net/1"
_SIGNL4_BASE_URL = "https://connect.signl4.com/webhook"


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
            detail = (
                body.get("error")
                or body.get("errorDescription")
                or body.get("message")
                or body.get("detail")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _pushbullet_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="pushbullet",
            provider_aliases=("pushbullet_oauth2", "pushbullet_oauth2_api"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pushbullet_base_url")
        or _PUSHBULLET_BASE_URL
    )
    token = _credential_value(
        provider="pushbullet",
        provider_aliases=("pushbullet_oauth2", "pushbullet_oauth2_api"),
        field_names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("pushbullet_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="pushbullet",
            field_names=("access_token", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="PUSHBULLET_ACCESS_TOKEN",
            display_name="Pushbullet",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Access-Token": token,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _pushcut_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="pushcut",
            provider_aliases=("pushcut_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pushcut_base_url")
        or _PUSHCUT_BASE_URL
    )
    api_key = _credential_value(
        provider="pushcut",
        provider_aliases=("pushcut_api",),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("pushcut_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="pushcut",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="PUSHCUT_API_KEY",
            display_name="Pushcut",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "API-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _gotify_base(tool_name: str, config: Optional[RunnableConfig]) -> str | None:
    base = (
        _credential_value(
            provider="gotify",
            provider_aliases=("gotify_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("gotify_base_url")
    )
    return _base_url(base) if base else None


def _gotify_config(
    *,
    token_kind: str,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str]:
    base = _gotify_base(tool_name, config)
    if not base:
        return "", (
            "[Error]: No Gotify base URL found. Save a Gotify credential with "
            '"base_url" / "url", or set GOTIFY_BASE_URL.'
        )
    if token_kind == "app":
        field_names = ("app_token", "appApiToken", "app_api_token", "token", "value")
        env_var = "GOTIFY_APP_TOKEN"
        token = _credential_value(
            provider="gotify",
            provider_aliases=("gotify_api",),
            field_names=field_names,
            tool_name=tool_name,
            config=config,
        ) or _settings_value("gotify_app_token")
    else:
        field_names = ("client_token", "clientApiToken", "client_api_token", "token", "value")
        env_var = "GOTIFY_CLIENT_TOKEN"
        token = _credential_value(
            provider="gotify",
            provider_aliases=("gotify_api",),
            field_names=field_names,
            tool_name=tool_name,
            config=config,
        ) or _settings_value("gotify_client_token")
    if not token:
        return base, _setup_hint(
            provider="gotify",
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name="Gotify",
        )
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "X-Gotify-Key": token,
    }


def _pushover_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="pushover",
            provider_aliases=("pushover_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pushover_base_url")
        or _PUSHOVER_BASE_URL
    )
    token = _credential_value(
        provider="pushover",
        provider_aliases=("pushover_api",),
        field_names=("api_token", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("pushover_api_token")
    user_key = _credential_value(
        provider="pushover",
        provider_aliases=("pushover_api",),
        field_names=("user_key", "userKey", "user", "group_key", "groupKey"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("pushover_user_key")
    if not token:
        return _base_url(base), "", "", _setup_hint(
            provider="pushover",
            field_names=("api_token", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="PUSHOVER_API_TOKEN",
            display_name="Pushover",
        )
    if not user_key:
        return _base_url(base), token, "", (
            "[Error]: No Pushover user/group key found. Save a Pushover credential with "
            '"user_key" / "user", or set PUSHOVER_USER_KEY.'
        )
    return _base_url(base), token, user_key, {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Nymeria",
    }


def _signl4_webhook(tool_name: str, config: Optional[RunnableConfig]) -> str | tuple[str, str]:
    webhook_url = (
        _credential_value(
            provider="signl4",
            provider_aliases=("signl4_api", "signl4_webhook"),
            field_names=("webhook_url", "webhookUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("signl4_webhook_url")
    )
    if webhook_url:
        return _base_url(webhook_url)
    base = (
        _credential_value(
            provider="signl4",
            provider_aliases=("signl4_api", "signl4_webhook"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("signl4_base_url")
        or _SIGNL4_BASE_URL
    )
    team_secret = _credential_value(
        provider="signl4",
        provider_aliases=("signl4_api", "signl4_webhook"),
        field_names=("team_secret", "teamSecret", "secret", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("signl4_team_secret")
    if not team_secret:
        return "", _setup_hint(
            provider="signl4",
            field_names=("team_secret", "teamSecret", "secret", "webhook_url", "value"),
            tool_name=tool_name,
            env_var="SIGNL4_TEAM_SECRET or SIGNL4_WEBHOOK_URL",
            display_name="SIGNL4",
        )
    return f"{_base_url(base)}/{quote(team_secret.strip(), safe='')}"


@tool
def pushbullet_send_push(
    title: str = "",
    body: str = "",
    url: str = "",
    push_type: str = "note",
    target_kind: str = "",
    target_value: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Pushbullet note or link push.

    Args:
        title: Push title.
        body: Push message body.
        url: URL for link pushes.
        push_type: Push type, either note or link.
        target_kind: Optional target: device_iden, email, channel_tag, or client_iden.
        target_value: Target value matching target_kind.
    """
    push_type = push_type.strip().lower() or "note"
    if push_type not in {"note", "link"}:
        return "[Error]: push_type must be note or link."
    if not title.strip() and not body.strip():
        return "[Error]: title or body is required."
    if push_type == "link" and not url.strip():
        return "[Error]: url is required for link pushes."
    try:
        base_url, headers_or_error = _pushbullet_config("pushbullet_send_push", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        payload = _filtered({"type": push_type, "title": title.strip(), "body": body.strip(), "url": url.strip()})
        target_kind = target_kind.strip()
        if target_kind or target_value.strip():
            if target_kind not in {"device_iden", "email", "channel_tag", "client_iden"}:
                return "[Error]: target_kind must be device_iden, email, channel_tag, or client_iden."
            if not target_value.strip():
                return "[Error]: target_value is required when target_kind is set."
            payload[target_kind] = target_value.strip()
        data = _request_json("POST", f"{base_url}/pushes", json_body=payload, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("pushbullet_send_push failed", exc_info=True)
        return f"[Error]: Pushbullet send failed: {e}"


@tool
def pushbullet_list_pushes(
    limit: int = 20,
    modified_after: str = "",
    active: bool = True,
    cursor: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Pushbullet push history.

    Args:
        limit: Number of pushes to return.
        modified_after: Optional unix timestamp filter for pushes modified after this value.
        active: Whether to include only active pushes.
        cursor: Optional pagination cursor.
    """
    try:
        base_url, headers_or_error = _pushbullet_config("pushbullet_list_pushes", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/pushes",
            params={
                "limit": _limit(limit, default=20, max_value=500),
                "modified_after": modified_after.strip(),
                "active": "true" if active else "false",
                "cursor": cursor.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("pushbullet_list_pushes failed", exc_info=True)
        return f"[Error]: Pushbullet list failed: {e}"


@tool
def pushbullet_update_push(
    push_id: str,
    dismissed: bool = True,
    items_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Pushbullet push, usually to dismiss it.

    Args:
        push_id: Pushbullet push iden.
        dismissed: Whether to mark the push dismissed.
        items_json: Optional list-push items JSON array.
    """
    if not push_id.strip():
        return "[Error]: push_id is required."
    try:
        payload: dict[str, Any] = {"dismissed": dismissed}
        if items_json.strip():
            payload["items"] = _parse_json(items_json, expected=list, label="items_json")
        base_url, headers_or_error = _pushbullet_config("pushbullet_update_push", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/pushes/{quote(push_id.strip(), safe='')}",
            json_body=payload,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("pushbullet_update_push failed", exc_info=True)
        return f"[Error]: Pushbullet update failed: {e}"


@tool
def pushbullet_delete_push(
    push_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Pushbullet push by ID.

    Args:
        push_id: Pushbullet push iden.
    """
    if not push_id.strip():
        return "[Error]: push_id is required."
    try:
        base_url, headers_or_error = _pushbullet_config("pushbullet_delete_push", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/pushes/{quote(push_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("pushbullet_delete_push failed", exc_info=True)
        return f"[Error]: Pushbullet delete failed: {e}"


@tool
def pushcut_send_notification(
    notification_name: str,
    title: str = "",
    text: str = "",
    input_text: str = "",
    device_names: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Pushcut notification.

    Args:
        notification_name: Pushcut notification name or reference ID.
        title: Optional title override.
        text: Optional text override.
        input_text: Optional input passed to the notification action.
        device_names: Optional comma-separated device names.
        fields_json: Optional extra Pushcut notification fields as JSON.
    """
    if not notification_name.strip():
        return "[Error]: notification_name is required."
    try:
        payload = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "title": title.strip(),
                    "text": text.strip(),
                    "input": input_text.strip(),
                    "devices": [part.strip() for part in device_names.split(",") if part.strip()],
                }
            ),
        }
        base_url, headers_or_error = _pushcut_config("pushcut_send_notification", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/notifications/{quote(notification_name.strip(), safe='')}",
            json_body=payload,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("pushcut_send_notification failed", exc_info=True)
        return f"[Error]: Pushcut notification send failed: {e}"


@tool
def gotify_send_message(
    message: str,
    title: str = "",
    priority: int = 5,
    extras_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Gotify message with an application token.

    Args:
        message: Message body.
        title: Optional message title.
        priority: Message priority.
        extras_json: Optional Gotify extras object as JSON.
    """
    if not message.strip():
        return "[Error]: message is required."
    try:
        base_url, headers_or_error = _gotify_config(token_kind="app", tool_name="gotify_send_message", config=config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        payload = _filtered(
            {
                "message": message,
                "title": title.strip(),
                "priority": int(priority),
                "extras": _parse_json(extras_json, expected=dict, label="extras_json"),
            }
        )
        data = _request_json("POST", f"{base_url}/message", json_body=payload, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("gotify_send_message failed", exc_info=True)
        return f"[Error]: Gotify message send failed: {e}"


@tool
def gotify_list_messages(
    limit: int = 100,
    since: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Gotify messages with a client token.

    Args:
        limit: Number of messages to return, 1-200.
        since: Optional message ID pagination cursor.
    """
    try:
        base_url, headers_or_error = _gotify_config(token_kind="client", tool_name="gotify_list_messages", config=config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/message",
            params={"limit": _limit(limit, default=100, max_value=200), "since": since if since else ""},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("gotify_list_messages failed", exc_info=True)
        return f"[Error]: Gotify message list failed: {e}"


@tool
def gotify_delete_message(
    message_id: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Gotify message by ID with a client token.

    Args:
        message_id: Gotify message ID.
    """
    if int(message_id) <= 0:
        return "[Error]: message_id must be positive."
    try:
        base_url, headers_or_error = _gotify_config(token_kind="client", tool_name="gotify_delete_message", config=config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/message/{int(message_id)}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("gotify_delete_message failed", exc_info=True)
        return f"[Error]: Gotify message delete failed: {e}"


@tool
def pushover_send_message(
    message: str,
    title: str = "",
    priority: int = 0,
    device: str = "",
    sound: str = "",
    url: str = "",
    url_title: str = "",
    retry_seconds: int = 0,
    expire_seconds: int = 0,
    ttl_seconds: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Pushover message.

    Args:
        message: Message body.
        title: Optional message title.
        priority: Priority from -2 through 2.
        device: Optional device name or comma-separated device names.
        sound: Optional Pushover sound name.
        url: Optional supplementary URL.
        url_title: Optional title for the supplementary URL.
        retry_seconds: Required for emergency priority 2.
        expire_seconds: Required for emergency priority 2.
        ttl_seconds: Optional time-to-live in seconds.
    """
    if not message.strip():
        return "[Error]: message is required."
    if priority == 2 and (retry_seconds < 30 or expire_seconds <= 0):
        return "[Error]: priority 2 requires retry_seconds >= 30 and expire_seconds > 0."
    try:
        base_url, token, user_key, headers_or_error = _pushover_config("pushover_send_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        form = {
            "token": token,
            "user": user_key,
            "message": message,
            "title": title.strip(),
            "priority": int(priority),
            "device": device.strip(),
            "sound": sound.strip(),
            "url": url.strip(),
            "url_title": url_title.strip(),
            "retry": int(retry_seconds) if retry_seconds else "",
            "expire": int(expire_seconds) if expire_seconds else "",
            "ttl": int(ttl_seconds) if ttl_seconds else "",
        }
        data = _request_json("POST", f"{base_url}/messages.json", form_data=form, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("pushover_send_message failed", exc_info=True)
        return f"[Error]: Pushover message send failed: {e}"


@tool
def signl4_send_alert(
    message: str,
    title: str = "",
    external_id: str = "",
    service: str = "",
    location: str = "",
    alerting_scenario: str = "",
    filtering: bool = False,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a SIGNL4 alert event.

    Args:
        message: Alert message.
        title: Optional alert title.
        external_id: Optional external correlation ID.
        service: Optional service/category name.
        location: Optional location, such as "latitude,longitude".
        alerting_scenario: Optional single_ack, multi_ack, or emergency.
        filtering: Whether SIGNL4 event filtering should apply.
        fields_json: Optional extra alert fields as JSON.
    """
    if not message.strip():
        return "[Error]: message is required."
    try:
        webhook = _signl4_webhook("signl4_send_alert", config)
        if isinstance(webhook, tuple):
            return webhook[1]
        payload = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "Message": message,
                    "Title": title.strip(),
                    "X-S4-ExternalID": external_id.strip(),
                    "X-S4-Service": service.strip(),
                    "X-S4-Location": location.strip(),
                    "X-S4-AlertingScenario": alerting_scenario.strip(),
                    "X-S4-Filtering": "true" if filtering else "",
                }
            ),
        }
        data = _request_json(
            "POST",
            webhook,
            json_body=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("signl4_send_alert failed", exc_info=True)
        return f"[Error]: SIGNL4 alert send failed: {e}"


@tool
def signl4_resolve_alert(
    external_id: str,
    message: str = "Resolved",
    title: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Resolve a SIGNL4 alert by external ID.

    Args:
        external_id: External ID originally sent as X-S4-ExternalID.
        message: Resolution message.
        title: Optional resolution title.
        fields_json: Optional extra event fields as JSON.
    """
    if not external_id.strip():
        return "[Error]: external_id is required."
    try:
        webhook = _signl4_webhook("signl4_resolve_alert", config)
        if isinstance(webhook, tuple):
            return webhook[1]
        payload = {
            **_parse_json(fields_json, expected=dict, label="fields_json"),
            **_filtered(
                {
                    "Message": message,
                    "Title": title.strip(),
                    "X-S4-ExternalID": external_id.strip(),
                    "X-S4-Status": "resolved",
                }
            ),
        }
        data = _request_json(
            "POST",
            webhook,
            json_body=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("signl4_resolve_alert failed", exc_info=True)
        return f"[Error]: SIGNL4 alert resolve failed: {e}"


NOTIFICATION_SERVICE_TOOLS = [
    pushbullet_send_push,
    pushbullet_list_pushes,
    pushbullet_update_push,
    pushbullet_delete_push,
    pushcut_send_notification,
    gotify_send_message,
    gotify_list_messages,
    gotify_delete_message,
    pushover_send_message,
    signl4_send_alert,
    signl4_resolve_alert,
]
