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
calling identity (duck-typed; ``command_service`` imports this module, never
the reverse), mirrors the VISIBILITY of the corresponding list command
(same doors, same identity), degrades to ``[]`` on faults, and never
mutates. Optional keyword arguments let a handler that already fetched the
underlying data pass it in instead of paying a second read; registry
callers invoke ``resolver(executor)`` bare.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .command_forms import form_option

logger = logging.getLogger(__name__)

OptionResolver = Callable[[Any], Awaitable[list[dict[str, Any]]]]


async def resolve_models(
    executor: Any, *, current: str | None = None
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
    executor: Any,
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


# Refs without an entry here have no live option set yet: consumers fall
# back to free text (forms) or no autocomplete (Discord). Keep the keys in
# sync with the ``choices_ref`` values declared in ``registry_defaults``;
# ``tests/test_command_option_resolvers.py`` pins the mapping both ways.
OPTION_RESOLVERS: dict[str, OptionResolver] = {
    "models": resolve_models,
    "providers": resolve_providers,
}
