"""Shared `/fast` temporary-model stash and restore protocol.

Both CLI shells (`CLIApp`'s Rich REPL and the legacy full-screen shell) run a
fast turn by stashing the thread's current model, switching it for one turn,
then restoring it. The two shells differ only in how they obtain their thread
and user ids and whether they pass a registry, so they each build their own
`CommandContext` and delegate the actual stash/restore logic here. Keeping the
protocol in one place stops the two copies drifting (a fix to the restore
semantics, e.g. the `clear_llm_config` path, now lives once).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .commands import CommandContext
from .commands._shared import call_client_method, mapping_get


async def apply_temporary_model(
    context: CommandContext, model: str
) -> dict[str, Any]:
    """Switch the thread to ``model`` and return a token for ``restore_temporary_model``.

    Reads the current thread config and the user's default model so the caller
    can report the effective model and undo the change afterwards.
    """

    config = await call_client_method(
        context,
        "get_thread_config",
        context.thread_id,
        user_id=context.user_id,
    )
    settings = await call_client_method(
        context,
        "get_settings",
        user_id=context.user_id,
    )
    llm_config = mapping_get(config, "llm_config", None)
    llm_config_present = isinstance(llm_config, Mapping)
    model_present = llm_config_present and "model" in llm_config
    previous_model = llm_config.get("model") if model_present else None
    default_model = str(mapping_get(settings, "llm_model", "") or "")
    effective_model = str(previous_model or default_model)
    # A one-turn model is not a route choice (#236): with a fallback hold
    # live, both writes keep it, so the hold still wins this turn and the
    # restore leaves the thread exactly as it was. Without one the writes
    # are unchanged (a hold the fast turn itself trips is then ended by the
    # restore's route change, since it held in for the temporary model).
    hold_flag = (
        {"keep_active_fallback": True}
        if mapping_get(config, "active_llm_fallback", None)
        else {}
    )

    await call_client_method(
        context,
        "update_thread_config",
        context.thread_id,
        user_id=context.user_id,
        llm_config={"model": model},
        **hold_flag,
    )
    return {
        "llm_config_present": llm_config_present,
        "model_present": model_present,
        "previous_model": previous_model,
        "effective_model": effective_model,
        **hold_flag,
    }


async def restore_temporary_model(
    context: CommandContext, restore: Mapping[str, Any]
) -> None:
    """Undo `apply_temporary_model`, using the token it returned.

    Clears the thread's llm_config when the thread had none before the fast
    turn, otherwise writes the previously-stashed model back. Keeps a
    fallback hold that was live when the temporary model applied.
    """

    hold_flag = (
        {"keep_active_fallback": True} if restore.get("keep_active_fallback") else {}
    )
    if not bool(restore.get("llm_config_present", False)):
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            clear_llm_config=True,
            **hold_flag,
        )
        return

    model_value = restore.get("previous_model") if restore.get("model_present") else None
    await call_client_method(
        context,
        "update_thread_config",
        context.thread_id,
        user_id=context.user_id,
        llm_config={"model": model_value},
        **hold_flag,
    )
