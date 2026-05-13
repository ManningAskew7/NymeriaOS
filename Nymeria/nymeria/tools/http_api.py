"""General-purpose HTTP and API discovery tools for Nymeria."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from langchain_core.tools import tool

from ..core.http_policy import (
    HTTPPolicyConfig,
    audit_http_event,
    blocked_network_error,
    evaluate_http_url,
    load_http_policy_config,
    redirect_allowed,
)

logger = logging.getLogger(__name__)

TOOL_VERSION = "2026-04-30.5"
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
RESPONSE_FORMATS = {"auto", "json", "text"}
MAX_TIMEOUT_SECONDS = 300
MAX_RESPONSE_CHARS = 200_000

SPEC_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/swagger.yaml",
    "/api-docs",
    "/v3/api-docs",
    "/docs/openapi.json",
    "/.well-known/openapi.json",
]

SELECTED_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "date",
    "server",
    "etag",
    "last-modified",
    "location",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


class _HTTPPolicyBlocked(Exception):
    def __init__(self, decision, redirect_chain: Optional[list[dict[str, Any]]] = None):
        self.decision = decision
        self.redirect_chain = redirect_chain or []
        super().__init__(decision.reason)


class _HTTPTooManyRedirects(Exception):
    def __init__(self, redirect_chain: list[dict[str, Any]]):
        self.redirect_chain = redirect_chain
        super().__init__("Maximum redirect count exceeded")


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)


def _base_result(**kwargs: Any) -> dict[str, Any]:
    return {"tool_version": TOOL_VERSION, **kwargs}


def _request_error(
    error_type: str,
    message: str,
    request: Optional[dict[str, str]] = None,
    elapsed_ms: Optional[float] = None,
) -> dict[str, Any]:
    result = _base_result(
        ok=False,
        http_ok=None,
        format_ok=None,
        error={"type": error_type, "message": message},
    )
    if request is not None:
        result["request"] = request
    if elapsed_ms is not None:
        result["elapsed_ms"] = elapsed_ms
    return result


def _redact_text(value: str, redact_values: Optional[list[str]] = None) -> str:
    redacted = value
    for secret in redact_values or []:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _redact_sensitive(value: Any, redact_values: Optional[list[str]] = None) -> Any:
    if not redact_values:
        return value
    if isinstance(value, str):
        return _redact_text(value, redact_values)
    if isinstance(value, dict):
        return {key: _redact_sensitive(item, redact_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item, redact_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item, redact_values) for item in value)
    return value


def _audit_http_result(
    *,
    tool_name: str,
    method: str,
    url: str,
    result: dict[str, Any],
    headers: Optional[dict[str, Any]] = None,
    body: Any = None,
    used_env_secrets: Optional[list[str]] = None,
    used_credentials: Optional[list[str]] = None,
    redact_values: Optional[list[str]] = None,
) -> None:
    safe_result = _redact_sensitive(result, redact_values)
    safe_url = _redact_text(url, redact_values)
    safe_headers = _redact_sensitive(headers or {}, redact_values)
    safe_body = _redact_sensitive(body, redact_values)
    response = (
        safe_result.get("response")
        if isinstance(safe_result.get("response"), dict)
        else {}
    )
    error = safe_result.get("error") if isinstance(safe_result.get("error"), dict) else {}
    body_preview = (
        safe_body
        if isinstance(safe_body, str)
        else ("[structured body]" if body is not None else None)
    )
    audit_http_event(
        {
            "tool": tool_name,
            "method": method,
            "url": safe_url,
            "headers": safe_headers,
            "body_preview": body_preview,
            "used_env_secrets": sorted(used_env_secrets or []),
            "used_credentials": sorted(used_credentials or []),
            "ok": safe_result.get("ok"),
            "http_ok": safe_result.get("http_ok"),
            "format_ok": safe_result.get("format_ok"),
            "status_code": response.get("status_code"),
            "response_url": response.get("url"),
            "policy": safe_result.get("policy"),
            "redirect_chain": safe_result.get("redirect_chain", []),
            "error_type": error.get("type"),
        }
    )


def _audit_api_discover_result(base_url: str, docs_url: Optional[str], result: dict[str, Any]) -> None:
    audit_http_event(
        {
            "tool": "api_discover",
            "method": "GET",
            "url": docs_url or base_url,
            "base_url": base_url,
            "ok": result.get("ok"),
            "found": result.get("found"),
            "spec_url": result.get("spec_url"),
            "tried_count": len(result.get("tried") or []),
            "policy": result.get("policy"),
            "error_type": (result.get("error") or {}).get("type")
            if isinstance(result.get("error"), dict)
            else None,
        }
    )


def _blocked_network_result(
    decision,
    *,
    method: str,
    url: str,
    elapsed_ms: Optional[float] = None,
    redirect_chain: Optional[list[dict[str, Any]]] = None,
    headers: Optional[dict[str, Any]] = None,
    body: Any = None,
    tool_name: str = "http_request",
    used_env_secrets: Optional[list[str]] = None,
    used_credentials: Optional[list[str]] = None,
    redact_values: Optional[list[str]] = None,
) -> dict[str, Any]:
    result = _request_error(
        "blocked_network_target",
        blocked_network_error(decision)["message"],
        request={"method": method, "url": url},
        elapsed_ms=elapsed_ms,
    )
    result["error"].update(blocked_network_error(decision))
    result["policy"] = decision.to_dict()
    if redirect_chain:
        result["redirect_chain"] = redirect_chain
    if used_env_secrets:
        result["used_env_secrets"] = sorted(used_env_secrets)
    if used_credentials:
        result["used_credentials"] = sorted(used_credentials)
    result = _redact_sensitive(result, redact_values)
    _audit_http_result(
        tool_name=tool_name,
        method=method,
        url=url,
        result=result,
        headers=headers,
        body=body,
        used_env_secrets=used_env_secrets,
        used_credentials=used_credentials,
        redact_values=redact_values,
    )
    return result


def _clamp_int(value: Optional[int], default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_url(url: str, field_name: str = "url") -> tuple[Optional[str], Optional[str]]:
    if not isinstance(url, str) or not url.strip():
        return None, f"{field_name} must be a non-empty string"

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, f"{field_name} must be an absolute http:// or https:// URL"

    return url.strip(), None


def _coerce_dict(value: Any, field_name: str) -> tuple[dict[str, Any], Optional[str]]:
    if value is None:
        return {}, None
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}, f"{field_name} must be an object or JSON object string"
        if isinstance(parsed, dict):
            return parsed, None
    return {}, f"{field_name} must be an object"


def _coerce_headers(headers: Any) -> tuple[dict[str, str], Optional[str]]:
    parsed, error = _coerce_dict(headers, "headers")
    if error:
        return {}, error
    return {str(key): str(value) for key, value in parsed.items()}, None


def _selected_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in SELECTED_RESPONSE_HEADERS
    }


def _looks_binary(content: bytes, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type.startswith(("image/", "audio/", "video/")):
        return True
    if media_type in {"application/octet-stream", "application/pdf", "application/zip"}:
        return True
    return b"\x00" in content[:2048]


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n\n[Truncated, {omitted} characters omitted]", True


def _body_for_request(body: Any) -> dict[str, Any]:
    if body is None:
        return {}
    if isinstance(body, (dict, list, int, float, bool)):
        return {"json": body}
    if isinstance(body, str):
        return {"content": body}
    return {"json": body}


def _request_with_policy(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    body_kwargs: Optional[dict[str, Any]] = None,
    follow_redirects: bool = True,
    policy_config: Optional[HTTPPolicyConfig] = None,
    policy_resolver: Optional[Any] = None,
) -> tuple[httpx.Response, list[dict[str, Any]], dict[str, Any]]:
    """Send an HTTP request with Nymeria's egress policy applied per hop."""
    policy_config = policy_config or load_http_policy_config()
    redirect_chain: list[dict[str, Any]] = []
    current_method = method
    current_url = url
    current_params = params
    current_body_kwargs = body_kwargs or {}

    while True:
        decision = evaluate_http_url(
            current_url,
            config=policy_config,
            resolver=policy_resolver,
        )
        if not decision.allowed:
            raise _HTTPPolicyBlocked(decision, redirect_chain)

        response = client.request(
            current_method,
            current_url,
            headers=headers or None,
            params=current_params or None,
            **current_body_kwargs,
        )

        if not follow_redirects or not response.is_redirect:
            return response, redirect_chain, decision.to_dict()

        location = response.headers.get("location")
        if not location:
            return response, redirect_chain, decision.to_dict()

        redirect_url = str(response.url.join(location))
        redirect_decision = redirect_allowed(
            str(response.url),
            redirect_url,
            config=policy_config,
            resolver=policy_resolver,
        )
        redirect_chain.append(
            {
                "status_code": response.status_code,
                "url": str(response.url),
                "location": location,
                "redirect_url": redirect_url,
                "policy": redirect_decision.to_dict(),
            }
        )
        if not redirect_decision.allowed:
            raise _HTTPPolicyBlocked(redirect_decision, redirect_chain)
        if len(redirect_chain) > policy_config.max_redirects:
            raise _HTTPTooManyRedirects(redirect_chain)

        if response.status_code in {301, 302, 303} and current_method != "HEAD":
            current_method = "GET"
            current_body_kwargs = {}
        current_url = redirect_url
        current_params = None


