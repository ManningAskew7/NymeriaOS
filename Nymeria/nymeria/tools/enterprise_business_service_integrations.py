"""Enterprise resource and invoicing service integration tools."""

from __future__ import annotations

import json
import logging
import random
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
    BASE_URL_FIELDS,
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_INVOICENINJA_V4_BASE_URL = "https://app.invoiceninja.com"
_INVOICENINJA_V5_BASE_URL = "https://invoicing.co"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _limit(value: int, *, default: int = 50, max_value: int = 250) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


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
                params=_filtered(params),
                json=json_body,
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
                or body.get("exception")
                or body.get("detail")
                or body.get("error_description")
                or body.get("error")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _erpnext_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider="erpnext",
            provider_aliases=("erp_next", "erpnext_api", "frappe"),
            field_names=("base_url", "url", "domain"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("erpnext_base_url")
    )
    api_key = _credential_value(
        provider="erpnext",
        provider_aliases=("erp_next", "erpnext_api", "frappe"),
        field_names=("api_key", "apiKey", "key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("erpnext_api_key")
    api_secret = _credential_value(
        provider="erpnext",
        provider_aliases=("erp_next", "erpnext_api", "frappe"),
        field_names=("api_secret", "apiSecret", "secret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("erpnext_api_secret")
    if not base:
        subdomain = _credential_value(
            provider="erpnext",
            provider_aliases=("erp_next", "erpnext_api", "frappe"),
            field_names=("subdomain",),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("erpnext_subdomain")
        domain = _credential_value(
            provider="erpnext",
            provider_aliases=("erp_next", "erpnext_api", "frappe"),
            field_names=("cloud_domain", "cloudDomain"),
            tool_name=tool_name,
            config=config,
        ) or _settings_value("erpnext_cloud_domain")
        if subdomain and domain:
            base = f"https://{subdomain.strip()}.{domain.strip()}"
    if not base:
        return "", (
            "[Error]: No ERPNext base URL found. Save an ERPNext credential with "
            '"base_url" / "domain", or set ERPNEXT_BASE_URL.'
        )
    if not api_key or not api_secret:
        return _base_url(base), _setup_hint(
            provider="erpnext",
            field_names=("api_key", "api_secret"),
            tool_name=tool_name,
            env_var="ERPNEXT_API_KEY and ERPNEXT_API_SECRET",
            display_name="ERPNext",
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _erpnext_resource_url(base_url: str, doc_type: str, document_name: str = "") -> str:
    doc = quote(doc_type.strip(), safe="")
    if document_name.strip():
        return f"{base_url}/api/resource/{doc}/{quote(document_name.strip(), safe='')}"
    return f"{base_url}/api/resource/{doc}"


_ODOO_RESOURCE_MODELS = {
    "contact": "res.partner",
    "contacts": "res.partner",
    "partner": "res.partner",
    "partners": "res.partner",
    "opportunity": "crm.lead",
    "opportunities": "crm.lead",
    "lead": "crm.lead",
    "leads": "crm.lead",
    "note": "note.note",
    "notes": "note.note",
}


def _odoo_model(value: str) -> str:
    model = value.strip()
    if not model:
        raise ValueError("model is required")
    return _ODOO_RESOURCE_MODELS.get(model.lower(), model)


def _odoo_database_from_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.split(".", 1)[0] if host else ""


def _odoo_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, str, str, str | None]:
    url = (
        _credential_value(
            provider="odoo",
            provider_aliases=("odoo_api",),
            field_names=("url", "base_url", "site_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("odoo_url")
    )
    username = _credential_value(
        provider="odoo",
        provider_aliases=("odoo_api",),
        field_names=("username", "email", "user"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("odoo_username")
    password = _credential_value(
        provider="odoo",
        provider_aliases=("odoo_api",),
        field_names=("password", "api_key", "apiKey"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("odoo_password")
    database = _credential_value(
        provider="odoo",
        provider_aliases=("odoo_api",),
        field_names=("database", "db"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("odoo_database")
    if not url:
        return "", "", "", "", (
            "[Error]: No Odoo URL found. Save an Odoo credential with field "
            '"url" or "base_url", or set ODOO_URL.'
        )
    base = _base_url(url)
    database = database or _odoo_database_from_url(base)
    if not username or not password or not database:
        return base, database, username or "", password or "", _setup_hint(
            provider="odoo",
            field_names=("url", "username", "password", "database"),
            tool_name=tool_name,
            env_var="ODOO_URL, ODOO_USERNAME, ODOO_PASSWORD, and ODOO_DATABASE",
            display_name="Odoo",
        )
    return base, database, username, password, None


def _odoo_rpc(base_url: str, payload: dict[str, Any]) -> Any:
    data = _request_json(
        "POST",
        f"{base_url}/jsonrpc",
        json_body=payload,
        headers={
            "Accept": "application/json",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": "Nymeria",
        },
    )
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        detail = err.get("data", {}).get("message") if isinstance(err, dict) else ""
        message = detail or (err.get("message") if isinstance(err, dict) else str(err))
        raise RuntimeError(message)
    return data.get("result") if isinstance(data, dict) else data


def _odoo_payload(service: str, method: str, args: list[Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": random.randint(1, 999_999),
    }


def _odoo_login(base_url: str, database: str, username: str, password: str) -> int:
    result = _odoo_rpc(base_url, _odoo_payload("common", "login", [database, username, password]))
    if not result:
        raise RuntimeError("Odoo login failed")
    return int(result)


def _odoo_execute_kw(
    base_url: str,
    database: str,
    username: str,
    password: str,
    model: str,
    method: str,
    args: list[Any],
    kwargs: Optional[dict[str, Any]] = None,
) -> Any:
    user_id = _odoo_login(base_url, database, username, password)
    return _odoo_rpc(
        base_url,
        _odoo_payload(
            "object",
            "execute_kw",
            [database, user_id, password, model, method, args, _filtered(kwargs)],
        ),
    )


_INVOICENINJA_RESOURCES = {
    "bank_transaction": "bank_transactions",
    "bank_transactions": "bank_transactions",
    "client": "clients",
    "clients": "clients",
    "expense": "expenses",
    "expenses": "expenses",
    "invoice": "invoices",
    "invoices": "invoices",
    "payment": "payments",
    "payments": "payments",
    "quote": "quotes",
    "quotes": "quotes",
    "task": "tasks",
    "tasks": "tasks",
}


def _invoiceninja_resource(resource: str) -> str:
    mapped = _INVOICENINJA_RESOURCES.get(resource.strip().lower())
    if not mapped:
        raise ValueError(
            "resource must be one of client, invoice, payment, quote, task, expense, or bank_transaction"
        )
    return mapped


def _invoiceninja_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str] | str]:
    version = (
        _credential_value(
            provider="invoiceninja",
            provider_aliases=("invoice_ninja", "invoice_ninja_api", "invoiceninja_api"),
            field_names=("api_version", "version"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("invoiceninja_api_version")
        or "v5"
    ).strip()
    default_base = _INVOICENINJA_V4_BASE_URL if version == "v4" else _INVOICENINJA_V5_BASE_URL
    base = (
        _credential_value(
            provider="invoiceninja",
            provider_aliases=("invoice_ninja", "invoice_ninja_api", "invoiceninja_api"),
            field_names=BASE_URL_FIELDS,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("invoiceninja_base_url")
        or default_base
    )
    token = _credential_value(
        provider="invoiceninja",
        provider_aliases=("invoice_ninja", "invoice_ninja_api", "invoiceninja_api"),
        field_names=("api_token", "apiToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("invoiceninja_api_token")
    secret = _credential_value(
        provider="invoiceninja",
        provider_aliases=("invoice_ninja", "invoice_ninja_api", "invoiceninja_api"),
        field_names=("secret", "api_secret", "apiSecret"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("invoiceninja_secret")
    if not token:
        return _base_url(base), version, _setup_hint(
            provider="invoiceninja",
            field_names=("api_token", "apiToken", "token", "value"),
            tool_name=tool_name,
            env_var="INVOICENINJA_API_TOKEN",
            display_name="Invoice Ninja",
        )
    headers = {"Accept": "application/json", "User-Agent": "Nymeria"}
    if len(token) < 64:
        headers["X-Ninja-Token"] = token
    else:
        headers.update(
            {
                "Content-Type": "application/json",
                "X-API-TOKEN": token,
                "X-API-SECRET": secret or "",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
    return _base_url(base), version, headers


def _invoiceninja_url(base_url: str, resource: str, record_id: str = "") -> str:
    path = _invoiceninja_resource(resource)
    if record_id.strip():
        return f"{base_url}/api/v1/{path}/{quote(record_id.strip(), safe='')}"
    return f"{base_url}/api/v1/{path}"


def _data_field(data: Any) -> Any:
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


@tool
def erpnext_get_logged_user(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the current ERPNext user for the configured credential."""
    try:
        base_url, headers_or_error = _erpnext_config("erpnext_get_logged_user", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            f"{base_url}/api/method/frappe.auth.get_logged_user",
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("erpnext_get_logged_user failed", exc_info=True)
        return f"[Error]: ERPNext current user lookup failed: {e}"


@tool
def erpnext_list_documents(
    doc_type: str,
    fields: str = "",
    filters_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List ERPNext documents for a DocType.

    Args:
        doc_type: ERPNext DocType name.
        fields: Optional comma-separated fields to return. Use * for all fields.
        filters_json: Optional ERPNext filters JSON array.
        limit: Number of documents to return, 1-250.
    """
    if not doc_type.strip():
        return "[Error]: doc_type is required."
    try:
        base_url, headers_or_error = _erpnext_config("erpnext_list_documents", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        selected_fields = _split_csv(fields)
        params: dict[str, Any] = {"limit_page_length": _limit(limit), "limit_start": 0}
        if selected_fields:
            params["fields"] = json.dumps(["*"] if "*" in selected_fields else selected_fields)
        filters = _json_array(filters_json, field_name="filters_json")
        if filters:
            params["filters"] = json.dumps(filters)
        data = _request_json(
            "GET",
            _erpnext_resource_url(base_url, doc_type),
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("erpnext_list_documents failed", exc_info=True)
        return f"[Error]: ERPNext document list failed: {e}"


@tool
def erpnext_get_document(
    doc_type: str,
    document_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an ERPNext document by DocType and document name.

    Args:
        doc_type: ERPNext DocType name.
        document_name: ERPNext document name.
    """
    if not doc_type.strip() or not document_name.strip():
        return "[Error]: doc_type and document_name are required."
    try:
        base_url, headers_or_error = _erpnext_config("erpnext_get_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _erpnext_resource_url(base_url, doc_type, document_name),
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("erpnext_get_document failed", exc_info=True)
        return f"[Error]: ERPNext document lookup failed: {e}"


@tool
def erpnext_create_document(
    doc_type: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an ERPNext document.

    Args:
        doc_type: ERPNext DocType name.
        fields_json: Document fields as a JSON object.
    """
    if not doc_type.strip() or not fields_json.strip():
        return "[Error]: doc_type and fields_json are required."
    try:
        fields = _json_object(fields_json, field_name="fields_json", allow_empty=False)
        base_url, headers_or_error = _erpnext_config("erpnext_create_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            _erpnext_resource_url(base_url, doc_type),
            json_body=fields,
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("erpnext_create_document failed", exc_info=True)
        return f"[Error]: ERPNext document creation failed: {e}"


@tool
def erpnext_update_document(
    doc_type: str,
    document_name: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an ERPNext document.

    Args:
        doc_type: ERPNext DocType name.
        document_name: ERPNext document name.
        fields_json: Document fields to update as a JSON object.
    """
    if not doc_type.strip() or not document_name.strip() or not fields_json.strip():
        return "[Error]: doc_type, document_name, and fields_json are required."
    try:
        fields = _json_object(fields_json, field_name="fields_json", allow_empty=False)
        base_url, headers_or_error = _erpnext_config("erpnext_update_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "PUT",
            _erpnext_resource_url(base_url, doc_type, document_name),
            json_body=fields,
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("erpnext_update_document failed", exc_info=True)
        return f"[Error]: ERPNext document update failed: {e}"


@tool
def erpnext_delete_document(
    doc_type: str,
    document_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an ERPNext document.

    Args:
        doc_type: ERPNext DocType name.
        document_name: ERPNext document name.
    """
    if not doc_type.strip() or not document_name.strip():
        return "[Error]: doc_type and document_name are required."
    try:
        base_url, headers_or_error = _erpnext_config("erpnext_delete_document", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            _erpnext_resource_url(base_url, doc_type, document_name),
            headers=headers_or_error,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("erpnext_delete_document failed", exc_info=True)
        return f"[Error]: ERPNext document deletion failed: {e}"


@tool
def odoo_get_server_version(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get the Odoo server version."""
    try:
        base_url, _database, _username, _password, error = _odoo_config("odoo_get_server_version", config)
        if error:
            return error
        data = _odoo_rpc(base_url, _odoo_payload("common", "version", []))
        return _dump_json(data)
    except Exception as e:
        logger.error("odoo_get_server_version failed", exc_info=True)
        return f"[Error]: Odoo server version lookup failed: {e}"


@tool
def odoo_list_records(
    model: str,
    fields: str = "id,name",
    filters_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Odoo records from a model.

    Args:
        model: Odoo model name, or contact, opportunity, or note alias.
        fields: Optional comma-separated fields to return.
        filters_json: Optional Odoo domain/filter JSON array.
        limit: Number of records to return, 1-250.
    """
    try:
        base_url, database, username, password, error = _odoo_config("odoo_list_records", config)
        if error:
            return error
        data = _odoo_execute_kw(
            base_url,
            database,
            username,
            password,
            _odoo_model(model),
            "search_read",
            [_json_array(filters_json, field_name="filters_json")],
            {"fields": _split_csv(fields), "limit": _limit(limit)},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("odoo_list_records failed", exc_info=True)
        return f"[Error]: Odoo record list failed: {e}"


@tool
def odoo_get_record(
    model: str,
    record_id: int,
    fields: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Odoo record by ID.

    Args:
        model: Odoo model name, or contact, opportunity, or note alias.
        record_id: Odoo record ID.
        fields: Optional comma-separated fields to return.
    """
    if record_id <= 0:
        return "[Error]: record_id must be a positive integer."
    try:
        base_url, database, username, password, error = _odoo_config("odoo_get_record", config)
        if error:
            return error
        data = _odoo_execute_kw(
            base_url,
            database,
            username,
            password,
            _odoo_model(model),
            "read",
            [[record_id]],
            {"fields": _split_csv(fields)},
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("odoo_get_record failed", exc_info=True)
        return f"[Error]: Odoo record lookup failed: {e}"


@tool
def odoo_create_record(
    model: str,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Odoo record.

    Args:
        model: Odoo model name, or contact, opportunity, or note alias.
        fields_json: Record fields as a JSON object.
    """
    if not fields_json.strip():
        return "[Error]: fields_json is required."
    try:
        fields = _json_object(fields_json, field_name="fields_json", allow_empty=False)
        base_url, database, username, password, error = _odoo_config("odoo_create_record", config)
        if error:
            return error
        data = _odoo_execute_kw(
            base_url,
            database,
            username,
            password,
            _odoo_model(model),
            "create",
            [fields],
        )
        return _dump_json({"id": data})
    except Exception as e:
        logger.error("odoo_create_record failed", exc_info=True)
        return f"[Error]: Odoo record creation failed: {e}"


@tool
def odoo_update_record(
    model: str,
    record_id: int,
    fields_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an Odoo record.

    Args:
        model: Odoo model name, or contact, opportunity, or note alias.
        record_id: Odoo record ID.
        fields_json: Record fields to update as a JSON object.
    """
    if record_id <= 0 or not fields_json.strip():
        return "[Error]: record_id and fields_json are required."
    try:
        fields = _json_object(fields_json, field_name="fields_json", allow_empty=False)
        base_url, database, username, password, error = _odoo_config("odoo_update_record", config)
        if error:
            return error
        _odoo_execute_kw(
            base_url,
            database,
            username,
            password,
            _odoo_model(model),
            "write",
            [[record_id], fields],
        )
        return _dump_json({"id": record_id})
    except Exception as e:
        logger.error("odoo_update_record failed", exc_info=True)
        return f"[Error]: Odoo record update failed: {e}"


@tool
def odoo_delete_record(
    model: str,
    record_id: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Odoo record.

    Args:
        model: Odoo model name, or contact, opportunity, or note alias.
        record_id: Odoo record ID.
    """
    if record_id <= 0:
        return "[Error]: record_id must be a positive integer."
    try:
        base_url, database, username, password, error = _odoo_config("odoo_delete_record", config)
        if error:
            return error
        _odoo_execute_kw(
            base_url,
            database,
            username,
            password,
            _odoo_model(model),
            "unlink",
            [[record_id]],
        )
        return _dump_json({"success": True})
    except Exception as e:
        logger.error("odoo_delete_record failed", exc_info=True)
        return f"[Error]: Odoo record deletion failed: {e}"


@tool
def invoiceninja_list_records(
    resource: str,
    include: str = "",
    status: str = "",
    filters_json: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Invoice Ninja records.

    Args:
        resource: One of client, invoice, payment, quote, task, expense, or bank_transaction.
        include: Optional related resource include value.
        status: Optional status filter.
        filters_json: Optional extra query parameters as a JSON object.
        limit: Number of records to return, 1-250.
    """
    try:
        base_url, _version, headers_or_error = _invoiceninja_config("invoiceninja_list_records", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        params = _json_object(filters_json, field_name="filters_json")
        params.update(_filtered({"include": include.strip(), "status": status.strip(), "per_page": _limit(limit)}))
        data = _request_json(
            "GET",
            _invoiceninja_url(base_url, resource),
            params=params,
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("invoiceninja_list_records failed", exc_info=True)
        return f"[Error]: Invoice Ninja record list failed: {e}"


@tool
def invoiceninja_get_record(
    resource: str,
    record_id: str,
    include: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Invoice Ninja record by ID.

    Args:
        resource: One of client, invoice, payment, quote, task, expense, or bank_transaction.
        record_id: Invoice Ninja record ID.
        include: Optional related resource include value.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        base_url, _version, headers_or_error = _invoiceninja_config("invoiceninja_get_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "GET",
            _invoiceninja_url(base_url, resource, record_id),
            params={"include": include.strip()},
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("invoiceninja_get_record failed", exc_info=True)
        return f"[Error]: Invoice Ninja record lookup failed: {e}"


@tool
def invoiceninja_create_record(
    resource: str,
    fields_json: str,
    query_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Invoice Ninja record.

    Args:
        resource: One of client, invoice, payment, quote, task, expense, or bank_transaction.
        fields_json: Record fields as a JSON object.
        query_json: Optional query parameters as a JSON object.
    """
    if not fields_json.strip():
        return "[Error]: fields_json is required."
    try:
        base_url, _version, headers_or_error = _invoiceninja_config("invoiceninja_create_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "POST",
            _invoiceninja_url(base_url, resource),
            params=_json_object(query_json, field_name="query_json"),
            json_body=_json_object(fields_json, field_name="fields_json", allow_empty=False),
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("invoiceninja_create_record failed", exc_info=True)
        return f"[Error]: Invoice Ninja record creation failed: {e}"


@tool
def invoiceninja_delete_record(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Invoice Ninja record.

    Args:
        resource: One of client, invoice, payment, quote, task, expense, or bank_transaction.
        record_id: Invoice Ninja record ID.
    """
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        base_url, _version, headers_or_error = _invoiceninja_config("invoiceninja_delete_record", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        data = _request_json(
            "DELETE",
            _invoiceninja_url(base_url, resource, record_id),
            headers=headers_or_error,
        )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("invoiceninja_delete_record failed", exc_info=True)
        return f"[Error]: Invoice Ninja record deletion failed: {e}"


@tool
def invoiceninja_email_invoice_or_quote(
    resource: str,
    record_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Email an Invoice Ninja invoice or quote.

    Args:
        resource: invoice or quote.
        record_id: Invoice or quote ID.
    """
    key = resource.strip().lower()
    if key not in {"invoice", "invoices", "quote", "quotes"}:
        return "[Error]: resource must be invoice or quote."
    if not record_id.strip():
        return "[Error]: record_id is required."
    try:
        base_url, version, headers_or_error = _invoiceninja_config("invoiceninja_email_invoice_or_quote", config)
        if isinstance(headers_or_error, str):
            return headers_or_error
        if version == "v4":
            data = _request_json(
                "POST",
                f"{base_url}/api/v1/email_invoice",
                json_body={"id": record_id.strip()},
                headers=headers_or_error,
            )
        else:
            data = _request_json(
                "GET",
                f"{base_url}/api/v1/{_invoiceninja_resource(resource)}/{quote(record_id.strip(), safe='')}/email",
                headers=headers_or_error,
            )
        return _dump_json(_data_field(data))
    except Exception as e:
        logger.error("invoiceninja_email_invoice_or_quote failed", exc_info=True)
        return f"[Error]: Invoice Ninja email action failed: {e}"


ENTERPRISE_BUSINESS_SERVICE_TOOLS = [
    erpnext_get_logged_user,
    erpnext_list_documents,
    erpnext_get_document,
    erpnext_create_document,
    erpnext_update_document,
    erpnext_delete_document,
    odoo_get_server_version,
    odoo_list_records,
    odoo_get_record,
    odoo_create_record,
    odoo_update_record,
    odoo_delete_record,
    invoiceninja_list_records,
    invoiceninja_get_record,
    invoiceninja_create_record,
    invoiceninja_delete_record,
    invoiceninja_email_invoice_or_quote,
]
