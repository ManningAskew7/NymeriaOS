"""Messaging and transactional email service integration tools."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
    base_url as _base_url,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered_params,
    parse_json as _parse_json,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_TWILIO_BASE_URL = "https://api.twilio.com/2010-04-01"
_SENDGRID_BASE_URL = "https://api.sendgrid.com/v3"
_MAILGUN_BASE_URL = "https://api.mailgun.net/v3"
_BREVO_BASE_URL = "https://api.brevo.com/v3"
_MAILJET_BASE_URL = "https://api.mailjet.com"
_MANDRILL_BASE_URL = "https://mandrillapp.com/api/1.0"
_MESSAGEBIRD_BASE_URL = "https://rest.messagebird.com"
_MOCEAN_BASE_URL = "https://rest.moceanapi.com"
_MSG91_BASE_URL = "https://api.msg91.com/api"
_PLIVO_BASE_URL = "https://api.plivo.com/v1"
_VONAGE_BASE_URL = "https://rest.nexmo.com"
_SEVEN_BASE_URL = "https://gateway.seven.io/api"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _limit(value: int, *, default: int = 25, max_value: int = 1000) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


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
                params=_filtered_params(params) if params is not None else None,
                json=json_body,
                data=_filtered_params(form_data) if form_data is not None else None,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except ValueError:
                return {
                    "status": "ok",
                    "status_code": response.status_code,
                    "text": response.text,
                }
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                detail = "; ".join(
                    str(item.get("message", item)) if isinstance(item, dict) else str(item)
                    for item in errors
                )
            detail = (
                detail
                or body.get("message")
                or body.get("error")
                or body.get("error_description")
                or body.get("detail")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _twilio_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str] | str]:
    account_sid = _credential_value(
        provider="twilio",
        provider_aliases=("twilio_api",),
        field_names=("account_sid", "accountSid", "sid"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("twilio_account_sid")
    auth_token = _credential_value(
        provider="twilio",
        provider_aliases=("twilio_api",),
        field_names=("auth_token", "authToken", "api_key_secret", "apiKeySecret", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("twilio_auth_token")
    api_key_sid = _credential_value(
        provider="twilio",
        provider_aliases=("twilio_api",),
        field_names=("api_key_sid", "apiKeySid"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("twilio_api_key_sid")
    base = (
        _credential_value(
            provider="twilio",
            provider_aliases=("twilio_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("twilio_base_url")
        or _TWILIO_BASE_URL
    )
    if not account_sid:
        return _base_url(base), "", (
            "[Error]: No Twilio account SID found. Save a Twilio credential with "
            '"account_sid", or set TWILIO_ACCOUNT_SID.'
        )
    if not auth_token:
        return _base_url(base), account_sid, _setup_hint(
            provider="twilio",
            field_names=("auth_token", "api_key_secret", "token", "value"),
            tool_name=tool_name,
            env_var="TWILIO_AUTH_TOKEN",
            display_name="Twilio",
        )
    username = api_key_sid or account_sid
    auth = base64.b64encode(f"{username}:{auth_token}".encode()).decode()
    return _base_url(base), account_sid, {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Nymeria",
    }


def _sendgrid_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="sendgrid",
            provider_aliases=("sendgrid_api", "twilio_sendgrid"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("sendgrid_base_url")
        or _SENDGRID_BASE_URL
    )
    api_key = _credential_value(
        provider="sendgrid",
        provider_aliases=("sendgrid_api", "twilio_sendgrid"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("sendgrid_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="sendgrid",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="SENDGRID_API_KEY",
            display_name="SendGrid",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _mailgun_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="mailgun",
            provider_aliases=("mailgun_api",),
            field_names=("base_url", "api_domain", "apiDomain", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailgun_base_url")
        or _MAILGUN_BASE_URL
    )
    domain = _credential_value(
        provider="mailgun",
        provider_aliases=("mailgun_api",),
        field_names=("domain", "email_domain", "emailDomain"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailgun_domain")
    api_key = _credential_value(
        provider="mailgun",
        provider_aliases=("mailgun_api",),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailgun_api_key")
    if not domain:
        return _base_url(base), "", (
            "[Error]: No Mailgun domain found. Save a Mailgun credential with "
            '"domain" or "email_domain", or set MAILGUN_DOMAIN.'
        )
    if not api_key:
        return _base_url(base), domain, _setup_hint(
            provider="mailgun",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="MAILGUN_API_KEY",
            display_name="Mailgun",
        )
    auth = base64.b64encode(f"api:{api_key}".encode()).decode()
    return _base_url(base), domain, {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        "User-Agent": "Nymeria",
    }


def _email_objects(value: str, *, key: str = "email") -> list[dict[str, str]]:
    return [{key: email} for email in _split_csv(value)]


def _brevo_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="brevo",
            provider_aliases=("brevo_api", "sendinblue", "send_in_blue", "sendInBlueApi"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("brevo_base_url")
        or _BREVO_BASE_URL
    )
    api_key = _credential_value(
        provider="brevo",
        provider_aliases=("brevo_api", "sendinblue", "send_in_blue", "sendInBlueApi"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("brevo_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="brevo",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="BREVO_API_KEY",
            display_name="Brevo",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "api-key": api_key,
    }


def _mailjet_email_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="mailjet",
            provider_aliases=("mailjet_email", "mailjet_email_api"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailjet_base_url")
        or _MAILJET_BASE_URL
    )
    api_key = _credential_value(
        provider="mailjet",
        provider_aliases=("mailjet_email", "mailjet_email_api"),
        field_names=("api_key", "apiKey", "public_key", "publicKey", "username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailjet_api_key")
    secret_key = _credential_value(
        provider="mailjet",
        provider_aliases=("mailjet_email", "mailjet_email_api"),
        field_names=("secret_key", "secretKey", "api_secret", "apiSecret", "private_key", "privateKey", "password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailjet_secret_key")
    if not api_key or not secret_key:
        return _base_url(base), _setup_hint(
            provider="mailjet",
            field_names=("api_key", "secret_key"),
            tool_name=tool_name,
            env_var="MAILJET_API_KEY + MAILJET_SECRET_KEY",
            display_name="Mailjet",
        )
    auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _mailjet_sms_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="mailjet",
            provider_aliases=("mailjet_sms", "mailjet_sms_api"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailjet_base_url")
        or _MAILJET_BASE_URL
    )
    token = _credential_value(
        provider="mailjet",
        provider_aliases=("mailjet_sms", "mailjet_sms_api"),
        field_names=("sms_token", "token", "api_key", "apiKey", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailjet_sms_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="mailjet",
            field_names=("sms_token", "token", "value"),
            tool_name=tool_name,
            env_var="MAILJET_SMS_TOKEN",
            display_name="Mailjet SMS",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _mandrill_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None, str | None]:
    base = (
        _credential_value(
            provider="mandrill",
            provider_aliases=("mandrill_api", "mailchimp_transactional"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mandrill_base_url")
        or _MANDRILL_BASE_URL
    )
    api_key = _credential_value(
        provider="mandrill",
        provider_aliases=("mandrill_api", "mailchimp_transactional"),
        field_names=("api_key", "apiKey", "key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mandrill_api_key")
    if not api_key:
        return _base_url(base), None, _setup_hint(
            provider="mandrill",
            field_names=("api_key", "key", "token", "value"),
            tool_name=tool_name,
            env_var="MANDRILL_API_KEY",
            display_name="Mandrill",
        )
    return _base_url(base), api_key, None


def _messagebird_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="messagebird",
            provider_aliases=("message_bird", "messagebird_api"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("messagebird_base_url")
        or _MESSAGEBIRD_BASE_URL
    )
    access_key = _credential_value(
        provider="messagebird",
        provider_aliases=("message_bird", "messagebird_api"),
        field_names=("access_key", "accessKey", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("messagebird_access_key")
    if not access_key:
        return _base_url(base), _setup_hint(
            provider="messagebird",
            field_names=("access_key", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="MESSAGEBIRD_ACCESS_KEY",
            display_name="MessageBird",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"AccessKey {access_key}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _mocean_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, str, str | None]:
    base = (
        _credential_value(
            provider="mocean",
            provider_aliases=("mocean_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mocean_base_url")
        or _MOCEAN_BASE_URL
    )
    api_key = _credential_value(
        provider="mocean",
        provider_aliases=("mocean_api",),
        field_names=("api_key", "apiKey", "mocean-api-key", "key", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mocean_api_key")
    api_secret = _credential_value(
        provider="mocean",
        provider_aliases=("mocean_api",),
        field_names=("api_secret", "apiSecret", "mocean-api-secret", "secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mocean_api_secret")
    if not api_key or not api_secret:
        return _base_url(base), "", "", _setup_hint(
            provider="mocean",
            field_names=("api_key", "api_secret"),
            tool_name=tool_name,
            env_var="MOCEAN_API_KEY + MOCEAN_API_SECRET",
            display_name="Mocean",
        )
    return _base_url(base), api_key, api_secret, None


def _msg91_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None, str | None]:
    base = (
        _credential_value(
            provider="msg91",
            provider_aliases=("msg91_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("msg91_base_url")
        or _MSG91_BASE_URL
    )
    auth_key = _credential_value(
        provider="msg91",
        provider_aliases=("msg91_api",),
        field_names=("auth_key", "authkey", "api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("msg91_auth_key")
    if not auth_key:
        return _base_url(base), None, _setup_hint(
            provider="msg91",
            field_names=("auth_key", "authkey", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="MSG91_AUTH_KEY",
            display_name="MSG91",
        )
    return _base_url(base), auth_key, None


def _plivo_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str] | str]:
    auth_id = _credential_value(
        provider="plivo",
        provider_aliases=("plivo_api",),
        field_names=("auth_id", "authId", "account_id", "accountId", "username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("plivo_auth_id")
    auth_token = _credential_value(
        provider="plivo",
        provider_aliases=("plivo_api",),
        field_names=("auth_token", "authToken", "api_secret", "apiSecret", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("plivo_auth_token")
    base = (
        _credential_value(
            provider="plivo",
            provider_aliases=("plivo_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("plivo_base_url")
        or _PLIVO_BASE_URL
    )
    if not auth_id or not auth_token:
        return _base_url(base), "", _setup_hint(
            provider="plivo",
            field_names=("auth_id", "auth_token"),
            tool_name=tool_name,
            env_var="PLIVO_AUTH_ID + PLIVO_AUTH_TOKEN",
            display_name="Plivo",
        )
    token = base64.b64encode(f"{auth_id}:{auth_token}".encode("utf-8")).decode("ascii")
    return _base_url(base), auth_id, {
        "Accept": "application/json",
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _vonage_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, str, str | None]:
    base = (
        _credential_value(
            provider="vonage",
            provider_aliases=("vonage_api", "nexmo", "nexmo_api"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("vonage_base_url")
        or _VONAGE_BASE_URL
    )
    api_key = _credential_value(
        provider="vonage",
        provider_aliases=("vonage_api", "nexmo", "nexmo_api"),
        field_names=("api_key", "apiKey", "key", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("vonage_api_key")
    api_secret = _credential_value(
        provider="vonage",
        provider_aliases=("vonage_api", "nexmo", "nexmo_api"),
        field_names=("api_secret", "apiSecret", "secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("vonage_api_secret")
    if not api_key or not api_secret:
        return _base_url(base), "", "", _setup_hint(
            provider="vonage",
            field_names=("api_key", "api_secret"),
            tool_name=tool_name,
            env_var="VONAGE_API_KEY + VONAGE_API_SECRET",
            display_name="Vonage",
        )
    return _base_url(base), api_key, api_secret, None


def _seven_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="seven",
            provider_aliases=("seven_io", "seven_api", "sms77"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("seven_base_url")
        or _SEVEN_BASE_URL
    )
    api_key = _credential_value(
        provider="seven",
        provider_aliases=("seven_io", "seven_api", "sms77"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("seven_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="seven",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="SEVEN_API_KEY",
            display_name="seven.io",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Nymeria",
        "X-Api-Key": api_key,
    }


@tool
def twilio_send_message(
    from_number: str,
    to_number: str,
    body: str,
    messaging_service_sid: str = "",
    media_urls: str = "",
    status_callback: str = "",
    whatsapp: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS/MMS/WhatsApp message with Twilio.

    Args:
        from_number: Sender phone number, sender ID, or WhatsApp sender.
        to_number: Recipient phone number or WhatsApp address.
        body: Message body.
        messaging_service_sid: Optional Twilio Messaging Service SID.
        media_urls: Optional comma-separated media URLs for MMS/WhatsApp.
        status_callback: Optional webhook URL for delivery status callbacks.
        whatsapp: Prefix From/To with whatsapp: when not already present.
    """
    if not to_number.strip() or not body.strip() or (not from_number.strip() and not messaging_service_sid.strip()):
        return "[Error]: to_number, body, and from_number or messaging_service_sid are required."
    try:
        base_url, account_sid, headers_or_error = _twilio_config("twilio_send_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        sender = from_number.strip()
        recipient = to_number.strip()
        if whatsapp:
            if sender and not sender.startswith("whatsapp:"):
                sender = f"whatsapp:{sender}"
            if not recipient.startswith("whatsapp:"):
                recipient = f"whatsapp:{recipient}"
        form = _filtered_params(
            {
                "From": sender,
                "To": recipient,
                "Body": body,
                "MessagingServiceSid": messaging_service_sid.strip(),
                "StatusCallback": status_callback.strip(),
            }
        )
        media = _split_csv(media_urls)
        if media:
            form["MediaUrl"] = media
        data = _request_json(
            "POST",
            f"{base_url}/Accounts/{quote(account_sid, safe='')}/Messages.json",
            form_data=form,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("twilio_send_message failed", exc_info=True)
        return f"[Error]: Twilio message send failed: {e}"


@tool
def twilio_list_messages(
    to_number: str = "",
    from_number: str = "",
    date_sent: str = "",
    page_size: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Twilio messages.

    Args:
        to_number: Optional recipient filter.
        from_number: Optional sender filter.
        date_sent: Optional date filter in YYYY-MM-DD format.
        page_size: Number of messages to return, 1-1000.
    """
    try:
        base_url, account_sid, headers_or_error = _twilio_config("twilio_list_messages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/Accounts/{quote(account_sid, safe='')}/Messages.json",
            params={
                "To": to_number.strip(),
                "From": from_number.strip(),
                "DateSent": date_sent.strip(),
                "PageSize": _limit(page_size, default=20),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("messages", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("twilio_list_messages failed", exc_info=True)
        return f"[Error]: Twilio message list failed: {e}"


@tool
def twilio_get_message(
    message_sid: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Twilio message by SID.

    Args:
        message_sid: Twilio Message SID.
    """
    message_sid = message_sid.strip()
    if not message_sid:
        return "[Error]: message_sid is required."
    try:
        base_url, account_sid, headers_or_error = _twilio_config("twilio_get_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/Accounts/{quote(account_sid, safe='')}/Messages/{quote(message_sid, safe='')}.json",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("twilio_get_message failed", exc_info=True)
        return f"[Error]: Twilio message lookup failed: {e}"


@tool
def twilio_make_call(
    from_number: str,
    to_number: str,
    twiml: str = "",
    url: str = "",
    status_callback: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Start an outbound Twilio voice call.

    Args:
        from_number: Caller ID phone number.
        to_number: Recipient phone number.
        twiml: Optional TwiML instructions. Provide twiml or url.
        url: Optional URL returning TwiML. Provide twiml or url.
        status_callback: Optional webhook URL for call status callbacks.
    """
    if not from_number.strip() or not to_number.strip() or (not twiml.strip() and not url.strip()):
        return "[Error]: from_number, to_number, and twiml or url are required."
    try:
        base_url, account_sid, headers_or_error = _twilio_config("twilio_make_call", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/Accounts/{quote(account_sid, safe='')}/Calls.json",
            form_data={
                "From": from_number.strip(),
                "To": to_number.strip(),
                "Twiml": twiml,
                "Url": url.strip(),
                "StatusCallback": status_callback.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("twilio_make_call failed", exc_info=True)
        return f"[Error]: Twilio call creation failed: {e}"


@tool
def sendgrid_send_email(
    from_email: str,
    to_emails: str,
    subject: str = "",
    text: str = "",
    html: str = "",
    from_name: str = "",
    cc: str = "",
    bcc: str = "",
    reply_to: str = "",
    template_id: str = "",
    dynamic_template_data_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an email with SendGrid Mail Send.

    Args:
        from_email: Sender email address.
        to_emails: Comma-separated recipient email addresses.
        subject: Email subject. Required unless template_id supplies it.
        text: Optional plain text body.
        html: Optional HTML body.
        from_name: Optional sender display name.
        cc: Optional comma-separated CC addresses.
        bcc: Optional comma-separated BCC addresses.
        reply_to: Optional reply-to email address.
        template_id: Optional dynamic template ID.
        dynamic_template_data_json: Optional dynamic template data object as JSON.
    """
    recipients = _split_csv(to_emails)
    if not from_email.strip() or not recipients:
        return "[Error]: from_email and to_emails are required."
    if not template_id.strip() and not (subject.strip() and (text.strip() or html.strip())):
        return "[Error]: subject and text/html are required when template_id is not supplied."
    try:
        personalization: dict[str, Any] = {"to": [{"email": email} for email in recipients]}
        if cc.strip():
            personalization["cc"] = [{"email": email} for email in _split_csv(cc)]
        if bcc.strip():
            personalization["bcc"] = [{"email": email} for email in _split_csv(bcc)]
        dynamic_data = _parse_json(
            dynamic_template_data_json,
            expected=dict,
            label="dynamic_template_data_json",
        )
        if dynamic_data:
            personalization["dynamic_template_data"] = dynamic_data
        body: dict[str, Any] = {
            "personalizations": [personalization],
            "from": _filtered_params({"email": from_email.strip(), "name": from_name.strip()}),
        }
        if reply_to.strip():
            body["reply_to"] = {"email": reply_to.strip()}
        if subject.strip():
            body["subject"] = subject.strip()
        if template_id.strip():
            body["template_id"] = template_id.strip()
        else:
            content = []
            if text.strip():
                content.append({"type": "text/plain", "value": text})
            if html.strip():
                content.append({"type": "text/html", "value": html})
            body["content"] = content
        base_url, headers_or_error = _sendgrid_config("sendgrid_send_email", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/mail/send", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("sendgrid_send_email failed", exc_info=True)
        return f"[Error]: SendGrid email send failed: {e}"


@tool
def sendgrid_list_contacts(
    query: str = "",
    page_size: int = 50,
    page_token: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List or search SendGrid marketing contacts.

    Args:
        query: Optional SendGrid marketing contact query. Uses contact search when supplied.
        page_size: Number of contacts to return, 1-100.
        page_token: Optional pagination token for list mode.
    """
    try:
        base_url, headers_or_error = _sendgrid_config("sendgrid_list_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if query.strip():
            data = _request_json(
                "POST",
                f"{base_url}/marketing/contacts/search",
                json_body={"query": query.strip()},
                headers=headers_or_error,
            )
        else:
            data = _request_json(
                "GET",
                f"{base_url}/marketing/contacts",
                params={"page_size": _limit(page_size, default=50, max_value=100), "page_token": page_token.strip()},
                headers=headers_or_error,
            )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("sendgrid_list_contacts failed", exc_info=True)
        return f"[Error]: SendGrid contact list failed: {e}"


@tool
def sendgrid_get_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a SendGrid marketing contact by ID.

    Args:
        contact_id: SendGrid contact ID.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _sendgrid_config("sendgrid_get_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/marketing/contacts/{quote(contact_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sendgrid_get_contact failed", exc_info=True)
        return f"[Error]: SendGrid contact lookup failed: {e}"


@tool
def sendgrid_upsert_contacts(
    contacts_json: str,
    list_ids: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create or update SendGrid marketing contacts.

    Args:
        contacts_json: JSON array of contact objects.
        list_ids: Optional comma-separated SendGrid list IDs to add contacts to.
    """
    if not contacts_json.strip():
        return "[Error]: contacts_json is required."
    try:
        contacts = _parse_json(contacts_json, expected=list, label="contacts_json")
        body = {"contacts": contacts}
        ids = _split_csv(list_ids)
        if ids:
            body["list_ids"] = ids
        base_url, headers_or_error = _sendgrid_config("sendgrid_upsert_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("PUT", f"{base_url}/marketing/contacts", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("sendgrid_upsert_contacts failed", exc_info=True)
        return f"[Error]: SendGrid contact upsert failed: {e}"


@tool
def sendgrid_list_lists(
    page_size: int = 50,
    page_token: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List SendGrid marketing contact lists.

    Args:
        page_size: Number of lists to return, 1-100.
        page_token: Optional pagination token.
    """
    try:
        base_url, headers_or_error = _sendgrid_config("sendgrid_list_lists", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/marketing/lists",
            params={"page_size": _limit(page_size, default=50, max_value=100), "page_token": page_token.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("sendgrid_list_lists failed", exc_info=True)
        return f"[Error]: SendGrid list lookup failed: {e}"


@tool
def mailgun_send_email(
    from_email: str,
    to_emails: str,
    subject: str,
    text: str = "",
    html: str = "",
    cc: str = "",
    bcc: str = "",
    extra_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an email with Mailgun.

    Args:
        from_email: Sender email address, optionally with display name.
        to_emails: Comma-separated recipient email addresses.
        subject: Email subject.
        text: Optional plain text body.
        html: Optional HTML body.
        cc: Optional comma-separated CC addresses.
        bcc: Optional comma-separated BCC addresses.
        extra_fields_json: Optional extra Mailgun form fields as JSON.
    """
    if not from_email.strip() or not _split_csv(to_emails) or not subject.strip() or not (text.strip() or html.strip()):
        return "[Error]: from_email, to_emails, subject, and text or html are required."
    try:
        base_url, domain, headers_or_error = _mailgun_config("mailgun_send_email", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        form = _filtered_params(
            {
                "from": from_email.strip(),
                "to": ",".join(_split_csv(to_emails)),
                "cc": ",".join(_split_csv(cc)),
                "bcc": ",".join(_split_csv(bcc)),
                "subject": subject.strip(),
                "text": text,
                "html": html,
                **_parse_json(extra_fields_json, expected=dict, label="extra_fields_json"),
            }
        )
        data = _request_json(
            "POST",
            f"{base_url}/{quote(domain, safe='')}/messages",
            form_data=form,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mailgun_send_email failed", exc_info=True)
        return f"[Error]: Mailgun email send failed: {e}"


@tool
def mailgun_list_events(
    event: str = "",
    begin: str = "",
    end: str = "",
    ascending: bool = False,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mailgun events for the configured sending domain.

    Args:
        event: Optional event type filter, such as delivered, failed, opened, or clicked.
        begin: Optional begin date/time accepted by Mailgun.
        end: Optional end date/time accepted by Mailgun.
        ascending: Return oldest first.
        limit: Number of events to return, 1-300.
    """
    try:
        base_url, domain, headers_or_error = _mailgun_config("mailgun_list_events", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{quote(domain, safe='')}/events",
            params={
                "event": event.strip(),
                "begin": begin.strip(),
                "end": end.strip(),
                "ascending": "yes" if ascending else "no",
                "limit": _limit(limit, default=25, max_value=300),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailgun_list_events failed", exc_info=True)
        return f"[Error]: Mailgun event list failed: {e}"


@tool
def mailgun_get_domain(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Mailgun sending domain metadata for the configured domain."""
    try:
        base_url, domain, headers_or_error = _mailgun_config("mailgun_get_domain", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/domains/{quote(domain, safe='')}", headers=headers_or_error)
        return _dump_json(data.get("domain", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailgun_get_domain failed", exc_info=True)
        return f"[Error]: Mailgun domain lookup failed: {e}"


@tool
def brevo_send_email(
    from_email: str,
    to_emails: str,
    subject: str = "",
    text: str = "",
    html: str = "",
    from_name: str = "",
    cc: str = "",
    bcc: str = "",
    reply_to: str = "",
    template_id: int = 0,
    params_json: str = "",
    tags: str = "",
    headers_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a transactional email with Brevo.

    Args:
        from_email: Sender email address.
        to_emails: Comma-separated recipient email addresses.
        subject: Email subject.
        text: Optional plain text body.
        html: Optional HTML body.
        from_name: Optional sender display name.
        cc: Optional comma-separated CC addresses.
        bcc: Optional comma-separated BCC addresses.
        reply_to: Optional reply-to email address.
        template_id: Optional Brevo template ID. When supplied, body content is optional.
        params_json: Optional template/dynamic parameters as a JSON object.
        tags: Optional comma-separated Brevo tags.
        headers_json: Optional custom headers as a JSON object.
    """
    recipients = _split_csv(to_emails)
    if not from_email.strip() or not recipients:
        return "[Error]: from_email and to_emails are required."
    if not template_id and not (subject.strip() and (text.strip() or html.strip())):
        return "[Error]: subject and text/html are required when template_id is not supplied."
    try:
        body: dict[str, Any] = {
            "sender": _filtered_params({"email": from_email.strip(), "name": from_name.strip()}),
            "to": _email_objects(to_emails),
        }
        if subject.strip():
            body["subject"] = subject.strip()
        if template_id:
            body["templateId"] = int(template_id)
        else:
            if text.strip():
                body["textContent"] = text
            if html.strip():
                body["htmlContent"] = html
        if cc.strip():
            body["cc"] = _email_objects(cc)
        if bcc.strip():
            body["bcc"] = _email_objects(bcc)
        if reply_to.strip():
            body["replyTo"] = {"email": reply_to.strip()}
        params = _parse_json(params_json, expected=dict, label="params_json")
        if params:
            body["params"] = params
        tag_list = _split_csv(tags)
        if tag_list:
            body["tags"] = tag_list
        headers = _parse_json(headers_json, expected=dict, label="headers_json")
        if headers:
            body["headers"] = headers
        base_url, headers_or_error = _brevo_config("brevo_send_email", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/smtp/email", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("brevo_send_email failed", exc_info=True)
        return f"[Error]: Brevo email send failed: {e}"


@tool
def brevo_list_contacts(
    limit: int = 50,
    offset: int = 0,
    modified_since: str = "",
    sort: str = "desc",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Brevo contacts.

    Args:
        limit: Number of contacts to return, 1-500.
        offset: Pagination offset.
        modified_since: Optional modifiedSince filter accepted by Brevo.
        sort: Sort order, asc or desc.
    """
    try:
        base_url, headers_or_error = _brevo_config("brevo_list_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/contacts",
            params={
                "limit": _limit(limit, default=50, max_value=500),
                "offset": max(0, int(offset)),
                "modifiedSince": modified_since.strip(),
                "sort": sort.strip() if sort.strip() in {"asc", "desc"} else "desc",
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("contacts", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("brevo_list_contacts failed", exc_info=True)
        return f"[Error]: Brevo contact list failed: {e}"


@tool
def brevo_get_contact(
    identifier: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Brevo contact by email, ID, SMS attribute, or external ID.

    Args:
        identifier: Contact email, ID, SMS value, or external ID.
    """
    identifier = identifier.strip()
    if not identifier:
        return "[Error]: identifier is required."
    try:
        base_url, headers_or_error = _brevo_config("brevo_get_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/contacts/{quote(identifier, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("brevo_get_contact failed", exc_info=True)
        return f"[Error]: Brevo contact lookup failed: {e}"


@tool
def brevo_create_contact(
    email: str = "",
    attributes_json: str = "",
    list_ids: str = "",
    update_enabled: bool = False,
    sms: str = "",
    ext_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Brevo contact.

    Args:
        email: Contact email address. Required unless sms or ext_id identifies the contact.
        attributes_json: Optional attributes object as JSON.
        list_ids: Optional comma-separated numeric list IDs.
        update_enabled: Update the contact if it already exists.
        sms: Optional SMS attribute value.
        ext_id: Optional external ID.
    """
    if not email.strip() and not sms.strip() and not ext_id.strip():
        return "[Error]: email, sms, or ext_id is required."
    try:
        attributes = _parse_json(attributes_json, expected=dict, label="attributes_json")
        if sms.strip():
            attributes["SMS"] = sms.strip()
        body: dict[str, Any] = _filtered_params(
            {
                "email": email.strip(),
                "attributes": attributes,
                "updateEnabled": update_enabled,
                "ext_id": ext_id.strip(),
            }
        )
        ids = [int(item) for item in _split_csv(list_ids)]
        if ids:
            body["listIds"] = ids
        base_url, headers_or_error = _brevo_config("brevo_create_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/contacts", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("brevo_create_contact failed", exc_info=True)
        return f"[Error]: Brevo contact create failed: {e}"


@tool
def brevo_update_contact(
    identifier: str,
    attributes_json: str = "",
    list_ids: str = "",
    unlink_list_ids: str = "",
    email_blacklisted: bool = False,
    sms_blacklisted: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Brevo contact.

    Args:
        identifier: Contact email, ID, SMS value, or external ID.
        attributes_json: Optional attributes object as JSON.
        list_ids: Optional comma-separated numeric list IDs to add.
        unlink_list_ids: Optional comma-separated numeric list IDs to remove.
        email_blacklisted: Whether to blacklist the contact from email.
        sms_blacklisted: Whether to blacklist the contact from SMS.
    """
    identifier = identifier.strip()
    if not identifier:
        return "[Error]: identifier is required."
    try:
        body: dict[str, Any] = {}
        attributes = _parse_json(attributes_json, expected=dict, label="attributes_json")
        if attributes:
            body["attributes"] = attributes
        ids = [int(item) for item in _split_csv(list_ids)]
        if ids:
            body["listIds"] = ids
        unlink_ids = [int(item) for item in _split_csv(unlink_list_ids)]
        if unlink_ids:
            body["unlinkListIds"] = unlink_ids
        if email_blacklisted:
            body["emailBlacklisted"] = True
        if sms_blacklisted:
            body["smsBlacklisted"] = True
        if not body:
            return "[Error]: Provide attributes_json, list_ids, unlink_list_ids, or a blacklist flag."
        base_url, headers_or_error = _brevo_config("brevo_update_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/contacts/{quote(identifier, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("brevo_update_contact failed", exc_info=True)
        return f"[Error]: Brevo contact update failed: {e}"


@tool
def brevo_list_senders(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Brevo senders available for transactional email."""
    try:
        base_url, headers_or_error = _brevo_config("brevo_list_senders", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/senders", headers=headers_or_error)
        return _dump_json(data.get("senders", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("brevo_list_senders failed", exc_info=True)
        return f"[Error]: Brevo sender list failed: {e}"


@tool
def mailjet_send_email(
    from_email: str,
    to_emails: str,
    subject: str = "",
    text: str = "",
    html: str = "",
    from_name: str = "",
    cc: str = "",
    bcc: str = "",
    reply_to: str = "",
    template_id: int = 0,
    variables_json: str = "",
    sandbox_mode: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an email with Mailjet.

    Args:
        from_email: Sender email address.
        to_emails: Comma-separated recipient email addresses.
        subject: Email subject.
        text: Optional plain text body.
        html: Optional HTML body.
        from_name: Optional sender display name.
        cc: Optional comma-separated CC addresses.
        bcc: Optional comma-separated BCC addresses.
        reply_to: Optional reply-to email address.
        template_id: Optional Mailjet template ID.
        variables_json: Optional template variables as a JSON object.
        sandbox_mode: Validate without delivering the message.
    """
    if not from_email.strip() or not _split_csv(to_emails):
        return "[Error]: from_email and to_emails are required."
    if not template_id and not (subject.strip() and (text.strip() or html.strip())):
        return "[Error]: subject and text/html are required when template_id is not supplied."
    try:
        message: dict[str, Any] = {
            "From": _filtered_params({"Email": from_email.strip(), "Name": from_name.strip()}),
            "To": _email_objects(to_emails, key="Email"),
        }
        if subject.strip():
            message["Subject"] = subject.strip()
        if text.strip():
            message["TextPart"] = text
        if html.strip():
            message["HTMLPart"] = html
        if cc.strip():
            message["Cc"] = _email_objects(cc, key="Email")
        if bcc.strip():
            message["Bcc"] = _email_objects(bcc, key="Email")
        if reply_to.strip():
            message["ReplyTo"] = {"Email": reply_to.strip()}
        if template_id:
            message["TemplateID"] = int(template_id)
            message["TemplateLanguage"] = True
        variables = _parse_json(variables_json, expected=dict, label="variables_json")
        if variables:
            message["Variables"] = variables
        body: dict[str, Any] = {"Messages": [message]}
        if sandbox_mode:
            body["SandboxMode"] = True
        base_url, headers_or_error = _mailjet_email_config("mailjet_send_email", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/v3.1/send", json_body=body, headers=headers_or_error)
        return _dump_json(data.get("Messages", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailjet_send_email failed", exc_info=True)
        return f"[Error]: Mailjet email send failed: {e}"


@tool
def mailjet_send_sms(
    from_name: str,
    to_number: str,
    text: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS with Mailjet.

    Args:
        from_name: Sender name or number.
        to_number: Recipient phone number in international format.
        text: SMS text.
    """
    if not from_name.strip() or not to_number.strip() or not text.strip():
        return "[Error]: from_name, to_number, and text are required."
    try:
        base_url, headers_or_error = _mailjet_sms_config("mailjet_send_sms", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/v4/sms-send",
            json_body={"From": from_name.strip(), "To": to_number.strip(), "Text": text},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mailjet_send_sms failed", exc_info=True)
        return f"[Error]: Mailjet SMS send failed: {e}"


@tool
def mailjet_list_contacts(
    limit: int = 50,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mailjet contacts.

    Args:
        limit: Number of contacts to return, 1-1000.
        offset: Pagination offset.
    """
    try:
        base_url, headers_or_error = _mailjet_email_config("mailjet_list_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/v3/REST/contact",
            params={"Limit": _limit(limit, default=50), "Offset": max(0, int(offset))},
            headers=headers_or_error,
        )
        return _dump_json(data.get("Data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailjet_list_contacts failed", exc_info=True)
        return f"[Error]: Mailjet contact list failed: {e}"


@tool
def mailjet_get_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Mailjet contact by ID or email.

    Args:
        contact_id: Mailjet contact ID or email address.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _mailjet_email_config("mailjet_get_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/v3/REST/contact/{quote(contact_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("Data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailjet_get_contact failed", exc_info=True)
        return f"[Error]: Mailjet contact lookup failed: {e}"


def _mandrill_message(
    *,
    from_email: str,
    to_emails: str,
    subject: str,
    text: str,
    html: str,
    from_name: str,
    cc: str,
    bcc: str,
    tags: str,
    metadata_json: str,
    headers_json: str,
) -> dict[str, Any]:
    recipients = [{"email": email, "type": "to"} for email in _split_csv(to_emails)]
    recipients.extend({"email": email, "type": "cc"} for email in _split_csv(cc))
    recipients.extend({"email": email, "type": "bcc"} for email in _split_csv(bcc))
    message: dict[str, Any] = _filtered_params(
        {
            "from_email": from_email.strip(),
            "from_name": from_name.strip(),
            "subject": subject.strip(),
            "text": text,
            "html": html,
        }
    )
    message["to"] = recipients
    tag_list = _split_csv(tags)
    if tag_list:
        message["tags"] = tag_list
    metadata = _parse_json(metadata_json, expected=dict, label="metadata_json")
    if metadata:
        message["metadata"] = metadata
    headers = _parse_json(headers_json, expected=dict, label="headers_json")
    if headers:
        message["headers"] = headers
    return message


@tool
def mandrill_send_email(
    from_email: str,
    to_emails: str,
    subject: str,
    text: str = "",
    html: str = "",
    from_name: str = "",
    cc: str = "",
    bcc: str = "",
    tags: str = "",
    metadata_json: str = "",
    headers_json: str = "",
    async_send: bool = False,
    send_at: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an email with Mandrill / Mailchimp Transactional.

    Args:
        from_email: Sender email address.
        to_emails: Comma-separated recipient email addresses.
        subject: Email subject.
        text: Optional plain text body.
        html: Optional HTML body.
        from_name: Optional sender display name.
        cc: Optional comma-separated CC addresses.
        bcc: Optional comma-separated BCC addresses.
        tags: Optional comma-separated Mandrill tags.
        metadata_json: Optional metadata object as JSON.
        headers_json: Optional custom headers as JSON.
        async_send: Whether to send asynchronously.
        send_at: Optional UTC send time in YYYY-MM-DD HH:MM:SS.
    """
    if not from_email.strip() or not _split_csv(to_emails) or not subject.strip() or not (text.strip() or html.strip()):
        return "[Error]: from_email, to_emails, subject, and text or html are required."
    try:
        base_url, api_key, error = _mandrill_config("mandrill_send_email", config)
        if error:
            return error
        body: dict[str, Any] = {
            "key": api_key,
            "message": _mandrill_message(
                from_email=from_email,
                to_emails=to_emails,
                subject=subject,
                text=text,
                html=html,
                from_name=from_name,
                cc=cc,
                bcc=bcc,
                tags=tags,
                metadata_json=metadata_json,
                headers_json=headers_json,
            ),
            "async": async_send,
        }
        if send_at.strip():
            body["send_at"] = send_at.strip()
        data = _request_json("POST", f"{base_url}/messages/send.json", json_body=body)
        return _dump_json(data)
    except Exception as e:
        logger.error("mandrill_send_email failed", exc_info=True)
        return f"[Error]: Mandrill email send failed: {e}"


@tool
def mandrill_send_template(
    template_name: str,
    from_email: str,
    to_emails: str,
    subject: str = "",
    merge_vars_json: str = "",
    from_name: str = "",
    tags: str = "",
    async_send: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an email with a Mandrill template.

    Args:
        template_name: Mandrill template slug/name.
        from_email: Sender email address.
        to_emails: Comma-separated recipient email addresses.
        subject: Optional email subject.
        merge_vars_json: Optional global merge vars array as JSON.
        from_name: Optional sender display name.
        tags: Optional comma-separated Mandrill tags.
        async_send: Whether to send asynchronously.
    """
    if not template_name.strip() or not from_email.strip() or not _split_csv(to_emails):
        return "[Error]: template_name, from_email, and to_emails are required."
    try:
        base_url, api_key, error = _mandrill_config("mandrill_send_template", config)
        if error:
            return error
        message = _mandrill_message(
            from_email=from_email,
            to_emails=to_emails,
            subject=subject,
            text="",
            html="",
            from_name=from_name,
            cc="",
            bcc="",
            tags=tags,
            metadata_json="",
            headers_json="",
        )
        merge_vars = _parse_json(merge_vars_json, expected=list, label="merge_vars_json")
        if merge_vars:
            message["global_merge_vars"] = merge_vars
        body = {
            "key": api_key,
            "template_name": template_name.strip(),
            "template_content": [],
            "message": message,
            "async": async_send,
        }
        data = _request_json("POST", f"{base_url}/messages/send-template.json", json_body=body)
        return _dump_json(data)
    except Exception as e:
        logger.error("mandrill_send_template failed", exc_info=True)
        return f"[Error]: Mandrill template send failed: {e}"


@tool
def messagebird_send_sms(
    originator: str,
    recipients: str,
    body: str,
    reference: str = "",
    report_url: str = "",
    scheduled_datetime: str = "",
    message_type: str = "sms",
    datacoding: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS with MessageBird.

    Args:
        originator: Sender number or sender ID.
        recipients: Comma-separated recipient phone numbers.
        body: Message body.
        reference: Optional client reference.
        report_url: Optional status report URL.
        scheduled_datetime: Optional scheduled date/time in RFC3339 format.
        message_type: MessageBird message type, usually sms.
        datacoding: Optional datacoding, such as plain, unicode, or auto.
    """
    if not originator.strip() or not _split_csv(recipients) or not body.strip():
        return "[Error]: originator, recipients, and body are required."
    try:
        base_url, headers_or_error = _messagebird_config("messagebird_send_sms", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        payload = _filtered_params(
            {
                "originator": originator.strip(),
                "recipients": _split_csv(recipients),
                "body": body,
                "reference": reference.strip(),
                "reportUrl": report_url.strip(),
                "scheduledDatetime": scheduled_datetime.strip(),
                "type": message_type.strip() or "sms",
                "datacoding": datacoding.strip(),
            }
        )
        data = _request_json("POST", f"{base_url}/messages", json_body=payload, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("messagebird_send_sms failed", exc_info=True)
        return f"[Error]: MessageBird SMS send failed: {e}"


@tool
def messagebird_get_balance(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get MessageBird account balance."""
    try:
        base_url, headers_or_error = _messagebird_config("messagebird_get_balance", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/balance", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("messagebird_get_balance failed", exc_info=True)
        return f"[Error]: MessageBird balance lookup failed: {e}"


@tool
def mocean_send_sms(
    from_number: str,
    to_number: str,
    text: str,
    delivery_report_url: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS with Mocean.

    Args:
        from_number: Sender number or sender ID.
        to_number: Recipient phone number.
        text: SMS text.
        delivery_report_url: Optional delivery report callback URL.
    """
    if not from_number.strip() or not to_number.strip() or not text.strip():
        return "[Error]: from_number, to_number, and text are required."
    try:
        base_url, api_key, api_secret, error = _mocean_config("mocean_send_sms", config)
        if error:
            return error
        form = {
            "mocean-api-key": api_key,
            "mocean-api-secret": api_secret,
            "mocean-from": from_number.strip(),
            "mocean-to": to_number.strip(),
            "mocean-text": text,
        }
        if delivery_report_url.strip():
            form["mocean-dlr-url"] = delivery_report_url.strip()
            form["mocean-dlr-mask"] = "1"
        data = _request_json("POST", f"{base_url}/rest/2/sms", form_data=form)
        return _dump_json(data.get("messages", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mocean_send_sms failed", exc_info=True)
        return f"[Error]: Mocean SMS send failed: {e}"


@tool
def mocean_send_voice(
    from_number: str,
    to_number: str,
    text: str,
    language: str = "en-US",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Start a Mocean voice call that speaks text.

    Args:
        from_number: Caller/sender number.
        to_number: Recipient phone number.
        text: Text to speak.
        language: Speech language, such as en-US, en-GB, ja-JP, ko-KR, or cmn-CN.
    """
    if not from_number.strip() or not to_number.strip() or not text.strip():
        return "[Error]: from_number, to_number, and text are required."
    try:
        base_url, api_key, api_secret, error = _mocean_config("mocean_send_voice", config)
        if error:
            return error
        command = [{"action": "say", "language": language.strip() or "en-US", "text": text}]
        data = _request_json(
            "POST",
            f"{base_url}/rest/2/voice/dial",
            form_data={
                "mocean-api-key": api_key,
                "mocean-api-secret": api_secret,
                "mocean-from": from_number.strip(),
                "mocean-to": to_number.strip(),
                "mocean-command": json.dumps(command),
            },
        )
        return _dump_json(data.get("voice", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mocean_send_voice failed", exc_info=True)
        return f"[Error]: Mocean voice send failed: {e}"


@tool
def mocean_get_balance(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Mocean account balance."""
    try:
        base_url, api_key, api_secret, error = _mocean_config("mocean_get_balance", config)
        if error:
            return error
        data = _request_json(
            "GET",
            f"{base_url}/rest/2/account/balance",
            params={"mocean-api-key": api_key, "mocean-api-secret": api_secret},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mocean_get_balance failed", exc_info=True)
        return f"[Error]: Mocean balance lookup failed: {e}"


@tool
def msg91_send_sms(
    sender_id: str,
    to_numbers: str,
    message: str,
    route: int = 4,
    country: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS with MSG91.

    Args:
        sender_id: MSG91 sender ID.
        to_numbers: Comma-separated recipient numbers.
        message: SMS message text.
        route: MSG91 route, default 4 for transactional.
        country: Country code filter, default 0 for international format in numbers.
    """
    if not sender_id.strip() or not _split_csv(to_numbers) or not message.strip():
        return "[Error]: sender_id, to_numbers, and message are required."
    try:
        base_url, auth_key, error = _msg91_config("msg91_send_sms", config)
        if error:
            return error
        data = _request_json(
            "GET",
            f"{base_url}/sendhttp.php",
            params={
                "authkey": auth_key,
                "route": int(route),
                "country": int(country),
                "sender": sender_id.strip(),
                "mobiles": ",".join(_split_csv(to_numbers)),
                "message": message,
            },
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("msg91_send_sms failed", exc_info=True)
        return f"[Error]: MSG91 SMS send failed: {e}"


@tool
def plivo_send_message(
    from_number: str,
    to_numbers: str,
    text: str,
    message_type: str = "sms",
    callback_url: str = "",
    callback_method: str = "POST",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS or MMS message with Plivo."""
    recipients = _split_csv(to_numbers)
    if not from_number.strip() or not recipients or not text.strip():
        return "[Error]: from_number, to_numbers, and text are required."
    try:
        base_url, auth_id, headers_or_error = _plivo_config("plivo_send_message", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {
            "src": from_number.strip(),
            "dst": "<".join(recipients),
            "text": text,
            "type": message_type.strip() or "sms",
        }
        if callback_url.strip():
            body["url"] = callback_url.strip()
            body["method"] = callback_method.strip().upper() or "POST"
        data = _request_json(
            "POST",
            f"{base_url}/Account/{quote(auth_id, safe='')}/Message/",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("plivo_send_message failed", exc_info=True)
        return f"[Error]: Plivo message send failed: {e}"


@tool
def plivo_get_account(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Plivo account metadata."""
    try:
        base_url, auth_id, headers_or_error = _plivo_config("plivo_get_account", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/Account/{quote(auth_id, safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("plivo_get_account failed", exc_info=True)
        return f"[Error]: Plivo account lookup failed: {e}"


@tool
def vonage_send_sms(
    from_name: str,
    to_number: str,
    text: str,
    message_type: str = "unicode",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS message with Vonage."""
    if not from_name.strip() or not to_number.strip() or not text.strip():
        return "[Error]: from_name, to_number, and text are required."
    try:
        base_url, api_key, api_secret, error = _vonage_config("vonage_send_sms", config)
        if error:
            return error
        data = _request_json(
            "POST",
            f"{base_url}/sms/json",
            form_data={
                "api_key": api_key,
                "api_secret": api_secret,
                "from": from_name.strip(),
                "to": to_number.strip(),
                "text": text,
                "type": message_type.strip() or "unicode",
            },
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("vonage_send_sms failed", exc_info=True)
        return f"[Error]: Vonage SMS send failed: {e}"


@tool
def vonage_get_balance(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Vonage account balance."""
    try:
        base_url, api_key, api_secret, error = _vonage_config("vonage_get_balance", config)
        if error:
            return error
        data = _request_json(
            "GET",
            f"{base_url}/account/get-balance",
            params={"api_key": api_key, "api_secret": api_secret},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("vonage_get_balance failed", exc_info=True)
        return f"[Error]: Vonage balance lookup failed: {e}"


@tool
def seven_send_sms(
    to_numbers: str,
    text: str,
    from_name: str = "",
    flash: bool = False,
    label: str = "",
    foreign_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an SMS message with seven.io."""
    recipients = _split_csv(to_numbers)
    if not recipients or not text.strip():
        return "[Error]: to_numbers and text are required."
    try:
        base_url, headers_or_error = _seven_config("seven_send_sms", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/sms",
            form_data={
                "to": ",".join(recipients),
                "text": text,
                "from": from_name.strip(),
                "flash": "1" if flash else "0",
                "label": label.strip(),
                "foreign_id": foreign_id.strip(),
                "json": "1",
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("seven_send_sms failed", exc_info=True)
        return f"[Error]: seven.io SMS send failed: {e}"


@tool
def seven_get_balance(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get seven.io account balance."""
    try:
        base_url, headers_or_error = _seven_config("seven_get_balance", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/balance", params={"json": "1"}, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("seven_get_balance failed", exc_info=True)
        return f"[Error]: seven.io balance lookup failed: {e}"


MESSAGING_DELIVERY_SERVICE_TOOLS = [
    twilio_send_message,
    twilio_list_messages,
    twilio_get_message,
    twilio_make_call,
    sendgrid_send_email,
    sendgrid_list_contacts,
    sendgrid_get_contact,
    sendgrid_upsert_contacts,
    sendgrid_list_lists,
    mailgun_send_email,
    mailgun_list_events,
    mailgun_get_domain,
    brevo_send_email,
    brevo_list_contacts,
    brevo_get_contact,
    brevo_create_contact,
    brevo_update_contact,
    brevo_list_senders,
    mailjet_send_email,
    mailjet_send_sms,
    mailjet_list_contacts,
    mailjet_get_contact,
    mandrill_send_email,
    mandrill_send_template,
    messagebird_send_sms,
    messagebird_get_balance,
    mocean_send_sms,
    mocean_send_voice,
    mocean_get_balance,
    msg91_send_sms,
    plivo_send_message,
    plivo_get_account,
    vonage_send_sms,
    vonage_get_balance,
    seven_send_sms,
    seven_get_balance,
]
