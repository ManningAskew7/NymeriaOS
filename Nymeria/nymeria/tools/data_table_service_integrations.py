"""No-code database and document-table service tools."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 80_000
_BASEROW_BASE_URL = "https://api.baserow.io"
_NOCODB_BASE_URL = "https://app.nocodb.com"
_CODA_BASE_URL = "https://coda.io/apis/v1"
_GRIST_BASE_URL = "https://docs.getgrist.com/api"


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


def _limit(value: int, *, default: int = 50, max_value: int = 500) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


def _parse_json(value: str, *, expected: type, label: str) -> Any:
    if not value.strip():
        return {} if expected is dict else []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} must be valid JSON: {e}") from e
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} must be a JSON {expected.__name__}.")
    return parsed


def _csv_to_list(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


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
            if isinstance(body, dict):
                detail = (
                    body.get("message")
                    or body.get("error")
                    or body.get("error_description")
                    or body.get("detail")
                    or body.get("title")
                    or ""
                )
                errors = body.get("errors") or body.get("fieldErrors")
                if not detail and isinstance(errors, list):
                    detail = "; ".join(str(item) for item in errors[:3])
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


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
    field_names: tuple[str, ...] = (
        "api_key",
        "apiKey",
        "api_token",
        "apiToken",
        "access_token",
        "accessToken",
        "token",
        "value",
    ),
) -> tuple[str, str | None]:
    base = (
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=("base_url", "host", "url", "api_url", "apiUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_base_name)
        or default_base
    )
    api_key = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(settings_key_name)
    if not api_key:
        return _base_url(base), _setup_hint(
            provider=provider,
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return _base_url(base), api_key


def _json_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _bearer_headers(api_key: str) -> dict[str, str]:
    headers = _json_headers()
    headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _baserow_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider="baserow",
        provider_aliases=("baserow_api", "baserowApi", "baserow_token_api", "baserowTokenApi"),
        env_var="BASEROW_API_TOKEN",
        settings_key_name="baserow_api_token",
        settings_base_name="baserow_base_url",
        default_base=_BASEROW_BASE_URL,
        tool_name=tool_name,
        display_name="Baserow",
        config=config,
        field_names=("token", "api_token", "apiToken", "api_key", "apiKey", "database_token", "databaseToken"),
    )
    if not api_key or api_key.startswith("[Error]:"):
        return base_url, api_key or ""
    headers = _json_headers()
    headers["Authorization"] = f"Token {api_key}"
    return base_url, headers


def _nocodb_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider="nocodb",
        provider_aliases=("noco_db", "nocoDb", "nocodb_api_token", "noco_db_api_token", "nocoDbApiToken"),
        env_var="NOCODB_API_TOKEN",
        settings_key_name="nocodb_api_token",
        settings_base_name="nocodb_base_url",
        default_base=_NOCODB_BASE_URL,
        tool_name=tool_name,
        display_name="NocoDB",
        config=config,
        field_names=("api_token", "apiToken", "token", "access_token", "accessToken", "api_key", "apiKey", "value"),
    )
    if not api_key or api_key.startswith("[Error]:"):
        return base_url, api_key or ""
    header_name = (
        _credential_value(
            provider="nocodb",
            provider_aliases=("noco_db", "nocoDb", "nocodb_api_token", "noco_db_api_token", "nocoDbApiToken"),
            field_names=("auth_header", "authHeader", "header_name", "headerName"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("nocodb_auth_header")
        or "xc-token"
    )
    if header_name not in {"xc-token", "xc-auth"}:
        return base_url, "[Error]: NocoDB auth header must be xc-token or xc-auth."
    headers = _json_headers()
    headers[header_name] = api_key
    return base_url, headers


def _coda_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider="coda",
        provider_aliases=("coda_api", "codaApi"),
        env_var="CODA_API_TOKEN",
        settings_key_name="coda_api_token",
        settings_base_name="coda_base_url",
        default_base=_CODA_BASE_URL,
        tool_name=tool_name,
        display_name="Coda",
        config=config,
        field_names=("access_token", "accessToken", "api_token", "apiToken", "api_key", "apiKey", "token", "value"),
    )
    if not api_key or api_key.startswith("[Error]:"):
        return base_url, api_key or ""
    return base_url, _bearer_headers(api_key)


def _grist_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base_url, api_key = _api_key_config(
        provider="grist",
        provider_aliases=("grist_api", "gristApi"),
        env_var="GRIST_API_KEY",
        settings_key_name="grist_api_key",
        settings_base_name="grist_base_url",
        default_base=_GRIST_BASE_URL,
        tool_name=tool_name,
        display_name="Grist",
        config=config,
    )
    if not api_key or api_key.startswith("[Error]:"):
        return base_url, api_key or ""
    return base_url, _bearer_headers(api_key)


def _cells_from_mapping(cells_json: str) -> list[dict[str, Any]]:
    cells = _parse_json(cells_json, expected=dict, label="cells_json")
    return [{"column": column, "value": value} for column, value in cells.items()]


@tool
def baserow_list_tables(
    database_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Baserow tables available to the credential.

    Args:
        database_id: Optional database ID. When omitted, uses the all-tables endpoint.
    """
    try:
        base_url, headers_or_error = _baserow_config("baserow_list_tables", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = (
            f"/api/database/tables/database/{quote(database_id.strip(), safe='')}/"
            if database_id.strip()
            else "/api/database/tables/all-tables/"
        )
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("baserow_list_tables failed", exc_info=True)
        return f"[Error]: Baserow table list failed: {e}"


@tool
def baserow_list_fields(
    table_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List fields for a Baserow table."""
    if not table_id.strip():
        return "[Error]: table_id is required."
    try:
        base_url, headers_or_error = _baserow_config("baserow_list_fields", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/database/fields/table/{quote(table_id.strip(), safe='')}/"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("baserow_list_fields failed", exc_info=True)
        return f"[Error]: Baserow field list failed: {e}"


@tool
def baserow_list_rows(
    table_id: str,
    limit: int = 50,
    page: int = 1,
    search: str = "",
    order_by: str = "",
    filters_json: str = "",
    filter_type: str = "",
    user_field_names: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List rows from a Baserow table.

    Args:
        table_id: Baserow table ID.
        limit: Number of rows to return, 1-200.
        page: Result page number.
        search: Optional full-table search text.
        order_by: Optional comma-separated Baserow order_by expression.
        filters_json: Optional JSON object of Baserow query parameters.
        filter_type: Optional AND or OR filter type.
        user_field_names: Return fields by visible names instead of field IDs.
    """
    if not table_id.strip():
        return "[Error]: table_id is required."
    try:
        base_url, headers_or_error = _baserow_config("baserow_list_rows", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = _parse_json(filters_json, expected=dict, label="filters_json")
        params.update(
            {
                "size": _limit(limit, max_value=200),
                "page": max(1, int(page)),
                "search": search.strip(),
                "order_by": order_by.strip(),
                "filter_type": filter_type.strip(),
                "user_field_names": str(bool(user_field_names)).lower(),
            }
        )
        endpoint = f"/api/database/rows/table/{quote(table_id.strip(), safe='')}/"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("baserow_list_rows failed", exc_info=True)
        return f"[Error]: Baserow row list failed: {e}"


@tool
def baserow_get_row(
    table_id: str,
    row_id: str,
    user_field_names: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a single Baserow row."""
    if not table_id.strip() or not row_id.strip():
        return "[Error]: table_id and row_id are required."
    try:
        base_url, headers_or_error = _baserow_config("baserow_get_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/database/rows/table/{quote(table_id.strip(), safe='')}/{quote(row_id.strip(), safe='')}/"
        return _dump_json(
            _request_json(
                "GET",
                f"{base_url}{endpoint}",
                params={"user_field_names": str(bool(user_field_names)).lower()},
                headers=headers_or_error,
            )
        )
    except Exception as e:
        logger.error("baserow_get_row failed", exc_info=True)
        return f"[Error]: Baserow row lookup failed: {e}"


@tool
def baserow_create_row(
    table_id: str,
    fields_json: str,
    user_field_names: bool = True,
    before_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Baserow row from a JSON field mapping."""
    if not table_id.strip():
        return "[Error]: table_id is required."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json must contain at least one field."
        base_url, headers_or_error = _baserow_config("baserow_create_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "user_field_names": str(bool(user_field_names)).lower(),
            "before": before_id.strip(),
        }
        endpoint = f"/api/database/rows/table/{quote(table_id.strip(), safe='')}/"
        return _dump_json(
            _request_json("POST", f"{base_url}{endpoint}", params=params, json_body=fields, headers=headers_or_error)
        )
    except Exception as e:
        logger.error("baserow_create_row failed", exc_info=True)
        return f"[Error]: Baserow row creation failed: {e}"


@tool
def baserow_update_row(
    table_id: str,
    row_id: str,
    fields_json: str,
    user_field_names: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Baserow row from a JSON field mapping."""
    if not table_id.strip() or not row_id.strip():
        return "[Error]: table_id and row_id are required."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json must contain at least one field."
        base_url, headers_or_error = _baserow_config("baserow_update_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/database/rows/table/{quote(table_id.strip(), safe='')}/{quote(row_id.strip(), safe='')}/"
        return _dump_json(
            _request_json(
                "PATCH",
                f"{base_url}{endpoint}",
                params={"user_field_names": str(bool(user_field_names)).lower()},
                json_body=fields,
                headers=headers_or_error,
            )
        )
    except Exception as e:
        logger.error("baserow_update_row failed", exc_info=True)
        return f"[Error]: Baserow row update failed: {e}"


@tool
def baserow_delete_row(
    table_id: str,
    row_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Baserow row."""
    if not table_id.strip() or not row_id.strip():
        return "[Error]: table_id and row_id are required."
    try:
        base_url, headers_or_error = _baserow_config("baserow_delete_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/database/rows/table/{quote(table_id.strip(), safe='')}/{quote(row_id.strip(), safe='')}/"
        data = _request_json("DELETE", f"{base_url}{endpoint}", headers=headers_or_error)
        return _dump_json({"success": True, "row_id": row_id.strip(), "response": data})
    except Exception as e:
        logger.error("baserow_delete_row failed", exc_info=True)
        return f"[Error]: Baserow row deletion failed: {e}"


@tool
def nocodb_list_bases(
    workspace_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List NocoDB bases, optionally within a workspace."""
    try:
        base_url, headers_or_error = _nocodb_config("nocodb_list_bases", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = (
            f"/api/v3/meta/workspaces/{quote(workspace_id.strip(), safe='')}/bases"
            if workspace_id.strip()
            else "/api/v2/meta/bases/"
        )
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("nocodb_list_bases failed", exc_info=True)
        return f"[Error]: NocoDB base list failed: {e}"


@tool
def nocodb_get_base(
    base_id: str,
    include_tables: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get NocoDB base metadata."""
    if not base_id.strip():
        return "[Error]: base_id is required."
    try:
        base_url, headers_or_error = _nocodb_config("nocodb_get_base", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        encoded_base = quote(base_id.strip(), safe="")
        data = _request_json("GET", f"{base_url}/api/v3/meta/bases/{encoded_base}", headers=headers_or_error)
        if include_tables and isinstance(data, dict):
            tables = _request_json("GET", f"{base_url}/api/v3/meta/bases/{encoded_base}/tables", headers=headers_or_error)
            data["tables"] = tables.get("list", tables) if isinstance(tables, dict) else tables
        return _dump_json(data)
    except Exception as e:
        logger.error("nocodb_get_base failed", exc_info=True)
        return f"[Error]: NocoDB base lookup failed: {e}"


@tool
def nocodb_list_records(
    base_id: str,
    table_id: str,
    limit: int = 50,
    offset: int = 0,
    where: str = "",
    fields: str = "",
    sort_json: str = "",
    view_id: str = "",
    shuffle: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List NocoDB records from a table."""
    if not base_id.strip() or not table_id.strip():
        return "[Error]: base_id and table_id are required."
    try:
        base_url, headers_or_error = _nocodb_config("nocodb_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {
            "limit": _limit(limit, max_value=1000),
            "offset": max(0, int(offset)),
            "where": where.strip(),
            "fields": ",".join(_csv_to_list(fields)),
            "viewId": view_id.strip(),
            "shuffle": 1 if shuffle else "",
        }
        if sort_json.strip():
            params["sort"] = json.dumps(_parse_json(sort_json, expected=list, label="sort_json"))
        endpoint = f"/api/v3/data/{quote(base_id.strip(), safe='')}/{quote(table_id.strip(), safe='')}/records"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("nocodb_list_records failed", exc_info=True)
        return f"[Error]: NocoDB record list failed: {e}"


@tool
def nocodb_get_record(
    base_id: str,
    table_id: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a single NocoDB record."""
    if not base_id.strip() or not table_id.strip() or not record_id.strip():
        return "[Error]: base_id, table_id, and record_id are required."
    try:
        base_url, headers_or_error = _nocodb_config("nocodb_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = (
            f"/api/v3/data/{quote(base_id.strip(), safe='')}/"
            f"{quote(table_id.strip(), safe='')}/records/{quote(record_id.strip(), safe='')}"
        )
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("nocodb_get_record failed", exc_info=True)
        return f"[Error]: NocoDB record lookup failed: {e}"


@tool
def nocodb_count_records(
    base_id: str,
    table_id: str,
    where: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Count NocoDB records, optionally filtered by formula."""
    if not base_id.strip() or not table_id.strip():
        return "[Error]: base_id and table_id are required."
    try:
        base_url, headers_or_error = _nocodb_config("nocodb_count_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/v3/data/{quote(base_id.strip(), safe='')}/{quote(table_id.strip(), safe='')}/count"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params={"where": where.strip()}, headers=headers_or_error))
    except Exception as e:
        logger.error("nocodb_count_records failed", exc_info=True)
        return f"[Error]: NocoDB record count failed: {e}"


@tool
def nocodb_create_record(
    base_id: str,
    table_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a NocoDB record from a JSON field mapping."""
    if not base_id.strip() or not table_id.strip():
        return "[Error]: base_id and table_id are required."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json must contain at least one field."
        base_url, headers_or_error = _nocodb_config("nocodb_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/v3/data/{quote(base_id.strip(), safe='')}/{quote(table_id.strip(), safe='')}/records"
        return _dump_json(_request_json("POST", f"{base_url}{endpoint}", json_body=[{"fields": fields}], headers=headers_or_error))
    except Exception as e:
        logger.error("nocodb_create_record failed", exc_info=True)
        return f"[Error]: NocoDB record creation failed: {e}"


@tool
def nocodb_update_record(
    base_id: str,
    table_id: str,
    record_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a NocoDB record from a JSON field mapping."""
    if not base_id.strip() or not table_id.strip() or not record_id.strip():
        return "[Error]: base_id, table_id, and record_id are required."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json must contain at least one field."
        base_url, headers_or_error = _nocodb_config("nocodb_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/v3/data/{quote(base_id.strip(), safe='')}/{quote(table_id.strip(), safe='')}/records"
        body = [{"id": record_id.strip(), "fields": fields}]
        return _dump_json(_request_json("PATCH", f"{base_url}{endpoint}", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("nocodb_update_record failed", exc_info=True)
        return f"[Error]: NocoDB record update failed: {e}"


@tool
def nocodb_delete_record(
    base_id: str,
    table_id: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a NocoDB record."""
    if not base_id.strip() or not table_id.strip() or not record_id.strip():
        return "[Error]: base_id, table_id, and record_id are required."
    try:
        base_url, headers_or_error = _nocodb_config("nocodb_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/api/v3/data/{quote(base_id.strip(), safe='')}/{quote(table_id.strip(), safe='')}/records"
        data = _request_json("DELETE", f"{base_url}{endpoint}", json_body=[{"id": record_id.strip()}], headers=headers_or_error)
        return _dump_json(data)
    except Exception as e:
        logger.error("nocodb_delete_record failed", exc_info=True)
        return f"[Error]: NocoDB record deletion failed: {e}"


@tool
def coda_list_docs(
    query: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Coda docs visible to the credential."""
    try:
        base_url, headers_or_error = _coda_config("coda_list_docs", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {"query": query.strip(), "limit": _limit(limit, max_value=100)}
        return _dump_json(_request_json("GET", f"{base_url}/docs", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_list_docs failed", exc_info=True)
        return f"[Error]: Coda doc list failed: {e}"


@tool
def coda_list_tables(
    doc_id: str,
    table_types: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Coda tables and views in a doc."""
    if not doc_id.strip():
        return "[Error]: doc_id is required."
    try:
        base_url, headers_or_error = _coda_config("coda_list_tables", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params={"tableTypes": table_types.strip()}, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_list_tables failed", exc_info=True)
        return f"[Error]: Coda table list failed: {e}"


@tool
def coda_list_table_rows(
    doc_id: str,
    table_id: str,
    limit: int = 50,
    query: str = "",
    sort_by: str = "",
    value_format: str = "simple",
    use_column_names: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List rows from a Coda table or view."""
    if not doc_id.strip() or not table_id.strip():
        return "[Error]: doc_id and table_id are required."
    try:
        base_url, headers_or_error = _coda_config("coda_list_table_rows", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "limit": _limit(limit, max_value=500),
            "query": query.strip(),
            "sortBy": sort_by.strip(),
            "valueFormat": value_format.strip(),
            "useColumnNames": str(bool(use_column_names)).lower(),
        }
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/rows"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_list_table_rows failed", exc_info=True)
        return f"[Error]: Coda row list failed: {e}"


@tool
def coda_get_table_row(
    doc_id: str,
    table_id: str,
    row_id: str,
    value_format: str = "simple",
    use_column_names: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Coda table row."""
    if not doc_id.strip() or not table_id.strip() or not row_id.strip():
        return "[Error]: doc_id, table_id, and row_id are required."
    try:
        base_url, headers_or_error = _coda_config("coda_get_table_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = {
            "valueFormat": value_format.strip(),
            "useColumnNames": str(bool(use_column_names)).lower(),
        }
        endpoint = (
            f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}"
            f"/rows/{quote(row_id.strip(), safe='')}"
        )
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_get_table_row failed", exc_info=True)
        return f"[Error]: Coda row lookup failed: {e}"


@tool
def coda_create_table_row(
    doc_id: str,
    table_id: str,
    cells_json: str,
    disable_parsing: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create or upsert a Coda table row from a JSON column-value mapping."""
    if not doc_id.strip() or not table_id.strip():
        return "[Error]: doc_id and table_id are required."
    try:
        cells = _cells_from_mapping(cells_json)
        if not cells:
            return "[Error]: cells_json must contain at least one cell."
        base_url, headers_or_error = _coda_config("coda_create_table_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"rows": [{"cells": cells}], "disableParsing": bool(disable_parsing)}
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/rows"
        return _dump_json(_request_json("POST", f"{base_url}{endpoint}", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_create_table_row failed", exc_info=True)
        return f"[Error]: Coda row creation failed: {e}"


@tool
def coda_update_table_row(
    doc_id: str,
    table_id: str,
    row_id: str,
    cells_json: str,
    disable_parsing: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Coda table row from a JSON column-value mapping."""
    if not doc_id.strip() or not table_id.strip() or not row_id.strip():
        return "[Error]: doc_id, table_id, and row_id are required."
    try:
        cells = _cells_from_mapping(cells_json)
        if not cells:
            return "[Error]: cells_json must contain at least one cell."
        base_url, headers_or_error = _coda_config("coda_update_table_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"row": {"cells": cells}, "disableParsing": bool(disable_parsing)}
        endpoint = (
            f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}"
            f"/rows/{quote(row_id.strip(), safe='')}"
        )
        return _dump_json(_request_json("PUT", f"{base_url}{endpoint}", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_update_table_row failed", exc_info=True)
        return f"[Error]: Coda row update failed: {e}"


@tool
def coda_delete_table_row(
    doc_id: str,
    table_id: str,
    row_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Coda table row."""
    if not doc_id.strip() or not table_id.strip() or not row_id.strip():
        return "[Error]: doc_id, table_id, and row_id are required."
    try:
        base_url, headers_or_error = _coda_config("coda_delete_table_row", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/rows"
        return _dump_json(_request_json("DELETE", f"{base_url}{endpoint}", json_body={"rowIds": [row_id.strip()]}, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_delete_table_row failed", exc_info=True)
        return f"[Error]: Coda row deletion failed: {e}"


@tool
def coda_list_formulas(
    doc_id: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List formulas in a Coda doc."""
    if not doc_id.strip():
        return "[Error]: doc_id is required."
    try:
        base_url, headers_or_error = _coda_config("coda_list_formulas", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/formulas"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params={"limit": _limit(limit)}, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_list_formulas failed", exc_info=True)
        return f"[Error]: Coda formula list failed: {e}"


@tool
def coda_list_controls(
    doc_id: str,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List controls in a Coda doc."""
    if not doc_id.strip():
        return "[Error]: doc_id is required."
    try:
        base_url, headers_or_error = _coda_config("coda_list_controls", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/controls"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params={"limit": _limit(limit)}, headers=headers_or_error))
    except Exception as e:
        logger.error("coda_list_controls failed", exc_info=True)
        return f"[Error]: Coda control list failed: {e}"


@tool
def grist_list_orgs(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Grist organizations available to the credential."""
    try:
        base_url, headers_or_error = _grist_config("grist_list_orgs", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        return _dump_json(_request_json("GET", f"{base_url}/orgs", headers=headers_or_error))
    except Exception as e:
        logger.error("grist_list_orgs failed", exc_info=True)
        return f"[Error]: Grist organization list failed: {e}"


@tool
def grist_list_workspaces(
    org_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Grist workspaces in an organization."""
    if not org_id.strip():
        return "[Error]: org_id is required."
    try:
        base_url, headers_or_error = _grist_config("grist_list_workspaces", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/orgs/{quote(org_id.strip(), safe='')}/workspaces"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("grist_list_workspaces failed", exc_info=True)
        return f"[Error]: Grist workspace list failed: {e}"


@tool
def grist_list_docs(
    workspace_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Grist docs in a workspace."""
    if not workspace_id.strip():
        return "[Error]: workspace_id is required."
    try:
        base_url, headers_or_error = _grist_config("grist_list_docs", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/workspaces/{quote(workspace_id.strip(), safe='')}/docs"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("grist_list_docs failed", exc_info=True)
        return f"[Error]: Grist doc list failed: {e}"


@tool
def grist_list_tables(
    doc_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List tables in a Grist doc."""
    if not doc_id.strip():
        return "[Error]: doc_id is required."
    try:
        base_url, headers_or_error = _grist_config("grist_list_tables", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("grist_list_tables failed", exc_info=True)
        return f"[Error]: Grist table list failed: {e}"


@tool
def grist_list_columns(
    doc_id: str,
    table_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List columns in a Grist table."""
    if not doc_id.strip() or not table_id.strip():
        return "[Error]: doc_id and table_id are required."
    try:
        base_url, headers_or_error = _grist_config("grist_list_columns", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/columns"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", headers=headers_or_error))
    except Exception as e:
        logger.error("grist_list_columns failed", exc_info=True)
        return f"[Error]: Grist column list failed: {e}"


@tool
def grist_list_records(
    doc_id: str,
    table_id: str,
    limit: int = 100,
    sort: str = "",
    filter_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List records from a Grist table."""
    if not doc_id.strip() or not table_id.strip():
        return "[Error]: doc_id and table_id are required."
    try:
        base_url, headers_or_error = _grist_config("grist_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params: dict[str, Any] = {"limit": _limit(limit, max_value=1000), "sort": sort.strip()}
        if filter_json.strip():
            params["filter"] = json.dumps(_parse_json(filter_json, expected=dict, label="filter_json"))
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/records"
        return _dump_json(_request_json("GET", f"{base_url}{endpoint}", params=params, headers=headers_or_error))
    except Exception as e:
        logger.error("grist_list_records failed", exc_info=True)
        return f"[Error]: Grist record list failed: {e}"


@tool
def grist_create_record(
    doc_id: str,
    table_id: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Grist record from a JSON field mapping."""
    if not doc_id.strip() or not table_id.strip():
        return "[Error]: doc_id and table_id are required."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json must contain at least one field."
        base_url, headers_or_error = _grist_config("grist_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"records": [{"fields": fields}]}
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/records"
        return _dump_json(_request_json("POST", f"{base_url}{endpoint}", json_body=body, headers=headers_or_error))
    except Exception as e:
        logger.error("grist_create_record failed", exc_info=True)
        return f"[Error]: Grist record creation failed: {e}"


@tool
def grist_update_record(
    doc_id: str,
    table_id: str,
    record_id: int,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Grist record from a JSON field mapping."""
    if not doc_id.strip() or not table_id.strip():
        return "[Error]: doc_id and table_id are required."
    try:
        fields = _parse_json(fields_json, expected=dict, label="fields_json")
        if not fields:
            return "[Error]: fields_json must contain at least one field."
        base_url, headers_or_error = _grist_config("grist_update_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        body = {"records": [{"id": int(record_id), "fields": fields}]}
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/records"
        data = _request_json("PATCH", f"{base_url}{endpoint}", json_body=body, headers=headers_or_error)
        return _dump_json({"success": True, "record_id": record_id, "response": data})
    except Exception as e:
        logger.error("grist_update_record failed", exc_info=True)
        return f"[Error]: Grist record update failed: {e}"


@tool
def grist_delete_records(
    doc_id: str,
    table_id: str,
    row_ids: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete one or more Grist records.

    Args:
        doc_id: Grist document ID.
        table_id: Grist table ID.
        row_ids: Comma-separated numeric row IDs.
    """
    if not doc_id.strip() or not table_id.strip() or not row_ids.strip():
        return "[Error]: doc_id, table_id, and row_ids are required."
    try:
        ids = [int(value) for value in _csv_to_list(row_ids)]
        if not ids:
            return "[Error]: row_ids must contain at least one ID."
        base_url, headers_or_error = _grist_config("grist_delete_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        endpoint = f"/docs/{quote(doc_id.strip(), safe='')}/tables/{quote(table_id.strip(), safe='')}/data/delete"
        data = _request_json("POST", f"{base_url}{endpoint}", json_body=ids, headers=headers_or_error)
        return _dump_json({"success": True, "deleted": ids, "response": data})
    except Exception as e:
        logger.error("grist_delete_records failed", exc_info=True)
        return f"[Error]: Grist record deletion failed: {e}"


DATA_TABLE_SERVICE_TOOLS = [
    baserow_list_tables,
    baserow_list_fields,
    baserow_list_rows,
    baserow_get_row,
    baserow_create_row,
    baserow_update_row,
    baserow_delete_row,
    nocodb_list_bases,
    nocodb_get_base,
    nocodb_list_records,
    nocodb_get_record,
    nocodb_count_records,
    nocodb_create_record,
    nocodb_update_record,
    nocodb_delete_record,
    coda_list_docs,
    coda_list_tables,
    coda_list_table_rows,
    coda_get_table_row,
    coda_create_table_row,
    coda_update_table_row,
    coda_delete_table_row,
    coda_list_formulas,
    coda_list_controls,
    grist_list_orgs,
    grist_list_workspaces,
    grist_list_docs,
    grist_list_tables,
    grist_list_columns,
    grist_list_records,
    grist_create_record,
    grist_update_record,
    grist_delete_records,
]
