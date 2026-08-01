"""Marketing and contact-list service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import json
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
    basic_auth as _basic_auth,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    json_object as _json_object,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL = "https://example.api-us1.com"
_CONVERTKIT_BASE_URL = "https://api.convertkit.com/v3"
_GETRESPONSE_BASE_URL = "https://api.getresponse.com/v3"
_MAILERLITE_BASE_URL = "https://connect.mailerlite.com/api"
_MAILERLITE_CLASSIC_BASE_URL = "https://api.mailerlite.com/api/v2"
_CUSTOMERIO_TRACK_BASE_URL = "https://track.customer.io/api/v1"
_CUSTOMERIO_TRACK_EU_BASE_URL = "https://track-eu.customer.io/api/v1"
_CUSTOMERIO_APP_BASE_URL = "https://api.customer.io/v1"
_CUSTOMERIO_APP_EU_BASE_URL = "https://api-eu.customer.io/v1"
_ITERABLE_BASE_URL = "https://api.iterable.com/api"
_POSTHOG_BASE_URL = "https://app.posthog.com"
_SEGMENT_BASE_URL = "https://api.segment.io/v1"
_ACTIONNETWORK_BASE_URL = "https://actionnetwork.org/api/v2"
_AUTOPILOT_BASE_URL = "https://api2.autopilothq.com/v1"
_EGOI_BASE_URL = "https://api.egoiapp.com"
_VERO_BASE_URL = "https://api.getvero.com/api/v2"
_LEMLIST_BASE_URL = "https://api.lemlist.com/api"
_EMELIA_GRAPHQL_URL = "https://graphql.emelia.io/graphql"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered. The five
# token-config providers (actionnetwork, autopilot, egoi, lemlist, vero) share
# the base_url tuple that stays inline in _token_config; each spec still
# declares that group.
_ACTIONNETWORK = register_provider_spec(
    ProviderCredentialSpec(
        provider="actionnetwork",
        aliases=("action_network", "actionnetwork_api", "action_network_api"),
        groups=(
            CredentialFieldGroup(role="token", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "apiKey", "token", "value"),
        env_var="ACTIONNETWORK_API_KEY",
        display_name="Action Network",
    )
)

_ACTIVECAMPAIGN = register_provider_spec(
    ProviderCredentialSpec(
        provider="activecampaign",
        aliases=("active_campaign", "activecampaign_api", "active_campaign_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("api_url", "apiUrl", "base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "api_url"),
        env_var="ACTIVECAMPAIGN_API_KEY and ACTIVECAMPAIGN_BASE_URL",
        display_name="ActiveCampaign",
    )
)

_AUTOPILOT = register_provider_spec(
    ProviderCredentialSpec(
        provider="autopilot",
        aliases=("autopilot_api",),
        groups=(
            CredentialFieldGroup(role="token", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "apiKey", "token", "value"),
        env_var="AUTOPILOT_API_KEY",
        display_name="Autopilot",
    )
)

_CONVERTKIT = register_provider_spec(
    ProviderCredentialSpec(
        provider="convertkit",
        aliases=("convert_kit", "convertkit_api", "kit"),
        groups=(
            CredentialFieldGroup(
                role="secret",
                names=("api_secret", "apiSecret", "secret", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_secret", "value"),
        env_var="CONVERTKIT_API_SECRET",
        display_name="ConvertKit",
    )
)

# Branch-variant provider: two credential kinds (app + tracking), each with its
# own required fields, env var, and setup hint. The spec declares all six lookup
# groups; hint_fields/env_var carry the tracking variant, and _customerio_auth_error
# keeps the app-branch literals inline (see below).
_CUSTOMERIO = register_provider_spec(
    ProviderCredentialSpec(
        provider="customerio",
        aliases=("customer_io", "customerio_api", "customer_io_api"),
        groups=(
            CredentialFieldGroup(
                role="region",
                names=("region", "tracking_region", "trackingRegion"),
                required=False,
            ),
            CredentialFieldGroup(
                role="tracking_base_url",
                names=("tracking_base_url", "trackingBaseUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="app_base_url",
                names=("app_base_url", "appBaseUrl", "base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="tracking_site_id",
                names=("tracking_site_id", "trackingSiteId", "site_id", "siteId"),
            ),
            CredentialFieldGroup(
                role="tracking_api_key",
                names=("tracking_api_key", "trackingApiKey", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="app_api_key",
                names=("app_api_key", "appApiKey", "access_token", "accessToken"),
            ),
        ),
        hint_fields=("tracking_site_id", "tracking_api_key"),
        env_var="CUSTOMERIO_TRACKING_SITE_ID and CUSTOMERIO_TRACKING_API_KEY",
        display_name="Customer.io",
    )
)

_EGOI = register_provider_spec(
    ProviderCredentialSpec(
        provider="egoi",
        aliases=("e_goi", "egoi_api", "e_goi_api"),
        groups=(
            CredentialFieldGroup(role="token", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "apiKey", "token", "value"),
        env_var="EGOI_API_KEY",
        display_name="E-goi",
    )
)

_EMELIA = register_provider_spec(
    ProviderCredentialSpec(
        provider="emelia",
        aliases=("emelia_api",),
        groups=(
            CredentialFieldGroup(role="token", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("graphql_url", "graphqlUrl", "base_url", "baseUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="EMELIA_API_KEY",
        display_name="Emelia",
    )
)

_GETRESPONSE = register_provider_spec(
    ProviderCredentialSpec(
        provider="getresponse",
        aliases=("get_response", "getresponse_api", "get_response_api"),
        groups=(
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "access_token", "value"),
        env_var="GETRESPONSE_API_KEY",
        display_name="GetResponse",
    )
)

_ITERABLE = register_provider_spec(
    ProviderCredentialSpec(
        provider="iterable",
        aliases=("iterable_api",),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "region", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="ITERABLE_API_KEY",
        display_name="Iterable",
    )
)

_LEMLIST = register_provider_spec(
    ProviderCredentialSpec(
        provider="lemlist",
        aliases=("lemlist_api",),
        groups=(
            CredentialFieldGroup(role="token", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "apiKey", "token", "value"),
        env_var="LEMLIST_API_KEY",
        display_name="Lemlist",
    )
)

_MAILERLITE = register_provider_spec(
    ProviderCredentialSpec(
        provider="mailerlite",
        aliases=("mailer_lite", "mailerlite_api", "mailer_lite_api"),
        groups=(
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="classic_api",
                names=("classic_api", "classicApi"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="MAILERLITE_API_KEY",
        display_name="MailerLite",
    )
)

_MAUTIC = register_provider_spec(
    ProviderCredentialSpec(
        provider="mautic",
        aliases=("mautic_api", "mautic_oauth2"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(role="username", names=("username", "user", "email")),
            CredentialFieldGroup(role="password", names=("password", "api_password", "apiPassword")),
        ),
        hint_fields=("access_token", "username", "password"),
        env_var="MAUTIC_ACCESS_TOKEN or MAUTIC_USERNAME + MAUTIC_PASSWORD",
        display_name="Mautic",
    )
)

_POSTHOG = register_provider_spec(
    ProviderCredentialSpec(
        provider="posthog",
        aliases=("posthog_api", "post_hog"),
        groups=(
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "project_api_key", "projectApiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "host"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "project_api_key", "value"),
        env_var="POSTHOG_API_KEY",
        display_name="PostHog",
    )
)

_SEGMENT = register_provider_spec(
    ProviderCredentialSpec(
        provider="segment",
        aliases=("segment_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="write_key",
                names=("write_key", "writeKey", "writekey", "api_key", "apiKey", "value"),
            ),
        ),
        hint_fields=("write_key", "value"),
        env_var="SEGMENT_WRITE_KEY",
        display_name="Segment",
    )
)

_SENDY = register_provider_spec(
    ProviderCredentialSpec(
        provider="sendy",
        aliases=("sendy_api",),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "key", "value")),
            CredentialFieldGroup(
                role="base_url",
                names=("url", "base_url", "baseUrl"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="SENDY_API_KEY",
        display_name="Sendy",
    )
)

_VERO = register_provider_spec(
    ProviderCredentialSpec(
        provider="vero",
        aliases=("vero_api",),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=("auth_token", "authToken", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("auth_token", "authToken", "api_key", "apiKey", "token", "value"),
        env_var="VERO_AUTH_TOKEN",
        display_name="Vero",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 20, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
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
                params=_filtered(params),
                json=json_body,
                data=_filtered(data) if data is not None else None,
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


def _request_any(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
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
                params=_filtered(params),
                json=json_body,
                data=_filtered(data) if data is not None else None,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except ValueError:
                return response.text
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


def _token_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    field_names: tuple[str, ...],
    settings_token_name: str,
    settings_base_name: str,
    default_base: str,
    env_var: str,
    display_name: str,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, str | None]:
    base_from_vault = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
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
    token = token_from_vault or _settings_value(settings_token_name)
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=token_from_vault,
        secret=token,
        provider=provider,
    )
    if not token:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return _base_url(base), str(token)


def _mautic_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_from_vault = _credential_value(
        provider=_MAUTIC.provider,
        provider_aliases=_MAUTIC.aliases,
        field_names=_MAUTIC.group("base_url"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value("mautic_base_url")
    if not base:
        return "", (
            '[Error]: No Mautic base URL found. Save a Mautic credential with "base_url" / "url", '
            "or set MAUTIC_BASE_URL."
        )
    token_from_vault = _credential_value(
        provider=_MAUTIC.provider,
        provider_aliases=_MAUTIC.aliases,
        field_names=_MAUTIC.group("token"),
        tool_name=tool_name,
        config=config,
    )
    token = token_from_vault or _settings_value("mautic_access_token")
    username = _credential_value(
        provider=_MAUTIC.provider,
        provider_aliases=_MAUTIC.aliases,
        field_names=_MAUTIC.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mautic_username")
    password_from_vault = _credential_value(
        provider=_MAUTIC.provider,
        provider_aliases=_MAUTIC.aliases,
        field_names=_MAUTIC.group("password"),
        tool_name=tool_name,
        config=config,
    )
    password = password_from_vault or _settings_value("mautic_password")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"}
    # The guard runs per BRANCH, on the credential that actually authenticates the
    # request. Asking instead whether the record supplied EITHER alternative would
    # reproduce slice B's own weakness one level down: a record holding base_url plus
    # a password clears "some anchor", and the token branch then sends the operator's
    # access token to that record's address.
    if token:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=token_from_vault,
            secret=token,
            provider=_MAUTIC.provider,
        )
        headers["Authorization"] = f"Bearer {token}"
        return _base_url(base), headers
    if username and password:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=password_from_vault,
            secret=password,
            provider=_MAUTIC.provider,
        )
        headers["Authorization"] = f"Basic {_basic_auth(username, password)}"
        return _base_url(base), headers
    return _base_url(base), _setup_hint(
        provider=_MAUTIC.provider,
        field_names=_MAUTIC.hint_fields,
        tool_name=tool_name,
        env_var=_MAUTIC.env_var,
        display_name=_MAUTIC.display_name,
    )


def _mautic_request(
    tool_name: str,
    method: str,
    endpoint: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    config: Optional[RunnableConfig] = None,
) -> Any:
    base, headers_or_error = _mautic_config(tool_name, config)
    if isinstance(headers_or_error, str):
        return headers_or_error
    data = _request_json(method, f"{base}/api{endpoint}", params=params, json_body=json_body, headers=headers_or_error)
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(_dump_json(data["errors"], max_chars=1000))
    return data


def _mautic_collection(data: Any, key: str, *, limit: int) -> Any:
    if isinstance(data, str):
        return data
    records = data.get(key, data) if isinstance(data, dict) else data
    if isinstance(records, dict):
        records = list(records.values())
    if isinstance(records, list):
        return records[:limit]
    return records


def _mautic_entity(data: Any, key: str, *, simple: bool = True) -> Any:
    if isinstance(data, str):
        return data
    entity = data.get(key, data) if isinstance(data, dict) else data
    if simple and isinstance(entity, dict):
        fields = entity.get("fields")
        if isinstance(fields, dict) and isinstance(fields.get("all"), dict):
            return fields["all"]
    return entity


def _lemlist_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, token_or_error = _token_config(
        provider=_LEMLIST.provider,
        provider_aliases=_LEMLIST.aliases,
        field_names=_LEMLIST.group("token"),
        settings_token_name="lemlist_api_key",
        settings_base_name="lemlist_base_url",
        default_base=_LEMLIST_BASE_URL,
        env_var=_LEMLIST.env_var,
        display_name=_LEMLIST.display_name,
        tool_name=tool_name,
        config=config,
    )
    if not token_or_error or token_or_error.startswith("[Error]:"):
        return base, token_or_error or "[Error]: No Lemlist credential found."
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {_basic_auth('', token_or_error)}",
        "User-Agent": "Nymeria",
    }


def _sendy_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider=_SENDY.provider,
            provider_aliases=_SENDY.aliases,
            field_names=_SENDY.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("sendy_url")
        or _settings_value("sendy_base_url")
    )
    if not base:
        return "", (
            "[Error]: No Sendy URL found. Save a Sendy credential with "
            '"url" / "base_url", or set SENDY_URL.'
        )
    api_key = _credential_value(
        provider=_SENDY.provider,
        provider_aliases=_SENDY.aliases,
        field_names=_SENDY.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("sendy_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_SENDY.provider,
            field_names=_SENDY.hint_fields,
            tool_name=tool_name,
            env_var=_SENDY.env_var,
            display_name=_SENDY.display_name,
        )
    return _base_url(base), str(api_key)


def _sendy_request(
    tool_name: str,
    path: str,
    body: dict[str, Any],
    config: Optional[RunnableConfig],
) -> Any:
    base, key_or_error = _sendy_config(tool_name, config)
    if key_or_error is None or key_or_error.startswith("[Error]:"):
        return key_or_error or "[Error]: No Sendy credential found."
    return _request_any(
        "POST",
        f"{base}{path}",
        data={**body, "api_key": key_or_error, "boolean": "true"},
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Nymeria"},
    )


def _emelia_graphql_url(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    url = (
        _credential_value(
            provider=_EMELIA.provider,
            provider_aliases=_EMELIA.aliases,
            field_names=_EMELIA.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("emelia_graphql_url")
        or _EMELIA_GRAPHQL_URL
    )
    token = _credential_value(
        provider=_EMELIA.provider,
        provider_aliases=_EMELIA.aliases,
        field_names=_EMELIA.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("emelia_api_key")
    if not token:
        return _absolute_url_or_base_graphql(url), _setup_hint(
            provider=_EMELIA.provider,
            field_names=_EMELIA.hint_fields,
            tool_name=tool_name,
            env_var=_EMELIA.env_var,
            display_name=_EMELIA.display_name,
        )
    return _absolute_url_or_base_graphql(url), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": str(token),
        "User-Agent": "Nymeria",
    }


def _absolute_url_or_base_graphql(value: str) -> str:
    base = _base_url(value)
    if base.endswith("/graphql"):
        return base
    return f"{base}/graphql"


def _emelia_graphql(
    tool_name: str,
    query: str,
    *,
    operation_name: str = "",
    variables: Optional[dict[str, Any]] = None,
    config: Optional[RunnableConfig] = None,
) -> Any:
    url, headers_or_error = _emelia_graphql_url(tool_name, config)
    if isinstance(headers_or_error, str):
        return headers_or_error
    body: dict[str, Any] = {"query": query}
    if operation_name:
        body["operationName"] = operation_name
    if variables is not None:
        body["variables"] = variables
    data = _request_json("POST", url, json_body=body, headers=headers_or_error)
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(_dump_json(data["errors"], max_chars=1000))
    return data


def _json_or_text(data: Any) -> str:
    return data if isinstance(data, str) else _dump_json(data)


def _actionnetwork_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, token_or_error = _token_config(
        provider=_ACTIONNETWORK.provider,
        provider_aliases=_ACTIONNETWORK.aliases,
        field_names=_ACTIONNETWORK.group("token"),
        settings_token_name="actionnetwork_api_key",
        settings_base_name="actionnetwork_base_url",
        default_base=_ACTIONNETWORK_BASE_URL,
        env_var=_ACTIONNETWORK.env_var,
        display_name=_ACTIONNETWORK.display_name,
        tool_name=tool_name,
        config=config,
    )
    if not token_or_error or token_or_error.startswith("[Error]:"):
        return base, token_or_error or "[Error]: No Action Network credential found."
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OSDI-API-Token": token_or_error,
        "User-Agent": "Nymeria",
    }


def _actionnetwork_person_link(base: str, person_id: str) -> dict[str, Any]:
    return {"_links": {"osdi:person": {"href": f"{base}/people/{quote(person_id.strip(), safe='')}"}}}


def _actionnetwork_endpoint(resource: str, record_id: str = "", parent_id: str = "") -> tuple[str, str]:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "event": ("events", "/events"),
        "events": ("events", "/events"),
        "person": ("people", "/people"),
        "people": ("people", "/people"),
        "petition": ("petitions", "/petitions"),
        "petitions": ("petitions", "/petitions"),
        "tag": ("tags", "/tags"),
        "tags": ("tags", "/tags"),
    }
    if key in mapping:
        embedded_key, endpoint = mapping[key]
    elif key in {"attendance", "attendances"}:
        if not parent_id.strip():
            raise ValueError("parent_id must be the event ID for attendance records")
        embedded_key = "attendances"
        endpoint = f"/events/{quote(parent_id.strip(), safe='')}/attendances"
    elif key in {"signature", "signatures"}:
        if not parent_id.strip():
            raise ValueError("parent_id must be the petition ID for signature records")
        embedded_key = "signatures"
        endpoint = f"/petitions/{quote(parent_id.strip(), safe='')}/signatures"
    elif key in {"person_tag", "person_tags", "tagging", "taggings"}:
        if not parent_id.strip():
            raise ValueError("parent_id must be the tag ID for person tag records")
        embedded_key = "taggings"
        endpoint = f"/tags/{quote(parent_id.strip(), safe='')}/taggings"
    else:
        raise ValueError("resource must be event, person, petition, tag, attendance, signature, or person_tag")
    if record_id.strip():
        endpoint = f"{endpoint}/{quote(record_id.strip(), safe='')}"
    return embedded_key, endpoint


def _actionnetwork_list_items(data: Any, embedded_key: str) -> list[Any]:
    if not isinstance(data, dict):
        return []
    embedded = data.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    return list(embedded.get(f"osdi:{embedded_key}") or [])


def _autopilot_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, key_or_error = _token_config(
        provider=_AUTOPILOT.provider,
        provider_aliases=_AUTOPILOT.aliases,
        field_names=_AUTOPILOT.group("token"),
        settings_token_name="autopilot_api_key",
        settings_base_name="autopilot_base_url",
        default_base=_AUTOPILOT_BASE_URL,
        env_var=_AUTOPILOT.env_var,
        display_name=_AUTOPILOT.display_name,
        tool_name=tool_name,
        config=config,
    )
    if not key_or_error or key_or_error.startswith("[Error]:"):
        return base, key_or_error or "[Error]: No Autopilot credential found."
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "autopilotapikey": key_or_error,
    }


def _egoi_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base, key_or_error = _token_config(
        provider=_EGOI.provider,
        provider_aliases=_EGOI.aliases,
        field_names=_EGOI.group("token"),
        settings_token_name="egoi_api_key",
        settings_base_name="egoi_base_url",
        default_base=_EGOI_BASE_URL,
        env_var=_EGOI.env_var,
        display_name=_EGOI.display_name,
        tool_name=tool_name,
        config=config,
    )
    if not key_or_error or key_or_error.startswith("[Error]:"):
        return base, key_or_error or "[Error]: No E-goi credential found."
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Apikey": key_or_error,
        "User-Agent": "Nymeria",
    }


def _vero_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    return _token_config(
        provider=_VERO.provider,
        provider_aliases=_VERO.aliases,
        field_names=_VERO.group("token"),
        settings_token_name="vero_auth_token",
        settings_base_name="vero_base_url",
        default_base=_VERO_BASE_URL,
        env_var=_VERO.env_var,
        display_name=_VERO.display_name,
        tool_name=tool_name,
        config=config,
    )


def _vero_request(
    tool_name: str,
    method: str,
    endpoint: str,
    body: Optional[dict[str, Any]] = None,
    *,
    config: Optional[RunnableConfig] = None,
) -> Any:
    base, token_or_error = _vero_config(tool_name, config)
    if token_or_error is None or token_or_error.startswith("[Error]:"):
        return token_or_error or ""
    return _request_json(
        method,
        f"{base}{endpoint}",
        data={"auth_token": token_or_error, **(body or {})},
        headers={"Accept": "application/json", "User-Agent": "Nymeria"},
    )


def _customerio_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[dict[str, Any], str | None]:
    region = (
        _credential_value(
            provider=_CUSTOMERIO.provider,
            provider_aliases=_CUSTOMERIO.aliases,
            field_names=_CUSTOMERIO.group("region"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("customerio_region")
        or "track.customer.io"
    )
    region_key = str(region).lower()
    track_base_from_vault = _credential_value(
        provider=_CUSTOMERIO.provider,
        provider_aliases=_CUSTOMERIO.aliases,
        field_names=_CUSTOMERIO.group("tracking_base_url"),
        tool_name=tool_name,
        config=config,
    )
    track_base = (
        track_base_from_vault
        or _settings_value("customerio_tracking_base_url")
        or (_CUSTOMERIO_TRACK_EU_BASE_URL if "eu" in region_key else _CUSTOMERIO_TRACK_BASE_URL)
    )
    app_base_from_vault = _credential_value(
        provider=_CUSTOMERIO.provider,
        provider_aliases=_CUSTOMERIO.aliases,
        field_names=_CUSTOMERIO.group("app_base_url"),
        tool_name=tool_name,
        config=config,
    )
    app_base = (
        app_base_from_vault
        or _settings_value("customerio_app_base_url")
        or (_CUSTOMERIO_APP_EU_BASE_URL if "eu" in region_key else _CUSTOMERIO_APP_BASE_URL)
    )
    site_id = _credential_value(
        provider=_CUSTOMERIO.provider,
        provider_aliases=_CUSTOMERIO.aliases,
        field_names=_CUSTOMERIO.group("tracking_site_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("customerio_tracking_site_id")
    tracking_key_from_vault = _credential_value(
        provider=_CUSTOMERIO.provider,
        provider_aliases=_CUSTOMERIO.aliases,
        field_names=_CUSTOMERIO.group("tracking_api_key"),
        tool_name=tool_name,
        config=config,
    )
    tracking_key = tracking_key_from_vault or _settings_value("customerio_tracking_api_key")
    app_key_from_vault = _credential_value(
        provider=_CUSTOMERIO.provider,
        provider_aliases=_CUSTOMERIO.aliases,
        field_names=_CUSTOMERIO.group("app_api_key"),
        tool_name=tool_name,
        config=config,
    )
    app_key = app_key_from_vault or _settings_value("customerio_app_api_key")

    tracking_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    app_headers = dict(tracking_headers)
    # Two destinations, two keys: each key is guarded against the base URL it is
    # actually sent to, since a record can supply one base without the key that
    # rides to it. site_id is an account identifier, not a secret, so it is the
    # tracking key alone that has to be joined to the tracking base.
    if site_id and tracking_key:
        _require_joined_destination(
            destination_from_vault=track_base_from_vault,
            secret_from_vault=tracking_key_from_vault,
            secret=tracking_key,
            provider=_CUSTOMERIO.provider,
        )
        raw = f"{site_id}:{tracking_key}".encode()
        tracking_headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    if app_key:
        _require_joined_destination(
            destination_from_vault=app_base_from_vault,
            secret_from_vault=app_key_from_vault,
            secret=app_key,
            provider=_CUSTOMERIO.provider,
        )
        app_headers["Authorization"] = f"Bearer {app_key}"
    missing: list[str] = []
    if not (site_id and tracking_key):
        missing.append("tracking")
    if not app_key:
        missing.append("app")
    return {
        "tracking_base": _base_url(track_base),
        "app_base": _base_url(app_base),
        "tracking_headers": tracking_headers,
        "app_headers": app_headers,
        "has_tracking": bool(site_id and tracking_key),
        "has_app": bool(app_key),
    }, ",".join(missing) if missing else None


def _customerio_auth_error(tool_name: str, *, app: bool) -> str:
    # Branch-variant hint: the app branch keeps its own field/env literals inline;
    # the tracking branch sources them from the spec (hint_fields/env_var).
    fields = ("app_api_key",) if app else _CUSTOMERIO.hint_fields
    env_var = "CUSTOMERIO_APP_API_KEY" if app else _CUSTOMERIO.env_var
    return _setup_hint(
        provider=_CUSTOMERIO.provider,
        field_names=fields,
        tool_name=tool_name,
        env_var=env_var,
        display_name=_CUSTOMERIO.display_name,
    )


def _iterable_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_ITERABLE.provider,
            provider_aliases=_ITERABLE.aliases,
            field_names=_ITERABLE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("iterable_base_url")
        or _ITERABLE_BASE_URL
    )
    api_key = _credential_value(
        provider=_ITERABLE.provider,
        provider_aliases=_ITERABLE.aliases,
        field_names=_ITERABLE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("iterable_api_key")
    base = _base_url(base)
    if not base.endswith("/api"):
        base = f"{base}/api"
    if not api_key:
        return base, _setup_hint(
            provider=_ITERABLE.provider,
            field_names=_ITERABLE.hint_fields,
            tool_name=tool_name,
            env_var=_ITERABLE.env_var,
            display_name=_ITERABLE.display_name,
        )
    return base, {
        "Accept": "application/json",
        "Api_Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _posthog_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider=_POSTHOG.provider,
            provider_aliases=_POSTHOG.aliases,
            field_names=_POSTHOG.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("posthog_base_url")
        or _POSTHOG_BASE_URL
    )
    api_key = _credential_value(
        provider=_POSTHOG.provider,
        provider_aliases=_POSTHOG.aliases,
        field_names=_POSTHOG.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("posthog_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_POSTHOG.provider,
            field_names=_POSTHOG.hint_fields,
            tool_name=tool_name,
            env_var=_POSTHOG.env_var,
            display_name=_POSTHOG.display_name,
        )
    return _base_url(base), api_key


def _segment_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_SEGMENT.provider,
            provider_aliases=_SEGMENT.aliases,
            field_names=_SEGMENT.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("segment_base_url")
        or _SEGMENT_BASE_URL
    )
    write_key = _credential_value(
        provider=_SEGMENT.provider,
        provider_aliases=_SEGMENT.aliases,
        field_names=_SEGMENT.group("write_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("segment_write_key")
    if not write_key:
        return _base_url(base), _setup_hint(
            provider=_SEGMENT.provider,
            field_names=_SEGMENT.hint_fields,
            tool_name=tool_name,
            env_var=_SEGMENT.env_var,
            display_name=_SEGMENT.display_name,
        )
    raw = f"{write_key}:".encode()
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Basic {base64.b64encode(raw).decode()}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


@tool
def customerio_list_campaigns(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Customer.io campaigns."""
    try:
        cfg, _missing = _customerio_config("customerio_list_campaigns", config)
        if not cfg["has_app"]:
            return _customerio_auth_error("customerio_list_campaigns", app=True)
        return _dump_json(_request_json("GET", f"{cfg['app_base']}/campaigns", headers=cfg["app_headers"]))
    except Exception as e:
        logger.error("customerio_list_campaigns failed", exc_info=True)
        return f"[Error]: Customer.io campaign listing failed: {e}"


