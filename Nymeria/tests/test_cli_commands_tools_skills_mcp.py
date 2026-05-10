from __future__ import annotations

import asyncio
import copy
from typing import Any

from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import mcp, skills, tools


def run(coro):
    return asyncio.run(coro)


class CapabilityFakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.thread_config = {
            "thread_id": "thread-1",
            "enabled_tools": ["memory"],
            "disabled_tools": [],
            "enabled_skills": [],
            "disabled_skills": [],
        }
        self.default_tools = ["filesystem"]
        self.unified_tools = [
            {
                "id": "filesystem",
                "name": "filesystem",
                "description": "Read and write files",
                "category": "filesystem",
                "enabled": True,
                "enabled_reason": "default_thread_tools",
                "tool_type": "builtin",
            },
            {
                "id": "web_search",
                "name": "web_search",
                "description": "Search the web",
                "category": "web",
                "enabled": False,
                "enabled_reason": "default_thread_tools",
                "tool_type": "builtin",
            },
            {
                "id": "custom.weather",
                "name": "Weather",
                "description": "Weather lookup",
                "category": "custom",
                "enabled": True,
                "enabled_reason": "default",
                "tool_type": "custom",
            },
        ]
        self.skills = [
            {
                "name": "skill-creator",
                "description": "Create skills",
                "scope": "user",
                "required_tools": ["filesystem"],
                "allowed_tools": [],
                "is_skill_kit": True,
            },
            {
                "name": "trigger-management",
                "description": "Manage triggers",
                "scope": "bundled",
                "required_tools": [],
                "allowed_tools": ["http_api"],
                "is_skill_kit": False,
            },
        ]
        self.global_skills = ["trigger-management"]
        self.mcp_servers = [
            {
                "id": "fetch",
                "name": "Fetch",
                "enabled": True,
                "transport": "stdio",
                "install_status": "ready",
                "discovered_tools": [{"name": "fetch", "description": "Fetch URL"}],
                "install_logs": ["installed fetch", "discovered fetch"],
            },
            {
                "id": "draft",
                "name": "Draft",
                "enabled": False,
                "transport": "stdio",
                "install_status": "draft",
                "discovered_tools": [],
                "last_error": "missing TOKEN",
                "install_logs": ["missing TOKEN"],
            },
        ]

    async def get_unified_tools(self, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("get_unified_tools", {"user_id": user_id}))
        return {"tools": copy.deepcopy(self.unified_tools), "total": len(self.unified_tools)}

    async def get_optional_tools(self, user_id: str = "default") -> list[dict[str, Any]]:
        self.calls.append(("get_optional_tools", {"user_id": user_id}))
        return [copy.deepcopy(self.unified_tools[1])]

    async def get_thread_config(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_thread_config", {"thread_id": thread_id, "user_id": user_id})
        )
        return copy.deepcopy({**self.thread_config, "thread_id": thread_id})

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            ("update_thread_config", {"thread_id": thread_id, "user_id": user_id, **kwargs})
        )
        self.thread_config.update(kwargs)
        return copy.deepcopy(self.thread_config)

    async def get_default_tools(self, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("get_default_tools", {"user_id": user_id}))
        return {
            "default_tools": copy.deepcopy(self.default_tools),
            "available_tools": [
                {**tool, "is_default": tool["id"] in self.default_tools}
                for tool in self.unified_tools
            ],
        }

    async def set_default_tools(
        self,
        tool_names: list[str],
        user_id: str = "default",
    ) -> dict[str, Any]:
        self.calls.append(
            ("set_default_tools", {"tool_names": tool_names, "user_id": user_id})
        )
        self.default_tools = list(tool_names)
        return {"status": "ok", "default_tools": copy.deepcopy(self.default_tools)}

    async def reset_default_tools(self, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("reset_default_tools", {"user_id": user_id}))
        self.default_tools = ["filesystem"]
        return {"status": "ok", "default_tools": copy.deepcopy(self.default_tools)}

    async def test_custom_tool(
        self,
        tool_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("test_custom_tool", {"tool_id": tool_id, "params": params}))
        return {"status": "ok", "tool_id": tool_id, "result": {"params": params}}

    async def list_skills(
        self,
        user_id: str = "default",
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_skills", {"user_id": user_id, "scope": scope}))
        if scope:
            return [copy.deepcopy(skill) for skill in self.skills if skill["scope"] == scope]
        return copy.deepcopy(self.skills)

    async def search_skills_marketplace(
        self,
        source: str = "anthropic",
        query: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("search_skills_marketplace", {"source": source, "query": query, "user_id": user_id})
        )
        return [
            {
                "name": "skill-creator",
                "description": "Create and package skills",
                "source": source,
            }
        ]

    async def install_skill(
        self,
        request: dict[str, Any],
        user_id: str = "default",
    ) -> dict[str, Any]:
        self.calls.append(("install_skill", {"request": request, "user_id": user_id}))
        installed = {
            "name": request["name"],
            "description": "Installed",
            "scope": request.get("scope", "user"),
        }
        self.skills.append(installed)
        return installed

    async def get_skill(self, name: str, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("get_skill", {"name": name, "user_id": user_id}))
        return {
            "name": name,
            "description": "Create skills",
            "scope": "user",
            "required_tools": ["filesystem"],
            "allowed_tools": [],
            "is_skill_kit": True,
            "path": "/skills/skill-creator/SKILL.md",
            "scripts": ["scripts/build.py"],
            "references": ["references/spec.md"],
            "body": "Skill body content",
        }

    async def get_global_skills(self, user_id: str = "default") -> list[str]:
        self.calls.append(("get_global_skills", {"user_id": user_id}))
        return copy.deepcopy(self.global_skills)

    async def set_global_skills(
        self,
        skill_names: list[str],
        user_id: str = "default",
    ) -> list[str]:
        self.calls.append(
            ("set_global_skills", {"skill_names": skill_names, "user_id": user_id})
        )
        self.global_skills = list(skill_names)
        return copy.deepcopy(self.global_skills)

    async def list_mcp_servers(self) -> dict[str, Any]:
        self.calls.append(("list_mcp_servers", {}))
        return {"servers": copy.deepcopy(self.mcp_servers), "total": len(self.mcp_servers)}

    async def get_mcp_server(self, server_id: str) -> dict[str, Any]:
        self.calls.append(("get_mcp_server", {"server_id": server_id}))
        for server in self.mcp_servers:
            if server["id"] == server_id:
                return copy.deepcopy(server)
        return {"id": server_id, "install_logs": []}

    async def install_mcp_server(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("install_mcp_server", {"request": request}))
        server = {
            "id": "new-fetch",
            "name": request.get("name") or "New Fetch",
            "enabled": True,
            "install_status": "ready",
            "discovered_tools": [{"name": "fetch"}],
            "install_logs": ["ready"],
        }
        self.mcp_servers.append(server)
        return {"status": "ok", "server": server, "discovered_tools": 1}

    async def delete_mcp_server(self, server_id: str) -> dict[str, Any]:
        self.calls.append(("delete_mcp_server", {"server_id": server_id}))
        self.mcp_servers = [server for server in self.mcp_servers if server["id"] != server_id]
        return {"status": "ok", "deleted": server_id}

    async def discover_mcp_server_tools(self, server_id: str) -> dict[str, Any]:
        self.calls.append(("discover_mcp_server_tools", {"server_id": server_id}))
        return {"status": "ok", "server_id": server_id, "count": 2}

    async def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        self.calls.append(("test_mcp_server", {"server_id": server_id}))
        return {"status": "ok", "tools_count": 1}

    async def retry_mcp_server_install(
        self,
        server_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            ("retry_mcp_server_install", {"server_id": server_id, "request": request})
        )
        server = {"id": server_id, "name": "Draft", "install_status": "ready"}
        return {"status": "ok", "server": server, "discovered_tools": 1}


def make_registry() -> CommandRegistry:
    registry = CommandRegistry()
    tools.register(registry)
    skills.register(registry)
    mcp.register(registry)
    return registry


def make_context(
    client: CapabilityFakeClient,
    *,
    output: ListCommandOutputSink | None = None,
    actions: list[Any] | None = None,
    confirm: bool = False,
) -> CommandContext:
    return CommandContext(
        client=client,
        output=output or ListCommandOutputSink(),
        dispatch_state=(actions.append if actions is not None else None),
        confirm_handler=lambda _prompt: confirm,
        thread_id="thread-1",
        user_id="alice",
    )


def test_tools_commands_use_api_client_methods() -> None:
    client = CapabilityFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    actions: list[Any] = []
    ctx = make_context(client, output=sink, actions=actions)

    assert run(registry.dispatch_async(ctx, "/tools list")).ok is True
    assert run(registry.dispatch_async(ctx, "/tools enable web_search")).ok is True
    assert run(registry.dispatch_async(ctx, "/tools disable filesystem")).ok is True
    assert run(registry.dispatch_async(ctx, "/tools optional")).ok is True
    assert run(registry.dispatch_async(ctx, "/tools defaults")).ok is True
    assert run(registry.dispatch_async(ctx, "/tools defaults add web_search")).ok is True
    assert run(registry.dispatch_async(ctx, "/tools defaults remove filesystem")).ok is True
    assert run(
        registry.dispatch_async(ctx, "/tools test custom.weather city=Tulsa days=2")
    ).ok is True

    assert ("get_unified_tools", {"user_id": "alice"}) in client.calls
    assert (
        "update_thread_config",
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "enabled_tools": ["memory", "web_search"],
            "disabled_tools": [],
        },
    ) in client.calls
    assert (
        "update_thread_config",
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "enabled_tools": ["memory", "web_search"],
            "disabled_tools": ["filesystem"],
        },
    ) in client.calls
    assert (
        "test_custom_tool",
        {"tool_id": "custom.weather", "params": {"city": "Tulsa", "days": 2}},
    ) in client.calls
    assert actions == [
        {"type": "thread_config_updated", "thread_id": "thread-1"},
        {"type": "thread_config_updated", "thread_id": "thread-1"},
    ]
    assert any("Default tools saved" in message.content for message in sink.messages)


