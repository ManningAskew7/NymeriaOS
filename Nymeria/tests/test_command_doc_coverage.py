"""Doc-coverage gates for the two native chat-bot command surfaces (#131).

Every other surface discovers commands from the live registry, so its docs can
only go stale in prose. The Discord and Telegram bots are different: their
command names are DECLARED in the bot (Discord needs a static slash tree,
Telegram needs a flat `CommandHandler` per name), and `docs/chat-apps/`
is where a user looks them up. Nothing connected those two facts, so a rename
wave could ship a working bot whose documentation described the old surface.

These tests connect them. Both name lists are derived from the code, never
hand-maintained here:

* Discord: the live command tree (`ALL_COGS`), which is the generator's build
  plus the hand cogs. `tests/test_discord_generated_cogs.py` already fails if
  the checked-in generated half has drifted from the registry, so the tree is
  a faithful stand-in for "what the generator would produce today".
* Telegram: the `CommandHandler` names `_register_handlers` actually adds,
  read back off a recording stand-in for the PTB `Application`.

The last test pins the two hand commands that exist only because Discord makes
a multi-command family a group: without them the surface has no model switch
and no settings readout at all, so they are a capability, not a spelling.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional, cast

import pytest
from discord import app_commands
from telegram.ext import CommandHandler

from nymeria.core.command_params import BoundArgs
from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.discord_cogs import ALL_COGS
from nymeria.triggers.discord_cogs.config import ConfigCog
from nymeria.triggers.telegram_bot import NymeriaTelegramBot

CHAT_APP_DOCS = Path(__file__).resolve().parents[1] / "docs" / "public" / "chat-apps"
DISCORD_DOC = CHAT_APP_DOCS / "discord-bot.md"
TELEGRAM_DOC = CHAT_APP_DOCS / "telegram-bot.md"
TWITCH_DOC = CHAT_APP_DOCS / "twitch-bot.md"


def _discord_command_names() -> set[str]:
    """Every INVOKABLE Discord command name in the loaded tree.

    A `Group` is a namespace: Discord will not let a user run it, so it is not
    a command a doc could describe.
    """
    names: set[str] = set()
    for cog in ALL_COGS:
        for entry in cog.__cog_app_commands__:
            walked: list[Any] = [entry]
            if isinstance(entry, app_commands.Group):
                walked += list(entry.walk_commands())
            for item in walked:
                if isinstance(item, app_commands.Group):
                    continue
                names.add(item.qualified_name)
    return names


class _RecordingApplication:
    """The slice of PTB's `Application` that `_register_handlers` touches."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.error_handlers: list[Any] = []

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler: Any) -> None:
        self.error_handlers.append(handler)


def _telegram_command_names() -> set[str]:
    """Every command name a native `CommandHandler` claims."""
    # Registration touches no API: `command()` only wraps bound methods in the
    # access guard, so the client is never called here.
    bot = NymeriaTelegramBot(api=cast(Any, None), bot_token="doc-coverage")
    app = _RecordingApplication()
    bot._register_handlers(app)
    return {
        name
        for handler in app.handlers
        if isinstance(handler, CommandHandler)
        for name in handler.commands
    }


def _undocumented(names: set[str], doc: Path) -> list[str]:
    """The names the doc never mentions as a slash command.

    The trailing boundary matters: `/tools enable` is a prefix of
    `/tools enabled`, so a substring test would let a missing row hide behind
    its longer sibling.
    """
    text = doc.read_text(encoding="utf-8")
    return sorted(
        name
        for name in names
        if re.search(rf"/{re.escape(name)}(?![\w-])", text) is None
    )


def test_every_discord_slash_command_is_documented() -> None:
    names = _discord_command_names()
    # A tree this small would mean the cogs failed to load, and an empty set
    # passes any coverage check vacuously.
    assert len(names) > 50
    missing = _undocumented(names, DISCORD_DOC)
    assert not missing, (
        f"discord-bot.md documents no {missing}; add a row or the command is "
        "invisible to everyone who reads the docs instead of the tree."
    )


def test_every_telegram_native_command_is_documented() -> None:
    names = _telegram_command_names()
    assert len(names) > 30
    missing = _undocumented(names, TELEGRAM_DOC)
    assert not missing, (
        f"telegram-bot.md documents no {missing}; add a row or the command is "
        "invisible to everyone who reads the docs instead of the source."
    )


def test_the_coverage_check_fails_on_an_undocumented_name() -> None:
    """The gate has teeth: an unmentioned name is reported, not skipped."""
    assert _undocumented({"definitely-not-a-command"}, DISCORD_DOC) == [
        "definitely-not-a-command"
    ]
    assert _undocumented({"definitely-not-a-command"}, TELEGRAM_DOC) == [
        "definitely-not-a-command"
    ]


def _twitch_command_names() -> set[str]:
    """The !command names the Twitch bot registers, read from its source.

    TwitchIO v3 registers commands explicitly via `@commands.command(name=...)`
    decorators inside `NymeriaTwitchBot.__init__`; parsing the AST keeps this
    derivation faithful to the declarations without instantiating the SDK.
    """
    import ast
    import inspect

    import nymeria.triggers.twitch_bot as twitch_bot

    tree = ast.parse(inspect.getsource(twitch_bot))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "command":
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(str(kw.value.value))
    return names


