"""Regression tests for Discord slash-command backend wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.discord_cogs.chat import ChatCog
from nymeria.triggers.discord_cogs.config import ConfigCog
from nymeria.triggers.discord_cogs.hooks import HooksCog
from nymeria.triggers.discord_cogs.info import InfoCog
from nymeria.triggers.discord_cogs.memory import MemoryCog
from nymeria.triggers.discord_cogs.todos import TodosCog


class _FakeAPI:
    def __init__(
        self,
        *,
        role: str = "user",
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.role = role
        self.events = events or [{"type": "done"}]
        self.chat_calls: list[dict[str, Any]] = []
        self.command_calls: list[dict[str, Any]] = []
        self.list_command_calls: list[dict[str, Any]] = []

    async def get_me(self, *, act_as: Optional[str] = None) -> dict[str, Any]:
        return {"id": act_as, "role": self.role}

    async def chat_stream(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        **kwargs: Any,
    ):
        self.chat_calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "user_id": user_id,
                **kwargs,
            }
        )
        for event in self.events:
            yield event

    async def chat(
        self,
        message: str,
        thread_id: str,
        user_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.chat_calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "user_id": user_id,
                **kwargs,
            }
        )
        return {"response": "", "tool_call_count": 0}

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
        self.command_calls.append(
            {
                "command": command,
                "thread_id": thread_id,
                "source": source,
                "actor": actor,
                "surface": surface,
                "user_id": user_id,
            }
        )
        return {
            "success": True,
            "markdown": f"backend result for {command}",
            "command": command.lstrip("/"),
            "level": "success",
        }

    async def list_commands(
        self,
        *,
        source: Optional[str] = None,
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.list_command_calls.append(
            {
                "source": source,
                "actor": actor,
                "surface": surface,
                "user_id": user_id,
            }
        )
        return [
            {
                "name": "compact",
                "usage": "/compact",
                "description": "Compact the active chat context",
                "category": "Thread",
                "execution_kind": "chat_stream",
            },
            {
                "name": "tools core",
                "usage": "/tools core",
                "description": "Show core tools",
                "category": "Tools",
            },
            {
                "name": "memory list",
                "usage": "/memory list",
                "description": "List saved memories",
                "category": "Memory",
            },
        ]


class _FakeResponse:
    def __init__(self, interaction: "_FakeInteraction") -> None:
        self._interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self._interaction.deferred_ephemeral = ephemeral

    async def send_message(
        self,
        content: Optional[str] = None,
        *,
        embed: Any = None,
        ephemeral: bool = False,
    ) -> None:
        self._done = True
        self._interaction.messages.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral}
        )


class _FakeFollowup:
    def __init__(self, interaction: "_FakeInteraction") -> None:
        self._interaction = interaction

    async def send(
        self,
        content: Optional[str] = None,
        *,
        embed: Any = None,
        ephemeral: bool = False,
        **_: Any,
    ) -> "_FakeSentMessage":
        self._interaction.messages.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral}
        )
        return _FakeSentMessage(self._interaction.messages[-1])


class _FakeSentMessage:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    async def edit(self, *, content: Optional[str] = None, embed: Any = None) -> None:
        if content is not None:
            self.record["content"] = content
        if embed is not None:
            self.record["embed"] = embed


class _FakeChannel:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    async def send(
        self,
        content: Optional[str] = None,
        *,
        embed: Any = None,
        **_: Any,
    ) -> _FakeSentMessage:
        self.messages.append({"content": content, "embed": embed, "ephemeral": False})
        return _FakeSentMessage(self.messages[-1])


class _FakeUser:
    id = 987


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = _FakeUser()
        self.guild_id = 123
        self.channel_id = 456
        self.response = _FakeResponse(self)
        self.followup = _FakeFollowup(self)
        self.messages: list[dict[str, Any]] = []
        self.channel = _FakeChannel(self.messages)
        self.deferred_ephemeral: Optional[bool] = None


def _bot(api: _FakeAPI) -> NymeriaDiscordBot:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot.api = api

    async def resolve_user_id(discord_user_id: int) -> str:
        assert discord_user_id == _FakeUser.id
        return "user-1"

    bot.resolve_user_id = resolve_user_id
    return bot


def test_memory_save_uses_backend_command_endpoint():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = MemoryCog(bot)

    async def run() -> None:
        await MemoryCog.cmd_memory_save.callback(
            cog,
            interaction,
            "favorite_color",
            "deep blue",
        )

    asyncio.run(run())

    assert api.command_calls == [
        {
            "command": "/memory save favorite_color deep blue",
            "thread_id": "discord_123_456",
            "source": "user",
            "actor": "user",
            "surface": "discord",
            "user_id": "user-1",
        }
    ]
    assert interaction.messages[0]["content"] == (
        "backend result for /memory save favorite_color deep blue"
    )


def test_compact_uses_chat_stream_endpoint():
    api = _FakeAPI(
        events=[
            {"type": "compacting", "message": "Compacting thread context..."},
            {"type": "response", "content": "Compacted."},
            {"type": "done"},
        ]
    )
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = ChatCog(bot)

    async def run() -> None:
        await ChatCog.cmd_compact.callback(cog, interaction)

    asyncio.run(run())

    assert api.command_calls == []
    assert api.chat_calls == [
        {
            "message": "/compact",
            "thread_id": "discord_123_456",
            "user_id": "user-1",
            "attachments": None,
            "force_unsupported_attachments": False,
            "is_self_invoke": False,
            "trigger_override": None,
            "source": None,
            "source_label": None,
            "publish_autonomous_events": None,
            "platform_origin": None,
        }
    ]
    assert any(
        msg["content"] == "Compacting thread context..."
        for msg in interaction.messages
    )


def test_todos_add_preserves_discord_fields_as_backend_pipe_syntax():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = TodosCog(bot)

    class _Repeat:
        value = "daily"

    async def run() -> None:
        await TodosCog.cmd_todos_add.callback(
            cog,
            interaction,
            "Check logs",
            "2h",
            _Repeat(),
            "include worker",
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/todos add Check logs | 2h | daily | include worker"
    )
    assert api.command_calls[0]["thread_id"] == "discord_123_456"


def test_global_model_change_requires_admin_before_backend_command():
    api = _FakeAPI(role="user")
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = ConfigCog(bot)

    class _Scope:
        value = "global"

    async def run() -> None:
        await ConfigCog.cmd_model.callback(
            cog,
            interaction,
            "gpt-test",
            _Scope(),
        )

    asyncio.run(run())

    assert api.command_calls == []
    assert interaction.messages[0]["content"] == "Admin only."


def test_resolver_failure_renders_infra_copy_in_interaction_funnel():
    # Backlog #108: a backend auth failure (expired service token) must render
    # infrastructure copy, never account-link instructions. Uses the real
    # resolve_user_id -> UserResolver path, no monkeypatched resolver.
    import httpx

    from nymeria.triggers.bot_helpers import UserResolver

    class _AuthDownAPI(_FakeAPI):
        async def resolve_platform_user(self, platform: str, platform_user_id: str):
            request = httpx.Request("GET", "http://api.test/platform/resolve")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401", request=request, response=response)

    api = _AuthDownAPI()
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot.api = api
    bot._user_resolver = UserResolver(api, "discord")
    interaction = _FakeInteraction()

    result = asyncio.run(bot._resolve_or_reject_interaction(interaction))

    assert result is None
    content = interaction.messages[0]["content"]
    assert "isn't linked" not in content
    assert "service token" in content
    assert interaction.messages[0]["ephemeral"] is True


class _FakeTreeCommand:
    """Stand-in for a registered app command (only the attrs the cog reads)."""

    def __init__(self, qualified_name: str, description: str, binding: Any = None):
        self.qualified_name = qualified_name
        self.description = description
        self.binding = binding


class _FakeTree:
    def __init__(self, commands: list[Any]):
        self._commands = commands

    def walk_commands(self):
        return list(self._commands)


def test_help_lists_only_invokable_commands_within_discord_embed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The old merged backend catalog advertised ~110 rows unreachable on
    # Discord and overflowed the 6,000-char embed total, so /help itself
    # failed with Discord's generic error.
    from discord import app_commands

    class ToolsCog:  # category derives from the binding's class name
        pass

    class ChatCog:
        pass

    api = _FakeAPI()
    bot = _bot(api)
    fake_tree = _FakeTree(
        [
            _FakeTreeCommand("help", "Show Nymeria bot commands"),
            # Curated override: replaced by the DISCORD_LOCAL_COMMANDS row.
            _FakeTreeCommand("tools search", "Search tools.", binding=ToolsCog()),
            # Sibling subcommand under a curated root: MUST still render (a
            # first-token curated match used to swallow the whole family).
            _FakeTreeCommand("tools core", "Show core tools.", binding=ToolsCog()),
            _FakeTreeCommand("ask", "Send a message.", binding=ChatCog()),
            app_commands.Group(name="tools", description="Tool management"),
        ]
    )
    # ``tree`` is a read-only property on discord.py's Bot; patch the class.
    monkeypatch.setattr(NymeriaDiscordBot, "tree", property(lambda self: fake_tree))
    interaction = _FakeInteraction()
    cog = InfoCog(bot)

    async def run() -> None:
        await InfoCog.cmd_help.callback(cog, interaction)

    asyncio.run(run())

    # Defers first (backend resolution can miss the 3s deadline), and never
    # fetches the backend catalog (those commands are not invokable here).
    assert interaction.deferred_ephemeral is True
    assert api.list_command_calls == []

    embed = interaction.messages[0]["embed"]
    help_text = "\n".join(field.value for field in embed.fields)
    assert "`/tools search <query>` - Search tools by name, category, or description." in help_text
    # The curated /tools search row must not swallow its siblings.
    assert "`/tools core` - Show core tools." in help_text
    # The tree's own "tools search" and "help" rows are skipped, groups too.
    assert "`/tools search` - Search tools." not in help_text
    assert "- Show Nymeria bot commands" not in help_text
    assert help_text.count("`/compact`") == 1
    # Category names derive from the binding cog's class name.
    field_names = [field.name for field in embed.fields]
    assert "Tools" in field_names
    assert "Chat" in field_names
    # Unreachable backend-only commands must not be advertised.
    assert "`/provider" not in help_text

    total = (
        len(embed.title or "")
        + len(embed.description or "")
        + sum(len(field.name or "") + len(field.value or "") for field in embed.fields)
        + len(embed.footer.text or "")
    )
    assert total <= 6000


class _Choice:
    """Stand-in for app_commands.Choice (only .value is read by the cog)."""

    def __init__(self, value: str) -> None:
        self.value = value


def test_hook_create_assembles_and_quotes_the_flag_line():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_create.callback(
            cog,
            interaction,
            "Guard rm",
            _Choice("pre_tool_use"),
            _Choice("block_if_matches"),
            matcher="bash",
            condition="command contains rm -rf",
            reason="No destructive deletes",
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/hook create 'Guard rm' --event pre_tool_use --action block_if_matches "
        "--matcher bash --cond 'command contains rm -rf' "
        "--reason 'No destructive deletes'"
    )
    assert api.command_calls[0]["surface"] == "discord"


def test_hook_create_flag_line_round_trips_through_the_backend_parser():
    """The quoted line the cog emits must re-tokenize to the intended fields."""
    from nymeria.core.command_params import bind_args
    from nymeria.core.command_service import (
        CommandService,
        _split_args,
        _split_rest_after_tokens,
    )

    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_create.callback(
            cog,
            interaction,
            "Guard rm",
            _Choice("pre_tool_use"),
            _Choice("block_if_matches"),
            matcher="bash",
            condition="command contains rm -rf",
            reason="No destructive deletes",
        )

    asyncio.run(run())

    command_text = api.command_calls[0]["command"]
    # Strip the two path tokens ("hook create") the dispatcher would consume,
    # then bind against the registered declaration the dispatcher would use.
    rest = _split_rest_after_tokens(command_text, 2)
    params = CommandService()._commands["hook.create"].params
    assert params is not None
    bound, error = bind_args(params, _split_args(rest), rest)
    assert error is None, error
    assert bound is not None
    assert " ".join(bound.get("name")) == "Guard rm"
    assert bound.get("event") == "pre_tool_use"
    assert bound.get("action") == "block_if_matches"
    assert bound.get("matcher") == "bash"
    assert bound.get("cond") == ["command contains rm -rf"]
    assert bound.get("reason") == "No destructive deletes"


def test_hook_list_maps_scope_and_enabled_only_to_flags():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_list.callback(
            cog, interaction, _Choice("global"), True
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/hook list --global --enabled-only"


def test_hook_edit_assembles_key_values_and_repeatable_flags():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_edit.callback(
            cog,
            interaction,
            "abc12345",
            name="New Name",
            enabled=False,
            action=_Choice("notify"),
            text="hello world",
            condition="tool_name equals bash",
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/hook edit abc12345 name='New Name' enabled=false action=notify "
        "text='hello world' --cond 'tool_name equals bash'"
    )


def test_hook_edit_flag_line_round_trips_values_with_equals_and_quote_characters():
    """An edit value containing '=' or quote chars must survive shlex + partition("=").

    The backend's edit grammar treats any non---cond/--set token containing '='
    as a scalar key=value pair, split on the FIRST '=' (partition). The cog only
    shlex.quotes the VALUE half of key=value, so this locks that the quoting
    still produces one shlex token whose value, after partition("="), is exactly
    what the user typed, with no leaked quote characters and no `=`
    mis-splitting (e.g. a value of "a=b" must not be truncated to "a").
    """
    from nymeria.core.command_params import bind_args
    from nymeria.core.command_service import (
        CommandService,
        _split_args,
        _split_rest_after_tokens,
    )

    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_edit.callback(
            cog,
            interaction,
            "abc12345",
            text="a=b and it's \"quoted\"",
        )

    asyncio.run(run())

    command_text = api.command_calls[0]["command"]
    # Strip "hook edit" (2 path tokens), then bind against the registered
    # declaration, which is what the dispatcher hands the handler.
    rest = _split_rest_after_tokens(command_text, 2)
    params = CommandService()._commands["hook.edit"].params
    assert params is not None
    bound, error = bind_args(params, _split_args(rest), rest)
    assert error is None, error
    assert bound is not None
    assert bound.get("id") == "abc12345"
    kv: dict[str, str] = {}
    for token in bound.get("fields"):
        assert "=" in token
        key, _, value = token.partition("=")
        kv[key.strip().lower()] = value
    assert kv["text"] == "a=b and it's \"quoted\""


def test_hook_delete_forwards_confirmation_flag():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_delete.callback(cog, interaction, "abc12345")

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/hook delete abc12345 --yes"
