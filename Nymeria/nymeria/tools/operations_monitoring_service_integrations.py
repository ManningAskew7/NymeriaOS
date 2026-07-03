"""Operations, monitoring, and infrastructure service integration tools."""

from __future__ import annotations

import json
import logging
import shlex
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
_NETLIFY_BASE_URL = "https://api.netlify.com/api/v1"
_UPTIMEROBOT_BASE_URL = "https://api.uptimerobot.com/v2"
_PAGERDUTY_BASE_URL = "https://api.pagerduty.com"
_SENTRY_BASE_URL = "https://sentry.io"
_CLOUDFLARE_BASE_URL = "https://api.cloudflare.com/client/v4"
_GRAFANA_API_SUFFIX = "/api"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered. The shared
# _service_base / _bearer_config helpers keep their generic base-URL field-name
# tuple inline, so the spec still declares it as a group even where no call site
# reads it back. Providers with two setup-hint variants (base URL vs token)
# carry the token variant in the spec and keep the base-URL branch's env_var
# inline. PagerDuty resolves different alias subsets per call site, so those
# per-branch provider_aliases stay inline.
_GRAFANA = register_provider_spec(
    ProviderCredentialSpec(
        provider="grafana",
        aliases=("grafana_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token", names=("api_key", "apiKey", "access_token", "token", "value")
            ),
        ),
        hint_fields=("api_key", "access_token", "token", "value"),
        env_var="GRAFANA_API_TOKEN",
        display_name="Grafana",
    )
)

_METABASE = register_provider_spec(
    ProviderCredentialSpec(
        provider="metabase",
        aliases=("metabase_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(role="session_token", names=("session_token", "sessionToken")),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey", "token", "value")),
            CredentialFieldGroup(role="username", names=("username", "email")),
            CredentialFieldGroup(role="password", names=("password",)),
        ),
        hint_fields=("session_token", "api_key", "username", "password"),
        env_var="METABASE_SESSION_TOKEN or METABASE_API_KEY",
        display_name="Metabase",
    )
)

_ELASTICSEARCH = register_provider_spec(
    ProviderCredentialSpec(
        provider="elasticsearch",
        aliases=("elastic", "elastic_cloud", "elasticsearch_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "apiKey", "encoded_api_key", "token", "value")
            ),
            CredentialFieldGroup(
                role="bearer_token",
                names=("access_token", "bearer_token", "bearerToken"),
                required=False,
            ),
            CredentialFieldGroup(role="username", names=("username", "user")),
            CredentialFieldGroup(role="password", names=("password",)),
            CredentialFieldGroup(
                role="ignore_ssl",
                names=("ignore_ssl_issues", "ignoreSSLIssues", "allow_insecure"),
                required=False,
            ),
        ),
        hint_fields=("api_key", "username", "password"),
        env_var="ELASTICSEARCH_API_KEY or ELASTICSEARCH_USERNAME + ELASTICSEARCH_PASSWORD",
        display_name="Elasticsearch",
    )
)

_SPLUNK = register_provider_spec(
    ProviderCredentialSpec(
        provider="splunk",
        aliases=("splunk_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url",
                names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
                required=False,
            ),
            CredentialFieldGroup(
                role="token", names=("auth_token", "authToken", "access_token", "token", "value")
            ),
            CredentialFieldGroup(
                role="allow_insecure",
                names=("allow_unauthorized_certs", "allowUnauthorizedCerts", "allow_insecure"),
                required=False,
            ),
        ),
        hint_fields=("auth_token", "access_token", "token", "value"),
        env_var="SPLUNK_AUTH_TOKEN",
        display_name="Splunk",
    )
)

_RUNDECK = register_provider_spec(
    ProviderCredentialSpec(
        provider="rundeck",
        aliases=("rundeck_api",),
        groups=(
            CredentialFieldGroup(role="base_url", names=("base_url", "url"), required=False),
            CredentialFieldGroup(
                role="token", names=("token", "api_token", "apiToken", "access_token", "value")
            ),
        ),
        hint_fields=("token", "api_token", "value"),
        env_var="RUNDECK_TOKEN",
        display_name="Rundeck",
    )
)

_NETLIFY = register_provider_spec(
    ProviderCredentialSpec(
        provider="netlify",
        aliases=("netlify_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
            ),
        ),
        hint_fields=("access_token", "accessToken", "api_key", "apiKey", "token", "value"),
        env_var="NETLIFY_ACCESS_TOKEN",
        display_name="Netlify",
    )
)

_UPTIMEROBOT = register_provider_spec(
    ProviderCredentialSpec(
        provider="uptimerobot",
        aliases=("uptime_robot", "uptimerobot_api", "uptime_robot_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="api_key", names=("api_key", "apiKey", "key", "token", "value")
            ),
        ),
        hint_fields=("api_key", "token", "value"),
        env_var="UPTIMEROBOT_API_KEY",
        display_name="UptimeRobot",
    )
)

_PAGERDUTY = register_provider_spec(
    ProviderCredentialSpec(
        provider="pagerduty",
        aliases=("pagerduty_api", "pagerduty_oauth2_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="access_token",
                names=("access_token", "accessToken", "bearer_token", "bearerToken"),
            ),
            CredentialFieldGroup(
                role="api_token",
                names=("api_token", "apiToken", "api_key", "apiKey", "token", "value"),
            ),
            CredentialFieldGroup(
                role="from_email", names=("from_email", "fromEmail", "email"), required=False
            ),
        ),
        hint_fields=("api_token", "access_token", "token", "value"),
        env_var="PAGERDUTY_API_TOKEN",
        display_name="PagerDuty",
    )
)

_SENTRY = register_provider_spec(
    ProviderCredentialSpec(
        provider="sentry",
        aliases=("sentry_io", "sentryio", "sentry_io_api", "sentry_io_server_api"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=(
                    "auth_token",
                    "authToken",
                    "access_token",
                    "accessToken",
                    "api_key",
                    "token",
                    "value",
                ),
            ),
        ),
        hint_fields=(
            "auth_token",
            "authToken",
            "access_token",
            "accessToken",
            "api_key",
            "token",
            "value",
        ),
        env_var="SENTRY_AUTH_TOKEN",
        display_name="Sentry",
    )
)

_CLOUDFLARE = register_provider_spec(
    ProviderCredentialSpec(
        provider="cloudflare",
        aliases=("cloudflare_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "url", "api_url", "apiUrl"), required=False
            ),
            CredentialFieldGroup(
                role="token",
                names=("api_token", "apiToken", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_token", "apiToken", "access_token", "accessToken", "token", "value"),
        env_var="CLOUDFLARE_API_TOKEN",
        display_name="Cloudflare",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 500) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    form_data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    auth: Any = None,
    verify: bool = True,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, verify=verify) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params) if params is not None else None,
                json=json_body,
                data=_filtered(form_data) if form_data is not None else None,
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
                    or body.get("detail")
                    or ""
                )
                errors = body.get("errors")
                if not detail and isinstance(errors, list):
                    detail = "; ".join(str(item) for item in errors[:3])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def _csv_to_dash(value: str) -> str:
    return "-".join(_csv_to_list(value.replace("-", ",")))