def test_every_twitch_bang_command_is_documented() -> None:
    names = _twitch_command_names()
    # Fewer means the AST derivation broke, and an empty set passes vacuously.
    assert len(names) >= 8
    text = TWITCH_DOC.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in names
        if re.search(rf"!{re.escape(name)}(?![\w-])", text) is None
    )
    assert not missing, (
        f"twitch-bot.md documents no {missing}; add a row or the command is "
        "invisible to everyone who reads the docs instead of the source."
    )


def test_a_longer_sibling_does_not_document_a_shorter_name() -> None:
    """`/tools enabled` in the doc must not vouch for a missing `/tools enab`."""
    assert _undocumented({"tools enab"}, DISCORD_DOC) == ["tools enab"]


# ---------------------------------------------------------------------------
# The two Discord capabilities the group rule removed, and the hand commands
# that give them back.
# ---------------------------------------------------------------------------


class _FakeAPI:
    def __init__(self) -> None:
        self.command_calls: list[dict[str, Any]] = []

    async def get_me(self, *, act_as: Optional[str] = None) -> dict[str, Any]:
        return {"id": act_as, "role": "admin"}

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: Optional[str] = None,
        source: str = "user",
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        self.command_calls.append({"command": command, "thread_id": thread_id})
        return {"success": True, "markdown": f"ran {command}", "level": "info"}


class _FakeResponse:
    def __init__(self, interaction: "_FakeInteraction") -> None:
        self._interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True

    async def send_message(
        self, content: Optional[str] = None, *, ephemeral: bool = False, **_: Any
    ) -> None:
        self._done = True
        self._interaction.messages.append(content)


class _FakeFollowup:
    def __init__(self, interaction: "_FakeInteraction") -> None:
        self._interaction = interaction

    async def send(
        self, content: Optional[str] = None, *, ephemeral: bool = False, **_: Any
    ) -> None:
        self._interaction.messages.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = type("_User", (), {"id": 987})()
        self.guild_id = 123
        self.channel_id = 456
        self.messages: list[Optional[str]] = []
        self.response = _FakeResponse(self)
        self.followup = _FakeFollowup(self)


def _discord_bot(api: _FakeAPI) -> NymeriaDiscordBot:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot.api = api

    async def resolve_user_id(
        discord_user_id: int, *, guild_id: int | None = None
    ) -> str:
        return "user-1"

    bot.resolve_user_id = resolve_user_id
    return bot


def _bind_backend_command(command_text: str, command_id: str) -> BoundArgs:
    """Re-parse a cog-emitted line exactly as the backend dispatcher would."""
    from nymeria.core.command_params import bind_args
    from nymeria.core.command_service import (
        CommandService,
        _split_args,
        _split_rest_after_tokens,
    )

    definition = CommandService()._commands[command_id]
    rest = _split_rest_after_tokens(command_text, len(definition.path))
    params = definition.params or ()
    bound, error = bind_args(params, _split_args(rest), rest)
    assert error is None, error
    assert bound is not None
    return bound


@pytest.mark.parametrize(
    "name, replaces",
    [("set-model", "model"), ("show-settings", "settings")],
)
def test_the_hand_command_restoring_a_dropped_root_is_invokable(
    name: str, replaces: str
) -> None:
    """Both must be real commands, not group children Discord cannot run."""
    tree = _discord_command_names()
    assert name in tree
    # The registry family they stand in for is a GROUP here, which is exactly
    # why they exist: its bare action is unreachable on Discord.
    assert replaces not in tree


def test_set_model_relays_the_canonical_model_switch() -> None:
    api = _FakeAPI()
    cog = ConfigCog(_discord_bot(api))
    interaction = _FakeInteraction()

    asyncio.run(
        ConfigCog.cmd_set_model.callback(cog, interaction, "claude-fable-5", "thread")
    )

    assert api.command_calls == [
        {"command": "/model claude-fable-5 thread", "thread_id": "discord_123_456"}
    ]
    # The line must still MEAN the switch after the dispatcher re-splits it.
    bound = _bind_backend_command(api.command_calls[0]["command"], "model")
    assert bound.get("name") == "claude-fable-5"
    assert bound.get("scope") == "thread"
    assert interaction.messages == ["ran /model claude-fable-5 thread"]


def test_set_model_flag_leads_the_positional_and_the_scope_tails() -> None:
    api = _FakeAPI()
    cog = ConfigCog(_discord_bot(api))

    asyncio.run(
        ConfigCog.cmd_set_model.callback(
            cog, _FakeInteraction(), "some/unlisted-model", "global", True
        )
    )

    assert api.command_calls[0]["command"] == (
        "/model --force some/unlisted-model global"
    )
    bound = _bind_backend_command(api.command_calls[0]["command"], "model")
    assert bound.get("force") is True
    assert bound.get("name") == "some/unlisted-model"
    assert bound.get("scope") == "global"


def test_set_model_omits_the_optional_scope_when_unset() -> None:
    api = _FakeAPI()
    cog = ConfigCog(_discord_bot(api))

    asyncio.run(ConfigCog.cmd_set_model.callback(cog, _FakeInteraction(), "gpt-5.5"))

    assert api.command_calls[0]["command"] == "/model gpt-5.5"


def test_show_settings_relays_the_canonical_settings_readout() -> None:
    api = _FakeAPI()
    cog = ConfigCog(_discord_bot(api))
    interaction = _FakeInteraction()

    asyncio.run(ConfigCog.cmd_show_settings.callback(cog, interaction))

    assert api.command_calls == [
        {"command": "/settings", "thread_id": "discord_123_456"}
    ]
    assert interaction.messages == ["ran /settings"]
