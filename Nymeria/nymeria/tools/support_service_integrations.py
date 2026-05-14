"""Support and customer messaging service integration tools."""

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
_HELPSCOUT_BASE_URL = "https://api.helpscout.net/v2"
_INTERCOM_BASE_URL = "https://api.intercom.io"
_INTERCOM_VERSION = "2.11"


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


def _limit(value: int, *, default: int = 25, max_value: int = 100) -> int:
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
        return [] if expected is list else {}
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
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered_params(params),
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
            if isinstance(body.get("errors"), list) and body["errors"]:
                detail = "; ".join(str(item) for item in body["errors"])
            elif isinstance(body.get("errors"), dict):
                detail = "; ".join(f"{key}: {value}" for key, value in body["errors"].items())
            detail = (
                detail
                or body.get("message")
                or body.get("description")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _freshdesk_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="freshdesk",
            provider_aliases=("freshdesk_api", "freshworks"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshdesk_base_url")
    )
    domain = (
        _credential_value(
            provider="freshdesk",
            provider_aliases=("freshdesk_api", "freshworks"),
            field_names=("domain", "subdomain"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshdesk_domain")
    )
    api_key = _credential_value(
        provider="freshdesk",
        provider_aliases=("freshdesk_api", "freshworks"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("freshdesk_api_key")
    if not base and domain:
        domain = domain.strip().replace(".freshdesk.com", "")
        base = f"https://{domain}.freshdesk.com/api/v2"
    if not base:
        return "", (
            "[Error]: No Freshdesk base URL found. Save a Freshdesk credential with "
            '"base_url" or "domain", or set FRESHDESK_BASE_URL or FRESHDESK_DOMAIN.'
        )
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="freshdesk",
            field_names=("api_key", "domain"),
            tool_name=tool_name,
            env_var="FRESHDESK_API_KEY",
            display_name="Freshdesk",
        )
    auth = base64.b64encode(f"{api_key}:X".encode()).decode()
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _helpscout_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="helpscout",
            provider_aliases=("help_scout", "helpscout_oauth2"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("helpscout_base_url")
        or _HELPSCOUT_BASE_URL
    )
    token = _credential_value(
        provider="helpscout",
        provider_aliases=("help_scout", "helpscout_oauth2"),
        field_names=("access_token", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("helpscout_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="helpscout",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="HELPSCOUT_ACCESS_TOKEN",
            display_name="Help Scout",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _intercom_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="intercom",
            provider_aliases=("intercom_api", "intercom_oauth2"),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("intercom_base_url")
        or _INTERCOM_BASE_URL
    )
    version = (
        _credential_value(
            provider="intercom",
            provider_aliases=("intercom_api", "intercom_oauth2"),
            field_names=("intercom_version", "version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("intercom_version")
        or _INTERCOM_VERSION
    )
    token = _credential_value(
        provider="intercom",
        provider_aliases=("intercom_api", "intercom_oauth2"),
        field_names=("access_token", "api_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("intercom_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="intercom",
            field_names=("access_token", "api_key", "token", "value"),
            tool_name=tool_name,
            env_var="INTERCOM_ACCESS_TOKEN",
            display_name="Intercom",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Intercom-Version": version,
        "User-Agent": "Nymeria",
    }


@tool
def freshdesk_list_tickets(
    email: str = "",
    requester_id: str = "",
    filter_name: str = "",
    updated_since: str = "",
    include: str = "",
    order_by: str = "",
    order_type: str = "",
    page: int = 1,
    per_page: int = 30,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Freshdesk tickets.

    Args:
        email: Optional requester email filter.
        requester_id: Optional requester ID filter.
        filter_name: Optional Freshdesk ticket filter name.
        updated_since: Optional ISO date/time filter for recently updated tickets.
        include: Optional comma-separated embeds such as stats.
        order_by: Optional sort field.
        order_type: Optional sort order, asc or desc.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    try:
        base_url, headers_or_error = _freshdesk_config("freshdesk_list_tickets", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tickets",
            params={
                "email": email.strip(),
                "requester_id": requester_id.strip(),
                "filter": filter_name.strip(),
                "updated_since": updated_since.strip(),
                "include": ",".join(_split_csv(include)),
                "order_by": order_by.strip(),
                "order_type": order_type.strip(),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, default=30),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_list_tickets failed", exc_info=True)
        return f"[Error]: Freshdesk ticket list failed: {e}"


@tool
def freshdesk_search_tickets(
    query: str,
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Freshdesk tickets with Freshdesk's ticket query syntax.

    Args:
        query: Freshdesk search query, such as "status:2 OR status:3".
        page: Result page number.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _freshdesk_config("freshdesk_search_tickets", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/search/tickets",
            params={"query": query.strip(), "page": max(1, int(page or 1))},
            headers=headers_or_error,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("freshdesk_search_tickets failed", exc_info=True)
        return f"[Error]: Freshdesk ticket search failed: {e}"


@tool
def freshdesk_get_ticket(
    ticket_id: str,
    include: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Freshdesk ticket by ID.

    Args:
        ticket_id: Freshdesk ticket ID.
        include: Optional comma-separated embeds such as conversations, requester, or stats.
    """
    ticket_id = ticket_id.strip()
    if not ticket_id:
        return "[Error]: ticket_id is required."
    try:
        base_url, headers_or_error = _freshdesk_config("freshdesk_get_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tickets/{quote(ticket_id, safe='')}",
            params={"include": ",".join(_split_csv(include))},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_get_ticket failed", exc_info=True)
        return f"[Error]: Freshdesk ticket lookup failed: {e}"


@tool
def freshdesk_create_ticket(
    subject: str,
    description: str,
    email: str = "",
    requester_id: str = "",
    priority: int = 1,
    status: int = 2,
    ticket_type: str = "",
    tags: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Freshdesk ticket.

    Args:
        subject: Ticket subject.
        description: Ticket description text or HTML.
        email: Requester email. Required when requester_id is not supplied.
        requester_id: Existing requester ID.
        priority: Freshdesk priority value, usually 1-4.
        status: Freshdesk status value, usually 2=open.
        ticket_type: Optional ticket type.
        tags: Optional comma-separated tags.
        custom_fields_json: Optional custom_fields object as JSON.
    """
    if not subject.strip() or not description.strip():
        return "[Error]: subject and description are required."
    if not email.strip() and not requester_id.strip():
        return "[Error]: provide email or requester_id."
    try:
        body = _filtered_params(
            {
                "subject": subject.strip(),
                "description": description,
                "email": email.strip(),
                "requester_id": requester_id.strip(),
                "priority": int(priority),
                "status": int(status),
                "type": ticket_type.strip(),
                "tags": _split_csv(tags),
                "custom_fields": _parse_json(custom_fields_json, expected=dict, label="custom_fields_json"),
            }
        )
        base_url, headers_or_error = _freshdesk_config("freshdesk_create_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/tickets", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_create_ticket failed", exc_info=True)
        return f"[Error]: Freshdesk ticket creation failed: {e}"


@tool
def freshdesk_update_ticket(
    ticket_id: str,
    subject: str = "",
    description: str = "",
    priority: int = 0,
    status: int = 0,
    ticket_type: str = "",
    tags: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Freshdesk ticket.

    Args:
        ticket_id: Freshdesk ticket ID.
        subject: Optional updated subject.
        description: Optional updated description.
        priority: Optional Freshdesk priority value.
        status: Optional Freshdesk status value.
        ticket_type: Optional ticket type.
        tags: Optional comma-separated replacement tags.
        custom_fields_json: Optional custom_fields object as JSON.
    """
    ticket_id = ticket_id.strip()
    if not ticket_id:
        return "[Error]: ticket_id is required."
    try:
        body = _filtered_params(
            {
                "subject": subject.strip(),
                "description": description,
                "priority": int(priority) if priority else None,
                "status": int(status) if status else None,
                "type": ticket_type.strip(),
                "tags": _split_csv(tags),
                "custom_fields": _parse_json(custom_fields_json, expected=dict, label="custom_fields_json"),
            }
        )
        if not body:
            return "[Error]: provide at least one ticket field to update."
        base_url, headers_or_error = _freshdesk_config("freshdesk_update_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/tickets/{quote(ticket_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_update_ticket failed", exc_info=True)
        return f"[Error]: Freshdesk ticket update failed: {e}"


@tool
def freshdesk_delete_ticket(
    ticket_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Freshdesk ticket.

    Args:
        ticket_id: Freshdesk ticket ID.
    """
    ticket_id = ticket_id.strip()
    if not ticket_id:
        return "[Error]: ticket_id is required."
    try:
        base_url, headers_or_error = _freshdesk_config("freshdesk_delete_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/tickets/{quote(ticket_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_delete_ticket failed", exc_info=True)
        return f"[Error]: Freshdesk ticket deletion failed: {e}"


@tool
def freshdesk_list_contacts(
    email: str = "",
    company_id: str = "",
    state: str = "",
    page: int = 1,
    per_page: int = 30,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Freshdesk contacts.

    Args:
        email: Optional email filter.
        company_id: Optional company ID filter.
        state: Optional contact state filter.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    try:
        base_url, headers_or_error = _freshdesk_config("freshdesk_list_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/contacts",
            params={
                "email": email.strip(),
                "company_id": company_id.strip(),
                "state": state.strip(),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, default=30),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_list_contacts failed", exc_info=True)
        return f"[Error]: Freshdesk contact list failed: {e}"


@tool
def freshdesk_get_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Freshdesk contact by ID.

    Args:
        contact_id: Freshdesk contact ID.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _freshdesk_config("freshdesk_get_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/contacts/{quote(contact_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_get_contact failed", exc_info=True)
        return f"[Error]: Freshdesk contact lookup failed: {e}"


@tool
def freshdesk_create_contact(
    name: str,
    email: str,
    phone: str = "",
    mobile: str = "",
    company_id: str = "",
    description: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Freshdesk contact.

    Args:
        name: Contact name.
        email: Contact email.
        phone: Optional phone number.
        mobile: Optional mobile number.
        company_id: Optional Freshdesk company ID.
        description: Optional contact description.
        custom_fields_json: Optional custom_fields object as JSON.
    """
    if not name.strip() or not email.strip():
        return "[Error]: name and email are required."
    try:
        body = _filtered_params(
            {
                "name": name.strip(),
                "email": email.strip(),
                "phone": phone.strip(),
                "mobile": mobile.strip(),
                "company_id": company_id.strip(),
                "description": description.strip(),
                "custom_fields": _parse_json(custom_fields_json, expected=dict, label="custom_fields_json"),
            }
        )
        base_url, headers_or_error = _freshdesk_config("freshdesk_create_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/contacts", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_create_contact failed", exc_info=True)
        return f"[Error]: Freshdesk contact creation failed: {e}"


@tool
def freshdesk_update_contact(
    contact_id: str,
    name: str = "",
    email: str = "",
    phone: str = "",
    mobile: str = "",
    company_id: str = "",
    description: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Freshdesk contact.

    Args:
        contact_id: Freshdesk contact ID.
        name: Optional updated name.
        email: Optional updated email.
        phone: Optional phone number.
        mobile: Optional mobile number.
        company_id: Optional Freshdesk company ID.
        description: Optional contact description.
        custom_fields_json: Optional custom_fields object as JSON.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        body = _filtered_params(
            {
                "name": name.strip(),
                "email": email.strip(),
                "phone": phone.strip(),
                "mobile": mobile.strip(),
                "company_id": company_id.strip(),
                "description": description.strip(),
                "custom_fields": _parse_json(custom_fields_json, expected=dict, label="custom_fields_json"),
            }
        )
        if not body:
            return "[Error]: provide at least one contact field to update."
        base_url, headers_or_error = _freshdesk_config("freshdesk_update_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/contacts/{quote(contact_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshdesk_update_contact failed", exc_info=True)
        return f"[Error]: Freshdesk contact update failed: {e}"


@tool
def helpscout_list_mailboxes(
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Help Scout mailboxes.

    Args:
        page: Result page number.
    """
    try:
        base_url, headers_or_error = _helpscout_config("helpscout_list_mailboxes", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/mailboxes", params={"page": max(1, int(page or 1))}, headers=headers_or_error)
        return _dump_json(data.get("_embedded", {}).get("mailboxes", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("helpscout_list_mailboxes failed", exc_info=True)
        return f"[Error]: Help Scout mailbox list failed: {e}"


@tool
def helpscout_get_mailbox(
    mailbox_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Help Scout mailbox by ID.

    Args:
        mailbox_id: Help Scout mailbox ID.
    """
    mailbox_id = mailbox_id.strip()
    if not mailbox_id:
        return "[Error]: mailbox_id is required."
    try:
        base_url, headers_or_error = _helpscout_config("helpscout_get_mailbox", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/mailboxes/{quote(mailbox_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_get_mailbox failed", exc_info=True)
        return f"[Error]: Help Scout mailbox lookup failed: {e}"


@tool
def helpscout_list_conversations(
    mailbox_id: str = "",
    status: str = "active",
    query: str = "",
    tag: str = "",
    embed: str = "",
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List and filter Help Scout conversations.

    Args:
        mailbox_id: Optional mailbox ID filter.
        status: Conversation status filter, such as active, open, closed, or all.
        query: Optional Help Scout query.
        tag: Optional tag filter.
        embed: Optional comma-separated embeds such as threads.
        page: Result page number.
    """
    try:
        base_url, headers_or_error = _helpscout_config("helpscout_list_conversations", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/conversations",
            params={
                "mailbox": mailbox_id.strip(),
                "status": status.strip() or "active",
                "query": query.strip(),
                "tag": tag.strip(),
                "embed": ",".join(_split_csv(embed)),
                "page": max(1, int(page or 1)),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("_embedded", {}).get("conversations", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("helpscout_list_conversations failed", exc_info=True)
        return f"[Error]: Help Scout conversation list failed: {e}"


@tool
def helpscout_get_conversation(
    conversation_id: str,
    embed: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Help Scout conversation by ID.

    Args:
        conversation_id: Help Scout conversation ID.
        embed: Optional comma-separated embeds such as threads.
    """
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return "[Error]: conversation_id is required."
    try:
        base_url, headers_or_error = _helpscout_config("helpscout_get_conversation", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/conversations/{quote(conversation_id, safe='')}",
            params={"embed": ",".join(_split_csv(embed))},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_get_conversation failed", exc_info=True)
        return f"[Error]: Help Scout conversation lookup failed: {e}"


@tool
def helpscout_create_conversation(
    mailbox_id: str,
    subject: str,
    customer_email: str,
    thread_text: str,
    customer_first_name: str = "",
    customer_last_name: str = "",
    assign_to: str = "",
    status: str = "active",
    tags: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Help Scout conversation with an initial customer thread.

    Args:
        mailbox_id: Help Scout mailbox ID.
        subject: Conversation subject.
        customer_email: Customer email. Help Scout can create the customer if needed.
        thread_text: Initial thread text.
        customer_first_name: Optional customer first name.
        customer_last_name: Optional customer last name.
        assign_to: Optional user ID to assign to.
        status: Initial status.
        tags: Optional comma-separated tags.
    """
    if not mailbox_id.strip() or not subject.strip() or not customer_email.strip() or not thread_text.strip():
        return "[Error]: mailbox_id, subject, customer_email, and thread_text are required."
    try:
        body = _filtered_params(
            {
                "mailboxId": int(mailbox_id),
                "subject": subject.strip(),
                "customer": _filtered_params(
                    {
                        "email": customer_email.strip(),
                        "firstName": customer_first_name.strip(),
                        "lastName": customer_last_name.strip(),
                    }
                ),
                "threads": [{"type": "customer", "text": thread_text, "customer": {"email": customer_email.strip()}}],
                "assignTo": int(assign_to) if assign_to.strip() else None,
                "status": status.strip() or "active",
                "tags": _split_csv(tags),
            }
        )
        base_url, headers_or_error = _helpscout_config("helpscout_create_conversation", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/conversations", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_create_conversation failed", exc_info=True)
        return f"[Error]: Help Scout conversation creation failed: {e}"


@tool
def helpscout_create_thread(
    conversation_id: str,
    thread_type: str,
    text: str,
    customer_email: str = "",
    user_id: str = "",
    status: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Help Scout conversation thread.

    Args:
        conversation_id: Help Scout conversation ID.
        thread_type: Thread type, such as customer, reply, note, or message.
        text: Thread body text.
        customer_email: Customer email for customer threads.
        user_id: Help Scout user ID for agent/user-authored threads.
        status: Optional new conversation status.
    """
    conversation_id = conversation_id.strip()
    if not conversation_id or not thread_type.strip() or not text.strip():
        return "[Error]: conversation_id, thread_type, and text are required."
    try:
        body = _filtered_params(
            {
                "type": thread_type.strip(),
                "text": text,
                "customer": {"email": customer_email.strip()} if customer_email.strip() else None,
                "user": int(user_id) if user_id.strip() else None,
                "status": status.strip(),
            }
        )
        base_url, headers_or_error = _helpscout_config("helpscout_create_thread", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/conversations/{quote(conversation_id, safe='')}/threads",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_create_thread failed", exc_info=True)
        return f"[Error]: Help Scout thread creation failed: {e}"


@tool
def helpscout_list_customers(
    query: str = "",
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Help Scout customers.

    Args:
        query: Optional customer query/filter.
        page: Result page number.
    """
    try:
        base_url, headers_or_error = _helpscout_config("helpscout_list_customers", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/customers",
            params={"query": query.strip(), "page": max(1, int(page or 1))},
            headers=headers_or_error,
        )
        return _dump_json(data.get("_embedded", {}).get("customers", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("helpscout_list_customers failed", exc_info=True)
        return f"[Error]: Help Scout customer list failed: {e}"


@tool
def helpscout_get_customer(
    customer_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Help Scout customer by ID.

    Args:
        customer_id: Help Scout customer ID.
    """
    customer_id = customer_id.strip()
    if not customer_id:
        return "[Error]: customer_id is required."
    try:
        base_url, headers_or_error = _helpscout_config("helpscout_get_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/customers/{quote(customer_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_get_customer failed", exc_info=True)
        return f"[Error]: Help Scout customer lookup failed: {e}"


@tool
def helpscout_create_customer(
    first_name: str,
    last_name: str = "",
    email: str = "",
    phone: str = "",
    organization: str = "",
    job_title: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Help Scout customer.

    Args:
        first_name: Customer first name.
        last_name: Optional customer last name.
        email: Optional customer email.
        phone: Optional customer phone.
        organization: Optional organization name.
        job_title: Optional job title.
    """
    if not first_name.strip() and not email.strip():
        return "[Error]: provide first_name or email."
    try:
        body = _filtered_params(
            {
                "firstName": first_name.strip(),
                "lastName": last_name.strip(),
                "emails": [{"value": email.strip()}] if email.strip() else None,
                "phones": [{"value": phone.strip()}] if phone.strip() else None,
                "organization": organization.strip(),
                "jobTitle": job_title.strip(),
            }
        )
        base_url, headers_or_error = _helpscout_config("helpscout_create_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/customers", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_create_customer failed", exc_info=True)
        return f"[Error]: Help Scout customer creation failed: {e}"


@tool
def helpscout_update_customer(
    customer_id: str,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    organization: str = "",
    job_title: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Help Scout customer.

    Args:
        customer_id: Help Scout customer ID.
        first_name: Optional first name.
        last_name: Optional last name.
        email: Optional replacement email.
        phone: Optional replacement phone.
        organization: Optional organization name.
        job_title: Optional job title.
    """
    customer_id = customer_id.strip()
    if not customer_id:
        return "[Error]: customer_id is required."
    try:
        body = _filtered_params(
            {
                "firstName": first_name.strip(),
                "lastName": last_name.strip(),
                "emails": [{"value": email.strip()}] if email.strip() else None,
                "phones": [{"value": phone.strip()}] if phone.strip() else None,
                "organization": organization.strip(),
                "jobTitle": job_title.strip(),
            }
        )
        if not body:
            return "[Error]: provide at least one customer field to update."
        base_url, headers_or_error = _helpscout_config("helpscout_update_customer", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/customers/{quote(customer_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("helpscout_update_customer failed", exc_info=True)
        return f"[Error]: Help Scout customer update failed: {e}"


@tool
def intercom_list_contacts(
    per_page: int = 25,
    starting_after: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Intercom contacts.

    Args:
        per_page: Results per page, 1-150.
        starting_after: Optional pagination cursor.
    """
    try:
        base_url, headers_or_error = _intercom_config("intercom_list_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/contacts",
            params={"per_page": _limit(per_page, default=25, max_value=150), "starting_after": starting_after.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("intercom_list_contacts failed", exc_info=True)
        return f"[Error]: Intercom contact list failed: {e}"


@tool
def intercom_search_contacts(
    query_json: str,
    pagination_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Intercom contacts with Intercom's JSON query DSL.

    Args:
        query_json: Intercom query object as JSON.
        pagination_json: Optional pagination object as JSON.
    """
    if not query_json.strip():
        return "[Error]: query_json is required."
    try:
        body = {"query": _parse_json(query_json, expected=dict, label="query_json")}
        pagination = _parse_json(pagination_json, expected=dict, label="pagination_json")
        if pagination:
            body["pagination"] = pagination
        base_url, headers_or_error = _intercom_config("intercom_search_contacts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/contacts/search", json_body=body, headers=headers_or_error)
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("intercom_search_contacts failed", exc_info=True)
        return f"[Error]: Intercom contact search failed: {e}"


@tool
def intercom_get_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Intercom contact by ID.

    Args:
        contact_id: Intercom contact ID.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _intercom_config("intercom_get_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/contacts/{quote(contact_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("intercom_get_contact failed", exc_info=True)
        return f"[Error]: Intercom contact lookup failed: {e}"


@tool
def intercom_create_contact(
    email: str = "",
    external_id: str = "",
    role: str = "user",
    name: str = "",
    phone: str = "",
    custom_attributes_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Intercom contact.

    Args:
        email: Optional email address.
        external_id: Optional external user ID.
        role: Contact role, usually user or lead.
        name: Optional contact name.
        phone: Optional phone number.
        custom_attributes_json: Optional custom_attributes object as JSON.
    """
    if not email.strip() and not external_id.strip():
        return "[Error]: provide email or external_id."
    try:
        body = _filtered_params(
            {
                "email": email.strip(),
                "external_id": external_id.strip(),
                "role": role.strip() or "user",
                "name": name.strip(),
                "phone": phone.strip(),
                "custom_attributes": _parse_json(
                    custom_attributes_json,
                    expected=dict,
                    label="custom_attributes_json",
                ),
            }
        )
        base_url, headers_or_error = _intercom_config("intercom_create_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/contacts", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("intercom_create_contact failed", exc_info=True)
        return f"[Error]: Intercom contact creation failed: {e}"


@tool
def intercom_update_contact(
    contact_id: str,
    email: str = "",
    external_id: str = "",
    role: str = "",
    name: str = "",
    phone: str = "",
    custom_attributes_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an Intercom contact.

    Args:
        contact_id: Intercom contact ID.
        email: Optional email address.
        external_id: Optional external user ID.
        role: Optional role.
        name: Optional contact name.
        phone: Optional phone number.
        custom_attributes_json: Optional custom_attributes object as JSON.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        body = _filtered_params(
            {
                "email": email.strip(),
                "external_id": external_id.strip(),
                "role": role.strip(),
                "name": name.strip(),
                "phone": phone.strip(),
                "custom_attributes": _parse_json(
                    custom_attributes_json,
                    expected=dict,
                    label="custom_attributes_json",
                ),
            }
        )
        if not body:
            return "[Error]: provide at least one contact field to update."
        base_url, headers_or_error = _intercom_config("intercom_update_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/contacts/{quote(contact_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("intercom_update_contact failed", exc_info=True)
        return f"[Error]: Intercom contact update failed: {e}"


@tool
def intercom_archive_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Archive an Intercom contact.

    Args:
        contact_id: Intercom contact ID.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _intercom_config("intercom_archive_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/contacts/{quote(contact_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("intercom_archive_contact failed", exc_info=True)
        return f"[Error]: Intercom contact archive failed: {e}"


@tool
def intercom_list_conversations(
    per_page: int = 25,
    starting_after: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Intercom conversations.

    Args:
        per_page: Results per page, 1-150.
        starting_after: Optional pagination cursor.
    """
    try:
        base_url, headers_or_error = _intercom_config("intercom_list_conversations", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/conversations",
            params={"per_page": _limit(per_page, default=25, max_value=150), "starting_after": starting_after.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("conversations", data.get("data", data)) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("intercom_list_conversations failed", exc_info=True)
        return f"[Error]: Intercom conversation list failed: {e}"


@tool
def intercom_get_conversation(
    conversation_id: str,
    display_as: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Intercom conversation by ID.

    Args:
        conversation_id: Intercom conversation ID.
        display_as: Optional display mode such as plaintext.
    """
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return "[Error]: conversation_id is required."
    try:
        base_url, headers_or_error = _intercom_config("intercom_get_conversation", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/conversations/{quote(conversation_id, safe='')}",
            params={"display_as": display_as.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("intercom_get_conversation failed", exc_info=True)
        return f"[Error]: Intercom conversation lookup failed: {e}"


@tool
def intercom_reply_conversation(
    conversation_id: str,
    body: str,
    admin_id: str = "",
    contact_email: str = "",
    intercom_user_id: str = "",
    user_id: str = "",
    message_type: str = "comment",
    attachment_urls: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Reply to an Intercom conversation as an admin or contact.

    Args:
        conversation_id: Intercom conversation ID or last.
        body: Reply body.
        admin_id: Admin ID for admin replies.
        contact_email: Contact email for contact replies.
        intercom_user_id: Intercom user ID for contact replies.
        user_id: External user ID for contact replies.
        message_type: Message type such as comment or note.
        attachment_urls: Optional comma-separated attachment URLs.
    """
    conversation_id = conversation_id.strip()
    if not conversation_id or not body.strip():
        return "[Error]: conversation_id and body are required."
    try:
        if admin_id.strip():
            body_json = _filtered_params(
                {
                    "type": "admin",
                    "admin_id": admin_id.strip(),
                    "message_type": message_type.strip() or "comment",
                    "body": body,
                    "attachment_urls": _split_csv(attachment_urls),
                }
            )
        else:
            body_json = _filtered_params(
                {
                    "type": "user",
                    "email": contact_email.strip(),
                    "intercom_user_id": intercom_user_id.strip(),
                    "user_id": user_id.strip(),
                    "message_type": "comment",
                    "body": body,
                    "attachment_urls": _split_csv(attachment_urls),
                }
            )
            if not any(body_json.get(key) for key in ("email", "intercom_user_id", "user_id")):
                return "[Error]: provide admin_id for admin replies, or contact_email/intercom_user_id/user_id for contact replies."
        base_url, headers_or_error = _intercom_config("intercom_reply_conversation", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/conversations/{quote(conversation_id, safe='')}/reply",
            json_body=body_json,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("intercom_reply_conversation failed", exc_info=True)
        return f"[Error]: Intercom conversation reply failed: {e}"


SUPPORT_SERVICE_TOOLS = [
    freshdesk_list_tickets,
    freshdesk_search_tickets,
    freshdesk_get_ticket,
    freshdesk_create_ticket,
    freshdesk_update_ticket,
    freshdesk_delete_ticket,
    freshdesk_list_contacts,
    freshdesk_get_contact,
    freshdesk_create_contact,
    freshdesk_update_contact,
    helpscout_list_mailboxes,
    helpscout_get_mailbox,
    helpscout_list_conversations,
    helpscout_get_conversation,
    helpscout_create_conversation,
    helpscout_create_thread,
    helpscout_list_customers,
    helpscout_get_customer,
    helpscout_create_customer,
    helpscout_update_customer,
    intercom_list_contacts,
    intercom_search_contacts,
    intercom_get_contact,
    intercom_create_contact,
    intercom_update_contact,
    intercom_archive_contact,
    intercom_list_conversations,
    intercom_get_conversation,
    intercom_reply_conversation,
]
