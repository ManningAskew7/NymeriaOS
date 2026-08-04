"""Lifecycle-hooks contract: the frozen boundary everything crosses.

This module defines the retrofit-hostile core of the hooks engine: the event
set, the primitives-only ``HookContext`` handed to hook logic, and the typed
``HookOutcome`` family a hook may return. Getting this right matters because the
same shapes must later structure-clone across an out-of-process sandbox boundary
(the ``nym`` workflow substrate), so nothing here may carry a live agent/thread
object. Reads of cross-event scratch state arrive as a read-only snapshot on the
context; writes go back as ``scratch_patch`` on the outcome (see
``core/hooks/scratch.py``).

Design doc: ``docs/private/plans/lifecycle-hooks.md``. This contract carries the
whole product surface: persisted ``HookDefinition`` records (``core/hook_manager``),
the canned actions (``actions.py``), the store->registry bridge (``bridge.py``),
and the enable model (``core/agent_safety``) all sit on top of it unchanged. The
``nym`` workflow substrate is the remaining deferred consumer.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional


class HookEvent(enum.Enum):
    """The fixed set of lifecycle moments a hook can attach to."""

    PROMPT_SUBMIT = "prompt_submit"   # any turn start (user OR autonomous)
    PRE_TOOL_USE = "pre_tool_use"     # before a single tool call executes
    POST_TOOL_USE = "post_tool_use"   # after a single tool call executes
    DONE = "done"                     # turn finished
    COMMAND_SUBMIT = "command_submit" # before a slash command's handler runs


@dataclass(frozen=True)
class HookProvenance:
    """Loop-guard lineage for a turn.

    Carried on every ``HookContext`` from day one because retrofitting the loop
    guard is painful. ``done_continuation_active`` mirrors Claude Code's
    ``stop_hook_active``; ``continuation_depth`` is the consecutive Done
    continuation count backing the hard cap.
    """

    done_continuation_active: bool = False
    continuation_depth: int = 0


@dataclass(frozen=True)
class HookContext:
    """Primitives-only input handed to hook logic.

    No live objects: this is what keeps the contract cloneable across the future
    sandbox RPC boundary. Which optional fields are populated depends on
    ``event`` (see the per-event comments). ``scratch`` is a read-only snapshot
    of the per-thread scratch store taken at dispatch time.
    """

    event: HookEvent
    thread_id: str
    user_id: str
    is_autonomous: bool
    holder_kind: Optional[str] = None      # interactive|todo|trigger|watchdog|dream|handoff
    trigger_label: Optional[str] = None    # "User Message"/"Scheduled TODO"/trigger name
    provenance: HookProvenance = field(default_factory=HookProvenance)
    scratch: Mapping[str, object] = field(default_factory=dict)
    # Context-usage signal (best-effort, populated by the fire points; None when
    # unknown). ``context_tokens`` is the thread's current context occupancy
    # (fresh mid-turn at the tool events), ``context_limit`` the model's window,
    # ``compact_trigger_tokens`` the resolved auto-compact trigger (None when
    # context_management != auto_compact). Primitive ints so the contract stays
    # cloneable across the future sandbox boundary.
    context_tokens: Optional[int] = None
    context_limit: Optional[int] = None
    compact_trigger_tokens: Optional[int] = None
    # PROMPT_SUBMIT
    prompt: Optional[str] = None
    # PRE/POST_TOOL_USE
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result_text: Optional[str] = None   # POST only
    tool_status: Optional[str] = None        # POST only: "success" | "error"
    # DONE
    completed_normally: Optional[bool] = None
    final_text: Optional[str] = None
    # COMMAND_SUBMIT. ``command`` is the canonical registry path ("tools list"),
    # ``command_display`` the hyphenated user spelling ("tools-list"). The
    # command's argument view rides ``tool_args`` as ``{"rest": <raw arg tail>}``
    # (redacted for secret-bearing commands) so the guardrail actions'
    # ``conditions`` matching works unchanged; ``tool_name`` stays None.
    command: Optional[str] = None
    command_display: Optional[str] = None
    command_category: Optional[str] = None
    command_danger_level: Optional[str] = None
    command_mutates_state: Optional[bool] = None
    command_actor: Optional[str] = None      # "user" | "agent" | "system"
    command_surface: Optional[str] = None    # cli|api|telegram|discord|...
    command_source: Optional[str] = None     # "user" | "agent" | "cli"
    command_is_admin: Optional[bool] = None  # None = unknown (absent in fire data)
    command_via_act_as: Optional[bool] = None


# --- Outcome families: one per event. Returning None == observe/allow. ---
# Every outcome may also carry a ``scratch_patch`` (writes-in-outcome, applied by
# the dispatcher after the hook runs -- never a live handle into the store).


@dataclass
class PromptOutcome:
    """PROMPT_SUBMIT result: text to append to the model-facing tail."""

    inject_context: Optional[str] = None
    scratch_patch: Optional[dict] = None


@dataclass
class PreToolOutcome:
    """Veto-plane result (PRE_TOOL_USE and COMMAND_SUBMIT): allow, deny, or modify.

    On PRE_TOOL_USE ``updated_args`` shallow-merges into the tool call's args.
    On COMMAND_SUBMIT the only recognized key is ``rest`` (the command's raw
    argument tail), honored for SCHEMA'D commands only: the fire point
    re-splits it and the strict binder validates the result, so a bad rewrite
    fails as a usage error, never a silent wrong execution. Schema-less and
    secret-bearing commands ignore rewrites (with a visible note).
    """

    decision: Literal["allow", "deny", "modify"] = "allow"
    reason: Optional[str] = None          # shown to the model on deny
    updated_args: Optional[dict] = None   # on modify
    scratch_patch: Optional[dict] = None
    # Diagnostic context for the execution log / live activity line, never
    # shown to the model. Lets an ALLOW carry its story: "approved by <user>"
    # (require_approval) or a guardrail script-bug warning (run_command exit
    # !=0/2 fails open but should be visible; backlog #74C).
    note: Optional[str] = None


@dataclass
class PostToolOutcome:
    """POST_TOOL_USE result: rewrite the model-visible result or append a note."""

    updated_result_text: Optional[str] = None
    additional_context: Optional[str] = None
    scratch_patch: Optional[dict] = None


@dataclass
class DoneOutcome:
    """DONE result: force a continuation and/or message the user."""

    continue_: bool = False               # force another turn (subject to loop guard)
    reason: Optional[str] = None          # continuation prompt to the model
    user_message: Optional[str] = None    # out-of-band, to the user
    scratch_patch: Optional[dict] = None


HookOutcome = PromptOutcome | PreToolOutcome | PostToolOutcome | DoneOutcome

# Legality map: an outcome must match its event, or the dispatcher drops it.
EVENT_OUTCOME_TYPES: dict[HookEvent, type] = {
    HookEvent.PROMPT_SUBMIT: PromptOutcome,
    HookEvent.PRE_TOOL_USE: PreToolOutcome,
    HookEvent.POST_TOOL_USE: PostToolOutcome,
    HookEvent.DONE: DoneOutcome,
    HookEvent.COMMAND_SUBMIT: PreToolOutcome,
}

# The veto-plane events: dispatch fails CLOSED for these (a hook fault, a
# saturated pool, a reduction error, or hitting the re-entrance depth all
# become a deny). A guardrail that was not evaluated must not silently pass.
VETO_EVENTS = (HookEvent.PRE_TOOL_USE, HookEvent.COMMAND_SUBMIT)
