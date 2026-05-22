"""Public information service integration tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlencode, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import (
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
    httpx_request_with_policy,
    validate_http_egress_url,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
_HACKERNEWS_BASE_URL = "https://hn.algolia.com/api/v1"
_NPM_REGISTRY_DEFAULT_URL = "https://registry.npmjs.org"
_NASA_BASE_URL = "https://api.nasa.gov"
_OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5"
_OPENTHESAURUS_BASE_URL = "https://www.openthesaurus.de"
_QUICKCHART_URL = "https://quickchart.io/chart"
_MAX_JSON_CHARS = 60_000


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _csv(value: str) -> str:
    return ",".join(_split_csv(value))


def _filtered_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if value is not None and value != "" and value != []
    }


def _get_json(url: str, params: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None) -> Any:
    import httpx

    try:
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers=headers,
            limits=httpx.Limits(max_keepalive_connections=0),
            trust_env=False,
        ) as client:
            response, _redirect_chain, _policy = httpx_request_with_policy(
                "GET",
                url,
                client=client,
                params=_filtered_params(params or {}),
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
    except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
        raise RuntimeError(f"HTTP request blocked by egress policy: {e}") from e
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = body.get("error") or body.get("message") or body.get("status", {}).get("error_message", "")
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _get_text(url: str, *, verify: bool = True) -> str:
    import httpx

    try:
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=False,
            verify=verify,
            limits=httpx.Limits(max_keepalive_connections=0),
            trust_env=False,
        ) as client:
            response, _redirect_chain, _policy = httpx_request_with_policy(
                "GET",
                url,
                client=client,
                headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.text
    except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
        raise RuntimeError(f"HTTP request blocked by egress policy: {e}") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"HTTP {e.response.status_code}: {e.response.text[:300]}") from e


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
    env_var: str = "",
    display_name: str = "",
) -> str:
    from .native_credentials import native_credential_setup_hint

    return native_credential_setup_hint(
        provider=provider,
        field_names=field_names,
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    )


def _npm_registry_and_headers(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str]]:
    registry = (
        _credential_value(
            provider="npm",
            field_names=("registry_url", "base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("npm_registry_url")
        or _NPM_REGISTRY_DEFAULT_URL
    ).rstrip("/")
    registry = validate_http_egress_url(
        registry,
        label="npm registry URL",
        resolve_dns=False,
    ).rstrip("/")
    headers: dict[str, str] = {}
    token = _credential_value(
        provider="npm",
        field_names=("token", "api_key", "value"),
        tool_name=tool_name,
        config=config,
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return registry, headers


@tool
def coingecko_price(
    ids: str,
    vs_currencies: str = "usd",
    include_market_cap: bool = False,
    include_24hr_vol: bool = False,
    include_24hr_change: bool = False,
    include_last_updated_at: bool = False,
) -> str:
    """Get current cryptocurrency prices from CoinGecko.

    Args:
        ids: CoinGecko coin IDs, comma-separated. Example: "bitcoin,ethereum".
        vs_currencies: Quote currencies, comma-separated. Example: "usd,aud".
        include_market_cap: Include market-cap fields.
        include_24hr_vol: Include 24-hour volume fields.
        include_24hr_change: Include 24-hour change fields.
        include_last_updated_at: Include Unix update timestamps.
    """
    if not _csv(ids):
        return "[Error]: ids is required."
    try:
        data = _get_json(
            f"{_COINGECKO_BASE_URL}/simple/price",
            {
                "ids": _csv(ids),
                "vs_currencies": _csv(vs_currencies) or "usd",
                "include_market_cap": str(include_market_cap).lower(),
                "include_24hr_vol": str(include_24hr_vol).lower(),
                "include_24hr_change": str(include_24hr_change).lower(),
                "include_last_updated_at": str(include_last_updated_at).lower(),
            },
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("coingecko_price failed", exc_info=True)
        return f"[Error]: CoinGecko price lookup failed: {e}"


@tool
def coingecko_coin_markets(
    vs_currency: str = "usd",
    ids: str = "",
    category: str = "",
    order: str = "market_cap_desc",
    limit: int = 10,
    page: int = 1,
    sparkline: bool = False,
    price_change_percentage: str = "",
) -> str:
    """List CoinGecko coin market data.

    Args:
        vs_currency: Quote currency, e.g. "usd".
        ids: Optional comma-separated CoinGecko coin IDs.
        category: Optional CoinGecko market category.
        order: CoinGecko ordering, default "market_cap_desc".
        limit: Number of markets to return, 1-250.
        page: Result page, 1 or greater.
        sparkline: Include sparkline data.
        price_change_percentage: Optional comma-separated windows, e.g. "1h,24h,7d".
    """
    try:
        data = _get_json(
            f"{_COINGECKO_BASE_URL}/coins/markets",
            {
                "vs_currency": (vs_currency or "usd").strip(),
                "ids": _csv(ids),
                "category": category.strip(),
                "order": order.strip() or "market_cap_desc",
                "per_page": max(1, min(250, int(limit))),
                "page": max(1, int(page)),
                "sparkline": str(sparkline).lower(),
                "price_change_percentage": _csv(price_change_percentage),
            },
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("coingecko_coin_markets failed", exc_info=True)
        return f"[Error]: CoinGecko markets lookup failed: {e}"


@tool
def hackernews_search(query: str = "", tags: str = "", limit: int = 10, page: int = 0) -> str:
    """Search Hacker News via the Algolia HN API.

    Args:
        query: Keyword query. Empty is allowed when filtering by tags.
        tags: Optional comma-separated tags like "story", "front_page", "ask_hn".
        limit: Number of results to return, 1-100.
        page: Zero-based result page.
    """
    try:
        data = _get_json(
            f"{_HACKERNEWS_BASE_URL}/search",
            {
                "query": query.strip(),
                "tags": _csv(tags),
                "hitsPerPage": max(1, min(100, int(limit))),
                "page": max(0, int(page)),
            },
        )
        return _dump_json(data.get("hits", data))
    except Exception as e:
        logger.error("hackernews_search failed", exc_info=True)
        return f"[Error]: Hacker News search failed: {e}"


@tool
def hackernews_get_item(item_id: int, include_comments: bool = False) -> str:
    """Get a Hacker News item by ID.

    Args:
        item_id: Hacker News item/article ID.
        include_comments: Include nested comments when available.
    """
    try:
        data = _get_json(f"{_HACKERNEWS_BASE_URL}/items/{int(item_id)}")
        if isinstance(data, dict) and not include_comments:
            data.pop("children", None)
        return _dump_json(data)
    except Exception as e:
        logger.error("hackernews_get_item failed", exc_info=True)
        return f"[Error]: Hacker News item lookup failed: {e}"


@tool
def hackernews_get_user(username: str) -> str:
    """Get a Hacker News user profile.

    Args:
        username: Hacker News username.
    """
    username = username.strip()
    if not username:
        return "[Error]: username is required."
    try:
        return _dump_json(_get_json(f"{_HACKERNEWS_BASE_URL}/users/{quote(username, safe='')}"))
    except Exception as e:
        logger.error("hackernews_get_user failed", exc_info=True)
        return f"[Error]: Hacker News user lookup failed: {e}"


@tool
def npm_package_info(
    package_name: str,
    version: str = "latest",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get npm package metadata.

    Args:
        package_name: Package name, including scope when applicable.
        version: Package version or dist-tag, default "latest".
    """
    package_name = package_name.strip()
    if not package_name:
        return "[Error]: package_name is required."
    registry, headers = _npm_registry_and_headers("npm_package_info", config)
    path = f"{quote(package_name, safe='')}/{quote((version or 'latest').strip(), safe='')}"
    try:
        return _dump_json(_get_json(f"{registry}/{path}", headers=headers or None))
    except Exception as e:
        logger.error("npm_package_info failed", exc_info=True)
        return f"[Error]: npm package lookup failed: {e}"


