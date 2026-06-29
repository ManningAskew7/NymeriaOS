"""Chat and community platform service tools."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Annotated, Any, Optional
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    parse_json as _parse_json,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_DISCORD_BASE_URL = "https://discord.com/api/v10"
_MATRIX_BASE_URL = "https://matrix-client.matrix.org/_matrix/client/v3"
_TELEGRAM_BASE_URL = "https://api.telegram.org"
_WEBEX_BASE_URL = "https://webexapis.com/v1"
_WHATSAPP_BASE_URL = "https://graph.facebook.com/v19.0"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 50, max_value: int = 200) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    data: Optional[dict[str, Any]] = None,
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
                data=data,
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
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _json_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _api_token_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    env_var: str,
    settings_key_name: str,
    settings_base_name: str,
    default_base: str,
    tool_name: str,
    display_name: str,
    config: Optional[RunnableConfig],
    field_names: tuple[str, ...] = (
        "api_key",
        "apiKey",
        "api_token",
        "apiToken",
        "access_token",
        "accessToken",
        "token",
        "bot_token",
        "botToken",
        "value",
    ),
) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=("base_url", "baseUrl", "homeserverUrl", "domain", "url", "api_url", "apiUrl"),
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
    ) or _settings_value(settings_key_name)
    if not token:
        return "", _setup_hint(
            provider=provider,
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    if not base:
        return "", f"[Error]: No {display_name} base URL found. Set {settings_base_name.upper()} or save base_url in the credential."
    return _base_url(base), token


def _append_path(base: str, suffix: str) -> str:
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _telegram_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    base_url, token = _api_token_config(
        provider="telegram",
        provider_aliases=("telegram_bot", "telegram_api", "telegramApi"),
        env_var="TELEGRAM_BOT_TOKEN",
        settings_key_name="telegram_bot_token",
        settings_base_name="telegram_api_base_url",
        default_base=_TELEGRAM_BASE_URL,
        tool_name=tool_name,
        display_name="Telegram",
        config=config,
        field_names=("bot_token", "botToken", "api_key", "apiKey", "token", "value"),
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    return base_url, token


def _telegram_url(base_url: str, token: str, method: str) -> str:
    return f"{base_url}/bot{token}/{method}"


def _webex_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, token = _api_token_config(
        provider="webex",
        provider_aliases=("cisco_webex", "ciscoWebex", "webex_api", "cisco_webex_api"),
        env_var="WEBEX_ACCESS_TOKEN",
        settings_key_name="webex_access_token",
        settings_base_name="webex_base_url",
        default_base=_WEBEX_BASE_URL,
        tool_name=tool_name,
        display_name="Webex",
        config=config,
        field_names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    headers = _json_headers()
    headers["Authorization"] = f"Bearer {token}"
    return base_url, headers


def _whatsapp_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[dict[str, Any], str | None]:
    access_token = _credential_value(
        provider="whatsapp",
        provider_aliases=("whats_app", "whatsapp_business", "whatsapp_api", "whatsAppApi"),
        field_names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("whatsapp_access_token")
    business_account_id = _credential_value(
        provider="whatsapp",
        provider_aliases=("whats_app", "whatsapp_business", "whatsapp_api", "whatsAppApi"),
        field_names=("business_account_id", "businessAccountId", "account_id", "accountId"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("whatsapp_business_account_id")
    phone_number_id = _credential_value(
        provider="whatsapp",
        provider_aliases=("whats_app", "whatsapp_business", "whatsapp_api", "whatsAppApi"),
        field_names=("phone_number_id", "phoneNumberId", "sender_phone_number_id", "senderPhoneNumberId"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("whatsapp_phone_number_id")
    base_url = (
        _credential_value(
            provider="whatsapp",
            provider_aliases=("whats_app", "whatsapp_business", "whatsapp_api", "whatsAppApi"),
            field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("whatsapp_base_url")
        or _WHATSAPP_BASE_URL
    )
    if not access_token:
        return {}, _setup_hint(
            provider="whatsapp",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="WHATSAPP_ACCESS_TOKEN",
            display_name="WhatsApp Business Cloud",
        )
    headers = _json_headers()
    headers["Authorization"] = f"Bearer {access_token}"
    return {
        "base_url": _base_url(base_url),
        "business_account_id": business_account_id or "",
        "phone_number_id": phone_number_id or "",
        "headers": headers,
    }, None


def _whatsapp_phone_id(cfg: dict[str, Any], phone_number_id: str) -> str | None:
    resolved = phone_number_id.strip() or str(cfg.get("phone_number_id") or "").strip()
    return resolved or None


@tool
def telegram_get_me(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the Telegram bot profile."""
    try:
        base_url, token_or_error = _telegram_config("telegram_get_me", config)
        if not token_or_error or token_or_error.startswith("[Error]:"):
            return token_or_error or ""
        return _dump_json(_request_json("GET", _telegram_url(base_url, token_or_error, "getMe")))
    except Exception as e:
        logger.error("telegram_get_me failed", exc_info=True)
        return f"[Error]: Telegram bot lookup failed: {e}"


