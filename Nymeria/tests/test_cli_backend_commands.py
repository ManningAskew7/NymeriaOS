"""Tests for CLI backend slash-command proxy registration."""

from __future__ import annotations

from typing import Any, Optional

from cli_fixtures import run
from nymeria.triggers.cli.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import memory, model
from nymeria.triggers.cli.commands.backend import BackendCommandProvider


class _FakeCommandClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: Optional[str] = None,
        source: str = "cli",
        actor: Optional[str] = None,
        surface: Optional[str] = None,
        user_id: Optional[str] = None,
        supports_forms: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
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


def command_info(
    name: str,
    *,
    category: str = "Memory",
    execution_kind: str = "command",
) -> dict[str, Any]:
    path = name.split()
    return {
        "id": ".".join(path),
        "name": name,
        "path": path,
        "usage": f"/{name}",
        "description": f"Backend {name}",
        "category": category,
        "execution_kind": execution_kind,
        "aliases": [],
    }


def make_context(client: Any, output: ListCommandOutputSink) -> CommandContext:
    return CommandContext(
        client=client,
        output=output,
        thread_id="cli-thread",
        user_id="alice",
    )


def test_backend_provider_overrides_duplicate_memory_subcommand():
    registry = CommandRegistry(include_builtins=False)
    memory.register(registry)
    BackendCommandProvider([command_info("memory save")]).register(registry)

    client = _FakeCommandClient()
    sink = ListCommandOutputSink()
    result = run(
        registry.dispatch_async(
            make_context(client, sink),
            "/memory save color deep blue",
        )
    )

    assert result.ok is True
    assert client.calls == [
        {
            "command": "/memory save color deep blue",
            "thread_id": "cli-thread",
            "source": "cli",
            "actor": "user",
            "surface": "cli",
            "user_id": "alice",
        }
    ]
    assert sink.messages[0].content == "backend result for /memory save color deep blue"


def test_backend_model_root_wins_local_registration():
    """The /model root is backend-owned (it returns the declarative form).

    Since the form contract shipped, the backend proxy owns the /model root
    (the payload in ``CommandResult.data`` carries the picker form) and the
    old ``_LOCAL_ROOT_WINS`` exception is gone. Local subcommands still merge
    under the backend root.
    """
    registry = CommandRegistry(include_builtins=False)
    model.register(registry)
    BackendCommandProvider([command_info("model", category="LLM")]).register(registry)

    root = registry.get("model")
    assert root is not None
    assert root.handler is not model._handle_model_context
    assert root.metadata.get("backend_command") is True
    assert registry.resolve("/model show").path == ("model", "show")
    assert registry.resolve("/model show").command.metadata.get("backend_command") is None


def test_backend_model_root_wins_backend_first_registration_order():
    """Order independence: backend provider first, local module second."""
    registry = CommandRegistry(include_builtins=False)
    BackendCommandProvider([command_info("model", category="LLM")]).register(registry)
    model.register(registry)

    root = registry.get("model")
    assert root is not None
    assert root.handler is not model._handle_model_context
    assert root.metadata.get("backend_command") is True
    assert registry.resolve("/model show").path == ("model", "show")
    assert registry.resolve("/model show").command.metadata.get("backend_command") is None


def test_model_root_forwards_set_shorthand_args_to_backend():
    """/model <name> [global|thread] still reaches the backend command."""
    registry = CommandRegistry(include_builtins=False)
    model.register(registry)
    BackendCommandProvider([command_info("model", category="LLM")]).register(registry)

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/model gpt-test thread",
        )
    )

    assert result.ok is True
    assert client.calls[0]["command"] == "/model gpt-test thread"


def test_backend_provider_adds_new_group_subcommands_to_help_once():
    registry = CommandRegistry(include_builtins=True)
    BackendCommandProvider(
        [
            command_info("todos list", category="TODOs"),
            command_info("todos add", category="TODOs"),
        ]
    ).register(registry)

    entries = registry.get_palette_entries()
    labels = [entry.usage for entry in entries]

    assert labels.count("/todos list") == 1
    assert "/todos add" in labels
    assert "/help [query]" in labels


