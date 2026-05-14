"""Community forum and publishing service tools."""

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
_REDDIT_BASE_URL = "https://oauth.reddit.com"
_REDDIT_PUBLIC_BASE_URL = "https://www.reddit.com"
_REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_MEDIUM_BASE_URL = "https://api.medium.com/v1"
_REDDIT_TOKEN_CACHE: dict[tuple[str, str, str, str], tuple[str, float]] = {}


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


def _csv_to_list(value: str, *, max_items: int | None = None) -> list[str]:
    parts = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    return parts[:max_items] if max_items is not None else parts


def _csv(value: str) -> str:
    return ",".join(_csv_to_list(value))


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
                    detail = str(
                        error.get("message")
                        or error.get("error_description")
                        or error.get("code")
                        or ""
                    )
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


def _reddit_token_url(tool_name: str, config: Optional[RunnableConfig]) -> str:
    return _base_url(
        _credential_value(
            provider="reddit",
            provider_aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
            field_names=("token_url", "tokenUrl", "auth_url", "authUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("reddit_token_url")
        or _REDDIT_TOKEN_URL
    )


def _reddit_base(tool_name: str, config: Optional[RunnableConfig], *, public: bool = False) -> str:
    field_names = ("public_base_url", "publicBaseUrl", "base_url", "baseUrl", "url") if public else (
        "base_url",
        "baseUrl",
        "api_url",
        "apiUrl",
        "url",
    )
    settings_name = "reddit_public_base_url" if public else "reddit_base_url"
    default = _REDDIT_PUBLIC_BASE_URL if public else _REDDIT_BASE_URL
    return _base_url(
        _credential_value(
            provider="reddit",
            provider_aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
            field_names=field_names,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_name)
        or default
    )


def _reddit_client_credentials(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str | None, str | None]:
    client_id = _credential_value(
        provider="reddit",
        provider_aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
        field_names=("client_id", "clientId", "id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("reddit_client_id")
    client_secret = _credential_value(
        provider="reddit",
        provider_aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
        field_names=("client_secret", "clientSecret", "secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("reddit_client_secret")
    return client_id, client_secret


def _reddit_cached_token(
    *,
    client_id: str,
    client_secret: str,
    token_url: str,
    grant_key: str,
    form_data: dict[str, str],
) -> str:
    cache_key = (client_id, client_secret, token_url, grant_key)
    now = time.time()
    cached = _REDDIT_TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > now + 30:
        return cached[0]

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    data = _request_json(
        "POST",
        token_url,
        form_data=form_data,
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Nymeria",
        },
    )
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError("Reddit token response did not include access_token.")
    expires_in = int(data.get("expires_in") or 3600) if isinstance(data, dict) else 3600
    _REDDIT_TOKEN_CACHE[cache_key] = (str(token), now + max(60, expires_in - 60))
    return str(token)


def _reddit_access_token(
    tool_name: str,
    config: Optional[RunnableConfig],
    *,
    allow_client_credentials: bool,
) -> str | None:
    access_token = _credential_value(
        provider="reddit",
        provider_aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
        field_names=("access_token", "accessToken", "bearer_token", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("reddit_access_token")
    if access_token:
        return access_token

    client_id, client_secret = _reddit_client_credentials(tool_name, config)
    if not client_id or not client_secret:
        return None
    token_url = _reddit_token_url(tool_name, config)
    refresh_token = _credential_value(
        provider="reddit",
        provider_aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
        field_names=("refresh_token", "refreshToken"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("reddit_refresh_token")
    if refresh_token:
        return _reddit_cached_token(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            grant_key=f"refresh:{refresh_token}",
            form_data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    if allow_client_credentials:
        return _reddit_cached_token(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            grant_key="client_credentials",
            form_data={"grant_type": "client_credentials"},
        )
    return None


def _reddit_config(
    tool_name: str,
    config: Optional[RunnableConfig],
    *,
    require_user_token: bool = False,
) -> tuple[str, dict[str, str] | str, bool]:
    token = _reddit_access_token(
        tool_name,
        config,
        allow_client_credentials=not require_user_token,
    )
    if token:
        headers = _json_headers()
        headers["Authorization"] = f"Bearer {token}"
        return _reddit_base(tool_name, config), headers, True
    if require_user_token:
        return _reddit_base(tool_name, config), _setup_hint(
            provider="reddit",
            field_names=("access_token", "refresh_token", "client_id", "client_secret", "value"),
            tool_name=tool_name,
            env_var="REDDIT_ACCESS_TOKEN or REDDIT_REFRESH_TOKEN with REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET",
            display_name="Reddit",
        ), False
    return _reddit_base(tool_name, config, public=True), _json_headers(), False


def _reddit_endpoint(base_url: str, path: str, *, oauth: bool) -> str:
    clean_path = path.strip().lstrip("/")
    if not oauth and not clean_path.endswith(".json"):
        clean_path += ".json"
    return f"{base_url}/{clean_path}"


def _reddit_fullname(value: str, default_prefix: str) -> str:
    clean = value.strip()
    if not clean:
        return clean
    return clean if "_" in clean and clean[:2] in {"t1", "t2", "t3", "t4", "t5", "t6"} else f"{default_prefix}_{clean}"


def _discourse_base(tool_name: str, config: Optional[RunnableConfig]) -> str | None:
    return (
        _credential_value(
            provider="discourse",
            provider_aliases=("discourse_api",),
            field_names=("base_url", "baseUrl", "url", "domain", "host"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("discourse_base_url")
    )


def _discourse_auth(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str | None, str | None]:
    api_key = _credential_value(
        provider="discourse",
        provider_aliases=("discourse_api",),
        field_names=("api_key", "apiKey", "key", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("discourse_api_key")
    username = _credential_value(
        provider="discourse",
        provider_aliases=("discourse_api",),
        field_names=("api_username", "apiUsername", "username", "user"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("discourse_api_username")
    return api_key, username


def _discourse_config(
    tool_name: str,
    config: Optional[RunnableConfig],
    *,
    require_auth: bool = False,
) -> tuple[str, dict[str, str] | str]:
    base = _discourse_base(tool_name, config)
    if not base:
        return "", _setup_hint(
            provider="discourse",
            field_names=("base_url", "url", "domain", "host"),
            tool_name=tool_name,
            env_var="DISCOURSE_BASE_URL",
            display_name="Discourse",
        )
    api_key, username = _discourse_auth(tool_name, config)
    if require_auth and (not api_key or not username):
        return _base_url(base), _setup_hint(
            provider="discourse",
            field_names=("api_key", "api_username", "base_url"),
            tool_name=tool_name,
            env_var="DISCOURSE_API_KEY and DISCOURSE_API_USERNAME",
            display_name="Discourse",
        )
    headers = _json_headers()
    if api_key:
        headers["Api-Key"] = api_key
    if username:
        headers["Api-Username"] = username
    return _base_url(base), headers


def _medium_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = _base_url(
        _credential_value(
            provider="medium",
            provider_aliases=("medium_api", "medium_oauth2", "medium_oauth2_api"),
            field_names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("medium_base_url")
        or _MEDIUM_BASE_URL
    )
    token = _credential_value(
        provider="medium",
        provider_aliases=("medium_api", "medium_oauth2", "medium_oauth2_api"),
        field_names=("access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("medium_access_token")
    if not token:
        return base, _setup_hint(
            provider="medium",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="MEDIUM_ACCESS_TOKEN",
            display_name="Medium",
        )
    headers = _json_headers()
    headers["Accept-Charset"] = "utf-8"
    headers["Authorization"] = f"Bearer {token}"
    return base, headers


def _medium_user_id(base_url: str, headers: dict[str, str], explicit_user_id: str) -> str:
    if explicit_user_id.strip():
        return explicit_user_id.strip()
    data = _request_json("GET", f"{base_url}/me", headers=headers)
    user = data.get("data") if isinstance(data, dict) else {}
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        raise RuntimeError("Medium /me response did not include data.id.")
    return str(user_id)


@tool
def reddit_search_posts(
    query: str,
    subreddit: str = "",
    sort: str = "relevance",
    time_filter: str = "all",
    limit: int = 10,
    after: str = "",
    include_over_18: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Reddit posts.

    Args:
        query: Reddit search query.
        subreddit: Optional subreddit name to restrict the search.
        sort: Search sort, e.g. "relevance", "new", "top", or "comments".
        time_filter: Time filter, e.g. "hour", "day", "week", "month", "year", "all".
        limit: Number of posts, 1-100.
        after: Optional listing cursor.
        include_over_18: Include NSFW results when the API permits it.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_search_posts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        path = f"r/{quote(subreddit.strip(), safe='')}/search" if subreddit.strip() else "search"
        data = _request_json(
            "GET",
            _reddit_endpoint(base_url, path, oauth=oauth),
            params={
                "q": query.strip(),
                "restrict_sr": bool(subreddit.strip()),
                "sort": sort.strip() or "relevance",
                "t": time_filter.strip() or "all",
                "limit": _limit(limit, default=10, max_value=100),
                "after": after.strip(),
                "include_over_18": include_over_18,
                "raw_json": 1,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_search_posts failed", exc_info=True)
        return f"[Error]: Reddit search failed: {e}"


@tool
def reddit_list_subreddit_posts(
    subreddit: str,
    listing: str = "hot",
    time_filter: str = "day",
    limit: int = 10,
    after: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List posts from a subreddit listing.

    Args:
        subreddit: Subreddit name without r/.
        listing: "hot", "new", "top", "rising", or "controversial".
        time_filter: Time filter for top/controversial listings.
        limit: Number of posts, 1-100.
        after: Optional listing cursor.
    """
    subreddit = subreddit.strip().removeprefix("r/")
    if not subreddit:
        return "[Error]: subreddit is required."
    allowed = {"hot", "new", "top", "rising", "controversial"}
    listing = listing.strip().lower() or "hot"
    if listing not in allowed:
        return f"[Error]: listing must be one of {sorted(allowed)}."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_list_subreddit_posts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _reddit_endpoint(base_url, f"r/{quote(subreddit, safe='')}/{listing}", oauth=oauth),
            params={
                "t": time_filter.strip() or "day",
                "limit": _limit(limit, default=10, max_value=100),
                "after": after.strip(),
                "raw_json": 1,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_list_subreddit_posts failed", exc_info=True)
        return f"[Error]: Reddit listing failed: {e}"


@tool
def reddit_get_post(
    post_id: str,
    subreddit: str = "",
    comment_limit: int = 25,
    comment_sort: str = "confidence",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Reddit post and top-level comment listing.

    Args:
        post_id: Reddit post ID or t3_ fullname.
        subreddit: Optional subreddit name. If omitted, Reddit resolves by ID.
        comment_limit: Number of comments to include, 1-100.
        comment_sort: Comment sort, e.g. "confidence", "top", "new", "old".
    """
    post_id = _reddit_fullname(post_id, "t3").removeprefix("t3_")
    if not post_id:
        return "[Error]: post_id is required."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_get_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        path = f"r/{quote(subreddit.strip().removeprefix('r/'), safe='')}/comments/{quote(post_id, safe='')}" if subreddit.strip() else f"comments/{quote(post_id, safe='')}"
        data = _request_json(
            "GET",
            _reddit_endpoint(base_url, path, oauth=oauth),
            params={
                "limit": _limit(comment_limit, default=25, max_value=100),
                "sort": comment_sort.strip() or "confidence",
                "raw_json": 1,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_get_post failed", exc_info=True)
        return f"[Error]: Reddit post lookup failed: {e}"


@tool
def reddit_get_subreddit(
    subreddit: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get subreddit metadata.

    Args:
        subreddit: Subreddit name without r/.
    """
    subreddit = subreddit.strip().removeprefix("r/")
    if not subreddit:
        return "[Error]: subreddit is required."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_get_subreddit", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _reddit_endpoint(base_url, f"r/{quote(subreddit, safe='')}/about", oauth=oauth),
            params={"raw_json": 1},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_get_subreddit failed", exc_info=True)
        return f"[Error]: Reddit subreddit lookup failed: {e}"


@tool
def reddit_get_user(
    username: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Reddit user metadata.

    Args:
        username: Reddit username.
    """
    username = username.strip()
    if not username:
        return "[Error]: username is required."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _reddit_endpoint(base_url, f"user/{quote(username, safe='')}/about", oauth=oauth),
            params={"raw_json": 1},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_get_user failed", exc_info=True)
        return f"[Error]: Reddit user lookup failed: {e}"


@tool
def reddit_create_post(
    subreddit: str,
    title: str,
    kind: str = "self",
    text: str = "",
    url: str = "",
    resubmit: bool = False,
    send_replies: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Reddit post.

    Args:
        subreddit: Target subreddit without r/.
        title: Post title.
        kind: "self" for text, "link" for URL.
        text: Text body for self posts.
        url: URL for link posts.
        resubmit: Allow resubmitting a link.
        send_replies: Send inbox replies for the post.
    """
    subreddit = subreddit.strip().removeprefix("r/")
    if not subreddit or not title.strip():
        return "[Error]: subreddit and title are required."
    kind = (kind or "self").strip().lower()
    if kind not in {"self", "link"}:
        return '[Error]: kind must be "self" or "link".'
    if kind == "self" and not text.strip():
        return "[Error]: text is required for self posts."
    if kind == "link" and not url.strip():
        return "[Error]: url is required for link posts."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_create_post", config, require_user_token=True)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            _reddit_endpoint(base_url, "api/submit", oauth=oauth),
            form_data={
                "api_type": "json",
                "sr": subreddit,
                "title": title.strip(),
                "kind": kind,
                "text": text.strip() if kind == "self" else None,
                "url": url.strip() if kind == "link" else None,
                "resubmit": resubmit,
                "sendreplies": send_replies,
                "raw_json": 1,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_create_post failed", exc_info=True)
        return f"[Error]: Reddit post creation failed: {e}"


@tool
def reddit_create_comment(
    parent_fullname: str,
    text: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Reddit comment or reply.

    Args:
        parent_fullname: Parent post/comment fullname, e.g. t3_abc or t1_def.
        text: Markdown comment body.
    """
    parent_fullname = parent_fullname.strip()
    if not parent_fullname or not text.strip():
        return "[Error]: parent_fullname and text are required."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_create_comment", config, require_user_token=True)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            _reddit_endpoint(base_url, "api/comment", oauth=oauth),
            form_data={
                "api_type": "json",
                "thing_id": parent_fullname,
                "text": text,
                "raw_json": 1,
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("reddit_create_comment failed", exc_info=True)
        return f"[Error]: Reddit comment creation failed: {e}"


@tool
def reddit_delete_thing(
    fullname: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Reddit post or comment owned by the authenticated account.

    Args:
        fullname: Reddit fullname for a post/comment, e.g. t3_abc or t1_def.
    """
    fullname = fullname.strip()
    if not fullname:
        return "[Error]: fullname is required."
    try:
        base_url, headers_or_error, oauth = _reddit_config("reddit_delete_thing", config, require_user_token=True)
        if isinstance(headers_or_error, str):
            return headers_or_error
        _request_json(
            "POST",
            _reddit_endpoint(base_url, "api/del", oauth=oauth),
            form_data={"id": fullname},
            headers=headers_or_error,
        )
        return _dump_json({"status": "deleted", "id": fullname})
    except Exception as e:
        logger.error("reddit_delete_thing failed", exc_info=True)
        return f"[Error]: Reddit delete failed: {e}"


@tool
def discourse_search(
    query: str,
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search a Discourse forum.

    Args:
        query: Discourse search query.
        page: Result page, 1 or greater.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _discourse_config("discourse_search", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/search.json",
            params={"q": query.strip(), "page": max(1, int(page))},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_search failed", exc_info=True)
        return f"[Error]: Discourse search failed: {e}"


@tool
def discourse_list_latest_topics(
    page: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List latest topics from a Discourse forum.

    Args:
        page: Zero-based page number.
    """
    try:
        base_url, headers_or_error = _discourse_config("discourse_list_latest_topics", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/latest.json",
            params={"page": max(0, int(page))},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_list_latest_topics failed", exc_info=True)
        return f"[Error]: Discourse latest topics failed: {e}"


@tool
def discourse_get_topic(
    topic_id: int,
    include_raw: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Discourse topic and its first post stream page.

    Args:
        topic_id: Discourse topic ID.
        include_raw: Include raw post Markdown when permissions allow it.
    """
    try:
        base_url, headers_or_error = _discourse_config("discourse_get_topic", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/t/{int(topic_id)}.json",
            params={"include_raw": include_raw},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_get_topic failed", exc_info=True)
        return f"[Error]: Discourse topic lookup failed: {e}"


@tool
def discourse_get_post(
    post_id: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Discourse post by ID.

    Args:
        post_id: Discourse post ID.
    """
    try:
        base_url, headers_or_error = _discourse_config("discourse_get_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/posts/{int(post_id)}.json", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_get_post failed", exc_info=True)
        return f"[Error]: Discourse post lookup failed: {e}"


@tool
def discourse_create_topic(
    title: str,
    raw: str,
    category_id: Optional[int] = None,
    tags: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Discourse topic.

    Args:
        title: Topic title.
        raw: Raw Markdown post body.
        category_id: Optional category ID.
        tags: Optional comma-separated tag names.
    """
    if not title.strip() or not raw.strip():
        return "[Error]: title and raw are required."
    try:
        base_url, headers_or_error = _discourse_config("discourse_create_topic", config, require_auth=True)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/posts.json",
            json_body={
                "title": title.strip(),
                "raw": raw,
                "category": category_id,
                "tags": _csv_to_list(tags),
                "archetype": "regular",
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_create_topic failed", exc_info=True)
        return f"[Error]: Discourse topic creation failed: {e}"


@tool
def discourse_create_post(
    topic_id: int,
    raw: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a reply post in a Discourse topic.

    Args:
        topic_id: Target Discourse topic ID.
        raw: Raw Markdown post body.
    """
    if not raw.strip():
        return "[Error]: raw is required."
    try:
        base_url, headers_or_error = _discourse_config("discourse_create_post", config, require_auth=True)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/posts.json",
            json_body={"topic_id": int(topic_id), "raw": raw},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_create_post failed", exc_info=True)
        return f"[Error]: Discourse post creation failed: {e}"


@tool
def discourse_update_post(
    post_id: int,
    raw: str,
    edit_reason: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Discourse post.

    Args:
        post_id: Discourse post ID.
        raw: Replacement raw Markdown body.
        edit_reason: Optional edit reason.
    """
    if not raw.strip():
        return "[Error]: raw is required."
    try:
        base_url, headers_or_error = _discourse_config("discourse_update_post", config, require_auth=True)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/posts/{int(post_id)}.json",
            json_body={"post": {"raw": raw, "edit_reason": edit_reason.strip()}},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("discourse_update_post failed", exc_info=True)
        return f"[Error]: Discourse post update failed: {e}"


@tool
def medium_get_me(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Get the authenticated Medium user profile."""
    try:
        base_url, headers_or_error = _medium_config("medium_get_me", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/me", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("medium_get_me failed", exc_info=True)
        return f"[Error]: Medium profile lookup failed: {e}"


@tool
def medium_list_publications(
    user_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Medium publications associated with a user.

    Args:
        user_id: Optional Medium user ID. If omitted, the authenticated user's ID is used.
    """
    try:
        base_url, headers_or_error = _medium_config("medium_list_publications", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        resolved_user_id = _medium_user_id(base_url, headers_or_error, user_id)
        data = _request_json(
            "GET",
            f"{base_url}/users/{quote(resolved_user_id, safe='')}/publications",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("medium_list_publications failed", exc_info=True)
        return f"[Error]: Medium publications lookup failed: {e}"


def _medium_post_body(
    *,
    title: str,
    content_format: str,
    content: str,
    tags: str,
    publish_status: str,
    canonical_url: str,
    notify_followers: bool,
    license_value: str,
) -> dict[str, Any] | str:
    if not title.strip() or not content.strip():
        return "[Error]: title and content are required."
    content_format = (content_format or "markdown").strip().lower()
    if content_format not in {"html", "markdown"}:
        return '[Error]: content_format must be "html" or "markdown".'
    body: dict[str, Any] = {
        "title": title.strip(),
        "contentFormat": content_format,
        "content": content,
        "publishStatus": (publish_status or "draft").strip(),
        "tags": _csv_to_list(tags, max_items=3),
        "notifyFollowers": notify_followers,
    }
    if canonical_url.strip():
        body["canonicalUrl"] = canonical_url.strip()
    if license_value.strip():
        body["license"] = license_value.strip()
    return body


@tool
def medium_create_post(
    title: str,
    content: str,
    content_format: str = "markdown",
    user_id: str = "",
    tags: str = "",
    publish_status: str = "draft",
    canonical_url: str = "",
    notify_followers: bool = False,
    license_value: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Medium post on the authenticated user's profile.

    Args:
        title: Post title.
        content: HTML fragment or Markdown post content.
        content_format: "html" or "markdown".
        user_id: Optional Medium author ID. If omitted, /me is used.
        tags: Optional comma-separated tags; Medium uses at most three.
        publish_status: "draft", "public", or "unlisted".
        canonical_url: Optional canonical source URL.
        notify_followers: Notify followers when publishing.
        license_value: Optional Medium license value.
    """
    try:
        base_url, headers_or_error = _medium_config("medium_create_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _medium_post_body(
            title=title,
            content_format=content_format,
            content=content,
            tags=tags,
            publish_status=publish_status,
            canonical_url=canonical_url,
            notify_followers=notify_followers,
            license_value=license_value,
        )
        if isinstance(body, str):
            return body
        author_id = _medium_user_id(base_url, headers_or_error, user_id)
        data = _request_json(
            "POST",
            f"{base_url}/users/{quote(author_id, safe='')}/posts",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("medium_create_post failed", exc_info=True)
        return f"[Error]: Medium post creation failed: {e}"


@tool
def medium_create_publication_post(
    publication_id: str,
    title: str,
    content: str,
    content_format: str = "markdown",
    tags: str = "",
    publish_status: str = "draft",
    canonical_url: str = "",
    notify_followers: bool = False,
    license_value: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Medium post under a publication.

    Args:
        publication_id: Medium publication ID.
        title: Post title.
        content: HTML fragment or Markdown post content.
        content_format: "html" or "markdown".
        tags: Optional comma-separated tags; Medium uses at most three.
        publish_status: "draft", "public", or "unlisted".
        canonical_url: Optional canonical source URL.
        notify_followers: Notify followers when publishing.
        license_value: Optional Medium license value.
    """
    publication_id = publication_id.strip()
    if not publication_id:
        return "[Error]: publication_id is required."
    try:
        base_url, headers_or_error = _medium_config("medium_create_publication_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _medium_post_body(
            title=title,
            content_format=content_format,
            content=content,
            tags=tags,
            publish_status=publish_status,
            canonical_url=canonical_url,
            notify_followers=notify_followers,
            license_value=license_value,
        )
        if isinstance(body, str):
            return body
        data = _request_json(
            "POST",
            f"{base_url}/publications/{quote(publication_id, safe='')}/posts",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("medium_create_publication_post failed", exc_info=True)
        return f"[Error]: Medium publication post creation failed: {e}"


COMMUNITY_PUBLISHING_SERVICE_TOOLS = [
    reddit_search_posts,
    reddit_list_subreddit_posts,
    reddit_get_post,
    reddit_get_subreddit,
    reddit_get_user,
    reddit_create_post,
    reddit_create_comment,
    reddit_delete_thing,
    discourse_search,
    discourse_list_latest_topics,
    discourse_get_topic,
    discourse_get_post,
    discourse_create_topic,
    discourse_create_post,
    discourse_update_post,
    medium_get_me,
    medium_list_publications,
    medium_create_post,
    medium_create_publication_post,
]
