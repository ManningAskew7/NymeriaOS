"""Shared choices_ref option resolvers (backlog #110).

One registry maps each ``choices_ref`` declared in the command catalog to a
live option resolver. These tests pin the resolver contract (form_option
shape, current marking, degrade-to-empty on faults), the registry-vs-catalog
mapping both ways, and the ``CommandService.resolve_options`` entry the
options endpoint rides.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
from nymeria.core.command_option_resolvers import (
    OPTION_RESOLVERS,
    resolve_fallback_models,
    resolve_hooks,
    resolve_mcp_servers,
    resolve_models,
    resolve_providers,
    resolve_skills,
    resolve_threads,
    resolve_todos,
    resolve_tools,
    resolve_triggers,
)
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
    _CommandExecutor,
)

# Declared refs whose resolver is not built yet: consumers fall back to free
# text / no autocomplete for these. Shrink toward empty; never grow without
# a recorded decision (the resolver registry is the point of #110). Empty
# since every declared ref resolves.
_PENDING_REFS: frozenset[str] = frozenset()


def _executor(api: Any, *, thread_id: str | None = "thread-1", is_admin: bool = True) -> _CommandExecutor:
    return _CommandExecutor(
        api=api,
        thread_id=thread_id,
        user_id="alice",
        actor="user",
        is_admin=is_admin,
        service=CommandService(),
        surface="cli",
    )


# ── registry <-> catalog mapping ─────────────────────────────────────────────


def test_every_resolver_ref_is_declared_by_some_param() -> None:
    service = CommandService()
    declared = {
        param.choices_ref
        for cmd in service._commands.values()
        if cmd.params
        for param in cmd.params
        if param.choices_ref
    }
    unclaimed = set(OPTION_RESOLVERS) - declared
    assert not unclaimed, (
        f"Resolvers with no declaring param (dead registry entries): {sorted(unclaimed)}"
    )


def test_every_declared_ref_has_a_resolver_or_a_recorded_exemption() -> None:
    service = CommandService()
    declared = {
        param.choices_ref
        for cmd in service._commands.values()
        if cmd.params
        for param in cmd.params
        if param.choices_ref
    }
    missing = declared - set(OPTION_RESOLVERS) - _PENDING_REFS
    assert not missing, (
        f"choices_ref values without a resolver: {sorted(missing)}. "
        "Add a resolver to OPTION_RESOLVERS or record the exemption in "
        "_PENDING_REFS with a reason."
    )
    stale = _PENDING_REFS & set(OPTION_RESOLVERS)
    assert not stale, f"_PENDING_REFS entries now resolved, remove: {sorted(stale)}"


# ── resolve_models ───────────────────────────────────────────────────────────


class _ModelsApi:
    def __init__(self) -> None:
        self.thread_config_reads: list[str] = []

    async def list_available_models(self) -> list[dict[str, Any]]:
        return [
            {"id": "gpt-test", "context_length": 128000},
            {"id": "gpt-next"},
            {"name": "named-only"},
            {"context_length": 1},  # no id: dropped
        ]

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        return {"llm_model": "gpt-test"}

    async def get_thread_config(self, thread_id: str) -> dict[str, Any]:
        self.thread_config_reads.append(thread_id)
        return {"llm_config": {"model": "gpt-next"}}


def test_resolve_models_builds_options_with_thread_current() -> None:
    api = _ModelsApi()
    options = run(resolve_models(_executor(api)))

    assert [o["id"] for o in options] == ["gpt-test", "gpt-next", "named-only"]
    assert options[0]["meta"] == "128.0k ctx"
    # The thread override wins the current marker, mirroring bare /model.
    assert [o["id"] for o in options if o["current"]] == ["gpt-next"]
    assert api.thread_config_reads == ["thread-1"]


def test_resolve_models_explicit_current_skips_the_lookup() -> None:
    api = _ModelsApi()
    options = run(resolve_models(_executor(api), current="gpt-test"))

    assert [o["id"] for o in options if o["current"]] == ["gpt-test"]
    assert api.thread_config_reads == []


def test_resolve_models_degrades_to_empty_on_listing_fault() -> None:
    class _Broken:
        async def list_available_models(self) -> list[dict[str, Any]]:
            raise RuntimeError("provider has no listing endpoint")

    assert run(resolve_models(_executor(_Broken()))) == []


# ── resolve_providers ────────────────────────────────────────────────────────


class _ProvidersApi:
    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        return {"llm_provider": "anthropic", "llm_model": "claude-fable-5"}

    async def get_env_vars(self, user_id: str | None = None) -> dict[str, Any]:
        return {
            "entries": [
                {"name": "anthropic_api_key", "is_set": True},
                {"name": "openai_api_key", "is_set": False},
            ]
        }


def test_resolve_providers_orders_tiers_and_marks_active() -> None:
    options = run(resolve_providers(_executor(_ProvidersApi())))

    ids = [o["id"] for o in options]
    assert "anthropic" in ids and "openai" in ids
    current = [o for o in options if o["current"]]
    assert [o["id"] for o in current] == ["anthropic"]
    # Managed provider meta carries the credential status the picker showed.
    anthropic = next(o for o in options if o["id"] == "anthropic")
    assert "authenticated" in anthropic["meta"]
    # Native tier sorts ahead of unverified (the picker's browse order).
    assert ids.index("anthropic") < ids.index("302ai")


def test_resolve_providers_degrades_to_empty_on_fault() -> None:
    class _Broken:
        async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
            raise RuntimeError("settings store down")

    assert run(resolve_providers(_executor(_Broken()))) == []


# ── resolve_fallback_models ──────────────────────────────────────────────────


class _FallbackApi:
    def __init__(self, chain: str) -> None:
        self._chain = chain

    async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
        return {"llm_fallback_models": self._chain}


def test_resolve_fallback_models_lists_the_configured_chain_in_order() -> None:
    # The value set of /fallback remove is the CONFIGURED CHAIN, not the
    # model catalog (which would offer mostly values the handler rejects).
    options = run(
        resolve_fallback_models(_executor(_FallbackApi(" model-a , model-b,model-c ")))
    )

    assert [o["id"] for o in options] == ["model-a", "model-b", "model-c"]
    assert [o["meta"] for o in options] == [
        "chain position 1",
        "chain position 2",
        "chain position 3",
    ]
    assert not any(o["current"] for o in options)


def test_resolve_fallback_models_empty_chain_yields_no_options() -> None:
    # No options means no generated picker: the bare command keeps its
    # usage error instead of offering an empty radio.
    assert run(resolve_fallback_models(_executor(_FallbackApi("")))) == []


def test_resolve_fallback_models_degrades_to_empty_on_fault() -> None:
    class _Broken:
        async def get_settings(self, user_id: str | None = None) -> dict[str, Any]:
            raise RuntimeError("settings store down")

    assert run(resolve_fallback_models(_executor(_Broken()))) == []


# ── resolve_tools ────────────────────────────────────────────────────────────


class _ToolsApi:
    """The two tool doors plus the thread overlay the /tools handlers read."""

    def __init__(self, thread_config: dict[str, Any] | None = None) -> None:
        self.thread_config = (
            thread_config
            if thread_config is not None
            else {"enabled_tools": ["send_email"], "disabled_tools": ["bash_execute"]}
        )
        self.default_tools_calls: list[str] = []

    async def get_tool_categories(self) -> dict[str, Any]:
        return {
            "categories": {
                "email": ["send_email", "read_email"],
                "system": ["bash_execute"],
            }
        }

    async def get_default_tools(self, user_id: str = "default") -> dict[str, Any]:
        self.default_tools_calls.append(user_id)
        return {
            "default_tools": ["bash_execute", "web_search"],
            "available_tools": [
                {"name": "web_search", "description": "Search the web\nsecond line"},
                {"name": "bash_execute", "description": "Run a shell command"},
                {"name": "send_email", "description": ""},
                {"name": "read_email"},
                # A tool the category map shadows: /tools enable resolves this
                # spelling to the category, so it must not be offered twice.
                {"name": "email", "description": "shadowed by the category"},
                {"description": "no name: dropped"},
            ],
        }

    async def get_thread_config(self, thread_id: str) -> dict[str, Any]:
        return self.thread_config


def test_resolve_tools_offers_categories_then_tools_with_thread_state() -> None:
    api = _ToolsApi()
    options = run(resolve_tools(_executor(api)))

    ids = [o["id"] for o in options]
    # Categories first (sorted), then the tool names (sorted), and the tool
    # the category map shadows is dropped rather than offered unreachably.
    assert ids == [
        "email",
        "system",
        "bash_execute",
        "read_email",
        "send_email",
        "web_search",
    ]
    by_id = {o["id"]: o for o in options}
    assert by_id["email"]["meta"] == "category, 2 tools"
    assert by_id["system"]["meta"] == "category, 1 tool"
    # Core minus this thread's disable, thread extra on, everything else off.
    assert by_id["bash_execute"]["meta"] == "off, core"
    assert by_id["web_search"]["meta"] == "on, core"
    assert by_id["send_email"]["meta"] == "on"
    assert by_id["read_email"]["meta"] == "off"
    assert by_id["web_search"]["description"] == "Search the web"
    # The tool list is per-user, like the handlers' own read.
    assert api.default_tools_calls == ["alice"]


def test_resolve_tools_counts_live_skill_kit_tools() -> None:
    """TTL'd Skill Kit tools ride temporary_tools, not enabled_tools; the
    resolver folds the still-live ones into the on badge exactly like
    /tools enabled does (the shared live_temporary_tools helper; an inline
    re-derivation here once dropped them)."""
    api = _ToolsApi(
        thread_config={
            "temporary_tools": {
                "read_email": {"expires_at": "2999-01-01T00:00:00+00:00"},
                "send_email": {"expires_at": "2000-01-01T00:00:00+00:00"},
            }
        }
    )
    options = run(resolve_tools(_executor(api)))
    by_id = {o["id"]: o for o in options}
    assert by_id["read_email"]["meta"] == "on"  # live kit tool counts
    assert by_id["send_email"]["meta"] == "off"  # expired kit tool does not


def test_resolve_tools_marks_nothing_current() -> None:
    """Enable state rides meta: a radio preselect would park the cursor on an
    already-enabled tool and mark several options at once."""
    options = run(resolve_tools(_executor(_ToolsApi())))

    assert options
    assert [o["id"] for o in options if o["current"]] == []


def test_resolve_tools_without_a_thread_reports_core_only() -> None:
    options = run(resolve_tools(_executor(_ToolsApi(), thread_id=None)))

    by_id = {o["id"]: o for o in options}
    # No thread, so the overlay is absent: core is on, the thread extra is not.
    assert by_id["bash_execute"]["meta"] == "on, core"
    assert by_id["send_email"]["meta"] == "off"


def test_resolve_tools_keeps_options_when_only_the_thread_read_faults() -> None:
    class _BrokenThreadConfig(_ToolsApi):
        async def get_thread_config(self, thread_id: str) -> dict[str, Any]:
            raise RuntimeError("thread config store down")

    options = run(resolve_tools(_executor(_BrokenThreadConfig())))

    by_id = {o["id"]: o for o in options}
    assert by_id["web_search"]["meta"] == "on, core"
    assert by_id["send_email"]["meta"] == "off"


def test_resolve_tools_survives_a_malformed_category_payload() -> None:
    """A category map that is not a mapping (hand-edited store, older API)
    costs the category options, not the whole picker."""

    class _MalformedCategories(_ToolsApi):
        async def get_tool_categories(self) -> dict[str, Any]:
            return {"categories": ["email", "system"]}

    options = run(resolve_tools(_executor(_MalformedCategories())))

    assert [o["id"] for o in options] == [
        "bash_execute",
        "email",
        "read_email",
        "send_email",
        "web_search",
    ]


def test_resolve_tools_degrades_to_empty_on_listing_fault() -> None:
    class _Broken(_ToolsApi):
        async def get_default_tools(self, user_id: str = "default") -> dict[str, Any]:
            raise RuntimeError("tools service down")

    assert run(resolve_tools(_executor(_Broken()))) == []


# ── resolve_skills ───────────────────────────────────────────────────────────


def _skill(
    name: str,
    *,
    description: str = "",
    is_kit: bool = False,
    is_internal: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        is_skill_kit=is_kit,
        is_internal=is_internal,
        tool_ttl="2h",
    )


class _FakeSkillManager:
    def __init__(self, skills: list[SimpleNamespace]) -> None:
        self.skills = skills
        self.list_calls: list[str | None] = []

    def list_installed(self, user_id: str | None = None) -> list[SimpleNamespace]:
        self.list_calls.append(user_id)
        return list(self.skills)


class _FakeThreadConfigManager:
    def __init__(self, enabled_skills: list[str]) -> None:
        self.enabled_skills = enabled_skills

    def get_config(self, thread_id: str) -> SimpleNamespace:
        return SimpleNamespace(enabled_skills=list(self.enabled_skills))


def _skills_api(
    skills: list[SimpleNamespace], *, active: list[str] | None = None
) -> SimpleNamespace:
    """An api door carrying the in-process agent handle ``_agent()`` reads."""
    return SimpleNamespace(
        agent=SimpleNamespace(
            skill_manager=_FakeSkillManager(skills),
            thread_config_manager=_FakeThreadConfigManager(active or []),
        )
    )


def test_resolve_skills_lists_visible_skills_with_kind_and_activation() -> None:
    api = _skills_api(
        [
            _skill("research", description="Deep research\nmore"),
            _skill("deploy-kit", is_kit=True),
            _skill("internal-helper", is_internal=True),
        ],
        active=["deploy-kit"],
    )
    options = run(resolve_skills(_executor(api)))

    # The internal skill is invisible to /skills list, so it is not offered.
    assert [o["id"] for o in options] == ["research", "deploy-kit"]
    by_id = {o["id"]: o for o in options}
    assert by_id["research"]["meta"] == "skill, inactive"
    assert by_id["deploy-kit"]["meta"] == "kit, active"
    assert by_id["research"]["description"] == "Deep research"
    # Skills are per-user, like the list command's own read.
    assert api.agent.skill_manager.list_calls == ["alice"]


def test_resolve_skills_degrades_to_empty_without_an_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP-client path has no in-process agent, so no skill door."""
    import nymeria.core.agent as agent_module

    monkeypatch.setattr(agent_module, "get_current_agent", lambda: None)

    assert run(resolve_skills(_executor(SimpleNamespace()))) == []