def _body_payload(body: Any, body_type: str, truncated: bool, preview: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "body_type": body_type,
        "body_format": body_type,
        "body_truncated": truncated,
        "body": None if truncated else body,
        "format_ok": True,
    }
    if truncated:
        payload["body_preview"] = preview if preview is not None else str(body)
    return payload


def _parse_response_body(
    response: httpx.Response,
    response_format: str,
    max_response_chars: int,
) -> dict[str, Any]:
    content = response.content
    if not content:
        return _body_payload(None, "empty", False)

    content_type = response.headers.get("content-type", "")
    is_json = "json" in content_type.lower()

    if response_format in {"auto", "json"} and is_json:
        try:
            body = response.json()
            serialized = json.dumps(body, indent=2, default=str)
            if len(serialized) <= max_response_chars:
                result = _body_payload(body, "json", False)
                result["json_parse_ok"] = True
                return result
            truncated, _ = _truncate_text(serialized, max_response_chars)
            result = _body_payload(body, "json", True, truncated)
            result["json_parse_ok"] = True
            return result
        except json.JSONDecodeError as exc:
            if response_format == "json":
                text, truncated = _truncate_text(response.text, max_response_chars)
                result = _body_payload(text, "text", truncated, text)
                result.update(
                    {
                        "format_ok": False,
                        "json_parse_ok": False,
                        "parse_error": f"JSON parse failed: {exc}",
                    }
                )
                return result

    if response_format == "json":
        try:
            body = response.json()
            result = _body_payload(body, "json", False)
            result["json_parse_ok"] = True
            return result
        except json.JSONDecodeError as exc:
            text, truncated = _truncate_text(response.text, max_response_chars)
            result = _body_payload(text, "text", truncated, text)
            result.update(
                {
                    "format_ok": False,
                    "json_parse_ok": False,
                    "parse_error": f"JSON parse failed: {exc}",
                }
            )
            return result

    if _looks_binary(content, content_type):
        result = _body_payload(None, "binary", False)
        result["body_omitted"] = True
        result["body_preview"] = (
            f"[Binary response omitted: {len(content)} bytes, "
            f"content-type={content_type or 'unknown'}]"
        )
        return result

    text, truncated = _truncate_text(response.text, max_response_chars)
    return _body_payload(text, "text", truncated, text)