@tool
def telegram_get_chat(
    chat_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Telegram chat metadata."""
    if not chat_id.strip():
        return "[Error]: chat_id is required."
    try:
        base_url, token_or_error = _telegram_config("telegram_get_chat", config)
        if not token_or_error or token_or_error.startswith("[Error]:"):
            return token_or_error or ""
        return _dump_json(
            _request_json(
                "POST",
                _telegram_url(base_url, token_or_error, "getChat"),
                json_body={"chat_id": chat_id.strip()},
            )
        )
    except Exception as e:
        logger.error("telegram_get_chat failed", exc_info=True)
        return f"[Error]: Telegram chat lookup failed: {e}"


@tool
def telegram_send_message(
    chat_id: str,
    text: str,
    parse_mode: str = "",
    reply_to_message_id: int = 0,
    disable_notification: bool = False,
    reply_markup_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Telegram text message."""
    if not chat_id.strip() or not text.strip():
        return "[Error]: chat_id and text are required."
    try:
        body: dict[str, Any] = {
            "chat_id": chat_id.strip(),
            "text": text,
            "disable_notification": bool(disable_notification),
        }
        if parse_mode.strip():
            body["parse_mode"] = parse_mode.strip()
        if reply_to_message_id:
            body["reply_to_message_id"] = int(reply_to_message_id)
        if reply_markup_json.strip():
            body["reply_markup"] = _parse_json(reply_markup_json, expected=dict, label="reply_markup_json")
        base_url, token_or_error = _telegram_config("telegram_send_message", config)
        if not token_or_error or token_or_error.startswith("[Error]:"):
            return token_or_error or ""
        return _dump_json(_request_json("POST", _telegram_url(base_url, token_or_error, "sendMessage"), json_body=body))
    except Exception as e:
        logger.error("telegram_send_message failed", exc_info=True)
        return f"[Error]: Telegram message send failed: {e}"


@tool
def telegram_delete_message(
    chat_id: str,
    message_id: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Telegram message."""
    if not chat_id.strip() or not message_id:
        return "[Error]: chat_id and message_id are required."
    try:
        base_url, token_or_error = _telegram_config("telegram_delete_message", config)
        if not token_or_error or token_or_error.startswith("[Error]:"):
            return token_or_error or ""
        body = {"chat_id": chat_id.strip(), "message_id": int(message_id)}
        return _dump_json(_request_json("POST", _telegram_url(base_url, token_or_error, "deleteMessage"), json_body=body))
    except Exception as e:
        logger.error("telegram_delete_message failed", exc_info=True)
        return f"[Error]: Telegram message deletion failed: {e}"


@tool
def webex_list_rooms(
    room_type: str = "",
    team_id: str = "",
    max_results: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Webex rooms."""
    try:
        base_url, headers_or_error = _webex_config("webex_list_rooms", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"type": room_type.strip(), "teamId": team_id.strip(), "max": _limit(max_results, max_value=100)}
        return _dump_json(_request_json("GET", f"{base_url}/rooms", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("webex_list_rooms failed", exc_info=True)
        return f"[Error]: Webex room list failed: {e}"


@tool
def webex_get_room(
    room_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Webex room metadata."""
    if not room_id.strip():
        return "[Error]: room_id is required."
    try:
        base_url, headers_or_error = _webex_config("webex_get_room", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/rooms/{quote(room_id.strip(), safe='')}", headers=headers_or_error))
    except Exception as e:
        logger.error("webex_get_room failed", exc_info=True)
        return f"[Error]: Webex room lookup failed: {e}"


@tool
def webex_list_messages(
    room_id: str = "",
    person_id: str = "",
    person_email: str = "",
    mentioned_people: str = "",
    before: str = "",
    before_message: str = "",
    max_results: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Webex messages."""
    try:
        base_url, headers_or_error = _webex_config("webex_list_messages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "roomId": room_id.strip(),
            "personId": person_id.strip(),
            "personEmail": person_email.strip(),
            "mentionedPeople": mentioned_people.strip(),
            "before": before.strip(),
            "beforeMessage": before_message.strip(),
            "max": _limit(max_results, max_value=100),
        }
        return _dump_json(_request_json("GET", f"{base_url}/messages", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("webex_list_messages failed", exc_info=True)
        return f"[Error]: Webex message list failed: {e}"


@tool
def webex_get_message(
    message_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Webex message."""
    if not message_id.strip():
        return "[Error]: message_id is required."
    try:
        base_url, headers_or_error = _webex_config("webex_get_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/messages/{quote(message_id.strip(), safe='')}"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("webex_get_message failed", exc_info=True)
        return f"[Error]: Webex message lookup failed: {e}"


@tool
def webex_send_message(
    text: str = "",
    markdown: str = "",
    room_id: str = "",
    to_person_id: str = "",
    to_person_email: str = "",
    files: str = "",
    attachments_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Webex message to a room or person."""
    if not (room_id.strip() or to_person_id.strip() or to_person_email.strip()):
        return "[Error]: room_id, to_person_id, or to_person_email is required."
    if not (text.strip() or markdown.strip() or files.strip() or attachments_json.strip()):
        return "[Error]: Provide text, markdown, files, or attachments_json."
    try:
        body: dict[str, Any] = {}
        if room_id.strip():
            body["roomId"] = room_id.strip()
        if to_person_id.strip():
            body["toPersonId"] = to_person_id.strip()
        if to_person_email.strip():
            body["toPersonEmail"] = to_person_email.strip()
        if text.strip():
            body["text"] = text
        if markdown.strip():
            body["markdown"] = markdown
        file_list = [item.strip() for item in files.split(",") if item.strip()]
        if file_list:
            body["files"] = file_list
        if attachments_json.strip():
            body["attachments"] = _parse_json(attachments_json, expected=list, label="attachments_json")
        base_url, headers_or_error = _webex_config("webex_send_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("POST", f"{base_url}/messages", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("webex_send_message failed", exc_info=True)
        return f"[Error]: Webex message send failed: {e}"


@tool
def webex_delete_message(
    message_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Webex message."""
    if not message_id.strip():
        return "[Error]: message_id is required."
    try:
        base_url, headers_or_error = _webex_config("webex_delete_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/messages/{quote(message_id.strip(), safe='')}"
        data = _request_json("DELETE", f"{base_url}{endpoint}", headers=headers_or_error)
        return _dump_json({"success": True, "message_id": message_id.strip(), "response": data})
    except Exception as e:
        logger.error("webex_delete_message failed", exc_info=True)
        return f"[Error]: Webex message deletion failed: {e}"


@tool
def whatsapp_list_phone_numbers(
    business_account_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List WhatsApp Business Cloud phone numbers."""
    try:
        cfg, error = _whatsapp_config("whatsapp_list_phone_numbers", config)
        if error:
            return error
        account_id = business_account_id.strip() or str(cfg.get("business_account_id") or "").strip()
        if not account_id:
            return "[Error]: business_account_id is required. Save it in the credential or set WHATSAPP_BUSINESS_ACCOUNT_ID."
        return _dump_json(
            _request_json("GET", f"{cfg['base_url']}/{quote(account_id, safe='')}/phone_numbers", headers=cfg["headers"])
        )
    except Exception as e:
        logger.error("whatsapp_list_phone_numbers failed", exc_info=True)
        return f"[Error]: WhatsApp phone number list failed: {e}"


@tool
def whatsapp_send_text_message(
    to: str,
    text: str,
    phone_number_id: str = "",
    preview_url: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a WhatsApp Business Cloud text message."""
    if not to.strip() or not text.strip():
        return "[Error]: to and text are required."
    try:
        cfg, error = _whatsapp_config("whatsapp_send_text_message", config)
        if error:
            return error
        phone_id = _whatsapp_phone_id(cfg, phone_number_id)
        if not phone_id:
            return "[Error]: phone_number_id is required. Save it in the credential or set WHATSAPP_PHONE_NUMBER_ID."
        body = {
            "messaging_product": "whatsapp",
            "to": to.strip(),
            "type": "text",
            "text": {"body": text, "preview_url": bool(preview_url)},
        }
        endpoint = f"/{quote(phone_id, safe='')}/messages"
        return _dump_json(_request_json("POST", f"{cfg['base_url']}{endpoint}", json_body=body, headers=cfg["headers"]))
    except Exception as e:
        logger.error("whatsapp_send_text_message failed", exc_info=True)
        return f"[Error]: WhatsApp text send failed: {e}"


@tool
def whatsapp_send_template_message(
    to: str,
    template_name: str,
    language_code: str = "en_US",
    components_json: str = "",
    phone_number_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a WhatsApp Business Cloud template message."""
    if not to.strip() or not template_name.strip():
        return "[Error]: to and template_name are required."
    try:
        cfg, error = _whatsapp_config("whatsapp_send_template_message", config)
        if error:
            return error
        phone_id = _whatsapp_phone_id(cfg, phone_number_id)
        if not phone_id:
            return "[Error]: phone_number_id is required. Save it in the credential or set WHATSAPP_PHONE_NUMBER_ID."
        template: dict[str, Any] = {
            "name": template_name.strip(),
            "language": {"code": language_code.strip() or "en_US"},
        }
        if components_json.strip():
            template["components"] = _parse_json(components_json, expected=list, label="components_json")
        body = {
            "messaging_product": "whatsapp",
            "to": to.strip(),
            "type": "template",
            "template": template,
        }
        endpoint = f"/{quote(phone_id, safe='')}/messages"
        return _dump_json(_request_json("POST", f"{cfg['base_url']}{endpoint}", json_body=body, headers=cfg["headers"]))
    except Exception as e:
        logger.error("whatsapp_send_template_message failed", exc_info=True)
        return f"[Error]: WhatsApp template send failed: {e}"


@tool
def whatsapp_get_media_url(
    media_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a WhatsApp Business Cloud media download URL."""
    if not media_id.strip():
        return "[Error]: media_id is required."
    try:
        cfg, error = _whatsapp_config("whatsapp_get_media_url", config)
        if error:
            return error
        return _dump_json(
            _request_json("GET", f"{cfg['base_url']}/{quote(media_id.strip(), safe='')}", headers=cfg["headers"])
        )
    except Exception as e:
        logger.error("whatsapp_get_media_url failed", exc_info=True)
        return f"[Error]: WhatsApp media lookup failed: {e}"


@tool
def whatsapp_delete_media(
    media_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete WhatsApp Business Cloud media."""
    if not media_id.strip():
        return "[Error]: media_id is required."
    try:
        cfg, error = _whatsapp_config("whatsapp_delete_media", config)
        if error:
            return error
        data = _request_json("DELETE", f"{cfg['base_url']}/{quote(media_id.strip(), safe='')}", headers=cfg["headers"])
        return _dump_json({"success": True, "media_id": media_id.strip(), "response": data})
    except Exception as e:
        logger.error("whatsapp_delete_media failed", exc_info=True)
        return f"[Error]: WhatsApp media deletion failed: {e}"


def _discord_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, token = _api_token_config(
        provider="discord",
        provider_aliases=("discord_bot", "discordBotApi", "discord_bot_api"),
        env_var="DISCORD_BOT_TOKEN",
        settings_key_name="discord_bot_token",
        settings_base_name="discord_base_url",
        default_base=_DISCORD_BASE_URL,
        tool_name=tool_name,
        display_name="Discord",
        config=config,
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    headers = _json_headers()
    headers["Authorization"] = token if token.lower().startswith("bot ") else f"Bot {token}"
    return base_url, headers


def _mattermost_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, token = _api_token_config(
        provider="mattermost",
        provider_aliases=("mattermost_api", "mattermostApi"),
        env_var="MATTERMOST_ACCESS_TOKEN",
        settings_key_name="mattermost_access_token",
        settings_base_name="mattermost_base_url",
        default_base="",
        tool_name=tool_name,
        display_name="Mattermost",
        config=config,
        field_names=("access_token", "accessToken", "api_token", "apiToken", "token", "value"),
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    headers = _json_headers()
    headers["Authorization"] = f"Bearer {token}"
    return _append_path(base_url, "/api/v4"), headers


def _matrix_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, token = _api_token_config(
        provider="matrix",
        provider_aliases=("matrix_api", "matrixApi"),
        env_var="MATRIX_ACCESS_TOKEN",
        settings_key_name="matrix_access_token",
        settings_base_name="matrix_base_url",
        default_base=_MATRIX_BASE_URL,
        tool_name=tool_name,
        display_name="Matrix",
        config=config,
        field_names=("access_token", "accessToken", "token", "value"),
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    headers = _json_headers()
    headers["Authorization"] = f"Bearer {token}"
    return _append_path(base_url, "/_matrix/client/v3") if "/_matrix/client/" not in base_url else base_url, headers


def _rocketchat_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url = (
        _credential_value(
            provider="rocketchat",
            provider_aliases=("rocket_chat", "rocketchat_api", "rocketchatApi"),
            field_names=("base_url", "domain", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("rocketchat_base_url")
        or ""
    )
    auth_token = _credential_value(
        provider="rocketchat",
        provider_aliases=("rocket_chat", "rocketchat_api", "rocketchatApi"),
        field_names=("auth_token", "authToken", "auth_key", "authKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("rocketchat_auth_token")
    user_id = _credential_value(
        provider="rocketchat",
        provider_aliases=("rocket_chat", "rocketchat_api", "rocketchatApi"),
        field_names=("user_id", "userId", "userid"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("rocketchat_user_id")
    if not auth_token:
        return "", _setup_hint(
            provider="rocketchat",
            field_names=("auth_token", "auth_key", "token", "value"),
            tool_name=tool_name,
            env_var="ROCKETCHAT_AUTH_TOKEN",
            display_name="Rocket.Chat",
        )
    if not user_id:
        return "", "[Error]: No Rocket.Chat user ID found. Save user_id in the credential or set ROCKETCHAT_USER_ID."
    if not base_url:
        return "", "[Error]: No Rocket.Chat base URL found. Save base_url/domain in the credential or set ROCKETCHAT_BASE_URL."
    headers = _json_headers()
    headers["X-Auth-Token"] = auth_token
    headers["X-User-Id"] = user_id
    return _append_path(_base_url(base_url), "/api/v1"), headers


def _zulip_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url = (
        _credential_value(
            provider="zulip",
            provider_aliases=("zulip_api", "zulipApi"),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("zulip_base_url")
        or ""
    )
    email = _credential_value(
        provider="zulip",
        provider_aliases=("zulip_api", "zulipApi"),
        field_names=("email", "username", "user"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("zulip_email")
    api_key = _credential_value(
        provider="zulip",
        provider_aliases=("zulip_api", "zulipApi"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("zulip_api_key")
    if not api_key:
        return "", _setup_hint(
            provider="zulip",
            field_names=("api_key", "apiKey", "token", "value"),
            tool_name=tool_name,
            env_var="ZULIP_API_KEY",
            display_name="Zulip",
        )
    if not email:
        return "", "[Error]: No Zulip email found. Save email in the credential or set ZULIP_EMAIL."
    if not base_url:
        return "", "[Error]: No Zulip base URL found. Save url/base_url in the credential or set ZULIP_BASE_URL."
    basic = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    headers = _json_headers()
    headers["Authorization"] = f"Basic {basic}"
    return _append_path(_base_url(base_url), "/api/v1"), headers


@tool
def discord_list_guild_channels(
    guild_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List channels in a Discord guild."""
    if not guild_id.strip():
        return "[Error]: guild_id is required."
    try:
        base_url, headers_or_error = _discord_config("discord_list_guild_channels", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/guilds/{quote(guild_id.strip(), safe='')}/channels"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("discord_list_guild_channels failed", exc_info=True)
        return f"[Error]: Discord channel list failed: {e}"


@tool
def discord_get_channel(
    channel_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Discord channel metadata."""
    if not channel_id.strip():
        return "[Error]: channel_id is required."
    try:
        base_url, headers_or_error = _discord_config("discord_get_channel", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(
            _request_json("GET", f"{base_url}/channels/{quote(channel_id.strip(), safe='')}", headers=headers_or_error)
        )
    except Exception as e:
        logger.error("discord_get_channel failed", exc_info=True)
        return f"[Error]: Discord channel lookup failed: {e}"


@tool
def discord_get_channel_messages(
    channel_id: str,
    limit: int = 50,
    before: str = "",
    after: str = "",
    around: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get recent Discord channel messages."""
    if not channel_id.strip():
        return "[Error]: channel_id is required."
    try:
        base_url, headers_or_error = _discord_config("discord_get_channel_messages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "limit": _limit(limit, max_value=100),
            "before": before.strip(),
            "after": after.strip(),
            "around": around.strip(),
        }
        endpoint = f"/channels/{quote(channel_id.strip(), safe='')}/messages"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("discord_get_channel_messages failed", exc_info=True)
        return f"[Error]: Discord message list failed: {e}"


@tool
def discord_send_channel_message(
    channel_id: str,
    content: str,
    embeds_json: str = "",
    allowed_mentions_json: str = "",
    message_reference_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Discord channel message."""
    if not channel_id.strip() or not content.strip():
        return "[Error]: channel_id and content are required."
    try:
        body: dict[str, Any] = {"content": content}
        if embeds_json.strip():
            body["embeds"] = _parse_json(embeds_json, expected=list, label="embeds_json")
        if allowed_mentions_json.strip():
            body["allowed_mentions"] = _parse_json(allowed_mentions_json, expected=dict, label="allowed_mentions_json")
        if message_reference_json.strip():
            body["message_reference"] = _parse_json(message_reference_json, expected=dict, label="message_reference_json")
        base_url, headers_or_error = _discord_config("discord_send_channel_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/channels/{quote(channel_id.strip(), safe='')}/messages"
        return _dump_json(_request_json("POST", f"{base_url}{endpoint}", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("discord_send_channel_message failed", exc_info=True)
        return f"[Error]: Discord message send failed: {e}"


@tool
def discord_delete_message(
    channel_id: str,
    message_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Discord message."""
    if not channel_id.strip() or not message_id.strip():
        return "[Error]: channel_id and message_id are required."
    try:
        base_url, headers_or_error = _discord_config("discord_delete_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/channels/{quote(channel_id.strip(), safe='')}/messages/{quote(message_id.strip(), safe='')}"
        data = _request_json("DELETE", f"{base_url}{endpoint}", headers=headers_or_error)
        return _dump_json({"success": True, "message_id": message_id.strip(), "response": data})
    except Exception as e:
        logger.error("discord_delete_message failed", exc_info=True)
        return f"[Error]: Discord message deletion failed: {e}"


@tool
def mattermost_get_me(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Mattermost user."""
    try:
        base_url, headers_or_error = _mattermost_config("mattermost_get_me", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/users/me", headers=headers_or_error))
    except Exception as e:
        logger.error("mattermost_get_me failed", exc_info=True)
        return f"[Error]: Mattermost user lookup failed: {e}"


@tool
def mattermost_list_teams(
    page: int = 0,
    per_page: int = 60,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mattermost teams."""
    try:
        base_url, headers_or_error = _mattermost_config("mattermost_list_teams", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"page": max(0, int(page)), "per_page": _limit(per_page, default=60, max_value=200)}
        return _dump_json(_request_json("GET", f"{base_url}/teams", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("mattermost_list_teams failed", exc_info=True)
        return f"[Error]: Mattermost team list failed: {e}"


@tool
def mattermost_list_channels(
    team_id: str,
    page: int = 0,
    per_page: int = 60,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mattermost channels in a team."""
    if not team_id.strip():
        return "[Error]: team_id is required."
    try:
        base_url, headers_or_error = _mattermost_config("mattermost_list_channels", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"page": max(0, int(page)), "per_page": _limit(per_page, default=60, max_value=200)}
        endpoint = f"/teams/{quote(team_id.strip(), safe='')}/channels"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("mattermost_list_channels failed", exc_info=True)
        return f"[Error]: Mattermost channel list failed: {e}"


@tool
def mattermost_list_channel_posts(
    channel_id: str,
    page: int = 0,
    per_page: int = 60,
    before: str = "",
    after: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List posts in a Mattermost channel."""
    if not channel_id.strip():
        return "[Error]: channel_id is required."
    try:
        base_url, headers_or_error = _mattermost_config("mattermost_list_channel_posts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "page": max(0, int(page)),
            "per_page": _limit(per_page, default=60, max_value=200),
            "before": before.strip(),
            "after": after.strip(),
        }
        endpoint = f"/channels/{quote(channel_id.strip(), safe='')}/posts"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("mattermost_list_channel_posts failed", exc_info=True)
        return f"[Error]: Mattermost post list failed: {e}"


@tool
def mattermost_create_post(
    channel_id: str,
    message: str,
    root_id: str = "",
    props_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Mattermost post."""
    if not channel_id.strip() or not message.strip():
        return "[Error]: channel_id and message are required."
    try:
        body: dict[str, Any] = {"channel_id": channel_id.strip(), "message": message}
        if root_id.strip():
            body["root_id"] = root_id.strip()
        if props_json.strip():
            body["props"] = _parse_json(props_json, expected=dict, label="props_json")
        base_url, headers_or_error = _mattermost_config("mattermost_create_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("POST", f"{base_url}/posts", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("mattermost_create_post failed", exc_info=True)
        return f"[Error]: Mattermost post creation failed: {e}"


@tool
def mattermost_delete_post(
    post_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Mattermost post."""
    if not post_id.strip():
        return "[Error]: post_id is required."
    try:
        base_url, headers_or_error = _mattermost_config("mattermost_delete_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/posts/{quote(post_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json({"success": True, "post_id": post_id.strip(), "response": data})
    except Exception as e:
        logger.error("mattermost_delete_post failed", exc_info=True)
        return f"[Error]: Mattermost post deletion failed: {e}"


@tool
def matrix_whoami(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Matrix account."""
    try:
        base_url, headers_or_error = _matrix_config("matrix_whoami", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/account/whoami", headers=headers_or_error))
    except Exception as e:
        logger.error("matrix_whoami failed", exc_info=True)
        return f"[Error]: Matrix account lookup failed: {e}"


@tool
def matrix_list_joined_rooms(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Matrix rooms the current account has joined."""
    try:
        base_url, headers_or_error = _matrix_config("matrix_list_joined_rooms", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/joined_rooms", headers=headers_or_error))
    except Exception as e:
        logger.error("matrix_list_joined_rooms failed", exc_info=True)
        return f"[Error]: Matrix room list failed: {e}"


@tool
def matrix_get_room_messages(
    room_id: str,
    limit: int = 50,
    from_token: str = "",
    direction: str = "b",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Matrix room messages."""
    if not room_id.strip():
        return "[Error]: room_id is required."
    try:
        base_url, headers_or_error = _matrix_config("matrix_get_room_messages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"limit": _limit(limit, max_value=100), "dir": direction.strip() or "b", "from": from_token.strip()}
        endpoint = f"/rooms/{quote(room_id.strip(), safe='')}/messages"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("matrix_get_room_messages failed", exc_info=True)
        return f"[Error]: Matrix message list failed: {e}"


@tool
def matrix_send_room_message(
    room_id: str,
    body: str,
    formatted_body: str = "",
    txn_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Matrix room text message."""
    if not room_id.strip() or not body.strip():
        return "[Error]: room_id and body are required."
    try:
        message: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if formatted_body.strip():
            message["format"] = "org.matrix.custom.html"
            message["formatted_body"] = formatted_body
        base_url, headers_or_error = _matrix_config("matrix_send_room_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        txid = txn_id.strip() or f"nymeria-{int(time.time() * 1000)}"
        endpoint = f"/rooms/{quote(room_id.strip(), safe='')}/send/m.room.message/{quote(txid, safe='')}"
        return _dump_json(_request_json("PUT", f"{base_url}{endpoint}", json_body=message, headers=headers_or_error))
    except Exception as e:
        logger.error("matrix_send_room_message failed", exc_info=True)
        return f"[Error]: Matrix message send failed: {e}"


@tool
def matrix_leave_room(
    room_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Leave a Matrix room."""
    if not room_id.strip():
        return "[Error]: room_id is required."
    try:
        base_url, headers_or_error = _matrix_config("matrix_leave_room", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/rooms/{quote(room_id.strip(), safe='')}/leave"
        return _dump_json(_request_json("POST", f"{base_url}{endpoint}", json_body={}, headers=headers_or_error))
    except Exception as e:
        logger.error("matrix_leave_room failed", exc_info=True)
        return f"[Error]: Matrix leave room failed: {e}"


@tool
def rocketchat_get_me(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Rocket.Chat user."""
    try:
        base_url, headers_or_error = _rocketchat_config("rocketchat_get_me", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/me", headers=headers_or_error))
    except Exception as e:
        logger.error("rocketchat_get_me failed", exc_info=True)
        return f"[Error]: Rocket.Chat user lookup failed: {e}"


@tool
def rocketchat_list_channels(
    count: int = 50,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Rocket.Chat public channels."""
    try:
        base_url, headers_or_error = _rocketchat_config("rocketchat_list_channels", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"count": _limit(count, max_value=200), "offset": max(0, int(offset))}
        return _dump_json(_request_json("GET", f"{base_url}/channels.list", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("rocketchat_list_channels failed", exc_info=True)
        return f"[Error]: Rocket.Chat channel list failed: {e}"


@tool
def rocketchat_get_channel_history(
    room_id: str = "",
    room_name: str = "",
    count: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Rocket.Chat channel history by room ID or room name."""
    if not room_id.strip() and not room_name.strip():
        return "[Error]: room_id or room_name is required."
    try:
        base_url, headers_or_error = _rocketchat_config("rocketchat_get_channel_history", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"roomId": room_id.strip(), "roomName": room_name.strip(), "count": _limit(count, max_value=200)}
        return _dump_json(_request_json("GET", f"{base_url}/channels.history", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("rocketchat_get_channel_history failed", exc_info=True)
        return f"[Error]: Rocket.Chat channel history failed: {e}"


@tool
def rocketchat_post_message(
    channel: str,
    text: str,
    alias: str = "",
    emoji: str = "",
    avatar: str = "",
    attachments_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Post a Rocket.Chat message."""
    if not channel.strip() or not text.strip():
        return "[Error]: channel and text are required."
    try:
        body: dict[str, Any] = {"channel": channel.strip(), "text": text}
        if alias.strip():
            body["alias"] = alias.strip()
        if emoji.strip():
            body["emoji"] = emoji.strip()
        if avatar.strip():
            body["avatar"] = avatar.strip()
        if attachments_json.strip():
            body["attachments"] = _parse_json(attachments_json, expected=list, label="attachments_json")
        base_url, headers_or_error = _rocketchat_config("rocketchat_post_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("POST", f"{base_url}/chat.postMessage", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("rocketchat_post_message failed", exc_info=True)
        return f"[Error]: Rocket.Chat message send failed: {e}"


@tool
def rocketchat_delete_message(
    room_id: str,
    message_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Rocket.Chat message."""
    if not room_id.strip() or not message_id.strip():
        return "[Error]: room_id and message_id are required."
    try:
        base_url, headers_or_error = _rocketchat_config("rocketchat_delete_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"roomId": room_id.strip(), "msgId": message_id.strip()}
        return _dump_json(_request_json("POST", f"{base_url}/chat.delete", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("rocketchat_delete_message failed", exc_info=True)
        return f"[Error]: Rocket.Chat message deletion failed: {e}"


@tool
def zulip_get_profile(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current Zulip user profile."""
    try:
        base_url, headers_or_error = _zulip_config("zulip_get_profile", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/users/me", headers=headers_or_error))
    except Exception as e:
        logger.error("zulip_get_profile failed", exc_info=True)
        return f"[Error]: Zulip profile lookup failed: {e}"


@tool
def zulip_list_streams(
    include_public: bool = True,
    include_subscribed: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Zulip streams."""
    try:
        base_url, headers_or_error = _zulip_config("zulip_list_streams", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "include_public": str(bool(include_public)).lower(),
            "include_subscribed": str(bool(include_subscribed)).lower(),
        }
        return _dump_json(_request_json("GET", f"{base_url}/streams", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("zulip_list_streams failed", exc_info=True)
        return f"[Error]: Zulip stream list failed: {e}"


@tool
def zulip_get_messages(
    anchor: str = "newest",
    num_before: int = 20,
    num_after: int = 0,
    narrow_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Zulip messages."""
    try:
        base_url, headers_or_error = _zulip_config("zulip_get_messages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {
            "anchor": anchor.strip() or "newest",
            "num_before": _limit(num_before, default=20, max_value=500),
            "num_after": max(0, min(500, int(num_after))),
        }
        if narrow_json.strip():
            params["narrow"] = json.dumps(_parse_json(narrow_json, expected=list, label="narrow_json"))
        return _dump_json(_request_json("GET", f"{base_url}/messages", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("zulip_get_messages failed", exc_info=True)
        return f"[Error]: Zulip message lookup failed: {e}"


@tool
def zulip_send_message(
    message_type: str,
    to: str,
    content: str,
    topic: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Zulip message.

    Args:
        message_type: stream or private.
        to: Stream name for stream messages, or comma-separated emails/user IDs for private messages.
        content: Message content.
        topic: Required for stream messages.
    """
    if message_type not in {"stream", "private"}:
        return "[Error]: message_type must be stream or private."
    if not to.strip() or not content.strip():
        return "[Error]: to and content are required."
    if message_type == "stream" and not topic.strip():
        return "[Error]: topic is required for stream messages."
    try:
        base_url, headers_or_error = _zulip_config("zulip_send_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = {"type": message_type, "to": to.strip(), "content": content}
        if topic.strip():
            data["topic"] = topic.strip()
        return _dump_json(_request_json("POST", f"{base_url}/messages", data=data, headers=headers_or_error))
    except Exception as e:
        logger.error("zulip_send_message failed", exc_info=True)
        return f"[Error]: Zulip message send failed: {e}"


@tool
def zulip_delete_message(
    message_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Zulip message."""
    if not message_id.strip():
        return "[Error]: message_id is required."
    try:
        base_url, headers_or_error = _zulip_config("zulip_delete_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("DELETE", f"{base_url}/messages/{quote(message_id.strip(), safe='')}", headers=headers_or_error))
    except Exception as e:
        logger.error("zulip_delete_message failed", exc_info=True)
        return f"[Error]: Zulip message deletion failed: {e}"


CHAT_PLATFORM_SERVICE_TOOLS = [
    telegram_get_me,
    telegram_get_chat,
    telegram_send_message,
    telegram_delete_message,
    webex_list_rooms,
    webex_get_room,
    webex_list_messages,
    webex_get_message,
    webex_send_message,
    webex_delete_message,
    whatsapp_list_phone_numbers,
    whatsapp_send_text_message,
    whatsapp_send_template_message,
    whatsapp_get_media_url,
    whatsapp_delete_media,
    discord_list_guild_channels,
    discord_get_channel,
    discord_get_channel_messages,
    discord_send_channel_message,
    discord_delete_message,
    mattermost_get_me,
    mattermost_list_teams,
    mattermost_list_channels,
    mattermost_list_channel_posts,
    mattermost_create_post,
    mattermost_delete_post,
    matrix_whoami,
    matrix_list_joined_rooms,
    matrix_get_room_messages,
    matrix_send_room_message,
    matrix_leave_room,
    rocketchat_get_me,
    rocketchat_list_channels,
    rocketchat_get_channel_history,
    rocketchat_post_message,
    rocketchat_delete_message,
    zulip_get_profile,
    zulip_list_streams,
    zulip_get_messages,
    zulip_send_message,
    zulip_delete_message,
]
