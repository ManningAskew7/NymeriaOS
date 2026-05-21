"""Regression tests for Discord slash-command backend wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.discord_cogs.chat import ChatCog
from nymeria.triggers.discord_cogs.config import ConfigCog
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


def test_help_merges_backend_catalog_with_discord_local_commands_without_duplicates():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = InfoCog(bot)

    async def run() -> None:
        await InfoCog.cmd_help.callback(cog, interaction)

    asyncio.run(run())

    assert api.list_command_calls == [
        {
            "source": None,
            "actor": "user",
            "surface": "discord",
            "user_id": "user-1",
        }
    ]
    embed = interaction.messages[0]["embed"]
    help_text = "\n".join(field.value for field in embed.fields)
    assert "`/tools core` - Show core tools." in help_text
    assert "`/tools search <query>` - Search tools by name, category, or description." in help_text
    assert help_text.count("`/compact`") == 1