def _truthy(value: bool | str | int) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bearer_config(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    token_fields: tuple[str, ...],
    env_token: str,
    settings_token_name: str,
    settings_base_name: str,
    default_base: str,
    tool_name: str,
    display_name: str,
    config: Optional[RunnableConfig],
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
        field_names=token_fields,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(settings_token_name)
    if not token:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=token_fields,
            tool_name=tool_name,
            env_var=env_token,
            display_name=display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _rooted_api_base(base: str, suffix: str) -> str:
    base = _base_url(base)
    suffix = suffix.rstrip("/")
    if base.rstrip("/").endswith(suffix):
        return base
    return f"{base}{suffix}"


def _service_base(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    settings_base_name: str,
    tool_name: str,
    display_name: str,
    env_var: str,
    config: Optional[RunnableConfig],
) -> str | None:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=("base_url", "baseUrl", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_base_name)
    )
    if base:
        return _base_url(base)
    return _setup_hint(
        provider=provider,
        field_names=("base_url", "url"),
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    )


def _grafana_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str]:
    base_or_error = _service_base(
        provider=_GRAFANA.provider,
        provider_aliases=_GRAFANA.aliases,
        settings_base_name="grafana_base_url",
        tool_name=tool_name,
        display_name=_GRAFANA.display_name,
        env_var="GRAFANA_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: Grafana base URL is required."
    token = _credential_value(
        provider=_GRAFANA.provider,
        provider_aliases=_GRAFANA.aliases,
        field_names=_GRAFANA.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("grafana_api_token")
    if not token:
        return "", _setup_hint(
            provider=_GRAFANA.provider,
            field_names=_GRAFANA.hint_fields,
            tool_name=tool_name,
            env_var=_GRAFANA.env_var,
            display_name=_GRAFANA.display_name,
        )
    return _rooted_api_base(base_or_error, _GRAFANA_API_SUFFIX), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _metabase_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str]:
    base_or_error = _service_base(
        provider=_METABASE.provider,
        provider_aliases=_METABASE.aliases,
        settings_base_name="metabase_base_url",
        tool_name=tool_name,
        display_name=_METABASE.display_name,
        env_var="METABASE_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: Metabase base URL is required."
    session_token = _credential_value(
        provider=_METABASE.provider,
        provider_aliases=_METABASE.aliases,
        field_names=_METABASE.group("session_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("metabase_session_token")
    api_key = _credential_value(
        provider=_METABASE.provider,
        provider_aliases=_METABASE.aliases,
        field_names=_METABASE.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("metabase_api_key")
    if session_token:
        return base_or_error, {"Accept": "application/json", "X-Metabase-Session": session_token}
    if api_key:
        return base_or_error, {"Accept": "application/json", "x-api-key": api_key}

    username = _credential_value(
        provider=_METABASE.provider,
        provider_aliases=_METABASE.aliases,
        field_names=_METABASE.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("metabase_username")
    password = _credential_value(
        provider=_METABASE.provider,
        provider_aliases=_METABASE.aliases,
        field_names=_METABASE.group("password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("metabase_password")
    if username and password:
        session = _request_json(
            "POST",
            f"{base_or_error}/api/session",
            json_body={"username": username, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        token = session.get("id") if isinstance(session, dict) else None
        if token:
            return base_or_error, {"Accept": "application/json", "X-Metabase-Session": token}

    return "", _setup_hint(
        provider=_METABASE.provider,
        field_names=_METABASE.hint_fields,
        tool_name=tool_name,
        env_var=_METABASE.env_var,
        display_name=_METABASE.display_name,
    )


def _elasticsearch_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str, Any, bool]:
    base_or_error = _service_base(
        provider=_ELASTICSEARCH.provider,
        provider_aliases=_ELASTICSEARCH.aliases,
        settings_base_name="elasticsearch_base_url",
        tool_name=tool_name,
        display_name=_ELASTICSEARCH.display_name,
        env_var="ELASTICSEARCH_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: Elasticsearch base URL is required.", None, True
    api_key = _credential_value(
        provider=_ELASTICSEARCH.provider,
        provider_aliases=_ELASTICSEARCH.aliases,
        field_names=_ELASTICSEARCH.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("elasticsearch_api_key")
    bearer_token = _credential_value(
        provider=_ELASTICSEARCH.provider,
        provider_aliases=_ELASTICSEARCH.aliases,
        field_names=_ELASTICSEARCH.group("bearer_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("elasticsearch_bearer_token")
    username = _credential_value(
        provider=_ELASTICSEARCH.provider,
        provider_aliases=_ELASTICSEARCH.aliases,
        field_names=_ELASTICSEARCH.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("elasticsearch_username")
    password = _credential_value(
        provider=_ELASTICSEARCH.provider,
        provider_aliases=_ELASTICSEARCH.aliases,
        field_names=_ELASTICSEARCH.group("password"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("elasticsearch_password")
    verify = not _truthy(
        _credential_value(
            provider=_ELASTICSEARCH.provider,
            provider_aliases=_ELASTICSEARCH.aliases,
            field_names=_ELASTICSEARCH.group("ignore_ssl"),
            tool_name=tool_name,
            config=config,
        )
        or str(_settings_value("elasticsearch_ignore_ssl_issues") or "")
    )
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth = None
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    elif bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif username and password:
        auth = (username, password)
    else:
        return "", _setup_hint(
            provider=_ELASTICSEARCH.provider,
            field_names=_ELASTICSEARCH.hint_fields,
            tool_name=tool_name,
            env_var=_ELASTICSEARCH.env_var,
            display_name=_ELASTICSEARCH.display_name,
        ), None, verify
    return base_or_error, headers, auth, verify


def _splunk_config(
    tool_name: str,
    config: Optional[RunnableConfig],
) -> tuple[str, dict[str, str] | str, bool]:
    base_or_error = _service_base(
        provider=_SPLUNK.provider,
        provider_aliases=_SPLUNK.aliases,
        settings_base_name="splunk_base_url",
        tool_name=tool_name,
        display_name=_SPLUNK.display_name,
        env_var="SPLUNK_BASE_URL",
        config=config,
    )
    if base_or_error is None or base_or_error.startswith("[Error]:"):
        return "", base_or_error or "[Error]: Splunk base URL is required.", True
    token = _credential_value(
        provider=_SPLUNK.provider,
        provider_aliases=_SPLUNK.aliases,
        field_names=_SPLUNK.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("splunk_auth_token")
    verify = not _truthy(
        _credential_value(
            provider=_SPLUNK.provider,
            provider_aliases=_SPLUNK.aliases,
            field_names=_SPLUNK.group("allow_insecure"),
            tool_name=tool_name,
            config=config,
        )
        or str(_settings_value("splunk_allow_unauthorized_certs") or "")
    )
    if not token:
        return "", _setup_hint(
            provider=_SPLUNK.provider,
            field_names=_SPLUNK.hint_fields,
            tool_name=tool_name,
            env_var=_SPLUNK.env_var,
            display_name=_SPLUNK.display_name,
        ), verify
    return base_or_error, {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, verify


def _rundeck_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_RUNDECK.provider,
            provider_aliases=_RUNDECK.aliases,
            field_names=_RUNDECK.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("rundeck_base_url")
    )
    if not base:
        return "", (
            '[Error]: No Rundeck base URL found. Save a Rundeck credential with "base_url" / "url", '
            "or set RUNDECK_BASE_URL."
        )
    token = _credential_value(
        provider=_RUNDECK.provider,
        provider_aliases=_RUNDECK.aliases,
        field_names=_RUNDECK.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("rundeck_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_RUNDECK.provider,
            field_names=_RUNDECK.hint_fields,
            tool_name=tool_name,
            env_var=_RUNDECK.env_var,
            display_name=_RUNDECK.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "X-Rundeck-Auth-Token": str(token),
    }


def _rundeck_arg_string(arguments_json: str) -> str:
    if not arguments_json.strip():
        return ""
    try:
        parsed = json.loads(arguments_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"arguments_json must be valid JSON: {e}") from e
    if isinstance(parsed, dict):
        items = parsed.items()
    elif isinstance(parsed, list):
        items = []
        for entry in parsed:
            if not isinstance(entry, dict):
                raise ValueError("arguments_json list entries must be objects")
            name = entry.get("name")
            value = entry.get("value")
            if name is not None:
                items.append((str(name), "" if value is None else str(value)))
    else:
        raise ValueError("arguments_json must be a JSON object or array")
    parts: list[str] = []
    for name, value in items:
        clean_name = str(name).strip().lstrip("-")
        if clean_name:
            parts.append(f"-{clean_name} {shlex.quote(str(value))}")
    return " ".join(parts)


def _netlify_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider=_NETLIFY.provider,
        provider_aliases=_NETLIFY.aliases,
        token_fields=_NETLIFY.group("token"),
        env_token=_NETLIFY.env_var,
        settings_token_name="netlify_access_token",
        settings_base_name="netlify_base_url",
        default_base=_NETLIFY_BASE_URL,
        tool_name=tool_name,
        display_name=_NETLIFY.display_name,
        config=config,
    )


def _uptimerobot_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str | dict[str, str]]:
    base = (
        _credential_value(
            provider=_UPTIMEROBOT.provider,
            provider_aliases=_UPTIMEROBOT.aliases,
            field_names=_UPTIMEROBOT.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("uptimerobot_base_url")
        or _UPTIMEROBOT_BASE_URL
    )
    api_key = _credential_value(
        provider=_UPTIMEROBOT.provider,
        provider_aliases=_UPTIMEROBOT.aliases,
        field_names=_UPTIMEROBOT.group("api_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("uptimerobot_api_key")
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=_UPTIMEROBOT.provider,
            field_names=_UPTIMEROBOT.hint_fields,
            tool_name=tool_name,
            env_var=_UPTIMEROBOT.env_var,
            display_name=_UPTIMEROBOT.display_name,
        )
    return _base_url(base), api_key


def _uptimerobot_request(
    endpoint: str,
    *,
    payload: dict[str, Any],
    tool_name: str,
    config: Optional[RunnableConfig],
) -> Any:
    base_url, key_or_error = _uptimerobot_config(tool_name, config)
    if isinstance(key_or_error, dict):
        return key_or_error
    if key_or_error.startswith("[Error]:"):
        return key_or_error
    data = {"api_key": key_or_error, "format": "json", **payload}
    return _request_json(
        "POST",
        f"{base_url}/{endpoint.lstrip('/')}",
        form_data=data,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Nymeria",
        },
    )


def _pagerduty_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_PAGERDUTY.provider,
            provider_aliases=_PAGERDUTY.aliases,
            field_names=_PAGERDUTY.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pagerduty_base_url")
        or _PAGERDUTY_BASE_URL
    )
    # The access-token and api-token lookups scope to different alias subsets
    # per branch, so those provider_aliases stay inline while the field tuples
    # come from the spec.
    access_token = _credential_value(
        provider=_PAGERDUTY.provider,
        provider_aliases=("pagerduty_oauth2_api",),
        field_names=_PAGERDUTY.group("access_token"),
        tool_name=tool_name,
        config=config,
    )
    api_token = (
        access_token
        or _credential_value(
            provider=_PAGERDUTY.provider,
            provider_aliases=("pagerduty_api",),
            field_names=_PAGERDUTY.group("api_token"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pagerduty_api_token")
    )
    if not api_token:
        return _base_url(base), _setup_hint(
            provider=_PAGERDUTY.provider,
            field_names=_PAGERDUTY.hint_fields,
            tool_name=tool_name,
            env_var=_PAGERDUTY.env_var,
            display_name=_PAGERDUTY.display_name,
        )
    auth_value = f"Bearer {access_token}" if access_token else f"Token token={api_token}"
    return _base_url(base), {
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Authorization": auth_value,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _pagerduty_from_email(
    *,
    tool_name: str,
    config: Optional[RunnableConfig],
    explicit: str,
) -> str | None:
    return (
        explicit.strip()
        or _credential_value(
            provider=_PAGERDUTY.provider,
            provider_aliases=("pagerduty_api",),
            field_names=_PAGERDUTY.group("from_email"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("pagerduty_from_email")
    )


def _sentry_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider=_SENTRY.provider,
        provider_aliases=_SENTRY.aliases,
        token_fields=_SENTRY.group("token"),
        env_token=_SENTRY.env_var,
        settings_token_name="sentry_auth_token",
        settings_base_name="sentry_base_url",
        default_base=_SENTRY_BASE_URL,
        tool_name=tool_name,
        display_name=_SENTRY.display_name,
        config=config,
    )


def _cloudflare_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    return _bearer_config(
        provider=_CLOUDFLARE.provider,
        provider_aliases=_CLOUDFLARE.aliases,
        token_fields=_CLOUDFLARE.group("token"),
        env_token=_CLOUDFLARE.env_var,
        settings_token_name="cloudflare_api_token",
        settings_base_name="cloudflare_base_url",
        default_base=_CLOUDFLARE_BASE_URL,
        tool_name=tool_name,
        display_name=_CLOUDFLARE.display_name,
        config=config,
    )


@tool
def netlify_list_sites(
    limit: int = 25,
    filter_mode: str = "all",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Netlify sites available to the configured token.

    Args:
        limit: Number of sites to return, 1-100.
        filter_mode: Netlify site filter, usually all.
    """
    try:
        base_url, headers_or_error = _netlify_config("netlify_list_sites", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/sites",
            params={"filter": filter_mode.strip() or "all", "per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_list_sites failed", exc_info=True)
        return f"[Error]: Netlify site listing failed: {e}"


@tool
def netlify_get_site(
    site_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Netlify site by ID, name, or domain."""
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_get_site", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/sites/{quote(site_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_get_site failed", exc_info=True)
        return f"[Error]: Netlify site lookup failed: {e}"


@tool
def netlify_list_deploys(
    site_id: str,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Netlify deploys for a site.

    Args:
        site_id: Netlify site ID, name, or domain.
        limit: Number of deploys to return, 1-100.
    """
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_list_deploys", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/sites/{quote(site_id.strip(), safe='')}/deploys",
            params={"per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_list_deploys failed", exc_info=True)
        return f"[Error]: Netlify deploy listing failed: {e}"


@tool
def netlify_get_deploy(
    site_id: str,
    deploy_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Netlify deploy by site and deploy ID."""
    if not site_id.strip() or not deploy_id.strip():
        return "[Error]: site_id and deploy_id are required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_get_deploy", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/sites/{quote(site_id.strip(), safe='')}/deploys/{quote(deploy_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_get_deploy failed", exc_info=True)
        return f"[Error]: Netlify deploy lookup failed: {e}"


@tool
def netlify_cancel_deploy(
    deploy_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Cancel a Netlify deploy by deploy ID."""
    if not deploy_id.strip():
        return "[Error]: deploy_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_cancel_deploy", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/deploys/{quote(deploy_id.strip(), safe='')}/cancel",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_cancel_deploy failed", exc_info=True)
        return f"[Error]: Netlify deploy cancellation failed: {e}"


@tool
def netlify_delete_site(
    site_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Netlify site by ID, name, or domain."""
    if not site_id.strip():
        return "[Error]: site_id is required."
    try:
        base_url, headers_or_error = _netlify_config("netlify_delete_site", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("DELETE", f"{base_url}/sites/{quote(site_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("netlify_delete_site failed", exc_info=True)
        return f"[Error]: Netlify site deletion failed: {e}"


@tool
def uptimerobot_get_account(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get UptimeRobot account quota and monitor counts."""
    try:
        data = _uptimerobot_request("getAccountDetails", payload={}, tool_name="uptimerobot_get_account", config=config)
        if isinstance(data, str):
            return data
        return _dump_json(data.get("account", data))
    except Exception as e:
        logger.error("uptimerobot_get_account failed", exc_info=True)
        return f"[Error]: UptimeRobot account lookup failed: {e}"


@tool
def uptimerobot_list_monitors(
    limit: int = 25,
    monitor_ids: str = "",
    statuses: str = "",
    types: str = "",
    include_logs: bool = False,
    include_response_times: bool = False,
    include_alert_contacts: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List UptimeRobot monitors.

    Args:
        limit: Number of monitors to return, 1-100.
        monitor_ids: Optional comma- or dash-separated monitor IDs.
        statuses: Optional comma-separated UptimeRobot status codes.
        types: Optional comma-separated UptimeRobot monitor type codes.
        include_logs: Include monitor logs.
        include_response_times: Include response time data.
        include_alert_contacts: Include alert contacts.
    """
    try:
        payload: dict[str, Any] = {"limit": _limit(limit, max_value=100)}
        if monitor_ids.strip():
            payload["monitors"] = _csv_to_dash(monitor_ids)
        if statuses.strip():
            payload["statuses"] = _csv_to_dash(statuses)
        if types.strip():
            payload["types"] = _csv_to_dash(types)
        if include_logs:
            payload["logs"] = 1
        if include_response_times:
            payload["response_times"] = 1
        if include_alert_contacts:
            payload["alert_contacts"] = 1
        data = _uptimerobot_request(
            "getMonitors",
            payload=payload,
            tool_name="uptimerobot_list_monitors",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitors", data))
    except Exception as e:
        logger.error("uptimerobot_list_monitors failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor listing failed: {e}"


@tool
def uptimerobot_get_monitor(
    monitor_id: str,
    include_logs: bool = False,
    include_response_times: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an UptimeRobot monitor by ID."""
    if not monitor_id.strip():
        return "[Error]: monitor_id is required."
    try:
        payload: dict[str, Any] = {"monitors": monitor_id.strip()}
        if include_logs:
            payload["logs"] = 1
        if include_response_times:
            payload["response_times"] = 1
        data = _uptimerobot_request(
            "getMonitors",
            payload=payload,
            tool_name="uptimerobot_get_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitors", data))
    except Exception as e:
        logger.error("uptimerobot_get_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor lookup failed: {e}"


@tool
def uptimerobot_create_monitor(
    friendly_name: str,
    url: str,
    monitor_type: int = 1,
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an UptimeRobot monitor.

    Args:
        friendly_name: Monitor display name.
        url: URL, IP, or heartbeat URL target.
        monitor_type: UptimeRobot type code, e.g. 1 HTTP(S), 2 keyword, 3 ping, 4 port, 5 heartbeat.
        fields_json: Additional UptimeRobot newMonitor fields as JSON.
    """
    if not friendly_name.strip() or not url.strip():
        return "[Error]: friendly_name and url are required."
    try:
        payload = {
            "friendly_name": friendly_name.strip(),
            "url": url.strip(),
            "type": int(monitor_type),
            **_parse_json(fields_json, expected=dict, label="fields_json"),
        }
        data = _uptimerobot_request(
            "newMonitor",
            payload=payload,
            tool_name="uptimerobot_create_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_create_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor creation failed: {e}"


@tool
def uptimerobot_update_monitor(
    monitor_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an UptimeRobot monitor.

    Args:
        monitor_id: Monitor ID.
        fields_json: UptimeRobot editMonitor fields as JSON.
    """
    if not monitor_id.strip() or not fields_json.strip():
        return "[Error]: monitor_id and fields_json are required."
    try:
        payload = {"id": monitor_id.strip(), **_parse_json(fields_json, expected=dict, label="fields_json")}
        data = _uptimerobot_request(
            "editMonitor",
            payload=payload,
            tool_name="uptimerobot_update_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_update_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor update failed: {e}"


@tool
def uptimerobot_delete_monitor(
    monitor_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an UptimeRobot monitor."""
    if not monitor_id.strip():
        return "[Error]: monitor_id is required."
    try:
        data = _uptimerobot_request(
            "deleteMonitor",
            payload={"id": monitor_id.strip()},
            tool_name="uptimerobot_delete_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_delete_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor deletion failed: {e}"


@tool
def uptimerobot_reset_monitor(
    monitor_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Reset an UptimeRobot monitor's stats."""
    if not monitor_id.strip():
        return "[Error]: monitor_id is required."
    try:
        data = _uptimerobot_request(
            "resetMonitor",
            payload={"id": monitor_id.strip()},
            tool_name="uptimerobot_reset_monitor",
            config=config,
        )
        if isinstance(data, str):
            return data
        return _dump_json(data.get("monitor", data))
    except Exception as e:
        logger.error("uptimerobot_reset_monitor failed", exc_info=True)
        return f"[Error]: UptimeRobot monitor reset failed: {e}"


@tool
def pagerduty_list_incidents(
    limit: int = 25,
    statuses: str = "",
    service_ids: str = "",
    user_ids: str = "",
    since: str = "",
    until: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List PagerDuty incidents.

    Args:
        limit: Number of incidents to return, 1-100.
        statuses: Optional comma-separated statuses, e.g. triggered,acknowledged,resolved.
        service_ids: Optional comma-separated service IDs.
        user_ids: Optional comma-separated user IDs.
        since: Optional ISO timestamp lower bound.
        until: Optional ISO timestamp upper bound.
    """
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_list_incidents", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {"limit": _limit(limit, max_value=100), "total": "false"}
        if statuses.strip():
            params["statuses[]"] = _csv_to_list(statuses)
        if service_ids.strip():
            params["service_ids[]"] = _csv_to_list(service_ids)
        if user_ids.strip():
            params["user_ids[]"] = _csv_to_list(user_ids)
        if since.strip():
            params["since"] = since.strip()
        if until.strip():
            params["until"] = until.strip()
        data = _request_json("GET", f"{base_url}/incidents", params=params, headers=headers_or_error)
        return _dump_json(data.get("incidents", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_list_incidents failed", exc_info=True)
        return f"[Error]: PagerDuty incident listing failed: {e}"


@tool
def pagerduty_get_incident(
    incident_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a PagerDuty incident by ID."""
    if not incident_id.strip():
        return "[Error]: incident_id is required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_get_incident", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/incidents/{quote(incident_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("incident", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_get_incident failed", exc_info=True)
        return f"[Error]: PagerDuty incident lookup failed: {e}"


@tool
def pagerduty_create_incident(
    title: str,
    service_id: str,
    from_email: str = "",
    urgency: str = "",
    details: str = "",
    incident_key: str = "",
    priority_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a PagerDuty incident.

    Args:
        title: Incident title.
        service_id: PagerDuty service ID.
        from_email: Optional PagerDuty account email for the From header.
        urgency: Optional high or low.
        details: Optional incident body details.
        incident_key: Optional deduplication key.
        priority_id: Optional priority ID.
    """
    if not title.strip() or not service_id.strip():
        return "[Error]: title and service_id are required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_create_incident", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        headers = dict(headers_or_error)
        email = _pagerduty_from_email(tool_name="pagerduty_create_incident", config=config, explicit=from_email)
        if email:
            headers["From"] = email
        incident: dict[str, Any] = {
            "type": "incident",
            "title": title.strip(),
            "service": {"id": service_id.strip(), "type": "service_reference"},
        }
        if details.strip():
            incident["body"] = {"type": "incident_body", "details": details.strip()}
        if urgency.strip():
            incident["urgency"] = urgency.strip()
        if incident_key.strip():
            incident["incident_key"] = incident_key.strip()
        if priority_id.strip():
            incident["priority"] = {"id": priority_id.strip(), "type": "priority_reference"}
        data = _request_json("POST", f"{base_url}/incidents", json_body={"incident": incident}, headers=headers)
        return _dump_json(data.get("incident", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_create_incident failed", exc_info=True)
        return f"[Error]: PagerDuty incident creation failed: {e}"


@tool
def pagerduty_update_incident(
    incident_id: str,
    from_email: str = "",
    status: str = "",
    title: str = "",
    urgency: str = "",
    resolution: str = "",
    details: str = "",
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a PagerDuty incident.

    Args:
        incident_id: PagerDuty incident ID.
        from_email: Optional PagerDuty account email for the From header.
        status: Optional status such as acknowledged or resolved.
        title: Optional new title.
        urgency: Optional high or low.
        resolution: Optional resolution text when resolving.
        details: Optional body details.
        fields_json: Additional incident update fields as JSON.
    """
    if not incident_id.strip():
        return "[Error]: incident_id is required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_update_incident", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        headers = dict(headers_or_error)
        email = _pagerduty_from_email(tool_name="pagerduty_update_incident", config=config, explicit=from_email)
        if email:
            headers["From"] = email
        incident = {"type": "incident", **_parse_json(fields_json, expected=dict, label="fields_json")}
        if status.strip():
            incident["status"] = status.strip()
        if title.strip():
            incident["title"] = title.strip()
        if urgency.strip():
            incident["urgency"] = urgency.strip()
        if resolution.strip():
            incident["resolution"] = resolution.strip()
        if details.strip():
            incident["body"] = {"type": "incident_body", "details": details.strip()}
        data = _request_json(
            "PUT",
            f"{base_url}/incidents/{quote(incident_id.strip(), safe='')}",
            json_body={"incident": incident},
            headers=headers,
        )
        return _dump_json(data.get("incident", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_update_incident failed", exc_info=True)
        return f"[Error]: PagerDuty incident update failed: {e}"


@tool
def pagerduty_add_incident_note(
    incident_id: str,
    content: str,
    from_email: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a note to a PagerDuty incident."""
    if not incident_id.strip() or not content.strip():
        return "[Error]: incident_id and content are required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_add_incident_note", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        headers = dict(headers_or_error)
        email = _pagerduty_from_email(tool_name="pagerduty_add_incident_note", config=config, explicit=from_email)
        if email:
            headers["From"] = email
        data = _request_json(
            "POST",
            f"{base_url}/incidents/{quote(incident_id.strip(), safe='')}/notes",
            json_body={"note": {"content": content.strip()}},
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("pagerduty_add_incident_note failed", exc_info=True)
        return f"[Error]: PagerDuty incident note creation failed: {e}"


@tool
def pagerduty_list_services(
    limit: int = 25,
    query: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List PagerDuty services."""
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_list_services", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/services",
            params={"limit": _limit(limit, max_value=100), "query": query.strip()},
            headers=headers_or_error,
        )
        return _dump_json(data.get("services", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_list_services failed", exc_info=True)
        return f"[Error]: PagerDuty service listing failed: {e}"


@tool
def pagerduty_get_user(
    user_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a PagerDuty user by ID."""
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        base_url, headers_or_error = _pagerduty_config("pagerduty_get_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/users/{quote(user_id.strip(), safe='')}", headers=headers_or_error)
        return _dump_json(data.get("user", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("pagerduty_get_user failed", exc_info=True)
        return f"[Error]: PagerDuty user lookup failed: {e}"


@tool
def sentry_list_organizations(
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Sentry organizations available to the token."""
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_organizations", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/organizations/",
            params={"limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_organizations failed", exc_info=True)
        return f"[Error]: Sentry organization listing failed: {e}"


@tool
def sentry_list_projects(
    organization_slug: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Sentry projects, optionally scoped to an organization."""
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_projects", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        path = (
            f"/api/0/organizations/{quote(organization_slug.strip(), safe='')}/projects/"
            if organization_slug.strip()
            else "/api/0/projects/"
        )
        data = _request_json(
            "GET",
            f"{base_url}{path}",
            params={"limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_projects failed", exc_info=True)
        return f"[Error]: Sentry project listing failed: {e}"


@tool
def sentry_list_project_issues(
    organization_slug: str,
    project_slug: str,
    query: str = "",
    stats_period: str = "24h",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List issues for a Sentry project."""
    if not organization_slug.strip() or not project_slug.strip():
        return "[Error]: organization_slug and project_slug are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_project_issues", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/projects/{quote(organization_slug.strip(), safe='')}/{quote(project_slug.strip(), safe='')}/issues/",
            params={
                "query": query.strip(),
                "statsPeriod": stats_period.strip() or "24h",
                "limit": _limit(limit, max_value=100),
            },
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_project_issues failed", exc_info=True)
        return f"[Error]: Sentry issue listing failed: {e}"


@tool
def sentry_get_issue(
    issue_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Sentry issue by issue/group ID."""
    if not issue_id.strip():
        return "[Error]: issue_id is required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_get_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json("GET", f"{base_url}/api/0/issues/{quote(issue_id.strip(), safe='')}/", headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_get_issue failed", exc_info=True)
        return f"[Error]: Sentry issue lookup failed: {e}"


@tool
def sentry_update_issue(
    organization_slug: str,
    issue_id: str,
    status: str = "",
    assigned_to: str = "",
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Sentry issue.

    Args:
        organization_slug: Sentry organization slug.
        issue_id: Sentry issue/group ID.
        status: Optional status such as resolved or unresolved.
        assigned_to: Optional assignee identifier.
        fields_json: Additional Sentry issue attributes as JSON.
    """
    if not organization_slug.strip() or not issue_id.strip():
        return "[Error]: organization_slug and issue_id are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_update_issue", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = _parse_json(fields_json, expected=dict, label="fields_json")
        if status.strip():
            body["status"] = status.strip()
        if assigned_to.strip():
            body["assignedTo"] = assigned_to.strip()
        data = _request_json(
            "PUT",
            f"{base_url}/api/0/organizations/{quote(organization_slug.strip(), safe='')}/issues/{quote(issue_id.strip(), safe='')}/",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_update_issue failed", exc_info=True)
        return f"[Error]: Sentry issue update failed: {e}"


@tool
def sentry_list_project_events(
    organization_slug: str,
    project_slug: str,
    full: bool = False,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List events for a Sentry project."""
    if not organization_slug.strip() or not project_slug.strip():
        return "[Error]: organization_slug and project_slug are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_list_project_events", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/projects/{quote(organization_slug.strip(), safe='')}/{quote(project_slug.strip(), safe='')}/events/",
            params={"full": str(bool(full)).lower(), "limit": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_list_project_events failed", exc_info=True)
        return f"[Error]: Sentry event listing failed: {e}"


@tool
def sentry_get_event(
    organization_slug: str,
    project_slug: str,
    event_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Sentry event by project and event ID."""
    if not organization_slug.strip() or not project_slug.strip() or not event_id.strip():
        return "[Error]: organization_slug, project_slug, and event_id are required."
    try:
        base_url, headers_or_error = _sentry_config("sentry_get_event", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/0/projects/{quote(organization_slug.strip(), safe='')}/{quote(project_slug.strip(), safe='')}/events/{quote(event_id.strip(), safe='')}/",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("sentry_get_event failed", exc_info=True)
        return f"[Error]: Sentry event lookup failed: {e}"


@tool
def cloudflare_list_zones(
    name: str = "",
    status: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Cloudflare zones."""
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_list_zones", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones",
            params={"name": name.strip(), "status": status.strip(), "per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_list_zones failed", exc_info=True)
        return f"[Error]: Cloudflare zone listing failed: {e}"


@tool
def cloudflare_list_dns_records(
    zone_id: str,
    name: str = "",
    record_type: str = "",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Cloudflare DNS records for a zone."""
    if not zone_id.strip():
        return "[Error]: zone_id is required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_list_dns_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records",
            params={"name": name.strip(), "type": record_type.strip().upper(), "per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_list_dns_records failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record listing failed: {e}"


@tool
def cloudflare_create_dns_record(
    zone_id: str,
    record_type: str,
    name: str,
    content: str,
    ttl: int = 1,
    proxied: bool = False,
    fields_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Cloudflare DNS record.

    Args:
        zone_id: Cloudflare zone ID.
        record_type: DNS record type such as A, AAAA, CNAME, TXT, MX.
        name: DNS record name.
        content: DNS record content.
        ttl: TTL in seconds; 1 means automatic in Cloudflare.
        proxied: Whether the record is proxied through Cloudflare.
        fields_json: Additional Cloudflare DNS record fields as JSON.
    """
    if not zone_id.strip() or not record_type.strip() or not name.strip() or not content.strip():
        return "[Error]: zone_id, record_type, name, and content are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_create_dns_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {
            "type": record_type.strip().upper(),
            "name": name.strip(),
            "content": content.strip(),
            "ttl": int(ttl),
            "proxied": bool(proxied),
            **_parse_json(fields_json, expected=dict, label="fields_json"),
        }
        data = _request_json(
            "POST",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_create_dns_record failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record creation failed: {e}"


@tool
def cloudflare_update_dns_record(
    zone_id: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Patch a Cloudflare DNS record.

    Args:
        zone_id: Cloudflare zone ID.
        record_id: DNS record ID.
        fields_json: Cloudflare DNS record fields to patch as JSON.
    """
    if not zone_id.strip() or not record_id.strip() or not fields_json.strip():
        return "[Error]: zone_id, record_id, and fields_json are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_update_dns_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PATCH",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records/{quote(record_id.strip(), safe='')}",
            json_body=_parse_json(fields_json, expected=dict, label="fields_json"),
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_update_dns_record failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record update failed: {e}"


@tool
def cloudflare_delete_dns_record(
    zone_id: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Cloudflare DNS record."""
    if not zone_id.strip() or not record_id.strip():
        return "[Error]: zone_id and record_id are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_delete_dns_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/dns_records/{quote(record_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_delete_dns_record failed", exc_info=True)
        return f"[Error]: Cloudflare DNS record deletion failed: {e}"


@tool
def cloudflare_list_origin_certificates(
    zone_id: str,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Cloudflare zone-level authenticated origin pull certificates."""
    if not zone_id.strip():
        return "[Error]: zone_id is required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_list_origin_certificates", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth",
            params={"per_page": _limit(limit, max_value=100)},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_list_origin_certificates failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate listing failed: {e}"


@tool
def cloudflare_get_origin_certificate(
    zone_id: str,
    certificate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Cloudflare zone-level authenticated origin pull certificate."""
    if not zone_id.strip() or not certificate_id.strip():
        return "[Error]: zone_id and certificate_id are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_get_origin_certificate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth/{quote(certificate_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_get_origin_certificate failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate lookup failed: {e}"


@tool
def cloudflare_upload_origin_certificate(
    zone_id: str,
    certificate_pem: str,
    private_key_pem: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload a Cloudflare zone-level authenticated origin pull certificate."""
    if not zone_id.strip() or not certificate_pem.strip() or not private_key_pem.strip():
        return "[Error]: zone_id, certificate_pem, and private_key_pem are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_upload_origin_certificate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth",
            json_body={"certificate": certificate_pem, "private_key": private_key_pem},
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_upload_origin_certificate failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate upload failed: {e}"


@tool
def cloudflare_delete_origin_certificate(
    zone_id: str,
    certificate_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Cloudflare zone-level authenticated origin pull certificate."""
    if not zone_id.strip() or not certificate_id.strip():
        return "[Error]: zone_id and certificate_id are required."
    try:
        base_url, headers_or_error = _cloudflare_config("cloudflare_delete_origin_certificate", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/zones/{quote(zone_id.strip(), safe='')}/origin_tls_client_auth/{quote(certificate_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data.get("result", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("cloudflare_delete_origin_certificate failed", exc_info=True)
        return f"[Error]: Cloudflare origin certificate deletion failed: {e}"


@tool
def grafana_search_dashboards(
    query: str = "",
    tag: str = "",
    folder_ids: str = "",
    starred: bool = False,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Grafana dashboards.

    Args:
        query: Optional search text.
        tag: Optional dashboard tag filter.
        folder_ids: Optional comma-separated folder IDs.
        starred: Only return starred dashboards.
        limit: Maximum dashboards to return, 1-500.
    """
    try:
        base_url, headers_or_error = _grafana_config("grafana_search_dashboards", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {
            "type": "dash-db",
            "query": query.strip(),
            "tag": tag.strip(),
            "starred": str(bool(starred)).lower() if starred else "",
            "limit": _limit(limit, max_value=500),
        }
        folders = _csv_to_list(folder_ids)
        if folders:
            params["folderIds"] = folders
        data = _request_json(
            "GET",
            f"{base_url}/search",
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("grafana_search_dashboards failed", exc_info=True)
        return f"[Error]: Grafana dashboard search failed: {e}"


@tool
def grafana_get_dashboard(
    dashboard_uid: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Grafana dashboard by UID."""
    if not dashboard_uid.strip():
        return "[Error]: dashboard_uid is required."
    try:
        base_url, headers_or_error = _grafana_config("grafana_get_dashboard", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/dashboards/uid/{quote(dashboard_uid.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("grafana_get_dashboard failed", exc_info=True)
        return f"[Error]: Grafana dashboard lookup failed: {e}"


@tool
def grafana_create_dashboard(
    title: str,
    folder_uid: str = "",
    dashboard_json: str = "",
    overwrite: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create or update a Grafana dashboard.

    Args:
        title: Dashboard title. Used when dashboard_json is empty.
        folder_uid: Optional Grafana folder UID.
        dashboard_json: Optional full Grafana dashboard JSON object.
        overwrite: Whether Grafana may overwrite an existing dashboard.
    """
    if not title.strip() and not dashboard_json.strip():
        return "[Error]: title or dashboard_json is required."
    try:
        base_url, headers_or_error = _grafana_config("grafana_create_dashboard", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        dashboard = (
            _parse_json(dashboard_json, expected=dict, label="dashboard_json")
            if dashboard_json.strip()
            else {"id": None, "title": title.strip()}
        )
        body: dict[str, Any] = {"dashboard": dashboard, "overwrite": bool(overwrite)}
        if folder_uid.strip():
            body["folderUid"] = folder_uid.strip()
        data = _request_json(
            "POST",
            f"{base_url}/dashboards/db",
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("grafana_create_dashboard failed", exc_info=True)
        return f"[Error]: Grafana dashboard creation failed: {e}"


@tool
def grafana_delete_dashboard(
    dashboard_uid: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Grafana dashboard by UID."""
    if not dashboard_uid.strip():
        return "[Error]: dashboard_uid is required."
    try:
        base_url, headers_or_error = _grafana_config("grafana_delete_dashboard", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/dashboards/uid/{quote(dashboard_uid.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("grafana_delete_dashboard failed", exc_info=True)
        return f"[Error]: Grafana dashboard deletion failed: {e}"


@tool
def grafana_list_teams(
    query: str = "",
    limit: int = 50,
    page: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Grafana teams."""
    try:
        base_url, headers_or_error = _grafana_config("grafana_list_teams", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/teams/search",
            params={"query": query.strip(), "perpage": _limit(limit, max_value=500), "page": max(1, int(page))},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("grafana_list_teams failed", exc_info=True)
        return f"[Error]: Grafana team listing failed: {e}"


@tool
def metabase_list_questions(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Metabase questions/cards."""
    try:
        base_url, headers_or_error = _metabase_config("metabase_list_questions", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/card",
            headers=headers_or_error,
        )
        if isinstance(data, list):
            data = data[: _limit(limit, max_value=500)]
        return _dump_json(data)
    except Exception as e:
        logger.error("metabase_list_questions failed", exc_info=True)
        return f"[Error]: Metabase question listing failed: {e}"


@tool
def metabase_get_question(
    question_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Metabase question/card by ID."""
    if not question_id.strip():
        return "[Error]: question_id is required."
    try:
        base_url, headers_or_error = _metabase_config("metabase_get_question", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/card/{quote(question_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("metabase_get_question failed", exc_info=True)
        return f"[Error]: Metabase question lookup failed: {e}"


@tool
def metabase_query_question(
    question_id: str,
    parameters_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a Metabase question/card and return JSON results.

    Args:
        question_id: Metabase card/question ID.
        parameters_json: Optional Metabase parameter array as JSON.
    """
    if not question_id.strip():
        return "[Error]: question_id is required."
    try:
        base_url, headers_or_error = _metabase_config("metabase_query_question", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        parameters = _parse_json(parameters_json, expected=list, label="parameters_json") if parameters_json.strip() else []
        data = _request_json(
            "POST",
            f"{base_url}/api/card/{quote(question_id.strip(), safe='')}/query/json",
            json_body={"parameters": parameters},
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("metabase_query_question failed", exc_info=True)
        return f"[Error]: Metabase question query failed: {e}"


@tool
def metabase_list_dashboards(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Metabase dashboards."""
    try:
        base_url, headers_or_error = _metabase_config("metabase_list_dashboards", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/dashboard",
            headers=headers_or_error,
        )
        if isinstance(data, list):
            data = data[: _limit(limit, max_value=500)]
        return _dump_json(data)
    except Exception as e:
        logger.error("metabase_list_dashboards failed", exc_info=True)
        return f"[Error]: Metabase dashboard listing failed: {e}"


@tool
def metabase_get_dashboard(
    dashboard_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Metabase dashboard by ID."""
    if not dashboard_id.strip():
        return "[Error]: dashboard_id is required."
    try:
        base_url, headers_or_error = _metabase_config("metabase_get_dashboard", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/dashboard/{quote(dashboard_id.strip(), safe='')}",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("metabase_get_dashboard failed", exc_info=True)
        return f"[Error]: Metabase dashboard lookup failed: {e}"


@tool
def elasticsearch_list_indices(
    index_pattern: str = "*",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Elasticsearch indices."""
    try:
        base_url, headers_or_error, auth, verify = _elasticsearch_config("elasticsearch_list_indices", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/_cat/indices/{quote(index_pattern.strip() or '*', safe='*')}",
            params={"format": "json"},
            headers=headers_or_error,
            auth=auth,
            verify=verify,
        )
        if isinstance(data, list):
            data = data[: _limit(limit, max_value=1000)]
        return _dump_json(data)
    except Exception as e:
        logger.error("elasticsearch_list_indices failed", exc_info=True)
        return f"[Error]: Elasticsearch index listing failed: {e}"


@tool
def elasticsearch_search(
    index: str,
    query_json: str = "",
    limit: int = 10,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Elasticsearch documents.

    Args:
        index: Index or index pattern to search.
        query_json: Optional Elasticsearch query body JSON.
        limit: Number of hits to request when query_json does not specify size.
    """
    if not index.strip():
        return "[Error]: index is required."
    try:
        base_url, headers_or_error, auth, verify = _elasticsearch_config("elasticsearch_search", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body: dict[str, Any] = _parse_json(query_json, expected=dict, label="query_json") if query_json.strip() else {"query": {"match_all": {}}}
        body.setdefault("size", _limit(limit, max_value=1000))
        data = _request_json(
            "POST",
            f"{base_url}/{quote(index.strip(), safe='*,')}/_search",
            json_body=body,
            headers=headers_or_error,
            auth=auth,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("elasticsearch_search failed", exc_info=True)
        return f"[Error]: Elasticsearch search failed: {e}"


@tool
def elasticsearch_get_document(
    index: str,
    document_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Elasticsearch document by ID."""
    if not index.strip() or not document_id.strip():
        return "[Error]: index and document_id are required."
    try:
        base_url, headers_or_error, auth, verify = _elasticsearch_config("elasticsearch_get_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/{quote(index.strip(), safe='')}/_doc/{quote(document_id.strip(), safe='')}",
            headers=headers_or_error,
            auth=auth,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("elasticsearch_get_document failed", exc_info=True)
        return f"[Error]: Elasticsearch document lookup failed: {e}"


@tool
def elasticsearch_index_document(
    index: str,
    document_json: str,
    document_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create or replace an Elasticsearch document."""
    if not index.strip() or not document_json.strip():
        return "[Error]: index and document_json are required."
    try:
        base_url, headers_or_error, auth, verify = _elasticsearch_config("elasticsearch_index_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        path = f"{base_url}/{quote(index.strip(), safe='')}/_doc"
        method = "POST"
        if document_id.strip():
            path += f"/{quote(document_id.strip(), safe='')}"
            method = "PUT"
        data = _request_json(
            method,
            path,
            json_body=_parse_json(document_json, expected=dict, label="document_json"),
            headers=headers_or_error,
            auth=auth,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("elasticsearch_index_document failed", exc_info=True)
        return f"[Error]: Elasticsearch document indexing failed: {e}"


@tool
def elasticsearch_delete_document(
    index: str,
    document_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Elasticsearch document by ID."""
    if not index.strip() or not document_id.strip():
        return "[Error]: index and document_id are required."
    try:
        base_url, headers_or_error, auth, verify = _elasticsearch_config("elasticsearch_delete_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            f"{base_url}/{quote(index.strip(), safe='')}/_doc/{quote(document_id.strip(), safe='')}",
            headers=headers_or_error,
            auth=auth,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("elasticsearch_delete_document failed", exc_info=True)
        return f"[Error]: Elasticsearch document deletion failed: {e}"


@tool
def splunk_list_saved_searches(
    search: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Splunk saved searches."""
    try:
        base_url, headers_or_error, verify = _splunk_config("splunk_list_saved_searches", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/servicesNS/-/-/saved/searches",
            params={"output_mode": "json", "search": search.strip(), "count": _limit(limit, max_value=500)},
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("splunk_list_saved_searches failed", exc_info=True)
        return f"[Error]: Splunk saved-search listing failed: {e}"


@tool
def splunk_create_search_job(
    search_query: str,
    earliest_time: str = "",
    latest_time: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Splunk search job."""
    if not search_query.strip():
        return "[Error]: search_query is required."
    try:
        base_url, headers_or_error, verify = _splunk_config("splunk_create_search_job", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        form_data = {"search": search_query.strip(), "output_mode": "json"}
        if earliest_time.strip():
            form_data["earliest_time"] = earliest_time.strip()
        if latest_time.strip():
            form_data["latest_time"] = latest_time.strip()
        data = _request_json(
            "POST",
            f"{base_url}/services/search/jobs",
            form_data=form_data,
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("splunk_create_search_job failed", exc_info=True)
        return f"[Error]: Splunk search job creation failed: {e}"


@tool
def splunk_get_search_job(
    search_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Splunk search job by SID."""
    if not search_id.strip():
        return "[Error]: search_id is required."
    try:
        base_url, headers_or_error, verify = _splunk_config("splunk_get_search_job", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/services/search/jobs/{quote(search_id.strip(), safe='')}",
            params={"output_mode": "json"},
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("splunk_get_search_job failed", exc_info=True)
        return f"[Error]: Splunk search job lookup failed: {e}"


@tool
def splunk_get_search_results(
    search_id: str,
    limit: int = 100,
    offset: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Splunk search job results."""
    if not search_id.strip():
        return "[Error]: search_id is required."
    try:
        base_url, headers_or_error, verify = _splunk_config("splunk_get_search_results", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/services/search/jobs/{quote(search_id.strip(), safe='')}/results",
            params={"output_mode": "json", "count": _limit(limit, max_value=1000), "offset": max(0, int(offset))},
            headers=headers_or_error,
            verify=verify,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("splunk_get_search_results failed", exc_info=True)
        return f"[Error]: Splunk search results lookup failed: {e}"


@tool
def rundeck_get_job_metadata(
    job_id: str,
    api_version: int = 18,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Rundeck job metadata by job ID.

    Args:
        job_id: Rundeck job UUID.
        api_version: Rundeck API version to use. Defaults to 18.
    """
    job_id = job_id.strip()
    if not job_id:
        return "[Error]: job_id is required."
    try:
        base_url, headers_or_error = _rundeck_config("rundeck_get_job_metadata", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        version = max(14, int(api_version or 18))
        data = _request_json(
            "GET",
            f"{base_url}/api/{version}/job/{quote(job_id, safe='')}/info",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("rundeck_get_job_metadata failed", exc_info=True)
        return f"[Error]: Rundeck job metadata lookup failed: {e}"


@tool
def rundeck_execute_job(
    job_id: str,
    arguments_json: str = "",
    node_filter: str = "",
    api_version: int = 14,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Execute a Rundeck job.

    Args:
        job_id: Rundeck job UUID.
        arguments_json: Optional JSON object of option names to values, or list of {"name","value"} objects.
        node_filter: Optional Rundeck node filter.
        api_version: Rundeck API version to use. Defaults to 14.
    """
    job_id = job_id.strip()
    if not job_id:
        return "[Error]: job_id is required."
    try:
        base_url, headers_or_error = _rundeck_config("rundeck_execute_job", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        version = max(14, int(api_version or 14))
        body = {"argString": _rundeck_arg_string(arguments_json)}
        data = _request_json(
            "POST",
            f"{base_url}/api/{version}/job/{quote(job_id, safe='')}/run",
            params={"filter": node_filter.strip()},
            json_body=body,
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("rundeck_execute_job failed", exc_info=True)
        return f"[Error]: Rundeck job execution failed: {e}"


OPERATIONS_MONITORING_SERVICE_TOOLS = [
    netlify_list_sites,
    netlify_get_site,
    netlify_list_deploys,
    netlify_get_deploy,
    netlify_cancel_deploy,
    netlify_delete_site,
    uptimerobot_get_account,
    uptimerobot_list_monitors,
    uptimerobot_get_monitor,
    uptimerobot_create_monitor,
    uptimerobot_update_monitor,
    uptimerobot_delete_monitor,
    uptimerobot_reset_monitor,
    pagerduty_list_incidents,
    pagerduty_get_incident,
    pagerduty_create_incident,
    pagerduty_update_incident,
    pagerduty_add_incident_note,
    pagerduty_list_services,
    pagerduty_get_user,
    sentry_list_organizations,
    sentry_list_projects,
    sentry_list_project_issues,
    sentry_get_issue,
    sentry_update_issue,
    sentry_list_project_events,
    sentry_get_event,
    cloudflare_list_zones,
    cloudflare_list_dns_records,
    cloudflare_create_dns_record,
    cloudflare_update_dns_record,
    cloudflare_delete_dns_record,
    cloudflare_list_origin_certificates,
    cloudflare_get_origin_certificate,
    cloudflare_upload_origin_certificate,
    cloudflare_delete_origin_certificate,
    grafana_search_dashboards,
    grafana_get_dashboard,
    grafana_create_dashboard,
    grafana_delete_dashboard,
    grafana_list_teams,
    metabase_list_questions,
    metabase_get_question,
    metabase_query_question,
    metabase_list_dashboards,
    metabase_get_dashboard,
    elasticsearch_list_indices,
    elasticsearch_search,
    elasticsearch_get_document,
    elasticsearch_index_document,
    elasticsearch_delete_document,
    splunk_list_saved_searches,
    splunk_create_search_job,
    splunk_get_search_job,
    splunk_get_search_results,
    rundeck_get_job_metadata,
    rundeck_execute_job,
]