def test_resolve_skills_degrades_to_empty_on_manager_fault() -> None:
    class _BrokenSkillManager(_FakeSkillManager):
        def list_installed(self, user_id: str | None = None) -> list[SimpleNamespace]:
            raise RuntimeError("skill store unreadable")

    api = SimpleNamespace(
        agent=SimpleNamespace(
            skill_manager=_BrokenSkillManager([]),
            thread_config_manager=_FakeThreadConfigManager([]),
        )
    )
    assert run(resolve_skills(_executor(api))) == []


def test_resolve_skills_keeps_options_when_only_the_thread_read_faults() -> None:
    class _BrokenThreadConfigManager(_FakeThreadConfigManager):
        def get_config(self, thread_id: str) -> SimpleNamespace:
            raise RuntimeError("thread config store down")

    api = SimpleNamespace(
        agent=SimpleNamespace(
            skill_manager=_FakeSkillManager([_skill("research")]),
            thread_config_manager=_BrokenThreadConfigManager([]),
        )
    )
    options = run(resolve_skills(_executor(api)))

    assert [o["id"] for o in options] == ["research"]
    assert options[0]["meta"] == "skill, inactive"


# ── resolve_threads ──────────────────────────────────────────────────────────


class _ThreadsApi:
    def __init__(self) -> None:
        self.list_calls: list[str] = []

    async def list_threads(self, user_id: str | None = None) -> list[Any]:
        self.list_calls.append(str(user_id))
        return [
            {
                "thread_id": "thread-1",
                "title": "Active chat",
                "updated_at": "2026-08-01T00:00:00Z",
                "platform": "cli",
            },
            {
                "thread_id": "thread-2",
                "title": "",
                "updated_at": "2026-08-03T00:00:00Z",
            },
            {
                "thread_id": "thread-3",
                "title": "Nightly ops",
                "pinned": True,
                "updated_at": "2026-07-01T00:00:00Z",
                "platform": "discord",
            },
            {"title": "no id: dropped"},
            "not-a-mapping",
        ]