@tool
def npm_package_search(
    query: str,
    limit: int = 10,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search the npm registry for packages.

    Args:
        query: Search query.
        limit: Number of results, 1-100.
        offset: Result offset.
    """
    query = query.strip()
    if not query:
        return "[Error]: query is required."
    registry, headers = _npm_registry_and_headers("npm_package_search", config)
    try:
        data = _get_json(
            f"{registry}/-/v1/search",
            {
                "text": query,
                "size": max(1, min(100, int(limit))),
                "from": max(0, int(offset)),
                "popularity": 0.99,
            },
            headers=headers or None,
        )
        objects = data.get("objects", []) if isinstance(data, dict) else []
        results = [
            {
                "name": item.get("package", {}).get("name"),
                "version": item.get("package", {}).get("version"),
                "description": item.get("package", {}).get("description"),
                "keywords": item.get("package", {}).get("keywords"),
                "date": item.get("package", {}).get("date"),
                "links": item.get("package", {}).get("links"),
            }
            for item in objects
        ]
        return _dump_json(results)
    except Exception as e:
        logger.error("npm_package_search failed", exc_info=True)
        return f"[Error]: npm package search failed: {e}"


@tool
def open_thesaurus_synonyms(
    text: str,
    baseform: bool = False,
    similar: bool = False,
    startswith: bool = False,
    substring: bool = False,
    substring_from_results: int = 0,
    substring_max_results: int = 10,
    subsynsets: bool = False,
    supersynsets: bool = False,
) -> str:
    """Get German synonyms from OpenThesaurus.

    Args:
        text: German word or phrase.
        baseform: Ask OpenThesaurus to search the base form.
        similar: Include similarly written suggestions.
        startswith: Match words beginning with the query.
        substring: Match words containing the query.
        substring_from_results: Offset for substring results.
        substring_max_results: Maximum substring hits, 0-250.
        subsynsets: Include sub-terms where available.
        supersynsets: Include generic terms where available.
    """
    text = text.strip()
    if not text:
        return "[Error]: text is required."
    try:
        data = _get_json(
            f"{_OPENTHESAURUS_BASE_URL}/synonyme/search",
            {
                "q": text,
                "format": "application/json",
                "baseform": str(baseform).lower(),
                "similar": str(similar).lower(),
                "startswith": str(startswith).lower(),
                "substring": str(substring).lower(),
                "substringFromResults": max(0, int(substring_from_results)),
                "substringMaxResults": max(0, min(250, int(substring_max_results))),
                "subsynsets": str(subsynsets).lower(),
                "supersynsets": str(supersynsets).lower(),
            },
            headers={"User-Agent": "Nymeria"},
        )
        return _dump_json(data.get("synsets", data))
    except Exception as e:
        logger.error("open_thesaurus_synonyms failed", exc_info=True)
        return f"[Error]: OpenThesaurus lookup failed: {e}"


@tool
def rss_feed_read(url: str, max_items: int = 20, ignore_ssl: bool = False) -> str:
    """Read an RSS or Atom feed URL.

    Args:
        url: RSS or Atom feed URL.
        max_items: Maximum entries to return, 1-100.
        ignore_ssl: Ignore TLS certificate errors for the feed request.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "[Error]: url must be an absolute http(s) URL."
    try:
        import feedparser

        feed = feedparser.parse(_get_text(url.strip(), verify=not ignore_ssl))
        items = []
        for entry in feed.entries[: max(1, min(100, int(max_items)))]:
            items.append(
                {
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "published": entry.get("published") or entry.get("updated"),
                    "author": entry.get("author"),
                    "summary": entry.get("summary"),
                }
            )
        feed_meta = feed.feed if isinstance(feed.feed, dict) else {}
        return _dump_json(
            {
                "feed": {
                    "title": feed_meta.get("title"),
                    "link": feed_meta.get("link"),
                    "description": feed_meta.get("description"),
                },
                "items": items,
            }
        )
    except Exception as e:
        logger.error("rss_feed_read failed", exc_info=True)
        return f"[Error]: RSS read failed: {e}"


@tool
def nasa_apod(
    date: str = "",
    start_date: str = "",
    end_date: str = "",
    thumbs: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get NASA Astronomy Picture of the Day metadata.

    Args:
        date: Optional date in YYYY-MM-DD format.
        start_date: Optional range start date in YYYY-MM-DD format.
        end_date: Optional range end date in YYYY-MM-DD format.
        thumbs: Include video thumbnails when available.
    """
    api_key = _credential_value(
        provider="nasa",
        provider_aliases=("nasa_api",),
        field_names=("api_key", "value"),
        tool_name="nasa_apod",
        config=config,
    ) or _settings_value("nasa_api_key")
    if not api_key:
        return _setup_hint(
            provider="nasa",
            field_names=("api_key", "value"),
            tool_name="nasa_apod",
            env_var="NASA_API_KEY",
            display_name="NASA",
        )
    if date and (start_date or end_date):
        return "[Error]: use either date or start_date/end_date, not both."
    try:
        data = _get_json(
            f"{_NASA_BASE_URL}/planetary/apod",
            {
                "api_key": api_key,
                "date": date.strip(),
                "start_date": start_date.strip(),
                "end_date": end_date.strip(),
                "thumbs": str(thumbs).lower(),
            },
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("nasa_apod failed", exc_info=True)
        return f"[Error]: NASA APOD lookup failed: {e}"


def _openweather_location_params(
    city: str,
    city_id: Optional[int],
    latitude: Optional[float],
    longitude: Optional[float],
    zip_code: str,
) -> dict[str, Any]:
    if city.strip():
        return {"q": city.strip()}
    if city_id is not None:
        return {"id": city_id}
    if latitude is not None and longitude is not None:
        return {"lat": latitude, "lon": longitude}
    if zip_code.strip():
        return {"zip": zip_code.strip()}
    raise ValueError("provide city, city_id, latitude/longitude, or zip_code")


def _openweather_request(
    endpoint: str,
    city: str,
    city_id: Optional[int],
    latitude: Optional[float],
    longitude: Optional[float],
    zip_code: str,
    units: str,
    language: str,
    config: Optional[RunnableConfig],
    tool_name: str,
) -> str:
    api_key = _credential_value(
        provider="openweathermap",
        provider_aliases=("openweather", "open_weather_map"),
        field_names=("api_key", "access_token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("openweathermap_api_key")
    if not api_key:
        return _setup_hint(
            provider="openweathermap",
            field_names=("api_key", "value"),
            tool_name=tool_name,
            env_var="OPENWEATHERMAP_API_KEY",
            display_name="OpenWeatherMap",
        )
    try:
        params = {
            "APPID": api_key,
            "units": units or "metric",
            "lang": language.strip(),
            **_openweather_location_params(city, city_id, latitude, longitude, zip_code),
        }
        return _dump_json(_get_json(f"{_OPENWEATHER_BASE_URL}/{endpoint}", params))
    except Exception as e:
        logger.error("openweather %s failed", endpoint, exc_info=True)
        return f"[Error]: OpenWeatherMap request failed: {e}"


@tool
def openweathermap_current(
    city: str = "",
    city_id: Optional[int] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    zip_code: str = "",
    units: str = "metric",
    language: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get current weather from OpenWeatherMap.

    Args:
        city: City query, e.g. "Berlin,de".
        city_id: Optional OpenWeatherMap city ID.
        latitude: Optional latitude; requires longitude.
        longitude: Optional longitude; requires latitude.
        zip_code: Optional zip query, e.g. "10115,de".
        units: "metric", "imperial", or "standard".
        language: Optional two-letter language code.
    """
    return _openweather_request("weather", city, city_id, latitude, longitude, zip_code, units, language, config, "openweathermap_current")


@tool
def openweathermap_forecast(
    city: str = "",
    city_id: Optional[int] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    zip_code: str = "",
    units: str = "metric",
    language: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a 5-day weather forecast from OpenWeatherMap.

    Args:
        city: City query, e.g. "Berlin,de".
        city_id: Optional OpenWeatherMap city ID.
        latitude: Optional latitude; requires longitude.
        longitude: Optional longitude; requires latitude.
        zip_code: Optional zip query, e.g. "10115,de".
        units: "metric", "imperial", or "standard".
        language: Optional two-letter language code.
    """
    return _openweather_request("forecast", city, city_id, latitude, longitude, zip_code, units, language, config, "openweathermap_forecast")


@tool
def quickchart_create_url(
    chart_type: str,
    labels_json: str,
    data_json: str,
    label: str = "Chart",
    width: int = 500,
    height: int = 300,
    output_format: str = "png",
    background_color: str = "",
    device_pixel_ratio: int = 2,
) -> str:
    """Create a QuickChart chart URL from labels and data arrays.

    Args:
        chart_type: Chart.js type, e.g. "bar", "line", "pie".
        labels_json: JSON array of labels.
        data_json: JSON array of numeric values.
        label: Dataset label.
        width: Chart width in pixels.
        height: Chart height in pixels.
        output_format: "png", "svg", "pdf", or "webp".
        background_color: Optional CSS color for the background.
        device_pixel_ratio: Pixel ratio, 1-2.
    """
    try:
        labels = json.loads(labels_json)
        data = json.loads(data_json)
        if not isinstance(labels, list) or not isinstance(data, list):
            return "[Error]: labels_json and data_json must be JSON arrays."
        chart = {
            "type": chart_type.strip() or "bar",
            "data": {
                "labels": labels,
                "datasets": [{"label": label or "Chart", "data": data}],
            },
        }
        params = {
            "chart": json.dumps(chart, separators=(",", ":")),
            "width": max(100, min(4000, int(width))),
            "height": max(100, min(4000, int(height))),
            "format": (output_format or "png").strip().lower(),
            "devicePixelRatio": max(1, min(2, int(device_pixel_ratio))),
            "backgroundColor": background_color.strip(),
        }
        return _dump_json({"url": f"{_QUICKCHART_URL}?{urlencode(_filtered_params(params))}", "chart": chart})
    except Exception as e:
        logger.error("quickchart_create_url failed", exc_info=True)
        return f"[Error]: QuickChart URL creation failed: {e}"


PUBLIC_INFO_TOOLS = [
    coingecko_price,
    coingecko_coin_markets,
    hackernews_search,
    hackernews_get_item,
    hackernews_get_user,
    npm_package_info,
    npm_package_search,
    open_thesaurus_synonyms,
    rss_feed_read,
    nasa_apod,
    openweathermap_current,
    openweathermap_forecast,
    quickchart_create_url,
]
