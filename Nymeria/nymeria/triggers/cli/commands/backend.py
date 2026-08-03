"""Backend-backed CLI command proxies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, Literal

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
    def from_service(cls, user_id: str | None = None) -> "BackendCommandProvider":
        """Build a provider from the in-process backend command registry."""
        from ....core.command_service import get_command_service

        commands = [
            asdict(info)
            for info in get_command_service().list_commands(
                actor="user",
                surface="cli",
                is_admin=True,
                user_id=user_id,
                include_user_aliases=True,
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
        list_commands_attr = getattr(client, "list_commands", None)
        if callable(list_commands_attr):
            list_commands: Any = list_commands_attr
            commands = await list_commands(
                actor="user",
                surface="cli",
                user_id=user_id,
            )
            return cls([_mapping(command) for command in commands])
        return cls.from_service(user_id)

    def register(self, registry: CommandRegistry) -> None:
        """Register every backend command as a CLI proxy.

        Collisions with local CLI commands are handled by the registry's
        backend-wins-over-local rule (see ``CommandRegistry.register``).
        Protected builtins (``/help``, ``/cls``, ``/exit``) are preserved by
        the registry's builtin protection. No whitelists are needed here.

        Root-shaped aliases register in a second pass so a real command
        always wins its own name.
        """
        registerable = [
            (info, path)
            for info in self.commands
            if str(info.get("execution_kind") or "command") in {"command", "chat_stream"}
            for path in (_path(info),)
            if path
        ]
        for info, path in registerable:
            self._register_path(registry, info, path)
        for info, path in registerable:
            self._register_root_aliases(registry, info, path)

    def register_user_alias_proxies(self, registry: CommandRegistry) -> None:
        """The DEFERRED third pass: run this AFTER local command modules.

        The backend provider registers before the locals so backend roots
        win, which means a proxy registered here-and-now for a user alias
        spelled like a CLI-LOCAL command (`retry`, `undo`, `theme`...)
        would land first and the backend-wins merge would then DISCARD the
        local root when it arrived. Deferring the pass makes the
        `registry.get` guard see the locals, so the catalog and local
        commands genuinely win the name.
        """
        for info in self.commands:
            path = _path(info)
            if path:
                self._register_user_alias_proxies(registry, info, path)

    def _register_root_aliases(
        self,
        registry: CommandRegistry,
        info: Mapping[str, Any],
        path: tuple[str, ...],
    ) -> None:
        """Keep root-shaped aliases of deeper commands typeable here.

        Backend aliases are whole paths and every other surface resolves them
        server-side, but the CLI resolves the first token against its own
        registry, so an alias naming a root the catalog no longer has
        (``/branch`` for ``thread branch``, ``/tasks`` for ``todos list``)
        would be an unknown command. Those register as HIDDEN root proxies:
        still typeable and Tab-completable, absent from the palette, which
        speaks canon. (Dispatch alone would survive without them via the
        registry's unknown-token backend fallback; completion is their
        remaining value.)

        Proxies forward the ALIAS spelling raw, never the target's
        canonical path: the backend's own expansion is the authority, and a
        substituted path would silently drop the argument tokens an
        INJECTED alias carries (the #131 wrong-view bug, one layer up).

        Flat ``<family>_<verb>`` aliases are skipped: they are the chat
        platforms' flattening of a path the CLI reaches by typing the path.
        """
        if len(path) < 2:
            return
        for raw_alias in info.get("aliases", []):
            if not isinstance(raw_alias, str):
                continue
            alias = raw_alias.strip().lstrip("/").casefold()
            if not alias or " " in alias:
                continue
            if alias == path[0] or alias.startswith(f"{path[0]}_"):
                continue
            if registry.get(alias) is not None:
                continue
            proxy = _backend_command(info, (alias,))
            proxy.name = alias
            proxy.aliases = []
            proxy.hidden = True
            proxy.usage = f"/{alias}"
            registry.register(proxy)

    def _register_user_alias_proxies(
        self,
        registry: CommandRegistry,
        info: Mapping[str, Any],
        path: tuple[str, ...],
    ) -> None:
        """The caller's user-defined aliases (#133) as RAW-FORWARD proxies.

        Same forwarding shape as ``_register_root_aliases`` (the alias
        spelling goes to the backend verbatim; its stamped, gate-preserving
        expansion is the authority, so a mid-session alias change is
        honored), but VISIBLE: hiding exists so the palette speaks canon
        for retired spellings, and a user's own vocabulary is not a retired
        spelling. With dispatch already covered by the unknown-token
        fallback, discoverability is the mirror's entire value. Local
        commands and the catalog win the name.
        """
        for raw_alias in info.get("user_aliases", []) or []:
            alias = str(raw_alias).strip().lstrip("/").casefold()
            if not alias or " " in alias:
                continue
            if registry.get(alias) is not None:
                continue
            proxy = _backend_command(info, (alias,))
            proxy.name = alias
            proxy.aliases = []
            proxy.usage = f"/{alias}"
            proxy.description = f"Your alias for /{_display_path(path)}"
            registry.register(proxy)

    def _register_path(
        self,
        registry: CommandRegistry,
        info: Mapping[str, Any],
        path: tuple[str, ...],
    ) -> None:
        """Register one backend command at its full depth.

        Every level of the path gets its own node, so a depth-3 command nests
        under its depth-2 parent instead of overwriting it. Before backlog
        #131 wave B this keyed everything off ``path[1]``: ``/account tokens
        issue`` and ``/account tokens revoke`` both landed on the ``tokens``
        key, so the LAST one registered answered ``/account tokens`` (with the
        real depth-2 command gone), ``/account tokens issue`` dispatched
        ``account tokens revoke`` with ``issue`` as an argument, and the
        palette advertised the leaf's name under the parent's key.

        Order independence matters: ``account tokens`` and its children arrive
        in catalog order, so an intermediate node may exist as a placeholder
        group before its real command shows up, or as a real command before
        its children do. Either way the node keeps whatever children and
        aliases it had.
        """
        if len(path) == 1:
            command = _backend_command(info, path)
            existing = registry.get(path[0])
            if (
                existing is not None
                and existing.name == _cli_token(path[0])
                and existing.metadata.get("backend_command")
            ):
                # The family's children may arrive BEFORE this root in
                # catalog order, leaving a backend group holding them. The
                # registry's same-tier rule REPLACES, so carry those
                # children onto the real root or they silently vanish.
                for sub_name, sub in existing.subcommands.items():
                    command.subcommands.setdefault(sub_name, sub)
            registry.register(command)
            return
        root = registry.get(path[0])
        if root is None:
            registry.register(_backend_group_command((path[0],), info))
            root = registry.get(path[0])
            assert root is not None, f"Failed to register group command {path[0]}"

        parent = root
        for depth in range(1, len(path) - 1):
            parent = _ensure_child(
                parent,
                lambda prefix=path[: depth + 1]: _backend_group_command(prefix, info),
                key=_cli_token(path[depth]),
            )
        _ensure_child(
            parent,
            lambda: _backend_command(info, path, parent=" ".join(path[:-1])),
            key=_cli_token(path[-1]),
            replace=True,
        )


def register(
    registry: CommandRegistry, user_id: str | None = None
) -> BackendCommandProvider:
    """Register backend global command proxies on a CLI registry.

    ``user_id`` rides into the catalog read so the caller's user-defined
    aliases (#133) mirror as raw-forward proxies. The local store answers,
    which is exact in the slim shape; against a remote backend the mirror
    is best-effort and the registry's unknown-token fallback stays the
    dispatch authority. Returns the provider so the caller can run
    ``register_user_alias_proxies`` AFTER the local command modules (see
    that method for why the pass must be deferred).
    """
    provider = BackendCommandProvider.from_service(user_id)
    provider.register(registry)
    return provider


def _ensure_child(
    parent: Command,
    build: Any,
    *,
    key: str,
    replace: bool = False,
) -> Command:
    """Get or install ``parent``'s child at ``key``, keeping what was there.

    ``replace`` marks the REAL command for a path: it takes the slot from a
    placeholder group (or an earlier local registration) but inherits that
    node's children and aliases, so registration order cannot cost a family a
    subcommand. Without ``replace`` an existing node wins untouched, which is
    what an intermediate group wants once the real command for that path has
    already registered.
    """
    existing = parent.subcommands.get(key)
    if existing is not None and not replace:
        return existing
    child = build()
    child.name = key
    if existing is not None:
        child.aliases = sorted({*child.aliases, *existing.aliases})
        for sub_key, sub in existing.subcommands.items():
            child.subcommands.setdefault(sub_key, sub)
    parent.subcommands[key] = child
    return child


def _backend_group_command(path: tuple[str, ...], info: Mapping[str, Any]) -> Command:
    """A non-invokable node for a path the backend registers no command at.

    Only reachable when a family's own path has no command of its own (every
    depth-2 parent in today's catalog does), so it exists to keep the tree
    well-formed rather than to be run.
    """
    category = _cli_category(info)
    display = _display_path(path)

    async def _handle_group(_context: CommandContext, _args: list[str]) -> CommandResult:
        return CommandResult.failed(
            f"Usage: /{display} <subcommand>. Type /help {display} for options.",
            command_path=path,
            error_code="usage_error",
        )

    return Command(
        name=_cli_token(path[-1]),
        description=f"{display} commands",
        usage=f"/{display} <subcommand>",
        handler=_handle_group,
        category=category,
        metadata={"backend_command": True, "backend_group": True},
    )


def _backend_command(
    info: Mapping[str, Any],
    path: tuple[str, ...],
    *,
    parent: str | None = None,
) -> Command:
    usage = str(info.get("usage") or f"/{_display_path(path)}")
    command_id = str(info.get("id") or ".".join(path))
    execution_kind = str(info.get("execution_kind") or "command")
    aliases = [
        str(alias)
        for alias in info.get("aliases", [])
        if isinstance(alias, str) and _alias_belongs_to_parent(alias, parent)
    ]
    # The command is KEYED by its displayed (hyphenated) token so the palette,
    # the completions and the inline usage hint all speak the spelling the
    # backend's generated usage advertises; the stored underscore spelling
    # rides along as an alias, because the backend parses either and a CLI
    # user who typed `/background set_url` yesterday must not lose it. Without
    # this the hyphenated form was not registered at all: `/sequential-tools`
    # was an unknown command here while every other surface accepted it.
    name = _cli_token(path[-1])
    if name != path[-1]:
        aliases.append(path[-1])

    async def _handler(context: CommandContext, args: list[str]) -> CommandResult:
        if execution_kind == "chat_stream":
            return _chat_stream_command_result(path, args)
        return await _execute_backend_command(context, path, args)

    return Command(
        name=name,
        aliases=aliases,
        description=str(info.get("description") or ""),
        usage=usage,
        handler=_handler,
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
    raw_command = "/" + " ".join((*path, *[str(arg) for arg in args])).strip()
    return await forward_raw_command(context, raw_command, command_path=path)


async def forward_raw_command(
    context: CommandContext,
    raw_command: str,
    *,
    command_path: tuple[str, ...] = (),
) -> CommandResult:
    """Send one command string to the backend dispatcher verbatim.

    The shared tail of every backend proxy, and (#133) the registry's
    unknown-token fallback: forwarding the TYPED text rather than a
    recomposed path is what lets the backend's own alias expansion (built-in
    and user-defined, both gate-preserving) answer for spellings this
    process has never heard of.
    """
    path = command_path or tuple(raw_command.lstrip("/").split()[:1])
    client = context.client
    execute_command_attr = getattr(client, "execute_command", None)
    if not callable(execute_command_attr):
        return CommandResult.failed(
            "Connected transport does not expose backend slash commands.",
            command_path=path,
            error_code="unsupported_transport",
        )
    execute_command: Any = execute_command_attr

    result = await execute_command(
        raw_command,
        thread_id=context.thread_id,
        source="cli",
        actor="user",
        surface="cli",
        user_id=context.user_id,
        supports_forms=context.supports_forms(),
    )
    payload = _mapping(result)
    markdown = str(payload.get("markdown") or "").strip()
    success = bool(payload.get("success", True))
    level = _message_level(str(payload.get("level") or ""))
    if not markdown:
        markdown = "Done." if success else "Command returned no output."

    message = CommandMessage(markdown, level=level)
    if success:
        data = payload.get("data")
        thread_switched = False
        if isinstance(data, Mapping):
            from .form_contract import (
                apply_state_hints,
                form_notes,
                form_spec_from_payload,
            )

            state = data.get("state")
            await apply_state_hints(state, context)
            thread_switched = _state_switches_thread(state)
            if context.supports_forms():
                spec = form_spec_from_payload(data.get("form"), context=context)
                if spec is not None:
                    await context.dispatch({"type": "open_form", "spec": spec})
                    # The markdown renders too: form-bearing commands are
                    # often status-shaped (/think's effective breakdown,
                    # /provider's active table) and the form replaces none
                    # of that output, so suppressing it would make the bare
                    # command the only surface that CANNOT show its status.
                    # EXCEPT when the form carries step notes: a chained
                    # step's markdown is mostly a reprint of the rail the
                    # panel already shows, so print only the delta lines
                    # (the full markdown remains for form-less surfaces).
                    notes = form_notes(data.get("form"))
                    if notes:
                        message = CommandMessage(
                            "\n".join(notes), level=level
                        )
                    return CommandResult.completed(
                        message,
                        command_path=path,
                        payload={
                            "backend_command": True,
                            "command": payload.get("command"),
                        },
                        json_payload=payload,
                    )
        # A switch_thread hint means the active thread changed server-side; flag
        # it so the REPL loads the new thread's history (the local /thread switch
        # handler set the same payload flag before the migration).
        return CommandResult.completed(
            message,
            command_path=path,
            payload={
                "backend_command": True,
                "command": payload.get("command"),
                "thread_switched": thread_switched,
            },
            json_payload=payload,
        )
    return CommandResult.failed(
        message,
        command_path=path,
        error_code="backend_command_error",
        payload={"backend_command": True, "command": payload.get("command")},
        json_payload=payload,
    )


def _state_switches_thread(state: Any) -> bool:
    """Whether a ``data["state"]`` hint asks the REPL to switch threads."""
    if not isinstance(state, Mapping):
        return False
    switch = state.get("switch_thread")
    return isinstance(switch, Mapping) and bool(str(switch.get("thread_id") or "").strip())


def _cli_token(token: str) -> str:
    """One path token as the CLI keys and shows it: hyphens, not underscores."""
    return token.replace("_", "-")


def _display_path(path: tuple[str, ...]) -> str:
    return " ".join(_cli_token(token) for token in path)


def _path(info: Mapping[str, Any]) -> tuple[str, ...]:
    """The backend's STORED path, which is what execution replays.

    Hyphens fold back to underscores because the ``name`` fallback reads a
    DISPLAYED name (``sequential-tools``) while the backend stores and
    dispatches on ``sequential_tools``; the ``path`` field is already stored
    form, so the fold is a no-op there.
    """
    path = info.get("path")
    if isinstance(path, Sequence) and not isinstance(path, (str, bytes)):
        parts = [str(part).strip() for part in path]
    else:
        parts = str(info.get("name") or "").strip().split()
    return tuple(
        part.casefold().replace("-", "_") for part in parts if part.strip()
    )


def _mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _cli_category(info: Mapping[str, Any]) -> str:
    raw = str(info.get("category") or "Other")
    return _CLI_CATEGORY_MAP.get(raw, raw)


def _message_level(value: str) -> Literal["error", "info", "success", "warning"]:
    value = value.casefold()
    if value == "success":
        return "success"
    if value == "warning":
        return "warning"
    if value == "error":
        return "error"
    return "info"


def _alias_belongs_to_parent(alias: str, parent: str | None) -> bool:
    if parent is None:
        return " " not in alias.strip().lstrip("/")
    normalized = alias.strip().lstrip("/")
    return normalized.startswith(f"{parent} ")


__all__ = ["BackendCommandProvider", "register"]