def test_resolve_threads_orders_pinned_first_and_marks_the_active_thread() -> None:
    api = _ThreadsApi()
    options = run(resolve_threads(_executor(api)))

    # Pinned first, then most recently updated, mirroring /thread list.
    assert [o["id"] for o in options] == ["thread-3", "thread-2", "thread-1"]
    assert [o["label"] for o in options] == ["Nightly ops", "New Chat", "Active chat"]
    assert [o["id"] for o in options if o["current"]] == ["thread-1"]
    by_id = {o["id"]: o for o in options}
    assert by_id["thread-3"]["meta"] == "thread-3, discord, pinned"
    assert by_id["thread-2"]["meta"] == "thread-2"
    assert api.list_calls == ["alice"]


def test_resolve_threads_degrades_to_empty_on_listing_fault() -> None:
    class _Broken:
        async def list_threads(self, user_id: str | None = None) -> list[Any]:
            raise RuntimeError("thread store down")

    assert run(resolve_threads(_executor(_Broken()))) == []


# ── resolve_todos ────────────────────────────────────────────────────────────


class _TodosApi:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> list[Any]:
        self.list_calls.append(
            {"user_id": user_id, "filter_status": filter_status, "thread_id": thread_id}
        )
        return [
            {
                "id": "abc12345-full-id",
                "task": "Water the plants",
                "status": "pending",
                "scheduled_for": "2026-08-05T09:00:00Z",
                "recurrence": "1d",
            },
            {"id": "def67890-full-id", "task": "Old chore", "status": "done"},
            {"task": "no id, skipped"},
        ]