def test_backend_provider_registers_chat_stream_commands_for_cli_forwarding():
    registry = CommandRegistry(include_builtins=False)
    BackendCommandProvider(
        [
            command_info(
                "skill",
                category="Skills",
                execution_kind="chat_stream",
            )
        ]
    ).register(registry)

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/skill draft-helper polish this",
        )
    )

    assert result.ok is True
    assert result.payload["chat_stream_command"] == "/skill draft-helper polish this"
    assert client.calls == []


def test_backend_provider_silently_overrides_unmanaged_local_subcommand():
    """Backend subcommands replace local same-named subcommands without raising.

    Previously this raised ``ValueError`` unless the path was on a hand-
    maintained whitelist. The merger now resolves collisions structurally:
    backend wins, the local handler is replaced by the backend proxy.
    """
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="custom",
            description="Local custom group",
            usage="/custom local",
            handler=lambda *_args: None,
            subcommands={
                "run": Command(
                    name="run",
                    description="Local run",
                    usage="run",
                    handler=lambda *_args: None,
                )
            },
        )
    )

    BackendCommandProvider([command_info("custom run")]).register(registry)

    match = registry.resolve("/custom run")
    assert match is not None
    assert match.path == ("custom", "run")
    assert match.command.metadata.get("backend_command") is True


def test_backend_root_overrides_local_root_and_preserves_local_subcommands():
    """When local-first ordering: backend root wins, local subs merge in.

    Regression guard for the order-independent merge in
    ``CommandRegistry.register``. Local /custom registers first with two
    subs; backend /custom root then registers. The backend handler runs
    at the root, but the local subcommands still resolve.
    """
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="custom",
            description="Local custom group",
            usage="/custom",
            handler=lambda *_args: None,
            subcommands={
                "first": Command(
                    name="first",
                    description="Local first sub",
                    usage="first",
                    handler=lambda *_args: None,
                ),
                "second": Command(
                    name="second",
                    description="Local second sub",
                    usage="second",
                    handler=lambda *_args: None,
                ),
            },
        )
    )

    BackendCommandProvider([command_info("custom", category="Other")]).register(registry)

    root = registry.get("custom")
    assert root is not None
    assert root.metadata.get("backend_command") is True
    assert sorted(root.subcommands.keys()) == ["first", "second"]
    assert registry.resolve("/custom first").path == ("custom", "first")


def test_backend_registration_is_idempotent_for_brand_new_commands():
    """Adding a new backend command never raises even with local modules loaded.

    Locks in the structural guarantee that new backend commands (like /skill,
    /goal, /orchestrate from commit 5734bf8) cannot break CLI startup.
    """
    registry = CommandRegistry(include_builtins=False)
    memory.register(registry)
    model.register(registry)

    # Simulate the additions that broke main on 2026-05-18: new chat_stream
    # commands plus a /skills root that would have collided with local /skills.
    BackendCommandProvider(
        [
            command_info("skill", category="Skills", execution_kind="chat_stream"),
            command_info("kit", category="Skills", execution_kind="chat_stream"),
            command_info("skills", category="Skills"),
            command_info("skills list", category="Skills"),
            command_info("goal", category="Goals", execution_kind="chat_stream"),
            command_info("orchestrate", category="Other", execution_kind="chat_stream"),
        ]
    ).register(registry)

    for path in ("/skill", "/kit", "/skills", "/skills list", "/goal", "/orchestrate"):
        match = registry.resolve(path)
        assert match is not None, f"{path} did not resolve"
        assert match.command.metadata.get("backend_command") is True


