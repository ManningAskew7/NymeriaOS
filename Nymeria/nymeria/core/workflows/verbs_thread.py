"""``nym.thread``: a sub-agent turn (plan: "The SDK surface").

Two shapes behind one verb:

- ``id_or_title`` set: a turn in an EXISTING thread, resolved by thread id or
  callable name, via ``thread_agent_executor.invoke`` (``mode="ask"``,
  blocking) or ``.handoff`` (``mode="handoff"``, fire-and-forget receipt).
  The target must be the calling user's own thread AND marked callable (the
  same consent flag the callable-tool path enforces); the callable-lifecycle
  circular guard plus a self-thread guard reject the two deadlock shapes.
  Callable-TEAM visibility is deliberately NOT enforced here (dev decision
  2026-07-23, backlog #97): workflows are admin-approved per revision, so
  ``nym.thread`` keeps owner-wide reach as the sanctioned cross-team
  orchestration escape hatch while ordinary callable tools are team-isolated
  in both directions.
- ``id_or_title`` unset: a FRESH configured thread via the ``spawn_thread``
  tool, invoked with an explicit configurable (no live turn needed). Fresh
  spawns are always blocking (``mode="ask"``); the spawned thread is real and
  inspectable in the UI.

``schema=`` runs the turn normally, then extracts structured JSON from the
reply with the shared structured-output helper (post-hoc extraction,
plan-settled). ``kit=`` activates a skill or Skill Kit on a FRESH spawn
(existing threads take kits via ``nym.threads.configure``).

This module also owns the ``nym.threads.*`` definition verbs (phase 5):

- ``nym.threads.create``: create a configured thread WITHOUT running a turn
  (the ``spawn_thread`` tool's create-without-prompt path). Returns the new
  ``thread_id`` (and ``callable_name``), so create-then-invoke is the
  CANCELLABLE composition: a later ``nym.thread(id_or_title=...)`` turn gets
  the existing-thread abort cascade that an inline fresh-spawn turn cannot
  (the child id is unknown until the spawn tool returns).
- ``nym.threads.configure``: adjust an EXISTING owned thread's config
  (instructions, model, tools enable/disable, kit activation). Addressing by
  id reaches any thread the caller owns; addressing by name resolves among
  the caller's callable threads (only those have names). Config changes
  apply from the thread's next turn.

Blocking dispatches run on a DEDICATED bounded pool (not the loop's default
executor: a sub-turn blocks its OS thread for minutes, and the default pool
is shared with the rest of the API's ``to_thread`` I/O), so a burst of
workflow sub-turns queues here instead of starving the process. On
cancellation (workflow killed or turn aborted) the handler cascades the
abort into the sub-thread so the cascade invariant holds through workflows.

A blocking ask also REGISTERS the caller->target invocation edge (the
``tool_factory`` idiom): ``is_ancestor_invocation`` is a BFS over that graph,
so without the edge a workflow-created cycle (A's workflow asks B, B asks A)
would be invisible to B's guard and deadlock instead of being rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Tuple

from .registry import VerbContext, VerbError, register_verb
from .structured import ainvoke_structured
from .verbs_llm import DEFAULT_LLM_TIER, build_llm_for_thread

logger = logging.getLogger(__name__)

MODES = ("ask", "handoff")

# Sub-turn dispatches only (invoke/handoff/spawn). Quick store reads stay on
# asyncio.to_thread; capping the minutes-long calls here bounds how many
# default-executor-equivalent threads workflows can pin at once.
DISPATCH_POOL_WORKERS = 4
_dispatch_pool_instance: Optional[ThreadPoolExecutor] = None
_dispatch_pool_lock = threading.Lock()


def _dispatch_pool() -> ThreadPoolExecutor:
    global _dispatch_pool_instance
    if _dispatch_pool_instance is None:
        with _dispatch_pool_lock:
            if _dispatch_pool_instance is None:
                _dispatch_pool_instance = ThreadPoolExecutor(
                    max_workers=DISPATCH_POOL_WORKERS,
                    thread_name_prefix="wf-thread-dispatch",
                )
    return _dispatch_pool_instance


async def _run_dispatch(fn, /, *args) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_dispatch_pool(), functools.partial(fn, *args))


def _current_agent() -> Optional[Any]:
    """Seam: the active agent runtime."""
    from ...tools.utils import current_agent

    return current_agent()


def _check_ownership(
    agent: Any, user_id: str, thread_id: str, name: str = "nym.thread"
) -> Optional[str]:
    """Seam: the callable-thread ownership gate (fail-closed, admin rules)."""
    from ...agents.tool_factory import _check_callable_ownership

    return _check_callable_ownership(agent, user_id, name=name, thread_id=thread_id)


def _invoke_thread(
    thread_id: str, task: str, caller_user_id: str, label: str, trigger: str
) -> str:
    """Seam: the blocking sub-agent turn."""
    from ..thread_agent_executor import invoke

    return invoke(thread_id, task, caller_user_id, label, trigger_override=trigger)


def _handoff_thread(
    thread_id: str,
    task: str,
    caller_user_id: str,
    label: str,
    caller_thread_id: str,
    trigger: str,
) -> str:
    """Seam: the fire-and-forget sub-agent turn."""
    from ..thread_agent_executor import handoff

    return handoff(
        thread_id,
        task,
        caller_user_id,
        label,
        caller_thread_id=caller_thread_id,
        trigger_override=trigger,
    )


def _spawn_fresh(spawn_args: dict, config: dict) -> str:
    """Seam: the spawn_thread tool with an explicit configurable.

    Sync on purpose: the caller runs it on the dedicated dispatch pool (the
    tool's own ``.ainvoke`` would put the minutes-long blocking body on the
    loop's DEFAULT executor instead).
    """
    from typing import cast

    from langchain_core.runnables import RunnableConfig

    from ...tools.spawn_thread import spawn_thread

    return cast(str, spawn_thread.invoke(spawn_args, cast(RunnableConfig, config)))


def _error_markered(text: str, *, include_plain: bool) -> Optional[str]:
    """The executor reports failures as marker strings; surface them as errors.

    ``include_plain`` extends the check to bare ``[Error]`` prefixes, which is
    correct ONLY for controlled receipts (handoff validation returns, spawn
    failures). A blocking ask's reply is FREE-FORM sub-agent text: a
    legitimate reply may open with "[Error]" (e.g. quoting a log line), so
    ask replies are checked against the structured marker alone.
    """
    from ..thread_agent_executor import ERROR_MARKER_PREFIX

    stripped = (text or "").strip()
    if stripped.startswith(ERROR_MARKER_PREFIX):
        return stripped
    if include_plain and stripped.startswith("[Error]"):
        return stripped
    return None


def resolve_target_thread(agent: Any, user_id: str, id_or_title: str) -> Tuple[str, str]:
    """Resolve ``id_or_title`` to ``(thread_id, label)``; raises VerbError.

    Exact thread id first, then the user's callable threads by callable name
    (case-insensitive). Ownership is checked with the shared fail-closed gate,
    and the target must be marked callable: that flag is the thread owner's
    consent to programmatic invocation, for workflows exactly as for
    callable-thread tools.
    """
    manager = agent.thread_config_manager
    wanted = str(id_or_title or "").strip()
    if not wanted:
        raise VerbError("id_or_title must be a thread id or a callable thread name")

    tc = manager.get_config(wanted)
    if tc is None:
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        for candidate in manager.list_callable_threads(owned_thread_ids=owned):
            name = str(candidate.callable_name or "")
            if name.casefold() == wanted.casefold():
                tc = candidate
                break
    if tc is None:
        raise VerbError(
            f"no thread matches {wanted!r} (pass a thread id or the callable "
            "name of one of your callable threads)"
        )

    denial = _check_ownership(agent, user_id, tc.thread_id)
    if denial:
        raise VerbError(denial)
    if not getattr(tc, "callable", False):
        raise VerbError(
            f"thread {tc.thread_id!r} is not callable; mark it callable to "
            "allow workflow invocation"
        )
    return tc.thread_id, str(tc.callable_name or tc.thread_id)


async def _run_existing(
    ctx: VerbContext, agent: Any, target: str, label: str, prompt: str, mode: str
) -> str:
    trigger = f"workflow:{ctx.workflow_id}"
    if mode == "handoff":
        return await _run_dispatch(
            _handoff_thread, target, prompt, ctx.user_id, label, ctx.thread_id, trigger
        )
    if target == ctx.thread_id:
        raise VerbError(
            "a blocking nym.thread call into the workflow's own thread would "
            'deadlock on the thread lock; use mode="handoff"'
        )
    if agent.is_ancestor_invocation(ctx.thread_id, target):
        raise VerbError(
            f"thread {label!r} is an ancestor of this workflow's thread and is "
            'waiting on it; a blocking call would deadlock (use mode="handoff")'
        )
    # Register the caller->target edge for the duration of the blocking wait
    # (the tool_factory idiom), so a target that calls BACK toward the
    # workflow's thread is rejected by ITS ancestor guard instead of
    # deadlocking. When the workflow runs outside a live turn the edge is a
    # conservative false positive for that window, which is the safe side.
    agent.register_callable_invocation(ctx.thread_id, target)
    try:
        return await _run_dispatch(
            _invoke_thread, target, prompt, ctx.user_id, label, trigger
        )
    except asyncio.CancelledError:
        # The workflow was killed or the turn aborted mid sub-turn: the abort
        # cascade MUST reach the sub-thread (plan invariant); the underlying
        # OS thread cannot be cancelled, but the cascade stops the turn it runs.
        with contextlib.suppress(Exception):
            agent.abort_with_cascade(target)
        raise
    finally:
        with contextlib.suppress(Exception):
            agent.unregister_callable_invocation(ctx.thread_id, target)


def _build_spawn_args(agent: Any, args: dict) -> dict:
    from ...config.model_tiers import is_thread_tier_alias, split_provider_model

    spawn_args: dict = {"prompt": str(args.get("prompt") or "")}
    if args.get("title") is not None:
        spawn_args["title"] = str(args["title"])
    if args.get("instructions") is not None:
        spawn_args["instructions"] = str(args["instructions"])
    tools = args.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise VerbError("tools must be a list of tool names")
        spawn_args["optional_tools"] = [str(t) for t in tools]
    if args.get("kit") is not None:
        spawn_args["kit"] = str(args["kit"])
    model = str(args.get("model") or "").strip()
    if model:
        if is_thread_tier_alias(model):
            spawn_args["llm_model"] = model
        else:
            provider, model_name = split_provider_model(
                model, agent.settings.llm_provider
            )
            spawn_args["llm_provider"] = provider
            spawn_args["llm_model"] = model_name
    return spawn_args


async def _extract(ctx: VerbContext, agent: Any, text: str, schema: Any) -> Any:
    """Post-hoc extraction: the reply through the structured helper."""
    # ctx.user_id rides along as the credential-owner fallback for unclaimed
    # threads (dev-todo #76), matching the nym.llm wire path.
    llm = await asyncio.to_thread(
        build_llm_for_thread, agent, ctx.thread_id, DEFAULT_LLM_TIER, ctx.user_id
    )
    prompt = (
        "Extract the requested data from this sub-agent reply. Use only what "
        "the reply itself states; ignore bracketed mechanical preamble lines "
        "(e.g. [Spawned]: thread ids, deletion instructions).\n\n"
        f"REPLY:\n{text}"
    )
    return await ainvoke_structured(llm, prompt, schema)


@register_verb(
    "thread",
    ai=True,
    side_effect=True,
    positional=("prompt",),
    description=(
        "Run a prompt in a sub-agent thread: an existing callable thread "
        "(id_or_title=) or a freshly spawned one; schema= extracts JSON."
    ),
)
async def _thread_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise VerbError("nym.thread requires a non-empty prompt")
    mode = str(args.get("mode") or "ask").strip().lower()
    if mode not in MODES:
        raise VerbError(f'mode must be one of {MODES}, got {mode!r}')
    schema = args.get("schema")
    if schema is not None and mode == "handoff":
        raise VerbError(
            'schema= needs the reply, which mode="handoff" does not wait for'
        )

    agent = _current_agent()
    if agent is None:
        raise VerbError("no agent runtime is available for nym.thread")

    id_or_title = args.get("id_or_title")
    if id_or_title is not None:
        if args.get("kit") is not None:
            raise VerbError(
                "kit= applies to fresh spawns only; use "
                "nym.threads.configure(id_or_title, kit=...) for an "
                "existing thread"
            )
        # Resolution reads thread/account stores (disk + SQL); off-loop.
        target, label = await asyncio.to_thread(
            resolve_target_thread, agent, ctx.user_id, str(id_or_title)
        )
        text = await _run_existing(ctx, agent, target, label, prompt, mode)
        # A blocking ask returns FREE-FORM sub-agent text; only the structured
        # marker means failure there. Handoff receipts are controlled strings.
        marker = _error_markered(text, include_plain=(mode == "handoff"))
    else:
        if mode == "handoff":
            raise VerbError(
                'a fresh spawn is always blocking; mode="handoff" needs '
                "id_or_title= (an existing thread)"
            )
        spawn_args = _build_spawn_args(agent, args)
        config = {
            "configurable": {"user_id": ctx.user_id, "thread_id": ctx.thread_id}
        }
        text = await _run_dispatch(_spawn_fresh, spawn_args, config)
        marker = _error_markered(text, include_plain=True)

    if marker is not None:
        raise VerbError(f"sub-agent turn failed: {marker}")
    if schema is not None:
        return await _extract(ctx, agent, text, schema)
    return text


# --- nym.threads.* definition verbs (phase 5) -------------------------------

_SPAWN_ID_RE = re.compile(r"^\[Spawned\]: thread_id=(\S+)", re.MULTILINE)
_SPAWN_CALLABLE_RE = re.compile(r"^Callable as: ([A-Za-z0-9_]+)\(", re.MULTILINE)


def _parse_spawn_receipt(text: str) -> dict:
    """Parse the spawn tool's ``[Spawned]`` receipt into a structured result.

    The preamble opening lines are a stable contract (pinned by the spawn
    preamble tests and by this module's tests); parsing them keeps the verb a
    thin facade over the tool instead of growing a second create path.
    """
    id_match = _SPAWN_ID_RE.search(text or "")
    if not id_match:
        raise VerbError(f"unrecognized spawn receipt: {(text or '')[:200]}")
    callable_match = _SPAWN_CALLABLE_RE.search(text or "")
    return {
        "thread_id": id_match.group(1),
        "callable_name": callable_match.group(1) if callable_match else None,
    }


def _activate_kit(
    agent: Any, thread_id: str, user_id: str, kit: str
) -> Tuple[bool, str]:
    """Seam: the shared skill/kit activation (the /kit and spawn code path)."""
    from ..command_service import activate_skill_kit

    return activate_skill_kit(
        agent=agent,
        thread_id=thread_id,
        user_id=user_id,
        skill_name=kit,
        reason="nym.threads.configure kit activation",
    )


def resolve_owned_thread(
    agent: Any,
    user_id: str,
    id_or_title: str,
    *,
    caller: str = "nym.threads.configure",
) -> Any:
    """Resolve ``id_or_title`` to a ThreadConfig for a thread the caller OWNS.

    Exact thread id first (an owned-but-unconfigured thread gets a fresh
    default config), then the caller's callable threads by name (only
    callable threads have names). Unlike ``resolve_target_thread`` the target
    need NOT be callable: that flag is consent to programmatic INVOCATION,
    not to configuration by its own owner. Shared with the ``team_manage``
    tool (backlog #100 phase 2); ``caller`` labels denial messages.
    """
    from ..thread_config import ThreadConfig

    manager = agent.thread_config_manager
    wanted = str(id_or_title or "").strip()
    if not wanted:
        raise VerbError("id_or_title must be a thread id or a callable thread name")

    tc = manager.get_config(wanted)
    if tc is None:
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        if wanted in owned:
            tc = ThreadConfig(thread_id=wanted)
        else:
            for candidate in manager.list_callable_threads(owned_thread_ids=owned):
                name = str(candidate.callable_name or "")
                if name.casefold() == wanted.casefold():
                    tc = candidate
                    break
    if tc is None:
        raise VerbError(
            f"no thread matches {wanted!r} (pass a thread id you own or the "
            "callable name of one of your callable threads)"
        )
    denial = _check_ownership(agent, user_id, tc.thread_id, name=caller)
    if denial:
        raise VerbError(denial)
    return tc


def _tool_name_list(args: dict, key: str) -> List[str]:
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise VerbError(f"{key} must be a list of tool names")
    return [str(v).strip() for v in value if str(v).strip()]


def _gate_restricted_tools(agent: Any, user_id: str, names: List[str]) -> None:
    """The REST PATCH admin gate: non-admins cannot enable restricted tools.

    Without this a non-admin's workflow could enable reload_all/claude_code
    on a thread and escalate via that thread's next turn, exactly the spawn
    escalation the spawn gate closes.
    """
    user = agent.accounts_repo.get_user_by_id(user_id)
    role = getattr(user, "role", None) or "user"
    if role == "admin":
        return
    from ...tools import ADMIN_ONLY_TOOL_NAMES, DEVELOPER_ONLY_TOOL_NAMES

    blocked = ADMIN_ONLY_TOOL_NAMES.intersection(names)
    if blocked:
        raise VerbError(
            f"admin-only tools cannot be enabled by this user: {sorted(blocked)}"
        )
    blocked = DEVELOPER_ONLY_TOOL_NAMES.intersection(names)
    if blocked:
        raise VerbError(
            "developer-only diagnostic tools cannot be enabled by this "
            f"user: {sorted(blocked)}"
        )


def _apply_team_field(agent: Any, user_id: str, tc: Any, ref: str) -> bool:
    """Resolve ``team=`` (id, name, or "none"/"" to unteam) onto the config.

    Uses the shared membership applier (legacy-name banking + dangling-id
    adoption); an unknown ref raises before anything is written. Returns
    whether the membership actually changed.
    """
    from ..team_manager import apply_team_membership_to_config, resolve_team_ref

    wanted = str(ref or "").strip()
    if wanted.lower() in ("", "none"):
        return apply_team_membership_to_config(agent, user_id, tc, None)
    team = resolve_team_ref(agent, user_id, wanted)
    if team is None:
        raise VerbError(
            f"unknown team {wanted!r}; pass a team id or name (see "
            'team_manage/workflow list surfaces) or "none" to unteam'
        )
    return apply_team_membership_to_config(agent, user_id, tc, team.id)


def _apply_thread_configuration(
    agent: Any, user_id: str, tc: Any, args: dict
) -> List[str]:
    """Mutate and save the config; returns the updated field names.

    Mirrors the load-bearing REST PATCH rules for the fields this verb
    exposes: the admin/developer-only tool gate for non-admins, the 5000-char
    instructions cap (ThreadConfig does not validate on assignment), and
    clearing the active LLM fallback when the model changes. Enabling a tool
    also removes it from disabled_tools (and vice versa): disabled_tools is
    authoritative subtraction at graph build, so a bare add would be a no-op.
    A team change runs the shared cross-thread fan-out (not the single-thread
    invalidation the other fields use).
    """
    from ...config.model_tiers import (
        is_thread_tier_alias,
        resolve_tier,
        split_provider_model,
    )
    from ..thread_config import ThreadLLMConfig

    updated: List[str] = []

    instructions = args.get("instructions")
    if instructions is not None:
        text = str(instructions)
        if len(text) > 5000:
            raise VerbError("instructions must be at most 5000 characters")
        tc.instructions = text or None
        updated.append("instructions")

    model = str(args.get("model") or "").strip()
    if model:
        if is_thread_tier_alias(model):
            resolved = resolve_tier(
                model, agent.settings, provider=agent.settings.llm_provider
            )
            if resolved is None:
                raise VerbError(f"model tier {model!r} could not be resolved")
            provider, model_name = resolved
        else:
            provider, model_name = split_provider_model(
                model, agent.settings.llm_provider
            )
        if tc.llm_config is not None:
            tc.llm_config = tc.llm_config.model_copy(
                update={"provider": provider, "model": model_name}
            )
        else:
            tc.llm_config = ThreadLLMConfig(provider=provider, model=model_name)
        tc.active_llm_fallback = None
        updated.append("model")

    tools_enable = _tool_name_list(args, "tools_enable")
    tools_disable = _tool_name_list(args, "tools_disable")
    both = set(tools_enable) & set(tools_disable)
    if both:
        raise VerbError(
            f"tools cannot be both enabled and disabled: {sorted(both)}"
        )
    if tools_enable:
        _gate_restricted_tools(agent, user_id, tools_enable)
        tc.enabled_tools = sorted(set(tc.enabled_tools or []) | set(tools_enable))
        tc.disabled_tools = [
            t for t in (tc.disabled_tools or []) if t not in set(tools_enable)
        ]
        updated.append("tools_enable")
    if tools_disable:
        tc.disabled_tools = sorted(set(tc.disabled_tools or []) | set(tools_disable))
        tc.enabled_tools = [
            t for t in (tc.enabled_tools or []) if t not in set(tools_disable)
        ]
        updated.append("tools_disable")

    team_ref = args.get("team")
    team_changed = False
    if team_ref is not None:
        team_changed = _apply_team_field(agent, user_id, tc, str(team_ref))
        if team_changed:
            updated.append("team")

    if updated:
        if not agent.thread_config_manager.save_config(tc):
            raise VerbError(f"failed to save config for thread {tc.thread_id!r}")
        with contextlib.suppress(Exception):
            agent.invalidate_thread_config_cache(tc.thread_id)
    if team_changed:
        # Team visibility is cross-thread: fan-out invalidation + search
        # re-tag + GUI nudge via the shared chokepoint (the single-thread
        # invalidation above is subsumed by the fan-out).
        from ..team_manager import after_team_change

        after_team_change(
            agent,
            user_id,
            membership_changed=True,
            team_id=tc.callable_team_id or "",
            reason="membership",
        )
    return updated


@register_verb(
    "threads.create",
    side_effect=True,
    positional=("title",),
    description=(
        "Create a configured thread WITHOUT running a turn (instructions=, "
        "model=, tools=, kit=, callable=, ttl_hours=); returns {thread_id, "
        "callable_name} for later nym.thread calls."
    ),
)
async def _threads_create_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    title = str(args.get("title") or "").strip()
    if not title:
        raise VerbError("threads.create requires a non-empty title")
    if args.get("prompt") is not None:
        raise VerbError(
            "threads.create does not run a turn; create first, then "
            "nym.thread(id_or_title=<thread_id>, prompt=...)"
        )
    agent = _current_agent()
    if agent is None:
        raise VerbError("no agent runtime is available for nym.threads.create")

    spawn_args = _build_spawn_args(agent, args)
    callable_flag = args.get("callable", True)
    if not isinstance(callable_flag, bool):
        raise VerbError("callable must be a boolean")
    spawn_args["make_callable"] = callable_flag
    ttl_hours = args.get("ttl_hours")
    if ttl_hours is not None:
        if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, int) or ttl_hours < 1:
            raise VerbError("ttl_hours must be a positive integer")
        spawn_args["ttl_hours"] = ttl_hours

    config = {"configurable": {"user_id": ctx.user_id, "thread_id": ctx.thread_id}}
    text = await _run_dispatch(_spawn_fresh, spawn_args, config)
    marker = _error_markered(text, include_plain=True)
    if marker is not None:
        raise VerbError(f"thread creation failed: {marker}")
    return _parse_spawn_receipt(text)


@register_verb(
    "threads.configure",
    side_effect=True,
    positional=("id_or_title",),
    description=(
        "Reconfigure one of your threads (instructions=, model=, "
        "tools_enable=, tools_disable=, kit=, team=; team takes a team id or "
        'name, or "none" to unteam); changes apply from the thread\'s next '
        "turn."
    ),
)
async def _threads_configure_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    agent = _current_agent()
    if agent is None:
        raise VerbError("no agent runtime is available for nym.threads.configure")

    kit = args.get("kit")
    has_field = kit is not None or any(
        args.get(field) is not None
        for field in ("instructions", "model", "tools_enable", "tools_disable", "team")
    )
    if not has_field:
        raise VerbError(
            "nothing to configure; pass at least one of instructions=, "
            "model=, tools_enable=, tools_disable=, kit=, team="
        )

    # Resolution + application read/write thread and account stores; off-loop.
    tc = await asyncio.to_thread(
        resolve_owned_thread, agent, ctx.user_id, str(args.get("id_or_title") or "")
    )
    updated = await asyncio.to_thread(
        _apply_thread_configuration, agent, ctx.user_id, tc, args
    )

    if kit is not None:
        # After the config save: activation re-reads the config by thread_id
        # (and creates a default one for an unconfigured owned thread).
        ok, message = await asyncio.to_thread(
            _activate_kit, agent, tc.thread_id, ctx.user_id, str(kit)
        )
        if not ok:
            # Honest partial-apply failure: earlier fields are already saved
            # (owner-only config, so no rollback dance), say so explicitly.
            applied = (
                f" (already applied and kept: {', '.join(updated)})"
                if updated
                else ""
            )
            raise VerbError(f"kit activation failed{applied}: {message}")
        updated.append("kit")

    return {"thread_id": tc.thread_id, "updated": updated}
