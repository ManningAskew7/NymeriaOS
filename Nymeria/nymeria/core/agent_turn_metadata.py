"""Turn-entry metadata as a system lifecycle hook (backlog #66).

The ``[Time:]/[Trigger:]`` block every turn carries on its message tail is a
reserved system hook definition (``hook_manager.SYSTEM_TURN_METADATA_ID``,
action ``turn_metadata``): inspectable, toggleable, gateable, and
template-editable through every hook surface. This module is the FIRE POINT:
``NymeriaAgent._prefix_turn_metadata`` / ``_aprefix_turn_metadata`` delegate
here from both chat paths.

Invariants (spec: ``docs/private/plans/metadata-injection-hook.md``):

- BYTE-IDENTICAL DEFAULT: with no stored override (the pristine state), the
  fast path is the legacy construction itself
  (``agent._get_time_context`` + the same assembly); no dispatch, no pools,
  no new failure modes. Pinned by golden tests in
  ``tests/test_turn_metadata.py``.
- STRIP SYNC: a customized block is re-validated against the actual
  ``agent_history.CONTEXT_PREFIX_PATTERN`` after rendering; a mismatch falls
  back to the built-in block, so custom metadata can never leak into
  compaction/history views.
- NO SILENT LOSS: dispatch faults (error/timeout/saturated/illegal), unbound
  planted definitions, and frame mismatches all fall back to the built-in
  block with a visible entry in the per-user hook execution log. Metadata is
  omitted only DELIBERATELY: the hook disabled (its ``enabled`` flag or a
  per-thread ``hook_overrides`` entry) or an unfired ``fire_conditions`` /
  ``once`` gate.
- CACHE STABILITY: the block stays on the message tail at the prefix
  position on every path; ``AUTONOMOUS_MODE_RULES`` remain the hardcoded
  constant appended after it for autonomous turns.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Recorder statuses that mean the customized hook FAILED (fall back to the
# built-in block), as opposed to a deliberate no-fire ("no_op" / an
# inject-free "ok" from the fire gate's re-arm outcome).
_FALLBACK_STATUSES = frozenset({"error", "timeout", "saturated", "illegal"})

_FALLBACK_NOTE = "fell back to the built-in turn metadata"


def _builtin_block(agent, is_self_invoke: bool, trigger_override: Optional[str]) -> str:
    """The legacy built-in block, via the agent facade (test monkeypatch seam)."""
    return agent._get_time_context(
        is_autonomous=is_self_invoke, trigger_override=trigger_override
    )


def _assemble(metadata: Optional[str], guidance: str, message: str) -> str:
    """Join metadata + autonomous guidance + message, dropping absent parts.

    With both metadata and guidance present this is byte-identical to the
    legacy ``f"{time_context}\\n\\n{guidance}\\n\\n{message}"`` (and the
    two-part variant without guidance).
    """
    head = [part for part in (metadata, guidance) if part]
    if not head:
        return message
    return "\n\n".join(head + [message])


# Lazy stand-in for the virtual (pristine) definition on the enablement gate:
# only ``id`` and ``enabled`` are read, so a full pydantic build per turn is
# unnecessary. The per-thread ``hook_overrides["turn-metadata"]`` toggle must
# work WITHOUT materializing a stored record (the GUI thread toggle writes
# thread config only), which is why the pristine path still resolves
# enablement against this stub.
_PRISTINE_STUB: Optional[Any] = None


def _pristine_stub():
    global _PRISTINE_STUB
    if _PRISTINE_STUB is None:
        import types

        from .hook_manager import SYSTEM_TURN_METADATA_ID

        _PRISTINE_STUB = types.SimpleNamespace(
            id=SYSTEM_TURN_METADATA_ID, enabled=True
        )
    return _PRISTINE_STUB


def _stored_override(agent, user_id: str) -> Tuple[Any, Any]:
    """``(hook_manager, stored_definition_or_None)`` for the system hook.

    Uses the fingerprint-cached raw store view (zero extra I/O on the hot
    path). Any resolve failure reads as pristine, so the built-in block can
    never be lost to a store hiccup.
    """
    hm = getattr(agent, "hook_manager", None)
    if hm is None or not user_id:
        return None, None
    try:
        from .hook_manager import SYSTEM_TURN_METADATA_ID

        for definition in hm.get_hooks_cached(user_id):
            if getattr(definition, "id", None) == SYSTEM_TURN_METADATA_ID:
                return hm, definition
    except Exception:  # noqa: BLE001 - a resolve failure must not break a turn
        logger.debug("turn-metadata override resolve failed", exc_info=True)
        return None, None
    return hm, None


def _system_hook_enabled(agent, definition, thread_id: str) -> bool:
    """Effective enablement (per-thread override -> definition.enabled)."""
    try:
        from .agent_safety import get_effective_system_hook_enabled

        return get_effective_system_hook_enabled(
            definition,
            thread_id,
            thread_config_manager=getattr(agent, "thread_config_manager", None),
        )
    except Exception:  # noqa: BLE001 - a resolve failure must not strip metadata
        return True


def _strippable(block: str) -> bool:
    """True when the rendered block is fully consumed by the history strip.

    Checks against the compiled pattern in ``agent_history`` itself (with the
    ``\\n\\n`` joiner the assembly adds), so the emit side and the strip side
    cannot drift.
    """
    from .agent_history import CONTEXT_PREFIX_PATTERN

    return bool(CONTEXT_PREFIX_PATTERN.fullmatch(block + "\n\n"))


def _tee_recorder(base, slot: Dict[str, Any]):
    """Wrap the standard execution-log recorder, capturing the run status.

    The seam needs the per-run status to tell a FAULT (fall back) from a
    deliberate no-fire (omit); the dispatcher only returns the reduced
    outcome. Fault entries get the fallback note appended so the log tells
    the whole story in one row. Never raises (dispatch's ``_record`` also
    guards the call).
    """

    def _recorder(reg, ctx, *, status: str, detail: str, duration: float) -> None:
        slot["status"] = status
        if status in _FALLBACK_STATUSES:
            detail = f"{detail}; {_FALLBACK_NOTE}" if detail else _FALLBACK_NOTE
        if base is not None:
            base(reg, ctx, status=status, detail=detail, duration=duration)

    return _recorder


def _log_seam_fault(hm, user_id: str, definition, thread_id: str, detail: str) -> None:
    """Write an explicit error entry to the hook execution log (never raises)."""
    try:
        from .hook_manager import HookExecution

        hm.log_execution(
            user_id,
            HookExecution(
                hook_id=getattr(definition, "id", "") or "",
                hook_name=getattr(definition, "name", "") or "",
                event="prompt_submit",
                plane="mutate",
                status="error",
                detail=detail[:300],
                thread_id=thread_id or "",
            ),
        )
    except Exception:  # noqa: BLE001 - logging must never break a turn
        logger.debug("turn-metadata fault logging failed", exc_info=True)


def _prepare_dispatch(agent, hm, definition, thread_id: str, user_id: str):
    """Build the one-definition registry + status slot, or None => built-in.

    ``None`` means the definition could not even be bound (a planted record
    the bridge skipped, or one that landed off the mutate plane); the caller
    falls back and the failure is logged here.
    """
    from .hook_manager import make_execution_recorder
    from .hooks import HookEvent, build_registry

    slot: Dict[str, Any] = {}
    try:
        base = make_execution_recorder(hm, user_id)
    except Exception:  # noqa: BLE001 - recording is best-effort
        base = None
    registry = build_registry([definition], recorder=_tee_recorder(base, slot))
    if not registry.has_mutating(HookEvent.PROMPT_SUBMIT):
        _log_seam_fault(
            hm, user_id, definition, thread_id,
            f"definition could not be bound for turn metadata; {_FALLBACK_NOTE}",
        )
        return None
    return registry, slot


def _metadata_hook_context(
    *,
    thread_id: str,
    user_id: str,
    message: str,
    is_self_invoke: bool,
    trigger_override: Optional[str],
    is_autonomous: bool,
    source: Optional[str],
):
    """The PROMPT_SUBMIT context for the metadata dispatch.

    ``trigger_label`` carries the RESOLVED label (override, else the
    is_self_invoke-keyed default), so the action's ``{trigger}`` var and any
    ``fire_conditions`` on ``trigger_label`` see what the block will say.
    ``is_autonomous`` stays the SOURCE flag, matching every other hook's
    fire-condition semantics. The context-usage signal is deliberately absent
    (computing it costs a full LLM-config resolve; the general PROMPT_SUBMIT
    dispatch that follows still carries it for user hooks).
    """
    from .hooks import HookContext, HookEvent
    from .prompts import resolve_trigger_label

    return HookContext(
        event=HookEvent.PROMPT_SUBMIT,
        thread_id=thread_id,
        user_id=user_id,
        is_autonomous=is_autonomous,
        holder_kind=source,
        trigger_label=resolve_trigger_label(is_self_invoke, trigger_override),
        prompt=message,
    )


def _finalize(
    agent,
    hm,
    definition,
    outcome,
    slot: Dict[str, Any],
    *,
    thread_id: str,
    user_id: str,
    is_self_invoke: bool,
    trigger_override: Optional[str],
) -> Optional[str]:
    """Turn the dispatch result into the metadata block (or a deliberate None).

    Fault status (or nothing recorded at all) -> built-in fallback (the tee
    recorder already carried the note into the log). A clean run with no
    injected text is the fire gate declining -> deliberately no metadata this
    turn. A rendered block that fails the strip frame -> explicit log entry +
    built-in fallback.
    """
    status = slot.get("status")
    if status is None or status in _FALLBACK_STATUSES:
        return _builtin_block(agent, is_self_invoke, trigger_override)
    text = getattr(outcome, "inject_context", None) if outcome is not None else None
    if not text:
        return None
    if _strippable(text):
        return text
    _log_seam_fault(
        hm, user_id, definition, thread_id,
        f"rendered turn metadata did not match the history-strip frame; {_FALLBACK_NOTE}",
    )
    return _builtin_block(agent, is_self_invoke, trigger_override)


def prefix_turn_metadata(
    agent,
    message: str,
    *,
    is_self_invoke: bool,
    trigger_override: Optional[str],
    is_autonomous: bool,
    thread_id: str = "",
    user_id: str = "",
    source: Optional[str] = None,
) -> str:
    """Sync seam (the ``chat()`` path, which runs off the event loop)."""
    from .prompts import get_autonomous_tail_guidance

    hm, definition = _stored_override(agent, user_id)
    if not _system_hook_enabled(agent, definition or _pristine_stub(), thread_id):
        # Deliberately off (the hook's enabled flag, or a per-thread
        # hook_overrides entry, which must work even when pristine).
        metadata: Optional[str] = None
    elif definition is None:
        metadata = _builtin_block(agent, is_self_invoke, trigger_override)
    else:
        prepared = _prepare_dispatch(agent, hm, definition, thread_id, user_id)
        if prepared is None:
            metadata = _builtin_block(agent, is_self_invoke, trigger_override)
        else:
            registry, slot = prepared
            ctx = _metadata_hook_context(
                thread_id=thread_id, user_id=user_id, message=message,
                is_self_invoke=is_self_invoke, trigger_override=trigger_override,
                is_autonomous=is_autonomous, source=source,
            )
            from .hooks import HookEvent, dispatch

            try:
                outcome = dispatch(HookEvent.PROMPT_SUBMIT, ctx, registry=registry)
            except Exception:  # noqa: BLE001 - dispatch never raises; belt and braces
                logger.warning("turn-metadata dispatch failed", exc_info=True)
                outcome, slot = None, {"status": "error"}
            metadata = _finalize(
                agent, hm, definition, outcome, slot,
                thread_id=thread_id, user_id=user_id,
                is_self_invoke=is_self_invoke, trigger_override=trigger_override,
            )
    return _assemble(metadata, get_autonomous_tail_guidance(is_autonomous), message)


async def aprefix_turn_metadata(
    agent,
    message: str,
    *,
    is_self_invoke: bool,
    trigger_override: Optional[str],
    is_autonomous: bool,
    thread_id: str = "",
    user_id: str = "",
    source: Optional[str] = None,
) -> str:
    """Async seam (the ``astream()`` path; never blocks the event loop)."""
    from .prompts import get_autonomous_tail_guidance

    hm, definition = _stored_override(agent, user_id)
    if not _system_hook_enabled(agent, definition or _pristine_stub(), thread_id):
        # Deliberately off (the hook's enabled flag, or a per-thread
        # hook_overrides entry, which must work even when pristine).
        metadata: Optional[str] = None
    elif definition is None:
        metadata = _builtin_block(agent, is_self_invoke, trigger_override)
    else:
        prepared = _prepare_dispatch(agent, hm, definition, thread_id, user_id)
        if prepared is None:
            metadata = _builtin_block(agent, is_self_invoke, trigger_override)
        else:
            registry, slot = prepared
            ctx = _metadata_hook_context(
                thread_id=thread_id, user_id=user_id, message=message,
                is_self_invoke=is_self_invoke, trigger_override=trigger_override,
                is_autonomous=is_autonomous, source=source,
            )
            from .hooks import HookEvent, adispatch

            try:
                outcome = await adispatch(HookEvent.PROMPT_SUBMIT, ctx, registry=registry)
            except Exception:  # noqa: BLE001 - dispatch never raises; belt and braces
                logger.warning("turn-metadata dispatch failed", exc_info=True)
                outcome, slot = None, {"status": "error"}
            metadata = _finalize(
                agent, hm, definition, outcome, slot,
                thread_id=thread_id, user_id=user_id,
                is_self_invoke=is_self_invoke, trigger_override=trigger_override,
            )
    return _assemble(metadata, get_autonomous_tail_guidance(is_autonomous), message)
