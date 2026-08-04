"""Command-dispatch hook seam: the COMMAND_SUBMIT fire point (backlog #134).

The dedicated-seam sibling of ``agent_turn_metadata.py``: ``command_service.py``
calls :func:`fire_command_submit` at ONE point in ``execute()`` (after parsing,
alias resolution, and every access gate; before argument binding and the
handler), and this module owns everything hook-shaped: registry resolution,
context building with secret redaction, dispatch, and outcome application.

Semantics (dev-locked 2026-08-04, plan in ``tmp/134-command-hooks-plan.md``,
durable record in the hooks doc):

- Mutate plane only (``command_submit`` is a veto event: dispatch fails
  CLOSED). A deny returns a ready-made error ``CommandResult``; a modify may
  rewrite ONLY the ``rest`` argument tail of a SCHEMA'D command (re-split,
  then validated by the strict binder; schema-less commands ignore rewrites
  with a note); allow-with-note surfaces as an italic feedback line for
  human actors. Observe-plane hooks (notify/webhook/create_todo) are scheduled
  fire-and-forget AT SUBMISSION.
- Hooks always see the CANONICAL command: user and built-in aliases resolve
  before the gates and before this seam, so no spelling dodges a matcher.
- The access gates run BEFORE the seam: a permissive hook cannot reopen
  ``AGENT_BLOCKED``/``agent_allowed``/``requires_admin``/``blocked_surfaces``.
- Secret redaction happens at CONTEXT BUILD time so every downstream consumer
  (execution log, run_command stdin, run_workflow payload, conditions) sees
  the redacted view: commands with a ``no_echo`` param, and the two
  params-exempt provider secret rails, fire with ``{"rest": "[redacted]"}``
  and their modify outcomes are ignored.
- Registry-resolution failures fail OPEN (no hooks fire), mirroring
  ``NymeriaAgent._hook_registry_for_turn``; hook execution faults inside
  dispatch fail CLOSED per the veto-event fault policy.
- No thread context (``ctx.thread_id`` is None) means only GLOBAL-scope hooks
  fire, and ``once`` sentinels for such fires share one scratch bucket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .hooks.base import HookContext, HookEvent

if TYPE_CHECKING:  # imported lazily at runtime to avoid the service cycle
    from .command_service import CommandContext, CommandDefinition, CommandResult, ParsedCommand

logger = logging.getLogger(__name__)

# The params-exempt secret step rails: their typed tails carry raw API keys or
# OAuth URLs but predate the #129 schema, so the ``no_echo`` predicate cannot
# see them. Kept in lockstep with ``command_form_generation``'s exclusion
# rationale; ids are the dotted registry ids.
_SECRET_RAIL_IDS = frozenset({"provider.setup", "provider.cliproxy"})

_REDACTED = "[redacted]"


@dataclass
class CommandHookOutcome:
    """What the ``execute()`` call site applies after the seam runs."""

    denied: Optional["CommandResult"] = None
    parsed: Optional["ParsedCommand"] = None  # possibly rewritten; None = unchanged
    notes: List[str] = field(default_factory=list)  # human-actor feedback lines


_NOOP = CommandHookOutcome()


def _managers() -> tuple[Any, Any, Any]:
    """Resolve (hook_manager, thread_config_manager, settings).

    Prefers the ambient turn agent's managers (warm caches, one instance);
    outside a turn (the HTTP command route, bots) falls back to module-cached
    instances over the same on-disk stores. Sharing is safe: the hook store is
    fingerprint-cached per read and the execution log's write-behind executor
    is module-level in ``hook_manager`` (one writer thread process-wide).
    """
    from ..config import get_settings

    settings = get_settings()
    from ..tools.utils import current_agent

    agent = current_agent()
    if agent is not None:
        hm = getattr(agent, "hook_manager", None)
        tcm = getattr(agent, "thread_config_manager", None)
        if hm is not None and tcm is not None:
            return hm, tcm, getattr(agent, "settings", settings)
    return _cached_managers(str(settings.data_dir)) + (settings,)


_manager_cache: Dict[str, tuple[Any, Any]] = {}


def _cached_managers(data_dir: str) -> tuple[Any, Any]:
    pair = _manager_cache.get(data_dir)
    if pair is None:
        from pathlib import Path

        from .hook_manager import HookManager
        from .thread_config import ThreadConfigManager

        # setdefault so a concurrent first call keeps ONE canonical pair (a
        # transient duplicate instance is harmless: reads are
        # fingerprint-keyed and the log writer is module-level).
        pair = _manager_cache.setdefault(
            data_dir,
            (HookManager(Path(data_dir)), ThreadConfigManager(Path(data_dir))),
        )
    return pair


def _resolve_registry(user_id: str, thread_id: Optional[str]):
    """Build the user's active command-hook registry, or None for the fast path.

    Mirrors ``NymeriaAgent._hook_registry_for_turn``: per-user store, system
    definitions excluded, scope filter (no thread -> global-scope hooks only),
    the four enable rungs (master switch included), execution recorder
    attached. Never raises; a resolve failure fires nothing (fail open, the
    agent-seam precedent: hook EXECUTION faults still fail closed in dispatch).
    """
    try:
        hm, tcm, settings = _managers()
        from .agent_safety import get_effective_hook_enabled
        from .hook_manager import SYSTEM_HOOK_IDS, make_execution_recorder
        from .hooks import build_registry

        defs = [
            d for d in hm.get_hooks_cached(user_id)
            if d.id not in SYSTEM_HOOK_IDS
            and d.event == HookEvent.COMMAND_SUBMIT.value
            and (d.scope == "global" or (thread_id and d.thread_id == thread_id))
            and get_effective_hook_enabled(
                d, thread_id, thread_config_manager=tcm, settings=settings
            )
        ]
        if not defs:
            return None
        return build_registry(defs, recorder=make_execution_recorder(hm, user_id))
    except Exception:  # noqa: BLE001 - hook resolution must not break dispatch
        logger.error("command hook registry resolve failed", exc_info=True)
        return None


def _is_redacted(definition: "CommandDefinition") -> bool:
    """True when the command's raw args may carry a secret.

    Same predicate family as ``command_form_generation`` (any ``no_echo``
    param), plus the params-exempt provider secret rails the schema cannot
    see.
    """
    if definition.id in _SECRET_RAIL_IDS:
        return True
    return any(getattr(p, "no_echo", False) for p in (definition.params or ()))


def _build_context(
    ctx: "CommandContext",
    definition: "CommandDefinition",
    parsed: "ParsedCommand",
    *,
    redacted: bool,
) -> HookContext:
    args: Dict[str, Any] = (
        {"rest": _REDACTED, "redacted": True} if redacted else {"rest": parsed.rest}
    )
    return HookContext(
        event=HookEvent.COMMAND_SUBMIT,
        thread_id=ctx.thread_id or "",
        user_id=ctx.user_id,
        is_autonomous=False,
        command=" ".join(definition.path),
        command_display=definition.name,
        command_category=definition.category,
        command_danger_level=definition.danger_level,
        command_mutates_state=definition.mutates_state,
        command_actor=ctx.effective_actor,
        command_surface=ctx.effective_surface,
        command_source=ctx.source,
        command_is_admin=ctx.is_admin,
        command_via_act_as=ctx.via_act_as,
        tool_args=args,
    )


def _note_lines(activity: List[Dict[str, Any]]) -> List[str]:
    """Human-feedback lines from the dispatch activity records.

    The PRE reduction deliberately drops per-hook notes, so allow-with-note
    stories (an approval grant, a guardrail script-bug warning) are recovered
    from the per-run activity stream. ONLY those records render: modify
    records are redundant (the seam writes its own "adjusted arguments"
    line), and fault records are engine diagnostics (on a veto event a real
    fault denies with its own copy; the execution log keeps the rest).
    """
    prefix = "allow: "
    lines: List[str] = []
    for record in activity:
        if len(lines) >= 5:
            break
        if str(record.get("status") or "") != "ok":
            continue
        detail = str(record.get("detail") or "").strip()
        if not detail.startswith(prefix):
            continue
        name = str(record.get("name") or "hook")
        lines.append(f"Hook '{name}': {detail[len(prefix):]}"[:300])
    return lines


def apply_hook_notes(result: "CommandResult", notes: List[str]) -> "CommandResult":
    """Append the seam's feedback lines to a command result (human actors).

    One italic line per note, after the result body. Notes are already
    capped (count and length) by the seam; ``data`` and levels pass through
    untouched. The call site skips this entirely for the agent actor.
    """
    if not notes:
        return result
    from .command_service import CommandResult

    lines = "\n".join(f"_{line}_" for line in notes)
    return CommandResult(
        result.success,
        f"{result.markdown}\n\n{lines}",
        result.command,
        level=result.level,
        data=result.data,
    )


async def fire_command_submit(
    ctx: "CommandContext",
    definition: "CommandDefinition",
    parsed: "ParsedCommand",
) -> CommandHookOutcome:
    """Fire COMMAND_SUBMIT for one dispatch; apply the reduced decision.

    Returns a :class:`CommandHookOutcome`: ``denied`` short-circuits the
    dispatch with a ready error result; ``parsed`` (when set) replaces the
    parse record ahead of binding (``rest`` rewrites only); ``notes`` are
    feedback lines the call site appends for human actors. Never raises.
    """
    try:
        registry = _resolve_registry(ctx.user_id, ctx.thread_id)
        if registry is None:
            return _NOOP
        has_mutating = registry.has_mutating(HookEvent.COMMAND_SUBMIT)
        has_observe = registry.has_observe(HookEvent.COMMAND_SUBMIT)
        if not has_mutating and not has_observe:
            return _NOOP
        redacted = _is_redacted(definition)
        hook_ctx = _build_context(ctx, definition, parsed, redacted=redacted)

        from .hooks.base import PreToolOutcome
        from .hooks.dispatch import adispatch, schedule_observe

        outcome: Optional[PreToolOutcome] = None
        activity: List[Dict[str, Any]] = []
        if has_mutating:
            raw = await adispatch(
                HookEvent.COMMAND_SUBMIT, hook_ctx,
                registry=registry, emit=activity.append,
            )
            outcome = raw if isinstance(raw, PreToolOutcome) else None
        if has_observe:
            # Observe = "command was submitted" (fires even when a mutate hook
            # denies: the submission happened; outcomes need a future
            # command_done event).
            schedule_observe(HookEvent.COMMAND_SUBMIT, hook_ctx, registry=registry)

        result = CommandHookOutcome(notes=_note_lines(activity))
        if outcome is None or outcome.decision == "allow":
            return result
        if outcome.decision == "deny":
            reason = (outcome.reason or "").strip()
            from .command_service import CommandResult

            result.denied = CommandResult(
                False,
                (
                    f"**Error:** Command `/{definition.name}` was blocked by a "
                    "lifecycle hook"
                    + (f": {reason}" if reason else ".")
                ),
                definition.name,
                level="error",
            )
            return result
        # modify: honor the ``rest`` key only; re-split so args/rest stay
        # consistent by construction and the strict binder validates the
        # result. Ignored (with visible feedback) for redacted commands AND
        # for schema-less commands (``params is None``): those hand-parse
        # ``(args, rest)`` with no binder, so an unvalidated substitution
        # would be exactly the silent wrong execution the contract rules out.
        updates = outcome.updated_args if isinstance(outcome.updated_args, dict) else {}
        new_rest = updates.get("rest")
        dropped = sorted(str(k) for k in updates if k != "rest")
        if dropped:
            logger.warning(
                "command hook modify ignored unknown keys %s for /%s",
                dropped, definition.name,
            )
        if not isinstance(new_rest, str):
            return result
        if redacted:
            result.notes.append(
                "Hook rewrite ignored (command carries secret arguments)"
            )
            return result
        if definition.params is None:
            result.notes.append(
                "Hook rewrite ignored (command has no declared argument schema)"
            )
            return result
        from .command_service import _split_args

        result.parsed = replace(parsed, rest=new_rest, args=_split_args(new_rest))
        result.notes.append(f"Hook adjusted arguments: {new_rest or '(cleared)'}")
        return result
    except Exception:  # noqa: BLE001 - the seam must never break dispatch
        logger.error("command hook seam failed for /%s", definition.name, exc_info=True)
        return _NOOP