def _http_request_impl(
    method: str,
    url: str,
    headers: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, Any]] = None,
    body: Any = None,
    timeout_seconds: int = 30,
    follow_redirects: bool = True,
    response_format: str = "auto",
    max_response_chars: int = 20_000,
    transport: Optional[httpx.BaseTransport] = None,
    policy_config: Optional[HTTPPolicyConfig] = None,
    policy_resolver: Optional[Any] = None,
    audit_tool_name: str = "http_request",
    used_env_secrets: Optional[list[str]] = None,
    used_credentials: Optional[list[str]] = None,
    redact_values: Optional[list[str]] = None,
) -> dict[str, Any]:
    method = (method or "").upper().strip()
    if method not in HTTP_METHODS:
        return _request_error(
            "validation_error",
            f"method must be one of {', '.join(sorted(HTTP_METHODS))}",
        )

    normalized_url, url_error = _normalize_url(url)
    if url_error:
        return _request_error("validation_error", url_error)

    parsed_headers, header_error = _coerce_headers(headers)
    if header_error:
        return _request_error("validation_error", header_error)

    parsed_query, query_error = _coerce_dict(query, "query")
    if query_error:
        return _request_error("validation_error", query_error)

    response_format = (response_format or "auto").lower().strip()
    if response_format not in RESPONSE_FORMATS:
        return _request_error(
            "validation_error",
            "response_format must be auto, json, or text",
        )

    timeout_seconds = _clamp_int(timeout_seconds, 30, 1, MAX_TIMEOUT_SECONDS)
    max_response_chars = _clamp_int(max_response_chars, 20_000, 1, MAX_RESPONSE_CHARS)
    policy_config = policy_config or load_http_policy_config()

    started = time.perf_counter()
    logger.info("HTTP request tool: %s %s", method, _redact_text(normalized_url, redact_values))

    try:
        # Test transports are frequently backed by synthetic hostnames; still
        # apply literal IP/metadata/domain policy, but skip DNS in that mode.
        effective_policy = policy_config
        if transport is not None and policy_config.resolve_dns:
            effective_policy = HTTPPolicyConfig(
                internal_allowlist=policy_config.internal_allowlist,
                domain_allowlist=policy_config.domain_allowlist,
                domain_blocklist=policy_config.domain_blocklist,
                max_redirects=policy_config.max_redirects,
                allow_https_to_http_redirect=policy_config.allow_https_to_http_redirect,
                resolve_dns=False,
            )
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response, redirect_chain, policy = _request_with_policy(
                client,
                method,
                normalized_url,
                headers=parsed_headers or None,
                params=parsed_query or None,
                body_kwargs=_body_for_request(body),
                follow_redirects=follow_redirects,
                policy_config=effective_policy,
                policy_resolver=policy_resolver,
            )
    except _HTTPPolicyBlocked as exc:
        return _blocked_network_result(
            exc.decision,
            method=method,
            url=normalized_url,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            redirect_chain=exc.redirect_chain,
            headers=parsed_headers,
            body=body,
            tool_name=audit_tool_name,
            used_env_secrets=used_env_secrets,
            used_credentials=used_credentials,
            redact_values=redact_values,
        )
    except _HTTPTooManyRedirects as exc:
        result = _request_error(
            "TooManyRedirects",
            f"Exceeded maximum redirect count of {policy_config.max_redirects}",
            request={"method": method, "url": normalized_url},
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        result["redirect_chain"] = exc.redirect_chain
        _audit_http_result(
            tool_name=audit_tool_name,
            method=method,
            url=normalized_url,
            result=result,
            headers=parsed_headers,
            body=body,
            used_env_secrets=used_env_secrets,
            used_credentials=used_credentials,
            redact_values=redact_values,
        )
        result = _redact_sensitive(result, redact_values)
        return result
    except httpx.TimeoutException as exc:
        result = _request_error(
            type(exc).__name__,
            f"Request timed out after {timeout_seconds} seconds",
            request={"method": method, "url": normalized_url},
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        _audit_http_result(
            tool_name=audit_tool_name,
            method=method,
            url=normalized_url,
            result=result,
            headers=parsed_headers,
            body=body,
            used_env_secrets=used_env_secrets,
            used_credentials=used_credentials,
            redact_values=redact_values,
        )
        result = _redact_sensitive(result, redact_values)
        return result
    except httpx.RequestError as exc:
        result = _request_error(
            type(exc).__name__,
            str(exc),
            request={"method": method, "url": normalized_url},
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        _audit_http_result(
            tool_name=audit_tool_name,
            method=method,
            url=normalized_url,
            result=result,
            headers=parsed_headers,
            body=body,
            used_env_secrets=used_env_secrets,
            used_credentials=used_credentials,
            redact_values=redact_values,
        )
        result = _redact_sensitive(result, redact_values)
        return result
    except Exception as exc:
        logger.error("HTTP request tool failed", exc_info=True)
        result = _request_error(
            type(exc).__name__,
            str(exc),
            request={"method": method, "url": normalized_url},
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        _audit_http_result(
            tool_name=audit_tool_name,
            method=method,
            url=normalized_url,
            result=result,
            headers=parsed_headers,
            body=body,
            used_env_secrets=used_env_secrets,
            used_credentials=used_credentials,
            redact_values=redact_values,
        )
        result = _redact_sensitive(result, redact_values)
        return result

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    body_info = _parse_response_body(
        response,
        response_format,
        max_response_chars,
    )
    http_ok = response.is_success
    format_ok = bool(body_info.get("format_ok", True))

    result: dict[str, Any] = _base_result(
        ok=http_ok and format_ok,
        http_ok=http_ok,
        request={
            "method": method,
            "url": str(response.request.url),
        },
        response={
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "url": str(response.url),
            "headers": _selected_headers(response.headers),
            "elapsed_ms": elapsed_ms,
        },
        policy=policy,
    )
    result.update(body_info)
    if redirect_chain:
        result["redirect_chain"] = redirect_chain
    if used_env_secrets:
        result["used_env_secrets"] = sorted(used_env_secrets)
    if used_credentials:
        result["used_credentials"] = sorted(used_credentials)

    if not http_ok:
        location = response.headers.get("location")
        if response.is_redirect:
            error = {
                "type": "http_redirect",
                "message": f"HTTP {response.status_code} {response.reason_phrase}",
            }
            if location:
                error["location"] = location
                error["redirect_url"] = str(response.url.join(location))
        else:
            error = {
                "type": "http_status",
                "message": f"HTTP {response.status_code} {response.reason_phrase}",
            }
        result["error"] = error
    elif not format_ok:
        result["error"] = {
            "type": "response_parse_error",
            "message": body_info.get("parse_error", "Response did not match requested response_format"),
        }

    _audit_http_result(
        tool_name=audit_tool_name,
        method=method,
        url=normalized_url,
        result=result,
        headers=parsed_headers,
        body=body,
        used_env_secrets=used_env_secrets,
        used_credentials=used_credentials,
        redact_values=redact_values,
    )
    result = _redact_sensitive(result, redact_values)
    return result


def _parse_possible_spec(url: str, text: str, content_type: str) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
    stripped = text.lstrip()
    lower_url = url.lower()
    lower_type = content_type.lower()
    yaml_markers = re.search(r"(?m)^\s*(openapi|swagger|info|paths):\s*", stripped[:2000])

    should_try_json = (
        "json" in lower_type
        or lower_url.endswith(".json")
        or stripped.startswith("{")
    )
    should_try_yaml = (
        "yaml" in lower_type
        or "yml" in lower_type
        or lower_url.endswith((".yaml", ".yml"))
        or (not should_try_json and yaml_markers is not None)
    )

    errors = []
    if should_try_json:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed, "json", None
        except json.JSONDecodeError as exc:
            errors.append(f"json: {exc}")

    if should_try_yaml:
        try:
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                return parsed, "yaml", None
        except yaml.YAMLError as exc:
            errors.append(f"yaml: {exc}")

    return None, None, "; ".join(errors) if errors else None


def _is_openapi_spec(data: Any) -> bool:
    return isinstance(data, dict) and (
        "openapi" in data
        or "swagger" in data
        or isinstance(data.get("paths"), dict)
    )


def _summarize_openapi(data: dict[str, Any]) -> dict[str, Any]:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}

    path_summaries = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        methods = [
            key.upper()
            for key in path_item.keys()
            if key.upper() in HTTP_METHODS
        ]
        if methods:
            path_summaries.append({"path": path, "methods": sorted(methods)})
        if len(path_summaries) >= 25:
            break

    servers = []
    for server in data.get("servers") or []:
        if isinstance(server, dict) and server.get("url"):
            servers.append(server["url"])
        elif isinstance(server, str):
            servers.append(server)
        if len(servers) >= 10:
            break

    if not servers and data.get("host"):
        schemes = data.get("schemes") or ["https"]
        base_path = data.get("basePath") or ""
        servers = [f"{scheme}://{data['host']}{base_path}" for scheme in schemes[:3]]

    components = data.get("components") if isinstance(data.get("components"), dict) else {}
    security_schemes = components.get("securitySchemes")
    if not isinstance(security_schemes, dict):
        security_schemes = data.get("securityDefinitions")
    security_scheme_names = sorted(security_schemes.keys())[:25] if isinstance(security_schemes, dict) else []

    description = info.get("description")
    if isinstance(description, str):
        description, _ = _truncate_text(description, 1000)

    return {
        "title": info.get("title"),
        "version": info.get("version"),
        "description": description,
        "openapi_version": data.get("openapi") or data.get("swagger"),
        "servers": servers,
        "path_count": len(paths),
        "sample_paths": path_summaries,
        "security_schemes": security_scheme_names,
    }


def _looks_like_spec_url(url: str) -> bool:
    lower = url.lower()
    return (
        "openapi" in lower
        or "swagger.json" in lower
        or "swagger.yaml" in lower
        or "swagger.yml" in lower
        or "api-docs" in lower
        or lower.endswith(("/swagger", "/openapi"))
    )


def _looks_like_swagger_config_script(url: str) -> bool:
    lower = url.lower()
    if not lower.endswith(".js"):
        return False
    if any(skip in lower for skip in ("swagger-ui-bundle", "swagger-ui-standalone", "swagger-ui-es-bundle")):
        return False
    return any(marker in lower for marker in ("swagger", "openapi", "initializer", "config", "redoc", "rapidoc"))


def _prioritize_base_host(urls: list[str], base_url: str) -> list[str]:
    base_host = urlparse(base_url).netloc.lower()
    return sorted(
        urls,
        key=lambda item: (
            urlparse(item).netloc.lower() != base_host,
            item,
        ),
    )


def _extract_spec_urls_from_text(text: str, base_url: str, limit: int = 20) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    patterns = [
        r"""["'`]([^"'`\s]+(?:openapi|swagger|api-docs)[^"'`\s]*)["'`]""",
        r"""[A-Za-z0-9_.:-]+=(https?://[^\s,'"`]+(?:openapi|swagger|api-docs)[^\s,'"`]*)""",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            candidate = match.group(1).strip()
            if "=" in candidate and not candidate.startswith(("http://", "https://", "/")):
                continue
            if not _looks_like_spec_url(candidate):
                continue
            absolute = urljoin(base_url, candidate)
            normalized, error = _normalize_url(absolute)
            if error or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
            if len(urls) >= limit:
                return _prioritize_base_host(urls, base_url)

    return _prioritize_base_host(urls, base_url)


def _extract_html_discovery_links(html: str, base_url: str, limit: int = 20) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    for spec_url in _extract_spec_urls_from_text(html, base_url, limit=limit):
        if spec_url not in seen:
            seen.add(spec_url)
            links.append(("docs_spec_link", spec_url))
        if len(links) >= limit:
            return links

    for match in re.finditer(r"""(?:href|src)=["']([^"']+)["']""", html, re.IGNORECASE):
        link = match.group(1)
        absolute = urljoin(base_url, link)
        if absolute in seen:
            continue

        if _looks_like_spec_url(link):
            source = "docs_spec_link"
        elif _looks_like_swagger_config_script(link):
            source = "swagger_ui_script"
        else:
            continue

        seen.add(absolute)
        links.append((source, absolute))
        if len(links) >= limit:
            break
    return links


def _enqueue_discovered(
    queue: list[tuple[str, str]],
    seen: set[str],
    insert_at: int,
    candidates: list[tuple[str, str]],
) -> None:
    offset = 0
    for source, url in candidates:
        if url in seen:
            continue
        seen.add(url)
        queue.insert(insert_at + offset, (source, url))
        offset += 1


def _candidate_urls(base_url: str, docs_url: Optional[str]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []

    if docs_url:
        if urlparse(docs_url).scheme:
            candidates.append(("docs_url", docs_url))
        else:
            candidates.append(("docs_url", urljoin(base_url.rstrip("/") + "/", docs_url)))

    for path in SPEC_PATHS:
        candidates.append(("common_path", urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))))

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, url in candidates:
        if url not in seen:
            seen.add(url)
            deduped.append((source, url))
    return deduped


def _api_discover_impl(
    base_url: str,
    docs_url: Optional[str] = None,
    timeout_seconds: int = 20,
    max_response_chars: int = 50_000,
    transport: Optional[httpx.BaseTransport] = None,
    policy_config: Optional[HTTPPolicyConfig] = None,
    policy_resolver: Optional[Any] = None,
) -> dict[str, Any]:
    normalized_base, base_error = _normalize_url(base_url, "base_url")
    if base_error:
        return _base_result(
            ok=False,
            found=False,
            error={"type": "validation_error", "message": base_error},
        )

    if docs_url:
        candidate_docs_url = docs_url if urlparse(docs_url).scheme else urljoin(normalized_base.rstrip("/") + "/", docs_url)
        _, docs_error = _normalize_url(candidate_docs_url, "docs_url")
        if docs_error:
            return _base_result(
                ok=False,
                found=False,
                error={"type": "validation_error", "message": docs_error},
            )

    timeout_seconds = _clamp_int(timeout_seconds, 20, 1, MAX_TIMEOUT_SECONDS)
    max_response_chars = _clamp_int(max_response_chars, 50_000, 1, MAX_RESPONSE_CHARS)
    policy_config = policy_config or load_http_policy_config()
    if transport is not None and policy_config.resolve_dns:
        policy_config = HTTPPolicyConfig(
            internal_allowlist=policy_config.internal_allowlist,
            domain_allowlist=policy_config.domain_allowlist,
            domain_blocklist=policy_config.domain_blocklist,
            max_redirects=policy_config.max_redirects,
            allow_https_to_http_redirect=policy_config.allow_https_to_http_redirect,
            resolve_dns=False,
        )

    base_decision = evaluate_http_url(
        normalized_base,
        config=policy_config,
        resolver=policy_resolver,
    )
    if not base_decision.allowed:
        result = _base_result(
            ok=False,
            found=False,
            base_url=normalized_base,
            error=blocked_network_error(base_decision),
            policy=base_decision.to_dict(),
        )
        _audit_api_discover_result(normalized_base, docs_url, result)
        return result

    headers = {
        "Accept": "application/json, application/yaml, text/yaml, text/html;q=0.8, */*;q=0.5",
    }
    queue = _candidate_urls(normalized_base, docs_url)
    tried: list[dict[str, Any]] = []
    docs_excerpt: Optional[str] = None
    seen = {url for _, url in queue}

    logger.info("API discovery tool: base_url=%s docs_url=%s", normalized_base, docs_url)

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False, transport=transport) as client:
            index = 0
            while index < len(queue) and index < 25:
                source, url = queue[index]
                index += 1
                record: dict[str, Any] = {"url": url, "source": source}

                try:
                    response, redirect_chain, policy = _request_with_policy(
                        client,
                        "GET",
                        url,
                        headers=headers,
                        follow_redirects=True,
                        policy_config=policy_config,
                        policy_resolver=policy_resolver,
                    )
                except _HTTPPolicyBlocked as exc:
                    record["error"] = blocked_network_error(exc.decision)
                    record["policy"] = exc.decision.to_dict()
                    if exc.redirect_chain:
                        record["redirect_chain"] = exc.redirect_chain
                    tried.append(record)
                    continue
                except _HTTPTooManyRedirects as exc:
                    record["error"] = {
                        "type": "TooManyRedirects",
                        "message": f"Exceeded maximum redirect count of {policy_config.max_redirects}",
                    }
                    record["redirect_chain"] = exc.redirect_chain
                    tried.append(record)
                    continue
                except httpx.TimeoutException as exc:
                    record["error"] = {
                        "type": type(exc).__name__,
                        "message": f"Request timed out after {timeout_seconds} seconds",
                    }
                    tried.append(record)
                    continue
                except httpx.RequestError as exc:
                    record["error"] = {"type": type(exc).__name__, "message": str(exc)}
                    tried.append(record)
                    continue

                content_type = response.headers.get("content-type", "")
                record.update(
                    {
                        "status_code": response.status_code,
                        "content_type": content_type,
                        "policy": policy,
                    }
                )
                if redirect_chain:
                    record["redirect_chain"] = redirect_chain

                if response.status_code != 200:
                    tried.append(record)
                    continue

                text = response.text
                lower_type = content_type.lower()
                is_html = "html" in lower_type or "<html" in text[:1000].lower()
                is_script = (
                    "javascript" in lower_type
                    or url.lower().endswith(".js")
                    or source == "swagger_ui_script"
                )

                if is_script:
                    discovered = [
                        ("swagger_ui_spec_url", spec_url)
                        for spec_url in _extract_spec_urls_from_text(text, normalized_base)
                    ]
                    _enqueue_discovered(queue, seen, index, discovered)
                    record["result"] = "script_spec_links_found" if discovered else "script_no_spec"
                    tried.append(record)
                    continue

                parsed, spec_format, parse_error = _parse_possible_spec(url, text, content_type)
                if parsed is not None and _is_openapi_spec(parsed):
                    record["result"] = "openapi_spec"
                    tried.append(record)
                    result = _base_result(
                        ok=True,
                        found=True,
                        base_url=normalized_base,
                        spec_url=url,
                        spec_format=spec_format,
                        summary=_summarize_openapi(parsed),
                        tried=tried,
                    )
                    _audit_api_discover_result(normalized_base, docs_url, result)
                    return result

                if parse_error:
                    record["parse_error"] = parse_error

                if is_html:
                    if docs_excerpt is None:
                        docs_excerpt, _ = _truncate_text(text, min(max_response_chars, 4000))
                    discovered = _extract_html_discovery_links(text, url)
                    _enqueue_discovered(queue, seen, index, discovered)
                    if discovered:
                        record["discovered_links"] = [
                            {"source": link_source, "url": link_url}
                            for link_source, link_url in discovered
                        ]
                    record["result"] = "html_no_spec"
                else:
                    record["result"] = "not_openapi_spec"

                tried.append(record)

    except Exception as exc:
        logger.error("API discovery tool failed", exc_info=True)
        result = _base_result(
            ok=False,
            found=False,
            base_url=normalized_base,
            tried=tried,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _audit_api_discover_result(normalized_base, docs_url, result)
        return result

    result: dict[str, Any] = _base_result(
        ok=True,
        found=False,
        base_url=normalized_base,
        tried=tried,
        hints=[
            "If the API has documentation, pass its exact URL as docs_url.",
            "If no OpenAPI/Swagger spec exists, use web_search for official docs and examples, then call http_request with the documented endpoint and payload.",
            "For GraphQL APIs, try an OPTIONS request or the documented GraphQL endpoint with http_request.",
        ],
    )
    if docs_excerpt:
        result["docs_excerpt"] = docs_excerpt
    _audit_api_discover_result(normalized_base, docs_url, result)
    return result


@tool
def http_request(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    query: Optional[dict] = None,
    body: Optional[Any] = None,
    timeout_seconds: int = 30,
    follow_redirects: bool = True,
    response_format: str = "auto",
    max_response_chars: int = 20_000,
) -> str:
    """Make a one-off HTTP request to a documented API endpoint.

    Use this as a general API primitive after you know the endpoint, method,
    auth headers, query parameters, and body shape from docs or examples.
    This is not a saved connector; use custom HTTP tools for reusable
    parameterized integrations.

    Args:
        method: HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, or OPTIONS.
        url: Absolute http:// or https:// URL.
        headers: Optional request headers. Do not include secrets unless the
            user has explicitly provided or approved them for this request.
        query: Optional query parameters.
        body: Optional JSON-serializable body. Strings are sent as raw content.
        timeout_seconds: Request timeout, clamped to 1-300 seconds.
        follow_redirects: Whether to follow redirects.
        response_format: "auto", "json", or "text".
        max_response_chars: Maximum returned body characters, clamped to 1-200000.

    Returns:
        Structured JSON. `ok` means HTTP status success and requested format
        success; `http_ok` is status-only. Truncated bodies use body_preview
        with body=null so partial JSON is not mistaken for a complete object.
    """
    return _dump_json(
        _http_request_impl(
            method=method,
            url=url,
            headers=headers,
            query=query,
            body=body,
            timeout_seconds=timeout_seconds,
            follow_redirects=follow_redirects,
            response_format=response_format,
            max_response_chars=max_response_chars,
        )
    )


@tool
def api_discover(
    base_url: str,
    docs_url: Optional[str] = None,
    timeout_seconds: int = 20,
    max_response_chars: int = 50_000,
) -> str:
    """Discover OpenAPI/Swagger details for an API base URL.

    Tries common OpenAPI/Swagger locations under base_url and optionally
    inspects docs_url for direct spec links. Use this before http_request
    when you need machine-readable endpoint and payload information.

    Args:
        base_url: Absolute API base URL, such as https://api.example.com.
        docs_url: Optional exact documentation URL or relative docs path.
        timeout_seconds: Per-request timeout, clamped to 1-300 seconds.
        max_response_chars: Maximum docs excerpt characters when no spec is found.

    Returns:
        Structured JSON with found/spec_url/spec summary/tried URLs/hints.
        Swagger UI docs pages are inspected for initializer/config scripts that
        point to OpenAPI/Swagger definitions.
    """
    return _dump_json(
        _api_discover_impl(
            base_url=base_url,
            docs_url=docs_url,
            timeout_seconds=timeout_seconds,
            max_response_chars=max_response_chars,
        )
    )


HTTP_API_TOOLS = [http_request, api_discover]
