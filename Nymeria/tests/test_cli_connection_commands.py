from __future__ import annotations

import asyncio
import json
from typing import Any

from nymeria.triggers.cli.commands import CommandContext, CommandRegistry, ListCommandOutputSink
from nymeria.triggers.cli.commands import connection
from nymeria.triggers.cli.transport.api import APIAgentClient
from nymeria.triggers.cli.transport.disconnected import DisconnectedAgentClient


def run(coro):
    return asyncio.run(coro)


class FakeLoginAPI:
    instances: list["FakeLoginAPI"] = []
    health_result = True
    me_result: dict[str, Any] = {"id": "alice", "role": "user"}

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.health_result = True
        cls.me_result = {"id": "alice", "role": "user"}

    async def health(self) -> bool:
        self.calls.append(("health", {}))
        return self.health_result

    async def get_me(self, act_as: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_me", {"act_as": act_as}))
        return self.me_result

    async def close(self) -> None:
        self.closed = True


def make_registry() -> CommandRegistry:
    registry = CommandRegistry(include_builtins=False)
    connection.register(registry)
    return registry


def make_context(
    *,
    config_path,
    actions: list[Any],
    prompts: list[str] | None = None,
    token: str = "nym_secret",
) -> CommandContext:
    prompt_values = iter(prompts or ["http://api"])
    return CommandContext(
        output=ListCommandOutputSink(),
        dispatch_state=actions.append,
        prompt_handler=lambda _prompt: next(prompt_values),
        secret_prompt_handler=lambda _prompt: token,
        thread_id="thread-1",
        user_id="default",
        metadata={
            "api_client_factory": FakeLoginAPI,
            "cli_config_path": config_path,
        },
    )


def test_login_validates_saves_profile_and_replaces_client(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    assert "nym_secret" not in result.messages[0].content
    assert actions[0]["type"] == "replace_client"
    assert isinstance(actions[0]["client"], APIAgentClient)
    assert actions[0]["user_id"] == "alice"
    assert FakeLoginAPI.instances[0].calls == [
        ("health", {}),
        ("get_me", {"act_as": "alice"}),
    ]
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_url"] == "http://api"
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert data["profiles"]["default"]["user_id"] == "alice"


def test_connect_alias_uses_login_handler(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    registry = make_registry()
    context = make_context(
        config_path=tmp_path / ".nymeria" / "cli.json",
        actions=actions,
        prompts=[],
    )

    result = run(registry.dispatch_async(context, "/connect http://api --user-id alice"))

    assert result.ok is True
    assert actions[0]["type"] == "replace_client"


def test_login_failure_does_not_save_profile(tmp_path) -> None:
    FakeLoginAPI.reset()
    FakeLoginAPI.health_result = False
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is False
    assert result.error_code == "api_unavailable"
    assert actions == []
    assert not config_path.exists()
    assert FakeLoginAPI.instances[0].closed is True


def test_logout_removes_active_profile_and_disconnects(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)
    assert run(registry.dispatch_async(context, "/login --user-id alice")).ok is True

    actions.clear()
    result = run(registry.dispatch_async(context, "/logout"))

    assert result.ok is True
    assert actions[0]["type"] == "replace_client"
    assert isinstance(actions[0]["client"], DisconnectedAgentClient)
    assert "nym_secret" not in config_path.read_text(encoding="utf-8")
