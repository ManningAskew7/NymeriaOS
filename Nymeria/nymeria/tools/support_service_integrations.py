"""Support and customer messaging service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import logging
import re
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
_HELPSCOUT_BASE_URL = "https://api.helpscout.net/v2"
_INTERCOM_BASE_URL = "https://api.intercom.io"
_INTERCOM_VERSION = "2.11"
_DRIFT_BASE_URL = "https://driftapi.com"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
#
# The "freshworks" alias is shared by freshdesk, freshservice, and
# freshworks_crm (sales_crm module); the registry tolerates the cross-provider
# alias overlap (canonical owner wins the name index).
_FRESHDESK = register_provider_spec(
    ProviderCredentialSpec(
        provider="freshdesk",
        aliases=("freshdesk_api", "freshworks"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="domain", names=("domain", "subdomain")),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "domain"),
        env_var="FRESHDESK_API_KEY",
        display_name="Freshdesk",
    )
)

_FRESHSERVICE = register_provider_spec(
    ProviderCredentialSpec(
        provider="freshservice",
        aliases=("freshservice_api", "freshworks"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="domain", names=("domain", "subdomain")),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "domain"),
        env_var="FRESHSERVICE_API_KEY",
        display_name="Freshservice",
    )
)

_SERVICENOW = register_provider_spec(
    ProviderCredentialSpec(
        provider="servicenow",
        aliases=("service_now", "service_now_basic", "service_now_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "instance_url", "instanceUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="instance", names=("instance", "subdomain"), required=False),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "bearer_token", "token", "value"),
            ),
            CredentialFieldGroup(role="username", names=("username", "user"), required=False),
            CredentialFieldGroup(
                role="password", names=("password", "api_password", "apiPassword"), required=False
            ),
        ),
        hint_fields=("access_token", "base_url"),
        env_var="SERVICENOW_ACCESS_TOKEN or SERVICENOW_USERNAME + SERVICENOW_PASSWORD",
        display_name="ServiceNow",
    )
)

_ZAMMAD = register_provider_spec(
    ProviderCredentialSpec(
        provider="zammad",
        aliases=("zammad_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url")),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "api_token", "apiToken", "token", "value"),
            ),
            CredentialFieldGroup(role="username", names=("username", "email"), required=False),
            CredentialFieldGroup(
                role="password", names=("password", "api_password", "apiPassword"), required=False
            ),
        ),
        hint_fields=("token", "base_url"),
        env_var="ZAMMAD_TOKEN or ZAMMAD_USERNAME + ZAMMAD_PASSWORD",
        display_name="Zammad",
    )
)

_HELPSCOUT = register_provider_spec(
    ProviderCredentialSpec(
        provider="helpscout",
        aliases=("help_scout", "helpscout_oauth2"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="token", names=("access_token", "token", "value")),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="HELPSCOUT_ACCESS_TOKEN",
        display_name="Help Scout",
    )
)

_INTERCOM = register_provider_spec(
    ProviderCredentialSpec(
        provider="intercom",
        aliases=("intercom_api", "intercom_oauth2"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="version", names=("intercom_version", "version"), required=False
            ),
            CredentialFieldGroup(role="token", names=("access_token", "api_key", "token", "value")),
        ),
        hint_fields=("access_token", "api_key", "token", "value"),
        env_var="INTERCOM_ACCESS_TOKEN",
        display_name="Intercom",
    )
)

_DRIFT = register_provider_spec(
    ProviderCredentialSpec(
        provider="drift",
        aliases=("drift_api", "drift_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
            ),
        ),
        hint_fields=("access_token", "accessToken", "api_key", "token", "value"),
        env_var="DRIFT_ACCESS_TOKEN",
        display_name="Drift",
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
            provider=_FRESHDESK.provider,
            provider_aliases=_FRESHDESK.aliases,
            field_names=_FRESHDESK.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshdesk_base_url")
    )
    domain = (
        _credential_value(
            provider=_FRESHDESK.provider,
            provider_aliases=_FRESHDESK.aliases,
            field_names=_FRESHDESK.group("domain"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshdesk_domain")
    )
    api_key = _credential_value(
        provider=_FRESHDESK.provider,
        provider_aliases=_FRESHDESK.aliases,
        field_names=_FRESHDESK.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("freshdesk_api_key")
    if not base and domain:
        base = f"https://{_vendor_host(domain, vendor_suffix='.freshdesk.com', provider=_FRESHDESK.provider, field='domain')}/api/v2"
    if not base:
        return "", (
            "[Error]: No Freshdesk base URL found. Save a Freshdesk credential with "
            '"base_url" or "domain", or set FRESHDESK_BASE_URL or FRESHDESK_DOMAIN.'
        )
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_FRESHDESK.provider,
            field_names=_FRESHDESK.hint_fields,
            tool_name=tool_name,
            env_var=_FRESHDESK.env_var,
            display_name=_FRESHDESK.display_name,
        )
    auth = base64.b64encode(f"{api_key}:X".encode()).decode()
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _freshservice_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_FRESHSERVICE.provider,
            provider_aliases=_FRESHSERVICE.aliases,
            field_names=_FRESHSERVICE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshservice_base_url")
    )
    domain = (
        _credential_value(
            provider=_FRESHSERVICE.provider,
            provider_aliases=_FRESHSERVICE.aliases,
            field_names=_FRESHSERVICE.group("domain"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("freshservice_domain")
    )
    api_key = _credential_value(
        provider=_FRESHSERVICE.provider,
        provider_aliases=_FRESHSERVICE.aliases,
        field_names=_FRESHSERVICE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("freshservice_api_key")
    if not base and domain:
        base = f"https://{_vendor_host(domain, vendor_suffix='.freshservice.com', provider=_FRESHSERVICE.provider, field='domain')}/api/v2"
    if not base:
        return "", (
            "[Error]: No Freshservice base URL found. Save a Freshservice credential with "
            '"base_url" or "domain", or set FRESHSERVICE_BASE_URL or FRESHSERVICE_DOMAIN.'
        )
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_FRESHSERVICE.provider,
            field_names=_FRESHSERVICE.hint_fields,
            tool_name=tool_name,
            env_var=_FRESHSERVICE.env_var,
            display_name=_FRESHSERVICE.display_name,
        )
    auth = base64.b64encode(f"{api_key}:X".encode()).decode()
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _servicenow_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_from_vault = _credential_value(
        provider=_SERVICENOW.provider,
        provider_aliases=_SERVICENOW.aliases,
        field_names=_SERVICENOW.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value("servicenow_base_url")
    instance = (
        _credential_value(
            provider=_SERVICENOW.provider,
            provider_aliases=_SERVICENOW.aliases,
            field_names=_SERVICENOW.group("instance"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("servicenow_instance")
    )
    token_from_vault = _credential_value(
        provider=_SERVICENOW.provider,
        provider_aliases=_SERVICENOW.aliases,
        field_names=_SERVICENOW.group("token"),
        tool_name=tool_name,
        config=config,
    )
    token = token_from_vault or _settings_value("servicenow_access_token")
    username = _credential_value(
        provider=_SERVICENOW.provider,
        provider_aliases=_SERVICENOW.aliases,
        field_names=_SERVICENOW.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("servicenow_username")
    password_from_vault = _credential_value(
        provider=_SERVICENOW.provider,
        provider_aliases=_SERVICENOW.aliases,
        field_names=_SERVICENOW.group("password"),
        tool_name=tool_name,
        config=config,
    )
    password = password_from_vault or _settings_value("servicenow_password")
    if not base and instance:
        base = f"https://{_vendor_host(instance, vendor_suffix='.service-now.com', provider=_SERVICENOW.provider, field='instance')}/api/now"
    if not base:
        return "", (
            "[Error]: No ServiceNow base URL found. Save a ServiceNow credential with "
            '"base_url" or "instance", or set SERVICENOW_BASE_URL or SERVICENOW_INSTANCE.'
        )
    base = _base_url(base)
    if not base.endswith("/api/now"):
        base = f"{base}/api/now"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request: a record holding base_url + password clears slice B's "some
    # anchor", and the bearer branch would then send the operator's access token
    # to the address that record chose.
    if token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=token_from_vault,
            secret=token,
            provider=_SERVICENOW.provider,
        )
        headers["Authorization"] = f"Bearer {token}"
        return base, headers
    if username and password:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=password_from_vault,
            secret=password,
            provider=_SERVICENOW.provider,
        )
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
        return base, headers
    return base, _setup_hint(
        provider=_SERVICENOW.provider,
        field_names=_SERVICENOW.hint_fields,
        tool_name=tool_name,
        env_var=_SERVICENOW.env_var,
        display_name=_SERVICENOW.display_name,
    )


def _zammad_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_from_vault = _credential_value(
        provider=_ZAMMAD.provider,
        provider_aliases=_ZAMMAD.aliases,
        field_names=_ZAMMAD.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value("zammad_base_url")
    token_from_vault = _credential_value(
        provider=_ZAMMAD.provider,
        provider_aliases=_ZAMMAD.aliases,
        field_names=_ZAMMAD.group("token"),
        tool_name=tool_name,
        config=config,
    )
    token = token_from_vault or _settings_value("zammad_token")
    username = _credential_value(
        provider=_ZAMMAD.provider,
        provider_aliases=_ZAMMAD.aliases,
        field_names=_ZAMMAD.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("zammad_username")
    password_from_vault = _credential_value(
        provider=_ZAMMAD.provider,
        provider_aliases=_ZAMMAD.aliases,
        field_names=_ZAMMAD.group("password"),
        tool_name=tool_name,
        config=config,
    )
    password = password_from_vault or _settings_value("zammad_password")
    if not base:
        return "", (
            '[Error]: No Zammad base URL found. Save a Zammad credential with "base_url", '
            "or set ZAMMAD_BASE_URL."
        )
    base = _base_url(base)
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request: a record holding base_url + password clears slice B's "some
    # anchor", and the token branch would then send the operator's token to the
    # address that record chose.
    if token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=token_from_vault,
            secret=token,
            provider=_ZAMMAD.provider,
        )
        headers["Authorization"] = f"Token token={token}"
        return base, headers
    if username and password:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=password_from_vault,
            secret=password,
            provider=_ZAMMAD.provider,
        )
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
        return base, headers
    return base, _setup_hint(
        provider=_ZAMMAD.provider,
        field_names=_ZAMMAD.hint_fields,
        tool_name=tool_name,
        env_var=_ZAMMAD.env_var,
        display_name=_ZAMMAD.display_name,
    )


def _safe_table_name(value: str, *, label: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", cleaned):
        raise ValueError(f"{label} may only contain letters, numbers, and underscores")
    return cleaned


def _zammad_collection(resource: str) -> str:
    mapping = {
        "ticket": "tickets",
        "tickets": "tickets",
        "user": "users",
        "users": "users",
        "organization": "organizations",
        "organizations": "organizations",
        "group": "groups",
        "groups": "groups",
    }
    key = resource.strip().lower()
    if key not in mapping:
        raise ValueError("resource must be ticket, user, organization, or group")
    return mapping[key]


def _helpscout_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_HELPSCOUT.provider,
            provider_aliases=_HELPSCOUT.aliases,
            field_names=_HELPSCOUT.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("helpscout_base_url")
        or _HELPSCOUT_BASE_URL
    )
    token = _credential_value(
        provider=_HELPSCOUT.provider,
        provider_aliases=_HELPSCOUT.aliases,
        field_names=_HELPSCOUT.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("helpscout_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_HELPSCOUT.provider,
            field_names=_HELPSCOUT.hint_fields,
            tool_name=tool_name,
            env_var=_HELPSCOUT.env_var,
            display_name=_HELPSCOUT.display_name,
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
            provider=_INTERCOM.provider,
            provider_aliases=_INTERCOM.aliases,
            field_names=_INTERCOM.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("intercom_base_url")
        or _INTERCOM_BASE_URL
    )
    version = (
        _credential_value(
            provider=_INTERCOM.provider,
            provider_aliases=_INTERCOM.aliases,
            field_names=_INTERCOM.group("version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("intercom_version")
        or _INTERCOM_VERSION
    )
    token = _credential_value(
        provider=_INTERCOM.provider,
        provider_aliases=_INTERCOM.aliases,
        field_names=_INTERCOM.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("intercom_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_INTERCOM.provider,
            field_names=_INTERCOM.hint_fields,
            tool_name=tool_name,
            env_var=_INTERCOM.env_var,
            display_name=_INTERCOM.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Intercom-Version": version,
        "User-Agent": "Nymeria",
    }


def _drift_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_DRIFT.provider,
            provider_aliases=_DRIFT.aliases,
            field_names=_DRIFT.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("drift_base_url")
        or _DRIFT_BASE_URL
    )
    token = _credential_value(
        provider=_DRIFT.provider,
        provider_aliases=_DRIFT.aliases,
        field_names=_DRIFT.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("drift_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_DRIFT.provider,
            field_names=_DRIFT.hint_fields,
            tool_name=tool_name,
            env_var=_DRIFT.env_var,
            display_name=_DRIFT.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _drift_payload(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


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
def freshservice_list_tickets(
    email: str = "",
    requester_id: str = "",
    updated_since: str = "",
    filter_name: str = "",
    include: str = "",
    page: int = 1,
    per_page: int = 30,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Freshservice tickets.

    Args:
        email: Optional requester email filter.
        requester_id: Optional requester ID filter.
        updated_since: Optional ISO date/time filter for recently updated tickets.
        filter_name: Optional Freshservice ticket filter name.
        include: Optional comma-separated embeds.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    try:
        base_url, headers_or_error = _freshservice_config("freshservice_list_tickets", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tickets",
            params={
                "email": email.strip(),
                "requester_id": requester_id.strip(),
                "updated_since": updated_since.strip(),
                "filter": filter_name.strip(),
                "include": ",".join(_split_csv(include)),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, default=30),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshservice_list_tickets failed", exc_info=True)
        return f"[Error]: Freshservice ticket list failed: {e}"


@tool
def freshservice_get_ticket(
    ticket_id: str,
    include: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Freshservice ticket by ID.

    Args:
        ticket_id: Freshservice ticket ID.
        include: Optional comma-separated embeds.
    """
    ticket_id = ticket_id.strip()
    if not ticket_id:
        return "[Error]: ticket_id is required."
    try:
        base_url, headers_or_error = _freshservice_config("freshservice_get_ticket", config)
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
        logger.error("freshservice_get_ticket failed", exc_info=True)
        return f"[Error]: Freshservice ticket lookup failed: {e}"


