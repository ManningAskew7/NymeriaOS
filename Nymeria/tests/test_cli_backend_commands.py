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

    They are whole-path aliases of deeper commands now, kept typeable and
    Tab-completable here as hidden proxies. Since the #133 unification the
    proxies forward the ALIAS spelling raw (the backend's own expansion is
    the authority; a substituted canonical path would drop the tokens an
    injected alias carries), so the pin is the raw forward, not a rewritten
    path.
    """
    registry = merged_registry()

    for raw in ("/branch", "/fork", "/models", "/tasks"):
        match = registry.resolve(raw)
        assert match is not None, f"{raw} did not resolve"
        assert match.command.hidden is True, f"{raw} should not be advertised"
        assert match.command.metadata.get("backend_path") == tuple(
            raw.lstrip("/").split()
        )

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/tasks all",
        )
    )

    assert result.ok is True
    assert client.calls[0]["command"] == "/tasks all"


def test_unknown_first_token_forwards_raw_to_the_backend():
    """The #133 fallback: the local registry no longer owns the last word.

    An unresolved first token forwards the TYPED text verbatim to the
    backend dispatcher, which expands user aliases (and built-in ones) or
    answers with its own did-you-mean copy. Offline, the local unknown
    error survives.
    """
    registry = CommandRegistry(include_builtins=False)
    BackendCommandProvider([command_info("memory save")]).register(registry)

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/gpt5 some args",
        )
    )
    assert result.ok is True
    assert client.calls[0]["command"] == "/gpt5 some args"

    # Known spellings still resolve locally: no forward happens.
    client.calls.clear()
    run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/memory save color blue",
        )
    )
    assert client.calls[0]["command"] == "/memory save color blue"

    # The CLI-local --json flag never reaches the wire: it was stripped
    # from the parsed invocation, and the forward strips it from the raw
    # text too.
    client.calls.clear()
    run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/gpt5 some args --json",
        )
    )
    assert client.calls[0]["command"] == "/gpt5 some args"

    # Offline (no transport): the local unknown-command error is kept.
    offline = run(
        registry.dispatch_async(
            make_context(None, ListCommandOutputSink()),
            "/gpt5",
        )
    )
    assert offline.ok is False
    assert offline.error_code == "unknown_command"


def test_user_alias_proxies_forward_the_typed_spelling():
    """A user alias in the catalog payload becomes a hidden proxy that
    forwards its OWN spelling, never the target's path: the alias can
    inject values, and a canonical-path proxy would silently drop them
    (the backend's stamped expansion is the authority).
    """
    info = command_info("model")
    info["user_aliases"] = ["/gpt5"]
    registry = CommandRegistry(include_builtins=False)
    provider = BackendCommandProvider([info])
    provider.register(registry)
    provider.register_user_alias_proxies(registry)

    proxy = registry.get("gpt5")
    assert proxy is not None
    # VISIBLE, unlike retired-spelling proxies: a user's own vocabulary is
    # not a retired spelling, and discoverability is the mirror's value.
    assert proxy.hidden is False
    assert "/gpt5" in {entry.text for entry in registry.get_palette_entries()}

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/gpt5 thread",
        )
    )
    assert result.ok is True
    assert client.calls[0]["command"] == "/gpt5 thread"


def test_user_alias_proxy_never_shadows_a_local_command_in_production_order():
    """The catalog and local commands win the name, under the ORDER app.py
    actually uses: backend provider first, local modules second, and the
    user-alias proxy pass DEFERRED to the very end.

    The first version of this test registered the local module first and
    passed while the live wiring was broken: a proxy registered in the
    provider's own pass landed before the local root, and the registry's
    backend-wins merge then DISCARDED the local command entirely (the #133
    correctness review's H1). The deferred pass is the fix; this pins it
    against the production sequence.
    """
    info = command_info("model")
    info["user_aliases"] = ["/memory"]
    registry = CommandRegistry(include_builtins=False)
    provider = BackendCommandProvider([info])
    provider.register(registry)
    memory.register(registry)
    provider.register_user_alias_proxies(registry)

    resolved = registry.get("memory")
    assert resolved is not None
    # The local family root survived; no alias proxy took the key.
    assert resolved.hidden is False
    assert resolved.subcommands
    assert resolved.metadata.get("backend_path") != ("memory",)


def test_a_transport_fault_on_the_fallback_degrades_not_crashes():
    """A backend hiccup on an UNKNOWN token must mirror the known-command
    containment (`command_exception`), not escape dispatch_async and kill
    the REPL (the #133 correctness review's M1)."""

    class _ExplodingClient:
        async def execute_command(self, *args, **kwargs):
            raise RuntimeError("boom")

    registry = CommandRegistry(include_builtins=False)
    result = run(
        registry.dispatch_async(
            make_context(_ExplodingClient(), ListCommandOutputSink()),
            "/gpt5",
        )
    )
    assert result.ok is False
    assert result.error_code == "command_exception"


def test_depth_three_commands_nest_instead_of_clobbering_their_parent():
    """`/account tokens` and its two children are three separate commands.

    Registration keyed every depth off ``path[1]`` before #131 wave B, so all
    three landed on the ``tokens`` key: the last leaf registered won, the
    depth-2 command was unreachable, and ``/account tokens issue`` dispatched
    ``account tokens revoke`` with ``issue`` as its argument. Each spelling
    must reach its OWN backend command.
    """
    registry = merged_registry()

    for typed, canonical in (
        ("/account tokens", ("account", "tokens")),
        ("/account tokens issue", ("account", "tokens", "issue")),
        ("/account tokens revoke", ("account", "tokens", "revoke")),
    ):
        match = registry.resolve(typed)
        assert match is not None, f"{typed} did not resolve"
        assert match.command.metadata.get("backend_path") == canonical, typed
        assert match.args == (), typed

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/account tokens issue laptop",
        )
    )

    assert result.ok is True
    assert client.calls[0]["command"] == "/account tokens issue laptop"


def test_family_root_arriving_after_its_children_keeps_them():
    """Catalog order must not decide whether a family has subcommands.

    The depth-1 arm of ``_register_path`` registered the root as-is, and
    the registry's same-tier rule REPLACES: a backend family root arriving
    AFTER its children swapped out the placeholder group holding them, so
    every subcommand went unknown (#131 review F2). The provider now
    carries the group's children onto the real root.
    """
    registry = CommandRegistry(include_builtins=False)
    BackendCommandProvider(
        [
            command_info("zzfam list"),
            command_info("zzfam delete"),
            command_info("zzfam"),  # root LAST: the order that used to wipe
        ]
    ).register(registry)

    root = registry.get("zzfam")
    assert root is not None
    # The real root command won the key (not the placeholder group)...
    assert root.metadata.get("backend_path") == ("zzfam",)
    # ...and the children registered before it are still reachable.
    for typed, canonical in (
        ("/zzfam list", ("zzfam", "list")),
        ("/zzfam delete", ("zzfam", "delete")),
    ):
        match = registry.resolve(typed)
        assert match is not None, f"{typed} did not resolve"
        assert match.command.metadata.get("backend_path") == canonical, typed


def test_palette_advertises_the_real_depth_three_spellings():
    """The palette taught a phantom before the nesting fix.

    The depth-3 leaf sat under the ``tokens`` key carrying its own leaf NAME,
    so the palette rendered `/account revoke`, a spelling no surface accepts,
    and never mentioned the two commands that do exist.
    """
    entries = {entry.text for entry in merged_registry().get_palette_entries()}

    assert "/account tokens" in entries
    assert "/account tokens issue" in entries
    assert "/account tokens revoke" in entries
    assert "/account revoke" not in entries
    assert "/account issue" not in entries


def test_backend_proxies_are_keyed_by_the_hyphenated_display_spelling():
    """One displayed spelling, both spellings typeable (#131 rule 4).

    The CLI resolves tokens against its own registry and does NOT fold
    hyphens, so keying the proxies on the stored underscore form left
    `/sequential-tools` an unknown command here while every other surface
    accepted it, and left the palette teaching a spelling the generated usage
    line contradicted.
    """
    registry = merged_registry()

    for hyphenated, underscored, canonical in (
        ("/sequential-tools", "/sequential_tools", ("sequential_tools",)),
        ("/background set-url", "/background set_url", ("background", "set_url")),
        (
            "/provider reasoning-passback",
            "/provider reasoning_passback",
            ("provider", "reasoning_passback"),
        ),
    ):
        for typed in (hyphenated, underscored):
            match = registry.resolve(typed)
            assert match is not None, f"{typed} did not resolve"
            assert match.command.metadata.get("backend_path") == canonical, typed
            assert match.args == (), typed

    entries = {entry.text for entry in registry.get_palette_entries()}
    assert "/sequential-tools" in entries
    assert "/sequential_tools" not in entries
    assert "/background set-url" in entries
    assert "/background set_url" not in entries

    # The retired spelling stays TYPEABLE, so it stays in the completion list
    # (flagged as an alias, which is what keeps it out of the slash panel).
    completions = {
        item.text: item for item in registry.get_completion_items()
    }
    assert completions["/sequential_tools"].alias is True
    assert completions["/background set_url"].alias is True


def test_dispatching_the_hyphenated_spelling_sends_the_stored_path():
    """The wire form is the backend's path, whichever spelling was typed."""
    registry = merged_registry()

    for typed in ("/background set-url http://x.test", "/background set_url http://x.test"):
        client = _FakeCommandClient()
        result = run(
            registry.dispatch_async(
                make_context(client, ListCommandOutputSink()),
                typed,
            )
        )
        assert result.ok is True, typed
        assert client.calls[0]["command"] == "/background set_url http://x.test"


def test_a_display_only_payload_still_yields_the_stored_backend_path():
    """`name` is a DISPLAY field now, so the path fallback must unfold it.

    The provider prefers the payload's `path`, but falls back to splitting
    `name` when a transport omits it. Since #131 wave B that name is
    hyphenated, so an unfolded fallback would send `/sequential-tools` on the
    wire and record a `backend_path` the dispatcher never stores.
    """
    registry = CommandRegistry(include_builtins=False)
    info = command_info("sequential-tools", category="Tools")
    del info["path"]
    BackendCommandProvider([info]).register(registry)

    match = registry.resolve("/sequential-tools")
    assert match is not None
    assert match.command.metadata["backend_path"] == ("sequential_tools",)

    client = _FakeCommandClient()
    result = run(
        registry.dispatch_async(
            make_context(client, ListCommandOutputSink()),
            "/sequential-tools on global",
        )
    )
    assert result.ok is True
    assert client.calls[0]["command"] == "/sequential_tools on global"


def test_subcommand_alias_completions_do_not_repeat_the_family_root():
    """Backend subcommand aliases are WHOLE paths; joining them twice lies.

    `("account", "current")` is stored as the alias "account current", so
    prefixing it with its parent produced `/account account current`, a
    completion that resolves to nothing on either side.
    """
    completions = {item.text for item in merged_registry().get_completion_items()}

    assert "/account current" in completions
    assert "/account account current" not in completions
    assert not [
        text
        for text in completions
        if len(text.split()) > 1 and text.split()[0].lstrip("/") == text.split()[1]
    ]


def test_flat_family_aliases_do_not_become_cli_roots():
    """`/thread_new` and friends stay off the CLI root namespace.

    They are the chat platforms' flattening of a path the CLI reaches by
    typing the path, so promoting all 100-plus of them would bury the real
    roots in autocomplete for no reachability gain.
    """
    registry = merged_registry()

    for flat in ("/thread_new", "/thread_create", "/hook_log", "/mcp_rm", "/tools_core"):
        assert registry.resolve(flat) is None, f"{flat} should not be a CLI root"
