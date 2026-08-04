"""Live option resolvers for ``choices_ref`` params (backlog #110).

A ``CommandParam`` can name a DYNAMIC value set via ``choices_ref``
("models", "tools", ...). This module is the one registry mapping each ref
to an async resolver that returns the live option set as ``form_option``
dicts (id, label, meta, description, current). Consumers:

- generated picker forms (``command_form_generation``), in-process via the
  live ``_CommandExecutor``;
- ``GET /commands/options/{ref}`` (Discord autocomplete and any future
  palette client), through ``CommandService.resolve_options``;
- the hand-authored ``/model`` and ``/provider`` pickers, which build their
  option lists HERE so a picker and an autocomplete can never drift.

Resolver contract: takes the constructed ``_CommandExecutor`` for the
calling identity (typed under TYPE_CHECKING only: at runtime
``command_service`` imports this module, and the reverse imports stay
function-local to dodge the cycle, the repo's usual idiom), mirrors the
VISIBILITY of the corresponding list command (same doors, same identity),
degrades to ``[]`` on faults, and writes nothing ITSELF. The executor
doors are not write-free, though: ``get_thread_config`` rides
``_require_thread_access``, whose first touch of an unowned thread id
records the caller as owner (the platform's TOFU thread-claim model, the
same behavior every thread-scoped command and ``GET /threads/{id}/config``
already carry). A review recorded rather than removed that ride-along
(2026-08-03): it is the established ownership mechanism, not a new door.
Optional keyword arguments let a handler that already fetched the
underlying data pass it in instead of paying a second read; registry
callers invoke ``resolver(executor)`` bare.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from .command_forms import form_option

if TYPE_CHECKING:
    from .command_service import _CommandExecutor

logger = logging.getLogger(__name__)

OptionResolver = Callable[[Any], Awaitable[list[dict[str, Any]]]]

# The list commands preview a description as its first line, bounded; an
# option's description line is bounded the same way.
_DESCRIPTION_LIMIT = 60


def _first_line(value: Any, *, limit: int = _DESCRIPTION_LIMIT) -> str:
    """The first line of a description, bounded like the list commands'."""
    return str(value or "").split("\n")[0][:limit].strip()


def filter_command_options(
    options: list[dict[str, Any]], *, q: str = "", limit: int = 0
) -> list[dict[str, Any]]:
    """Server-side narrowing for the options endpoint: case-folded substring
    over id, label, AND meta (the predicate remote autocompletes apply
    client-side), then an optional cap. Keeps whole-catalog payloads off
    per-keystroke round trips (Discord autocomplete has a 3s deadline and
    needs at most 25 rows)."""
    needle = (q or "").strip().casefold()
    if needle:
        options = [
            option
            for option in options
            if needle
            in (
                f"{option.get('id', '')} {option.get('label', '')} "
                f"{option.get('meta', '')}"
            ).casefold()
        ]
    if limit > 0:
        options = options[:limit]
    return options


async def resolve_models(
    executor: "_CommandExecutor", *, current: str | None = None
) -> list[dict[str, Any]]:
    """Model ids the active provider lists (best effort, like the picker:
    a provider with no listing endpoint yields no options, never an error).

    ``current`` marks the calling thread's effective model; when not
    supplied it is resolved the way bare ``/model`` reports it (thread
    override, else the global default).
    """
    from .command_service import fmt_tokens

    try:
        models = await executor.api.list_available_models()
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: list_available_models failed", exc_info=True)
        return []
    if current is None:
        current = ""
        try:
            settings = await executor.api.get_settings()
            thread_model = ""
            if executor.thread_id:
                tc = await executor.api.get_thread_config(executor.thread_id)
                thread_model = ((tc or {}).get("llm_config") or {}).get(
                    "model"
                ) or ""
            current = str(thread_model or settings.get("llm_model", "") or "")
        except Exception:  # noqa: BLE001 - current marking is cosmetic.
            logger.debug("options: current-model lookup failed", exc_info=True)
    options: list[dict[str, Any]] = []
    for entry in models or []:
        model_id = str(entry.get("id") or entry.get("name") or "")
        if not model_id:
            continue
        ctx_len = entry.get("context_length") or entry.get("context_window")
        meta = f"{fmt_tokens(ctx_len)} ctx" if ctx_len else ""
        options.append(
            form_option(model_id, meta=meta, current=model_id == current)
        )
    return options


