"""Regression tests for Discord slash-command backend wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from nymeria.core.command_params import BoundArgs
from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.discord_cogs.chat import ChatCog
from nymeria.triggers.discord_cogs.generated_cogs import GeneratedCommandsCog
from nymeria.triggers.discord_cogs.hooks import HooksCog
from nymeria.triggers.discord_cogs.info import InfoCog


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
        self.tool_search_calls: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.tool_categories: dict[str, list[str]] = {}
        self.tools: list[dict[str, Any]] = []

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

    async def list_available_models(
        self,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.models

    async def get_tool_categories(self) -> dict[str, Any]:
        return {"categories": self.tool_categories}

    async def search_tools(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.tool_search_calls.append({"query": query, **kwargs})
        return {"results": self.tools}

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


def _bind_backend_command(command_text: str, command_id: str) -> BoundArgs:
    """Re-parse a cog-emitted line exactly as the backend dispatcher would.

    Asserting the literal string proves the flattening is stable; binding it
    back proves the string still MEANS what the Discord fields said, which is
    the property the generated flatten-back exists to guarantee.
    """
    from nymeria.core.command_params import bind_args
    from nymeria.core.command_service import (
        CommandService,
        _split_args,
        _split_rest_after_tokens,
    )

    definition = CommandService()._commands[command_id]
    rest = _split_rest_after_tokens(command_text, len(definition.path))
    params = definition.params
    assert params is not None
    bound, error = bind_args(params, _split_args(rest), rest)
    assert error is None, error
    assert bound is not None
    return bound


def test_memory_save_uses_backend_command_endpoint():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_memory_save.callback(
            cog,
            interaction,
            "favorite_color",
            "deep blue",
        )

    asyncio.run(run())

    assert api.command_calls == [
        {
            "command": "/memory save favorite_color 'deep blue'",
            "thread_id": "discord_123_456",
            "source": "user",
            "actor": "user",
            "surface": "discord",
            "user_id": "user-1",
        }
    ]
    # The quoting must survive the dispatcher's shlex split: the multi-word
    # value is one token, so the rest param is the whole phrase.
    bound = _bind_backend_command(api.command_calls[0]["command"], "memory.save")
    assert bound.get("key") == "favorite_color"
    assert bound.get("value") == "deep blue"
    assert interaction.messages[0]["content"] == (
        "backend result for /memory save favorite_color 'deep blue'"
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


def test_todos_add_maps_discord_fields_onto_declared_flags():
    # The hand TodosCog and its pipe mapping retired with #143: the todos
    # family is generated from the declared schema now, so the Discord
    # fields must arrive as the canonical flag grammar (options first,
    # multi-word values quoted, the task tail verbatim).
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_todos_add.callback(
            cog,
            interaction,
            "Check logs",
            schedule="2h",
            notes="include worker",
            repeat="daily",
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/todos add --schedule 2h --notes 'include worker' --repeat daily Check logs"
    )
    assert api.command_calls[0]["thread_id"] == "discord_123_456"


def test_todos_add_task_apostrophe_survives_the_flatten():
    # The repeatable task tail is quoted per word: an unquoted apostrophe
    # would flip the dispatcher to whitespace-split fallback and mis-bind
    # the neighbouring option values (verified corruption: task swallowed
    # half the notes value). Plain words must stay unquoted.
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_todos_add.callback(
            cog,
            interaction,
            "Bob's plan",
            schedule="2h",
            notes="include worker",
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/todos add --schedule 2h --notes 'include worker' 'Bob'\"'\"'s' plan"
    )


def test_admin_only_generated_command_rejects_non_admin_before_backend_call():
    # `require_admin` is generated from the registry definition, so an
    # admin-only command must still be refused at the Discord layer rather
    # than relayed and rejected downstream.
    api = _FakeAPI(role="user")
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_env_set.callback(
            cog, interaction, "perplexity_api_key", "secret-value"
        )

    asyncio.run(run())

    assert api.command_calls == []
    assert interaction.messages[0]["content"] == "Admin only."


def test_non_admin_generated_command_is_relayed_without_an_admin_check():
    # The other half of the same generated flag: a command the registry does
    # not mark admin-only must not acquire a Discord-side admin gate.
    api = _FakeAPI(role="user")
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_memory_list.callback(cog, interaction)

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/memory list"


def test_flatten_puts_flags_first_then_positionals_then_the_scope_word():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        # A flag and an option ahead of nothing else.
        await GeneratedCommandsCog.cmd_triggers_list.callback(
            cog, interaction, True, "thread-1"
        )
        # A positional ahead of the trailing scope word.
        await GeneratedCommandsCog.cmd_provider_switch.callback(
            cog, interaction, "anthropic", "global"
        )
        # A positional ahead of the scope word on a command whose scope used
        # to be a `--global` flag (#131 wave B).
        await GeneratedCommandsCog.cmd_skills_disable.callback(
            cog, interaction, "summarize", "global"
        )

    asyncio.run(run())

    # Options and flags lead, positionals follow, the scope word is last:
    # exactly the order bind_args pops them back off in.
    assert api.command_calls[0]["command"] == (
        "/triggers list --enabled-only --thread thread-1"
    )
    listed = _bind_backend_command(api.command_calls[0]["command"], "triggers.list")
    assert listed.get("enabled_only") is True
    assert listed.get("thread") == "thread-1"

    assert api.command_calls[1]["command"] == "/provider switch anthropic global"
    switched = _bind_backend_command(
        api.command_calls[1]["command"], "provider.switch"
    )
    assert switched.get("provider") == "anthropic"
    assert switched.get("scope") == "global"

    assert api.command_calls[2]["command"] == "/skills disable summarize global"
    disabled = _bind_backend_command(api.command_calls[2]["command"], "skills.disable")
    assert disabled.get("name") == "summarize"
    assert disabled.get("scope") == "global"


def test_unset_optional_arguments_are_omitted_from_the_flattened_line():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_think.callback(cog, interaction)

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/think"


def test_integer_option_is_coerced_on_the_flattened_line():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_triggers_history.callback(
            cog, interaction, "trg-1", 5
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/triggers history --limit 5 trg-1"
    bound = _bind_backend_command(
        api.command_calls[0]["command"], "triggers.history"
    )
    assert bound.get("limit") == 5
    assert bound.get("trigger_id") == "trg-1"


def test_repeatable_positional_is_relayed_as_separate_tokens():
    # A repeatable positional is meant to arrive as several tokens, so its
    # text passes through unquoted while the option beside it is quoted.
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_skills_search.callback(
            cog, interaction, "pdf forms", "anthropic"
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/skills search --source anthropic pdf forms"
    )
    bound = _bind_backend_command(api.command_calls[0]["command"], "skills.search")
    assert bound.get("query") == ["pdf", "forms"]
    assert bound.get("source") == "anthropic"


def test_a_scope_field_alone_is_relayed_without_a_positional_gap():
    """Discord fields are independent; a scope is not a positional.

    `/memory limit` used to declare scope as the FIRST positional, so filling
    only `value` needed a generated gap guard or the value bound to the scope.
    Since #131 wave B the scope is a trailing token, so each field stands
    alone: value-only and scope-only both relay a line the backend re-binds to
    exactly what the user filled in.
    """
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_memory_limit.callback(
            cog, interaction, "4000", None
        )
        await GeneratedCommandsCog.cmd_memory_limit.callback(
            cog, interaction, None, "global"
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/memory limit 4000"
    value_only = _bind_backend_command(api.command_calls[0]["command"], "memory.limit")
    assert value_only.get("value") == "4000"
    assert value_only.get("scope") is None

    assert api.command_calls[1]["command"] == "/memory limit global"
    scope_only = _bind_backend_command(api.command_calls[1]["command"], "memory.limit")
    assert scope_only.get("scope") == "global"
    assert scope_only.get("value") is None


def test_optional_positionals_given_together_are_relayed_in_order():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_memory_limit.callback(
            cog, interaction, "4000", "thread"
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/memory limit 4000 thread"
    bound = _bind_backend_command(api.command_calls[0]["command"], "memory.limit")
    assert bound.get("scope") == "thread"
    assert bound.get("value") == "4000"


def test_hyphenated_command_path_is_relayed_in_the_spelling_users_type():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = GeneratedCommandsCog(bot)

    async def run() -> None:
        await GeneratedCommandsCog.cmd_background_set_url.callback(
            cog, interaction, "http://proxy.test/v1"
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == (
        "/background set-url http://proxy.test/v1"
    )


# The bespoke models/tools autocomplete resolvers are gone (#110 slice 6):
# every ref is answered by the backend option-resolver registry via
# GET /commands/options/{ref}. The generic Discord-side resolver is pinned in
# tests/test_discord_generated_cogs.py (scoping, filter, unlinked, fault,
# cap); option CONTENT (categories first, meta badges) is pinned at the
# backend in tests/test_command_option_resolvers.py.


def test_tools_cog_autocomplete_delegates_to_the_shared_resolver():
    # The hand cog must not keep a second copy of the resolver: /tools enable
    # and the generated commands have to suggest the same values, now served
    # by the backend option registry through the options endpoint.
    from nymeria.triggers.discord_cogs.tools import ToolsCog

    class _OptionsAPI(_FakeAPI):
        async def list_command_options(
            self, ref, thread_id=None, user_id=None, q=None, limit=0
        ):
            assert ref == "tools"
            assert user_id is not None  # acted as the linked user
            return [
                {"id": "email", "label": "email", "meta": "category, 1 tool"},
                {"id": "email_send", "label": "email_send", "meta": "on"},
            ]

    bot = _bot(_OptionsAPI())
    cog = ToolsCog(bot)
    interaction = _FakeInteraction()

    choices = asyncio.run(
        ToolsCog._enable_autocomplete(cog, interaction, "email")
    )

    assert [choice.value for choice in choices] == ["email", "email_send"]


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
            # Generated commands all share one cog class, so their heading
            # must come from the registry category, not the binding.
            _FakeTreeCommand(
                "memory list", "List saved memories.", binding=GeneratedCommandsCog
            ),
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
    # ... and generated ones by their registry category, never by the single
    # cog class that hosts all 60-plus of them.
    assert "Memory" in field_names
    assert "GeneratedCommands" not in field_names
    assert "`/memory list` - List saved memories." in help_text
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


def test_hook_list_maps_scope_to_the_trailing_token():
    api = _FakeAPI()
    bot = _bot(api)
    interaction = _FakeInteraction()
    cog = HooksCog(bot)

    async def run() -> None:
        await HooksCog.cmd_hook_list.callback(
            cog, interaction, _Choice("global"), True
        )

    asyncio.run(run())

    assert api.command_calls[0]["command"] == "/hook list --enabled-only global"
    # Grammar-drift tooth: the composed string must BIND against the live
    # registry declaration. The wave B --global retirement broke this cog
    # with every test green, because this test pinned the cog's output
    # without ever parsing it.
    from nymeria.core.command_params import bind_args
    from nymeria.core.command_service import CommandService

    service = CommandService()
    parsed = service._parse_for_registry(api.command_calls[0]["command"])
    assert parsed.definition is not None
    assert parsed.definition.params is not None
    bound, error = bind_args(parsed.definition.params, parsed.args, parsed.rest)
    assert error is None, error
    assert bound is not None
    assert bound.get("scope") == "global"
    assert bound.get("enabled_only") is True


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
