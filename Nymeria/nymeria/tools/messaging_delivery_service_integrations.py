"""Messaging and transactional email service integration tools."""

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
_TWILIO_BASE_URL = "https://api.twilio.com/2010-04-01"
_SENDGRID_BASE_URL = "https://api.sendgrid.com/v3"
_MAILGUN_BASE_URL = "https://api.mailgun.net/v3"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _filtered_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _limit(value: int, *, default: int = 25, max_value: int = 1000) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


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
            return response.json()
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
]