@tool
def customerio_get_campaign(
    campaign_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Customer.io campaign by ID."""
    try:
        cfg, _missing = _customerio_config("customerio_get_campaign", config)
        if not cfg["has_app"]:
            return _customerio_auth_error("customerio_get_campaign", app=True)
        return _dump_json(
            _request_json("GET", f"{cfg['app_base']}/campaigns/{quote(campaign_id, safe='')}", headers=cfg["app_headers"])
        )
    except Exception as e:
        logger.error("customerio_get_campaign failed", exc_info=True)
        return f"[Error]: Customer.io campaign lookup failed: {e}"


@tool
def customerio_upsert_customer(
    customer_id: str,
    fields_json: str = "",
    email: str = "",
    created_at: int = 0,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update a Customer.io customer profile."""
    if not customer_id.strip():
        return "[Error]: customer_id is required."
    try:
        cfg, _missing = _customerio_config("customerio_upsert_customer", config)
        if not cfg["has_tracking"]:
            return _customerio_auth_error("customerio_upsert_customer", app=False)
        body = _json_object(fields_json, field_name="fields_json")
        if email:
            body["email"] = email
        if created_at:
            body["created_at"] = int(created_at)
        return _dump_json(
            _request_json(
                "PUT",
                f"{cfg['tracking_base']}/customers/{quote(customer_id.strip(), safe='')}",
                json_body=body,
                headers=cfg["tracking_headers"],
            )
        )
    except Exception as e:
        logger.error("customerio_upsert_customer failed", exc_info=True)
        return f"[Error]: Customer.io customer upsert failed: {e}"


@tool
def customerio_track_event(
    customer_id: str,
    event_name: str,
    data_json: str = "",
    event_type: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a Customer.io event for a known customer."""
    if not customer_id.strip() or not event_name.strip():
        return "[Error]: customer_id and event_name are required."
    try:
        cfg, _missing = _customerio_config("customerio_track_event", config)
        if not cfg["has_tracking"]:
            return _customerio_auth_error("customerio_track_event", app=False)
        body: dict[str, Any] = {"name": event_name.strip(), "data": _json_object(data_json, field_name="data_json")}
        if event_type:
            body["data"]["type"] = event_type
        return _dump_json(
            _request_json(
                "POST",
                f"{cfg['tracking_base']}/customers/{quote(customer_id.strip(), safe='')}/events",
                json_body=body,
                headers=cfg["tracking_headers"],
            )
        )
    except Exception as e:
        logger.error("customerio_track_event failed", exc_info=True)
        return f"[Error]: Customer.io event tracking failed: {e}"


@tool
def customerio_track_anonymous_event(
    event_name: str,
    data_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a Customer.io event without a known customer ID."""
    if not event_name.strip():
        return "[Error]: event_name is required."
    try:
        cfg, _missing = _customerio_config("customerio_track_anonymous_event", config)
        if not cfg["has_tracking"]:
            return _customerio_auth_error("customerio_track_anonymous_event", app=False)
        body = {"name": event_name.strip(), "data": _json_object(data_json, field_name="data_json")}
        return _dump_json(_request_json("POST", f"{cfg['tracking_base']}/events", json_body=body, headers=cfg["tracking_headers"]))
    except Exception as e:
        logger.error("customerio_track_anonymous_event failed", exc_info=True)
        return f"[Error]: Customer.io anonymous event tracking failed: {e}"


@tool
def customerio_update_segment(
    segment_id: str,
    customer_ids: str,
    action: str = "add",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add or remove customers from a Customer.io manual segment."""
    ids = _csv_to_list(customer_ids)
    if not segment_id.strip() or not ids:
        return "[Error]: segment_id and customer_ids are required."
    action_key = action.strip().lower()
    if action_key not in {"add", "remove"}:
        return "[Error]: action must be add or remove."
    try:
        cfg, _missing = _customerio_config("customerio_update_segment", config)
        if not cfg["has_tracking"]:
            return _customerio_auth_error("customerio_update_segment", app=False)
        endpoint = "add_customers" if action_key == "add" else "remove_customers"
        body = {"id": segment_id.strip(), "ids": ids}
        return _dump_json(
            _request_json(
                "POST",
                f"{cfg['tracking_base']}/segments/{quote(segment_id.strip(), safe='')}/{endpoint}",
                json_body=body,
                headers=cfg["tracking_headers"],
            )
        )
    except Exception as e:
        logger.error("customerio_update_segment failed", exc_info=True)
        return f"[Error]: Customer.io segment update failed: {e}"


@tool
def iterable_get_user(
    identifier: str,
    value: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Iterable user by email or user ID."""
    if not value.strip():
        return "[Error]: value is required."
    try:
        base, auth = _iterable_config("iterable_get_user", config)
        if isinstance(auth, str):
            return auth
        if identifier.strip().lower() == "user_id":
            return _dump_json(_request_json("GET", f"{base}/users/byUserId/{quote(value.strip(), safe='')}", headers=auth))
        return _dump_json(_request_json("GET", f"{base}/users/getByEmail", params={"email": value.strip()}, headers=auth))
    except Exception as e:
        logger.error("iterable_get_user failed", exc_info=True)
        return f"[Error]: Iterable user lookup failed: {e}"


@tool
def iterable_upsert_user(
    identifier: str,
    value: str,
    data_fields_json: str = "",
    prefer_user_id: bool = False,
    merge_nested_objects: bool = True,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update an Iterable user."""
    if not value.strip():
        return "[Error]: value is required."
    try:
        base, auth = _iterable_config("iterable_upsert_user", config)
        if isinstance(auth, str):
            return auth
        data_fields = _json_object(data_fields_json, field_name="data_fields_json")
        body: dict[str, Any] = {
            "dataFields": data_fields,
            "mergeNestedObjects": bool(merge_nested_objects),
        }
        if identifier.strip().lower() == "user_id":
            body["userId"] = value.strip()
            body["preferUserId"] = bool(prefer_user_id)
        else:
            body["email"] = value.strip()
        return _dump_json(_request_json("POST", f"{base}/users/update", json_body=body, headers=auth))
    except Exception as e:
        logger.error("iterable_upsert_user failed", exc_info=True)
        return f"[Error]: Iterable user upsert failed: {e}"


@tool
def iterable_track_event(
    event_name: str,
    email: str = "",
    user_id: str = "",
    data_fields_json: str = "",
    created_at: str = "",
    campaign_id: int = 0,
    template_id: int = 0,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track an Iterable event."""
    if not event_name.strip() or not (email.strip() or user_id.strip()):
        return "[Error]: event_name and either email or user_id are required."
    try:
        base, auth = _iterable_config("iterable_track_event", config)
        if isinstance(auth, str):
            return auth
        body: dict[str, Any] = {
            "eventName": event_name.strip(),
            "dataFields": _json_object(data_fields_json, field_name="data_fields_json"),
        }
        if email.strip():
            body["email"] = email.strip()
        else:
            body["userId"] = user_id.strip()
        if created_at.strip():
            body["createdAt"] = created_at.strip()
        if campaign_id:
            body["campaignId"] = int(campaign_id)
        if template_id:
            body["templateId"] = int(template_id)
        return _dump_json(_request_json("POST", f"{base}/events/trackBulk", json_body={"events": [body]}, headers=auth))
    except Exception as e:
        logger.error("iterable_track_event failed", exc_info=True)
        return f"[Error]: Iterable event tracking failed: {e}"


@tool
def iterable_list_lists(
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Iterable static lists."""
    try:
        base, auth = _iterable_config("iterable_list_lists", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/lists", headers=auth))
    except Exception as e:
        logger.error("iterable_list_lists failed", exc_info=True)
        return f"[Error]: Iterable list listing failed: {e}"


@tool
def iterable_update_list_subscribers(
    list_id: int,
    values: str,
    identifier: str = "email",
    action: str = "add",
    campaign_id: int = 0,
    channel_unsubscribe: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe or unsubscribe Iterable users by email or user ID."""
    subscribers = _csv_to_list(values)
    if not subscribers:
        return "[Error]: values is required."
    action_key = action.strip().lower()
    if action_key not in {"add", "remove"}:
        return "[Error]: action must be add or remove."
    try:
        base, auth = _iterable_config("iterable_update_list_subscribers", config)
        if isinstance(auth, str):
            return auth
        key = "userId" if identifier.strip().lower() == "user_id" else "email"
        body = {"listId": int(list_id), "subscribers": [{key: value} for value in subscribers]}
        endpoint = "subscribe" if action_key == "add" else "unsubscribe"
        if action_key == "remove":
            if campaign_id:
                body["campaignId"] = int(campaign_id)
            body["channelUnsubscribe"] = bool(channel_unsubscribe)
        return _dump_json(_request_json("POST", f"{base}/lists/{endpoint}", json_body=body, headers=auth))
    except Exception as e:
        logger.error("iterable_update_list_subscribers failed", exc_info=True)
        return f"[Error]: Iterable list subscriber update failed: {e}"


@tool
def posthog_capture_event(
    event_name: str,
    distinct_id: str,
    properties_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Capture one PostHog event."""
    if not event_name.strip() or not distinct_id.strip():
        return "[Error]: event_name and distinct_id are required."
    try:
        base, api_key = _posthog_config("posthog_capture_event", config)
        if api_key is None or api_key.startswith("[Error]:"):
            return api_key or ""
        properties = _json_object(properties_json, field_name="properties_json")
        properties["distinct_id"] = distinct_id.strip()
        body = {"api_key": api_key, "event": event_name.strip(), "properties": properties}
        if timestamp.strip():
            body["timestamp"] = timestamp.strip()
        return _dump_json(_request_json("POST", f"{base}/capture", json_body=body, headers={"User-Agent": "Nymeria"}))
    except Exception as e:
        logger.error("posthog_capture_event failed", exc_info=True)
        return f"[Error]: PostHog event capture failed: {e}"


@tool
def posthog_identify(
    distinct_id: str,
    properties_json: str = "",
    context_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Identify a PostHog user and set properties."""
    if not distinct_id.strip():
        return "[Error]: distinct_id is required."
    try:
        base, api_key = _posthog_config("posthog_identify", config)
        if api_key is None or api_key.startswith("[Error]:"):
            return api_key or ""
        body = {
            "api_key": api_key,
            "event": "$identify",
            "distinct_id": distinct_id.strip(),
            "properties": _json_object(properties_json, field_name="properties_json"),
        }
        if context_json.strip():
            body["context"] = _json_object(context_json, field_name="context_json")
        if timestamp.strip():
            body["timestamp"] = timestamp.strip()
        return _dump_json(_request_json("POST", f"{base}/batch", json_body=body, headers={"User-Agent": "Nymeria"}))
    except Exception as e:
        logger.error("posthog_identify failed", exc_info=True)
        return f"[Error]: PostHog identify failed: {e}"


@tool
def posthog_create_alias(
    distinct_id: str,
    alias: str,
    context_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a PostHog alias for a distinct ID."""
    if not distinct_id.strip() or not alias.strip():
        return "[Error]: distinct_id and alias are required."
    try:
        base, api_key = _posthog_config("posthog_create_alias", config)
        if api_key is None or api_key.startswith("[Error]:"):
            return api_key or ""
        body = {
            "api_key": api_key,
            "event": "$create_alias",
            "properties": {"distinct_id": distinct_id.strip(), "alias": alias.strip()},
        }
        if context_json.strip():
            body["context"] = _json_object(context_json, field_name="context_json")
        if timestamp.strip():
            body["timestamp"] = timestamp.strip()
        return _dump_json(_request_json("POST", f"{base}/batch", json_body=body, headers={"User-Agent": "Nymeria"}))
    except Exception as e:
        logger.error("posthog_create_alias failed", exc_info=True)
        return f"[Error]: PostHog alias creation failed: {e}"


@tool
def posthog_track_page_or_screen(
    kind: str,
    distinct_id: str,
    name: str,
    properties_json: str = "",
    context_json: str = "",
    timestamp: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a PostHog page or screen view."""
    kind_key = kind.strip().lower()
    if kind_key not in {"page", "screen"}:
        return "[Error]: kind must be page or screen."
    if not distinct_id.strip() or not name.strip():
        return "[Error]: distinct_id and name are required."
    try:
        base, api_key = _posthog_config("posthog_track_page_or_screen", config)
        if api_key is None or api_key.startswith("[Error]:"):
            return api_key or ""
        properties = _json_object(properties_json, field_name="properties_json")
        properties["distinct_id"] = distinct_id.strip()
        properties["name"] = name.strip()
        body: dict[str, Any] = {
            "api_key": api_key,
            "event": "$page" if kind_key == "page" else "$screen",
            "properties": properties,
        }
        if context_json.strip():
            body["context"] = _json_object(context_json, field_name="context_json")
        if timestamp.strip():
            body["timestamp"] = timestamp.strip()
        return _dump_json(_request_json("POST", f"{base}/batch", json_body=body, headers={"User-Agent": "Nymeria"}))
    except Exception as e:
        logger.error("posthog_track_page_or_screen failed", exc_info=True)
        return f"[Error]: PostHog page or screen tracking failed: {e}"


@tool
def segment_identify(
    traits_json: str = "",
    user_id: str = "",
    anonymous_id: str = "",
    context_json: str = "",
    integrations_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Segment identify call."""
    if not (user_id.strip() or anonymous_id.strip()):
        return "[Error]: user_id or anonymous_id is required."
    try:
        base, auth = _segment_config("segment_identify", config)
        if isinstance(auth, str):
            return auth
        body: dict[str, Any] = {
            "traits": _json_object(traits_json, field_name="traits_json"),
            "context": _json_object(context_json, field_name="context_json"),
            "integrations": _json_object(integrations_json, field_name="integrations_json"),
        }
        if user_id.strip():
            body["userId"] = user_id.strip()
        else:
            body["anonymousId"] = anonymous_id.strip()
        return _dump_json(_request_json("POST", f"{base}/identify", json_body=_filtered(body), headers=auth))
    except Exception as e:
        logger.error("segment_identify failed", exc_info=True)
        return f"[Error]: Segment identify failed: {e}"


@tool
def segment_track(
    event: str,
    user_id: str = "",
    anonymous_id: str = "",
    properties_json: str = "",
    context_json: str = "",
    integrations_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Segment track event."""
    if not event.strip() or not (user_id.strip() or anonymous_id.strip()):
        return "[Error]: event and user_id or anonymous_id are required."
    try:
        base, auth = _segment_config("segment_track", config)
        if isinstance(auth, str):
            return auth
        body = {
            "event": event.strip(),
            "properties": _json_object(properties_json, field_name="properties_json"),
            "context": _json_object(context_json, field_name="context_json"),
            "integrations": _json_object(integrations_json, field_name="integrations_json"),
        }
        if user_id.strip():
            body["userId"] = user_id.strip()
        else:
            body["anonymousId"] = anonymous_id.strip()
        return _dump_json(_request_json("POST", f"{base}/track", json_body=_filtered(body), headers=auth))
    except Exception as e:
        logger.error("segment_track failed", exc_info=True)
        return f"[Error]: Segment track failed: {e}"


@tool
def segment_group(
    group_id: str,
    user_id: str = "",
    anonymous_id: str = "",
    traits_json: str = "",
    context_json: str = "",
    integrations_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Segment group call."""
    if not group_id.strip() or not (user_id.strip() or anonymous_id.strip()):
        return "[Error]: group_id and user_id or anonymous_id are required."
    try:
        base, auth = _segment_config("segment_group", config)
        if isinstance(auth, str):
            return auth
        body = {
            "groupId": group_id.strip(),
            "traits": _json_object(traits_json, field_name="traits_json"),
            "context": _json_object(context_json, field_name="context_json"),
            "integrations": _json_object(integrations_json, field_name="integrations_json"),
        }
        if user_id.strip():
            body["userId"] = user_id.strip()
        else:
            body["anonymousId"] = anonymous_id.strip()
        return _dump_json(_request_json("POST", f"{base}/group", json_body=_filtered(body), headers=auth))
    except Exception as e:
        logger.error("segment_group failed", exc_info=True)
        return f"[Error]: Segment group failed: {e}"


def _activecampaign_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_ACTIVECAMPAIGN.provider,
            provider_aliases=_ACTIVECAMPAIGN.aliases,
            field_names=_ACTIVECAMPAIGN.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("activecampaign_base_url")
        or _ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL
    )
    api_key = _credential_value(
        provider=_ACTIVECAMPAIGN.provider,
        provider_aliases=_ACTIVECAMPAIGN.aliases,
        field_names=_ACTIVECAMPAIGN.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("activecampaign_api_key")
    if not api_key or base == _ACTIVECAMPAIGN_PLACEHOLDER_BASE_URL:
        return _base_url(base), _setup_hint(
            provider=_ACTIVECAMPAIGN.provider,
            field_names=_ACTIVECAMPAIGN.hint_fields,
            tool_name=tool_name,
            env_var=_ACTIVECAMPAIGN.env_var,
            display_name=_ACTIVECAMPAIGN.display_name,
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
            provider=_CONVERTKIT.provider,
            provider_aliases=_CONVERTKIT.aliases,
            field_names=_CONVERTKIT.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("convertkit_base_url")
        or _CONVERTKIT_BASE_URL
    )
    secret = _credential_value(
        provider=_CONVERTKIT.provider,
        provider_aliases=_CONVERTKIT.aliases,
        field_names=_CONVERTKIT.group("secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("convertkit_api_secret")
    if not secret:
        return _base_url(base), _setup_hint(
            provider=_CONVERTKIT.provider,
            field_names=_CONVERTKIT.hint_fields,
            tool_name=tool_name,
            env_var=_CONVERTKIT.env_var,
            display_name=_CONVERTKIT.display_name,
        )
    return _base_url(base), secret


def _getresponse_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_GETRESPONSE.provider,
            provider_aliases=_GETRESPONSE.aliases,
            field_names=_GETRESPONSE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("getresponse_base_url")
        or _GETRESPONSE_BASE_URL
    )
    api_key = _credential_value(
        provider=_GETRESPONSE.provider,
        provider_aliases=_GETRESPONSE.aliases,
        field_names=_GETRESPONSE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("getresponse_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_GETRESPONSE.provider,
            field_names=_GETRESPONSE.hint_fields,
            tool_name=tool_name,
            env_var=_GETRESPONSE.env_var,
            display_name=_GETRESPONSE.display_name,
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
            provider=_MAILERLITE.provider,
            provider_aliases=_MAILERLITE.aliases,
            field_names=_MAILERLITE.group("classic_api"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailerlite_classic_api")
    )
    default_base = _MAILERLITE_CLASSIC_BASE_URL if str(classic).lower() in {"1", "true", "yes"} else _MAILERLITE_BASE_URL
    configured_base = (
        _credential_value(
            provider=_MAILERLITE.provider,
            provider_aliases=_MAILERLITE.aliases,
            field_names=_MAILERLITE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("mailerlite_base_url")
    )
    if default_base == _MAILERLITE_CLASSIC_BASE_URL and configured_base == _MAILERLITE_BASE_URL:
        configured_base = None
    base = configured_base or default_base
    api_key = _credential_value(
        provider=_MAILERLITE.provider,
        provider_aliases=_MAILERLITE.aliases,
        field_names=_MAILERLITE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("mailerlite_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_MAILERLITE.provider,
            field_names=_MAILERLITE.hint_fields,
            tool_name=tool_name,
            env_var=_MAILERLITE.env_var,
            display_name=_MAILERLITE.display_name,
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
    try:
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
    except Exception as e:
        logger.error("activecampaign_list_contacts failed", exc_info=True)
        return f"[Error]: ActiveCampaign contact listing failed: {e}"


@tool
def activecampaign_get_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one ActiveCampaign contact by ID."""
    try:
        base, auth = _activecampaign_config("activecampaign_get_contact", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/api/3/contacts/{quote(contact_id, safe='')}", headers=auth))
    except Exception as e:
        logger.error("activecampaign_get_contact failed", exc_info=True)
        return f"[Error]: ActiveCampaign contact lookup failed: {e}"


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
    try:
        base, auth = _activecampaign_config("activecampaign_sync_contact", config)
        if isinstance(auth, str):
            return auth
        contact = {"email": email, "firstName": first_name, "lastName": last_name, "phone": phone}
        contact.update(_json_object(fields_json, field_name="fields_json"))
        body = {"contact": _filtered(contact)}
        return _dump_json(_request_json("POST", f"{base}/api/3/contact/sync", json_body=body, headers=auth))
    except Exception as e:
        logger.error("activecampaign_sync_contact failed", exc_info=True)
        return f"[Error]: ActiveCampaign contact sync failed: {e}"


@tool
def activecampaign_update_contact(
    contact_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an ActiveCampaign contact by ID with a JSON object of contact fields."""
    try:
        base, auth = _activecampaign_config("activecampaign_update_contact", config)
        if isinstance(auth, str):
            return auth
        body = {"contact": _json_object(fields_json, field_name="fields_json")}
        return _dump_json(
            _request_json("PUT", f"{base}/api/3/contacts/{quote(contact_id, safe='')}", json_body=body, headers=auth)
        )
    except Exception as e:
        logger.error("activecampaign_update_contact failed", exc_info=True)
        return f"[Error]: ActiveCampaign contact update failed: {e}"


@tool
def activecampaign_list_lists(
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ActiveCampaign contact lists."""
    try:
        base, auth = _activecampaign_config("activecampaign_list_lists", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json("GET", f"{base}/api/3/lists", params={"limit": _limit(limit, default=50, max_value=100)}, headers=auth)
        )
    except Exception as e:
        logger.error("activecampaign_list_lists failed", exc_info=True)
        return f"[Error]: ActiveCampaign list listing failed: {e}"


@tool
def activecampaign_list_tags(
    search: str = "",
    limit: int = 50,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ActiveCampaign tags."""
    try:
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
    except Exception as e:
        logger.error("activecampaign_list_tags failed", exc_info=True)
        return f"[Error]: ActiveCampaign tag listing failed: {e}"


@tool
def activecampaign_add_contact_to_list(
    contact_id: str,
    list_id: str,
    status: int = 1,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe or unsubscribe an ActiveCampaign contact to a list. Use status 1 to subscribe, 2 to unsubscribe."""
    try:
        base, auth = _activecampaign_config("activecampaign_add_contact_to_list", config)
        if isinstance(auth, str):
            return auth
        body = {"contactList": {"list": list_id, "contact": contact_id, "status": status}}
        return _dump_json(_request_json("POST", f"{base}/api/3/contactLists", json_body=body, headers=auth))
    except Exception as e:
        logger.error("activecampaign_add_contact_to_list failed", exc_info=True)
        return f"[Error]: ActiveCampaign list membership update failed: {e}"


@tool
def activecampaign_add_contact_tag(
    contact_id: str,
    tag_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add an ActiveCampaign tag to a contact."""
    try:
        base, auth = _activecampaign_config("activecampaign_add_contact_tag", config)
        if isinstance(auth, str):
            return auth
        body = {"contactTag": {"contact": contact_id, "tag": tag_id}}
        return _dump_json(_request_json("POST", f"{base}/api/3/contactTags", json_body=body, headers=auth))
    except Exception as e:
        logger.error("activecampaign_add_contact_tag failed", exc_info=True)
        return f"[Error]: ActiveCampaign contact tag add failed: {e}"


@tool
def convertkit_get_account(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """Get ConvertKit account details."""
    try:
        base, secret = _convertkit_config("convertkit_get_account", config)
        if secret.startswith("[Error]:"):
            return secret
        return _dump_json(_request_json("GET", f"{base}/account", params={"api_secret": secret}))
    except Exception as e:
        logger.error("convertkit_get_account failed", exc_info=True)
        return f"[Error]: ConvertKit account lookup failed: {e}"


@tool
def convertkit_list_forms(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """List ConvertKit forms."""
    try:
        base, secret = _convertkit_config("convertkit_list_forms", config)
        if secret.startswith("[Error]:"):
            return secret
        return _dump_json(_request_json("GET", f"{base}/forms", params={"api_secret": secret}))
    except Exception as e:
        logger.error("convertkit_list_forms failed", exc_info=True)
        return f"[Error]: ConvertKit form listing failed: {e}"


@tool
def convertkit_list_tags(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """List ConvertKit tags."""
    try:
        base, secret = _convertkit_config("convertkit_list_tags", config)
        if secret.startswith("[Error]:"):
            return secret
        return _dump_json(_request_json("GET", f"{base}/tags", params={"api_secret": secret}))
    except Exception as e:
        logger.error("convertkit_list_tags failed", exc_info=True)
        return f"[Error]: ConvertKit tag listing failed: {e}"


@tool
def convertkit_list_subscribers(
    email: str = "",
    limit: int = 50,
    page: int = 1,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List ConvertKit subscribers, optionally filtered by email."""
    try:
        base, secret = _convertkit_config("convertkit_list_subscribers", config)
        if secret.startswith("[Error]:"):
            return secret
        params = {"api_secret": secret, "email_address": email, "per_page": _limit(limit, default=50, max_value=100), "page": page}
        return _dump_json(_request_json("GET", f"{base}/subscribers", params=params))
    except Exception as e:
        logger.error("convertkit_list_subscribers failed", exc_info=True)
        return f"[Error]: ConvertKit subscriber listing failed: {e}"


@tool
def convertkit_add_subscriber_to_form(
    form_id: str,
    email: str,
    first_name: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe an email address to a ConvertKit form."""
    try:
        base, secret = _convertkit_config("convertkit_add_subscriber_to_form", config)
        if secret.startswith("[Error]:"):
            return secret
        body = {"api_secret": secret, "email": email, "first_name": first_name, "fields": _json_object(fields_json, field_name="fields_json")}
        return _dump_json(
            _request_json("POST", f"{base}/forms/{quote(form_id, safe='')}/subscribe", json_body=_filtered(body))
        )
    except Exception as e:
        logger.error("convertkit_add_subscriber_to_form failed", exc_info=True)
        return f"[Error]: ConvertKit form subscription failed: {e}"


@tool
def convertkit_add_subscriber_to_tag(
    tag_id: str,
    email: str,
    first_name: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Subscribe an email address to a ConvertKit tag."""
    try:
        base, secret = _convertkit_config("convertkit_add_subscriber_to_tag", config)
        if secret.startswith("[Error]:"):
            return secret
        body = {"api_secret": secret, "email": email, "first_name": first_name, "fields": _json_object(fields_json, field_name="fields_json")}
        return _dump_json(_request_json("POST", f"{base}/tags/{quote(tag_id, safe='')}/subscribe", json_body=_filtered(body)))
    except Exception as e:
        logger.error("convertkit_add_subscriber_to_tag failed", exc_info=True)
        return f"[Error]: ConvertKit tag subscription failed: {e}"


@tool
def getresponse_list_campaigns(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """List GetResponse campaigns."""
    try:
        base, auth = _getresponse_config("getresponse_list_campaigns", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/campaigns", headers=auth))
    except Exception as e:
        logger.error("getresponse_list_campaigns failed", exc_info=True)
        return f"[Error]: GetResponse campaign listing failed: {e}"


@tool
def getresponse_list_contacts(
    email: str = "",
    campaign_id: str = "",
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List GetResponse contacts with optional email and campaign filters."""
    try:
        base, auth = _getresponse_config("getresponse_list_contacts", config)
        if isinstance(auth, str):
            return auth
        params: dict[str, Any] = {"perPage": _limit(limit, max_value=100)}
        if email:
            params["query[email]"] = email
        if campaign_id:
            params["query[campaignId]"] = campaign_id
        return _dump_json(_request_json("GET", f"{base}/contacts", params=params, headers=auth))
    except Exception as e:
        logger.error("getresponse_list_contacts failed", exc_info=True)
        return f"[Error]: GetResponse contact listing failed: {e}"


@tool
def getresponse_get_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a GetResponse contact by ID."""
    try:
        base, auth = _getresponse_config("getresponse_get_contact", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/contacts/{quote(contact_id, safe='')}", headers=auth))
    except Exception as e:
        logger.error("getresponse_get_contact failed", exc_info=True)
        return f"[Error]: GetResponse contact lookup failed: {e}"


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
    try:
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
    except Exception as e:
        logger.error("getresponse_create_contact failed", exc_info=True)
        return f"[Error]: GetResponse contact creation failed: {e}"


@tool
def getresponse_update_contact(
    contact_id: str,
    name: str = "",
    campaign_id: str = "",
    custom_fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a GetResponse contact."""
    try:
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
    except Exception as e:
        logger.error("getresponse_update_contact failed", exc_info=True)
        return f"[Error]: GetResponse contact update failed: {e}"


@tool
def getresponse_delete_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a GetResponse contact."""
    try:
        base, auth = _getresponse_config("getresponse_delete_contact", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("DELETE", f"{base}/contacts/{quote(contact_id, safe='')}", headers=auth))
    except Exception as e:
        logger.error("getresponse_delete_contact failed", exc_info=True)
        return f"[Error]: GetResponse contact deletion failed: {e}"


@tool
def mailerlite_list_subscribers(
    status: str = "",
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List MailerLite subscribers."""
    try:
        base, auth = _mailerlite_config("mailerlite_list_subscribers", config)
        if isinstance(auth, str):
            return auth
        params = {"limit": _limit(limit, max_value=100), "filter[status]": status}
        return _dump_json(_request_json("GET", f"{base}/subscribers", params=params, headers=auth))
    except Exception as e:
        logger.error("mailerlite_list_subscribers failed", exc_info=True)
        return f"[Error]: MailerLite subscriber listing failed: {e}"


@tool
def mailerlite_get_subscriber(
    subscriber_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get one MailerLite subscriber by ID or email."""
    try:
        base, auth = _mailerlite_config("mailerlite_get_subscriber", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/subscribers/{quote(subscriber_id, safe='')}", headers=auth))
    except Exception as e:
        logger.error("mailerlite_get_subscriber failed", exc_info=True)
        return f"[Error]: MailerLite subscriber lookup failed: {e}"


@tool
def mailerlite_create_subscriber(
    email: str,
    name: str = "",
    fields_json: str = "",
    groups: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a MailerLite subscriber."""
    try:
        base, auth = _mailerlite_config("mailerlite_create_subscriber", config)
        if isinstance(auth, str):
            return auth
        body: dict[str, Any] = {"email": email, "name": name, "fields": _json_object(fields_json, field_name="fields_json")}
        group_list = _csv_to_list(groups)
        if group_list:
            body["groups"] = group_list
        return _dump_json(_request_json("POST", f"{base}/subscribers", json_body=_filtered(body), headers=auth))
    except Exception as e:
        logger.error("mailerlite_create_subscriber failed", exc_info=True)
        return f"[Error]: MailerLite subscriber creation failed: {e}"


@tool
def mailerlite_update_subscriber(
    subscriber_id: str,
    fields_json: str,
    status: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a MailerLite subscriber with a JSON object of fields."""
    try:
        base, auth = _mailerlite_config("mailerlite_update_subscriber", config)
        if isinstance(auth, str):
            return auth
        body = _json_object(fields_json, field_name="fields_json")
        if status:
            body["status"] = status
        return _dump_json(
            _request_json("PUT", f"{base}/subscribers/{quote(subscriber_id, safe='')}", json_body=body, headers=auth)
        )
    except Exception as e:
        logger.error("mailerlite_update_subscriber failed", exc_info=True)
        return f"[Error]: MailerLite subscriber update failed: {e}"


@tool
def mailerlite_list_groups(
    limit: int = 20,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List MailerLite groups."""
    try:
        base, auth = _mailerlite_config("mailerlite_list_groups", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/groups", params={"limit": _limit(limit, max_value=100)}, headers=auth))
    except Exception as e:
        logger.error("mailerlite_list_groups failed", exc_info=True)
        return f"[Error]: MailerLite group listing failed: {e}"


@tool
def actionnetwork_list_records(
    resource: str,
    parent_id: str = "",
    limit: int = 25,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Action Network records.

    Args:
        resource: One of event, person, petition, tag, attendance, signature, or person_tag.
        parent_id: Event ID for attendances, petition ID for signatures, or tag ID for person_tag records.
        limit: Number of records to return, 1-100.
    """
    try:
        base, auth = _actionnetwork_config("actionnetwork_list_records", config)
        if isinstance(auth, str):
            return auth
        embedded_key, endpoint = _actionnetwork_endpoint(resource, parent_id=parent_id)
        page = 1
        items: list[Any] = []
        max_items = _limit(limit, default=25, max_value=100)
        while len(items) < max_items:
            data = _request_json("GET", f"{base}{endpoint}", params={"per_page": 25, "page": page}, headers=auth)
            chunk = _actionnetwork_list_items(data, embedded_key)
            items.extend(chunk)
            if len(items) >= max_items or not (isinstance(data, dict) and data.get("_links", {}).get("next")):
                break
            page += 1
        return _dump_json(items[:max_items])
    except Exception as e:
        logger.error("actionnetwork_list_records failed", exc_info=True)
        return f"[Error]: Action Network list failed: {e}"


@tool
def actionnetwork_get_record(
    resource: str,
    record_id: str,
    parent_id: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Action Network record by ID.

    Args:
        resource: One of event, person, petition, tag, attendance, signature, or person_tag.
        record_id: Record ID.
        parent_id: Event ID for attendances, petition ID for signatures, or tag ID for person_tag records.
    """
    try:
        if not record_id.strip():
            return "[Error]: record_id is required."
        base, auth = _actionnetwork_config("actionnetwork_get_record", config)
        if isinstance(auth, str):
            return auth
        _, endpoint = _actionnetwork_endpoint(resource, record_id=record_id, parent_id=parent_id)
        return _dump_json(_request_json("GET", f"{base}{endpoint}", headers=auth))
    except Exception as e:
        logger.error("actionnetwork_get_record failed", exc_info=True)
        return f"[Error]: Action Network lookup failed: {e}"


@tool
def actionnetwork_create_person(
    email: str,
    given_name: str = "",
    family_name: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network person.

    Args:
        email: Primary email address.
        given_name: Optional given name.
        family_name: Optional family name.
        fields_json: Optional extra person fields as JSON.
    """
    if not email.strip():
        return "[Error]: email is required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_person", config)
        if isinstance(auth, str):
            return auth
        person = {
            **_json_object(fields_json, field_name="fields_json"),
            "given_name": given_name.strip(),
            "family_name": family_name.strip(),
            "email_addresses": [{"address": email.strip(), "primary": True}],
        }
        return _dump_json(_request_json("POST", f"{base}/people", json_body={"person": _filtered(person)}, headers=auth))
    except Exception as e:
        logger.error("actionnetwork_create_person failed", exc_info=True)
        return f"[Error]: Action Network person creation failed: {e}"


@tool
def actionnetwork_update_person(
    person_id: str,
    fields_json: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an Action Network person with a JSON object of fields."""
    if not person_id.strip():
        return "[Error]: person_id is required."
    try:
        fields = _json_object(fields_json, field_name="fields_json",)
        if not fields:
            return "[Error]: fields_json cannot be empty."
        base, auth = _actionnetwork_config("actionnetwork_update_person", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json("PUT", f"{base}/people/{quote(person_id.strip(), safe='')}", json_body=fields, headers=auth)
        )
    except Exception as e:
        logger.error("actionnetwork_update_person failed", exc_info=True)
        return f"[Error]: Action Network person update failed: {e}"


@tool
def actionnetwork_create_event(
    title: str,
    origin_system: str = "nymeria",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network event."""
    if not title.strip():
        return "[Error]: title is required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_event", config)
        if isinstance(auth, str):
            return auth
        body = {"origin_system": origin_system.strip() or "nymeria", "title": title.strip()}
        body.update(_json_object(fields_json, field_name="fields_json"))
        return _dump_json(_request_json("POST", f"{base}/events", json_body=body, headers=auth))
    except Exception as e:
        logger.error("actionnetwork_create_event failed", exc_info=True)
        return f"[Error]: Action Network event creation failed: {e}"


@tool
def actionnetwork_create_petition(
    title: str,
    origin_system: str = "nymeria",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network petition."""
    if not title.strip():
        return "[Error]: title is required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_petition", config)
        if isinstance(auth, str):
            return auth
        body = {"origin_system": origin_system.strip() or "nymeria", "title": title.strip()}
        body.update(_json_object(fields_json, field_name="fields_json"))
        return _dump_json(_request_json("POST", f"{base}/petitions", json_body=body, headers=auth))
    except Exception as e:
        logger.error("actionnetwork_create_petition failed", exc_info=True)
        return f"[Error]: Action Network petition creation failed: {e}"


@tool
def actionnetwork_create_attendance(
    event_id: str,
    person_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network attendance for an event and person."""
    if not event_id.strip() or not person_id.strip():
        return "[Error]: event_id and person_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_attendance", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json(
                "POST",
                f"{base}/events/{quote(event_id.strip(), safe='')}/attendances",
                json_body=_actionnetwork_person_link(base, person_id),
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_create_attendance failed", exc_info=True)
        return f"[Error]: Action Network attendance creation failed: {e}"


@tool
def actionnetwork_create_signature(
    petition_id: str,
    person_id: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Action Network petition signature for a person."""
    if not petition_id.strip() or not person_id.strip():
        return "[Error]: petition_id and person_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_create_signature", config)
        if isinstance(auth, str):
            return auth
        body = _actionnetwork_person_link(base, person_id)
        body.update(_json_object(fields_json, field_name="fields_json"))
        return _dump_json(
            _request_json(
                "POST",
                f"{base}/petitions/{quote(petition_id.strip(), safe='')}/signatures",
                json_body=body,
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_create_signature failed", exc_info=True)
        return f"[Error]: Action Network signature creation failed: {e}"


@tool
def actionnetwork_add_person_tag(
    tag_id: str,
    person_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Tag an Action Network person."""
    if not tag_id.strip() or not person_id.strip():
        return "[Error]: tag_id and person_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_add_person_tag", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json(
                "POST",
                f"{base}/tags/{quote(tag_id.strip(), safe='')}/taggings",
                json_body=_actionnetwork_person_link(base, person_id),
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_add_person_tag failed", exc_info=True)
        return f"[Error]: Action Network tag add failed: {e}"


@tool
def actionnetwork_remove_person_tag(
    tag_id: str,
    tagging_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove an Action Network person tag by tagging ID."""
    if not tag_id.strip() or not tagging_id.strip():
        return "[Error]: tag_id and tagging_id are required."
    try:
        base, auth = _actionnetwork_config("actionnetwork_remove_person_tag", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(
            _request_json(
                "DELETE",
                f"{base}/tags/{quote(tag_id.strip(), safe='')}/taggings/{quote(tagging_id.strip(), safe='')}",
                headers=auth,
            )
        )
    except Exception as e:
        logger.error("actionnetwork_remove_person_tag failed", exc_info=True)
        return f"[Error]: Action Network tag removal failed: {e}"


@tool
def autopilot_list_contacts(
    list_id: str = "",
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Autopilot contacts, optionally scoped to a list."""
    try:
        base, auth = _autopilot_config("autopilot_list_contacts", config)
        if isinstance(auth, str):
            return auth
        endpoint = f"/list/{quote(list_id.strip(), safe='')}/contacts" if list_id.strip() else "/contacts"
        data = _request_json("GET", f"{base}{endpoint}", params={"limit": _limit(limit, default=100, max_value=500)}, headers=auth)
        contacts = data.get("contacts", data) if isinstance(data, dict) else data
        if isinstance(contacts, list):
            contacts = contacts[: _limit(limit, default=100, max_value=500)]
        return _dump_json(contacts)
    except Exception as e:
        logger.error("autopilot_list_contacts failed", exc_info=True)
        return f"[Error]: Autopilot contact list failed: {e}"


@tool
def autopilot_get_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Autopilot contact by ID."""
    if not contact_id.strip():
        return "[Error]: contact_id is required."
    try:
        base, auth = _autopilot_config("autopilot_get_contact", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/contact/{quote(contact_id.strip(), safe='')}", headers=auth))
    except Exception as e:
        logger.error("autopilot_get_contact failed", exc_info=True)
        return f"[Error]: Autopilot contact lookup failed: {e}"


@tool
def autopilot_upsert_contact(
    email: str,
    fields_json: str = "",
    list_id: str = "",
    session_id: str = "",
    new_email: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update an Autopilot contact."""
    if not email.strip():
        return "[Error]: email is required."
    try:
        base, auth = _autopilot_config("autopilot_upsert_contact", config)
        if isinstance(auth, str):
            return auth
        contact = {"Email": email.strip(), **_json_object(fields_json, field_name="fields_json")}
        if list_id.strip():
            contact["_autopilot_list"] = list_id.strip()
        if session_id.strip():
            contact["_autopilot_session_id"] = session_id.strip()
        if new_email.strip():
            contact["_NewEmail"] = new_email.strip()
        return _dump_json(_request_json("POST", f"{base}/contact", json_body={"contact": contact}, headers=auth))
    except Exception as e:
        logger.error("autopilot_upsert_contact failed", exc_info=True)
        return f"[Error]: Autopilot contact upsert failed: {e}"


@tool
def autopilot_delete_contact(
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete an Autopilot contact."""
    if not contact_id.strip():
        return "[Error]: contact_id is required."
    try:
        base, auth = _autopilot_config("autopilot_delete_contact", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("DELETE", f"{base}/contact/{quote(contact_id.strip(), safe='')}", headers=auth)
        return _dump_json(data or {"success": True})
    except Exception as e:
        logger.error("autopilot_delete_contact failed", exc_info=True)
        return f"[Error]: Autopilot contact delete failed: {e}"


@tool
def autopilot_list_lists(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Autopilot lists."""
    try:
        base, auth = _autopilot_config("autopilot_list_lists", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("GET", f"{base}/lists", headers=auth)
        lists = data.get("lists", data) if isinstance(data, dict) else data
        if isinstance(lists, list):
            lists = lists[: _limit(limit, default=100, max_value=500)]
        return _dump_json(lists)
    except Exception as e:
        logger.error("autopilot_list_lists failed", exc_info=True)
        return f"[Error]: Autopilot list lookup failed: {e}"


@tool
def autopilot_create_list(
    name: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Autopilot list."""
    if not name.strip():
        return "[Error]: name is required."
    try:
        base, auth = _autopilot_config("autopilot_create_list", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("POST", f"{base}/list", json_body={"name": name.strip()}, headers=auth))
    except Exception as e:
        logger.error("autopilot_create_list failed", exc_info=True)
        return f"[Error]: Autopilot list creation failed: {e}"


@tool
def autopilot_update_contact_list_membership(
    list_id: str,
    contact_id: str,
    action: str = "add",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add, remove, or check an Autopilot contact's list membership."""
    normalized = action.strip().lower()
    if normalized not in {"add", "remove", "check"}:
        return '[Error]: action must be "add", "remove", or "check".'
    if not list_id.strip() or not contact_id.strip():
        return "[Error]: list_id and contact_id are required."
    try:
        base, auth = _autopilot_config("autopilot_update_contact_list_membership", config)
        if isinstance(auth, str):
            return auth
        method = {"add": "POST", "remove": "DELETE", "check": "GET"}[normalized]
        data = _request_json(
            method,
            f"{base}/list/{quote(list_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}",
            headers=auth,
        )
        if normalized == "check":
            return _dump_json({"exists": True, "response": data})
        return _dump_json(data or {"success": True})
    except Exception as e:
        if normalized == "check":
            return _dump_json({"exists": False, "error": str(e)})
        logger.error("autopilot_update_contact_list_membership failed", exc_info=True)
        return f"[Error]: Autopilot list membership update failed: {e}"


@tool
def autopilot_add_contact_to_journey(
    trigger_id: str,
    contact_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add an Autopilot contact to a journey trigger."""
    if not trigger_id.strip() or not contact_id.strip():
        return "[Error]: trigger_id and contact_id are required."
    try:
        base, auth = _autopilot_config("autopilot_add_contact_to_journey", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "POST",
            f"{base}/trigger/{quote(trigger_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}",
            headers=auth,
        )
        return _dump_json(data or {"success": True})
    except Exception as e:
        logger.error("autopilot_add_contact_to_journey failed", exc_info=True)
        return f"[Error]: Autopilot journey update failed: {e}"


@tool
def egoi_list_lists(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List E-goi lists."""
    try:
        base, auth = _egoi_config("egoi_list_lists", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("GET", f"{base}/lists", params={"offset": 0, "count": _limit(limit, default=100, max_value=500)}, headers=auth)
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("egoi_list_lists failed", exc_info=True)
        return f"[Error]: E-goi list lookup failed: {e}"


@tool
def egoi_list_contacts(
    list_id: str,
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List contacts in an E-goi list."""
    if not list_id.strip():
        return "[Error]: list_id is required."
    try:
        base, auth = _egoi_config("egoi_list_contacts", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "GET",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts",
            params={"offset": 0, "count": _limit(limit, default=100, max_value=500)},
            headers=auth,
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("egoi_list_contacts failed", exc_info=True)
        return f"[Error]: E-goi contact list failed: {e}"


@tool
def egoi_get_contact(
    list_id: str,
    contact_id: str = "",
    email: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an E-goi contact by contact ID or email."""
    if not list_id.strip() or not (contact_id.strip() or email.strip()):
        return "[Error]: list_id and one of contact_id or email are required."
    try:
        base, auth = _egoi_config("egoi_get_contact", config)
        if isinstance(auth, str):
            return auth
        if contact_id.strip():
            url = f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(contact_id.strip(), safe='')}"
            data = _request_json("GET", url, headers=auth)
        else:
            url = f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts"
            data = _request_json("GET", url, params={"email": email.strip()}, headers=auth)
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("egoi_get_contact failed", exc_info=True)
        return f"[Error]: E-goi contact lookup failed: {e}"


def _egoi_contact_body(email: str = "", fields_json: str = "") -> dict[str, Any]:
    fields = _json_object(fields_json, field_name="fields_json")
    base_fields = fields.pop("base", fields)
    extra_fields = fields.pop("extra", [])
    body = {"base": _filtered({"email": email.strip(), **base_fields}), "extra": extra_fields}
    return body


def _egoi_attach_tags(base: str, auth: dict[str, str], list_id: str, contact_id: Any, tag_ids: str) -> None:
    for tag_id in _csv_to_list(tag_ids):
        _request_json(
            "POST",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/actions/attach-tag",
            json_body={"tag_id": tag_id, "contacts": [contact_id]},
            headers=auth,
        )


@tool
def egoi_create_contact(
    list_id: str,
    email: str,
    fields_json: str = "",
    tag_ids: str = "",
    resolve: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an E-goi contact."""
    if not list_id.strip() or not email.strip():
        return "[Error]: list_id and email are required."
    try:
        base, auth = _egoi_config("egoi_create_contact", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "POST",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts",
            json_body=_egoi_contact_body(email, fields_json),
            headers=auth,
        )
        contact_id = data.get("contact_id") if isinstance(data, dict) else None
        if contact_id and tag_ids.strip():
            _egoi_attach_tags(base, auth, list_id, contact_id, tag_ids)
        if resolve and contact_id:
            data = _request_json("GET", f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(str(contact_id), safe='')}", headers=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("egoi_create_contact failed", exc_info=True)
        return f"[Error]: E-goi contact creation failed: {e}"


@tool
def egoi_update_contact(
    list_id: str,
    contact_id: str,
    fields_json: str,
    tag_ids: str = "",
    resolve: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update an E-goi contact."""
    if not list_id.strip() or not contact_id.strip():
        return "[Error]: list_id and contact_id are required."
    try:
        base, auth = _egoi_config("egoi_update_contact", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "PATCH",
            f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(contact_id.strip(), safe='')}",
            json_body=_egoi_contact_body("", fields_json),
            headers=auth,
        )
        if tag_ids.strip():
            _egoi_attach_tags(base, auth, list_id, contact_id, tag_ids)
        if resolve:
            data = _request_json("GET", f"{base}/lists/{quote(list_id.strip(), safe='')}/contacts/{quote(contact_id.strip(), safe='')}", headers=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("egoi_update_contact failed", exc_info=True)
        return f"[Error]: E-goi contact update failed: {e}"


@tool
def vero_identify_user(
    user_id: str,
    email: str = "",
    data_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update a Vero user profile."""
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        body = {"id": user_id.strip(), "email": email.strip(), "data": json.dumps(_json_object(data_json, field_name="data_json")) if data_json.strip() else ""}
        data = _vero_request("vero_identify_user", "POST", "/users/track", body, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_identify_user failed", exc_info=True)
        return f"[Error]: Vero user identify failed: {e}"


@tool
def vero_alias_user(
    user_id: str,
    new_user_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Alias a Vero user ID to a new user ID."""
    if not user_id.strip() or not new_user_id.strip():
        return "[Error]: user_id and new_user_id are required."
    try:
        data = _vero_request("vero_alias_user", "PUT", "/users/reidentify", {"id": user_id.strip(), "new_id": new_user_id.strip()}, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_alias_user failed", exc_info=True)
        return f"[Error]: Vero user alias failed: {e}"


@tool
def vero_update_user_subscription(
    user_id: str,
    action: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Unsubscribe, resubscribe, or delete a Vero user."""
    normalized = action.strip().lower()
    if normalized not in {"unsubscribe", "resubscribe", "delete"}:
        return '[Error]: action must be "unsubscribe", "resubscribe", or "delete".'
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        data = _vero_request("vero_update_user_subscription", "POST", f"/users/{normalized}", {"id": user_id.strip()}, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_update_user_subscription failed", exc_info=True)
        return f"[Error]: Vero user subscription update failed: {e}"


@tool
def vero_update_user_tags(
    user_id: str,
    add_tags: str = "",
    remove_tags: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add or remove Vero user tags."""
    if not user_id.strip():
        return "[Error]: user_id is required."
    if not add_tags.strip() and not remove_tags.strip():
        return "[Error]: add_tags or remove_tags is required."
    try:
        body: dict[str, Any] = {"id": user_id.strip()}
        if add_tags.strip():
            body["add"] = json.dumps(_csv_to_list(add_tags))
        if remove_tags.strip():
            body["remove"] = json.dumps(_csv_to_list(remove_tags))
        data = _vero_request("vero_update_user_tags", "PUT", "/users/tags/edit", body, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_update_user_tags failed", exc_info=True)
        return f"[Error]: Vero user tag update failed: {e}"


@tool
def vero_track_event(
    user_id: str,
    email: str,
    event_name: str,
    data_json: str = "",
    extras_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Track a Vero event for a user."""
    if not user_id.strip() or not email.strip() or not event_name.strip():
        return "[Error]: user_id, email, and event_name are required."
    try:
        body = {
            "identity": json.dumps({"id": user_id.strip(), "email": email.strip()}),
            "email": email.strip(),
            "event_name": event_name.strip(),
            "data": json.dumps(_json_object(data_json, field_name="data_json")) if data_json.strip() else "",
            "extras": json.dumps(_json_object(extras_json, field_name="extras_json")) if extras_json.strip() else "",
        }
        data = _vero_request("vero_track_event", "POST", "/events/track", body, config=config)
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("vero_track_event failed", exc_info=True)
        return f"[Error]: Vero event tracking failed: {e}"


@tool
def lemlist_list_campaigns(
    filters_json: str = "",
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Lemlist campaigns."""
    try:
        base, auth = _lemlist_config("lemlist_list_campaigns", config)
        if isinstance(auth, str):
            return auth
        params = {**_json_object(filters_json, field_name="filters_json"), "limit": _limit(limit, default=100)}
        data = _request_json("GET", f"{base}/campaigns", params=params, headers=auth)
        return _dump_json(data[: _limit(limit, default=100)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("lemlist_list_campaigns failed", exc_info=True)
        return f"[Error]: Lemlist campaign listing failed: {e}"


@tool
def lemlist_get_campaign_stats(
    campaign_id: str,
    start_date: str = "",
    end_date: str = "",
    timezone: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get Lemlist campaign stats."""
    if not campaign_id.strip():
        return "[Error]: campaign_id is required."
    try:
        base, auth = _lemlist_config("lemlist_get_campaign_stats", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "GET",
            f"{base}/campaigns/{quote(campaign_id.strip(), safe='')}/stats",
            params={"startDate": start_date.strip(), "endDate": end_date.strip(), "timezone": timezone.strip()},
            headers=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("lemlist_get_campaign_stats failed", exc_info=True)
        return f"[Error]: Lemlist campaign stats lookup failed: {e}"


@tool
def lemlist_list_activities(
    filters_json: str = "",
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Lemlist activities."""
    try:
        base, auth = _lemlist_config("lemlist_list_activities", config)
        if isinstance(auth, str):
            return auth
        params = {**_json_object(filters_json, field_name="filters_json"), "limit": _limit(limit, default=100)}
        data = _request_json("GET", f"{base}/activities", params=params, headers=auth)
        return _dump_json(data[: _limit(limit, default=100)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("lemlist_list_activities failed", exc_info=True)
        return f"[Error]: Lemlist activity listing failed: {e}"


@tool
def lemlist_get_lead(
    email: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Lemlist lead by email."""
    if "@" not in email:
        return "[Error]: email must look like an email address."
    try:
        base, auth = _lemlist_config("lemlist_get_lead", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("GET", f"{base}/leads/{quote(email.strip(), safe='')}", headers=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("lemlist_get_lead failed", exc_info=True)
        return f"[Error]: Lemlist lead lookup failed: {e}"


@tool
def lemlist_create_lead(
    campaign_id: str,
    email: str,
    fields_json: str = "",
    deduplicate: bool = True,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create or update a Lemlist campaign lead."""
    if not campaign_id.strip() or "@" not in email:
        return "[Error]: campaign_id and a valid email are required."
    try:
        base, auth = _lemlist_config("lemlist_create_lead", config)
        if isinstance(auth, str):
            return auth
        data = _request_json(
            "POST",
            f"{base}/campaigns/{quote(campaign_id.strip(), safe='')}/leads/{quote(email.strip(), safe='')}",
            params={"deduplicate": deduplicate},
            json_body=_json_object(fields_json, field_name="fields_json"),
            headers=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("lemlist_create_lead failed", exc_info=True)
        return f"[Error]: Lemlist lead creation failed: {e}"


@tool
def lemlist_remove_lead(
    campaign_id: str,
    email: str,
    unsubscribe: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove or unsubscribe a Lemlist lead from a campaign."""
    if not campaign_id.strip() or "@" not in email:
        return "[Error]: campaign_id and a valid email are required."
    try:
        base, auth = _lemlist_config("lemlist_remove_lead", config)
        if isinstance(auth, str):
            return auth
        params = {} if unsubscribe else {"action": "remove"}
        data = _request_json(
            "DELETE",
            f"{base}/campaigns/{quote(campaign_id.strip(), safe='')}/leads/{quote(email.strip(), safe='')}",
            params=params,
            headers=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("lemlist_remove_lead failed", exc_info=True)
        return f"[Error]: Lemlist lead removal failed: {e}"


@tool
def lemlist_get_team(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """Get Lemlist team metadata."""
    try:
        base, auth = _lemlist_config("lemlist_get_team", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/team", headers=auth))
    except Exception as e:
        logger.error("lemlist_get_team failed", exc_info=True)
        return f"[Error]: Lemlist team lookup failed: {e}"


@tool
def lemlist_get_team_credits(config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
    """Get Lemlist team credit balances."""
    try:
        base, auth = _lemlist_config("lemlist_get_team_credits", config)
        if isinstance(auth, str):
            return auth
        return _dump_json(_request_json("GET", f"{base}/team/credits", headers=auth))
    except Exception as e:
        logger.error("lemlist_get_team_credits failed", exc_info=True)
        return f"[Error]: Lemlist team credits lookup failed: {e}"


@tool
def lemlist_list_unsubscribes(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Lemlist global unsubscribes."""
    try:
        base, auth = _lemlist_config("lemlist_list_unsubscribes", config)
        if isinstance(auth, str):
            return auth
        data = _request_json("GET", f"{base}/unsubscribes", params={"limit": _limit(limit, default=100)}, headers=auth)
        return _dump_json(data[: _limit(limit, default=100)] if isinstance(data, list) else data)
    except Exception as e:
        logger.error("lemlist_list_unsubscribes failed", exc_info=True)
        return f"[Error]: Lemlist unsubscribe listing failed: {e}"


@tool
def lemlist_update_unsubscribe(
    email: str,
    action: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add or remove a Lemlist global unsubscribe."""
    normalized = action.strip().lower()
    if normalized not in {"add", "delete", "remove"}:
        return '[Error]: action must be "add", "delete", or "remove".'
    if "@" not in email:
        return "[Error]: email must look like an email address."
    try:
        base, auth = _lemlist_config("lemlist_update_unsubscribe", config)
        if isinstance(auth, str):
            return auth
        method = "POST" if normalized == "add" else "DELETE"
        data = _request_json(method, f"{base}/unsubscribes/{quote(email.strip(), safe='')}", headers=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("lemlist_update_unsubscribe failed", exc_info=True)
        return f"[Error]: Lemlist unsubscribe update failed: {e}"


@tool
def sendy_create_campaign(
    from_name: str,
    from_email: str,
    reply_to: str,
    title: str,
    subject: str,
    html_text: str,
    send_campaign: bool = False,
    brand_id: str = "",
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Sendy campaign."""
    required = [from_name, from_email, reply_to, title, subject, html_text]
    if any(not value.strip() for value in required):
        return "[Error]: from_name, from_email, reply_to, title, subject, and html_text are required."
    try:
        body = {
            "from_name": from_name.strip(),
            "from_email": from_email.strip(),
            "reply_to": reply_to.strip(),
            "title": title.strip(),
            "subject": subject.strip(),
            "html_text": html_text,
            "send_campaign": 1 if send_campaign else 0,
            **_json_object(fields_json, field_name="fields_json"),
        }
        if brand_id.strip():
            body["brand_id"] = brand_id.strip()
        data = _sendy_request("sendy_create_campaign", "/api/campaigns/create.php", body, config)
        return _json_or_text({"message": data} if isinstance(data, str) else data)
    except Exception as e:
        logger.error("sendy_create_campaign failed", exc_info=True)
        return f"[Error]: Sendy campaign creation failed: {e}"


@tool
def sendy_add_subscriber(
    email: str,
    list_id: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add a Sendy subscriber to a list."""
    if "@" not in email or not list_id.strip():
        return "[Error]: email and list_id are required."
    try:
        data = _sendy_request(
            "sendy_add_subscriber",
            "/subscribe",
            {"email": email.strip(), "list": list_id.strip(), **_json_object(fields_json, field_name="fields_json")},
            config,
        )
        return _json_or_text({"success": True} if data == "1" else data)
    except Exception as e:
        logger.error("sendy_add_subscriber failed", exc_info=True)
        return f"[Error]: Sendy subscriber add failed: {e}"


@tool
def sendy_get_subscriber_status(
    email: str,
    list_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Sendy subscriber status."""
    if "@" not in email or not list_id.strip():
        return "[Error]: email and list_id are required."
    try:
        data = _sendy_request(
            "sendy_get_subscriber_status",
            "/api/subscribers/subscription-status.php",
            {"email": email.strip(), "list_id": list_id.strip()},
            config,
        )
        return _json_or_text({"status": data} if isinstance(data, str) else data)
    except Exception as e:
        logger.error("sendy_get_subscriber_status failed", exc_info=True)
        return f"[Error]: Sendy subscriber status lookup failed: {e}"


@tool
def sendy_count_active_subscribers(
    list_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Count active Sendy subscribers in a list."""
    if not list_id.strip():
        return "[Error]: list_id is required."
    try:
        data = _sendy_request(
            "sendy_count_active_subscribers",
            "/api/subscribers/active-subscriber-count.php",
            {"list_id": list_id.strip()},
            config,
        )
        return _json_or_text({"count": data} if isinstance(data, str) else data)
    except Exception as e:
        logger.error("sendy_count_active_subscribers failed", exc_info=True)
        return f"[Error]: Sendy subscriber count failed: {e}"


@tool
def sendy_update_subscriber_subscription(
    email: str,
    list_id: str,
    action: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Unsubscribe, remove, or delete a Sendy subscriber."""
    normalized = action.strip().lower()
    if normalized not in {"unsubscribe", "remove", "delete"}:
        return '[Error]: action must be "unsubscribe", "remove", or "delete".'
    if "@" not in email or not list_id.strip():
        return "[Error]: email and list_id are required."
    path = "/api/subscribers/delete.php" if normalized == "delete" else "/unsubscribe"
    key = "list_id" if normalized == "delete" else "list"
    try:
        data = _sendy_request(
            "sendy_update_subscriber_subscription",
            path,
            {"email": email.strip(), key: list_id.strip()},
            config,
        )
        return _json_or_text({"success": True} if data == "1" else data)
    except Exception as e:
        logger.error("sendy_update_subscriber_subscription failed", exc_info=True)
        return f"[Error]: Sendy subscriber subscription update failed: {e}"


@tool
def emelia_list_campaigns(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Emelia campaigns."""
    try:
        data = _emelia_graphql(
            "emelia_list_campaigns",
            """
            query all_campaigns {
              all_campaigns {
                _id
                name
                status
                createdAt
                stats {
                  mailsSent
                  uniqueOpensPercent
                  opens
                  linkClickedPercent
                  repliedPercent
                  bouncedPercent
                  unsubscribePercent
                  progressPercent
                }
              }
            }
            """,
            operation_name="all_campaigns",
            config=config,
        )
        if isinstance(data, str):
            return data
        campaigns = data.get("data", {}).get("all_campaigns", [])
        return _dump_json(campaigns[: _limit(limit, default=100)])
    except Exception as e:
        logger.error("emelia_list_campaigns failed", exc_info=True)
        return f"[Error]: Emelia campaign listing failed: {e}"


@tool
def emelia_get_campaign(
    campaign_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get an Emelia campaign."""
    if not campaign_id.strip():
        return "[Error]: campaign_id is required."
    try:
        data = _emelia_graphql(
            "emelia_get_campaign",
            """
            query campaign($id: ID!) {
              campaign(id: $id) {
                _id
                name
                status
                createdAt
                provider
                startAt
                estimatedEnd
                recipients { total_count }
              }
            }
            """,
            operation_name="campaign",
            variables={"id": campaign_id.strip()},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("data", {}).get("campaign", data))
    except Exception as e:
        logger.error("emelia_get_campaign failed", exc_info=True)
        return f"[Error]: Emelia campaign lookup failed: {e}"


@tool
def emelia_create_campaign(
    name: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create an Emelia campaign."""
    if not name.strip():
        return "[Error]: name is required."
    try:
        data = _emelia_graphql(
            "emelia_create_campaign",
            """
            mutation createCampaign($name: String!) {
              createCampaign(name: $name) {
                _id
                name
                status
                createdAt
                provider
                startAt
                estimatedEnd
              }
            }
            """,
            operation_name="createCampaign",
            variables={"name": name.strip()},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data.get("data", {}).get("createCampaign", data))
    except Exception as e:
        logger.error("emelia_create_campaign failed", exc_info=True)
        return f"[Error]: Emelia campaign creation failed: {e}"


@tool
def emelia_update_campaign_status(
    campaign_id: str,
    action: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Start or pause an Emelia campaign."""
    normalized = action.strip().lower()
    if normalized not in {"start", "pause"}:
        return '[Error]: action must be "start" or "pause".'
    if not campaign_id.strip():
        return "[Error]: campaign_id is required."
    operation = "startCampaign" if normalized == "start" else "pauseCampaign"
    try:
        data = _emelia_graphql(
            "emelia_update_campaign_status",
            f"mutation {operation}($id: ID!) {{ {operation}(id: $id) }}",
            operation_name=operation,
            variables={"id": campaign_id.strip()},
            config=config,
        )
        return data if isinstance(data, str) else _dump_json({"success": True})
    except Exception as e:
        logger.error("emelia_update_campaign_status failed", exc_info=True)
        return f"[Error]: Emelia campaign status update failed: {e}"


@tool
def emelia_duplicate_campaign(
    campaign_id: str,
    name: str,
    options_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Duplicate an Emelia campaign."""
    if not campaign_id.strip() or not name.strip():
        return "[Error]: campaign_id and name are required."
    variables = {
        "fromId": campaign_id.strip(),
        "name": name.strip(),
        "copySettings": True,
        "copyMails": True,
        "copyContacts": False,
        "copyProvider": True,
        **_json_object(options_json, field_name="options_json"),
    }
    try:
        data = _emelia_graphql(
            "emelia_duplicate_campaign",
            """
            mutation duplicateCampaign(
              $fromId: ID!
              $name: String!
              $copySettings: Boolean!
              $copyMails: Boolean!
              $copyContacts: Boolean!
              $copyProvider: Boolean!
            ) {
              duplicateCampaign(
                fromId: $fromId
                name: $name
                copySettings: $copySettings
                copyMails: $copyMails
                copyContacts: $copyContacts
                copyProvider: $copyProvider
              )
            }
            """,
            operation_name="duplicateCampaign",
            variables=variables,
            config=config,
        )
        duplicate_id = data.get("data", {}).get("duplicateCampaign") if isinstance(data, dict) else None
        return data if isinstance(data, str) else _dump_json({"_id": duplicate_id})
    except Exception as e:
        logger.error("emelia_duplicate_campaign failed", exc_info=True)
        return f"[Error]: Emelia campaign duplication failed: {e}"


@tool
def emelia_add_contact_to_campaign(
    campaign_id: str,
    email: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add a contact to an Emelia campaign."""
    if not campaign_id.strip() or "@" not in email:
        return "[Error]: campaign_id and a valid email are required."
    contact = {"email": email.strip(), **_json_object(fields_json, field_name="fields_json")}
    try:
        data = _emelia_graphql(
            "emelia_add_contact_to_campaign",
            """
            mutation AddContactToCampaignHook($id: ID!, $contact: JSON!) {
              addContactToCampaignHook(id: $id, contact: $contact)
            }
            """,
            operation_name="AddContactToCampaignHook",
            variables={"id": campaign_id.strip(), "contact": contact},
            config=config,
        )
        contact_id = data.get("data", {}).get("addContactToCampaignHook") if isinstance(data, dict) else None
        return data if isinstance(data, str) else _dump_json({"contactId": contact_id})
    except Exception as e:
        logger.error("emelia_add_contact_to_campaign failed", exc_info=True)
        return f"[Error]: Emelia campaign contact add failed: {e}"


@tool
def emelia_list_contact_lists(
    limit: int = 100,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Emelia contact lists."""
    try:
        data = _emelia_graphql(
            "emelia_list_contact_lists",
            """
            query contact_lists {
              contact_lists {
                _id
                name
                contactCount
                fields
                usedInCampaign
              }
            }
            """,
            operation_name="contact_lists",
            config=config,
        )
        if isinstance(data, str):
            return data
        lists = data.get("data", {}).get("contact_lists", [])
        return _dump_json(lists[: _limit(limit, default=100)])
    except Exception as e:
        logger.error("emelia_list_contact_lists failed", exc_info=True)
        return f"[Error]: Emelia contact-list listing failed: {e}"


@tool
def emelia_add_contact_to_list(
    contact_list_id: str,
    email: str,
    fields_json: str = "",
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add a contact to an Emelia contact list."""
    if not contact_list_id.strip() or "@" not in email:
        return "[Error]: contact_list_id and a valid email are required."
    contact = {"email": email.strip(), **_json_object(fields_json, field_name="fields_json")}
    try:
        data = _emelia_graphql(
            "emelia_add_contact_to_list",
            """
            mutation AddContactsToListHook($id: ID!, $contact: JSON!) {
              addContactsToListHook(id: $id, contact: $contact)
            }
            """,
            operation_name="AddContactsToListHook",
            variables={"id": contact_list_id.strip(), "contact": contact},
            config=config,
        )
        contact_id = data.get("data", {}).get("addContactsToListHook") if isinstance(data, dict) else None
        return data if isinstance(data, str) else _dump_json({"contactId": contact_id})
    except Exception as e:
        logger.error("emelia_add_contact_to_list failed", exc_info=True)
        return f"[Error]: Emelia contact-list contact add failed: {e}"


@tool
def mautic_list_contacts(
    search: str = "",
    order_by: str = "",
    order_direction: str = "",
    start: int = 0,
    limit: int = 30,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Mautic contacts.

    Args:
        search: Optional Mautic search string.
        order_by: Optional field to sort by.
        order_direction: Optional sort direction, asc or desc.
        start: Result offset.
        limit: Number of contacts to return, 1-100.
        raw_data: Return raw Mautic entities instead of flattened fields.
    """
    try:
        per_page = _limit(limit, default=30)
        data = _mautic_request(
            "mautic_list_contacts",
            "GET",
            "/contacts",
            params={
                "search": search.strip(),
                "orderBy": order_by.strip(),
                "orderByDir": order_direction.strip(),
                "start": max(0, int(start or 0)),
                "limit": per_page,
            },
            config=config,
        )
        if isinstance(data, str):
            return data
        records = _mautic_collection(data, "contacts", limit=per_page)
        if not raw_data and isinstance(records, list):
            records = [_mautic_entity(record, "contact", simple=True) for record in records]
        return _dump_json(records)
    except Exception as e:
        logger.error("mautic_list_contacts failed", exc_info=True)
        return f"[Error]: Mautic contact listing failed: {e}"


@tool
def mautic_get_contact(
    contact_id: str,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Mautic contact by ID."""
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        data = _mautic_request("mautic_get_contact", "GET", f"/contacts/{quote(contact_id, safe='')}", config=config)
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "contact", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_get_contact failed", exc_info=True)
        return f"[Error]: Mautic contact lookup failed: {e}"


@tool
def mautic_create_contact(
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    position: str = "",
    title: str = "",
    fields_json: str = "",
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Mautic contact.

    Args:
        email: Contact email address.
        first_name: Optional first name.
        last_name: Optional last name.
        company: Optional company name.
        position: Optional role or position.
        title: Optional title.
        fields_json: Optional JSON object of additional Mautic contact fields.
        raw_data: Return raw Mautic entity instead of flattened fields.
    """
    try:
        body = _json_object(fields_json, field_name="fields_json")
        body.update(
            _filtered(
                {
                    "email": email.strip(),
                    "firstname": first_name.strip(),
                    "lastname": last_name.strip(),
                    "company": company.strip(),
                    "position": position.strip(),
                    "title": title.strip(),
                }
            )
        )
        if not body:
            return "[Error]: provide email or fields_json."
        data = _mautic_request("mautic_create_contact", "POST", "/contacts/new", json_body=body, config=config)
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "contact", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_create_contact failed", exc_info=True)
        return f"[Error]: Mautic contact creation failed: {e}"


@tool
def mautic_update_contact(
    contact_id: str,
    fields_json: str,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Mautic contact with a JSON object of Mautic field aliases."""
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        body = _json_object(fields_json, field_name="fields_json")
        if not body:
            return "[Error]: fields_json is required."
        data = _mautic_request(
            "mautic_update_contact",
            "PATCH",
            f"/contacts/{quote(contact_id, safe='')}/edit",
            json_body=body,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "contact", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_update_contact failed", exc_info=True)
        return f"[Error]: Mautic contact update failed: {e}"


@tool
def mautic_delete_contact(
    contact_id: str,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Mautic contact by ID."""
    contact_id = contact_id.strip()
    if not contact_id:
        return "[Error]: contact_id is required."
    try:
        data = _mautic_request(
            "mautic_delete_contact",
            "DELETE",
            f"/contacts/{quote(contact_id, safe='')}/delete",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "contact", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_delete_contact failed", exc_info=True)
        return f"[Error]: Mautic contact deletion failed: {e}"


@tool
def mautic_list_companies(
    search: str = "",
    order_by: str = "",
    order_direction: str = "",
    start: int = 0,
    limit: int = 30,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """List Mautic companies."""
    try:
        per_page = _limit(limit, default=30)
        data = _mautic_request(
            "mautic_list_companies",
            "GET",
            "/companies",
            params={
                "search": search.strip(),
                "orderBy": order_by.strip(),
                "orderByDir": order_direction.strip(),
                "start": max(0, int(start or 0)),
                "limit": per_page,
            },
            config=config,
        )
        if isinstance(data, str):
            return data
        records = _mautic_collection(data, "companies", limit=per_page)
        if not raw_data and isinstance(records, list):
            records = [_mautic_entity(record, "company", simple=True) for record in records]
        return _dump_json(records)
    except Exception as e:
        logger.error("mautic_list_companies failed", exc_info=True)
        return f"[Error]: Mautic company listing failed: {e}"


@tool
def mautic_get_company(
    company_id: str,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Get a Mautic company by ID."""
    company_id = company_id.strip()
    if not company_id:
        return "[Error]: company_id is required."
    try:
        data = _mautic_request("mautic_get_company", "GET", f"/companies/{quote(company_id, safe='')}", config=config)
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "company", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_get_company failed", exc_info=True)
        return f"[Error]: Mautic company lookup failed: {e}"


@tool
def mautic_create_company(
    name: str,
    fields_json: str = "",
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Create a Mautic company."""
    if not name.strip():
        return "[Error]: name is required."
    try:
        body = {"companyname": name.strip(), **_json_object(fields_json, field_name="fields_json")}
        data = _mautic_request("mautic_create_company", "POST", "/companies/new", json_body=body, config=config)
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "company", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_create_company failed", exc_info=True)
        return f"[Error]: Mautic company creation failed: {e}"


@tool
def mautic_update_company(
    company_id: str,
    fields_json: str,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Update a Mautic company with a JSON object of Mautic field aliases."""
    company_id = company_id.strip()
    if not company_id:
        return "[Error]: company_id is required."
    try:
        body = _json_object(fields_json, field_name="fields_json")
        if not body:
            return "[Error]: fields_json is required."
        data = _mautic_request(
            "mautic_update_company",
            "PATCH",
            f"/companies/{quote(company_id, safe='')}/edit",
            json_body=body,
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "company", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_update_company failed", exc_info=True)
        return f"[Error]: Mautic company update failed: {e}"


@tool
def mautic_delete_company(
    company_id: str,
    raw_data: bool = False,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Delete a Mautic company by ID."""
    company_id = company_id.strip()
    if not company_id:
        return "[Error]: company_id is required."
    try:
        data = _mautic_request(
            "mautic_delete_company",
            "DELETE",
            f"/companies/{quote(company_id, safe='')}/delete",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(_mautic_entity(data, "company", simple=not raw_data))
    except Exception as e:
        logger.error("mautic_delete_company failed", exc_info=True)
        return f"[Error]: Mautic company deletion failed: {e}"


@tool
def mautic_add_contact_to_segment(
    contact_id: str,
    segment_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add a Mautic contact to a segment."""
    if not contact_id.strip() or not segment_id.strip():
        return "[Error]: contact_id and segment_id are required."
    try:
        data = _mautic_request(
            "mautic_add_contact_to_segment",
            "POST",
            f"/segments/{quote(segment_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/add",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_add_contact_to_segment failed", exc_info=True)
        return f"[Error]: Mautic segment contact add failed: {e}"


@tool
def mautic_remove_contact_from_segment(
    contact_id: str,
    segment_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove a Mautic contact from a segment."""
    if not contact_id.strip() or not segment_id.strip():
        return "[Error]: contact_id and segment_id are required."
    try:
        data = _mautic_request(
            "mautic_remove_contact_from_segment",
            "POST",
            f"/segments/{quote(segment_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/remove",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_remove_contact_from_segment failed", exc_info=True)
        return f"[Error]: Mautic segment contact removal failed: {e}"


@tool
def mautic_add_contact_to_campaign(
    contact_id: str,
    campaign_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add a Mautic contact to a campaign."""
    if not contact_id.strip() or not campaign_id.strip():
        return "[Error]: contact_id and campaign_id are required."
    try:
        data = _mautic_request(
            "mautic_add_contact_to_campaign",
            "POST",
            f"/campaigns/{quote(campaign_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/add",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_add_contact_to_campaign failed", exc_info=True)
        return f"[Error]: Mautic campaign contact add failed: {e}"


@tool
def mautic_remove_contact_from_campaign(
    contact_id: str,
    campaign_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove a Mautic contact from a campaign."""
    if not contact_id.strip() or not campaign_id.strip():
        return "[Error]: contact_id and campaign_id are required."
    try:
        data = _mautic_request(
            "mautic_remove_contact_from_campaign",
            "POST",
            f"/campaigns/{quote(campaign_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/remove",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_remove_contact_from_campaign failed", exc_info=True)
        return f"[Error]: Mautic campaign contact removal failed: {e}"


@tool
def mautic_add_contact_to_company(
    contact_id: str,
    company_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Add a Mautic contact to a company."""
    if not contact_id.strip() or not company_id.strip():
        return "[Error]: contact_id and company_id are required."
    try:
        data = _mautic_request(
            "mautic_add_contact_to_company",
            "POST",
            f"/companies/{quote(company_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/add",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_add_contact_to_company failed", exc_info=True)
        return f"[Error]: Mautic company contact add failed: {e}"


@tool
def mautic_remove_contact_from_company(
    contact_id: str,
    company_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Remove a Mautic contact from a company."""
    if not contact_id.strip() or not company_id.strip():
        return "[Error]: contact_id and company_id are required."
    try:
        data = _mautic_request(
            "mautic_remove_contact_from_company",
            "POST",
            f"/companies/{quote(company_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/remove",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_remove_contact_from_company failed", exc_info=True)
        return f"[Error]: Mautic company contact removal failed: {e}"


@tool
def mautic_send_email_to_contact(
    contact_id: str,
    email_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Mautic campaign/template email to a contact."""
    if not contact_id.strip() or not email_id.strip():
        return "[Error]: contact_id and email_id are required."
    try:
        data = _mautic_request(
            "mautic_send_email_to_contact",
            "POST",
            f"/emails/{quote(email_id.strip(), safe='')}/contact/{quote(contact_id.strip(), safe='')}/send",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_send_email_to_contact failed", exc_info=True)
        return f"[Error]: Mautic contact email send failed: {e}"


@tool
def mautic_send_segment_email(
    email_id: str,
    config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None,
) -> str:
    """Send a Mautic segment/list email."""
    if not email_id.strip():
        return "[Error]: email_id is required."
    try:
        data = _mautic_request(
            "mautic_send_segment_email",
            "POST",
            f"/emails/{quote(email_id.strip(), safe='')}/send",
            config=config,
        )
        return data if isinstance(data, str) else _dump_json(data)
    except Exception as e:
        logger.error("mautic_send_segment_email failed", exc_info=True)
        return f"[Error]: Mautic segment email send failed: {e}"


MARKETING_CONTACT_SERVICE_TOOLS = [
    customerio_list_campaigns,
    customerio_get_campaign,
    customerio_upsert_customer,
    customerio_track_event,
    customerio_track_anonymous_event,
    customerio_update_segment,
    iterable_list_lists,
    iterable_get_user,
    iterable_upsert_user,
    iterable_track_event,
    iterable_update_list_subscribers,
    posthog_capture_event,
    posthog_identify,
    posthog_create_alias,
    posthog_track_page_or_screen,
    segment_identify,
    segment_track,
    segment_group,
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
    actionnetwork_list_records,
    actionnetwork_get_record,
    actionnetwork_create_person,
    actionnetwork_update_person,
    actionnetwork_create_event,
    actionnetwork_create_petition,
    actionnetwork_create_attendance,
    actionnetwork_create_signature,
    actionnetwork_add_person_tag,
    actionnetwork_remove_person_tag,
    autopilot_list_contacts,
    autopilot_get_contact,
    autopilot_upsert_contact,
    autopilot_delete_contact,
    autopilot_list_lists,
    autopilot_create_list,
    autopilot_update_contact_list_membership,
    autopilot_add_contact_to_journey,
    egoi_list_lists,
    egoi_list_contacts,
    egoi_get_contact,
    egoi_create_contact,
    egoi_update_contact,
    vero_identify_user,
    vero_alias_user,
    vero_update_user_subscription,
    vero_update_user_tags,
    vero_track_event,
    lemlist_list_campaigns,
    lemlist_get_campaign_stats,
    lemlist_list_activities,
    lemlist_get_lead,
    lemlist_create_lead,
    lemlist_remove_lead,
    lemlist_get_team,
    lemlist_get_team_credits,
    lemlist_list_unsubscribes,
    lemlist_update_unsubscribe,
    sendy_create_campaign,
    sendy_add_subscriber,
    sendy_get_subscriber_status,
    sendy_count_active_subscribers,
    sendy_update_subscriber_subscription,
    emelia_list_campaigns,
    emelia_get_campaign,
    emelia_create_campaign,
    emelia_update_campaign_status,
    emelia_duplicate_campaign,
    emelia_add_contact_to_campaign,
    emelia_list_contact_lists,
    emelia_add_contact_to_list,
    mautic_list_contacts,
    mautic_get_contact,
    mautic_create_contact,
    mautic_update_contact,
    mautic_delete_contact,
    mautic_list_companies,
    mautic_get_company,
    mautic_create_company,
    mautic_update_company,
    mautic_delete_company,
    mautic_add_contact_to_segment,
    mautic_remove_contact_from_segment,
    mautic_add_contact_to_campaign,
    mautic_remove_contact_from_campaign,
    mautic_add_contact_to_company,
    mautic_remove_contact_from_company,
    mautic_send_email_to_contact,
    mautic_send_segment_email,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="marketing_contact", tools=tuple(MARKETING_CONTACT_SERVICE_TOOLS)))