@tool
def freshservice_create_ticket(
    subject: str,
    description: str,
    email: str = "",
    requester_id: str = "",
    priority: int = 1,
    status: int = 2,
    urgency: int = 1,
    impact: int = 1,
    category: str = "",
    sub_category: str = "",
    item_category: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Freshservice ticket.

    Args:
        subject: Ticket subject.
        description: Ticket description text or HTML.
        email: Requester email. Required when requester_id is not supplied.
        requester_id: Existing requester ID.
        priority: Freshservice priority value.
        status: Freshservice status value.
        urgency: Freshservice urgency value.
        impact: Freshservice impact value.
        category: Optional category.
        sub_category: Optional sub-category.
        item_category: Optional item category.
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
                "urgency": int(urgency),
                "impact": int(impact),
                "category": category.strip(),
                "sub_category": sub_category.strip(),
                "item_category": item_category.strip(),
                "custom_fields": _parse_json(custom_fields_json, expected=dict, label="custom_fields_json"),
            }
        )
        base_url, headers_or_error = _freshservice_config("freshservice_create_ticket", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/tickets", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("freshservice_create_ticket failed", exc_info=True)
        return f"[Error]: Freshservice ticket creation failed: {e}"


@tool
def freshservice_update_ticket(
    ticket_id: str,
    subject: str = "",
    description: str = "",
    priority: int = 0,
    status: int = 0,
    urgency: int = 0,
    impact: int = 0,
    category: str = "",
    sub_category: str = "",
    item_category: str = "",
    custom_fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Freshservice ticket.

    Args:
        ticket_id: Freshservice ticket ID.
        subject: Optional updated subject.
        description: Optional updated description.
        priority: Optional Freshservice priority value.
        status: Optional Freshservice status value.
        urgency: Optional urgency value.
        impact: Optional impact value.
        category: Optional category.
        sub_category: Optional sub-category.
        item_category: Optional item category.
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
                "urgency": int(urgency) if urgency else None,
                "impact": int(impact) if impact else None,
                "category": category.strip(),
                "sub_category": sub_category.strip(),
                "item_category": item_category.strip(),
                "custom_fields": _parse_json(custom_fields_json, expected=dict, label="custom_fields_json"),
            }
        )
        if not body:
            return "[Error]: provide at least one ticket field to update."
        base_url, headers_or_error = _freshservice_config("freshservice_update_ticket", config)
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
        logger.error("freshservice_update_ticket failed", exc_info=True)
        return f"[Error]: Freshservice ticket update failed: {e}"