def test_resolve_todos_offers_full_ids_across_every_status() -> None:
    api = _TodosApi()
    options = run(resolve_todos(_executor(api)))

    # Full id as the value (the todo_id params resolve exact-or-prefix),
    # task text as the label, and done TODOs included: edit and delete
    # address them too.
    assert [o["id"] for o in options] == ["abc12345-full-id", "def67890-full-id"]
    assert [o["label"] for o in options] == ["Water the plants", "Old chore"]
    by_id = {o["id"]: o for o in options}
    assert by_id["abc12345-full-id"]["meta"] == (
        "abc12345, pending, fires 2026-08-05T09:00, repeats 1d"
    )
    assert by_id["def67890-full-id"]["meta"] == "def67890, done"
    assert api.list_calls == [
        {"user_id": "alice", "filter_status": "all", "thread_id": None}
    ]


def test_resolve_todos_degrades_to_empty_on_listing_fault() -> None:
    class _Broken:
        async def list_todos(self, user_id: str, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("todo store down")

    assert run(resolve_todos(_executor(_Broken()))) == []


# ── resolve_triggers ─────────────────────────────────────────────────────────


class _FakeTriggerManager:
    def __init__(self, by_user: dict[str, list[SimpleNamespace]]) -> None:
        self.by_user = by_user

    def get_triggers(self, user_id: str) -> list[SimpleNamespace]:
        return list(self.by_user.get(user_id, []))


def _trigger(
    trigger_id: str,
    *,
    name: str = "",
    enabled: bool = True,
    source_type: str = "webhook",
    action_type: str = "send_message",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=trigger_id,
        name=name,
        enabled=enabled,
        source_type=source_type,
        action=SimpleNamespace(type=action_type),
    )


def test_resolve_triggers_lists_the_callers_triggers_with_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nymeria.tools.triggers._get_trigger_manager",
        lambda: _FakeTriggerManager(
            {
                "alice": [
                    _trigger("t2", name="Cron B", enabled=False, source_type="rss"),
                    _trigger("t1", name="Webhook A"),
                ],
                "bob": [_trigger("t9", name="Not alice's")],
            }
        ),
    )

    options = run(resolve_triggers(_executor(SimpleNamespace())))

    # Id-ordered like /triggers list, and scoped to the calling user only.
    assert [o["id"] for o in options] == ["t1", "t2"]
    assert [o["label"] for o in options] == ["Webhook A", "Cron B"]
    assert options[0]["meta"] == "enabled, webhook, send_message"
    assert options[1]["meta"] == "disabled, rss, send_message"