async def resolve_providers(
    executor: "_CommandExecutor",
    *,
    settings: Mapping[str, Any] | None = None,
    status: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Registered API-key provider specs, tier-grouped in the picker's
    order (native, gateway, unverified; registration order within a tier).

    Everything user-facing rides ``meta`` (tier badge, credential status
    for managed providers, unverified-tier notes); the synthetic
    unregistered-active entry is filtered out because it cannot be
    selected anywhere an option set is offered.
    """
    from .command_executor_llm import (
        _TIER_BADGES,
        _TIER_ORDER,
        PROVIDER_SECRET_SETTINGS,
    )
    from ..config.llm_providers import get_llm_provider_spec

    try:
        if settings is None:
            settings = await executor.api.get_settings()
        if status is None:
            status = await executor._provider_status_map()
        entries = executor._provider_entries(settings, status)
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: provider entries failed", exc_info=True)
        return []
    ordered = sorted(
        entries,
        key=lambda entry: (
            _TIER_ORDER.index(entry["tier"])
            if entry["tier"] in _TIER_ORDER
            else len(_TIER_ORDER)
        ),
    )
    options: list[dict[str, Any]] = []
    for entry in ordered:
        if get_llm_provider_spec(str(entry["provider"])) is None:
            continue
        meta_parts = [_TIER_BADGES.get(entry["tier"], str(entry["tier"]))]
        if entry["provider"] in PROVIDER_SECRET_SETTINGS:
            meta_parts.append(str(entry["status"]))
        if entry["notes_for_user"]:
            meta_parts.append(str(entry["notes_for_user"]))
        options.append(
            form_option(
                entry["provider"],
                label=entry["label"],
                meta=" ".join(part for part in meta_parts if part),
                current=bool(entry["active"]),
            )
        )
    return options


async def resolve_fallback_models(
    executor: "_CommandExecutor",
) -> list[dict[str, Any]]:
    """The CONFIGURED fallback chain, in order: the value set of
    ``/fallback remove``. Deliberately not the model catalog (which would
    offer mostly values the handler rejects); an empty chain yields no
    options, so the bare command keeps its usage error."""
    try:
        settings = await executor.api.get_settings()
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: settings read failed", exc_info=True)
        return []
    raw = str(settings.get("llm_fallback_models", "") or "")
    models = [entry.strip() for entry in raw.split(",") if entry.strip()]
    return [
        form_option(model, meta=f"chain position {index}")
        for index, model in enumerate(models, start=1)
    ]


async def resolve_tools(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """Tool names AND category names, categories first.

    Both spellings are offered because that is what the ``/tools`` target
    argument accepts (``tool_or_category``): ``_resolve_tool_names`` checks
    the category map first and the available-tool list second, so a tool
    shadowed by a category name is dropped here rather than offered as an
    option that would resolve to the category.

    Per-thread enable state rides ``meta`` instead of ``current``: many
    tools are on at once, so marking them all would fill a picker with
    selected markers and park its cursor on an already-enabled tool.
    """
    try:
        categories_payload = await executor.api.get_tool_categories()
        categories = (categories_payload or {}).get("categories") or {}
        if not isinstance(categories, Mapping):
            categories = {}
        data = await executor.api.get_default_tools(executor.user_id)
        data = data or {}
        available = data.get("available_tools") or []
        default_names = {str(name) for name in (data.get("default_tools") or [])}
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: tool listing failed", exc_info=True)
        return []

    # The thread's own overlay, exactly as "/tools list" computes it,
    # INCLUDING live Skill Kit / TTL'd tools (the shared helper is the one
    # liveness computation; an inline re-derivation here once dropped them).
    from .command_service import live_temporary_tools

    thread_extras: set[str] = set()
    thread_disabled: set[str] = set()
    live_temp: set[str] = set()
    if executor.thread_id:
        try:
            tc = await executor.api.get_thread_config(executor.thread_id) or {}
            thread_extras = {str(name) for name in (tc.get("enabled_tools") or [])}
            thread_disabled = {str(name) for name in (tc.get("disabled_tools") or [])}
            live_temp = live_temporary_tools(tc)
        except Exception:  # noqa: BLE001 - state marking is cosmetic.
            logger.debug("options: thread tool state lookup failed", exc_info=True)
    active = (default_names | thread_extras | live_temp) - thread_disabled

    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, names in sorted(categories.items()):
        category_name = str(category)
        if not category_name or category_name in seen:
            continue
        seen.add(category_name)
        count = len(names or [])
        options.append(
            form_option(
                category_name,
                meta=f"category, {count} tool{'' if count == 1 else 's'}",
            )
        )
    for entry in sorted(
        (tool for tool in available if isinstance(tool, Mapping)),
        key=lambda tool: str(tool.get("name") or ""),
    ):
        name = str(entry.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        meta_parts = ["on" if name in active else "off"]
        if name in default_names:
            meta_parts.append("core")
        options.append(
            form_option(
                name,
                meta=", ".join(meta_parts),
                description=_first_line(entry.get("description")),
            )
        )
    return options


async def resolve_skills(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """User-activatable skills and kits, the set ``/skills list`` shows.

    Reads the same door (``_visible_slash_skills``, which drops internal
    skills), so an option here is a name ``/skill`` accepts. The agent
    handle is in-process only: an HTTP-client executor has none and the
    option set is empty rather than wrong.

    Thread activation rides ``meta`` for the same reason tools do, and
    because one declaration serves both ``/skills enable`` and
    ``/skills disable``, where a preselection would mean opposite things.
    """
    from .command_service import get_command_service

    try:
        agent = executor._agent()
        if agent is None:
            return []
        service = executor._service or get_command_service()
        skills = service._visible_slash_skills(executor.user_id, agent=agent)
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: skill listing failed", exc_info=True)
        return []

    active: set[str] = set()
    if executor.thread_id:
        try:
            tc = agent.thread_config_manager.get_config(executor.thread_id)
            if tc is not None:
                active = {str(name) for name in (tc.enabled_skills or [])}
        except Exception:  # noqa: BLE001 - state marking is cosmetic.
            logger.debug("options: thread skill state lookup failed", exc_info=True)

    options: list[dict[str, Any]] = []
    for skill in skills or []:
        name = str(getattr(skill, "name", "") or "")
        if not name:
            continue
        kind = "kit" if getattr(skill, "is_skill_kit", False) else "skill"
        status = "active" if name in active else "inactive"
        options.append(
            form_option(
                name,
                meta=f"{kind}, {status}",
                description=_first_line(getattr(skill, "description", "")),
            )
        )
    return options


async def resolve_threads(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """The caller's own threads, ordered the way ``/thread list`` groups
    them (pinned first, then most recently updated).

    The id is the option value because ``/thread switch`` resolves an exact
    id before any title match; the title is the label, and the meta carries
    the short id and platform the list column shows.
    """
    from .command_executor_threads import (
        _compact_id,
        _normalize_thread_id,
        _ordered_threads,
        _thread_title,
    )

    try:
        threads = await executor.api.list_threads(executor.user_id)
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: list_threads failed", exc_info=True)
        return []
    entries = [thread for thread in (threads or []) if isinstance(thread, Mapping)]
    pinned = _ordered_threads([t for t in entries if t.get("pinned")])
    recent = _ordered_threads([t for t in entries if not t.get("pinned")])

    options: list[dict[str, Any]] = []
    for thread in [*pinned, *recent]:
        thread_id = _normalize_thread_id(thread)
        if not thread_id:
            continue
        meta_parts = [_compact_id(thread_id)]
        platform = str(thread.get("platform") or "").strip()
        if platform:
            meta_parts.append(platform)
        if thread.get("pinned"):
            meta_parts.append("pinned")
        options.append(
            form_option(
                thread_id,
                label=_thread_title(thread),
                meta=", ".join(meta_parts),
                current=thread_id == executor.thread_id,
            )
        )
    return options


async def resolve_todos(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """The caller's TODOs across every status, store order (#143).

    The full id is the option value because the ``todo_id`` params resolve
    exact-or-prefix; the task text is the label, and the meta carries the
    short id plus the status/schedule columns ``/todos list`` shows. All
    statuses are listed because edit and delete address done TODOs too.
    """
    try:
        items = await executor.api.list_todos(
            executor.user_id, filter_status="all"
        )
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: list_todos failed", exc_info=True)
        return []

    options: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        todo_id = str(item.get("id") or "")
        if not todo_id:
            continue
        meta_parts = [todo_id[:8], str(item.get("status") or "")]
        scheduled = str(item.get("scheduled_for") or "")
        if scheduled:
            meta_parts.append(f"fires {scheduled[:16]}")
        recurrence = str(item.get("recurrence") or "")
        if recurrence:
            meta_parts.append(f"repeats {recurrence}")
        options.append(
            form_option(
                todo_id,
                label=_first_line(item.get("task") or todo_id),
                meta=", ".join(part for part in meta_parts if part),
            )
        )
    return options


async def resolve_triggers(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """The caller's event triggers, id-ordered like ``/triggers list``.

    Same manager and same user scoping as the list command, so an option is
    a trigger id ``/triggers enable|disable|delete`` will find.
    """
    try:
        manager = executor._trigger_manager()
        triggers = manager.get_triggers(executor.user_id) or []
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: trigger listing failed", exc_info=True)
        return []

    options: list[dict[str, Any]] = []
    for trigger in sorted(triggers, key=lambda item: str(getattr(item, "id", ""))):
        trigger_id = str(getattr(trigger, "id", "") or "")
        if not trigger_id:
            continue
        action = getattr(trigger, "action", None)
        meta_parts = [
            "enabled" if getattr(trigger, "enabled", False) else "disabled",
            str(getattr(trigger, "source_type", "") or ""),
            str(getattr(action, "type", "") or ""),
        ]
        options.append(
            form_option(
                trigger_id,
                label=str(getattr(trigger, "name", "") or "") or trigger_id,
                meta=", ".join(part for part in meta_parts if part),
            )
        )
    return options


async def resolve_hooks(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """The caller's lifecycle hooks, id-ordered like ``/hook list``.

    Reads the shared HookManager singleton the commands use, so the virtual
    system turn-metadata hook is offered here exactly as the list shows it.
    Full ids: ``/hook enable`` also takes a unique prefix, but the full id
    is the spelling that can never be ambiguous.
    """
    try:
        manager = executor._hook_manager()
        hooks = manager.get_hooks(executor.user_id) or []
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: hook listing failed", exc_info=True)
        return []

    options: list[dict[str, Any]] = []
    for hook in sorted(hooks, key=lambda item: str(getattr(item, "id", ""))):
        hook_id = str(getattr(hook, "id", "") or "")
        if not hook_id:
            continue
        meta_parts = [
            "enabled" if getattr(hook, "enabled", False) else "disabled",
            str(getattr(hook, "event", "") or ""),
        ]
        options.append(
            form_option(
                hook_id,
                label=str(getattr(hook, "name", "") or "") or hook_id,
                meta=", ".join(part for part in meta_parts if part),
            )
        )
    return options


async def resolve_mcp_servers(executor: "_CommandExecutor") -> list[dict[str, Any]]:
    """Configured MCP servers, id-ordered like ``/mcp list``.

    The registry is process-wide rather than per-user, and every ``/mcp``
    command is admin-only, so the option set is gated on
    ``_command_offerable``'s admin and agent axes. Its third axis,
    ``blocked_surfaces``, is inert here: the options endpoint builds its
    context without a surface (latent today, no resolver-backed command
    declares one; if one ever does, the endpoint must learn a surface
    parameter before the gate is real). Without the gate, the options
    endpoint would hand a non-admin the server inventory that the command
    itself refuses.
    """
    if not executor._command_offerable("mcp list"):
        return []
    from .mcp_servers import get_mcp_server_registry

    try:
        registry = get_mcp_server_registry()
        servers = registry.get_all_servers() or []
    except Exception:  # noqa: BLE001 - option sets degrade, never block.
        logger.debug("options: mcp server listing failed", exc_info=True)
        return []

    options: list[dict[str, Any]] = []
    for server in sorted(servers, key=lambda item: str(getattr(item, "id", ""))):
        server_id = str(getattr(server, "id", "") or "")
        if not server_id:
            continue
        state = str(getattr(server, "install_status", "") or "") or (
            "enabled" if getattr(server, "enabled", False) else "disabled"
        )
        tool_count = len(getattr(server, "discovered_tools", None) or [])
        options.append(
            form_option(
                server_id,
                label=str(getattr(server, "name", "") or "") or server_id,
                meta=f"{state}, {tool_count} tool{'' if tool_count == 1 else 's'}",
            )
        )
    return options


# Refs without an entry here have no live option set yet: consumers fall
# back to free text (forms) or no autocomplete (Discord). Keep the keys in
# sync with the ``choices_ref`` values declared in ``registry_defaults``;
# ``tests/test_command_option_resolvers.py`` pins the mapping both ways.
OPTION_RESOLVERS: dict[str, OptionResolver] = {
    "models": resolve_models,
    "providers": resolve_providers,
    "fallback_models": resolve_fallback_models,
    "tools": resolve_tools,
    "skills": resolve_skills,
    "threads": resolve_threads,
    "todos": resolve_todos,
    "triggers": resolve_triggers,
    "hooks": resolve_hooks,
    "mcp_servers": resolve_mcp_servers,
}
