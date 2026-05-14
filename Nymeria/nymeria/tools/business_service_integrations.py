"""Business data and language service integration tools."""

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
_BITLY_BASE_URL = "https://api-ssl.bitly.com/v4"
_BRANDFETCH_BASE_URL = "https://api.brandfetch.io/v2"
_MARKETSTACK_BASE_URL = "https://api.marketstack.com/v1"
_DEEPL_PRO_BASE_URL = "https://api.deepl.com/v2"
_DEEPL_FREE_BASE_URL = "https://api-free.deepl.com/v2"
_LINGVANEX_BASE_URL = "https://api-b2b.backenster.com/b1/api/v3"
_APITEMPLATE_BASE_URL = "https://api.apitemplate.io/v1"
_ONESIMPLE_BASE_URL = "https://onesimpleapi.com/api"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


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
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


def _absolute_url(value: str, *, field_name: str = "url") -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL")
    return value.strip()


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
            provider="bitly",
            provider_aliases=("bitly_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("bitly_base_url")
        or _BITLY_BASE_URL
    )
    token = _credential_value(
        provider="bitly",
        provider_aliases=("bitly_api",),
        field_names=("access_token", "token", "api_key", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("bitly_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="bitly",
            field_names=("access_token", "value"),
            tool_name=tool_name,
            env_var="BITLY_TOKEN",
            display_name="Bitly",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _brandfetch_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="brandfetch",
            provider_aliases=("brandfetch_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("brandfetch_base_url")
        or _BRANDFETCH_BASE_URL
    )
    token = _credential_value(
        provider="brandfetch",
        provider_aliases=("brandfetch_api",),
        field_names=("api_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("brandfetch_api_key")
    if not token:
        return _base_url(base), _setup_hint(
            provider="brandfetch",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="BRANDFETCH_API_KEY",
            display_name="Brandfetch",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _marketstack_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str]:
    base = (
        _credential_value(
            provider="marketstack",
            provider_aliases=("marketstack_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("marketstack_base_url")
        or _MARKETSTACK_BASE_URL
    )
    key = _credential_value(
        provider="marketstack",
        provider_aliases=("marketstack_api",),
        field_names=("api_key", "access_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("marketstack_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider="marketstack",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="MARKETSTACK_API_KEY",
            display_name="Marketstack",
        )
    return _base_url(base), key


def _deepl_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    plan = (
        _credential_value(
            provider="deepl",
            provider_aliases=("deepl_api",),
            field_names=("api_plan", "plan"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("deepl_api_plan")
        or "pro"
    ).lower()
    default_base = _DEEPL_FREE_BASE_URL if plan == "free" else _DEEPL_PRO_BASE_URL
    base = (
        _credential_value(
            provider="deepl",
            provider_aliases=("deepl_api",),
            field_names=("base_url", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("deepl_base_url")
        or default_base
    )
    key = _credential_value(
        provider="deepl",
        provider_aliases=("deepl_api",),
        field_names=("api_key", "auth_key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("deepl_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider="deepl",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="DEEPL_API_KEY",
            display_name="DeepL",
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
            provider="apitemplate",
            provider_aliases=("apitemplate_io", "api_template", "api_template_io"),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("apitemplate_base_url")
        or _APITEMPLATE_BASE_URL
    )
    key = _credential_value(
        provider="apitemplate",
        provider_aliases=("apitemplate_io", "api_template", "api_template_io"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("apitemplate_api_key")
    if not key:
        return _base_url(base), _setup_hint(
            provider="apitemplate",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="APITEMPLATE_API_KEY",
            display_name="APITemplate",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-KEY": key,
        "User-Agent": "Nymeria",
    }


def _lingvanex_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider="lingvanex",
        provider_aliases=("lingvanex_api",),
        settings_key_name="lingvanex_api_key",
        settings_base_name="lingvanex_base_url",
        default_base=_LINGVANEX_BASE_URL,
        env_var="LINGVANEX_API_KEY",
        display_name="LingvaNex",
        tool_name=tool_name,
        config=config,
    )


def _onesimple_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str]:
    base = (
        _credential_value(
            provider="onesimple",
            provider_aliases=("one_simple_api", "onesimpleapi"),
            field_names=("base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("onesimple_base_url")
        or _ONESIMPLE_BASE_URL
    )
    token = _credential_value(
        provider="onesimple",
        provider_aliases=("one_simple_api", "onesimpleapi"),
        field_names=("api_token", "apiToken", "token", "api_key", "apiKey", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("onesimple_api_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider="onesimple",
            field_names=("api_token", "value"),
            tool_name=tool_name,
            env_var="ONESIMPLE_API_TOKEN",
            display_name="One Simple API",
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
]
