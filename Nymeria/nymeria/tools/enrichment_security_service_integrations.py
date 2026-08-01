"""Data enrichment, URL intelligence, and web extraction service tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import logging
from datetime import datetime, timezone
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
    filtered as _filtered,
    parse_json as _parse_json,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_URLSCAN_BASE_URL = "https://urlscan.io/api/v1"
_HUNTER_BASE_URL = "https://api.hunter.io/v2"
_MAILCHECK_BASE_URL = "https://api.mailcheck.co/v1"
_PEEKALINK_BASE_URL = "https://api.peekalink.io"
_JINA_READER_BASE_URL = "https://r.jina.ai"
_JINA_SEARCH_BASE_URL = "https://s.jina.ai"
_JINA_DEEPSEARCH_BASE_URL = "https://deepsearch.jina.ai/v1"
_SECURITYSCORECARD_BASE_URL = "https://api.securityscorecard.io"
_API_SUFFIX = "/api"
_OKTA_DEFAULT_DOMAIN_SUFFIX = ".okta.com"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered. The shared
# _service_base / _api_key_config / _jina_base helpers keep their generic (and
# for jina, dynamic) field-name tuples inline, so the spec still declares those
# tuples as groups even where no call site reads them back. Providers with two
# setup-hint variants (base URL vs token) carry the token variant in the spec
# and keep the base-URL branch's env_var inline.
_URLSCAN = register_provider_spec(
    ProviderCredentialSpec(
        provider="urlscan",
        aliases=("urlscan_io", "urlscanio", "urlscan_io_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="URLSCAN_API_KEY",
        display_name="urlscan.io",
    )
)

_HUNTER = register_provider_spec(
    ProviderCredentialSpec(
        provider="hunter",
        aliases=("hunter_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="HUNTER_API_KEY",
        display_name="Hunter",
    )
)

_MAILCHECK = register_provider_spec(
    ProviderCredentialSpec(
        provider="mailcheck",
        aliases=("mailcheck_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="MAILCHECK_API_KEY",
        display_name="Mailcheck",
    )
)

_PEEKALINK = register_provider_spec(
    ProviderCredentialSpec(
        provider="peekalink",
        aliases=("peekalink_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="PEEKALINK_API_KEY",
        display_name="Peekalink",
    )
)

_SECURITYSCORECARD = register_provider_spec(
    ProviderCredentialSpec(
        provider="securityscorecard",
        aliases=("security_scorecard", "securityscorecard_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="SECURITYSCORECARD_API_KEY",
        display_name="SecurityScorecard",
    )
)

_JINA = register_provider_spec(
    ProviderCredentialSpec(
        provider="jina",
        aliases=("jina_ai", "jinaai", "jina_ai_api"),
        groups=(
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
            ),
            # Three per-service base URL variants built inline in _jina_base
            # (first element varies by settings_name); declared here so the
            # registry mirrors every lookup tuple.
            CredentialFieldGroup(
                role="reader_base_url",
                names=("jina_reader_base_url", "base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="search_base_url",
                names=("jina_search_base_url", "base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="deepsearch_base_url",
                names=("jina_deepsearch_base_url", "base_url", "url", "api_url", "apiUrl"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="JINA_API_KEY",
        display_name="Jina AI",
    )
)

_MISP = register_provider_spec(
    ProviderCredentialSpec(
        provider="misp",
        aliases=("misp_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "apiKey", "auth_key", "authKey", "token", "value")
            ),
        ),
        # Two hint variants (api key vs base URL); spec carries the api-key one,
        # the base-URL branch keeps its "MISP_BASE_URL" env_var inline.
        hint_fields=("api_key", "auth_key", "token", "value"),
        env_var="MISP_API_KEY",
        display_name="MISP",
    )
)

_THEHIVE = register_provider_spec(
    ProviderCredentialSpec(
        provider="thehive",
        aliases=("the_hive", "thehive_api", "thehive_project"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="api_key",
                names=("api_key", "ApiKey", "apiKey", "access_token", "token", "value"),
            ),
            CredentialFieldGroup(
                role="api_version", names=("api_version", "apiVersion"), required=False
            ),
        ),
        # Two hint variants (api key vs base URL); spec carries the api-key one,
        # the base-URL branch keeps its "THEHIVE_BASE_URL" env_var inline.
        hint_fields=("api_key", "ApiKey", "access_token", "token", "value"),
        env_var="THEHIVE_API_KEY",
        display_name="TheHive",
    )
)

_OKTA = register_provider_spec(
    ProviderCredentialSpec(
        provider="okta",
        aliases=("okta_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "org_url", "orgUrl", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="domain",
                names=("domain", "subdomain", "org_domain", "orgDomain"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token",
                names=(
                    "access_token",
                    "accessToken",
                    "api_token",
                    "apiToken",
                    "ssws_token",
                    "token",
                    "value",
                ),
            ),
        ),
        hint_fields=("access_token", "accessToken", "api_token", "ssws_token", "token", "value"),
        env_var="OKTA_ACCESS_TOKEN",
        display_name="Okta",
    )
)

_ELASTIC_SECURITY = register_provider_spec(
    ProviderCredentialSpec(
        provider="elastic_security",
        aliases=("elasticsecurity", "elastic_security_api", "kibana"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(role="username", names=("username", "user")),
            CredentialFieldGroup(role="password", names=("password",)),
        ),
        # Two hint variants (api key vs base URL); spec carries the api-key one,
        # the base-URL branch keeps its "ELASTIC_SECURITY_BASE_URL" env_var inline.
        hint_fields=("api_key", "username", "password"),
        env_var="ELASTIC_SECURITY_API_KEY or ELASTIC_SECURITY_USERNAME + ELASTIC_SECURITY_PASSWORD",
        display_name="Elastic Security",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 500) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    auth: Any = None,
    verify: bool = True,
) -> Any:
    import httpx

    try:
        with _http_client(timeout=_HTTP_TIMEOUT, verify=verify) as client:
            response = _request_with_policy(
                client,
                method,
                url,
                params=_filtered(params) if params is not None else None,
                json=json_body,
                headers=headers,
                auth=auth,
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
                detail = (
                    body.get("message")
                    or body.get("error")
                    or body.get("error_description")
                    or body.get("description")
                    or body.get("detail")
                    or ""
                )
                errors = body.get("errors") or body.get("fieldErrors")
                if not detail and isinstance(errors, list):
                    detail = "; ".join(str(item) for item in errors[:3])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _truthy(value: bool | str | int) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rooted_api_base(base: str, suffix: str = _API_SUFFIX) -> str:
    base = _base_url(base)
    suffix = suffix.rstrip("/")
    if base.rstrip("/").endswith(suffix):
        return base
    return f"{base}{suffix}"


def _to_epoch_ms(value: str) -> int:
    if not value.strip():
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    text = value.strip()
    if text.isdigit():
        number = int(text)
        return number if number > 10_000_000_000 else number * 1000
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _service_base(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    settings_base_name: str,
    tool_name: str,
    display_name: str,
    env_var: str,
    config: Optional[RunnableConfig],
    default_base: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a self-hosted provider's base URL, reporting where it came from.

    Returns ``(base_or_error, base_from_vault)``. The second element is the
    provenance every caller needs for ``_require_joined_destination``: these
    helpers resolve the address here and the secret in the CALLER, so a
    per-function view of either one alone cannot see the pairing. It is None
    whenever the address came from settings or a default.
    """
    base_from_vault = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value(settings_base_name) or default_base
    if base:
        return _base_url(base), base_from_vault
    return _setup_hint(
        provider=provider,
        field_names=("base_url", "url"),
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    ), base_from_vault


