"""Customer engagement service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import hashlib
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
    filtered as _filtered_params,
    parse_json as _parse_json,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
    vendor_host as _vendor_host,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_HUBSPOT_BASE_URL = "https://api.hubapi.com"
_MAILCHIMP_ROOT = "https://api.mailchimp.com/3.0"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_HUBSPOT = register_provider_spec(
    ProviderCredentialSpec(
        provider="hubspot",
        aliases=("hubspot_api", "hubspot_app_token", "hubspot_oauth2"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="token",
                names=("private_app_token", "app_token", "access_token", "token", "api_key", "value"),
            ),
        ),
        hint_fields=("private_app_token", "app_token", "access_token", "token", "value"),
        env_var="HUBSPOT_ACCESS_TOKEN",
        display_name="HubSpot",
    )
)

_ZENDESK = register_provider_spec(
    ProviderCredentialSpec(
        provider="zendesk",
        aliases=("zendesk_api", "zendesk_oauth2"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="subdomain", names=("subdomain", "domain")),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "token", "value"), required=False
            ),
            CredentialFieldGroup(role="email", names=("email", "username", "user")),
            CredentialFieldGroup(role="api_token", names=("api_token", "apiToken", "password")),
        ),
        hint_fields=("email", "api_token", "subdomain"),
        env_var="ZENDESK_EMAIL, ZENDESK_API_TOKEN, and ZENDESK_SUBDOMAIN",
        display_name="Zendesk",
    )
)

_MAILCHIMP = register_provider_spec(
    ProviderCredentialSpec(
        provider="mailchimp",
        aliases=("mailchimp_api", "mailchimp_oauth2"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="server_prefix", names=("server_prefix", "dc", "data_center"), required=False
            ),
            CredentialFieldGroup(role="access_token", names=("access_token", "token")),
            CredentialFieldGroup(role="api_key", names=("api_key", "apikey", "value")),
        ),
        hint_fields=("api_key", "access_token", "value"),
        env_var="MAILCHIMP_API_KEY or MAILCHIMP_ACCESS_TOKEN",
        display_name="Mailchimp",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _limit(value: int, *, default: int = 25, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


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
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(
                client,
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
            detail = (
                body.get("message")
                or body.get("detail")
                or body.get("title")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
            if not detail and isinstance(body.get("errors"), list):
                detail = "; ".join(str(item) for item in body["errors"])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _hubspot_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_HUBSPOT.provider,
            provider_aliases=_HUBSPOT.aliases,
            field_names=_HUBSPOT.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("hubspot_base_url")
        or _HUBSPOT_BASE_URL
    )
    token = _credential_value(
        provider=_HUBSPOT.provider,
        provider_aliases=_HUBSPOT.aliases,
        field_names=_HUBSPOT.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("hubspot_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_HUBSPOT.provider,
            field_names=_HUBSPOT.hint_fields,
            tool_name=tool_name,
            env_var=_HUBSPOT.env_var,
            display_name=_HUBSPOT.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _zendesk_base(
    *,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve Zendesk's API root, reporting where the address came from.

    Returns ``(base_or_none, base_from_vault)``. The second element is the
    provenance ``_require_joined_destination`` needs: this helper resolves the
    address while its CALLER resolves the secrets, so neither half can see the
    pairing alone. It is None whenever the address came from settings.

    The subdomain branch counts as a vault-supplied address, on the same
    reasoning ERPNext's composed base records: the value is free text
    interpolated into the hostname, so a planted one steers this request exactly
    as a ``base_url`` would.
    """
    base_from_vault = _credential_value(
        provider=_ZENDESK.provider,
        provider_aliases=_ZENDESK.aliases,
        field_names=_ZENDESK.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value("zendesk_base_url")
    if base:
        clean = _base_url(base)
        return (clean if clean.endswith("/api/v2") else f"{clean}/api/v2"), base_from_vault
    subdomain_from_vault = _credential_value(
        provider=_ZENDESK.provider,
        provider_aliases=_ZENDESK.aliases,
        field_names=_ZENDESK.group("subdomain"),
        tool_name=tool_name,
        config=config,
    )
    subdomain = subdomain_from_vault or _settings_value("zendesk_subdomain")
    if subdomain:
        host = _vendor_host(subdomain, vendor_suffix=".zendesk.com", provider=_ZENDESK.provider, field="subdomain")
        return f"https://{host}/api/v2", subdomain_from_vault
    return None, None


def _zendesk_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, base_from_vault = _zendesk_base(tool_name=tool_name, config=config)
    if not base:
        return "", (
            "[Error]: No Zendesk base URL found. Save a Zendesk credential with "
            '"base_url" or "subdomain", or set ZENDESK_BASE_URL or ZENDESK_SUBDOMAIN.'
        )
    access_token_from_vault = _credential_value(
        provider=_ZENDESK.provider,
        provider_aliases=_ZENDESK.aliases,
        field_names=_ZENDESK.group("access_token"),
        tool_name=tool_name,
        config=config,
    )
    access_token = access_token_from_vault or _settings_value("zendesk_access_token")
    email = _credential_value(
        provider=_ZENDESK.provider,
        provider_aliases=_ZENDESK.aliases,
        field_names=_ZENDESK.group("email"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("zendesk_email")
    api_token_from_vault = _credential_value(
        provider=_ZENDESK.provider,
        provider_aliases=_ZENDESK.aliases,
        field_names=_ZENDESK.group("api_token"),
        tool_name=tool_name,
        config=config,
    )
    api_token = api_token_from_vault or _settings_value("zendesk_api_token")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request. Asking instead whether the record supplied EITHER alternative
    # would reproduce slice B's own weakness one level down: a record holding
    # base_url plus an api_token clears "some anchor", and the bearer branch
    # would then send the operator's access token to that record's address. The
    # basic branch guards the API TOKEN, not the email, which proves nothing.
    if access_token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=access_token_from_vault,
            secret=access_token,
            provider=_ZENDESK.provider,
        )
        headers["Authorization"] = f"Bearer {access_token}"
        return base, headers
    if email and api_token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=api_token_from_vault,
            secret=api_token,
            provider=_ZENDESK.provider,
        )
        raw = f"{email}/token:{api_token}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
        return base, headers
    return base, _setup_hint(
        provider=_ZENDESK.provider,
        field_names=_ZENDESK.hint_fields,
        tool_name=tool_name,
        env_var=_ZENDESK.env_var,
        display_name=_ZENDESK.display_name,
    )


def _mailchimp_base(
    *,
    api_key: Optional[str],
    api_key_from_vault: Optional[str],
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve Mailchimp's API root, reporting where the address came from.

    Returns ``(base_or_none, base_from_vault)``. The second element is the
    provenance ``_require_joined_destination`` needs: this helper resolves the
    address while its CALLER resolves the secrets, so neither half can see the
    pairing alone. It is None whenever the address came from settings.

    Both fallbacks count as a vault-supplied address. The server prefix is free
    text interpolated into the hostname, and the last resort SPLITS THAT PREFIX
    OUT OF THE API KEY ITSELF, so when the key came from the vault so did the
    address, which is why ``api_key_from_vault`` has to be passed in: the key
    lookup is not a destination lookup, so any record can answer it.
    """
    base_from_vault = _credential_value(
        provider=_MAILCHIMP.provider,
        provider_aliases=_MAILCHIMP.aliases,
        field_names=_MAILCHIMP.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value("mailchimp_base_url")
    if base:
        return _base_url(base), base_from_vault
    server_prefix_from_vault = _credential_value(
        provider=_MAILCHIMP.provider,
        provider_aliases=_MAILCHIMP.aliases,
        field_names=_MAILCHIMP.group("server_prefix"),
        tool_name=tool_name,
        config=config,
    )
    server_prefix = server_prefix_from_vault or _settings_value("mailchimp_server_prefix")
    prefix_from_vault = server_prefix_from_vault
    if not server_prefix and api_key and "-" in api_key:
        server_prefix = api_key.rsplit("-", 1)[-1]
        prefix_from_vault = api_key_from_vault
    if server_prefix:
        host = _vendor_host(server_prefix, vendor_suffix=".api.mailchimp.com", provider=_MAILCHIMP.provider, field="server_prefix")
        return f"https://{host}/3.0", prefix_from_vault
    return None, None


def _mailchimp_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    access_token_from_vault = _credential_value(
        provider=_MAILCHIMP.provider,
        provider_aliases=_MAILCHIMP.aliases,
        field_names=_MAILCHIMP.group("access_token"),
        tool_name=tool_name,
        config=config,
    )
    access_token = access_token_from_vault or _settings_value("mailchimp_access_token")
    api_key_from_vault = _credential_value(
        provider=_MAILCHIMP.provider,
        provider_aliases=_MAILCHIMP.aliases,
        field_names=_MAILCHIMP.group("api_key"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value("mailchimp_api_key")
    base, base_from_vault = _mailchimp_base(
        api_key=api_key,
        api_key_from_vault=api_key_from_vault,
        tool_name=tool_name,
        config=config,
    )
    if not base and access_token:
        # The vendor root is a constant, so it carries no vault provenance.
        base = _MAILCHIMP_ROOT
    if not base:
        return "", (
            "[Error]: No Mailchimp API root found. Save a Mailchimp credential with "
            '"api_key" or "server_prefix", or set MAILCHIMP_API_KEY / MAILCHIMP_SERVER_PREFIX.'
        )
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request. Asking instead whether the record supplied EITHER alternative
    # would reproduce slice B's own weakness one level down: a record holding
    # base_url plus an api_key clears "some anchor", and the access-token branch
    # would then send the operator's token to that record's address.
    if access_token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=access_token_from_vault,
            secret=access_token,
            provider=_MAILCHIMP.provider,
        )
        auth = f"Bearer {access_token}"
    elif api_key:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=api_key_from_vault,
            secret=api_key,
            provider=_MAILCHIMP.provider,
        )
        auth = f"apikey {api_key}"
    else:
        return base, _setup_hint(
            provider=_MAILCHIMP.provider,
            field_names=_MAILCHIMP.hint_fields,
            tool_name=tool_name,
            env_var=_MAILCHIMP.env_var,
            display_name=_MAILCHIMP.display_name,
        )
    return base, {
        "Accept": "application/json",
        "Authorization": auth,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _crm_object_type(object_type: str) -> str:
    value = object_type.strip().lower()
    aliases = {
        "contact": "contacts",
        "company": "companies",
        "deal": "deals",
        "ticket": "tickets",
    }
    return aliases.get(value, value)


def _subscriber_hash(email_or_hash: str) -> str:
    value = email_or_hash.strip()
    if "@" not in value and len(value) == 32:
        return value.lower()
    return hashlib.md5(value.lower().encode()).hexdigest()


@tool
def hubspot_list_crm_objects(
    object_type: str = "contacts",
    properties: str = "",
    limit: int = 50,
    after: str = "",
    archived: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List HubSpot CRM objects such as contacts, companies, deals, or tickets.

    Args:
        object_type: CRM object type, for example contacts, companies, deals, or tickets.
        properties: Optional comma-separated properties to return.
        limit: Number of records to return, 1-100.
        after: Optional pagination cursor.
        archived: Include archived records.
    """
    object_type = _crm_object_type(object_type)
    if not object_type:
        return "[Error]: object_type is required."
    try:
        base_url, headers_or_error = _hubspot_config("hubspot_list_crm_objects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/crm/v3/objects/{quote(object_type, safe='')}",
            params={
                "properties": ",".join(_split_csv(properties)),
                "limit": _limit(limit, default=50),
                "after": after.strip(),
                "archived": str(bool(archived)).lower(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("hubspot_list_crm_objects failed", exc_info=True)
        return f"[Error]: HubSpot CRM object list failed: {e}"


@tool
def hubspot_search_crm_objects(
    object_type: str,
    query: str = "",
    filter_groups_json: str = "",
    properties: str = "",
    sorts_json: str = "",
    limit: int = 50,
    after: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search HubSpot CRM objects.

    Args:
        object_type: CRM object type, for example contacts, companies, deals, or tickets.
        query: Optional free-text query.
        filter_groups_json: Optional HubSpot filterGroups array as JSON.
        properties: Optional comma-separated properties to return.
        sorts_json: Optional HubSpot sorts array as JSON.
        limit: Number of results to return, 1-100.
        after: Optional pagination cursor.
    """
    object_type = _crm_object_type(object_type)
    if not object_type:
        return "[Error]: object_type is required."
    try:
        body = _filtered_params(
            {
                "query": query.strip(),
                "filterGroups": _parse_json(filter_groups_json, expected=list, label="filter_groups_json"),
                "properties": _split_csv(properties),
                "sorts": _parse_json(sorts_json, expected=list, label="sorts_json"),
                "limit": _limit(limit, default=50),
                "after": after.strip(),
            }
        )
        base_url, headers_or_error = _hubspot_config("hubspot_search_crm_objects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/crm/v3/objects/{quote(object_type, safe='')}/search",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("hubspot_search_crm_objects failed", exc_info=True)
        return f"[Error]: HubSpot CRM object search failed: {e}"


@tool
def hubspot_get_crm_object(
    object_type: str,
    object_id: str,
    properties: str = "",
    id_property: str = "",
    associations: str = "",
    archived: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a HubSpot CRM object by ID or a custom unique property.

    Args:
        object_type: CRM object type.
        object_id: Record ID or unique property value.
        properties: Optional comma-separated properties to return.
        id_property: Optional unique ID property, such as email.
        associations: Optional comma-separated associated object types.
        archived: Include archived records.
    """
    object_type = _crm_object_type(object_type)
    object_id = object_id.strip()
    if not object_type or not object_id:
        return "[Error]: object_type and object_id are required."
    try:
        base_url, headers_or_error = _hubspot_config("hubspot_get_crm_object", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/crm/v3/objects/{quote(object_type, safe='')}/{quote(object_id, safe='')}",
            params={
                "properties": ",".join(_split_csv(properties)),
                "idProperty": id_property.strip(),
                "associations": ",".join(_split_csv(associations)),
                "archived": str(bool(archived)).lower(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("hubspot_get_crm_object failed", exc_info=True)
        return f"[Error]: HubSpot CRM object lookup failed: {e}"


@tool
def hubspot_create_crm_object(
    object_type: str,
    properties_json: str,
    associations_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a HubSpot CRM object.

    Args:
        object_type: CRM object type.
        properties_json: HubSpot properties object as JSON.
        associations_json: Optional HubSpot associations array as JSON.
    """
    object_type = _crm_object_type(object_type)
    if not object_type or not properties_json.strip():
        return "[Error]: object_type and properties_json are required."
    try:
        body = {"properties": _parse_json(properties_json, expected=dict, label="properties_json")}
        associations = _parse_json(associations_json, expected=list, label="associations_json")
        if associations:
            body["associations"] = associations
        base_url, headers_or_error = _hubspot_config("hubspot_create_crm_object", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/crm/v3/objects/{quote(object_type, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("hubspot_create_crm_object failed", exc_info=True)
        return f"[Error]: HubSpot CRM object creation failed: {e}"


@tool
def hubspot_update_crm_object(
    object_type: str,
    object_id: str,
    properties_json: str,
    id_property: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a HubSpot CRM object.

    Args:
        object_type: CRM object type.
        object_id: Record ID or unique property value.
        properties_json: HubSpot properties object as JSON.
        id_property: Optional unique ID property, such as email.
    """
    object_type = _crm_object_type(object_type)
    object_id = object_id.strip()
    if not object_type or not object_id or not properties_json.strip():
        return "[Error]: object_type, object_id, and properties_json are required."
    try:
        base_url, headers_or_error = _hubspot_config("hubspot_update_crm_object", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/crm/v3/objects/{quote(object_type, safe='')}/{quote(object_id, safe='')}",
            params={"idProperty": id_property.strip()},
            json_body={"properties": _parse_json(properties_json, expected=dict, label="properties_json")},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("hubspot_update_crm_object failed", exc_info=True)
        return f"[Error]: HubSpot CRM object update failed: {e}"


@tool
def hubspot_archive_crm_object(
    object_type: str,
    object_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Archive/delete a HubSpot CRM object.

    Args:
        object_type: CRM object type.
        object_id: HubSpot record ID.
    """
    object_type = _crm_object_type(object_type)
    object_id = object_id.strip()
    if not object_type or not object_id:
        return "[Error]: object_type and object_id are required."
    try:
        base_url, headers_or_error = _hubspot_config("hubspot_archive_crm_object", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/crm/v3/objects/{quote(object_type, safe='')}/{quote(object_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("hubspot_archive_crm_object failed", exc_info=True)
        return f"[Error]: HubSpot CRM object archive failed: {e}"


@tool
def zendesk_search(
    query: str,
    sort_by: str = "",
    sort_order: str = "",
    include: str = "",
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Zendesk tickets, users, organizations, and groups.

    Args:
        query: Zendesk search query, such as `type:ticket status:open`.
        sort_by: Optional sort field.
        sort_order: Optional sort order, asc or desc.
        include: Optional comma-separated sideloads.
        page: Result page number.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _zendesk_config("zendesk_search", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/search.json",
            params={
                "query": query.strip(),
                "sort_by": sort_by.strip(),
                "sort_order": sort_order.strip(),
                "include": ",".join(_split_csv(include)),
                "page": max(1, int(page or 1)),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("zendesk_search failed", exc_info=True)
        return f"[Error]: Zendesk search failed: {e}"


@tool
def zendesk_get_ticket(
    ticket_id: str,
    include: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Zendesk ticket by ID.

    Args:
        ticket_id: Zendesk ticket ID.
        include: Optional comma-separated sideloads.
    """
    ticket_id = ticket_id.strip()
    if not ticket_id:
        return "[Error]: ticket_id is required."
    try:
        base_url, headers_or_error = _zendesk_config("zendesk_get_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tickets/{quote(ticket_id, safe='')}.json",
            params={"include": ",".join(_split_csv(include))},
            headers=headers_or_error,
        )
        return _dump_json(data.get("ticket", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("zendesk_get_ticket failed", exc_info=True)
        return f"[Error]: Zendesk ticket lookup failed: {e}"


@tool
def zendesk_list_tickets(
    status: str = "",
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    page: int = 1,
    per_page: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Zendesk tickets, optionally filtered by status.

    Args:
        status: Optional ticket status filter. When set, uses Zendesk search.
        sort_by: Sort field.
        sort_order: Sort order, asc or desc.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    try:
        base_url, headers_or_error = _zendesk_config("zendesk_list_tickets", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if status.strip():
            data = _request_json(
                "GET",
                f"{base_url}/search.json",
                params={
                    "query": f"type:ticket status:{status.strip()}",
                    "sort_by": sort_by.strip() or "updated_at",
                    "sort_order": sort_order.strip() or "desc",
                    "page": max(1, int(page or 1)),
                },
                headers=headers_or_error,
            )
            return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
        data = _request_json(
            "GET",
            f"{base_url}/tickets.json",
            params={
                "sort_by": sort_by.strip() or "updated_at",
                "sort_order": sort_order.strip() or "desc",
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, default=25),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("tickets", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("zendesk_list_tickets failed", exc_info=True)
        return f"[Error]: Zendesk ticket list failed: {e}"


@tool
def zendesk_create_ticket(
    subject: str,
    comment_body: str,
    requester_id: str = "",
    requester_email: str = "",
    priority: str = "",
    status: str = "",
    tags: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Zendesk ticket.

    Args:
        subject: Ticket subject.
        comment_body: Initial comment body.
        requester_id: Optional requester user ID.
        requester_email: Optional requester email when creating/requesting on behalf.
        priority: Optional priority.
        status: Optional status.
        tags: Optional comma-separated tags.
        custom_fields_json: Optional custom_fields array as JSON.
    """
    if not subject.strip() or not comment_body.strip():
        return "[Error]: subject and comment_body are required."
    try:
        ticket = _filtered_params(
            {
                "subject": subject.strip(),
                "comment": {"body": comment_body},
                "requester_id": requester_id.strip(),
                "requester": {"email": requester_email.strip()} if requester_email.strip() else None,
                "priority": priority.strip(),
                "status": status.strip(),
                "tags": _split_csv(tags),
                "custom_fields": _parse_json(custom_fields_json, expected=list, label="custom_fields_json"),
            }
        )
        base_url, headers_or_error = _zendesk_config("zendesk_create_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/tickets.json", json_body={"ticket": ticket}, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("zendesk_create_ticket failed", exc_info=True)
        return f"[Error]: Zendesk ticket creation failed: {e}"


@tool
def zendesk_update_ticket(
    ticket_id: str,
    comment_body: str = "",
    status: str = "",
    priority: str = "",
    assignee_id: str = "",
    tags: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Zendesk ticket.

    Args:
        ticket_id: Zendesk ticket ID.
        comment_body: Optional public comment body.
        status: Optional ticket status.
        priority: Optional priority.
        assignee_id: Optional assignee user ID.
        tags: Optional comma-separated tags.
        custom_fields_json: Optional custom_fields array as JSON.
    """
    ticket_id = ticket_id.strip()
    if not ticket_id:
        return "[Error]: ticket_id is required."
    try:
        ticket = _filtered_params(
            {
                "comment": {"body": comment_body} if comment_body.strip() else None,
                "status": status.strip(),
                "priority": priority.strip(),
                "assignee_id": assignee_id.strip(),
                "tags": _split_csv(tags),
                "custom_fields": _parse_json(custom_fields_json, expected=list, label="custom_fields_json"),
            }
        )
        if not ticket:
            return "[Error]: provide at least one ticket field to update."
        base_url, headers_or_error = _zendesk_config("zendesk_update_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/tickets/{quote(ticket_id, safe='')}.json",
            json_body={"ticket": ticket},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zendesk_update_ticket failed", exc_info=True)
        return f"[Error]: Zendesk ticket update failed: {e}"


@tool
def zendesk_get_user(
    user_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Zendesk user by ID.

    Args:
        user_id: Zendesk user ID.
    """
    user_id = user_id.strip()
    if not user_id:
        return "[Error]: user_id is required."
    try:
        base_url, headers_or_error = _zendesk_config("zendesk_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/users/{quote(user_id, safe='')}.json", headers=headers_or_error)
        return _dump_json(data.get("user", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("zendesk_get_user failed", exc_info=True)
        return f"[Error]: Zendesk user lookup failed: {e}"


@tool
def zendesk_search_users(
    query: str,
    page: int = 1,
    per_page: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Zendesk users.

    Args:
        query: Zendesk user search query.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _zendesk_config("zendesk_search_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/users/search.json",
            params={"query": query.strip(), "page": max(1, int(page or 1)), "per_page": _limit(per_page, default=25)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("users", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("zendesk_search_users failed", exc_info=True)
        return f"[Error]: Zendesk user search failed: {e}"


@tool
def mailchimp_list_audiences(
    count: int = 10,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mailchimp audiences/lists.

    Args:
        count: Number of audiences to return, 1-100.
        offset: Result offset.
    """
    try:
        base_url, headers_or_error = _mailchimp_config("mailchimp_list_audiences", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/lists",
            params={"count": _limit(count, default=10), "offset": max(0, int(offset or 0))},
            headers=headers_or_error,
        )
        return _dump_json(data.get("lists", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailchimp_list_audiences failed", exc_info=True)
        return f"[Error]: Mailchimp audience list failed: {e}"


@tool
def mailchimp_list_members(
    list_id: str,
    status: str = "",
    count: int = 10,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mailchimp audience members.

    Args:
        list_id: Mailchimp audience/list ID.
        status: Optional status filter.
        count: Number of members to return, 1-100.
        offset: Result offset.
    """
    list_id = list_id.strip()
    if not list_id:
        return "[Error]: list_id is required."
    try:
        base_url, headers_or_error = _mailchimp_config("mailchimp_list_members", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/lists/{quote(list_id, safe='')}/members",
            params={"status": status.strip(), "count": _limit(count, default=10), "offset": max(0, int(offset or 0))},
            headers=headers_or_error,
        )
        return _dump_json(data.get("members", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailchimp_list_members failed", exc_info=True)
        return f"[Error]: Mailchimp member list failed: {e}"


@tool
def mailchimp_get_member(
    list_id: str,
    email_or_hash: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Mailchimp audience member by email or subscriber hash.

    Args:
        list_id: Mailchimp audience/list ID.
        email_or_hash: Member email address or MD5 subscriber hash.
    """
    if not list_id.strip() or not email_or_hash.strip():
        return "[Error]: list_id and email_or_hash are required."
    try:
        base_url, headers_or_error = _mailchimp_config("mailchimp_get_member", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/lists/{quote(list_id.strip(), safe='')}/members/{_subscriber_hash(email_or_hash)}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mailchimp_get_member failed", exc_info=True)
        return f"[Error]: Mailchimp member lookup failed: {e}"


@tool
def mailchimp_add_or_update_member(
    list_id: str,
    email: str,
    status_if_new: str = "subscribed",
    status: str = "",
    merge_fields_json: str = "",
    tags: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add or update a Mailchimp audience member.

    Args:
        list_id: Mailchimp audience/list ID.
        email: Member email address.
        status_if_new: Status to use for new members.
        status: Optional status update for existing/new member.
        merge_fields_json: Optional merge fields object as JSON.
        tags: Optional comma-separated tags.
    """
    if not list_id.strip() or not email.strip():
        return "[Error]: list_id and email are required."
    try:
        body = _filtered_params(
            {
                "email_address": email.strip(),
                "status_if_new": status_if_new.strip() or "subscribed",
                "status": status.strip(),
                "merge_fields": _parse_json(merge_fields_json, expected=dict, label="merge_fields_json"),
                "tags": _split_csv(tags),
            }
        )
        base_url, headers_or_error = _mailchimp_config("mailchimp_add_or_update_member", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/lists/{quote(list_id.strip(), safe='')}/members/{_subscriber_hash(email)}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mailchimp_add_or_update_member failed", exc_info=True)
        return f"[Error]: Mailchimp member add/update failed: {e}"


@tool
def mailchimp_update_member_tags(
    list_id: str,
    email_or_hash: str,
    tags: str,
    active: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add or remove Mailchimp member tags.

    Args:
        list_id: Mailchimp audience/list ID.
        email_or_hash: Member email address or MD5 subscriber hash.
        tags: Comma-separated tag names.
        active: True to apply tags, false to remove them.
    """
    tag_list = _split_csv(tags)
    if not list_id.strip() or not email_or_hash.strip() or not tag_list:
        return "[Error]: list_id, email_or_hash, and tags are required."
    try:
        body = {"tags": [{"name": tag, "status": "active" if active else "inactive"} for tag in tag_list]}
        base_url, headers_or_error = _mailchimp_config("mailchimp_update_member_tags", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/lists/{quote(list_id.strip(), safe='')}/members/{_subscriber_hash(email_or_hash)}/tags",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mailchimp_update_member_tags failed", exc_info=True)
        return f"[Error]: Mailchimp member tag update failed: {e}"


@tool
def mailchimp_list_campaigns(
    campaign_type: str = "",
    status: str = "",
    count: int = 10,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Mailchimp campaigns.

    Args:
        campaign_type: Optional campaign type filter.
        status: Optional campaign status filter.
        count: Number of campaigns to return, 1-100.
        offset: Result offset.
    """
    try:
        base_url, headers_or_error = _mailchimp_config("mailchimp_list_campaigns", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/campaigns",
            params={
                "type": campaign_type.strip(),
                "status": status.strip(),
                "count": _limit(count, default=10),
                "offset": max(0, int(offset or 0)),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("campaigns", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("mailchimp_list_campaigns failed", exc_info=True)
        return f"[Error]: Mailchimp campaign list failed: {e}"


CUSTOMER_ENGAGEMENT_SERVICE_TOOLS = [
    hubspot_list_crm_objects,
    hubspot_search_crm_objects,
    hubspot_get_crm_object,
    hubspot_create_crm_object,
    hubspot_update_crm_object,
    hubspot_archive_crm_object,
    zendesk_search,
    zendesk_get_ticket,
    zendesk_list_tickets,
    zendesk_create_ticket,
    zendesk_update_ticket,
    zendesk_get_user,
    zendesk_search_users,
    mailchimp_list_audiences,
    mailchimp_list_members,
    mailchimp_get_member,
    mailchimp_add_or_update_member,
    mailchimp_update_member_tags,
    mailchimp_list_campaigns,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="customer_engagement", tools=tuple(CUSTOMER_ENGAGEMENT_SERVICE_TOOLS)))