@tool
def freshservice_list_requesters(
    email: str = "",
    mobile_phone_number: str = "",
    query: str = "",
    page: int = 1,
    per_page: int = 30,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Freshservice requesters.

    Args:
        email: Optional requester email filter.
        mobile_phone_number: Optional requester mobile phone filter.
        query: Optional search query.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    try:
        base_url, headers_or_error = _freshservice_config("freshservice_list_requesters", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/requesters",
            params={
                "email": email.strip(),
                "mobile_phone_number": mobile_phone_number.strip(),
                "query": query.strip(),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, default=30),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshservice_list_requesters failed", exc_info=True)
        return f"[Error]: Freshservice requester list failed: {e}"


@tool
def freshservice_get_requester(
    requester_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Freshservice requester by ID.

    Args:
        requester_id: Freshservice requester ID.
    """
    requester_id = requester_id.strip()
    if not requester_id:
        return "[Error]: requester_id is required."
    try:
        base_url, headers_or_error = _freshservice_config("freshservice_get_requester", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/requesters/{quote(requester_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("freshservice_get_requester failed", exc_info=True)
        return f"[Error]: Freshservice requester lookup failed: {e}"


@tool
def servicenow_list_records(
    table: str,
    query: str = "",
    fields: str = "",
    limit: int = 25,
    offset: int = 0,
    display_value: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ServiceNow table records.

    Args:
        table: ServiceNow table name, such as incident or sys_user.
        query: Optional sysparm_query filter.
        fields: Optional comma-separated sysparm_fields.
        limit: Maximum records to request, 1-100.
        offset: Result offset.
        display_value: Whether ServiceNow should return display values.
    """
    try:
        table_name = _safe_table_name(table, label="table")
        base_url, headers_or_error = _servicenow_config("servicenow_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/table/{quote(table_name, safe='')}",
            params={
                "sysparm_query": query.strip(),
                "sysparm_fields": ",".join(_split_csv(fields)),
                "sysparm_limit": _limit(limit, default=25),
                "sysparm_offset": max(0, int(offset or 0)),
                "sysparm_display_value": str(bool(display_value)).lower(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("servicenow_list_records failed", exc_info=True)
        return f"[Error]: ServiceNow record list failed: {e}"


@tool
def servicenow_get_record(
    table: str,
    sys_id: str,
    fields: str = "",
    display_value: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a ServiceNow table record by sys_id.

    Args:
        table: ServiceNow table name, such as incident or sys_user.
        sys_id: Record sys_id.
        fields: Optional comma-separated sysparm_fields.
        display_value: Whether ServiceNow should return display values.
    """
    if not sys_id.strip():
        return "[Error]: sys_id is required."
    try:
        table_name = _safe_table_name(table, label="table")
        base_url, headers_or_error = _servicenow_config("servicenow_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/table/{quote(table_name, safe='')}/{quote(sys_id.strip(), safe='')}",
            params={
                "sysparm_fields": ",".join(_split_csv(fields)),
                "sysparm_display_value": str(bool(display_value)).lower(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("servicenow_get_record failed", exc_info=True)
        return f"[Error]: ServiceNow record lookup failed: {e}"


@tool
def servicenow_create_record(
    table: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a ServiceNow table record.

    Args:
        table: ServiceNow table name, such as incident or sys_user.
        fields_json: Record field object as JSON.
    """
    try:
        table_name = _safe_table_name(table, label="table")
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        if not body:
            return "[Error]: fields_json must include at least one field."
        base_url, headers_or_error = _servicenow_config("servicenow_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/table/{quote(table_name, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("servicenow_create_record failed", exc_info=True)
        return f"[Error]: ServiceNow record creation failed: {e}"


@tool
def servicenow_update_record(
    table: str,
    sys_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a ServiceNow table record.

    Args:
        table: ServiceNow table name, such as incident or sys_user.
        sys_id: Record sys_id.
        fields_json: Record field object as JSON.
    """
    if not sys_id.strip():
        return "[Error]: sys_id is required."
    try:
        table_name = _safe_table_name(table, label="table")
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        if not body:
            return "[Error]: fields_json must include at least one field."
        base_url, headers_or_error = _servicenow_config("servicenow_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/table/{quote(table_name, safe='')}/{quote(sys_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("servicenow_update_record failed", exc_info=True)
        return f"[Error]: ServiceNow record update failed: {e}"


@tool
def servicenow_delete_record(
    table: str,
    sys_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a ServiceNow table record.

    Args:
        table: ServiceNow table name, such as incident or sys_user.
        sys_id: Record sys_id.
    """
    if not sys_id.strip():
        return "[Error]: sys_id is required."
    try:
        table_name = _safe_table_name(table, label="table")
        base_url, headers_or_error = _servicenow_config("servicenow_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/table/{quote(table_name, safe='')}/{quote(sys_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("servicenow_delete_record failed", exc_info=True)
        return f"[Error]: ServiceNow record deletion failed: {e}"


@tool
def zammad_list_records(
    resource: str,
    query: str = "",
    page: int = 1,
    per_page: int = 30,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List or search Zammad records.

    Args:
        resource: ticket, user, organization, or group.
        query: Optional search query. When set, uses the resource search endpoint.
        page: Result page number.
        per_page: Results per page, 1-100.
    """
    try:
        collection = _zammad_collection(resource)
        base_url, headers_or_error = _zammad_config("zammad_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        path = f"{base_url}/{collection}/search" if query.strip() else f"{base_url}/{collection}"
        data = _request_json(
            "GET",
            path,
            params={
                "query": query.strip(),
                "page": max(1, int(page or 1)),
                "per_page": _limit(per_page, default=30),
                "limit": _limit(per_page, default=30),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zammad_list_records failed", exc_info=True)
        return f"[Error]: Zammad record list failed: {e}"


@tool
def zammad_get_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Zammad record by ID.

    Args:
        resource: ticket, user, organization, or group.
        record_id: Zammad record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        collection = _zammad_collection(resource)
        base_url, headers_or_error = _zammad_config("zammad_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{collection}/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zammad_get_record failed", exc_info=True)
        return f"[Error]: Zammad record lookup failed: {e}"


@tool
def zammad_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Zammad record.

    Args:
        resource: ticket, user, organization, or group.
        fields_json: Record field object as JSON.
    """
    try:
        collection = _zammad_collection(resource)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        if not body:
            return "[Error]: fields_json must include at least one field."
        base_url, headers_or_error = _zammad_config("zammad_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/{collection}", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("zammad_create_record failed", exc_info=True)
        return f"[Error]: Zammad record creation failed: {e}"


@tool
def zammad_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Zammad record.

    Args:
        resource: ticket, user, organization, or group.
        record_id: Zammad record ID.
        fields_json: Record field object as JSON.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        collection = _zammad_collection(resource)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        if not body:
            return "[Error]: fields_json must include at least one field."
        base_url, headers_or_error = _zammad_config("zammad_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/{collection}/{quote(record_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("zammad_update_record failed", exc_info=True)
        return f"[Error]: Zammad record update failed: {e}"


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


@tool
def drift_get_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Drift contact by ID.

    Args:
        contact_id: Drift contact ID.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _drift_config("drift_get_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/contacts/{quote(contact_id, safe='')}", headers=headers_or_error)
        return _dump_json(_drift_payload(data))
    except Exception as e:
        logger.error("drift_get_contact failed", exc_info=True)
        return f"[Error]: Drift contact lookup failed: {e}"


@tool
def drift_create_contact(
    email: str,
    name: str = "",
    phone: str = "",
    attributes_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Drift contact.

    Args:
        email: Contact email address.
        name: Optional contact name.
        phone: Optional phone number.
        attributes_json: Optional JSON object of additional Drift contact attributes.
    """
    if not email.strip():
        return "[Error]: email is required."
    try:
        attributes = _filtered_params(
            {
                "email": email.strip(),
                "name": name.strip(),
                "phone": phone.strip(),
            }
        )
        attributes.update(_parse_json(attributes_json, expected=dict, label="attributes_json"))
        base_url, headers_or_error = _drift_config("drift_create_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/contacts",
            json_body={"attributes": attributes},
            headers=headers_or_error,
        )
        return _dump_json(_drift_payload(data))
    except Exception as e:
        logger.error("drift_create_contact failed", exc_info=True)
        return f"[Error]: Drift contact creation failed: {e}"


@tool
def drift_update_contact(
    contact_id: str,
    email: str = "",
    name: str = "",
    phone: str = "",
    attributes_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Drift contact.

    Args:
        contact_id: Drift contact ID.
        email: Optional replacement email address.
        name: Optional replacement contact name.
        phone: Optional replacement phone number.
        attributes_json: Optional JSON object of additional Drift contact attributes.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        attributes = _filtered_params(
            {
                "email": email.strip(),
                "name": name.strip(),
                "phone": phone.strip(),
            }
        )
        attributes.update(_parse_json(attributes_json, expected=dict, label="attributes_json"))
        if not attributes:
            return "[Error]: provide at least one contact field to update."
        base_url, headers_or_error = _drift_config("drift_update_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/contacts/{quote(contact_id, safe='')}",
            json_body={"attributes": attributes},
            headers=headers_or_error,
        )
        return _dump_json(_drift_payload(data))
    except Exception as e:
        logger.error("drift_update_contact failed", exc_info=True)
        return f"[Error]: Drift contact update failed: {e}"


@tool
def drift_delete_contact(
    contact_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Drift contact.

    Args:
        contact_id: Drift contact ID.
    """
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        base_url, headers_or_error = _drift_config("drift_delete_contact", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/contacts/{quote(contact_id, safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("drift_delete_contact failed", exc_info=True)
        return f"[Error]: Drift contact deletion failed: {e}"


@tool
def drift_list_contact_attributes(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List custom contact attributes configured in Drift."""
    try:
        base_url, headers_or_error = _drift_config("drift_list_contact_attributes", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/contacts/attributes", headers=headers_or_error)
        payload = _drift_payload(data)
        if isinstance(payload, dict) and "properties" in payload:
            payload = payload["properties"]
        return _dump_json(payload)
    except Exception as e:
        logger.error("drift_list_contact_attributes failed", exc_info=True)
        return f"[Error]: Drift contact attribute listing failed: {e}"


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
    freshservice_list_tickets,
    freshservice_get_ticket,
    freshservice_create_ticket,
    freshservice_update_ticket,
    freshservice_list_requesters,
    freshservice_get_requester,
    servicenow_list_records,
    servicenow_get_record,
    servicenow_create_record,
    servicenow_update_record,
    servicenow_delete_record,
    zammad_list_records,
    zammad_get_record,
    zammad_create_record,
    zammad_update_record,
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
    drift_get_contact,
    drift_create_contact,
    drift_update_contact,
    drift_delete_contact,
    drift_list_contact_attributes,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="support", tools=tuple(SUPPORT_SERVICE_TOOLS)))
