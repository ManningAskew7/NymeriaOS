"""Provider credential commands: /provider."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    has_client_method,
    mapping_get,
    one_line,
    unsupported_transport_result,
)
from ..credentials_provider import (
    ProviderCredentialRecord,
    default_provider_credentials_path,
    load_provider_credentials,
    save_provider_credentials,
)

PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
}

PROVIDER_SECRET_SETTINGS = {
    "anthropic": {
        "api_key": "anthropic_api_key",
        "direct_api_key": "anthropic_direct_api_key",
    },
    "openai": {"api_key": "openai_api_key"},
    "openrouter": {"api_key": "openrouter_api_key"},
}

DEFAULT_PROVIDER_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-sonnet-4.5",
}

OPENAI_API_MODES = {"chat_completions", "responses"}
PROVIDERS = tuple(PROVIDER_LABELS)


async def _handle_provider(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /provider [list|set|test|switch]",
            error_code="usage_error",
        )
    return await _handle_provider_show(context, args)


async def _handle_provider_show(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    settings = await _settings_or_empty(context)
    status = await _provider_status_map(context)
    config = load_provider_credentials(_credentials_path(context))
    active_provider = _normalize_provider(mapping_get(settings, "llm_provider", ""))
    active = status.get(active_provider)

    rows = [
        ("Active provider", _provider_label(active_provider) if active_provider else "Unknown"),
        ("Model", mapping_get(settings, "llm_model", "") or "Unknown"),
        ("Base URL", mapping_get(settings, "llm_base_url", "") or "Provider default"),
        (
            "Credential",
            _status_text(active) if active else "missing key",
        ),
        ("Local store", str(_credentials_path(context))),
    ]
    lines = _aligned_rows(rows, title="Provider")
    providers = _provider_entries(settings, status, config)
    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Provider"),
        json_payload={
            "active_provider": active_provider,
            "providers": providers,
            "credential_store": str(_credentials_path(context)),
        },
    )


async def _handle_provider_list(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    settings = await _settings_or_empty(context)
    status = await _provider_status_map(context)
    config = load_provider_credentials(_credentials_path(context))
    entries = _provider_entries(settings, status, config)

    lines = ["Providers", "  Provider    Active  Status          Source"]
    for entry in entries:
        active = "yes" if entry["active"] else "no"
        lines.append(
            f"  {entry['provider']:<10}  {active:<6} "
            f"{entry['status']:<14} {entry['source'] or '-'}"
        )
    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Providers"),
        json_payload=entries,
    )


async def _handle_provider_set(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) < 2:
        return CommandResult.failed(
            "Usage: /provider set <provider> <key=value> [key=value...]",
            error_code="usage_error",
        )

    provider = _normalize_provider(args[0])
    if provider not in PROVIDERS:
        return _unknown_provider_result(args[0])

    values, error = _parse_provider_values(provider, args[1:])
    if error:
        return CommandResult.failed(error, error_code="usage_error")

    save_provider_credentials(
        provider,
        values,
        path=_credentials_path(context),
    )

    patch = _settings_patch_for_credentials(provider, values)
    if not patch:
        return CommandResult.failed(
            "No backend settings mapped for those credential fields.",
            error_code="provider_setting_unmapped",
        )

    if not has_client_method(context, "update_settings"):
        fields = ", ".join(sorted(values))
        return CommandResult.completed(
            CommandMessage(
                f"Saved {_provider_label(provider)} credentials locally ({fields}). "
                "Connect with /login to apply them to the backend.",
                level="warning",
            ),
            payload={"provider": provider, "saved": True, "applied": False},
        )

    try:
        result = await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/provider set", method_name=exc.method_name)
    except Exception as exc:  # noqa: BLE001 - keep partial local-save visible.
        return CommandResult.failed(
            "Saved credentials locally, but backend apply failed: "
            f"{_redact_secrets(one_line(exc, limit=240), values.values())}",
            error_code="provider_apply_failed",
            payload={"provider": provider, "saved": True, "applied": False},
        )

    updated = mapping_get(result, "updated", sorted(patch))
    fields = ", ".join(sorted(values))
    suffix = _restart_suffix(result)
    return CommandResult.completed(
        CommandMessage(
            f"Saved {_provider_label(provider)} credentials ({fields}) and applied "
            f"backend settings: {', '.join(str(item) for item in updated)}.{suffix}",
            level="success",
        ),
        payload={"provider": provider, "saved": True, "applied": True},
    )


async def _handle_provider_switch(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) != 1:
        return CommandResult.failed(
            "Usage: /provider switch <provider>",
            error_code="usage_error",
        )

    provider = _normalize_provider(args[0])
    if provider not in PROVIDERS:
        return _unknown_provider_result(args[0])

    config = load_provider_credentials(_credentials_path(context))
    record = config.get(provider)
    patch = {"llm_provider": provider}
    if record is not None:
        patch.update(_settings_patch_for_credentials(provider, record.values))

    try:
        result = await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/provider switch", method_name=exc.method_name)

    suffix = " Saved local credential was applied." if record and record.has_secret else (
        " No local credential is stored for this provider."
    )
    return CommandResult.completed(
        CommandMessage(
            f"Switched provider to {_provider_label(provider)}.{suffix}{_restart_suffix(result)}",
            level="success",
        ),
        payload={
            "provider": provider,
            "credential_applied": bool(record and record.has_secret),
        },
    )


async def _handle_provider_test(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) > 1:
        return CommandResult.failed(
            "Usage: /provider test [provider]",
            error_code="usage_error",
        )

    settings = await _settings_or_empty(context)
    provider = _normalize_provider(args[0] if args else mapping_get(settings, "llm_provider", ""))
    if provider not in PROVIDERS:
        return _unknown_provider_result(provider or "<active>")

    if not has_client_method(context, "test_llm_provider_config"):
        return unsupported_transport_result(
            "/provider test",
            method_name="test_llm_provider_config",
        )

    api_key = await _credential_for_test(context, provider, settings)
    if not api_key:
        return CommandResult.failed(
            f"No credential available for {_provider_label(provider)}. "
            f"Run /provider set {provider} api_key=<key> first.",
            error_code="provider_credential_missing",
        )

    request = _provider_test_request(provider, api_key, settings)
    try:
        result = await call_client_method(
            context,
            "test_llm_provider_config",
            request,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/provider test", method_name=exc.method_name)

    payload = _redacted_test_result(result)
    ok = bool(mapping_get(result, "ok", False))
    message = str(mapping_get(result, "message", "") or "")
    if ok:
        return CommandResult.completed(
            CommandMessage(
                f"{_provider_label(provider)} provider test succeeded.",
                level="success",
            ),
            payload=payload,
            json_payload=payload,
        )
    return CommandResult.failed(
        f"{_provider_label(provider)} provider test failed: {one_line(message, limit=260)}",
        error_code="provider_test_failed",
        payload=payload,
        json_payload=payload,
    )


async def _settings_or_empty(context: CommandContext) -> Mapping[str, Any]:
    try:
        settings = await call_client_method(
            context,
            "get_settings",
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return {}
    except Exception:  # noqa: BLE001 - status views should still show local store.
        return {}
    return settings if isinstance(settings, Mapping) else {}


async def _provider_status_map(
    context: CommandContext,
) -> dict[str, dict[str, str]]:
    env_fields = await _server_env_status(context)
    config = load_provider_credentials(_credentials_path(context))
    statuses: dict[str, dict[str, str]] = {}
    env_error = env_fields.get("__error__", "")
    for provider in PROVIDERS:
        record = config.get(provider)
        server_set = any(
            env_fields.get(setting) == "set"
            for setting in PROVIDER_SECRET_SETTINGS[provider].values()
        )
        local_set = bool(record and record.has_secret)
        if server_set:
            statuses[provider] = {"status": "authenticated", "source": "server"}
        elif local_set:
            statuses[provider] = {"status": "authenticated", "source": "local"}
        elif env_error:
            statuses[provider] = {"status": "error", "source": env_error}
        else:
            statuses[provider] = {"status": "missing key", "source": ""}
    return statuses


async def _server_env_status(context: CommandContext) -> dict[str, str]:
    if not has_client_method(context, "get_env_vars"):
        return {}
    try:
        data = await call_client_method(context, "get_env_vars", user_id=context.user_id)
    except Exception as exc:  # noqa: BLE001 - status view should not fail hard.
        return {"__error__": one_line(exc, limit=60) or "unavailable"}

    raw_entries = mapping_get(data, "entries", [])
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        return {}
    output: dict[str, str] = {}
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        output[name] = "set" if bool(entry.get("is_set", False)) else "missing"
    return output


async def _credential_for_test(
    context: CommandContext,
    provider: str,
    settings: Mapping[str, Any],
) -> str:
    config = load_provider_credentials(_credentials_path(context))
    record = config.get(provider)
    base_url = str(mapping_get(settings, "llm_base_url", "") or "")
    if record is not None:
        value = _credential_from_record(provider, record, base_url=base_url)
        if value:
            return value

    for field in _credential_field_order(provider, base_url=base_url):
        setting_name = PROVIDER_SECRET_SETTINGS[provider][field]
        value = await _get_server_secret(context, setting_name)
        if value:
            return value
    return ""


async def _get_server_secret(context: CommandContext, setting_name: str) -> str:
    if not has_client_method(context, "get_env_var"):
        return ""
    try:
        data = await call_client_method(
            context,
            "get_env_var",
            setting_name,
            user_id=context.user_id,
        )
    except Exception:  # noqa: BLE001 - absence just means local key is required.
        return ""
    value = mapping_get(data, "value", "")
    return str(value or "").strip()


def _provider_entries(
    settings: Mapping[str, Any],
    status: Mapping[str, Mapping[str, str]],
    config: Any,
) -> list[dict[str, Any]]:
    active_provider = _normalize_provider(mapping_get(settings, "llm_provider", ""))
    entries: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        record = config.get(provider)
        status_entry = status.get(provider, {})
        entries.append(
            {
                "provider": provider,
                "label": _provider_label(provider),
                "active": provider == active_provider,
                "status": status_entry.get("status", "missing key"),
                "source": status_entry.get("source", ""),
                "local_fields": sorted(record.values) if record else [],
                "server_fields": sorted(PROVIDER_SECRET_SETTINGS[provider].values()),
            }
        )
    return entries


def _parse_provider_values(
    provider: str,
    args: Sequence[str],
) -> tuple[dict[str, str], str]:
    allowed = PROVIDER_SECRET_SETTINGS[provider]
    values: dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            return {}, f"Expected key=value credential field, got {arg!r}."
        key, value = arg.split("=", 1)
        key = _normalize_key(key)
        value = value.strip()
        if key not in allowed:
            return {}, (
                f"Unsupported {_provider_label(provider)} credential field: {key}. "
                f"Allowed fields: {', '.join(sorted(allowed))}"
            )
        if not value:
            return {}, f"Credential field {key} cannot be blank."
        values[key] = value
    return values, ""


def _settings_patch_for_credentials(
    provider: str,
    values: Mapping[str, str],
) -> dict[str, str]:
    allowed = PROVIDER_SECRET_SETTINGS[provider]
    patch: dict[str, str] = {}
    for key, value in values.items():
        setting_name = allowed.get(_normalize_key(key))
        if setting_name and value:
            patch[setting_name] = value
    return patch


def _provider_test_request(
    provider: str,
    api_key: str,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    active_provider = _normalize_provider(mapping_get(settings, "llm_provider", ""))
    active = provider == active_provider
    model = (
        str(mapping_get(settings, "llm_model", "") or "").strip()
        if active
        else ""
    )
    if not model:
        model = DEFAULT_PROVIDER_MODELS[provider]

    request: dict[str, Any] = {
        "llm_provider": provider,
        "llm_model": model,
        "api_key": api_key,
    }
    base_url = str(mapping_get(settings, "llm_base_url", "") or "").strip()
    if active and base_url:
        request["llm_base_url"] = base_url
    if provider in {"openai", "openrouter"}:
        mode = str(mapping_get(settings, "openai_api_mode", "") or "responses").strip()
        request["openai_api_mode"] = mode if mode in OPENAI_API_MODES else "responses"
    return request


def _credential_from_record(
    provider: str,
    record: ProviderCredentialRecord,
    *,
    base_url: str,
) -> str:
    for field in _credential_field_order(provider, base_url=base_url):
        value = record.values.get(field)
        if value:
            return value
    return ""


def _credential_field_order(provider: str, *, base_url: str) -> tuple[str, ...]:
    if provider == "anthropic" and not base_url:
        return ("direct_api_key", "api_key")
    return tuple(PROVIDER_SECRET_SETTINGS[provider])


def _redacted_test_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"result": str(result)}
    return {
        key: value
        for key, value in dict(result).items()
        if key not in {"api_key", "authorization"}
    }


def _credentials_path(context: CommandContext) -> Path:
    path = context.metadata.get("provider_credentials_path")
    if isinstance(path, (str, Path)):
        return Path(path).expanduser()
    return default_provider_credentials_path()


def _status_text(entry: Mapping[str, str] | None) -> str:
    if not entry:
        return "missing key"
    source = entry.get("source", "")
    status = entry.get("status", "missing key")
    return f"{status} ({source})" if source else status


def _aligned_rows(
    rows: Sequence[tuple[str, Any]],
    *,
    title: str,
) -> list[str]:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    return lines


def _restart_suffix(result: Any) -> str:
    return " Restart required." if bool(mapping_get(result, "restart_required", False)) else ""


def _provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(_normalize_provider(provider), str(provider or "Unknown"))


def _normalize_provider(value: Any) -> str:
    return str(value or "").strip().casefold()


def _normalize_key(value: str) -> str:
    return str(value or "").strip().casefold().replace("-", "_")


def _unknown_provider_result(provider: str) -> CommandResult:
    return CommandResult.failed(
        f"Unknown provider: {provider}. Supported providers: {', '.join(PROVIDERS)}",
        error_code="unknown_provider",
    )


def _redact_secrets(text: str, secrets: Sequence[str]) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def register(registry: CommandRegistry) -> None:
    registry.register(Command(
        name="provider",
        description="Manage LLM provider credentials",
        usage="/provider [list|set|test|switch]",
        handler=_handle_provider,
        handler_mode="context",
        category="Model",
        subcommands={
            "list": Command(
                name="list",
                description="List provider credential status",
                usage="list",
                handler=_handle_provider_list,
                handler_mode="context",
                category="Model",
            ),
            "set": Command(
                name="set",
                description="Save and apply provider credentials",
                usage="set <provider> <key=value> [key=value...]",
                handler=_handle_provider_set,
                handler_mode="context",
                category="Model",
            ),
            "test": Command(
                name="test",
                description="Test provider connectivity",
                usage="test [provider]",
                handler=_handle_provider_test,
                handler_mode="context",
                category="Model",
            ),
            "switch": Command(
                name="switch",
                description="Switch the active LLM provider",
                usage="switch <provider>",
                handler=_handle_provider_switch,
                handler_mode="context",
                category="Model",
            ),
        },
    ))


__all__ = ["register"]
