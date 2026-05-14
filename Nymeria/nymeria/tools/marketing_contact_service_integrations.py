"""Marketing and contact-list service integration tools."""

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
_ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL = "https://example.api-us1.com"
_CONVERTKIT_BASE_URL = "https://api.convertkit.com/v3"
_GETRESPONSE_BASE_URL = "https://api.getresponse.com/v3"
_MAILERLITE_BASE_URL = "https://connect.mailerlite.com/api"
_MAILERLITE_CLASSIC_BASE_URL = "https://api.mailerlite.com/api/v2"


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


MARKETING_CONTACT_SERVICE_TOOLS = [
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
]
