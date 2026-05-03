"""Regression tests for bot self-restart shutdown behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.telegram_bot import NymeriaTelegramBot


class _FakeAPI:
    def __init__(self) -> None:
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class _FakeTelegramApplication:
    def __init__(self) -> None:
        self.stop_running_count = 0

    def stop_running(self) -> None:
        self.stop_running_count += 1


def test_discord_restart_closes_api_client_before_gateway_shutdown():
    async def run() -> tuple[int, int]:
        api = _FakeAPI()
        bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
        bot.api = api
        gateway_close_count = 0

        async def close_gateway() -> None:
            nonlocal gateway_close_count
            gateway_close_count += 1

        bot.close = close_gateway

        await bot._request_self_restart()
        return api.close_count, gateway_close_count

    assert asyncio.run(run()) == (1, 1)


def test_telegram_restart_asks_application_to_stop_polling():
    async def run() -> tuple[int, int]:
        api = _FakeAPI()
        app = _FakeTelegramApplication()
        bot = NymeriaTelegramBot(api=api, bot_token="test-token")
        bot._application = app

        await bot._request_self_restart()
        return app.stop_running_count, api.close_count

    assert asyncio.run(run()) == (1, 0)


def test_telegram_restart_without_application_cleans_up_then_exits():
    async def run() -> int:
        api = _FakeAPI()
        bot = NymeriaTelegramBot(api=api, bot_token="test-token")

        with pytest.raises(SystemExit) as exc_info:
            await bot._request_self_restart()

        assert exc_info.value.code == 0
        return api.close_count

    assert asyncio.run(run()) == 1


def test_telegram_shutdown_cancels_background_tasks_and_closes_owned_api():
    async def run() -> tuple[bool, int, int]:
        api = _FakeAPI()
        bot = NymeriaTelegramBot(api=api, bot_token="test-token")

        async def sleeper() -> None:
            await asyncio.sleep(60)

        task = bot._spawn_background_task(sleeper())
        await asyncio.sleep(0)
        await bot._cleanup_after_shutdown(close_api=True)
        return task.cancelled(), api.close_count, len(bot._background_tasks)

    assert asyncio.run(run()) == (True, 1, 0)


def test_user_owned_telegram_bot_shutdown_does_not_close_shared_api():
    async def run() -> int:
        api = _FakeAPI()
        bot = NymeriaTelegramBot(
            api=api,
            bot_token="test-token",
            user_telegram_bot_id=123,
            owns_api_client=False,
        )

        await bot._cleanup_after_shutdown(close_api=bot._owns_api_client)
        return api.close_count

    assert asyncio.run(run()) == 0


def test_bot_restart_handlers_do_not_call_os_exit():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "nymeria/triggers/discord_bot.py",
        "nymeria/triggers/telegram_bot.py",
    ):
        assert "os._exit" not in (root / relative).read_text()