def test_skills_commands_use_api_client_methods() -> None:
    client = CapabilityFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    actions: list[Any] = []
    ctx = make_context(client, output=sink, actions=actions)

    assert run(registry.dispatch_async(ctx, "/skills list")).ok is True
    assert run(registry.dispatch_async(ctx, "/skills search creator")).ok is True
    assert run(
        registry.dispatch_async(ctx, "/skills install skill-creator --scope user")
    ).ok is True
    assert run(registry.dispatch_async(ctx, "/skills enable skill-creator")).ok is True
    assert run(registry.dispatch_async(ctx, "/skills disable skill-creator")).ok is True
    assert run(
        registry.dispatch_async(ctx, "/skills enable --global skill-creator")
    ).ok is True
    assert run(registry.dispatch_async(ctx, "/skills inspect skill-creator")).ok is True

    assert (
        "search_skills_marketplace",
        {"source": "anthropic", "query": "creator", "user_id": "alice"},
    ) in client.calls
    assert (
        "install_skill",
        {
            "request": {
                "name": "skill-creator",
                "source": "anthropic",
                "scope": "user",
            },
            "user_id": "alice",
        },
    ) in client.calls
    assert (
        "update_thread_config",
        {
            "thread_id": "thread-1",
            "user_id": "alice",
            "enabled_skills": ["skill-creator"],
            "disabled_skills": [],
        },
    ) in client.calls
    assert (
        "set_global_skills",
        {"skill_names": ["skill-creator", "trigger-management"], "user_id": "alice"},
    ) in client.calls
    assert actions == [
        {"type": "thread_config_updated", "thread_id": "thread-1"},
        {"type": "thread_config_updated", "thread_id": "thread-1"},
    ]
    assert any("Skill body content" in message.content for message in sink.messages)


