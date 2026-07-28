"""CLI-owned connection profile storage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.storage_paths import write_text_atomic

CONFIG_VERSION = 1
DEFAULT_PROFILE_NAME = "default"
DEFAULT_CONFIG_PATH = Path("~/.nymeria/cli.json")


@dataclass(frozen=True, slots=True)
class CLIConnectionProfile:
    """Persisted CLI API connection profile."""

    api_url: str
    api_key: str
    user_id: str = "default"
    created_at: str = ""
    updated_at: str = ""

    @property
    def has_token(self) -> bool:
        return bool(self.api_key.strip())


@dataclass(frozen=True, slots=True)
class CLIProfileConfig:
    """Top-level ``~/.nymeria/cli.json`` schema."""

    version: int = CONFIG_VERSION
    active_profile: str | None = None
    profiles: dict[str, CLIConnectionProfile] = field(default_factory=dict)

    @property
    def active(self) -> CLIConnectionProfile | None:
        if not self.active_profile:
            return None
        return self.profiles.get(self.active_profile)


def default_cli_config_path() -> Path:
    """Return the CLI profile path, allowing tests to override it."""

    override = os.environ.get("NYMERIA_CLI_CONFIG")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def load_cli_config(path: Path | str | None = None) -> CLIProfileConfig:
    """Load the CLI connection config, recovering to an empty config on errors."""

    selected_path = Path(path).expanduser() if path is not None else default_cli_config_path()
    try:
        raw = json.loads(selected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CLIProfileConfig()

    if not isinstance(raw, dict) or raw.get("version") != CONFIG_VERSION:
        return CLIProfileConfig()

    profiles: dict[str, CLIConnectionProfile] = {}
    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for name, item in raw_profiles.items():
            if not isinstance(name, str) or not isinstance(item, dict):
                continue
            api_url = _clean(item.get("api_url"))
            api_key = _clean(item.get("api_key"))
            if not api_url:
                continue
            profiles[name] = CLIConnectionProfile(
                api_url=api_url.rstrip("/"),
                api_key=api_key or "",
                user_id=_clean(item.get("user_id")) or "default",
                created_at=_clean(item.get("created_at")) or "",
                updated_at=_clean(item.get("updated_at")) or "",
            )

    active_profile = _clean(raw.get("active_profile"))
    if active_profile not in profiles:
        active_profile = None
    return CLIProfileConfig(active_profile=active_profile, profiles=profiles)


def save_active_profile(
    *,
    api_url: str,
    api_key: str,
    user_id: str = "default",
    path: Path | str | None = None,
    profile_name: str = DEFAULT_PROFILE_NAME,
) -> CLIProfileConfig:
    """Save or replace the active CLI profile."""

    selected_path = Path(path).expanduser() if path is not None else default_cli_config_path()
    current = load_cli_config(selected_path)
    now = _now()
    existing = current.profiles.get(profile_name)
    profile = CLIConnectionProfile(
        api_url=api_url.strip().rstrip("/"),
        api_key=api_key.strip(),
        user_id=(user_id or "default").strip() or "default",
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    profiles = {**current.profiles, profile_name: profile}
    next_config = CLIProfileConfig(
        active_profile=profile_name,
        profiles=profiles,
    )
    write_cli_config(next_config, selected_path)
    return next_config


def remove_active_profile_token(
    path: Path | str | None = None,
) -> CLIProfileConfig:
    """Remove the active profile token and clear the active profile."""

    selected_path = Path(path).expanduser() if path is not None else default_cli_config_path()
    current = load_cli_config(selected_path)
    if not selected_path.exists() and not current.profiles:
        return current
    if not current.active_profile:
        write_cli_config(CLIProfileConfig(profiles=current.profiles), selected_path)
        return CLIProfileConfig(profiles=current.profiles)

    profiles = dict(current.profiles)
    profiles.pop(current.active_profile, None)
    next_config = CLIProfileConfig(profiles=profiles)
    write_cli_config(next_config, selected_path)
    return next_config


def write_cli_config(config: CLIProfileConfig, path: Path | str | None = None) -> None:
    """Write CLI config with private directory/file permissions.

    Only the managed keys (``version``, ``active_profile``, ``profiles``) are
    rewritten. Unknown top-level keys owned by other writers of this file
    (for example the ``theme`` section saved by ``theme.save_cli_theme``, or
    future sections like ``status_bar``) are preserved, mirroring the
    merge-before-write behavior of the theme saver.
    """

    selected_path = Path(path).expanduser() if path is not None else default_cli_config_path()
    selected_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(selected_path.parent, 0o700)

    data: dict[str, Any] = {}
    try:
        raw = json.loads(selected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict):
        data.update(raw)
    data.update(_config_to_json(config))

    # mode= so a FIRST write is created 0600 rather than at the umask
    # default and tightened after: these are credentials, and the widened
    # window would already contain them.
    write_text_atomic(
        selected_path,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    os.chmod(selected_path, 0o600)


def redact_token(token: str | None) -> str:
    """Return a token-safe diagnostic placeholder."""

    if not token:
        return ""
    return "nym_<redacted>"


def _config_to_json(config: CLIProfileConfig) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "active_profile": config.active_profile,
        "profiles": {
            name: {
                "api_url": profile.api_url,
                "api_key": profile.api_key,
                "user_id": profile.user_id,
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            }
            for name, profile in config.profiles.items()
        },
    }


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CLIConnectionProfile",
    "CLIProfileConfig",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_PROFILE_NAME",
    "default_cli_config_path",
    "load_cli_config",
    "redact_token",
    "remove_active_profile_token",
    "save_active_profile",
    "write_cli_config",
]