def test_resolve_triggers_degrades_to_empty_on_manager_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def get_triggers(self, user_id: str) -> list[SimpleNamespace]:
            raise RuntimeError("trigger store unreadable")

    monkeypatch.setattr("nymeria.tools.triggers._get_trigger_manager", lambda: _Broken())

    assert run(resolve_triggers(_executor(SimpleNamespace()))) == []


# ── resolve_hooks ────────────────────────────────────────────────────────────


class _FakeHookManager:
    def __init__(self, by_user: dict[str, list[SimpleNamespace]]) -> None:
        self.by_user = by_user

    def get_hooks(self, user_id: str) -> list[SimpleNamespace]:
        return list(self.by_user.get(user_id, []))


def _hook(
    hook_id: str,
    *,
    name: str = "",
    event: str = "pre_tool_use",
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(id=hook_id, name=name, event=event, enabled=enabled)


def test_resolve_hooks_lists_the_callers_hooks_with_event_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nymeria.tools.hooks._get_hook_manager",
        lambda: _FakeHookManager(
            {
                "alice": [
                    _hook("bbbb2222", name="Guard writes", enabled=False),
                    _hook("aaaa1111", name="Inject context", event="user_prompt"),
                ],
                "bob": [_hook("cccc3333", name="Not alice's")],
            }
        ),
    )

    options = run(resolve_hooks(_executor(SimpleNamespace())))

    assert [o["id"] for o in options] == ["aaaa1111", "bbbb2222"]
    assert [o["label"] for o in options] == ["Inject context", "Guard writes"]
    assert options[0]["meta"] == "enabled, user_prompt"
    assert options[1]["meta"] == "disabled, pre_tool_use"


