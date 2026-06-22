"""Shared foundation helpers for the ``*_service_integrations`` tool modules.

Each ``<area>_service_integrations.py`` module wraps a family of third-party HTTP
APIs as ``@tool`` functions. Historically every module re-declared the same small
foundation layer (JSON dumping, value filtering, base-URL validation, credential
and settings resolution, setup hints). Those copies were byte-identical yet
maintained independently, so a single fix had to be applied ~29 times and the
copies had begun to drift.

This module is the canonical home for the stable, behavior-identical helpers. Each
integration module imports them under its existing private name so the per-module
monkeypatch test seam is preserved, e.g.::

    from .service_integration_base import (
        credential_value as _credential_value,
        dump_json,
    )

The aliasing is mandatory: the integration tests do
``monkeypatch.setattr(<module>, "_credential_value", ...)`` and the tool bodies
call the bare name ``_credential_value(...)``, which resolves the module global at
call time and picks up the patch. Tool bodies must therefore keep calling the bare
aliased names rather than ``service_integration_base.credential_value`` directly.

Deliberately NOT centralized here yet (the copies genuinely diverge in
agent-visible behavior, so consolidating them is a behavior decision, not a pure
refactor):

* ``_request_json`` / ``_request_any``: the per-module copies differ in
  error-detail key precedence, non-JSON-200 fallback (raise vs return text), and
  form encoding (``commerce_billing`` flattens nested form fields). Folding them
  into one superset would change runtime error strings and fallback shapes.
* ``_limit``: the per-module default/max clamps differ and several call sites
  rely on the module-local defaults by omitting the keyword arguments.
* ``_csv_to_list``: some copies are newline-tolerant and some are not.

These are tracked as follow-ups in the slice 13 optimization report.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional
from urllib.parse import urlparse

from langchain_core.runnables import RunnableConfig

# Default truncation budget for tool output. Modules that legitimately need a
# larger budget (bulk row dumps, log payloads) keep an explicit module-local
# override and pass it through ``dump_json(..., max_chars=...)``.
DEFAULT_MAX_JSON_CHARS = 60_000


def dump_json(data: Any, *, max_chars: int = DEFAULT_MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def filtered(values: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (values or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    from ..core.http_policy import validate_http_egress_url

    return validate_http_egress_url(value.strip().rstrip("/"), label="base URL", resolve_dns=False)


def json_object(value: str, *, field_name: str) -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def json_array_or_object(value: str, *, field_name: str) -> list[dict[str, Any]]:
    if not value or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    records = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{field_name} must be a JSON object or array of objects")
    return records


def basic_auth(username: str, password: str = "") -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def settings_value(name: str) -> Optional[str]:
    from ..config import get_settings

    return getattr(get_settings(), name)


def credential_value(
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


def setup_hint(
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
