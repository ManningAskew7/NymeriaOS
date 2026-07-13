"""Content management and publishing service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import base64
import json
import logging
import time
from typing import Annotated, Any, Optional
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

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
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 80_000
_CONTENTFUL_BASE_URL = "https://cdn.contentful.com"
_CONTENTFUL_PREVIEW_BASE_URL = "https://preview.contentful.com"
_GHOST_API_VERSION = "v5.0"
_STORYBLOK_CONTENT_BASE_URL = "https://api.storyblok.com/v2/cdn"
_STORYBLOK_MANAGEMENT_BASE_URL = "https://mapi.storyblok.com/v1"
_WEBFLOW_BASE_URL = "https://api.webflow.com/v2"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_WORDPRESS = register_provider_spec(
    ProviderCredentialSpec(
        provider="wordpress",
        aliases=("wordpress_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "url", "site_url", "wordpress_url"),
                required=False,
            ),
            CredentialFieldGroup(role="username", names=("username", "user", "email")),
            CredentialFieldGroup(
                role="password",
                names=(
                    "password",
                    "application_password",
                    "applicationPassword",
                    "app_password",
                    "appPassword",
                ),
            ),
        ),
        hint_fields=("username", "password", "application_password"),
        env_var="WORDPRESS_USERNAME + WORDPRESS_PASSWORD",
        display_name="WordPress",
    )
)

_STRAPI = register_provider_spec(
    ProviderCredentialSpec(
        provider="strapi",
        aliases=("strapi_api",),
        groups=(
            CredentialFieldGroup(
                role="api_token",
                names=("api_token", "apiToken", "jwt", "access_token", "token", "value"),
            ),
            CredentialFieldGroup(
                role="api_version", names=("api_version", "apiVersion", "version"), required=False
            ),
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="email", names=("email", "identifier", "username"), required=False
            ),
            CredentialFieldGroup(role="password", names=("password",), required=False),
        ),
        hint_fields=("api_token", "jwt", "token", "value"),
        env_var="STRAPI_API_TOKEN or STRAPI_EMAIL + STRAPI_PASSWORD",
        display_name="Strapi",
    )
)

# Contentful selects the delivery or preview token/base per branch; the setup
# hint's env_var is set inline per branch (the spec carries the delivery value).
_CONTENTFUL = register_provider_spec(
    ProviderCredentialSpec(
        provider="contentful",
        aliases=("contentful_api",),
        groups=(
            CredentialFieldGroup(role="space_id", names=("space_id", "spaceId"), required=False),
            CredentialFieldGroup(
                role="delivery_token",
                names=(
                    "delivery_token",
                    "ContentDeliveryaccessToken",
                    "content_delivery_access_token",
                    "token",
                ),
            ),
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="preview_token",
                names=("preview_token", "ContentPreviewaccessToken", "content_preview_access_token"),
            ),
            CredentialFieldGroup(
                role="preview_base_url", names=("preview_base_url",), required=False
            ),
        ),
        hint_fields=("delivery_token", "preview_token", "token"),
        env_var="CONTENTFUL_DELIVERY_TOKEN",
        display_name="Contentful",
    )
)

# Ghost splits into admin-api and content-api paths with distinct aliases and
# two token/hint variants. The spec declares the union of aliases and both
# token groups; call sites that use a single-path alias keep it inline, and the
# content-path setup hint's field_names/env_var stay inline in that branch.
_GHOST = register_provider_spec(
    ProviderCredentialSpec(
        provider="ghost",
        aliases=("ghost_admin_api", "ghost_content_api"),
        groups=(
            CredentialFieldGroup(role="url", names=("url", "base_url", "site_url"), required=False),
            CredentialFieldGroup(
                role="admin_api_key", names=("admin_api_key", "apiKey", "api_key", "key", "value")
            ),
            CredentialFieldGroup(
                role="content_api_key",
                names=("content_api_key", "contentApiKey", "api_key", "key", "token", "value"),
            ),
        ),
        hint_fields=("admin_api_key", "api_key", "key", "value"),
        env_var="GHOST_ADMIN_API_KEY",
        display_name="Ghost",
    )
)

# Storyblok splits into content-api and management-api paths with distinct
# aliases and two token/hint variants (as Ghost above); the management setup
# hint's field_names/env_var stay inline in that branch.
_STORYBLOK = register_provider_spec(
    ProviderCredentialSpec(
        provider="storyblok",
        aliases=("storyblok_content_api", "storyblok_management_api"),
        groups=(
            CredentialFieldGroup(
                role="content_base_url",
                names=("base_url", "url", "content_base_url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="content_token",
                names=("content_token", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="mgmt_base_url", names=("management_base_url", "base_url", "url"), required=False
            ),
            CredentialFieldGroup(
                role="mgmt_token",
                names=("management_token", "accessToken", "access_token", "token", "value"),
            ),
            CredentialFieldGroup(role="space_id", names=("space_id", "spaceId"), required=False),
        ),
        hint_fields=("content_token", "api_key", "token", "value"),
        env_var="STORYBLOK_CONTENT_TOKEN",
        display_name="Storyblok",
    )
)

_WEBFLOW = register_provider_spec(
    ProviderCredentialSpec(
        provider="webflow",
        aliases=("webflow_api", "webflow_oauth2_api"),
        groups=(
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
        ),
        hint_fields=("access_token", "value"),
        env_var="WEBFLOW_ACCESS_TOKEN",
        display_name="Webflow",
    )
)

_WORDPRESS_RESOURCES = {
    "post": "posts",
    "posts": "posts",
    "page": "pages",
    "pages": "pages",
    "user": "users",
    "users": "users",
}
_CONTENTFUL_RESOURCES = {
    "entry": "entries",
    "entries": "entries",
    "asset": "assets",
    "assets": "assets",
    "content_type": "content_types",
    "content_types": "content_types",
    "locale": "locales",
    "locales": "locales",
}


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 500) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _normal_resource(resource: str, mapping: dict[str, str]) -> str:
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in mapping:
        raise ValueError(f"Unsupported resource {resource!r}. Supported: {', '.join(sorted(mapping))}.")
    return mapping[key]


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
                params=_filtered(params) if params is not None else None,
                json=json_body,
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
            detail = (
                body.get("message")
                or body.get("error")
                or body.get("error_description")
                or body.get("detail")
                or body.get("errorMessage")
                or ""
            )
            if not detail and isinstance(body.get("errors"), list):
                detail = "; ".join(str(item) for item in body["errors"][:3])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _auth_basic(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def _wordpress_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_WORDPRESS.provider,
            provider_aliases=_WORDPRESS.aliases,
            field_names=_WORDPRESS.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("wordpress_url")
    )
    username = _credential_value(
        provider=_WORDPRESS.provider,
        provider_aliases=_WORDPRESS.aliases,
        field_names=_WORDPRESS.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("wordpress_username")
    password = _credential_value(
        provider=_WORDPRESS.provider,
        provider_aliases=_WORDPRESS.aliases,
        field_names=_WORDPRESS.group("password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("wordpress_password")
    if not base:
        return "", (
            "[Error]: No WordPress URL found. Save a WordPress credential with "
            '"url" / "site_url", or set WORDPRESS_URL.'
        )
    base = _base_url(base)
    if not base.endswith("/wp-json/wp/v2"):
        base = f"{base}/wp-json/wp/v2"
    if not username or not password:
        return base, _setup_hint(
            provider=_WORDPRESS.provider,
            field_names=_WORDPRESS.hint_fields,
            tool_name=tool_name,
            env_var=_WORDPRESS.env_var,
            display_name=_WORDPRESS.display_name,
        )
    return base, {
        "Accept": "application/json",
        "Authorization": f"Basic {_auth_basic(username, password)}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _strapi_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_STRAPI.provider,
            provider_aliases=_STRAPI.aliases,
            field_names=_STRAPI.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("strapi_url")
    )
    version = (
        _credential_value(
            provider=_STRAPI.provider,
            provider_aliases=_STRAPI.aliases,
            field_names=_STRAPI.group("api_version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("strapi_api_version")
        or "v4"
    ).lower()
    if not base:
        return "", version, (
            "[Error]: No Strapi URL found. Save a Strapi credential with "
            '"url" / "base_url", or set STRAPI_URL.'
        )
    base = _base_url(base)
    api_root = base if version == "v3" else (base if base.endswith("/api") else f"{base}/api")
    token = _credential_value(
        provider=_STRAPI.provider,
        provider_aliases=_STRAPI.aliases,
        field_names=_STRAPI.group("api_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("strapi_api_token")
    if not token:
        email = _credential_value(
            provider=_STRAPI.provider,
            provider_aliases=_STRAPI.aliases,
            field_names=_STRAPI.group("email"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("strapi_email")
        password = _credential_value(
            provider=_STRAPI.provider,
            provider_aliases=_STRAPI.aliases,
            field_names=_STRAPI.group("password"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("strapi_password")
        if email and password:
            login_url = f"{api_root}/auth/local"
            login_data = _request_json(
                "POST",
                login_url,
                json_body={"identifier": email, "password": password},
                headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"},
            )
            token = login_data.get("jwt") if isinstance(login_data, dict) else None
    if not token:
        return api_root, version, _setup_hint(
            provider=_STRAPI.provider,
            field_names=_STRAPI.hint_fields,
            tool_name=tool_name,
            env_var=_STRAPI.env_var,
            display_name=_STRAPI.display_name,
        )
    return api_root, version, {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _strapi_payload(fields: dict[str, Any], version: str) -> dict[str, Any]:
    return fields if version == "v3" else {"data": fields}


def _contentful_config(
    tool_name: str,
    config: Optional[RunnableConfig],
    source: str,
) -> tuple[str, str, dict[str, str] | str]:
    space_id = _credential_value(
        provider=_CONTENTFUL.provider,
        provider_aliases=_CONTENTFUL.aliases,
        field_names=_CONTENTFUL.group("space_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("contentful_space_id")
    if source == "preview":
        token = _credential_value(
            provider=_CONTENTFUL.provider,
            provider_aliases=_CONTENTFUL.aliases,
            field_names=_CONTENTFUL.group("preview_token"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("contentful_preview_token")
        base = (
            _credential_value(
                provider=_CONTENTFUL.provider,
                provider_aliases=_CONTENTFUL.aliases,
                field_names=_CONTENTFUL.group("preview_base_url"),
                tool_name=tool_name,
                config=config,
            )
            or _settings_value("contentful_preview_base_url")
            or _CONTENTFUL_PREVIEW_BASE_URL
        )
        # Branch-variant env_var: preview vs delivery. Stays inline; the spec
        # carries the delivery value for the shared setup hint below.
        env_var = "CONTENTFUL_PREVIEW_TOKEN"
    else:
        token = _credential_value(
            provider=_CONTENTFUL.provider,
            provider_aliases=_CONTENTFUL.aliases,
            field_names=_CONTENTFUL.group("delivery_token"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("contentful_delivery_token")
        base = (
            _credential_value(
                provider=_CONTENTFUL.provider,
                provider_aliases=_CONTENTFUL.aliases,
                field_names=_CONTENTFUL.group("base_url"),
                tool_name=tool_name,
                config=config,
            )
            or _settings_value("contentful_base_url")
            or _CONTENTFUL_BASE_URL
        )
        env_var = "CONTENTFUL_DELIVERY_TOKEN"
    if not space_id:
        return _base_url(base), "", (
            "[Error]: No Contentful space ID found. Save a Contentful credential with "
            '"space_id" / "spaceId", or set CONTENTFUL_SPACE_ID.'
        )
    if not token:
        return _base_url(base), space_id, _setup_hint(
            provider=_CONTENTFUL.provider,
            field_names=_CONTENTFUL.hint_fields,
            tool_name=tool_name,
            env_var=env_var,
            display_name=_CONTENTFUL.display_name,
        )
    return _base_url(base), space_id, {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _ghost_site_url(tool_name: str, config: Optional[RunnableConfig]) -> str | None:
    value = (
        _credential_value(
            provider=_GHOST.provider,
            provider_aliases=_GHOST.aliases,
            field_names=_GHOST.group("url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("ghost_url")
    )
    return _base_url(value) if value else None


def _ghost_admin_headers(tool_name: str, config: Optional[RunnableConfig]) -> dict[str, str] | str:
    admin_key = _credential_value(
        provider=_GHOST.provider,
        provider_aliases=("ghost_admin_api",),  # admin-path alias only (see _GHOST spec)
        field_names=_GHOST.group("admin_api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("ghost_admin_api_key")
    if not admin_key:
        return _setup_hint(
            provider=_GHOST.provider,
            field_names=_GHOST.hint_fields,
            tool_name=tool_name,
            env_var=_GHOST.env_var,
            display_name=_GHOST.display_name,
        )
    try:
        import jwt

        key_id, secret = admin_key.split(":", 1)
        now = int(time.time())
        token = jwt.encode(
            {"iat": now, "exp": now + 300, "aud": "/admin/"},
            bytes.fromhex(secret),
            algorithm="HS256",
            headers={"kid": key_id},
        )
    except Exception as e:
        raise ValueError("Ghost admin API key must be formatted as key_id:hex_secret.") from e
    return {
        "Accept": "application/json",
        "Accept-Version": _settings_value("ghost_api_version") or _GHOST_API_VERSION,
        "Authorization": f"Ghost {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _ghost_content_key(tool_name: str, config: Optional[RunnableConfig]) -> str | None:
    return _credential_value(
        provider=_GHOST.provider,
        provider_aliases=("ghost_content_api",),  # content-path alias only (see _GHOST spec)
        field_names=_GHOST.group("content_api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("ghost_content_api_key")


def _storyblok_content_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | dict[str, str]]:
    base = (
        _credential_value(
            provider=_STORYBLOK.provider,
            provider_aliases=("storyblok_content_api",),  # content-path alias only (see _STORYBLOK)
            field_names=_STORYBLOK.group("content_base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("storyblok_content_base_url")
        or _STORYBLOK_CONTENT_BASE_URL
    )
    token = _credential_value(
        provider=_STORYBLOK.provider,
        provider_aliases=("storyblok_content_api",),  # content-path alias only (see _STORYBLOK)
        field_names=_STORYBLOK.group("content_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("storyblok_content_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_STORYBLOK.provider,
            field_names=_STORYBLOK.hint_fields,
            tool_name=tool_name,
            env_var=_STORYBLOK.env_var,
            display_name=_STORYBLOK.display_name,
        )
    return _base_url(base), {"token": token}


def _storyblok_management_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_STORYBLOK.provider,
            provider_aliases=("storyblok_management_api",),  # management-path alias only
            field_names=_STORYBLOK.group("mgmt_base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("storyblok_management_base_url")
        or _STORYBLOK_MANAGEMENT_BASE_URL
    )
    space_id = _credential_value(
        provider=_STORYBLOK.provider,
        provider_aliases=("storyblok_management_api",),  # management-path alias only
        field_names=_STORYBLOK.group("space_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("storyblok_space_id")
    token = _credential_value(
        provider=_STORYBLOK.provider,
        provider_aliases=("storyblok_management_api",),  # management-path alias only
        field_names=_STORYBLOK.group("mgmt_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("storyblok_management_token")
    if not space_id:
        return _base_url(base), "", (
            "[Error]: No Storyblok space ID found. Save a Storyblok credential with "
            '"space_id" / "spaceId", or set STORYBLOK_SPACE_ID.'
        )
    if not token:
        # Management-path hint variant: field_names/env_var inline (see _STORYBLOK).
        return _base_url(base), space_id, _setup_hint(
            provider=_STORYBLOK.provider,
            field_names=("management_token", "access_token", "accessToken", "token", "value"),
            tool_name=tool_name,
            env_var="STORYBLOK_MANAGEMENT_TOKEN",
            display_name=_STORYBLOK.display_name,
        )
    return _base_url(base), space_id, {
        "Accept": "application/json",
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _webflow_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_WEBFLOW.provider,
            provider_aliases=_WEBFLOW.aliases,
            field_names=_WEBFLOW.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("webflow_base_url")
        or _WEBFLOW_BASE_URL
    )
    token = _credential_value(
        provider=_WEBFLOW.provider,
        provider_aliases=_WEBFLOW.aliases,
        field_names=_WEBFLOW.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("webflow_access_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_WEBFLOW.provider,
            field_names=_WEBFLOW.hint_fields,
            tool_name=tool_name,
            env_var=_WEBFLOW.env_var,
            display_name=_WEBFLOW.display_name,
        )
    base_url = _base_url(base)
    if not base_url.endswith("/v2"):
        base_url = f"{base_url}/v2"
    return base_url, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Nymeria",
    }


def _webflow_request(
    tool_name: str,
    path: str,
    config: Optional[RunnableConfig],
    *,
    method: str = "GET",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> str:
    base_url, headers_or_error = _webflow_config(tool_name, config)
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


@tool
def wordpress_list_records(
    resource: str,
    per_page: int = 20,
    page: int = 1,
    search: str = "",
    status: str = "",
    context: str = "view",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List WordPress posts, pages, or users.

    Args:
        resource: One of posts, pages, or users.
        per_page: Number of records to return, 1-100.
        page: Page number.
        search: Optional search term.
        status: Optional post/page status filter.
        context: WordPress REST context, usually view or edit.
    """
    try:
        path = _normal_resource(resource, _WORDPRESS_RESOURCES)
        base_url, headers_or_error = _wordpress_config("wordpress_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{path}",
            params={
                "per_page": _limit(per_page, default=20, max_value=100),
                "page": max(1, int(page)),
                "search": search.strip(),
                "status": status.strip(),
                "context": context.strip() or "view",
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wordpress_list_records failed", exc_info=True)
        return f"[Error]: WordPress list failed: {e}"


@tool
def wordpress_get_record(
    resource: str,
    record_id: str,
    context: str = "view",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a WordPress post, page, or user by ID.

    Args:
        resource: One of posts, pages, or users.
        record_id: WordPress record ID.
        context: WordPress REST context, usually view or edit.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path = _normal_resource(resource, _WORDPRESS_RESOURCES)
        base_url, headers_or_error = _wordpress_config("wordpress_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{path}/{quote(record_id.strip(), safe='')}",
            params={"context": context.strip() or "view"},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wordpress_get_record failed", exc_info=True)
        return f"[Error]: WordPress lookup failed: {e}"


@tool
def wordpress_create_record(
    resource: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a WordPress post, page, or user.

    Args:
        resource: One of posts, pages, or users.
        fields_json: WordPress REST fields as JSON.
    """
    if not fields_json.strip():
        return "[Error]: fields_json is required."
    try:
        path = _normal_resource(resource, _WORDPRESS_RESOURCES)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        base_url, headers_or_error = _wordpress_config("wordpress_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/{path}", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("wordpress_create_record failed", exc_info=True)
        return f"[Error]: WordPress create failed: {e}"


@tool
def wordpress_update_record(
    resource: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a WordPress post, page, or user.

    Args:
        resource: One of posts, pages, or users.
        record_id: WordPress record ID.
        fields_json: WordPress REST fields as JSON.
    """
    if not record_id.strip() or not fields_json.strip():
        return "[Error]: record_id and fields_json are required."
    try:
        path = _normal_resource(resource, _WORDPRESS_RESOURCES)
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        base_url, headers_or_error = _wordpress_config("wordpress_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/{path}/{quote(record_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wordpress_update_record failed", exc_info=True)
        return f"[Error]: WordPress update failed: {e}"


@tool
def wordpress_delete_record(
    resource: str,
    record_id: str,
    force: bool = False,
    reassign_user_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a WordPress post, page, or user.

    Args:
        resource: One of posts, pages, or users.
        record_id: WordPress record ID.
        force: Whether to bypass trash where WordPress supports it.
        reassign_user_id: Required by WordPress when deleting users with authored content.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path = _normal_resource(resource, _WORDPRESS_RESOURCES)
        base_url, headers_or_error = _wordpress_config("wordpress_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/{path}/{quote(record_id.strip(), safe='')}",
            params={"force": "true" if force else "false", "reassign": reassign_user_id.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("wordpress_delete_record failed", exc_info=True)
        return f"[Error]: WordPress delete failed: {e}"


@tool
def strapi_list_entries(
    collection: str,
    page: int = 1,
    page_size: int = 25,
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Strapi collection entries.

    Args:
        collection: Strapi collection API ID, such as articles.
        page: Page number.
        page_size: Entries per page.
        query_json: Optional additional query parameters as JSON.
    """
    if not collection.strip():
        return "[Error]: collection is required."
    try:
        api_root, _version, headers_or_error = _strapi_config("strapi_list_entries", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = _parse_json(query_json, expected=dict, label="query_json")
        params.setdefault("pagination[page]", max(1, int(page)))
        params.setdefault("pagination[pageSize]", _limit(page_size, default=25, max_value=100))
        data = _request_json(
            "GET",
            f"{api_root}/{quote(collection.strip(), safe='')}",
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("strapi_list_entries failed", exc_info=True)
        return f"[Error]: Strapi list failed: {e}"


@tool
def strapi_get_entry(
    collection: str,
    entry_id: str,
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Strapi entry by ID.

    Args:
        collection: Strapi collection API ID.
        entry_id: Entry ID.
        query_json: Optional additional query parameters as JSON.
    """
    if not collection.strip() or not entry_id.strip():
        return "[Error]: collection and entry_id are required."
    try:
        api_root, _version, headers_or_error = _strapi_config("strapi_get_entry", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{api_root}/{quote(collection.strip(), safe='')}/{quote(entry_id.strip(), safe='')}",
            params=_parse_json(query_json, expected=dict, label="query_json"),
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("strapi_get_entry failed", exc_info=True)
        return f"[Error]: Strapi lookup failed: {e}"


@tool
def strapi_create_entry(
    collection: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Strapi entry.

    Args:
        collection: Strapi collection API ID.
        fields_json: Entry fields as JSON.
    """
    if not collection.strip() or not fields_json.strip():
        return "[Error]: collection and fields_json are required."
    try:
        api_root, version, headers_or_error = _strapi_config("strapi_create_entry", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _strapi_payload(_parse_json(fields_json, expected=dict, label="fields_json"), version)
        data = _request_json(
            "POST",
            f"{api_root}/{quote(collection.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("strapi_create_entry failed", exc_info=True)
        return f"[Error]: Strapi create failed: {e}"


@tool
def strapi_update_entry(
    collection: str,
    entry_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Strapi entry.

    Args:
        collection: Strapi collection API ID.
        entry_id: Entry ID.
        fields_json: Entry fields as JSON.
    """
    if not collection.strip() or not entry_id.strip() or not fields_json.strip():
        return "[Error]: collection, entry_id, and fields_json are required."
    try:
        api_root, version, headers_or_error = _strapi_config("strapi_update_entry", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _strapi_payload(_parse_json(fields_json, expected=dict, label="fields_json"), version)
        data = _request_json(
            "PUT",
            f"{api_root}/{quote(collection.strip(), safe='')}/{quote(entry_id.strip(), safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("strapi_update_entry failed", exc_info=True)
        return f"[Error]: Strapi update failed: {e}"


@tool
def strapi_delete_entry(
    collection: str,
    entry_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Strapi entry.

    Args:
        collection: Strapi collection API ID.
        entry_id: Entry ID.
    """
    if not collection.strip() or not entry_id.strip():
        return "[Error]: collection and entry_id are required."
    try:
        api_root, _version, headers_or_error = _strapi_config("strapi_delete_entry", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{api_root}/{quote(collection.strip(), safe='')}/{quote(entry_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("strapi_delete_entry failed", exc_info=True)
        return f"[Error]: Strapi delete failed: {e}"


@tool
def contentful_list_records(
    resource: str,
    environment: str = "master",
    source: str = "delivery",
    limit: int = 25,
    skip: int = 0,
    content_type: str = "",
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Contentful delivery or preview records.

    Args:
        resource: One of entries, assets, content_types, or locales.
        environment: Contentful environment ID.
        source: delivery or preview.
        limit: Number of records to return.
        skip: Number of records to skip.
        content_type: Optional content type filter for entries.
        query_json: Optional extra Contentful query parameters as JSON.
    """
    try:
        path = _normal_resource(resource, _CONTENTFUL_RESOURCES)
        source = "preview" if source.strip().lower() == "preview" else "delivery"
        base_url, space_id, headers_or_error = _contentful_config("contentful_list_records", config, source)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = _parse_json(query_json, expected=dict, label="query_json")
        params.update(
            _filtered(
                {
                    "limit": _limit(limit, default=25, max_value=1000),
                    "skip": max(0, int(skip)),
                    "content_type": content_type.strip() if path == "entries" else "",
                }
            )
        )
        data = _request_json(
            "GET",
            f"{base_url}/spaces/{quote(space_id, safe='')}/environments/{quote(environment.strip() or 'master', safe='')}/{path}",
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("contentful_list_records failed", exc_info=True)
        return f"[Error]: Contentful list failed: {e}"


@tool
def contentful_get_record(
    resource: str,
    record_id: str,
    environment: str = "master",
    source: str = "delivery",
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Contentful delivery or preview record.

    Args:
        resource: One of entries, assets, content_types, or locales.
        record_id: Contentful record ID.
        environment: Contentful environment ID.
        source: delivery or preview.
        query_json: Optional extra Contentful query parameters as JSON.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        path = _normal_resource(resource, _CONTENTFUL_RESOURCES)
        source = "preview" if source.strip().lower() == "preview" else "delivery"
        base_url, space_id, headers_or_error = _contentful_config("contentful_get_record", config, source)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            (
                f"{base_url}/spaces/{quote(space_id, safe='')}/environments/"
                f"{quote(environment.strip() or 'master', safe='')}/{path}/{quote(record_id.strip(), safe='')}"
            ),
            params=_parse_json(query_json, expected=dict, label="query_json"),
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("contentful_get_record failed", exc_info=True)
        return f"[Error]: Contentful lookup failed: {e}"


@tool
def ghost_list_posts(
    source: str = "content",
    limit: int = 15,
    page: int = 1,
    filter_query: str = "",
    include: str = "",
    fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Ghost posts from the Content or Admin API.

    Args:
        source: content or admin.
        limit: Number of posts to return.
        page: Page number.
        filter_query: Optional Ghost filter string.
        include: Optional include selector, such as tags,authors.
        fields: Optional comma-separated field selector.
    """
    try:
        site = _ghost_site_url("ghost_list_posts", config)
        if not site:
            return "[Error]: No Ghost URL found. Save a Ghost credential with \"url\", or set GHOST_URL."
        params = _filtered(
            {
                "limit": _limit(limit, default=15, max_value=100),
                "page": max(1, int(page)),
                "filter": filter_query.strip(),
                "include": include.strip(),
                "fields": fields.strip(),
            }
        )
        if source.strip().lower() == "admin":
            headers_or_error = _ghost_admin_headers("ghost_list_posts", config)
            if isinstance(headers_or_error, str):
                return headers_or_error
            data = _request_json("GET", f"{site}/ghost/api/admin/posts/", params=params, headers=headers_or_error)
        else:
            key = _ghost_content_key("ghost_list_posts", config)
            if not key:
                # Content-path hint variant: field_names/env_var inline (see _GHOST).
                return _setup_hint(
                    provider=_GHOST.provider,
                    field_names=("content_api_key", "api_key", "key", "token", "value"),
                    tool_name="ghost_list_posts",
                    env_var="GHOST_CONTENT_API_KEY",
                    display_name=_GHOST.display_name,
                )
            params["key"] = key
            data = _request_json("GET", f"{site}/ghost/api/content/posts/", params=params)
        return _dump_json(data.get("posts", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("ghost_list_posts failed", exc_info=True)
        return f"[Error]: Ghost post list failed: {e}"


@tool
def ghost_get_post(
    identifier: str,
    identifier_type: str = "id",
    source: str = "content",
    include: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Ghost post by ID or slug.

    Args:
        identifier: Ghost post ID or slug.
        identifier_type: id or slug.
        source: content or admin.
        include: Optional include selector, such as tags,authors.
    """
    if not identifier.strip():
        return "[Error]: identifier is required."
    try:
        site = _ghost_site_url("ghost_get_post", config)
        if not site:
            return "[Error]: No Ghost URL found. Save a Ghost credential with \"url\", or set GHOST_URL."
        identifier_type = "slug" if identifier_type.strip().lower() == "slug" else "id"
        suffix = f"slug/{quote(identifier.strip(), safe='')}/" if identifier_type == "slug" else f"{quote(identifier.strip(), safe='')}/"
        params = _filtered({"include": include.strip()})
        if source.strip().lower() == "admin":
            headers_or_error = _ghost_admin_headers("ghost_get_post", config)
            if isinstance(headers_or_error, str):
                return headers_or_error
            data = _request_json("GET", f"{site}/ghost/api/admin/posts/{suffix}", params=params, headers=headers_or_error)
        else:
            key = _ghost_content_key("ghost_get_post", config)
            if not key:
                # Content-path hint variant: field_names/env_var inline (see _GHOST).
                return _setup_hint(
                    provider=_GHOST.provider,
                    field_names=("content_api_key", "api_key", "key", "token", "value"),
                    tool_name="ghost_get_post",
                    env_var="GHOST_CONTENT_API_KEY",
                    display_name=_GHOST.display_name,
                )
            params["key"] = key
            data = _request_json("GET", f"{site}/ghost/api/content/posts/{suffix}", params=params)
        if isinstance(data, dict) and isinstance(data.get("posts"), list) and data["posts"]:
            return _dump_json(data["posts"][0])
        return _dump_json(data)
    except Exception as e:
        logger.error("ghost_get_post failed", exc_info=True)
        return f"[Error]: Ghost post lookup failed: {e}"


@tool
def ghost_create_post(
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Ghost post with the Admin API.

    Args:
        fields_json: Ghost post fields as JSON.
    """
    if not fields_json.strip():
        return "[Error]: fields_json is required."
    try:
        site = _ghost_site_url("ghost_create_post", config)
        if not site:
            return "[Error]: No Ghost URL found. Save a Ghost credential with \"url\", or set GHOST_URL."
        headers_or_error = _ghost_admin_headers("ghost_create_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"posts": [_parse_json(fields_json, expected=dict, label="fields_json")]}
        data = _request_json("POST", f"{site}/ghost/api/admin/posts/", json_body=body, headers=headers_or_error)
        return _dump_json(data.get("posts", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("ghost_create_post failed", exc_info=True)
        return f"[Error]: Ghost post create failed: {e}"


@tool
def ghost_update_post(
    post_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Ghost post with the Admin API.

    Args:
        post_id: Ghost post ID.
        fields_json: Ghost post fields as JSON. Include updated_at when Ghost requires conflict protection.
    """
    if not post_id.strip() or not fields_json.strip():
        return "[Error]: post_id and fields_json are required."
    try:
        site = _ghost_site_url("ghost_update_post", config)
        if not site:
            return "[Error]: No Ghost URL found. Save a Ghost credential with \"url\", or set GHOST_URL."
        headers_or_error = _ghost_admin_headers("ghost_update_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"posts": [_parse_json(fields_json, expected=dict, label="fields_json")]}
        data = _request_json(
            "PUT",
            f"{site}/ghost/api/admin/posts/{quote(post_id.strip(), safe='')}/",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("posts", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("ghost_update_post failed", exc_info=True)
        return f"[Error]: Ghost post update failed: {e}"


@tool
def ghost_delete_post(
    post_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Ghost post with the Admin API.

    Args:
        post_id: Ghost post ID.
    """
    if not post_id.strip():
        return "[Error]: post_id is required."
    try:
        site = _ghost_site_url("ghost_delete_post", config)
        if not site:
            return "[Error]: No Ghost URL found. Save a Ghost credential with \"url\", or set GHOST_URL."
        headers_or_error = _ghost_admin_headers("ghost_delete_post", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{site}/ghost/api/admin/posts/{quote(post_id.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("ghost_delete_post failed", exc_info=True)
        return f"[Error]: Ghost post delete failed: {e}"


@tool
def storyblok_list_stories(
    source: str = "content",
    limit: int = 25,
    page: int = 1,
    starts_with: str = "",
    version: str = "published",
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Storyblok stories from the Content or Management API.

    Args:
        source: content or management.
        limit: Number of stories to return.
        page: Page number.
        starts_with: Optional folder/path prefix filter.
        version: Content API version, usually published or draft.
        query_json: Optional extra query parameters as JSON.
    """
    try:
        params = _parse_json(query_json, expected=dict, label="query_json")
        params.update(
            _filtered(
                {
                    "per_page": _limit(limit, default=25, max_value=100),
                    "page": max(1, int(page)),
                    "starts_with": starts_with.strip(),
                    "version": version.strip() if source.strip().lower() != "management" else "",
                }
            )
        )
        if source.strip().lower() == "management":
            base_url, space_id, headers_or_error = _storyblok_management_config("storyblok_list_stories", config)
            if isinstance(headers_or_error, str):
                return headers_or_error
            data = _request_json(
                "GET",
                f"{base_url}/spaces/{quote(space_id, safe='')}/stories",
                params=params,
                headers=headers_or_error,
            )
        else:
            base_url, token_or_error = _storyblok_content_config("storyblok_list_stories", config)
            if isinstance(token_or_error, str):
                return token_or_error
            params["token"] = token_or_error["token"]
            data = _request_json("GET", f"{base_url}/stories", params=params)
        return _dump_json(data.get("stories", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("storyblok_list_stories failed", exc_info=True)
        return f"[Error]: Storyblok story list failed: {e}"


@tool
def storyblok_get_story(
    identifier: str,
    source: str = "content",
    version: str = "published",
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Storyblok story by slug/path or management story ID.

    Args:
        identifier: Content story slug/path, or management story ID.
        source: content or management.
        version: Content API version, usually published or draft.
        query_json: Optional extra query parameters as JSON.
    """
    if not identifier.strip():
        return "[Error]: identifier is required."
    try:
        params = _parse_json(query_json, expected=dict, label="query_json")
        if source.strip().lower() == "management":
            base_url, space_id, headers_or_error = _storyblok_management_config("storyblok_get_story", config)
            if isinstance(headers_or_error, str):
                return headers_or_error
            data = _request_json(
                "GET",
                f"{base_url}/spaces/{quote(space_id, safe='')}/stories/{quote(identifier.strip(), safe='')}",
                params=params,
                headers=headers_or_error,
            )
        else:
            base_url, token_or_error = _storyblok_content_config("storyblok_get_story", config)
            if isinstance(token_or_error, str):
                return token_or_error
            params.update({"token": token_or_error["token"], "version": version.strip() or "published"})
            data = _request_json(
                "GET",
                f"{base_url}/stories/{quote(identifier.strip(), safe='/')}",
                params=params,
            )
        return _dump_json(data.get("story", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("storyblok_get_story failed", exc_info=True)
        return f"[Error]: Storyblok story lookup failed: {e}"


@tool
def storyblok_publish_story(
    story_id: str,
    release_id: str = "",
    language: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Publish a Storyblok story through the Management API.

    Args:
        story_id: Storyblok management story ID.
        release_id: Optional release ID.
        language: Optional language code.
    """
    if not story_id.strip():
        return "[Error]: story_id is required."
    try:
        base_url, space_id, headers_or_error = _storyblok_management_config("storyblok_publish_story", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/spaces/{quote(space_id, safe='')}/stories/{quote(story_id.strip(), safe='')}/publish",
            params={"release_id": release_id.strip(), "lang": language.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("story", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("storyblok_publish_story failed", exc_info=True)
        return f"[Error]: Storyblok story publish failed: {e}"


@tool
def storyblok_unpublish_story(
    story_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Unpublish a Storyblok story through the Management API.

    Args:
        story_id: Storyblok management story ID.
    """
    if not story_id.strip():
        return "[Error]: story_id is required."
    try:
        base_url, space_id, headers_or_error = _storyblok_management_config("storyblok_unpublish_story", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/spaces/{quote(space_id, safe='')}/stories/{quote(story_id.strip(), safe='')}/unpublish",
            headers=headers_or_error,
        )
        return _dump_json(data.get("story", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("storyblok_unpublish_story failed", exc_info=True)
        return f"[Error]: Storyblok story unpublish failed: {e}"


@tool
def storyblok_delete_story(
    story_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Storyblok story through the Management API.

    Args:
        story_id: Storyblok management story ID.
    """
    if not story_id.strip():
        return "[Error]: story_id is required."
    try:
        base_url, space_id, headers_or_error = _storyblok_management_config("storyblok_delete_story", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/spaces/{quote(space_id, safe='')}/stories/{quote(story_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("story", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("storyblok_delete_story failed", exc_info=True)
        return f"[Error]: Storyblok story delete failed: {e}"


@tool
def webflow_list_sites(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """List Webflow sites available to the saved connection.

    Args:
        config: Runtime context injected by Nymeria.
    """
    try:
        data = _webflow_request("webflow_list_sites", "sites", config)
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        if isinstance(parsed, dict) and isinstance(parsed.get("sites"), list):
            return _dump_json(parsed["sites"])
        return data
    except Exception as e:
        logger.error("webflow_list_sites failed", exc_info=True)
        return f"[Error]: Webflow site listing failed: {e}"


@tool
def webflow_list_site_collections(
    site_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Webflow CMS collections for a site.

    Args:
        site_id: Webflow site ID.
    """
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        data = _webflow_request("webflow_list_site_collections", f"sites/{quote(site_id.strip(), safe='')}/collections", config)
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        if isinstance(parsed, dict) and isinstance(parsed.get("collections"), list):
            return _dump_json(parsed["collections"])
        return data
    except Exception as e:
        logger.error("webflow_list_site_collections failed", exc_info=True)
        return f"[Error]: Webflow collection listing failed: {e}"


@tool
def webflow_get_collection(
    collection_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Webflow CMS collection metadata and fields.

    Args:
        collection_id: Webflow collection ID.
    """
    if not collection_id.strip():
        return "[Error]: collection_id is required."
    try:
        return _webflow_request("webflow_get_collection", f"collections/{quote(collection_id.strip(), safe='')}", config)
    except Exception as e:
        logger.error("webflow_get_collection failed", exc_info=True)
        return f"[Error]: Webflow collection lookup failed: {e}"


@tool
def webflow_list_collection_items(
    collection_id: str,
    limit: int = 100,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List items in a Webflow CMS collection.

    Args:
        collection_id: Webflow collection ID.
        limit: Maximum items to return.
        offset: Zero-based pagination offset.
    """
    if not collection_id.strip():
        return "[Error]: collection_id is required."
    try:
        data = _webflow_request(
            "webflow_list_collection_items",
            f"collections/{quote(collection_id.strip(), safe='')}/items",
            config,
            params={"limit": _limit(limit, default=100, max_value=100), "offset": max(0, int(offset))},
        )
        if data.startswith("[Error]:"):
            return data
        parsed = json.loads(data)
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            return _dump_json(parsed["items"])
        return data
    except Exception as e:
        logger.error("webflow_list_collection_items failed", exc_info=True)
        return f"[Error]: Webflow collection item listing failed: {e}"


@tool
def webflow_get_collection_item(
    collection_id: str,
    item_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Webflow CMS collection item.

    Args:
        collection_id: Webflow collection ID.
        item_id: Webflow item ID.
    """
    if not collection_id.strip() or not item_id.strip():
        return "[Error]: collection_id and item_id are required."
    try:
        return _webflow_request(
            "webflow_get_collection_item",
            f"collections/{quote(collection_id.strip(), safe='')}/items/{quote(item_id.strip(), safe='')}",
            config,
        )
    except Exception as e:
        logger.error("webflow_get_collection_item failed", exc_info=True)
        return f"[Error]: Webflow collection item lookup failed: {e}"


@tool
def webflow_create_collection_item(
    collection_id: str,
    field_data_json: str,
    live: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Webflow CMS collection item.

    Args:
        collection_id: Webflow collection ID.
        field_data_json: JSON object keyed by Webflow field slug.
        live: Publish directly to the live site when supported.
    """
    if not collection_id.strip():
        return "[Error]: collection_id is required."
    try:
        field_data = _parse_json(field_data_json, expected=dict, label="field_data_json")
        if not field_data:
            return "[Error]: field_data_json must include at least one field."
        suffix = "/live" if live else ""
        return _webflow_request(
            "webflow_create_collection_item",
            f"collections/{quote(collection_id.strip(), safe='')}/items{suffix}",
            config,
            method="POST",
            json_body={"fieldData": field_data},
        )
    except Exception as e:
        logger.error("webflow_create_collection_item failed", exc_info=True)
        return f"[Error]: Webflow collection item creation failed: {e}"


@tool
def webflow_update_collection_item(
    collection_id: str,
    item_id: str,
    field_data_json: str,
    live: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Webflow CMS collection item.

    Args:
        collection_id: Webflow collection ID.
        item_id: Webflow item ID.
        field_data_json: JSON object keyed by Webflow field slug.
        live: Publish the update directly to the live site when supported.
    """
    if not collection_id.strip() or not item_id.strip():
        return "[Error]: collection_id and item_id are required."
    try:
        field_data = _parse_json(field_data_json, expected=dict, label="field_data_json")
        if not field_data:
            return "[Error]: field_data_json must include at least one field."
        suffix = "/live" if live else ""
        return _webflow_request(
            "webflow_update_collection_item",
            f"collections/{quote(collection_id.strip(), safe='')}/items/{quote(item_id.strip(), safe='')}{suffix}",
            config,
            method="PATCH",
            json_body={"fieldData": field_data},
        )
    except Exception as e:
        logger.error("webflow_update_collection_item failed", exc_info=True)
        return f"[Error]: Webflow collection item update failed: {e}"


@tool
def webflow_delete_collection_item(
    collection_id: str,
    item_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Webflow CMS collection item.

    Args:
        collection_id: Webflow collection ID.
        item_id: Webflow item ID.
    """
    if not collection_id.strip() or not item_id.strip():
        return "[Error]: collection_id and item_id are required."
    try:
        return _webflow_request(
            "webflow_delete_collection_item",
            f"collections/{quote(collection_id.strip(), safe='')}/items/{quote(item_id.strip(), safe='')}",
            config,
            method="DELETE",
        )
    except Exception as e:
        logger.error("webflow_delete_collection_item failed", exc_info=True)
        return f"[Error]: Webflow collection item deletion failed: {e}"


CONTENT_MANAGEMENT_SERVICE_TOOLS = [
    wordpress_list_records,
    wordpress_get_record,
    wordpress_create_record,
    wordpress_update_record,
    wordpress_delete_record,
    strapi_list_entries,
    strapi_get_entry,
    strapi_create_entry,
    strapi_update_entry,
    strapi_delete_entry,
    contentful_list_records,
    contentful_get_record,
    ghost_list_posts,
    ghost_get_post,
    ghost_create_post,
    ghost_update_post,
    ghost_delete_post,
    storyblok_list_stories,
    storyblok_get_story,
    storyblok_publish_story,
    storyblok_unpublish_story,
    storyblok_delete_story,
    webflow_list_sites,
    webflow_list_site_collections,
    webflow_get_collection,
    webflow_list_collection_items,
    webflow_get_collection_item,
    webflow_create_collection_item,
    webflow_update_collection_item,
    webflow_delete_collection_item,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="content_management", tools=tuple(CONTENT_MANAGEMENT_SERVICE_TOOLS)))
