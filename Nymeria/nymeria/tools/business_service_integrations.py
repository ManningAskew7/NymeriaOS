"""Business data and language service integration tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

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
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_BITLY_BASE_URL = "https://api-ssl.bitly.com/v4"
_BRANDFETCH_BASE_URL = "https://api.brandfetch.io/v2"
_MARKETSTACK_BASE_URL = "https://api.marketstack.com/v1"
_DEEPL_PRO_BASE_URL = "https://api.deepl.com/v2"
_DEEPL_FREE_BASE_URL = "https://api-free.deepl.com/v2"
_LINGVANEX_BASE_URL = "https://api-b2b.backenster.com/b1/api/v3"
_APITEMPLATE_BASE_URL = "https://api.apitemplate.io/v1"
_ONESIMPLE_BASE_URL = "https://onesimpleapi.com/api"
_DHL_BASE_URL = "https://api-eu.dhl.com"
_ONFLEET_BASE_URL = "https://onfleet.com/api/v2"
_PHANTOMBUSTER_BASE_URL = "https://api.phantombuster.com/api/v2"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_BITLY = register_provider_spec(
    ProviderCredentialSpec(
        provider="bitly",
        aliases=("bitly_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="access_token", names=("access_token", "token", "api_key", "value")
            ),
        ),
        hint_fields=("access_token", "value"),
        env_var="BITLY_TOKEN",
        display_name="Bitly",
    )
)

_BRANDFETCH = register_provider_spec(
    ProviderCredentialSpec(
        provider="brandfetch",
        aliases=("brandfetch_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="api_key", names=("api_key", "token", "value")),
        ),
        hint_fields=("api_key", "value"),
        env_var="BRANDFETCH_API_KEY",
        display_name="Brandfetch",
    )
)

_MARKETSTACK = register_provider_spec(
    ProviderCredentialSpec(
        provider="marketstack",
        aliases=("marketstack_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "access_key", "token", "value")
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="MARKETSTACK_API_KEY",
        display_name="Marketstack",
    )
)

_DEEPL = register_provider_spec(
    ProviderCredentialSpec(
        provider="deepl",
        aliases=("deepl_api",),
        groups=(
            CredentialFieldGroup(role="api_plan", names=("api_plan", "plan"), required=False),
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "auth_key", "token", "value")
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="DEEPL_API_KEY",
        display_name="DeepL",
    )
)

_APITEMPLATE = register_provider_spec(
    ProviderCredentialSpec(
        provider="apitemplate",
        aliases=("apitemplate_io", "api_template", "api_template_io"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "value"),
        env_var="APITEMPLATE_API_KEY",
        display_name="APITemplate",
    )
)

# lingvanex resolves through the shared _bearer_config helper: the helper keeps
# the base_url tuple and the default field_names tuple inline, so those groups
# are declared here for the registry but referenced inline in the helper body.
_LINGVANEX = register_provider_spec(
    ProviderCredentialSpec(
        provider="lingvanex",
        aliases=("lingvanex_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
        env_var="LINGVANEX_API_KEY",
        display_name="LingvaNex",
    )
)

_ONESIMPLE = register_provider_spec(
    ProviderCredentialSpec(
        provider="onesimple",
        aliases=("one_simple_api", "onesimpleapi"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="api_token",
                names=("api_token", "apiToken", "token", "api_key", "apiKey", "value"),
            ),
        ),
        hint_fields=("api_token", "value"),
        env_var="ONESIMPLE_API_TOKEN",
        display_name="One Simple API",
    )
)

_DHL = register_provider_spec(
    ProviderCredentialSpec(
        provider="dhl",
        aliases=("dhl_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "apiKey", "key", "token", "value")
            ),
        ),
        hint_fields=("api_key", "value"),
        env_var="DHL_API_KEY",
        display_name="DHL",
    )
)

_ONFLEET = register_provider_spec(
    ProviderCredentialSpec(
        provider="onfleet",
        aliases=("onfleet_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "value"),
        env_var="ONFLEET_API_KEY",
        display_name="Onfleet",
    )
)

_PHANTOMBUSTER = register_provider_spec(
    ProviderCredentialSpec(
        provider="phantombuster",
        aliases=("phantombuster_api", "phantom_buster"),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
        ),
        hint_fields=("api_key", "value"),
        env_var="PHANTOMBUSTER_API_KEY",
        display_name="Phantombuster",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _csv(value: str) -> str:
    return ",".join(_split_csv(value))


def _filtered_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != []
    }


def _json_object(value: str, *, field_name: str, allow_empty: bool = True) -> dict[str, Any]:
    if not value.strip():
        if allow_empty:
            return {}
        raise ValueError(f"{field_name} is required")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _json_array(value: str, *, field_name: str) -> list[Any]:
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return parsed


def _limit(value: int, *, default: int = 20, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _absolute_url(value: str, *, field_name: str = "url") -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL")
    return value.strip()


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
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered_params(params),
                json=json_body,
                data=data,
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
                or body.get("description")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _bitly_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_BITLY.provider,
            provider_aliases=_BITLY.aliases,
            field_names=_BITLY.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("bitly_base_url")
        or _BITLY_BASE_URL
    )
    token = _credential_value(
        provider=_BITLY.provider,
        provider_aliases=_BITLY.aliases,
        field_names=_BITLY.group("access_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("bitly_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_BITLY.provider,
            field_names=_BITLY.hint_fields,
            tool_name=tool_name,
            env_var=_BITLY.env_var,
            display_name=_BITLY.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _brandfetch_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_BRANDFETCH.provider,
            provider_aliases=_BRANDFETCH.aliases,
            field_names=_BRANDFETCH.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("brandfetch_base_url")
        or _BRANDFETCH_BASE_URL
    )
    token = _credential_value(
        provider=_BRANDFETCH.provider,
        provider_aliases=_BRANDFETCH.aliases,
        field_names=_BRANDFETCH.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("brandfetch_api_key")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_BRANDFETCH.provider,
            field_names=_BRANDFETCH.hint_fields,
            tool_name=tool_name,
            env_var=_BRANDFETCH.env_var,
            display_name=_BRANDFETCH.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _marketstack_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str]:
    base = (
        _credential_value(
            provider=_MARKETSTACK.provider,
            provider_aliases=_MARKETSTACK.aliases,
            field_names=_MARKETSTACK.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("marketstack_base_url")
        or _MARKETSTACK_BASE_URL
    )
    key = _credential_value(
        provider=_MARKETSTACK.provider,
        provider_aliases=_MARKETSTACK.aliases,
        field_names=_MARKETSTACK.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("marketstack_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider=_MARKETSTACK.provider,
            field_names=_MARKETSTACK.hint_fields,
            tool_name=tool_name,
            env_var=_MARKETSTACK.env_var,
            display_name=_MARKETSTACK.display_name,
        )
    return _base_url(base), key


def _deepl_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    plan = (
        _credential_value(
            provider=_DEEPL.provider,
            provider_aliases=_DEEPL.aliases,
            field_names=_DEEPL.group("api_plan"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("deepl_api_plan")
        or "pro"
    ).lower()
    default_base = _DEEPL_FREE_BASE_URL if plan == "free" else _DEEPL_PRO_BASE_URL
    base = (
        _credential_value(
            provider=_DEEPL.provider,
            provider_aliases=_DEEPL.aliases,
            field_names=_DEEPL.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("deepl_base_url")
        or default_base
    )
    key = _credential_value(
        provider=_DEEPL.provider,
        provider_aliases=_DEEPL.aliases,
        field_names=_DEEPL.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("deepl_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider=_DEEPL.provider,
            field_names=_DEEPL.hint_fields,
            tool_name=tool_name,
            env_var=_DEEPL.env_var,
            display_name=_DEEPL.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"DeepL-Auth-Key {key}",
        "User-Agent": "Nymeria",
    }


def _bearer_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    settings_key_name: str,
    settings_base_name: str,
    default_base: str,
    env_var: str,
    display_name: str,
    tool_name: str,
    config: Optional[RunnableConfig],
    field_names: tuple[str, ...] = ("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=("base_url", "url", "api_url", "apiUrl"),
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
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _apitemplate_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_APITEMPLATE.provider,
            provider_aliases=_APITEMPLATE.aliases,
            field_names=_APITEMPLATE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("apitemplate_base_url")
        or _APITEMPLATE_BASE_URL
    )
    key = _credential_value(
        provider=_APITEMPLATE.provider,
        provider_aliases=_APITEMPLATE.aliases,
        field_names=_APITEMPLATE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("apitemplate_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider=_APITEMPLATE.provider,
            field_names=_APITEMPLATE.hint_fields,
            tool_name=tool_name,
            env_var=_APITEMPLATE.env_var,
            display_name=_APITEMPLATE.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-KEY": key,
        "User-Agent": "Nymeria",
    }


def _lingvanex_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider=_LINGVANEX.provider,
        provider_aliases=_LINGVANEX.aliases,
        settings_key_name="lingvanex_api_key",
        settings_base_name="lingvanex_base_url",
        default_base=_LINGVANEX_BASE_URL,
        env_var=_LINGVANEX.env_var,
        display_name=_LINGVANEX.display_name,
        tool_name=tool_name,
        config=config,
    )


def _onesimple_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str]:
    base = (
        _credential_value(
            provider=_ONESIMPLE.provider,
            provider_aliases=_ONESIMPLE.aliases,
            field_names=_ONESIMPLE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("onesimple_base_url")
        or _ONESIMPLE_BASE_URL
    )
    token = _credential_value(
        provider=_ONESIMPLE.provider,
        provider_aliases=_ONESIMPLE.aliases,
        field_names=_ONESIMPLE.group("api_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("onesimple_api_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_ONESIMPLE.provider,
            field_names=_ONESIMPLE.hint_fields,
            tool_name=tool_name,
            env_var=_ONESIMPLE.env_var,
            display_name=_ONESIMPLE.display_name,
        )
    return _base_url(base), token


def _onesimple_get(
    tool_name: str,
    endpoint: str,
    params: dict[str, Any],
    config: Optional[RunnableConfig],
) -> str:
    base_url, token_or_error = _onesimple_config(tool_name, config)
    if token_or_error.startswith("[Error]:"):
        return token_or_error
    data = _request_json(
        "GET",
        f"{base_url}{endpoint}",
        params={**params, "token": token_or_error, "output": "json"},
        headers={"Accept": "application/json", "User-Agent": "Nymeria"},
    )
    return _dump_json(data)


def _dhl_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_DHL.provider,
            provider_aliases=_DHL.aliases,
            field_names=_DHL.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("dhl_base_url")
        or _DHL_BASE_URL
    )
    api_key = _credential_value(
        provider=_DHL.provider,
        provider_aliases=_DHL.aliases,
        field_names=_DHL.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("dhl_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_DHL.provider,
            field_names=_DHL.hint_fields,
            tool_name=tool_name,
            env_var=_DHL.env_var,
            display_name=_DHL.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "DHL-API-Key": api_key,
        "User-Agent": "Nymeria",
    }


def _onfleet_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_ONFLEET.provider,
            provider_aliases=_ONFLEET.aliases,
            field_names=_ONFLEET.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("onfleet_base_url")
        or _ONFLEET_BASE_URL
    )
    api_key = _credential_value(
        provider=_ONFLEET.provider,
        provider_aliases=_ONFLEET.aliases,
        field_names=_ONFLEET.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("onfleet_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_ONFLEET.provider,
            field_names=_ONFLEET.hint_fields,
            tool_name=tool_name,
            env_var=_ONFLEET.env_var,
            display_name=_ONFLEET.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {_basic_auth(api_key)}",
        "User-Agent": "Nymeria",
    }


def _phantombuster_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_PHANTOMBUSTER.provider,
            provider_aliases=_PHANTOMBUSTER.aliases,
            field_names=_PHANTOMBUSTER.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("phantombuster_base_url")
        or _PHANTOMBUSTER_BASE_URL
    )
    api_key = _credential_value(
        provider=_PHANTOMBUSTER.provider,
        provider_aliases=_PHANTOMBUSTER.aliases,
        field_names=_PHANTOMBUSTER.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("phantombuster_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_PHANTOMBUSTER.provider,
            field_names=_PHANTOMBUSTER.hint_fields,
            tool_name=tool_name,
            env_var=_PHANTOMBUSTER.env_var,
            display_name=_PHANTOMBUSTER.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Phantombuster-Key": api_key,
        "User-Agent": "Nymeria",
    }


def _onfleet_request(
    tool_name: str,
    path: str,
    config: Optional[RunnableConfig],
    *,
    method: str = "GET",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> str:
    base_url, headers_or_error = _onfleet_config(tool_name, config)
    if isinstance(headers_or_error, str):
        return headers_or_error
    data = _request_json(
        method,
        f"{base_url}/{path.strip('/')}",
        params=params,
        json_body=json_body,
        headers=headers_or_error,
    )
    return _dump_json(data)


def _brand_section(data: Any, section: str) -> Any:
    if not isinstance(data, dict):
        return data
    if section == "logos":
        return data.get("logos", [])
    if section == "colors":
        return data.get("colors", [])
    return data


@tool
def bitly_get_bitlink(
    bitlink_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Bitly bitlink metadata.

    Args:
        bitlink_id: Bitly link ID, e.g. "bit.ly/example".
    """
    bitlink_id = bitlink_id.strip()
    if not bitlink_id:
        return "[Error]: bitlink_id is required."
    try:
        base_url, headers_or_error = _bitly_config("bitly_get_bitlink", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/bitlinks/{quote(bitlink_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("bitly_get_bitlink failed", exc_info=True)
        return f"[Error]: Bitly bitlink lookup failed: {e}"


@tool
def bitly_create_bitlink(
    long_url: str,
    title: str = "",
    domain: str = "bit.ly",
    group_guid: str = "",
    tags: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Bitly short link.

    Args:
        long_url: Destination URL to shorten.
        title: Optional title.
        domain: Optional short domain, default "bit.ly".
        group_guid: Optional Bitly group GUID.
        tags: Optional comma-separated tags.
    """
    if not long_url.strip():
        return "[Error]: long_url is required."
    try:
        base_url, headers_or_error = _bitly_config("bitly_create_bitlink", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params(
            {
                "long_url": long_url.strip(),
                "title": title.strip(),
                "domain": domain.strip() or "bit.ly",
                "group_guid": group_guid.strip(),
                "tags": _split_csv(tags),
            }
        )
        data = _request_json("POST", f"{base_url}/bitlinks", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("bitly_create_bitlink failed", exc_info=True)
        return f"[Error]: Bitly bitlink creation failed: {e}"


@tool
def bitly_update_bitlink(
    bitlink_id: str,
    long_url: str = "",
    title: str = "",
    archived: Optional[bool] = None,
    group_guid: str = "",
    tags: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update Bitly bitlink metadata.

    Args:
        bitlink_id: Bitly link ID, e.g. "bit.ly/example".
        long_url: Optional new destination URL.
        title: Optional title.
        archived: Optional archive flag.
        group_guid: Optional Bitly group GUID.
        tags: Optional comma-separated tags.
    """
    bitlink_id = bitlink_id.strip()
    if not bitlink_id:
        return "[Error]: bitlink_id is required."
    try:
        base_url, headers_or_error = _bitly_config("bitly_update_bitlink", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params(
            {
                "long_url": long_url.strip(),
                "title": title.strip(),
                "archived": archived,
                "group_guid": group_guid.strip(),
                "tags": _split_csv(tags),
            }
        )
        if not body:
            return "[Error]: provide at least one field to update."
        data = _request_json(
            "PATCH",
            f"{base_url}/bitlinks/{quote(bitlink_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("bitly_update_bitlink failed", exc_info=True)
        return f"[Error]: Bitly bitlink update failed: {e}"


@tool
def brandfetch_get_brand(
    domain: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Brandfetch company, industry, color, font, and logo metadata.

    Args:
        domain: Company domain, e.g. "openai.com".
    """
    domain = domain.strip()
    if not domain:
        return "[Error]: domain is required."
    try:
        base_url, headers_or_error = _brandfetch_config("brandfetch_get_brand", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(
            _request_json(
                "GET",
                f"{base_url}/brands/{quote(domain, safe='')}",
                headers=headers_or_error,
            )
        )
    except Exception as e:
        logger.error("brandfetch_get_brand failed", exc_info=True)
        return f"[Error]: Brandfetch brand lookup failed: {e}"


@tool
def brandfetch_get_brand_logos(
    domain: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Brandfetch logo and icon metadata for a company domain.

    Args:
        domain: Company domain, e.g. "openai.com".
    """
    domain = domain.strip()
    if not domain:
        return "[Error]: domain is required."
    try:
        base_url, headers_or_error = _brandfetch_config("brandfetch_get_brand_logos", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/brands/{quote(domain, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(_brand_section(data, "logos"))
    except Exception as e:
        logger.error("brandfetch_get_brand_logos failed", exc_info=True)
        return f"[Error]: Brandfetch logo lookup failed: {e}"


@tool
def brandfetch_get_brand_colors(
    domain: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Brandfetch color metadata for a company domain.

    Args:
        domain: Company domain, e.g. "openai.com".
    """
    domain = domain.strip()
    if not domain:
        return "[Error]: domain is required."
    try:
        base_url, headers_or_error = _brandfetch_config("brandfetch_get_brand_colors", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/brands/{quote(domain, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(_brand_section(data, "colors"))
    except Exception as e:
        logger.error("brandfetch_get_brand_colors failed", exc_info=True)
        return f"[Error]: Brandfetch color lookup failed: {e}"


@tool
def marketstack_get_eod(
    symbols: str,
    latest: bool = True,
    date: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Marketstack end-of-day stock market data.

    Args:
        symbols: Comma-separated ticker symbols, e.g. "AAPL,MSFT".
        latest: Use the latest EOD endpoint.
        date: Optional specific date in YYYY-MM-DD format.
        date_from: Optional range start date in YYYY-MM-DD format.
        date_to: Optional range end date in YYYY-MM-DD format.
        limit: Number of rows to return, 1-100.
    """
    if not _csv(symbols):
        return "[Error]: symbols is required."
    if date and (date_from or date_to):
        return "[Error]: use either date or date_from/date_to, not both."
    if bool(date_from) != bool(date_to):
        return "[Error]: date_from and date_to must be provided together."
    try:
        base_url, key_or_error = _marketstack_config("marketstack_get_eod", config)
        if key_or_error.startswith("[Error]:"):
            return key_or_error
        endpoint = "/eod/latest" if latest and not date and not date_from else "/eod"
        if date:
            endpoint = f"/eod/{quote(date.strip(), safe='')}"
        data = _request_json(
            "GET",
            f"{base_url}{endpoint}",
            params={
                "access_key": key_or_error,
                "symbols": _csv(symbols),
                "date_from": date_from.strip(),
                "date_to": date_to.strip(),
                "limit": _limit(limit),
            },
            headers={"Accept": "application/json", "User-Agent": "Nymeria"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("marketstack_get_eod failed", exc_info=True)
        return f"[Error]: Marketstack EOD lookup failed: {e}"


@tool
def marketstack_get_ticker(
    symbol: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Marketstack ticker metadata.

    Args:
        symbol: Ticker symbol, e.g. "AAPL".
    """
    symbol = symbol.strip()
    if not symbol:
        return "[Error]: symbol is required."
    try:
        base_url, key_or_error = _marketstack_config("marketstack_get_ticker", config)
        if key_or_error.startswith("[Error]:"):
            return key_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tickers/{quote(symbol, safe='')}",
            params={"access_key": key_or_error},
            headers={"Accept": "application/json", "User-Agent": "Nymeria"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("marketstack_get_ticker failed", exc_info=True)
        return f"[Error]: Marketstack ticker lookup failed: {e}"


@tool
def marketstack_get_exchange(
    exchange: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Marketstack exchange metadata.

    Args:
        exchange: Exchange MIC/acronym, e.g. "XNAS".
    """
    exchange = exchange.strip()
    if not exchange:
        return "[Error]: exchange is required."
    try:
        base_url, key_or_error = _marketstack_config("marketstack_get_exchange", config)
        if key_or_error.startswith("[Error]:"):
            return key_or_error
        data = _request_json(
            "GET",
            f"{base_url}/exchanges/{quote(exchange, safe='')}",
            params={"access_key": key_or_error},
            headers={"Accept": "application/json", "User-Agent": "Nymeria"},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("marketstack_get_exchange failed", exc_info=True)
        return f"[Error]: Marketstack exchange lookup failed: {e}"


@tool
def deepl_translate_text(
    text: str,
    target_lang: str,
    source_lang: str = "",
    formality: str = "",
    preserve_formatting: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Translate text with DeepL.

    Args:
        text: Text to translate.
        target_lang: Target language code, e.g. "DE", "FR", or "EN-US".
        source_lang: Optional source language code.
        formality: Optional formality setting, e.g. "prefer_more" or "prefer_less".
        preserve_formatting: Preserve source text formatting when possible.
    """
    if not text.strip() or not target_lang.strip():
        return "[Error]: text and target_lang are required."
    try:
        base_url, headers_or_error = _deepl_config("deepl_translate_text", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/translate",
            data=_filtered_params(
                {
                    "text": text,
                    "target_lang": target_lang.strip(),
                    "source_lang": source_lang.strip(),
                    "formality": formality.strip(),
                    "preserve_formatting": "1" if preserve_formatting else "",
                }
            ),
            headers=headers_or_error,
        )
        return _dump_json(data.get("translations", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("deepl_translate_text failed", exc_info=True)
        return f"[Error]: DeepL translation failed: {e}"


@tool
def deepl_list_languages(
    language_type: str = "target",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List DeepL source or target languages.

    Args:
        language_type: "source" or "target".
    """
    try:
        base_url, headers_or_error = _deepl_config("deepl_list_languages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/languages",
            params={"type": language_type.strip() or "target"},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("deepl_list_languages failed", exc_info=True)
        return f"[Error]: DeepL language list failed: {e}"


@tool
def lingvanex_translate_text(
    text: str,
    target_lang: str,
    source_lang: str = "",
    platform: str = "api",
    translate_mode: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Translate text with LingvaNex.

    Args:
        text: Text to translate.
        target_lang: Target language code, for example "es" or "de_DE".
        source_lang: Optional source language code. Empty enables auto-detect.
        platform: Optional LingvaNex platform value.
        translate_mode: Optional translate mode.
    """
    if not text.strip() or not target_lang.strip():
        return "[Error]: text and target_lang are required."
    try:
        base_url, headers_or_error = _lingvanex_config("lingvanex_translate_text", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _filtered_params(
            {
                "data": text,
                "to": target_lang.strip(),
                "from": source_lang.strip(),
                "platform": platform.strip() or "api",
                "translateMode": translate_mode.strip(),
            }
        )
        data = _request_json(
            "POST",
            f"{base_url}/translate",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("lingvanex_translate_text failed", exc_info=True)
        return f"[Error]: LingvaNex translation failed: {e}"


@tool
def lingvanex_list_languages(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List LingvaNex supported languages.

    Args:
        config: Runtime context injected by Nymeria.
    """
    try:
        base_url, headers_or_error = _lingvanex_config("lingvanex_list_languages", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/getLanguages", headers=headers_or_error)
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            return _dump_json(data["result"])
        return _dump_json(data)
    except Exception as e:
        logger.error("lingvanex_list_languages failed", exc_info=True)
        return f"[Error]: LingvaNex language list failed: {e}"


@tool
def apitemplate_list_templates(
    template_type: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List APITemplate templates.

    Args:
        template_type: Optional filter, "pdf" or "image".
    """
    try:
        base_url, headers_or_error = _apitemplate_config("apitemplate_list_templates", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/list-templates", headers=headers_or_error)
        normalized_type = template_type.strip().lower()
        if normalized_type and isinstance(data, list):
            formats = {"pdf": {"PDF"}, "image": {"JPEG", "JPG", "PNG"}}.get(normalized_type)
            if formats:
                data = [
                    item
                    for item in data
                    if isinstance(item, dict) and str(item.get("format", "")).upper() in formats
                ]
        return _dump_json(data)
    except Exception as e:
        logger.error("apitemplate_list_templates failed", exc_info=True)
        return f"[Error]: APITemplate template list failed: {e}"


@tool
def apitemplate_get_account(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get APITemplate account information.

    Args:
        config: Runtime context injected by Nymeria.
    """
    try:
        base_url, headers_or_error = _apitemplate_config("apitemplate_get_account", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/account-information", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("apitemplate_get_account failed", exc_info=True)
        return f"[Error]: APITemplate account lookup failed: {e}"


@tool
def apitemplate_create_image(
    template_id: str,
    overrides_json: str = "[]",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an image from an APITemplate image template.

    Args:
        template_id: APITemplate image template ID.
        overrides_json: Optional JSON array of override objects.
    """
    if not template_id.strip():
        return "[Error]: template_id is required."
    try:
        base_url, headers_or_error = _apitemplate_config("apitemplate_create_image", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        overrides = _json_array(overrides_json, field_name="overrides_json")
        data = _request_json(
            "POST",
            f"{base_url}/create",
            params={"template_id": template_id.strip()},
            json_body={"overrides": overrides},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("apitemplate_create_image failed", exc_info=True)
        return f"[Error]: APITemplate image creation failed: {e}"


@tool
def apitemplate_create_pdf(
    template_id: str,
    properties_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a PDF from an APITemplate PDF template.

    Args:
        template_id: APITemplate PDF template ID.
        properties_json: JSON object of template properties.
    """
    if not template_id.strip():
        return "[Error]: template_id is required."
    try:
        base_url, headers_or_error = _apitemplate_config("apitemplate_create_pdf", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        properties = _json_object(properties_json, field_name="properties_json", allow_empty=False)
        data = _request_json(
            "POST",
            f"{base_url}/create",
            params={"template_id": template_id.strip()},
            json_body=properties,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("apitemplate_create_pdf failed", exc_info=True)
        return f"[Error]: APITemplate PDF creation failed: {e}"


@tool
def onesimple_create_pdf(
    url: str,
    page_size: str = "",
    force_refresh: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a PDF URL for a webpage with One Simple API.

    Args:
        url: Webpage URL to render.
        page_size: Optional page size such as "A4".
        force_refresh: Force a fresh capture instead of cached output.
    """
    try:
        return _onesimple_get(
            "onesimple_create_pdf",
            "/pdf",
            {
                "url": _absolute_url(url),
                "page": page_size.strip(),
                "force": "yes" if force_refresh else "no",
            },
            config,
        )
    except Exception as e:
        logger.error("onesimple_create_pdf failed", exc_info=True)
        return f"[Error]: One Simple API PDF creation failed: {e}"


@tool
def onesimple_create_screenshot(
    url: str,
    screen_size: str = "",
    full_page: bool = False,
    force_refresh: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a screenshot URL for a webpage with One Simple API.

    Args:
        url: Webpage URL to capture.
        screen_size: Optional screen size preset such as "desktop" or "mobile".
        full_page: Capture the full page height.
        force_refresh: Force a fresh capture instead of cached output.
    """
    try:
        return _onesimple_get(
            "onesimple_create_screenshot",
            "/screenshot",
            {
                "url": _absolute_url(url),
                "screen": screen_size.strip(),
                "fullpage": "yes" if full_page else "no",
                "force": "yes" if force_refresh else "no",
            },
            config,
        )
    except Exception as e:
        logger.error("onesimple_create_screenshot failed", exc_info=True)
        return f"[Error]: One Simple API screenshot creation failed: {e}"


@tool
def onesimple_get_page_info(
    url: str,
    include_headers: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get page SEO and metadata for a webpage with One Simple API.

    Args:
        url: Webpage URL to inspect.
        include_headers: Include response headers when supported.
    """
    try:
        return _onesimple_get(
            "onesimple_get_page_info",
            "/page_info",
            {"url": _absolute_url(url), "headers": "yes" if include_headers else ""},
            config,
        )
    except Exception as e:
        logger.error("onesimple_get_page_info failed", exc_info=True)
        return f"[Error]: One Simple API page info lookup failed: {e}"


@tool
def onesimple_get_exchange_rate(
    value: float,
    from_currency: str,
    to_currency: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Convert a currency amount with One Simple API exchange-rate data.

    Args:
        value: Amount to convert.
        from_currency: Source currency code.
        to_currency: Target currency code.
    """
    if not from_currency.strip() or not to_currency.strip():
        return "[Error]: from_currency and to_currency are required."
    try:
        return _onesimple_get(
            "onesimple_get_exchange_rate",
            "/exchange_rate",
            {
                "from_value": value,
                "from_currency": from_currency.strip().upper(),
                "to_currency": to_currency.strip().upper(),
            },
            config,
        )
    except Exception as e:
        logger.error("onesimple_get_exchange_rate failed", exc_info=True)
        return f"[Error]: One Simple API exchange-rate lookup failed: {e}"


@tool
def onesimple_get_image_metadata(
    image_url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get image metadata from an image URL with One Simple API.

    Args:
        image_url: Public image URL to inspect.
    """
    try:
        return _onesimple_get(
            "onesimple_get_image_metadata",
            "/image_info",
            {"url": _absolute_url(image_url, field_name="image_url"), "raw": "true"},
            config,
        )
    except Exception as e:
        logger.error("onesimple_get_image_metadata failed", exc_info=True)
        return f"[Error]: One Simple API image metadata lookup failed: {e}"


@tool
def onesimple_validate_email(
    email: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Validate an email address with One Simple API.

    Args:
        email: Email address to validate.
    """
    if "@" not in email:
        return "[Error]: email must look like an email address."
    try:
        return _onesimple_get(
            "onesimple_validate_email",
            "/email",
            {"email": email.strip()},
            config,
        )
    except Exception as e:
        logger.error("onesimple_validate_email failed", exc_info=True)
        return f"[Error]: One Simple API email validation failed: {e}"


@tool
def onesimple_expand_url(
    url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Expand a shortened URL with One Simple API.

    Args:
        url: Short URL to expand.
    """
    try:
        return _onesimple_get(
            "onesimple_expand_url",
            "/unshorten",
            {"url": _absolute_url(url)},
            config,
        )
    except Exception as e:
        logger.error("onesimple_expand_url failed", exc_info=True)
        return f"[Error]: One Simple API URL expansion failed: {e}"


@tool
def onesimple_create_qr_code(
    content: str,
    size: str = "",
    image_format: str = "png",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a QR-code image URL with One Simple API.

    Args:
        content: Content to encode in the QR code.
        size: Optional QR size preset or pixel size.
        image_format: Image format, usually "png" or "svg".
    """
    if not content.strip():
        return "[Error]: content is required."
    try:
        return _onesimple_get(
            "onesimple_create_qr_code",
            "/qr_code",
            {
                "message": content,
                "size": size.strip(),
                "format": image_format.strip() or "png",
            },
            config,
        )
    except Exception as e:
        logger.error("onesimple_create_qr_code failed", exc_info=True)
        return f"[Error]: One Simple API QR-code creation failed: {e}"


@tool
def dhl_track_shipment(
    tracking_number: str,
    recipient_postal_code: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get DHL shipment tracking details.

    Args:
        tracking_number: DHL shipment tracking number.
        recipient_postal_code: Optional recipient postal code for detailed tracking verification.
    """
    if not tracking_number.strip():
        return "[Error]: tracking_number is required."
    try:
        base_url, headers_or_error = _dhl_config("dhl_track_shipment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/track/shipments",
            params={
                "trackingNumber": tracking_number.strip(),
                "recipientPostalCode": recipient_postal_code.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data.get("shipments", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("dhl_track_shipment failed", exc_info=True)
        return f"[Error]: DHL shipment tracking lookup failed: {e}"


@tool
def onfleet_test_auth(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Validate the saved Onfleet API connection.

    Args:
        config: Runtime context injected by Nymeria.
    """
    try:
        return _onfleet_request("onfleet_test_auth", "auth/test", config)
    except Exception as e:
        logger.error("onfleet_test_auth failed", exc_info=True)
        return f"[Error]: Onfleet auth test failed: {e}"


@tool
def onfleet_list_tasks(
    from_timestamp_ms: int = 0,
    to_timestamp_ms: int = 0,
    state_csv: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Onfleet tasks.

    Args:
        from_timestamp_ms: Optional start timestamp in Unix milliseconds.
        to_timestamp_ms: Optional end timestamp in Unix milliseconds.
        state_csv: Optional comma-separated Onfleet task state values, or "0,1,2,3" for all active states.
        limit: Maximum tasks to return.
    """
    try:
        data = _onfleet_request(
            "onfleet_list_tasks",
            "tasks/all",
            config,
            params={
                "from": from_timestamp_ms or None,
                "to": to_timestamp_ms or None,
                "state": _csv(state_csv),
            },
        )
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
            return _dump_json(parsed["tasks"][: _limit(limit, default=50, max_value=200)])
        if isinstance(parsed, list):
            return _dump_json(parsed[: _limit(limit, default=50, max_value=200)])
        return data
    except Exception as e:
        logger.error("onfleet_list_tasks failed", exc_info=True)
        return f"[Error]: Onfleet task listing failed: {e}"


@tool
def onfleet_get_task(
    task_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Onfleet task by ID or short ID.

    Args:
        task_id: Onfleet task ID. Values up to 8 characters are treated as short IDs.
    """
    task_id = task_id.strip()
    if not task_id:
        return "[Error]: task_id is required."
    path = f"tasks/{'shortId/' if len(task_id) <= 8 else ''}{quote(task_id, safe='')}"
    try:
        return _onfleet_request("onfleet_get_task", path, config)
    except Exception as e:
        logger.error("onfleet_get_task failed", exc_info=True)
        return f"[Error]: Onfleet task lookup failed: {e}"


@tool
def onfleet_list_workers(
    states_csv: str = "",
    teams_csv: str = "",
    phones_csv: str = "",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Onfleet workers.

    Args:
        states_csv: Optional comma-separated worker state values.
        teams_csv: Optional comma-separated team IDs.
        phones_csv: Optional comma-separated phone numbers.
        limit: Maximum workers to return.
    """
    try:
        data = _onfleet_request(
            "onfleet_list_workers",
            "workers",
            config,
            params={
                "states": _csv(states_csv),
                "teams": _csv(teams_csv),
                "phones": _csv(phones_csv),
            },
        )
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        if isinstance(parsed, list):
            return _dump_json(parsed[: _limit(limit, default=100, max_value=500)])
        if isinstance(parsed, dict) and isinstance(parsed.get("workers"), list):
            return _dump_json(parsed["workers"][: _limit(limit, default=100, max_value=500)])
        return data
    except Exception as e:
        logger.error("onfleet_list_workers failed", exc_info=True)
        return f"[Error]: Onfleet worker listing failed: {e}"


@tool
def onfleet_get_worker(
    worker_id: str,
    include_analytics: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Onfleet worker by ID.

    Args:
        worker_id: Onfleet worker ID.
        include_analytics: Include analytics details when supported by Onfleet.
    """
    if not worker_id.strip():
        return "[Error]: worker_id is required."
    try:
        return _onfleet_request(
            "onfleet_get_worker",
            f"workers/{quote(worker_id.strip(), safe='')}",
            config,
            params={"analytics": "true" if include_analytics else None},
        )
    except Exception as e:
        logger.error("onfleet_get_worker failed", exc_info=True)
        return f"[Error]: Onfleet worker lookup failed: {e}"


@tool
def onfleet_list_teams(
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Onfleet teams.

    Args:
        limit: Maximum teams to return.
    """
    try:
        data = _onfleet_request("onfleet_list_teams", "teams", config)
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        if isinstance(parsed, list):
            return _dump_json(parsed[: _limit(limit, default=100, max_value=500)])
        return data
    except Exception as e:
        logger.error("onfleet_list_teams failed", exc_info=True)
        return f"[Error]: Onfleet team listing failed: {e}"


@tool
def onfleet_get_team(
    team_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Onfleet team by ID.

    Args:
        team_id: Onfleet team ID.
    """
    if not team_id.strip():
        return "[Error]: team_id is required."
    try:
        return _onfleet_request("onfleet_get_team", f"teams/{quote(team_id.strip(), safe='')}", config)
    except Exception as e:
        logger.error("onfleet_get_team failed", exc_info=True)
        return f"[Error]: Onfleet team lookup failed: {e}"


@tool
def onfleet_complete_task(
    task_id: str,
    success: bool = True,
    notes: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Force-complete an Onfleet task.

    Args:
        task_id: Onfleet task ID.
        success: Whether the completion should be marked successful.
        notes: Optional completion notes.
    """
    if not task_id.strip():
        return "[Error]: task_id is required."
    body: dict[str, Any] = {"completionDetails": {"success": success}}
    if notes.strip():
        body["completionDetails"]["notes"] = notes.strip()
    try:
        return _onfleet_request(
            "onfleet_complete_task",
            f"tasks/{quote(task_id.strip(), safe='')}/complete",
            config,
            method="POST",
            json_body=body,
        )
    except Exception as e:
        logger.error("onfleet_complete_task failed", exc_info=True)
        return f"[Error]: Onfleet task completion failed: {e}"


@tool
def phantombuster_list_agents(
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Phantombuster agents.

    Args:
        limit: Maximum agents to return.
    """
    try:
        base_url, headers_or_error = _phantombuster_config("phantombuster_list_agents", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/agents/fetch-all", headers=headers_or_error)
        if isinstance(data, list):
            return _dump_json(data[: _limit(limit, default=100, max_value=500)])
        return _dump_json(data)
    except Exception as e:
        logger.error("phantombuster_list_agents failed", exc_info=True)
        return f"[Error]: Phantombuster agent listing failed: {e}"


@tool
def phantombuster_get_agent(
    agent_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Phantombuster agent metadata.

    Args:
        agent_id: Phantombuster agent ID.
    """
    if not agent_id.strip():
        return "[Error]: agent_id is required."
    try:
        base_url, headers_or_error = _phantombuster_config("phantombuster_get_agent", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/agents/fetch",
            params={"id": agent_id.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("phantombuster_get_agent failed", exc_info=True)
        return f"[Error]: Phantombuster agent lookup failed: {e}"


@tool
def phantombuster_get_agent_output(
    agent_id: str,
    resolve_data: bool = False,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Phantombuster agent output, optionally resolving the result object.

    Args:
        agent_id: Phantombuster agent ID.
        resolve_data: Fetch and parse the result object for the returned container ID.
        fields_json: Optional JSON object of additional query parameters.
    """
    if not agent_id.strip():
        return "[Error]: agent_id is required."
    try:
        base_url, headers_or_error = _phantombuster_config("phantombuster_get_agent_output", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"id": agent_id.strip(), **_json_object(fields_json, field_name="fields_json")}
        data = _request_json(
            "GET",
            f"{base_url}/agents/fetch-output",
            params=params,
            headers=headers_or_error,
        )
        if not resolve_data:
            return _dump_json(data)
        container_id = data.get("containerId") if isinstance(data, dict) else None
        if not container_id:
            return _dump_json(data)
        result = _request_json(
            "GET",
            f"{base_url}/containers/fetch-result-object",
            params={"id": container_id},
            headers=headers_or_error,
        )
        result_object = result.get("resultObject") if isinstance(result, dict) else None
        if result_object is None:
            return _dump_json({})
        if isinstance(result_object, str):
            try:
                return _dump_json(json.loads(result_object))
            except json.JSONDecodeError:
                return _dump_json({"resultObject": result_object})
        return _dump_json(result_object)
    except Exception as e:
        logger.error("phantombuster_get_agent_output failed", exc_info=True)
        return f"[Error]: Phantombuster agent output lookup failed: {e}"


@tool
def phantombuster_launch_agent(
    agent_id: str,
    arguments_json: str = "",
    bonus_argument_json: str = "",
    fields_json: str = "",
    resolve_container: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Launch a Phantombuster agent.

    Args:
        agent_id: Phantombuster agent ID.
        arguments_json: Optional JSON object for the launch arguments.
        bonus_argument_json: Optional JSON object for bonus arguments.
        fields_json: Optional JSON object of additional launch fields.
        resolve_container: Fetch the launched container after starting the agent.
    """
    if not agent_id.strip():
        return "[Error]: agent_id is required."
    try:
        base_url, headers_or_error = _phantombuster_config("phantombuster_launch_agent", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"id": agent_id.strip(), **_json_object(fields_json, field_name="fields_json")}
        arguments = _json_object(arguments_json, field_name="arguments_json")
        if arguments:
            body["arguments"] = arguments
        bonus_argument = _json_object(bonus_argument_json, field_name="bonus_argument_json")
        if bonus_argument:
            body["bonusArgument"] = bonus_argument
        data = _request_json("POST", f"{base_url}/agents/launch", json_body=body, headers=headers_or_error)
        if resolve_container and isinstance(data, dict) and data.get("containerId"):
            data = _request_json(
                "GET",
                f"{base_url}/containers/fetch",
                params={"id": data["containerId"]},
                headers=headers_or_error,
            )
        return _dump_json(data)
    except Exception as e:
        logger.error("phantombuster_launch_agent failed", exc_info=True)
        return f"[Error]: Phantombuster agent launch failed: {e}"


@tool
def phantombuster_delete_agent(
    agent_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Phantombuster agent.

    Args:
        agent_id: Phantombuster agent ID.
    """
    if not agent_id.strip():
        return "[Error]: agent_id is required."
    try:
        base_url, headers_or_error = _phantombuster_config("phantombuster_delete_agent", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _request_json("POST", f"{base_url}/agents/delete", json_body={"id": agent_id.strip()}, headers=headers_or_error)
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("phantombuster_delete_agent failed", exc_info=True)
        return f"[Error]: Phantombuster agent deletion failed: {e}"


BUSINESS_SERVICE_TOOLS = [
    bitly_get_bitlink,
    bitly_create_bitlink,
    bitly_update_bitlink,
    brandfetch_get_brand,
    brandfetch_get_brand_logos,
    brandfetch_get_brand_colors,
    marketstack_get_eod,
    marketstack_get_ticker,
    marketstack_get_exchange,
    deepl_translate_text,
    deepl_list_languages,
    lingvanex_translate_text,
    lingvanex_list_languages,
    apitemplate_list_templates,
    apitemplate_get_account,
    apitemplate_create_image,
    apitemplate_create_pdf,
    onesimple_create_pdf,
    onesimple_create_screenshot,
    onesimple_get_page_info,
    onesimple_get_exchange_rate,
    onesimple_get_image_metadata,
    onesimple_validate_email,
    onesimple_expand_url,
    onesimple_create_qr_code,
    dhl_track_shipment,
    onfleet_test_auth,
    onfleet_list_tasks,
    onfleet_get_task,
    onfleet_list_workers,
    onfleet_get_worker,
    onfleet_list_teams,
    onfleet_get_team,
    onfleet_complete_task,
    phantombuster_list_agents,
    phantombuster_get_agent,
    phantombuster_get_agent_output,
    phantombuster_launch_agent,
    phantombuster_delete_agent,
]