def _api_key_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    env_var: str,
    settings_key_name: str,
    settings_base_name: str,
    default_base: str,
    tool_name: str,
    display_name: str,
    config: Optional[RunnableConfig],
    required: bool = True,
) -> tuple[str, str | None]:
    base_from_vault = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=("base_url", "url", "api_url", "apiUrl"),
        tool_name=tool_name,
        config=config,
    )
    base = base_from_vault or _settings_value(settings_base_name) or default_base
    api_key_from_vault = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=("api_key", "apiKey", "access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value(settings_key_name)
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=api_key_from_vault,
        secret=api_key,
        provider=provider,
    )
    if required and not api_key:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=("api_key", "access_token", "token", "value"),
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return _base_url(base), api_key


def _bearer_headers(api_key: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _misp_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str, bool]:
    base_or_error, base_from_vault = _service_base(
        provider=_MISP.provider,
        provider_aliases=_MISP.aliases,
        settings_base_name="misp_base_url",
        tool_name=tool_name,
        display_name=_MISP.display_name,
        env_var="MISP_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: MISP base URL is required.", True
    api_key_from_vault = _credential_value(
        provider=_MISP.provider,
        provider_aliases=_MISP.aliases,
        field_names=_MISP.group("api_key"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value("misp_api_key")
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=api_key_from_vault,
        secret=api_key,
        provider=_MISP.provider,
    )
    verify = True
    if not api_key:
        return "", _setup_hint(
            provider=_MISP.provider,
            field_names=_MISP.hint_fields,
            tool_name=tool_name,
            env_var=_MISP.env_var,
            display_name=_MISP.display_name,
        ), verify
    return base_or_error, {
        "Accept": "application/json",
        "Authorization": str(api_key),
        "Content-Type": "application/json",
    }, verify


def _thehive_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str, bool, str]:
    base_or_error, base_from_vault = _service_base(
        provider=_THEHIVE.provider,
        provider_aliases=_THEHIVE.aliases,
        settings_base_name="thehive_base_url",
        tool_name=tool_name,
        display_name=_THEHIVE.display_name,
        env_var="THEHIVE_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: TheHive base URL is required.", True, "v1"
    api_key_from_vault = _credential_value(
        provider=_THEHIVE.provider,
        provider_aliases=_THEHIVE.aliases,
        field_names=_THEHIVE.group("api_key"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value("thehive_api_key")
    _require_joined_destination(
        destination_from_vault=base_from_vault,
        secret_from_vault=api_key_from_vault,
        secret=api_key,
        provider=_THEHIVE.provider,
    )
    api_version = (
        _credential_value(
            provider=_THEHIVE.provider,
            provider_aliases=_THEHIVE.aliases,
            field_names=_THEHIVE.group("api_version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("thehive_api_version")
        or "v1"
    )
    verify = True
    if not api_key:
        return "", _setup_hint(
            provider=_THEHIVE.provider,
            field_names=_THEHIVE.hint_fields,
            tool_name=tool_name,
            env_var=_THEHIVE.env_var,
            display_name=_THEHIVE.display_name,
        ), verify, str(api_version)
    return _rooted_api_base(base_or_error), {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, verify, str(api_version)


def _securityscorecard_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider=_SECURITYSCORECARD.provider,
        provider_aliases=_SECURITYSCORECARD.aliases,
        env_var=_SECURITYSCORECARD.env_var,
        settings_key_name="securityscorecard_api_key",
        settings_base_name="securityscorecard_base_url",
        default_base=_SECURITYSCORECARD_BASE_URL,
        tool_name=tool_name,
        display_name=_SECURITYSCORECARD.display_name,
        config=config,
    )
    if api_key and api_key.startswith("[Error]:"):
        return base_url, api_key
    return base_url, {
        "Accept": "application/json",
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }


def _okta_root(value: str) -> str:
    root = value.strip().rstrip("/")
    if not root:
        raise ValueError("Okta base URL is required")
    if "://" not in root:
        root = f"https://{root}"
    return _base_url(root)


def _okta_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_OKTA.provider,
            provider_aliases=_OKTA.aliases,
            field_names=_OKTA.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("okta_base_url")
    )
    domain = (
        _credential_value(
            provider=_OKTA.provider,
            provider_aliases=_OKTA.aliases,
            field_names=_OKTA.group("domain"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("okta_domain")
    )
    if not base and domain:
        domain_text = domain.strip().replace("https://", "").replace("http://", "").rstrip("/")
        if "." not in domain_text:
            domain_text = f"{domain_text}{_OKTA_DEFAULT_DOMAIN_SUFFIX}"
        base = f"https://{domain_text}"
    if not base:
        return "", (
            '[Error]: No Okta base URL found. Save an Okta credential with "base_url" or '
            '"domain", or set OKTA_BASE_URL or OKTA_DOMAIN.'
        )
    token = _credential_value(
        provider=_OKTA.provider,
        provider_aliases=_OKTA.aliases,
        field_names=_OKTA.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("okta_access_token")
    if not token:
        return _okta_root(base), _setup_hint(
            provider=_OKTA.provider,
            field_names=_OKTA.hint_fields,
            tool_name=tool_name,
            env_var=_OKTA.env_var,
            display_name=_OKTA.display_name,
        )
    return _okta_root(base), {
        "Accept": "application/json",
        "Authorization": f"SSWS {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _okta_profile(
    *,
    first_name: str = "",
    last_name: str = "",
    login: str = "",
    email: str = "",
    profile_json: str = "",
) -> dict[str, Any]:
    profile = _parse_json(profile_json, expected=dict, label="profile_json")
    profile.update(
        _filtered(
            {
                "firstName": first_name.strip(),
                "lastName": last_name.strip(),
                "login": login.strip(),
                "email": email.strip(),
            }
        )
    )
    return profile


def _okta_simplify_user(user: Any) -> Any:
    if not isinstance(user, dict):
        return user
    profile = user.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    return {
        "id": user.get("id"),
        "status": user.get("status"),
        "created": user.get("created"),
        "activated": user.get("activated"),
        "lastLogin": user.get("lastLogin"),
        "lastUpdated": user.get("lastUpdated"),
        "passwordChanged": user.get("passwordChanged"),
        "profile": {
            "firstName": profile.get("firstName"),
            "lastName": profile.get("lastName"),
            "login": profile.get("login"),
            "email": profile.get("email"),
        },
    }


def _elastic_security_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str, Any]:
    base_or_error, base_from_vault = _service_base(
        provider=_ELASTIC_SECURITY.provider,
        provider_aliases=_ELASTIC_SECURITY.aliases,
        settings_base_name="elastic_security_base_url",
        tool_name=tool_name,
        display_name=_ELASTIC_SECURITY.display_name,
        env_var="ELASTIC_SECURITY_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: Elastic Security base URL is required.", None
    api_key_from_vault = _credential_value(
        provider=_ELASTIC_SECURITY.provider,
        provider_aliases=_ELASTIC_SECURITY.aliases,
        field_names=_ELASTIC_SECURITY.group("api_key"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value("elastic_security_api_key")
    username_from_vault = _credential_value(
        provider=_ELASTIC_SECURITY.provider,
        provider_aliases=_ELASTIC_SECURITY.aliases,
        field_names=_ELASTIC_SECURITY.group("username"),
        tool_name=tool_name,
        config=config,
    )
    username = username_from_vault or _settings_value("elastic_security_username")
    password_from_vault = _credential_value(
        provider=_ELASTIC_SECURITY.provider,
        provider_aliases=_ELASTIC_SECURITY.aliases,
        field_names=_ELASTIC_SECURITY.group("password"),
        tool_name=tool_name,
        config=config,
    )
    password = password_from_vault or _settings_value("elastic_security_password")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "kbn-xsrf": "true"}
    auth = None
    # The guard runs per BRANCH, on the credential that actually authenticates
    # the request. Asking instead whether the record supplied ANY of the
    # alternatives would reproduce slice B's own weakness one level down: a
    # record holding base_url + password clears "some anchor", and the api_key
    # branch then sends the operator's key to that record's address.
    if api_key:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=api_key_from_vault,
            secret=api_key,
            provider=_ELASTIC_SECURITY.provider,
        )
        headers["Authorization"] = f"ApiKey {api_key}"
    elif username and password:
        _require_joined_destination(
            destination_from_vault=base_from_vault,
            secret_from_vault=password_from_vault,
            secret=password,
            provider=_ELASTIC_SECURITY.provider,
        )
        auth = (username, password)
    else:
        return "", _setup_hint(
            provider=_ELASTIC_SECURITY.provider,
            field_names=_ELASTIC_SECURITY.hint_fields,
            tool_name=tool_name,
            env_var=_ELASTIC_SECURITY.env_var,
            display_name=_ELASTIC_SECURITY.display_name,
        ), None
    return _rooted_api_base(base_or_error), headers, auth


def _urlscan_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider=_URLSCAN.provider,
        provider_aliases=_URLSCAN.aliases,
        env_var=_URLSCAN.env_var,
        settings_key_name="urlscan_api_key",
        settings_base_name="urlscan_base_url",
        default_base=_URLSCAN_BASE_URL,
        tool_name=tool_name,
        display_name=_URLSCAN.display_name,
        config=config,
    )
    if not api_key:
        return base_url, api_key or ""
    if api_key.startswith("[Error]:"):
        return base_url, api_key
    return base_url, {
        "Accept": "application/json",
        "API-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _hunter_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | None]:
    return _api_key_config(
        provider=_HUNTER.provider,
        provider_aliases=_HUNTER.aliases,
        env_var=_HUNTER.env_var,
        settings_key_name="hunter_api_key",
        settings_base_name="hunter_base_url",
        default_base=_HUNTER_BASE_URL,
        tool_name=tool_name,
        display_name=_HUNTER.display_name,
        config=config,
    )


def _mailcheck_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider=_MAILCHECK.provider,
        provider_aliases=_MAILCHECK.aliases,
        env_var=_MAILCHECK.env_var,
        settings_key_name="mailcheck_api_key",
        settings_base_name="mailcheck_base_url",
        default_base=_MAILCHECK_BASE_URL,
        tool_name=tool_name,
        display_name=_MAILCHECK.display_name,
        config=config,
    )
    if not api_key or api_key.startswith("[Error]:"):
        return base_url, api_key or ""
    return base_url, _bearer_headers(api_key)


def _peekalink_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider=_PEEKALINK.provider,
        provider_aliases=_PEEKALINK.aliases,
        env_var=_PEEKALINK.env_var,
        settings_key_name="peekalink_api_key",
        settings_base_name="peekalink_base_url",
        default_base=_PEEKALINK_BASE_URL,
        tool_name=tool_name,
        display_name=_PEEKALINK.display_name,
        config=config,
    )
    if not api_key or api_key.startswith("[Error]:"):
        return base_url, api_key or ""
    return base_url, _bearer_headers(api_key)


def _jina_key(tool_name: str, config: Optional[RunnableConfig]) -> str | None:
    return _credential_value(
        provider=_JINA.provider,
        provider_aliases=_JINA.aliases,
        field_names=_JINA.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jina_api_key")


def _jina_base(tool_name: str, config: Optional[RunnableConfig], settings_name: str, default: str) -> str:
    # The per-service base URL tuple is built inline because its first element
    # (settings_name) varies per caller; the reader/search/deepsearch variants
    # are declared as groups on _JINA.
    base = (
        _credential_value(
            provider=_JINA.provider,
            provider_aliases=_JINA.aliases,
            field_names=(settings_name, "base_url", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_name)
        or default
    )
    return _base_url(base)


def _jina_headers(api_key: str | None, *, output_format: str = "") -> dict[str, str]:
    headers = _bearer_headers(api_key)
    if output_format.strip():
        headers["X-Return-Format"] = output_format.strip()
    return headers


@tool
def urlscan_search_scans(
    query: str,
    limit: int = 25,
    search_after: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search archived urlscan.io scans.

    Args:
        query: urlscan search query, for example domain:example.com or page.url:"example".
        limit: Number of results to return, 1-100.
        search_after: Optional pagination token from a prior result sort field.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers_or_error = _urlscan_config("urlscan_search_scans", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/search/",
            params={"q": query.strip(), "size": _limit(limit, max_value=100), "search_after": search_after.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("urlscan_search_scans failed", exc_info=True)
        return f"[Error]: urlscan.io search failed: {e}"


@tool
def urlscan_get_result(
    scan_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a urlscan.io scan result by UUID."""
    if not scan_id.strip():
        return "[Error]: scan_id is required."
    try:
        base_url, headers_or_error = _urlscan_config("urlscan_get_result", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/result/{quote(scan_id.strip(), safe='')}/", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("urlscan_get_result failed", exc_info=True)
        return f"[Error]: urlscan.io result lookup failed: {e}"


@tool
def urlscan_submit_scan(
    url: str,
    visibility: str = "unlisted",
    tags: str = "",
    custom_agent: str = "",
    referer: str = "",
    override_safety: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Submit a URL to urlscan.io for scanning.

    Args:
        url: URL to scan.
        visibility: public, unlisted, or private.
        tags: Optional comma-separated tags, at most 10.
        custom_agent: Optional browser user agent string.
        referer: Optional referer URL.
        override_safety: Optional urlscan safety override value.
    """
    if not url.strip():
        return "[Error]: url is required."
    try:
        tag_list = _csv_to_list(tags)
        if len(tag_list) > 10:
            return "[Error]: urlscan.io accepts at most 10 tags."
        body: dict[str, Any] = {"url": url.strip(), "visibility": visibility.strip() or "unlisted"}
        if tag_list:
            body["tags"] = tag_list
        if custom_agent.strip():
            body["customAgent"] = custom_agent.strip()
        if referer.strip():
            body["referer"] = referer.strip()
        if override_safety.strip():
            body["overrideSafety"] = override_safety.strip()
        base_url, headers_or_error = _urlscan_config("urlscan_submit_scan", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/scan/", json_body=body, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("urlscan_submit_scan failed", exc_info=True)
        return f"[Error]: urlscan.io scan submission failed: {e}"


@tool
def hunter_domain_search(
    domain: str,
    limit: int = 25,
    email_type: str = "",
    seniority: str = "",
    department: str = "",
    raw: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Find email addresses associated with a domain through Hunter.

    Args:
        domain: Domain to search, for example example.com.
        limit: Number of email results to return, 1-100.
        email_type: Optional personal or generic filter.
        seniority: Optional comma-separated seniority filters.
        department: Optional comma-separated department filters.
        raw: Return Hunter's full response instead of only the email list.
    """
    if not domain.strip():
        return "[Error]: domain is required."
    try:
        base_url, key_or_error = _hunter_config("hunter_domain_search", config)
        if key_or_error is None or key_or_error.startswith("[Error]:"):
            return key_or_error or ""
        params = {
            "domain": domain.strip(),
            "limit": _limit(limit, max_value=100),
            "api_key": key_or_error,
            "type": email_type.strip(),
            "seniority": ",".join(_csv_to_list(seniority)),
            "department": ",".join(_csv_to_list(department)),
        }
        data = _request_json("GET", f"{base_url}/domain-search", params=params)
        if raw:
            return _dump_json(data)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return _dump_json(data["data"].get("emails", data["data"]))
        return _dump_json(data)
    except Exception as e:
        logger.error("hunter_domain_search failed", exc_info=True)
        return f"[Error]: Hunter domain search failed: {e}"


@tool
def hunter_email_finder(
    domain: str,
    first_name: str,
    last_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Find a likely professional email address from name and domain."""
    if not domain.strip() or not first_name.strip() or not last_name.strip():
        return "[Error]: domain, first_name, and last_name are required."
    try:
        base_url, key_or_error = _hunter_config("hunter_email_finder", config)
        if key_or_error is None or key_or_error.startswith("[Error]:"):
            return key_or_error or ""
        data = _request_json(
            "GET",
            f"{base_url}/email-finder",
            params={
                "domain": domain.strip(),
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
                "api_key": key_or_error,
            },
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("hunter_email_finder failed", exc_info=True)
        return f"[Error]: Hunter email finder failed: {e}"


@tool
def hunter_email_verifier(
    email: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Verify deliverability details for an email address through Hunter."""
    if not email.strip():
        return "[Error]: email is required."
    try:
        base_url, key_or_error = _hunter_config("hunter_email_verifier", config)
        if key_or_error is None or key_or_error.startswith("[Error]:"):
            return key_or_error or ""
        data = _request_json(
            "GET",
            f"{base_url}/email-verifier",
            params={"email": email.strip(), "api_key": key_or_error},
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("hunter_email_verifier failed", exc_info=True)
        return f"[Error]: Hunter email verification failed: {e}"


@tool
def mailcheck_check_email(
    email: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Check an email address with Mailcheck."""
    if not email.strip():
        return "[Error]: email is required."
    try:
        base_url, headers_or_error = _mailcheck_config("mailcheck_check_email", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/singleEmail:check",
            json_body={"email": email.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("mailcheck_check_email failed", exc_info=True)
        return f"[Error]: Mailcheck email check failed: {e}"


@tool
def peekalink_preview_url(
    url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Return link-preview metadata for a URL through Peekalink."""
    if not url.strip():
        return "[Error]: url is required."
    try:
        base_url, headers_or_error = _peekalink_config("peekalink_preview_url", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("POST", f"{base_url}/", json_body={"link": url.strip()}, headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("peekalink_preview_url failed", exc_info=True)
        return f"[Error]: Peekalink preview failed: {e}"


@tool
def peekalink_check_availability(
    url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Check whether Peekalink can create a preview for a URL."""
    if not url.strip():
        return "[Error]: url is required."
    try:
        base_url, headers_or_error = _peekalink_config("peekalink_check_availability", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/is-available/",
            json_body={"link": url.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("peekalink_check_availability failed", exc_info=True)
        return f"[Error]: Peekalink availability check failed: {e}"


@tool
def jina_reader_fetch_url(
    url: str,
    output_format: str = "markdown",
    target_selector: str = "",
    remove_selector: str = "",
    wait_for_selector: str = "",
    with_generated_alt: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Fetch a URL through Jina Reader and return LLM-friendly content.

    Args:
        url: URL to fetch.
        output_format: markdown, text, html, screenshot, or json.
        target_selector: Optional CSS selector to focus extraction.
        remove_selector: Optional CSS selector to remove.
        wait_for_selector: Optional CSS selector to wait for.
        with_generated_alt: Generate alt text for images when supported.
    """
    if not url.strip():
        return "[Error]: url is required."
    try:
        api_key = _jina_key("jina_reader_fetch_url", config)
        base_url = _jina_base("jina_reader_fetch_url", config, "jina_reader_base_url", _JINA_READER_BASE_URL)
        headers = _jina_headers(api_key, output_format="" if output_format.strip() == "json" else output_format)
        if target_selector.strip():
            headers["X-Target-Selector"] = target_selector.strip()
        if remove_selector.strip():
            headers["X-Remove-Selector"] = remove_selector.strip()
        if wait_for_selector.strip():
            headers["X-Wait-For-Selector"] = wait_for_selector.strip()
        if with_generated_alt:
            headers["X-With-Generated-Alt"] = "true"
        data = _request_json("GET", f"{base_url}/{url.strip()}", headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("jina_reader_fetch_url failed", exc_info=True)
        return f"[Error]: Jina Reader fetch failed: {e}"


@tool
def jina_search_web(
    query: str,
    output_format: str = "markdown",
    site_filter: str = "",
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search the web through Jina Search and return LLM-friendly results."""
    if not query.strip():
        return "[Error]: query is required."
    try:
        api_key = _jina_key("jina_search_web", config)
        base_url = _jina_base("jina_search_web", config, "jina_search_base_url", _JINA_SEARCH_BASE_URL)
        headers = _jina_headers(api_key, output_format="" if output_format.strip() == "json" else output_format)
        if site_filter.strip():
            headers["X-Site"] = site_filter.strip()
        data = _request_json("GET", f"{base_url}/", params={"q": query.strip(), "page": max(1, int(page))}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("jina_search_web failed", exc_info=True)
        return f"[Error]: Jina Search failed: {e}"


@tool
def jina_deep_research(
    query: str,
    max_returned_sources: int = 5,
    prioritize_sources: str = "",
    exclude_sources: str = "",
    site_filter: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a Jina DeepSearch research query."""
    if not query.strip():
        return "[Error]: query is required."
    try:
        api_key = _jina_key("jina_deep_research", config)
        if not api_key:
            return _setup_hint(
                provider=_JINA.provider,
                field_names=_JINA.hint_fields,
                tool_name="jina_deep_research",
                env_var=_JINA.env_var,
                display_name=_JINA.display_name,
            )
        base_url = _jina_base("jina_deep_research", config, "jina_deepsearch_base_url", _JINA_DEEPSEARCH_BASE_URL)
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": query.strip()}],
            "max_returned_urls": _limit(max_returned_sources, default=5, max_value=20),
        }
        if prioritize_sources.strip():
            body["boost_hostnames"] = _csv_to_list(prioritize_sources)
        if exclude_sources.strip():
            body["bad_hostnames"] = _csv_to_list(exclude_sources)
        if site_filter.strip():
            body["only_hostnames"] = _csv_to_list(site_filter)
        data = _request_json(
            "POST",
            f"{base_url}/chat/completions",
            json_body=body,
            headers=_bearer_headers(api_key),
        )
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(message, dict):
                    return _dump_json(
                        {
                            "content": message.get("content"),
                            "annotations": message.get("annotations"),
                            "usage": data.get("usage"),
                        }
                    )
        return _dump_json(data)
    except Exception as e:
        logger.error("jina_deep_research failed", exc_info=True)
        return f"[Error]: Jina DeepSearch failed: {e}"


@tool
def misp_search_attributes(
    value: str = "",
    tags: str = "",
    query_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search MISP attributes with restSearch."""
    try:
        base_url, headers_or_error, verify = _misp_config("misp_search_attributes", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = _parse_json(query_json, expected=dict, label="query_json") if query_json.strip() else {}
        if value.strip():
            body["value"] = value.strip()
        if tags.strip():
            body["tags"] = _csv_to_list(tags)
        data = _request_json(
            "POST",
            f"{base_url}/attributes/restSearch",
            json_body=body,
            headers=headers_or_error,
            verify=verify,
        )
        results = data.get("response", {}).get("Attribute") if isinstance(data, dict) else data
        if isinstance(results, list):
            results = results[: _limit(limit, max_value=500)]
        return _dump_json(results)
    except Exception as e:
        logger.error("misp_search_attributes failed", exc_info=True)
        return f"[Error]: MISP attribute search failed: {e}"


@tool
def misp_search_events(
    value: str = "",
    tags: str = "",
    query_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search MISP events with restSearch."""
    try:
        base_url, headers_or_error, verify = _misp_config("misp_search_events", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = _parse_json(query_json, expected=dict, label="query_json") if query_json.strip() else {}
        if value.strip():
            body["value"] = value.strip()
        if tags.strip():
            body["tags"] = _csv_to_list(tags)
        data = _request_json(
            "POST",
            f"{base_url}/events/restSearch",
            json_body=body,
            headers=headers_or_error,
            verify=verify,
        )
        results = data.get("response") if isinstance(data, dict) else data
        if isinstance(results, list):
            results = results[: _limit(limit, max_value=500)]
        return _dump_json(results)
    except Exception as e:
        logger.error("misp_search_events failed", exc_info=True)
        return f"[Error]: MISP event search failed: {e}"


@tool
def misp_get_event(
    event_id: str,
    include_attributes: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a MISP event by ID."""
    if not event_id.strip():
        return "[Error]: event_id is required."
    try:
        base_url, headers_or_error, verify = _misp_config("misp_get_event", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/events/view/{quote(event_id.strip(), safe='')}",
            headers=headers_or_error,
            verify=verify,
        )
        event = data.get("Event") if isinstance(data, dict) else data
        if isinstance(event, dict) and not include_attributes:
            event = dict(event)
            event.pop("Attribute", None)
            event.pop("Object", None)
        return _dump_json(event)
    except Exception as e:
        logger.error("misp_get_event failed", exc_info=True)
        return f"[Error]: MISP event lookup failed: {e}"


@tool
def misp_create_event(
    info: str,
    org_id: str = "",
    distribution: int = 0,
    threat_level_id: int = 4,
    analysis: int = 0,
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a MISP event."""
    if not info.strip():
        return "[Error]: info is required."
    try:
        base_url, headers_or_error, verify = _misp_config("misp_create_event", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = {
            "info": info.strip(),
            "distribution": int(distribution),
            "threat_level_id": int(threat_level_id),
            "analysis": int(analysis),
        }
        if org_id.strip():
            body["org_id"] = org_id.strip()
        if fields_json.strip():
            body.update(_parse_json(fields_json, expected=dict, label="fields_json"))
        data = _request_json("POST", f"{base_url}/events", json_body=body, headers=headers_or_error, verify=verify)
        return _dump_json(data.get("Event") if isinstance(data, dict) and "Event" in data else data)
    except Exception as e:
        logger.error("misp_create_event failed", exc_info=True)
        return f"[Error]: MISP event creation failed: {e}"


@tool
def misp_list_tags(
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List MISP tags."""
    try:
        base_url, headers_or_error, verify = _misp_config("misp_list_tags", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/tags", headers=headers_or_error, verify=verify)
        tags = data.get("Tag") if isinstance(data, dict) else data
        if isinstance(tags, list):
            tags = tags[: _limit(limit, max_value=1000)]
        return _dump_json(tags)
    except Exception as e:
        logger.error("misp_list_tags failed", exc_info=True)
        return f"[Error]: MISP tag listing failed: {e}"


@tool
def misp_add_event_tag(
    event_id: str,
    tag_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a tag to a MISP event."""
    if not event_id.strip() or not tag_id.strip():
        return "[Error]: event_id and tag_id are required."
    try:
        base_url, headers_or_error, verify = _misp_config("misp_add_event_tag", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/events/addTag",
            json_body={"event": event_id.strip(), "tag": tag_id.strip()},
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("misp_add_event_tag failed", exc_info=True)
        return f"[Error]: MISP event tag add failed: {e}"


@tool
def misp_remove_event_tag(
    event_id: str,
    tag_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Remove a tag from a MISP event."""
    if not event_id.strip() or not tag_id.strip():
        return "[Error]: event_id and tag_id are required."
    try:
        base_url, headers_or_error, verify = _misp_config("misp_remove_event_tag", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/events/removeTag/{quote(event_id.strip(), safe='')}/{quote(tag_id.strip(), safe='')}",
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("misp_remove_event_tag failed", exc_info=True)
        return f"[Error]: MISP event tag removal failed: {e}"


def _thehive_list_body(resource: str, limit: int, query_json: str) -> dict[str, Any]:
    if query_json.strip():
        return _parse_json(query_json, expected=dict, label="query_json")
    return {
        "query": [
            {"_name": f"list{resource}"},
            {"_name": "page", "from": 0, "to": _limit(limit, max_value=500)},
        ]
    }


@tool
def thehive_list_cases(
    query_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List or query TheHive cases."""
    try:
        base_url, headers_or_error, verify, api_version = _thehive_config("thehive_list_cases", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if api_version == "v1":
            data = _request_json(
                "POST",
                f"{base_url}/v1/query",
                params={"name": "cases"},
                json_body=_thehive_list_body("Case", limit, query_json),
                headers=headers_or_error,
                verify=verify,
            )
        else:
            data = _request_json(
                "POST",
                f"{base_url}/case/_search",
                params={"range": f"0-{_limit(limit, max_value=500)}"},
                json_body=_parse_json(query_json, expected=dict, label="query_json") if query_json.strip() else {},
                headers=headers_or_error,
                verify=verify,
            )
        return _dump_json(data)
    except Exception as e:
        logger.error("thehive_list_cases failed", exc_info=True)
        return f"[Error]: TheHive case listing failed: {e}"


@tool
def thehive_get_case(
    case_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a TheHive case by ID."""
    if not case_id.strip():
        return "[Error]: case_id is required."
    try:
        base_url, headers_or_error, verify, _api_version = _thehive_config("thehive_get_case", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/case/{quote(case_id.strip(), safe='')}",
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("thehive_get_case failed", exc_info=True)
        return f"[Error]: TheHive case lookup failed: {e}"


@tool
def thehive_create_case(
    title: str,
    description: str,
    owner: str,
    severity: int = 2,
    tlp: int = 2,
    tags: str = "",
    start_date: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a TheHive case."""
    if not title.strip() or not description.strip() or not owner.strip():
        return "[Error]: title, description, and owner are required."
    try:
        base_url, headers_or_error, verify, _api_version = _thehive_config("thehive_create_case", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = {
            "title": title.strip(),
            "description": description.strip(),
            "severity": int(severity),
            "startDate": _to_epoch_ms(start_date),
            "owner": owner.strip(),
            "flag": False,
            "tlp": int(tlp),
            "tags": _csv_to_list(tags),
        }
        if fields_json.strip():
            body.update(_parse_json(fields_json, expected=dict, label="fields_json"))
        data = _request_json("POST", f"{base_url}/case", json_body=body, headers=headers_or_error, verify=verify)
        return _dump_json(data)
    except Exception as e:
        logger.error("thehive_create_case failed", exc_info=True)
        return f"[Error]: TheHive case creation failed: {e}"


@tool
def thehive_list_alerts(
    query_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List or query TheHive alerts."""
    try:
        base_url, headers_or_error, verify, api_version = _thehive_config("thehive_list_alerts", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if api_version == "v1":
            data = _request_json(
                "POST",
                f"{base_url}/v1/query",
                params={"name": "alerts"},
                json_body=_thehive_list_body("Alert", limit, query_json),
                headers=headers_or_error,
                verify=verify,
            )
        else:
            data = _request_json(
                "POST",
                f"{base_url}/alert/_search",
                params={"range": f"0-{_limit(limit, max_value=500)}"},
                json_body=_parse_json(query_json, expected=dict, label="query_json") if query_json.strip() else {},
                headers=headers_or_error,
                verify=verify,
            )
        return _dump_json(data)
    except Exception as e:
        logger.error("thehive_list_alerts failed", exc_info=True)
        return f"[Error]: TheHive alert listing failed: {e}"


@tool
def thehive_get_alert(
    alert_id: str,
    include_similar: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a TheHive alert by ID."""
    if not alert_id.strip():
        return "[Error]: alert_id is required."
    try:
        base_url, headers_or_error, verify, _api_version = _thehive_config("thehive_get_alert", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/alert/{quote(alert_id.strip(), safe='')}",
            params={"similarity": "true" if include_similar else ""},
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("thehive_get_alert failed", exc_info=True)
        return f"[Error]: TheHive alert lookup failed: {e}"


@tool
def thehive_create_alert(
    title: str,
    description: str,
    source: str,
    source_ref: str,
    alert_type: str,
    severity: int = 2,
    tlp: int = 2,
    tags: str = "",
    date: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a TheHive alert."""
    if not title.strip() or not description.strip() or not source.strip() or not source_ref.strip() or not alert_type.strip():
        return "[Error]: title, description, source, source_ref, and alert_type are required."
    try:
        base_url, headers_or_error, verify, _api_version = _thehive_config("thehive_create_alert", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = {
            "title": title.strip(),
            "description": description.strip(),
            "severity": int(severity),
            "date": _to_epoch_ms(date),
            "tags": _csv_to_list(tags),
            "tlp": int(tlp),
            "status": "New",
            "type": alert_type.strip(),
            "source": source.strip(),
            "sourceRef": source_ref.strip(),
            "follow": True,
        }
        if fields_json.strip():
            body.update(_parse_json(fields_json, expected=dict, label="fields_json"))
        data = _request_json("POST", f"{base_url}/alert", json_body=body, headers=headers_or_error, verify=verify)
        return _dump_json(data)
    except Exception as e:
        logger.error("thehive_create_alert failed", exc_info=True)
        return f"[Error]: TheHive alert creation failed: {e}"


@tool
def securityscorecard_get_company_scorecard(
    scorecard_identifier: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get SecurityScorecard company information and scorecard summary."""
    if not scorecard_identifier.strip():
        return "[Error]: scorecard_identifier is required."
    try:
        base_url, headers_or_error = _securityscorecard_config("securityscorecard_get_company_scorecard", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/companies/{quote(scorecard_identifier.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("securityscorecard_get_company_scorecard failed", exc_info=True)
        return f"[Error]: SecurityScorecard company lookup failed: {e}"


@tool
def securityscorecard_list_company_factors(
    scorecard_identifier: str,
    severity: str = "",
    severity_in: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List SecurityScorecard company factor scores and issue counts."""
    if not scorecard_identifier.strip():
        return "[Error]: scorecard_identifier is required."
    try:
        base_url, headers_or_error = _securityscorecard_config("securityscorecard_list_company_factors", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/companies/{quote(scorecard_identifier.strip(), safe='')}/factors",
            params={"severity": severity.strip(), "severity_in": severity_in.strip()},
            headers=headers_or_error,
        )
        results = data.get("entries") if isinstance(data, dict) else data
        if isinstance(results, list):
            results = results[: _limit(limit, max_value=100)]
        return _dump_json(results)
    except Exception as e:
        logger.error("securityscorecard_list_company_factors failed", exc_info=True)
        return f"[Error]: SecurityScorecard company factor listing failed: {e}"


@tool
def securityscorecard_get_company_history(
    scorecard_identifier: str,
    date_from: str = "",
    date_to: str = "",
    timing: str = "daily",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get SecurityScorecard company historical factor scores."""
    if not scorecard_identifier.strip():
        return "[Error]: scorecard_identifier is required."
    try:
        base_url, headers_or_error = _securityscorecard_config("securityscorecard_get_company_history", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/companies/{quote(scorecard_identifier.strip(), safe='')}/history/factors/score",
            params={"date_from": date_from.strip(), "date_to": date_to.strip(), "timing": timing.strip() or "daily"},
            headers=headers_or_error,
        )
        results = data.get("entries") if isinstance(data, dict) else data
        if isinstance(results, list):
            results = results[: _limit(limit, max_value=500)]
        return _dump_json(results)
    except Exception as e:
        logger.error("securityscorecard_get_company_history failed", exc_info=True)
        return f"[Error]: SecurityScorecard company history lookup failed: {e}"


@tool
def securityscorecard_list_portfolios(
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List SecurityScorecard portfolios."""
    try:
        base_url, headers_or_error = _securityscorecard_config("securityscorecard_list_portfolios", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/portfolios", headers=headers_or_error)
        results = data.get("entries") if isinstance(data, dict) else data
        if isinstance(results, list):
            results = results[: _limit(limit, max_value=500)]
        return _dump_json(results)
    except Exception as e:
        logger.error("securityscorecard_list_portfolios failed", exc_info=True)
        return f"[Error]: SecurityScorecard portfolio listing failed: {e}"


@tool
def securityscorecard_add_portfolio_company(
    portfolio_id: str,
    domain: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a company domain to a SecurityScorecard portfolio."""
    if not portfolio_id.strip() or not domain.strip():
        return "[Error]: portfolio_id and domain are required."
    try:
        base_url, headers_or_error = _securityscorecard_config("securityscorecard_add_portfolio_company", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            f"{base_url}/portfolios/{quote(portfolio_id.strip(), safe='')}/companies/{quote(domain.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("securityscorecard_add_portfolio_company failed", exc_info=True)
        return f"[Error]: SecurityScorecard portfolio company add failed: {e}"


@tool
def securityscorecard_remove_portfolio_company(
    portfolio_id: str,
    domain: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Remove a company domain from a SecurityScorecard portfolio."""
    if not portfolio_id.strip() or not domain.strip():
        return "[Error]: portfolio_id and domain are required."
    try:
        base_url, headers_or_error = _securityscorecard_config("securityscorecard_remove_portfolio_company", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/portfolios/{quote(portfolio_id.strip(), safe='')}/companies/{quote(domain.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("securityscorecard_remove_portfolio_company failed", exc_info=True)
        return f"[Error]: SecurityScorecard portfolio company removal failed: {e}"


@tool
def okta_list_users(
    search_query: str = "",
    q: str = "",
    filter_query: str = "",
    after: str = "",
    limit: int = 20,
    simplify: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List or search Okta users.

    Args:
        search_query: Optional Okta search expression such as profile.lastName sw "Smi".
        q: Optional free-text query against first name, last name, or email.
        filter_query: Optional Okta filter expression.
        after: Optional cursor from Okta's Link header.
        limit: Number of users to return, 1-200.
        simplify: Return compact user fields when true.
    """
    try:
        base_url, headers_or_error = _okta_config("okta_list_users", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/v1/users",
            params={
                "search": search_query.strip(),
                "q": q.strip(),
                "filter": filter_query.strip(),
                "after": after.strip(),
                "limit": _limit(limit, default=20, max_value=200),
            },
            headers=headers_or_error,
        )
        if simplify and isinstance(data, list):
            data = [_okta_simplify_user(item) for item in data]
        return _dump_json(data)
    except Exception as e:
        logger.error("okta_list_users failed", exc_info=True)
        return f"[Error]: Okta user listing failed: {e}"


@tool
def okta_get_user(
    user_id: str,
    simplify: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Okta user by ID, login, or email.

    Args:
        user_id: Okta user ID, login, or email.
        simplify: Return compact user fields when true.
    """
    user_id = user_id.strip()
    if not user_id:
        return "[Error]: user_id is required."
    try:
        base_url, headers_or_error = _okta_config("okta_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/api/v1/users/{quote(user_id, safe='')}", headers=headers_or_error)
        return _dump_json(_okta_simplify_user(data) if simplify else data)
    except Exception as e:
        logger.error("okta_get_user failed", exc_info=True)
        return f"[Error]: Okta user lookup failed: {e}"


@tool
def okta_create_user(
    first_name: str,
    last_name: str,
    login: str,
    email: str,
    activate: bool = True,
    profile_json: str = "",
    credentials_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Okta user.

    Args:
        first_name: User first name.
        last_name: User last name.
        login: Unique Okta login, usually an email address.
        email: Primary email address.
        activate: Whether Okta should activate the user immediately.
        profile_json: Optional JSON object of additional profile fields.
        credentials_json: Optional JSON object for password/recovery credentials.
    """
    required = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "login": login.strip(),
        "email": email.strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return f"[Error]: missing required fields: {', '.join(missing)}."
    try:
        body: dict[str, Any] = {
            "profile": _okta_profile(
                first_name=first_name,
                last_name=last_name,
                login=login,
                email=email,
                profile_json=profile_json,
            )
        }
        credentials = _parse_json(credentials_json, expected=dict, label="credentials_json")
        if credentials:
            body["credentials"] = credentials
        base_url, headers_or_error = _okta_config("okta_create_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/api/v1/users",
            params={"activate": "true" if activate else "false"},
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("okta_create_user failed", exc_info=True)
        return f"[Error]: Okta user creation failed: {e}"


@tool
def okta_update_user(
    user_id: str,
    profile_json: str,
    credentials_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an Okta user profile.

    Args:
        user_id: Okta user ID, login, or email.
        profile_json: JSON object of profile fields to update.
        credentials_json: Optional JSON object for password/recovery credential updates.
    """
    user_id = user_id.strip()
    if not user_id:
        return "[Error]: user_id is required."
    try:
        profile = _parse_json(profile_json, expected=dict, label="profile_json")
        credentials = _parse_json(credentials_json, expected=dict, label="credentials_json")
        body: dict[str, Any] = _filtered({"profile": profile, "credentials": credentials})
        if not body:
            return "[Error]: profile_json or credentials_json is required."
        base_url, headers_or_error = _okta_config("okta_update_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/api/v1/users/{quote(user_id, safe='')}",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("okta_update_user failed", exc_info=True)
        return f"[Error]: Okta user update failed: {e}"


@tool
def okta_delete_user(
    user_id: str,
    deactivate_first: bool = False,
    send_email: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Okta user, optionally deactivating the user first.

    Args:
        user_id: Okta user ID, login, or email.
        deactivate_first: Run Okta's deactivate lifecycle action before deletion.
        send_email: Whether Okta should send a deactivation email when applicable.
    """
    user_id = user_id.strip()
    if not user_id:
        return "[Error]: user_id is required."
    try:
        base_url, headers_or_error = _okta_config("okta_delete_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        results: dict[str, Any] = {}
        if deactivate_first:
            results["deactivate"] = _request_json(
                "POST",
                f"{base_url}/api/v1/users/{quote(user_id, safe='')}/lifecycle/deactivate",
                params={"sendEmail": "true" if send_email else "false"},
                headers=headers_or_error,
            )
        results["delete"] = _request_json(
            "DELETE",
            f"{base_url}/api/v1/users/{quote(user_id, safe='')}",
            params={"sendEmail": "true" if send_email else "false"},
            headers=headers_or_error,
        )
        return _dump_json(results)
    except Exception as e:
        logger.error("okta_delete_user failed", exc_info=True)
        return f"[Error]: Okta user deletion failed: {e}"


@tool
def elastic_security_list_cases(
    status: str = "",
    tags: str = "",
    limit: int = 50,
    sort_field: str = "createdAt",
    sort_order: str = "desc",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Elastic Security cases."""
    try:
        base_url, headers_or_error, auth = _elastic_security_config("elastic_security_list_cases", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/cases/_find",
            params={
                "perPage": _limit(limit, max_value=100),
                "status": status.strip(),
                "tags": ",".join(_csv_to_list(tags)),
                "sortField": sort_field.strip() or "createdAt",
                "sortOrder": sort_order.strip() or "desc",
            },
            headers=headers_or_error,
            auth=auth,
        )
        return _dump_json(data.get("cases") if isinstance(data, dict) and "cases" in data else data)
    except Exception as e:
        logger.error("elastic_security_list_cases failed", exc_info=True)
        return f"[Error]: Elastic Security case listing failed: {e}"


@tool
def elastic_security_get_case(
    case_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Elastic Security case by ID."""
    if not case_id.strip():
        return "[Error]: case_id is required."
    try:
        base_url, headers_or_error, auth = _elastic_security_config("elastic_security_get_case", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/cases/{quote(case_id.strip(), safe='')}",
            headers=headers_or_error,
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("elastic_security_get_case failed", exc_info=True)
        return f"[Error]: Elastic Security case lookup failed: {e}"


@tool
def elastic_security_list_case_tags(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Elastic Security case tags."""
    try:
        base_url, headers_or_error, auth = _elastic_security_config("elastic_security_list_case_tags", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/cases/tags", headers=headers_or_error, auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("elastic_security_list_case_tags failed", exc_info=True)
        return f"[Error]: Elastic Security tag listing failed: {e}"


@tool
def elastic_security_create_case(
    title: str,
    description: str = "",
    tags: str = "",
    owner: str = "securitySolution",
    connector_json: str = "",
    fields_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Elastic Security case."""
    if not title.strip():
        return "[Error]: title is required."
    try:
        base_url, headers_or_error, auth = _elastic_security_config("elastic_security_create_case", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        connector = (
            _parse_json(connector_json, expected=dict, label="connector_json")
            if connector_json.strip()
            else {"id": "none", "name": "none", "type": ".none", "fields": None}
        )
        body: dict[str, Any] = {
            "title": title.strip(),
            "description": description.strip(),
            "tags": _csv_to_list(tags),
            "owner": owner.strip() or "securitySolution",
            "connector": connector,
            "settings": {"syncAlerts": False},
        }
        if fields_json.strip():
            body.update(_parse_json(fields_json, expected=dict, label="fields_json"))
        data = _request_json("POST", f"{base_url}/cases", json_body=body, headers=headers_or_error, auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("elastic_security_create_case failed", exc_info=True)
        return f"[Error]: Elastic Security case creation failed: {e}"


@tool
def elastic_security_add_case_comment(
    case_id: str,
    comment: str,
    owner: str = "securitySolution",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a comment to an Elastic Security case."""
    if not case_id.strip() or not comment.strip():
        return "[Error]: case_id and comment are required."
    try:
        base_url, headers_or_error, auth = _elastic_security_config("elastic_security_add_case_comment", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/cases/{quote(case_id.strip(), safe='')}/comments",
            json_body={"comment": comment.strip(), "type": "user", "owner": owner.strip() or "securitySolution"},
            headers=headers_or_error,
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("elastic_security_add_case_comment failed", exc_info=True)
        return f"[Error]: Elastic Security case comment add failed: {e}"


ENRICHMENT_SECURITY_SERVICE_TOOLS = [
    urlscan_search_scans,
    urlscan_get_result,
    urlscan_submit_scan,
    hunter_domain_search,
    hunter_email_finder,
    hunter_email_verifier,
    mailcheck_check_email,
    peekalink_preview_url,
    peekalink_check_availability,
    jina_reader_fetch_url,
    jina_search_web,
    jina_deep_research,
    misp_search_attributes,
    misp_search_events,
    misp_get_event,
    misp_create_event,
    misp_list_tags,
    misp_add_event_tag,
    misp_remove_event_tag,
    thehive_list_cases,
    thehive_get_case,
    thehive_create_case,
    thehive_list_alerts,
    thehive_get_alert,
    thehive_create_alert,
    securityscorecard_get_company_scorecard,
    securityscorecard_list_company_factors,
    securityscorecard_get_company_history,
    securityscorecard_list_portfolios,
    securityscorecard_add_portfolio_company,
    securityscorecard_remove_portfolio_company,
    okta_list_users,
    okta_get_user,
    okta_create_user,
    okta_update_user,
    okta_delete_user,
    elastic_security_list_cases,
    elastic_security_get_case,
    elastic_security_list_case_tags,
    elastic_security_create_case,
    elastic_security_add_case_comment,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="enrichment_security", tools=tuple(ENRICHMENT_SECURITY_SERVICE_TOOLS)))
