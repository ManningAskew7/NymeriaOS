"""Chat and community platform service tools."""

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
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    parse_json as _parse_json,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_DISCORD_BASE_URL = "https://discord.com/api/v10"
_TELEGRAM_BASE_URL = "https://api.telegram.org"
_WHATSAPP_BASE_URL = "https://graph.facebook.com/v19.0"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered. The shared
# _api_token_config helper keeps its generic base-URL field-name tuple inline
# (and Discord relies on its default token tuple), so the spec still declares
# those tuples as groups even where no call site reads them back.
_TELEGRAM = register_provider_spec(
    ProviderCredentialSpec(
        provider="telegram",
        aliases=("telegram_bot", "telegram_api", "telegramApi"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "homeserverUrl", "domain", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token", names=("bot_token", "botToken", "api_key", "apiKey", "token", "value")
            ),
        ),
        hint_fields=("bot_token", "botToken", "api_key", "apiKey", "token", "value"),
        env_var="TELEGRAM_BOT_TOKEN",
        display_name="Telegram",
    )
)


_WHATSAPP = register_provider_spec(
    ProviderCredentialSpec(
        provider="whatsapp",
        aliases=("whats_app", "whatsapp_business", "whatsapp_api", "whatsAppApi"),
        groups=(
            CredentialFieldGroup(
                role="access_token",
                names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="business_account_id",
                names=("business_account_id", "businessAccountId", "account_id", "accountId"),
                required=False,
            ),
            CredentialFieldGroup(
                role="phone_number_id",
                names=(
                    "phone_number_id",
                    "phoneNumberId",
                    "sender_phone_number_id",
                    "senderPhoneNumberId",
                ),
                required=False,
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="WHATSAPP_ACCESS_TOKEN",
        display_name="WhatsApp Business Cloud",
    )
)

_DISCORD = register_provider_spec(
    ProviderCredentialSpec(
        provider="discord",
        aliases=("discord_bot", "discordBotApi", "discord_bot_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "homeserverUrl", "domain", "url", "api_url", "apiUrl"),
                required=False,
            ),
            # Discord uses the _api_token_config default token tuple (no
            # field_names at the call site); declared here so the registry
            # mirrors the lookup.
            CredentialFieldGroup(
                role="token",
                names=(
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
            ),
        ),
        hint_fields=(
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
        env_var="DISCORD_BOT_TOKEN",
        display_name="Discord",
    )
)


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
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(
                client,
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
    base_from_vault = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=("base_url", "baseUrl", "homeserverUrl", "domain", "url", "api_url", "apiUrl"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value(settings_base_name) or default_base
    token_from_vault = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    )
    token = token_from_vault or _settings_value(settings_key_name)
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=token_from_vault,
        secret=token,
        provider=provider,
    )
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


def _telegram_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    base_url, token = _api_token_config(
        provider=_TELEGRAM.provider,
        provider_aliases=_TELEGRAM.aliases,
        env_var=_TELEGRAM.env_var,
        settings_key_name="telegram_bot_token",
        settings_base_name="telegram_api_base_url",
        default_base=_TELEGRAM_BASE_URL,
        tool_name=tool_name,
        display_name=_TELEGRAM.display_name,
        config=config,
        field_names=_TELEGRAM.group("token"),
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    return base_url, token


def _telegram_url(base_url: str, token: str, method: str) -> str:
    return f"{base_url}/bot{token}/{method}"


def _whatsapp_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[dict[str, Any], str | None]:
    access_token = _credential_value(
        provider=_WHATSAPP.provider,
        provider_aliases=_WHATSAPP.aliases,
        field_names=_WHATSAPP.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("whatsapp_access_token")
    business_account_id = _credential_value(
        provider=_WHATSAPP.provider,
        provider_aliases=_WHATSAPP.aliases,
        field_names=_WHATSAPP.group("business_account_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("whatsapp_business_account_id")
    phone_number_id = _credential_value(
        provider=_WHATSAPP.provider,
        provider_aliases=_WHATSAPP.aliases,
        field_names=_WHATSAPP.group("phone_number_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("whatsapp_phone_number_id")
    base_url = (
        _credential_value(
            provider=_WHATSAPP.provider,
            provider_aliases=_WHATSAPP.aliases,
            field_names=_WHATSAPP.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("whatsapp_base_url")
        or _WHATSAPP_BASE_URL
    )
    if not access_token:
        return {}, _setup_hint(
            provider=_WHATSAPP.provider,
            field_names=_WHATSAPP.hint_fields,
            tool_name=tool_name,
            env_var=_WHATSAPP.env_var,
            display_name=_WHATSAPP.display_name,
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
        provider=_DISCORD.provider,
        provider_aliases=_DISCORD.aliases,
        env_var=_DISCORD.env_var,
        settings_key_name="discord_bot_token",
        settings_base_name="discord_base_url",
        default_base=_DISCORD_BASE_URL,
        tool_name=tool_name,
        display_name=_DISCORD.display_name,
        config=config,
    )
    if not token or token.startswith("[Error]:"):
        return base_url, token or ""
    headers = _json_headers()
    headers["Authorization"] = token if token.lower().startswith("bot ") else f"Bot {token}"
    return base_url, headers


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


CHAT_PLATFORM_SERVICE_TOOLS = [
    telegram_get_me,
    telegram_get_chat,
    telegram_send_message,
    telegram_delete_message,
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
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="chat_platform", tools=tuple(CHAT_PLATFORM_SERVICE_TOOLS)))
