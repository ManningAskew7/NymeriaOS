"""Community forum and publishing service tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import hashlib
import hmac
import logging
import time
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

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
    filtered as _filtered,
    parse_json as _parse_json,
    request_with_policy as _request_with_policy,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_REDDIT_BASE_URL = "https://oauth.reddit.com"
_REDDIT_PUBLIC_BASE_URL = "https://www.reddit.com"
_REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_MEDIUM_BASE_URL = "https://api.medium.com/v1"
_LINKEDIN_BASE_URL = "https://api.linkedin.com"
_TWITTER_BASE_URL = "https://api.twitter.com/2"
_FACEBOOK_GRAPH_BASE_URL = "https://graph.facebook.com/v23.0"
_REDDIT_TOKEN_CACHE: dict[tuple[str, str, str, str], tuple[str, float]] = {}

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_LINKEDIN = register_provider_spec(
    ProviderCredentialSpec(
        provider="linkedin",
        aliases=("linkedin_oauth2", "linkedin_oauth2_api", "linkedin_community_management"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "bearer_token", "bearerToken", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=("access_token", "accessToken", "bearer_token", "bearerToken", "token", "value"),
        env_var="LINKEDIN_ACCESS_TOKEN",
        display_name="LinkedIn",
    )
)

_TWITTER = register_provider_spec(
    ProviderCredentialSpec(
        provider="twitter",
        aliases=("x", "x_twitter", "twitter_oauth2", "twitter_oauth2_api"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=(
                    "bearer_token",
                    "bearerToken",
                    "access_token",
                    "accessToken",
                    "api_key",
                    "apiKey",
                    "token",
                    "value",
                ),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
        ),
        hint_fields=(
            "bearer_token",
            "bearerToken",
            "access_token",
            "accessToken",
            "api_key",
            "apiKey",
            "token",
            "value",
        ),
        env_var="TWITTER_BEARER_TOKEN",
        display_name="X/Twitter",
    )
)

_FACEBOOK = register_provider_spec(
    ProviderCredentialSpec(
        provider="facebook",
        aliases=("facebook_graph", "facebook_graph_api", "meta_graph"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=(
                    "access_token",
                    "accessToken",
                    "page_access_token",
                    "pageAccessToken",
                    "token",
                    "value",
                ),
            ),
            CredentialFieldGroup(
                role="app_secret",
                names=("app_secret", "appSecret", "client_secret", "clientSecret"),
                required=False,
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="page_access_token",
                names=("page_access_token", "pageAccessToken"),
                required=False,
            ),
        ),
        hint_fields=(
            "access_token",
            "accessToken",
            "page_access_token",
            "pageAccessToken",
            "token",
            "value",
        ),
        env_var="FACEBOOK_ACCESS_TOKEN",
        display_name="Facebook Graph",
    )
)

# Reddit selects the api or public base-URL tuple per branch in _reddit_base;
# both variants are declared as distinct groups (base_url / public_base_url).
_REDDIT = register_provider_spec(
    ProviderCredentialSpec(
        provider="reddit",
        aliases=("reddit_api", "reddit_oauth2", "reddit_oauth2_api"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "bearer_token", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="public_base_url",
                names=("public_base_url", "publicBaseUrl", "base_url", "baseUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(role="client_id", names=("client_id", "clientId", "id")),
            CredentialFieldGroup(
                role="client_secret", names=("client_secret", "clientSecret", "secret")
            ),
            CredentialFieldGroup(role="refresh_token", names=("refresh_token", "refreshToken")),
            CredentialFieldGroup(
                role="token_url",
                names=("token_url", "tokenUrl", "auth_url", "authUrl"),
                required=False,
            ),
        ),
        hint_fields=("access_token", "refresh_token", "client_id", "client_secret", "value"),
        env_var="REDDIT_ACCESS_TOKEN or REDDIT_REFRESH_TOKEN with REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET",
        display_name="Reddit",
    )
)

# Discourse has two setup-hint variants (missing-base-url vs missing-auth); the
# spec carries the auth variant and the base-url variant's field_names/env_var
# stay inline in that branch.
_DISCOURSE = register_provider_spec(
    ProviderCredentialSpec(
        provider="discourse",
        aliases=("discourse_api",),
        groups=(
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "key", "token", "value")),
            CredentialFieldGroup(
                role="api_username", names=("api_username", "apiUsername", "username", "user")
            ),
            CredentialFieldGroup(
                role="base_url", names=("base_url", "baseUrl", "url", "domain", "host")
            ),
        ),
        hint_fields=("api_key", "api_username", "base_url"),
        env_var="DISCOURSE_API_KEY and DISCOURSE_API_USERNAME",
        display_name="Discourse",
    )
)

_MEDIUM = register_provider_spec(
    ProviderCredentialSpec(
        provider="medium",
        aliases=("medium_api", "medium_oauth2", "medium_oauth2_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="token", names=("access_token", "accessToken", "token", "value")),
        ),
        hint_fields=("access_token", "token", "value"),
        env_var="MEDIUM_ACCESS_TOKEN",
        display_name="Medium",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _csv_to_list(value: str, *, max_items: int | None = None) -> list[str]:
    parts = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    return parts[:max_items] if max_items is not None else parts


def _csv(value: str) -> str:
    return ",".join(_csv_to_list(value))


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
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(
                client,
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


def _bearer_service_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    token_fields: tuple[str, ...],
    token_settings: tuple[str, ...],
    base_settings: tuple[str, ...],
    default_base_url: str,
    env_var: str,
    display_name: str,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str]] | str:
    token = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=token_fields,
        tool_name=tool_name,
        config=config,
    )
    for setting_name in token_settings:
        if token:
            break
        token = _settings_value(setting_name)
    if not token:
        return _setup_hint(
            provider=provider,
            field_names=token_fields,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )

    base_url = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
        tool_name=tool_name,
        config=config,
    )
    for setting_name in base_settings:
        if base_url:
            break
        base_url = _settings_value(setting_name)
    return _base_url(base_url or default_base_url), {
        **_json_headers(),
        "Authorization": f"Bearer {token}",
    }


def _linkedin_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str]] | str:
    resolved = _bearer_service_config(
        provider=_LINKEDIN.provider,
        provider_aliases=_LINKEDIN.aliases,
        token_fields=_LINKEDIN.group("token"),
        token_settings=("linkedin_access_token",),
        base_settings=("linkedin_base_url",),
        default_base_url=_LINKEDIN_BASE_URL,
        env_var=_LINKEDIN.env_var,
        display_name=_LINKEDIN.display_name,
        tool_name=tool_name,
        config=config,
    )
    if isinstance(resolved, str):
        return resolved
    base_url, headers = resolved
    headers["X-Restli-Protocol-Version"] = "2.0.0"
    headers["LinkedIn-Version"] = str(_settings_value("linkedin_api_version") or "202604")
    return base_url, headers


def _twitter_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str]] | str:
    return _bearer_service_config(
        provider=_TWITTER.provider,
        provider_aliases=_TWITTER.aliases,
        token_fields=_TWITTER.group("token"),
        token_settings=("twitter_bearer_token", "twitter_access_token"),
        base_settings=("twitter_api_base_url",),
        default_base_url=_TWITTER_BASE_URL,
        env_var=_TWITTER.env_var,
        display_name=_TWITTER.display_name,
        tool_name=tool_name,
        config=config,
    )


def _facebook_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str], dict[str, str]] | str:
    resolved = _bearer_service_config(
        provider=_FACEBOOK.provider,
        provider_aliases=_FACEBOOK.aliases,
        token_fields=_FACEBOOK.group("token"),
        token_settings=("facebook_access_token",),
        base_settings=("facebook_graph_base_url",),
        default_base_url=_FACEBOOK_GRAPH_BASE_URL,
        env_var=_FACEBOOK.env_var,
        display_name=_FACEBOOK.display_name,
        tool_name=tool_name,
        config=config,
    )
    if isinstance(resolved, str):
        return resolved
    base_url, headers = resolved
    app_secret = _credential_value(
        provider=_FACEBOOK.provider,
        provider_aliases=_FACEBOOK.aliases,
        field_names=_FACEBOOK.group("app_secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("facebook_app_secret")
    params: dict[str, str] = {}
    if app_secret:
        token = headers["Authorization"].removeprefix("Bearer ").strip()
        params["appsecret_proof"] = hmac.new(
            app_secret.encode("utf-8"),
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    return base_url, headers, params


def _reddit_token_url(tool_name: str, config: Optional[RunnableConfig]) -> str:
    return _base_url(
        _credential_value(
            provider=_REDDIT.provider,
            provider_aliases=_REDDIT.aliases,
            field_names=_REDDIT.group("token_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("reddit_token_url")
        or _REDDIT_TOKEN_URL
    )


def _reddit_base(tool_name: str, config: Optional[RunnableConfig], *, public: bool = False) -> str:
    field_names = _REDDIT.group("public_base_url") if public else _REDDIT.group("base_url")
    settings_name = "reddit_public_base_url" if public else "reddit_base_url"
    default = _REDDIT_PUBLIC_BASE_URL if public else _REDDIT_BASE_URL
    return _base_url(
        _credential_value(
            provider=_REDDIT.provider,
            provider_aliases=_REDDIT.aliases,
            field_names=field_names,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_name)
        or default
    )


def _reddit_client_credentials(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str | None, str | None]:
    client_id = _credential_value(
        provider=_REDDIT.provider,
        provider_aliases=_REDDIT.aliases,
        field_names=_REDDIT.group("client_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("reddit_client_id")
    client_secret = _credential_value(
        provider=_REDDIT.provider,
        provider_aliases=_REDDIT.aliases,
        field_names=_REDDIT.group("client_secret"),
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
        provider=_REDDIT.provider,
        provider_aliases=_REDDIT.aliases,
        field_names=_REDDIT.group("token"),
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
        provider=_REDDIT.provider,
        provider_aliases=_REDDIT.aliases,
        field_names=_REDDIT.group("refresh_token"),
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
            provider=_REDDIT.provider,
            field_names=_REDDIT.hint_fields,
            tool_name=tool_name,
            env_var=_REDDIT.env_var,
            display_name=_REDDIT.display_name,
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
            provider=_DISCOURSE.provider,
            provider_aliases=_DISCOURSE.aliases,
            field_names=_DISCOURSE.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("discourse_base_url")
    )


def _discourse_auth(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str | None, str | None]:
    api_key = _credential_value(
        provider=_DISCOURSE.provider,
        provider_aliases=_DISCOURSE.aliases,
        field_names=_DISCOURSE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("discourse_api_key")
    username = _credential_value(
        provider=_DISCOURSE.provider,
        provider_aliases=_DISCOURSE.aliases,
        field_names=_DISCOURSE.group("api_username"),
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
        # Branch-variant hint: missing base_url. field_names/env_var stay inline;
        # the spec's hint_fields/env_var carry the missing-auth variant below.
        return "", _setup_hint(
            provider=_DISCOURSE.provider,
            field_names=("base_url", "url", "domain", "host"),
            tool_name=tool_name,
            env_var="DISCOURSE_BASE_URL",
            display_name=_DISCOURSE.display_name,
        )
    api_key, username = _discourse_auth(tool_name, config)
    if require_auth and (not api_key or not username):
        return _base_url(base), _setup_hint(
            provider=_DISCOURSE.provider,
            field_names=_DISCOURSE.hint_fields,
            tool_name=tool_name,
            env_var=_DISCOURSE.env_var,
            display_name=_DISCOURSE.display_name,
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
            provider=_MEDIUM.provider,
            provider_aliases=_MEDIUM.aliases,
            field_names=_MEDIUM.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("medium_base_url")
        or _MEDIUM_BASE_URL
    )
    token = _credential_value(
        provider=_MEDIUM.provider,
        provider_aliases=_MEDIUM.aliases,
        field_names=_MEDIUM.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("medium_access_token")
    if not token:
        return base, _setup_hint(
            provider=_MEDIUM.provider,
            field_names=_MEDIUM.hint_fields,
            tool_name=tool_name,
            env_var=_MEDIUM.env_var,
            display_name=_MEDIUM.display_name,
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


def _twitter_tweet_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("tweet_id is required.")
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[-2] == "status":
            return parts[-1]
        raise ValueError("tweet URL must include /status/<id>.")
    return cleaned


def _twitter_user_id_from_username(base_url: str, headers: dict[str, str], username: str) -> str:
    cleaned = username.strip().lstrip("@")
    if not cleaned:
        raise ValueError("username is required.")
    data = _request_json(
        "GET",
        f"{base_url}/users/by/username/{quote(cleaned, safe='')}",
        headers=headers,
    )
    if isinstance(data, dict):
        user_id = data.get("id") or data.get("data", {}).get("id")
        if user_id:
            return str(user_id)
    raise RuntimeError("X/Twitter user lookup did not return an id.")


def _twitter_current_user_id(base_url: str, headers: dict[str, str]) -> str:
    data = _request_json("GET", f"{base_url}/users/me", headers=headers)
    if isinstance(data, dict):
        user_id = data.get("id") or data.get("data", {}).get("id")
        if user_id:
            return str(user_id)
    raise RuntimeError("X/Twitter /users/me did not return an id.")


@tool
def twitter_get_me(
    user_fields: str = "id,name,username,verified,profile_image_url",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the authenticated X/Twitter user."""
    try:
        resolved = _twitter_config("twitter_get_me", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        data = _request_json(
            "GET",
            f"{base_url}/users/me",
            params={"user.fields": _csv(user_fields)},
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("twitter_get_me failed", exc_info=True)
        return f"[Error]: X/Twitter profile lookup failed: {e}"


@tool
def twitter_get_user(
    user_id: str = "",
    username: str = "",
    user_fields: str = "id,name,username,description,verified,profile_image_url,public_metrics",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an X/Twitter user by ID or username."""
    try:
        resolved = _twitter_config("twitter_get_user", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        params = {"user.fields": _csv(user_fields)}
        if user_id.strip():
            url = f"{base_url}/users/{quote(user_id.strip(), safe='')}"
        elif username.strip():
            url = f"{base_url}/users/by/username/{quote(username.strip().lstrip('@'), safe='')}"
        else:
            return "[Error]: user_id or username is required."
        return _dump_json(_request_json("GET", url, params=params, headers=headers))
    except Exception as e:
        logger.error("twitter_get_user failed", exc_info=True)
        return f"[Error]: X/Twitter user lookup failed: {e}"


@tool
def twitter_search_recent(
    query: str,
    limit: int = 10,
    sort_order: str = "recency",
    start_time: str = "",
    end_time: str = "",
    tweet_fields: str = "id,text,author_id,created_at,public_metrics,lang",
    expansions: str = "author_id",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search recent X/Twitter posts."""
    if not query.strip():
        return "[Error]: query is required."
    try:
        resolved = _twitter_config("twitter_search_recent", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        params = {
            "query": query.strip(),
            "max_results": _limit(limit, default=10, max_value=100),
            "sort_order": sort_order.strip() or "recency",
            "tweet.fields": _csv(tweet_fields),
            "expansions": _csv(expansions),
        }
        if start_time.strip():
            params["start_time"] = start_time.strip()
        if end_time.strip():
            params["end_time"] = end_time.strip()
        return _dump_json(
            _request_json("GET", f"{base_url}/tweets/search/recent", params=params, headers=headers)
        )
    except Exception as e:
        logger.error("twitter_search_recent failed", exc_info=True)
        return f"[Error]: X/Twitter recent search failed: {e}"


@tool
def twitter_create_post(
    text: str,
    reply_to_tweet_id: str = "",
    quote_tweet_id: str = "",
    media_ids: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an X/Twitter post, reply, or quote post."""
    if not text.strip():
        return "[Error]: text is required."
    try:
        resolved = _twitter_config("twitter_create_post", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        body: dict[str, Any] = {"text": text}
        if reply_to_tweet_id.strip():
            body["reply"] = {"in_reply_to_tweet_id": _twitter_tweet_id(reply_to_tweet_id)}
        if quote_tweet_id.strip():
            body["quote_tweet_id"] = _twitter_tweet_id(quote_tweet_id)
        media = _csv_to_list(media_ids, max_items=4)
        if media:
            body["media"] = {"media_ids": media}
        return _dump_json(_request_json("POST", f"{base_url}/tweets", json_body=body, headers=headers))
    except Exception as e:
        logger.error("twitter_create_post failed", exc_info=True)
        return f"[Error]: X/Twitter post creation failed: {e}"


@tool
def twitter_delete_post(
    tweet_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an X/Twitter post by ID or URL."""
    try:
        resolved = _twitter_config("twitter_delete_post", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        return _dump_json(
            _request_json(
                "DELETE",
                f"{base_url}/tweets/{quote(_twitter_tweet_id(tweet_id), safe='')}",
                headers=headers,
            )
        )
    except Exception as e:
        logger.error("twitter_delete_post failed", exc_info=True)
        return f"[Error]: X/Twitter post deletion failed: {e}"


@tool
def twitter_like_post(
    tweet_id: str,
    user_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Like an X/Twitter post as the authenticated user."""
    try:
        resolved = _twitter_config("twitter_like_post", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        acting_user_id = user_id.strip() or _twitter_current_user_id(base_url, headers)
        body = {"tweet_id": _twitter_tweet_id(tweet_id)}
        return _dump_json(
            _request_json(
                "POST",
                f"{base_url}/users/{quote(acting_user_id, safe='')}/likes",
                json_body=body,
                headers=headers,
            )
        )
    except Exception as e:
        logger.error("twitter_like_post failed", exc_info=True)
        return f"[Error]: X/Twitter like failed: {e}"


@tool
def twitter_repost(
    tweet_id: str,
    user_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Repost an X/Twitter post as the authenticated user."""
    try:
        resolved = _twitter_config("twitter_repost", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        acting_user_id = user_id.strip() or _twitter_current_user_id(base_url, headers)
        body = {"tweet_id": _twitter_tweet_id(tweet_id)}
        return _dump_json(
            _request_json(
                "POST",
                f"{base_url}/users/{quote(acting_user_id, safe='')}/retweets",
                json_body=body,
                headers=headers,
            )
        )
    except Exception as e:
        logger.error("twitter_repost failed", exc_info=True)
        return f"[Error]: X/Twitter repost failed: {e}"


@tool
def twitter_send_direct_message(
    text: str,
    recipient_user_id: str = "",
    recipient_username: str = "",
    media_ids: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an X/Twitter direct message to a user."""
    if not text.strip():
        return "[Error]: text is required."
    try:
        resolved = _twitter_config("twitter_send_direct_message", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        user_id = recipient_user_id.strip()
        if not user_id and recipient_username.strip():
            user_id = _twitter_user_id_from_username(base_url, headers, recipient_username)
        if not user_id:
            return "[Error]: recipient_user_id or recipient_username is required."
        body: dict[str, Any] = {"text": text}
        media = _csv_to_list(media_ids, max_items=4)
        if media:
            body["attachments"] = [{"media_id": item} for item in media]
        return _dump_json(
            _request_json(
                "POST",
                f"{base_url}/dm_conversations/with/{quote(user_id, safe='')}/messages",
                json_body=body,
                headers=headers,
            )
        )
    except Exception as e:
        logger.error("twitter_send_direct_message failed", exc_info=True)
        return f"[Error]: X/Twitter direct message failed: {e}"


def _linkedin_author_urn(
    base_url: str,
    headers: dict[str, str],
    *,
    author_urn: str,
    person_id: str,
    organization_id: str,
) -> str:
    if author_urn.strip():
        return author_urn.strip()
    if person_id.strip():
        return f"urn:li:person:{person_id.strip()}"
    if organization_id.strip():
        return f"urn:li:organization:{organization_id.strip()}"
    profile = _request_json("GET", f"{base_url}/v2/userinfo", headers=headers)
    if isinstance(profile, dict):
        profile_id = profile.get("sub") or profile.get("id")
        if profile_id:
            return f"urn:li:person:{profile_id}"
    raise RuntimeError("LinkedIn profile lookup did not return a person id. Pass author_urn or person_id.")


@tool
def linkedin_get_me(
    use_userinfo: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the authenticated LinkedIn member profile."""
    try:
        resolved = _linkedin_config("linkedin_get_me", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        path = "/v2/userinfo" if use_userinfo else "/v2/me"
        return _dump_json(_request_json("GET", f"{base_url}{path}", headers=headers))
    except Exception as e:
        logger.error("linkedin_get_me failed", exc_info=True)
        return f"[Error]: LinkedIn profile lookup failed: {e}"


@tool
def linkedin_create_post(
    text: str,
    author_urn: str = "",
    person_id: str = "",
    organization_id: str = "",
    visibility: str = "PUBLIC",
    article_url: str = "",
    article_title: str = "",
    article_description: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a LinkedIn text or article post."""
    if not text.strip():
        return "[Error]: text is required."
    try:
        resolved = _linkedin_config("linkedin_create_post", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers = resolved
        author = _linkedin_author_urn(
            base_url,
            headers,
            author_urn=author_urn,
            person_id=person_id,
            organization_id=organization_id,
        )
        body: dict[str, Any] = {
            "author": author,
            "commentary": text,
            "lifecycleState": "PUBLISHED",
            "visibility": visibility.strip().upper() or "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "thirdPartyDistributionChannels": [],
            },
        }
        if article_url.strip():
            article: dict[str, Any] = {"source": article_url.strip()}
            if article_title.strip():
                article["title"] = article_title.strip()
            if article_description.strip():
                article["description"] = article_description.strip()
            body["content"] = {"article": article}
        return _dump_json(_request_json("POST", f"{base_url}/rest/posts", json_body=body, headers=headers))
    except Exception as e:
        logger.error("linkedin_create_post failed", exc_info=True)
        return f"[Error]: LinkedIn post creation failed: {e}"


@tool
def facebook_graph_get_me(
    fields: str = "id,name",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the authenticated Facebook Graph profile."""
    try:
        resolved = _facebook_config("facebook_graph_get_me", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers, auth_params = resolved
        params = {**auth_params, "fields": _csv(fields)}
        return _dump_json(_request_json("GET", f"{base_url}/me", params=params, headers=headers))
    except Exception as e:
        logger.error("facebook_graph_get_me failed", exc_info=True)
        return f"[Error]: Facebook Graph profile lookup failed: {e}"


@tool
def facebook_graph_get_node(
    node_id: str,
    edge: str = "",
    fields: str = "",
    query_json: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Facebook Graph node or list one of its edges."""
    if not node_id.strip():
        return "[Error]: node_id is required."
    try:
        resolved = _facebook_config("facebook_graph_get_node", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers, auth_params = resolved
        params = {**auth_params, **_parse_json(query_json, expected=dict, label="query_json")}
        if fields.strip():
            params["fields"] = _csv(fields)
        if edge.strip():
            params.setdefault("limit", _limit(limit, default=25, max_value=100))
            path = f"/{quote(node_id.strip(), safe='')}/{quote(edge.strip().strip('/'), safe='')}"
        else:
            path = f"/{quote(node_id.strip(), safe='')}"
        return _dump_json(_request_json("GET", f"{base_url}{path}", params=params, headers=headers))
    except Exception as e:
        logger.error("facebook_graph_get_node failed", exc_info=True)
        return f"[Error]: Facebook Graph lookup failed: {e}"


@tool
def facebook_page_list_accounts(
    fields: str = "id,name,category,tasks",
    include_access_tokens: bool = False,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Facebook pages/accounts available to the authenticated user."""
    try:
        resolved = _facebook_config("facebook_page_list_accounts", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers, auth_params = resolved
        requested_fields = _csv(fields)
        if include_access_tokens and "access_token" not in requested_fields.split(","):
            requested_fields = f"{requested_fields},access_token" if requested_fields else "access_token"
        params = {
            **auth_params,
            "fields": requested_fields,
            "limit": _limit(limit, default=25, max_value=100),
        }
        return _dump_json(_request_json("GET", f"{base_url}/me/accounts", params=params, headers=headers))
    except Exception as e:
        logger.error("facebook_page_list_accounts failed", exc_info=True)
        return f"[Error]: Facebook page account list failed: {e}"


@tool
def facebook_page_create_post(
    page_id: str,
    message: str,
    link: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Facebook Page feed post."""
    if not page_id.strip():
        return "[Error]: page_id is required."
    if not message.strip() and not link.strip():
        return "[Error]: message or link is required."
    try:
        resolved = _facebook_config("facebook_page_create_post", config)
        if isinstance(resolved, str):
            return resolved
        base_url, headers, auth_params = resolved
        body: dict[str, Any] = {}
        if message.strip():
            body["message"] = message
        if link.strip():
            body["link"] = link.strip()
        request_headers = dict(headers)
        params = dict(auth_params)
        page_access_token = _credential_value(
            provider=_FACEBOOK.provider,
            provider_aliases=_FACEBOOK.aliases,
            field_names=_FACEBOOK.group("page_access_token"),
            tool_name="facebook_page_create_post",
            config=config,
        )
        if page_access_token:
            request_headers["Authorization"] = f"Bearer {page_access_token}"
            app_secret = _credential_value(
                provider=_FACEBOOK.provider,
                provider_aliases=_FACEBOOK.aliases,
                field_names=_FACEBOOK.group("app_secret"),
                tool_name="facebook_page_create_post",
                config=config,
            ) or _settings_value("facebook_app_secret")
            if app_secret:
                params["appsecret_proof"] = hmac.new(
                    app_secret.encode("utf-8"),
                    page_access_token.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
        return _dump_json(
            _request_json(
                "POST",
                f"{base_url}/{quote(page_id.strip(), safe='')}/feed",
                params=params,
                form_data=body,
                headers=request_headers,
            )
        )
    except Exception as e:
        logger.error("facebook_page_create_post failed", exc_info=True)
        return f"[Error]: Facebook Page post creation failed: {e}"


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
    twitter_get_me,
    twitter_get_user,
    twitter_search_recent,
    twitter_create_post,
    twitter_delete_post,
    twitter_like_post,
    twitter_repost,
    twitter_send_direct_message,
    linkedin_get_me,
    linkedin_create_post,
    facebook_graph_get_me,
    facebook_graph_get_node,
    facebook_page_list_accounts,
    facebook_page_create_post,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="community_publishing", tools=tuple(COMMUNITY_PUBLISHING_SERVICE_TOOLS)))