def test_resolve_hooks_degrades_to_empty_on_manager_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def get_hooks(self, user_id: str) -> list[SimpleNamespace]:
            raise RuntimeError("hook store unreadable")

    monkeypatch.setattr("nymeria.tools.hooks._get_hook_manager", lambda: _Broken())

    assert run(resolve_hooks(_executor(SimpleNamespace()))) == []


# ── resolve_mcp_servers ──────────────────────────────────────────────────────


class _FakeMCPRegistry:
    def __init__(self, servers: list[SimpleNamespace]) -> None:
        self.servers = servers
        self.reads = 0

    def get_all_servers(self) -> list[SimpleNamespace]:
        self.reads += 1
        return list(self.servers)


def _server(
    server_id: str,
    *,
    name: str = "",
    enabled: bool = True,
    install_status: str = "ready",
    tools: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id,
        name=name,
        enabled=enabled,
        install_status=install_status,
        discovered_tools=[SimpleNamespace(name=f"tool{i}") for i in range(tools)],
    )


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch, registry: Any
) -> Any:
    monkeypatch.setattr(
        "nymeria.core.mcp_servers.get_mcp_server_registry", lambda: registry
    )
    return registry


def test_resolve_mcp_servers_lists_servers_with_state_and_tool_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_registry(
        monkeypatch,
        _FakeMCPRegistry(
            [
                _server("zeta", name="Zeta Server", tools=1),
                _server("alpha", install_status="failed", tools=3),
            ]
        ),
    )

    options = run(resolve_mcp_servers(_executor(SimpleNamespace())))

    assert [o["id"] for o in options] == ["alpha", "zeta"]
    # No name stored: the id doubles as the label, as /mcp list renders it.
    assert [o["label"] for o in options] == ["alpha", "Zeta Server"]
    assert options[0]["meta"] == "failed, 3 tools"
    assert options[1]["meta"] == "ready, 1 tool"


def test_resolve_mcp_servers_is_empty_and_unread_for_a_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/mcp list is admin-only; the option set must not leak the inventory
    (nor even read it) to a caller the dispatcher would refuse."""
    registry = _patch_registry(monkeypatch, _FakeMCPRegistry([_server("alpha")]))

    options = run(resolve_mcp_servers(_executor(SimpleNamespace(), is_admin=False)))

    assert options == []
    assert registry.reads == 0


def test_resolve_mcp_servers_degrades_to_empty_on_registry_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def get_all_servers(self) -> list[SimpleNamespace]:
            raise RuntimeError("registry file unreadable")

    _patch_registry(monkeypatch, _Broken())

    assert run(resolve_mcp_servers(_executor(SimpleNamespace()))) == []


# ── CommandService.resolve_options ───────────────────────────────────────────


def _ctx(thread_id: str | None = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id,
        actor="user",
        surface="cli",
        is_admin=True,
    )


def test_resolve_options_unknown_ref_is_none() -> None:
    service = CommandService()
    assert run(service.resolve_options(_ctx(), "nonesuch", api=_ModelsApi())) is None


def test_resolve_options_resolves_a_known_ref_for_the_caller() -> None:
    service = CommandService()
    options = run(service.resolve_options(_ctx(), "models", api=_ModelsApi()))
    assert options is not None
    assert [o["id"] for o in options][:2] == ["gpt-test", "gpt-next"]
    assert [o["id"] for o in options if o["current"]] == ["gpt-next"]


def test_resolve_options_swallows_a_resolver_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolvers degrade to [] internally by contract, but the endpoint must
    # never 500 a client mid-autocomplete if one slips a raise.
    async def _raiser(executor: Any) -> list[dict[str, Any]]:
        raise RuntimeError("resolver bug")

    monkeypatch.setitem(OPTION_RESOLVERS, "zzraise", _raiser)
    service = CommandService()
    assert run(service.resolve_options(_ctx(), "zzraise", api=_ModelsApi())) == []