def test_mcp_commands_use_api_client_methods_and_confirm_destructive_actions() -> None:
    client = CapabilityFakeClient()
    registry = make_registry()
    sink = ListCommandOutputSink()
    unconfirmed = make_context(client, output=sink, confirm=False)

    assert run(registry.dispatch_async(unconfirmed, "/mcp list")).ok is True
    assert run(registry.dispatch_async(unconfirmed, "/mcp status draft")).ok is True
    assert run(registry.dispatch_async(unconfirmed, "/mcp logs fetch 1")).ok is True
    assert run(registry.dispatch_async(unconfirmed, "/mcp test fetch")).ok is True
    assert run(registry.dispatch_async(unconfirmed, "/mcp discover fetch")).ok is True
    retry_result = run(registry.dispatch_async(unconfirmed, "/mcp retry draft"))
    remove_result = run(registry.dispatch_async(unconfirmed, "/mcp remove fetch"))

    assert retry_result.ok is True
    assert remove_result.ok is True
    assert not any(name == "retry_mcp_server_install" for name, _payload in client.calls)
    assert not any(name == "delete_mcp_server" for name, _payload in client.calls)
    assert sink.messages[-1].level == "warning"

    confirmed = make_context(client, output=ListCommandOutputSink(), confirm=True)
    assert run(registry.dispatch_async(confirmed, "/mcp retry draft")).ok is True
    assert run(
        registry.dispatch_async(
            confirmed,
            "/mcp add uvx mcp-server-fetch --name Fetch --thread current --yes",
        )
    ).ok is True
    assert run(registry.dispatch_async(confirmed, "/mcp remove fetch")).ok is True

    assert (
        "retry_mcp_server_install",
        {
            "server_id": "draft",
            "request": {"confirmed": True, "config_values": {}},
        },
    ) in client.calls
    assert (
        "install_mcp_server",
        {
            "request": {
                "source": "uvx mcp-server-fetch",
                "name": "Fetch",
                "confirmed": True,
                "auto_enable": True,
                "thread_id": "thread-1",
                "config_values": {},
            }
        },
    ) in client.calls
    assert ("delete_mcp_server", {"server_id": "fetch"}) in client.calls
