"""Shared choices_ref option resolvers (backlog #110).

One registry maps each ``choices_ref`` declared in the command catalog to a
live option resolver. These tests pin the resolver contract (form_option
shape, current marking, degrade-to-empty on faults), the registry-vs-catalog
mapping both ways, and the ``CommandService.resolve_options`` entry the
options endpoint rides.
"""

from __future__ import annotations

from typing import Any

from cli_fixtures import run
from nymeria.core.command_option_resolvers import (
    OPTION_RESOLVERS,
    resolve_models,
    resolve_providers,
)
from nymeria.core.command_service import (
    CommandContext,
    CommandService,
    _CommandExecutor,
)

# Declared refs whose resolver is not built yet: consumers fall back to free
# text / no autocomplete for these. Shrink toward empty; never grow without
# a recorded decision (the resolver registry is the point of #110).
_PENDING_REFS: frozenset[str] = frozenset({"skills", "tools"})


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
