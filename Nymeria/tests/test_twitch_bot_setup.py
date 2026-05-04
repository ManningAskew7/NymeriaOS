"""Regression tests for Twitch bot first-run setup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nymeria.config.settings import Settings
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.triggers.twitch_bot import (
    DEFAULT_TWITCH_PROMPT,
    DEFAULT_TWITCH_TOOLS,
    NymeriaTwitchBot,
)


def _make_setup_only_bot(tmp_path, prompt: str = "Configured Twitch prompt"):
    bot = NymeriaTwitchBot.__new__(NymeriaTwitchBot)
    bot.agent = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    bot._thread_id = "twitch_channelname"
    bot._default_system_prompt = prompt
    return bot


def test_twitch_channel_has_no_deployment_specific_default(monkeypatch):
    monkeypatch.delenv("TWITCH_CHANNEL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.twitch_channel is None


def test_twitch_prompt_override_loads_from_environment(monkeypatch):
    monkeypatch.setenv("TWITCH_SYSTEM_PROMPT", "Custom prompt from env")

    settings = Settings(_env_file=None)

    assert settings.twitch_system_prompt == "Custom prompt from env"


def test_default_twitch_prompt_is_generic():
    assert "twitch.tv/" not in DEFAULT_TWITCH_PROMPT
    assert "professional esports" not in DEFAULT_TWITCH_PROMPT
    assert "content creator" not in DEFAULT_TWITCH_PROMPT
    assert "inside joke" not in DEFAULT_TWITCH_PROMPT


def test_auto_setup_creates_twitch_thread_config(tmp_path):
    bot = _make_setup_only_bot(tmp_path)

    asyncio.run(bot._auto_setup_thread())

    saved = ThreadConfigManager(tmp_path).get_config("twitch_channelname")
    assert saved is not None
    assert saved.system_prompt == "Configured Twitch prompt"
    assert saved.enabled_tools == DEFAULT_TWITCH_TOOLS


def test_auto_setup_preserves_existing_twitch_thread_customizations(tmp_path):
    manager = ThreadConfigManager(tmp_path)
    manager.save_config(
        ThreadConfig(
            thread_id="twitch_channelname",
            system_prompt="User customized prompt",
            enabled_tools=["twitch_send"],
            disabled_tools=["twitch_ban"],
        )
    )
    bot = _make_setup_only_bot(tmp_path)

    asyncio.run(bot._auto_setup_thread())

    saved = manager.get_config("twitch_channelname")
    assert saved is not None
    assert saved.system_prompt == "User customized prompt"
    assert saved.enabled_tools == ["twitch_send"]
    assert saved.disabled_tools == ["twitch_ban"]


def test_auto_setup_fills_missing_defaults_without_resetting_config(tmp_path):
    manager = ThreadConfigManager(tmp_path)
    manager.save_config(
        ThreadConfig(
            thread_id="twitch_channelname",
            instructions="Keep this extra instruction",
        )
    )
    bot = _make_setup_only_bot(tmp_path)

    asyncio.run(bot._auto_setup_thread())

    saved = manager.get_config("twitch_channelname")
    assert saved is not None
    assert saved.instructions == "Keep this extra instruction"
    assert saved.system_prompt == "Configured Twitch prompt"
    assert saved.enabled_tools == DEFAULT_TWITCH_TOOLS
