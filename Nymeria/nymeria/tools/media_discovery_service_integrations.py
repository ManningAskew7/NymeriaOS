"""Media, book, and video catalog service tools."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_GOOGLE_BOOKS_BASE_URL = "https://www.googleapis.com/books/v1"
_YOUTUBE_BASE_URL = "https://www.googleapis.com/youtube/v3"
_SPOTIFY_BASE_URL = "https://api.spotify.com/v1"
_SPOTIFY_ACCOUNTS_BASE_URL = "https://accounts.spotify.com"
_SPOTIFY_TOKEN_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _filtered(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _limit(value: int, *, default: int = 10, max_value: int = 50) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


def _csv(value: str) -> str:
    return ",".join(part.strip() for part in value.replace("\n", ",").split(",") if part.strip())


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
    json_body: Any = None,
    form_data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params) if params is not None else None,
                json=json_body,
                data=_filtered(form_data) if form_data is not None else None,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except ValueError:
                return {"status": "ok", "status_code": response.status_code, "text": response.text}
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or error.get("error_description") or error.get("code") or "")
                else:
                    detail = str(error or body.get("message") or body.get("error_description") or "")
                errors = body.get("errors")
                if not detail and isinstance(errors, list):
                    detail = "; ".join(str(item) for item in errors[:3])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _json_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _is_setup_hint(value: str) -> bool:
    return value.startswith("[Error]: No ") or value.startswith("[Credential Setup Required]")


def _google_books_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider="google_books",
            provider_aliases=("googlebooks", "google_books_api"),
            field_names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("google_books_base_url")
        or _GOOGLE_BOOKS_BASE_URL
    )
    api_key = _credential_value(
        provider="google_books",
        provider_aliases=("googlebooks", "google_books_api"),
        field_names=("api_key", "apiKey", "key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("google_books_api_key")
    return _base_url(base), api_key


def _youtube_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | dict[str, str]]:
    base = (
        _credential_value(
            provider="youtube",
            provider_aliases=("youtube_data", "youtube_data_api", "google_youtube"),
            field_names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("youtube_base_url")
        or _YOUTUBE_BASE_URL
    )
    api_key = _credential_value(
        provider="youtube",
        provider_aliases=("youtube_data", "youtube_data_api", "google_youtube"),
        field_names=("api_key", "apiKey", "key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("youtube_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider="youtube",
            field_names=("api_key", "key", "value"),
            tool_name=tool_name,
            env_var="YOUTUBE_API_KEY",
            display_name="YouTube Data API",
        )
    return _base_url(base), api_key


def _spotify_base(tool_name: str, config: Optional[RunnableConfig]) -> str:
    base = (
        _credential_value(
            provider="spotify",
            provider_aliases=("spotify_api",),
            field_names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("spotify_base_url")
        or _SPOTIFY_BASE_URL
    )
    return _base_url(base)


def _spotify_accounts_base(tool_name: str, config: Optional[RunnableConfig]) -> str:
    base = (
        _credential_value(
            provider="spotify",
            provider_aliases=("spotify_api",),
            field_names=("accounts_base_url", "auth_base_url", "token_base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("spotify_accounts_base_url")
        or _SPOTIFY_ACCOUNTS_BASE_URL
    )
    return _base_url(base)


def _spotify_token_from_client_credentials(
    *,
    client_id: str,
    client_secret: str,
    accounts_base: str,
) -> str:
    cache_key = (client_id, client_secret, accounts_base)
    cached = _SPOTIFY_TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached[1] > now + 30:
        return cached[0]

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    data = _request_json(
        "POST",
        f"{accounts_base}/api/token",
        form_data={"grant_type": "client_credentials"},
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Nymeria",
        },
    )
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError("Spotify token response did not include access_token.")
    expires_in = int(data.get("expires_in") or 3600) if isinstance(data, dict) else 3600
    _SPOTIFY_TOKEN_CACHE[cache_key] = (str(token), now + max(60, expires_in - 60))
    return str(token)


def _spotify_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = _spotify_base(tool_name, config)
    access_token = _credential_value(
        provider="spotify",
        provider_aliases=("spotify_api",),
        field_names=("access_token", "accessToken", "bearer_token", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("spotify_access_token")
    if not access_token:
        client_id = _credential_value(
            provider="spotify",
            provider_aliases=("spotify_api",),
            field_names=("client_id", "clientId", "id"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("spotify_client_id")
        client_secret = _credential_value(
            provider="spotify",
            provider_aliases=("spotify_api",),
            field_names=("client_secret", "clientSecret", "secret"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("spotify_client_secret")
        if not client_id or not client_secret:
            return base, _setup_hint(
                provider="spotify",
                field_names=("access_token", "client_id", "client_secret", "value"),
                tool_name=tool_name,
                env_var="SPOTIFY_ACCESS_TOKEN or SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET",
                display_name="Spotify",
            )
        access_token = _spotify_token_from_client_credentials(
            client_id=client_id,
            client_secret=client_secret,
            accounts_base=_spotify_accounts_base(tool_name, config),
        )

    headers = _json_headers()
    headers["Authorization"] = f"Bearer {access_token}"
    return base, headers


@tool
def google_books_search(
    query: str,
    print_type: str = "all",
    projection: str = "lite",
    order_by: str = "relevance",
    filter_value: str = "",
    language: str = "",
    country: str = "",
    limit: int = 10,
    start_index: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search public Google Books volume metadata.

    Args:
        query: Book search query.
        print_type: "all", "books", or "magazines".
        projection: "lite" or "full".
        order_by: "relevance" or "newest".
        filter_value: Optional Google Books filter, e.g. "free-ebooks".
        language: Optional ISO language code.
        country: Optional two-letter country code.
        limit: Number of volumes, 1-40.
        start_index: Zero-based result offset.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, api_key = _google_books_config("google_books_search", config)
        data = _request_json(
            "GET",
            f"{base_url}/volumes",
            params={
                "q": query.strip(),
                "printType": print_type.strip() or "all",
                "projection": projection.strip() or "lite",
                "orderBy": order_by.strip() or "relevance",
                "filter": filter_value.strip(),
                "langRestrict": language.strip(),
                "country": country.strip(),
                "maxResults": _limit(limit, default=10, max_value=40),
                "startIndex": max(0, int(start_index)),
                "key": api_key,
            },
            headers=_json_headers(),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("google_books_search failed", exc_info=True)
        return f"[Error]: Google Books search failed: {e}"


@tool
def google_books_get_volume(
    volume_id: str,
    projection: str = "lite",
    country: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Google Books volume by ID.

    Args:
        volume_id: Google Books volume ID.
        projection: "lite" or "full".
        country: Optional two-letter country code.
    """
    volume_id = volume_id.strip()
    if not volume_id:
        return "[Error]: volume_id is required."
    try:
        base_url, api_key = _google_books_config("google_books_get_volume", config)
        data = _request_json(
            "GET",
            f"{base_url}/volumes/{quote(volume_id, safe='')}",
            params={
                "projection": projection.strip() or "lite",
                "country": country.strip(),
                "key": api_key,
            },
            headers=_json_headers(),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("google_books_get_volume failed", exc_info=True)
        return f"[Error]: Google Books volume lookup failed: {e}"


@tool
def youtube_search(
    query: str,
    search_type: str = "video",
    channel_id: str = "",
    order: str = "relevance",
    published_after: str = "",
    published_before: str = "",
    region_code: str = "",
    safe_search: str = "moderate",
    page_token: str = "",
    limit: int = 10,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search YouTube videos, channels, or playlists with the Data API.

    Args:
        query: Search query.
        search_type: Comma-separated types: "video", "channel", "playlist".
        channel_id: Optional channel ID filter.
        order: YouTube ordering, e.g. "relevance", "date", "viewCount".
        published_after: Optional RFC3339 lower bound.
        published_before: Optional RFC3339 upper bound.
        region_code: Optional two-letter region code.
        safe_search: "moderate", "none", or "strict".
        page_token: Optional page token for pagination.
        limit: Number of results, 1-50.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, key_or_error = _youtube_config("youtube_search", config)
        if isinstance(key_or_error, str) and _is_setup_hint(key_or_error):
            return key_or_error
        data = _request_json(
            "GET",
            f"{base_url}/search",
            params={
                "part": "snippet",
                "q": query.strip(),
                "type": _csv(search_type) or "video",
                "channelId": channel_id.strip(),
                "order": order.strip() or "relevance",
                "publishedAfter": published_after.strip(),
                "publishedBefore": published_before.strip(),
                "regionCode": region_code.strip(),
                "safeSearch": safe_search.strip() or "moderate",
                "pageToken": page_token.strip(),
                "maxResults": _limit(limit, default=10, max_value=50),
                "key": key_or_error,
            },
            headers=_json_headers(),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("youtube_search failed", exc_info=True)
        return f"[Error]: YouTube search failed: {e}"


@tool
def youtube_get_videos(
    video_ids: str,
    parts: str = "snippet,contentDetails,statistics,status",
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get YouTube video metadata for one or more video IDs.

    Args:
        video_ids: Comma-separated YouTube video IDs.
        parts: Comma-separated API parts.
        max_width: Optional player embed max width.
        max_height: Optional player embed max height.
    """
    if not _csv(video_ids):
        return "[Error]: video_ids is required."
    try:
        base_url, key_or_error = _youtube_config("youtube_get_videos", config)
        if isinstance(key_or_error, str) and _is_setup_hint(key_or_error):
            return key_or_error
        data = _request_json(
            "GET",
            f"{base_url}/videos",
            params={
                "part": _csv(parts) or "snippet,contentDetails,statistics,status",
                "id": _csv(video_ids),
                "maxWidth": max_width,
                "maxHeight": max_height,
                "key": key_or_error,
            },
            headers=_json_headers(),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("youtube_get_videos failed", exc_info=True)
        return f"[Error]: YouTube video lookup failed: {e}"


@tool
def youtube_get_channels(
    channel_ids: str = "",
    for_username: str = "",
    parts: str = "snippet,contentDetails,statistics,status",
    limit: int = 10,
    page_token: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get YouTube channel metadata by channel ID or legacy username.

    Args:
        channel_ids: Optional comma-separated channel IDs.
        for_username: Optional legacy YouTube username.
        parts: Comma-separated API parts.
        limit: Number of channels, 1-50.
        page_token: Optional page token.
    """
    if not _csv(channel_ids) and not for_username.strip():
        return "[Error]: provide channel_ids or for_username."
    try:
        base_url, key_or_error = _youtube_config("youtube_get_channels", config)
        if isinstance(key_or_error, str) and _is_setup_hint(key_or_error):
            return key_or_error
        data = _request_json(
            "GET",
            f"{base_url}/channels",
            params={
                "part": _csv(parts) or "snippet,contentDetails,statistics,status",
                "id": _csv(channel_ids),
                "forUsername": for_username.strip(),
                "maxResults": _limit(limit, default=10, max_value=50),
                "pageToken": page_token.strip(),
                "key": key_or_error,
            },
            headers=_json_headers(),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("youtube_get_channels failed", exc_info=True)
        return f"[Error]: YouTube channel lookup failed: {e}"


@tool
def youtube_list_playlist_items(
    playlist_id: str,
    parts: str = "snippet,contentDetails,status",
    page_token: str = "",
    limit: int = 10,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List items in a YouTube playlist.

    Args:
        playlist_id: YouTube playlist ID.
        parts: Comma-separated API parts.
        page_token: Optional page token.
        limit: Number of playlist items, 1-50.
    """
    playlist_id = playlist_id.strip()
    if not playlist_id:
        return "[Error]: playlist_id is required."
    try:
        base_url, key_or_error = _youtube_config("youtube_list_playlist_items", config)
        if isinstance(key_or_error, str) and _is_setup_hint(key_or_error):
            return key_or_error
        data = _request_json(
            "GET",
            f"{base_url}/playlistItems",
            params={
                "part": _csv(parts) or "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "pageToken": page_token.strip(),
                "maxResults": _limit(limit, default=10, max_value=50),
                "key": key_or_error,
            },
            headers=_json_headers(),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("youtube_list_playlist_items failed", exc_info=True)
        return f"[Error]: YouTube playlist item lookup failed: {e}"


@tool
def spotify_search(
    query: str,
    types: str = "track,artist,album,playlist",
    market: str = "",
    limit: int = 10,
    offset: int = 0,
    include_external: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Spotify catalog metadata.

    Args:
        query: Spotify search query.
        types: Comma-separated item types: track, artist, album, playlist, show, episode, audiobook.
        market: Optional ISO country code.
        limit: Number of results per type, 1-50.
        offset: Result offset.
        include_external: Optional value "audio".
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _spotify_config("spotify_search", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/search",
            params={
                "q": query.strip(),
                "type": _csv(types) or "track,artist,album,playlist",
                "market": market.strip(),
                "limit": _limit(limit, default=10, max_value=50),
                "offset": max(0, min(1000, int(offset))),
                "include_external": include_external.strip(),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("spotify_search failed", exc_info=True)
        return f"[Error]: Spotify search failed: {e}"


@tool
def spotify_get_track(
    track_id: str,
    market: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Spotify track metadata.

    Args:
        track_id: Spotify track ID.
        market: Optional ISO country code.
    """
    track_id = track_id.strip()
    if not track_id:
        return "[Error]: track_id is required."
    try:
        base_url, headers_or_error = _spotify_config("spotify_get_track", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/tracks/{quote(track_id, safe='')}",
            params={"market": market.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("spotify_get_track failed", exc_info=True)
        return f"[Error]: Spotify track lookup failed: {e}"


@tool
def spotify_get_artist(
    artist_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Spotify artist metadata.

    Args:
        artist_id: Spotify artist ID.
    """
    artist_id = artist_id.strip()
    if not artist_id:
        return "[Error]: artist_id is required."
    try:
        base_url, headers_or_error = _spotify_config("spotify_get_artist", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/artists/{quote(artist_id, safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("spotify_get_artist failed", exc_info=True)
        return f"[Error]: Spotify artist lookup failed: {e}"


@tool
def spotify_get_album(
    album_id: str,
    market: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Spotify album metadata.

    Args:
        album_id: Spotify album ID.
        market: Optional ISO country code.
    """
    album_id = album_id.strip()
    if not album_id:
        return "[Error]: album_id is required."
    try:
        base_url, headers_or_error = _spotify_config("spotify_get_album", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/albums/{quote(album_id, safe='')}",
            params={"market": market.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("spotify_get_album failed", exc_info=True)
        return f"[Error]: Spotify album lookup failed: {e}"


@tool
def spotify_get_playlist(
    playlist_id: str,
    market: str = "",
    fields: str = "",
    additional_types: str = "track,episode",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Spotify playlist metadata.

    Args:
        playlist_id: Spotify playlist ID.
        market: Optional ISO country code.
        fields: Optional Spotify fields filter expression.
        additional_types: Comma-separated item types to include.
    """
    playlist_id = playlist_id.strip()
    if not playlist_id:
        return "[Error]: playlist_id is required."
    try:
        base_url, headers_or_error = _spotify_config("spotify_get_playlist", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/playlists/{quote(playlist_id, safe='')}",
            params={
                "market": market.strip(),
                "fields": fields.strip(),
                "additional_types": _csv(additional_types) or "track,episode",
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("spotify_get_playlist failed", exc_info=True)
        return f"[Error]: Spotify playlist lookup failed: {e}"


MEDIA_DISCOVERY_SERVICE_TOOLS = [
    google_books_search,
    google_books_get_volume,
    youtube_search,
    youtube_get_videos,
    youtube_get_channels,
    youtube_list_playlist_items,
    spotify_search,
    spotify_get_track,
    spotify_get_artist,
    spotify_get_album,
    spotify_get_playlist,
]