def merged_registry() -> CommandRegistry:
    """Build the tree a running CLI actually resolves against.

    Backend proxies first, then every local command module, exactly as
    ``CLIApp._register_all_commands`` does. The unit tests above use
    hand-built catalogs; these use the REAL one, because the questions they
    answer (which spelling wins a key, what the palette advertises) are only
    meaningful against the shipped catalog.
    """
    from nymeria.triggers.cli.commands import (
        account,
        activity,
        artifacts,
        backend as backend_module,
        clipboard,
        connection,
        context,
        conversation,
        doctor,
        export,
        mcp as mcp_module,
        memory as memory_module,
        model as model_module,
        skills,
        statusbar,
        system,
        theme,
        todos,
        toolicon,
        tools,
        triggers,
    )

    registry = CommandRegistry()
    backend_module.register(registry)
    for module in (
        system,
        connection,
        context,
        model_module,
        tools,
        skills,
        mcp_module,
        todos,
        memory_module,
        account,
        triggers,
        activity,
        artifacts,
        doctor,
        theme,
        toolicon,
        statusbar,
        export,
        clipboard,
        conversation,
    ):
        module.register(registry)
    return registry


def test_palette_speaks_the_canonical_spelling_for_renamed_commands():
    """The command palette advertises canon, not the retired spellings.

    Backlog #131 renamed these; the old spellings survive as whole-path
    aliases, and an alias must never be what the CLI teaches.
    """
    entries = {entry.text for entry in merged_registry().get_palette_entries()}

    for canonical in (
        "/thread create",
        "/thread show",
        "/tools list",
        "/model list",
        "/todos list",
        "/account show",
        "/memory delete",
        "/skills show",
        "/mcp delete",
        "/artifacts list",
        "/hook history",
        "/activity list",
        "/settings",
    ):
        assert canonical in entries, f"{canonical} missing from the palette"

    for retired in (
        "/thread new",
        "/thread info",
        "/tools core",
        "/tools optional",
        "/tools enabled",
        "/tools category",
        "/account current",
        "/memory forget",
        "/skills inspect",
        "/mcp remove",
        "/artifacts recent",
        "/hook log",
        "/activity recent",
        "/branch",
        "/models",
        "/tasks",
        "/config",
    ):
        assert retired not in entries, f"{retired} is still advertised"


def test_freed_backend_spellings_do_not_fall_through_to_a_local_handler():
    """A rename frees a key; no local command may quietly inherit it.

    Every entry here was a BACKEND command before backlog #131 and is a
    backend alias after it. If a local declaration survives under the freed
    key, the CLI becomes the one surface where the old spelling means
    something else.
    """
    registry = merged_registry()

    for retired in (
        "/account current",
        "/memory forget",
        "/skills inspect",
        "/mcp remove fetch",
        "/tools core",
        "/tools optional",
        "/activity recent",
    ):
        match = registry.resolve(retired)
        assert match is not None, f"{retired} did not resolve"
        assert match.command.metadata.get("backend_command") is True, (
            f"{retired} resolved to a local handler"
        )


def test_retired_root_spellings_still_reach_their_replacement():
    """`/branch`, `/fork`, `/models`, `/tasks` were roots before the rename.

    They are whole-path aliases of deeper commands now. Every other surface
    resolves those server-side; the CLI resolves the first token itself, so
    without the hidden root proxies these would be unknown commands here.
    """
    registry = merged_registry()
    expected = {
        "/branch": "/thread branch",
        "/fork": "/thread branch",
        "/models": "/model list",
        "/tasks": "/todos list",
    }

    for raw, canonical in expected.items():
        match = registry.resolve(raw)
        assert match is not None, f"{raw} did not resolve"
        assert match.command.hidden is True, f"{raw} should not be advertised"
        assert match.command.metadata.get("backend_path") == tuple(
            canonical.lstrip("/").split()
        )

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/tasks all",
        )
    )

    assert result.ok is True
    assert client.calls[0]["command"] == "/todos list all"


def test_flat_family_aliases_do_not_become_cli_roots():
    """`/thread_new` and friends stay off the CLI root namespace.

    They are the chat platforms' flattening of a path the CLI reaches by
    typing the path, so promoting all 100-plus of them would bury the real
    roots in autocomplete for no reachability gain.
    """
    registry = merged_registry()

    for flat in ("/thread_new", "/thread_create", "/hook_log", "/mcp_rm", "/tools_core"):
        assert registry.resolve(flat) is None, f"{flat} should not be a CLI root"
