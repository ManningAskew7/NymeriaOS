from __future__ import annotations

import json
import stat

from nymeria.triggers.cli.credentials import (
    load_cli_config,
    redact_token,
    remove_active_profile_token,
    save_active_profile,
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_cli_config_load_missing_file_returns_empty(tmp_path) -> None:
    config = load_cli_config(tmp_path / "missing" / "cli.json")

    assert config.active_profile is None
    assert config.profiles == {}


def test_cli_config_save_profile_uses_private_permissions(tmp_path) -> None:
    config_path = tmp_path / ".nymeria" / "cli.json"

    save_active_profile(
        api_url=" http://api ",
        api_key=" nym_secret ",
        user_id="alice",
        path=config_path,
    )

    assert _mode(config_path.parent) == 0o700
    assert _mode(config_path) == 0o600
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["active_profile"] == "default"
    assert data["profiles"]["default"]["api_url"] == "http://api"
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert data["profiles"]["default"]["user_id"] == "alice"


def test_cli_config_invalid_json_recovers_on_next_save(tmp_path) -> None:
    config_path = tmp_path / ".nymeria" / "cli.json"
    config_path.parent.mkdir()
    config_path.write_text("{not json", encoding="utf-8")

    assert load_cli_config(config_path).profiles == {}

    save_active_profile(
        api_url="http://api",
        api_key="nym_secret",
        path=config_path,
    )

    assert load_cli_config(config_path).active is not None


def test_cli_config_logout_removes_active_token(tmp_path) -> None:
    config_path = tmp_path / ".nymeria" / "cli.json"
    save_active_profile(
        api_url="http://api",
        api_key="nym_secret",
        path=config_path,
    )

    config = remove_active_profile_token(config_path)
    text = config_path.read_text(encoding="utf-8")

    assert config.active_profile is None
    assert "nym_secret" not in text


def test_cli_config_profile_save_preserves_unmanaged_sections(tmp_path) -> None:
    """A profile write must not clobber sections owned by other writers.

    Regression test: /login and /logout previously rewrote cli.json with only
    the managed profile keys, silently deleting a saved theme (or any future
    section such as status_bar).
    """
    config_path = tmp_path / ".nymeria" / "cli.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "theme": {"spinner": "#123456"},
                "status_bar": {"segments": ["model"]},
            }
        ),
        encoding="utf-8",
    )

    save_active_profile(
        api_url="http://api",
        api_key="nym_secret",
        path=config_path,
    )
    remove_active_profile_token(config_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["theme"] == {"spinner": "#123456"}
    assert data["status_bar"] == {"segments": ["model"]}
    assert data["profiles"] == {}


def test_cli_theme_save_preserves_profiles(tmp_path) -> None:
    """The reverse direction: a theme write must not clobber saved profiles."""
    from nymeria.triggers.cli.theme import CLITheme, save_cli_theme

    config_path = tmp_path / ".nymeria" / "cli.json"
    save_active_profile(
        api_url="http://api",
        api_key="nym_secret",
        path=config_path,
    )

    save_cli_theme(CLITheme(overrides={"spinner": "#123456"}), config_path)

    config = load_cli_config(config_path)
    assert config.active_profile == "default"
    assert config.active is not None
    assert config.active.api_key == "nym_secret"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["theme"] == {"spinner": "#123456"}


def test_cli_token_redaction_never_prints_raw_token() -> None:
    assert redact_token("nym_secret") == "nym_<redacted>"
    assert redact_token("") == ""
