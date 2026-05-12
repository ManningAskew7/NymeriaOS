"""CLI-owned LLM provider credential storage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
DEFAULT_CREDENTIALS_PATH = Path("~/.nymeria/credentials.json")


@dataclass(frozen=True, slots=True)
class ProviderCredentialRecord:
    """Persisted credential fields for one LLM provider."""

    provider: str
    values: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def has_secret(self) -> bool:
        return any(bool(value.strip()) for value in self.values.values())


@dataclass(frozen=True, slots=True)
class ProviderCredentialsConfig:
    """Top-level ``~/.nymeria/credentials.json`` schema."""

    version: int = CONFIG_VERSION
    providers: dict[str, ProviderCredentialRecord] = field(default_factory=dict)

    def get(self, provider: str) -> ProviderCredentialRecord | None:
        return self.providers.get(_normalize_provider(provider))


def default_provider_credentials_path() -> Path:
    """Return the provider credential path, allowing tests to override it."""

    override = os.environ.get("NYMERIA_PROVIDER_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CREDENTIALS_PATH.expanduser()


def load_provider_credentials(
    path: Path | str | None = None,
) -> ProviderCredentialsConfig:
    """Load provider credentials, recovering to an empty config on errors."""

    selected_path = (
        Path(path).expanduser() if path is not None else default_provider_credentials_path()
    )
    try:
        raw = json.loads(selected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ProviderCredentialsConfig()

    if not isinstance(raw, dict) or raw.get("version") != CONFIG_VERSION:
        return ProviderCredentialsConfig()

    providers: dict[str, ProviderCredentialRecord] = {}
    raw_providers = raw.get("providers")
    if isinstance(raw_providers, dict):
        for provider, item in raw_providers.items():
            if not isinstance(provider, str) or not isinstance(item, dict):
                continue
            normalized = _normalize_provider(provider)
            values = {
                str(key): value.strip()
                for key, value in item.items()
                if key not in {"created_at", "updated_at"}
                and isinstance(key, str)
                and isinstance(value, str)
                and value.strip()
            }
            providers[normalized] = ProviderCredentialRecord(
                provider=normalized,
                values=values,
                created_at=_clean(item.get("created_at")) or "",
                updated_at=_clean(item.get("updated_at")) or "",
            )

    return ProviderCredentialsConfig(providers=providers)


def save_provider_credentials(
    provider: str,
    values: dict[str, str],
    *,
    path: Path | str | None = None,
) -> ProviderCredentialsConfig:
    """Merge and persist credential values for one provider."""

    normalized = _normalize_provider(provider)
    if not normalized:
        raise ValueError("Provider cannot be blank")

    selected_path = (
        Path(path).expanduser() if path is not None else default_provider_credentials_path()
    )
    current = load_provider_credentials(selected_path)
    now = _now()
    existing = current.providers.get(normalized)
    merged = dict(existing.values if existing else {})
    merged.update({key: value for key, value in values.items() if value})

    record = ProviderCredentialRecord(
        provider=normalized,
        values=merged,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    providers = {**current.providers, normalized: record}
    next_config = ProviderCredentialsConfig(providers=providers)
    write_provider_credentials(next_config, selected_path)
    return next_config


def write_provider_credentials(
    config: ProviderCredentialsConfig,
    path: Path | str | None = None,
) -> None:
    """Write provider credentials with private directory/file permissions."""

    selected_path = (
        Path(path).expanduser() if path is not None else default_provider_credentials_path()
    )
    selected_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(selected_path.parent, 0o700)

    tmp_path = selected_path.with_name(f".{selected_path.name}.tmp")
    tmp_path.write_text(
        json.dumps(_config_to_json(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, selected_path)
    os.chmod(selected_path, 0o600)


def _config_to_json(config: ProviderCredentialsConfig) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "providers": {
            provider: {
                **record.values,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            for provider, record in config.providers.items()
        },
    }


def _normalize_provider(provider: str) -> str:
    return str(provider or "").strip().casefold()


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_CREDENTIALS_PATH",
    "ProviderCredentialRecord",
    "ProviderCredentialsConfig",
    "default_provider_credentials_path",
    "load_provider_credentials",
    "save_provider_credentials",
    "write_provider_credentials",
]
