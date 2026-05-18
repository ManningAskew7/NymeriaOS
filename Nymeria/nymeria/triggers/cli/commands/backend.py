"""Backend-backed CLI command proxies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult

_CLI_CATEGORY_MAP = {
    "LLM": "Model",
    "Memory": "Personal",
    "Settings": "System",
    "Status": "Context",
    "Thread": "Context",
    "TODOs": "Personal",
}


class BackendCommandProvider:
    """Registers backend global commands as CLI proxy handlers."""

    def __init__(self, commands: Sequence[Mapping[str, Any]]) -> None:
        self.commands = list(commands)

    @classmethod
    def from_service(cls) -> "BackendCommandProvider":
        """Build a provider from the in-process backend command registry."""
        from ....core.command_service import get_command_service

        commands = [
            asdict(info)
            for info in get_command_service().list_commands(
                actor="user",
                surface="cli",
                is_admin=True,
            )
        ]
        return cls(commands)

    @classmethod
    async def fetch(
        cls,
        client: Any,
        *,
        user_id: str = "default",
    ) -> "BackendCommandProvider":
        """Fetch the backend command catalog through a transport when possible."""
        list_commands = getattr(client, "list_commands", None)
        if callable(list_commands):
            commands = await list_commands(
                actor="user",
                surface="cli",
                user_id=user_id,
            )
            return cls([_mapping(command) for command in commands])
        return cls.from_service()

    def register(self, registry: CommandRegistry) -> None:
        """Register every backend command as a CLI proxy.

        Collisions with local CLI commands are handled by the registry's
        backend-wins-over-local rule (see ``CommandRegistry.register``).
        Protected builtins (``/help``, ``/cls``, ``/exit``) are preserved by
        the registry's builtin protection. No whitelists are needed here.
        """
        for info in self.commands:
            execution_kind = str(info.get("execution_kind") or "command")
            if execution_kind not in {"command", "chat_stream"}:
                continue
            path = _path(info)
            if not path:
                continue
            self._register_path(registry, info, path)

    def _register_path(
        self,
        registry: CommandRegistry,
        info: Mapping[str, Any],
        path: tuple[str, ...],
    ) -> None:
        if len(path) == 1:
            registry.register(_backend_command(info, path))
            return
        root = registry.get(path[0])
        if root is None:
            registry.register(_backend_group_command(path[0], info))
            root = registry.get(path[0])

        proxy = _backend_command(info, path, parent=root.name)
        existing = root.subcommands.get(path[1])
        if existing is not None:
            proxy.aliases = sorted({*proxy.aliases, *existing.aliases})
        root.subcommands[path[1]] = proxy


def register(registry: CommandRegistry) -> None:
    """Register backend global command proxies on a CLI registry."""
    BackendCommandProvider.from_service().register(registry)


def _backend_group_command(root: str, info: Mapping[str, Any]) -> Command:
    category = _cli_category(info)

    async def _handle_group(_context: CommandContext, _args: list[str]) -> CommandResult:
        return CommandResult.failed(
            f"Usage: /{root} <subcommand>. Type /help {root} for options.",
            command_path=(root,),
            error_code="usage_error",
        )

    return Command(
        name=root,
        description=f"{root} commands",
        usage=f"/{root} <subcommand>",
        handler=_handle_group,
        handler_mode="context",
        category=category,
        metadata={"backend_command": True, "backend_group": True},
    )


def _backend_command(
    info: Mapping[str, Any],
    path: tuple[str, ...],
    *,
    parent: str | None = None,
) -> Command:
    usage = str(info.get("usage") or f"/{' '.join(path)}")
    command_id = str(info.get("id") or ".".join(path))
    execution_kind = str(info.get("execution_kind") or "command")
    aliases = [
        str(alias)
        for alias in info.get("aliases", [])
        if isinstance(alias, str) and _alias_belongs_to_parent(alias, parent)
    ]

    async def _handler(context: CommandContext, args: list[str]) -> CommandResult:
        if execution_kind == "chat_stream":
            return _chat_stream_command_result(path, args)
        return await _execute_backend_command(context, path, args)

    return Command(
        name=path[-1],
        aliases=aliases,
        description=str(info.get("description") or ""),
        usage=usage,
        handler=_handler,
        handler_mode="context",
        category=_cli_category(info),
        metadata={
            "backend_command": True,
            "backend_command_id": command_id,
            "backend_path": path,
            "execution_kind": execution_kind,
        },
    )


def _chat_stream_command_result(
    path: tuple[str, ...],
    args: list[str],
) -> CommandResult:
    raw_command = "/" + " ".join((*path, *[str(arg) for arg in args])).strip()
    return CommandResult.completed(
        command_path=path,
        payload={
            "chat_stream_command": raw_command,
            "backend_command": True,
        },
    )


async def _execute_backend_command(
    context: CommandContext,
    path: tuple[str, ...],
    args: list[str],
) -> CommandResult:
    client = context.client
    execute_command = getattr(client, "execute_command", None)
    if not callable(execute_command):
        return CommandResult.failed(
            "Connected transport does not expose backend slash commands.",
            command_path=path,
            error_code="unsupported_transport",
        )

    raw_command = "/" + " ".join((*path, *[str(arg) for arg in args])).strip()
    result = await execute_command(
        raw_command,
        thread_id=context.thread_id,
        source="cli",
        actor="user",
        surface="cli",
        user_id=context.user_id,
    )
    payload = _mapping(result)
    markdown = str(payload.get("markdown") or "").strip()
    success = bool(payload.get("success", True))
    level = _message_level(str(payload.get("level") or ""))
    if not markdown:
        markdown = "Done." if success else "Command returned no output."

    message = CommandMessage(markdown, level=level)
    if success:
        return CommandResult.completed(
            message,
            command_path=path,
            payload={"backend_command": True, "command": payload.get("command")},
            json_payload=payload,
        )
    return CommandResult.failed(
        message,
        command_path=path,
        error_code="backend_command_error",
        payload={"backend_command": True, "command": payload.get("command")},
        json_payload=payload,
    )


def _path(info: Mapping[str, Any]) -> tuple[str, ...]:
    path = info.get("path")
    if isinstance(path, Sequence) and not isinstance(path, (str, bytes)):
        return tuple(str(part).strip().casefold() for part in path if str(part).strip())
    name = str(info.get("name") or "").strip()
    if not name:
        return ()
    return tuple(part.casefold() for part in name.split() if part)


def _mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _cli_category(info: Mapping[str, Any]) -> str:
    raw = str(info.get("category") or "Other")
    return _CLI_CATEGORY_MAP.get(raw, raw)


def _message_level(value: str) -> str:
    value = value.casefold()
    if value in {"success", "warning", "error"}:
        return value
    return "info"


def _alias_belongs_to_parent(alias: str, parent: str | None) -> bool:
    if parent is None:
        return " " not in alias.strip().lstrip("/")
    normalized = alias.strip().lstrip("/")
    return normalized.startswith(f"{parent} ")


__all__ = ["BackendCommandProvider", "register"]
